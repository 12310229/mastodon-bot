"""
캐시 관리 (CoC 봇)

도움말 시트만 캐싱한다. 캐릭터 워크시트는 요청 시점에 동적으로 읽는다.
일반 키/값 저장용 `CacheManager` + 봇 전역 `BotCacheManager` 만 유지.
"""

import os
import sys
import time
import threading
from collections import OrderedDict
from typing import Any, Optional, Dict, List

try:
    from config.settings import config
    from utils.logging_config import logger, should_log_debug
except ImportError:
    import logging

    logger = logging.getLogger('cache_manager')

    class _FallbackConfig:
        DEBUG_MODE = False

    config = _FallbackConfig()

    def should_log_debug() -> bool:
        return False


class CacheManager:
    """스레드 안전 key/value LRU 캐시. TTL 은 외부에서 관리.

    `OrderedDict` + `move_to_end` 기반 진짜 LRU:
        - `get(key)` 적중 시 항목을 끝으로 이동 → 최근 사용 표시
        - 가득 차서 새 항목을 넣을 때 가장 오래 사용되지 않은 항목(맨 앞) 제거
    """

    def __init__(self, max_size: int = 500):
        self.max_size = max_size
        self._data: "OrderedDict[str, Any]" = OrderedDict()
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._data:
                return None
            # LRU: 최근 접근을 끝으로 이동.
            self._data.move_to_end(key)
            return self._data[key]

    def set(self, key: str, value: Any) -> bool:
        with self._lock:
            if key in self._data:
                # 기존 항목 갱신 — 끝으로 이동 후 값 교체.
                self._data.move_to_end(key)
                self._data[key] = value
                return True
            # 새 항목 — 상한 초과 시 가장 오래된 항목(맨 앞) 제거.
            while len(self._data) >= self.max_size:
                evicted_key, _ = self._data.popitem(last=False)
                if should_log_debug():
                    logger.debug(f"LRU 제거: {evicted_key}")
            self._data[key] = value
            return True

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._data:
                del self._data[key]
                return True
            return False

    def clear(self) -> int:
        with self._lock:
            count = len(self._data)
            self._data.clear()
            return count

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def get_keys(self, pattern: Optional[str] = None) -> List[str]:
        with self._lock:
            keys = list(self._data.keys())
            if pattern:
                keys = [k for k in keys if pattern in k]
            return keys

    def get_size(self) -> int:
        with self._lock:
            return len(self._data)


class BotCacheManager:
    """봇 전역 캐시. 도움말 시트 전용 API.

    도움말 캐시 키는 항상 **실제 시트 이름**으로 정규화된다 — 호출자가
    `sheet_name=None` 으로 부르더라도 `config.get_worksheet_name('HELP')` 로
    해석되므로, 워밍업/명령어 양쪽이 동일 키를 사용해 캐시 미스가 발생하지 않는다.
    """

    _HELP_CACHE_PREFIX = "help_items"
    _DEFAULT_FALLBACK_NAME = "__default__"

    def __init__(self):
        # 도움말 시트는 봇당 1~소수 개 — 50 으로 충분.
        self.command_cache = CacheManager(max_size=50)
        logger.debug("[초기화] BotCacheManager")

    # --- 도움말 시트 ---
    def _resolve_help_sheet_name(self, sheet_name: Optional[str]) -> str:
        """
        호출자가 `None` 을 넘긴 경우 실제 기본 도움말 시트 이름으로 정규화.

        config.get_worksheet_name('HELP') 가 환경 변수 `HELP_SHEET` 를 반영하므로
        시트 이름을 캐시 키로 사용한다. config 가 없는 테스트 환경(폴백 config)
        에서는 `__default__` 으로 폴백.
        """
        if sheet_name:
            return sheet_name
        getter = getattr(config, 'get_worksheet_name', None)
        if callable(getter):
            try:
                resolved = getter('HELP')
            except Exception:
                resolved = None
            if resolved:
                return resolved
        return self._DEFAULT_FALLBACK_NAME

    def _help_key(self, sheet_name: Optional[str]) -> str:
        return f"{self._HELP_CACHE_PREFIX}:{self._resolve_help_sheet_name(sheet_name)}"

    def cache_help_items(
        self,
        help_items: List[Dict[str, str]],
        sheet_name: Optional[str] = None,
    ) -> bool:
        ttl = getattr(config, 'CACHE_TTL', 1800)
        payload = {
            'data': help_items,
            'expire_time': time.time() + ttl,
            'cached_at': time.time(),
        }
        return self.command_cache.set(self._help_key(sheet_name), payload)

    def get_help_items(
        self, sheet_name: Optional[str] = None,
    ) -> Optional[List[Dict[str, str]]]:
        key = self._help_key(sheet_name)
        payload = self.command_cache.get(key)
        if not payload:
            return None
        if time.time() > payload.get('expire_time', 0):
            self.command_cache.delete(key)
            return None
        return payload.get('data')

    def invalidate_help_cache(self, sheet_name: Optional[str] = None) -> bool:
        """특정 시트 캐시만 무효화. sheet_name 미지정 시 모든 도움말 캐시 삭제."""
        # 명시적 sheet_name 이 들어오면 정확히 그 키만 무효화 — 정규화 없이.
        # 이전 코드와 동작 호환을 위해 None 일 때 전체 무효화 유지.
        if sheet_name is not None:
            return self.command_cache.delete(self._help_key(sheet_name))
        # 전체 무효화 — `_HELP_CACHE_PREFIX` 로 시작하는 키 전부.
        prefix = f"{self._HELP_CACHE_PREFIX}:"
        keys = self.command_cache.get_keys(prefix)
        any_deleted = False
        for k in keys:
            any_deleted = self.command_cache.delete(k) or any_deleted
        return any_deleted

    def cleanup_all_expired(self) -> None:
        """명시 무효화만 사용하므로 실제 작업 없음 (main.py 정리 경로 호환)."""
        if should_log_debug():
            logger.debug("cleanup_all_expired 호출 (no-op)")


# 전역 싱글톤
bot_cache = BotCacheManager()


def warmup_cache(sheets_manager, sheet_name: Optional[str] = None) -> None:
    """부팅 시 도움말 시트를 프리로드.

    Args:
        sheets_manager: SheetsManager 인스턴스.
        sheet_name: 워밍업할 도움말 시트 이름. None 이면 `config.get_worksheet_name('HELP')`.
            호출자가 명시하지 않아도 BotCacheManager 가 같은 규칙으로 키를 정규화하므로
            HelpCommand 의 캐시 적중이 보장된다.
    """
    try:
        from config.settings import config as _config
    except ImportError:
        _config = config

    def _is_rate_limit(err: Exception) -> bool:
        msg = str(err).lower()
        return '429' in msg or ('rate' in msg and 'limit' in msg)

    max_attempts = max(getattr(_config, 'MAX_RETRIES', 5), 1)
    attempt = 0
    while attempt < max_attempts:
        try:
            help_items = sheets_manager.get_help_items(sheet_name=sheet_name)
            bot_cache.cache_help_items(help_items, sheet_name=sheet_name)
            resolved = bot_cache._resolve_help_sheet_name(sheet_name)
            logger.info(f"  ✓ 도움말 캐시 ({len(help_items)}개 항목)")
            logger.debug(f"   sheet={resolved}")
            return
        except Exception as e:
            attempt += 1
            if _is_rate_limit(e):
                logger.warning(
                    f"Google API 호출 제한에 걸렸습니다 — 60초 후 재시도 ({attempt}/{max_attempts})"
                )
                time.sleep(60)
            else:
                if attempt < max_attempts:
                    logger.warning(
                        f"도움말 캐시 준비 실패 — 10초 후 재시도 ({attempt}/{max_attempts})"
                    )
                    logger.debug(f"  사유: {e}")
                    time.sleep(10)
                else:
                    logger.warning(
                        f"도움말 캐시 준비 포기 (재시도 {max_attempts}회 초과) — 첫 사용 시 지연 가능"
                    )


def warmup_aux_caches(sheets_manager) -> None:
    """부팅 시 보조 시트(랜덤표/커스텀) 인덱스/캐시를 프리로드.

    이걸 안 하면 첫 사용자의 보조 시트 명령어에서 스프레드시트 연결 비용
    (3~5초/시트) 이 통째로 발생한다. 각 봇이 부팅 시 한 번 호출.

    설정되지 않은 시트(env 미설정) 는 자동 스킵.
    """
    if sheets_manager is None:
        return

    # 랜덤표
    try:
        rt_count = sheets_manager.warmup_random_table()
        if rt_count >= 0:
            logger.info(f"  ✓ 랜덤표 캐시 ({rt_count}개 표)")
    except Exception as e:
        logger.warning("랜덤표 캐시 준비 실패 — 첫 사용 시 지연 가능")
        logger.debug(f"  사유: {e}")

    # 커스텀
    try:
        cmd_count = sheets_manager.warmup_custom_command()
        if cmd_count >= 0:
            logger.info(f"  ✓ 커스텀 명령어 캐시 ({cmd_count}개)")
    except Exception as e:
        logger.warning("커스텀 명령어 캐시 준비 실패 — 첫 사용 시 지연 가능")
        logger.debug(f"  사유: {e}")
