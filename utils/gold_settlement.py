"""
[골드 정산] 명령어 상태 저장소 (JSON 영속화)

캐릭터별로 다음을 보관:
- last_count       : DM 제외한 누적 게시글 수 (마지막 정산 시점)
- last_status_id   : 마지막 정산 시 가장 최신 status 의 id (다음 정산에서
                     since_id 파라미터로 사용 → 증분 페이지네이션)

stock_engine.py 와 같은 패턴: 단일 JSON 파일, 락으로 보호, atomic write.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional, Tuple

from utils.logging_config import logger


_DEFAULT_STATE_FILE = (
    Path(__file__).resolve().parent.parent / 'data' / 'gold_settlement.json'
)


class _SettlementStore:
    """프로세스 단일 인스턴스로 사용. 첫 호출 시 lazy 로드."""

    def __init__(self, path: Path = _DEFAULT_STATE_FILE):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._state: dict = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            if self.path.exists():
                try:
                    with open(self.path, 'r', encoding='utf-8') as f:
                        self._state = json.load(f) or {}
                except Exception as e:
                    logger.warning(f"[정산] 상태 로드 실패 — 초기화: {e}")
                    self._state = {}
            self._loaded = True

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning(f"[정산] 상태 저장 실패: {e}")

    def get_record(self, user_id: str) -> Tuple[int, Optional[str]]:
        """`(last_count, last_status_id)` 반환. 없으면 `(0, None)`."""
        self._ensure_loaded()
        with self._lock:
            rec = self._state.get(user_id) or {}
            return int(rec.get('last_count', 0)), rec.get('last_status_id')

    def set_record(
        self,
        user_id: str,
        last_count: int,
        last_status_id: Optional[str],
    ) -> None:
        self._ensure_loaded()
        with self._lock:
            self._state[user_id] = {
                'last_count': int(last_count),
                'last_status_id': last_status_id,
            }
            self._save()


_store = _SettlementStore()


def get_record(user_id: str) -> Tuple[int, Optional[str]]:
    return _store.get_record(user_id)


def set_record(user_id: str, last_count: int, last_status_id: Optional[str]) -> None:
    _store.set_record(user_id, last_count, last_status_id)
