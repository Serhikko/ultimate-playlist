"""User settings and the per-user application data directory."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_NAME = "ultimate-playlist"
ENV_HOME = "ULTIMATE_PLAYLIST_HOME"


def app_data_dir() -> Path:
    """Where config, the library index and playlists live. Override with ULTIMATE_PLAYLIST_HOME."""
    raw = os.environ.get(ENV_HOME)
    path = Path(os.path.expandvars(raw)).expanduser() if raw else Path.home() / f".{APP_NAME}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _default_library_dir() -> Path:
    return Path.home() / "Music" / "Ultimate Playlist"


def _expand(p: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser()


@dataclass
class Settings:
    library_dir: Path = field(default_factory=_default_library_dir)
    audio_format: str = "mp3"
    audio_quality: str = "0"  # yt-dlp "preferredquality"; "0" = best VBR
    ffmpeg_path: str | None = None
    concurrency: int = 2
    embed_cover: bool = True
    js_runtimes: list[str] = field(default_factory=lambda: ["deno", "node"])

    def __post_init__(self) -> None:
        self.library_dir = _expand(self.library_dir)
        self.concurrency = max(1, min(int(self.concurrency), 6))
        if self.ffmpeg_path is not None:
            self.ffmpeg_path = str(_expand(self.ffmpeg_path)) if self.ffmpeg_path else None

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
        data = asdict(self)
        data["library_dir"] = str(self.library_dir)
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
