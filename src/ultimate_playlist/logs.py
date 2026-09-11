"""Logging setup: a calm console plus a rotating app.log that keeps the full story.

The console is what a user sees in the terminal, so it must not scroll with the UI's one-second
polling or with yt-dlp's warnings; those still land in ``app.log`` for troubleshooting.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import app_data_dir

log = logging.getLogger(__name__)

LOG_FILE_NAME = "app.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3
YTDLP_LOGGER = "ultimate_playlist.youtube"  # yt-dlp's own messages, see providers/youtube.py
FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
CONSOLE_DATEFMT = "%H:%M:%S"


def log_file_path() -> Path:
    return app_data_dir() / LOG_FILE_NAME


def _access_status(record: logging.LogRecord) -> int | None:
    """HTTP status of a uvicorn access record ('%s - "%s %s HTTP/%s" %d'), if recognisable."""
    args = record.args
    if isinstance(args, tuple) and args and isinstance(args[-1], int):
        return args[-1]
    return None


class RoutineAccessFilter(logging.Filter):
    """Drop access lines for successful requests: the UI polls /api/jobs every second."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access":
            return True
        status = _access_status(record)
        return status is None or status >= 400


class ConsoleFilter(logging.Filter):
    """Keep yt-dlp chatter off the terminal; it is in app.log and in the job's error text."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name != YTDLP_LOGGER and not record.name.startswith(YTDLP_LOGGER + ".")


class TidyNameFilter(logging.Filter):
    """uvicorn logs its ordinary startup lines under 'uvicorn.error'; call them 'uvicorn'."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.error":
            record.name = "uvicorn"
        return True


def build_handlers(
    console_level: int = logging.INFO, file_level: int = logging.INFO
) -> list[logging.Handler]:
    """A stderr handler plus (when the app data dir is writable) a rotating file handler."""
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, CONSOLE_DATEFMT))
    for flt in (TidyNameFilter(), RoutineAccessFilter(), ConsoleFilter()):
        console.addFilter(flt)
    handlers: list[logging.Handler] = [console]
    log_path = log_file_path()
    try:
        file_handler = RotatingFileHandler(
            log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
    except OSError as exc:
        log.warning("Could not open the log file %s: %s", log_path, exc)
        return handlers
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
    for flt in (TidyNameFilter(), RoutineAccessFilter()):
        file_handler.addFilter(flt)
    handlers.append(file_handler)
    return handlers


def install_handlers(
    console_level: int = logging.INFO, file_level: int = logging.INFO
) -> list[logging.Handler]:
    """Attach the handlers to the root logger (no de-duplication; callers guard against repeats)."""
    root = logging.getLogger()
    root.setLevel(min(console_level, file_level))
    handlers = build_handlers(console_level, file_level)
    for handler in handlers:
        root.addHandler(handler)
    return handlers
