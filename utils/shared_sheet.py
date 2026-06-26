"""
공유 시트 헬퍼 (장비 및 주식 / 레이드 정보 / 자동봇용 정보 / 상점 / 전투용 정보)

기존 CoC 봇은 캐릭터별 워크시트(acct 로컬 파트)를 가졌지만, 본 모듈이 지원하는
신규 기능들은 모두 **공유 시트**에서 A열의 '칭호(=마스토돈 display_name)' 로
캐릭터를 식별한다.

설계 원칙:
- 컬럼·행 좌표는 모듈 상단 상수로 모음. 시트 구조 변경 시 한 곳만 수정.
- 캐릭터 행 lookup 은 TTL 캐시. 시트 행 위치는 자주 변하지 않음.
- 모든 함수는 실패 시 None 반환 (예외 전파 안 함) — 호출자가 적절한 사용자
  메시지를 만들도록.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from utils.logging_config import logger
from utils.sheets_operations import SheetsManager


# ======================================================================
# 워크시트 이름 (env 로 오버라이드 가능하도록 추후 확장 가능; 지금은 고정)
# ======================================================================
WS_EQUIP_STOCK = '장비 및 주식'
WS_RAID = '레이드 정보'
WS_BOT_INFO = '자동봇용 정보'
WS_SHOP = '상점'
WS_COMBAT = '전투용 정보'


# ======================================================================
# 컬럼 좌표 (1-based)
# ======================================================================
# '장비 및 주식' — 1행 header, 2행~ 데이터, A열 칭호
EQUIP_COL_TITLE = 1     # A
EQUIP_COL_GOLD = 2      # B
EQUIP_COL_ARMOR = 3     # C
EQUIP_COL_WEAPON = 4    # D
EQUIP_COL_ACCESSORY = 5 # E
# 주식: F/G = 재원, H/I = 차성, J/K = 적연 (각각 주 수 / 투자금)
# 시세·상승률은 stock_engine(JSON)에서만 산출 — 시트에는 상승률 컬럼 없음.
EQUIP_STOCK_COLS: Dict[str, Tuple[int, int]] = {
    '재원': (6, 7),    # F, G
    '차성': (8, 9),    # H, I
    '적연': (10, 11),  # J, K
}
EQUIP_DATA_START_ROW = 2  # 1행은 헤더

# '레이드 정보' — 8행 header, 9행~ 데이터
# 능력치(근력/민첩/지능/행운/정신)는 '전투용 정보' 쪽을 정본으로 삼는다.
# (이전에 H~L 로 가정했으나 사용자 정정으로 HP/MP 가 K~N 을 사용)
RAID_HEADER_ROW = 8
RAID_DATA_START_ROW = 9
RAID_COL_TITLE = 1        # A
RAID_COL_HP_CUR = 11      # K 현재 HP
RAID_COL_HP_MAX = 12      # L 최대 HP
RAID_COL_MP_CUR = 13      # M 현재 MP
RAID_COL_MP_MAX = 14      # N 최대 MP

# '자동봇용 정보' — 1행 header, 2행~ 데이터
# 주식 상태는 JSON 로컬 영속화만 사용. 시트 미러 없음 (API 호출 절약).
BOT_INFO_DATA_START_ROW = 2
BOT_INFO_COL_ITEM_NAME = 13  # M
BOT_INFO_COL_ITEM_QTY = 14   # N

# '상점' — 1행 header, 2행~ 데이터
SHOP_DATA_START_ROW = 2
SHOP_COL_NAME = 1     # A
SHOP_COL_PRICE = 2    # B
SHOP_COL_STOCK = 3    # C

# '전투용 정보' — 2행 header, 3행~ 데이터.
# A열 칭호. 고정 컬럼: H~L = 근력/민첩/지능/행운/정신 (상태창에서 사용).
# 그 외 임의 스탯은 헤더 텍스트로 매칭 ([판정/스탯명] 명령용).
COMBAT_HEADER_ROW = 2
COMBAT_DATA_START_ROW = 3
COMBAT_COL_TITLE = 1
COMBAT_COL_STR = 8     # H 근력
COMBAT_COL_DEX = 9     # I 민첩
COMBAT_COL_INT = 10    # J 지능
COMBAT_COL_LUK = 11    # K 행운
COMBAT_COL_MEN = 12    # L 정신


# ======================================================================
# HP/MP 회복 아이템 정의 (코드성 데이터)
# ======================================================================
POTION_EFFECTS: Dict[str, Tuple[str, int]] = {
    # 표기 변동(공백 유무)을 흡수하기 위해 _normalize_item_name 정규화 후 매칭.
    '소형hp포션': ('hp', 20),
    '중형hp포션': ('hp', 40),
    '대형hp포션': ('hp', 60),
    '소형mp포션': ('mp', 20),
    '중형mp포션': ('mp', 40),
    '대형mp포션': ('mp', 60),
}


def normalize_item_name(name: str) -> str:
    """아이템명 정규화: 소문자, 공백/언더바 제거."""
    if not name:
        return ''
    return ''.join(name.split()).replace('_', '').lower()


# 기존 호출자(commands/coc/*.py) 호환용 별칭.
_normalize_item_name = normalize_item_name


# ======================================================================
# 캐릭터 행 lookup 캐시
# ======================================================================
# (worksheet_name, title) → (row, expires_at)
_ROW_CACHE_TTL_SECONDS = 60
_row_cache: Dict[Tuple[str, str], Tuple[int, float]] = {}
_row_cache_lock = threading.Lock()


def invalidate_row_cache() -> None:
    """캐릭터 행 캐시 전체 무효화."""
    with _row_cache_lock:
        _row_cache.clear()


def _normalize_title(title: str) -> str:
    return (title or '').strip()


def find_character_row(
    sheets_manager: SheetsManager,
    worksheet_name: str,
    title: str,
    data_start_row: int,
) -> Optional[int]:
    """
    공유 시트의 A열에서 `title`(칭호=display_name)과 일치하는 행 번호 반환.

    Args:
        sheets_manager: SheetsManager
        worksheet_name: 검색할 워크시트 이름
        title: 마스토돈 display_name (= 시트 A열의 칭호)
        data_start_row: 데이터 시작 행 (1-based, 헤더 제외)

    Returns:
        1-based 행 번호 또는 None.
    """
    title_normalized = _normalize_title(title)
    if not title_normalized:
        return None

    key = (worksheet_name, title_normalized)
    now = time.time()

    with _row_cache_lock:
        cached = _row_cache.get(key)
        if cached and now < cached[1]:
            return cached[0]

    try:
        worksheet = sheets_manager.get_worksheet(worksheet_name)
        col_a = worksheet.col_values(1)
    except Exception as e:
        logger.warning(f"[shared_sheet] '{worksheet_name}' A열 조회 실패: {e}")
        return None

    for idx, value in enumerate(col_a, start=1):
        if idx < data_start_row:
            continue
        if _normalize_title(value) == title_normalized:
            with _row_cache_lock:
                _row_cache[key] = (idx, now + _ROW_CACHE_TTL_SECONDS)
            return idx

    return None


def list_character_titles(
    sheets_manager: SheetsManager,
    worksheet_name: str = WS_EQUIP_STOCK,
    data_start_row: int = EQUIP_DATA_START_ROW,
) -> List[Tuple[int, str]]:
    """
    공유 시트의 A열에서 모든 (행번호, 칭호) 페어를 반환.

    Returns:
        [(row, title), ...] 비어있는 칭호는 제외.
    """
    try:
        worksheet = sheets_manager.get_worksheet(worksheet_name)
        col_a = worksheet.col_values(1)
    except Exception as e:
        logger.warning(f"[shared_sheet] '{worksheet_name}' A열 조회 실패: {e}")
        return []

    result: List[Tuple[int, str]] = []
    for idx, value in enumerate(col_a, start=1):
        if idx < data_start_row:
            continue
        title = _normalize_title(value)
        if title:
            result.append((idx, title))
    return result


# ======================================================================
# 공동 창고 (자동봇용 정보 M/N열)
# ======================================================================

@dataclass
class InventoryItem:
    row: int       # 1-based 시트 행
    name: str
    qty: int


def get_inventory(sheets_manager: SheetsManager) -> List[InventoryItem]:
    """공동 창고 전체 목록. 빈 행은 스킵."""
    try:
        worksheet = sheets_manager.get_worksheet(WS_BOT_INFO)
        all_values = worksheet.get_all_values()
    except Exception as e:
        logger.warning(f"[shared_sheet] 공동창고 조회 실패: {e}")
        return []

    items: List[InventoryItem] = []
    for idx, row in enumerate(all_values, start=1):
        if idx < BOT_INFO_DATA_START_ROW:
            continue
        if len(row) < BOT_INFO_COL_ITEM_NAME:
            continue
        name = (row[BOT_INFO_COL_ITEM_NAME - 1] or '').strip()
        if not name:
            continue
        qty_raw = row[BOT_INFO_COL_ITEM_QTY - 1] if len(row) >= BOT_INFO_COL_ITEM_QTY else '0'
        try:
            qty = int(str(qty_raw or '0').strip() or '0')
        except (TypeError, ValueError):
            qty = 0
        items.append(InventoryItem(row=idx, name=name, qty=qty))
    return items


def find_inventory_item(
    sheets_manager: SheetsManager, name: str,
) -> Optional[InventoryItem]:
    """공동 창고에서 아이템 이름으로 검색 (정규화 매칭)."""
    target = _normalize_item_name(name)
    if not target:
        return None
    for item in get_inventory(sheets_manager):
        if _normalize_item_name(item.name) == target:
            return item
    return None


def add_to_inventory(
    sheets_manager: SheetsManager, name: str, qty: int,
) -> bool:
    """공동 창고에 아이템 추가. 기존 항목 있으면 수량 증가, 없으면 신규 행 추가."""
    if qty == 0:
        return True

    existing = find_inventory_item(sheets_manager, name)
    if existing:
        new_qty = max(0, existing.qty + qty)
        ok = sheets_manager.update_cell(
            WS_BOT_INFO, existing.row, BOT_INFO_COL_ITEM_QTY, str(new_qty),
        )
        return ok

    # 신규: 빈 행을 찾거나 append.
    try:
        worksheet = sheets_manager.get_worksheet(WS_BOT_INFO)
        all_values = worksheet.get_all_values()
    except Exception as e:
        logger.warning(f"[shared_sheet] 공동창고 쓰기용 조회 실패: {e}")
        return False

    insert_row = BOT_INFO_DATA_START_ROW
    for idx, row in enumerate(all_values, start=1):
        if idx < BOT_INFO_DATA_START_ROW:
            continue
        cell = (row[BOT_INFO_COL_ITEM_NAME - 1] or '').strip() if len(row) >= BOT_INFO_COL_ITEM_NAME else ''
        if not cell:
            insert_row = idx
            break
    else:
        insert_row = len(all_values) + 1

    ok_name = sheets_manager.update_cell(
        WS_BOT_INFO, insert_row, BOT_INFO_COL_ITEM_NAME, name,
    )
    ok_qty = sheets_manager.update_cell(
        WS_BOT_INFO, insert_row, BOT_INFO_COL_ITEM_QTY, str(max(0, qty)),
    )
    return ok_name and ok_qty


def consume_from_inventory(
    sheets_manager: SheetsManager, name: str, qty: int,
) -> bool:
    """공동 창고 차감. 잔량 미달 시 False."""
    if qty <= 0:
        return True
    existing = find_inventory_item(sheets_manager, name)
    if not existing or existing.qty < qty:
        return False
    new_qty = existing.qty - qty
    return sheets_manager.update_cell(
        WS_BOT_INFO, existing.row, BOT_INFO_COL_ITEM_QTY, str(new_qty),
    )


# ======================================================================
# 상점 (상점 시트 A/B/C열)
# ======================================================================

@dataclass
class ShopItem:
    row: int
    name: str
    price: int
    stock: int


def get_shop_items(sheets_manager: SheetsManager) -> List[ShopItem]:
    try:
        worksheet = sheets_manager.get_worksheet(WS_SHOP)
        all_values = worksheet.get_all_values()
    except Exception as e:
        logger.warning(f"[shared_sheet] 상점 조회 실패: {e}")
        return []

    items: List[ShopItem] = []
    for idx, row in enumerate(all_values, start=1):
        if idx < SHOP_DATA_START_ROW:
            continue
        name = (row[SHOP_COL_NAME - 1] or '').strip() if len(row) >= SHOP_COL_NAME else ''
        if not name:
            continue
        try:
            price = int(str(row[SHOP_COL_PRICE - 1] or '0').strip() or '0')
        except (TypeError, ValueError):
            price = 0
        try:
            stock = int(str(row[SHOP_COL_STOCK - 1] or '0').strip() or '0')
        except (TypeError, ValueError):
            stock = 0
        items.append(ShopItem(row=idx, name=name, price=price, stock=stock))
    return items


def find_shop_item(sheets_manager: SheetsManager, name: str) -> Optional[ShopItem]:
    target = _normalize_item_name(name)
    if not target:
        return None
    for item in get_shop_items(sheets_manager):
        if _normalize_item_name(item.name) == target:
            return item
    return None


def update_shop_stock(
    sheets_manager: SheetsManager, row: int, new_stock: int,
) -> bool:
    return sheets_manager.update_cell(
        WS_SHOP, row, SHOP_COL_STOCK, str(max(0, new_stock)),
    )


# ======================================================================
# 캐릭터 골드 / 장비 (장비 및 주식 시트)
# ======================================================================

def read_int_cell(
    sheets_manager: SheetsManager,
    worksheet_name: str,
    row: int,
    col: int,
    default: int = 0,
) -> int:
    """단일 셀을 정수로 안전하게 읽기. 실패 시 default."""
    try:
        ws = sheets_manager.get_worksheet(worksheet_name)
        raw = sheets_manager.get_cell_value_safe(ws, row, col)
        if raw is None or str(raw).strip() == '':
            return default
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    except Exception as e:
        logger.warning(f"[shared_sheet] {worksheet_name}!R{row}C{col} 읽기 실패: {e}")
        return default


def read_str_cell(
    sheets_manager: SheetsManager,
    worksheet_name: str,
    row: int,
    col: int,
    default: str = '',
) -> str:
    """단일 셀을 문자열로 안전하게 읽기."""
    try:
        ws = sheets_manager.get_worksheet(worksheet_name)
        raw = sheets_manager.get_cell_value_safe(ws, row, col)
        if raw is None:
            return default
        return str(raw).strip()
    except Exception as e:
        logger.warning(f"[shared_sheet] {worksheet_name}!R{row}C{col} 읽기 실패: {e}")
        return default


def get_character_gold(
    sheets_manager: SheetsManager, equip_row: int,
) -> int:
    return read_int_cell(sheets_manager, WS_EQUIP_STOCK, equip_row, EQUIP_COL_GOLD, 0)


def set_character_gold(
    sheets_manager: SheetsManager, equip_row: int, new_gold: int,
) -> bool:
    """음수 골드도 그대로 기록한다 (사양에 따라 clamp 없음)."""
    return sheets_manager.update_cell(
        WS_EQUIP_STOCK, equip_row, EQUIP_COL_GOLD, str(new_gold),
    )


# ======================================================================
# 전투용 정보 — 헤더 텍스트로 컬럼 매칭
# ======================================================================

def get_combat_stat(
    sheets_manager: SheetsManager, title: str, stat_name: str,
) -> Optional[int]:
    """
    '전투용 정보' 시트에서 `title` 캐릭터의 `stat_name` 스탯 값을 정수로 반환.

    헤더(8행)에서 `stat_name` 과 일치하는 컬럼을 찾아 해당 캐릭터 행의 값을 반환.
    공백 무시 / 대소문자 무시 매칭.
    """
    title_norm = _normalize_title(title)
    stat_norm = ''.join((stat_name or '').split()).lower()
    if not title_norm or not stat_norm:
        return None

    try:
        worksheet = sheets_manager.get_worksheet(WS_COMBAT)
        all_values = worksheet.get_all_values()
    except Exception as e:
        logger.warning(f"[shared_sheet] '{WS_COMBAT}' 조회 실패: {e}")
        return None

    if len(all_values) < COMBAT_HEADER_ROW:
        return None

    header = all_values[COMBAT_HEADER_ROW - 1]
    target_col_idx: Optional[int] = None
    for col_idx, header_cell in enumerate(header):
        if ''.join((header_cell or '').split()).lower() == stat_norm:
            target_col_idx = col_idx
            break
    if target_col_idx is None:
        return None

    for idx, row in enumerate(all_values, start=1):
        if idx < COMBAT_DATA_START_ROW:
            continue
        if len(row) <= max(target_col_idx, COMBAT_COL_TITLE - 1):
            continue
        row_title = _normalize_title(row[COMBAT_COL_TITLE - 1])
        if row_title != title_norm:
            continue
        raw = (row[target_col_idx] or '').strip()
        if not raw:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return None
