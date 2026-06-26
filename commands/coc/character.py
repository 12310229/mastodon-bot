"""
CoC 캐릭터 데이터 모델

시트에서 읽어낸 값들을 dataclass 로 표현. 메서드를 통해 능력치/기본기능/추가기능/
무기를 통합 조회한다. 셀 좌표 맵은 `sheet_reader.py` 에서 관리.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ======================================================================
# 기본 능력치 (C3~C11) — 순서 유지가 중요.
# ======================================================================
ATTRIBUTES: tuple[str, ...] = (
    "근력",   # C3
    "민첩",   # C4
    "정신",   # C5
    "건강",   # C6
    "외모",   # C7
    "교육",   # C8
    "크기",   # C9
    "지능",   # C10
    "이동력", # C11  (참고: E9 에도 이동력이 존재. 시트마다 둘 중 하나만 쓰일 수 있음.)
)


# ======================================================================
# 체력/운/마력/피해보너스 (E열)
# ======================================================================
# 값은 정수 또는 문자열(피해보너스의 다이스식)
E_COL_STATS: tuple[str, ...] = (
    "최대 체력",   # E3
    "체력",        # E4
    "운",          # E5
    "마력",        # E6
    # E7 은 비어있음
    "피해보너스",  # E8 — "-1" / "-2" / "0" / "1d4" / "1d6" / "2d6" 등 문자열
    # E9 는 이동력 (C11 과 중복 가능)
)


# ======================================================================
# 이성 (G열 상단)
# ======================================================================
G_TOP_STATS: tuple[str, ...] = (
    "이성",       # G3
    "최대 이성",  # G4
    "시작 이성",  # G5
)


# ======================================================================
# 기본 기능 (C14~C31, E14~E31, G14~G18)
# ======================================================================
# 명세에 따른 고정된 순서. 시트의 해당 행/열에서 값을 읽는다.
BASE_SKILLS_C: tuple[str, ...] = (
    "감정",          # C14
    "고고학",        # C15
    "관찰력",        # C16
    "근접전(격투)",  # C17
    "기계수리",      # C18
    "도약",          # C19
    "듣기",          # C20
    "말재주",        # C21
    "매혹",          # C22
    "법률",          # C23
    "변장",          # C24
    "사격(권총)",    # C25
    "사격(라/산)",   # C26
    "설득",          # C27
    "손놀림",        # C28
    "수영",          # C29
    "승마",          # C30
    "심리학",        # C31
)

BASE_SKILLS_E: tuple[str, ...] = (
    "언어(모국어)",      # E14
    "역사",              # E15
    "열쇠공",            # E16
    "오르기",            # E17
    "오컬트",            # E18
    "위협",              # E19
    "은밀행동",          # E20
    "응급처치",          # E21
    "의료",              # E22
    "인류학",            # E23
    "자동차 운전",       # E24
    "자료조사",          # E25
    "자연",              # E26
    "재력",              # E27
    "전기수리",          # E28
    "정신분석",          # E29
    "중장비 조작",       # E30
    "추적",              # E31
)

BASE_SKILLS_G: tuple[str, ...] = (
    "크툴루 신화",  # G14
    "투척",         # G15
    "항법",         # G16
    "회계",         # G17
    "회피",         # G18
)


# 명세: 무기 35 ~ 40행. B35=무기명, C35=관통/비관통, D:E=기능치명, F=피해식, G=추가피해(db)
WEAPON_ROW_START = 35
WEAPON_ROW_END = 40


# ======================================================================
# DB(피해보너스) 추가피해 옵션
# ======================================================================
DB_NONE = "0"
DB_HALF = "1/2 db"
DB_FULL = "db"
DB_VALID = {DB_NONE, DB_HALF, DB_FULL, ""}


# ======================================================================
# DTO
# ======================================================================

@dataclass(frozen=True)
class Weapon:
    """무기 한 줄 (35 ~ 40행 중 하나)."""

    name: str                   # B열
    penetrates: bool            # C열 '관통' 이면 True, '비관통' 이면 False
    skill_name: str             # D:E 병합 셀 — 사용할 기능치명
    damage_formula: str         # F열 — 다이스식 (예: '1d4', '1d4+2', '3d10+1d5')
    db_mode: str                # G열 — DB_NONE / DB_HALF / DB_FULL ('' 면 NONE 취급)
    row: int                    # 시트 행 번호 (쓰기 작업 시 사용)

    @property
    def damage_bonus(self) -> str:
        """저장된 db_mode 를 정규화 (빈 문자열 → DB_NONE)."""
        return self.db_mode or DB_NONE


@dataclass
class CoCCharacter:
    """
    CoC 캐릭터 한 명분 파싱 결과.

    모든 수치는 시트에서 읽은 raw 문자열이 아닌 정수/문자열로 정규화된 값.
    (단, `damage_bonus_formula` 는 '1d4' 같은 다이스식일 수 있어 문자열 유지)
    """

    user_id: str
    worksheet_name: str

    # 능력치 (근력/민첩/…): 없는 항목은 None
    attributes: Dict[str, Optional[int]] = field(default_factory=dict)

    # E 열 체력/운/마력/이동력
    hp_current: Optional[int] = None
    hp_max: Optional[int] = None
    luck: Optional[int] = None
    magic: Optional[int] = None
    movement_e: Optional[int] = None  # E9
    damage_bonus_formula: str = "0"   # E8 원문 (다이스식일 수 있음)

    # 이성
    san_current: Optional[int] = None   # G3
    san_max: Optional[int] = None       # G4
    san_start: Optional[int] = None     # G5

    # 기본 기능 (이름 → 값). 없으면 None.
    base_skills: Dict[str, Optional[int]] = field(default_factory=dict)

    # 추가 기능 (F19~F31 = 이름, G19~G31 = 값). 이름이 비어있는 행은 제외.
    extra_skills: Dict[str, Optional[int]] = field(default_factory=dict)

    # 무기 목록 (비무장 포함 35 ~ 40행)
    weapons: List[Weapon] = field(default_factory=list)

    # ----- 통합 조회 -----

    def get_skill_value(self, name: str) -> Optional[int]:
        """
        기능치/능력치 통합 조회.

        순서: 능력치 → 기본 기능 → 추가 기능 → '최대 체력'/'이성' 같은 특수 항목.
        없으면 None.
        """
        key = (name or "").strip()
        if not key:
            return None

        if key in self.attributes:
            return self.attributes[key]
        if key in self.base_skills:
            return self.base_skills[key]
        if key in self.extra_skills:
            return self.extra_skills[key]

        # 특수 항목 (판정보다는 스탯 변동에서 주로 쓰임이지만, 참조용)
        SPECIAL = {
            "체력": self.hp_current,
            "최대 체력": self.hp_max,
            "운": self.luck,
            "마력": self.magic,
            "이동력": self.movement_e if self.movement_e is not None else self.attributes.get("이동력"),
            "이성": self.san_current,
            "최대 이성": self.san_max,
            "시작 이성": self.san_start,
        }
        return SPECIAL.get(key)

    def get_weapon(self, name: str) -> Optional[Weapon]:
        """무기명으로 탐색. 이름 공백/대소문자 정규화 없이 정확 매칭."""
        key = (name or "").strip()
        if not key:
            return None
        for w in self.weapons:
            if w.name == key:
                return w
        return None

    def has_weapon(self, name: str) -> bool:
        return self.get_weapon(name) is not None

    def has_skill(self, name: str) -> bool:
        """능력치/기본 기능/추가 기능 중에 해당 이름이 존재 (값이 None 이어도 존재로 판정)."""
        key = (name or "").strip()
        if not key:
            return False
        return (
            key in self.attributes
            or key in self.base_skills
            or key in self.extra_skills
        )
