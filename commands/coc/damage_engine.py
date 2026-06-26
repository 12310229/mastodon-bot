"""
CoC 무기 피해 계산

- `parse_damage_formula(formula)` — 복합 다이스식 파싱. 예: `"1d4"`, `"1d4+2"`, `"3d10+1d5"`.
- `roll_damage(formula, rng=None)` — 복합식 굴려 (total, max, detail) 반환.
- `compute_weapon_damage(base, result, penetrates, db_rolled, db_max)` — 등급/관통별 최종 피해.
- `apply_damage_bonus(db_mode, db_formula, result, rng)` — E8 피해보너스 적용.

복합식 문법:
- 토큰: `ndm` | `ndm+k` | `ndm-k` | 정수
- 토큰 사이를 `+` 또는 `-` 로 잇는다. 예: `3d10+1d5-2`
- 부호는 토큰 앞에 붙는다. 토큰 내부의 `±k` 는 토큰 자체의 보정값.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .check_engine import CheckResult

# 단일 토큰: ndm, ndm+k, ndm-k, 정수
_TOKEN_DICE = re.compile(r'^(\d+)[dD](\d+)(?:([+-])(\d+))?$')
_TOKEN_INT = re.compile(r'^(\d+)$')

# 허용 범위
MIN_DICE_COUNT = 1
MAX_DICE_COUNT = 30
MIN_DICE_SIDES = 2
MAX_DICE_SIDES = 1000


@dataclass(frozen=True)
class DamageRoll:
    """복합 다이스식 한 번 굴림 결과."""

    formula: str        # 원본 문자열
    total: int          # 실제 굴림 합
    max_value: int      # 이론상 최대값
    detail: str         # "1d4(3) + 2 = 5" 형태의 설명


def _split_terms(formula: str) -> List[Tuple[int, str]]:
    """
    복합식을 부호 있는 토큰 리스트로 분해.

    `"3d10+1d5-2"` → `[(+1, '3d10'), (+1, '1d5'), (-1, '2')]`

    `"-1d6"` → `[(-1, '1d6')]`
    """
    s = (formula or "").strip().replace(" ", "")
    if not s:
        raise ValueError("빈 피해식")

    # 선행 부호 처리
    if s[0] in ('+', '-'):
        if s[0] == '-':
            sign = -1
        else:
            sign = 1
        s = s[1:]
    else:
        sign = 1

    tokens: List[Tuple[int, str]] = []
    current = ""
    for ch in s:
        if ch in ('+', '-'):
            if not current:
                raise ValueError(f"피해식 문법 오류: '{formula}'")
            tokens.append((sign, current))
            sign = 1 if ch == '+' else -1
            current = ""
        else:
            current += ch
    if current:
        tokens.append((sign, current))
    if not tokens:
        raise ValueError(f"피해식 문법 오류: '{formula}'")
    return tokens


def _eval_token(
    token: str,
    rng: random.Random,
) -> Tuple[int, int, str]:
    """
    단일 토큰 평가. 반환: (rolled_value, max_value, detail).
    """
    dice_m = _TOKEN_DICE.match(token)
    if dice_m:
        n = int(dice_m.group(1))
        m = int(dice_m.group(2))
        op = dice_m.group(3)
        k = int(dice_m.group(4)) if dice_m.group(4) else 0

        if not (MIN_DICE_COUNT <= n <= MAX_DICE_COUNT):
            raise ValueError(f"다이스 개수 범위 초과: {n} (허용 {MIN_DICE_COUNT}~{MAX_DICE_COUNT})")
        if not (MIN_DICE_SIDES <= m <= MAX_DICE_SIDES):
            raise ValueError(f"다이스 면수 범위 초과: {m} (허용 {MIN_DICE_SIDES}~{MAX_DICE_SIDES})")

        rolls = [rng.randint(1, m) for _ in range(n)]
        base_total = sum(rolls)
        base_max = n * m

        if op == '+':
            total = base_total + k
            max_v = base_max + k
        elif op == '-':
            total = base_total - k
            max_v = base_max - k
        else:
            total = base_total
            max_v = base_max

        roll_str = "+".join(str(x) for x in rolls) if len(rolls) > 1 else str(rolls[0])
        if op:
            detail = f"{n}d{m}({roll_str}){op}{k}={total}"
        else:
            detail = f"{n}d{m}({roll_str})={total}"
        return total, max_v, detail

    int_m = _TOKEN_INT.match(token)
    if int_m:
        v = int(int_m.group(1))
        return v, v, str(v)

    raise ValueError(f"피해식 토큰 해석 불가: '{token}'")


def roll_damage(
    formula: str,
    rng: Optional[random.Random] = None,
) -> DamageRoll:
    """
    복합 다이스식 굴림.

    Args:
        formula: `"1d4"`, `"1d4+2"`, `"3d10+1d5"`, `"0"` 등
        rng: 테스트용

    Returns:
        DamageRoll
    """
    r = rng or random
    cleaned = (formula or "").strip()
    if not cleaned:
        return DamageRoll(formula="", total=0, max_value=0, detail="0")

    # "0" 같은 단순 정수 fast path
    if _TOKEN_INT.match(cleaned):
        v = int(cleaned)
        return DamageRoll(formula=cleaned, total=v, max_value=v, detail=str(v))

    terms = _split_terms(cleaned)
    total = 0
    max_total = 0
    pieces: List[str] = []
    for sign, token in terms:
        val, mx, det = _eval_token(token, r)
        total += sign * val
        max_total += sign * mx
        prefix = "-" if sign < 0 else ("" if not pieces else "+")
        pieces.append(prefix + det)

    detail = " ".join(pieces).strip().lstrip("+")
    return DamageRoll(formula=cleaned, total=total, max_value=max_total, detail=detail)


# ======================================================================
# 판정 등급 × 관통 여부 → 최종 피해
# ======================================================================

def compute_weapon_base_damage(
    damage: DamageRoll,
    result: CheckResult,
    penetrates: bool,
) -> Tuple[int, str]:
    """
    무기 기본 피해(무기란 F열) 계산. db 추가피해는 별도.

    - 대성공 / 극단:
        * 관통: `rolled + max`  → "관통, r + max_r"
        * 비관통: `max` 만       → "비관통, max_r 만"
    - 어려운 / 성공: `rolled`
    - 실패 / 대실패: 0

    Returns:
        (피해값, 설명 문자열)
    """
    if not result.is_success:
        return 0, "실패로 피해 없음"

    if result.is_max_damage:
        if penetrates:
            total = damage.total + damage.max_value
            return total, f"관통, 굴림 + 최대값: {damage.total} + {damage.max_value} = {total}"
        else:
            return damage.max_value, f"비관통, 최대값: {damage.max_value}"

    # 어려운 / 성공
    return damage.total, f"굴림: {damage.total}"


def apply_damage_bonus(
    db_mode: str,
    db_formula: str,
    result: CheckResult,
    rng: Optional[random.Random] = None,
) -> Tuple[int, str]:
    """
    추가 피해(db) 처리.

    Args:
        db_mode: '0' / '1/2 db' / 'db' 중 하나. 그 외/빈값 → 0 취급
        db_formula: E8 의 피해보너스 원문 (예: '1d4', '2d6', '0', '-1', '-2')
        result: 이번 판정 결과 등급 (대성공/극단이면 다이스일 때 최대값 적용)
        rng: 테스트용

    Returns:
        (추가 피해값, 설명)
    """
    mode = (db_mode or "0").strip()
    formula = (db_formula or "0").strip()

    if mode not in ("1/2 db", "db"):
        return 0, "db 없음"

    # 피해보너스 문자열 → DamageRoll
    try:
        bonus_roll = roll_damage(formula, rng=rng)
    except ValueError:
        return 0, f"db 파싱 실패: '{formula}'"

    # 대성공/극단 이면 다이스식은 최대값 적용. 고정 정수도 동일하게 max_value 를 사용.
    if result.is_max_damage:
        base = bonus_roll.max_value
        base_detail = f"db={formula} 최대값={base}"
    else:
        base = bonus_roll.total
        base_detail = f"db={formula} 굴림={base}"

    if mode == "db":
        return base, base_detail

    # 1/2 db
    half = base // 2
    return half, f"1/2 × ({base_detail}) = {half}"
