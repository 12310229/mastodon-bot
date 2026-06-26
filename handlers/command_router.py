"""
새로운 명령어 라우터 - 개선된 버전
기존 command_router.py를 새로운 아키텍처로 완전히 교체하고 피드백을 반영한 개선 버전
"""

import os
import sys
import re
import threading
import time
import uuid
from typing import List, Optional, Dict, Any, Tuple, Protocol, Union
from abc import ABC, abstractmethod
from utils.log_sanitizer import sanitize_log_input

# 경로 설정
try:
    from commands.registry import get_registry, CommandRegistry
    from commands.factory import get_factory, CommandFactory, create_command_context
    from commands.base_command import BaseCommand, CommandContext, CommandResponse
    from models.dynamic_command_types import CommandType, DynamicCommandType, get_command_type
    from models.command_result import CommandResult
    from utils.logging_config import logger, LogFormatter, should_log_debug, log_command_result
    from utils.sheets_operations import SheetsManager
    from config.settings import config
    IMPORTS_AVAILABLE = True
except ImportError as e:
    import logging
    logger = logging.getLogger('command_router')
    logger.warning(f"모듈 임포트 실패: {e}")

    class CommandRegistry:
        pass

    class CommandFactory:
        pass

    IMPORTS_AVAILABLE = False

# 공통 결과 클래스들 (DRY 위반 해결)
class CommandResultProtocol(Protocol):
    """CommandResult 프로토콜 (타입 안정성 보장)"""
    
    def is_successful(self) -> bool:
        """성공 여부 반환"""
        ...
    
    def get_user_message(self) -> str:
        """사용자 메시지 반환"""
        ...


class FallbackCommandResult:
    """CommandResult를 import할 수 없을 때 사용하는 폴백 결과"""
    
    def __init__(self, success: bool, message: str, user_id: str = "", execution_time: float = 0.0):
        self.success = success
        self.message = message
        self.user_id = user_id
        self.execution_time = execution_time
        
    def is_successful(self) -> bool:
        """성공 여부 반환"""
        return self.success
        
    def get_user_message(self) -> str:
        """사용자 메시지 반환"""
        return self.message


class ErrorResult:
    """에러 결과 전용 클래스"""
    
    def __init__(self, message: str, user_id: str = ""):
        self.message = message
        self.success = False
        self.user_id = user_id
    
    def is_successful(self) -> bool:
        """항상 False 반환"""
        return False
    
    def get_user_message(self) -> str:
        """에러 메시지 반환"""
        return self.message


class ModernCommandRouter:
    """
    새로운 명령어 라우터 - 개선된 버전
    
    레지스트리와 팩토리를 사용하여 동적으로 명령어를 처리합니다.
    기존의 하드코딩된 라우팅 로직을 완전히 제거했습니다.
    """
    
    def __init__(self, sheets_manager: 'SheetsManager' = None, api=None, **additional_deps):
        """
        ModernCommandRouter 초기화

        Args:
            sheets_manager: Google Sheets 관리자
            api: 마스토돈 API 인스턴스
            **additional_deps: 추가 의존성 (팩토리로 그대로 전달)
        """
        self.sheets_manager = sheets_manager
        self.api = api
        self._additional_deps = additional_deps

        # 레지스트리와 팩토리 초기화 (import 가능한 경우만)
        if IMPORTS_AVAILABLE:
            self.registry = get_registry()
            self.factory = get_factory()

            self.factory.configure_dependencies(
                sheets_manager=sheets_manager,
                mastodon_api=api,
                **additional_deps,
            )

            self._initialize()
        else:
            self.registry = None
            self.factory = None
            logger.warning("의존성 임포트 실패 - 제한된 모드로 실행")
        
        logger.info("ModernCommandRouter 초기화 완료")
    
    def _initialize(self) -> None:
        """라우터 초기화"""
        if not IMPORTS_AVAILABLE or not self.registry:
            logger.warning("[초기화] 의존성 임포트 실패로 초기화 생략")
            return

        try:
            logger.info("[초기화] ModernCommandRouter 초기화 시작")

            # 명령어 발견
            discovered_count = self.registry.discover_commands()
            logger.info(f"[초기화] 명령어 발견 완료: {discovered_count}개")

            # 싱글톤 인스턴스 미리 생성
            # 주의: sheets_manager가 없어도 일부 명령어는 생성 가능
            if self.factory:
                logger.debug(f"[초기화] 싱글톤 인스턴스 생성 시작 (sheets_manager={'있음' if self.sheets_manager else '없음'})")
                singleton_results = self.factory.create_all_singleton_instances()
                success_count = sum(singleton_results.values())
                total_count = len(singleton_results)

                if success_count < total_count:
                    failed_count = total_count - success_count
                    logger.warning(f"[초기화] 싱글톤 인스턴스 일부 생성 실패: {success_count}/{total_count} (실패={failed_count})")
                    logger.debug(f"[초기화] sheets_manager 상태: {'있음' if self.sheets_manager else '없음'}, api 상태: {'있음' if self.api else '없음'}")
                else:
                    logger.info(f"[초기화] 싱글톤 인스턴스 생성 완료: {success_count}/{total_count}")

            logger.info("[초기화] ModernCommandRouter 초기화 완료")

        except Exception as e:
            logger.error(f"[초기화] 라우터 초기화 중 예외 발생: {e}", exc_info=True)
    
    def route_command(
        self,
        user_id: str,
        keywords: List[str],
        context: Dict[str, Any] = None
    ) -> CommandResultProtocol:
        """
        명령어 라우팅 및 실행

        Args:
            user_id: 사용자 ID
            keywords: 명령어 키워드들
            context: 실행 컨텍스트 (기존 호환성)

        Returns:
            CommandResultProtocol: 명령어 실행 결과 (타입 안정성 보장)
        """
        start_time = time.time()

        try:
            # 1. 입력 검증
            if not keywords:
                logger.warning("[라우팅] 명령어가 비어있음")
                return self._create_error_result(
                    user_id,
                    "명령어를 입력해 주세요. 예: [도움말], [2d6]",
                )

            first_keyword = keywords[0].strip()
            full_command = '/'.join(keywords)

            if should_log_debug():
                logger.debug(LogFormatter.command(user_id, full_command, True, details="명령어 처리 시작"))

            # 2. 의존성 확인
            if not IMPORTS_AVAILABLE or not self.factory:
                logger.error("[라우팅] 명령어 시스템 초기화 실패 - IMPORTS_AVAILABLE={}, factory={}".format(
                    IMPORTS_AVAILABLE, self.factory is not None
                ))
                return self._create_error_result(
                    user_id,
                    "봇이 아직 준비되지 않았습니다. 잠시 후 다시 시도해 주세요. "
                    "문제가 계속되면 운영자에게 문의해 주세요."
                )

            first_keyword_lower = first_keyword.lower()

            # 3. 레지스트리 매칭 정보 수집
            registered = (
                self.registry.get_command_by_keyword(first_keyword_lower)
                if self.registry else None
            )
            registered_package = (
                (registered.metadata.command_package or '').strip().lower()
                if registered else ''
            )

            # 4. 1순위: 공용 명령어 (다이스/랜덤/yn/도움말 등)
            if registered and registered_package in ('default', 'system'):
                executed = self._execute_registered(
                    registered, user_id, keywords, context,
                    first_keyword_lower, full_command, start_time,
                )
                if executed is not None:
                    return executed

            # 5. 2순위: 랜덤표 시트
            random_table_value = self._lookup_random_table(first_keyword)
            if random_table_value is not None:
                return self._build_aux_sheet_result(
                    user_id, keywords, full_command, first_keyword_lower,
                    random_table_value, start_time, source='random_table',
                )

            # 6. 3순위: 커스텀 명령어 시트
            custom_value = self._lookup_custom_command(first_keyword)
            if custom_value is not None:
                return self._build_aux_sheet_result(
                    user_id, keywords, full_command, first_keyword_lower,
                    custom_value, start_time, source='custom',
                )

            # 7. 4순위: CoC 룰 명령어 (default/system 외에 등록된 것)
            if registered:
                executed = self._execute_registered(
                    registered, user_id, keywords, context,
                    first_keyword_lower, full_command, start_time,
                )
                if executed is not None:
                    return executed

            # 8. 5순위: CoC 폴백 핸들러
            #    위에서 모두 매칭 실패하면 CoC 룰 폴백 (`__coc_fallback__`,
            #    시트 기반 능력치/기능/무기/스탯변동) 으로 넘긴다.
            #    폴백이 자체 에러 메시지를 가질 수 있으므로 (예: "'근력'은 시트에서
            #    찾을 수 없습니다") 성공/실패 모두 그 결과를 그대로 반환한다.
            fallback_instance = self.factory.create_command_by_keyword('__coc_fallback__')
            if fallback_instance is not None:
                if should_log_debug():
                    logger.debug(f"[라우팅] CoC 폴백 사용 (키워드={first_keyword})")

                execution_context = self._create_execution_context(user_id, keywords, context)
                response = self._execute_command(fallback_instance, execution_context)

                execution_time = time.time() - start_time
                command_result = self._convert_to_command_result(
                    response, first_keyword_lower, user_id, keywords, execution_time,
                )
                log_command_result(
                    user_id=user_id,
                    command=full_command,
                    success=command_result.is_successful(),
                    duration=execution_time,
                    result_length=len(command_result.get_user_message()) if command_result.get_user_message() else 0,
                )
                return command_result

            # 9. 모두 매칭 실패 → "없는 명령어" 안내
            execution_time = time.time() - start_time
            log_command_result(
                user_id=user_id,
                command=full_command,
                success=False,
                duration=execution_time,
                result_length=0,
            )
            return self._create_error_result(
                user_id,
                f"[{first_keyword}]은(는) 사용 가능한 명령어가 아닙니다. "
                f"[도움말]을 입력해 명령어 목록을 확인해 주세요.",
            )

        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(LogFormatter.command(user_id, '/'.join(keywords) if keywords else "unknown", False, execution_time, f"예외: {str(e)}"), exc_info=True)
            # 사용자에게는 일반화된 메시지만 — `str(e)` 는 내부 정보가 노출될 수 있어 로그로만.
            return self._create_error_result(user_id, "명령어 처리 중 오류가 발생했습니다.")
    
    def _execute_registered(
        self,
        registered: Any,
        user_id: str,
        keywords: List[str],
        context: Dict[str, Any],
        first_keyword_lower: str,
        full_command: str,
        start_time: float,
    ) -> Optional[CommandResultProtocol]:
        """레지스트리에 등록된 명령어 인스턴스 생성 + 실행 + 로깅."""
        try:
            command_name = registered.metadata.name
        except AttributeError:
            command_name = first_keyword_lower

        command_instance = self.factory.create_command_by_name(command_name)
        if command_instance is None:
            # 의존성 미충족 등으로 인스턴스화 실패 — 다음 단계로 폴백.
            if should_log_debug():
                logger.debug(
                    f"[라우팅] 등록된 명령어 인스턴스 생성 실패: {command_name} "
                    f"(키워드={first_keyword_lower})"
                )
            return None

        if should_log_debug():
            logger.debug(
                f"명령어 매칭: {first_keyword_lower} → {command_instance.__class__.__name__} "
                f"(패키지={registered.metadata.command_package})"
            )

        execution_context = self._create_execution_context(user_id, keywords, context)
        response = self._execute_command(command_instance, execution_context)

        execution_time = time.time() - start_time
        command_result = self._convert_to_command_result(
            response, first_keyword_lower, user_id, keywords, execution_time,
        )
        log_command_result(
            user_id=user_id,
            command=full_command,
            success=command_result.is_successful(),
            duration=execution_time,
            result_length=len(command_result.get_user_message()) if command_result.get_user_message() else 0,
        )
        return command_result

    def _lookup_random_table(self, keyword: str) -> Optional[str]:
        """랜덤표 시트에서 매칭되는 워크시트의 무작위 값을 반환. 없으면 None."""
        if not self.sheets_manager:
            return None
        if not getattr(config, 'RANDOM_TABLE_SHEET_ID', ''):
            return None
        try:
            return self.sheets_manager.pick_random_table_value(keyword)
        except Exception as e:
            logger.warning(f"[랜덤표] 조회 실패 (키워드={keyword}): {e}")
            return None

    def _lookup_custom_command(self, keyword: str) -> Optional[str]:
        """커스텀 명령어 시트에서 매칭되는 문구를 반환. 없으면 None."""
        if not self.sheets_manager:
            return None
        if not getattr(config, 'CUSTOM_COMMAND_SHEET_ID', ''):
            return None
        try:
            return self.sheets_manager.pick_custom_command_value(keyword)
        except Exception as e:
            logger.warning(f"[커스텀] 조회 실패 (키워드={keyword}): {e}")
            return None

    def _build_aux_sheet_result(
        self,
        user_id: str,
        keywords: List[str],
        full_command: str,
        first_keyword_lower: str,
        message: str,
        start_time: float,
        source: str,
    ) -> CommandResultProtocol:
        """랜덤표 / 커스텀 시트 매칭 결과를 CommandResult 로 변환 + 로깅."""
        execution_time = time.time() - start_time

        if IMPORTS_AVAILABLE:
            try:
                command_type = get_command_type(first_keyword_lower) or CommandType.UNKNOWN
                command_result: CommandResultProtocol = CommandResult.success(
                    command_type=command_type,
                    user_id=user_id,
                    user_name=user_id,
                    original_command=f"[{'/'.join(keywords)}]",
                    message=message,
                    result_data={'source': source},
                    execution_time=execution_time,
                )
            except Exception as e:
                logger.debug(f"보조 시트 결과 변환 실패, 폴백 사용: {e}")
                command_result = FallbackCommandResult(
                    success=True,
                    message=message,
                    user_id=user_id,
                    execution_time=execution_time,
                )
        else:
            command_result = FallbackCommandResult(
                success=True,
                message=message,
                user_id=user_id,
                execution_time=execution_time,
            )

        log_command_result(
            user_id=user_id,
            command=full_command,
            success=True,
            duration=execution_time,
            result_length=len(message) if message else 0,
        )
        return command_result

    def _create_execution_context(
        self,
        user_id: str,
        keywords: List[str],
        legacy_context: Dict[str, Any] = None
    ) -> 'CommandContext':
        """실행 컨텍스트 생성"""
        # 기본 정보
        user_name = user_id  # 기본값
        original_text = ""
        metadata = {}
        
        # 레거시 컨텍스트에서 정보 추출
        if legacy_context:
            original_text = legacy_context.get('original_text', '')
            user_name = legacy_context.get('user_name', user_id)
            
            # 추가 메타데이터 복사
            for key, value in legacy_context.items():
                if key not in ['original_text', 'user_name', 'user_id']:
                    metadata[key] = value
        
        # 요청 ID 생성
        request_id = str(uuid.uuid4())[:8]
        
        # import 가능한 경우 실제 컨텍스트 생성
        if IMPORTS_AVAILABLE:
            return create_command_context(
                user_id=user_id,
                keywords=keywords,
                user_name=user_name,
                original_text=original_text,
                request_id=request_id,
                **metadata
            )
        else:
            # 더미 컨텍스트
            class DummyContext:
                def __init__(self):
                    self.user_id = user_id
                    self.keywords = keywords
                    self.user_name = user_name
                    self.original_text = original_text
                    self.request_id = request_id
                    self.metadata = metadata
            
            return DummyContext()
    
    def _execute_command(
        self,
        command_instance: Any,
        context: 'CommandContext'
    ) -> 'CommandResponse':
        """명령어 실행"""
        command_class_name = command_instance.__class__.__name__

        try:
            # 모든 명령어는 BaseCommand 를 상속한다. lifecycle 훅 경로 사용.
            if isinstance(command_instance, BaseCommand):
                logger.debug(f"[실행] BaseCommand 방식으로 실행: {command_class_name}")
                return command_instance.execute_with_lifecycle(context)

            # BaseCommand 가 아닌 경우 단순 execute 만 호출 (레지스트리 검증 통과 시 여기까지 올 일 거의 없음)
            if hasattr(command_instance, 'execute'):
                logger.debug(f"[실행] 직접 execute 호출: {command_class_name}")
                return command_instance.execute(context)

            logger.error(f"[실행] execute 메서드 없음: {command_class_name}")
            return CommandResponse.create_error("명령어 인스턴스에 execute 메서드가 없습니다.")

        except Exception as e:
            logger.error(f"[실행] 명령어 실행 중 예외 발생: {command_class_name} | 오류: {e}", exc_info=True)
            return CommandResponse.create_error("명령어 실행 중 오류가 발생했습니다.", error=e)
    
    def _convert_to_command_result(
        self,
        response: 'CommandResponse',
        command_keyword: str,
        user_id: str,
        keywords: List[str],
        execution_time: float
    ) -> CommandResultProtocol:
        """CommandResponse를 CommandResult로 변환 (타입 안정성 개선)"""
        if not IMPORTS_AVAILABLE:
            # import 실패 시 폴백 결과 반환
            return FallbackCommandResult(
                success=getattr(response, 'success', False),
                message=getattr(response, 'message', '알 수 없는 오류'),
                user_id=user_id,
                execution_time=execution_time
            )
        
        try:
            # CommandType 결정 (미등록 키워드는 UNKNOWN)
            command_type = get_command_type(command_keyword) or CommandType.UNKNOWN

            original_command = f"[{'/'.join(keywords)}]"

            if getattr(response, 'success', False):
                return CommandResult.success(
                    command_type=command_type,
                    user_id=user_id,
                    user_name=user_id,
                    original_command=original_command,
                    message=getattr(response, 'message', ''),
                    result_data=getattr(response, 'data', None),
                    execution_time=execution_time,
                )
            else:
                return CommandResult.error(
                    command_type=command_type,
                    user_id=user_id,
                    user_name=user_id,
                    original_command=original_command,
                    error=getattr(response, 'error', None) or Exception(getattr(response, 'message', '오류')),
                    execution_time=execution_time,
                )
            
        except Exception as e:
            # 변환 실패 시 컨텍스트(원본 명령/키워드/사용자) 와 스택을 함께 보존.
            # 사용자에게는 폴백 결과를 반환하되, 운영자는 로그에서 원인을 추적할 수 있어야 한다.
            logger.error(
                "CommandResult 변환 실패 | user=%s | command=%s | keyword=%s | response_success=%s | err=%s",
                user_id,
                f"[{'/'.join(keywords)}]" if keywords else "[?]",
                command_keyword,
                getattr(response, 'success', None),
                e,
                exc_info=True,
            )
            fallback_message = getattr(response, 'message', None) or '오류 사유 미상 (변환 실패)'
            return FallbackCommandResult(
                success=getattr(response, 'success', False),
                message=fallback_message,
                user_id=user_id,
                execution_time=execution_time
            )
    
    def _create_error_result(
        self,
        user_id: str,
        error_message: str
    ) -> CommandResultProtocol:
        """에러 결과 생성 (통합된 방식)"""
        if not IMPORTS_AVAILABLE:
            return ErrorResult(error_message, user_id)

        try:
            from models.dynamic_command_types import DynamicCommandType
            from models.command_result import CommandResult

            return CommandResult.error(
                command_type=DynamicCommandType.UNKNOWN,
                user_id=user_id,
                user_name=user_id,
                original_command="[UNKNOWN]",
                error=Exception(error_message)
            )
        except Exception:
            # 완전 폴백
            return ErrorResult(error_message, user_id)
    
    def get_available_commands(self) -> List[Dict[str, Any]]:
        """사용 가능한 명령어 목록 반환."""
        commands = []

        if IMPORTS_AVAILABLE and self.registry:
            try:
                for name, registered_command in self.registry.get_enabled_commands().items():
                    metadata = registered_command.metadata
                    commands.append({
                        'name': metadata.name,
                        'aliases': metadata.aliases,
                        'description': metadata.description,
                        'category': metadata.category,
                        'examples': metadata.examples,
                        'admin_only': metadata.admin_only,
                        'keywords': metadata.get_all_keywords(),
                    })
            except Exception as e:
                logger.error(f"명령어 목록 조회 실패: {e}", exc_info=True)

        commands.sort(key=lambda x: (x['category'], x['name']))
        return commands
    
    def reload_all_commands(self) -> Dict[str, Any]:
        """모든 명령어 재로드"""
        if not IMPORTS_AVAILABLE:
            logger.error("[재로드] 의존성 임포트 실패로 재로드 불가")
            return {
                'success': False,
                'error': '명령어 시스템이 초기화되지 않았습니다',
                'message': '의존성 임포트 실패'
            }

        try:
            logger.info("[재로드] 명령어 재로드 시작")

            # 팩토리 인스턴스 정리
            if self.factory:
                logger.debug("[재로드] 팩토리 인스턴스 정리 중")
                self.factory.cleanup_all_instances()

            # 레지스트리 재로드
            discovered_count = 0
            if self.registry:
                logger.debug("[재로드] 레지스트리 재로드 중")
                discovered_count = self.registry.reload_commands()
                logger.info(f"[재로드] 명령어 재발견 완료: {discovered_count}개")

            # 싱글톤 인스턴스 재생성
            success_count = 0
            total_count = 0
            if self.sheets_manager and self.factory:
                logger.debug("[재로드] 싱글톤 인스턴스 재생성 중")
                singleton_results = self.factory.create_all_singleton_instances()
                success_count = sum(singleton_results.values())
                total_count = len(singleton_results)
                logger.info(f"[재로드] 싱글톤 인스턴스 재생성: {success_count}/{total_count}")

            result = {
                'success': True,
                'discovered_commands': discovered_count,
                'singleton_instances': f"{success_count}/{total_count}",
                'message': f"명령어 재로드 완료: {discovered_count}개 발견"
            }

            logger.info(f"[재로드] 완료 - 발견={discovered_count}개, 싱글톤={success_count}/{total_count}")
            return result

        except Exception as e:
            logger.error(f"[재로드] 명령어 재로드 중 예외 발생: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e),
                'message': '명령어 재로드 실패'
            }
    
    def validate_all_systems(self) -> Dict[str, Any]:
        """모든 시스템 유효성 검증"""
        if not IMPORTS_AVAILABLE:
            return {
                'overall_valid': False,
                'errors': ['의존성 임포트 실패'],
                'warnings': [],
                'message': '시스템 검증 불가'
            }
        
        validation_result = {
            'overall_valid': True,
            'registry_validation': {},
            'factory_validation': {},
            'errors': [],
            'warnings': []
        }
        
        registry_result: Dict[str, Any] = {}
        factory_result: Dict[str, Any] = {}
        try:
            # 레지스트리 검증
            if self.registry:
                registry_result = self.registry.validate_all_commands()
                validation_result['registry_validation'] = registry_result
                if not registry_result.get('valid', True):
                    validation_result['overall_valid'] = False
                    validation_result['errors'].extend(registry_result.get('errors', []))

            # 팩토리 검증
            if self.factory:
                factory_result = self.factory.validate_dependencies()
                validation_result['factory_validation'] = factory_result
                if not factory_result.get('valid', True):
                    validation_result['overall_valid'] = False
                    validation_result['errors'].extend(factory_result.get('errors', []))

            # 레지스트리/팩토리 경고 수집 (둘 중 하나가 None 이어도 빈 dict 라 안전)
            for result in (registry_result, factory_result):
                validation_result['warnings'].extend(result.get('warnings', []) or [])

        except Exception as e:
            validation_result['overall_valid'] = False
            validation_result['errors'].append(f"검증 중 오류: {e}")
        
        return validation_result
    
    def health_check(self) -> Dict[str, Any]:
        """라우터 상태 확인"""
        health_status = {
            'status': 'healthy',
            'errors': [],
            'warnings': [],
            'components': {}
        }
        
        # 기본 임포트 상태 확인
        if not IMPORTS_AVAILABLE:
            health_status['status'] = 'error'
            health_status['errors'].append("의존성 임포트 실패")
            return health_status
        
        try:
            # 레지스트리 상태
            if self.registry:
                try:
                    registry_commands = len(self.registry.get_all_commands())
                    health_status['components']['registry'] = {
                        'status': 'healthy',
                        'commands_count': registry_commands
                    }
                except Exception as e:
                    health_status['errors'].append(f"레지스트리 오류: {e}")
                    health_status['components']['registry'] = {'status': 'error', 'error': str(e)}
            else:
                health_status['warnings'].append("레지스트리가 없음")
            
            # 팩토리 상태
            if self.factory:
                try:
                    factory_stats = self.factory.get_instance_statistics()
                    health_status['components']['factory'] = {
                        'status': 'healthy',
                        'instances': factory_stats.get('total_instances', 0)
                    }
                except Exception as e:
                    health_status['errors'].append(f"팩토리 오류: {e}")
                    health_status['components']['factory'] = {'status': 'error', 'error': str(e)}
            else:
                health_status['warnings'].append("팩토리가 없음")
            
            # 의존성 상태
            deps_status = 'healthy'
            if not self.sheets_manager:
                health_status['warnings'].append("Google Sheets 연결 없음")
                deps_status = 'warning'
            
            if not self.api:
                health_status['warnings'].append("Mastodon API 연결 없음")
                deps_status = 'warning'
            
            health_status['components']['dependencies'] = {'status': deps_status}
            
            # 전체 상태 결정
            if health_status['errors']:
                health_status['status'] = 'error'
            elif health_status['warnings']:
                health_status['status'] = 'warning'
        
        except Exception as e:
            health_status['status'] = 'error'
            health_status['errors'].append(f"상태 확인 중 오류: {e}")
        
        return health_status


# SimpleCommandRouter 제거 (deprecated)
# 기존 코드에서 SimpleCommandRouter를 사용하는 경우 ModernCommandRouter로 직접 교체 필요


# 전역 라우터 인스턴스 관리.
# 부팅 직후 스트림 핸들러와 헬스체크 등이 동시에 라우터를 요청할 수 있으므로
# 더블 체크 락 패턴으로 부분 초기화된 인스턴스가 노출되지 않도록 한다.
_global_router: Optional[ModernCommandRouter] = None
_router_lock = threading.Lock()


def get_command_router() -> ModernCommandRouter:
    """
    전역 명령어 라우터 반환

    주의: initialize_command_router()를 먼저 호출해야 합니다.
    호출하지 않은 경우 의존성 없이 라우터가 생성됩니다.
    """
    global _global_router
    if _global_router is None:
        with _router_lock:
            if _global_router is None:
                logger.warning("전역 라우터가 초기화되지 않았습니다. 의존성 없이 생성합니다.")
                logger.warning("먼저 initialize_command_router(sheets_manager, api)를 호출하세요!")
                _global_router = ModernCommandRouter()
    return _global_router


def initialize_command_router(
    sheets_manager: 'SheetsManager',
    api=None,
    **additional_deps,
) -> ModernCommandRouter:
    """
    명령어 라우터 초기화

    Args:
        sheets_manager: Google Sheets 관리자
        api: 마스토돈 API 인스턴스
        **additional_deps: 추가 의존성 (팩토리로 전달)

    Returns:
        ModernCommandRouter: 초기화된 라우터
    """
    global _global_router
    with _router_lock:
        _global_router = ModernCommandRouter(sheets_manager, api, **additional_deps)
        logger.info("전역 ModernCommandRouter 초기화 완료")
        return _global_router


def route_command(
    user_id: str, 
    keywords: List[str], 
    context: Dict[str, Any] = None
) -> CommandResultProtocol:
    """
    편의 함수: 명령어 라우팅 실행
    
    Args:
        user_id: 사용자 ID
        keywords: 키워드 리스트
        context: 실행 컨텍스트
        
    Returns:
        CommandResultProtocol: 실행 결과 (타입 안정성 보장)
    """
    router = get_command_router()
    return router.route_command(user_id, keywords, context)


def parse_command_from_text(text: str) -> List[str]:
    """
    텍스트에서 명령어 키워드 추출 (다이스 패턴 지원)
    
    Args:
        text: 분석할 텍스트 (예: "[다이스/2d6] 안녕하세요." 또는 "[2d6] 던지기")
        
    Returns:
        List[str]: 추출된 키워드들 (예: ['다이스', '2d6'] 또는 ['다이스', '2d6'])
    """
    # BBCode 스타일 포맷팅 태그 제거 ([color:hex], [/color], [bg:hex], [/bg])
    text = re.sub(r'\[/?(color|bg)(:[0-9a-fA-F]{3,8})?\]', '', text)

    # 모든 [] 패턴 찾기
    matches = re.findall(r'\[([^\]]+)\]', text)
    if not matches:
        return []
    
    # 첫 번째 매치만 사용
    keywords_str = matches[0]
    
    # 다이스 표현식 패턴 확인 (예: "1d6", "2d10+5", "3d6-2", "1d20>15")
    dice_pattern = re.compile(r'^\d+[dD]\d+([\+\-]\d+)?([<>]\d+)?$')

    # 단순한 다이스 표현식인 경우 (예: [1d6], [1d20+5])
    if dice_pattern.match(keywords_str.strip()):
        return ['다이스', keywords_str.strip()]

    # 일반적인 경우: / 기준으로 분할
    keywords = [keyword.strip() for keyword in keywords_str.split('/')]
    
    # 빈 키워드 제거
    keywords = [keyword for keyword in keywords if keyword]
    
    return keywords


def validate_command_format(text: str) -> Tuple[bool, str]:
    """
    명령어 형식 유효성 검사 (개선된 검증)
    
    Args:
        text: 검사할 텍스트
        
    Returns:
        Tuple[bool, str]: (유효성, 메시지)
    """
    # 기본 [] 패턴 확인
    if '[' not in text or ']' not in text:
        return False, "명령어는 [명령어] 형식으로 입력해야 합니다."
    
    # [] 위치 확인
    start_pos = text.find('[')
    end_pos = text.find(']')
    
    if start_pos >= end_pos:
        return False, "명령어 형식이 올바르지 않습니다. [명령어] 순서를 확인해주세요."
    
    # 중첩된 대괄호 확인
    bracket_content = text[start_pos:end_pos+1]
    if bracket_content.count('[') > 1 or bracket_content.count(']') > 1:
        return False, "중첩된 대괄호는 사용할 수 없습니다."
    
    # 키워드 추출 시도
    keywords = parse_command_from_text(text)
    if not keywords:
        return False, "명령어가 비어있습니다."
    
    # 키워드 길이 확인
    if any(len(keyword) > 50 for keyword in keywords):
        return False, "명령어가 너무 깁니다. (최대 50자)"
    
    return True, "올바른 명령어 형식입니다."


# 편의 함수들
def get_available_commands() -> List[Dict[str, Any]]:
    """사용 가능한 명령어 목록 반환 (편의 함수)"""
    router = get_command_router()
    return router.get_available_commands()


def reload_all_commands() -> Dict[str, Any]:
    """모든 명령어 재로드 (편의 함수)"""
    router = get_command_router()
    return router.reload_all_commands()


def validate_all_systems() -> Dict[str, Any]:
    """모든 시스템 검증 (편의 함수)"""
    router = get_command_router()
    return router.validate_all_systems()


def get_router_health() -> Dict[str, Any]:
    """라우터 상태 확인 (편의 함수)"""
    router = get_command_router()
    return router.health_check()


# 개발자를 위한 유틸리티
def show_router_info() -> None:
    """
    라우터 기본 정보 출력 (개발용)
    
    실제 테스트 코드는 tests/test_command_router.py로 분리하세요
    """
    try:
        router = get_command_router()
        
        print("=== ModernCommandRouter 정보 ===")
        print(f"임포트 상태: {'정상' if IMPORTS_AVAILABLE else '실패'}")
        
        # 상태 확인
        health = router.health_check()
        print(f"전체 상태: {health['status']}")
        
        if health['errors']:
            print("오류:")
            for error in health['errors'][:3]:  # 최대 3개만
                print(f"  - {error}")
        
        if health['warnings']:
            print("경고:")
            for warning in health['warnings'][:3]:  # 최대 3개만
                print(f"  - {warning}")
        
        # 명령어 목록
        commands = router.get_available_commands()
        if commands:
            print(f"\n등록된 명령어: {len(commands)}개")
            # 카테고리별 그룹화
            categories = {}
            for cmd in commands:
                category = cmd['category']
                if category not in categories:
                    categories[category] = []
                categories[category].append(cmd['name'])
            
            for category, cmd_names in categories.items():
                print(f"  {category}: {', '.join(cmd_names[:3])}" + 
                      (f" 외 {len(cmd_names)-3}개" if len(cmd_names) > 3 else ""))
        else:
            print("\n등록된 명령어: 없음")
        
        print("\n=== 정보 출력 완료 ===")
        
    except Exception as e:
        print(f"라우터 정보 출력 실패: {e}")


# 호환성 유지를 위한 알리아스
CommandRouter = ModernCommandRouter  # 기존 코드 호환성