"""Playlists (ordered lists of track ids) persisted as JSON, plus M3U8 export."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from .library import Library, atomic_write_json
from .models import Playlist, Track, utc_now_iso

log = logging.getLogger(__name__)

STORE_VERSION = 1


class PlaylistNotFound(KeyError):
    """Raised for an unknown playlist id. A KeyError, so callers can map it to 404."""

    def __init__(self, pid: str) -> None:
        super().__init__(pid)
        self.pid = pid

    def __str__(self) -> str:
        return f"Playlist not found: {self.pid}"


class PlaylistStore:
    """All playlists, in creation order. Thread-safe; every save is atomic."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._playlists: dict[str, Playlist] = {}
        self.load()

    # -- persistence -------------------------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            self._playlists = {}
            if not self.path.exists():
                return
            try:
                import json

                raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or not isinstance(raw.get("playlists"), list):
                    raise ValueError("expected an object with a 'playlists' list")
            except Exception as exc:  # noqa: BLE001 - never take the app down over a bad file
                backup = self.path.with_name(self.path.name + ".corrupt")
                try:
                    os.replace(self.path, backup)
                except OSError:
                    backup = None  # type: ignore[assignment]
                log.warning(
                    "The playlists file %s could not be read (%s). Starting empty; the old file "
                    "was kept as %s.",
                    self.path,
                    exc,
                    backup,
                )
                return
            for entry in raw["playlists"]:
                try:
                    playlist = Playlist.from_dict(entry)
                    playlist.track_ids = [str(t) for t in playlist.track_ids]
                except Exception as exc:  # noqa: BLE001
                    log.warning("Skipping unreadable playlist entry: %s", exc)
                    continue
                self._playlists[playlist.id] = playlist

    def save(self) -> None:
        with self._lock:
            data = {
                "version": STORE_VERSION,
                "playlists": [p.to_dict() for p in self._playlists.values()],
            }
            atomic_write_json(self.path, data)

    # -- queries -----------------------------------------------------------------------------

    def all(self) -> list[Playlist]:
        with self._lock:
            return list(self._playlists.values())

    def get(self, pid: str) -> Playlist | None:
        with self._lock:
            return self._playlists.get(pid)

    def _require(self, pid: str) -> Playlist:
        playlist = self._playlists.get(pid)
        if playlist is None:
            raise PlaylistNotFound(pid)
        return playlist

    # -- mutations ---------------------------------------------------------------------------

    @staticmethod
    def clean_name(name: str) -> str:
        """The stripped name, or ValueError with the user-facing message when it is blank."""
        cleaned = (name or "").strip()
        if not cleaned:
            raise ValueError("Give the playlist a name.")
        return cleaned

    def create(self, name: str) -> Playlist:
        playlist = Playlist(name=self.clean_name(name))
        with self._lock:
            self._playlists[playlist.id] = playlist
            self.save()
        return playlist

    def rename(self, pid: str, name: str) -> Playlist:
        cleaned = self.clean_name(name)
        with self._lock:
            playlist = self._require(pid)
            playlist.name = cleaned
            playlist.updated_at = utc_now_iso()
            self.save()
            return playlist

    def delete(self, pid: str) -> bool:
        with self._lock:
            if pid not in self._playlists:
                return False
            del self._playlists[pid]
            self.save()
            return True

    def add_tracks(self, pid: str, track_ids: list[str]) -> Playlist:
        """Append ids that are not already in the playlist (order kept, duplicates dropped)."""
        with self._lock:
            playlist = self._require(pid)
            present = set(playlist.track_ids)
            added = False
            for tid in track_ids:
                if tid in present:
                    continue
                playlist.track_ids.append(tid)
                present.add(tid)
                added = True
            if added:
                playlist.updated_at = utc_now_iso()
                self.save()
            return playlist

    def remove_track(self, pid: str, track_id: str) -> Playlist:
        with self._lock:
            playlist = self._require(pid)
            if track_id in playlist.track_ids:
                playlist.track_ids = [t for t in playlist.track_ids if t != track_id]
                playlist.updated_at = utc_now_iso()
                self.save()
            return playlist

    def set_order(self, pid: str, track_ids: list[str]) -> Playlist:
        """Replace the order. `track_ids` must be a permutation of the current ids."""
        with self._lock:
            playlist = self._require(pid)
            new_ids = list(track_ids)
            if sorted(new_ids) != sorted(playlist.track_ids):
                raise ValueError(
                    "The new order must contain exactly the tracks already in the playlist."
                )
            if new_ids != playlist.track_ids:
                playlist.track_ids = new_ids
                playlist.updated_at = utc_now_iso()
                self.save()
            return playlist

    def prune(self, existing_ids: set[str]) -> int:
        """Drop track ids that no longer exist in the library. Returns how many were removed."""
        with self._lock:
            removed = 0
            for playlist in self._playlists.values():
                kept = [t for t in playlist.track_ids if t in existing_ids]
                if len(kept) != len(playlist.track_ids):
                    removed += len(playlist.track_ids) - len(kept)
                    playlist.track_ids = kept
                    playlist.updated_at = utc_now_iso()
            if removed:
                self.save()
            return removed


# -- M3U8 export -------------------------------------------------------------------------------


def extinf_line(track: Track) -> str:
    try:
        seconds = int(float(track.duration)) if track.duration is not None else -1
    except (TypeError, ValueError):  # a hand-edited index entry must not break the export
        seconds = -1
    return f"#EXTINF:{seconds},{track.artist} - {track.title}"


def _entry_path(abs_path: Path, relative_to: Path | None) -> str:
    if relative_to is None:
        return str(abs_path)
    try:
        return Path(os.path.relpath(abs_path, relative_to)).as_posix()
    except ValueError:  # different drive on Windows: no relative form exists
        return str(abs_path)


def m3u8_text(playlist: Playlist, library: Library, relative_to: Path | None = None) -> str:
    """The extended M3U8 content ("\\n" line ends). Tracks missing from the library are skipped.

    Paths are relative to `relative_to` (posix separators) when given, else absolute OS paths.
    """
    lines = ["#EXTM3U"]
    for tid in playlist.track_ids:
        track = library.get(tid)
        if track is None:
            log.info("Skipping %s in playlist %r: not in the library", tid, playlist.name)
            continue
        lines.append(extinf_line(track))
        lines.append(_entry_path(library.abs_path(track), relative_to))
    return "\n".join(lines) + "\n"


def export_m3u8(
    playlist: Playlist,
    library: Library,
    out_path: Path,
    relative_to: Path | None = None,
) -> Path:
    """Write an extended M3U8 (UTF-8) atomically; see `m3u8_text` for the content."""
    out_path = Path(out_path)
    text = m3u8_text(playlist, library, relative_to)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, out_path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return out_path
