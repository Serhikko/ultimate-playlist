"""TikTok provider: the audio of single TikTok videos and photo slideshows, through yt-dlp.

Downloading, converting, tagging and naming are `YouTubeProvider`'s pipeline (this provider is
a subclass); what this module adds:

* the link shapes TikTok uses (``parse_link``): ``www.tiktok.com/@user/video/<id>`` and
  ``/photo/<id>``, the ``m.tiktok.com`` / embed / share forms, and the short links
  (``vm.tiktok.com/...``, ``vt.tiktok.com/...``, ``www.tiktok.com/t/...``), which `resolve()`
  expands with one HEAD request. Every post is then fetched as
  ``https://www.tiktok.com/@<user>/video/<id>``, the one shape yt-dlp's TikTok extractor
  accepts for every link form above (it does not know ``/photo/``, ``/embed/v2/``,
  ``m.tiktok.com/v/<id>.html`` nor the bare ``tiktok.com`` host);
* which audio is taken (``ydl_format``): what you hear in the video, not TikTok's separate
  "sound" file and not the watermarked copy, which ends in TikTok's outro jingle. A photo
  slideshow has no video, so its sound is the audio;
* the name (``pick_tags``): a post set to a named sound is "<sound artists> - <sound title>",
  an "original sound" is "<creator> - <caption>";
* TikTok's own error texts (``friendly_error``).

Every other link on a TikTok host (a profile, a sound page, a live stream) belongs to this
provider too, so `resolve()` can say why it can't be downloaded (and, where there is one, what to
paste instead) rather than a bare "No provider for this link".
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
import yt_dlp

from ..config import Settings
from ..models import TrackRef
from . import youtube
from .base import ProviderError

log = logging.getLogger(__name__)

PROVIDER_NAME = "tiktok"
_HOSTS = frozenset(
    {
        "tiktok.com",
        "www.tiktok.com",
        "m.tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
        "tiktokv.com",  # TikTok Lite shares
        "www.tiktokv.com",
    }
)
_SHORT_HOSTS = frozenset({"vm.tiktok.com", "vt.tiktok.com"})
_POST_PATHS = (
    re.compile(r"^/@(?P<user>[^/]*)/(?:video|photo)/(?P<id>\d+)$"),
    re.compile(r"^/(?:share/)?(?:video|photo)/(?P<id>\d+)$"),
    re.compile(r"^/embed(?:/v\d+)?/(?P<id>\d+)$"),
    re.compile(r"^/v/(?P<id>\d+)(?:\.html)?$"),  # old m.tiktok.com share links
)
_SHORT_PATH_RE = re.compile(r"^/t/[\w-]+$")  # www.tiktok.com/t/ZTRC5xgJp
_SHORT_HOST_PATH_RE = re.compile(r"^/[\w-]+$")  # vm.tiktok.com/ZMabc123
_HANDLE_RE = re.compile(r"^[\w.-]+$")
_PROFILE_RE = re.compile(r"^/@[^/]+$")
# yt-dlp's title when a post has no caption: "TikTok video #7059698374567611694".
_PLACEHOLDER_TITLE_RE = re.compile(r"^TikTok video #\d+$")

# The video's soundtrack, from the best non-watermarked video; else the sound (a photo
# slideshow has no video); else whatever there is (the watermarked copy, outro and all).
# A prefix match: yt-dlp renames two watermarked formats to "download-0" / "download-1".
YDL_FORMAT = "best[format_id!^=download]/bestaudio/best"
SHORT_LINK_TIMEOUT = 15.0  # seconds
# TikTok sends its short-link redirect to link-preview bots without a challenge page; yt-dlp's
# own vm.tiktok extractor asks the same way.
_SHORT_LINK_HEADERS = {"User-Agent": "facebookexternalhit/1.1"}

# The name TikTok gives a sound recorded in the post itself ("original sound", or
# "original sound - <owner>"), in the languages TikTok answers in most often, plus the sound of
# ads. A post with such a sound is named after its creator and caption, not after the sound.
_GENERIC_SOUND_RE = re.compile(
    r"^(?:original sound|promoted music|son original|sonido original|som original"
    r"|suono originale|originalton|originele sound|origineel geluid|оригинальный звук"
    r"|оригінальний звук|suara asli|bunyi asal|orijinal ses|dźwięk oryginalny"
    r"|oryginalny dźwięk|nhạc nền|เสียงต้นฉบับ|原声|原聲|オリジナル楽曲|오리지널 사운드"
    r"|الصوت الأصلي)(?:\s*[-–—]\s*.*)?$",
    re.IGNORECASE,
)
CAPTION_MAX_LEN = 80
_CAPTION_MIN_SENTENCE = 15  # a sentence end before this many characters is too early to cut at
_REPLY_PREFIX_RE = re.compile(r"^\s*(?:replying|reply) to @\S+\s*", re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"[.!?…](?=\s)")
_EDGE_PUNCT = " \t-–—|·•,;:"
# Punctuation that may follow a hashtag directly and belongs to the sentence ("#mom! Love").
_CLOSING_PUNCT = frozenset(".,!?;:…)]»”")
_SENTENCE_PUNCT = frozenset(".,!?;:…")
# Characters people paste to fake an empty caption, line or display name: they look blank but
# are letters or symbols to Unicode (Braille blank, the Hangul fillers).
_BLANK_FILLERS = "⠀ㅤᅟᅠﾠ"
_BLANK_RE = re.compile(f"[\\s{_BLANK_FILLERS}]+")


# --------------------------------------------------------------------------------------------
# Links (offline)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TikTokLink:
    """What a TikTok link points at: a post (``post_id`` set), a short link, or anything else."""

    kind: str  # "post" | "short" | "other"
    post_id: str | None = None
    user: str | None = None
    path: str = "/"
    host: str = "www.tiktok.com"  # one of _HOSTS, lower case

    @property
    def clean_url(self) -> str:
        """The link rebuilt from its checked parts: the network only ever sees a TikTok host,
        whatever userinfo, port or backslash tricks the pasted text contained."""
        return f"https://{self.host}{self.path}"


def parse_link(url: str) -> TikTokLink | None:
    """Classify a link without touching the network; None when it is not on a TikTok host."""
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
    for pattern in _POST_PATHS:
        match = pattern.match(path)
        if match:
            user = match.groupdict().get("user") or None
            if user is not None and not _HANDLE_RE.match(user):
                user = None
            return TikTokLink("post", match.group("id"), user, path, host)
    if host in _SHORT_HOSTS and _SHORT_HOST_PATH_RE.match(path):
        return TikTokLink("short", path=path, host=host)
    if host not in _SHORT_HOSTS and _SHORT_PATH_RE.match(path):
        return TikTokLink("short", path=path, host=host)
    return TikTokLink("other", path=path, host=host)


def post_url(post_id: str, user: str | None = None) -> str:
    """The link yt-dlp's TikTok extractor understands, for videos and photo posts alike
    (TikTok serves a photo post's data on its /video/ address too). "_" stands in for an
    unknown user, as in yt-dlp."""
    handle = user if user and _HANDLE_RE.match(user) else "_"
    return f"https://www.tiktok.com/@{handle}/video/{post_id}"


def unsupported_message(link: TikTokLink) -> str:
    """Why a TikTok link that is not a post cannot be downloaded and, where there is one, what
    to paste instead."""
    path = link.path
    if _PROFILE_RE.match(path):
        return (
            "That's a TikTok profile, not a single post. Open one of its videos and copy that "
            "link (Share, then Copy link)."
        )
    if path.startswith("/music/"):
        return (
            "That's a TikTok sound page. Open a video that uses the sound and copy the video's "
            "link instead."
        )
    if path == "/live" or path.endswith("/live") or path.startswith("/live/"):
        return "TikTok live streams can't be downloaded."
    return (
        "That TikTok link isn't a video or photo post. Copy the link from the post itself "
        "(Share, then Copy link)."
    )


SHORT_LINK_DEAD = (
    "That TikTok link doesn't lead to a video or photo post any more (it may have been deleted)."
)


def _short_link_status_message(code: int) -> str:
    """TikTok answered the short link itself with an error instead of a redirect."""
    if code == 429:
        return "TikTok is rate-limiting this computer. Wait a few minutes and try again."
    if code >= 500:
        return "TikTok is having problems right now. Try again later."
    if code in (404, 410):
        return SHORT_LINK_DEAD
    return (
        f"TikTok wouldn't open that short link (HTTP {code}). Try again in a minute, or open it "
        "in your browser and paste the full link it leads to."
    )


def expand_short_link(link: TikTokLink) -> str:
    """Where a vm.tiktok.com / vt.tiktok.com / tiktok.com/t/ link leads (one HEAD request,
    sent to the checked TikTok host only, see `TikTokLink.clean_url`)."""
    try:
        response = requests.head(
            link.clean_url,
            allow_redirects=True,
            timeout=SHORT_LINK_TIMEOUT,
            headers=_SHORT_LINK_HEADERS,
        )
    except requests.TooManyRedirects as exc:
        raise ProviderError(SHORT_LINK_DEAD) from exc
    except requests.RequestException as exc:
        raise ProviderError(
            "Couldn't reach TikTok. Check your internet connection and try again. "
            f"({type(exc).__name__})"
        ) from exc
    # A redirect that ends on an error page is still a redirect: the post id is in its URL.
    if not response.history and response.status_code >= 400:
        raise ProviderError(_short_link_status_message(response.status_code))
    return str(response.url)


# --------------------------------------------------------------------------------------------
# Naming (offline)
# --------------------------------------------------------------------------------------------


def _clean_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = youtube._tidy(_BLANK_RE.sub(" ", value))  # "ㅤ" as a display name is no name
        return text or None
    return None


def is_generic_sound(track: str, *creators: str | None) -> bool:
    """True for a sound that only carries the post's own audio: "original sound", its
    translations, or a sound named after the post's creator ("<anything> - <creator>")."""
    text = youtube._tidy(track)
    if not text or _GENERIC_SOUND_RE.match(text):
        return True
    lowered = text.casefold()
    return any(
        creator and lowered.endswith(f" - {youtube._tidy(creator).casefold()}")
        for creator in creators
    )


def _shorten(text: str, limit: int = CAPTION_MAX_LEN) -> str:
    """Cut a long caption after its first sentence, else at a word boundary."""
    if len(text) <= limit:
        return text
    for match in _SENTENCE_END_RE.finditer(text):
        end = match.end()
        if end > limit:
            break
        if end >= _CAPTION_MIN_SENTENCE:
            return text[:end].strip(_EDGE_PUNCT)
    # Look one character past the limit: a word that ends exactly there is kept.
    space = text[: limit + 1].rfind(" ")
    cut = text[:space] if space >= limit // 2 else text[:limit]
    return cut.strip(_EDGE_PUNCT)


def _is_tag_char(ch: str) -> bool:
    """Letters (with their combining marks, for Indic and Thai tags), digits, "_" and the
    zero-width joiners that are part of the spelling in Sinhala and Persian."""
    return ch in "_‌‍" or unicodedata.category(ch)[0] in "LMN"


def strip_hashtags(text: str) -> str:
    """Remove hashtags, including runs glued together ("##funny#fyp"). A tag ends where TikTok
    ends it, at anything but a letter, digit or "_": punctuation or an emoji after it stays, and
    the space before the tag goes when the tag closed the sentence ("Hi #mom! Bye" -> "Hi! Bye").
    "C#" and "&#39;" are not tags."""
    out: list[str] = []
    i = 0
    while i < len(text):
        before = text[i - 1] if i else " "
        if text[i] != "#" or before.isalnum() or before in "_&":
            out.append(text[i])
            i += 1
            continue
        end = i
        while end < len(text) and (text[end] == "#" or _is_tag_char(text[end])):
            end += 1
        if end < len(text) and text[end] in _CLOSING_PUNCT:
            while out and out[-1].isspace():
                out.pop()
            # Nothing before the tag for its punctuation to close ("#mom! Love you" at the
            # start of a line), or a sentence already closed ("Great day. #blessed! Next"):
            # the punctuation goes with the tag. A bracket stays, it pairs with another.
            # Only a run that ends the word: in "#tbt:D" the ":" is half an emoticon.
            if not out or out[-1] in ".!?…":
                stop = end
                while stop < len(text) and text[stop] in _SENTENCE_PUNCT:
                    stop += 1
                if stop == len(text) or text[stop].isspace():
                    end = stop
        i = end
    return "".join(out)


def clean_caption(text: str) -> str:
    """A caption made fit for a title: hashtags and a "Replying to @user" prefix removed, the
    first line with something to read, shortened to about 80 characters. May return ""."""
    # Fillers first: to the tag and @mention rules below they would be letters ("ㅤ#fyp").
    text = youtube.strip_invisible(re.sub(f"[{_BLANK_FILLERS}]", " ", text or ""))
    for line in text.splitlines():
        line = _REPLY_PREFIX_RE.sub("", line)
        line = strip_hashtags(line)
        line = youtube.strip_invisible(_BLANK_RE.sub(" ", line)).strip(_EDGE_PUNCT)
        if _has_content(line):  # "#fyp #viral." leaves "." and the caption is on the next line
            return youtube.strip_invisible(_shorten(line))  # a hard cut can bare a joiner
    return ""


def _has_content(text: str) -> bool:
    """Any letter, digit or symbol (emoji included): "..." or "?!" alone is not a title, nor
    a blank filler such as "ㅤ"."""
    return any(unicodedata.category(ch)[0] in "LNS" and ch not in _BLANK_FILLERS for ch in text)


def caption_title(info: dict[str, Any]) -> str:
    """The post's caption as a title, or "TikTok post <id>" when it has none worth using."""
    caption = info.get("description")
    if not isinstance(caption, str) or not caption.strip():
        title = info.get("title")
        caption = title if isinstance(title, str) else ""
        if _PLACEHOLDER_TITLE_RE.match(caption.strip()):
            caption = ""
    cleaned = clean_caption(caption)
    if _has_content(cleaned):
        return cleaned
    post_id = _clean_str(info.get("id"))
    return f"TikTok post {post_id}" if post_id else "TikTok post"


def join_artists(names: list[str]) -> str:
    """yt-dlp splits TikTok's sound author on ", " and " & " alike; join two names with " & "
    and more as "A, B & C", which gives back "Simon & Garfunkel" and "Earth, Wind & Fire" (at
    the price of "Tyler & The Creator")."""
    if len(names) <= 2:
        return " & ".join(names)
    return f"{', '.join(names[:-1])} & {names[-1]}"


def _sound_artists(info: dict[str, Any]) -> str | None:
    artists = info.get("artists")
    if isinstance(artists, list):
        names = [name for name in (_clean_str(a) for a in artists) if name]
        if names:
            return join_artists(list(dict.fromkeys(names)))
    return _clean_str(info.get("artist"))


def pick_tags(info: dict[str, Any]) -> tuple[str, str, str | None]:
    """(artist, title, album) for a TikTok post.

    A post set to a named sound (a song, or a sound someone uploaded under a name) is
    "<sound artists> - <sound title>" with the sound's album. A post whose sound is its own
    audio ("original sound", see `is_generic_sound`) is "<creator> - <caption>"; the creator is
    the display name, else the @handle.
    """
    creator = _clean_str(info.get("channel")) or _clean_str(info.get("uploader")) or "TikTok"
    handle = _clean_str(info.get("uploader"))
    track = _clean_str(info.get("track"))
    if track and not is_generic_sound(track, creator, handle):
        return _sound_artists(info) or creator, track, _clean_str(info.get("album"))
    return creator, caption_title(info), None


# --------------------------------------------------------------------------------------------
# Errors (offline)
# --------------------------------------------------------------------------------------------

_UNAVAILABLE = "This TikTok isn't available (it may have been deleted)."
_PAGE_UNREADABLE = (
    "TikTok's page couldn't be read. TikTok changes it now and then and a newer yt-dlp "
    "(a newer Ultimate Playlist release) fixes that; try again later."
)
_ERROR_RULES: youtube.ErrorRules = (
    (
        youtube.NETWORK_ERRORS,
        "Couldn't reach TikTok. Check your internet connection and try again.",
    ),
    (
        ("ip address is blocked",),
        # TikTok's status 10204. The app already looks like a browser (curl_cffi), so this is
        # TikTok's answer for this connection's location, or a removed post.
        "TikTok won't show this post in your country (or it was removed). If it plays in your "
        "browser through a VPN, switch on a VPN for the whole computer and try again: a VPN "
        "browser extension only covers the browser, not Ultimate Playlist.",
    ),
    (
        ("do not have permission", "private post", "private account", "account is private"),
        "This TikTok is private, so it can't be downloaded.",
    ),
    (
        (
            "requiring login",
            "log in for access",
            "login required",
            "log into",
            "comfortable for some audiences",
            "age-restricted",
        ),
        "TikTok only shows this post to signed-in users (for example age-restricted posts), "
        "which Ultimate Playlist doesn't support.",
    ),
    # What yt-dlp says when TikTok moved the data it reads: no data script on the page at all,
    # or the script without the post (its status then falls back to 0). Before "status code".
    (("unexpected response from webpage", "status code 0"), _PAGE_UNREADABLE),
    (
        (
            "video not available",
            "post not available",
            "status code",
            "couldn't find this",
            "has been removed",
            "has been deleted",
            "does not exist",
        ),
        _UNAVAILABLE,
    ),
    (
        ("universal data", "unable to extract", "challenge"),
        _PAGE_UNREADABLE,
    ),
    (
        ("requested format is not available", "no video formats found"),
        "TikTok didn't offer any audio for this post.",
    ),
    (
        ("unsupported url",),
        "That TikTok link doesn't lead to a video or photo post (it may have been deleted).",
    ),
    (youtube.CONVERSION_ERRORS, youtube.CONVERSION_FAILED),
)


def _http_status_message(code: int) -> str | None:
    if code == 403:
        return (
            "TikTok refused to serve the audio. Try again in a minute; if it keeps happening, "
            "yt-dlp needs an update (a newer Ultimate Playlist release)."
        )
    if code == 429:
        return "TikTok is rate-limiting this computer. Wait a few minutes and try again."
    if code in (404, 410):
        return _UNAVAILABLE
    if 500 <= code < 600:
        return "TikTok is having problems right now. Try again later."
    return None


def friendly_error(exc: BaseException | str) -> str:
    """Turn a yt-dlp error about a TikTok post into something a non-programmer can act on."""
    return youtube.explain_error(exc, _ERROR_RULES, _http_status_message, "TikTok download failed.")


# --------------------------------------------------------------------------------------------
# The provider
# --------------------------------------------------------------------------------------------


class TikTokProvider(youtube.YouTubeProvider):
    """Single TikTok posts; downloads go through `YouTubeProvider.download` (see module doc)."""

    name = PROVIDER_NAME
    display_name = "TikTok"
    ydl_format = YDL_FORMAT

    def _tags_for(self, info: dict[str, Any]) -> tuple[str, str, str | None]:
        return pick_tags(info)

    def _explain(self, error: BaseException | str) -> str:
        return friendly_error(error)

    def matches(self, url: str) -> bool:
        return parse_link(url) is not None

    def resolve(self, url: str) -> list[TrackRef]:
        url = youtube.normalize_url(url)
        link = parse_link(url)
        if link is None:
            raise ProviderError("That isn't a TikTok link.")
        if link.kind == "short":
            target = expand_short_link(link)
            expanded = parse_link(target)
            if expanded is None or expanded.kind == "short" or expanded.path == "/":
                # TikTok sends the short link of a deleted post to its home page. The target
                # carries TikTok's share-tracking query; keep it out of app.log.
                log.info("TikTok short link %s led to %s", url, target.split("?", 1)[0])
                raise ProviderError(SHORT_LINK_DEAD)
            link = expanded  # a profile or sound page shared from the app is explained below
        if link.kind != "post" or not link.post_id:
            raise ProviderError(unsupported_message(link))

        page = post_url(link.post_id, link.user)
        info = self._extract(page)
        post_id = _clean_str(info.get("id")) or link.post_id
        artist, title, album = pick_tags(info)
        return [
            TrackRef(
                provider=self.name,
                source_id=post_id,
                url=post_url(post_id, _clean_str(info.get("uploader")) or link.user),
                title=title,
                artist=artist,
                album=album,
                duration=youtube._float_or_none(info.get("duration")),
                thumbnail_url=youtube._thumbnail_of(info),
                extra={"original_url": url},
            )
        ]

    def _extract(self, page: str) -> dict[str, Any]:
        """yt-dlp's metadata for one post (no download); ProviderError with a friendly text."""
        logger = youtube._YdlLogger(quiet_warnings=True)
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "logger": logger,
        }
        try:
            with youtube.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(page, download=False)
        except yt_dlp.utils.YoutubeDLError as exc:
            raise ProviderError(friendly_error(exc)) from exc
        except Exception as exc:
            log.exception("Unexpected failure resolving %s", page)
            raise ProviderError(f"Could not read that TikTok link: {exc}") from exc
        info = youtube._single_info(info)
        if info is None:
            raise ProviderError(
                friendly_error(logger.last_error or "TikTok returned no information for this post")
            )
        return info

    def doctor(self, settings: Settings | None = None) -> list[tuple[bool, str, str]]:
        """TikTok needs ffmpeg, yt-dlp and its browser impersonation; no JavaScript runtime,
        no account."""
        settings = self._doctor_settings(settings)
        return [youtube.ffmpeg_check(settings), youtube.ytdlp_check(), impersonation_check()]


IMPERSONATION_LABEL = "Browser impersonation"


def impersonation_check() -> tuple[bool, str, str]:
    """Whether yt-dlp can make its requests look like a browser's: its curl_cffi handler only
    loads with curl_cffi installed in a version it supports (in the exe: bundled). Without it
    TikTok refuses more posts as "blocked". Never raises."""
    try:
        importlib.import_module("yt_dlp.networking._curlcffi")
    except ImportError as exc:
        return (
            False,
            IMPERSONATION_LABEL,
            f"Not available ({exc}): TikTok may refuse some posts. Run `uv sync` in a source "
            "checkout, or unpack the release zip again.",
        )
    except Exception as exc:  # noqa: BLE001 - doctor never raises
        return False, IMPERSONATION_LABEL, f"Could not check: {exc}"
    try:
        version = importlib.metadata.version("curl_cffi")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown version"
    return True, IMPERSONATION_LABEL, f"curl_cffi {version}"
