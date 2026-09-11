"""YouTube provider built on yt-dlp.

This is the only module in the project that imports yt-dlp or knows YouTube URL shapes.
The pure helpers (``split_artist_title``, ``clean_title``, ``safe_filename``, ``unique_path``,
``pick_artist_title``, ``friendly_error``, ``_ydl_opts``) never touch the network so they can be
unit-tested offline.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import mutagen
import yt_dlp
from mutagen.flac import FLAC
from mutagen.id3 import COMM, ID3, TALB, TIT2, TPE1, TXXX, ID3FileType, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from ..bundled import bundled_tool, is_bundled, is_frozen
from ..config import Settings
from ..ffmpeg import find_ffmpeg, install_hint, missing_bundled_hint, resolve_explicit
from ..library import read_tags
from ..models import JobStatus, ProgressEvent, Track, TrackRef
from .base import DownloadCancelled, ProgressCallback, ProviderError

log = logging.getLogger(__name__)
# yt-dlp's own chatter is forwarded here so it can be filtered independently of our code.
ydl_log = logging.getLogger("ultimate_playlist.youtube")

YoutubeDL = yt_dlp.YoutubeDL  # module-level alias so tests can swap in a fake

TXXX_ID_DESC = "ULTIMATE_PLAYLIST_ID"
MP4_ID_KEY = f"----:com.apple.iTunes:{TXXX_ID_DESC}"  # what library.read_tags() looks for
# Picking the final file name and moving the file onto it is one critical section: two workers
# finishing "Artist - Title" for two different videos at the same moment must not both see the
# name as free and overwrite each other.
_move_lock = threading.Lock()
DEFAULT_JS_RUNTIMES: tuple[str, ...] = ("deno", "node")
# yt-dlp runtime key -> executable name when they differ (yt_dlp/utils/_jsruntime.py looks for
# `qjs`, not `quickjs`); the others are named after the runtime.
RUNTIME_BINARY: dict[str, str] = {"quickjs": "qjs"}
MAX_FILENAME_LEN = 150
_ERROR_DETAIL_LEN = 200

_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
# Real video ids are 11 characters, but a typo'd id is better rejected by YouTube itself
# ("That link looks incomplete") than by us with a misleading "No provider for this link".
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ID_PATH_RE = re.compile(r"^/(?:shorts|live|embed|v|e|clip)/([^/?#]+)/?$")
# YouTube Music album / playlist pages: music.youtube.com/browse/MPREb_..., /browse/VLPL...
_BROWSE_ID_RE = re.compile(r"^/browse/((?:MPREb_|VL)[A-Za-z0-9_-]+)/?$")
_UNAVAILABLE_TITLES = frozenset({"[Private video]", "[Deleted video]"})

# "Artist - Title", "Artist – Title", "Artist — Title", "Artist | Title"
_SPLIT_RE = re.compile(r"^\s*(.+?)\s+[-–—|]\s+(.+?)\s*$")

# Bracketed junk that YouTube uploaders append to titles: (Official Video), [Official Audio],
# (Lyrics), (Lyric Video), (Audio), (HD), (4K), (Visualizer), (Official Music Video),
# [Official HD Video], (Official 4K Video), [Lyrics/Lyric Video], ...
_JUNK = (
    r"(?:official\s+)?(?:hd\s+|4k\s+)?(?:music\s+|lyrics?\s+)?(?:video|audio|visuali[sz]er)"
    r"(?:\s+(?:hd|4k))?"
    r"|official(?:\s+(?:hd|4k))?"
    r"|lyrics?(?:\s*/\s*lyrics?(?:\s+video)?)?"
    r"|(?:full\s+)?hd|hq|4k|1080p|720p|2160p"
    r"|audio\s+only|hd\s+audio|high\s+quality"
)
# Unanchored on purpose: "(Official Video) (4K Remaster)" must lose the first group only.
_BRACKET_JUNK_RE = re.compile(r"\s*[(\[]\s*(?:" + _JUNK + r")\s*[)\]]", re.IGNORECASE)
_TAIL = r"(?:official\b.*|lyrics?(?:\s+video)?|(?:official\s+)?audio|hd|4k|visuali[sz]er)"
_TRAIL_PIPE_RE = re.compile(r"\s*\|\s*" + _TAIL + r"\s*$", re.IGNORECASE)
_TRAIL_DASH_RE = re.compile(r"\s+[-–—]\s+" + _TAIL + r"\s*$", re.IGNORECASE)
# A "| tail" that reads like a label / channel / format marker rather than part of the title
# ("| Some Label Records", "| Vevo Presents"); "| Live at Wembley" and "Artist | Title" stay.
_LABEL_TAIL_RE = re.compile(
    r"\b(?:official|lyrics?|audio|video|visuali[sz]er|vevo|records|recordings|label|music"
    r"|channel|tv|entertainment|media|productions?|network|hd|hq|4k)\b",
    re.IGNORECASE,
)
_TOPIC_RE = re.compile(r"\s*-\s*Topic\s*$", re.IGNORECASE)

_ILLEGAL_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# Zero-width spaces, BOM, bidi overrides and other invisible format characters that get
# copy-pasted into YouTube titles; they must not end up in file names or tags.
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_HTTP_ERROR_RE = re.compile(r"http error (\d{3})")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_BUG_REPORT_MARKER = "; please report this issue"


# --------------------------------------------------------------------------------------------
# Pure helpers (offline, unit-tested)
# --------------------------------------------------------------------------------------------


def normalize_url(url: str) -> str:
    """Strip whitespace and add https:// when the scheme is missing ("youtu.be/abc")."""
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def split_artist_title(title: str) -> tuple[str, str] | None:
    """Split "Artist - Title" (any dash, or "|") into (artist, title).

    Returns None when there is no separator surrounded by whitespace, when either side is empty,
    or when the left side contains "(" (then it is almost certainly not an artist name).
    """
    match = _SPLIT_RE.match(title or "")
    if not match:
        return None
    left, right = match.group(1).strip(), match.group(2).strip()
    if not left or not right or "(" in left:
        return None
    return left, right


def strip_invisible(text: str) -> str:
    """Drop zero-width / bidi / BOM characters (they render as nothing or reverse the text)."""
    return _INVISIBLE_RE.sub("", text or "")


def clean_title(title: str) -> str:
    """Remove "(Official Video)"-style junk; "feat."/"ft." parts and real brackets are kept.

    Junk bracket groups go wherever they sit after the core title ("Song (Official Video)
    (4K Remaster)" -> "Song (4K Remaster)"), trailing "| Official ..." / "- Lyrics" tails are
    cut, and once nothing else applies a remaining "| ..." segment that reads like a label or
    channel name ("| Some Label Records") is dropped too. A pipe tail that could be part of the
    title, or an artist ("Song | Live at Wembley", "Artist | Title"), is kept. Works repeatedly
    so "Song [Official Audio] (Lyrics)" becomes "Song". May return "" when the whole title was
    junk; callers should fall back to the raw title in that case.
    """
    text = strip_invisible(title).strip()
    while True:
        new = _BRACKET_JUNK_RE.sub("", text)
        new = _TRAIL_PIPE_RE.sub("", new)
        new = _TRAIL_DASH_RE.sub("", new)
        new = new.strip().rstrip("-–—|").strip()
        if new == text and " | " in new:
            head, tail = new.split(" | ", 1)
            if _LABEL_TAIL_RE.search(tail):
                new = head.strip()
        if new == text:
            break
        text = new
    return re.sub(r"\s+", " ", text)


def safe_filename(name: str, max_len: int = MAX_FILENAME_LEN) -> str:
    """Make a Windows-safe file name (no extension handling)."""
    text = re.sub(r"[ \t\r\n\f\v]+", " ", name or "")  # real whitespace -> space first
    text = _ILLEGAL_FILENAME_RE.sub("", text)  # then drop illegal + other control chars
    text = strip_invisible(text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ").strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip(". ").strip()
    if not text:
        text = "untitled"
    if text.split(".", 1)[0].strip().upper() in _WINDOWS_RESERVED:
        text = "_" + text
    return text


def unique_path(path: Path) -> Path:
    """Return `path` if free, else "name (2).ext", "name (3).ext", ..."""
    path = Path(path)
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def destination_for(path: Path, track_id: str) -> Path:
    """Where a finished file for `track_id` goes: `path`, or the " (n)" sibling that already
    holds this very track (a re-download replaces it instead of minting " (3)"), or else the
    first free numbered name."""
    path = Path(path)
    if not path.exists() or stored_track_id(path) == track_id:
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists() or stored_track_id(candidate) == track_id:
            return candidate
        counter += 1


def _clean_str(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _tidy(text: str) -> str:
    """Whitespace-collapsed text without invisible characters (for tags and file names)."""
    return re.sub(r"\s+", " ", strip_invisible(text)).strip()


def _strip_topic(channel: str) -> str:
    return _TOPIC_RE.sub("", channel).strip() or channel


def pick_artist_title(info: dict[str, Any]) -> tuple[str, str]:
    """Decide the (artist, title) pair used for the file name and the tags.

    1. yt-dlp's own `artist` + `track` (from YouTube's music metadata) win when both exist;
    2. else "Artist - Title" parsed from the *cleaned* video title: the junk tail is removed
       first, so "Song - Official Video" / "Song | Lyrics" are titles, not artist-title pairs;
    3. else the channel/uploader (minus " - Topic") and the cleaned video title.
    """
    artist = _clean_str(info.get("artist"))
    track = _clean_str(info.get("track"))
    if artist and track:
        return _tidy(artist) or artist, _tidy(track) or track

    raw_title = _clean_str(info.get("title")) or ""
    cleaned = clean_title(raw_title) or _tidy(raw_title) or raw_title
    parts = split_artist_title(cleaned)
    if parts is None:
        # "Artist - (Official Video)": cleaning swallowed the whole title half together with
        # the separator; the raw split still tells us who the artist is.
        raw_parts = split_artist_title(raw_title)
        if raw_parts is not None and not clean_title(raw_parts[1]):
            parts = raw_parts
    if parts:
        left, right = parts
        return _tidy(left) or left, clean_title(right) or _tidy(right) or right

    channel = (
        _clean_str(info.get("channel")) or _clean_str(info.get("uploader")) or "Unknown Artist"
    )
    return _tidy(_strip_topic(channel)) or channel, cleaned or "Unknown Title"


def _clean_error_text(text: str) -> str:
    text = _ANSI_RE.sub("", text or "")
    text = re.sub(r"^\s*ERROR:\s*", "", text)
    cut = text.find(_BUG_REPORT_MARKER)
    if cut != -1:
        text = text[:cut]
    return re.sub(r"\s+", " ", text).strip()


# (substrings to look for in the lower-cased yt-dlp message, friendly explanation)
_FRIENDLY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "getaddrinfo failed",
            "failed to resolve",
            "name or service not known",
            "temporary failure in name resolution",
            "nodename nor servname",
            "network is unreachable",
            "no route to host",
            "connection reset",
            "connection refused",
            "unable to download webpage",
            "unable to download api page",
            "remote end closed connection",
            "timed out",
            "errno 11001",
            "errno 11004",
        ),
        "Couldn't reach YouTube. Check your internet connection and try again.",
    ),
    (
        ("private video", "video is private", "is a private video"),
        "This video is private, so it can't be downloaded.",
    ),
    (
        ("age-restricted", "age restricted", "confirm your age", "inappropriate for some users"),
        "This video is age-restricted and needs a signed-in YouTube account, which Ultimate Playlist doesn't support.",
    ),
    (
        (
            "not available in your country",
            "available in your country",
            "geo-restricted",
            "georestricted",
            "geo restricted",
            "from your location",
            "blocked it in your country",
        ),
        "This video isn't available in your country.",
    ),
    (
        (
            "members-only",
            "members only",
            "join this channel",
            "premium subscribers",
            "requires payment",
            "purchase",
            "channel's members",
        ),
        "This video is only available to channel members or paying subscribers.",
    ),
    (
        ("not a bot", "sign in to confirm", "login required", "cookies", "sign in to"),
        "YouTube is asking for a sign-in to confirm you're not a bot. Wait a while and try again.",
    ),
    (
        ("playlist does not exist", "the playlist is private", "playlist type is unviewable"),
        "That playlist doesn't exist or is private.",
    ),
    (
        ("requested format is not available", "no video formats found"),
        "YouTube didn't offer a downloadable audio stream for this video (try updating yt-dlp).",
    ),
    (
        ("only images are available",),
        "That link has no audio or video to download.",
    ),
    (
        ("incomplete youtube id", "looks truncated", "not a valid url"),
        "That link looks incomplete; copy it again from YouTube.",
    ),
    (
        (
            "video unavailable",
            "is unavailable",
            "has been removed",
            "no longer available",
            "video is not available",
            "content is not available",
            "does not exist",
            "has been terminated",
            "account associated with this video",
            "video has been deleted",
        ),
        "This video is unavailable (it may have been removed or deleted).",
    ),
    (
        (
            "is a live event",
            "live event will begin",
            "premieres in",
            "this live event",
            "is live",
            "livestream",
        ),
        "This is a live stream or premiere that hasn't finished yet; try again after it ends.",
    ),
    (
        ("unsupported url", "unsupported link"),
        "This link isn't a YouTube video or playlist that can be downloaded.",
    ),
    (
        ("ffmpeg", "ffprobe", "postprocessing", "conversion failed", "audio conversion"),
        "Converting the audio failed. Check that ffmpeg is installed (see the doctor page).",
    ),
)


def friendly_error(exc: BaseException | str) -> str:
    """Turn a yt-dlp error into something a non-programmer can act on.

    The original message is appended in parentheses (trimmed to ~200 characters) so power users
    can still search for it.
    """
    detail = _clean_error_text(str(exc))
    lowered = detail.lower()
    message = _http_status_message(lowered)
    if message is None:
        message = "YouTube download failed."
        for needles, friendly in _FRIENDLY_RULES:
            if any(needle in lowered for needle in needles):
                message = friendly
                break
    if len(detail) > _ERROR_DETAIL_LEN:
        detail = detail[: _ERROR_DETAIL_LEN - 1].rstrip() + "…"
    return f"{message} ({detail})" if detail else message


def _http_status_message(lowered: str) -> str | None:
    """An HTTP status in the message beats the substring rules: a 403 while fetching the media
    is the classic stale-signature / out-of-date-yt-dlp failure, not a connectivity problem."""
    match = _HTTP_ERROR_RE.search(lowered)
    if not match:
        return None
    code = int(match.group(1))
    if code == 403:
        return (
            "YouTube refused to serve the audio. This usually means yt-dlp is out of date; "
            "update it (uv lock --upgrade-package yt-dlp && uv sync) and try again."
        )
    if code == 429:
        return "YouTube is rate-limiting this computer. Wait a few minutes and try again."
    if code == 404:
        return "This video is unavailable (it may have been removed or deleted)."
    if 500 <= code < 600:
        return "YouTube is having problems right now. Try again later."
    return None


# --------------------------------------------------------------------------------------------
# yt-dlp plumbing
# --------------------------------------------------------------------------------------------


class _YdlLogger:
    """Adapter that yt-dlp calls; forwards to `logging` and remembers the last error text.

    yt-dlp sends both info and debug messages to `debug()`; with `ignoreerrors=True` extraction
    errors are only reported through `error()` and `extract_info` returns None, so remembering
    the last error is the only way to explain a None result to the user.
    """

    def __init__(self, quiet_warnings: bool = False) -> None:
        self.last_error: str | None = None
        self._warn_level = logging.DEBUG if quiet_warnings else logging.WARNING

    def debug(self, msg: str) -> None:
        ydl_log.debug(msg[8:] if msg.startswith("[debug] ") else msg)

    def info(self, msg: str) -> None:
        ydl_log.debug(msg)

    def warning(self, msg: str) -> None:
        ydl_log.log(self._warn_level, re.sub(r"^\s*WARNING:\s*", "", msg))

    def error(self, msg: str) -> None:
        text = _clean_error_text(msg)
        if not text:
            return
        self.last_error = text
        ydl_log.error(text)


def _bundled_runtime(name: str) -> str | None:
    """Path of the runtime's executable in the package's bin folder, else None."""
    return bundled_tool(RUNTIME_BINARY.get(name, name))


def _shipped_runtimes() -> dict[str, str]:
    """The runtimes the package actually carries in ``bin`` (deno.exe and/or node.exe)."""
    found = {name: _bundled_runtime(name) for name in DEFAULT_JS_RUNTIMES}
    return {name: path for name, path in found.items() if path}


_warned_unbundled_runtimes: set[tuple[str, ...]] = set()


def _effective_runtimes(names: list[str] | tuple[str, ...] | None) -> dict[str, str | None]:
    """Configured runtime -> bundled path (None = yt-dlp looks the name up on PATH).

    In the packaged build, as soon as one configured runtime ships in ``bin``, the others are
    dropped: yt-dlp ranks Deno above Node regardless of order, so a stray Deno on PATH would
    otherwise win over the bundled Node and the package would not be isolated from the machine.
    A configuration that names only runtimes the package does not carry (``["deno"]`` with a
    zip built around node.exe) falls back to the shipped ones, logged once: the bundled runtime
    is right there and a PATH lookup on a clean machine finds nothing. From source every
    configured runtime stays enabled.
    """
    cleaned = [str(n).strip().lower() for n in (names or ()) if str(n).strip()]
    if not cleaned:
        cleaned = list(DEFAULT_JS_RUNTIMES)
    runtimes = {name: _bundled_runtime(name) for name in dict.fromkeys(cleaned)}
    if is_frozen():
        if any(runtimes.values()):
            runtimes = {name: path for name, path in runtimes.items() if path}
        else:
            shipped = _shipped_runtimes()
            if shipped:
                key = tuple(runtimes)
                if key not in _warned_unbundled_runtimes:
                    _warned_unbundled_runtimes.add(key)
                    log.warning(
                        "js_runtimes %s not bundled; using the packaged %s",
                        list(runtimes),
                        ", ".join(shipped),
                    )
                runtimes = dict(shipped)
    return runtimes


def _js_runtimes_opt(names: list[str] | tuple[str, ...] | None) -> dict[str, dict[str, Any]]:
    """yt-dlp only auto-enables Deno; we enable every configured runtime.

    A runtime shipped in the package's ``bin`` folder is pinned by path so the no-install build
    works on a machine with nothing on PATH; ``None`` leaves yt-dlp to look the name up on PATH.
    """
    return {name: {"path": path} for name, path in _effective_runtimes(names).items()}


_warned_ffmpeg_paths: set[str] = set()


def _ffmpeg_location(settings: Settings) -> str | None:
    """What to hand yt-dlp as ``ffmpeg_location``: the file the configured ``ffmpeg_path``
    resolves to (``find_ffmpeg`` resolves it the same way: a file, a folder holding ffmpeg.exe,
    or a bare name on PATH), else the bundled bin *folder* (so ffprobe is found next to ffmpeg),
    else None (yt-dlp searches PATH). yt-dlp finds ffprobe next to a file path as well.

    yt-dlp silently "continues without ffmpeg" when the location does not exist, which turns
    every download into a failed MP3 conversion; a stale ``ffmpeg_path`` from an uninstalled
    ffmpeg must therefore fall back exactly like ``find_ffmpeg`` does, and be logged once.
    """
    explicit = settings.ffmpeg_path
    if explicit:
        resolved = resolve_explicit(explicit)
        if resolved:
            return resolved
        if explicit not in _warned_ffmpeg_paths:
            _warned_ffmpeg_paths.add(explicit)
            log.warning("ffmpeg_path %s does not exist; ignoring it", explicit)
    bundled_ffmpeg = bundled_tool("ffmpeg")
    if bundled_ffmpeg:
        return str(Path(bundled_ffmpeg).parent)
    return None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ydl_opts(
    settings: Settings,
    incoming_dir: Path,
    progress: ProgressCallback,
    cancel: threading.Event,
) -> dict[str, Any]:
    """Build the YoutubeDL option dict for one download (kept separate so tests can inspect it)."""
    fmt = settings.audio_format
    converting_msg = f"Converting to {fmt.upper()}"
    # yt-dlp reports `PostProcessor.pp_key()` ("ExtractAudio", "Metadata", "MoveFiles"): the
    # class name minus the "FFmpeg" prefix and "PP" suffix. Accept the option keys too.
    pp_messages = {
        "ExtractAudio": converting_msg,
        "FFmpegExtractAudio": converting_msg,
        "Metadata": "Writing tags",
        "FFmpegMetadata": "Writing tags",
        "EmbedThumbnail": "Embedding cover art",
        "MoveFiles": "Finishing up",
        "MoveFilesAfterDownload": "Finishing up",
    }
    last_pp_event: list[tuple[str, str]] = []  # yt-dlp registers pp hooks twice; dedupe

    def hook(d: dict[str, Any]) -> None:
        if cancel.is_set():
            raise DownloadCancelled("Download cancelled")
        status = d.get("status")
        if status == "downloading":
            done = _float_or_none(d.get("downloaded_bytes"))
            total = _float_or_none(d.get("total_bytes")) or _float_or_none(
                d.get("total_bytes_estimate")
            )
            fraction = max(0.0, min(1.0, done / total)) if done is not None and total else None
            progress(
                ProgressEvent(
                    JobStatus.DOWNLOADING,
                    progress=fraction,
                    speed=_float_or_none(d.get("speed")),
                    eta=_float_or_none(d.get("eta")),
                )
            )
        elif status == "finished":
            progress(ProgressEvent(JobStatus.CONVERTING, progress=1.0, message=converting_msg))

    def pp_hook(d: dict[str, Any]) -> None:
        if cancel.is_set():
            raise DownloadCancelled("Download cancelled")
        status = str(d.get("status") or "")
        name = str(d.get("postprocessor") or "")
        key = (status, name)
        if last_pp_event and last_pp_event[-1] == key:
            return
        last_pp_event[:] = [key]
        if status == "started":
            message = pp_messages.get(name, f"Processing ({name})" if name else "Processing")
            progress(ProgressEvent(JobStatus.CONVERTING, progress=1.0, message=message))

    postprocessors: list[dict[str, Any]] = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": fmt,
            "preferredquality": settings.audio_quality,
        },
        {"key": "FFmpegMetadata", "add_metadata": True},
    ]
    if settings.embed_cover:
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": str(Path(incoming_dir) / "%(id)s.%(ext)s"),
        "postprocessors": postprocessors,
        "writethumbnail": bool(settings.embed_cover),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "windowsfilenames": True,
        "retries": 3,
        "fragment_retries": 3,
        "overwrites": True,
        "continuedl": False,
        "js_runtimes": _js_runtimes_opt(settings.js_runtimes),
        "logger": _YdlLogger(),
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
    }
    location = _ffmpeg_location(settings)
    if location:
        opts["ffmpeg_location"] = location
    return opts


def _thumbnail_of(info: dict[str, Any]) -> str | None:
    thumbs = info.get("thumbnails")
    if isinstance(thumbs, list):
        for thumb in reversed(thumbs):
            if isinstance(thumb, dict) and thumb.get("url"):
                return str(thumb["url"])
    return _clean_str(info.get("thumbnail"))


def _ref_from_info(info: dict[str, Any], original_url: str | None = None) -> TrackRef | None:
    video_id = _clean_str(info.get("id"))
    if not video_id:
        return None
    artist = (
        _clean_str(info.get("artist"))
        or _clean_str(info.get("channel"))
        or _clean_str(info.get("uploader"))
    )
    extra: dict[str, Any] = {}
    if original_url:
        extra["original_url"] = original_url
    return TrackRef(
        provider="youtube",
        source_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=_clean_str(info.get("title")),
        artist=_strip_topic(artist) if artist else None,
        album=_clean_str(info.get("album")),
        duration=_float_or_none(info.get("duration")),
        thumbnail_url=_thumbnail_of(info),
        extra=extra,
    )


def _refs_from_playlist(info: dict[str, Any]) -> list[TrackRef]:
    refs: list[TrackRef] = []
    for entry in info.get("entries") or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        if entry.get("title") in _UNAVAILABLE_TITLES:
            continue
        ref = _ref_from_info(entry)
        if ref is not None:
            refs.append(ref)
    return refs


def _single_info(info: Any) -> dict[str, Any] | None:
    """`noplaylist` keeps this a video dict; be defensive and unwrap a playlist anyway."""
    if not isinstance(info, dict):
        return None
    if info.get("_type") in ("playlist", "multi_video"):
        for entry in info.get("entries") or []:
            if isinstance(entry, dict) and entry.get("id"):
                return entry
        return None
    return info


def _produced_file(incoming: Path, video_id: str, fmt: str, info: dict[str, Any]) -> Path:
    candidate = incoming / f"{video_id}.{fmt}"
    if candidate.is_file():
        return candidate
    downloads = info.get("requested_downloads") or [{}]
    first = downloads[0] if downloads and isinstance(downloads[0], dict) else {}
    filepath = first.get("filepath")
    if filepath and Path(filepath).is_file():
        return Path(filepath)
    raise ProviderError(
        "The converted audio file was not produced. Check that ffmpeg is installed "
        "(run the doctor) and try again."
    )


def _cleanup_incoming(incoming: Path, video_id: str) -> None:
    """Remove `.incoming/<id>.*` leftovers (thumbnails, .part, .ytdl, originals)."""
    if not video_id or not incoming.is_dir():
        return
    prefix = f"{video_id}."
    for path in incoming.iterdir():
        if path.name.startswith(prefix) and path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                log.debug("Could not remove leftover %s: %s", path, exc)


def _fill_id3(
    tags: ID3, artist: str, title: str, album: str | None, source_url: str, track_id: str
) -> None:
    """Replace our frames on an ID3 tag; everything else (notably the APIC cover) is kept."""
    tags.delall("TPE1")
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.delall("TIT2")
    tags.add(TIT2(encoding=3, text=[title]))
    tags.delall("TALB")
    if album:
        tags.add(TALB(encoding=3, text=[album]))
    tags.delall("COMM")
    tags.add(COMM(encoding=3, lang="eng", desc="", text=[source_url]))
    tags.delall(f"TXXX:{TXXX_ID_DESC}")
    tags.add(TXXX(encoding=3, desc=TXXX_ID_DESC, text=[track_id]))


def _write_mp3_tags(
    path: Path, artist: str, title: str, album: str | None, source_url: str, track_id: str
) -> bool:
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()
    _fill_id3(tags, artist, title, album, source_url, track_id)
    tags.save(str(path), v2_version=3)  # v2.3 is what Windows Explorer reads most reliably
    return bool(tags.getall("APIC"))


def _write_mp4_tags(
    audio: MP4, artist: str, title: str, album: str | None, source_url: str, track_id: str
) -> bool:
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None
    tags["\xa9ART"] = [artist]
    tags["\xa9nam"] = [title]
    if album:
        tags["\xa9alb"] = [album]
    else:
        tags.pop("\xa9alb", None)
    tags["\xa9cmt"] = [source_url]
    tags[MP4_ID_KEY] = [MP4FreeForm(track_id.encode("utf-8"))]
    audio.save()
    return bool(tags.get("covr"))


def _write_vorbis_tags(
    audio: FLAC | OggOpus | OggVorbis,
    artist: str,
    title: str,
    album: str | None,
    source_url: str,
    track_id: str,
) -> bool:
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None
    tags["artist"] = [artist]
    tags["title"] = [title]
    if album:
        tags["album"] = [album]
    elif "album" in tags:
        del tags["album"]
    tags["comment"] = [source_url]
    tags[TXXX_ID_DESC] = [track_id]
    audio.save()
    if getattr(audio, "pictures", None):  # FLAC picture blocks
        return True
    return bool(tags.get("metadata_block_picture"))  # Opus / Vorbis


def write_tags(
    path: Path,
    artist: str,
    title: str,
    album: str | None,
    source_url: str,
    track_id: str,
) -> bool:
    """Write artist / title / album / comment (source URL) and the ULTIMATE_PLAYLIST_ID marker
    into the file; returns whether a cover is embedded.

    The container decides the tag format: ID3 for MP3 (raw mutagen API, a header is added when
    missing), MP4 atoms for M4A (the id lives in the freeform
    ``----:com.apple.iTunes:ULTIMATE_PLAYLIST_ID`` atom) and Vorbis comments for Opus / FLAC /
    Ogg. Those are exactly the keys `library.read_tags()` reads back on a rescan. Existing tags
    yt-dlp wrote (notably the cover) are kept; ours replace theirs. Writing ID3 frames into a
    non-MP3 container would corrupt it, so an unsupported container is a ProviderError.
    """
    path = Path(path)
    if path.suffix.lower() == ".mp3":
        return _write_mp3_tags(path, artist, title, album, source_url, track_id)
    audio = mutagen.File(str(path))
    if isinstance(audio, MP4):
        return _write_mp4_tags(audio, artist, title, album, source_url, track_id)
    if isinstance(audio, FLAC | OggOpus | OggVorbis):
        return _write_vorbis_tags(audio, artist, title, album, source_url, track_id)
    if isinstance(audio, ID3FileType):  # e.g. WAVE / AIFF carry an ID3 chunk of their own
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        _fill_id3(audio.tags, artist, title, album, source_url, track_id)
        audio.save()
        return bool(audio.tags.getall("APIC"))
    raise ProviderError(
        f"Ultimate Playlist can't write tags into {path.suffix or 'this kind of'} files. "
        "Choose mp3, m4a, opus or flac as the audio format in the settings."
    )


def stored_track_id(path: Path) -> str | None:
    """Read the ULTIMATE_PLAYLIST_ID marker back (None if absent or unreadable), whatever the
    container: ID3 TXXX for MP3, the iTunes freeform atom for M4A, a Vorbis comment otherwise."""
    path = Path(path)
    if path.suffix.lower() == ".mp3":
        try:
            frames = ID3(str(path)).getall(f"TXXX:{TXXX_ID_DESC}")
        except Exception:  # noqa: BLE001 - any unreadable file simply has no id
            return None
        if frames and frames[0].text:
            return str(frames[0].text[0])
        return None
    if not path.is_file():
        return None
    return read_tags(path).track_id


def _audio_length(path: Path) -> float | None:
    try:
        audio = MP3(str(path)) if path.suffix.lower() == ".mp3" else mutagen.File(str(path))
        length = getattr(getattr(audio, "info", None), "length", None)
        return float(length) if length else None
    except Exception as exc:  # noqa: BLE001 - duration is best effort
        log.debug("Could not read duration of %s: %s", path, exc)
        return None


def _runtime_info(name: str) -> Any:
    """yt-dlp's runtime probe; `info` is a cached_property in current yt-dlp, a method in older."""
    try:
        from yt_dlp.utils import _jsruntime

        cls = {
            "deno": _jsruntime.DenoJsRuntime,
            "node": _jsruntime.NodeJsRuntime,
            "bun": getattr(_jsruntime, "BunJsRuntime", None),
            "quickjs": getattr(_jsruntime, "QuickJsRuntime", None),
        }.get(name)
        if cls is None:
            return None
        info = cls(path=_bundled_runtime(name)).info  # path=None means "look on PATH"
        return info() if callable(info) else info
    except Exception as exc:  # noqa: BLE001 - probing must never raise
        log.debug("JS runtime probe for %s failed: %s", name, exc)
        return None


def _js_runtime_hint(names: list[str] | tuple[str, ...]) -> str:
    """How to get a runtime: re-extract the zip in the packaged build, install one from source.

    The packaged hint names the runtime the zip really carries (``bin\\node.exe`` for a build
    around Node) when one is there; when the bin folder holds none (quarantined, half
    extracted) it lists the acceptable ones and points at the folder, not at ``deno.exe``.
    """
    shipped = _shipped_runtimes()
    if shipped:
        display = " or ".join(f"bin\\{RUNTIME_BINARY.get(n, n)}.exe" for n in shipped)
        first = next(iter(shipped))
        bundled = missing_bundled_hint(display, RUNTIME_BINARY.get(first, first))
    else:
        display = " or ".join(f"bin\\{RUNTIME_BINARY.get(n, n)}.exe" for n in names)
        bundled = missing_bundled_hint(display or "bin\\deno.exe or bin\\node.exe")
    if bundled:
        return bundled
    return "Install Deno: winget install DenoLand.Deno (or Node.js 22+)"


def _js_runtime_check(names: list[str] | tuple[str, ...]) -> tuple[bool, str]:
    hint = _js_runtime_hint(names)
    # Probe the runtimes yt-dlp will really use (the bundled ones only, in the packaged build).
    names = list(_effective_runtimes(names))
    found = [(name, _runtime_info(name)) for name in names]
    found = [(name, info) for name, info in found if info is not None]
    for name, info in found:
        if getattr(info, "supported", True):
            version = getattr(info, "version", None) or "unknown version"
            path = getattr(info, "path", None) or name
            binary = RUNTIME_BINARY.get(name, name)  # quickjs ships as qjs.exe
            where = f"{path}, bundled" if is_bundled(binary, path) else path
            return True, f"{getattr(info, 'name', name)} {version} ({where})"
    if found:
        name, info = found[0]
        version = getattr(info, "version", None) or "unknown version"
        return False, f"{name} {version} is too old for yt-dlp. {hint}"
    for name in names:  # yt-dlp probe unavailable: fall back to a plain lookup, bin folder first
        bundled = _bundled_runtime(name)
        if bundled:
            return True, f"{name} ({bundled}, bundled)"
        path = shutil.which(name)
        if path:
            return True, f"{name} ({path})"
    return False, hint


# --------------------------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------------------------


class YouTubeProvider:
    name = "youtube"
    display_name = "YouTube"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    def configure(self, settings: Settings) -> None:
        """Use these (live) settings for resolve()/doctor(); the registry calls this at start."""
        self._settings = settings

    def _current_settings(self) -> Settings:
        """The configured settings, else the user's config.json (Settings.load never raises)."""
        return self._settings if self._settings is not None else Settings.load()

    def _js_runtime_names(self) -> list[str]:
        settings = self._current_settings()
        if settings.js_runtimes:
            return list(settings.js_runtimes)
        return list(DEFAULT_JS_RUNTIMES)

    # -- matching ------------------------------------------------------------------------------

    def matches(self, url: str) -> bool:
        try:
            parts = urlsplit(normalize_url(url))
        except ValueError:
            return False
        host = (parts.hostname or "").lower()
        if host not in _HOSTS:
            return False
        path = (parts.path or "/").rstrip("/") or "/"  # "/watch/" is "/watch" to yt-dlp too
        query = parse_qs(parts.query)
        video = query.get("v", [""])[0]
        playlist = query.get("list", [""])[0]
        if host == "youtu.be":
            return bool(_VIDEO_ID_RE.match(path.strip("/")))
        if path in ("/watch", "/") and _VIDEO_ID_RE.match(video):
            return True  # youtube.com/?v=<id> is a watch URL as well
        if path == "/watch":
            return bool(playlist)
        if path == "/playlist":
            return bool(playlist)
        if _BROWSE_ID_RE.match(path):
            return True
        match = _ID_PATH_RE.match(path)
        if match:
            ident = match.group(1)
            return bool(_VIDEO_ID_RE.match(ident)) or (ident == "videoseries" and bool(playlist))
        return False

    # -- resolving -----------------------------------------------------------------------------

    def resolve(self, url: str) -> list[TrackRef]:
        url = normalize_url(url)
        logger = _YdlLogger(quiet_warnings=True)
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "noplaylist": True,
            "skip_download": True,
            "ignoreerrors": True,
            "js_runtimes": _js_runtimes_opt(self._js_runtime_names()),
            "logger": logger,
        }
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.YoutubeDLError as exc:
            raise ProviderError(friendly_error(exc)) from exc
        except Exception as exc:
            log.exception("Unexpected failure resolving %s", url)
            raise ProviderError(f"Could not read that YouTube link: {exc}") from exc

        if not isinstance(info, dict):
            raise ProviderError(
                friendly_error(logger.last_error or "YouTube returned no information for this link")
            )
        if info.get("_type") in ("playlist", "multi_video"):
            return _refs_from_playlist(info)
        ref = _ref_from_info(info, original_url=url)
        if ref is None:
            raise ProviderError("YouTube returned no video id for this link.")
        return [ref]

    # -- downloading ---------------------------------------------------------------------------

    def download(
        self,
        ref: TrackRef,
        dest_dir: Path,
        settings: Settings,
        progress: ProgressCallback,
        cancel: threading.Event,
    ) -> Track:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        incoming = dest_dir / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        fmt = settings.audio_format
        video_id = ref.source_id
        opts = _ydl_opts(settings, incoming, progress, cancel)
        logger: _YdlLogger = opts["logger"]
        try:
            if cancel.is_set():
                raise DownloadCancelled("Download cancelled")
            progress(
                ProgressEvent(JobStatus.DOWNLOADING, progress=0.0, message="Starting download")
            )
            with YoutubeDL(opts) as ydl:
                raw_info = ydl.extract_info(ref.url, download=True)
            if cancel.is_set():
                raise DownloadCancelled("Download cancelled")
            info = _single_info(raw_info)
            if info is None:
                raise ProviderError(
                    friendly_error(
                        logger.last_error or "YouTube returned no information for this video"
                    )
                )
            video_id = _clean_str(info.get("id")) or ref.source_id
            produced = _produced_file(incoming, video_id, fmt, info)

            artist, title = pick_artist_title(info)
            album = _clean_str(info.get("album"))
            progress(ProgressEvent(JobStatus.CONVERTING, progress=1.0, message="Writing tags"))
            has_cover = write_tags(produced, artist, title, album, ref.url, ref.track_id)

            # The real container decides the extension ("alac" -> .m4a, "vorbis" -> .ogg).
            suffix = produced.suffix.lower() or f".{fmt}"
            wanted = dest_dir / f"{safe_filename(f'{artist} - {title}')}{suffix}"
            with _move_lock:
                final = destination_for(wanted, ref.track_id)
                try:
                    os.replace(produced, final)
                except PermissionError as exc:
                    raise ProviderError(
                        f"Couldn't replace {final.name} because it is in use (is it playing?). "
                        "Stop playback and retry."
                    ) from exc

            duration = _audio_length(final) or _float_or_none(info.get("duration"))
            return Track(
                id=ref.track_id,
                provider=self.name,
                source_id=video_id,
                source_url=ref.url,
                title=title,
                artist=artist,
                path=final.relative_to(dest_dir).as_posix(),
                album=album,
                duration=duration,
                has_cover=has_cover,
                file_size=final.stat().st_size,
            )
        except DownloadCancelled:
            raise
        except ProviderError:
            raise
        except yt_dlp.utils.YoutubeDLError as exc:
            if cancel.is_set():
                raise DownloadCancelled("Download cancelled") from exc
            raise ProviderError(friendly_error(exc)) from exc
        except Exception as exc:
            log.exception("Unexpected failure downloading %s", ref.url)
            raise ProviderError(f"Download failed: {exc}") from exc
        finally:
            _cleanup_incoming(incoming, ref.source_id)
            if video_id != ref.source_id:
                _cleanup_incoming(incoming, video_id)

    # -- diagnostics ---------------------------------------------------------------------------

    def doctor(self, settings: Settings | None = None) -> list[tuple[bool, str, str]]:
        """Checks honour the user's settings (ffmpeg_path, js_runtimes), like downloads do."""
        checks: list[tuple[bool, str, str]] = []
        try:
            settings = settings if settings is not None else self._current_settings()
        except Exception as exc:  # noqa: BLE001 - doctor never raises
            log.debug("Could not load settings for doctor(): %s", exc)
            settings = None
        try:
            ffmpeg = find_ffmpeg(settings.ffmpeg_path if settings else None)
            if ffmpeg.found:
                checks.append((True, "ffmpeg", ffmpeg.describe()))
            else:
                checks.append((False, "ffmpeg", install_hint()))
        except Exception as exc:  # noqa: BLE001 - doctor never raises
            checks.append((False, "ffmpeg", f"Could not check ffmpeg: {exc}"))
        try:
            names = (
                list(settings.js_runtimes)
                if settings and settings.js_runtimes
                else list(DEFAULT_JS_RUNTIMES)
            )
            ok, detail = _js_runtime_check(names)
            checks.append((ok, "JavaScript runtime", detail))
        except Exception as exc:  # noqa: BLE001
            checks.append((False, "JavaScript runtime", f"Could not check JS runtimes: {exc}"))
        version = getattr(getattr(yt_dlp, "version", None), "__version__", None) or "unknown"
        checks.append((True, "yt-dlp", str(version)))
        return checks
