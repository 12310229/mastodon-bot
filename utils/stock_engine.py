"""
주식 엔진 (재원 / 차성 / 적연) — 시트 기반 저장.

가격은 '자동봇용 정보' 시트의 Q2/Q3/Q4에 저장된다. 관리자가 그 셀을 직접
수정하면 다음 매매/조회에서 즉시 반영 = **주가 조작** 가능.

동작:
- 매매 (buy/sell): 시트에서 현재가 읽음 → 봇 메모리의 매수/매도 카운터 증가.
                   시트에 매매 카운터는 쓰지 않음 (매매마다 write API 호출 없음).
- 갱신 사이클: KST 정각 00/06/12/18 에 봇 메모리의 카운터로 가격 갱신
              → 시트에 batch write → 카운터 리셋.
- 시세 조회 (get_all_snapshots / get_price): 시트에서 즉시 읽음.
- Q5 유지여부: 시트의 Q5 셀 값이 1 이면 그 사이클을 스킵 (동결).

산식:
    base     = uniform(-1, 1)
    pressure = (buys - sells) / (buys + sells + 1)         # ∈ [-1, +1]
    delta    = clamp(base + PRESSURE_WEIGHT × pressure, -1, +1)
    new_price = max(PRICE_FLOOR, round(before × (1 + delta)))

주요 상수:
- PRICE_FLOOR = 1           (절대 최저가)
- UPDATE_KST_HOURS = (0, 6, 12, 18)   (갱신 시각, KST)
- INITIAL_PRICE = 20        (시트가 비어있을 때만 씨앗값)

24h 상승률은 봇 메모리의 히스토리로 계산 (재시작 시 리셋).
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

try:
    import pytz
    _KST = pytz.timezone('Asia/Seoul')
except ImportError:
    pytz = None
    _KST = None

from utils.logging_config import logger
from utils.shared_sheet import (
    STOCK_NAMES,
    clear_stock_previews,
    ensure_stock_sheet_initialized,
    read_stock_hold_switch,
    read_stock_previews,
    read_stock_prices,
    write_stock_previews,
    write_stock_prices,
)


# ======================================================================
# 설정 상수
# ======================================================================
INITIAL_PRICE = 20                    # 시트 비어있을 때만 씨앗값
PRICE_FLOOR = 1                       # 절대 최저가
# 갱신은 KST 절대 시각 00/06/12/18 정각에 실행. 아래 슬롯 리스트로 정의.
UPDATE_KST_HOURS: Tuple[int, ...] = (0, 6, 12, 18)
PREVIEW_LEAD_SECONDS = 30 * 60        # 정각 30분 전에 다음 사이클 예상가 계산 → R열
HISTORY_KEEP_CYCLES = 8               # 6h × 8 = 48h 분량
DAILY_COMPARE_INDEX = 4               # 24h = 4 사이클 전
PRESSURE_WEIGHT = 0.7                 # 매수/매도 압력 가중치 (기존 0.5)

# 연속 하락 → 강제 상승 로직
FORCED_RALLY_THRESHOLD = 4            # N 회 연속 하락 시 다음 사이클 강제 상승
FORCED_RALLY_MIN_DELTA = 0.5          # 강제 상승 시 최소 상승률 (+50%)

# 상장폐지 로직
DELIST_TRIGGER_PRICE = 2              # 강제 상승 결과 이 가격이면 다음 사이클 상장폐지 감시
DELIST_RECOVERY_PRICE = 10            # 상장폐지 후 다음 사이클에 이 가격으로 재상장

# 종목 페이즈
PHASE_NORMAL = 'normal'
PHASE_DELIST_WATCH = 'watch'          # 다음 사이클 매도 비율 ≥50% 시 상장폐지 발동
PHASE_DELISTED = 'delisted'           # 이번 사이클 결과가 0G, 다음 사이클에 재상장


def _next_kst_slot_ts() -> float:
    """다음 KST 정각(00/06/12/18) 의 UTC timestamp 반환.

    지금이 정확히 정각인 경우엔 다음 슬롯으로 넘어감(같은 정각의 중복 발화 방지).
    """
    if _KST is not None:
        now_kst = datetime.now(_KST)
    else:
        # pytz 미설치 (아주 예외적) — 로컬 TZ 를 KST 로 가정.
        now_kst = datetime.now()

    for hour in UPDATE_KST_HOURS:
        candidate = now_kst.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now_kst:
            return candidate.timestamp()

    # 오늘의 모든 슬롯이 지났으면 내일 00:00.
    tomorrow_zero = (now_kst + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return tomorrow_zero.timestamp()


class StockEngine:
    """프로세스 전역 단일 인스턴스."""

    def __init__(self):
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_update_ts: float = 0.0
        self._sheets_manager = None
        self._callback: Optional[Callable] = None

        # 봇 메모리 상태 (재시작 시 리셋)
        self._buys: Dict[str, int] = {n: 0 for n in STOCK_NAMES}
        self._sells: Dict[str, int] = {n: 0 for n in STOCK_NAMES}
        self._history: Dict[str, List[int]] = {n: [] for n in STOCK_NAMES}
        # 연속 하락 카운터 (4회 이상 시 다음 사이클에 강제 상승)
        self._consecutive_drops: Dict[str, int] = {n: 0 for n in STOCK_NAMES}
        # 종목별 페이즈 (정상 / 폐지감시 / 폐지)
        self._phase: Dict[str, str] = {n: PHASE_NORMAL for n in STOCK_NAMES}

    # ------------------------------------------------------------------
    # 시트 I/O 래퍼
    # ------------------------------------------------------------------

    def _read_prices(self) -> Dict[str, int]:
        """시트에서 3종목 가격 읽음. 미설정/None 은 PRICE_FLOOR 로 폴백."""
        if self._sheets_manager is None:
            return {n: PRICE_FLOOR for n in STOCK_NAMES}
        raw = read_stock_prices(self._sheets_manager)
        return {n: (v if v is not None else PRICE_FLOOR) for n, v in raw.items()}

    def _write_prices(self, prices: Dict[str, int]) -> bool:
        if self._sheets_manager is None:
            return False
        return write_stock_prices(self._sheets_manager, prices)

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------

    def get_price(self, stock_name: str) -> Optional[int]:
        if stock_name not in STOCK_NAMES:
            return None
        return self._read_prices().get(stock_name)

    def get_all_snapshots(self) -> List[Tuple[str, int, Optional[float]]]:
        """[(이름, 현재가, 24h 상승률%), ...]."""
        prices = self._read_prices()
        result: List[Tuple[str, int, Optional[float]]] = []
        with self._lock:
            for name in STOCK_NAMES:
                price = prices.get(name, PRICE_FLOOR)
                change = self._change_rate_24h(name, price)
                result.append((name, price, change))
        return result

    def _change_rate_24h(self, name: str, current: int) -> Optional[float]:
        """봇 메모리 히스토리 기반 24h 상승률. 부족 시 None."""
        hist = self._history.get(name) or []
        if len(hist) >= DAILY_COMPARE_INDEX:
            prev = hist[-DAILY_COMPARE_INDEX]
        elif hist:
            prev = hist[0]
        else:
            return None
        if prev == 0:
            return None
        return (current - prev) / prev * 100.0

    def is_valid_stock(self, stock_name: str) -> bool:
        return stock_name in STOCK_NAMES

    def stock_names(self) -> List[str]:
        return list(STOCK_NAMES)

    # ------------------------------------------------------------------
    # 거래
    # ------------------------------------------------------------------

    def buy(self, stock_name: str, quantity: int) -> Optional[Tuple[int, int]]:
        if quantity <= 0 or not self.is_valid_stock(stock_name):
            return None
        prices = self._read_prices()
        price = prices.get(stock_name)
        if price is None:
            return None
        with self._lock:
            self._buys[stock_name] += quantity
        return (price, price * quantity)

    def sell(self, stock_name: str, quantity: int) -> Optional[Tuple[int, int]]:
        if quantity <= 0 or not self.is_valid_stock(stock_name):
            return None
        prices = self._read_prices()
        price = prices.get(stock_name)
        if price is None:
            return None
        with self._lock:
            self._sells[stock_name] += quantity
        return (price, price * quantity)

    # ------------------------------------------------------------------
    # 가격 갱신 (KST 정각 사이클) — 2단계
    #   1) precompute (정각 30분 전): 예상가 계산 → R2:R4 기록.
    #      페이즈 전이·카운터 리셋·시트 초기화(상장폐지 시)도 이때.
    #   2) commit (정각): R2:R4 읽어서 Q2:Q4 에 그대로 복사.
    #      관리자가 30분 사이 R값을 수정하면 그 값이 그대로 반영됨.
    # ------------------------------------------------------------------

    def _precompute_next_cycle(self) -> List[Tuple[str, int, int]]:
        """
        다음 사이클의 예상가를 계산해 R2:R4 에 기록.

        페이즈 전이 / 카운터 리셋 / 히스토리 push / 상장폐지 시 캐릭터 시트
        초기화까지 여기서 확정. 관리자는 R 값(숫자)만 수정 가능하며,
        페이즈 이벤트(폐지·재상장) 는 취소되지 않는다.

        반환: `[(이름, 현재가, 예상가), ...]`.
        """
        # Q5 유지여부 = 1 이면 예상가 = 현재가 (동결).
        if self._sheets_manager is not None and read_stock_hold_switch(self._sheets_manager):
            prices_now = self._read_prices()
            preview = {name: prices_now.get(name, PRICE_FLOOR) for name in STOCK_NAMES}
            for name in STOCK_NAMES:
                self._push_history(name, preview[name])
            self._write_previews(preview)
            logger.info("[stock] 프리뷰(동결): 유지여부(Q5)=1 → 예상가 = 현재가")
            return [(name, preview[name], preview[name]) for name in STOCK_NAMES]

        prices_before = self._read_prices()
        preview: Dict[str, int] = {}
        results: List[Tuple[str, int, int]] = []

        for name in STOCK_NAMES:
            before = prices_before.get(name, PRICE_FLOOR)
            buys = self._buys[name]
            sells = self._sells[name]
            total_volume = buys + sells
            phase = self._phase[name]

            # --- 2) 상장폐지 후 재상장 ---
            if phase == PHASE_DELISTED:
                new_price = DELIST_RECOVERY_PRICE
                self._phase[name] = PHASE_NORMAL
                self._consecutive_drops[name] = 0
                logger.info(f"[stock][프리뷰] {name} 재상장 예상 → {new_price}G")
                preview[name] = new_price
                self._buys[name] = 0
                self._sells[name] = 0
                self._push_history(name, new_price)
                results.append((name, before, new_price))
                continue

            # --- 3) 상장폐지 발동 판정 ---
            if (phase == PHASE_DELIST_WATCH
                    and total_volume > 0
                    and sells * 2 >= total_volume):
                new_price = 0
                self._phase[name] = PHASE_DELISTED
                self._consecutive_drops[name] = 0
                logger.warning(
                    f"[stock][프리뷰] {name} 상장폐지 확정! "
                    f"(매도 {sells} / 총매매 {total_volume}) → 0G"
                )
                self._delist_all_holders(name)
                preview[name] = new_price
                self._buys[name] = 0
                self._sells[name] = 0
                self._push_history(name, new_price)
                results.append((name, before, new_price))
                continue

            # --- 4) 정상 계산 (강제 상승 포함) ---
            base = random.uniform(-1.0, 1.0)
            pressure = (buys - sells) / (total_volume + 1)
            raw_delta = base + PRESSURE_WEIGHT * pressure

            forced_rally = self._consecutive_drops[name] >= FORCED_RALLY_THRESHOLD
            if forced_rally:
                delta = max(FORCED_RALLY_MIN_DELTA, min(1.0, raw_delta))
            else:
                delta = max(-1.0, min(1.0, raw_delta))

            new_price = max(PRICE_FLOOR, int(round(before * (1.0 + delta))))

            if forced_rally:
                self._consecutive_drops[name] = 0
                logger.info(
                    f"[stock][프리뷰] {name} 강제 상승 발동 ({before} → {new_price}, +{delta*100:.0f}%)"
                )
            elif new_price < before:
                self._consecutive_drops[name] += 1
            else:
                self._consecutive_drops[name] = 0

            if phase == PHASE_DELIST_WATCH:
                self._phase[name] = PHASE_NORMAL
                logger.info(f"[stock][프리뷰] {name} 폐지 감시 해제 (매도 부족)")
            if forced_rally and new_price == DELIST_TRIGGER_PRICE:
                self._phase[name] = PHASE_DELIST_WATCH
                logger.info(
                    f"[stock][프리뷰] {name} 폐지 감시 진입 "
                    f"(다음 사이클 매도 ≥50% 시 상장폐지)"
                )

            preview[name] = new_price
            self._buys[name] = 0
            self._sells[name] = 0
            self._push_history(name, new_price)
            results.append((name, before, new_price))

        self._write_previews(preview)
        return results

    def _write_previews(self, prices: Dict[str, int]) -> None:
        if self._sheets_manager is None:
            return
        try:
            ok = write_stock_previews(self._sheets_manager, prices)
            if not ok:
                logger.warning("[stock][프리뷰] R 열 기록 실패 (다음 정각에 재시도 안 됨)")
        except Exception as e:
            logger.warning(f"[stock][프리뷰] R 열 기록 예외: {e}")

    def _commit_from_preview(self) -> List[Tuple[str, int, int]]:
        """
        정각에 R2:R4 → Q2:Q4 복사. 관리자가 수정한 값 그대로 반영.
        반환: `[(이름, before, after), ...]`.
        """
        if self._sheets_manager is None:
            return []

        prices_before = self._read_prices()
        previews = read_stock_previews(self._sheets_manager)

        prices_after: Dict[str, int] = {}
        results: List[Tuple[str, int, int]] = []
        for name in STOCK_NAMES:
            before = prices_before.get(name, PRICE_FLOOR)
            preview = previews.get(name)
            if preview is None:
                # 프리뷰 값이 없으면 현재가 유지 (안전).
                logger.warning(f"[stock][커밋] {name} 예상가 셀이 비어있음 — 현재가 유지")
                after = before
            else:
                # 관리자가 극단값으로 조작했을 수 있음 — floor 만 강제.
                after = max(PRICE_FLOOR, int(preview)) if preview > 0 else int(preview)
            prices_after[name] = after
            results.append((name, before, after))

            # 히스토리 마지막 항목을 실제 커밋된 값으로 갱신 (관리자 조작 반영).
            hist = self._history.setdefault(name, [])
            if hist:
                hist[-1] = after

        ok = self._write_prices(prices_after)
        if not ok:
            logger.warning("[stock][커밋] 시트에 새 가격 저장 실패")

        # R 열은 다음 사이클을 위해 비움.
        try:
            clear_stock_previews(self._sheets_manager)
        except Exception as e:
            logger.debug(f"[stock][커밋] R 열 정리 실패 (무시): {e}")

        self._last_update_ts = time.time()
        return results

    def _apply_cycle_legacy(self) -> List[Tuple[str, int, int]]:
        """[LEGACY] 한 번에 계산+커밋. 현재 스케줄러는 precompute/commit 을 분리
        사용하므로 이 메서드는 force_update_cycle 및 하위 호환용으로만 남아있다.

        처리 순서 (종목마다):
          1) Q5 유지여부 == 1 이면 사이클 전체 스킵 (동결).
          2) 페이즈 == DELISTED → DELIST_RECOVERY_PRICE (=10G) 로 재상장.
          3) 페이즈 == DELIST_WATCH & 이번 사이클 매도 비율 ≥ 50%
             → 상장폐지 (가격 0G, 모든 캐릭터의 보유/투자금 시트에서 0 초기화).
          4) 아니면 정상 계산 — 4회 연속 하락 시 강제 상승 (+50% 이상 보장).
          5) 강제 상승 결과 = 2G 인 경우, 다음 사이클을 위한 폐지 감시 진입.
        """
        # Q5 유지여부 스위치 확인
        if self._sheets_manager is not None and read_stock_hold_switch(self._sheets_manager):
            self._last_update_ts = time.time()
            logger.info("[stock] 유지여부(Q5)=1 — 이번 사이클 스킵 (동결)")
            prices_now = self._read_prices()
            for name in STOCK_NAMES:
                self._push_history(name, prices_now.get(name, PRICE_FLOOR))
            return []

        prices_before = self._read_prices()
        prices_after: Dict[str, int] = {}
        results: List[Tuple[str, int, int]] = []

        for name in STOCK_NAMES:
            before = prices_before.get(name, PRICE_FLOOR)
            buys = self._buys[name]
            sells = self._sells[name]
            total_volume = buys + sells
            phase = self._phase[name]

            # --- 2) 상장폐지 후 재상장 ---
            if phase == PHASE_DELISTED:
                new_price = DELIST_RECOVERY_PRICE
                self._phase[name] = PHASE_NORMAL
                self._consecutive_drops[name] = 0
                logger.info(f"[stock] {name} 재상장 → {new_price}G")
                prices_after[name] = new_price
                self._buys[name] = 0
                self._sells[name] = 0
                self._push_history(name, new_price)
                results.append((name, before, new_price))
                continue

            # --- 3) 상장폐지 발동 판정 ---
            if (phase == PHASE_DELIST_WATCH
                    and total_volume > 0
                    and sells * 2 >= total_volume):  # sells ≥ 50%
                new_price = 0
                self._phase[name] = PHASE_DELISTED
                self._consecutive_drops[name] = 0
                logger.warning(
                    f"[stock] {name} 상장폐지! (매도 {sells} / 총매매 {total_volume}) → 0G"
                )
                self._delist_all_holders(name)
                prices_after[name] = new_price
                self._buys[name] = 0
                self._sells[name] = 0
                self._push_history(name, new_price)
                results.append((name, before, new_price))
                continue

            # --- 4) 정상 계산 (강제 상승 포함) ---
            base = random.uniform(-1.0, 1.0)
            pressure = (buys - sells) / (total_volume + 1)
            raw_delta = base + PRESSURE_WEIGHT * pressure

            forced_rally = self._consecutive_drops[name] >= FORCED_RALLY_THRESHOLD
            if forced_rally:
                delta = max(FORCED_RALLY_MIN_DELTA, min(1.0, raw_delta))
            else:
                delta = max(-1.0, min(1.0, raw_delta))

            new_price = max(PRICE_FLOOR, int(round(before * (1.0 + delta))))

            # --- 하락 카운터 갱신 ---
            if forced_rally:
                self._consecutive_drops[name] = 0
                logger.info(
                    f"[stock] {name} 강제 상승 발동 ({before} → {new_price}, +{delta*100:.0f}%)"
                )
            elif new_price < before:
                self._consecutive_drops[name] += 1
            else:
                self._consecutive_drops[name] = 0

            # --- 5) 페이즈 전이 ---
            # 폐지감시 상태였는데 매도 비율 부족 → 감시 해제
            if phase == PHASE_DELIST_WATCH:
                self._phase[name] = PHASE_NORMAL
                logger.info(f"[stock] {name} 폐지 감시 해제 (매도 부족)")
            # 강제 상승 결과 = 2G면 다음 사이클 폐지 감시
            if forced_rally and new_price == DELIST_TRIGGER_PRICE:
                self._phase[name] = PHASE_DELIST_WATCH
                logger.info(
                    f"[stock] {name} 폐지 감시 진입 (다음 사이클 매도 ≥50% 시 상장폐지)"
                )

            prices_after[name] = new_price
            self._buys[name] = 0
            self._sells[name] = 0
            self._push_history(name, new_price)
            results.append((name, before, new_price))

        ok = self._write_prices(prices_after)
        if not ok:
            logger.warning("[stock] 시트에 새 가격 저장 실패 (다음 사이클에 재시도)")
        self._last_update_ts = time.time()
        return results

    def _push_history(self, name: str, price: int) -> None:
        """히스토리에 push + 사이즈 제한."""
        hist = self._history.setdefault(name, [])
        hist.append(price)
        if len(hist) > HISTORY_KEEP_CYCLES:
            del hist[: len(hist) - HISTORY_KEEP_CYCLES]

    def _delist_all_holders(self, name: str) -> None:
        """상장폐지: '장비 및 주식' 시트에서 모든 캐릭터의 이 종목 주 수/투자금을 0 으로.

        투자금 회수는 불가 (사양). 골드는 이미 매도 시점에 받은 금액이 반영됐으므로
        별도 조정 없음.
        """
        if self._sheets_manager is None:
            return
        # 순환 import 방지 — 함수 내부에서 지연 import.
        from utils.shared_sheet import (
            EQUIP_DATA_START_ROW,
            EQUIP_STOCK_COLS,
            WS_EQUIP_STOCK,
        )
        cols = EQUIP_STOCK_COLS.get(name)
        if cols is None:
            return
        shares_col, invest_col = cols

        try:
            ws = self._sheets_manager.get_worksheet(WS_EQUIP_STOCK)
            all_values = ws.get_all_values()
        except Exception as e:
            logger.warning(f"[stock] 상장폐지 시트 조회 실패 ({name}): {e}")
            return

        updates = []
        for idx, row_values in enumerate(all_values, start=1):
            if idx < EQUIP_DATA_START_ROW:
                continue
            if not row_values:
                continue
            title = (row_values[0] or '').strip() if len(row_values) >= 1 else ''
            if not title:
                continue
            cur_shares = row_values[shares_col - 1] if len(row_values) >= shares_col else ''
            cur_invest = row_values[invest_col - 1] if len(row_values) >= invest_col else ''
            if str(cur_shares).strip() not in ('', '0'):
                updates.append((idx, shares_col, '0'))
            if str(cur_invest).strip() not in ('', '0'):
                updates.append((idx, invest_col, '0'))

        if not updates:
            return
        ok = self._sheets_manager.batch_update_cells(WS_EQUIP_STOCK, updates)
        if ok:
            logger.info(f"[stock] 상장폐지 시트 초기화 ({name}): {len(updates)}셀")
        else:
            logger.warning(f"[stock] 상장폐지 시트 초기화 실패 ({name})")

    def force_update_cycle(self) -> List[Tuple[str, int, int]]:
        """디버깅/수동 트리거용. precompute + commit 을 즉시 순차 실행."""
        with self._lock:
            self._precompute_next_cycle()
            results = self._commit_from_preview()
        for name, before, after in results:
            logger.info(f"[stock] 강제 사이클: {name} {before} → {after}")
        return results

    # ------------------------------------------------------------------
    # 백그라운드 스레드
    # ------------------------------------------------------------------

    def start(
        self,
        sheets_manager,
        post_update_callback: Optional[Callable] = None,
    ) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._sheets_manager = sheets_manager
        self._callback = post_update_callback

        try:
            ensure_stock_sheet_initialized(sheets_manager, INITIAL_PRICE)
        except Exception as e:
            logger.warning(f"[stock] 시트 초기화 실패 (계속 진행): {e}")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name='stock-engine', daemon=True,
        )
        self._thread.start()
        logger.info(
            f"[stock] 백그라운드 스레드 시작 "
            f"(갱신 시각 KST {UPDATE_KST_HOURS}, floor={PRICE_FLOOR}G)"
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.debug("[stock] 백그라운드 스레드 종료")

    def _run_loop(self) -> None:
        """KST 정각(00/06/12/18) 스케줄:
          1) 정각 30분 전에 precompute (예상가 → R2:R4).
          2) 정각에 commit (R → Q 복사).
        stop 이벤트로 즉시 종료 가능.
        """
        while not self._stop_event.is_set():
            next_fire = _next_kst_slot_ts()
            preview_time = next_fire - PREVIEW_LEAD_SECONDS

            # (1) preview 시각까지 대기 (이미 지났으면 즉시 진행)
            if not self._sleep_until(preview_time, log_prefix='다음 프리뷰'):
                break

            # (2) precompute — 예상가 계산 + R 열 기록
            with self._lock:
                preview_results = self._precompute_next_cycle()
            if _KST is not None:
                fire_dt = datetime.fromtimestamp(next_fire, tz=_KST)
                logger.info(
                    f"[stock] 프리뷰 완료 → {fire_dt:%H:%M KST} 커밋 예정. "
                    "R 열을 확인/수정하세요."
                )
            for name, before, after in preview_results:
                logger.info(f"[stock][프리뷰] {name}: {before} → {after}")

            # (3) 정각까지 대기
            if not self._sleep_until(next_fire, log_prefix='다음 커밋'):
                break

            # (4) commit — R2:R4 → Q2:Q4
            with self._lock:
                results = self._commit_from_preview()
            for name, before, after in results:
                logger.info(f"[stock][커밋] {name}: {before} → {after}")

            if self._callback:
                try:
                    self._callback(results)
                except Exception as e:
                    logger.warning(f"[stock] post_update_callback 실패: {e}")

    def _sleep_until(self, target_ts: float, log_prefix: str = '') -> bool:
        """target_ts 까지 짧은 sleep 반복. stop 이벤트로 즉시 종료. 반환: False=stop, True=완료."""
        now = time.time()
        wait_s = target_ts - now
        if wait_s <= 0:
            return not self._stop_event.is_set()

        if wait_s > 60 and _KST is not None and log_prefix:
            dt = datetime.fromtimestamp(target_ts, tz=_KST)
            logger.info(
                f"[stock] {log_prefix} 대기 → {dt:%Y-%m-%d %H:%M KST} "
                f"({int(wait_s // 60)}분 후)"
            )

        slept = 0.0
        while slept < wait_s and not self._stop_event.is_set():
            step = min(0.5, wait_s - slept)
            time.sleep(step)
            slept += step
        return not self._stop_event.is_set()


# ======================================================================
# 전역 싱글톤
# ======================================================================

_global_engine: Optional[StockEngine] = None
_global_lock = threading.Lock()


def get_stock_engine() -> StockEngine:
    global _global_engine
    if _global_engine is None:
        with _global_lock:
            if _global_engine is None:
                _global_engine = StockEngine()
    return _global_engine
