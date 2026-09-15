"""The console must stay calm: no polling noise, no yt-dlp chatter, no 'uvicorn.error' label."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest

from ultimate_playlist import logs
from ultimate_playlist.config import Settings


def _record(name: str, msg: str, args: tuple = (), level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, msg, args, None)


def _access(status: int) -> logging.LogRecord:
    fmt = '%s - "%s %s HTTP/%s" %d'
    return _record("uvicorn.access", fmt, ("127.0.0.1:1", "GET", "/api/jobs", "1.1", status))


def test_routine_access_lines_are_dropped_but_errors_kept() -> None:
    flt = logs.RoutineAccessFilter()
    assert not flt.filter(_access(200))
    assert not flt.filter(_access(206))
    assert not flt.filter(_access(304))
    assert flt.filter(_access(404))
    assert flt.filter(_access(500))
    assert flt.filter(_record("uvicorn.access", "unknown shape %s", ("x",)))
    assert flt.filter(_record("ultimate_playlist.library", "Picked up a file"))


def test_the_spotify_login_code_never_reaches_an_access_line() -> None:
    """A callback that fails (400) is logged, but without its query string (state and code)."""
    flt = logs.RoutineAccessFilter()
    fmt = '%s - "%s %s HTTP/%s" %d'
    callback = "/api/spotify/callback?state=x&code=SECRETCODE1234567890"
    failed = _record("uvicorn.access", fmt, ("127.0.0.1:1", "GET", callback, "1.1", 400))
    assert flt.filter(failed)
    assert "SECRETCODE" not in failed.getMessage() and "state=x" not in failed.getMessage()
    assert '"GET /api/spotify/callback?[hidden] HTTP/1.1" 400' in failed.getMessage()
    other = _record("uvicorn.access", fmt, ("127.0.0.1:1", "GET", "/api/x?q=daft", "1.1", 404))
    assert flt.filter(other) and "/api/x?q=daft" in other.getMessage()


def test_secret_query_paths_follow_the_spotify_callback() -> None:
    from ultimate_playlist.providers import spotify_auth

    assert spotify_auth.CALLBACK_PATH in logs.SECRET_QUERY_PATHS


def test_console_filter_hides_ytdlp_chatter_only() -> None:
    flt = logs.ConsoleFilter()
    assert not flt.filter(
        _record(logs.YTDLP_LOGGER, "nsig extraction failed", level=logging.WARNING)
    )
    assert not flt.filter(_record(logs.YTDLP_LOGGER + ".sub", "x", level=logging.ERROR))
    assert flt.filter(_record("ultimate_playlist.providers.youtube", "our own message"))
    assert flt.filter(_record("ultimate_playlist.youtube_ish", "different logger"))


def test_tidy_name_filter_renames_uvicorn_error() -> None:
    flt = logs.TidyNameFilter()
    rec = _record("uvicorn.error", "Started server process")
    assert flt.filter(rec) and rec.name == "uvicorn"
    rec = _record("uvicorn.access", "x")
    assert flt.filter(rec) and rec.name == "uvicorn.access"


def test_console_filter_drops_uvicorn_bookkeeping_only_when_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exe's window says 'close this window to stop' and shows one line with the URL;
    uvicorn's three startup lines and its Ctrl+C advice must not bury it (app.log keeps them)."""
    flt = logs.ConsoleFilter()
    tidy = logs.TidyNameFilter()
    lines = [
        "Started server process [36132]",
        "Waiting for application startup.",
        "Application startup complete.",
        "Uvicorn running on http://127.0.0.1:8765 (Press CTRL+C to quit)",
    ]
    for line in lines:  # from source the terminal shows them, Ctrl+C included
        assert flt.filter(_record("uvicorn.error", line))
        assert flt.filter(_record("uvicorn", line))
    monkeypatch.setattr(logs, "is_frozen", lambda: True)
    for line in lines:
        assert not flt.filter(_record("uvicorn.error", line))
        assert not flt.filter(_record("uvicorn", line))  # renamed by TidyNameFilter first
    assert flt.filter(_record("uvicorn.error", "Port in use", level=logging.WARNING))
    assert flt.filter(_record("uvicorn.error", "boom", level=logging.ERROR))
    assert flt.filter(_record("ultimate_playlist.server.app", "Ultimate Playlist 0.1.0 at ..."))
    rec = _record("uvicorn.error", "Started server process")
    assert tidy.filter(rec) and rec.name == "uvicorn"  # TidyNameFilter only renames


def test_build_handlers_console_and_rotating_file(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    handlers = logs.build_handlers(console_level=logging.WARNING, file_level=logging.INFO)
    try:
        assert len(handlers) == 2
        console, file_handler = handlers
        assert isinstance(console, logging.StreamHandler)
        assert console.level == logging.WARNING
        assert isinstance(file_handler, logging.handlers.RotatingFileHandler)
        assert Path(file_handler.baseFilename) == logs.log_file_path()
        assert file_handler.maxBytes == logs.LOG_MAX_BYTES
        assert file_handler.backupCount == logs.LOG_BACKUPS
        # yt-dlp chatter: file yes, console no
        chatter = _record(logs.YTDLP_LOGGER, "WARNING from yt-dlp", level=logging.WARNING)
        assert not console.filter(chatter)
        assert file_handler.filter(chatter)
        # routine polling: neither
        assert not console.filter(_access(200)) and not file_handler.filter(_access(200))
        # the packaged build: uvicorn's bookkeeping stays in the file only
        monkeypatch.setattr(logs, "is_frozen", lambda: True)
        startup = _record("uvicorn.error", "Application startup complete.")
        assert not console.filter(startup)
        assert file_handler.filter(startup) and startup.name == "uvicorn"
    finally:
        for handler in handlers:
            handler.close()


def test_install_handlers_attaches_to_root(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    level_before = root.level
    handlers = logs.install_handlers(console_level=logging.INFO, file_level=logging.INFO)
    try:
        assert all(h in root.handlers for h in handlers)
        assert root.level == logging.INFO
        logging.getLogger("ultimate_playlist.test").info("hello from the log test")
        for handler in handlers:
            handler.flush()
        assert "hello from the log test" in logs.log_file_path().read_text(encoding="utf-8")
    finally:
        for handler in handlers:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(level_before)
        assert root.handlers == before
