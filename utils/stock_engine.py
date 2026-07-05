"""
주식 엔진 (재원 / 차성 / 적연)

캐릭터와 무관한 전역 주식 시장을 시뮬레이션한다.
- 현재가, 매수/매도 누적 카운터, 6시간 단위 가격 히스토리(24h 비교용) 를
  JSON 파일에 영속화.
- 백그라운드 스레드가 6시간마다 가격을 갱신:
    base = uniform(-1.0, +1.0)
    pressure = (buys - sells) / (buys + sells + 1)          # ∈ [-1, +1]
    delta = clamp(base + PRESSURE_WEIGHT * pressure, -1.0, +1.0)
    new_price = max(1, round(price * (1 + delta)))
- 거래(buy/sell)는 동기 API. 각 호출은 락 보호하에 누적 카운터를 증가시킨다.

설계 메모:
- "최소 -100%~ 최대 +100% 변동" 사양을 그대로 따라 delta 를 ±1.0 으로 클램프.
- 가격이 0 이하로 떨어지지 않도록 1 골드 최소 floor.
- 가격*수량 결과는 음수가 될 수 없지만(현재가는 양수), 매도 후 잔여 골드는
  사양에 따라 음수도 그대로 표기됨 (이 엔진의 책임은 아님).
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from utils.logging_config import logger


# ======================================================================
# 설정 상수
# ======================================================================
STOCK_NAMES: Tuple[str, ...] = ('재원', '차성', '적연')
INITIAL_PRICE = 20
PRICE_FLOOR = 1
UPDATE_INTERVAL_SECONDS = 6 * 60 * 60   # 6시간
HISTORY_KEEP_CYCLES = 8                  # 48시간 분량 (24h 비교에 4번째 사용)
DAILY_COMPARE_INDEX = 4                  # 24h = 4 × 6h 사이클
PRESSURE_WEIGHT = 0.5

DEFAULT_STATE_FILE = Path(__file__).resolve().parent.parent / 'data' / 'stock_state.json'


# ======================================================================
# 데이터 모델
# ======================================================================

@dataclass
class StockState:
    """단일 종목의 상태."""
    name: str
    price: int = INITIAL_PRICE
    buys: int = 0
    sells: int = 0
    # 6시간 사이클마다 push 되는 가격 히스토리 (가장 최근이 마지막).
    # 길이 ≤ HISTORY_KEEP_CYCLES.
    history: List[int] = field(default_factory=list)

    def price_24h_ago(self) -> Optional[int]:
        """24시간 전(4 사이클 전) 가격. 히스토리가 부족하면 None."""
        if len(self.history) >= DAILY_COMPARE_INDEX:
            return self.history[-DAILY_COMPARE_INDEX]
        if self.history:
            return self.history[0]
        return None

    def change_rate_24h(self) -> Optional[float]:
        """24h 대비 상승률 (%). 비교 기준이 0 이면 None."""
        prev = self.price_24h_ago()
        if prev is None or prev == 0:
            return None
        return (self.price - prev) / prev * 100.0


# ======================================================================
# 엔진 본체
# ======================================================================

class StockEngine:
    """프로세스 전역 단일 인스턴스로 사용."""

    def __init__(self, state_path: Optional[Path] = None):
        self.state_path = Path(state_path) if state_path else DEFAULT_STATE_FILE
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_update_ts: float = 0.0
        self._stocks: Dict[str, StockState] = {
            name: StockState(name=name) for name in STOCK_NAMES
        }
        self._load_state()

    # ------------------------------------------------------------------
    # 영속화
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        if not self.state_path.exists():
            logger.info(
                f"[stock] 상태 파일 없음 — 초기값으로 시작 ({self.state_path})"
            )
            return
        try:
            with open(self.state_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._last_update_ts = float(data.get('last_update_ts', 0.0))
            for name in STOCK_NAMES:
                raw = data.get('stocks', {}).get(name)
                if not raw:
                    continue
                self._stocks[name] = StockState(
                    name=name,
                    price=int(raw.get('price', INITIAL_PRICE)),
                    buys=int(raw.get('buys', 0)),
                    sells=int(raw.get('sells', 0)),
                    history=[int(p) for p in raw.get('history', [])],
                )
            logger.info(f"[stock] 상태 로드 완료 ({self.state_path})")
        except Exception as e:
            logger.warning(f"[stock] 상태 로드 실패 — 초기값 사용: {e}")

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                'last_update_ts': self._last_update_ts,
                'stocks': {name: asdict(s) for name, s in self._stocks.items()},
            }
            tmp_path = self.state_path.with_suffix('.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.state_path)
        except Exception as e:
            logger.warning(f"[stock] 상태 저장 실패: {e}")

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------

    def get_price(self, stock_name: str) -> Optional[int]:
        with self._lock:
            stock = self._stocks.get(stock_name)
            return stock.price if stock else None

    def get_all_snapshots(self) -> List[Tuple[str, int, Optional[float]]]:
        """[(이름, 현재가, 24h 상승률%), ...] 반환."""
        with self._lock:
            return [
                (s.name, s.price, s.change_rate_24h())
                for s in self._stocks.values()
            ]

    def is_valid_stock(self, stock_name: str) -> bool:
        return stock_name in self._stocks

    def stock_names(self) -> List[str]:
        return list(STOCK_NAMES)

    # ------------------------------------------------------------------
    # 거래
    # ------------------------------------------------------------------

    def buy(self, stock_name: str, quantity: int) -> Optional[Tuple[int, int]]:
        """
        구매. 카운터를 증가시키고 (단가, 총액) 반환.
        잘못된 종목/수량이면 None.
        """
        if quantity <= 0 or not self.is_valid_stock(stock_name):
            return None
        with self._lock:
            stock = self._stocks[stock_name]
            stock.buys += quantity
            total = stock.price * quantity
            self._save_state()
            return (stock.price, total)

    def sell(self, stock_name: str, quantity: int) -> Optional[Tuple[int, int]]:
        """매도. (단가, 총액) 반환."""
        if quantity <= 0 or not self.is_valid_stock(stock_name):
            return None
        with self._lock:
            stock = self._stocks[stock_name]
            stock.sells += quantity
            total = stock.price * quantity
            self._save_state()
            return (stock.price, total)

    # ------------------------------------------------------------------
    # 가격 갱신 (6h)
    # ------------------------------------------------------------------

    def _apply_cycle(self) -> List[Tuple[str, int, int]]:
        """
        1사이클 가격 갱신을 수행하고 [(이름, before, after), ...] 반환.
        호출자가 락을 잡고 있어야 한다.
        """
        results: List[Tuple[str, int, int]] = []
        for stock in self._stocks.values():
            before = stock.price
            base = random.uniform(-1.0, 1.0)
            total_volume = stock.buys + stock.sells
            pressure = (stock.buys - stock.sells) / (total_volume + 1)
            delta = max(-1.0, min(1.0, base + PRESSURE_WEIGHT * pressure))
            new_price = max(PRICE_FLOOR, int(round(before * (1.0 + delta))))
            stock.price = new_price

            # 히스토리에 새 가격 push (사이즈 캡)
            stock.history.append(new_price)
            if len(stock.history) > HISTORY_KEEP_CYCLES:
                stock.history = stock.history[-HISTORY_KEEP_CYCLES:]

            # 카운터 리셋
            stock.buys = 0
            stock.sells = 0

            results.append((stock.name, before, new_price))
        self._last_update_ts = time.time()
        return results

    def force_update_cycle(self) -> List[Tuple[str, int, int]]:
        """디버깅/수동 트리거용. 락 + 저장 포함."""
        with self._lock:
            results = self._apply_cycle()
            self._save_state()
        for name, before, after in results:
            logger.info(f"[stock] 강제 사이클: {name} {before} → {after}")
        return results

    # ------------------------------------------------------------------
    # 백그라운드 스레드
    # ------------------------------------------------------------------

    def start(self, post_update_callback=None) -> None:
        """
        백그라운드 스레드 시작. 이미 실행 중이면 무시.

        Args:
            post_update_callback: 한 사이클 갱신 직후 호출되는 콜백.
                시그니처: `fn(results: List[Tuple[name, before, after]])`.
                시트 미러링용. 예외는 삼킴.
        """
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._callback = post_update_callback
        self._thread = threading.Thread(
            target=self._run_loop,
            name='stock-engine',
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"[stock] 백그라운드 스레드 시작 (주기={UPDATE_INTERVAL_SECONDS}s)"
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.debug("[stock] 백그라운드 스레드 종료")

    def _run_loop(self) -> None:
        """
        루프 동작:
        - 부팅 직후 `last_update_ts` 기준으로 다음 사이클까지 남은 시간을 계산.
        - 0.5초 단위로 stop 이벤트를 폴링하여 빠른 종료 가능.
        """
        while not self._stop_event.is_set():
            with self._lock:
                next_fire = self._last_update_ts + UPDATE_INTERVAL_SECONDS
                if self._last_update_ts <= 0:
                    # 최초 실행: 한 주기 뒤 첫 갱신.
                    self._last_update_ts = time.time()
                    next_fire = self._last_update_ts + UPDATE_INTERVAL_SECONDS
                    self._save_state()

            now = time.time()
            wait_s = max(1.0, next_fire - now)
            # 짧은 sleep 으로 stop 응답성 확보.
            slept = 0.0
            while slept < wait_s and not self._stop_event.is_set():
                step = min(0.5, wait_s - slept)
                time.sleep(step)
                slept += step

            if self._stop_event.is_set():
                break

            results: List[Tuple[str, int, int]] = []
            with self._lock:
                results = self._apply_cycle()
                self._save_state()
            for name, before, after in results:
                logger.info(f"[stock] 주기 갱신: {name} {before} → {after}")

            callback = getattr(self, '_callback', None)
            if callback:
                try:
                    callback(results)
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
