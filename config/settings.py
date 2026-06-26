"""
런타임 설정 (CoC 봇)

`config/defaults.py` 의 상수를 읽고 환경 변수로 필요한 것만 오버라이드한다.
`.env` 가 있으면 프로세스 환경에 로드한 뒤 `Config` 클래스로 노출.
"""

import os
import sys
from pathlib import Path
from typing import Optional

from config import defaults
from utils.env_loader import read_env_file


def _load_env() -> None:
    """프로젝트 루트의 .env 파일을 읽어 환경 변수로 주입.

    파싱 규칙은 `utils/env_loader.read_env_file` 가 정본 — 인라인 주석/따옴표 처리가
    한 곳에서 관리되어 동작 차이가 없도록 통합.
    """
    base_dir = Path(__file__).parent.parent
    env_path = base_dir / '.env'
    for key, value in read_env_file(env_path).items():
        os.environ.setdefault(key, value)


_load_env()


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() == 'true'


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _resolve_decoration_char() -> str:
    """`DECORATION_CHAR` 환경 변수를 해석.

    - 미설정(None) → 기본값(✦)
    - 빈 문자열 또는 공백만 → '' (사용자가 명시적으로 장식 생략)
    - 그 외 → 공백 제거한 문자열
    """
    raw = os.getenv('DECORATION_CHAR')
    if raw is None:
        return defaults.DECORATION_CHAR
    return raw.strip()


class Config:
    """CoC 봇 런타임 설정."""

    BASE_DIR = Path(__file__).parent.parent

    # ------------------------------------------------------------------
    # 응답 장식 (특수문자 + 위치)
    # ------------------------------------------------------------------
    DECORATION_CHAR: str = _resolve_decoration_char()
    DECORATION_BOTH_SIDES: bool = _env_bool(
        'DECORATION_BOTH_SIDES', defaults.DECORATION_BOTH_SIDES,
    )

    # RESPONSE_PREFIX 는 멘션과 본문 사이의 줄바꿈 용도. 장식 문자는 본문 안에서
    # 처리되므로 프리픽스 자체는 단순 `\n` 이 기본값.
    RESPONSE_PREFIX: str = os.getenv('RESPONSE_PREFIX') or defaults.RESPONSE_PREFIX

    # ------------------------------------------------------------------
    # Mastodon API
    # ------------------------------------------------------------------
    MASTODON_CLIENT_ID: str = os.getenv('MASTODON_CLIENT_ID', '')
    MASTODON_CLIENT_SECRET: str = os.getenv('MASTODON_CLIENT_SECRET', '')
    MASTODON_ACCESS_TOKEN: str = os.getenv('MASTODON_ACCESS_TOKEN', '')
    MASTODON_API_BASE_URL: str = os.getenv('MASTODON_API_BASE_URL', '')

    # ------------------------------------------------------------------
    # Google Sheets
    # ------------------------------------------------------------------
    GOOGLE_CREDENTIALS_PATH: str = os.getenv(
        'GOOGLE_CREDENTIALS_PATH',
        str(BASE_DIR / 'credentials.json'),
    )
    SHEET_ID: str = os.getenv('SHEET_ID', '')

    # ------------------------------------------------------------------
    # 공유 보조 시트 (모든 봇이 참조)
    # ------------------------------------------------------------------
    RANDOM_TABLE_SHEET_ID: str = os.getenv('RANDOM_TABLE_SHEET_ID', '').strip()
    CUSTOM_COMMAND_SHEET_ID: str = os.getenv('CUSTOM_COMMAND_SHEET_ID', '').strip()
    CUSTOM_COMMAND_WORKSHEET: str = os.getenv('CUSTOM_COMMAND_WORKSHEET', '커스텀').strip() or '커스텀'

    # ------------------------------------------------------------------
    # 가동 기간 (KST, YYYY-MM-DD; 빈 값은 무제한)
    # ------------------------------------------------------------------
    OPERATION_START_DATE: str = os.getenv('OPERATION_START_DATE', '').strip()
    OPERATION_END_DATE: str = os.getenv('OPERATION_END_DATE', '').strip()

    # ------------------------------------------------------------------
    # 게임/메시지 (defaults.py 오버라이드 허용)
    # ------------------------------------------------------------------
    MAX_DICE_COUNT: int = _env_int('BOT_MAX_DICE_COUNT', defaults.MAX_DICE_COUNT)
    MAX_DICE_SIDES: int = _env_int('BOT_MAX_DICE_SIDES', defaults.MAX_DICE_SIDES)
    MAX_MESSAGE_LENGTH: int = _env_int('MAX_MESSAGE_LENGTH', defaults.MAX_MESSAGE_LENGTH)
    # `MAX_MESSAGE_LENGTH * MESSAGE_SAFE_RATIO` 가 단일 툿 한도. 초과 시 분할.
    MESSAGE_SAFE_RATIO: float = defaults.MESSAGE_SAFE_RATIO

    @classmethod
    def safe_message_length(cls) -> int:
        """안전 임계 = MAX * SAFE_RATIO. 단일 툿/청크 본문의 상한 계산용."""
        return max(50, int(cls.MAX_MESSAGE_LENGTH * cls.MESSAGE_SAFE_RATIO))

    # ------------------------------------------------------------------
    # 네트워킹
    # ------------------------------------------------------------------
    MAX_RETRIES: int = _env_int('BOT_MAX_RETRIES', defaults.MAX_RETRIES)
    BASE_WAIT_TIME: int = _env_int('BOT_BASE_WAIT_TIME', defaults.BASE_WAIT_SECONDS)

    # ------------------------------------------------------------------
    # 관리자
    # ------------------------------------------------------------------
    SYSTEM_ADMIN_ID: str = os.getenv('SYSTEM_ADMIN_ID', '')

    # ------------------------------------------------------------------
    # 로깅 (사용자 보통 수정 불필요 — defaults 사용)
    # ------------------------------------------------------------------
    LOG_LEVEL: str = os.getenv('LOG_LEVEL', defaults.LOG_LEVEL)
    LOG_FILE_PATH: str = os.getenv('LOG_FILE_PATH', 'logs/bot.log')
    LOG_MAX_BYTES: int = _env_int('LOG_MAX_BYTES', defaults.LOG_MAX_BYTES)
    LOG_BACKUP_COUNT: int = _env_int('LOG_BACKUP_COUNT', defaults.LOG_BACKUP_COUNT)
    ENABLE_CONSOLE_LOG: bool = _env_bool('ENABLE_CONSOLE_LOG', defaults.ENABLE_CONSOLE_LOG)

    # ------------------------------------------------------------------
    # 캐시
    # ------------------------------------------------------------------
    CACHE_TTL: int = _env_int('CACHE_TTL', defaults.CACHE_TTL_SECONDS)

    # ------------------------------------------------------------------
    # 폴링
    # ------------------------------------------------------------------
    POLLING_INTERVAL: int = _env_int('POLLING_INTERVAL', defaults.POLLING_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # 마스토돈 Markdown 렌더링 지원 여부 (한참 등 일부 인스턴스만 지원)
    # ------------------------------------------------------------------
    MARKDOWN_ENABLED: bool = _env_bool('MARKDOWN_ENABLED', defaults.MARKDOWN_ENABLED)

    # ------------------------------------------------------------------
    # 디버그
    # ------------------------------------------------------------------
    DEBUG_MODE: bool = _env_bool('DEBUG_MODE', False)

    # ------------------------------------------------------------------
    # 워크시트 이름
    # ------------------------------------------------------------------
    WORKSHEET_NAMES = {
        'HELP': os.getenv('HELP_SHEET', defaults.DEFAULT_HELP_SHEET),
    }

    # 캐시 가능한 워크시트 (현재 도움말만)
    CACHEABLE_WORKSHEETS = ['도움말']

    # 시스템 키워드
    SYSTEM_KEYWORDS = ['도움말', '다이스', '랜덤', 'yn', 'YN']

    # ------------------------------------------------------------------
    # 메시지 상수
    # ------------------------------------------------------------------
    ERROR_MESSAGES = {
        'USER_NOT_FOUND': '등록된 캐릭터 시트가 없습니다. 본인 아이디와 같은 이름의 워크시트 탭을 시트에 추가해 주세요.',
        'USER_ID_CHECK_FAILED': '사용자 정보를 일시적으로 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.',
        'USER_NAME_INVALID': '사용자 정보가 올바르지 않습니다.',
        'TEMPORARY_ERROR': '일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.',
        'UNKNOWN_COMMAND': '사용 가능한 명령어가 아닙니다. [도움말]을 입력해 명령어 목록을 확인해 주세요.',
        'DICE_FORMAT_ERROR': (
            '주사위 형식이 올바르지 않습니다. '
            '예: [2d6], [1d6<4] (4 이하 성공), [3d10>7] (7 이상 성공)'
        ),
        'DICE_COUNT_LIMIT': '주사위 개수는 최대 {max}개까지 가능합니다.',
        'DICE_SIDES_LIMIT': '주사위 면수는 최대 {max}면까지 가능합니다.',
        'SHEET_NOT_FOUND': (
            '시트에 접근할 수 없습니다. '
            '시트 ID 와 서비스 계정 공유 권한을 확인해 주세요. '
            '문제가 계속되면 운영자에게 문의해 주세요.'
        ),
        'DATA_NOT_FOUND': '요청하신 데이터를 찾을 수 없습니다. 시트에 해당 항목이 있는지 확인해 주세요.',
    }
    SUCCESS_MESSAGES = {
        'SHEET_CONNECTED': '스프레드시트 연결 성공',
        'AUTH_SUCCESS': 'auth success',
        'STREAMING_START': 'Mastodon 스트리밍 시작',
        'ERROR_NOTIFICATION_SENT': '오류 알림 전송 완료',
    }

    # ------------------------------------------------------------------
    # 편의 메서드
    # ------------------------------------------------------------------
    @classmethod
    def get_credentials_path(cls) -> Path:
        cred = Path(cls.GOOGLE_CREDENTIALS_PATH)
        return cred if cred.is_absolute() else cls.BASE_DIR / cred

    @classmethod
    def is_system_keyword(cls, keyword: str) -> bool:
        return keyword in cls.SYSTEM_KEYWORDS

    @classmethod
    def get_worksheet_name(cls, key: str) -> Optional[str]:
        return cls.WORKSHEET_NAMES.get(key.upper())

    @classmethod
    def get_error_message(cls, key: str) -> str:
        return cls.ERROR_MESSAGES.get(key, cls.ERROR_MESSAGES['TEMPORARY_ERROR'])

    @classmethod
    def get_success_message(cls, key: str) -> str:
        return cls.SUCCESS_MESSAGES.get(key, '')

    @classmethod
    def format_response(cls, message: str) -> str:
        """응답 본문 앞에 RESPONSE_PREFIX 부착.

        RESPONSE_PREFIX 가 공백만(`\\n` 등)이면 `prefix.strip()` 이 빈 문자열이 되어
        `startswith('')` 가 항상 참이 되므로, 그런 경우는 단순히 prepend 만 한다.
        프리픽스에 가시 문자(예: `✦`)가 있으면, 메시지가 이미 그 문자로 시작할 때
        중복 부착을 방지한다.
        """
        if not message or not isinstance(message, str):
            return message
        message = message.strip()
        if not message:
            return message
        prefix = cls.RESPONSE_PREFIX or ''
        if not prefix:
            return message
        prefix_marker = prefix.strip()
        if prefix_marker and message.startswith(prefix_marker):
            return message
        return f"{prefix}{message}"


config = Config()
