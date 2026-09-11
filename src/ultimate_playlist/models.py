"""Shared data models. Every module talks in these types; keep them dependency-free."""

from __future__ import annotations

import enum
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class TrackRef:
    """Something a provider can turn into exactly one audio file. Cheap to create (no download)."""

    provider: str  # e.g. "youtube", "spotify"
    source_id: str  # provider-native id, e.g. the YouTube video id
    url: str  # canonical URL for this single track
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration: float | None = None  # seconds
    thumbnail_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # provider-specific scratch data

    @property
    def track_id(self) -> str:
        """Stable library id: '<provider>:<source_id>'."""
        return f"{self.provider}:{self.source_id}"


@dataclass
class Track:
    """A finished audio file inside the library."""

    id: str  # '<provider>:<source_id>'
    provider: str
    source_id: str
    source_url: str
    title: str
    artist: str
    path: str  # relative to the library directory, forward slashes
    album: str | None = None
    duration: float | None = None  # seconds
    has_cover: bool = False
    file_size: int = 0
    added_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Track:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"  # provider is expanding the URL (playlist -> tracks)
    DOWNLOADING = "downloading"
    CONVERTING = "converting"  # ffmpeg / tagging step
    DONE = "done"
    SKIPPED = "skipped"  # already in the library
    ERROR = "error"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.DONE,
            JobStatus.SKIPPED,
            JobStatus.ERROR,
            JobStatus.CANCELLED,
        }


@dataclass
class ProgressEvent:
    """Emitted by providers while downloading. All fields optional except status."""

    status: JobStatus
    progress: float | None = None  # 0.0 .. 1.0
    speed: float | None = None  # bytes / second
    eta: float | None = None  # seconds
    message: str | None = None


@dataclass
class Job:
    """One unit of work in the queue. A playlist URL becomes a parent job plus one child per track."""

    url: str
    provider: str | None = None
    id: str = field(default_factory=new_id)
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    speed: float | None = None
    eta: float | None = None
    message: str | None = None
    error: str | None = None
    parent_id: str | None = None
    child_count: int = 0
    track_ref: TrackRef | None = None
    track: Track | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @property
    def title(self) -> str:
        if self.track:
            return f"{self.track.artist} - {self.track.title}"
        if self.track_ref and self.track_ref.title:
            return self.track_ref.title
        return self.url

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "provider": self.provider,
            "status": self.status.value,
            "progress": self.progress,
            "speed": self.speed,
            "eta": self.eta,
            "message": self.message,
            "error": self.error,
            "parent_id": self.parent_id,
            "child_count": self.child_count,
            "title": self.title,
            "track_ref": asdict(self.track_ref) if self.track_ref else None,
            "track": self.track.to_dict() if self.track else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Playlist:
    name: str
    id: str = field(default_factory=new_id)
    track_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Playlist:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
