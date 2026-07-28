"""Logging setup for the whole application."""

import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3


class _ResilientRotatingFileHandler(RotatingFileHandler):
    """A write failure here must be loud, not swallowed.

    The stock Handler.handleError() already avoids crashing the caller, but
    it only writes to stderr - fine for an attached terminal, invisible for
    a backgrounded/detached process (exactly how this app runs in practice).
    This mirrors the same failure to stdout too, through the still-working
    stream handler, so a broken file sink can never again be the only thing
    that noticed.
    """

    def handleError(self, record: logging.LogRecord) -> None:
        message = f"log file handler failed to write a record: {record.getMessage()!r}"
        print(f"WARNING | app.utils.logging | {message}", file=sys.stdout, flush=True)
        traceback.print_exc(file=sys.stderr)


def setup_logging(level: str, log_dir: str) -> None:
    """Configure root logging once for the process: stdout and a rotating file.

    stdout is the primary sink - it never broke in the incident this was
    written for. The file handler is secondary and best-effort:
    delay=True defers opening the file until the first actual log call
    (avoids grabbing a handle before the directory necessarily exists, and
    means a broken/replaced file only affects the handler on its next write,
    not at process startup). log_dir is caller-supplied (from Settings, see
    CLAUDE.md - config is read only in app/config/settings.py) specifically
    so it can be pointed outside a cloud-synced folder; see Settings.log_dir.

    force=True: re-applies config even if something (a library import) already
    called basicConfig first, so our format always wins regardless of import
    order.
    """
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    file_handler = _ResilientRotatingFileHandler(
        log_dir_path / "polybot.log", maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, delay=True
    )
    file_handler.setFormatter(formatter)

    logging.basicConfig(level=level, handlers=[stream_handler, file_handler], force=True)
