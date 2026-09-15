"""Spotify metadata: link parsing, the public embed pages and the Web API.

Spotify is only ever asked for *metadata* (names, artists, album, cover, duration). No Spotify
audio is touched; the audio comes from YouTube Music through the YouTube provider. Two sources:

* ``EmbedClient``: ``https://open.spotify.com/embed/{track|album|playlist}/{id}`` is a public
  page that needs no account. It carries a ``<script id="__NEXT_DATA__">`` JSON blob whose
  ``props.pageProps.state.data.entity`` is the track / album / playlist. Playlist track lists
  are capped (``EMBED_LIST_CAP``, observed: 100 items), a single track carries no album name
  and playlist items carry no per-track cover.
* ``UserApiClient``: the Web API as the account the user connected in Settings
  (``spotify_auth``: their own developer app's Client ID, Authorization Code + PKCE, no
  secret). No cap, full metadata (album, ISRC, track number, release year), and the only way
  to read Liked Songs. Since March 2026 Spotify returns playlist contents only for playlists
  the user owns or collaborates on; any other playlist is read from its embed page instead.

``get_metadata`` uses the Web API when the configured Client ID has a connected account and the
embed page otherwise. Everything here is offline-testable: the HTTP session is injectable and
``parse_embed_html`` / ``parse_spotify_url`` are pure.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests

from . import spotify_auth
from .base import DownloadCancelled, ProviderError
from .spotify_auth import SpotifyAccount, SpotifyAuthError

log = logging.getLogger(__name__)

# A desktop browser UA: the embed page and short links answer differently to bare clients.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
EMBED_URL = "https://open.spotify.com/embed/{kind}/{id}"
EMBED_LIST_CAP = 100  # observed: the embed page lists at most this many playlist tracks
API_BASE = "https://api.spotify.com/v1"
API_PAGE_LIMIT = 50  # the documented maximum for album tracks, playlist items and saved tracks
MAX_PAGES = 400  # 20 000 items: a guard against a `next` loop, not a feature
HTTP_TIMEOUT = 15.0
SHORT_LINK_TIMEOUT = 10.0
RETRY_AFTER_CAP = 30.0

SUPPORTED_KINDS: tuple[str, ...] = ("track", "album", "playlist")
LIKED_KIND = "liked"  # Liked Songs (open.spotify.com/collection/tracks); Web API only
LIKED_ID = "tracks"
LIKED_NAME = "Liked Songs"
LIKED_URL = "https://open.spotify.com/collection/tracks"

UNSUPPORTED_KINDS_MESSAGE = (
    "Only Spotify tracks, albums, playlists and your Liked Songs are supported (not artists, "
    "podcasts or profiles)."
)
NOT_FOUND_MESSAGE = "That Spotify link does not exist or is private."
NOT_A_LINK_MESSAGE = "That doesn't look like a Spotify track, album or playlist link."
LIKED_NEEDS_CONNECTION_MESSAGE = "Connect Spotify in Settings to download your Liked Songs"
NOT_CONNECTED_MESSAGE = "Spotify is not connected any more. Click Connect Spotify in Settings."
NOT_YOURS_WARNING = "Spotify only shares the first %d songs of playlists you don't own"
FORBIDDEN_MESSAGE = (
    "Spotify refused this request (HTTP 403). Check that your Spotify account is listed under "
    "User Management of your developer app and that the app owner has Spotify Premium."
)
RATE_LIMIT_MESSAGE = "Spotify is rate-limiting us, try again in a minute."
UNREACHABLE_MESSAGE = "Could not reach Spotify. Check your internet connection and try again."

_HOSTS = frozenset({"open.spotify.com", "play.spotify.com"})
_SHORT_HOSTS = frozenset({"spotify.link", "spotify.app.link"})
_ID_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")  # base62 ids are 22 characters; stay lenient
_URI_RE = re.compile(
    r"^spotify:(?:user:[^:]+:)?(?P<kind>[a-z]+):(?P<id>[A-Za-z0-9]+)$", re.IGNORECASE
)
_INTL_RE = re.compile(r"^intl-[a-z]{2,3}(?:-[a-z]{2,4})?$", re.IGNORECASE)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>', re.DOTALL | re.IGNORECASE
)
_OPEN_URL_RE = re.compile(r"https?://open\.spotify\.com/[^\s\"'<>\\]+")
_NBSP = "\xa0"  # NO-BREAK SPACE


class NotYourPlaylist(ProviderError):
    """The Web API shares no contents for this playlist (the user neither owns it nor
    collaborates on it, or it is one of Spotify's own). `get_metadata` reads its public page."""


# --------------------------------------------------------------------------------------------
# The entity
# --------------------------------------------------------------------------------------------


@dataclass
class SpotifyEntity:
    """A track, or an album / playlist / Liked Songs with its tracks. Optional fields may be None."""

    kind: str  # "track" | "album" | "playlist" | "liked"
    id: str
    name: str
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    album_artist: str | None = None
    duration: float | None = None  # seconds
    cover_url: str | None = None
    release_year: str | None = None
    track_number: int | None = None
    isrc: str | None = None
    tracks: list[SpotifyEntity] = field(default_factory=list)
    truncated: bool = False  # the source could not list every track (embed cap)
    source: str = "embed"  # "embed" | "api"

    @property
    def url(self) -> str:
        if self.kind == LIKED_KIND:
            return LIKED_URL
        return f"https://open.spotify.com/{self.kind}/{self.id}"


# --------------------------------------------------------------------------------------------
# Link parsing
# --------------------------------------------------------------------------------------------


def _is_open_spotify(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").lower() in _HOSTS
    except ValueError:
        return False


def _resolve_short_link(url: str, session: Any | None) -> str:
    """Follow a spotify.link / spotify.app.link redirect to the open.spotify.com URL."""
    sess = session if session is not None else requests.Session()
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = sess.head(url, allow_redirects=True, timeout=SHORT_LINK_TIMEOUT, headers=headers)
        final = str(getattr(resp, "url", "") or "")
        if not _is_open_spotify(final):
            resp = sess.get(url, allow_redirects=True, timeout=SHORT_LINK_TIMEOUT, headers=headers)
            final = str(getattr(resp, "url", "") or "")
            if not _is_open_spotify(final):
                match = _OPEN_URL_RE.search(str(getattr(resp, "text", "") or ""))
                if match:
                    final = match.group(0)
    except requests.RequestException as exc:
        raise ProviderError(
            f"Could not open that Spotify short link ({exc.__class__.__name__}). "
            "Check your internet connection and try again."
        ) from exc
    if not _is_open_spotify(final):
        raise ProviderError(
            "That Spotify short link did not lead to a track, album or playlist. "
            "Open it in your browser and copy the open.spotify.com address instead."
        )
    return final


def _kind_and_id(kind: str, ident: str) -> tuple[str, str]:
    kind = kind.lower()
    if kind not in SUPPORTED_KINDS:
        raise ProviderError(UNSUPPORTED_KINDS_MESSAGE)
    if not _ID_RE.match(ident):
        raise ProviderError(NOT_A_LINK_MESSAGE)
    return kind, ident


def parse_spotify_url(url: str, session: Any | None = None) -> tuple[str, str]:
    """``(kind, id)`` for a Spotify link or URI.

    Accepts ``https://open.spotify.com/{track|album|playlist}/<id>`` (with an optional
    ``/intl-xx/`` prefix, ``/embed/`` segment, old ``/user/<name>/playlist/<id>`` shape, query
    string, trailing slash), ``play.spotify.com``, ``spotify:track:<id>`` URIs (also the old
    ``spotify:user:<name>:playlist:<id>``) and ``spotify.link`` / ``spotify.app.link`` short links
    (resolved over the network with `session`, default: a fresh requests session). Liked Songs,
    ``https://open.spotify.com/collection/tracks``, is ``("liked", "tracks")``.

    Raises ProviderError with a friendly message for artists, podcasts, profiles and anything
    that is not a Spotify link.
    """
    text = (url or "").strip()
    if not text:
        raise ProviderError(NOT_A_LINK_MESSAGE)
    uri = _URI_RE.match(text)
    if uri:
        if uri.group("kind").lower() == "collection" and uri.group("id").lower() == LIKED_ID:
            return LIKED_KIND, LIKED_ID
        return _kind_and_id(uri.group("kind"), uri.group("id"))
    if text.lower().startswith("spotify:"):
        raise ProviderError(UNSUPPORTED_KINDS_MESSAGE)
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise ProviderError(NOT_A_LINK_MESSAGE) from exc
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host in _SHORT_HOSTS:
        return parse_spotify_url(_resolve_short_link(text, session))
    if host not in _HOSTS:
        raise ProviderError(NOT_A_LINK_MESSAGE)
    segments = [s for s in parts.path.split("/") if s]
    if segments and _INTL_RE.match(segments[0]):
        segments = segments[1:]
    if segments and segments[0].lower() in ("embed", "embed-podcast"):
        segments = segments[1:]
    if segments and segments[0].lower() == "collection":
        if len(segments) >= 2 and segments[1].lower() == LIKED_ID:
            return LIKED_KIND, LIKED_ID
        raise ProviderError(UNSUPPORTED_KINDS_MESSAGE)  # saved albums, podcasts, ...
    if len(segments) >= 4 and segments[0].lower() == "user":
        segments = segments[2:]  # /user/<name>/playlist/<id>
    if len(segments) < 2:
        if segments and segments[0].lower() in ("user", "artist", "show", "episode"):
            raise ProviderError(UNSUPPORTED_KINDS_MESSAGE)
        raise ProviderError(NOT_A_LINK_MESSAGE)
    return _kind_and_id(segments[0], segments[1])


# --------------------------------------------------------------------------------------------
# Shared parsing helpers
# --------------------------------------------------------------------------------------------


def _dig(obj: Any, *keys: str) -> Any:
    """Nested dict lookup that returns None instead of raising on any missing/odd level."""
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _clean(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.replace(_NBSP, " ").strip()
        return re.sub(r"\s+", " ", value) or None
    return None


def _seconds(ms: Any) -> float | None:
    if isinstance(ms, bool) or ms is None:
        return None
    try:
        value = float(ms) / 1000.0
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _year(value: Any) -> str | None:
    """ "2013-05-20T00:00:00Z" / "2013-05-20" / "2013" -> "2013"."""
    if isinstance(value, dict):
        value = value.get("isoString") or value.get("year")
    text = str(value).strip() if value is not None else ""
    match = re.match(r"^(\d{4})", text)
    return match.group(1) if match else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _id_from_uri(uri: Any) -> str | None:
    if not isinstance(uri, str):
        return None
    parts = uri.split(":")
    return parts[-1] if len(parts) >= 3 and parts[0] == "spotify" and parts[-1] else None


def split_artists(subtitle: Any) -> list[str]:
    """Artist names from an embed subtitle.

    Spotify joins several artists with a comma followed by a NO-BREAK SPACE
    (``"KAROL G,\\xa0Judeline"``); a name that itself contains a comma keeps a plain space
    (``"Tyler, The Creator"``), so only the NBSP variant is a separator.
    """
    if not isinstance(subtitle, str) or not subtitle.strip():
        return []
    raw = subtitle.split("," + _NBSP) if ("," + _NBSP) in subtitle else [subtitle]
    names = [_clean(name) for name in raw]
    return [name for name in names if name]


# --------------------------------------------------------------------------------------------
# Embed pages
# --------------------------------------------------------------------------------------------


def _embed_cover(entity: dict[str, Any]) -> str | None:
    """The largest image in visualIdentity.image, else the first coverArt source."""
    best_url: str | None = None
    best_width = -1
    visual = entity.get("visualIdentity")
    images = visual.get("image") if isinstance(visual, dict) else None
    for image in images if isinstance(images, list) else []:
        if not isinstance(image, dict) or not isinstance(image.get("url"), str):
            continue
        width = _int_or_none(image.get("maxWidth")) or 0
        if width > best_width:
            best_width, best_url = width, image["url"]
    if best_url:
        return best_url
    cover = entity.get("coverArt")
    sources = cover.get("sources") if isinstance(cover, dict) else None
    for source in sources if isinstance(sources, list) else []:
        if isinstance(source, dict) and isinstance(source.get("url"), str):
            return source["url"]
    return None


def _embed_track_item(
    item: dict[str, Any], container: SpotifyEntity, position: int
) -> SpotifyEntity | None:
    if item.get("entityType") not in (None, "track"):
        return None
    if item.get("isPlayable") is False:
        return None
    track_id = _id_from_uri(item.get("uri"))
    title = _clean(item.get("title")) or _clean(item.get("name"))
    if not track_id or not title:
        return None
    is_album = container.kind == "album"
    return SpotifyEntity(
        kind="track",
        id=track_id,
        name=title,
        artists=split_artists(item.get("subtitle")),
        album=container.name if is_album else None,
        album_artist=container.album_artist if is_album else None,
        duration=_seconds(item.get("duration")),
        cover_url=container.cover_url if is_album else None,
        release_year=container.release_year if is_album else None,
        track_number=position if is_album else None,
        source="embed",
    )


def entity_from_embed(entity: dict[str, Any]) -> SpotifyEntity:
    """Turn the ``__NEXT_DATA__`` entity dict into a SpotifyEntity (pure, unit-tested)."""
    kind = str(entity.get("type") or "").lower()
    if kind not in SUPPORTED_KINDS:
        raise ProviderError(UNSUPPORTED_KINDS_MESSAGE)
    entity_id = _clean(entity.get("id")) or _id_from_uri(entity.get("uri"))
    name = _clean(entity.get("name")) or _clean(entity.get("title"))
    if not entity_id or not name:
        raise ProviderError("Could not read that Spotify page (the page had no track data).")
    if kind == "track":
        artists = entity.get("artists")
        names = [
            _clean(a.get("name"))
            for a in (artists if isinstance(artists, list) else []) or []
            if isinstance(a, dict)
        ]
        return SpotifyEntity(
            kind="track",
            id=entity_id,
            name=name,
            artists=[n for n in names if n],
            duration=_seconds(entity.get("duration")),
            cover_url=_embed_cover(entity),
            release_year=_year(entity.get("releaseDate")),
            source="embed",
        )
    subtitle = entity.get("subtitle")
    container = SpotifyEntity(
        kind=kind,
        id=entity_id,
        name=name,
        artists=split_artists(subtitle) if kind == "album" else [],
        album=name if kind == "album" else None,
        album_artist=(", ".join(split_artists(subtitle)) or None) if kind == "album" else None,
        cover_url=_embed_cover(entity),
        release_year=_year(entity.get("releaseDate")) if kind == "album" else None,
        source="embed",
    )
    if kind == "playlist":
        container.album_artist = _clean(subtitle)  # the owner, for the log line only
    items = entity.get("trackList")
    items = items if isinstance(items, list) else []
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        track = _embed_track_item(item, container, position)
        if track is not None:
            container.tracks.append(track)
    container.truncated = len(items) >= EMBED_LIST_CAP
    return container


def parse_embed_html(html: str) -> SpotifyEntity:
    """Extract and parse the entity from an embed page's HTML."""
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        raise ProviderError("Could not read that Spotify page (it did not contain track data).")
    try:
        data = json.loads(match.group("json"))
    except ValueError as exc:
        raise ProviderError("Could not read that Spotify page (broken page data).") from exc
    page = _dig(data, "props", "pageProps")
    if not isinstance(page, dict):
        raise ProviderError("Could not read that Spotify page (unexpected page data).")
    entity = _dig(page, "state", "data", "entity")
    if not isinstance(entity, dict):
        if page.get("status") == 404 or not isinstance(page.get("state"), dict):
            raise ProviderError(NOT_FOUND_MESSAGE)
        raise ProviderError("Could not read that Spotify page (no entity in the page data).")
    return entity_from_embed(entity)


class EmbedClient:
    """Reads public embed pages; no account involved."""

    def __init__(self, session: Any | None = None, timeout: float = HTTP_TIMEOUT) -> None:
        self._session = session
        self._timeout = timeout

    @property
    def session(self) -> Any:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def fetch(self, kind: str, entity_id: str) -> SpotifyEntity:
        if kind == LIKED_KIND:
            raise SpotifyAuthError(LIKED_NEEDS_CONNECTION_MESSAGE)
        kind, entity_id = _kind_and_id(kind, entity_id)
        url = EMBED_URL.format(kind=kind, id=entity_id)
        headers = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
        try:
            resp = self.session.get(url, headers=headers, timeout=self._timeout)
        except requests.RequestException as exc:
            log.debug("Embed request for %s/%s failed: %s", kind, entity_id, exc)
            raise ProviderError(UNREACHABLE_MESSAGE) from exc
        status = int(getattr(resp, "status_code", 0) or 0)
        if status == 404:
            raise ProviderError(NOT_FOUND_MESSAGE)
        if status >= 400:
            raise ProviderError(f"Could not read that Spotify page (HTTP {status}).")
        entity = parse_embed_html(str(getattr(resp, "text", "") or ""))
        if entity.truncated:
            log.warning(
                "Spotify's public page lists at most about %d tracks; %s %s may be longer. "
                "Connect Spotify in Settings to read your own playlists of any size.",
                EMBED_LIST_CAP,
                kind,
                entity_id,
            )
        return entity


# --------------------------------------------------------------------------------------------
# Web API (as the connected user)
# --------------------------------------------------------------------------------------------


def _api_artists(obj: dict[str, Any] | None) -> list[str]:
    artists = obj.get("artists") if isinstance(obj, dict) else None
    names = [
        _clean(a.get("name"))
        for a in (artists if isinstance(artists, list) else []) or []
        if isinstance(a, dict)
    ]
    return [n for n in names if n]


def _api_cover(obj: dict[str, Any] | None) -> str | None:
    images = obj.get("images") if isinstance(obj, dict) else None
    best_url: str | None = None
    best_width = -1
    for image in images if isinstance(images, list) else []:
        if not isinstance(image, dict) or not isinstance(image.get("url"), str):
            continue
        width = _int_or_none(image.get("width")) or 0
        if width > best_width:
            best_width, best_url = width, image["url"]
    return best_url


def _api_track(track: Any, album: dict[str, Any] | None = None) -> SpotifyEntity | None:
    """A full or simplified track object -> SpotifyEntity; `album` overrides the track's own."""
    if not isinstance(track, dict) or track.get("is_local"):
        return None
    if track.get("type") not in (None, "track"):
        return None  # podcast episodes in playlists
    track_id = _clean(track.get("id"))
    name = _clean(track.get("name"))
    if not track_id or not name:
        return None
    album = album if album is not None else track.get("album")
    album = album if isinstance(album, dict) else {}
    external = track.get("external_ids")
    isrc = _clean(external.get("isrc")) if isinstance(external, dict) else None
    album_artists = _api_artists(album)
    return SpotifyEntity(
        kind="track",
        id=track_id,
        name=name,
        artists=_api_artists(track),
        album=_clean(album.get("name")),
        album_artist=", ".join(album_artists) or None,
        duration=_seconds(track.get("duration_ms")),
        cover_url=_api_cover(album),
        release_year=_year(album.get("release_date")),
        track_number=_int_or_none(track.get("track_number")),
        isrc=isrc,
        source="api",
    )


def _item_track(item: dict[str, Any], *keys: str) -> Any:
    """The track object of a playlist / saved-track item: the first of `keys` holding a dict."""
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            return value
    return None


class UserApiClient:
    """The Spotify Web API as the account connected through `spotify_auth`.

    Access tokens come from ``spotify_auth.access_token(client_id)``, which renews them; a 401
    renews once more and then gives up with SpotifyAuthError. `session` (anything with a
    requests-style ``get``) and `sleep` are injectable for tests.
    """

    def __init__(
        self,
        client_id: str,
        session: Any | None = None,
        timeout: float = HTTP_TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client_id = (client_id or "").strip()
        self._session = session
        self._timeout = timeout
        self._sleep = sleep

    @property
    def session(self) -> Any:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    # -- requests ------------------------------------------------------------------------------

    def _token(self, cancel: threading.Event | None, stale: str | None = None) -> str:
        token = spotify_auth.access_token(self.client_id, cancel, stale_token=stale)
        if not token:
            raise SpotifyAuthError(NOT_CONNECTED_MESSAGE)
        return token

    def _wait_retry_after(self, resp: Any, cancel: threading.Event | None) -> None:
        raw = None
        headers = getattr(resp, "headers", None)
        if headers is not None:
            raw = headers.get("Retry-After")
        try:
            delay = float(raw) if raw is not None else 1.0
        except (TypeError, ValueError):
            delay = 1.0
        delay = max(0.0, min(delay, RETRY_AFTER_CAP))
        log.info("Spotify asked us to wait %.0f s (rate limit)", delay)
        step = 0.5
        waited = 0.0
        while waited < delay:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("Download cancelled")
            chunk = min(step, delay - waited)
            self._sleep(chunk)
            waited += chunk

    def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        cancel: threading.Event | None = None,
        what: str = "",
    ) -> dict[str, Any]:
        """GET an API path or URL. `what` ("playlist" / "items" for playlist requests) decides
        whether a 403/404 means "not your playlist" (NotYourPlaylist) or a plain error."""
        if not url.startswith("http"):
            url = API_BASE + url
        token = self._token(cancel)
        retried_token = False
        retried_rate = False
        while True:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelled("Download cancelled")
            headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self._timeout)
            except requests.RequestException as exc:
                log.debug("Spotify API request failed: %s", exc.__class__.__name__)
                raise ProviderError(UNREACHABLE_MESSAGE) from None
            status = int(getattr(resp, "status_code", 0) or 0)
            if status == 200:
                try:
                    body = resp.json()
                except ValueError as exc:
                    raise ProviderError("Spotify sent an unreadable answer. Try again.") from exc
                if not isinstance(body, dict):
                    raise ProviderError("Spotify sent an unexpected answer. Try again.")
                return body
            if status == 401 and not retried_token:
                retried_token = True  # the token may have been revoked or expired early
                token = self._token(cancel, stale=token)
                continue
            if status == 401:
                raise SpotifyAuthError(spotify_auth.EXPIRED_CONNECTION_MESSAGE)
            if status == 429 and not retried_rate:
                retried_rate = True
                self._wait_retry_after(resp, cancel)
                continue
            if status == 429:
                raise ProviderError(RATE_LIMIT_MESSAGE)
            if status in (403, 404) and what in ("playlist", "items"):
                raise NotYourPlaylist(NOT_YOURS_WARNING % EMBED_LIST_CAP)
            if status == 403:
                raise ProviderError(FORBIDDEN_MESSAGE)
            if status in (400, 404):
                raise ProviderError(NOT_FOUND_MESSAGE)
            if 500 <= status < 600:
                raise ProviderError(
                    f"Spotify is having problems right now (HTTP {status}). Try again later."
                )
            raise ProviderError(f"Spotify answered with HTTP {status}.")

    def _pages(
        self,
        first: dict[str, Any] | None,
        url: str | None,
        params: dict[str, Any] | None,
        cancel: threading.Event | None,
        what: str,
    ) -> list[dict[str, Any]]:
        """All `items` of a paging object, following `next` (the first page may be given).

        `next` is only followed to api.spotify.com (the access token goes with it) and for at
        most MAX_PAGES pages.
        """
        items: list[dict[str, Any]] = []
        page = first
        pages = 0
        if page is None:
            assert url is not None
            page = self._get(url, params, cancel, what)
        while True:
            pages += 1
            batch = page.get("items")
            items.extend(
                i for i in (batch if isinstance(batch, list) else []) if isinstance(i, dict)
            )
            next_url = page.get("next")
            if not isinstance(next_url, str) or not next_url:
                return items
            if not next_url.startswith(API_BASE + "/"):
                log.warning("Not following an unexpected Spotify page link: %s", next_url[:120])
                return items
            if pages >= MAX_PAGES:
                log.warning(
                    "Stopped reading Spotify after %d pages (%d items); the list may be longer",
                    pages,
                    len(items),
                )
                return items
            page = self._get(next_url, None, cancel, what)

    # -- entities ------------------------------------------------------------------------------

    def fetch(
        self, kind: str, entity_id: str, cancel: threading.Event | None = None
    ) -> SpotifyEntity:
        if kind == LIKED_KIND:
            return self._fetch_liked(cancel)
        kind, entity_id = _kind_and_id(kind, entity_id)
        if kind == "track":
            return self._fetch_track(entity_id, cancel)
        if kind == "album":
            return self._fetch_album(entity_id, cancel)
        return self._fetch_playlist(entity_id, cancel)

    def _fetch_track(self, track_id: str, cancel: threading.Event | None) -> SpotifyEntity:
        data = self._get(f"/tracks/{track_id}", None, cancel, "track")
        track = _api_track(data)
        if track is None:
            raise ProviderError(NOT_FOUND_MESSAGE)
        return track

    def _fetch_album(self, album_id: str, cancel: threading.Event | None) -> SpotifyEntity:
        data = self._get(f"/albums/{album_id}", None, cancel, "album")
        name = _clean(data.get("name"))
        if not name:
            raise ProviderError(NOT_FOUND_MESSAGE)
        album = SpotifyEntity(
            kind="album",
            id=_clean(data.get("id")) or album_id,
            name=name,
            artists=_api_artists(data),
            album=name,
            album_artist=", ".join(_api_artists(data)) or None,
            cover_url=_api_cover(data),
            release_year=_year(data.get("release_date")),
            source="api",
        )
        paging = data.get("tracks") if isinstance(data.get("tracks"), dict) else None
        items = self._pages(
            paging,
            f"/albums/{album_id}/tracks",
            {"limit": API_PAGE_LIMIT},
            cancel,
            "album",
        )
        # Simplified track objects carry no album block: hand them the album's own.
        album_block = {
            "name": name,
            "images": data.get("images"),
            "release_date": data.get("release_date"),
            "artists": data.get("artists"),
        }
        for item in items:
            if item.get("is_playable") is False:
                continue
            track = _api_track(item, album_block)
            if track is not None:
                album.tracks.append(track)
        return album

    def _fetch_playlist(self, playlist_id: str, cancel: threading.Event | None) -> SpotifyEntity:
        # No `fields` filter: its absence of `items` is the "not your playlist" signal, and a
        # filter naming a key Spotify renames would hide that signal.
        data = self._get(f"/playlists/{playlist_id}", None, cancel, "playlist")
        contents = data.get("items") if "items" in data else data.get("tracks")
        if not isinstance(contents, dict):
            raise NotYourPlaylist(NOT_YOURS_WARNING % EMBED_LIST_CAP)
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        playlist = SpotifyEntity(
            kind="playlist",
            id=_clean(data.get("id")) or playlist_id,
            name=_clean(data.get("name")) or "Playlist",
            album_artist=_clean(owner.get("display_name")) or _clean(owner.get("id")),
            cover_url=_api_cover(data),
            source="api",
        )
        items = self._pages(
            None,
            f"/playlists/{playlist_id}/items",
            {"limit": API_PAGE_LIMIT},
            cancel,
            "items",
        )
        for item in items:
            if item.get("is_local"):
                continue
            track = _api_track(_item_track(item, "item", "track"))  # "track" is the old name
            if track is not None:
                playlist.tracks.append(track)
        return playlist

    def _fetch_liked(self, cancel: threading.Event | None) -> SpotifyEntity:
        items = self._pages(None, "/me/tracks", {"limit": API_PAGE_LIMIT}, cancel, "liked")
        liked = SpotifyEntity(kind=LIKED_KIND, id=LIKED_ID, name=LIKED_NAME, source="api")
        for item in items:
            track = _api_track(_item_track(item, "track", "item"))
            if track is not None:
                liked.tracks.append(track)
        return liked


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------


def client_id_of(settings: Any) -> str:
    """The Spotify Client ID from the settings; "" when unset (or on an old settings object)."""
    return str(getattr(settings, "spotify_client_id", "") or "").strip()


def connected_account(settings: Any) -> SpotifyAccount | None:
    """The account connected through the configured Client ID, or None. Offline."""
    client_id = client_id_of(settings)
    if not client_id:
        return None
    try:
        return spotify_auth.current_account(client_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable token file means "not connected"
        log.debug("Could not read the Spotify connection: %s", exc)
        return None


def get_metadata(
    kind: str,
    entity_id: str,
    settings: Any,
    cancel: threading.Event | None = None,
    session: Any | None = None,
) -> SpotifyEntity:
    """The entity through the Web API when Spotify is connected, else the public embed page.

    Liked Songs need a connection (SpotifyAuthError otherwise) and report any Web API error.
    A playlist the API shares no contents for (not the user's own, or Spotify-made) is read
    from its public page, which lists up to EMBED_LIST_CAP songs. So is a track, album or
    playlist whose Web API request fails for any other reason (an expired connection, a 403
    once the developer app's owner has no Premium any more, a rate limit, a 5xx, a network
    error): the public page needs no account, so being connected never makes a link fail that
    works without a connection. Only a cancel is passed on as it is.
    """
    account = connected_account(settings)
    if kind == LIKED_KIND:
        if account is None:
            raise SpotifyAuthError(LIKED_NEEDS_CONNECTION_MESSAGE)
        return UserApiClient(client_id_of(settings), session=session).fetch(kind, entity_id, cancel)
    if account is None:
        return EmbedClient(session=session).fetch(kind, entity_id)
    try:
        return UserApiClient(client_id_of(settings), session=session).fetch(kind, entity_id, cancel)
    except DownloadCancelled:
        raise
    except NotYourPlaylist:
        log.warning(
            NOT_YOURS_WARNING + "; reading playlist %s from its public page",
            EMBED_LIST_CAP,
            entity_id,
        )
    except ProviderError as exc:  # SpotifyAuthError included
        log.warning(
            "Spotify's Web API failed (%s); reading %s %s from its public page",
            exc,
            kind,
            entity_id,
        )
    return EmbedClient(session=session).fetch(kind, entity_id)
