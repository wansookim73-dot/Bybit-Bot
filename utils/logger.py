import logging
import os
import sys
import errno
from logging.handlers import RotatingFileHandler

from config import LOG_FILE_PATH

_DEFAULT_BASE_NAME = "BYBIT_BOT"
_CONFIGURED = False


class SafeStreamHandler(logging.StreamHandler):
    """
    BrokenPipe(EPIPE) 상황에서 logging이 스택트레이스를 찍으며 죽는 것을 방지.
    - 예: python main_v10.py --once 2>&1 | head -n 6
    """

    def handleError(self, record) -> None:
        exc_type, exc, _tb = sys.exc_info()

        # BrokenPipeError: [Errno 32] Broken pipe
        if isinstance(exc, BrokenPipeError):
            return

        # OSError: [Errno 32] Broken pipe
        if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.EPIPE:
            return

        # 그 외에는 기본 동작(개발 중엔 문제 노출)
        super().handleError(record)


def _ensure_log_dir(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)


def _parse_level() -> int:
    """
    LOG_LEVEL 환경변수 지원: DEBUG / INFO / WARNING / ERROR
    """
    lv = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    return getattr(logging, lv, logging.INFO)


def setup_logger(name: str = _DEFAULT_BASE_NAME) -> logging.Logger:
    """
    단일 베이스 로거(BYBIT_BOT)에만 핸들러를 붙이고,
    나머지는 child logger로 propagate 해서 중복 핸들러/중복 출력 방지.
    """
    global _CONFIGURED

    base = logging.getLogger(_DEFAULT_BASE_NAME)
    level = _parse_level()
    base.setLevel(level)
    base.propagate = False

    if not _CONFIGURED:
        _ensure_log_dir(LOG_FILE_PATH)

        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # 파일 핸들러 (10MB * 5개 백업)
        file_handler = RotatingFileHandler(
            LOG_FILE_PATH,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)

        # 콘솔 핸들러는 stderr 권장:
        # - stdout을 head로 파이프할 때 BrokenPipe가 덜 발생
        # - systemd/journalctl에서도 stderr 수집됨
        console_handler = SafeStreamHandler(stream=sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)

        base.addHandler(file_handler)
        base.addHandler(console_handler)

        _CONFIGURED = True

    # name이 비어있거나 base면 base 리턴
    if not name or name == _DEFAULT_BASE_NAME:
        return base

    # child logger 구성
    if name.startswith(_DEFAULT_BASE_NAME + "."):
        child_name = name
    else:
        child_name = f"{_DEFAULT_BASE_NAME}.{name}"

    child = logging.getLogger(child_name)
    child.setLevel(logging.NOTSET)  # base 레벨 상속
    child.propagate = True          # base로 전달
    return child


# 기본 로거 (기존 코드 호환)
logger = setup_logger(_DEFAULT_BASE_NAME)


def get_logger(name: str = _DEFAULT_BASE_NAME) -> logging.Logger:
    """
    v10.1 아키텍처 호환용 래퍼.
    - get_logger("wave_fsm") -> BYBIT_BOT.wave_fsm
    - get_logger("BYBIT_BOT.wave_fsm")도 허용
    """
    return setup_logger(name)
