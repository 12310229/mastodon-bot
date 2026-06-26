"""
CoC 마스토돈 자동봇 — 메인 실행 파일.
"""

import os
import sys
import signal
import time
from typing import Optional

# Windows 콘솔 UTF-8 인코딩 설정
if sys.platform == 'win32':
    import codecs
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
    sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# 경로 설정 (VM 환경 대응)
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    import mastodon
    from config.settings import config
    from config.validators import validate_startup_config
    from utils.logging_config import setup_logging, logger, should_log_debug, LogFormatter
    from utils.error_handling import setup_global_exception_handler
    from utils.sheets_operations import SheetsManager
    from utils.cache_manager import bot_cache, warmup_cache, warmup_aux_caches
    from handlers.stream_handler import StreamManager,validate_stream_dependencies
    from handlers.command_router import initialize_command_router
    from utils.api_retry import api_retry
    from utils.stock_engine import get_stock_engine
except ImportError as e:
    print(f"필수 모듈 임포트 실패: {e}")
    print("필요한 패키지가 설치되어 있는지 확인해주세요.")
    sys.exit(1)


# 부팅 단계 총 개수 — boot_phase / boot_ok / boot_fail 호출 시 분모로 사용.
# 단계 추가 시 이 값과 _initialize_basic_systems 등의 step 인자를 함께 갱신할 것.
_BOOT_TOTAL_STEPS = 4


class BotApplication:
    """
    마스토돈 봇 애플리케이션 클래스
    
    봇의 전체 생명주기를 관리합니다:
    - 초기화 및 설정 검증
    - 마스토돈 API 연결
    - Google Sheets 연결
    - 명령어 시스템 초기화
    - 스트리밍 시작 및 관리
    """
    
    def __init__(self):
        """BotApplication 초기화"""
        self.api: Optional[mastodon.Mastodon] = None
        self.sheets_manager: Optional[SheetsManager] = None
        self.stream_manager: Optional[StreamManager] = None
        self.stock_engine = None
        self.is_running = False
        self._shutdown_requested = False
        self.startup_time = time.time()

        # 시그널 핸들러 설정 (Ctrl+C 처리)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def run(self) -> int:
        """
        봇 애플리케이션 실행

        Returns:
            int: 종료 코드 (0: 정상, 1: 오류)
        """
        try:
            logger.info("CoC 봇을 시작합니다.")

            if not self._initialize_basic_systems():
                return 1
            if not self._connect_external_services():
                return 1
            if not self._initialize_bot_systems():
                return 1
            if not self._start_streaming():
                return 1

            logger.info("봇이 정상 종료되었습니다.")
            return 0

        except KeyboardInterrupt:
            logger.info("사용자 중단 요청 — 봇을 종료합니다.")
            return 0
        except Exception as e:
            logger.critical(
                LogFormatter.operation_fail("예상치 못한 오류로 봇이 중단됐습니다", e),
                exc_info=True,
            )
            self._send_emergency_notification(str(e))
            return 1
        finally:
            self._cleanup()
    
    def _initialize_basic_systems(self) -> bool:
        """[1/4] 전역 예외 핸들러 + 설정/스트리밍 의존성 검증."""
        step, total, name = 1, _BOOT_TOTAL_STEPS, "설정/의존성 검증"
        logger.debug(LogFormatter.boot_phase(step, total, name))
        try:
            setup_global_exception_handler()

            is_valid, summary = validate_startup_config()
            if not is_valid:
                logger.error(f"설정에 문제가 있습니다.\n{summary}")
                return False

            deps_valid, deps_errors = validate_stream_dependencies()
            if not deps_valid:
                logger.error("필요한 모듈이 빠져 있습니다:")
                for err in deps_errors:
                    logger.error(f"  - {err}")
                return False

            logger.info("  ✓ 설정 확인")
            logger.debug(LogFormatter.boot_ok(step, total, name))
            return True

        except Exception as e:
            logger.error(
                LogFormatter.boot_fail(step, total, name, e),
                exc_info=True,
            )
            return False
    
    def _connect_external_services(self) -> bool:
        """[2/4] 마스토돈 + Google Sheets 연결."""
        step, total, name = 2, _BOOT_TOTAL_STEPS, "외부 서비스 연결"
        logger.debug(LogFormatter.boot_phase(step, total, name))
        try:
            if not self._connect_mastodon_api():
                return False
            if not self._connect_google_sheets():
                return False
            logger.debug(LogFormatter.boot_ok(step, total, name))
            return True
        except Exception as e:
            logger.error(
                LogFormatter.boot_fail(step, total, name, e),
                exc_info=True,
            )
            return False

    # 토큰 오타·만료 등 비일시적 오류에서 3분 대기는 운영자 시간을 잡아먹는다.
    # 일시적 네트워크 장애만 방어하는 수준 (max=2, delay=10s) 으로 완화.
    @api_retry(max_retries=2, delay_seconds=10)
    def _connect_mastodon_api(self) -> bool:
        """마스토돈 API 연결. 실패 시 운영자가 토큰/URL 어느 쪽 문제인지 식별 가능해야 함."""
        api_url = config.MASTODON_API_BASE_URL
        auth_mode = "access_token" if config.MASTODON_ACCESS_TOKEN else "client_id+secret"
        try:
            if config.MASTODON_ACCESS_TOKEN:
                self.api = mastodon.Mastodon(
                    access_token=config.MASTODON_ACCESS_TOKEN,
                    api_base_url=api_url,
                    version_check_mode='none',
                )
            else:
                # 토큰이 없는 경우 client_id+secret 으로 OAuth 로그인.
                # access_token 은 전달하지 않는다 (빈 문자열 전달 시 인증 모호해짐).
                self.api = mastodon.Mastodon(
                    client_id=config.MASTODON_CLIENT_ID,
                    client_secret=config.MASTODON_CLIENT_SECRET,
                    api_base_url=api_url,
                    version_check_mode='none',
                )

            username = self.api.me().get('username', '?')
            logger.info(f"  ✓ 마스토돈 로그인 (@{username})")
            logger.debug(f"   url={api_url} | auth={auth_mode}")
            return True

        except Exception as e:
            logger.error(f"마스토돈 연결 실패 — 토큰과 서버 주소를 확인하세요.")
            logger.debug(
                LogFormatter.operation_fail(
                    "  ↳ Mastodon 연결", e,
                    url=api_url, auth=auth_mode,
                ),
                exc_info=True,
            )
            return False

    def _connect_google_sheets(self) -> bool:
        """Google Sheets 연결 + 기본 시트 구조 검증.
        실패 메시지로 운영자가 sheet_id 오타 vs 권한 vs 구조 문제를 구분할 수 있어야 함.
        """
        sheet_id = config.SHEET_ID
        cred_path = config.get_credentials_path()
        try:
            self.sheets_manager = SheetsManager(
                sheet_id=sheet_id,
                credentials_path=cred_path,
            )

            result = self.sheets_manager.validate_sheet_structure()
            if not result['valid']:
                errors = result['errors']
                logger.error("시트 구조에 문제가 있습니다:")
                for err in errors[:5]:
                    logger.error(f"  - {err}")
                if len(errors) > 5:
                    logger.error(f"  ... 외 {len(errors) - 5}개 (자세한 내용은 logs/bot.log)")
                return False
            warnings = result.get('warnings', [])
            for warn in warnings:
                logger.warning(warn)

            ws_count = len(result.get('worksheets_found', []))
            logger.info(f"  ✓ 시트 연결 (워크시트 {ws_count}개)")
            return True

        except Exception as e:
            logger.error("시트 연결 실패 — 시트 ID 와 서비스 계정 권한을 확인하세요.")
            logger.debug(
                LogFormatter.operation_fail(
                    "  ↳ Google Sheets 연결", e,
                    sheet_id=sheet_id, credentials=str(cred_path),
                ),
                exc_info=True,
            )
            return False

    def _log_discovered_commands(self) -> None:
        """발견된 명령어 목록을 콘솔에 친화적으로 출력 (자세한 분류는 파일 로그)."""
        try:
            from commands.registry import get_registry

            registry = get_registry()
            all_commands = registry._commands

            if not all_commands:
                logger.warning("등록된 명령어가 없습니다.")
                return

            # 패키지별 분류는 파일 로그에만 (디버그용)
            by_package: dict = {}
            for cmd_name, registered_cmd in all_commands.items():
                pkg = registered_cmd.metadata.command_package or '(unknown)'
                by_package.setdefault(pkg, []).append(registered_cmd.metadata.name or cmd_name)

            ordered_packages = ['default', 'system', 'trpg_common', 'coc']
            counts = []
            for pkg_name in ordered_packages:
                if pkg_name in by_package:
                    counts.append(f"{pkg_name}={len(by_package[pkg_name])}")
            for pkg_name in by_package:
                if pkg_name not in ordered_packages:
                    counts.append(f"{pkg_name}={len(by_package[pkg_name])}")

            logger.info(f"  ✓ 명령어 {len(all_commands)}개 준비")
            logger.debug(f"   패키지별 분포: {' '.join(counts)}")
            if should_log_debug():
                for pkg_name in ordered_packages:
                    if pkg_name in by_package:
                        names = ", ".join(sorted(by_package[pkg_name]))
                        logger.debug(f"      [{pkg_name}] {names}")

        except Exception as e:
            logger.warning(
                LogFormatter.operation_fail("명령어 목록 출력", e)
            )

    def _initialize_bot_systems(self) -> bool:
        """[3/4] 명령어 라우터, 캐시 워밍업, 스트림 매니저 초기화."""
        step, total, name = 3, _BOOT_TOTAL_STEPS, "봇 시스템 초기화"
        logger.debug(LogFormatter.boot_phase(step, total, name))
        try:
            command_router = initialize_command_router(self.sheets_manager, self.api)
            self._log_discovered_commands()

            try:
                warmup_cache(self.sheets_manager)
            except Exception as e:
                logger.warning("도움말 캐시 준비 중 문제가 발생했지만 계속 진행합니다.")
                logger.debug(LogFormatter.operation_fail("캐시 워밍업", e))

            # 보조 시트(랜덤표 / 커스텀) — 첫 사용자의 3~5초 지연 제거.
            try:
                warmup_aux_caches(self.sheets_manager)
            except Exception as e:
                logger.warning("보조 시트 캐시 준비 중 문제가 발생했지만 계속 진행합니다.")
                logger.debug(LogFormatter.operation_fail("보조 시트 워밍업", e))

            try:
                from handlers.stream_handler import initialize_stream_with_dm
                self.stream_manager = initialize_stream_with_dm(self.api, self.sheets_manager)
                logger.debug("  ↳ 스트림 매니저 ✓ | mode=stream+dm")
            except ImportError as e:
                self.stream_manager = StreamManager(self.api, self.sheets_manager)
                logger.warning("DM 송신 기능을 불러오지 못해 기본 모드로 시작합니다.")
                logger.debug(LogFormatter.operation_fail("DM 스트림 매니저 import", e))
            except Exception as e:
                self.stream_manager = StreamManager(self.api, self.sheets_manager)
                logger.error("DM 송신 모듈 초기화 실패 — 기본 모드로 동작합니다.")
                logger.debug(
                    LogFormatter.operation_fail("DM 스트림 매니저 초기화", e),
                    exc_info=True,
                )

            validation = command_router.validate_all_systems()
            errors = validation.get('errors', [])
            if not validation['overall_valid']:
                logger.warning(f"명령어 검증에서 {len(errors)}건의 문제 발견:")
                for err in errors[:5]:
                    logger.warning(f"  - {err}")
                if len(errors) > 5:
                    logger.warning(f"  ... 외 {len(errors) - 5}개")

            # 주식 엔진 시작 — 백그라운드에서 6시간마다 가격 갱신.
            # 상태는 JSON 파일(data/stock_state.json)에 영속화. 시트 미러 없음.
            try:
                self.stock_engine = get_stock_engine()
                self.stock_engine.start(
                    post_update_callback=self._refresh_character_stock_rates,
                )
                logger.info("  ✓ 주식 엔진 시작")
            except Exception as e:
                logger.warning("주식 엔진 시작 실패 — 거래는 가능하나 자동 갱신은 비활성")
                logger.debug(LogFormatter.operation_fail("주식 엔진 시작", e))

            logger.debug(LogFormatter.boot_ok(step, total, name))
            return True

        except Exception as e:
            logger.error(
                LogFormatter.boot_fail(step, total, name, e),
                exc_info=True,
            )
            return False

    def _start_streaming(self) -> bool:
        """[4/4] 마스토돈 스트리밍 시작 (블로킹)."""
        step, total, name = 4, _BOOT_TOTAL_STEPS, "스트리밍 시작"
        logger.debug(LogFormatter.boot_phase(step, total, name))
        logger.info("봇이 동작 중입니다. (종료: Ctrl+C)")
        try:
            self.is_running = True
            success = self.stream_manager.start_streaming(max_retries=config.MAX_RETRIES)
            self.is_running = False
            if not success:
                logger.error("스트리밍 연결에 실패했습니다.")
                logger.debug(
                    f"[부팅 {step}/{total}] {name} 실패 | reason=start_streaming returned False "
                    f"| max_retries={config.MAX_RETRIES}"
                )
            return success

        except Exception as e:
            self.is_running = False
            logger.error("스트리밍 중 오류가 발생했습니다.")
            logger.debug(
                LogFormatter.boot_fail(
                    step, total, name, e, max_retries=config.MAX_RETRIES,
                ),
                exc_info=True,
            )
            return False
    
    def _send_emergency_notification(self, error_message: str) -> None:
        """긴급 상황 알림 전송"""
        @api_retry(max_retries=3, delay_seconds=60)
        def _send_status(status_text, visibility_level):
            return self.api.status_post(
                status=status_text,
                visibility=visibility_level
            )

        try:
            if not self.api:
                logger.warning("[종료] 긴급 알림 스킵 | reason=Mastodon API 미연결")
                return

            _send_status(
                config.format_response("자동봇이 오류로 중지되었습니다. 복구 작업 중입니다."),
                'unlisted'
            )

            if config.SYSTEM_ADMIN_ID:
                max_error_length = (config.MAX_MESSAGE_LENGTH if config else 1000) - 50
                admin_body = config.format_response(
                    f"봇 시스템 오류\n{error_message[:max_error_length]}"
                )
                _send_status(
                    f"@{config.SYSTEM_ADMIN_ID} {admin_body}",
                    'direct'
                )

            logger.info(
                f"[종료] 긴급 알림 전송 ✓ | admin={'@' + config.SYSTEM_ADMIN_ID if config.SYSTEM_ADMIN_ID else '(미설정)'}"
            )

        except Exception as e:
            logger.error(
                LogFormatter.operation_fail(
                    "[종료] 긴급 알림 전송", e,
                    admin=config.SYSTEM_ADMIN_ID or '(미설정)',
                ),
                exc_info=True,
            )

    def _signal_handler(self, signum, frame):
        """Ctrl+C / SIGTERM 핸들러."""
        signal_name = {
            signal.SIGINT: 'SIGINT',
            signal.SIGTERM: 'SIGTERM',
        }.get(signum, str(signum))

        if self._shutdown_requested:
            logger.info("강제 종료합니다.")
            logger.debug(f"signal={signal_name}")
            sys.exit(1)

        self._shutdown_requested = True
        logger.info("종료 요청을 받았습니다 — 정리 중... (다시 누르면 강제 종료)")
        logger.debug(f"signal={signal_name}")
        self.is_running = False

        if self.stream_manager:
            self.stream_manager.stop_streaming()

    def _cleanup(self) -> None:
        """정리 작업."""
        try:
            if self.stream_manager:
                self.stream_manager.stop_streaming()
            if self.stock_engine:
                try:
                    self.stock_engine.stop()
                except Exception as e:
                    logger.debug(LogFormatter.operation_fail("[종료] 주식 엔진 정지", e))
            try:
                bot_cache.cleanup_all_expired()
            except Exception as e:
                logger.debug(LogFormatter.operation_fail("[종료] 캐시 정리", e))

            logger.debug("[종료] 정리 완료")

        except Exception as e:
            logger.error("종료 정리 중 오류가 발생했습니다.")
            logger.debug(LogFormatter.operation_fail("[종료] 정리 작업", e), exc_info=True)

    # ------------------------------------------------------------------
    # 주식 가격 변동 콜백 — 시트 동기화 대상 없음.
    # 시트에는 주 수/투자금만 저장하고, 수익금·이익률은 [상태창]/거래 응답에서
    # 즉시 계산하므로 6h마다 일괄 갱신할 셀이 없다. 로그만 남긴다.
    # ------------------------------------------------------------------
    def _refresh_character_stock_rates(self, results) -> None:
        if not results:
            return
        summary = ', '.join(f"{name} {before}→{after}" for name, before, after in results)
        logger.info(f"[stock] 6h 사이클 적용: {summary}")


    def get_status(self) -> dict:
        """애플리케이션 상태 반환 (개발/디버깅용)"""
        status = {
            'is_running': self.is_running,
            'startup_time': self.startup_time,
            'uptime_seconds': time.time() - self.startup_time,
            'api_connected': self.api is not None,
            'sheets_connected': self.sheets_manager is not None,
            'stream_manager_ready': self.stream_manager is not None,
        }
        
        # 스트림 매니저 상태 추가
        if self.stream_manager:
            status['stream_status'] = self.stream_manager.get_status()
            # handler_stats 제거됨 - 통계 기능 사용 안함
        
        return status


def main() -> int:
    """메인 엔트리 포인트"""
    # 로깅 시스템 초기화
    setup_logging()
    
    try:
        # 봇 애플리케이션 생성 및 실행
        app = BotApplication()
        return app.run()
        
    except Exception as e:
        print(f"애플리케이션 시작 실패: {e}")
        return 1


def show_version():
    """버전 정보 출력"""
    print("CoC 마스토돈 자동봇")
    print("모듈형 아키텍처 / Google Sheets 연동")
    print("지원 룰: Call of Cthulhu 7판")


def show_help():
    """도움말 출력"""
    print("CoC 마스토돈 자동봇 사용법")
    print()
    print("실행:")
    print("  python main.py              # 봇 시작")
    print("  python main.py --version    # 버전 정보")
    print("  python main.py --help       # 이 도움말")
    print()
    print("공통 명령어:")
    print("  [도움말]                     # 도움말 시트 출력")
    print("  [NdM] [NdM+K] [NdM-K]        # 다이스")
    print("  [랜덤/옵션1, 옵션2, ...]     # 랜덤 선택")
    print("  [YN] / [yn]                  # 예/아니오")
    print()
    print("환경 설정: .env.example 을 .env 로 복사 후 편집")
    print("  MASTODON_ACCESS_TOKEN 과 SHEET_ID 만 채우면 됩니다.")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='CoC 마스토돈 자동봇')
    parser.add_argument('--version', '-v', action='store_true', help='버전 정보 출력')
    parser.add_argument('--help-full', action='store_true', help='상세 도움말 출력')

    args = parser.parse_args()

    if args.version:
        show_version()
        sys.exit(0)

    if args.help_full:
        show_help()
        sys.exit(0)

    exit_code = main()
    sys.exit(exit_code)