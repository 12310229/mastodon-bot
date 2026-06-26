"""
CoC 폴백 명령어 — 시트 기반 키워드(능력치/기능/무기) + 스탯 변동 처리

레지스트리에 등록되지 않은 모든 키워드는 라우터가 이 명령어로 넘긴다
(`__coc_fallback__` 이름). 이 명령어가 첫 키워드를 분석해 아래 중 하나로 분기:

1. `[<스탯명> 변화/±n]` 또는 `[<스탯명> 변동/±ndm]` 또는 `[최대 <스탯명> 변화/...]`
   → 스탯 변동.
2. `[<무기명>]` / `[<무기명>+n]` / `[<무기명>-n]`
   → 무기 판정 + 피해 계산.
3. `[<기능명>]` / `[<기능명>+n]` / `[<기능명>-n]`
   → 기능/능력치 판정.

인식 순서: 스탯 변동 → 무기 → 기능 → 에러.
"""

from __future__ import annotations

import os
import re
import sys
from typing import List, Optional, Tuple

from commands.base_command import BaseCommand, CommandContext, CommandResponse
from commands.registry import register_command
from commands.trpg_common.fallback_helpers import (
    acquire_user_lock,
    get_character_worksheet,
    split_skill_modifier,
)
from utils.decorators import handle_command_errors
from utils.error_handling import CommandError
from utils.logging_config import logger
from utils.sheets_operations import SheetsManager

from .character import CoCCharacter
from .check_engine import perform_check
from .damage_engine import apply_damage_bonus, compute_weapon_base_damage, roll_damage
from .formatter import format_check, format_stat_change, format_weapon_attack
from .sheet_reader import get_cell_address, load_character_from_worksheet


# `[<스탯> 변화]` / `[<스탯> 변동]` / `[<스탯> 변경]` / `[최대 <스탯> 변화/변동/변경]` 패턴.
# 세 키워드(변화·변동·변경) 는 의미상 동의어로 처리한다.
_STAT_CHANGE_RE = re.compile(r'^(최대\s+)?(.+?)\s*(변화|변동|변경)$')


@register_command(
    name="__coc_fallback__",
    aliases=[],
    description="CoC 폴백 (시트 기반 기능/무기 판정 + 스탯 변동)",
    category="CoC",
    examples=["[근력]", "[회피+1]", "[권총]", "[이성 변화/-3]", "[체력 변동/+1d6]"],
    requires_sheets=True,
    requires_api=False,
    priority=0,
)
class CoCFallbackCommand(BaseCommand):
    """CoC 룰 시스템의 모든 동적 명령어를 처리하는 catch-all."""

    def __init__(self, sheets_manager: SheetsManager = None, api=None, **kwargs):
        super().__init__(sheets_manager=sheets_manager, api=api, **kwargs)

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    @handle_command_errors(
        system_tag="CoC",
        user_error_message="CoC 명령어 처리 중 오류가 발생했습니다.",
    )
    def execute(self, context: CommandContext) -> CommandResponse:
        if not context.keywords:
            return CommandResponse.create_error(
                "명령어를 입력해 주세요. [도움말]을 입력하면 사용 가능한 명령어를 볼 수 있습니다."
            )

        first = context.keywords[0].strip()
        rest = list(context.keywords[1:])

        # 1. 스탯 변동 분기
        stat_match = _STAT_CHANGE_RE.match(first)
        if stat_match and rest:
            is_max = bool(stat_match.group(1))
            stat_name = stat_match.group(2).strip()
            return self._handle_stat_change(context, stat_name, is_max, rest[0])

        # 2/3. 기능/무기 판정 — 키워드에서 +n/-n 분리
        skill_or_weapon, modifier = split_skill_modifier(first)
        if not skill_or_weapon:
            raise CommandError(
                "능력치나 기능 이름을 먼저 입력해 주세요. 예: [회피+1], [근력]"
            )
        return self._handle_check(context, skill_or_weapon, modifier)

    # ------------------------------------------------------------------
    # 판정 (기능 or 무기)
    # ------------------------------------------------------------------

    def _handle_check(
        self,
        context: CommandContext,
        name: str,
        modifier: int,
    ) -> CommandResponse:
        character = self._load_character(context.user_id)

        # 무기 우선 (동명이 있을 경우 무기가 우선)
        weapon = character.get_weapon(name)
        if weapon is not None:
            return self._handle_weapon_attack(character, weapon, modifier)

        # 일반 기능/능력치
        skill_value = character.get_skill_value(name)
        if skill_value is None:
            raise CommandError(
                f"'{name}'을(를) 시트에서 찾을 수 없습니다. "
                f"능력치나 기능 이름을 다시 확인해 주세요."
            )

        outcome = perform_check(skill_name=name, skill_value=skill_value, modifier=modifier)
        logger.info(
            f"[CoC 판정] @{context.user_id} [{name}] modifier={modifier} "
            f"d100={outcome.rolled.d100}/{skill_value} → {outcome.result.label}"
        )
        return CommandResponse.create_success(
            format_check(outcome),
            data={
                "skill": name,
                "skill_value": skill_value,
                "d100": outcome.rolled.d100,
                "result": outcome.result.value,
                "modifier": modifier,
            },
        )

    def _handle_weapon_attack(
        self,
        character: CoCCharacter,
        weapon,
        modifier: int,
    ) -> CommandResponse:
        # 무기가 지정한 기능치 값
        skill_value = character.get_skill_value(weapon.skill_name)
        if skill_value is None:
            raise CommandError(
                f"무기 '{weapon.name}'에 연결된 기능 '{weapon.skill_name}'을(를) "
                f"시트에서 찾을 수 없습니다. "
                f"시트의 무기 칸에 적힌 기능 이름이 올바른지 확인해 주세요."
            )

        outcome = perform_check(
            skill_name=weapon.skill_name,
            skill_value=skill_value,
            modifier=modifier,
        )

        damage_roll = roll_damage(weapon.damage_formula or "0")
        base_damage, base_detail = compute_weapon_base_damage(
            damage_roll, outcome.result, penetrates=weapon.penetrates,
        )
        bonus_damage, bonus_detail = apply_damage_bonus(
            db_mode=weapon.db_mode,
            db_formula=character.damage_bonus_formula,
            result=outcome.result,
        )
        total = base_damage + bonus_damage if outcome.result.is_success else 0

        logger.info(
            f"[CoC 무기] @{character.user_id} [{weapon.name}] "
            f"→ {outcome.result.label} 피해={total} (기본 {base_damage} + db {bonus_damage})"
        )

        message = format_weapon_attack(
            weapon_name=weapon.name,
            skill_name=weapon.skill_name,
            outcome=outcome,
            damage_roll=damage_roll,
            base_damage=base_damage,
            base_detail=base_detail,
            bonus_damage=bonus_damage,
            bonus_detail=bonus_detail,
            total_damage=total,
            penetrates=weapon.penetrates,
        )
        return CommandResponse.create_success(
            message,
            data={
                "weapon": weapon.name,
                "skill": weapon.skill_name,
                "skill_value": skill_value,
                "d100": outcome.rolled.d100,
                "result": outcome.result.value,
                "penetrates": weapon.penetrates,
                "damage_total": total,
                "damage_base": base_damage,
                "damage_bonus": bonus_damage,
                "modifier": modifier,
            },
        )

    # ------------------------------------------------------------------
    # 스탯 변동
    # ------------------------------------------------------------------

    # 현재 값에 최대치가 존재하는 스탯 → (스탯명) : (최대치를 조회할 이름)
    # 예: '체력' 의 상한은 '최대 체력'(E3). '이성' 의 상한은 '최대 이성'(G4).
    _MAX_BOUND_FOR: dict = {
        "체력": "체력",   # is_max=True 로 셀 주소 조회하면 E3
        "이성": "이성",   # is_max=True 로 G4
    }

    def _handle_stat_change(
        self,
        context: CommandContext,
        stat_name: str,
        is_max: bool,
        change_expr: str,
    ) -> CommandResponse:
        """`[이성 변화/-3]`, `[체력 변동/+1d6]`, `[최대 체력 변화/-4]` 처리."""
        cell = get_cell_address(stat_name, is_max=is_max)
        if cell is None:
            label = f"최대 {stat_name}" if is_max else stat_name
            raise CommandError(
                f"'{label}'은(는) 직접 변경할 수 없는 항목입니다. "
                f"변경 가능한 항목: 체력, 이성, 마력, 운, 최대 체력, 최대 이성, 최대 마력"
            )
        row, col = cell
        label = f"최대 {stat_name}" if is_max else stat_name

        delta, dice_detail = self._evaluate_change(change_expr)

        # 최대치 셀 주소 사전 계산 — 락 안에서 batch_get 으로 한 번에 읽기 위함.
        max_cell: Optional[Tuple[int, int]] = None
        if not is_max and stat_name in self._MAX_BOUND_FOR:
            max_cell = get_cell_address(
                self._MAX_BOUND_FOR[stat_name], is_max=True,
            )

        with acquire_user_lock(context.user_id, timeout=10.0):
            worksheet = get_character_worksheet(self.sheets_manager, context.user_id)

            # 현재값 + 최대치를 단일 batch_get 으로 읽어 API 쿼터 절감.
            # batch_get 미지원 워크시트는 sequential 폴백.
            before_raw, max_raw = self._read_stat_cells(worksheet, (row, col), max_cell)

            try:
                before = int((before_raw or "0").strip())
            except (TypeError, ValueError):
                raise CommandError(
                    f"'{label}' 칸의 현재 값을 읽을 수 없습니다. "
                    f"시트에 숫자만 입력되어 있는지 확인해 주세요."
                )

            new_value = before + delta
            clamped_low = False
            clamped_high = False

            # 하한: 0 미만 불가
            if new_value < 0:
                new_value = 0
                clamped_low = True

            # 상한: '최대 X' 를 직접 변동하는 경우가 아니라면,
            # 위에서 미리 읽어둔 max_raw 를 사용해 clamp.
            upper_bound: Optional[int] = None
            if max_raw is not None and str(max_raw).strip():
                try:
                    upper_bound = int(str(max_raw).strip())
                except (TypeError, ValueError):
                    upper_bound = None

            if upper_bound is not None and new_value > upper_bound:
                new_value = upper_bound
                clamped_high = True

            write_ok = self.sheets_manager.update_cell_safe(worksheet, row, col, str(new_value))
            if not write_ok:
                # 값이 시트에 반영되지 않았다 - 사용자에게 분명히 안내해 시트와 채팅 상태가
                # 어긋나는 것을 막는다.
                raise CommandError(
                    f"'{label}' 수정을 시트에 저장하지 못했습니다. "
                    f"잠시 후 다시 시도해 주세요. "
                    f"문제가 계속되면 [시트 업데이트]를 입력해 주세요."
                )

        clamped = clamped_low or clamped_high
        logger.info(
            f"[CoC 스탯] @{context.user_id} [{label}] {before} → {new_value} "
            f"(delta={delta:+d}"
            f"{' clamped=0' if clamped_low else ''}"
            f"{f' clamped={upper_bound}' if clamped_high else ''}"
            ")"
        )

        message = format_stat_change(
            stat_label=label,
            before=before,
            after=new_value,
            delta=delta,
            dice_detail=dice_detail,
            clamped=clamped,
            upper_bound=upper_bound if clamped_high else None,
        )
        return CommandResponse.create_success(
            message,
            data={
                "stat": label,
                "before": before,
                "after": new_value,
                "delta": delta,
                "clamped": clamped,
                "clamped_low": clamped_low,
                "clamped_high": clamped_high,
                "upper_bound": upper_bound,
            },
        )

    def _read_stat_cells(
        self,
        worksheet,
        current_cell: Tuple[int, int],
        max_cell: Optional[Tuple[int, int]],
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        스탯 변동에 필요한 셀(현재값, 선택적 최대치)을 읽어 반환.

        `max_cell` 이 주어지면 batch_get 으로 한 번에 두 셀을 읽어 API 호출을
        절반으로 줄인다. batch_get 이 실패하거나 미지원이면 개별 cell 읽기로
        폴백 — 이전 동작과 동일한 결과 보장.

        Returns:
            (current_raw, max_raw). max_cell 이 None 이거나 batch 실패 폴백 후에도
            최대치 읽기에 실패하면 max_raw 는 None.
        """
        if max_cell is None:
            current_raw = self.sheets_manager.get_cell_value_safe(
                worksheet, current_cell[0], current_cell[1],
            )
            return current_raw, None

        # 두 셀을 단일 batch_get 으로 시도.
        values = self.sheets_manager.batch_get_cells_safe(
            worksheet, [current_cell, max_cell],
        )
        if values is not None and len(values) == 2:
            return values[0], values[1]

        # 폴백: 개별 cell 읽기 (이전 코드 경로와 동일).
        current_raw = self.sheets_manager.get_cell_value_safe(
            worksheet, current_cell[0], current_cell[1],
        )
        max_raw = self.sheets_manager.get_cell_value_safe(
            worksheet, max_cell[0], max_cell[1],
        )
        return current_raw, max_raw

    def _evaluate_change(self, expr: str) -> Tuple[int, Optional[str]]:
        """
        `+3`, `-3`, `+1d6`, `-1d6`, `+2d4`, `-1d10+2` 같은 식을 평가.

        Returns:
            (delta: int, dice_detail: Optional[str])
            고정 정수라면 dice_detail=None.
        """
        s = (expr or "").strip().replace(" ", "")
        if not s:
            raise CommandError(
                "변화량을 입력해 주세요. 예: [이성 변화/+3], [체력 변동/-1d6]"
            )

        sign_char = s[0]
        if sign_char not in ('+', '-'):
            # 부호가 없으면 +n 으로 해석
            sign_char = '+'
            body = s
        else:
            body = s[1:]
        sign = 1 if sign_char == '+' else -1

        # 순수 정수
        if body.isdigit():
            return sign * int(body), None

        # 다이스식 (damage_engine 의 roll_damage 재활용)
        try:
            rolled = roll_damage(body)
        except ValueError as e:
            logger.debug(f"변화량 파싱 실패: '{expr}' → {e}")
            raise CommandError(
                f"'{expr}'을(를) 변화량으로 인식할 수 없습니다. "
                f"예: +3, -5, +1d6, -2d4"
            )

        delta = sign * rolled.total
        detail_prefix = sign_char  # '+' or '-'
        detail = f"{detail_prefix}{rolled.detail} = {delta:+d}"
        return delta, detail

    # ------------------------------------------------------------------
    # 시트 조회
    # ------------------------------------------------------------------

    def _load_character(self, user_id: str) -> CoCCharacter:
        ws = get_character_worksheet(self.sheets_manager, user_id)
        return load_character_from_worksheet(ws, user_id=user_id)

    @staticmethod
    def get_supported_keywords() -> List[str]:
        return ['__coc_fallback__']
