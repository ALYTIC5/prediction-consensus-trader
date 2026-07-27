"""Logging setup for the whole application."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR = Path("logs")
_LOG_FILE = _LOG_DIR / "polybot.log"
_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3


def setup_logging(level: str) -> None:
    """Configure root logging once for the process: stdout and a rotating file.

    force=True: re-applies config even if something (a library import) already
    called basicConfig first, so our format always wins regardless of import
    order.
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(_LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT)
    file_handler.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=[stream_handler, file_handler], force=True)
