"""Spotify provider: metadata from Spotify, audio from YouTube Music, tags from Spotify.

Spotify streams are DRM-protected and are never touched. A pasted track / album / playlist /
Liked Songs link is read for its metadata only (``spotify_meta``: the public embed page, or the
Web API when the user has connected their Spotify account in Settings), each track is matched to
a YouTube Music recording (``ytmusic_match``) and downloaded through the existing
``YouTubeProvider``, then renamed and re-tagged with Spotify's title, artists, album, cover and
ids. ``Track.id`` is ``spotify:<track id>``; the chosen YouTube video id is kept in the tags
(``TXXX YOUTUBE_ID``) and in ``TrackRef.extra["youtube_id"]``.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import mutagen
import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TDRC, TPE2, TRCK, TSRC, TXXX, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm, MP4Tags
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from ..config import Settings
from ..models import JobStatus, ProgressEvent, Track, TrackRef
from . import spotify_auth, youtube
from .base import DownloadCancelled, ProgressCallback, ProviderError
from .spotify_auth import SpotifyAuthError
from .spotify_meta import (
    EMBED_LIST_CAP,
    USER_AGENT,
    SpotifyEntity,
    client_id_of,
    get_metadata,
    parse_spotify_url,
)
from .ytmusic_match import Match, find_match

log = logging.getLogger(__name__)

TXXX_YOUTUBE_ID = "YOUTUBE_ID"
TXXX_ISRC = "ISRC"
MP4_YOUTUBE_ID_KEY = f"----:com.apple.iTunes:{TXXX_YOUTUBE_ID}"
MP4_ISRC_KEY = f"----:com.apple.iTunes:{TXXX_ISRC}"
COVER_TIMEOUT = 10.0
MAX_COVER_BYTES = 5 * 1024 * 1024
_COVER_MIMES = ("image/jpeg", "image/png")
_HOSTS = frozenset({"open.spotify.com", "play.spotify.com", "spotify.link", "spotify.app.link"})
# The YouTube step writes into a private sub-folder of this one, never into the library root: a
# YouTube-provider file of the same video must not be replaced and then moved away.
STAGING_DIR_NAME = ".incoming"
_STAGING_ID_RE = re.compile(r"[^A-Za-z0-9]")

DOCTOR_PUBLIC_ONLY = (
    f"Public pages only (tracks, albums, playlists up to {EMBED_LIST_CAP} songs). Connect "
    "Spotify in Settings for your own bigger playlists and Liked Songs."
)
DOCTOR_NOT_CONNECTED = "Client ID set, not connected: click Connect Spotify in Settings"
DOCTOR_CONNECTED = "Connected as {name} (your playlists of any size and Liked Songs)"

# Two Spotify tracks can map to the same YouTube video (single vs. album version, or a song
# that is twice in a playlist). Each Spotify download stages in its own folder, so their files
# never collide; this lock only keeps two workers from downloading one video at the same time.
_video_locks: dict[str, threading.Lock] = {}
_video_locks_guard = threading.Lock()


def _video_lock(video_id: str) -> threading.Lock:
    with _video_locks_guard:
        return _video_locks.setdefault(video_id, threading.Lock())


def _mmss(seconds: float | None) -> str:
    if seconds is None:
        return "?:??"
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


# --------------------------------------------------------------------------------------------
# Refs from entities
# --------------------------------------------------------------------------------------------


def _ref_from_entity(track: SpotifyEntity, container: SpotifyEntity | None) -> TrackRef:
    extra: dict[str, Any] = {"source": track.source}
    if track.release_year:
        extra["release_year"] = track.release_year
    if track.track_number is not None:
        extra["track_number"] = track.track_number
    if track.album_artist:
        extra["album_artist"] = track.album_artist
    if track.isrc:
        extra["isrc"] = track.isrc
    if container is not None:
        label = "Album" if container.kind == "album" else "Playlist"  # Liked Songs too
        extra["container"] = f"{label}: {container.name}"
    return TrackRef(
        provider="spotify",
        source_id=track.id,
        url=track.url,
        title=track.name,
        artist=", ".join(track.artists) or None,
        album=track.album,
        duration=track.duration,
        thumbnail_url=track.cover_url,
        extra=extra,
    )


def refs_from_entity(entity: SpotifyEntity) -> list[TrackRef]:
    if entity.kind == "track":
        return [_ref_from_entity(entity, None)]
    return [_ref_from_entity(track, entity) for track in entity.tracks]


# --------------------------------------------------------------------------------------------
# Cover art and tags
# --------------------------------------------------------------------------------------------


def _sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def fetch_cover(url: str, session: Any | None = None) -> tuple[bytes, str] | None:
    """Spotify's cover as (bytes, mime); None on any problem (the YouTube cover stays then)."""
    if not url:
        return None
    try:
        getter = session.get if session is not None else requests.get
        resp = getter(url, timeout=COVER_TIMEOUT, headers={"User-Agent": USER_AGENT})
        if int(getattr(resp, "status_code", 0) or 0) != 200:
            log.debug("Cover %s answered HTTP %s", url, getattr(resp, "status_code", "?"))
            return None
        headers = getattr(resp, "headers", None) or {}
        length = headers.get("Content-Length")
        if length and int(length) > MAX_COVER_BYTES:
            log.debug("Cover %s is too large (%s bytes)", url, length)
            return None
        data = bytes(resp.content)
        if not data or len(data) > MAX_COVER_BYTES:
            return None
        declared = str(headers.get("Content-Type") or "").split(";")[0].strip().lower()
        mime = declared if declared in _COVER_MIMES else _sniff_image(data)
        if mime not in _COVER_MIMES:
            log.debug("Cover %s is not a JPEG/PNG (%s)", url, declared or "no content type")
            return None
        return data, mime
    except Exception as exc:  # noqa: BLE001 - the cover is best effort
        log.debug("Could not fetch cover %s: %s", url, exc)
        return None


def _write_id3_extras(
    tags: ID3, ref: TrackRef, video_id: str, cover: tuple[bytes, str] | None
) -> None:
    extra = ref.extra or {}
    tags.delall("TPE2")
    if extra.get("album_artist"):
        tags.add(TPE2(encoding=3, text=[str(extra["album_artist"])]))
    tags.delall("TRCK")
    if extra.get("track_number") is not None:
        tags.add(TRCK(encoding=3, text=[str(extra["track_number"])]))
    tags.delall("TDRC")
    if extra.get("release_year"):
        tags.add(TDRC(encoding=3, text=[str(extra["release_year"])]))
    tags.delall(f"TXXX:{TXXX_YOUTUBE_ID}")
    tags.add(TXXX(encoding=3, desc=TXXX_YOUTUBE_ID, text=[video_id]))
    tags.delall(f"TXXX:{TXXX_ISRC}")
    tags.delall("TSRC")
    if extra.get("isrc"):
        tags.add(TXXX(encoding=3, desc=TXXX_ISRC, text=[str(extra["isrc"])]))
        tags.add(TSRC(encoding=3, text=[str(extra["isrc"])]))
    if cover is not None:
        data, mime = cover
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))


def _set_or_drop(tags: Any, key: str, value: list[Any] | None) -> None:
    """Set `key` to `value`, or remove it when Spotify has no value (so yt-dlp's never stays)."""
    if value:
        tags[key] = value
    elif key in tags:
        del tags[key]


def _write_mp4_extras(
    audio: MP4, ref: TrackRef, video_id: str, cover: tuple[bytes, str] | None
) -> None:
    extra = ref.extra or {}
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert isinstance(tags, MP4Tags)
    album_artist = extra.get("album_artist")
    track_number = extra.get("track_number")
    year = extra.get("release_year")
    isrc = extra.get("isrc")
    _set_or_drop(tags, "aART", [str(album_artist)] if album_artist else None)
    _set_or_drop(tags, "trkn", [(int(track_number), 0)] if track_number is not None else None)
    _set_or_drop(tags, "\xa9day", [str(year)] if year else None)
    tags[MP4_YOUTUBE_ID_KEY] = [MP4FreeForm(video_id.encode("utf-8"))]
    _set_or_drop(tags, MP4_ISRC_KEY, [MP4FreeForm(str(isrc).encode("utf-8"))] if isrc else None)
    if cover is not None:
        data, mime = cover
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        tags["covr"] = [MP4Cover(data, fmt)]


def _write_vorbis_extras(
    audio: FLAC | OggOpus | OggVorbis,
    ref: TrackRef,
    video_id: str,
    cover: tuple[bytes, str] | None,
) -> None:
    extra = ref.extra or {}
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    assert tags is not None
    album_artist = extra.get("album_artist")
    track_number = extra.get("track_number")
    year = extra.get("release_year")
    isrc = extra.get("isrc")
    _set_or_drop(tags, "album_artist", None)  # the non-standard spelling some tools write
    _set_or_drop(tags, "albumartist", [str(album_artist)] if album_artist else None)
    _set_or_drop(tags, "tracknumber", [str(track_number)] if track_number is not None else None)
    _set_or_drop(tags, "date", [str(year)] if year else None)
    tags[TXXX_YOUTUBE_ID] = [video_id]
    _set_or_drop(tags, "isrc", [str(isrc)] if isrc else None)
    if cover is not None:
        data, mime = cover
        picture = Picture()
        picture.type = 3
        picture.mime = mime
        picture.data = data
        if isinstance(audio, FLAC):
            audio.clear_pictures()
            audio.add_picture(picture)
        else:
            tags["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]


def write_spotify_tags(
    path: Path, ref: TrackRef, video_id: str, cover: tuple[bytes, str] | None
) -> bool:
    """Replace the YouTube tags with Spotify's and return whether a cover is embedded.

    The base frames (artist, title, album, comment = Spotify URL, ULTIMATE_PLAYLIST_ID) go
    through the YouTube provider's `write_tags`; album artist, track number, year, the YouTube
    id, the ISRC and the Spotify cover are added here in each container's own format, and the
    first four are removed when Spotify has no value for them.
    """
    path = Path(path)
    artist = ref.artist or "Unknown Artist"
    title = ref.title or "Unknown Title"
    has_cover = youtube.write_tags(path, artist, title, ref.album, ref.url, ref.track_id)
    if path.suffix.lower() == ".mp3":
        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            tags = ID3()
        _write_id3_extras(tags, ref, video_id, cover)
        tags.save(str(path), v2_version=3)
        return bool(tags.getall("APIC"))
    audio = mutagen.File(str(path))
    if isinstance(audio, MP4):
        _write_mp4_extras(audio, ref, video_id, cover)
        audio.save()
        return bool(audio.tags and audio.tags.get("covr"))
    if isinstance(audio, FLAC | OggOpus | OggVorbis):
        _write_vorbis_extras(audio, ref, video_id, cover)
        audio.save()
        if getattr(audio, "pictures", None):
            return True
        return bool(audio.tags and audio.tags.get("metadata_block_picture"))
    return has_cover


# --------------------------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------------------------


def _staging_dir(root: Path, ref: TrackRef) -> Path:
    """A folder of its own for one Spotify download: <library>/.incoming/sp-<id>-<random>."""
    safe_id = _STAGING_ID_RE.sub("", ref.source_id or "")[:32] or "track"
    return root / STAGING_DIR_NAME / f"sp-{safe_id}-{uuid.uuid4().hex[:8]}"


class SpotifyProvider:
    name = "spotify"
    display_name = "Spotify"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    def configure(self, settings: Settings) -> None:
        """Use these (live) settings for resolve()/doctor(); the registry calls this at start."""
        self._settings = settings

    def _current_settings(self) -> Settings:
        return self._settings if self._settings is not None else Settings.load()

    # -- matching ------------------------------------------------------------------------------

    def matches(self, url: str) -> bool:
        url = (url or "").strip()
        if not url:
            return False
        if url.lower().startswith("spotify:"):
            return True
        if "://" not in url:
            url = "https://" + url
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return False
        if host.startswith("www."):
            host = host[4:]
        return host in _HOSTS

    # -- resolving -----------------------------------------------------------------------------

    def resolve(self, url: str) -> list[TrackRef]:
        kind, entity_id = parse_spotify_url(url)
        settings = self._current_settings()
        entity = get_metadata(kind, entity_id, settings)
        refs = refs_from_entity(entity)
        if entity.kind != "track":
            log.info(
                "Spotify %s '%s': %d playable track(s) via %s%s",
                entity.kind,
                entity.name,
                len(refs),
                entity.source,
                " (list may be capped; connect Spotify in Settings for your own playlists)"
                if entity.truncated
                else "",
            )
        return refs

    # -- downloading ---------------------------------------------------------------------------

    @staticmethod
    def _youtube_provider(settings: Settings) -> Any:
        from . import provider_by_name

        provider = provider_by_name("youtube")
        if provider is not None:
            return provider
        return youtube.YouTubeProvider(settings)

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
        label = f"{ref.artist or 'Unknown Artist'} - {ref.title or 'Unknown Title'}"
        staging = _staging_dir(dest_dir, ref)
        try:
            if cancel.is_set():
                raise DownloadCancelled("Download cancelled")
            progress(
                ProgressEvent(
                    JobStatus.DOWNLOADING, progress=0.0, message="Searching YouTube Music…"
                )
            )
            match = find_match(ref, settings, cancel)
            if match is None:
                raise ProviderError(
                    f"Couldn't find '{label}' on YouTube Music (no close enough match)."
                )
            ref.extra["youtube_id"] = match.video_id
            if not ref.album and match.album:
                # Spotify's public track page names no album; YouTube Music's catalogue does.
                ref.album = match.album
            progress(
                ProgressEvent(
                    JobStatus.DOWNLOADING,
                    progress=0.0,
                    message=f"Matched: {match.title} ({match.channel or '?'}, "
                    f"{_mmss(match.duration)})",
                )
            )
            if cancel.is_set():
                raise DownloadCancelled("Download cancelled")
            staging.mkdir(parents=True, exist_ok=True)
            yt_track = self._download_via_youtube(ref, match, staging, settings, progress, cancel)
            produced = staging / Path(*yt_track.path.split("/"))
            if not produced.is_file():
                raise ProviderError("The YouTube download did not produce a file.")
            if cancel.is_set():
                raise DownloadCancelled("Download cancelled")

            progress(
                ProgressEvent(JobStatus.CONVERTING, progress=1.0, message="Writing Spotify tags")
            )
            cover = None
            if settings.embed_cover and ref.thumbnail_url:
                cover = fetch_cover(ref.thumbnail_url)
            has_cover = write_spotify_tags(produced, ref, match.video_id, cover)

            wanted = dest_dir / f"{youtube.safe_filename(label)}{produced.suffix.lower()}"
            with youtube._move_lock:
                final = youtube.destination_for(wanted, ref.track_id)
                try:
                    os.replace(produced, final)
                except PermissionError as exc:
                    raise ProviderError(
                        f"Couldn't replace {final.name} because it is in use (is it playing?). "
                        "Stop playback and retry."
                    ) from exc
            duration = youtube._audio_length(final) or ref.duration
            return Track(
                id=ref.track_id,
                provider=self.name,
                source_id=ref.source_id,
                source_url=ref.url,
                title=ref.title or "Unknown Title",
                artist=ref.artist or "Unknown Artist",
                path=final.relative_to(dest_dir).as_posix(),
                album=ref.album,
                duration=duration,
                has_cover=has_cover,
                file_size=final.stat().st_size,
            )
        except DownloadCancelled:
            raise
        except ProviderError:
            raise
        except Exception as exc:
            log.exception("Unexpected failure downloading %s", ref.url)
            raise ProviderError(f"Spotify download failed: {exc}") from exc
        finally:
            # Whatever is left in this download's own folder (a failed or cancelled step, the
            # YouTube step's temporary files) goes; the finished file has been moved out.
            shutil.rmtree(staging, ignore_errors=True)

    def _download_via_youtube(
        self,
        ref: TrackRef,
        match: Match,
        staging: Path,
        settings: Settings,
        progress: ProgressCallback,
        cancel: threading.Event,
    ) -> Track:
        yt_ref = TrackRef(
            provider="youtube",
            source_id=match.video_id,
            url=match.url,
            title=ref.title,
            artist=ref.artist,
            album=ref.album,
            duration=ref.duration,
        )
        provider = self._youtube_provider(settings)
        with _video_lock(match.video_id):
            return provider.download(yt_ref, staging, settings, progress, cancel)

    # -- diagnostics ---------------------------------------------------------------------------

    def doctor(self, settings: Settings | None = None) -> list[tuple[bool, str, str]]:
        """Which metadata route is active. Offline, except one token renewal when the stored
        access token has expired (that is how a revoked connection is noticed)."""
        try:
            settings = settings if settings is not None else self._current_settings()
            client_id = client_id_of(settings)
        except Exception as exc:  # noqa: BLE001 - doctor never raises
            log.debug("Could not load settings for doctor(): %s", exc)
            client_id = ""
        if not client_id:
            return [(True, "Spotify", DOCTOR_PUBLIC_ONLY)]
        try:
            account = spotify_auth.current_account(client_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not read the Spotify connection: %s", exc)
            account = None
        if account is None:
            return [(True, "Spotify", DOCTOR_NOT_CONNECTED)]
        if account.expires_at - spotify_auth.EXPIRY_MARGIN <= spotify_auth._clock():
            try:
                spotify_auth.access_token(client_id)
            except SpotifyAuthError as exc:
                return [(False, "Spotify", str(exc))]
            except Exception as exc:  # noqa: BLE001 - offline right now: still connected
                log.debug("Could not renew the Spotify token for doctor(): %s", exc)
        return [(True, "Spotify", DOCTOR_CONNECTED.format(name=account.display_name))]
