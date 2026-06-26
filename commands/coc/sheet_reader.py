"""
CoC 캐릭터 시트 파서

시트는 `acct 로컬 파트` 이름의 워크시트를 사용하며, 값은 **고정된 셀 좌표**에
존재한다(프롬프트 명세 참조). 이 모듈은 raw 시트 데이터(get_all_values 결과)를
`CoCCharacter` 로 변환한다.

외부 API:
- `load_character_from_worksheet(ws, user_id)` — gspread Worksheet 객체 받아 파싱
- `get_cell_address(name, is_max=False)` — 스탯명 → (row, col) 1-based 좌표 (쓰기용)

테스트용 헬퍼:
- `parse_character_values(values, user_id, worksheet_name)` — 2D 리스트만 받아 파싱
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from .character import (
    ATTRIBUTES,
    BASE_SKILLS_C,
    BASE_SKILLS_E,
    BASE_SKILLS_G,
    CoCCharacter,
    DB_NONE,
    WEAPON_ROW_END,
    WEAPON_ROW_START,
    Weapon,
)


# ======================================================================
# 셀 좌표 맵 (1-based row, 1-based col)
# ======================================================================
# 스탯명 → (row, col). '최대 X' 는 is_max=True 로 구분.
_CELL_ADDRESS: dict[tuple[str, bool], tuple[int, int]] = {
    # 능력치 (C열 = 3)
    ("근력",   False): (3, 3),
    ("민첩",   False): (4, 3),
    ("정신",   False): (5, 3),
    ("건강",   False): (6, 3),
    ("외모",   False): (7, 3),
    ("교육",   False): (8, 3),
    ("크기",   False): (9, 3),
    ("지능",   False): (10, 3),
    ("이동력", False): (11, 3),
    # E열 (5)
    ("체력",       False): (4, 5),
    ("체력",       True):  (3, 5),   # '최대 체력'
    ("운",         False): (5, 5),
    ("마력",       False): (6, 5),
    ("피해보너스", False): (8, 5),
    # G열 (7) — 이성
    ("이성",      False): (3, 7),
    ("이성",      True):  (4, 7),   # '최대 이성'
    ("시작 이성", False): (5, 7),
}


# E열의 이동력 (E9) — C11 과 중복될 수 있음. 기본은 C11 을 '이동력' 으로 취급.
_MOVEMENT_E_CELL: tuple[int, int] = (9, 5)


def get_cell_address(stat_name: str, is_max: bool = False) -> Optional[Tuple[int, int]]:
    """
    스탯명으로부터 쓰기용 셀 좌표 반환. 찾지 못하면 None.

    Args:
        stat_name: '체력', '이성', '근력' 등
        is_max: '최대 체력' / '최대 이성' 등을 가리킬 때 True

    Returns:
        (row, col) 1-based 또는 None
    """
    key = (stat_name.strip(), bool(is_max))
    return _CELL_ADDRESS.get(key)


# ======================================================================
# 파서
# ======================================================================

def parse_character_values(
    values: List[List[str]],
    user_id: str,
    worksheet_name: str,
) -> CoCCharacter:
    """
    2차원 문자열 리스트(gspread `get_all_values()` 결과)를 CoCCharacter 로 변환.

    `values[row][col]` 은 0-based 이며, 시트 위치와 1씩 차이가 난다.
    """
    char = CoCCharacter(user_id=user_id, worksheet_name=worksheet_name)

    def cell(row_1based: int, col_1based: int) -> str:
        """1-based 좌표에서 값 조회. 범위를 벗어나면 빈 문자열."""
        r = row_1based - 1
        c = col_1based - 1
        if r < 0 or r >= len(values):
            return ""
        row = values[r]
        if c < 0 or c >= len(row):
            return ""
        return (row[c] or "").strip()

    # ----- 능력치 (C3 ~ C11) -----
    # 명세 순서와 _CELL_ADDRESS 가 일관되어야 함 (근/민/정/건/외/교/크/지/이동력).
    # ATTRIBUTES 의 순서대로 C3..C11 매핑.
    for idx, name in enumerate(ATTRIBUTES):
        row_1 = 3 + idx
        char.attributes[name] = _parse_int(cell(row_1, 3))

    # ----- E열 (체력/운/마력/db/이동력) -----
    char.hp_max = _parse_int(cell(3, 5))
    char.hp_current = _parse_int(cell(4, 5))
    char.luck = _parse_int(cell(5, 5))
    char.magic = _parse_int(cell(6, 5))
    # E8 은 다이스식일 수 있음 → 문자열 유지
    db_raw = cell(8, 5)
    char.damage_bonus_formula = db_raw if db_raw else "0"
    # E9 이동력
    char.movement_e = _parse_int(cell(9, 5))

    # ----- 이성 (G3 ~ G5) -----
    char.san_current = _parse_int(cell(3, 7))
    char.san_max = _parse_int(cell(4, 7))
    char.san_start = _parse_int(cell(5, 7))

    # ----- 기본 기능 (C14~C31) -----
    for idx, name in enumerate(BASE_SKILLS_C):
        row_1 = 14 + idx
        char.base_skills[name] = _parse_int(cell(row_1, 3))

    # ----- 기본 기능 (E14~E31) -----
    for idx, name in enumerate(BASE_SKILLS_E):
        row_1 = 14 + idx
        char.base_skills[name] = _parse_int(cell(row_1, 5))

    # ----- 기본 기능 (G14~G18) -----
    for idx, name in enumerate(BASE_SKILLS_G):
        row_1 = 14 + idx
        char.base_skills[name] = _parse_int(cell(row_1, 7))

    # ----- 추가 기능 (F19~F31 이름, G19~G31 값) -----
    for row_1 in range(19, 32):
        name = cell(row_1, 6)  # F열 = 6
        if not name:
            continue
        char.extra_skills[name] = _parse_int(cell(row_1, 7))

    # ----- 무기 (35~40행) -----
    for row_1 in range(WEAPON_ROW_START, WEAPON_ROW_END + 1):
        weapon_name = cell(row_1, 2)  # B열 = 2
        if not weapon_name:
            continue
        penetration_raw = cell(row_1, 3)
        skill_name = cell(row_1, 4)  # D:E 병합셀 대표: D = 4
        # D가 비어있으면 E(=5)도 확인 (gspread가 병합셀을 다르게 줄 수 있음)
        if not skill_name:
            skill_name = cell(row_1, 5)
        damage_formula = cell(row_1, 6)
        db_mode = cell(row_1, 7)

        char.weapons.append(Weapon(
            name=weapon_name,
            penetrates=(penetration_raw == "관통"),
            skill_name=skill_name,
            damage_formula=damage_formula,
            db_mode=(db_mode or DB_NONE),
            row=row_1,
        ))

    return char


def load_character_from_worksheet(worksheet, user_id: str) -> CoCCharacter:
    """
    gspread Worksheet 객체에서 CoCCharacter 를 로드.

    Args:
        worksheet: `gspread.Worksheet` 인스턴스
        user_id: 원본 마스토돈 acct (로컬 파트가 워크시트 이름과 같음)

    Returns:
        CoCCharacter
    """
    values = worksheet.get_all_values()
    worksheet_name = getattr(worksheet, 'title', user_id)
    return parse_character_values(values, user_id=user_id, worksheet_name=worksheet_name)


# ======================================================================
# 내부 유틸
# ======================================================================

def _parse_int(raw: str) -> Optional[int]:
    """빈 문자열이나 정수로 해석 불가능하면 None. 공백 trim."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # 음수·공백 허용
    try:
        return int(s)
    except (TypeError, ValueError):
        # '1d6' 같은 다이스식이 섞여 들어온 경우 None.
        return None
