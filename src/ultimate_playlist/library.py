"""Index of finished tracks: a JSON file, tag reading via mutagen, rescan of the library folder."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import ntpath
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Track, utc_now_iso

log = logging.getLogger(__name__)

INDEX_VERSION = 1
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".flac"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".mkv", ".mov"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
SKIP_DIRS = {".incoming", "Playlists"}
TAG_ID_KEY = "ULTIMATE_PLAYLIST_ID"
LOCAL_PROVIDER = "local"
UNKNOWN_ARTIST = "Unknown Artist"
FOLDER_NAME_MAX = 80
UNSORTED = ""  # the folder name of a track that sits in the library folder itself
_ILLEGAL_FOLDER_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
# A file the player is streaming is open for a moment on every range request; on Windows that
# blocks a rename. A short retry lets a move slip in between two requests.
MOVE_RETRIES = 5
MOVE_RETRY_DELAY = 0.2  # seconds


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


class LibraryFolderError(ValueError):
    """A folder name that cannot be used, or a folder that does not exist / already exists.
    The message is meant for the user (the API answers 400 with it)."""


@dataclass
class MoveResult:
    """What `Library.move_tracks` did: ids now in the target folder (or already there), and
    (id, reason) for every track that could not be moved."""

    moved: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    note: str | None = None  # delete_folder: why the folder itself stayed on disk


_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400  # a junction or a symbolic link


def folder_key(name: str) -> str:
    """How the file system compares two names: Windows ignores case one character at a time
    ("Flight" is "flight", but "Straße" is not "Strasse", which str.casefold() would merge).
    Elsewhere the names are lowercased the same way."""
    return ntpath.normcase(name) if os.name == "nt" else name.lower()


_SKIP_DIRS_KEYS = frozenset(folder_key(d) for d in SKIP_DIRS)
# Folders that trash cans, sync tools, NAS boxes and file systems put next to the music; their
# copies would come back as duplicates.
_TOOL_DIRS_KEYS = frozenset(
    folder_key(d)
    for d in (
        "$RECYCLE.BIN",
        "System Volume Information",
        ".Trash",
        ".Trashes",
        ".stversions",
        ".stfolder",
        ".Spotlight-V100",
        ".fseventsd",
        ".TemporaryItems",
        ".DocumentRevisions-V100",
        ".git",
        ".thumbnails",
        ".sync",
        ".dropbox.cache",
        "@eaDir",
        "#recycle",
        "#snapshot",
        "@Recycle",
        ".@__thumb",
    )
)


def _trash_name(name: str) -> bool:
    """The app's own folders and the known trash / sync / system ones, by name. Tracks an
    older version indexed inside one are dropped by rescan (nothing else ever is, by folder)."""
    key = folder_key(name)
    return key in _SKIP_DIRS_KEYS or key in _TOOL_DIRS_KEYS or key.startswith(".trash-")


def _skipped_name(name: str) -> bool:
    """Is a directory of this name never walked or listed, whatever its attributes?

    _trash_name, and outside Windows a dot followed by a letter (".cache"). A dot on its own
    hides nothing: "...And Justice for All" and ".38 Special" are music."""
    if _trash_name(name):
        return True
    return os.name != "nt" and len(name) > 1 and name[0] == "." and name[1].isalpha()


def _skipped_dir(name: str, path: os.DirEntry[str] | Path) -> bool:
    """Directories rescan does not walk and the folder list leaves out: see _skipped_name,
    plus on Windows a directory that is both hidden and system, as the folders Windows
    protects are ("$Recycle.Bin", "System Volume Information" at a drive's root). Hidden
    alone is not enough: a folder hidden in Explorer, or by a NAS for its leading dot, is
    still the user's music."""
    if _skipped_name(name):
        return True
    try:
        if isinstance(path, os.DirEntry):
            info = path.stat(follow_symlinks=False)
        else:
            info = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    protected = _FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM
    return getattr(info, "st_file_attributes", 0) & protected == protected


def _is_link(path: str | Path) -> bool:
    """A symbolic link or (Windows) a junction: removing empty folders must never step into
    one, which can point anywhere on the drive. True when it cannot be checked."""
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _remove_empty_tree(top: Path) -> None:
    """Remove `top` and the empty directories under it, deepest first. Links and junctions
    are neither followed nor removed, so a folder holding one stays."""
    if _is_link(top):
        return
    found: list[str] = []
    for dirpath, dirnames, _filenames in os.walk(top):
        found.append(dirpath)
        dirnames[:] = [d for d in dirnames if not _is_link(os.path.join(dirpath, d))]
    for dirpath in reversed(found):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass  # not empty: something the library does not know about


def media_of(rel_path: str) -> str:
    """ "video" for a video container (by extension), else "audio"."""
    return "video" if Path(rel_path).suffix.lower() in VIDEO_EXTENSIONS else "audio"


def folder_of(rel_path: str) -> str:
    """The top-level folder a track's file sits in ("" = the library folder itself)."""
    head, sep, _rest = (rel_path or "").partition("/")
    return head if sep else UNSORTED


def clean_folder_name(name: str | None) -> str:
    """A folder name made safe to create directly under the library folder.

    "" (or only spaces) means Unsorted, the library folder itself. Raises LibraryFolderError
    for what Windows (or the library) cannot hold: separators and other illegal characters,
    control characters, a reserved device name, a leading dot, a trailing dot or space, the
    app's own folders ("Playlists", ".incoming") and names over 80 characters.
    """
    text = " ".join((name or "").split())
    if not text:
        return UNSORTED
    if any(ch in _ILLEGAL_FOLDER_CHARS or ord(ch) < 32 for ch in text):
        raise LibraryFolderError(
            "A folder name can't contain any of these characters: < > : \" / \\ | ? *"
        )
    if text.startswith(".") or text.endswith(".") or text in {".", ".."}:
        raise LibraryFolderError("A folder name can't start or end with a dot.")
    if text.split(".", 1)[0].strip().upper() in _WINDOWS_RESERVED:
        raise LibraryFolderError(f'"{text}" is a name Windows reserves; choose another one.')
    if text.casefold() in {d.casefold() for d in SKIP_DIRS}:
        raise LibraryFolderError(f'"{text}" is used by Ultimate Playlist itself; choose another.')
    if _skipped_name(text):  # "$RECYCLE.BIN", "@eaDir": its tracks would never be listed
        raise LibraryFolderError(f'"{text}" is a name Windows or a sync tool uses; choose another.')
    if len(text) > FOLDER_NAME_MAX:
        raise LibraryFolderError(f"A folder name can be at most {FOLDER_NAME_MAX} characters.")
    return text


def _free_path(path: Path, taken: set[str]) -> Path:
    """`path`, or "name (2).ext", "name (3).ext"... when the name is used on disk or in `taken`
    (folder_key of the names already claimed by this batch)."""
    candidate = path
    counter = 2
    while candidate.exists() or folder_key(candidate.name) in taken:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        counter += 1
    return candidate


def _move_file(source: Path, target: Path) -> None:
    """os.replace with a few retries for a file that is open for a moment (see MOVE_RETRIES)."""
    for attempt in range(MOVE_RETRIES):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == MOVE_RETRIES - 1:
                raise
            time.sleep(MOVE_RETRY_DELAY)


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
    """Key used to compare relative paths (Windows file systems ignore case; see folder_key)."""
    return ntpath.normcase(rel) if os.name == "nt" else rel


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
        # Folders renamed or removed while the app runs: folder_key(old name) -> its new name
        # ("" = removed). A download that started with the old name follows it (move_tracks).
        self._moved_folders: dict[str, str] = {}
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
            self._moved_folders.clear()

    # -- folders -----------------------------------------------------------------------------
    #
    # A folder is a real directory directly under the library folder; a track's folder is the
    # first part of its relative path, and "" (Unsorted) is the library folder itself. Moving
    # a track moves its file, so a folder can be copied to a phone or a USB stick as it is.
    # Track ids live in the files' tags, so playlists and a rescan follow a moved file.

    def folders(self) -> list[str]:
        """Every folder: the directories directly under the library folder (not the app's own,
        not trash or sync-tool folders, nor folders Windows protects: see _skipped_dir), plus
        any folder a track's path names. A name on disk wins over another spelling in the
        index. Sorted case-insensitively."""
        names: dict[str, str] = {}
        with self._lock:
            for track in self._tracks.values():
                folder = folder_of(track.path)
                if folder:
                    names.setdefault(folder_key(folder), folder)
            root = self.library_dir
        # A folder the index names is listed as long as it holds tracks (rescan drops only
        # what sits in a trash folder), so the list and the counts always agree.
        names = {k: v for k, v in names.items() if not _trash_name(v)}
        try:
            entries = list(os.scandir(root))
        except OSError:
            entries = []
        for entry in entries:
            try:
                if entry.is_dir() and not _skipped_dir(entry.name, entry):
                    names[folder_key(entry.name)] = entry.name
            except OSError:
                continue
        return sorted(names.values(), key=str.casefold)

    def canonical_folders(self) -> dict[str, str]:
        """folder_key -> the folder's name as `folders()` lists it (Windows treats "Flight" and
        "flight" as one folder; the index may hold both spellings)."""
        return {folder_key(name): name for name in self.folders()}

    def folder_name_of(self, rel_path: str, canonical: dict[str, str] | None = None) -> str:
        """The folder a track is in, spelled as the folder list spells it ("" = Unsorted)."""
        folder = folder_of(rel_path)
        if not folder:
            return UNSORTED
        canonical = canonical if canonical is not None else self.canonical_folders()
        return canonical.get(folder_key(folder), folder)

    def folder_counts(self) -> dict[str, int]:
        """Tracks per folder ("" = Unsorted), keyed by the names `folders()` lists."""
        canonical = self.canonical_folders()
        counts: dict[str, int] = {}
        with self._lock:
            for track in self._tracks.values():
                folder = self.folder_name_of(track.path, canonical)
                counts[folder] = counts.get(folder, 0) + 1
        return counts

    def _existing_folder(self, name: str) -> str | None:
        """An existing folder by the name the user picked: exactly as listed, else the same
        name in another case or with its spaces tidied. Never a path ("..", "a/b")."""
        if not name or "/" in name or "\\" in name or name.strip() in {".", ".."}:
            return None
        listed = self.folders()
        if name in listed:
            return name
        for candidate in (name, " ".join(name.split())):
            wanted = folder_key(candidate)
            match = next((f for f in listed if folder_key(f) == wanted), None)
            if match is not None:
                return match
        return None

    def _require_library_dir(self) -> None:
        if not self.library_dir.is_dir():
            raise LibraryUnavailable(self.library_dir)

    def create_folder(self, name: str) -> str:
        """Create a folder (an existing one is fine); returns its name as listed.

        On a fresh install (no track yet) the library folder itself is created too; with
        tracks in the index a missing library folder is an unplugged drive or a moved folder,
        and recreating it empty would make the next rescan drop every track.
        """
        with self._lock:
            if not self.library_dir.is_dir():
                if self._tracks:
                    raise LibraryUnavailable(self.library_dir)
                try:
                    self.library_dir.mkdir(exist_ok=True)  # not parents: never invent a drive
                except OSError as exc:
                    raise LibraryUnavailable(self.library_dir) from exc
            existing = self._existing_folder(name)
            if existing is not None:
                (self.library_dir / existing).mkdir(exist_ok=True)  # one the index names
                self._forget_moved(existing)
                return existing
            clean = clean_folder_name(name)
            if not clean:
                raise LibraryFolderError("Give the folder a name.")
            try:
                (self.library_dir / clean).mkdir(exist_ok=True)
            except OSError as exc:
                raise LibraryFolderError(
                    f'Could not create the folder "{clean}": {exc.strerror or exc}'
                ) from exc
            self._forget_moved(clean)
            return clean

    def resolve_folder(self, folder: str) -> str:
        """Where a move goes: an existing folder by its listed name (whatever its spelling or
        length, it already exists), else a new name that passes clean_folder_name."""
        if not (folder or "").strip():
            return UNSORTED
        return self._existing_folder(folder) or clean_folder_name(folder)

    def pick_folder(self, folder: str) -> str:
        """resolve_folder for a folder the user picks for new downloads.

        An existing folder (kept on disk by a desktop.ini after a removal, made again in
        Explorer) is the one picked: from now on downloads naming it go to it, whatever was
        renamed or removed under that name before. A name that is not a folder now may be a
        stale one (a second tab, a link pasted while the rename ran): a renamed folder gives
        its new name, and a removed one stays removed (move_tracks' follow_renames then
        keeps the file in Unsorted with a message) instead of being made again.
        """
        if not (folder or "").strip():
            return UNSORTED
        with self._lock:
            existing = self._existing_folder(folder)
            if existing is not None:
                self._forget_moved(existing)
                return existing
            name = clean_folder_name(folder)
            return self._follow_moves(name) or name

    def move_tracks(
        self, track_ids: list[str], folder: str, *, follow_renames: bool = False
    ) -> MoveResult:
        """Move tracks' files into `folder` ("" = Unsorted), creating it when needed.

        A name already used in the target gets " (2)", " (3)"..., unless the file there is a
        stale copy of the very track that moves (same id in its tags, not in the index: a
        re-download after "Remove from library"), which is replaced. A track already there
        counts as moved. A file that cannot be moved (missing, in use) is reported in
        `failed` and keeps its place; the others move anyway.

        follow_renames=True is for a download that named its folder when it started: a folder
        renamed since is followed to its new name, and one removed since is not made again
        (every track fails, so the file stays in Unsorted), also when other files kept it on
        disk. The lookup happens under the same lock as the rename or removal, so a download
        finishing meanwhile cannot slip in. Picking the name again for new downloads
        (pick_folder), making it or moving tracks into it ends that.
        """
        result = MoveResult()
        with self._lock:
            self._require_library_dir()
            if follow_renames and folder:
                current = self._follow_moves(folder)
                if current is None:
                    reason = f'The folder "{folder}" was removed.'
                    result.failed = [(track_id, reason) for track_id in dict.fromkeys(track_ids)]
                    return result
                folder = current
            target_name = self.resolve_folder(folder)
            target_dir = self.library_dir / target_name if target_name else self.library_dir
            self._forget_moved(target_name)
            indexed = {_norm_rel(t.path) for t in self._tracks.values()}
            taken: set[str] = set()
            changed = False
            for track_id in dict.fromkeys(track_ids):
                track = self._tracks.get(track_id)
                if track is None:
                    result.failed.append((track_id, "Not in the library."))
                    continue
                if folder_key(folder_of(track.path)) == folder_key(target_name):
                    result.moved.append(track_id)
                    continue
                source = self.abs_path(track)
                if not source.is_file():
                    result.failed.append((track_id, f"{source.name} is missing from the folder."))
                    continue
                try:
                    target_dir.mkdir(exist_ok=True)
                    target = target_dir / source.name
                    if not self._stale_copy(target, track.id, indexed, taken):
                        target = _free_path(target, taken)
                    _move_file(source, target)
                except OSError as exc:
                    log.warning("Could not move %s to %s: %s", source, target_dir, exc)
                    reason = (
                        f"{source.name} is in use (playing?). Stop playback and try again."
                        if isinstance(exc, PermissionError)
                        else f"Could not move {source.name}: {exc.strerror or exc}"
                    )
                    result.failed.append((track_id, reason))
                    continue
                taken.add(folder_key(target.name))
                indexed.discard(_norm_rel(track.path))
                track.path = target.relative_to(self.library_dir).as_posix()
                indexed.add(_norm_rel(track.path))
                result.moved.append(track_id)
                changed = True
            if changed:
                self.save()
        return result

    def _forget_moved(self, name: str) -> None:
        """A folder of this name is wanted again (made, moved into, renamed to): downloads
        that name it go there, not where an earlier folder of that name went."""
        if name:
            self._moved_folders.pop(folder_key(name), None)

    def _follow_moves(self, name: str) -> str | None:
        """The current name of folder `name` after the renames made since (`name` itself when
        none was), or None when it was removed."""
        seen: set[str] = set()
        key = folder_key(name)
        while key in self._moved_folders and key not in seen:
            seen.add(key)
            name = self._moved_folders[key]
            if not name:
                return None
            key = folder_key(name)
        return name

    def _stale_copy(self, target: Path, track_id: str, indexed: set[str], taken: set[str]) -> bool:
        """Is `target` a file of this very track that the index has forgotten?"""
        if folder_key(target.name) in taken or not target.is_file():
            return False
        rel = target.relative_to(self.library_dir).as_posix()
        if _norm_rel(rel) in indexed:
            return False
        return read_tags(target).track_id == track_id

    def rename_folder(self, old: str, new: str) -> str:
        """Rename a folder on disk and in the index; returns the new name. Tracks whose path
        names the folder in another case move with it."""
        with self._lock:
            # Before anything else: with the library folder missing, a rename would create it
            # empty, and the next rescan would then drop every track.
            self._require_library_dir()
            current = self._existing_folder(old)
            if current is None:
                raise LibraryFolderError(f'There is no folder "{old}".')
            new_name = clean_folder_name(new)
            if not new_name:
                raise LibraryFolderError("Give the folder a name.")
            clash = self._existing_folder(new_name)
            if clash is not None and folder_key(clash) != folder_key(current):
                raise LibraryFolderError(f'A folder named "{clash}" already exists.')
            if new_name == current:
                return current
            source = self.library_dir / current
            try:
                if source.is_dir():
                    os.rename(source, self.library_dir / new_name)
                else:  # a folder only the index names (its files are gone)
                    (self.library_dir / new_name).mkdir(exist_ok=True)
            except OSError as exc:
                raise LibraryFolderError(
                    f'Could not rename "{current}": {exc.strerror or exc}. If a file in it is '
                    "playing, stop playback and try again."
                ) from exc
            wanted = folder_key(current)
            for track in self._tracks.values():
                folder = folder_of(track.path)
                if folder and folder_key(folder) == wanted:
                    track.path = f"{new_name}{track.path[len(folder) :]}"
            self.save()
            self._forget_moved(new_name)
            if folder_key(new_name) != wanted:  # not a change of case only
                self._moved_folders[wanted] = new_name
            return new_name

    def delete_folder(self, name: str) -> MoveResult:
        """Move the folder's tracks back to Unsorted, then remove the folder and the empty
        subfolders the move left. Files the library does not know about keep it on disk;
        `note` then says so."""
        with self._lock:
            self._require_library_dir()
            current = self._existing_folder(name)
            if current is None:
                raise LibraryFolderError(f'There is no folder "{name}".')
            wanted = folder_key(current)
            inside = [
                t.id for t in self._tracks.values() if folder_key(folder_of(t.path)) == wanted
            ]
            result = self.move_tracks(inside, UNSORTED)
            # Downloads still on their way stay in Unsorted (the API re-targets the queued ones
            # to Unsorted too), even when the folder itself stays below.
            self._moved_folders[wanted] = ""
            folder_dir = self.library_dir / current
            if result.failed or not folder_dir.is_dir():
                return result  # some tracks are still in it, or nothing is on disk
            _remove_empty_tree(folder_dir)
            if os.path.lexists(folder_dir):
                log.info("Folder %s kept on disk: it holds other files", folder_dir)
                result.note = (
                    f'Its tracks are back in Unsorted, but "{current}" holds other files '
                    "(not music the library knows), so the folder stays on disk."
                )
            return result

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
                elif any(_trash_name(part) for part in track.path.split("/")[:-1]):
                    # An older version indexed trash and sync-tool copies ($RECYCLE.BIN when the
                    # library is a drive's root, .stversions); they are not the user's music.
                    # By name only: a folder's attributes never cost the user a track (and
                    # with it its place in every playlist).
                    log.info(
                        "Dropping %s from the index: it is in a trash or system folder", track.path
                    )
                else:
                    continue
                del self._tracks[tid]
                changes += 1
            known = {_norm_rel(t.path) for t in self._tracks.values()}
            for rel in self._media_files():
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

    def _media_files(self) -> list[str]:
        """Audio and video files anywhere under the library folder (folders included)."""
        root = self.library_dir
        if not root.is_dir():
            return []
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            # Not the app's folders, trash or sync-tool folders, nor ones Windows protects
            # (see _skipped_dir): a trash or a sync tool's old versions would come back as
            # duplicates.
            dirnames[:] = sorted(d for d in dirnames if not _skipped_dir(d, Path(dirpath) / d))
            found.extend(
                (Path(dirpath) / name).relative_to(root).as_posix()
                for name in sorted(filenames)
                if Path(name).suffix.lower() in MEDIA_EXTENSIONS
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
            # A track moved away from this path keeps the id this path gives; a new file here
            # must not take it over (it would replace that track in the index and playlists).
            counter = 2
            while track_id in self._tracks:
                track_id = local_track_id(f"{rel}#{counter}")
                counter += 1
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
