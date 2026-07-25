"""
[아이템 사용/<이름(수량)>, ...]
[아이템 구매/<이름(수량)>, ...]

- 사용: 공동 창고에서 수량 차감 + (HP/MP 포션이면) 레이드 정보 회복.
- 구매: '상점' 페이지에서 재고/가격/잔액 검증 후 골드 차감 + 공동 창고 추가.

아이템 인자 포맷 예: `소형HP포션(2), 대형MP포션(1)` 또는 `이름` (수량 1 기본).
"""

from __future__ import annotations

import os
import random
import re
import time
from typing import List, Optional, Tuple

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
    POTION_EFFECTS,
    RAID_COL_HP_CUR,
    RAID_COL_HP_MAX,
    RAID_COL_MP_CUR,
    RAID_COL_MP_MAX,
    RAID_DATA_START_ROW,
    WS_EQUIP_STOCK,
    WS_RAID,
    WS_SHOP,
    _normalize_item_name,
    add_to_inventory,
    consume_from_inventory,
    find_character_row,
    find_inventory_item,
    find_shop_item,
    read_int_cell,
    update_shop_stock,
)


# `소형HP포션(2)` 또는 `대형MP포션` 패턴.
_ITEM_PATTERN = re.compile(r'^(?P<name>.+?)(?:\((?P<qty>\d+)\))?$')


# ======================================================================
# 가챠 설정
# ======================================================================
GACHA_POUCH_NAME = '가챠파우치'         # 상점 A열에 등록된 구매 대상 이름
GACHA_LIST_COL = 2                      # 상점 B열
GACHA_LIST_ROW_START = 50               # B50
GACHA_LIST_ROW_END = 78                 # B78 (총 29개)
GACHA_IMAGE_DIR = config.BASE_DIR / 'gacha_images'
GACHA_IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
MASTODON_MAX_MEDIA = 4                  # 툿당 이미지 최대 개수
GACHA_MAX_DRAW = 40                     # 한 번에 뽑을 수 있는 최대 수 (스팸 방지)


def _parse_item_list(raw: str) -> List[Tuple[str, int]]:
    """`소형HP포션(2), 대형MP포션` → [('소형HP포션', 2), ('대형MP포션', 1)]."""
    items: List[Tuple[str, int]] = []
    for token in raw.split(','):
        token = token.strip()
        if not token:
            continue
        m = _ITEM_PATTERN.match(token)
        if not m:
            raise CommandError(f"'{token}' 형식을 인식할 수 없습니다. 예: 소형HP포션(2)")
        name = m.group('name').strip()
        qty_str = m.group('qty')
        qty = int(qty_str) if qty_str else 1
        if not name or qty <= 0:
            raise CommandError(f"'{token}' 형식을 인식할 수 없습니다.")
        items.append((name, qty))
    if not items:
        raise CommandError("아이템 목록이 비어 있습니다.")
    return items


@register_command(
    name="아이템",
    aliases=['아이템 사용', '아이템사용', '아이템 구매', '아이템구매'],
    description="아이템 사용 / 구매. 여러 개를 한 번에 처리할 수 있다.",
    category="아이템",
    examples=[
        "[아이템 사용/소형HP포션(1), 대형MP포션(3)]",
        "[아이템 구매/소형HP포션(2)]",
        "[아이템 구매/가챠파우치(3)]",
    ],
    requires_sheets=True,
    requires_api=True,   # 가챠 이미지 첨부 툿을 직접 전송하므로 API 필요
    priority=10,
)
class ItemCommand(BaseCommand):

    @handle_command_errors(
        system_tag="아이템",
        user_error_message="아이템 명령어 처리 중 오류가 발생했습니다.",
    )
    def execute(self, context: CommandContext) -> CommandResponse:
        head = context.keywords[0].replace(' ', '')

        if head == '아이템사용':
            return self._handle_use(context)
        if head == '아이템구매':
            return self._handle_buy(context)
        raise CommandError(
            "사용법: [아이템 사용/이름(수량), ...] / [아이템 구매/이름(수량), ...]"
        )

    # ------------------------------------------------------------------
    # 사용
    # ------------------------------------------------------------------
    def _handle_use(self, context: CommandContext) -> CommandResponse:
        if len(context.keywords) < 2:
            raise CommandError("사용할 아이템을 입력해 주세요.")

        title = (context.user_name or '').strip()
        if not title:
            raise CommandError("마스토돈 표시명(=칭호)을 확인할 수 없습니다.")

        # 키워드 1번 이후를 모두 합쳐서 파싱 ('/' 로 분할되어 다 들어옴)
        raw = ', '.join(context.keywords[1:])
        items = _parse_item_list(raw)

        # 사전 검증: 모든 아이템 보유량 확인
        for name, qty in items:
            entry = find_inventory_item(self.sheets_manager, name)
            if entry is None:
                raise CommandError(f"공동 창고에 '{name}'이(가) 없습니다.")
            if entry.qty < qty:
                raise CommandError(
                    f"'{name}' 보유량이 부족합니다. (보유 {entry.qty} / 요청 {qty})"
                )

        raid_row = find_character_row(
            self.sheets_manager, WS_RAID, title, RAID_DATA_START_ROW,
        )

        used_lines: List[str] = []
        effect_lines: List[str] = []

        with acquire_user_lock(context.user_id, timeout=10.0):
            for name, qty in items:
                ok = consume_from_inventory(self.sheets_manager, name, qty)
                if not ok:
                    raise CommandError(f"'{name}' 사용을 시트에 반영하지 못했습니다.")
                used_lines.append(f"{name} × {qty}")

                # HP/MP 포션 효과 적용
                potion = POTION_EFFECTS.get(_normalize_item_name(name))
                if potion is None:
                    continue
                if raid_row is None:
                    effect_lines.append(
                        f"  ↳ {name}: '레이드 정보'에서 캐릭터 행을 찾지 못해 효과 미적용"
                    )
                    continue

                kind, recover_per_use = potion
                total_recover = recover_per_use * qty
                if kind == 'hp':
                    cur_col, max_col, label = RAID_COL_HP_CUR, RAID_COL_HP_MAX, 'HP'
                else:
                    cur_col, max_col, label = RAID_COL_MP_CUR, RAID_COL_MP_MAX, 'MP'

                cur_val = read_int_cell(self.sheets_manager, WS_RAID, raid_row, cur_col)
                max_val = read_int_cell(self.sheets_manager, WS_RAID, raid_row, max_col)
                new_val = cur_val + total_recover
                if max_val > 0:
                    new_val = min(new_val, max_val)

                write_ok = self.sheets_manager.update_cell(
                    WS_RAID, raid_row, cur_col, str(new_val),
                )
                if not write_ok:
                    effect_lines.append(f"  ↳ {name}: {label} 시트 반영 실패")
                else:
                    effect_lines.append(
                        f"  ↳ {name}: {label} +{total_recover} ({cur_val} → {new_val})"
                    )

        body = "사용 아이템:\n  - " + '\n  - '.join(used_lines)
        if effect_lines:
            body += "\n효과:\n" + '\n'.join(effect_lines)
        logger.info(f"[아이템 사용] @{context.user_id} ({title}) {used_lines}")
        return CommandResponse.create_success(body)

    # ------------------------------------------------------------------
    # 구매
    # ------------------------------------------------------------------
    def _handle_buy(self, context: CommandContext) -> CommandResponse:
        if len(context.keywords) < 2:
            raise CommandError("구매할 아이템을 입력해 주세요.")

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

        raw = ', '.join(context.keywords[1:])
        items = _parse_item_list(raw)

        # 가챠파우치 단독 구매 감지 → 특수 처리 (뽑기 + 이미지 전송)
        if (len(items) == 1
                and _normalize_item_name(items[0][0]) == _normalize_item_name(GACHA_POUCH_NAME)):
            return self._handle_gacha(context, title, equip_row, items[0][1])

        # 사전 검증
        resolved = []  # [(ShopItem, qty, line_total)]
        total_cost = 0
        for name, qty in items:
            shop_item = find_shop_item(self.sheets_manager, name)
            if shop_item is None:
                raise CommandError(f"'상점'에서 '{name}'을(를) 찾을 수 없습니다.")
            if shop_item.stock < qty:
                raise CommandError(
                    f"'{name}' 재고 부족 (재고 {shop_item.stock} / 요청 {qty})"
                )
            line_total = shop_item.price * qty
            total_cost += line_total
            resolved.append((shop_item, qty, line_total))

        with acquire_user_lock(context.user_id, timeout=10.0):
            current_gold = read_int_cell(
                self.sheets_manager, WS_EQUIP_STOCK, equip_row, EQUIP_COL_GOLD,
            )
            if current_gold < total_cost:
                raise CommandError(
                    f"골드 부족 (보유 {current_gold} / 필요 {total_cost})"
                )

            # 골드 차감
            new_gold = current_gold - total_cost
            gold_ok = self.sheets_manager.update_cell(
                WS_EQUIP_STOCK, equip_row, EQUIP_COL_GOLD, str(new_gold),
            )
            if not gold_ok:
                raise CommandError("골드 차감에 실패했습니다.")

            lines: List[str] = []
            for shop_item, qty, line_total in resolved:
                stock_ok = update_shop_stock(
                    self.sheets_manager, shop_item.row, shop_item.stock - qty,
                )
                inv_ok = add_to_inventory(self.sheets_manager, shop_item.name, qty)
                status = '✓' if stock_ok and inv_ok else '⚠ 일부 실패'
                lines.append(
                    f"  - {shop_item.name} × {qty} = {line_total} 골드 {status}"
                )

        body = (
            f"━━━ {title}님의 아이템 구매 ━━━\n"
            f"총액: {total_cost} 골드 (보유 {current_gold} → {new_gold})\n"
            + '\n'.join(lines)
        )
        logger.info(
            f"[아이템 구매] @{context.user_id} ({title}) cost={total_cost} "
            f"{current_gold}→{new_gold}"
        )
        return CommandResponse.create_success(body)

    # ------------------------------------------------------------------
    # 가챠파우치
    # ------------------------------------------------------------------
    def _handle_gacha(
        self, context: CommandContext, title: str, equip_row: int, qty: int,
    ) -> CommandResponse:
        """가챠파우치 n개 구매 → 랜덤 n개 뽑아 이미지+텍스트로 스레드 전송.

        - 상점 A열의 '가챠파우치' 가격/재고로 골드 차감 + 재고 감소.
        - 상점 B50:B78 에서 중복 허용 랜덤 n개.
        - 결과는 창고에 반영하지 않고 출력만 한다.
        - 이미지+스레드는 self.api 로 직접 전송하고, 빈 응답을 반환해
          stream_handler 의 추가 전송을 막는다.
        """
        if qty <= 0:
            raise CommandError("뽑을 수량은 1 이상이어야 합니다.")
        if qty > GACHA_MAX_DRAW:
            raise CommandError(f"한 번에 최대 {GACHA_MAX_DRAW}개까지 뽑을 수 있습니다.")

        # 상점에서 가챠파우치 가격/재고 확인
        pouch = find_shop_item(self.sheets_manager, GACHA_POUCH_NAME)
        if pouch is None:
            raise CommandError(
                f"'상점'에 '{GACHA_POUCH_NAME}' 이(가) 없습니다. A열에 등록해 주세요."
            )
        if pouch.stock < qty:
            raise CommandError(
                f"'{GACHA_POUCH_NAME}' 재고 부족 (재고 {pouch.stock} / 요청 {qty})"
            )
        total_cost = pouch.price * qty

        # 가챠 후보 리스트 (B50:B78)
        candidates = self._read_gacha_candidates()
        if not candidates:
            raise CommandError("가챠 아이템 목록(상점 B50:B78)이 비어 있습니다.")

        with acquire_user_lock(context.user_id, timeout=10.0):
            current_gold = read_int_cell(
                self.sheets_manager, WS_EQUIP_STOCK, equip_row, EQUIP_COL_GOLD,
            )
            if current_gold < total_cost:
                raise CommandError(
                    f"골드 부족 (보유 {current_gold} / 필요 {total_cost})"
                )
            new_gold = current_gold - total_cost
            gold_ok = self.sheets_manager.update_cell(
                WS_EQUIP_STOCK, equip_row, EQUIP_COL_GOLD, str(new_gold),
            )
            if not gold_ok:
                raise CommandError("골드 차감에 실패했습니다.")
            update_shop_stock(self.sheets_manager, pouch.row, pouch.stock - qty)

        # 뽑기 (중복 허용)
        drawn = [random.choice(candidates) for _ in range(qty)]
        logger.info(
            f"[가챠] @{context.user_id} ({title}) x{qty} cost={total_cost} "
            f"gold {current_gold}→{new_gold} 결과={drawn}"
        )

        # 이미지+텍스트 스레드 전송 (api 직접)
        header = (
            f"🎁 {title}님의 가챠파우치 개봉! ({qty}개)\n"
            f"골드 {current_gold} → {new_gold} (-{total_cost})"
        )
        self._send_gacha_result(context, drawn, header)

        # stream_handler 가 추가 전송하지 않도록 빈 응답.
        return CommandResponse.create_success('')

    def _read_gacha_candidates(self) -> List[str]:
        """상점 B50:B78 의 비어있지 않은 아이템명 리스트."""
        try:
            ws = self.sheets_manager.get_worksheet(WS_SHOP)
            col_b = ws.col_values(GACHA_LIST_COL)
        except Exception as e:
            logger.warning(f"[가챠] 후보 목록 조회 실패: {e}")
            return []
        result: List[str] = []
        for idx, value in enumerate(col_b, start=1):
            if idx < GACHA_LIST_ROW_START or idx > GACHA_LIST_ROW_END:
                continue
            name = (value or '').strip()
            if name:
                result.append(name)
        return result

    def _resolve_image_path(self, item_name: str) -> Optional[str]:
        """gacha_images/{아이템명}.{ext} 를 탐색. 없으면 None."""
        for ext in GACHA_IMAGE_EXTS:
            path = GACHA_IMAGE_DIR / f"{item_name}{ext}"
            if path.exists():
                return str(path)
        return None

    def _resolve_reply_visibility(self, original: str) -> str:
        """원본 visibility → 응답 visibility (stream_handler 규칙과 동일)."""
        if original == 'direct':
            return 'direct'
        if original == 'unlisted':
            return 'unlisted'
        return 'private'

    def _send_gacha_result(
        self, context: CommandContext, drawn: List[str], header: str,
    ) -> None:
        """뽑은 아이템을 이미지+텍스트로 스레드 전송 (이미지 4개/툿)."""
        metadata = context.metadata or {}
        reply_to = metadata.get('status_id')
        visibility = self._resolve_reply_visibility(metadata.get('visibility', 'public'))
        sender = context.user_id
        mention = f"@{sender} " if sender else ""

        # 4개씩 청크로 분할
        chunks = [
            drawn[i:i + MASTODON_MAX_MEDIA]
            for i in range(0, len(drawn), MASTODON_MAX_MEDIA)
        ]

        for chunk_idx, chunk in enumerate(chunks):
            base_index = chunk_idx * MASTODON_MAX_MEDIA
            media_ids: List[str] = []
            text_lines: List[str] = []
            for offset, name in enumerate(chunk):
                num = base_index + offset + 1
                path = self._resolve_image_path(name)
                if path:
                    try:
                        media = self.api.media_post(path, description=name)
                        media_ids.append(media['id'])
                        text_lines.append(f"{num}. {name}")
                    except Exception as e:
                        logger.warning(f"[가챠] 이미지 업로드 실패 ({name}): {e}")
                        text_lines.append(f"{num}. {name} (이미지 없음)")
                else:
                    text_lines.append(f"{num}. {name} (이미지 없음)")

            # 첫 청크에만 헤더
            body = (header + "\n\n" if chunk_idx == 0 else "") + "\n".join(text_lines)
            status_text = config.format_response(f"{mention}{body}")

            try:
                sent = self.api.status_post(
                    status=status_text,
                    in_reply_to_id=reply_to,
                    visibility=visibility,
                    media_ids=media_ids or None,
                )
                reply_to = sent['id']
            except Exception as e:
                logger.error(f"[가챠] 툿 전송 실패 (청크 {chunk_idx + 1}): {e}", exc_info=True)
                break

            if chunk_idx < len(chunks) - 1:
                time.sleep(0.5)
