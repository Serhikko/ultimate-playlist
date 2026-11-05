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
# Spotify client IDs are 32 hex characters today; the rule stays loose (any printable ASCII)
# so a change on Spotify's side cannot lock people out of the field.
CLIENT_ID_MAX_LENGTH = 100


# What a download produces (picked next to the Download button, per link): an audio file in
# `audio_format`, or an MP4 video in one of these qualities.
MEDIA_KINDS: tuple[str, ...] = ("audio", "video")
# best: the highest resolution in H.264 (plays on any phone, tablet or TV; usually 1080p).
# max: the highest resolution up to 4K (often VP9/AV1). 720 / 480: about that resolution (the
# smallest there is when a site has nothing that small), H.264.
VIDEO_QUALITIES: tuple[str, ...] = ("best", "max", "720", "480")
DEFAULT_VIDEO_QUALITY = "best"


def clean_audio_format(value: str) -> str:
    fmt = (value or "").strip().lower()
    if fmt not in AUDIO_FORMATS:
        raise ValueError(f"Audio format must be one of: {', '.join(AUDIO_FORMATS)}.")
    return fmt


def clean_media(value: str | None) -> str:
    """ "audio" (the default for a missing value) or "video"."""
    media = (value or "audio").strip().lower()
    if media not in MEDIA_KINDS:
        raise ValueError("Download as audio or video.")
    return media


def clean_video_quality(value: str | None) -> str:
    """A key of VIDEO_QUALITIES ("720p" / "720P" are read as "720"; empty = the default)."""
    quality = (value or DEFAULT_VIDEO_QUALITY).strip().lower().removesuffix("p")
    if quality not in VIDEO_QUALITIES:
        raise ValueError(f"Video quality must be one of: {', '.join(VIDEO_QUALITIES)}.")
    return quality


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


def clean_client_id(value: str) -> str:
    """A Spotify client ID: stripped, printable ASCII, at most CLIENT_ID_MAX_LENGTH characters.

    An empty string is fine and means "not set". Only the ID of the user's own developer app is
    ever stored: connecting an account uses Authorization Code + PKCE, which needs no secret.
    """
    text = (value or "").strip()
    if len(text) > CLIENT_ID_MAX_LENGTH:
        raise ValueError(
            f"The Spotify client ID is too long (at most {CLIENT_ID_MAX_LENGTH} characters)."
        )
    if not (text.isascii() and text.isprintable()):
        raise ValueError(
            "The Spotify client ID can only contain plain ASCII letters, digits and punctuation."
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


def _client_id_or_blank(value: object) -> str:
    """A stored client ID that breaks the rule (a hand-edited file) is dropped, never fatal."""
    try:
        return clean_client_id(_text(value))
    except ValueError as exc:
        log.warning("Ignoring spotify_client_id from the config: %s", exc)
        return ""


@dataclass
class Settings:
    library_dir: Path = field(default_factory=_default_library_dir)
    audio_format: str = "mp3"
    audio_quality: str = "0"  # yt-dlp "preferredquality"; "0" = best VBR
    ffmpeg_path: str | None = None
    concurrency: int = 2
    embed_cover: bool = True
    js_runtimes: list[str] = field(default_factory=lambda: ["deno", "node"])
    # Optional: the Client ID of the user's own Spotify developer app
    # (https://developer.spotify.com/dashboard). Spotify links work without it; with it the
    # user can connect their account (Authorization Code + PKCE, no client secret anywhere)
    # for their own playlists of any size and their Liked Songs. "" when unset. The sign-in
    # tokens are not settings: providers/spotify_auth.py keeps them in their own file.
    spotify_client_id: str = ""

    def __post_init__(self) -> None:
        self.library_dir = _expand(self.library_dir)
        self.concurrency = max(1, min(int(self.concurrency), 6))
        if self.ffmpeg_path is not None:
            self.ffmpeg_path = str(_expand(self.ffmpeg_path)) if self.ffmpeg_path else None
        self.spotify_client_id = _client_id_or_blank(self.spotify_client_id)

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
        """What config.json stores, `GET /api/settings` returns and `up config show` prints.

        Nothing in it is secret (the Spotify sign-in tokens live in their own file).
        """
        data = asdict(self)
        data["library_dir"] = str(self.library_dir)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Unknown keys are ignored, so older files load fine: the `spotify_client_secret` of
        pre-release 0.2 builds is simply dropped (and disappears from config.json at the next
        save)."""
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
