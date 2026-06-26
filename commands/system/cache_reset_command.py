"""
시트 업데이트 명령어

분기:
    [시트 업데이트]              — 디폴트: 도움말 + 커스텀 + 워크시트 핸들 캐시
    [시트 업데이트/도움말]       — 도움말 페이지만
    [시트 업데이트/커스텀]       — '커스텀' 페이지만
    [시트 업데이트/캐릭터]       — 워크시트 핸들 LRU 캐시만 (시트 추가·삭제·이름변경 반영)
    [시트 업데이트/전체]         — 위 3종 모두

누구나 사용 가능 — 시트를 수정한 직후 봇이 즉시 새 내용을 읽도록 강제할 때 사용.
"""

import os
import sys
from typing import List

try:
    from utils.logging_config import logger
    from utils.cache_manager import bot_cache
    from commands.base_command import BaseCommand, CommandContext, CommandResponse
    from commands.registry import register_command
except ImportError as e:
    import logging
    logger = logging.getLogger('sheet_update_command')
    raise ImportError(f"필수 모듈 임포트 실패: {e}")


# 허용 대상.
_TARGET_HELP = '도움말'
_TARGET_CHARACTER = '캐릭터'
_TARGET_CUSTOM = '커스텀'
_TARGET_ALL = '전체'

# 디폴트(no arg) = 도움말 + 커스텀 + 워크시트 핸들 캐시
_TARGET_DEFAULT = '__default__'

_TARGET_ALIASES = {
    _TARGET_HELP: _TARGET_HELP,
    _TARGET_CHARACTER: _TARGET_CHARACTER,
    '플레이어': _TARGET_CHARACTER,
    '캐릭터시트': _TARGET_CHARACTER,
    _TARGET_CUSTOM: _TARGET_CUSTOM,
    '커스텀명령어': _TARGET_CUSTOM,
    _TARGET_ALL: _TARGET_ALL,
}


def _normalize_target(value: str) -> str:
    return ''.join(value.split()).lower()


@register_command(
    name="시트 업데이트",
    aliases=[
        "시트업데이트",
        "캐시 리셋", "캐시리셋", "캐시 초기화", "캐시초기화",
    ],
    description="시트 캐시 새로고침 (인자 없음=도움말+커스텀+워크시트 핸들)",
    examples=[
        "[시트 업데이트]",
        "[시트 업데이트/도움말]",
        "[시트 업데이트/커스텀]",
        "[시트 업데이트/캐릭터]",
        "[시트 업데이트/전체]",
    ],
    category="시스템",
    admin_only=False,
    requires_sheets=True,
)
class CacheResetCommand(BaseCommand):

    def __init__(self, sheets_manager=None, api=None, **kwargs):
        super().__init__(sheets_manager=sheets_manager, api=api, **kwargs)

    def execute(self, context: CommandContext) -> CommandResponse:
        try:
            target = self._resolve_target(context)
            if target is None:
                provided = context.keywords[1].strip() if len(context.keywords) >= 2 else ''
                return CommandResponse.create_error(
                    f"'{provided}'은(는) 사용할 수 없는 옵션입니다.\n"
                    "사용 가능한 옵션: 도움말, 커스텀, 캐릭터, 전체"
                )

            run_help = target in (_TARGET_HELP, _TARGET_DEFAULT, _TARGET_ALL)
            run_custom = target in (_TARGET_CUSTOM, _TARGET_DEFAULT, _TARGET_ALL)
            run_character = target in (_TARGET_CHARACTER, _TARGET_DEFAULT, _TARGET_ALL)

            messages: List[str] = []
            if run_help:
                messages.append(self._reset_help_cache())
            if run_custom:
                messages.append(self._reset_custom_command_cache())
            if run_character:
                messages.append(self._reset_worksheet_handles())
            # shared_sheet 캐릭터 행 lookup 캐시는 항상 같이 비움 (저비용).
            self._reset_shared_row_cache()

            return CommandResponse.create_success("\n".join(messages))

        except Exception as e:
            logger.error(f"시트 업데이트 실패: {e}", exc_info=True)
            return CommandResponse.create_error(
                "시트 업데이트 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
                error=e,
            )

    @staticmethod
    def _resolve_target(context: CommandContext):
        if len(context.keywords) < 2:
            return _TARGET_DEFAULT
        raw = context.keywords[1].strip()
        if not raw:
            return _TARGET_DEFAULT
        return _TARGET_ALIASES.get(_normalize_target(raw))

    # ------------------------------------------------------------------
    def _reset_help_cache(self) -> str:
        bot_cache.invalidate_help_cache()
        if not self.sheets_manager:
            return "도움말 시트 캐시 삭제 완료 (시트 미연결)"
        try:
            help_items = self.sheets_manager.get_help_items()
            bot_cache.cache_help_items(help_items)
            return f"도움말 시트 업데이트 완료 ({len(help_items)}개 항목)"
        except Exception as e:
            logger.error(f"도움말 데이터 로드 실패: {e}")
            return "도움말 시트 캐시 삭제 완료 — 데이터 재로드는 다음 요청 시 자동 시도됩니다."

    def _reset_custom_command_cache(self) -> str:
        if not self.sheets_manager:
            return "커스텀 명령어 캐시 삭제 완료 (시트 미연결)"
        try:
            self.sheets_manager.invalidate_custom_command_cache()
            count = self.sheets_manager.warmup_custom_command()
            return f"커스텀 명령어 업데이트 완료 ({count}개 명령어)"
        except Exception as e:
            logger.error(f"커스텀 명령어 데이터 로드 실패: {e}")
            return "커스텀 명령어 캐시 삭제 완료 — 데이터 재로드는 다음 요청 시 자동 시도됩니다."

    def _reset_worksheet_handles(self) -> str:
        """워크시트 핸들 LRU 캐시 비움. 시트 추가/삭제/이름변경 반영용."""
        if not self.sheets_manager:
            return "워크시트 핸들 캐시 삭제 완료 (시트 미연결)"
        try:
            self.sheets_manager.invalidate_worksheets_cache()
            return "워크시트 핸들 캐시 갱신 완료"
        except Exception as e:
            logger.error(f"워크시트 핸들 캐시 갱신 실패: {e}")
            return "워크시트 핸들 캐시 삭제 시도 완료"

    def _reset_shared_row_cache(self) -> None:
        """shared_sheet 캐릭터 행 lookup 캐시 비움 (정렬 변경 즉시 반영)."""
        try:
            from utils.shared_sheet import invalidate_row_cache
            invalidate_row_cache()
        except Exception as e:
            logger.debug(f"shared row 캐시 무효화 실패 (무시): {e}")

    @staticmethod
    def get_supported_keywords() -> List[str]:
        return [
            '시트 업데이트', '시트업데이트',
            '캐시 리셋', '캐시리셋', '캐시 초기화', '캐시초기화',
        ]
