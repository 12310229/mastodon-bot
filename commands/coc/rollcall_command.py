"""
[점호] — 아침/저녁 점호. KST 기준 허용 시간대에만 응답.

허용 시간 (KST):
  - 아침 점호: 08:00 ~ 08:59
  - 저녁 점호: 18:00 ~ 18:59

그 외 시간엔 봇이 침묵 (빈 메시지 반환 → stream_handler 가 송신 생략).

선정된 등급에 따라 MP 회복/감소:
  우수 +30 / 양호 +25 / 보통 +20 / 주의 +10 / 위험 -5
'레이드 정보' 페이지의 현재 MP(M열)를 최대 MP(N열) 한도 내에서 변동.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Optional

try:
    import pytz
    _KST = pytz.timezone('Asia/Seoul')
except ImportError:
    pytz = None
    _KST = None

from commands.base_command import BaseCommand, CommandContext, CommandResponse
from commands.registry import register_command
from commands.trpg_common.fallback_helpers import acquire_user_lock
from utils.decorators import handle_command_errors
from utils.error_handling import CommandError
from utils.logging_config import logger
from utils.shared_sheet import (
    RAID_COL_MP_CUR,
    RAID_COL_MP_MAX,
    RAID_DATA_START_ROW,
    WS_RAID,
    find_character_row,
    read_int_cell,
)


_GRADES = [
    # (이름, MP 변동, 출력 문구)
    ('우수', +30, '오늘의 상태는... 우수!'),
    ('양호', +25, '오늘의 상태는... 양호!'),
    ('보통', +20, '오늘의 상태는... 보통!'),
    ('주의', +10, '오늘의 상태는... 주의!'),
    ('위험', -5,  '오늘의 상태는... 위험!!!'),
]


# 허용 시간 (KST). 시작 시각 ≤ now.hour < 종료 시각.
# 8시 ~ 9시(미만) → 8:00 ~ 8:59 까지.
# 18시 ~ 19시(미만) → 18:00 ~ 18:59 까지.
_MORNING_START_HOUR = 8
_MORNING_END_HOUR = 9
_EVENING_START_HOUR = 18
_EVENING_END_HOUR = 19


def _current_session() -> Optional[str]:
    """현재 KST 시각이 점호 세션 안이면 '아침' / '저녁', 그 외 None."""
    if _KST is not None:
        now = datetime.now(_KST)
    else:
        # pytz 미설치 환경(이론상 거의 없음) — 시스템 TZ 로 폴백.
        now = datetime.now()
    hour = now.hour
    if _MORNING_START_HOUR <= hour < _MORNING_END_HOUR:
        return '아침'
    if _EVENING_START_HOUR <= hour < _EVENING_END_HOUR:
        return '저녁'
    return None


@register_command(
    name="점호",
    aliases=[],
    description="아침(08~09시 KST) / 저녁(18~19시 KST) 점호 — MP 회복",
    category="레이드",
    examples=["[점호]"],
    requires_sheets=True,
    requires_api=False,
    priority=10,
)
class RollcallCommand(BaseCommand):

    @handle_command_errors(
        system_tag="점호",
        user_error_message="점호 처리 중 오류가 발생했습니다.",
    )
    def execute(self, context: CommandContext) -> CommandResponse:
        session = _current_session()
        if session is None:
            # 허용 시간 외 — 빈 메시지로 침묵.
            # stream_handler 의 _send_response 가 빈 메시지를 송신하지 않음.
            logger.info(
                f"[점호] @{context.user_id} 시간 외 호출 무시 "
                f"(허용: 08~09시 / 18~19시 KST)"
            )
            return CommandResponse.create_success('')

        title = (context.user_name or '').strip()
        if not title:
            raise CommandError("마스토돈 표시명(=칭호)을 확인할 수 없습니다.")

        raid_row = find_character_row(
            self.sheets_manager, WS_RAID, title, RAID_DATA_START_ROW,
        )
        if raid_row is None:
            raise CommandError(
                f"'레이드 정보' 시트에서 '{title}' 캐릭터를 찾을 수 없습니다."
            )

        grade, delta, script = random.choice(_GRADES)

        with acquire_user_lock(context.user_id, timeout=10.0):
            mp_cur = read_int_cell(self.sheets_manager, WS_RAID, raid_row, RAID_COL_MP_CUR)
            mp_max = read_int_cell(self.sheets_manager, WS_RAID, raid_row, RAID_COL_MP_MAX)
            new_mp = mp_cur + delta
            if new_mp < 0:
                new_mp = 0
            if mp_max > 0 and new_mp > mp_max:
                new_mp = mp_max

            ok = self.sheets_manager.update_cell(
                WS_RAID, raid_row, RAID_COL_MP_CUR, str(new_mp),
            )
            if not ok:
                raise CommandError("MP 변경을 시트에 저장하지 못했습니다.")

        sign = '+' if delta >= 0 else ''
        message = (
            f"[{session} 점호] {script}\n"
            f"MP {sign}{delta} ({mp_cur} → {new_mp}"
            f"{f' / {mp_max}' if mp_max > 0 else ''})"
        )
        logger.info(
            f"[점호] @{context.user_id} ({title}) {session} {grade} delta={delta:+d} "
            f"{mp_cur}→{new_mp}"
        )
        return CommandResponse.create_success(
            message,
            data={
                'session': session, 'grade': grade, 'delta': delta,
                'mp_before': mp_cur, 'mp_after': new_mp,
            },
        )
