"""
CoC 7판 판정 엔진

- `roll_d100(rng=None) -> int`: 1~100 정수
- `roll_with_modifier(n, mode, rng=None) -> RolledD100`
  보너스/패널티 다이스 적용 (10의 자리 주사위 n+1 개 굴려 최저/최고 선택).
- `determine_result(d100, skill) -> CheckResult`
  명세대로 6등급 판정 (50 미만/이상에 따라 대성공/대실패 임계 달라짐).

`CheckResult` 는 enum — 포매터가 한국어 라벨을 붙인다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class CheckResult(Enum):
    """d100 판정 결과 등급."""

    CRITICAL = "critical"           # 대성공
    EXTREME = "extreme"              # 극단적 성공
    HARD = "hard"                    # 어려운 성공
    REGULAR = "regular"              # 성공
    FAILURE = "failure"              # 실패
    FUMBLE = "fumble"                # 대실패

    @property
    def label(self) -> str:
        return {
            CheckResult.CRITICAL: "대성공",
            CheckResult.EXTREME: "극단적 성공",
            CheckResult.HARD: "어려운 성공",
            CheckResult.REGULAR: "성공",
            CheckResult.FAILURE: "실패",
            CheckResult.FUMBLE: "대실패",
        }[self]

    @property
    def is_success(self) -> bool:
        """성공 등급(대성공/극단/어려운/성공) 인지."""
        return self in (
            CheckResult.CRITICAL,
            CheckResult.EXTREME,
            CheckResult.HARD,
            CheckResult.REGULAR,
        )

    @property
    def is_max_damage(self) -> bool:
        """무기 피해 계산에서 최대값을 더해야 하는 등급(대성공/극단)."""
        return self in (CheckResult.CRITICAL, CheckResult.EXTREME)


# ======================================================================
# 다이스 굴림
# ======================================================================

def roll_d100(rng: Optional[random.Random] = None) -> int:
    """1~100. rng 인자로 시드 가능."""
    r = rng or random
    return r.randint(1, 100)


@dataclass(frozen=True)
class RolledD100:
    """판정에 실제로 사용된 d100 결과 + 디버그 정보."""

    d100: int                           # 최종 1~100 값
    ones: int                           # 1의 자리 (1~10; raw 0 은 10 으로 해석)
    tens: int                           # 채택된 10의 자리 (0, 10, 20, …, 90)
    tens_candidates: Tuple[int, ...]    # 굴린 모든 10의 자리 후보 (보너스/패널티 시 n+1 개)
    modifier: int                       # 보너스(+n) / 패널티(-n) 개수. 0=일반 판정.


def roll_with_modifier(
    modifier: int = 0,
    rng: Optional[random.Random] = None,
) -> RolledD100:
    """
    보너스/패널티 다이스를 적용한 d100 판정.

    동작:
    - 1의 자리 주사위(d10, 0~9) 1개를 굴린다. 0 은 수치 10으로 해석.
    - 10의 자리 주사위(d10, 0~9) `|modifier| + 1` 개를 굴린다. 값은 0~90 (0=0, 1=10, ...).
    - modifier > 0 (보너스): 10의 자리 후보 중 **가장 낮은** 값을 채택.
    - modifier < 0 (패널티): 10의 자리 후보 중 **가장 높은** 값을 채택.
    - modifier == 0: 10의 자리 1개만 굴려 그대로 사용.
    - 최종 d100 = tens + ones. 단 tens_raw=0 and ones_raw=0 이면 100 (관습).

    Args:
        modifier: 양수=보너스, 음수=패널티, 0=일반 판정
        rng: 테스트용 시드 가능 RNG

    Returns:
        RolledD100
    """
    r = rng or random

    ones_raw = r.randint(0, 9)
    ones = 10 if ones_raw == 0 else ones_raw

    n = abs(modifier) + 1
    tens_raw = tuple(r.randint(0, 9) for _ in range(n))
    tens_candidates = tuple(t * 10 for t in tens_raw)

    if modifier > 0:
        chosen_tens = min(tens_candidates)
    elif modifier < 0:
        chosen_tens = max(tens_candidates)
    else:
        chosen_tens = tens_candidates[0]

    # 관습: tens=0 and ones_raw=0 → 100
    if chosen_tens == 0 and ones_raw == 0:
        d100 = 100
    else:
        d100 = chosen_tens + ones

    return RolledD100(
        d100=d100,
        ones=ones,
        tens=chosen_tens,
        tens_candidates=tens_candidates,
        modifier=modifier,
    )


# ======================================================================
# 판정 등급 결정
# ======================================================================

def determine_result(d100: int, skill_value: int) -> CheckResult:
    """
    명세의 6등급 판정 로직.

    - 기능값 50 미만:
        * 1 → 대성공
        * 96~100 → 대실패
    - 기능값 50 이상:
        * 1~5 → 대성공
        * 100 → 대실패

    공통:
    - d100 ≤ skill/5 → 극단적 성공
    - d100 ≤ skill/2 → 어려운 성공
    - d100 ≤ skill   → 성공
    - 나머지 → 실패

    단 대성공/대실패 판정이 다른 임계보다 우선한다.
    """
    skill = max(0, int(skill_value))

    if skill >= 50:
        if 1 <= d100 <= 5:
            return CheckResult.CRITICAL
        if d100 == 100:
            return CheckResult.FUMBLE
    else:
        if d100 == 1:
            return CheckResult.CRITICAL
        if 96 <= d100 <= 100:
            return CheckResult.FUMBLE

    if d100 <= skill // 5:
        return CheckResult.EXTREME
    if d100 <= skill // 2:
        return CheckResult.HARD
    if d100 <= skill:
        return CheckResult.REGULAR

    return CheckResult.FAILURE


# ======================================================================
# 임계값 (포매터가 출력할 때 참조)
# ======================================================================

@dataclass(frozen=True)
class CheckThresholds:
    """한 판정에서의 성공 등급 임계값 묶음."""

    skill: int
    extreme: int
    hard: int
    regular: int


def compute_thresholds(skill_value: int) -> CheckThresholds:
    skill = max(0, int(skill_value))
    return CheckThresholds(
        skill=skill,
        extreme=skill // 5,
        hard=skill // 2,
        regular=skill,
    )


# ======================================================================
# 상위 함수 — 한 번에 판정 수행
# ======================================================================

@dataclass(frozen=True)
class CheckOutcome:
    """판정 전체 결과."""

    skill_name: str
    rolled: RolledD100
    result: CheckResult
    thresholds: CheckThresholds


def perform_check(
    skill_name: str,
    skill_value: int,
    modifier: int = 0,
    rng: Optional[random.Random] = None,
) -> CheckOutcome:
    """기능명 + 기능값 + 보너스/패널티 개수 → CheckOutcome."""
    rolled = roll_with_modifier(modifier=modifier, rng=rng)
    result = determine_result(rolled.d100, skill_value)
    thresholds = compute_thresholds(skill_value)
    return CheckOutcome(
        skill_name=skill_name,
        rolled=rolled,
        result=result,
        thresholds=thresholds,
    )
