"""
[주식 확인 / 주식 구매/<종목>/<수량> / 주식 매도/<종목>/<수량>]

- 종목: 재원 / 차성 / 적연
- 가격 / 매수·매도 누적 / 6시간 주기 갱신은 utils.stock_engine 이 담당.
- 본 명령어는 캐릭터의 골드·주 수·투자금을 '장비 및 주식' 시트에서 갱신한다.
- 시트의 주식 컬럼 구조: F/G = 재원, H/I = 차성, J/K = 적연 (각각 주 수 / 투자금).
- 상승률 컬럼은 시트에 없음. 수익금/이익률은 [상태창]에서 즉시 계산해 표시.
"""

from __future__ import annotations

from commands.base_command import BaseCommand, CommandContext, CommandResponse
from commands.registry import register_command
from commands.trpg_common.fallback_helpers import acquire_user_lock
from utils.decorators import handle_command_errors
from utils.error_handling import CommandError
from utils.logging_config import logger
from utils.shared_sheet import (
    EQUIP_COL_GOLD,
    EQUIP_DATA_START_ROW,
    EQUIP_STOCK_COLS,
    WS_EQUIP_STOCK,
    find_character_row,
    read_int_cell,
)
from utils.stock_engine import get_stock_engine


def _format_rate(rate: float) -> str:
    sign = '+' if rate >= 0 else ''
    return f"{sign}{rate:.1f}%"


@register_command(
    name="주식",
    aliases=['주식 확인', '주식확인', '주식 구매', '주식구매', '주식 매도', '주식매도'],
    description="주식 시세 확인 / 구매 / 매도",
    category="주식",
    examples=["[주식 확인]", "[주식 구매/재원/3]", "[주식 매도/적연/1]"],
    requires_sheets=True,
    requires_api=False,
    priority=10,
)
class StockCommand(BaseCommand):

    @handle_command_errors(
        system_tag="주식",
        user_error_message="주식 명령어 처리 중 오류가 발생했습니다.",
    )
    def execute(self, context: CommandContext) -> CommandResponse:
        if not context.keywords:
            raise CommandError("주식 명령어가 비어 있습니다.")

        head = context.keywords[0].replace(' ', '')

        if head == '주식확인':
            return self._handle_check(context)
        if head == '주식구매':
            return self._handle_trade(context, side='buy')
        if head == '주식매도':
            return self._handle_trade(context, side='sell')

        raise CommandError(
            "사용법: [주식 확인] / [주식 구매/종목/수량] / [주식 매도/종목/수량]"
        )

    # ------------------------------------------------------------------
    # [주식 확인]
    # ------------------------------------------------------------------
    def _handle_check(self, context: CommandContext) -> CommandResponse:
        engine = get_stock_engine()
        snapshots = engine.get_all_snapshots()

        lines = ["━━━ 주식 시세 ━━━"]
        for name, price, change in snapshots:
            if change is None:
                rate_str = "(전일 데이터 없음)"
            else:
                rate_str = _format_rate(change) + " (24h)"
            lines.append(f"{name}: {price} 골드  {rate_str}")
        return CommandResponse.create_success('\n'.join(lines))

    # ------------------------------------------------------------------
    # [주식 구매/매도/종목/수량]
    # ------------------------------------------------------------------
    def _handle_trade(self, context: CommandContext, side: str) -> CommandResponse:
        if len(context.keywords) < 3:
            raise CommandError(
                f"사용법: [주식 {'구매' if side == 'buy' else '매도'}/종목/수량]"
            )

        stock_name = context.keywords[1].strip()
        qty_raw = context.keywords[2].strip()

        try:
            quantity = int(qty_raw)
        except (TypeError, ValueError):
            raise CommandError(f"'수량'은 정수여야 합니다. (입력: {qty_raw})")
        if quantity <= 0:
            raise CommandError("수량은 1 이상이어야 합니다.")

        engine = get_stock_engine()
        if not engine.is_valid_stock(stock_name):
            raise CommandError(
                f"'{stock_name}'은(는) 등록된 종목이 아닙니다. "
                f"가능 종목: {', '.join(engine.stock_names())}"
            )

        title = (context.user_name or '').strip()
        if not title:
            raise CommandError("마스토돈 표시명(=칭호)을 확인할 수 없습니다.")

        equip_row = find_character_row(
            self.sheets_manager, WS_EQUIP_STOCK, title, EQUIP_DATA_START_ROW,
        )
        if equip_row is None:
            raise CommandError(
                f"'장비 및 주식' 시트에서 '{title}' 캐릭터를 찾을 수 없습니다."
            )

        shares_col, invest_col = EQUIP_STOCK_COLS[stock_name]

        with acquire_user_lock(context.user_id, timeout=10.0):
            current_gold = read_int_cell(
                self.sheets_manager, WS_EQUIP_STOCK, equip_row, EQUIP_COL_GOLD,
            )
            cur_shares = read_int_cell(
                self.sheets_manager, WS_EQUIP_STOCK, equip_row, shares_col,
            )
            cur_invest = read_int_cell(
                self.sheets_manager, WS_EQUIP_STOCK, equip_row, invest_col,
            )

            if side == 'buy':
                tx = engine.buy(stock_name, quantity)
                if tx is None:
                    raise CommandError("주식 구매에 실패했습니다.")
                price, total = tx
                new_gold = current_gold - total
                new_shares = cur_shares + quantity
                new_invest = cur_invest + total
                action_label = '구매'
            else:
                if quantity > cur_shares:
                    raise CommandError(
                        f"보유 주식 수가 부족합니다. (보유 {cur_shares}주, 매도 {quantity}주)"
                    )
                tx = engine.sell(stock_name, quantity)
                if tx is None:
                    raise CommandError("주식 매도에 실패했습니다.")
                price, total = tx
                new_gold = current_gold + total
                new_shares = cur_shares - quantity
                # 매도 시 투자금은 비례 차감 (평단가 유지)
                if cur_shares > 0:
                    new_invest = int(round(cur_invest * (new_shares / cur_shares)))
                else:
                    new_invest = 0
                action_label = '매도'

            updates = [
                (equip_row, EQUIP_COL_GOLD, str(new_gold)),
                (equip_row, shares_col, str(new_shares)),
                (equip_row, invest_col, str(new_invest)),
            ]
            ok = self.sheets_manager.batch_update_cells(WS_EQUIP_STOCK, updates)
            if not ok:
                raise CommandError("시트 업데이트에 실패했습니다.")

        # 표시용 즉시 계산.
        # 수익금 = 현재가 × 주 수 − 투자금
        # 이익률 = 현재가 × 주 수 / 투자금 × 100  (투자금 0이면 0%)
        value = price * new_shares
        profit = value - new_invest
        if new_invest > 0:
            ratio = value / new_invest * 100.0
        else:
            ratio = 0.0
        profit_sign = '+' if profit >= 0 else ''

        delta_sign = '-' if side == 'buy' else '+'
        message = (
            f"━━━ {title}님의 {stock_name} {action_label} ━━━\n"
            f"단가 {price} 골드 × {quantity}주 = {total} 골드\n"
            f"보유 골드: {current_gold} → {new_gold} ({delta_sign}{total})\n"
            f"보유 주: {cur_shares} → {new_shares}주\n"
            f"누적 투자금: {cur_invest} → {new_invest} 골드\n"
            f"평가 손익: {profit_sign}{profit}G ({ratio:.2f}%)"
        )
        logger.info(
            f"[주식 {action_label}] @{context.user_id} ({title}) {stock_name} "
            f"x{quantity} @{price} → 골드 {current_gold}→{new_gold}"
        )
        return CommandResponse.create_success(
            message,
            data={
                'side': side, 'stock': stock_name, 'quantity': quantity,
                'price': price, 'total': total,
                'gold_before': current_gold, 'gold_after': new_gold,
                'shares_after': new_shares, 'invest_after': new_invest,
                'profit': profit, 'ratio': ratio,
            },
        )
