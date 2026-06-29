"""
[골드 정산] — 마지막 정산 이후 늘어난 게시글 수(DM 제외) / 100마다 20G 지급.

동작:
1) 마스토돈 API 로 사용자 계정을 조회 → account_id.
2) 마지막 정산의 status_id 를 since_id 로 사용해 새 statuses 만 페이지네이션.
3) visibility != 'direct' 인 status 만 카운트.
4) 누적 카운트(last_count + new_non_dm) 와 차이를 계산해 골드 지급.
5) 새 last_count / last_status_id 를 JSON 에 영속화.

상태 파일: data/gold_settlement.json — 캐릭터별 (last_count, last_status_id).

성능 메모:
- 첫 정산은 since_id 가 없어 모든 statuses 를 페이지로 받아야 함.
  안전을 위해 _MAX_PAGES = 100 페이지(=최대 4000글)까지만 가져온다.
  그 이상 글이 있어도 4000글 기준으로 처리되고, 다음 정산부터 정상화.
- 두 번째 정산부터는 since_id 가 마지막 status_id 라 새 글만 받아 빠름.

출력:
    [골드 정산] 현재 툿 수: 782 / 이전 정산 툿 수: 351 ─ [취득 골드: 80G]
"""

from __future__ import annotations

from typing import Any, List

from commands.base_command import BaseCommand, CommandContext, CommandResponse
from commands.registry import register_command
from commands.trpg_common.fallback_helpers import acquire_user_lock
from utils.decorators import handle_command_errors
from utils.error_handling import CommandError
from utils.gold_settlement import get_record, set_record
from utils.logging_config import logger
from utils.shared_sheet import (
    EQUIP_COL_GOLD,
    EQUIP_DATA_START_ROW,
    WS_EQUIP_STOCK,
    find_character_row,
    read_int_cell,
)


# 정산 단위
_GOLD_PER_BUCKET = 20
_POSTS_PER_BUCKET = 100

# 페이지네이션 안전장치 (첫 정산 시 무한 호출 방지)
_PAGE_LIMIT = 40
_MAX_PAGES = 100   # 4000 statuses 까지 처리


def _attr(obj: Any, key: str, default=None):
    """mastodon.py 응답은 dict-like(AttribAccessDict). 둘 다 안전 처리."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


@register_command(
    name="골드 정산",
    aliases=["골드정산"],
    description="툿 수(DM 제외)/100 × 20G 지급. 마지막 정산 이후 증가분만 계산.",
    category="레이드",
    examples=["[골드 정산]"],
    requires_sheets=True,
    requires_api=True,
    priority=10,
)
class SettlementCommand(BaseCommand):

    @handle_command_errors(
        system_tag="골드 정산",
        user_error_message="골드 정산 처리 중 오류가 발생했습니다.",
    )
    def execute(self, context: CommandContext) -> CommandResponse:
        title = (context.user_name or '').strip()
        if not title:
            raise CommandError("마스토돈 표시명(=칭호)을 확인할 수 없습니다.")

        user_id = context.user_id
        if not user_id:
            raise CommandError("발신자 acct 를 확인할 수 없습니다.")

        # 1) 시트 행 사전 확인 (없으면 API 호출 낭비 방지)
        equip_row = find_character_row(
            self.sheets_manager, WS_EQUIP_STOCK, title, EQUIP_DATA_START_ROW,
        )
        if equip_row is None:
            raise CommandError(
                f"'장비 및 주식' 시트에서 '{title}' 캐릭터를 찾을 수 없습니다."
            )

        # 2) 마스토돈 계정 조회
        account_id = self._resolve_account_id(user_id)

        # 3) 새 statuses 페이지네이션
        prev_count, prev_status_id = get_record(user_id)
        new_statuses = self._fetch_new_statuses(account_id, prev_status_id)

        # 4) DM 제외 카운트
        new_non_dm = sum(
            1 for s in new_statuses
            if _attr(s, 'visibility', '') != 'direct'
        )
        new_count = prev_count + new_non_dm
        delta = new_non_dm  # 증가분 = 새 글 중 DM 제외 수
        bonus = (delta // _POSTS_PER_BUCKET) * _GOLD_PER_BUCKET

        # 5) 최신 status_id (가장 첫 항목 = 가장 최신). 새 글 없으면 이전 값 유지.
        if new_statuses:
            latest_status_id = str(_attr(new_statuses[0], 'id', prev_status_id))
        else:
            latest_status_id = prev_status_id

        # 6) 시트 골드 가산 + 상태 저장 (락 안에서)
        with acquire_user_lock(user_id, timeout=10.0):
            current_gold = read_int_cell(
                self.sheets_manager, WS_EQUIP_STOCK, equip_row, EQUIP_COL_GOLD,
            )
            new_gold = current_gold + bonus
            if bonus > 0:
                ok = self.sheets_manager.update_cell(
                    WS_EQUIP_STOCK, equip_row, EQUIP_COL_GOLD, str(new_gold),
                )
                if not ok:
                    raise CommandError("골드 갱신을 시트에 저장하지 못했습니다.")
            # 골드 변동이 없어도 카운트는 반드시 진척시켜야 함 — 안 그러면
            # 다음 정산이 또 같은 글을 카운트해 중복 지급될 수 있다.
            set_record(user_id, new_count, latest_status_id)

        logger.info(
            f"[골드 정산] @{user_id} ({title}) prev={prev_count} new={new_count} "
            f"delta={delta} bonus={bonus}G gold={current_gold}→{new_gold}"
        )

        message = (
            f"[골드 정산] 현재 툿 수: {new_count} / 이전 정산 툿 수: {prev_count} "
            f"─ [취득 골드: {bonus}G]"
        )
        return CommandResponse.create_success(
            message,
            data={
                'count_before': prev_count,
                'count_after': new_count,
                'delta': delta,
                'bonus': bonus,
                'gold_before': current_gold,
                'gold_after': new_gold,
            },
        )

    # ------------------------------------------------------------------
    def _resolve_account_id(self, acct: str) -> Any:
        """`acct` → 마스토돈 account_id. lookup 우선, 실패 시 search 폴백."""
        if self.api is None:
            raise CommandError("마스토돈 API 가 연결되어 있지 않습니다.")

        # 1차: account_lookup (mastodon.py 1.5+ / Mastodon 3.4+)
        lookup = getattr(self.api, 'account_lookup', None)
        if callable(lookup):
            try:
                acc = lookup(acct)
                acc_id = _attr(acc, 'id')
                if acc_id is not None:
                    return acc_id
            except Exception as e:
                logger.debug(f"[정산] account_lookup 실패 — search 폴백: {e}")

        # 2차: account_search
        try:
            results = self.api.account_search(acct, limit=5, resolve=True)
        except Exception as e:
            raise CommandError(f"마스토돈 계정 조회 실패: {e}")

        for acc in results or []:
            acc_acct = (_attr(acc, 'acct', '') or '').lower()
            if acc_acct == acct.lower():
                acc_id = _attr(acc, 'id')
                if acc_id is not None:
                    return acc_id

        # 정확 일치 없으면 첫 결과 (best-effort)
        if results:
            acc_id = _attr(results[0], 'id')
            if acc_id is not None:
                return acc_id

        raise CommandError(f"마스토돈 계정 '{acct}' 을(를) 찾을 수 없습니다.")

    def _fetch_new_statuses(
        self,
        account_id: Any,
        since_id,
    ) -> List[Any]:
        """`since_id` 이후의 statuses 를 페이지네이션으로 모아 반환.

        반환 순서: 최신 → 과거 (마스토돈 API 표준 순서 그대로 누적).
        """
        results: List[Any] = []
        max_id = None
        for page_idx in range(_MAX_PAGES):
            kwargs = {'limit': _PAGE_LIMIT}
            if since_id:
                kwargs['since_id'] = since_id
            if max_id is not None:
                kwargs['max_id'] = max_id
            try:
                page = self.api.account_statuses(account_id, **kwargs)
            except Exception as e:
                raise CommandError(f"마스토돈 글 목록 조회 실패: {e}")

            if not page:
                break
            results.extend(page)
            last_id = _attr(page[-1], 'id')
            if not last_id:
                break
            max_id = last_id
            if len(page) < _PAGE_LIMIT:
                break

        if len(results) >= _MAX_PAGES * _PAGE_LIMIT:
            logger.warning(
                f"[정산] account={account_id} 페이지 한도 도달 "
                f"({_MAX_PAGES} 페이지 = {_MAX_PAGES * _PAGE_LIMIT}글). "
                f"누락된 과거 글은 이번 정산에서 제외됨."
            )
        return results
