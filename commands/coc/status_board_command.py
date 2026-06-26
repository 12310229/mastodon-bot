"""
[상태창] — 능력치 + HP/MP + 골드/장비/주식 종합 출력.

데이터 소스:
- 능력치 (근/민/지/행/정): '전투용 정보' 시트의 H~L 컬럼 (고정 위치)
- HP/MP (현재/최대): '레이드 정보' 시트의 K/L/M/N
- 골드/장비: '장비 및 주식' 시트의 B/C/D/E
- 주식: '장비 및 주식' 시트의 F~K (재원 F/G, 차성 H/I, 적연 J/K — 주 수/투자금)

주식 표시 — 종목마다 한 줄:
    재원: 3주 / 투자 150G / 평가손익 +30G (120.00%)
- 수익금 = 현재가 × 주 수 − 투자금
- 이익률 = 현재가 × 주 수 / 투자금 × 100 (투자금 0이면 0%)
"""

from __future__ import annotations

from commands.base_command import BaseCommand, CommandContext, CommandResponse
from commands.registry import register_command
from utils.decorators import handle_command_errors
from utils.error_handling import CommandError
from utils.logging_config import logger
from utils.shared_sheet import (
    COMBAT_COL_DEX,
    COMBAT_COL_INT,
    COMBAT_COL_LUK,
    COMBAT_COL_MEN,
    COMBAT_COL_STR,
    COMBAT_DATA_START_ROW,
    EQUIP_COL_ACCESSORY,
    EQUIP_COL_ARMOR,
    EQUIP_COL_GOLD,
    EQUIP_COL_WEAPON,
    EQUIP_DATA_START_ROW,
    EQUIP_STOCK_COLS,
    RAID_COL_HP_CUR,
    RAID_COL_HP_MAX,
    RAID_COL_MP_CUR,
    RAID_COL_MP_MAX,
    RAID_DATA_START_ROW,
    WS_COMBAT,
    WS_EQUIP_STOCK,
    WS_RAID,
    find_character_row,
    read_int_cell,
)
from utils.stock_engine import get_stock_engine


@register_command(
    name="상태창",
    aliases=[],
    description="능력치/HP/MP/골드/장비/주식 종합 표시",
    category="레이드",
    examples=["[상태창]"],
    requires_sheets=True,
    requires_api=False,
    priority=10,
)
class StatusBoardCommand(BaseCommand):

    @handle_command_errors(
        system_tag="상태창",
        user_error_message="상태창 조회 중 오류가 발생했습니다.",
    )
    def execute(self, context: CommandContext) -> CommandResponse:
        title = (context.user_name or '').strip()
        if not title:
            raise CommandError("마스토돈 표시명(=칭호)을 확인할 수 없습니다.")

        combat_row = find_character_row(
            self.sheets_manager, WS_COMBAT, title, COMBAT_DATA_START_ROW,
        )
        raid_row = find_character_row(
            self.sheets_manager, WS_RAID, title, RAID_DATA_START_ROW,
        )
        equip_row = find_character_row(
            self.sheets_manager, WS_EQUIP_STOCK, title, EQUIP_DATA_START_ROW,
        )

        if combat_row is None and raid_row is None and equip_row is None:
            raise CommandError(
                f"'{title}' 캐릭터를 어느 시트에서도 찾지 못했습니다. "
                f"'장비 및 주식'/'레이드 정보'/'전투용 정보' A열의 칭호를 확인해 주세요."
            )

        # 능력치 (전투용 정보 H~L 고정 컬럼) -----------------------------
        if combat_row is None:
            ability_line = "(전투용 정보 행을 찾지 못했습니다.)"
        else:
            s_str = read_int_cell(self.sheets_manager, WS_COMBAT, combat_row, COMBAT_COL_STR)
            s_dex = read_int_cell(self.sheets_manager, WS_COMBAT, combat_row, COMBAT_COL_DEX)
            s_int = read_int_cell(self.sheets_manager, WS_COMBAT, combat_row, COMBAT_COL_INT)
            s_luk = read_int_cell(self.sheets_manager, WS_COMBAT, combat_row, COMBAT_COL_LUK)
            s_men = read_int_cell(self.sheets_manager, WS_COMBAT, combat_row, COMBAT_COL_MEN)
            ability_line = (
                f"근력 {s_str} / 민첩 {s_dex} / 지능 {s_int} / "
                f"행운 {s_luk} / 정신 {s_men}"
            )

        # HP/MP (레이드 정보 K~N) ----------------------------------------
        if raid_row is None:
            hpmp_line = "(레이드 정보 행을 찾지 못했습니다.)"
        else:
            hp_cur = read_int_cell(self.sheets_manager, WS_RAID, raid_row, RAID_COL_HP_CUR)
            hp_max = read_int_cell(self.sheets_manager, WS_RAID, raid_row, RAID_COL_HP_MAX)
            mp_cur = read_int_cell(self.sheets_manager, WS_RAID, raid_row, RAID_COL_MP_CUR)
            mp_max = read_int_cell(self.sheets_manager, WS_RAID, raid_row, RAID_COL_MP_MAX)
            hpmp_line = f"HP {hp_cur}/{hp_max}    MP {mp_cur}/{mp_max}"

        # 골드 / 장비 / 주식 --------------------------------------------
        if equip_row is None:
            equip_block = "(장비 및 주식 행을 찾지 못했습니다.)"
        else:
            equip_block = self._build_equip_block(equip_row)

        message = (
            f"━━━ {title}님의 상태창 ━━━\n"
            f"[능력치]\n{ability_line}\n"
            f"{hpmp_line}\n"
            f"[장비 및 주식]\n{equip_block}"
        )
        logger.info(f"[상태창] @{context.user_id} ({title}) 출력")
        return CommandResponse.create_success(message)

    # ------------------------------------------------------------------
    def _build_equip_block(self, equip_row: int) -> str:
        """장비 및 주식 시트의 B~K를 한 번에 읽어 보기 좋게 포맷.

        row_values() 한 번으로 전체 행을 가져와 셀별 호출(read_int_cell × 11)을
        대체 — API 호출 수를 11회에서 1회로 줄임.
        """
        try:
            ws = self.sheets_manager.get_worksheet(WS_EQUIP_STOCK)
            row_values = ws.row_values(equip_row)
        except Exception as e:
            logger.warning(f"[상태창] 장비 행 읽기 실패: {e}")
            return "(시트 읽기 실패)"

        def _cell(col_1based: int) -> str:
            if len(row_values) < col_1based:
                return ''
            return (row_values[col_1based - 1] or '').strip()

        def _int(col_1based: int) -> int:
            raw = _cell(col_1based)
            try:
                return int(raw) if raw else 0
            except ValueError:
                return 0

        gold = _int(EQUIP_COL_GOLD)
        armor = _cell(EQUIP_COL_ARMOR) or '(없음)'
        weapon = _cell(EQUIP_COL_WEAPON) or '(없음)'
        accessory = _cell(EQUIP_COL_ACCESSORY) or '(없음)'

        lines = [
            f"보유 골드: {gold}G",
            f"방어구: {armor}    무기: {weapon}    액세서리: {accessory}",
            "[주식 보유 현황]",
        ]

        engine = get_stock_engine()
        current_prices = {
            name: price for name, price, _change in engine.get_all_snapshots()
        }

        for stock_name, (shares_col, invest_col) in EQUIP_STOCK_COLS.items():
            shares = _int(shares_col)
            invested = _int(invest_col)
            price = current_prices.get(stock_name)
            if price is None:
                lines.append(
                    f"  - {stock_name}: 시세 없음 (보유 {shares}주, 투자 {invested}G)"
                )
                continue
            value = price * shares
            profit = value - invested
            if invested > 0:
                ratio = value / invested * 100.0
            else:
                ratio = 0.0
            profit_sign = '+' if profit >= 0 else ''
            lines.append(
                f"  - {stock_name}: {shares}주 / 투자 {invested}G / "
                f"평가손익 {profit_sign}{profit}G ({ratio:.2f}%)"
            )

        return '\n'.join(lines)
