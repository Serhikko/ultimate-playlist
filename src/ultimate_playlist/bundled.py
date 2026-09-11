"""Tools shipped next to the packaged executable: ``<app folder>/bin/ffmpeg.exe`` and friends.

The no-install Windows build (PyInstaller) puts ffmpeg, ffprobe and a JavaScript runtime in a
``bin`` folder beside ``UltimatePlaylist.exe``. Those must win over whatever happens to be on
PATH so the package works on a machine with nothing installed. Everything here is a cheap
filesystem lookup, safe to call on every probe. ``ULTIMATE_PLAYLIST_BIN`` overrides the folder
(tests, development, or a user who unpacks the tools somewhere else).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

ENV_BIN = "ULTIMATE_PLAYLIST_BIN"
BIN_DIR_NAME = "bin"
TOOL_NAMES: tuple[str, ...] = ("ffmpeg", "ffprobe", "deno", "node")

# Values of ULTIMATE_PLAYLIST_BIN already reported as unusable: bin_dir() runs on every probe
# and the user deliberately set the variable, so say it once at WARNING instead of on every call.
_warned_env_values: set[str] = set()


def is_frozen() -> bool:
    """True inside a PyInstaller build (its bootloader sets ``sys.frozen``)."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path | None:
    """The folder holding the packaged executable; None when running from source."""
    if not is_frozen():
        return None
    exe = Path(sys.executable)
    try:
        return exe.resolve().parent
    except OSError:  # a path resolve() cannot walk (dead symlink, odd share); still usable
        return exe.absolute().parent


def bin_dir() -> Path | None:
    r"""``ULTIMATE_PLAYLIST_BIN`` when it names a directory, else ``<app>/bin`` when it exists.

    ``set ULTIMATE_PLAYLIST_BIN="C:\tools\my bin"`` in cmd keeps the quotes in the value, so
    surrounding quotes and whitespace are stripped before the folder is looked up.
    """
    raw = os.environ.get(ENV_BIN, "").strip().strip('"').strip()
    if raw:
        candidate = Path(os.path.expandvars(raw)).expanduser()
        if candidate.is_dir():
            return Path(os.path.abspath(candidate))
        if raw not in _warned_env_values:
            _warned_env_values.add(raw)
            log.warning("%s=%s is not a directory; ignoring it", ENV_BIN, raw)
    app = app_dir()
    if app is not None:
        folder = app / BIN_DIR_NAME
        if folder.is_dir():
            return folder
    return None


def executable_names(name: str) -> tuple[str, ...]:
    """File names ``name`` may have: ``("ffmpeg.exe", "ffmpeg")`` on Windows, ``("ffmpeg",)`` elsewhere."""
    if sys.platform.startswith("win") and not name.lower().endswith(".exe"):
        return (f"{name}.exe", name)
    return (name,)


def bundled_tool(name: str) -> str | None:
    """Path of ``name`` in the bundled bin folder (``name.exe`` first on Windows), else None."""
    folder = bin_dir()
    if folder is None:
        return None
    for filename in executable_names(name):
        path = folder / filename
        if path.is_file():
            return str(path)
    return None


def bundled_tools() -> dict[str, str]:
    """The known tools that are actually present, for diagnostics."""
    found: dict[str, str] = {}
    for name in TOOL_NAMES:
        path = bundled_tool(name)
        if path:
            found[name] = path
    return found


def is_bundled(name: str, path: str | os.PathLike[str] | None) -> bool:
    """True when ``path`` is the bundled copy of ``name`` (case- and separator-insensitive)."""
    bundled = bundled_tool(name)
    if not bundled or not path:
        return False
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(bundled))
