"""
스트림 핸들러 - 개선된 버전
마스토돈 스트리밍 이벤트를 처리하고 명령어 라우터와 연동하는 모듈입니다.
모든 응답에 사용자 멘션(@사용자명)을 포함합니다.
"""

import os
import re
import sys
import threading
import time
from typing import Optional, Tuple, Any, List, Dict, Set
from utils.dm_sender import initialize_dm_sender
from utils.log_sanitizer import sanitize_log_input
from utils.mastodon_utils import HTMLCleaner, MentionManager
from utils.operation_period import (
    EXPIRATION_MESSAGE,
    get_current_operation_status,
)

# 경로 설정 (VM 환경 대응)
try:
    import mastodon
    from config.settings import config
    from utils.logging_config import logger, LogContext, should_log_debug
    from utils.sheets_operations import SheetsManager
    from handlers.command_router import ModernCommandRouter, parse_command_from_text
    from models.command_result import CommandResult
    from models.dynamic_command_types import CommandType
    from utils.api_retry import api_retry
    IMPORTS_AVAILABLE = True
except ImportError as e:
    # VM 환경에서 임포트 실패 시 폴백
    import logging
    logger = logging.getLogger('stream_handler')
    logger.warning(f"모듈 임포트 실패: {e}")
    
    # 마스토돈 더미 클래스
    class StreamListener:
        pass
    
    IMPORTS_AVAILABLE = False


# HTMLCleaner / MentionManager 는 utils/mastodon_utils.py 로 이동.
# 기존 호출자들이 모듈 상단 import 를 통해 참조하므로 동작은 동일.


class BotStreamHandler(mastodon.StreamListener):
    """
    마스토돈 스트리밍 이벤트를 처리하는 핸들러 - 개선된 버전
    
    개선된 기능:
    - ModernCommandRouter 사용
    - 통계 기능 제거 (불필요한 복잡성 제거)
    - HTML 처리 통합
    - 멘션 길이 초과 방지
    - 구조화된 에러 처리
    - 모든 응답에 사용자 멘션 포함
    - 과제 명령어를 위한 답글 컨텍스트 지원
    """
    
    def __init__(self, api: mastodon.Mastodon, sheets_manager: SheetsManager):
        """
        BotStreamHandler 초기화
        
        Args:
            api: 마스토돈 API 객체
            sheets_manager: Google Sheets 관리자
        """
        super().__init__()
        self.api = api
        self.sheets_manager = sheets_manager
        
        # 의존성 확인
        if not IMPORTS_AVAILABLE:
            logger.error("필수 의존성 임포트 실패 - 제한된 모드로 실행")
            self.command_router = None
            self.dm_sender = None
        else:
            # 전역 ModernCommandRouter 사용 (중복 생성 방지)
            from handlers.command_router import get_command_router
            self.command_router = get_command_router()
            # DM 전송기 초기화
            self.dm_sender = initialize_dm_sender(api)
        
        # 봇 계정 캐시
        self._bot_acct_cache: Optional[str] = None

        # 가동 기간 만료 후 안내를 받은 사용자 (사용자당 1회만 발송).
        # 프로세스 재시작 시 초기화 — 운영 중 재시작이 잦지 않다는 가정.
        self._expiration_notified_users: Set[str] = set()
        self._expiration_lock = threading.Lock()

        # 가동 기간 정보 — 콘솔에는 설정된 경우에만, 평소엔 파일 로그만.
        try:
            start = getattr(config, 'OPERATION_START_DATE', '') or ''
            end = getattr(config, 'OPERATION_END_DATE', '') or ''
            op_status = get_current_operation_status()
            if start or end:
                logger.info(
                    f"  ✓ 가동 기간 ({start or '무제한'} ~ {end or '무제한'}, 현재: {op_status})"
                )
            logger.debug(f"[초기화] 가동기간 시작={start or '없음'} 종료={end or '없음'} 현재상태={op_status}")
        except Exception as e:
            logger.warning(f"가동 기간 상태 확인 실패: {e}")

        logger.debug("BotStreamHandler 초기화 완료 (DM 전송기 포함, 멘션 응답)")
    
    def on_notification(self, notification) -> None:
        """
        알림 이벤트 처리
        
        Args:
            notification: 마스토돈 알림 객체
        """
        try:
            # 멘션만 처리
            if notification.type != 'mention':
                return
            
            with LogContext("멘션 처리", notification_id=notification.id):
                self._process_mention(notification)
                
        except Exception as e:
            logger.error(f"알림 처리 중 예상치 못한 오류: {e}", exc_info=True)
            
            # 사용자에게 오류 메시지 전송 시도
            try:
                self._send_error_response(notification, "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
            except Exception as send_error:
                logger.error(f"오류 응답 전송 실패: {send_error}", exc_info=True)
    
    def _process_mention(self, notification) -> None:
        """
        멘션 처리

        Args:
            notification: 마스토돈 알림 객체
        """
        # 기본 정보 추출
        status = notification.status
        user_id = status.account.acct
        visibility = getattr(status, 'visibility', 'public')
        content = status.content

        # HTML 태그 제거하여 텍스트 추출
        text_content = HTMLCleaner.extract_text(content)

        # 명령어 형식 검증
        if not self._has_command_format(text_content):
            if should_log_debug():
                logger.debug(f"명령어 형식 없음: {user_id}")
            return

        # 명령어 추출
        keywords = parse_command_from_text(text_content)
        if not keywords:
            if should_log_debug():
                logger.debug(f"명령어 추출 실패: {user_id}")
            return

        # 가동 기간 체크 (KST 기준)
        # - 'pre'     : 시작 전 — 침묵
        # - 'expired' : 만료 — 사용자당 1회 안내 후 침묵
        # - 'active'  : 정상 처리
        op_status = get_current_operation_status()
        if op_status == 'pre':
            if should_log_debug():
                logger.debug(f"[가동기간] 시작 전 — 무시: {user_id}")
            return
        if op_status == 'expired':
            self._send_expiration_notice(notification, user_id)
            return

        # Visibility 정책:
        # - direct          : direct 로 응답 (DM 그대로)
        # - unlisted        : unlisted 로 응답 (로컬/공개 타임라인엔 안 뜨지만 프로필·URL 열람 가능)
        # - public/private/그 외 : private (팔로워 전용) 으로 응답
        # 공개(public) 응답은 만들지 않아 공개·로컬 타임라인 오염을 막는다.
        if visibility == 'direct':
            response_visibility = 'direct'
        elif visibility == 'unlisted':
            response_visibility = 'unlisted'
        else:
            response_visibility = 'private'
        if should_log_debug() and visibility != response_visibility:
            logger.debug(
                f"응답 가시성 다운그레이드: {visibility} → {response_visibility} | user={user_id}"
            )

        # 대화 참여자 추출 (봇 제외)
        mentioned_users = self._extract_mentioned_users(status)

        # 답글 컨텍스트 생성 (과제 명령어용)
        context = self._create_command_context(status, notification)

        # 명령어 실행 (컨텍스트 포함)
        command_result = self._execute_command(user_id, keywords, context)

        # 응답 전송 (모든 참여자 멘션 포함)
        self._send_response(notification, command_result, response_visibility, mentioned_users)
    
    def _create_command_context(self, status, notification) -> Dict[str, Any]:
        """
        명령어 실행을 위한 컨텍스트 생성
        
        Args:
            status: 마스토돈 status 객체
            notification: 마스토돈 notification 객체
            
        Returns:
            Dict[str, Any]: 명령어 컨텍스트
        """
        # 원본 텍스트 추출
        content = status.content
        original_text = HTMLCleaner.extract_text(content)
        
        context = {
            'status_id': status.id,
            'user_id': status.account.acct,
            'user_name': getattr(status.account, 'display_name', status.account.acct),
            'visibility': getattr(status, 'visibility', 'public'),
            'notification': notification,
            'original_status': status,
            'original_text': original_text
        }
        
        # 답글인 경우 원본 툿 ID 추가
        if hasattr(status, 'in_reply_to_id') and status.in_reply_to_id:
            context['reply_to_id'] = status.in_reply_to_id
            context['is_reply'] = True
            if should_log_debug():
                logger.debug(f"답글 컨텍스트 생성: {status.id} -> {status.in_reply_to_id}")
        else:
            context['is_reply'] = False
        
        return context
    
    def _extract_mentioned_users(self, status) -> List[str]:
        """
        툿에서 멘션된 사용자들 추출 (봇 제외, 개선된 버전)
        
        Args:
            status: 마스토돈 status 객체
            
        Returns:
            List[str]: 멘션된 사용자 ID 목록 (봇 제외)
        """
        mentioned_users = []
        
        try:
            # 1. mentions 속성에서 추출 (가장 정확함)
            if hasattr(status, 'mentions') and status.mentions:
                for mention in status.mentions:
                    user_acct = mention.get('acct', '')
                    if user_acct and not self._is_bot_account(user_acct):
                        mentioned_users.append(user_acct)
            
            # 2. mentions가 없는 경우 HTML에서 파싱 (통합된 방식 사용)
            else:
                html_mentions = HTMLCleaner.extract_mentions(status.content)
                for user_id in html_mentions:
                    if user_id and not self._is_bot_account(user_id):
                        mentioned_users.append(user_id)
            
            # 3. 원작성자도 포함 (자신이 아닌 경우)
            author_acct = status.account.acct
            if author_acct and not self._is_bot_account(author_acct) and author_acct not in mentioned_users:
                mentioned_users.append(author_acct)
            
            # 중복 제거 및 정렬
            mentioned_users = list(set(mentioned_users))
            mentioned_users.sort()

            if should_log_debug():
                logger.debug(f"추출된 멘션 사용자: {mentioned_users}")
            
        except Exception as e:
            logger.warning(f"멘션 사용자 추출 실패: {e}")
            # 실패 시 최소한 원작성자는 포함
            author_acct = status.account.acct
            if author_acct and not self._is_bot_account(author_acct):
                mentioned_users = [author_acct]
        
        return mentioned_users
    
    def _is_bot_account(self, user_acct: str) -> bool:
        """
        봇 계정 여부 확인 (캐싱 적용).

        스트리밍 핸들러는 동기 컨텍스트라 멘션마다 호출되므로 재시도/대기를 하지 않는다.
        API 실패 시엔 안전하게 False (= 봇이 아님) 반환 — 잘못된 false-positive 보다
        스트림이 멈추지 않는 게 우선.
        """
        if self._bot_acct_cache is not None:
            return user_acct == self._bot_acct_cache

        try:
            bot_info = self.api.me()
            acct = bot_info.get('acct', bot_info.get('username', ''))
        except Exception as e:
            logger.debug(f"봇 계정 조회 실패 (안전하게 False 반환): {e}")
            return False

        # 빈 문자열은 캐시하지 않음 — 다음 호출에서 다시 시도
        if acct:
            self._bot_acct_cache = acct
        return user_acct == acct
    
    def _has_command_format(self, text: str) -> bool:
        """
        텍스트에 명령어 형식이 있는지 확인
        
        Args:
            text: 확인할 텍스트
            
        Returns:
            bool: 명령어 형식 포함 여부
        """
        if not text:
            return False

        # BBCode 스타일 포맷팅 태그 제거 ([color:hex], [/color], [bg:hex], [/bg])
        cleaned = re.sub(r'\[/?(color|bg)(:[0-9a-fA-F]{3,8})?\]', '', text)

        # [] 패턴 확인
        if '[' not in cleaned or ']' not in cleaned:
            return False

        # [] 위치 확인
        start_pos = cleaned.find('[')
        end_pos = cleaned.find(']')

        return start_pos < end_pos
    
    def _execute_command(self, user_id: str, keywords: list, context: Dict[str, Any] = None) -> 'CommandResult':
        """
        명령어 실행 (컨텍스트 지원)
        
        Args:
            user_id: 사용자 ID
            keywords: 키워드 리스트
            context: 명령어 실행 컨텍스트
            
        Returns:
            CommandResult: 실행 결과
        """
        start_time = time.time()
        
        try:
            # 의존성 확인
            if not self.command_router:
                return self._create_fallback_error_result(
                    user_id, keywords, "명령어 시스템이 초기화되지 않았습니다. 관리자에게 문의해주세요."
                )
            
            # 명령어 라우터를 통한 실행 (컨텍스트 포함)
            result = self.command_router.route_command(user_id, keywords, context)
            
            execution_time = time.time() - start_time

            # 과제 명령어인 경우 추가 로깅
            if keywords and keywords[0].replace(' ', '') == '과제참여':
                reply_info = ""
                if context and context.get('is_reply'):
                    reply_info = f" (답글: {context.get('reply_to_id')})"
                logger.debug(f"과제 명령어 → @{user_id} | {keywords}{reply_info}")

            if execution_time > 5.0:
                safe_kw = [sanitize_log_input(k) for k in keywords] if keywords else keywords
                logger.warning(f"느린 명령어 {safe_kw} ({execution_time:.1f}초)")
            
            return result
            
        except Exception as e:
            execution_time = time.time() - start_time
            logger.error(f"명령어 실행 중 오류: {keywords} - {e}", exc_info=True)
            
            # 오류 결과 생성 — 사용자에게는 일반화된 메시지만 노출.
            # str(e) 는 내부 정보(파일 경로, 클래스명 등)를 포함할 수 있어 사용자에게 직접 노출 금지.
            return self._create_fallback_error_result(user_id, keywords, execution_time)

    def _create_fallback_error_result(self, user_id: str, keywords: list, execution_time: float = 0.0):
        """폴백 에러 결과 생성 — 항상 동일한 친화적 메시지 사용."""
        user_message = "명령어 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."

        if IMPORTS_AVAILABLE:
            try:
                return CommandResult.error(
                    command_type=CommandType.UNKNOWN,
                    user_id=user_id,
                    user_name=user_id,
                    original_command=f"[{'/'.join(keywords)}]",
                    error=Exception(user_message),
                    execution_time=execution_time
                )
            except Exception as e:
                logger.warning("CommandResult.error 생성 실패: %s", e)

        # 완전 폴백
        class FallbackErrorResult:
            def __init__(self, message: str):
                self.message = message

            def is_successful(self):
                return False

            def get_user_message(self):
                return self.message

        return FallbackErrorResult(user_message)
    
    def _send_response(self, notification, command_result, visibility: str, mentioned_users: List[str]) -> None:
        """
        명령어 결과에 따른 응답 전송 (모든 참여자 멘션 포함, 길이 초과 방지, 이미지 첨부 지원)

        Args:
            notification: 마스토돈 알림 객체
            command_result: 명령어 실행 결과
            visibility: 공개 범위
            mentioned_users: 멘션할 사용자 목록
        """
        try:
            original_status_id = notification.status.id

            # command_result의 metadata에서 visibility 확인 (우선순위)
            # 먼저 metadata에서 확인
            if hasattr(command_result, 'metadata') and command_result.metadata.get('visibility'):
                visibility = command_result.metadata.get('visibility')
            # 그 다음 직접 속성 확인 (하위 호환성)
            elif hasattr(command_result, 'visibility') and command_result.visibility:
                visibility = command_result.visibility

            # 모든 참여자 멘션 생성 (길이 초과 방지)
            mentions = MentionManager.format_mentions(mentioned_users)

            # 실패한 경우 단순 오류 메시지 전송
            if not command_result.is_successful():
                formatted_message = config.format_response(command_result.get_user_message())

                # 빈 메시지는 전송하지 않음 (무시된 명령어)
                if not formatted_message or not formatted_message.strip():
                    logger.debug("빈 응답 - 전송 생략 (무시된 명령어)")
                    return

                full_message = f"{mentions} {formatted_message}"
                self._send_status_with_retry(
                    status=full_message,
                    in_reply_to_id=original_status_id,
                    visibility=visibility
                )
                command_name = command_result.original_command if hasattr(command_result, 'original_command') else "?"
                user_info = f"@{mentioned_users[0]}" if mentioned_users else "@unknown"
                error_msg = formatted_message[:50] if formatted_message else "알 수 없는 오류"
                logger.warning(f"{user_info} {command_name} → 오류: {error_msg}")
                return

            # 성공한 경우 메시지 길이에 따라 처리
            formatted_message = config.format_response(command_result.get_user_message())

            # 빈 메시지면 송신 생략 (예: [점호] 시간 외 호출 — 의도적 침묵)
            if not formatted_message or not formatted_message.strip():
                logger.debug("빈 응답 - 전송 생략 (의도적 무응답)")
                return

            full_message = f"{mentions} {formatted_message}"
            message_length = len(full_message)

            # 이미지 첨부를 사용하지 않음
            media_ids = []

            # 단일 툿 한도 = 서버 글자수 × MESSAGE_SAFE_RATIO (기본 1000 × 0.9 = 900).
            single_threshold = (
                config.safe_message_length() if config and hasattr(config, 'safe_message_length')
                else 900
            )
            command_name = command_result.original_command if hasattr(command_result, 'original_command') else "?"
            user_info = f"@{mentioned_users[0]}" if mentioned_users else "@unknown"

            if message_length <= single_threshold:
                self._send_status_with_retry(
                    status=full_message,
                    in_reply_to_id=original_status_id,
                    visibility=visibility,
                    media_ids=media_ids if media_ids else None,
                )
                logger.info(f"{user_info} {command_name} → {message_length}자")

            else:
                logger.info(f"{user_info} {command_name} → {message_length}자 (스레드)")

                # 메시지 분할 및 전송 (첫 번째 메시지에만 이미지 첨부)
                sent_statuses = self._send_threaded_response(
                    original_status_id,
                    command_result,
                    visibility,
                    mentions,
                    media_ids=media_ids if media_ids else None
                )


        except Exception as e:
            logger.error(f"응답 전송 실패: {mentioned_users} - {e}", exc_info=True)

            try:
                mentions = MentionManager.format_mentions(mentioned_users)
                formatted_error = config.format_response("응답 처리 중 오류가 발생했습니다.")
                self.api.status_post(
                    in_reply_to_id=notification.status.id,
                    status=f"{mentions} {formatted_error}",
                    visibility=visibility
                )
            except Exception as fallback_error:
                logger.error(f"오류 메시지 전송도 실패: {fallback_error}", exc_info=True)
    
    def _send_threaded_response(self, original_status_id: str, command_result, visibility: str, mentions: str, media_ids: List = None) -> List[Dict]:
        """
        스레드 형태로 긴 응답 전송 (모든 툿에 멘션 포함, 첫 번째 메시지에만 이미지 첨부)

        Args:
            original_status_id: 원본 툿 ID
            command_result: 명령어 결과
            visibility: 공개 범위
            mentions: 멘션 문자열 (@user1 @user2 ...)
            media_ids: 미디어 ID 리스트 (첫 번째 메시지에만 첨부)

        Returns:
            List[Dict]: 전송된 툿들의 정보
        """
        try:
            # 메시지 분할기 import
            from utils.message_chunking import MessageChunker

            # mentions + prefix 길이를 고려하여 chunker 설정.
            # 청크 본문 한도 = 안전 임계(MAX × 0.9) − 멘션 − 프리픽스 − 10자 여유.
            mentions_length = len(mentions) + 1  # 공백 포함
            prefix_length = len(config.RESPONSE_PREFIX) if config and config.RESPONSE_PREFIX else 0
            safe_total = (
                config.safe_message_length() if config and hasattr(config, 'safe_message_length')
                else 900
            )
            safe_length = safe_total - mentions_length - prefix_length - 10
            chunker = MessageChunker(max_length=max(50, safe_length))  # 최소 50자 보장
            chunks = []

            # 특수 결과 타입을 쓰지 않음 — 메시지 단일 분할만
            chunks = chunker.split_message(command_result.get_user_message())

            # 청크들을 순차적으로 전송
            sent_statuses = []
            reply_to_id = original_status_id

            for i, chunk in enumerate(chunks):
                try:
                    logger.debug(f"청크 {i+1}/{len(chunks)} 전송 중... ({len(chunk)}자)")

                    formatted_chunk = config.format_response(chunk)
                    # 모든 청크에 멘션 포함 (두 번째부터도 멘션이 와야 함)
                    if mentions.strip():
                        full_chunk = f"{mentions} {formatted_chunk}"
                    else:
                        full_chunk = formatted_chunk

                    # 디버그: 실제 전송될 메시지 길이 확인
                    server_limit = config.MAX_MESSAGE_LENGTH if config else 1000
                    logger.debug(f"실제 전송 메시지 길이: {len(full_chunk)}자 - '{full_chunk[:100]}...'")
                    if len(full_chunk) > server_limit:
                        logger.warning(f"{server_limit}자 초과 메시지 감지! {len(full_chunk)}자: '{full_chunk[:50]}...'")

                    # 첫 번째 청크에만 이미지 첨부
                    chunk_media_ids = media_ids if (i == 0 and media_ids) else None

                    status = self._send_status_with_retry(
                        status=full_chunk,
                        in_reply_to_id=reply_to_id,
                        visibility=visibility,
                        media_ids=chunk_media_ids
                    )

                    sent_statuses.append(status)
                    reply_to_id = status['id']  # 다음 답장은 방금 보낸 툿에 연결

                    # API 제한 고려하여 대기 (마지막 제외)
                    if i < len(chunks) - 1:
                        time.sleep(0.5)

                except Exception as e:
                    logger.error(f"청크 {i+1} 전송 실패: {e}", exc_info=True)
                    break

            return sent_statuses

        except Exception as e:
            logger.error(f"스레드 응답 전송 실패: {e}", exc_info=True)
            return []
    
    def process_pending_dms(self) -> Dict[str, int]:
        """
        대기 중인 DM들을 처리
        
        Returns:
            Dict: 처리 결과
        """
        try:
            if self.dm_sender:
                return self.dm_sender.process_pending_dms()
            return {'processed': 0, 'success': 0, 'failed': 0, 'retries': 0}
        except Exception as e:
            logger.error(f"DM 처리 실패: {e}", exc_info=True)
            return {'processed': 0, 'success': 0, 'failed': 0, 'retries': 0}
    
    def _send_error_response(self, notification, error_message: str) -> None:
        """
        오류 응답 전송 (모든 참여자 멘션 포함)
        
        Args:
            notification: 원본 알림
            error_message: 오류 메시지
        """
        try:
            status = notification.status
            visibility = getattr(status, 'visibility', 'public')
            
            # 모든 참여자 추출
            mentioned_users = self._extract_mentioned_users(status)
            mentions = MentionManager.format_mentions(mentioned_users)
            
            formatted_message = config.format_response(error_message)
            self._send_status_with_retry(
                status=f"{mentions} {formatted_message}",
                in_reply_to_id=status.id,
                visibility=visibility
            )
            
        except Exception as e:
            logger.error(f"오류 응답 전송 실패: {e}", exc_info=True)

    def _send_expiration_notice(self, notification, user_id: str) -> None:
        """
        가동 기간 만료 안내 전송 (사용자당 1회). 이후 연락은 침묵.

        스팸 방지를 위해 항상 `direct` 가시성으로 보낸다. 1회 전송 후 사용자 ID 를
        기록해 동일 사용자의 추가 멘션은 무시.

        Args:
            notification: 마스토돈 알림 객체
            user_id: 발신자 acct
        """
        # 동시 멘션이 있어도 사용자별 1회 보장 — lock 안에서 add 후 판정.
        with self._expiration_lock:
            if user_id in self._expiration_notified_users:
                if should_log_debug():
                    logger.debug(f"[가동기간] 만료 안내 이미 전송됨 — 무시: {user_id}")
                return
            self._expiration_notified_users.add(user_id)

        try:
            status = notification.status
            mentioned_users = self._extract_mentioned_users(status)
            mentions = MentionManager.format_mentions(mentioned_users)
            formatted_message = config.format_response(EXPIRATION_MESSAGE)

            self._send_status_with_retry(
                status=f"{mentions} {formatted_message}",
                in_reply_to_id=status.id,
                visibility='direct',
            )
            logger.info(f"@{user_id} → 가동기간 만료 안내 전송")
        except Exception as e:
            # 전송 실패 시 다음 시도 가능하도록 기록 해제.
            with self._expiration_lock:
                self._expiration_notified_users.discard(user_id)
            logger.error(f"@{user_id} 가동기간 만료 안내 전송 실패")
            logger.debug(f"  사유: {e}", exc_info=True)

    def health_check(self) -> dict:
        """
        핸들러 상태 확인
        
        Returns:
            dict: 상태 정보
        """
        health_status = {
            'status': 'healthy',
            'errors': [],
            'warnings': []
        }
        
        try:
            # 기본 의존성 확인
            if not IMPORTS_AVAILABLE:
                health_status['errors'].append("필수 의존성 임포트 실패")
                health_status['status'] = 'error'
                return health_status
            
            # API 연결 상태 확인
            if not self.api:
                health_status['errors'].append("마스토돈 API 객체 없음")
                health_status['status'] = 'error'
            
            # Sheets 관리자 상태 확인
            if not self.sheets_manager:
                health_status['errors'].append("Sheets 관리자 없음")
                health_status['status'] = 'error'
            
            # 명령어 라우터 상태 확인
            if not self.command_router:
                health_status['errors'].append("명령어 라우터 없음")
                health_status['status'] = 'error'
            else:
                # 라우터 검증
                try:
                    validation = self.command_router.validate_all_systems()
                    if not validation.get('overall_valid', True):
                        health_status['warnings'].append("일부 명령어에 문제가 있습니다.")
                        if health_status['status'] == 'healthy':
                            health_status['status'] = 'warning'
                except Exception as e:
                    health_status['warnings'].append(f"명령어 검증 실패: {str(e)}")
            
            # DM 전송기 상태 확인
            if not self.dm_sender:
                health_status['warnings'].append("DM 전송기 없음")
                if health_status['status'] == 'healthy':
                    health_status['status'] = 'warning'
            else:
                # DM 전송기 상세 상태 확인
                try:
                    dm_health = self.dm_sender.health_check()
                    if dm_health['status'] != 'healthy':
                        health_status['warnings'].extend(dm_health.get('warnings', []))
                        health_status['errors'].extend(dm_health.get('errors', []))
                        
                        if dm_health['status'] == 'error':
                            health_status['status'] = 'error'
                        elif dm_health['status'] == 'warning' and health_status['status'] == 'healthy':
                            health_status['status'] = 'warning'
                except Exception as e:
                    health_status['warnings'].append(f"DM 전송기 상태 확인 실패: {str(e)}")
            
            # DM 관련 경고 확인
            if self.dm_sender:
                try:
                    pending_dms = self.dm_sender.get_pending_count()
                    if pending_dms > 10:  # 대기 중인 DM이 10개 이상
                        health_status['warnings'].append(f"대기 중인 DM이 많습니다: {pending_dms}개")
                        if health_status['status'] == 'healthy':
                            health_status['status'] = 'warning'
                    
                    # DM 실패율 확인
                    dm_stats = self.dm_sender.get_stats()
                    if dm_stats.get('total_sent', 0) > 5:  # 최소 5개 이상 전송한 경우
                        dm_failure_rate = (dm_stats.get('failed_sent', 0) / dm_stats.get('total_sent', 1)) * 100
                        if dm_failure_rate > 30:  # 30% 이상 실패율
                            health_status['warnings'].append(f"DM 높은 실패율: {dm_failure_rate:.1f}%")
                            if health_status['status'] == 'healthy':
                                health_status['status'] = 'warning'
                except Exception as e:
                    health_status['warnings'].append(f"DM 상태 확인 실패: {str(e)}")
            
        except Exception as e:
            health_status['errors'].append(f"상태 확인 중 오류: {str(e)}")
            health_status['status'] = 'error'
        
        return health_status
    
    @api_retry(max_retries=3, delay_seconds=10)
    def _send_status_with_retry(self, status: str, in_reply_to_id: str = None, visibility: str = 'public', media_ids: List = None):
        """
        재시도 로직이 적용된 status_post 메서드

        Args:
            status: 게시할 내용
            in_reply_to_id: 답글 대상 ID
            visibility: 공개 범위
            media_ids: 미디어 ID 리스트 (선택사항)

        Returns:
            마스토돈 status 객체
        """
        return self.api.status_post(
            status=status,
            in_reply_to_id=in_reply_to_id,
            visibility=visibility,
            media_ids=media_ids
        )


class StreamManager:
    """
    스트림 매니저 - 스트리밍 연결 관리 (통계 기능 제거, DM 처리 포함)
    """
    
    def __init__(self, api: mastodon.Mastodon, sheets_manager: SheetsManager):
        """
        StreamManager 초기화
        
        Args:
            api: 마스토돈 API 객체
            sheets_manager: Google Sheets 관리자
        """
        self.api = api
        self.sheets_manager = sheets_manager
        self.handler = None
        self.is_running = False
        self.dm_process_interval = 30  # 30초마다 DM 처리
        self.last_dm_process = 0
        
        logger.debug("StreamManager 초기화 완료")
    
    def start_streaming(self, max_retries: int = None, use_polling_fallback: bool = True) -> bool:
        """
        스트리밍 시작 (DM 처리 포함)

        Args:
            max_retries: 최대 재시도 횟수
            use_polling_fallback: 모든 재시도 실패 후 HTTP 폴링으로 전환할지

        Returns:
            bool: 시작 성공 여부
        """
        if not IMPORTS_AVAILABLE:
            logger.error("필수 의존성이 없어 스트리밍을 시작할 수 없습니다.")
            return False

        max_retries = max_retries or getattr(config, 'MAX_RETRIES', 10)
        self.handler = BotStreamHandler(self.api, self.sheets_manager)

        if self._run_with_retry_loop(max_retries):
            return True

        if use_polling_fallback:
            logger.warning("스트리밍 연결 실패 — HTTP 폴링 모드로 전환합니다.")
            return self._start_polling_fallback()
        return False

    def _run_with_retry_loop(self, max_retries: int) -> bool:
        """`max_retries` 까지 스트리밍 시작을 재시도. 정상 종료 시 True."""
        attempt = 0
        while attempt < max_retries:
            try:
                logger.debug(f"마스토돈 스트리밍 시작 시도 {attempt + 1}/{max_retries}")
                self.is_running = True
                self._start_streaming_with_dm_processing()
                self.is_running = False
                logger.info("스트리밍이 정상 종료되었습니다.")
                return True
            except Exception as e:
                attempt += 1
                self.is_running = False
                self._handle_stream_failure(e, attempt, max_retries)
        return False

    def _handle_stream_failure(self, exc: Exception, attempt: int, max_retries: int) -> None:
        """스트림 시작 실패 1회분 처리: 분류·로깅·대기."""
        # 상세 정보는 파일 로그에만 — 운영자가 logs/bot.log 에서 확인 가능.
        error_details = {
            'error_type': type(exc).__name__,
            'error_message': str(exc),
            'attempt': attempt,
            'max_retries': max_retries,
        }
        if hasattr(exc, 'response'):
            error_details['http_status'] = getattr(exc.response, 'status_code', 'N/A')
            error_details['http_content'] = str(getattr(exc.response, 'content', 'N/A'))[:200]
        logger.debug(f"스트리밍 연결 실패 상세: {error_details}", exc_info=True)

        if attempt >= max_retries:
            logger.error("스트리밍 재시도 한계를 초과했습니다.")
            return

        wait_time = self._wait_seconds_for(exc, attempt)
        if self._is_transient_network_error(exc):
            logger.warning(
                f"서버/네트워크 오류 감지 — {wait_time}초 후 재시도 ({attempt}/{max_retries})"
            )
        else:
            logger.warning(f"연결 오류 — {wait_time}초 후 재시도 ({attempt}/{max_retries})")
        time.sleep(wait_time)

    @staticmethod
    def _is_transient_network_error(exc: Exception) -> bool:
        """502/503/Bad Gateway/MastodonNetworkError 류 — 잠시 후 재시도하면 회복 가능."""
        msg = str(exc)
        return (
            '503' in msg
            or '502' in msg
            or 'Bad Gateway' in msg
            or 'MastodonNetworkError' in str(type(exc))
        )

    def _wait_seconds_for(self, exc: Exception, attempt: int) -> int:
        """재시도 대기 시간(초). transient 오류는 점증, 그 외는 기본값."""
        base = getattr(config, 'BASE_WAIT_TIME', 5)
        if self._is_transient_network_error(exc):
            return min(base * (attempt + 1), 30)
        return base
    
    def _start_streaming_with_dm_processing(self):
        """DM 처리가 포함된 스트리밍 시작"""

        # DM 처리를 위한 별도 스레드 시작
        dm_thread = threading.Thread(target=self._dm_processing_loop, daemon=True)
        dm_thread.start()
        logger.debug("DM 처리 스레드 시작")

        try:
            # 메인 스트리밍 시작 (연결 파라미터 최적화)
            logger.debug("스트리밍 연결 파라미터 설정 중...")
            self.api.stream_user(
                listener=self.handler,
                timeout=60,  # 타임아웃 설정 (초)
                reconnect_async=True,  # 자동 재연결 활성화
                reconnect_async_wait_sec=10,  # 재연결 대기 시간
                run_async=False  # 동기 실행
            )
        finally:
            # 스트리밍 종료 시 DM 처리도 정리
            self.is_running = False
            logger.debug("DM 처리 스레드 종료 요청")
    
    def _dm_processing_loop(self):
        """DM 처리 루프 (별도 스레드에서 실행)"""
        while self.is_running:
            try:
                current_time = time.time()
                
                # 일정 간격마다 DM 처리
                if current_time - self.last_dm_process >= self.dm_process_interval:
                    if self.handler:
                        results = self.handler.process_pending_dms()
                        if results['processed'] > 0:
                            logger.info(f"DM {results['processed']}건 처리됨")
                    
                    self.last_dm_process = current_time
                
                # 1초 대기
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"DM 처리 루프 오류: {e}", exc_info=True)
                time.sleep(5)  # 오류 시 잠시 대기
    
    def _start_polling_fallback(self) -> bool:
        """
        HTTP 폴링 방식 백업 시스템
        스트리밍이 실패할 때 대안으로 사용
        """
        logger.info("HTTP 폴링 모드로 전환합니다.")

        try:
            # 폴링을 위한 변수들
            self.is_running = True
            self.last_notification_id = None
            self.polling_interval = getattr(config, 'POLLING_INTERVAL', 30)  # 30초마다 확인

            # DM 처리 스레드 시작
            dm_thread = threading.Thread(target=self._dm_processing_loop, daemon=True)
            dm_thread.start()
            logger.debug("DM 처리 스레드 시작 (폴링 모드)")

            # 폴링 루프 시작
            self._polling_loop()

            return True

        except Exception as e:
            logger.error("폴링 모드 시작에 실패했습니다.")
            logger.debug(f"  사유: {e}", exc_info=True)
            self.is_running = False
            return False

    def _polling_loop(self):
        """폴링 기반 알림 확인 루프"""
        logger.debug(f"폴링 루프 시작 (간격={self.polling_interval}초)")

        while self.is_running:
            try:
                # 새로운 알림 확인
                self._check_new_notifications()

                # 대기
                for _ in range(self.polling_interval):
                    if not self.is_running:
                        break
                    time.sleep(1)

            except KeyboardInterrupt:
                logger.info("사용자 중단 요청 — 폴링 종료.")
                break
            except Exception as e:
                logger.error("폴링 루프에서 오류가 발생했습니다.")
                logger.debug(f"  사유: {e}", exc_info=True)
                time.sleep(10)  # 오류 시 잠시 대기

        logger.debug("폴링 루프 종료")
    
    def _check_new_notifications(self):
        """새로운 알림 확인 및 처리"""
        try:
            # 최신 알림 가져오기 (API 호출)
            notifications = self.api.notifications(
                limit=20,  # 최대 20개
                since_id=self.last_notification_id
            )
            
            if not notifications:
                logger.debug("새로운 알림 없음")
                return
            
            logger.info(f"📬 새로운 알림 {len(notifications)}개 발견")
            
            # 가장 최신 알림 ID 업데이트
            if notifications:
                self.last_notification_id = notifications[0].id
            
            # 각 알림 처리 (최신순이므로 역순으로)
            for notification in reversed(notifications):
                try:
                    # 멘션만 처리
                    if notification.type == 'mention':
                        logger.debug(f"멘션 알림 처리: @{notification.account.acct}")
                        self.handler.on_notification(notification)
                    else:
                        logger.debug(f"스킵된 알림 타입: {notification.type}")
                        
                except Exception as e:
                    logger.error(f"알림 처리 오류 (ID: {notification.id}): {e}", exc_info=True)
                    
        except Exception as e:
            logger.error(f"알림 확인 실패: {e}", exc_info=True)
            # API 오류 시 간격을 늘림
            time.sleep(5)
    
    def get_dm_stats(self) -> dict:
        """DM 전송 통계만 반환"""
        if self.handler and self.handler.dm_sender:
            return self.handler.dm_sender.get_stats()
        return {}
    
    def process_pending_dms_manually(self) -> dict:
        """수동으로 대기 중인 DM 처리"""
        if self.handler:
            return self.handler.process_pending_dms()
        return {'processed': 0, 'success': 0, 'failed': 0, 'retries': 0}
    
    def get_status(self) -> dict:
        """매니저 상태 반환 (통계 제거, DM 상태 포함)"""
        status = {
            'is_running': self.is_running,
            'handler_initialized': self.handler is not None,
            'api_connected': self.api is not None,
            'sheets_connected': self.sheets_manager is not None,
            'imports_available': IMPORTS_AVAILABLE,
            'dm_sender_initialized': False,
            'pending_dms': 0
        }
        
        if self.handler and self.handler.dm_sender:
            status['dm_sender_initialized'] = True
            try:
                status['pending_dms'] = self.handler.dm_sender.get_pending_count()
            except Exception as e:
                logger.debug("pending_dms 조회 실패: %s", e)
                status['pending_dms'] = 0
        
        return status
    
    def get_health_status(self) -> dict:
        """핸들러 상태 확인"""
        if self.handler:
            return self.handler.health_check()
        
        return {
            'status': 'error',
            'errors': ['핸들러가 초기화되지 않았습니다'],
            'warnings': []
        }
    
    def stop_streaming(self) -> None:
        """스트리밍 중지"""
        self.is_running = False
        logger.debug("스트리밍 중지 요청")
    
    @api_retry(max_retries=3, delay_seconds=10)
    def _get_notifications_with_retry(self, limit: int = 20, since_id: str = None):
        """
        재시도 로직이 적용된 notifications 메서드
        
        Args:
            limit: 최대 알림 개수
            since_id: 마지막 확인한 알림 ID
            
        Returns:
            알림 리스트
        """
        return self.api.notifications(
            limit=limit,
            since_id=since_id
        )


def initialize_stream_with_dm(api: mastodon.Mastodon, sheets_manager: SheetsManager) -> StreamManager:
    """
    DM 지원이 포함된 스트림 매니저 초기화
    
    Args:
        api: 마스토돈 API 객체
        sheets_manager: Google Sheets 관리자
        
    Returns:
        StreamManager: 초기화된 스트림 매니저
    """
    if not IMPORTS_AVAILABLE:
        logger.error("필수 의존성이 없어 스트림 매니저를 초기화할 수 없습니다.")
        return None
    
    # DM 전송기 전역 초기화
    try:
        from utils.dm_sender import initialize_dm_sender
        initialize_dm_sender(api)
    except Exception as e:
        logger.warning(f"DM 전송기 초기화 실패: {e}")
    
    # 스트림 매니저 생성
    manager = StreamManager(api, sheets_manager)
    logger.debug("DM 지원 스트림 매니저 초기화 완료")
    
    return manager


# 편의 함수들
def create_stream_handler(api: mastodon.Mastodon, sheets_manager: SheetsManager) -> Optional[BotStreamHandler]:
    """스트림 핸들러 생성"""
    if not IMPORTS_AVAILABLE:
        logger.error("필수 의존성이 없어 스트림 핸들러를 생성할 수 없습니다.")
        return None
    
    return BotStreamHandler(api, sheets_manager)


def create_stream_manager(api: mastodon.Mastodon, sheets_manager: SheetsManager) -> Optional[StreamManager]:
    """스트림 매니저 생성"""
    if not IMPORTS_AVAILABLE:
        logger.error("필수 의존성이 없어 스트림 매니저를 생성할 수 없습니다.")
        return None
    
    return StreamManager(api, sheets_manager)


def validate_stream_dependencies() -> Tuple[bool, list]:
    """
    스트리밍 의존성 검증
    
    Returns:
        Tuple[bool, list]: (유효성, 오류 목록)
    """
    errors = []
    
    # 라이브러리 확인
    try:
        import mastodon
    except ImportError:
        errors.append("mastodon.py 라이브러리가 설치되지 않았습니다.")
    
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        errors.append("beautifulsoup4 라이브러리가 설치되지 않았습니다.")
    
    # 환경 변수 확인 (config 모듈이 있는 경우만)
    if IMPORTS_AVAILABLE:
        try:
            required_env = ['MASTODON_ACCESS_TOKEN']
            for env_var in required_env:
                if not hasattr(config, env_var) or not getattr(config, env_var, None):
                    errors.append(f"환경 변수 {env_var}가 설정되지 않았습니다.")
        except Exception as e:
            errors.append(f"환경 변수 검증 실패: {e}")
    
    return len(errors) == 0, errors


# 개발자를 위한 유틸리티
def show_stream_info() -> None:
    """
    스트림 핸들러 기본 정보 출력 (개발용)
    """
    try:
        print("=== Stream Handler 정보 ===")
        print(f"의존성 상태: {'✅ 정상' if IMPORTS_AVAILABLE else '❌ 실패'}")
        
        # 의존성 검증
        is_valid, errors = validate_stream_dependencies()
        print(f"의존성 검증: {'✅ 통과' if is_valid else '❌ 실패'}")
        
        if errors:
            print("오류:")
            for error in errors[:3]:  # 최대 3개만
                print(f"  - {error}")
            if len(errors) > 3:
                print(f"  ... 외 {len(errors) - 3}개")
        
        # 주요 기능
        print("\n주요 기능:")
        print("  ✅ ModernCommandRouter 연동")
        print("  ✅ HTML 처리 통합 (HTMLCleaner)")
        print("  ✅ 멘션 길이 초과 방지 (MentionManager)")
        print("  ✅ DM 전송 지원")
        
        print("\n=== 정보 출력 완료 ===")
        
    except Exception as e:
        print(f"스트림 정보 출력 실패: {e}")


