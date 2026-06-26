"""
CoC 폴백 명령어 헬퍼.

- 캐릭터 워크시트 조회 + 부재 시 사용자 친화 오류
- 사용자 락 획득 + 충돌 시 사용자 친화 오류
- `이름+1` / `이름-2` 같은 접미어 분리
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Iterator, Tuple

from utils.error_handling import CommandError
from utils.lock_manager import get_lock_manager


# `'이름' / '이름+1' / '이름-2'` — 마지막 ±정수만 접미어로 분리.
# 현재 어떤 룰의 이름에도 가운데 +/- 가 들어가지 않으므로 lazy match (`(.*?)`)로
# 끝부분 부호+숫자만 떼어내는 방식이 충분하다.
_SKILL_MODIFIER_RE = re.compile(r'^(.*?)([+-]\d+)$')


# 사용자 친화 오류 문구 (룰 간 통일)
USER_BUSY_MESSAGE = "다른 처리가 진행 중입니다. 잠시 후 다시 시도해 주세요."
SHEETS_DISCONNECTED_MESSAGE = (
    "시트에 연결되어 있지 않습니다. 잠시 후 다시 시도해 주세요. "
    "문제가 계속되면 운영자에게 문의해 주세요."
)
WORKSHEET_NOT_FOUND_TEMPLATE = (
    "캐릭터 시트를 찾을 수 없습니다. "
    "시트에 본인 아이디와 같은 이름의 워크시트 탭이 있는지 확인해 주세요. "
    "(찾는 탭 이름: '{user_id}')"
)


def split_skill_modifier(keyword: str) -> Tuple[str, int]:
    """
    `'기쁨'` → ('기쁨', 0)
    `'파괴+1'` → ('파괴', 1)
    `'회피-2'` → ('회피', -2)
    `'+1'` → ('', 1) — 호출측에서 빈 이름 검증 책임.

    Args:
        keyword: 사용자 입력 키워드.

    Returns:
        (이름, 보정치). 매칭 실패 시 (원본, 0).
    """
    name = (keyword or "").strip()
    m = _SKILL_MODIFIER_RE.match(name)
    if not m:
        return name, 0
    base = m.group(1).strip()
    return base, int(m.group(2))


def get_character_worksheet(sheets_manager, user_id: str):
    """
    캐릭터 워크시트 조회. 누락/미연결 시 사용자 친화적 `CommandError`.

    Args:
        sheets_manager: `SheetsManager` 인스턴스 (None 허용).
        user_id: 마스토돈 acct (로컬 파트만 워크시트 이름으로 매칭).

    Returns:
        gspread `Worksheet` 객체.

    Raises:
        CommandError: 시트 미연결 또는 워크시트 부재.
    """
    if sheets_manager is None:
        raise CommandError(SHEETS_DISCONNECTED_MESSAGE)
    ws = sheets_manager.get_character_worksheet_for_write(user_id)
    if ws is None:
        raise CommandError(WORKSHEET_NOT_FOUND_TEMPLATE.format(user_id=user_id))
    return ws


@contextmanager
def acquire_user_lock(user_id: str, timeout: float = 10.0) -> Iterator[None]:
    """
    사용자 락 컨텍스트 매니저 — 미획득 시 `CommandError(USER_BUSY_MESSAGE)`.

    Example:
        with acquire_user_lock(user_id):
            ws = get_character_worksheet(sheets_manager, user_id)
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
