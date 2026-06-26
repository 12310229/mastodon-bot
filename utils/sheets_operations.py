"""
Google Sheets 작업 모듈
Google Sheets와 관련된 모든 작업을 통합 관리합니다.
"""

import os
import random
import sys
import threading
import gspread
import pytz
import time
import re
from collections import OrderedDict
from datetime import datetime
from typing import List, Dict, Any, Optional, Union, Tuple, Set
from gspread.exceptions import APIError
from difflib import SequenceMatcher

# 경로 설정 (VM 환경 대응)
from config.settings import config
from utils.error_handling import (
    safe_execute, SheetAccessError, UserNotFoundError,
    SheetErrorHandler, ErrorContext,
)
from utils.logging_config import logger, bot_logger, should_log_debug, log_api_operation


def normalize_text(text: str) -> str:
    """
    텍스트 정규화 - 매칭을 위해 텍스트를 정리
    """
    if not text:
        return ""

    # 1. HTML 태그 제거 (이미 되어있을 수도 있지만 재확인)
    text = re.sub(r'<[^>]+>', '', text)

    # 2. 연속된 공백을 단일 공백으로 변환
    text = re.sub(r'\s+', ' ', text)

    # 3. 앞뒤 공백 제거
    text = text.strip()

    # 4. 특수문자 통일 (전각 → 반각)
    text = text.replace('（', '(').replace('）', ')')
    text = text.replace('！', '!').replace('？', '?')
    text = text.replace('【', '[').replace('】', ']')

    return text


def _normalize_keyword(text: str) -> str:
    """명령어/시트명 매칭용 정규화: 소문자 + 모든 공백 제거."""
    if not text:
        return ""
    return re.sub(r'\s+', '', str(text)).lower()



class SheetsManager:
    """Google Sheets 관리 클래스"""

    # `_worksheets_cache` 의 상한. 캐릭터별 워크시트를 동적으로 캐싱하므로
    # 사용자가 늘어날수록 무한 증가한다 — LRU 로 바운드.
    _WORKSHEETS_CACHE_LIMIT = 256

    def __init__(self, sheet_id: str = None, credentials_path: str = None):
        """
        SheetsManager 초기화

        Args:
            sheet_id: 스프레드시트 ID
            credentials_path: 인증 파일 경로
        """
        self.sheet_id = sheet_id or config.SHEET_ID
        self.credentials_path = credentials_path or config.get_credentials_path()
        self._spreadsheet = None
        # OrderedDict 으로 LRU 동작 (최근 접근을 마지막으로 이동)
        self._worksheets_cache: "OrderedDict[str, gspread.Worksheet]" = OrderedDict()

        # 보조 스프레드시트 (랜덤표 / 커스텀 명령어). 모든 봇이 공유하며 lazy 로딩.
        self._random_table_spreadsheet: Optional[gspread.Spreadsheet] = None
        self._custom_command_spreadsheet: Optional[gspread.Spreadsheet] = None

        # 보조 시트 캐시 (TTL 기반)
        self._random_table_index_cache: Optional[Dict[str, str]] = None
        self._random_table_index_expires: float = 0.0
        self._random_table_values_cache: Dict[str, Tuple[float, List[str]]] = {}
        self._custom_command_cache: Optional[Dict[str, List[str]]] = None
        self._custom_command_cache_expires: float = 0.0
        self._aux_lock = threading.RLock()
        
    @property
    def spreadsheet(self):
        """스프레드시트 객체 (지연 로딩)"""
        if self._spreadsheet is None:
            self._spreadsheet = self.connect_to_sheet()
        return self._spreadsheet
    
    def connect_to_sheet(self) -> gspread.Spreadsheet:
        """
        스프레드시트 연결 (기존 connect_to_sheet 함수 개선 버전)
        
        Returns:
            gspread.Spreadsheet: 연결된 스프레드시트 객체
            
        Raises:
            SheetAccessError: 연결 실패 시
        """
        def connection_operation():
            try:
                # Google API를 사용한 인증
                gc = gspread.service_account(filename=str(self.credentials_path))
                
                # 스프레드시트 열기 (ID 기반)
                spreadsheet = gc.open_by_key(self.sheet_id)
                logger.debug(f"스프레드시트 연결: {self.sheet_id}")
                return spreadsheet
                
            except FileNotFoundError:
                raise SheetAccessError(f"인증 파일을 찾을 수 없습니다: {self.credentials_path}")
            except gspread.exceptions.SpreadsheetNotFound:
                raise SheetAccessError(f"스프레드시트 ID '{self.sheet_id}'를 찾을 수 없습니다.")
            except Exception as e:
                raise SheetAccessError(f"스프레드시트 연결 실패: {str(e)}")
        
        with ErrorContext("스프레드시트 연결", sheet_id=self.sheet_id):
            result = safe_execute(
                operation_func=connection_operation,
                max_retries=config.MAX_RETRIES
            )
            
            if result.success:
                return result.result
            else:
                raise result.error or SheetAccessError("스프레드시트 연결 실패")
    
    def get_worksheet(self, worksheet_name: str, use_cache: bool = True) -> gspread.Worksheet:
        """
        워크시트 가져오기 (캐싱 지원)
        
        Args:
            worksheet_name: 워크시트 이름
            use_cache: 캐시 사용 여부
            
        Returns:
            gspread.Worksheet: 워크시트 객체
            
        Raises:
            SheetAccessError: 워크시트를 찾을 수 없을 때
        """
        if use_cache and worksheet_name in self._worksheets_cache:
            # LRU: 최근 접근을 끝으로 이동
            self._worksheets_cache.move_to_end(worksheet_name)
            return self._worksheets_cache[worksheet_name]

        def get_operation():
            try:
                worksheet = self.spreadsheet.worksheet(worksheet_name)
                if use_cache:
                    self._worksheets_cache[worksheet_name] = worksheet
                    # 상한 초과 시 가장 오래된 항목 제거 (FIFO/LRU)
                    while len(self._worksheets_cache) > self._WORKSHEETS_CACHE_LIMIT:
                        self._worksheets_cache.popitem(last=False)
                return worksheet
            except gspread.exceptions.WorksheetNotFound:
                raise SheetErrorHandler.handle_worksheet_not_found(worksheet_name)
        
        with ErrorContext("워크시트 접근", worksheet=worksheet_name):
            result = safe_execute(get_operation)
            
            if result.success:
                return result.result
            else:
                raise result.error or SheetErrorHandler.handle_worksheet_not_found(worksheet_name)
    
    def get_worksheet_data(self, worksheet_name: str, use_cache: bool = False) -> List[Dict[str, Any]]:
        """
        워크시트 데이터 가져오기 (1행: 헤더, 2행: 설명, 3행부터: 데이터)

        Args:
            worksheet_name: 워크시트 이름
            use_cache: 캐시 사용 여부 (데이터는 기본적으로 캐시하지 않음)

        Returns:
            List[Dict]: 워크시트 데이터
        """
        def get_data_operation():
            worksheet = self.get_worksheet(worksheet_name)
            if worksheet.row_count <= 2:  # 헤더와 설명만 있거나 빈 시트
                return []

            # 수동으로 헤더와 데이터 파싱 (1행: 헤더, 2행: 설명, 3행부터: 데이터)
            all_values = worksheet.get_all_values()
            if len(all_values) < 3:  # 헤더, 설명, 데이터 최소 1개 필요
                return []

            headers = all_values[0]  # 1행: 헤더
            # all_values[1]은 설명 행 - 무시
            data_rows = all_values[2:]  # 3행부터: 데이터

            # 딕셔너리 리스트로 변환
            # enumerate로 실제 시트 행 번호를 추적 (빈 행 포함)
            records = []
            for idx, row_values in enumerate(data_rows):
                # 빈 행 스킵 (단, idx는 계속 증가하여 실제 행 번호 유지)
                if not any(row_values):
                    continue
                record = dict(zip(headers, row_values))
                # _row_number: 실제 시트 행 번호 (1-indexed)
                # idx=0 → 시트 3행 (헤더1 + 설명1 + idx0 + 1)
                record['_row_number'] = idx + 3
                records.append(record)

            return records

        with ErrorContext("워크시트 데이터 조회", worksheet=worksheet_name):
            result = safe_execute(get_data_operation, fallback_return=[])

            if result.success:
                bot_logger.log_sheet_operation("데이터 조회", worksheet_name, True)
                return result.result
            else:
                bot_logger.log_sheet_operation("데이터 조회", worksheet_name, False, str(result.error))
                return []
    
    def append_row(self, worksheet_name: str, values: List[Any]) -> bool:
        """
        워크시트에 행 추가
        
        Args:
            worksheet_name: 워크시트 이름
            values: 추가할 값들
            
        Returns:
            bool: 성공 여부
        """
        def append_operation():
            worksheet = self.get_worksheet(worksheet_name)
            worksheet.append_row(values)
            return True
        
        with ErrorContext("행 추가", worksheet=worksheet_name, values_count=len(values)):
            result = safe_execute(append_operation)
            
            success = result.success
            bot_logger.log_sheet_operation("행 추가", worksheet_name, success, 
                                         str(result.error) if not success else None)
            return success
    
    def update_cell(self, worksheet_name: str, row: int, col: int, value: Any) -> bool:
        """
        특정 셀 업데이트

        Args:
            worksheet_name: 워크시트 이름
            row: 행 번호 (1부터 시작)
            col: 열 번호 (1부터 시작)
            value: 업데이트할 값

        Returns:
            bool: 성공 여부
        """
        def update_operation():
            worksheet = self.get_worksheet(worksheet_name)
            worksheet.update_cell(row, col, value)
            return True

        with ErrorContext("셀 업데이트", worksheet=worksheet_name, row=row, col=col):
            result = safe_execute(update_operation)

            success = result.success
            bot_logger.log_sheet_operation("셀 업데이트", worksheet_name, success,
                                         str(result.error) if not success else None)
            return success

    def get_cell_value_safe(self, worksheet, row: int, col: int) -> Optional[str]:
        """
        이미 확보한 워크시트 객체에서 셀 값을 안전하게 조회 (safe_execute 재시도 포함).

        캐릭터 시트처럼 워크시트 객체를 별도 경로로 얻은 경우(`get_character_worksheet_for_write`),
        gspread 의 raw 호출을 직접 쓰는 대신 이 래퍼를 사용해 일시적 API 오류에 자동 재시도.

        Args:
            worksheet: gspread Worksheet 객체
            row: 1-based 행
            col: 1-based 열

        Returns:
            셀 값 문자열 또는 실패 시 None
        """
        worksheet_title = getattr(worksheet, 'title', '?')

        def read_operation():
            return worksheet.cell(row, col).value

        with ErrorContext("셀 조회", worksheet=worksheet_title, row=row, col=col):
            result = safe_execute(read_operation)
            success = result.success
            bot_logger.log_sheet_operation(
                "셀 조회", worksheet_title, success,
                str(result.error) if not success else None,
            )
            return result.result if success else None

    def batch_get_cells_safe(
        self, worksheet, cells: List[Tuple[int, int]],
    ) -> Optional[List[Optional[str]]]:
        """
        여러 셀을 단일 `batch_get` 호출로 읽어 값 리스트로 반환.

        N 개 셀을 개별 `cell()` 으로 읽으면 N 회의 Google Sheets API 호출이
        발생하지만, `batch_get` 은 한 번의 호출로 처리해 쿼터를 절약한다.
        스탯 변동(현재값 + 최대치) 같이 동일 사용자의 두 셀을 함께 읽을 때 사용.

        Args:
            worksheet: gspread Worksheet 객체.
            cells: `(row, col)` (1-based) 튜플의 리스트.

        Returns:
            셀 값 문자열 리스트 (각 위치는 빈 문자열 또는 None 가능).
            `batch_get` 미지원/실패 시 `None` — 호출자가 `get_cell_value_safe` 로 폴백.
        """
        if not cells:
            return []

        worksheet_title = getattr(worksheet, 'title', '?')
        addresses: List[str] = []
        for row, col in cells:
            col_letter = self._column_number_to_letter(col)
            addresses.append(f"{col_letter}{row}")

        batch_get = getattr(worksheet, "batch_get", None)
        if not callable(batch_get):
            # Mock/legacy 워크시트는 batch_get 이 없을 수 있다 — 호출자가 폴백.
            return None

        def _do_batch_get():
            return worksheet.batch_get(addresses)

        with ErrorContext("배치 셀 조회", worksheet=worksheet_title, count=len(cells)):
            result = safe_execute(_do_batch_get)
            if not result.success:
                bot_logger.log_sheet_operation(
                    f"배치 셀 조회 ({len(cells)}개)", worksheet_title, False,
                    str(result.error),
                )
                return None
            bot_logger.log_sheet_operation(
                f"배치 셀 조회 ({len(cells)}개)", worksheet_title, True,
            )

        responses = result.result or []
        values: List[Optional[str]] = []
        for idx in range(len(cells)):
            cell_value: Optional[str] = None
            if idx < len(responses):
                rows_2d = responses[idx]
                if rows_2d:
                    first_row = rows_2d[0]
                    if first_row:
                        raw = first_row[0]
                        cell_value = str(raw).strip() if raw is not None else ""
            values.append(cell_value)
        return values

    def update_cell_safe(self, worksheet, row: int, col: int, value: Any) -> bool:
        """
        이미 확보한 워크시트 객체에 셀 값을 안전하게 기록 (safe_execute 재시도 포함).

        Args:
            worksheet: gspread Worksheet 객체
            row: 1-based 행
            col: 1-based 열
            value: 기록할 값

        Returns:
            성공 여부
        """
        worksheet_title = getattr(worksheet, 'title', '?')

        def update_operation():
            worksheet.update_cell(row, col, value)
            return True

        with ErrorContext("셀 업데이트", worksheet=worksheet_title, row=row, col=col):
            result = safe_execute(update_operation)
            success = result.success
            bot_logger.log_sheet_operation(
                "셀 업데이트", worksheet_title, success,
                str(result.error) if not success else None,
            )
            return success

    def batch_update_cells(self, worksheet_name: str, updates: List[Tuple[int, int, Any]]) -> bool:
        """
        여러 셀을 원자적으로 업데이트 (트랜잭션 방식)

        Args:
            worksheet_name: 워크시트 이름
            updates: [(row, col, value), ...] 형태의 업데이트 리스트

        Returns:
            bool: 성공 여부
        """
        def batch_update_operation():
            worksheet = self.get_worksheet(worksheet_name)

            # gspread의 batch_update를 사용하여 한 번에 업데이트
            # A1 표기법으로 변환
            cell_list = []
            for row, col, value in updates:
                col_letter = self._column_number_to_letter(col)
                cell_address = f"{col_letter}{row}"
                cell_list.append({'range': cell_address, 'values': [[value]]})

            # batch_update 실행
            if cell_list:
                worksheet.batch_update(cell_list, value_input_option='RAW')

            return True

        with ErrorContext("배치 셀 업데이트", worksheet=worksheet_name, update_count=len(updates)):
            result = safe_execute(batch_update_operation)

            success = result.success
            bot_logger.log_sheet_operation(f"배치 업데이트 ({len(updates)}개 셀)",
                                         worksheet_name, success,
                                         str(result.error) if not success else None)
            return success
    
    # ------------------------------------------------------------------
    # 캐릭터 워크시트 조회
    # ------------------------------------------------------------------
    # CoC 시트 안에는 각 캐릭터가 자신의 워크시트를 하나씩 가진다.
    # 워크시트 이름은 마스토돈 acct 로컬 파트.
    # 예) alpha@example.com → 워크시트 이름 'alpha'

    @staticmethod
    def normalize_character_id(user_id: str) -> str:
        """마스토돈 acct 에서 워크시트 이름(=로컬 파트)을 추출."""
        if not user_id:
            return ""
        user_id = user_id.strip()
        if '@' in user_id:
            return user_id.split('@', 1)[0]
        return user_id

    def _get_character_worksheet(self, user_id: str):
        """user_id 에 해당하는 워크시트 객체 반환. 없으면 None.

        `_worksheets_cache` LRU 를 활용해 동일 사용자의 반복 조회에서 발생하던
        `spreadsheet.worksheet(name)` API 호출을 제거한다. 워크시트 핸들은
        메타데이터만 담고 셀 값은 캐싱하지 않으므로 stale 위험 없음.

        `__init__` 을 우회한 인스턴스(예: 테스트의 `__new__`)에서도 안전하도록
        캐시 속성 부재 시 캐시 없이 동작한다.
        """
        name = self.normalize_character_id(user_id)
        if not name:
            return None

        cache: Optional["OrderedDict[str, gspread.Worksheet]"] = getattr(
            self, '_worksheets_cache', None,
        )

        # 캐시 적중: 최근 접근으로 이동 (LRU 갱신)
        if cache is not None and name in cache:
            cache.move_to_end(name)
            return cache[name]

        try:
            ws = self.spreadsheet.worksheet(name)
        except gspread.exceptions.WorksheetNotFound:
            return None
        except Exception as e:
            logger.warning(f"캐릭터 워크시트 조회 실패 ({name}): {e}")
            return None

        # 캐시 저장 + LRU 상한 초과 시 가장 오래된 항목 제거.
        if cache is not None:
            cache[name] = ws
            while len(cache) > self._WORKSHEETS_CACHE_LIMIT:
                cache.popitem(last=False)
        return ws

    def character_worksheet_exists(self, user_id: str) -> bool:
        """CoC 시트에 해당 캐릭터의 워크시트가 있는지 확인."""
        return self._get_character_worksheet(user_id) is not None

    def get_character_data(self, user_id: str) -> Optional[List[Dict[str, Any]]]:
        """
        캐릭터 워크시트를 읽어 레코드 리스트로 반환.

        레이아웃 규약:
          - 1행 헤더, 2행 설명, 3행부터 데이터 (프로젝트 공통 규칙)
          - 각 행은 `dict(header → value)`, `_row_number` 는 실제 시트 1-based 행 번호.

        캐릭터 시트의 헤더·컬럼 구성은 CoC 룰의 정의를 따른다.
        명령어가 필요한 컬럼을 읽어 사용한다.

        Args:
            user_id: 마스토돈 acct (로컬 파트가 워크시트 이름과 일치)

        Returns:
            List[Dict] | None: 워크시트가 없으면 None.
        """
        ws = self._get_character_worksheet(user_id)
        if ws is None:
            return None

        try:
            all_values = ws.get_all_values()
            if len(all_values) < 3:
                # 헤더+설명만 있고 데이터가 없는 경우 빈 리스트.
                return []

            headers = all_values[0]
            records: List[Dict[str, Any]] = []
            for offset, row_values in enumerate(all_values[2:], start=3):
                if not any(row_values):
                    continue
                record = dict(zip(headers, row_values))
                record['_row_number'] = offset
                records.append(record)
            return records
        except Exception as e:
            logger.warning(f"캐릭터 데이터 조회 실패 ({user_id}): {e}")
            return None

    def get_character_worksheet_for_write(self, user_id: str):
        """
        쓰기 작업용 워크시트 핸들 반환. 없으면 None.

        쓰기는 `update_cell(worksheet_name, row, col, value)` 또는
        `batch_update_cells(worksheet_name, updates)` 를 사용.
        이 메서드는 워크시트 존재 확인 + 워크시트 이름 획득용 보조 API.
        """
        ws = self._get_character_worksheet(user_id)
        return ws

    @staticmethod
    def get_current_time() -> str:
        """
        현재 KST 기준 시간 반환
        
        Returns:
            str: 현재 시간 (YYYY-MM-DD HH:MM:SS 형식)
        """
        return datetime.now(pytz.timezone('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S')
    
    def get_help_items(self, sheet_name: Optional[str] = None) -> List[Dict[str, str]]:
        """
        도움말 항목들 조회

        Args:
            sheet_name: 도움말 시트 이름 (None이면 기본 HELP 시트 사용)

        Returns:
            List[Dict]: [{'명령어': str, '설명': str}] 형태의 리스트
        """
        # 시트 이름이 지정되지 않으면 기본 HELP 시트 사용
        if sheet_name is None:
            sheet_name = config.get_worksheet_name('HELP')

        help_data = self.get_worksheet_data(sheet_name)
        help_items = []

        for row in help_data:
            command = str(row.get('명령어', '')).strip()
            description = str(row.get('설명', '')).strip()

            if command and description:
                help_items.append({'명령어': command, '설명': description})

        return help_items
    
    def _column_number_to_letter(self, col_num: int) -> str:
        """
        컬럼 번호를 알파벳으로 변환 (1 -> A, 2 -> B, ...)
        
        Args:
            col_num: 컬럼 번호 (1부터 시작)
            
        Returns:
            str: 컬럼 알파벳 (A, B, C, ..., AA, AB, ...)
        """
        result = ""
        while col_num > 0:
            col_num -= 1
            result = chr(col_num % 26 + ord('A')) + result
            col_num //= 26
        return result
    
    def _find_student_row_by_id(self, user_id: str) -> Optional[int]:
        """
        사용자 ID로 학생관리 시트에서 행 번호 찾기 (1행: 헤더, 2행: 설명, 3행부터: 데이터)

        Args:
            user_id: 사용자 ID

        Returns:
            Optional[int]: 행 번호 (1부터 시작) 또는 None
        """
        try:
            worksheet = self.get_worksheet('학생관리')
            all_values = worksheet.get_all_values()

            # 헤더에서 '아이디' 컬럼 찾기
            if not all_values:
                return None

            headers = all_values[0]  # 1행: 헤더
            id_col = None
            for i, header in enumerate(headers):
                if header == '아이디':
                    id_col = i
                    break

            if id_col is None:
                return None

            # 사용자 ID가 있는 행 찾기 (3행부터 데이터)
            for i, row in enumerate(all_values[2:], start=3):  # 3번째 행부터 시작
                if len(row) > id_col and str(row[id_col]).strip() == user_id:
                    return i

            return None

        except Exception as e:
            logger.error(f"학생 행 찾기 실패: {e}")
            return None
    
    # ==================== 보조 시트 (랜덤표 / 커스텀) ====================

    def _open_aux_spreadsheet(self, sheet_id: str) -> Optional[gspread.Spreadsheet]:
        """보조 스프레드시트 1회 연결. 인증 파일은 본 시트와 공유."""
        if not sheet_id:
            return None
        try:
            gc = gspread.service_account(filename=str(self.credentials_path))
            spreadsheet = gc.open_by_key(sheet_id)
            logger.debug(f"보조 스프레드시트 연결: {sheet_id}")
            return spreadsheet
        except FileNotFoundError:
            logger.error(f"보조 스프레드시트 인증 파일 없음: {self.credentials_path}")
        except gspread.exceptions.SpreadsheetNotFound:
            logger.error(f"보조 스프레드시트를 찾을 수 없음 (ID={sheet_id})")
        except Exception as e:
            logger.error(f"보조 스프레드시트 연결 실패 (ID={sheet_id}): {e}")
        return None

    def _get_random_table_spreadsheet(self) -> Optional[gspread.Spreadsheet]:
        """랜덤표 스프레드시트 핸들 (lazy)."""
        if not getattr(config, 'RANDOM_TABLE_SHEET_ID', ''):
            return None
        with self._aux_lock:
            if self._random_table_spreadsheet is None:
                self._random_table_spreadsheet = self._open_aux_spreadsheet(
                    config.RANDOM_TABLE_SHEET_ID
                )
            return self._random_table_spreadsheet

    def _get_custom_command_spreadsheet(self) -> Optional[gspread.Spreadsheet]:
        """커스텀 명령어 스프레드시트 핸들 (lazy)."""
        if not getattr(config, 'CUSTOM_COMMAND_SHEET_ID', ''):
            return None
        with self._aux_lock:
            if self._custom_command_spreadsheet is None:
                self._custom_command_spreadsheet = self._open_aux_spreadsheet(
                    config.CUSTOM_COMMAND_SHEET_ID
                )
            return self._custom_command_spreadsheet

    def _aux_cache_ttl(self) -> int:
        """보조 시트 캐시 TTL (config.CACHE_TTL 사용, 최소 30초)."""
        ttl = getattr(config, 'CACHE_TTL', 1800)
        try:
            ttl = int(ttl)
        except (TypeError, ValueError):
            ttl = 1800
        return max(ttl, 30)

    def _refresh_random_table_index(self) -> Dict[str, str]:
        """랜덤표의 워크시트 이름 인덱스를 (재)구축. {정규화된 이름: 실제 이름}."""
        spreadsheet = self._get_random_table_spreadsheet()
        if spreadsheet is None:
            return {}

        index: Dict[str, str] = {}
        try:
            worksheets = spreadsheet.worksheets()
        except Exception as e:
            logger.warning(f"랜덤표 워크시트 목록 조회 실패: {e}")
            return {}

        for ws in worksheets:
            title = (ws.title or '').strip()
            if not title:
                continue
            normalized = _normalize_keyword(title)
            if not normalized:
                continue
            # 동일 정규화에 여러 시트가 매핑되면 먼저 발견된 것을 유지.
            index.setdefault(normalized, title)
        return index

    def _get_random_table_index(self) -> Dict[str, str]:
        """랜덤표 워크시트 인덱스 (TTL 캐시)."""
        with self._aux_lock:
            now = time.time()
            if self._random_table_index_cache is not None and now < self._random_table_index_expires:
                return self._random_table_index_cache
            index = self._refresh_random_table_index()
            self._random_table_index_cache = index
            self._random_table_index_expires = now + self._aux_cache_ttl()
            return index

    def _read_random_table_values(self, worksheet_title: str) -> List[str]:
        """랜덤표의 한 워크시트에서 2행~끝 컬럼 A의 비어 있지 않은 값들을 반환."""
        spreadsheet = self._get_random_table_spreadsheet()
        if spreadsheet is None:
            return []

        try:
            worksheet = spreadsheet.worksheet(worksheet_title)
        except gspread.exceptions.WorksheetNotFound:
            logger.debug(f"랜덤표 워크시트 사라짐: {worksheet_title}")
            return []
        except Exception as e:
            logger.warning(f"랜덤표 워크시트 접근 실패 ({worksheet_title}): {e}")
            return []

        try:
            column_values = worksheet.col_values(1)
        except Exception as e:
            logger.warning(f"랜덤표 컬럼 읽기 실패 ({worksheet_title}): {e}")
            return []

        # 1행은 헤더로 간주하고 스킵, 2행부터 비어 있지 않은 값만.
        values = [v.strip() for v in column_values[1:] if v and v.strip()]
        return values

    def pick_random_table_value(self, table_name: str) -> Optional[str]:
        """
        랜덤표 시트에서 `table_name` 과 매칭되는 워크시트의 무작위 값을 반환.

        매칭 규칙: 대소문자 무시, 모든 공백 제거.
        매칭되는 워크시트가 없거나 값이 없으면 None.
        """
        if not table_name or not getattr(config, 'RANDOM_TABLE_SHEET_ID', ''):
            return None

        normalized = _normalize_keyword(table_name)
        if not normalized:
            return None

        index = self._get_random_table_index()
        actual_title = index.get(normalized)
        if actual_title is None:
            # 캐시가 오래된 경우 한 번 더 강제 새로고침 시도.
            with self._aux_lock:
                self._random_table_index_cache = None
                self._random_table_index_expires = 0.0
            index = self._get_random_table_index()
            actual_title = index.get(normalized)
            if actual_title is None:
                return None

        ttl = self._aux_cache_ttl()
        now = time.time()
        with self._aux_lock:
            cached = self._random_table_values_cache.get(actual_title)
            if cached and now < cached[0]:
                values = cached[1]
            else:
                values = self._read_random_table_values(actual_title)
                self._random_table_values_cache[actual_title] = (now + ttl, values)

        if not values:
            logger.debug(f"랜덤표 '{actual_title}'에 사용 가능한 값이 없음")
            return None

        return random.choice(values)

    def _refresh_custom_command_cache(self) -> Dict[str, List[str]]:
        """'커스텀' 워크시트 전체를 읽어 {정규화된 명령어: [문구, ...]} 로 반환."""
        spreadsheet = self._get_custom_command_spreadsheet()
        if spreadsheet is None:
            return {}

        worksheet_name = getattr(config, 'CUSTOM_COMMAND_WORKSHEET', '커스텀') or '커스텀'

        try:
            worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            logger.warning(f"커스텀 명령어 워크시트를 찾을 수 없음: {worksheet_name}")
            return {}
        except Exception as e:
            logger.warning(f"커스텀 명령어 워크시트 접근 실패: {e}")
            return {}

        try:
            all_values = worksheet.get_all_values()
        except Exception as e:
            logger.warning(f"커스텀 명령어 데이터 읽기 실패: {e}")
            return {}

        if len(all_values) < 3:
            return {}

        headers = [h.strip() for h in all_values[0]]
        try:
            cmd_col = headers.index('명령어')
            text_col = headers.index('문구')
        except ValueError:
            logger.warning(
                "커스텀 명령어 워크시트 헤더에 '명령어'/'문구' 컬럼이 없음 "
                f"(현재 헤더: {headers})"
            )
            return {}

        # 2행은 설명행으로 무시, 3행부터 데이터.
        result: Dict[str, List[str]] = {}
        for row in all_values[2:]:
            if len(row) <= max(cmd_col, text_col):
                continue
            cmd = (row[cmd_col] or '').strip()
            text = (row[text_col] or '').strip()
            if not cmd or not text:
                continue
            normalized = _normalize_keyword(cmd)
            if not normalized:
                continue
            result.setdefault(normalized, []).append(text)
        return result

    def _get_custom_command_cache(self) -> Dict[str, List[str]]:
        """커스텀 명령어 캐시 (TTL)."""
        with self._aux_lock:
            now = time.time()
            if self._custom_command_cache is not None and now < self._custom_command_cache_expires:
                return self._custom_command_cache
            cache = self._refresh_custom_command_cache()
            self._custom_command_cache = cache
            self._custom_command_cache_expires = now + self._aux_cache_ttl()
            return cache

    def pick_custom_command_value(self, command_name: str) -> Optional[str]:
        """
        커스텀 명령어 시트에서 `command_name` 과 매칭되는 문구를 무작위로 반환.

        매칭 규칙: 대소문자 무시, 모든 공백 제거.
        매칭이 없으면 None.
        """
        if not command_name or not getattr(config, 'CUSTOM_COMMAND_SHEET_ID', ''):
            return None

        normalized = _normalize_keyword(command_name)
        if not normalized:
            return None

        cache = self._get_custom_command_cache()
        phrases = cache.get(normalized)
        if not phrases:
            # 캐시 만료 직전에 시트가 갱신됐을 수 있으므로 1회 강제 갱신.
            with self._aux_lock:
                self._custom_command_cache = None
                self._custom_command_cache_expires = 0.0
            cache = self._get_custom_command_cache()
            phrases = cache.get(normalized)
            if not phrases:
                return None

        return random.choice(phrases)

    def invalidate_worksheets_cache(self) -> None:
        """이 봇의 메인 시트의 워크시트 핸들 LRU 캐시만 비운다.

        캐릭터 데이터(`get_character_data`) 는 매번 시트에서 직접 읽으므로
        별도 데이터 캐시가 없다. 워크시트 핸들 캐시만 비우면 다음 요청 시점에
        시트 메타데이터가 새로 fetch 되어 시트 추가/이름변경/삭제가 즉시 반영된다.
        보조 시트(랜덤표/커스텀) 캐시는 건드리지 않는다.
        """
        self._worksheets_cache.clear()

    def count_character_worksheets(self) -> int:
        """이 봇 시트의 캐릭터 워크시트 개수 (도움말 워크시트 제외).

        Returns:
            캐릭터 워크시트 개수. 시트 목록 조회 실패 시 -1.
        """
        try:
            all_ws = self.spreadsheet.worksheets()
        except Exception as e:
            logger.warning(f"워크시트 목록 조회 실패: {e}")
            return -1

        # 도움말 시트 이름 — env/config 에서 가져와 그것만 제외.
        help_name = ''
        try:
            getter = getattr(config, 'get_worksheet_name', None)
            if callable(getter):
                help_name = (getter('HELP') or '').strip()
        except Exception:
            help_name = ''

        return sum(
            1 for ws in all_ws
            if (getattr(ws, 'title', '') or '').strip() != help_name
        )

    def invalidate_random_table_cache(self) -> None:
        """랜덤표 캐시(인덱스 + 워크시트별 값)만 무효화."""
        with self._aux_lock:
            self._random_table_index_cache = None
            self._random_table_index_expires = 0.0
            self._random_table_values_cache.clear()

    def invalidate_custom_command_cache(self) -> None:
        """커스텀 명령어 캐시만 무효화."""
        with self._aux_lock:
            self._custom_command_cache = None
            self._custom_command_cache_expires = 0.0

    def invalidate_aux_caches(self) -> None:
        """랜덤표/커스텀 명령어 캐시를 모두 무효화."""
        self.invalidate_random_table_cache()
        self.invalidate_custom_command_cache()

    def warmup_random_table(self) -> int:
        """랜덤표 워크시트 인덱스를 미리 채운다.

        Returns:
            캐시된 워크시트 개수. `RANDOM_TABLE_SHEET_ID` 미설정 시 -1.
        """
        if not getattr(config, 'RANDOM_TABLE_SHEET_ID', ''):
            return -1
        index = self._get_random_table_index()
        return len(index)

    def warmup_custom_command(self) -> int:
        """커스텀 명령어 캐시를 미리 채운다.

        Returns:
            캐시된 명령어 개수. `CUSTOM_COMMAND_SHEET_ID` 미설정 시 -1.
        """
        if not getattr(config, 'CUSTOM_COMMAND_SHEET_ID', ''):
            return -1
        cache = self._get_custom_command_cache()
        return len(cache)

    # ==================== 기존 메서드들 ====================

    def clear_cache(self):
        """워크시트 캐시 초기화"""
        self._worksheets_cache.clear()
        self.invalidate_aux_caches()
        if should_log_debug():
            logger.debug("워크시트 캐시가 초기화되었습니다.")
    
    def validate_sheet_structure(self) -> Dict[str, Any]:
        """
        시트 구조 검증
        
        Returns:
            Dict: 검증 결과
        """
        validation_results = {
            'valid': True,
            'errors': [],
            'warnings': [],
            'worksheets_found': []
        }
        
        try:
            # 모든 워크시트 이름 가져오기
            all_worksheets = [ws.title for ws in self.spreadsheet.worksheets()]
            validation_results['worksheets_found'] = all_worksheets

            # 필수 워크시트: 도움말만. 캐릭터 데이터는 각 사용자 acct 이름의
            # 워크시트이므로 부팅 시점에 전부 검증하지 않는다.
            required_worksheets = [
                config.get_worksheet_name('HELP'),
            ]

            for required in required_worksheets:
                if required and required not in all_worksheets:
                    validation_results['warnings'].append(
                        f"권장 워크시트 '{required}'가 없습니다. (도움말 명령어 사용 시 비어 있는 응답)"
                    )

            self._validate_help_structure(validation_results)

        except Exception as e:
            validation_results['errors'].append(f"시트 구조 검증 중 오류: {str(e)}")
            validation_results['valid'] = False
        
        return validation_results
    
    def _validate_help_structure(self, results: Dict):
        """도움말 시트 구조 검증 (1행: 헤더, 2행: 설명)"""
        try:
            worksheet = self.get_worksheet(config.get_worksheet_name('HELP'))
            if worksheet.row_count > 1:  # 헤더와 설명 행 필요
                headers = worksheet.row_values(1)
                required_headers = ['명령어', '설명']
                for header in required_headers:
                    if header not in headers:
                        results['errors'].append(f"'도움말' 시트에 '{header}' 헤더가 없습니다.")
                        results['valid'] = False

                # 2행 설명 행 존재 확인
                if worksheet.row_count < 2:
                    results['warnings'].append(f"'도움말' 시트에 설명 행(2행)이 없습니다.")
            else:
                results['errors'].append(f"'도움말' 시트에 헤더와 설명 행이 필요합니다.")
                results['valid'] = False
        except Exception as e:
            results['errors'].append(f"도움말 시트 검증 실패: {str(e)}")
            results['valid'] = False
    
# 전역 인스턴스 (편의)
_global_sheets_manager: Optional[SheetsManager] = None


def get_sheets_manager() -> SheetsManager:
    """전역 SheetsManager 인스턴스 반환."""
    global _global_sheets_manager
    if _global_sheets_manager is None:
        _global_sheets_manager = SheetsManager()
    return _global_sheets_manager