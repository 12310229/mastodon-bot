"""시트의 A1 셀 주소를 (row, col) 1-based 좌표로 변환.

CoC 룰의 시트 리더가 사용한다. gspread 의 `utils.a1_to_rowcol` 도 있지만,
봇 내부에서는:
  - 잘못된 입력에 대한 한국어 에러 메시지가 필요하고,
  - 외부 라이브러리 의존을 줄이고 싶고,
  - 테스트 더블이 한 곳만 가짜로 만들면 되도록
이 작은 유틸을 직접 둔다.
"""

from __future__ import annotations

from typing import Tuple


def a1_to_rowcol(addr: str) -> Tuple[int, int]:
    """A1 표기 셀 주소를 1-based (row, col) 로 변환.

    예: 'P5' → (5, 16). 'AB10' → (10, 28). 'AJ13' → (13, 36).

    Args:
        addr: A1 표기 셀 주소.

    Returns:
        (row, col) 1-based 튜플.

    Raises:
        ValueError: 형식이 잘못되었거나 행 번호가 0 이하일 때.
    """
    s = (addr or "").strip().upper()
    i = 0
    while i < len(s) and s[i].isalpha():
        i += 1
    if i == 0 or i == len(s):
        raise ValueError(f"잘못된 A1 주소: '{addr}'")

    letters = s[:i]
    digits = s[i:]

    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - ord('A') + 1)

    try:
        row = int(digits)
    except ValueError as exc:
        raise ValueError(f"잘못된 A1 주소: '{addr}'") from exc

    if row <= 0:
        raise ValueError(f"잘못된 A1 주소(행 ≤ 0): '{addr}'")

    return row, col
