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
    ensure_stock_sheet_initialized,
    read_stock_hold_switch,
    read_stock_prices,
    write_stock_prices,
)


# ======================================================================
# 설정 상수
# ======================================================================
INITIAL_PRICE = 20                    # 시트 비어있을 때만 씨앗값
PRICE_FLOOR = 1                       # 절대 최저가
# 갱신은 KST 절대 시각 00/06/12/18 정각에 실행. 아래 슬롯 리스트로 정의.
UPDATE_KST_HOURS: Tuple[int, ...] = (0, 6, 12, 18)
HISTORY_KEEP_CYCLES = 8               # 6h × 8 = 48h 분량
DAILY_COMPARE_INDEX = 4               # 24h = 4 사이클 전
PRESSURE_WEIGHT = 0.5


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
    # 가격 갱신 (KST 정각 사이클)
    # ------------------------------------------------------------------

    def _apply_cycle(self) -> List[Tuple[str, int, int]]:
        """한 사이클 실행. `[(이름, before, after), ...]`. 락 보유 전제.

        Q5 유지여부 셀 값이 1 이면 사이클 전체를 스킵 (가격 그대로, 카운터도 유지).
        관리자가 시장을 얼어붙게 하고 싶을 때 사용.
        """
        # Q5 유지여부 스위치 확인
        if self._sheets_manager is not None and read_stock_hold_switch(self._sheets_manager):
            self._last_update_ts = time.time()
            logger.info("[stock] 유지여부(Q5)=1 — 이번 사이클 스킵 (동결)")
            # 카운터는 리셋하지 않음: 압력이 다음 사이클로 누적됨.
            # 히스토리에는 현재가를 다시 push (24h 상승률 계산 시 시간축 유지).
            prices_now = self._read_prices()
            for name in STOCK_NAMES:
                hist = self._history.setdefault(name, [])
                hist.append(prices_now.get(name, PRICE_FLOOR))
                if len(hist) > HISTORY_KEEP_CYCLES:
                    del hist[: len(hist) - HISTORY_KEEP_CYCLES]
            return []  # 갱신 없음 표시 (post_update_callback 도 빈 리스트 수신)

        prices_before = self._read_prices()
        prices_after: Dict[str, int] = {}
        results: List[Tuple[str, int, int]] = []
        for name in STOCK_NAMES:
            before = prices_before.get(name, PRICE_FLOOR)
            buys = self._buys[name]
            sells = self._sells[name]
            base = random.uniform(-1.0, 1.0)
            total = buys + sells
            pressure = (buys - sells) / (total + 1)
            delta = max(-1.0, min(1.0, base + PRESSURE_WEIGHT * pressure))
            new_price = max(PRICE_FLOOR, int(round(before * (1.0 + delta))))
            prices_after[name] = new_price

            hist = self._history.setdefault(name, [])
            hist.append(new_price)
            if len(hist) > HISTORY_KEEP_CYCLES:
                del hist[: len(hist) - HISTORY_KEEP_CYCLES]

            self._buys[name] = 0
            self._sells[name] = 0
            results.append((name, before, new_price))

        ok = self._write_prices(prices_after)
        if not ok:
            logger.warning("[stock] 시트에 새 가격 저장 실패 (다음 사이클에 재시도)")
        self._last_update_ts = time.time()
        return results

    def force_update_cycle(self) -> List[Tuple[str, int, int]]:
        """디버깅/수동 트리거용."""
        with self._lock:
            results = self._apply_cycle()
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
        """KST 정각(00/06/12/18) 마다 사이클 실행. stop 이벤트로 즉시 종료 가능."""
        while not self._stop_event.is_set():
            next_fire = _next_kst_slot_ts()
            now = time.time()
            wait_s = max(1.0, next_fire - now)

            # 로그로 다음 갱신 시각 알림 (긴 대기의 경우만)
            if wait_s > 60 and _KST is not None:
                next_dt = datetime.fromtimestamp(next_fire, tz=_KST)
                logger.info(
                    f"[stock] 다음 갱신 대기 → {next_dt:%Y-%m-%d %H:%M KST} "
                    f"({int(wait_s // 60)}분 후)"
                )

            # 짧은 sleep 반복 — stop 응답성 확보
            slept = 0.0
            while slept < wait_s and not self._stop_event.is_set():
                step = min(0.5, wait_s - slept)
                time.sleep(step)
                slept += step

            if self._stop_event.is_set():
                break

            with self._lock:
                results = self._apply_cycle()
            for name, before, after in results:
                logger.info(f"[stock] 주기 갱신: {name} {before} → {after}")

            if self._callback:
                try:
                    self._callback(results)
                except Exception as e:
                    logger.warning(f"[stock] post_update_callback 실패: {e}")


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
