"""
[정찰] — '개인조사시트'의 '정찰' 페이지 조회 + 결과 적용.

동작:
- [정찰]        : 개인조사시트 '정찰' 페이지의 A3 셀 내용을 그대로 출력.
- [정찰/<행동>] : B열에서 <행동> 과 정확히 일치하는 셀을 찾음.
                  같은 값이 여러 행에 있으면 그 중 하나를 랜덤 선정.
                  선정된 행의 C열 값을 출력하고, 아래 효과 패턴을 자동 적용.

효과 패턴 (선정된 C열 값 안 어디에나):
  *HP+n / *HP-n   → '레이드 정보' K열 (현재 HP)
  *MP+n / *MP-n   → '레이드 정보' M열 (현재 MP)
  *골드+n / *골드-n → '장비 및 주식' B열 (소유 골드)
  *소지품+이름(n)   → '자동봇용 정보' 공동 창고 (M/N열)
                    이름이 이미 있으면 수량 증가, 없으면 최하단 추가.

같은 카테고리(HP/MP/골드)에 여러 패턴이 있으면 마지막 매치만 적용.
소지품은 여러 개(*소지품+A(1) *소지품+B(2))를 모두 적용.
"""

from __future__ import annotations

import random
import re
from typing import List, Optional

from commands.base_command import BaseCommand, CommandContext, CommandResponse
from commands.registry import register_command
from commands.trpg_common.fallback_helpers import acquire_user_lock
from config.settings import config
from utils.decorators import handle_command_errors
from utils.error_handling import CommandError
from utils.logging_config import logger
from utils.shared_sheet import (
    EQUIP_COL_GOLD,
    EQUIP_DATA_START_ROW,
    RAID_COL_HP_CUR,
    RAID_COL_HP_MAX,
    RAID_COL_MP_CUR,
    RAID_COL_MP_MAX,
    RAID_DATA_START_ROW,
    WS_EQUIP_STOCK,
    WS_RAID,
    add_to_inventory,
    find_character_row,
    read_int_cell,
)


# 효과 정규식
_RE_HP = re.compile(r'\*HP([+-])(\d+)')
_RE_MP = re.compile(r'\*MP([+-])(\d+)')
_RE_GOLD = re.compile(r'\*골드([+-])(\d+)')
# 이름은 괄호가 아닌 문자 (한글/영문/공백 모두 허용). 괄호 안 수량은 양의 정수.
_RE_ITEM = re.compile(r'\*소지품\+([^\(\)]+?)\((\d+)\)')


@register_command(
    name="정찰",
    aliases=[],
    description="개인조사시트 '정찰' 페이지 조회. 인자 없으면 위치 안내, 있으면 행동 결과 + 효과 적용",
    category="레이드",
    examples=["[정찰]", "[정찰/사우나]", "[정찰/쿵]"],
    requires_sheets=True,
    requires_api=False,
    priority=10,
)
class ResearchCommand(BaseCommand):

    @handle_command_errors(
        system_tag="정찰",
        user_error_message="정찰 처리 중 오류가 발생했습니다.",
    )
    def execute(self, context: CommandContext) -> CommandResponse:
        title = (context.user_name or '').strip()
        if not title:
            raise CommandError("마스토돈 표시명(=칭호)을 확인할 수 없습니다.")

        if not getattr(config, 'RESEARCH_SHEET_ID', ''):
            raise CommandError(
                "'개인조사시트' 가 설정되지 않았습니다. "
                ".env 에 RESEARCH_SHEET_ID 를 추가한 뒤 봇을 재시작해 주세요."
            )

        worksheet_name = getattr(config, 'RESEARCH_WORKSHEET', '정찰') or '정찰'
        ws = self.sheets_manager.get_research_worksheet(worksheet_name)
        if ws is None:
            raise CommandError(
                f"'개인조사시트'의 '{worksheet_name}' 워크시트를 열 수 없습니다. "
                f"시트 ID / 공유 권한 / 워크시트 이름을 확인해 주세요."
            )

        # -------------------------------------------------------------
        # [정찰] — A3 출력
        # -------------------------------------------------------------
        if len(context.keywords) < 2:
            try:
                value = ws.acell('A3').value
            except Exception as e:
                raise CommandError(f"정찰 A3 셀 조회 실패: {e}")
            text = str(value or '').strip()
            if not text:
                raise CommandError("정찰 페이지 A3 셀이 비어 있습니다.")
            logger.info(f"[정찰] @{context.user_id} ({title}) A3 조회")
            return CommandResponse.create_success(text)

        # -------------------------------------------------------------
        # [정찰/<행동>] — B열 매칭 → C열 출력 + 효과 적용
        # -------------------------------------------------------------
        action = context.keywords[1].strip()
        if not action:
            raise CommandError("행동 이름이 비어 있습니다.")

        try:
            all_values = ws.get_all_values()
        except Exception as e:
            raise CommandError(f"정찰 페이지 조회 실패: {e}")

        # B열(index 1) 매칭 → C열(index 2) 값 수집
        matched: List[str] = []
        for row in all_values:
            if len(row) < 3:
                continue
            b_val = (row[1] or '').strip()
            if b_val == action:
                c_val = (row[2] or '').strip()
                if c_val:
                    matched.append(c_val)

        if not matched:
            raise CommandError(
                f"'{action}' 행동을 정찰 페이지의 B열에서 찾지 못했습니다."
            )

        chosen = random.choice(matched)
        logger.info(
            f"[정찰] @{context.user_id} ({title}) 행동={action} "
            f"매칭 {len(matched)}개 중 1개 선정"
        )

        # 효과 적용
        effects_report = self._apply_effects(context, title, chosen)

        message = chosen
        if effects_report:
            message += "\n\n" + effects_report

        return CommandResponse.create_success(message)

    # ------------------------------------------------------------------
    # 효과 파싱 및 적용
    # ------------------------------------------------------------------

    def _apply_effects(
        self, context: CommandContext, title: str, cell_value: str,
    ) -> str:
        """C 값 안의 효과 패턴을 찾아 시트에 적용. 요약 문자열 반환."""
        reports: List[str] = []

        # HP — 여러 개면 마지막 매치만
        hp_matches = _RE_HP.findall(cell_value)
        if hp_matches:
            sign, n = hp_matches[-1]
            delta = int(n) if sign == '+' else -int(n)
            rpt = self._apply_hp_or_mp(context, title, 'HP', delta)
            if rpt:
                reports.append(rpt)

        # MP
        mp_matches = _RE_MP.findall(cell_value)
        if mp_matches:
            sign, n = mp_matches[-1]
            delta = int(n) if sign == '+' else -int(n)
            rpt = self._apply_hp_or_mp(context, title, 'MP', delta)
            if rpt:
                reports.append(rpt)

        # 골드
        gold_matches = _RE_GOLD.findall(cell_value)
        if gold_matches:
            sign, n = gold_matches[-1]
            delta = int(n) if sign == '+' else -int(n)
            rpt = self._apply_gold(context, title, delta)
            if rpt:
                reports.append(rpt)

        # 소지품 — 여러 개 지원
        for m in _RE_ITEM.finditer(cell_value):
            item_name = m.group(1).strip()
            qty = int(m.group(2))
            if not item_name or qty == 0:
                continue
            rpt = self._apply_item(item_name, qty)
            if rpt:
                reports.append(rpt)

        if reports:
            return "[효과 적용]\n" + "\n".join(f"  - {r}" for r in reports)
        return ""

    def _apply_hp_or_mp(
        self, context: CommandContext, title: str, kind: str, delta: int,
    ) -> str:
        raid_row = find_character_row(
            self.sheets_manager, WS_RAID, title, RAID_DATA_START_ROW,
        )
        if raid_row is None:
            return f"[{kind} {delta:+d}] '레이드 정보'에서 캐릭터 행을 찾지 못해 미적용"

        if kind == 'HP':
            cur_col, max_col = RAID_COL_HP_CUR, RAID_COL_HP_MAX
        else:
            cur_col, max_col = RAID_COL_MP_CUR, RAID_COL_MP_MAX

        with acquire_user_lock(context.user_id, timeout=10.0):
            cur = read_int_cell(self.sheets_manager, WS_RAID, raid_row, cur_col)
            max_val = read_int_cell(self.sheets_manager, WS_RAID, raid_row, max_col)
            new_val = cur + delta
            if new_val < 0:
                new_val = 0
            if max_val > 0 and new_val > max_val:
                new_val = max_val
            ok = self.sheets_manager.update_cell(
                WS_RAID, raid_row, cur_col, str(new_val),
            )
            if not ok:
                return f"[{kind} {delta:+d}] 시트 저장 실패"

        sign = '+' if delta >= 0 else ''
        return f"{kind} {sign}{delta} ({cur} → {new_val})"

    def _apply_gold(
        self, context: CommandContext, title: str, delta: int,
    ) -> str:
        equip_row = find_character_row(
            self.sheets_manager, WS_EQUIP_STOCK, title, EQUIP_DATA_START_ROW,
        )
        if equip_row is None:
            return f"[골드 {delta:+d}] '장비 및 주식'에서 캐릭터 행을 찾지 못해 미적용"

        with acquire_user_lock(context.user_id, timeout=10.0):
            cur = read_int_cell(
                self.sheets_manager, WS_EQUIP_STOCK, equip_row, EQUIP_COL_GOLD,
            )
            new_val = cur + delta  # 음수 허용 (전체 사양)
            ok = self.sheets_manager.update_cell(
                WS_EQUIP_STOCK, equip_row, EQUIP_COL_GOLD, str(new_val),
            )
            if not ok:
                return f"[골드 {delta:+d}] 시트 저장 실패"

        sign = '+' if delta >= 0 else ''
        return f"골드 {sign}{delta} ({cur} → {new_val})"

    def _apply_item(self, item_name: str, qty: int) -> str:
        """공동 창고에 아이템 추가 (기존 add_to_inventory 활용)."""
        ok = add_to_inventory(self.sheets_manager, item_name, qty)
        if not ok:
            return f"[소지품 +{qty} {item_name}] 시트 저장 실패"
        return f"소지품 획득: {item_name} × {qty}"
