"""
시트 업데이트 명령어 (CoC 봇)

분기:
    [시트 업데이트]              — 이 봇의 시트만 갱신: 도움말 + 캐릭터 워크시트
                                    (보조 시트인 랜덤표/커스텀은 건드리지 않음)
    [시트 업데이트/도움말]       — 도움말 시트만
    [시트 업데이트/캐릭터]       — 캐릭터 워크시트 캐시만 (alias: 플레이어)
    [시트 업데이트/랜덤표]       — 랜덤표 시트만 (네 봇 공유 보조 시트)
    [시트 업데이트/커스텀]       — 커스텀 명령어 시트만 (네 봇 공유 보조 시트)
    [시트 업데이트/전체]         — 위 4종 모두

`도움말`/`캐릭터` 가 디폴트인 이유: 운영 중 가장 자주 갱신되는 데이터가 이 둘이고,
보조 시트(랜덤표/커스텀) 는 변경 빈도가 낮아 자동 갱신 대상에서 제외. 보조 시트는
명시 입력했을 때만 캐시가 비워진다.

누구나 사용 가능 — 시트를 수정한 직후 봇이 즉시 새 내용을 읽도록 강제할 때 사용.
"""

import os
import sys
from typing import List

try:
    from config.settings import config
    from utils.logging_config import logger
    from utils.cache_manager import bot_cache
    from commands.base_command import BaseCommand, CommandContext, CommandResponse
    from commands.registry import register_command
except ImportError as e:
    import logging
    logger = logging.getLogger('sheet_update_command')
    raise ImportError(f"필수 모듈 임포트 실패: {e}")


# 허용 대상. 키워드는 정규화 비교(소문자, 공백 제거) — 사용자 오타 흡수.
_TARGET_HELP = '도움말'
_TARGET_CHARACTER = '캐릭터'
_TARGET_RANDOM = '랜덤표'
_TARGET_CUSTOM = '커스텀'
_TARGET_ALL = '전체'

# 디폴트(no arg) — 이 봇의 시트만 갱신: 도움말 + 캐릭터.
_TARGET_DEFAULT = '__default__'

_TARGET_ALIASES = {
    _TARGET_HELP: _TARGET_HELP,
    _TARGET_CHARACTER: _TARGET_CHARACTER,
    '플레이어': _TARGET_CHARACTER,
    '캐릭터시트': _TARGET_CHARACTER,
    _TARGET_RANDOM: _TARGET_RANDOM,
    '랜덤': _TARGET_RANDOM,
    _TARGET_CUSTOM: _TARGET_CUSTOM,
    '커스텀명령어': _TARGET_CUSTOM,
    _TARGET_ALL: _TARGET_ALL,
}


def _normalize_target(value: str) -> str:
    """키워드 매칭용 정규화: 공백 제거 + 소문자."""
    return ''.join(value.split()).lower()


@register_command(
    name="시트 업데이트",
    aliases=[
        "시트업데이트",
        # 옛 이름 — 외부 도움말/스크립트 호환을 위해 유지.
        "캐시 리셋", "캐시리셋", "캐시 초기화", "캐시초기화",
    ],
    description=(
        "이 봇 시트의 캐시를 새로고침합니다 "
        "(인자 없음=도움말+캐릭터, /랜덤표·/커스텀 은 명시했을 때만)."
    ),
    examples=[
        "[시트 업데이트]",
        "[시트 업데이트/도움말]",
        "[시트 업데이트/캐릭터]",
        "[시트 업데이트/랜덤표]",
        "[시트 업데이트/커스텀]",
        "[시트 업데이트/전체]",
    ],
    category="시스템",
    admin_only=False,
    requires_sheets=True,
)
class CacheResetCommand(BaseCommand):
    """봇 시트 + 보조 시트 캐시를 무효화하고 즉시 재로드.

    클래스 이름은 옛 import 경로(`CacheResetCommand`) 유지를 위해 그대로 둠.
    """

    def __init__(self, sheets_manager=None, api=None, **kwargs):
        super().__init__(sheets_manager=sheets_manager, api=api, **kwargs)

    def execute(self, context: CommandContext) -> CommandResponse:
        try:
            target = self._resolve_target(context)
            if target is None:
                provided = context.keywords[1].strip() if len(context.keywords) >= 2 else ''
                return CommandResponse.create_error(
                    f"'{provided}'은(는) 사용할 수 없는 옵션입니다.\n"
                    "사용 가능한 옵션: 도움말, 캐릭터, 랜덤표, 커스텀, 전체"
                )

            # 디폴트와 /전체 의 분기 단계를 명시. 디폴트엔 보조 시트 미포함.
            run_help = target in (_TARGET_HELP, _TARGET_DEFAULT, _TARGET_ALL)
            run_character = target in (_TARGET_CHARACTER, _TARGET_DEFAULT, _TARGET_ALL)
            run_random = target in (_TARGET_RANDOM, _TARGET_ALL)
            run_custom = target in (_TARGET_CUSTOM, _TARGET_ALL)

            messages: List[str] = []
            if run_help:
                messages.append(self._reset_help_cache())
            if run_character:
                messages.append(self._reset_character_sheets())
            if run_random:
                messages.append(self._reset_random_table_cache())
            if run_custom:
                messages.append(self._reset_custom_command_cache())

            return CommandResponse.create_success("\n".join(messages))

        except Exception as e:
            logger.error(f"시트 업데이트 실패: {e}", exc_info=True)
            return CommandResponse.create_error(
                "시트 업데이트 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
                error=e,
            )

    @staticmethod
    def _resolve_target(context: CommandContext):
        """키워드에서 대상을 결정.

        인자 없음 = 디폴트(도움말+캐릭터). 알 수 없는 값 = None.
        """
        if len(context.keywords) < 2:
            return _TARGET_DEFAULT
        raw = context.keywords[1].strip()
        if not raw:
            return _TARGET_DEFAULT
        return _TARGET_ALIASES.get(_normalize_target(raw))

    # ------------------------------------------------------------------
    # 도움말
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

    # ------------------------------------------------------------------
    # 캐릭터 워크시트
    # ------------------------------------------------------------------
    def _reset_character_sheets(self) -> str:
        """이 봇 시트의 워크시트 핸들 LRU 캐시를 비우고 카운트 회신.

        캐릭터 데이터 자체는 매 요청마다 시트에서 직접 읽으므로 별도 데이터
        캐시는 없다. 워크시트 핸들 캐시만 비우면 시트 추가/이름변경/삭제가
        다음 요청부터 즉시 반영된다.
        """
        if not self.sheets_manager:
            return "캐릭터 워크시트 캐시 삭제 완료 (시트 미연결)"
        try:
            self.sheets_manager.invalidate_worksheets_cache()
            count = self.sheets_manager.count_character_worksheets()
            if count < 0:
                return "캐릭터 워크시트 캐시 갱신 (시트 목록 조회 실패)"
            return f"캐릭터 워크시트 업데이트 완료 ({count}개 워크시트)"
        except Exception as e:
            logger.error(f"캐릭터 워크시트 갱신 실패: {e}")
            return "캐릭터 워크시트 캐시 삭제 완료 — 데이터 재로드는 다음 요청 시 자동 시도됩니다."

    # ------------------------------------------------------------------
    # 랜덤표
    # ------------------------------------------------------------------
    def _reset_random_table_cache(self) -> str:
        if not self.sheets_manager:
            return "랜덤표 시트 캐시 삭제 완료 (시트 미연결)"
        if not getattr(config, 'RANDOM_TABLE_SHEET_ID', ''):
            return "랜덤표 시트가 설정되어 있지 않습니다."
        try:
            self.sheets_manager.invalidate_random_table_cache()
            count = self.sheets_manager.warmup_random_table()
            return f"랜덤표 시트 업데이트 완료 ({count}개 워크시트)"
        except Exception as e:
            logger.error(f"랜덤표 데이터 로드 실패: {e}")
            return "랜덤표 시트 캐시 삭제 완료 — 데이터 재로드는 다음 요청 시 자동 시도됩니다."

    # ------------------------------------------------------------------
    # 커스텀
    # ------------------------------------------------------------------
    def _reset_custom_command_cache(self) -> str:
        if not self.sheets_manager:
            return "커스텀 명령어 시트 캐시 삭제 완료 (시트 미연결)"
        if not getattr(config, 'CUSTOM_COMMAND_SHEET_ID', ''):
            return "커스텀 명령어 시트가 설정되어 있지 않습니다."
        try:
            self.sheets_manager.invalidate_custom_command_cache()
            count = self.sheets_manager.warmup_custom_command()
            return f"커스텀 명령어 시트 업데이트 완료 ({count}개 명령어)"
        except Exception as e:
            logger.error(f"커스텀 명령어 데이터 로드 실패: {e}")
            return "커스텀 명령어 시트 캐시 삭제 완료 — 데이터 재로드는 다음 요청 시 자동 시도됩니다."

    @staticmethod
    def get_supported_keywords() -> List[str]:
        return [
            '시트 업데이트', '시트업데이트',
            '캐시 리셋', '캐시리셋', '캐시 초기화', '캐시초기화',
        ]
