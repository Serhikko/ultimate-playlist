"""User settings and the per-user application data directory."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_NAME = "ultimate-playlist"
ENV_HOME = "ULTIMATE_PLAYLIST_HOME"

# Value rules shared by `PUT /api/settings` and `up config set`, so the two never disagree.
AUDIO_FORMATS: tuple[str, ...] = ("mp3", "m4a", "opus", "flac")
JS_RUNTIMES: tuple[str, ...] = ("deno", "node", "bun", "quickjs")  # what yt-dlp knows
MIN_CONCURRENCY, MAX_CONCURRENCY = 1, 6
# yt-dlp's preferredquality: 0-10 is a VBR level (0 = best), anything above is a bitrate in kbps.
_AUDIO_QUALITY_RE = re.compile(r"^\d{1,3}$")
# What the API and the CLI show instead of a stored Spotify client secret. Sending it back
# unchanged in a settings update means "keep the secret I already have".
SECRET_MASK = "********"
CREDENTIAL_MAX_LENGTH = 200


def clean_audio_format(value: str) -> str:
    fmt = (value or "").strip().lower()
    if fmt not in AUDIO_FORMATS:
        raise ValueError(f"Audio format must be one of: {', '.join(AUDIO_FORMATS)}.")
    return fmt


def clean_audio_quality(value: str) -> str:
    """A bare number for yt-dlp: "0".."10" (VBR level) or a bitrate such as "192" / "192k"."""
    quality = (value or "").strip().rstrip("kK").strip()
    if not quality:
        raise ValueError("Audio quality cannot be empty (use 0 for best).")
    if not _AUDIO_QUALITY_RE.match(quality):
        raise ValueError(
            "Audio quality must be 0-10 (0 = best variable bitrate) or a bitrate in kbps "
            "such as 192."
        )
    return quality


def clean_concurrency(value: int) -> int:
    if not MIN_CONCURRENCY <= value <= MAX_CONCURRENCY:
        raise ValueError(f"Concurrency must be between {MIN_CONCURRENCY} and {MAX_CONCURRENCY}.")
    return value


def clean_js_runtimes(values: list[str]) -> list[str]:
    """Lower-cased, de-duplicated, order kept; every name must be one yt-dlp understands."""
    runtimes = [rt.strip().lower() for rt in values if rt and rt.strip()]
    if not runtimes:
        raise ValueError("List at least one JavaScript runtime (deno, node).")
    for runtime in runtimes:
        if runtime not in JS_RUNTIMES:
            raise ValueError(
                f"Unknown JavaScript runtime {runtime!r}; use one of: {', '.join(JS_RUNTIMES)}."
            )
    return list(dict.fromkeys(runtimes))


def clean_credential(value: str, label: str) -> str:
    """A Spotify client id / secret: stripped, printable ASCII, at most 200 characters.

    An empty string is fine and means "not set"; nothing else about the value is checked, so a
    future change on Spotify's side cannot lock people out of the field.
    """
    text = (value or "").strip()
    if len(text) > CREDENTIAL_MAX_LENGTH:
        raise ValueError(f"The {label} is too long (at most {CREDENTIAL_MAX_LENGTH} characters).")
    if not (text.isascii() and text.isprintable()):
        raise ValueError(
            f"The {label} can only contain plain ASCII letters, digits and punctuation."
        )
    return text


def app_data_dir() -> Path:
    """Where config, the library index and playlists live. Override with ULTIMATE_PLAYLIST_HOME."""
    raw = os.environ.get(ENV_HOME)
    path = Path(os.path.expandvars(raw)).expanduser() if raw else Path.home() / f".{APP_NAME}"
    path.mkdir(parents=True, exist_ok=True)
    return path


LIBRARY_FOLDER_NAME = "Ultimate Playlist"
# FOLDERID_Music: the user's Music library as Explorer shows it. With OneDrive's "Known Folder
# Move" (the default on many consumer Windows 11 installs) that is C:\Users\<you>\OneDrive\Music,
# not ~/Music; a library created under ~/Music would never show up under "Music" in Explorer.
_FOLDERID_MUSIC = "{4BD688B7-F18E-4E6C-A72F-8B8F2B53EDE7}"


def _known_folder(folder_id: str) -> Path | None:
    """SHGetKnownFolderPath(folder_id) on Windows, None anywhere else or on any failure."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
        guid = ctypes.create_string_buffer(16)
        if ole32.CLSIDFromString(folder_id, guid) != 0:
            return None
        out = ctypes.c_wchar_p()
        if shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(out)) != 0 or not out.value:
            return None
        try:
            return Path(out.value)
        finally:
            ole32.CoTaskMemFree(ctypes.cast(out, wintypes.LPVOID))
    except Exception as exc:  # noqa: BLE001 - any shell hiccup means "use ~/Music"
        log.debug("SHGetKnownFolderPath failed: %s", exc)
        return None


def music_dir() -> Path:
    """The user's Music folder: the Windows known folder when available, else ``~/Music``."""
    return _known_folder(_FOLDERID_MUSIC) or Path.home() / "Music"


def _default_library_dir() -> Path:
    return music_dir() / LIBRARY_FOLDER_NAME


def _expand(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser()


def _text(value: object) -> str:
    """A stripped string; None (or anything that is not text) means "not set"."""
    return value.strip() if isinstance(value, str) else ""


@dataclass
class Settings:
    library_dir: Path = field(default_factory=_default_library_dir)
    audio_format: str = "mp3"
    audio_quality: str = "0"  # yt-dlp "preferredquality"; "0" = best VBR
    ffmpeg_path: str | None = None
    concurrency: int = 2
    embed_cover: bool = True
    js_runtimes: list[str] = field(default_factory=lambda: ["deno", "node"])
    # Optional Spotify developer app (https://developer.spotify.com/dashboard). Not needed for
    # Spotify links to work; it lifts the size cap on playlists. config.json is a local
    # per-user file under the app data folder, so the secret is stored there as-is (plain
    # text), like a browser's cookie jar; it is never logged and the API only ever shows a
    # mask (SECRET_MASK) for it. Both are "" when unset.
    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    def __post_init__(self) -> None:
        self.library_dir = _expand(self.library_dir)
        self.concurrency = max(1, min(int(self.concurrency), 6))
        if self.ffmpeg_path is not None:
            self.ffmpeg_path = str(_expand(self.ffmpeg_path)) if self.ffmpeg_path else None
        self.spotify_client_id = _text(self.spotify_client_id)
        self.spotify_client_secret = _text(self.spotify_client_secret)

    @property
    def has_spotify_credentials(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    # -- persistence -------------------------------------------------------------------------

    @staticmethod
    def default_path() -> Path:
        return app_data_dir() / "config.json"

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path or cls.default_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config root must be an object")
            return cls.from_dict(data)
        except Exception as exc:  # corrupt config must never take the app down
            log.warning("Ignoring unreadable config at %s: %s", path, exc)
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or self.default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def to_dict(self) -> dict[str, Any]:
        """Everything, including the Spotify secret: this is what config.json stores."""
        data = asdict(self)
        data["library_dir"] = str(self.library_dir)
        return data

    def public_dict(self) -> dict[str, Any]:
        """`to_dict()` with the Spotify secret replaced by SECRET_MASK ("" when unset).

        What `GET /api/settings` and `up config show` hand out; the real secret never leaves
        the process.
        """
        data = self.to_dict()
        data["spotify_client_secret"] = SECRET_MASK if self.spotify_client_secret else ""
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        known = {f.name for f in fields(cls)}
        clean: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key == "js_runtimes":
                if isinstance(value, list) and all(isinstance(v, str) for v in value):
                    clean[key] = value
                continue
            if key == "library_dir" and value:
                clean[key] = Path(str(value))
                continue
            clean[key] = value
        try:
            return cls(**clean)
        except (TypeError, ValueError) as exc:
            log.warning("Bad values in config, using defaults: %s", exc)
            return cls()

    # -- derived paths -----------------------------------------------------------------------

    @property
    def incoming_dir(self) -> Path:
        return self.library_dir / ".incoming"

    @property
    def index_path(self) -> Path:
        return app_data_dir() / "library.json"

    @property
    def playlists_path(self) -> Path:
        return app_data_dir() / "playlists.json"

    @property
    def playlists_export_dir(self) -> Path:
        return self.library_dir / "Playlists"
