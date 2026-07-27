"""Logging setup for the whole application."""

import logging
import sys


def setup_logging(level: str) -> None:
    """Configure root logging once for the process.

    force=True: re-applies config even if something (a library import) already
    called basicConfig first, so our format always wins regardless of import
    order.
    """
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )
