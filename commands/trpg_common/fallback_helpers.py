"""
신규 명령어용 공용 헬퍼.

CoC 폴백(능력치/기능/무기)이 제거되어, 현재 남는 공용 도구는
사용자 락 컨텍스트 매니저뿐. (양도/구매/주식 등의 시트 쓰기 명령이 사용)
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from utils.error_handling import CommandError
from utils.lock_manager import get_lock_manager


USER_BUSY_MESSAGE = "다른 처리가 진행 중입니다. 잠시 후 다시 시도해 주세요."


@contextmanager
def acquire_user_lock(user_id: str, timeout: float = 10.0) -> Iterator[None]:
    """
    사용자 락 컨텍스트 매니저 — 미획득 시 `CommandError(USER_BUSY_MESSAGE)`.

    Example:
        with acquire_user_lock(user_id):
            ... # 시트 읽기/쓰기

    Args:
        user_id: 사용자 ID.
        timeout: 락 획득 대기 시간 (초).

    Raises:
        CommandError: timeout 안에 락을 획득하지 못한 경우.
    """
    lock_manager = get_lock_manager()
    with lock_manager.acquire_lock(user_id, timeout=timeout) as acquired:
        if not acquired:
            raise CommandError(USER_BUSY_MESSAGE)
        yield
