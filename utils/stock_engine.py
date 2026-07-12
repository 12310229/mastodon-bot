"""
주식 엔진 (재원 / 차성 / 적연) — 시트 기반 저장 + '동물의 숲' 형 일일 변동 패턴.

시트 배치 ('자동봇용 정보'):
    P열: 종목명 (재원/차성/적연)      — 2~4행
    Q열: 현재가                       — 관리자 수정 = 즉시 반영 (주가 조작)
    R열: 다음 예상가                  — 각 변동 30분 전 봇이 기록. 관리자가 수정하면
                                        정각 커밋 때 그 값이 그대로 반영됨.
    S열: 오늘의 변동형                — 매일 첫 변동(06:00)에서 재추첨. 관리자가
                                        수정하면 다음 변동부터 그 패턴을 따름.
    Q5 : 유지여부                     — 1 이면 해당 변동 스킵 (가격 동결).

변동 스케줄 (KST): 06:00(1번) → 12:00(2번) → 18:00(3번) → 00:00(4번).
하루 총 4번의 변동 중 마지막(익일 00:00)은 전일 패턴의 마지막 변동으로 취급
("1번은 전일과 겹친다").

변동형 4종 — 매일 06:00 변동에서 확률 재추첨, 전일과 같은 형은 다시 나오지 않음:
    하락형   : 매 변동 -25%
    상승형   : 매 변동 +20%
    계단형   : ÷1.5 ↔ ×1.5 반복 (1·3번째 하락, 2·4번째 상승)
    봉우리형 : 가격 유지, 유지, 3번째 변동에서 +100%, 유지

매수/매도 압력:
    pressure       = (buys - sells) / (buys + sells + 1)   ∈ (-1, +1)
    pressure_delta = 0.2 × pressure                        ∈ (-20%, +20%)
    total_delta    = max(-70%, pattern_delta + pressure_delta)
    new_price      = max(5, round(before × (1 + total_delta)))

[주식 확인] 의 상승률은 직전 변동(6시간 전) 대비. 봇 재시작 직후 첫 변동
전까지는 비교 데이터 없음으로 표시.
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
    read_stock_patterns,
    read_stock_previews,
    read_stock_prices,
    write_stock_patterns,
    write_stock_previews,
    write_stock_prices,
)


# ======================================================================
# 설정 상수
# ======================================================================
INITIAL_PRICE = 20                    # 시트 비어있을 때만 씨앗값
PRICE_FLOOR = 5                       # 절대 최저가
UPDATE_KST_HOURS: Tuple[int, ...] = (0, 6, 12, 18)
PREVIEW_LEAD_SECONDS = 30 * 60        # 각 변동 30분 전에 예상가 → R열

# 변동형
PATTERN_DOWN = '하락형'
PATTERN_UP = '상승형'
PATTERN_STAIR = '계단형'
PATTERN_PEAK = '봉우리형'
PATTERNS: Tuple[str, ...] = (PATTERN_DOWN, PATTERN_UP, PATTERN_STAIR, PATTERN_PEAK)

# 변동 번호 (KST 시각 → 하루 안에서 몇 번째 변동인지)
# 06:00 = 1번 (패턴 재추첨), 12:00 = 2번, 18:00 = 3번, 00:00 = 4번 (전일 패턴 마지막)
_VAR_INDEX_BY_HOUR: Dict[int, int] = {6: 1, 12: 2, 18: 3, 0: 4}

PRESSURE_MAX_DELTA = 0.20   # 매수/매도 압력이 delta 에 기여하는 한도 (±20%)
TOTAL_DELTA_FLOOR = -0.70   # 한 변동에서의 총 하락 한계 (-70%)


def _pattern_base_delta(pattern: str, var_idx: int) -> float:
    """변동형 × 변동 번호 → 기본 delta."""
    if pattern == PATTERN_DOWN:
        return -0.25
    if pattern == PATTERN_UP:
        return 0.20
    if pattern == PATTERN_STAIR:
        # 하락-상승 반복: 1·3번째 ÷1.5 (-33.3%), 2·4번째 ×1.5 (+50%)
        return (1.0 / 1.5 - 1.0) if var_idx % 2 == 1 else 0.5
    if pattern == PATTERN_PEAK:
        return 1.0 if var_idx == 3 else 0.0
    # 알 수 없는 패턴 (시트에 오타 등) — 변동 없음
    return 0.0


def _next_kst_slot_ts() -> float:
    """다음 KST 정각(00/06/12/18) 의 UTC timestamp 반환."""
    if _KST is not None:
        now_kst = datetime.now(_KST)
    else:
        now_kst = datetime.now()

    for hour in UPDATE_KST_HOURS:
        candidate = now_kst.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now_kst:
            return candidate.timestamp()

    tomorrow_zero = (now_kst + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return tomorrow_zero.timestamp()


def _variation_index_for(fire_ts: float) -> int:
    """fire 시각(KST) → 변동 번호 (1~4)."""
    if _KST is not None:
        dt = datetime.fromtimestamp(fire_ts, tz=_KST)
    else:
        dt = datetime.fromtimestamp(fire_ts)
    return _VAR_INDEX_BY_HOUR.get(dt.hour, 1)


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
        # 직전 변동 이전의 가격 (6h 대비 상승률 계산용)
        self._prev_prices: Dict[str, int] = {}

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
        """[(이름, 현재가, 직전 변동 대비 상승률%), ...]."""
        prices = self._read_prices()
        result: List[Tuple[str, int, Optional[float]]] = []
        with self._lock:
            for name in STOCK_NAMES:
                price = prices.get(name, PRICE_FLOOR)
                prev = self._prev_prices.get(name)
                if prev is None or prev == 0:
                    change = None
                else:
                    change = (price - prev) / prev * 100.0
                result.append((name, price, change))
        return result

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
    # 변동형 결정
    # ------------------------------------------------------------------

    def _resolve_patterns(self, var_idx: int) -> Dict[str, str]:
        """
        오늘 적용할 변동형을 결정.

        - var_idx == 1 (06:00 변동): 재추첨. 시트 S 열의 현재 값(=전일 패턴)과
          다른 형 중에서 균등 확률로 선정 후 S 열에 기록.
        - 그 외: 시트 S 열의 값을 그대로 사용 (관리자가 중간에 수정 가능).
          비어있거나 알 수 없는 값이면 임의 선정 후 기록.
        """
        current = read_stock_patterns(self._sheets_manager) if self._sheets_manager else {}

        if var_idx == 1:
            new_patterns: Dict[str, str] = {}
            for name in STOCK_NAMES:
                prev = current.get(name)
                choices = [p for p in PATTERNS if p != prev]
                new_patterns[name] = random.choice(choices)
            if self._sheets_manager:
                write_stock_patterns(self._sheets_manager, new_patterns)
            logger.info(f"[stock] 오늘의 변동형 추첨: {new_patterns}")
            return new_patterns

        resolved: Dict[str, str] = {}
        to_fill: Dict[str, str] = {}
        for name in STOCK_NAMES:
            p = current.get(name)
            if p not in PATTERNS:
                p = random.choice(PATTERNS)
                to_fill[name] = p
                logger.warning(
                    f"[stock] {name} 변동형이 비어있거나 잘못됨 — '{p}' 로 임시 선정"
                )
            resolved[name] = p
        if to_fill and self._sheets_manager:
            write_stock_patterns(self._sheets_manager, to_fill)
        return resolved

    # ------------------------------------------------------------------
    # 가격 갱신 — 2단계 (precompute 30분 전 / commit 정각)
    # ------------------------------------------------------------------

    def _precompute_next_cycle(self, fire_ts: float) -> List[Tuple[str, int, int]]:
        """
        다음 변동의 예상가를 계산해 R2:R4 에 기록.

        fire_ts (커밋 시각) 로 변동 번호(1~4) 를 결정하고, 1번이면 변동형 재추첨.
        매수/매도 카운터는 여기서 소진(리셋)된다 — 즉 압력 집계 마감은 30분 전.

        반환: `[(이름, 현재가, 예상가), ...]`
        """
        var_idx = _variation_index_for(fire_ts)

        # Q5 유지여부 = 1 → 예상가 = 현재가 (동결). 카운터는 유지(누적).
        if self._sheets_manager is not None and read_stock_hold_switch(self._sheets_manager):
            prices_now = self._read_prices()
            preview = {name: prices_now.get(name, PRICE_FLOOR) for name in STOCK_NAMES}
            self._write_previews(preview)
            logger.info("[stock] 프리뷰(동결): 유지여부(Q5)=1 → 예상가 = 현재가")
            return [(name, preview[name], preview[name]) for name in STOCK_NAMES]

        patterns = self._resolve_patterns(var_idx)
        prices_before = self._read_prices()
        preview: Dict[str, int] = {}
        results: List[Tuple[str, int, int]] = []

        for name in STOCK_NAMES:
            before = prices_before.get(name, PRICE_FLOOR)
            pattern = patterns[name]
            base = _pattern_base_delta(pattern, var_idx)

            buys = self._buys[name]
            sells = self._sells[name]
            pressure = (buys - sells) / (buys + sells + 1)
            pressure_delta = max(
                -PRESSURE_MAX_DELTA,
                min(PRESSURE_MAX_DELTA, PRESSURE_MAX_DELTA * pressure),
            )

            total_delta = max(TOTAL_DELTA_FLOOR, base + pressure_delta)
            new_price = max(PRICE_FLOOR, int(round(before * (1.0 + total_delta))))

            preview[name] = new_price
            self._buys[name] = 0
            self._sells[name] = 0
            results.append((name, before, new_price))
            logger.info(
                f"[stock][프리뷰] {name} [{pattern} {var_idx}번째] "
                f"base={base*100:+.1f}% pressure={pressure_delta*100:+.1f}% "
                f"→ {before} → {new_price}"
            )

        self._write_previews(preview)
        return results

    def _write_previews(self, prices: Dict[str, int]) -> None:
        if self._sheets_manager is None:
            return
        try:
            ok = write_stock_previews(self._sheets_manager, prices)
            if not ok:
                logger.warning("[stock][프리뷰] R 열 기록 실패")
        except Exception as e:
            logger.warning(f"[stock][프리뷰] R 열 기록 예외: {e}")

    def _commit_from_preview(self) -> List[Tuple[str, int, int]]:
        """
        정각에 R2:R4 → Q2:Q4 복사. 관리자가 30분 사이 수정한 값 그대로 반영.
        최저가 5G 는 커밋 시에도 강제.
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
                logger.warning(f"[stock][커밋] {name} 예상가 셀이 비어있음 — 현재가 유지")
                after = before
            else:
                after = max(PRICE_FLOOR, int(preview))
            prices_after[name] = after
            results.append((name, before, after))

        ok = self._write_prices(prices_after)
        if not ok:
            logger.warning("[stock][커밋] 시트에 새 가격 저장 실패")

        with self._lock:
            for name, before, _after in results:
                self._prev_prices[name] = before

        try:
            clear_stock_previews(self._sheets_manager)
        except Exception as e:
            logger.debug(f"[stock][커밋] R 열 정리 실패 (무시): {e}")

        self._last_update_ts = time.time()
        return results

    def force_update_cycle(self) -> List[Tuple[str, int, int]]:
        """디버깅/수동 트리거용. precompute + commit 즉시 순차 실행."""
        fire_ts = _next_kst_slot_ts()
        with self._lock:
            self._precompute_next_cycle(fire_ts)
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
            f"(변동 시각 KST {UPDATE_KST_HOURS}, floor={PRICE_FLOOR}G, "
            f"패턴={'/'.join(PATTERNS)})"
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.debug("[stock] 백그라운드 스레드 종료")

    def _run_loop(self) -> None:
        """KST 정각(00/06/12/18) 스케줄:
          1) 정각 30분 전 precompute (예상가 → R2:R4)
          2) 정각 commit (R → Q 복사)
        """
        while not self._stop_event.is_set():
            next_fire = _next_kst_slot_ts()
            preview_time = next_fire - PREVIEW_LEAD_SECONDS

            if not self._sleep_until(preview_time, log_prefix='다음 프리뷰'):
                break

            with self._lock:
                preview_results = self._precompute_next_cycle(next_fire)
            if _KST is not None:
                fire_dt = datetime.fromtimestamp(next_fire, tz=_KST)
                logger.info(
                    f"[stock] 프리뷰 완료 → {fire_dt:%H:%M KST} 커밋 예정. "
                    "R 열을 확인/수정하세요."
                )
            for name, before, after in preview_results:
                logger.info(f"[stock][프리뷰] {name}: {before} → {after}")

            if not self._sleep_until(next_fire, log_prefix='다음 커밋'):
                break

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
        """target_ts 까지 짧은 sleep 반복. 반환: False=stop 요청, True=완료."""
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
