"""
[판정/스테이터스 종류] — '전투용 정보'에서 스탯값을 조회 후 d100 굴려 결과 판정.

일반 판정 기준:
- d100 ≤ 스탯 × 3        → [대성공]
- d100 ≤ 스탯 × 7        → [성공]
- d100 ≥ max(100, 스탯×7 × 1.5)  → [대실패]
- 그 외 (성공 기준치 초과 ~ 대실패 미만) → [실패]

특수 케이스: '성공 기준치 × 1.5 ≥ 100' 이면 99 까지가 [실패], 100 이 [대실패].

[판정/이능력] — 특수 판정. '레이드 정보' O열(이능력 등급 0~5) 사용:
- R = 1d100 - 등급×2  (R < 1 이면 1 로 처리)
- R ≤ 등급×3                → [대성공]
- R ≤ 50                    → [성공]
- R > 100 - 100/(등급×5)    → [대실패]
- 그 외                     → [실패]
등급 표기: 5=S, 4=A, 3=B, 2=C, 1=D, 0=F.
(등급 0 은 대실패 기준식이 정의되지 않으므로 50 초과 전부 [대실패].)
"""

from __future__ import annotations

import random

from commands.base_command import BaseCommand, CommandContext, CommandResponse
from commands.registry import register_command
from utils.decorators import handle_command_errors
from utils.error_handling import CommandError
from utils.logging_config import logger
from utils.shared_sheet import (
    RAID_COL_ABILITY,
    RAID_DATA_START_ROW,
    WS_RAID,
    find_character_row,
    get_combat_stat,
    read_int_cell,
)


# 이능력 등급 숫자 → 문자 표기
_ABILITY_GRADE_LETTER = {0: 'F', 1: 'D', 2: 'C', 3: 'B', 4: 'A', 5: 'S'}


@register_command(
    name="판정",
    aliases=[],
    description="d100 판정. [판정/스탯이름]",
    category="레이드",
    examples=["[판정/근력]", "[판정/회피]"],
    requires_sheets=True,
    requires_api=False,
    priority=10,
)
class JudgmentCommand(BaseCommand):

    @handle_command_errors(
        system_tag="판정",
        user_error_message="판정 처리 중 오류가 발생했습니다.",
    )
    def execute(self, context: CommandContext) -> CommandResponse:
        if len(context.keywords) < 2:
            raise CommandError("스탯 이름을 함께 입력해 주세요. 예: [판정/근력]")

        stat_name = context.keywords[1].strip()
        if not stat_name:
            raise CommandError("스탯 이름이 비어 있습니다.")

        title = (context.user_name or '').strip()
        if not title:
            raise CommandError("마스토돈 표시명(=칭호)을 확인할 수 없습니다.")

        # 특수 판정: 이능력 (레이드 정보 O열 등급 기반)
        if stat_name.replace(' ', '') == '이능력':
            return self._handle_ability_check(context, title)

        stat_value = get_combat_stat(self.sheets_manager, title, stat_name)
        if stat_value is None:
            raise CommandError(
                f"'전투용 정보' 시트에서 {title}의 '{stat_name}' 스탯을 찾지 못했습니다."
            )
        if stat_value < 0:
            raise CommandError(f"스탯 '{stat_name}' 값이 음수입니다 ({stat_value}).")

        roll = random.randint(1, 100)
        critical_success_threshold = stat_value * 3
        success_threshold = stat_value * 7
        critical_failure_floor = int(success_threshold * 1.5)

        # 사양: 성공 기준치*1.5가 100 이상일 시 99까지가 실패, 100이 대실패.
        if critical_failure_floor >= 100:
            critical_failure_floor = 100

        if roll <= critical_success_threshold:
            label = '대성공'
            emoji = '🌟'
        elif roll <= success_threshold:
            label = '성공'
            emoji = '✅'
        elif roll >= critical_failure_floor:
            label = '대실패'
            emoji = '💥'
        else:
            label = '실패'
            emoji = '❌'

        message = (
            f"━━━ {title}님의 {stat_name} 판정 ━━━\n"
            f"d100 = {roll} (기준 스탯 {stat_value})\n"
            f"대성공 ≤ {critical_success_threshold} / "
            f"성공 ≤ {success_threshold} / "
            f"대실패 ≥ {critical_failure_floor}\n"
            f"{emoji} 결과: [{label}]"
        )
        logger.info(
            f"[판정] @{context.user_id} ({title}) {stat_name}={stat_value} "
            f"d100={roll} → {label}"
        )
        return CommandResponse.create_success(
            message,
            data={
                'stat': stat_name,
                'stat_value': stat_value,
                'd100': roll,
                'result': label,
            },
        )

    # ------------------------------------------------------------------
    # [판정/이능력] — 레이드 정보 O열 등급(0~5) 기반 특수 판정
    # ------------------------------------------------------------------
    def _handle_ability_check(
        self, context: CommandContext, title: str,
    ) -> CommandResponse:
        raid_row = find_character_row(
            self.sheets_manager, WS_RAID, title, RAID_DATA_START_ROW,
        )
        if raid_row is None:
            raise CommandError(
                f"'레이드 정보' 시트에서 '{title}' 캐릭터를 찾을 수 없습니다."
            )

        grade = read_int_cell(
            self.sheets_manager, WS_RAID, raid_row, RAID_COL_ABILITY, default=-1,
        )
        if grade < 0 or grade > 5:
            raise CommandError(
                f"이능력 등급(O열) 값이 올바르지 않습니다. "
                f"0~5 사이 정수여야 합니다. (현재: {grade if grade >= 0 else '비어있음/숫자 아님'})"
            )
        grade_letter = _ABILITY_GRADE_LETTER[grade]

        roll = random.randint(1, 100)
        adjusted = roll - grade * 2
        if adjusted < 1:
            adjusted = 1

        critical_success_threshold = grade * 3
        # 등급 0 은 100/(0×5) 가 정의되지 않음 → 극한 해석: 50 초과 전부 대실패.
        if grade == 0:
            critical_failure_over = 50.0
        else:
            critical_failure_over = 100.0 - 100.0 / (grade * 5)

        if adjusted <= critical_success_threshold:
            label = '대성공'
            emoji = '🌟'
        elif adjusted <= 50:
            label = '성공'
            emoji = '✅'
        elif adjusted > critical_failure_over:
            label = '대실패'
            emoji = '💥'
        else:
            label = '실패'
            emoji = '❌'

        message = (
            f"━━━ {title}님의 이능력 판정 ━━━\n"
            f"d100 = {adjusted} (이능력 등급 {grade_letter})\n"
            f"대성공 ≤ {critical_success_threshold} / "
            f"성공 ≤ 50 / "
            f"대실패 > {critical_failure_over:.1f}\n"
            f"{emoji} 결과: [{label}]"
        )
        logger.info(
            f"[판정] @{context.user_id} ({title}) 이능력 등급={grade}({grade_letter}) "
            f"d100={roll} R={adjusted} → {label}"
        )
        return CommandResponse.create_success(
            message,
            data={
                'stat': '이능력',
                'grade': grade,
                'grade_letter': grade_letter,
                'd100': roll,
                'adjusted': adjusted,
                'result': label,
            },
        )
