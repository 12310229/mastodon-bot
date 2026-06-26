"""
.env 파일 관리 헬퍼 (CoC 봇)

CoC 봇 1개를 위한 .env 파일을 대화형으로 생성합니다.
사용자가 실제 편집해야 하는 항목은 MASTODON_API_BASE_URL / MASTODON_ACCESS_TOKEN
/ SHEET_ID 정도. 나머지는 코드 기본값으로 처리되어 prompt 하지 않습니다.
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Set, Tuple


@dataclass(frozen=True)
class FieldSpec:
    key: str
    prompt: str
    default: Optional[str] = None
    condition: Optional[Callable[[Dict[str, str]], bool]] = None


@dataclass(frozen=True)
class SectionSpec:
    title: str
    keys: Tuple[str, ...]
    description: Optional[str] = None


# ----------------------------------------------------------------------
# 전역 prompt 대상 (사용자가 실제 편집해야 하는 것만)
# ----------------------------------------------------------------------
GLOBAL_FIELD_SPECS: Dict[str, FieldSpec] = {
    'MASTODON_API_BASE_URL': FieldSpec(
        key='MASTODON_API_BASE_URL',
        prompt='Mastodon 서버 URL',
        default='',
    ),
    'MASTODON_ACCESS_TOKEN': FieldSpec(
        key='MASTODON_ACCESS_TOKEN',
        prompt='Mastodon 액세스 토큰',
        default='',
    ),
    'SHEET_ID': FieldSpec(
        key='SHEET_ID',
        prompt='CoC 캐릭터 시트의 Google Sheets ID',
        default='',
    ),
    'SYSTEM_ADMIN_ID': FieldSpec(
        key='SYSTEM_ADMIN_ID',
        prompt='시스템 관리자 ID (콤마 구분, 선택)',
        default='',
    ),
    'GOOGLE_CREDENTIALS_PATH': FieldSpec(
        key='GOOGLE_CREDENTIALS_PATH',
        prompt='Google 인증 파일 경로',
        default='credentials.json',
    ),
    'RANDOM_TABLE_SHEET_ID': FieldSpec(
        key='RANDOM_TABLE_SHEET_ID',
        prompt='랜덤표 스프레드시트 ID (선택, 비우면 비활성화)',
        default='',
    ),
    'CUSTOM_COMMAND_SHEET_ID': FieldSpec(
        key='CUSTOM_COMMAND_SHEET_ID',
        prompt='커스텀 명령어 스프레드시트 ID (선택, 비우면 비활성화)',
        default='',
    ),
    'OPERATION_START_DATE': FieldSpec(
        key='OPERATION_START_DATE',
        prompt='가동 시작 날짜 KST YYYY-MM-DD (선택, 비우면 무제한)',
        default='',
    ),
    'OPERATION_END_DATE': FieldSpec(
        key='OPERATION_END_DATE',
        prompt='가동 종료 날짜 KST YYYY-MM-DD (선택, 비우면 무제한)',
        default='',
    ),
    'MARKDOWN_ENABLED': FieldSpec(
        key='MARKDOWN_ENABLED',
        prompt='서버가 Markdown(*기울임*/**볼드**)을 지원하나요? (y/n)',
        default='false',
    ),
    'DECORATION_CHAR': FieldSpec(
        key='DECORATION_CHAR',
        prompt='장식 특수문자 (엔터=✦ 기본 / no=생략 / 다른 문자 붙여넣기)',
        default='✦',
    ),
    'DECORATION_BOTH_SIDES': FieldSpec(
        key='DECORATION_BOTH_SIDES',
        prompt='장식 위치 — 1=앞에만 / 2=양옆 (엔터=2)',
        default='true',
        # DECORATION_CHAR 가 비어 있으면 위치는 의미가 없으므로 묻지 않음
        condition=lambda cfg: bool(cfg.get('DECORATION_CHAR', '').strip()),
    ),
}

GLOBAL_SECTIONS: Tuple[SectionSpec, ...] = (
    SectionSpec(
        title='필수 항목',
        description='Mastodon 연결 + CoC 시트 설정',
        keys=(
            'MASTODON_API_BASE_URL',
            'MASTODON_ACCESS_TOKEN',
            'SHEET_ID',
            'SYSTEM_ADMIN_ID',
            'GOOGLE_CREDENTIALS_PATH',
        ),
    ),
    SectionSpec(
        title='보조 시트 (선택)',
        description='랜덤표/커스텀 명령어 시트. 비우면 해당 기능 비활성화.',
        keys=('RANDOM_TABLE_SHEET_ID', 'CUSTOM_COMMAND_SHEET_ID'),
    ),
    SectionSpec(
        title='가동 기간 (선택)',
        description='KST 기준. 종료 날짜 00:00 KST 부터 만료 안내 후 침묵. 비우면 무기한.',
        keys=('OPERATION_START_DATE', 'OPERATION_END_DATE'),
    ),
    SectionSpec(
        title='Markdown 렌더링',
        description='판정 출력의 기능명/판정결과/피해 값을 **볼드** 로 감싼다. 한참 등 일부 인스턴스만 지원.',
        keys=('MARKDOWN_ENABLED',),
    ),
    SectionSpec(
        title='응답 장식 (특수문자)',
        description=(
            '판정 제목 양옆에 붙는 특수문자. 기본 ✦. '
            "DECORATION_CHAR 에 'no' 입력 시 생략. "
            'DECORATION_BOTH_SIDES: 2=양옆, 1=앞에만.'
        ),
        keys=('DECORATION_CHAR', 'DECORATION_BOTH_SIDES'),
    ),
)


_YES_TOKENS = {'y', 'yes', 'true', '1', 't'}
_NO_TOKENS = {'n', 'no', 'false', '0', 'f', ''}


def _normalize_bool_input(raw: str) -> Optional[bool]:
    """y/n 계열 문자열을 bool 로. 알 수 없으면 None."""
    token = raw.strip().lower()
    if token in _YES_TOKENS:
        return True
    if token in _NO_TOKENS:
        return False
    return None


class EnvManager:
    """환경 변수 관리 클래스."""

    def __init__(self, env_path: str = '.env'):
        self.env_path = Path(env_path)
        self.config: Dict[str, str] = {}

    def _should_prompt(self, field_spec: FieldSpec) -> bool:
        if field_spec.condition is None:
            return True
        return field_spec.condition(self.config)

    def _prompt_field(self, field_spec: FieldSpec) -> None:
        current = self.get_value(field_spec.key, field_spec.default or '')
        if field_spec.key == 'MARKDOWN_ENABLED':
            self._prompt_bool_field(field_spec, current)
            return
        if field_spec.key == 'DECORATION_CHAR':
            self._prompt_decoration_char(field_spec, current)
            return
        if field_spec.key == 'DECORATION_BOTH_SIDES':
            self._prompt_decoration_position(field_spec, current)
            return
        placeholder = current or '입력 필요'
        value = input(f"{field_spec.prompt} [{placeholder}]: ").strip()
        if not value:
            value = current
        if value is None:
            value = ''
        self.set_value(field_spec.key, value)

    def _prompt_bool_field(self, field_spec: FieldSpec, current: str) -> None:
        """y/n 입력을 받아 'true'/'false' 로 정규화해 저장."""
        current_bool = _normalize_bool_input(current)
        placeholder = 'y' if current_bool else 'n'
        raw = input(f"{field_spec.prompt} [{placeholder}]: ").strip()
        if not raw:
            normalized = current_bool if current_bool is not None else False
        else:
            parsed = _normalize_bool_input(raw)
            if parsed is None:
                print(f"[경고] '{raw}' 는 인식할 수 없어 기본값({placeholder}) 사용")
                normalized = current_bool if current_bool is not None else False
            else:
                normalized = parsed
        self.set_value(field_spec.key, 'true' if normalized else 'false')

    def _prompt_decoration_char(self, field_spec: FieldSpec, current: str) -> None:
        """특수문자 입력. 엔터=현재값(또는 ✦) 유지, 'no'=빈 문자열, 그 외=그대로."""
        placeholder = current if current else '엔터=✦, no=생략'
        # 사용자가 붙여넣을 특수문자가 strip 으로 사라지지 않도록 양 끝 공백만 정리.
        raw = input(f"{field_spec.prompt} [{placeholder}]: ").strip()
        if not raw:
            value = current if current else (field_spec.default or '')
        elif raw.lower() == 'no':
            value = ''
        else:
            value = raw
        self.set_value(field_spec.key, value)

    def _prompt_decoration_position(self, field_spec: FieldSpec, current: str) -> None:
        """1=앞에만(false), 2=양옆(true). 엔터/잘못 입력 시 현재값 유지(없으면 양옆)."""
        current_bool = _normalize_bool_input(current)
        placeholder = '2' if current_bool is None or current_bool else '1'
        raw = input(f"{field_spec.prompt} [{placeholder}]: ").strip()
        if not raw:
            normalized = current_bool if current_bool is not None else True
        elif raw == '1':
            normalized = False
        elif raw == '2':
            normalized = True
        else:
            print(f"[경고] '{raw}' 는 1/2 가 아니어서 기본값({placeholder}) 사용")
            normalized = current_bool if current_bool is not None else True
        self.set_value(field_spec.key, 'true' if normalized else 'false')

    def load_existing(self) -> bool:
        if not self.env_path.exists():
            return False
        try:
            with open(self.env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        self.config[key.strip()] = value.strip()
            return True
        except Exception as e:
            print(f"[오류] .env 파일 로드 실패: {e}")
            return False

    def get_value(self, key: str, default: str = '') -> str:
        return self.config.get(key, default)

    def set_value(self, key: str, value: str) -> None:
        self.config[key] = value

    def interactive_setup(self) -> None:
        print("=" * 60)
        print("CoC 봇 환경 설정")
        print("=" * 60)
        print()

        if self.load_existing():
            print("[정보] 기존 .env 파일을 찾았습니다.")
            use_existing = input("기존 설정을 유지하시겠습니까? (Y/n): ").strip().lower()
            if use_existing == 'n':
                self.config = {}

        for section in GLOBAL_SECTIONS:
            print()
            print(f"=== {section.title} ===")
            if section.description:
                print(section.description)
            print()
            for key in section.keys:
                spec = GLOBAL_FIELD_SPECS[key]
                if not self._should_prompt(spec):
                    if spec.default is not None and key not in self.config:
                        self.set_value(key, spec.default)
                    continue
                self._prompt_field(spec)

        print()
        print("=== 설정 완료 ===")
        print()

    def save(self) -> bool:
        try:
            if self.env_path.exists():
                backup_path = Path(f"{self.env_path}.backup")
                with open(self.env_path, 'r', encoding='utf-8') as src:
                    with open(backup_path, 'w', encoding='utf-8') as dst:
                        dst.write(src.read())
                print(f"[정보] 기존 파일 백업: {backup_path}")

            with open(self.env_path, 'w', encoding='utf-8') as f:
                f.write("# CoC 봇 설정 (자동 생성)\n\n")

                written: Set[str] = set()
                for section in GLOBAL_SECTIONS:
                    rows = [(k, self.config[k]) for k in section.keys if k in self.config]
                    if not rows:
                        continue
                    f.write(f"# {section.title}\n")
                    for k, v in rows:
                        f.write(f"{k}={v}\n")
                        written.add(k)
                    f.write("\n")

                remaining = [k for k in sorted(self.config) if k not in written]
                if remaining:
                    f.write("# 기타\n")
                    for k in remaining:
                        f.write(f"{k}={self.config[k]}\n")

            print(f"[성공] 설정 파일 저장: {self.env_path}")
            return True
        except Exception as e:
            print(f"[오류] 설정 파일 저장 실패: {e}")
            return False

    def quick_edit(self, key: str, value: str) -> bool:
        self.load_existing()
        self.set_value(key, value)
        return self.save()

    def show_current(self) -> None:
        if not self.load_existing():
            print("[오류] .env 파일을 찾을 수 없습니다.")
            return
        print("현재 설정")
        print("=" * 60)
        for key, value in sorted(self.config.items()):
            display = value[:10] + '…' if 'TOKEN' in key and len(value) > 10 else value
            print(f"{key}={display}")


def main() -> None:
    manager = EnvManager()

    if len(sys.argv) > 1:
        command = sys.argv[1]
        if command == 'show':
            manager.show_current()
        elif command == 'edit' and len(sys.argv) >= 4:
            key, value = sys.argv[2], sys.argv[3]
            if manager.quick_edit(key, value):
                print(f"[성공] {key}={value}")
        elif command == 'setup':
            manager.interactive_setup()
            manager.save()
        else:
            print("사용법:")
            print("  python env_manager.py setup         - 대화형 설정")
            print("  python env_manager.py show          - 현재 설정 보기")
            print("  python env_manager.py edit KEY VALUE - 빠른 수정")
    else:
        manager.interactive_setup()
        print()
        if input("설정을 저장하시겠습니까? (Y/n): ").strip().lower() != 'n':
            manager.save()
            print()
            print("[완료] 이제 'python main.py' 로 봇을 실행하세요!")


if __name__ == '__main__':
    main()
