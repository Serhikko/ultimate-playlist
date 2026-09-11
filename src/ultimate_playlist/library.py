"""Index of finished tracks: a JSON file, tag reading via mutagen, rescan of the library folder."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Track, utc_now_iso

log = logging.getLogger(__name__)

INDEX_VERSION = 1
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".flac"}
SKIP_DIRS = {".incoming", "Playlists"}
TAG_ID_KEY = "ULTIMATE_PLAYLIST_ID"
LOCAL_PROVIDER = "local"
UNKNOWN_ARTIST = "Unknown Artist"


class LibraryError(OSError):
    """Base class for library problems that callers should show to the user."""


class LibraryUnavailable(LibraryError):
    """The library folder cannot be reached right now (unplugged drive, renamed folder)."""

    def __init__(self, library_dir: Path) -> None:
        super().__init__(
            f"The library folder {library_dir} is not reachable right now. "
            "Plug the drive in (or fix the folder in the settings) and try again."
        )
        self.library_dir = Path(library_dir)


class LibraryFileInUse(LibraryError):
    """The audio file could not be deleted (on Windows: it is open, e.g. still playing)."""

    def __init__(self, path: Path, cause: OSError) -> None:
        super().__init__(
            f"{path.name} could not be deleted; it is probably still in use (playing?). "
            f"Stop playback and try again. ({cause})"
        )
        self.path = Path(path)


def local_track_id(rel_path: str) -> str:
    """Id for a file that was not downloaded by us: 'local:<sha1 of relative posix path>[:12]'."""
    digest = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:12]
    return f"{LOCAL_PROVIDER}:{digest}"


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON to `path` via a sibling .tmp file and os.replace, so a crash never truncates it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# -- tag reading -------------------------------------------------------------------------------


@dataclass
class TagInfo:
    """What we can learn about an audio file from its tags. Every field is optional."""

    track_id: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    duration: float | None = None
    has_cover: bool = False


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value).strip()
    return text or None


def _id3_text(tags: Any, key: str) -> str | None:
    frame = tags.get(key)
    text = getattr(frame, "text", None)
    return _clean(text[0]) if text else None


def _read_id3(tags: Any, info: TagInfo) -> None:
    info.title = _id3_text(tags, "TIT2")
    info.artist = _id3_text(tags, "TPE1")
    info.album = _id3_text(tags, "TALB")
    for frame in tags.getall("TXXX"):
        if getattr(frame, "desc", None) == TAG_ID_KEY and frame.text:
            info.track_id = _clean(frame.text[0])
            break
    info.has_cover = bool(tags.getall("APIC"))


def _mp4_text(tags: Any, key: str) -> str | None:
    values = tags.get(key)
    return _clean(values[0]) if values else None


def _read_mp4(tags: Any, info: TagInfo) -> None:
    info.title = _mp4_text(tags, "\xa9nam")
    info.artist = _mp4_text(tags, "\xa9ART")
    info.album = _mp4_text(tags, "\xa9alb")
    info.track_id = _mp4_text(tags, f"----:com.apple.iTunes:{TAG_ID_KEY}")
    info.has_cover = bool(tags.get("covr"))


def _vorbis_text(tags: Any, key: str) -> str | None:
    try:
        values = tags.get(key)
    except Exception:  # noqa: BLE001 - odd comment blocks are not worth a crash
        values = None
    return _clean(values[0]) if values else None


def _read_vorbis(audio: Any, tags: Any, info: TagInfo) -> None:
    info.title = _vorbis_text(tags, "title")
    info.artist = _vorbis_text(tags, "artist")
    info.album = _vorbis_text(tags, "album")
    info.track_id = _vorbis_text(tags, TAG_ID_KEY)
    pictures = getattr(audio, "pictures", None)
    info.has_cover = bool(pictures) or _vorbis_text(tags, "metadata_block_picture") is not None


def read_tags(path: Path) -> TagInfo:
    """Best-effort read of title/artist/album/duration/cover/our id. Never raises."""
    info = TagInfo()
    try:
        import mutagen
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4Tags

        audio = mutagen.File(path)
        if audio is None:
            return info
        length = getattr(getattr(audio, "info", None), "length", None)
        if length:
            info.duration = float(length)
        tags = audio.tags
        if tags is None:
            return info
        if isinstance(tags, ID3):
            _read_id3(tags, info)
        elif isinstance(tags, MP4Tags):
            _read_mp4(tags, info)
        else:  # FLAC / Ogg Opus: Vorbis comments
            _read_vorbis(audio, tags, info)
    except Exception as exc:  # noqa: BLE001 - a broken file must not stop a rescan
        log.warning("Could not read tags from %s: %s", path, exc)
    return info


_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def image_mime(declared: Any, data: bytes) -> str:
    """The cover's MIME type, trusting the tag only when it names an image.

    The value stored in a tag is attacker-controlled for files that were copied into the library
    folder, and the cover endpoint serves it verbatim: a `text/html` cover would run as a page on
    the app's own origin. Anything that is not `image/*` is replaced by what the bytes look like.
    """
    text = (
        declared.decode("ascii", "replace") if isinstance(declared, bytes) else str(declared or "")
    )
    text = text.strip().lower()
    if text.startswith("image/") and not any(c in text for c in " ;,\r\n"):
        return text
    for magic, mime in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def read_cover(path: Path) -> tuple[bytes, str] | None:
    """Embedded cover art as (bytes, mime) from ID3 APIC, MP4 covr or FLAC/Opus pictures.

    The MIME type is always `image/*` (see `image_mime`)."""
    try:
        import mutagen
        from mutagen.flac import Picture
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4Cover, MP4Tags

        audio = mutagen.File(path)
        if audio is None or audio.tags is None:
            return None
        tags = audio.tags
        if isinstance(tags, ID3):
            for apic in tags.getall("APIC"):
                if apic.data:
                    data = bytes(apic.data)
                    return data, image_mime(apic.mime, data)
            return None
        if isinstance(tags, MP4Tags):
            for cover in tags.get("covr") or []:
                fmt = getattr(cover, "imageformat", MP4Cover.FORMAT_JPEG)
                mime = "image/png" if fmt == MP4Cover.FORMAT_PNG else "image/jpeg"
                return bytes(cover), mime
            return None
        for pic in getattr(audio, "pictures", None) or []:
            if pic.data:
                data = bytes(pic.data)
                return data, image_mime(pic.mime, data)
        for raw in tags.get("metadata_block_picture") or []:
            pic = Picture(base64.b64decode(raw))
            if pic.data:
                data = bytes(pic.data)
                return data, image_mime(pic.mime, data)
    except Exception as exc:  # noqa: BLE001
        log.debug("No cover readable from %s: %s", path, exc)
    return None


def _norm_rel(rel: str) -> str:
    """Key used to compare relative paths (Windows file systems are case-insensitive)."""
    return rel.casefold() if os.name == "nt" else rel


def _normalise_entry(track: Track) -> Track:
    """Repair a hand-edited index entry so no later query trips over a null/odd field."""
    if not isinstance(track.id, str) or not track.id:
        raise ValueError("entry has no id")
    if not isinstance(track.path, str) or not track.path:
        raise ValueError("entry has no path")
    track.title = str(track.title or Path(track.path).stem)
    track.artist = str(track.artist or UNKNOWN_ARTIST)
    track.album = (str(track.album) or None) if track.album is not None else None
    # all() sorts on added_at and the M3U8 export casts duration: neither may be null/odd.
    track.added_at = str(track.added_at) if track.added_at else utc_now_iso()
    try:
        track.duration = float(track.duration) if track.duration is not None else None
    except (TypeError, ValueError):
        track.duration = None
    try:
        track.file_size = int(track.file_size or 0)
    except (TypeError, ValueError):
        track.file_size = 0
    track.has_cover = bool(track.has_cover)
    return track


# -- the library -------------------------------------------------------------------------------


class Library:
    """All finished tracks. Thread-safe; every save is atomic."""

    def __init__(self, library_dir: Path, index_path: Path) -> None:
        self.library_dir = Path(library_dir)
        self.index_path = Path(index_path)
        self._lock = threading.RLock()
        self._tracks: dict[str, Track] = {}
        self.load()

    # -- persistence -------------------------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            self._tracks = {}
            if not self.index_path.exists():
                return
            try:
                raw = json.loads(self.index_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or not isinstance(raw.get("tracks"), dict):
                    raise ValueError("expected an object with a 'tracks' map")
            except Exception as exc:  # noqa: BLE001 - a corrupt index must never take the app down
                backup = self._backup_corrupt()
                log.warning(
                    "The library index %s could not be read (%s). Starting with an empty index; "
                    "the old file was kept as %s. Use rescan to pick your files up again.",
                    self.index_path,
                    exc,
                    backup,
                )
                return
            for key, entry in raw["tracks"].items():
                try:
                    track = _normalise_entry(Track.from_dict(entry))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Skipping unreadable library entry %s: %s", key, exc)
                    continue
                self._tracks[track.id] = track

    def _backup_corrupt(self) -> Path | None:
        backup = self.index_path.with_name(self.index_path.name + ".corrupt")
        try:
            os.replace(self.index_path, backup)
            return backup
        except OSError as exc:
            log.warning("Could not move the corrupt index aside: %s", exc)
            return None

    def save(self) -> None:
        with self._lock:
            data = {
                "version": INDEX_VERSION,
                "tracks": {tid: track.to_dict() for tid, track in self._tracks.items()},
            }
            atomic_write_json(self.index_path, data)

    # -- queries -----------------------------------------------------------------------------

    def all(self) -> list[Track]:
        with self._lock:
            tracks = list(self._tracks.values())
        return sorted(tracks, key=lambda t: str(t.added_at or ""), reverse=True)

    def __len__(self) -> int:
        with self._lock:
            return len(self._tracks)

    def get(self, track_id: str) -> Track | None:
        with self._lock:
            return self._tracks.get(track_id)

    def has(self, track_id: str) -> bool:
        """True only if the index knows the id AND the file is still on disk."""
        track = self.get(track_id)
        return track is not None and self.abs_path(track).is_file()

    def abs_path(self, track: Track | str) -> Path:
        if isinstance(track, str):
            found = self.get(track)
            if found is None:
                raise KeyError(track)
            track = found
        return self.library_dir / Path(*track.path.split("/"))

    def search(self, q: str) -> list[Track]:
        needle = (q or "").strip().casefold()
        if not needle:
            return self.all()
        return [
            t
            for t in self.all()
            if needle in (t.title or "").casefold()
            or needle in (t.artist or "").casefold()
            or needle in (t.album or "").casefold()
        ]

    def cover_bytes(self, track_id: str) -> tuple[bytes, str] | None:
        track = self.get(track_id)
        if track is None:
            return None
        path = self.abs_path(track)
        if not path.is_file():
            return None
        return read_cover(path)

    # -- mutations ---------------------------------------------------------------------------

    def add(self, track: Track) -> None:
        with self._lock:
            self._tracks[track.id] = track
            self.save()

    def remove(self, track_id: str, delete_file: bool = False) -> bool:
        """Drop the index entry (and the file when asked). Unknown id -> False.

        When the file cannot be deleted the entry is kept and LibraryFileInUse is raised, so a
        track never silently reappears at the next rescan after the user was told it was gone.
        """
        with self._lock:
            track = self._tracks.get(track_id)
            if track is None:
                return False
            if delete_file:
                path = self.abs_path(track)
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("Could not delete %s (%s); the entry is kept.", path, exc)
                    raise LibraryFileInUse(path, exc) from exc
            del self._tracks[track_id]
            self.save()
            return True

    def set_library_dir(self, library_dir: Path) -> None:
        """Re-point the library folder (call rescan() afterwards)."""
        with self._lock:
            self.library_dir = Path(library_dir)

    # -- rescan ------------------------------------------------------------------------------

    def rescan(self) -> int:
        """Sync the index with the folder: drop vanished files, pick up new audio files.

        Returns the number of changes (entries added + entries dropped). Raises
        LibraryUnavailable when the folder itself is missing: an unplugged drive must not
        look like "every file vanished" and wipe the index (and, via prune, every playlist).
        """
        with self._lock:
            if not self.library_dir.is_dir():
                log.warning("Library folder %s is not reachable; rescan skipped", self.library_dir)
                raise LibraryUnavailable(self.library_dir)
            changes = 0
            for tid, track in list(self._tracks.items()):
                if not self.abs_path(track).is_file():
                    log.info("Dropping %s from the index: file is gone", track.path)
                    del self._tracks[tid]
                    changes += 1
            known = {_norm_rel(t.path) for t in self._tracks.values()}
            for rel in self._audio_files():
                if _norm_rel(rel) in known:
                    continue
                track = self._track_from_file(rel)
                self._tracks[track.id] = track
                known.add(_norm_rel(rel))
                changes += 1
                log.info("Picked up %s as %s", rel, track.id)
            if changes:
                self.save()
            return changes

    def _audio_files(self) -> list[str]:
        root = self.library_dir
        if not root.is_dir():
            return []
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            found.extend(
                (Path(dirpath) / name).relative_to(root).as_posix()
                for name in sorted(filenames)
                if Path(name).suffix.lower() in AUDIO_EXTENSIONS
            )
        return found

    def _track_from_file(self, rel: str) -> Track:
        path = self.library_dir / Path(*rel.split("/"))
        info = read_tags(path)
        track_id = info.track_id
        if track_id and ":" not in track_id:
            track_id = f"{LOCAL_PROVIDER}:{track_id}"
        if not track_id or track_id in self._tracks:
            track_id = local_track_id(rel)
        provider, _, source_id = track_id.partition(":")

        artist, title = info.artist, info.title
        stem = Path(rel).stem
        left, sep, right = stem.partition(" - ")
        if sep and left.strip() and right.strip():
            artist = artist or left.strip()
            title = title or right.strip()
        title = title or stem
        artist = artist or UNKNOWN_ARTIST

        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return Track(
            id=track_id,
            provider=provider,
            source_id=source_id,
            source_url="",
            title=title,
            artist=artist,
            path=rel,
            album=info.album,
            duration=info.duration,
            has_cover=info.has_cover,
            file_size=size,
        )
