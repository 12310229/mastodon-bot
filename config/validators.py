"""
설정 검증 모듈 (CoC 봇)

애플리케이션 설정과 Google Sheets 구조를 검증합니다.
"""

import os
import sys
from pathlib import Path
from typing import List, Tuple
from dataclasses import dataclass

try:
    from config.settings import Config
except ImportError:
    import importlib.util
    settings_path = os.path.join(os.path.dirname(__file__), 'settings.py')
    spec = importlib.util.spec_from_file_location("settings", settings_path)
    settings_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(settings_module)
    Config = settings_module.Config


@dataclass
class ValidationResult:
    is_valid: bool
    errors: List[str]
    warnings: List[str]

    def add_error(self, error: str) -> None:
        self.errors.append(error)
        self.is_valid = False

    def add_warning(self, warning: str) -> None:
        self.warnings.append(warning)

    def get_summary(self) -> str:
        summary = []
        if self.is_valid:
            summary.append("✅ 모든 설정이 유효합니다.")
        else:
            summary.append("❌ 설정 검증 실패")

        if self.errors:
            summary.append("\n🚨 오류:")
            for error in self.errors:
                summary.append(f"  - {error}")

        if self.warnings:
            summary.append("\n⚠️ 경고:")
            for warning in self.warnings:
                summary.append(f"  - {warning}")

        return "\n".join(summary)


class ConfigValidator:
    """설정 검증."""

    @staticmethod
    def validate_environment() -> ValidationResult:
        result = ValidationResult(is_valid=True, errors=[], warnings=[])

        # 마스토돈 인증
        if not Config.MASTODON_ACCESS_TOKEN or not Config.MASTODON_ACCESS_TOKEN.strip():
            if not Config.MASTODON_CLIENT_ID or not Config.MASTODON_CLIENT_ID.strip():
                result.add_error("MASTODON_ACCESS_TOKEN이 없으면 MASTODON_CLIENT_ID가 필수입니다.")
            if not Config.MASTODON_CLIENT_SECRET or not Config.MASTODON_CLIENT_SECRET.strip():
                result.add_error("MASTODON_ACCESS_TOKEN이 없으면 MASTODON_CLIENT_SECRET이 필수입니다.")

        # Mastodon URL
        if not Config.MASTODON_API_BASE_URL.startswith(('http://', 'https://')):
            result.add_error("MASTODON_API_BASE_URL은 http:// 또는 https://로 시작해야 합니다.")

        # Google 서비스 계정 JSON
        cred_path = Path(Config.get_credentials_path()).resolve()
        if not cred_path.exists():
            result.add_error(
                f"Google 서비스 계정 JSON 을 찾을 수 없습니다: {cred_path}\n"
                f"    Google Cloud Console 에서 서비스 계정 키(JSON) 를 받아 위 경로에 저장하세요."
            )
        elif not cred_path.is_file():
            result.add_error(f"Google 인증 파일이 올바른 파일이 아닙니다: {cred_path}")

        # 숫자 설정값
        numeric_configs = [
            ('MAX_RETRIES', Config.MAX_RETRIES, 1, 10),
            ('BASE_WAIT_TIME', Config.BASE_WAIT_TIME, 1, 60),
            ('MAX_DICE_COUNT', Config.MAX_DICE_COUNT, 1, 100),
            ('MAX_DICE_SIDES', Config.MAX_DICE_SIDES, 2, 10000),
            ('CACHE_TTL', Config.CACHE_TTL, 0, 3600),
            ('LOG_MAX_BYTES', Config.LOG_MAX_BYTES, 1024, 104857600),
            ('LOG_BACKUP_COUNT', Config.LOG_BACKUP_COUNT, 1, 20),
        ]
        for name, value, min_val, max_val in numeric_configs:
            if not isinstance(value, int) or value < min_val or value > max_val:
                result.add_error(f"{name}은 {min_val}과 {max_val} 사이의 정수여야 합니다. 현재값: {value}")

        # 로그 레벨
        valid_log_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        if Config.LOG_LEVEL.upper() not in valid_log_levels:
            result.add_error(f"LOG_LEVEL은 다음 중 하나여야 합니다: {', '.join(valid_log_levels)}")

        # 시트 ID 3종 모두 필수 (CoC 캐릭터 / 랜덤표 / 커스텀)
        if not Config.SHEET_ID or not Config.SHEET_ID.strip():
            result.add_error("SHEET_ID 가 설정되지 않았습니다 (CoC 캐릭터 시트).")
        if not Config.RANDOM_TABLE_SHEET_ID or not Config.RANDOM_TABLE_SHEET_ID.strip():
            result.add_error("RANDOM_TABLE_SHEET_ID 가 설정되지 않았습니다 (랜덤표 시트).")
        if not Config.CUSTOM_COMMAND_SHEET_ID or not Config.CUSTOM_COMMAND_SHEET_ID.strip():
            result.add_error("CUSTOM_COMMAND_SHEET_ID 가 설정되지 않았습니다 (커스텀 명령어 시트).")

        # 관리자 ID
        if not Config.SYSTEM_ADMIN_ID or not Config.SYSTEM_ADMIN_ID.strip():
            result.add_warning("SYSTEM_ADMIN_ID가 설정되지 않았습니다. 오류 알림을 받을 수 없습니다.")

        # 로그 디렉토리
        log_dir = Path(Config.LOG_FILE_PATH).parent
        if not log_dir.exists():
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                result.add_warning(f"로그 디렉토리를 생성했습니다: {log_dir}")
            except PermissionError:
                result.add_error(f"로그 디렉토리를 생성할 권한이 없습니다: {log_dir}")

        return result

    @staticmethod
    def validate_sheet_structure(sheet) -> ValidationResult:
        """Google Sheets 구조 검증 (도움말만 선택적으로 검증)."""
        result = ValidationResult(is_valid=True, errors=[], warnings=[])

        try:
            worksheet_titles = [ws.title for ws in sheet.worksheets()]

            help_sheet_name = Config.get_worksheet_name('HELP')
            if help_sheet_name and help_sheet_name not in worksheet_titles:
                result.add_warning(
                    f"'{help_sheet_name}' 워크시트가 없습니다. [도움말] 명령어 응답이 비어 있습니다."
                )
            else:
                ConfigValidator._validate_help_sheet(sheet, result)

        except Exception as e:
            result.add_error(f"시트 구조 검증 중 오류 발생: {str(e)}")

        return result

    @staticmethod
    def _validate_help_sheet(sheet, result: ValidationResult) -> None:
        """도움말 시트 검증."""
        try:
            help_sheet = sheet.worksheet(Config.get_worksheet_name('HELP'))

            if help_sheet.row_count < 2:
                result.add_error("'도움말' 시트에 헤더와 설명 행이 필요합니다.")
                return

            headers = help_sheet.row_values(1)
            for header in ('명령어', '설명'):
                if header not in headers:
                    result.add_error(f"'도움말' 시트에 '{header}' 헤더가 없습니다.")

            if help_sheet.row_count > 2:
                all_values = help_sheet.get_all_values()
                if len(all_values) >= 3:
                    headers = all_values[0]
                    data_rows = all_values[2:]

                    records = [
                        dict(zip(headers, row_values))
                        for row_values in data_rows
                        if any(row_values)
                    ]
                    valid_helps = sum(
                        1 for record in records
                        if str(record.get('명령어', '')).strip()
                        and str(record.get('설명', '')).strip()
                    )
                    if valid_helps == 0:
                        result.add_warning("'도움말' 시트에 유효한 도움말이 없습니다.")

        except Exception as e:
            result.add_error(f"'도움말' 시트 검증 실패: {str(e)}")

    @staticmethod
    def validate_all(sheet=None) -> ValidationResult:
        env_result = ConfigValidator.validate_environment()

        if sheet is not None:
            sheet_result = ConfigValidator.validate_sheet_structure(sheet)
            combined_result = ValidationResult(
                is_valid=env_result.is_valid and sheet_result.is_valid,
                errors=env_result.errors + sheet_result.errors,
                warnings=env_result.warnings + sheet_result.warnings,
            )
        else:
            combined_result = env_result
            combined_result.add_warning("시트 구조 검증을 수행하지 않았습니다.")

        return combined_result


def validate_startup_config(sheet=None) -> Tuple[bool, str]:
    result = ConfigValidator.validate_all(sheet)
    return result.is_valid, result.get_summary()
