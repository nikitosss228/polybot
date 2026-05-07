"""Logging: rotating file + stdout, INFO level."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import SETTINGS

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(SETTINGS.log_level)
    formatter = logging.Formatter(_FORMAT)

    fh = RotatingFileHandler(
        SETTINGS.log_file,
        maxBytes=SETTINGS.log_max_bytes,
        backupCount=SETTINGS.log_backup_count,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    root.addHandler(sh)

    # Tame third-party noise.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("web3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("py_clob_client").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
