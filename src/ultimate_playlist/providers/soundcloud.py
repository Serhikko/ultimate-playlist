"""SoundCloud provider: tracks and sets (albums, EPs, singles, playlists), as audio, through yt-dlp.

Downloading, converting, tagging and naming are `YouTubeProvider`'s pipeline (this provider is
a subclass); what this module adds:

* the link shapes SoundCloud uses (``parse_link``): ``soundcloud.com/<user>/<track>``, private
  tracks (``.../<track>/s-<token>``), sets (``soundcloud.com/<user>/sets/<set>``, private ones
  too), the ``api.soundcloud.com`` / ``api-v2.soundcloud.com`` track and playlist links, the
  ``w.soundcloud.com/player/?url=...`` embed (its inner link) and the ``on.soundcloud.com``
  short links, which `resolve()` expands with one HEAD request. yt-dlp only ever sees a link
  rebuilt from the checked parts, without the share-tracking query (``?si=``, ``utm_*``);
* a set becomes one track per entry from one request for the whole set (two for a private set
  of more than five tracks), not one per track (``extract_flat``); an album's, EP's, single's or
  compilation's title becomes the album tag of its tracks. yt-dlp's extractor is chosen by the
  kind of link (``_extractor_for``), never left to yt-dlp's first match;
* Go+ tracks, which SoundCloud only plays as a 30-second preview without a subscription, are
  refused instead of being saved as a 30-second file (``YDL_FORMAT``, ``is_preview_only``);
* the name (``pick_tags``): "Artist - Title" from the track title when the uploader is a label
  or the artist themselves, else the artist SoundCloud lists, else the uploader;
* SoundCloud's own error texts (``friendly_error``);
* the cover: SoundCloud's 500x500 artwork, not the "original" upload (often over a megabyte, at
  whatever size it was uploaded) nor SoundCloud's grey default avatar (``TrimArtworkPP``).

Every other page on a SoundCloud host (a profile, likes, a station, Discover, a search) belongs
to this provider too, so `resolve()` can say why it can't be downloaded and what to paste
instead, rather than a bare "No provider for this link".
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import requests
import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor

from ..config import Settings
from ..models import TrackRef
from . import youtube
from .base import ProviderError

log = logging.getLogger(__name__)

PROVIDER_NAME = "soundcloud"
MAIN_HOST = "soundcloud.com"
_MAIN_HOSTS = frozenset({"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"})
_SHORT_HOST = "on.soundcloud.com"
_API_HOSTS = frozenset({"api.soundcloud.com", "api-v2.soundcloud.com"})
_EMBED_HOST = "w.soundcloud.com"
_HOSTS = _MAIN_HOSTS | _API_HOSTS | {_SHORT_HOST, _EMBED_HOST}

# A user or track permalink: the characters yt-dlp's SoundCloud extractor accepts ([\w\d-]+).
_NAME_RE = re.compile(r"^[\w-]+\Z")
# The secret part of a private link: "s-8Pjrp".
_SECRET_RE = re.compile(r"^s-[A-Za-z0-9_-]+\Z")
_SHORT_PATH_RE = re.compile(r"^/[A-Za-z0-9_-]+\Z")  # on.soundcloud.com/KPfBadGrMoV5Jxze7
_API_TRACK_RE = re.compile(r"^/tracks/(?:soundcloud(?::|%3A)tracks(?::|%3A))?([0-9]+)\Z", re.I)
_API_SET_RE = re.compile(r"^/playlists/(?:soundcloud(?::|%3A)playlists(?::|%3A))?([0-9]+)\Z", re.I)
_API_USER_RE = re.compile(r"^/users/", re.I)
_EMBED_PATH_RE = re.compile(r"^/player\Z")

# The tabs of a profile page: soundcloud.com/<user>/<tab>.
_PROFILE_TABS = frozenset(
    {
        "tracks",
        "albums",
        "sets",
        "reposts",
        "likes",
        "spotlight",
        "popular-tracks",
        "followers",
        "following",
        "comments",
    }
)
_LIKES_TABS = frozenset({"likes", "reposts"})
# Lists SoundCloud builds around one track: soundcloud.com/<user>/<track>/<this>.
_TRACK_RELATED = frozenset({"recommended", "albums", "sets"})
# First path segments under which every page is SoundCloud's own (soundcloud.com/tags/lofi,
# /pages/privacy, /settings/content): no user has these names (SoundCloud's API knows no such
# user), so any path there is refused without a request. "discover", "charts", "search",
# "stations" and "you" get sentences of their own (_parse_main).
_SITE_SECTIONS = frozenset(
    {
        "charts",
        "discover",
        "messages",
        "pages",
        "search",
        "settings",
        "stations",
        "tags",
        "you",
    }
)
# Names of SoundCloud's own top-level pages (soundcloud.com/upload, /feed). Some are also real
# accounts: "library" has public tracks and sets, "home" and "jobs" exist too. So only a
# page-shaped path is refused: the name alone or with a profile tab (/library, /library/likes).
# /<name>/<track> and /<name>/sets/<set> are read as any user's links; one that is no track or
# set fails with SoundCloud's 404 (_NOT_FOUND).
_SITE_PAGES = frozenset(
    {
        "apps",
        "artists",
        "connect",
        "creators",
        "explore",
        "feed",
        "go",
        "groups",
        "home",
        "imprint",
        "jobs",
        "library",
        "logout",
        "mobile",
        "notifications",
        "people",
        "playlists",
        "popular",
        "premium",
        "pro",
        "signin",
        "signup",
        "stream",
        "terms-of-use",
        "tracks",
        "upload",
    }
)

# Never a 30-second preview: a Go+ track offers nothing else, so it fails with "Requested format
# is not available" (explained as GO_PLUS) instead of being saved cut off.
YDL_FORMAT = "bestaudio[format_id!*=preview]/best[format_id!*=preview]"
SHORT_LINK_TIMEOUT = 15.0  # seconds

# SoundCloud's set types (yt-dlp's album_type) that are a release: their title is the album tag.
_SET_LABELS = {"album": "Album", "ep": "EP", "single": "Single", "compilation": "Compilation"}
ALBUM_SET_TYPES = frozenset(_SET_LABELS)


# --------------------------------------------------------------------------------------------
# Links (offline)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SoundCloudLink:
    """What a SoundCloud link points at: a track, a set, a short link, or anything else (then
    ``reason`` says what: "profile", "likes", "station", "discover", "search", "account",
    "embed", "home" or "page")."""

    kind: str  # "track" | "set" | "short" | "other"
    host: str = MAIN_HOST  # one of _HOSTS, lower case; www. and m. become soundcloud.com
    path: str = "/"  # checked path, without the secret part
    secret: str | None = None  # "s-..." of a private track or set
    reason: str = ""

    @property
    def is_api(self) -> bool:
        return self.host in _API_HOSTS

    @property
    def clean_url(self) -> str:
        """The link rebuilt from its checked parts: the network only ever sees a SoundCloud
        host, whatever query, userinfo, port or backslash tricks the pasted text contained."""
        url = f"https://{self.host}{self.path}"
        if self.secret:
            url += f"?secret_token={self.secret}" if self.is_api else f"/{self.secret}"
        return url


def parse_link(url: str) -> SoundCloudLink | None:
    """Classify a link without touching the network; None when it is not on a SoundCloud
    host."""
    return _parse(url, allow_embed=True)


def _parse(url: str, allow_embed: bool) -> SoundCloudLink | None:
    try:
        parts = urlsplit(youtube.normalize_url(url))
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if host not in _HOSTS:
        return None
    path = (parts.path or "/").rstrip("/") or "/"
    if host in _MAIN_HOSTS:
        return _parse_main(path)
    if host == _SHORT_HOST:
        if _SHORT_PATH_RE.match(path):
            return SoundCloudLink("short", host=host, path=path)
        return SoundCloudLink("other", host=host, path=path, reason="page")
    if host in _API_HOSTS:
        return _parse_api(host, path, parts.query)
    # w.soundcloud.com/player/?url=<inner>: the player of the track or set in `url`.
    if allow_embed and _EMBED_PATH_RE.match(path):
        query = parse_qs(parts.query)
        inner = _parse((query.get("url") or [""])[0], allow_embed=False)
        if inner is not None and inner.kind in ("track", "set"):
            secret = (query.get("secret_token") or [""])[0]
            if inner.is_api and not inner.secret and _SECRET_RE.match(secret):
                inner = SoundCloudLink(inner.kind, inner.host, inner.path, secret)
            return inner
    return SoundCloudLink("other", host=host, path=path, reason="embed")


def _parse_main(path: str) -> SoundCloudLink:
    segments = path.strip("/").split("/") if path != "/" else []
    if not segments:
        return SoundCloudLink("other", path=path, reason="home")
    if any(not segment for segment in segments):
        return SoundCloudLink("other", path=path, reason="page")
    first = segments[0].lower()
    if first in ("discover", "charts"):
        return SoundCloudLink("other", path=path, reason="discover")
    if first == "search":
        return SoundCloudLink("other", path=path, reason="search")
    if first == "stations":
        return SoundCloudLink("other", path=path, reason="station")
    if first == "you":
        return SoundCloudLink("other", path=path, reason="account")
    if first in _SITE_SECTIONS or not _NAME_RE.match(segments[0]):
        return SoundCloudLink("other", path=path, reason="page")
    if first in _SITE_PAGES and (
        len(segments) == 1 or (len(segments) == 2 and segments[1].lower() in _PROFILE_TABS)
    ):
        return SoundCloudLink("other", path=path, reason="page")
    user = segments[0]
    if len(segments) == 1:
        return SoundCloudLink("other", path=path, reason="profile")
    second = segments[1]
    if len(segments) == 2:
        if second.lower() in _LIKES_TABS:
            return SoundCloudLink("other", path=path, reason="likes")
        if second.lower() in _PROFILE_TABS:
            return SoundCloudLink("other", path=path, reason="profile")
        if _NAME_RE.match(second):
            return SoundCloudLink("track", path=f"/{user}/{second}")
        return SoundCloudLink("other", path=path, reason="page")
    third = segments[2]
    if second.lower() == "sets":
        # /<user>/sets/<set>[/s-<token>]
        if len(segments) <= 4 and _NAME_RE.match(third):
            secret = segments[3] if len(segments) == 4 else None
            if secret is None or _SECRET_RE.match(secret):
                return SoundCloudLink("set", path=f"/{user}/sets/{third}", secret=secret)
        return SoundCloudLink("other", path=path, reason="page")
    if second.lower() in _PROFILE_TABS or not _NAME_RE.match(second):
        return SoundCloudLink("other", path=path, reason="page")
    if len(segments) == 3 and _SECRET_RE.match(third):
        return SoundCloudLink("track", path=f"/{user}/{second}", secret=third)
    if third.lower() in _TRACK_RELATED:
        return SoundCloudLink("other", path=path, reason="station")
    return SoundCloudLink("other", path=path, reason="page")


def _parse_api(host: str, path: str, query: str) -> SoundCloudLink:
    secret = (parse_qs(query).get("secret_token") or [""])[0]
    secret = secret if _SECRET_RE.match(secret) else None
    track = _API_TRACK_RE.match(path)
    if track:
        return SoundCloudLink("track", host=host, path=f"/tracks/{track.group(1)}", secret=secret)
    playlist = _API_SET_RE.match(path)
    if playlist:
        return SoundCloudLink(
            "set", host=host, path=f"/playlists/{playlist.group(1)}", secret=secret
        )
    if _API_USER_RE.match(path):
        return SoundCloudLink("other", host=host, path=path, reason="profile")
    return SoundCloudLink("other", host=host, path=path, reason="page")


_SHARE_HINT = "(Share, then Copy link)"
_PROFILE_MESSAGE = (
    "That's a SoundCloud profile, not a track or a playlist. Open one of its tracks or playlists "
    f"and copy that link {_SHARE_HINT}."
)
_DEFAULT_UNSUPPORTED = (
    "That SoundCloud link isn't a track or a playlist. Copy the link from the track or playlist "
    f"itself {_SHARE_HINT}."
)
_UNSUPPORTED_MESSAGES = {
    "profile": _PROFILE_MESSAGE,
    "likes": (
        "That's a SoundCloud profile's likes or reposts, not a track or a playlist. Open one of "
        f"the tracks and copy its link {_SHARE_HINT}."
    ),
    "station": (
        "SoundCloud makes that list on the fly (a station, or the tracks and playlists it "
        "gathers around a track), so it can't be downloaded. Open one of its tracks or "
        f"playlists and copy that link {_SHARE_HINT}."
    ),
    "discover": (
        "SoundCloud's Discover pages can't be downloaded: their mixes are made for each listener "
        "and need a sign-in, and the charts are gone. Open a track or playlist and copy that "
        f"link {_SHARE_HINT}."
    ),
    "search": (
        f"That's a SoundCloud search. Open one of the results and copy its link {_SHARE_HINT}."
    ),
    "account": (
        "That page belongs to a signed-in SoundCloud account, which Ultimate Playlist doesn't "
        f"support. Open one of the tracks or playlists and copy that link {_SHARE_HINT}."
    ),
}
# Pages a short link may lead to that get their own explanation instead of "deleted".
_EXPLAINED_REASONS = frozenset(_UNSUPPORTED_MESSAGES)


def unsupported_message(link: SoundCloudLink) -> str:
    """Why a SoundCloud link that is not a track or a set cannot be downloaded and, where there
    is one, what to paste instead."""
    return _UNSUPPORTED_MESSAGES.get(link.reason, _DEFAULT_UNSUPPORTED)


SHORT_LINK_DEAD = (
    "That SoundCloud link doesn't lead to a track or a playlist any more (it may have been "
    "deleted)."
)


def _short_link_status_message(code: int) -> str:
    """SoundCloud answered the short link itself with an error instead of a redirect."""
    if code == 429:
        return "SoundCloud is rate-limiting this computer. Wait a few minutes and try again."
    if code >= 500:
        return "SoundCloud is having problems right now. Try again later."
    if code in (404, 410):
        return SHORT_LINK_DEAD
    return (
        f"SoundCloud wouldn't open that short link (HTTP {code}). Try again in a minute, or open "
        "it in your browser and paste the full link it leads to."
    )


def expand_short_link(link: SoundCloudLink) -> str:
    """Where an on.soundcloud.com link leads: one HEAD request, sent to on.soundcloud.com only
    (see `SoundCloudLink.clean_url`); the redirect is read, not followed."""
    try:
        response = requests.head(link.clean_url, allow_redirects=False, timeout=SHORT_LINK_TIMEOUT)
    except requests.RequestException as exc:
        raise ProviderError(
            "Couldn't reach SoundCloud. Check your internet connection and try again. "
            f"({type(exc).__name__})"
        ) from exc
    status = int(response.status_code)
    location = response.headers.get("Location") if 300 <= status < 400 else None
    if location:
        return urljoin(link.clean_url, str(location))
    if status >= 400:
        raise ProviderError(_short_link_status_message(status))
    raise ProviderError(SHORT_LINK_DEAD)  # answered without leading anywhere


# --------------------------------------------------------------------------------------------
# Naming (offline)
# --------------------------------------------------------------------------------------------

# "Artist - Title" on a dash only: on SoundCloud "|" separates a label or a hashtag-ish tail
# ("Still Alive By Boss, Harp | Coin Digital | ..."), not the artist.
_DASH_SPLIT_RE = re.compile(r"^\s*(.+?)\s+[-–—]\s+(.+?)\s*$")
# Tails SoundCloud uploaders put on titles: "[NCS Release]", "[Free Download]", "(FREE DL)",
# "[OUT NOW]". A release marker only in square brackets: "(2019 Release)" is a version.
_SC_TAIL_RE = re.compile(
    r"\s*(?:\[\s*[\w .&'!-]+?\s+release\s*\]"
    r"|[\[(]\s*(?:free\s+(?:download|dl)|out\s+now)\s*[\])])\s*$",
    re.IGNORECASE,
)
# Between the names of a listed artist: "Tobu, Diviners", "Unknown Brain, Rival & Jex".
_LISTED_NAMES_SPLIT_RE = re.compile(r",\s*|\s+(?:&|and|x|feat\.?|ft\.?)\s+", re.IGNORECASE)


def _clean_str(value: Any) -> str | None:
    if isinstance(value, str):
        return youtube._tidy(value) or None
    return None


def _has_word(text: str) -> bool:
    """Any letter or digit: "-" or "♡" alone is not a name."""
    return any(unicodedata.category(ch)[0] in "LN" for ch in text)


def clean_sc_title(title: str) -> str:
    """The title without YouTube-style junk ("(Official Audio)", see `youtube.clean_title`) and
    without SoundCloud's own tails ("[NCS Release]", "[Free Download]"). May return ""."""
    text = youtube.clean_title(title)
    while True:
        # clean_title again: a separator the tail leaves behind goes too ("Song - [NCS
        # Release]" is "Song", not "Song -").
        new = youtube.clean_title(_SC_TAIL_RE.sub("", text))
        if new == text:
            return text
        text = new


def _split_dash(title: str) -> tuple[str, str] | None:
    match = _DASH_SPLIT_RE.match(title or "")
    if not match:
        return None
    left, right = match.group(1).strip(), match.group(2).strip()
    if not left or not right or "(" in left:
        return None
    return left, right


def _contains_name(text: str, name: str) -> bool:
    """`name` occurs in `text` as whole words, ignoring case ("Chainsmokers" in "The
    Chainsmokers ft. Daya", but not "MO" in "Mountain")."""
    pattern = rf"(?<!\w){re.escape(name.casefold())}(?!\w)"
    return re.search(pattern, text.casefold()) is not None


def _same_artist(a: str, b: str) -> bool:
    return _contains_name(a, b) or _contains_name(b, a)


def _first_listed_name(artists: str) -> str:
    """The first name of a listed artist that names several: SoundCloud joins them with commas
    ("Tobu, Diviners", "Robin Hustin, Tobimorrow and Jex"), "&", "x" or "feat."."""
    first = _LISTED_NAMES_SPLIT_RE.split(artists, maxsplit=1)[0].strip()
    return first if _has_word(first) else artists


def _left_names_artist(left: str, publisher: str | None, uploader: str | None) -> bool:
    """Whether the left side of "Left - Right" is the artist (rule 1 of `pick_tags`)."""
    named = [name for name in (publisher, uploader) if name]
    if not any(ch.isalpha() for ch in left) and not any(_same_artist(n, left) for n in named):
        return False  # "634 - Monstercat Call of the Wild: ...": an episode number, not "311"
    if publisher is None:
        return True
    if uploader and publisher.casefold() == uploader.casefold():
        return True
    # "Tobu & Diviners - Geometry", listed as "Tobu, Diviners": no whole-string match, but the
    # first listed name is on the left.
    return _same_artist(publisher, left) or _contains_name(left, _first_listed_name(publisher))


def _is_title_echo(name: str, title: str) -> bool:
    """A title pasted into the artist field (or most of it): "BENDY AND THE INK MACHINE SONG
    (Build Our Machine) INSTRUMENTAL" for "... INSTRUMENTAL by DAGAMES". A real artist that
    starts the title ("Adele - Hello", "Adele Hello Remix") is not an echo."""
    name_cf, title_cf = name.casefold(), title.casefold()
    if name_cf == title_cf:
        return True
    if not title_cf.startswith(name_cf):
        return False
    rest = title_cf[len(name_cf) :].lstrip()
    if rest[:1] in ("-", "–", "—", "|", ":"):
        return False
    return len(name_cf) * 2 > len(title_cf)


def _publisher_artist(info: dict[str, Any], title: str) -> str | None:
    """The artist SoundCloud lists for the track (its publisher metadata, yt-dlp's
    ``artists[0]``). Not yt-dlp's ``artist``: that joins the list with a FULLWIDTH COMMA in
    place of each comma ("Boss， Harp")."""
    artists = info.get("artists")
    if not isinstance(artists, list) or not artists:
        return None
    name = _clean_str(artists[0])
    if not name or not _has_word(name) or _is_title_echo(name, title):
        return None
    return name


def _set_album(info: dict[str, Any]) -> str | None:
    """The set's title when the set is a release (album, EP, single, compilation)."""
    if str(info.get("album_type") or "").lower() in ALBUM_SET_TYPES:
        return _clean_str(info.get("album"))
    return None


def pick_tags(info: dict[str, Any]) -> tuple[str, str, str | None]:
    """(artist, title, album) for a SoundCloud track.

    1. "Artist - Title" split from the title on a dash, when SoundCloud lists no artist, or the
       listed artist is the uploader (a label: NCS, Monstercat) or the left side itself
       ("Adele - Hello" by Adele must not become "Adele - Adele - Hello"), or the first of the
       listed names is on the left ("Tobu & Diviners - Geometry" listed as "Tobu, Diviners").
       A left side without a letter ("634 - Monstercat Call of the Wild: ...") is an episode
       or track number, not an artist, unless it is the listed artist or the uploader ("311");
    2. else the artist SoundCloud lists and the whole title ("MadReal" - "Sideways (Prod. Mad
       Real)" uploaded by garyvee);
    3. else the uploader and the title.

    The album is the set's title when the track comes from an album, EP, single or compilation
    (yt-dlp gives a track on its own no album).
    """
    raw = _clean_str(info.get("title")) or ""
    cleaned = clean_sc_title(raw) or raw
    track_id = _clean_str(str(info.get("id") or "")) or ""
    fallback_title = f"SoundCloud track {track_id}" if track_id else "SoundCloud track"
    uploader = _clean_str(info.get("uploader"))
    publisher = _publisher_artist(info, raw)
    album = _set_album(info)
    parts = _split_dash(cleaned)
    if parts is not None and _left_names_artist(parts[0], publisher, uploader):
        left, right = parts
        return left, clean_sc_title(right) or right, album
    if publisher:
        return publisher, cleaned or fallback_title, album
    return uploader or "SoundCloud", cleaned or fallback_title, album


def is_preview_only(info: dict[str, Any]) -> bool:
    """True when SoundCloud offers nothing but its 30-second preview (a Go+ track)."""
    formats = info.get("formats")
    if not isinstance(formats, list) or not formats:
        return False
    return all(
        isinstance(f, dict)
        and (str(f.get("format_id") or "").endswith("_preview") or f.get("preference") == -10)
        for f in formats
    )


# --------------------------------------------------------------------------------------------
# Artwork
# --------------------------------------------------------------------------------------------


def _is_placeholder(thumb: dict[str, Any]) -> bool:
    """SoundCloud's grey default avatar, used when a track has no artwork and its uploader no
    picture: not a cover."""
    return "/images/default_avatar_" in str(thumb.get("url") or "")


def trim_artwork(thumbnails: Any) -> list[dict[str, Any]]:
    """SoundCloud's artwork list without the "original" upload (at whatever size it was
    uploaded, often over a megabyte; the 500x500 one is left last, which is the one yt-dlp
    writes) and without the default-avatar placeholder. The original stays when it is all there
    is."""
    if not isinstance(thumbnails, list):
        return []
    kept = [t for t in thumbnails if isinstance(t, dict) and not _is_placeholder(t)]
    sized = [t for t in kept if t.get("id") != "original"]
    return sized or kept


class TrimArtworkPP(PostProcessor):
    """Runs before yt-dlp writes the thumbnail (``when="pre_process"``): see `trim_artwork`."""

    def run(self, information: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        if isinstance(information.get("thumbnails"), list):
            thumbnails = trim_artwork(information["thumbnails"])
            information["thumbnails"] = thumbnails
            if thumbnails:
                information["thumbnail"] = thumbnails[-1].get("url")
            else:
                information.pop("thumbnail", None)
        return [], information

    def _hook_progress(self, status: dict[str, Any], info_dict: dict[str, Any]) -> None:
        """Bookkeeping, not a step the queue should show ("Processing (TrimArtwork)" before the
        download has even started)."""
        return None


# --------------------------------------------------------------------------------------------
# Errors (offline)
# --------------------------------------------------------------------------------------------

GO_PLUS = (
    "SoundCloud only plays a 30-second preview of this track without a SoundCloud Go+ "
    "subscription, which Ultimate Playlist doesn't support."
)
_NOT_FOUND = (
    "This SoundCloud track or playlist doesn't exist (it may have been deleted), or it is "
    "private: a private link has to keep its s-... part at the end."
)
_RATE_LIMITED = (
    "SoundCloud is rate-limiting this computer (too many requests in a short time). Wait a few "
    "minutes, then retry the failed tracks."
)
# SoundCloud's rate limit often arrives as a 403 on its JSON calls rather than a 429 ("Unable
# to download JSON metadata: HTTP Error 403: Forbidden"), and clears by itself within minutes;
# a 403 that never goes away is SoundCloud having changed something yt-dlp relies on.
_REFUSED = (
    "SoundCloud refused the request for this track or playlist. This usually clears up by itself "
    "within a few minutes (SoundCloud limits how many requests one computer makes in a short "
    "time): wait a few minutes, then retry the failed tracks. If it still happens much later, "
    "yt-dlp needs an update (a newer Ultimate Playlist release)."
)
_PAGE_UNREADABLE = (
    "SoundCloud's page couldn't be read. SoundCloud changes it now and then and a newer yt-dlp "
    "(a newer Ultimate Playlist release) fixes that; try again later."
)
_ERROR_RULES: youtube.ErrorRules = (
    (
        youtube.NETWORK_ERRORS,
        "Couldn't reach SoundCloud. Check your internet connection and try again.",
    ),
    (("rate limit", "too many requests"), _RATE_LIMITED),
    (
        ("drm protected",),
        "SoundCloud protects this track with DRM (some record labels ask for that), so it "
        "can't be downloaded.",
    ),
    (
        (
            "geo restriction",
            "geo-restricted",
            "georestricted",
            "from your location",
            "not available in your country",
        ),
        "This track isn't available in your country.",
    ),
    (("requested format is not available",), GO_PLUS),
    (("unable to extract client id",), _PAGE_UNREADABLE),
    (
        ("no video formats found", "unsupported url"),
        "That SoundCloud link isn't a track or a playlist.",
    ),
    (youtube.CONVERSION_ERRORS, youtube.CONVERSION_FAILED),
)


def _http_status_message(code: int) -> str | None:
    if code in (401, 403):
        return _REFUSED
    if code == 429:
        return _RATE_LIMITED
    if code in (404, 410):
        return _NOT_FOUND
    if 500 <= code < 600:
        return "SoundCloud is having problems right now. Try again later."
    return None


def friendly_error(exc: BaseException | str) -> str:
    """Turn a yt-dlp error about a SoundCloud link into something a non-programmer can act
    on."""
    return youtube.explain_error(
        exc, _ERROR_RULES, _http_status_message, "SoundCloud download failed."
    )


# --------------------------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------------------------


class SoundCloudProvider(youtube.YouTubeProvider):
    """SoundCloud tracks and sets; downloads go through `YouTubeProvider.download` (see module
    doc)."""

    name = PROVIDER_NAME
    display_name = "SoundCloud"
    ydl_format = YDL_FORMAT
    # SoundCloud is audio only: Video downloads are saved as audio (the queue says so).
    supports_video = False

    def _tags_for(self, info: dict[str, Any]) -> tuple[str, str, str | None]:
        return pick_tags(info)

    def _tags_for_ref(self, ref: TrackRef, info: dict[str, Any]) -> tuple[str, str, str | None]:
        """A track downloaded from a set gets the set's album, which only the ref knows (yt-dlp
        fetches the track on its own)."""
        artist, title, album = pick_tags(info)
        return artist, title, album or ref.album

    def _source_url(self, ref: TrackRef, info: dict[str, Any]) -> str:
        """The track's page, also for the API links yt-dlp gives the later tracks of a set
        (``api-v2.soundcloud.com/tracks/<id>``): SoundCloud's permalink, which keeps the s-...
        part of a private track. A track named like a profile tab (``/<user>/spotlight``), which
        a set lists by its id, has a page parse_link does not take as a track link: that page
        is still used when yt-dlp read this very track (the same id)."""
        link = parse_link(ref.url)
        if link is None or not link.is_api:
            return ref.url
        webpage = _clean_str(info.get("webpage_url")) or ""
        page = parse_link(webpage)
        if page is not None and page.kind == "track" and not page.is_api:
            return page.clean_url
        if _clean_str(str(info.get("id") or "")) == ref.source_id:
            return _track_page(webpage) or ref.url
        return ref.url

    def _prepare_ydl(self, ydl: Any) -> None:
        ydl.add_post_processor(TrimArtworkPP(), when="pre_process")

    def _explain(self, error: BaseException | str) -> str:
        return friendly_error(error)

    def matches(self, url: str) -> bool:
        return parse_link(url) is not None

    def resolve(self, url: str) -> list[TrackRef]:
        url = youtube.normalize_url(url)
        link = parse_link(url)
        if link is None:
            raise ProviderError("That isn't a SoundCloud link.")
        if link.kind == "short":
            link = self._follow_short_link(link)
        if link.kind not in ("track", "set"):
            raise ProviderError(unsupported_message(link))

        info = self._extract(link)
        if info.get("_type") in ("playlist", "multi_video"):
            return self._refs_from_set(info)
        if is_preview_only(info):
            raise ProviderError(GO_PLUS)
        track_id = _clean_str(str(info.get("id") or ""))
        if not track_id:
            raise ProviderError("SoundCloud returned no track id for this link.")
        artist, title, album = pick_tags(info)
        page = parse_link(_clean_str(info.get("webpage_url")) or "")
        canonical = page.clean_url if page and page.kind == "track" and not page.is_api else None
        return [
            TrackRef(
                provider=self.name,
                source_id=track_id,
                url=canonical or link.clean_url,
                title=title,
                artist=artist,
                album=album,
                duration=youtube._float_or_none(info.get("duration")),
                thumbnail_url=_thumbnail_url(info),
                extra={"original_url": url},
            )
        ]

    def _follow_short_link(self, link: SoundCloudLink) -> SoundCloudLink:
        target = expand_short_link(link)
        # The target carries SoundCloud's share-tracking query (utm_*); keep it out of app.log.
        log.info("SoundCloud short link %s led to %s", link.clean_url, target.split("?", 1)[0])
        expanded = parse_link(target)
        if expanded is None or expanded.host != MAIN_HOST:
            raise ProviderError(SHORT_LINK_DEAD)
        if expanded.kind in ("track", "set"):
            return expanded
        if expanded.kind == "other" and expanded.reason in _EXPLAINED_REASONS:
            raise ProviderError(unsupported_message(expanded))  # a profile shared from the app
        raise ProviderError(SHORT_LINK_DEAD)

    def _refs_from_set(self, info: dict[str, Any]) -> list[TrackRef]:
        """One ref per track of a set, from the flat listing: one request for the whole set
        (two for a private set of more than five tracks), not one per track. The listing has no
        titles: each track is "<set> · track <n>" in the queue until its download names it.
        An entry that is not a track link is skipped (`_entry_url`)."""
        set_title = _clean_str(info.get("title")) or _clean_str(info.get("album"))
        set_title = set_title or "SoundCloud playlist"
        set_type = str(info.get("album_type") or "").lower()
        album = set_title if set_type in ALBUM_SET_TYPES else None
        container = f"{_SET_LABELS.get(set_type, 'Playlist')}: {set_title}"
        refs: list[TrackRef] = []
        for position, entry in enumerate(info.get("entries") or [], start=1):
            if not isinstance(entry, dict):
                continue
            track_id = _clean_str(str(entry.get("id") or ""))
            url = _entry_url(entry)
            if not track_id or url is None:
                log.info("SoundCloud set entry %d is not a track; skipped", position)
                continue
            refs.append(
                TrackRef(
                    provider=self.name,
                    source_id=track_id,
                    url=url,
                    title=f"{set_title} · track {position}",
                    album=album,
                    extra={"container": container},
                )
            )
        return refs

    def _extract(self, link: SoundCloudLink) -> dict[str, Any]:
        """yt-dlp's metadata for a track (complete, so its formats can be checked) or a set
        (flat: the track list without fetching every track), from the extractor for the link's
        kind (`_extractor_for`); ProviderError with a friendly text."""
        page = link.clean_url
        logger = youtube._YdlLogger(quiet_warnings=True)
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "noplaylist": True,
            "skip_download": True,
            "logger": logger,
        }
        try:
            with youtube.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(page, download=False, ie_key=_extractor_for(link))
        except yt_dlp.utils.YoutubeDLError as exc:
            raise ProviderError(friendly_error(exc)) from exc
        except Exception as exc:
            log.exception("Unexpected failure resolving %s", page)
            raise ProviderError(f"Could not read that SoundCloud link: {exc}") from exc
        if not isinstance(info, dict):
            raise ProviderError(
                friendly_error(logger.last_error or "SoundCloud returned no information")
            )
        return info

    def doctor(self, settings: Settings | None = None) -> list[tuple[bool, str, str]]:
        """SoundCloud needs ffmpeg and yt-dlp; no JavaScript runtime, no account."""
        settings = self._doctor_settings(settings)
        return [youtube.ffmpeg_check(settings), youtube.ytdlp_check()]


def _extractor_for(link: SoundCloudLink) -> str:
    """The yt-dlp extractor (``ie_key``) for a track or set link, by the kind `parse_link` gave
    it. Left to itself yt-dlp takes the first extractor whose pattern matches, and the one for
    the lists SoundCloud builds around a track (SoundcloudRelated, `/<user>/<track>/albums`)
    comes before the set's and also matches every set whose name merely starts with "albums",
    "sets" or "recommended" (`/<user>/sets/recommended`): it would list some other tracks."""
    if link.kind == "set":
        return "SoundcloudPlaylist" if link.is_api else "SoundcloudSet"
    return "Soundcloud"


def _entry_url(entry: dict[str, Any]) -> str | None:
    """A set entry's link as yt-dlp gave it: the track's page, or for the later tracks of a set
    ``api-v2.soundcloud.com/tracks/<id>``.

    A track yt-dlp marks as one (``ie_key`` Soundcloud) whose page is not a link parse_link (or
    yt-dlp's own track pattern) takes, a track named like a profile tab such as
    ``/<user>/spotlight``, is fetched by its id instead, keeping a private track's s- part.
    None for anything else: its id need not be a track's (a playlist's id would name some
    other track)."""
    url = _clean_str(entry.get("url"))
    link = parse_link(url) if url else None
    if url and link is not None and link.kind == "track":
        return url
    track_id = _clean_str(str(entry.get("id") or ""))
    if entry.get("ie_key") != "Soundcloud" or not _is_ascii_digits(track_id):
        return None
    secret = _secret_of(url)
    query = f"?secret_token={secret}" if secret else ""
    return f"https://api-v2.soundcloud.com/tracks/{track_id}{query}"


def _track_page(url: str) -> str | None:
    """``https://soundcloud.com/<user>/<name>[/s-...]`` rebuilt from a page of that shape on a
    main SoundCloud host (no port, no user info, no query), whatever its name; else None."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or host not in _MAIN_HOSTS:
        return None
    if parts.netloc.lower() != host:  # a port or user info
        return None
    segments = parts.path.split("/")[1:]
    if segments and segments[-1] == "":
        segments = segments[:-1]
    if len(segments) not in (2, 3) or not all(_NAME_RE.match(s) for s in segments[:2]):
        return None
    if len(segments) == 3 and not _SECRET_RE.match(segments[2]):
        return None
    return "https://soundcloud.com/" + "/".join(segments)


def _is_ascii_digits(text: str | None) -> bool:
    return bool(text) and text.isascii() and text.isdigit()


def _secret_of(url: str | None) -> str | None:
    """The s-... part a private track's page ends with (``/<user>/<track>/s-AbC``), if any."""
    if not url:
        return None
    try:
        parts = [p for p in urlsplit(url).path.split("/") if p]
    except ValueError:
        return None
    return parts[-1] if parts and _SECRET_RE.match(parts[-1]) else None


def _thumbnail_url(info: dict[str, Any]) -> str | None:
    thumbnails = trim_artwork(info.get("thumbnails"))
    for thumb in reversed(thumbnails):
        if thumb.get("url"):
            return str(thumb["url"])
    thumbnail = _clean_str(info.get("thumbnail"))
    if thumbnail and "/images/default_avatar_" not in thumbnail:
        return thumbnail
    return None
