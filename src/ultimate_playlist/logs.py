"""Logging setup: a calm console plus a rotating app.log that keeps the full story.

The console is what a user sees in the terminal, so it must not scroll with the UI's one-second
polling or with yt-dlp's warnings; those still land in ``app.log`` for troubleshooting.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .bundled import is_frozen
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


# Paths whose query string carries a secret: Spotify's one-time login code. Their access lines
# are written without it. (The same value as providers/spotify_auth.CALLBACK_PATH, which a test
# checks; importing it here would pull the HTTP stack into the logging setup.)
SECRET_QUERY_PATHS: tuple[str, ...] = ("/api/spotify/callback",)
HIDDEN_QUERY = "?[hidden]"


def _scrub_access_path(record: logging.LogRecord) -> None:
    """Replace the query string of a secret-carrying path in a uvicorn access record."""
    args = record.args
    if not (isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str)):
        return
    path, _, query = args[2].partition("?")
    if query and path.startswith(SECRET_QUERY_PATHS):
        record.args = (*args[:2], path + HIDDEN_QUERY, *args[3:])


class RoutineAccessFilter(logging.Filter):
    """Drop access lines for successful requests (the UI polls /api/jobs every second) and keep
    the Spotify login code out of the ones that are written (a callback that failed)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access":
            return True
        status = _access_status(record)
        if status is not None and status < 400:
            return False
        _scrub_access_path(record)
        return True


UVICORN_LOGGERS = ("uvicorn", "uvicorn.error")  # TidyNameFilter may have renamed it already


class ConsoleFilter(logging.Filter):
    """What the terminal does not need (app.log keeps all of it).

    yt-dlp chatter is in app.log and in the job's error text. In the packaged build the user was
    just told to close the window to stop, so uvicorn's bookkeeping ("Started server process",
    "Waiting for application startup", "Uvicorn running on ... (Press CTRL+C to quit)") is kept
    off the console too: the app's own line with the URL is the one that matters, and it must
    not be one of four similar-looking INFO lines. Warnings and errors from uvicorn still show.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == YTDLP_LOGGER or record.name.startswith(YTDLP_LOGGER + "."):
            return False
        if record.name in UVICORN_LOGGERS and record.levelno < logging.WARNING and is_frozen():
            return False
        return True


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
