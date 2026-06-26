"""
랜덤 선택 명령어 구현
주어진 옵션 중 하나를 무작위로 선택합니다.
"""

import os
import sys
import random
from typing import List, Optional

# 경로 설정 (VM 환경 대응)
try:
    from config.settings import config
    from utils.logging_config import logger
    from utils.error_handling import CommandError
    from commands.base_command import BaseCommand, CommandContext, CommandResponse
    from commands.registry import register_command
except ImportError as e:
    import logging
    logger = logging.getLogger('random_command')
    logger.error(f"필수 모듈 임포트 실패: {e}")
    raise


_MAX_OPTIONS = 40


@register_command(
    name="랜덤",
    aliases=["random", "무작위", "선택"],
    description=f"주어진 옵션 중 하나를 랜덤으로 선택합니다. (최대 {_MAX_OPTIONS}개)",
    category="게임",
    examples=[
        "[랜덤/사과, 바나나, 포도]",
        "[선택/사과, 바나나, 포도]",
        "[랜덤/예, 아니오]",
    ],
    requires_sheets=False,
    requires_api=False
)
class RandomCommand(BaseCommand):
    """
    랜덤 선택 명령어 클래스

    주어진 옵션들 중 하나를 무작위로 선택하여 반환합니다.

    지원하는 형식:
    - [랜덤/옵션1, 옵션2, 옵션3] : 옵션 중 하나를 랜덤 선택
    - [무작위/A, B, C, D] : 옵션 중 하나를 랜덤 선택
    """

    @staticmethod
    def get_supported_keywords() -> List[str]:
        """지원 키워드 (대표 우선)"""
        return ['랜덤', 'random', '무작위', '선택']

    def __init__(self, sheets_manager=None, api=None, **kwargs):
        """
        RandomCommand 초기화

        Args:
            sheets_manager: Google Sheets 관리자 (사용 안 함)
            api: 마스토돈 API 인스턴스 (사용 안 함)
            **kwargs: 추가 의존성
        """
        super().__init__(sheets_manager, api, **kwargs)
        logger.debug("RandomCommand 초기화 완료")

    def execute(self, context: CommandContext) -> CommandResponse:
        """
        랜덤 선택 명령어 실행

        Args:
            context: 명령어 실행 컨텍스트

        Returns:
            CommandResponse: 실행 결과
        """
        try:
            # 1. 옵션 파싱
            options = self._parse_options(context.keywords)

            if not options:
                return CommandResponse.create_error(
                    "옵션을 입력해 주세요. 예: [랜덤/A, B, C]"
                )

            # 2. 랜덤 선택
            selected = random.choice(options)

            logger.debug(f"랜덤 선택: {selected} (옵션 {len(options)}개 중)")

            # 3. 결과 반환
            return CommandResponse.create_success(selected, data={
                'selected': selected,
                'options': options,
                'total_count': len(options)
            })

        except CommandError as e:
            # 비즈니스 예외
            return CommandResponse.create_error(str(e), error=e)
        except Exception as e:
            # 시스템 예외
            logger.error(f"랜덤 선택 명령어 실행 오류: {e}", exc_info=True)
            return CommandResponse.create_error("랜덤 선택 중 오류가 발생했습니다.", error=e)

    def _parse_options(self, keywords: List[str]) -> List[str]:
        """
        키워드에서 옵션 리스트 추출

        Args:
            keywords: 키워드 리스트

        Returns:
            List[str]: 옵션 리스트

        Raises:
            CommandError: 파싱 실패
        """
        if len(keywords) < 2:
            raise CommandError("옵션을 입력해 주세요. 예: [랜덤/A, B, C]")

        # keywords[1]부터 모든 키워드를 합쳐서 처리
        # 예: ["랜덤", "사과, 바나나, 포도"] 또는 ["랜덤", "사과", " 바나나", " 포도"]

        # 모든 키워드를 합침 (첫 번째 제외)
        combined = ', '.join(keywords[1:])

        # 컴마로 분리하고 공백 제거
        options = [opt.strip() for opt in combined.split(',') if opt.strip()]

        if len(options) > _MAX_OPTIONS:
            raise CommandError(
                f"옵션은 최대 {_MAX_OPTIONS}개까지 입력할 수 있습니다. "
                f"(입력: {len(options)}개)"
            )

        logger.debug(f"파싱된 옵션: {options}")
        return options


# 유틸리티 함수

def is_random_command(keyword: str) -> bool:
    """
    키워드가 랜덤 명령어인지 확인

    Args:
        keyword: 확인할 키워드

    Returns:
        bool: 랜덤 명령어 여부
    """
    if not keyword:
        return False

    keyword = keyword.lower().strip()
    return keyword in ['랜덤', 'random', '무작위', '선택']


def create_random_command() -> RandomCommand:
    """
    랜덤 명령어 인스턴스 생성

    Returns:
        RandomCommand: 랜덤 명령어 인스턴스
    """
    return RandomCommand()
