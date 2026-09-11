"""Provider interface. Implement this to add a new source (Spotify, SoundCloud, Bandcamp...)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..config import Settings
from ..models import ProgressEvent, Track, TrackRef

ProgressCallback = Callable[[ProgressEvent], None]


class ProviderError(Exception):
    """A user-facing error: the message must be readable by a non-programmer."""


class ProviderNotAvailable(ProviderError):
    """The provider exists but cannot run (missing dependency, missing credentials, not built yet)."""


class DownloadCancelled(ProviderError):
    """Raised inside download() when the cancel event is set."""


@runtime_checkable
class Provider(Protocol):
    """A source of audio.

    Contract:
    - `name` is a short lowercase slug ("youtube"). It is the first half of every Track id.
    - `matches(url)` must be fast and offline (regex on the hostname/path). Return True if this
      provider should own the URL.
    - `resolve(url)` may hit the network. It expands the URL into one TrackRef per track:
      a single video returns a 1-element list, a playlist/album returns one per entry.
      Unavailable entries are dropped, not raised. Raise ProviderError for a bad/unsupported URL.
    - `download(ref, dest_dir, settings, progress, cancel)` produces exactly one finished, tagged
      audio file inside `dest_dir` (the library root) and returns its Track, with `Track.path`
      relative to `dest_dir` using forward slashes. It must:
        * call `progress(...)` at least on status changes (DOWNLOADING -> CONVERTING);
        * check `cancel.is_set()` regularly and raise DownloadCancelled, cleaning up partial files;
        * raise ProviderError with a friendly message on failure (private video, geo-block, ...).
    - `doctor()` returns a list of (ok, label, detail) checks so the UI/CLI can explain what is
      missing and how to fix it. Never raises.
    """

    name: str
    display_name: str

    def matches(self, url: str) -> bool: ...

    def resolve(self, url: str) -> list[TrackRef]: ...

    def download(
        self,
        ref: TrackRef,
        dest_dir: Path,
        settings: Settings,
        progress: ProgressCallback,
        cancel: threading.Event,
    ) -> Track: ...

    def doctor(self) -> list[tuple[bool, str, str]]: ...
