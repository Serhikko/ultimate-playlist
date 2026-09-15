"""Find the YouTube Music recording of a Spotify track.

The Spotify provider knows title, artists and duration; this module turns that into a YouTube
video id. Search goes through yt-dlp: the YouTube Music "Songs" shelf first (the catalogue
tracks labels upload themselves, i.e. the "Artist - Topic" / "Provided to YouTube by ..." audio),
plain YouTube search (``ytsearch``) as the fallback. Candidates are scored 0..1 on title, artist
and duration; a match is only accepted above ``ACCEPT_SCORE`` and within
``MAX_DURATION_DELTA`` seconds, because a wrong song is worse than no song.

Everything but the two ``search_*`` functions is pure and deterministic, so the scoring is
unit-tested with canned candidates; ``find_match`` takes a ``search`` callable for that.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

import yt_dlp

from ..config import Settings
from ..models import TrackRef
from . import youtube
from .base import DownloadCancelled, ProviderError

log = logging.getLogger(__name__)

# --- scoring weights ---------------------------------------------------------------------------
# score = W_TITLE * title + W_ARTIST * artist + W_DURATION * duration + bonuses - penalties,
# clamped to 0..1. The three similarities are each 0..1 and the weights sum to 1.0, so a perfect
# candidate scores 1.0 before bonuses. Tuned on real playlists (see tests/test_ytmusic_match.py
# for the cases that pin the behaviour down).
W_TITLE = 0.5
W_ARTIST = 0.3
W_DURATION = 0.2
BONUS_AUTO_UPLOAD = 0.05  # "- Topic" channel, YouTube Music catalogue entry, "Provided to YouTube"
BONUS_ARTIST_EXACT = 0.05  # the channel / credited artist is exactly one of the Spotify artists
# Per distinct variant word (live, remix, ...) missing from the Spotify title. 0.5 is chosen so
# that even a candidate that is perfect otherwise (1.0 + both bonuses) ends below ACCEPT_SCORE.
PENALTY_VARIANT = 0.5
PENALTY_CAP = 1.0
ACCEPT_SCORE = 0.62
MIN_TITLE_SIMILARITY = 0.5  # never accept a candidate whose title barely resembles the track
MAX_SIMILARITY_WITHOUT_SHARED_WORD = 0.45  # titles with no word in common stay below that
MIN_ARTIST_SIMILARITY = 0.6  # nor one credited to somebody else (a cover band, a tribute act)
MAX_DURATION_DELTA = 25.0  # seconds; hard limit when both durations are known
SEARCH_LIMIT = 10
UNKNOWN_DURATION_SCORE = 0.5  # neutral when one side has no duration

# Words that mark a different recording when they appear in the candidate title only.
VARIANT_WORDS: tuple[str, ...] = (
    "live",
    "remix",
    "cover",
    "karaoke",
    "instrumental",
    "acoustic",
    "sped up",
    "slowed",
    "nightcore",
    "8d",
    "reverb",
    "reaction",
    "tutorial",
    "extended",
    "mashup",
    "edit",
)
_VARIANT_RES: dict[str, re.Pattern[str]] = {
    word: re.compile(rf"(?<!\w){re.escape(word)}(?!\w)") for word in VARIANT_WORDS
}
# A " - Tail" on a Spotify title that labels the edition of the same recording ("Song -
# Remastered 2011", "Song - Radio Edit"). Tails that mean a different recording (live, remix,
# acoustic, instrumental, demo) are deliberately not here: "Song - Live" must not match "Song".
_EDITION_TAIL_RE = re.compile(
    r"\b(?:remaster(?:ed)?|version|edit|mix|mono|stereo|bonus|deluxe|single|radio|"
    r"original|album|anniversary|from|soundtrack|\d{4})\b",
    re.IGNORECASE,
)
_FEAT_RE = re.compile(r"\bfeat\b.*$")
# Spotify's spelling of a collaboration credit, "Rein Me In (with Olivia Dean)"; YouTube
# Music's catalogue title usually leaves it out ("Rein Me In"). Only the bracketed form: a
# plain "with" inside a title ("Girl with the Tattoo") is part of the title.
_WITH_CREDIT_RE = re.compile(r"\s*[(\[]\s*with\s+[^()\[\]]*[)\]]", re.IGNORECASE)
_DURATION_TEXT_RE = re.compile(r"^(?:\d{1,2}:)?\d{1,2}:\d{2}$")
_TOPIC_RE = re.compile(r"\s*-\s*Topic\s*$", re.IGNORECASE)
# YouTube Music search "Songs" shelf; the same value yt-dlp uses for `#songs`.
MUSIC_SONGS_PARAMS = "EgWKAQIIAWoKEAoQAxAEEAkQBQ=="


@dataclass
class Candidate:
    """One search result. `artists` is filled by the YouTube Music search, `channel` by both."""

    video_id: str
    title: str
    channel: str | None = None
    artists: list[str] = field(default_factory=list)
    album: str | None = None
    duration: float | None = None
    auto_upload: bool = False  # catalogue upload ("- Topic", "Provided to YouTube by ...")
    source: str = "music"  # "music" | "youtube"

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass
class Match:
    video_id: str
    url: str
    title: str
    channel: str | None
    duration: float | None
    score: float
    query: str
    album: str | None = None  # YouTube Music's album name, a fallback when Spotify gave none


@dataclass
class Scored:
    candidate: Candidate
    score: float
    title_similarity: float
    artist_similarity: float
    duration_delta: float | None
    penalties: list[str]

    @property
    def accepted(self) -> bool:
        if self.score < ACCEPT_SCORE or self.title_similarity < MIN_TITLE_SIMILARITY:
            return False
        if self.artist_similarity < MIN_ARTIST_SIMILARITY:
            return False
        return self.duration_delta is None or self.duration_delta <= MAX_DURATION_DELTA


SearchFn = Callable[[str], list[Candidate]]


# --------------------------------------------------------------------------------------------
# Text normalisation and similarities (pure)
# --------------------------------------------------------------------------------------------


def normalize_text(text: str | None) -> str:
    """casefold, accents stripped, "&" -> "and", "ft."/"featuring" -> "feat", punctuation
    dropped, whitespace collapsed."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"\b(?:ft|featuring)\b\.?", "feat", text)
    text = re.sub(r"\bfeat\.", "feat", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _text_similarity(a: str, b: str) -> float:
    """0..1: character similarity, or token overlap when the candidate merely wraps the title."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    tokens_a, tokens_b = set(a.split()), set(b.split())
    common = tokens_a & tokens_b
    if not common:
        # "Halo" vs "Hello": short titles look alike letter by letter; without one shared
        # word they are different songs.
        return min(ratio, MAX_SIMILARITY_WITHOUT_SHARED_WORD)
    containment = len(common) / len(tokens_a)  # how much of the Spotify title is present
    jaccard = len(common) / len(tokens_a | tokens_b)
    return max(ratio, 0.5 * containment + 0.5 * jaccard)


def _strip_edition_tail(title: str) -> str | None:
    """ "Song - Remastered 2011" -> "Song"; None when there is no edition tail."""
    parts = youtube.split_artist_title(title)
    if parts is None:
        return None
    head, tail = parts
    return head if _EDITION_TAIL_RE.search(tail) else None


_BRACKET_GROUP_RE = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]")


def _strip_edition_brackets(title: str) -> str:
    """ "Song (Album Version)" / "Song [Remastered 2011]" -> "Song"; other brackets stay."""

    def drop(match: re.Match[str]) -> str:
        return "" if _EDITION_TAIL_RE.search(match.group(1)) else match.group(0)

    return _BRACKET_GROUP_RE.sub(drop, title).strip()


def _without_feat(texts: list[str]) -> list[str]:
    """Normalised, "feat ..." and "(with ...)" credits removed (they are compared as artists,
    not as title words: shared feat credits must not make two different songs look alike),
    with and without bracketed edition markers, deduplicated."""
    variants: list[str] = []
    for text in texts:
        for raw in (text, _strip_edition_brackets(text)):
            norm = normalize_text(_WITH_CREDIT_RE.sub("", raw))
            variant = _FEAT_RE.sub("", norm).strip() or norm
            if variant and variant not in variants:
                variants.append(variant)
    return variants


def spotify_title_variants(title: str) -> list[str]:
    """Normalised forms of the Spotify title to compare against (full, and without an
    edition tail such as " - Remastered 2011")."""
    raw = [title]
    core = _strip_edition_tail(title)
    if core:
        raw.append(core)
    return _without_feat(raw)


def artist_similarity(spotify_artists: list[str], names: list[str]) -> float:
    """Best match between any Spotify artist and any candidate name (channel or credit)."""
    best = 0.0
    for artist in spotify_artists:
        norm_artist = normalize_text(artist)
        if not norm_artist:
            continue
        compact_artist = norm_artist.replace(" ", "")
        for name in names:
            norm_name = normalize_text(_TOPIC_RE.sub("", name or ""))
            if not norm_name:
                continue
            if norm_name == norm_artist:
                return 1.0
            compact_name = norm_name.replace(" ", "")
            if len(compact_artist) >= 3 and (
                compact_artist in compact_name or compact_name in compact_artist
            ):
                best = max(best, 0.9)
                continue
            best = max(best, SequenceMatcher(None, norm_artist, norm_name).ratio())
    return best


def _artist_in_title(spotify_artists: list[str], normalized_title: str) -> bool:
    for artist in spotify_artists:
        norm = normalize_text(artist)
        if norm and re.search(rf"(?<!\w){re.escape(norm)}(?!\w)", normalized_title):
            return True
    return False


def candidate_title_variants(candidate_title: str, spotify_artists: list[str]) -> list[str]:
    """Normalised forms of a candidate title: cleaned of "(Official Video)"-style junk, with
    an "Artist - " prefix (or " - Artist" suffix) removed when that half names the artist,
    and without a trailing "feat ..." part."""
    cleaned = youtube.clean_title(candidate_title) or (candidate_title or "").strip()
    raw = [cleaned]
    parts = youtube.split_artist_title(cleaned)
    if parts is not None:
        left, right = parts
        if artist_similarity(spotify_artists, [left]) >= 0.7:
            raw.append(right)
        if artist_similarity(spotify_artists, [right]) >= 0.7:
            raw.append(left)
    return _without_feat(raw)


def title_similarity(spotify_title: str, candidate_title: str, spotify_artists: list[str]) -> float:
    targets = spotify_title_variants(spotify_title)
    variants = candidate_title_variants(candidate_title, spotify_artists)
    return max(
        (_text_similarity(t, v) for t in targets for v in variants),
        default=0.0,
    )


def duration_score(delta: float | None) -> float:
    if delta is None:
        return UNKNOWN_DURATION_SCORE
    if delta <= 2:
        return 1.0
    if delta <= 5:
        return 0.8
    if delta <= 12:
        return 0.5
    if delta <= MAX_DURATION_DELTA:
        return 0.2
    return 0.0


def variant_penalties(spotify_title: str, candidate_title: str) -> list[str]:
    """Variant words present in the candidate title but absent from the Spotify title."""
    spotify_norm = normalize_text(spotify_title)
    candidate_norm = normalize_text(candidate_title)
    found: list[str] = []
    for word, pattern in _VARIANT_RES.items():
        if pattern.search(candidate_norm) and not pattern.search(spotify_norm):
            found.append(word)
    return found


def _is_auto_upload(candidate: Candidate) -> bool:
    if candidate.auto_upload:
        return True
    return bool(candidate.channel and _TOPIC_RE.search(candidate.channel))


def score_candidate(
    candidate: Candidate,
    title: str,
    artists: list[str],
    duration: float | None,
) -> Scored:
    """Deterministic 0..1 score of one candidate for the Spotify track (title, artists, seconds)."""
    t_sim = title_similarity(title, candidate.title, artists)
    names = list(candidate.artists)
    if candidate.channel:
        names.append(candidate.channel)
    a_sim = artist_similarity(artists, names)
    exact_artist = a_sim >= 1.0
    if (
        a_sim < 0.8
        and candidate.source != "music"  # catalogue entries credit their artists properly
        and _artist_in_title(artists, normalize_text(candidate.title))
    ):
        a_sim = 0.8  # "Artist - Title (Lyrics)" on a random channel: probably the right audio
    delta = (
        abs(float(candidate.duration) - float(duration))
        if candidate.duration is not None and duration is not None
        else None
    )
    penalties = variant_penalties(title, candidate.title)
    score = W_TITLE * t_sim + W_ARTIST * a_sim + W_DURATION * duration_score(delta)
    if _is_auto_upload(candidate):
        score += BONUS_AUTO_UPLOAD
    if exact_artist:
        score += BONUS_ARTIST_EXACT
    score -= min(PENALTY_CAP, PENALTY_VARIANT * len(penalties))
    score = max(0.0, min(1.0, round(score, 4)))
    return Scored(candidate, score, t_sim, a_sim, delta, penalties)


def rank_candidates(
    candidates: list[Candidate], title: str, artists: list[str], duration: float | None
) -> list[Scored]:
    """Every candidate scored, best first (ties keep the search order: YouTube ranks well)."""
    scored = [score_candidate(c, title, artists, duration) for c in candidates if c.video_id]
    return sorted(scored, key=lambda s: -s.score)


def build_queries(title: str | None, artist: str | None) -> list[str]:
    """ "{artist} - {title}", "{title} {artist}", "{title}" (empty / duplicate ones dropped)."""
    title = (title or "").strip()
    artist = (artist or "").strip()
    raw = [f"{artist} - {title}", f"{title} {artist}", title] if artist else [title]
    queries: list[str] = []
    for query in raw:
        query = re.sub(r"\s+", " ", query).strip(" -")
        if query and query not in queries:
            queries.append(query)
    return queries


# --------------------------------------------------------------------------------------------
# yt-dlp searches
# --------------------------------------------------------------------------------------------


def _parse_duration_text(text: str | None) -> float | None:
    if not text or not _DURATION_TEXT_RE.match(text.strip()):
        return None
    total = 0.0
    for part in text.strip().split(":"):
        total = total * 60 + float(part)
    return total


def _runs(column: Any) -> list[dict[str, Any]]:
    renderer = column.get("musicResponsiveListItemFlexColumnRenderer") if column else None
    if not isinstance(renderer, dict):
        renderer = column.get("musicResponsiveListItemFixedColumnRenderer") if column else None
    text = renderer.get("text") if isinstance(renderer, dict) else None
    runs = text.get("runs") if isinstance(text, dict) else None
    return [r for r in runs if isinstance(r, dict)] if isinstance(runs, list) else []


_BULLET = "•"
_ARTIST_SEPARATORS = frozenset({",", "&", "and", ", and", ",&"})
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,\s*&|,\s*and\b|,|&)\s*")
_COUNT_TEXT_RE = re.compile(r"^[\d.,]+\s*[KMB]?\s*(?:views|plays|likes)$", re.IGNORECASE)


def _browse_id(run: dict[str, Any]) -> str:
    endpoint = run.get("navigationEndpoint")
    browse = endpoint.get("browseEndpoint") if isinstance(endpoint, dict) else None
    browse_id = browse.get("browseId") if isinstance(browse, dict) else None
    return browse_id if isinstance(browse_id, str) else ""


def _candidate_from_renderer(renderer: Any) -> Candidate | None:
    """One `musicResponsiveListItemRenderer` of the Songs shelf -> Candidate."""
    if not isinstance(renderer, dict):
        return None
    video_id = (renderer.get("playlistItemData") or {}).get("videoId")
    if not isinstance(video_id, str) or not video_id:
        overlay = renderer.get("overlay") or {}
        video_id = (
            (
                (overlay.get("musicItemThumbnailOverlayRenderer") or {})
                .get("content", {})
                .get("musicPlayButtonRenderer", {})
                .get("playNavigationEndpoint", {})
                .get("watchEndpoint", {})
                .get("videoId")
            )
            if isinstance(overlay, dict)
            else None
        )
    if not isinstance(video_id, str) or not video_id:
        return None
    columns = renderer.get("flexColumns")
    columns = [c for c in columns if isinstance(c, dict)] if isinstance(columns, list) else []
    if not columns:
        return None
    title_runs = _runs(columns[0])
    title = "".join(str(r.get("text") or "") for r in title_runs).strip()
    if not title:
        return None
    artists: list[str] = []
    album: str | None = None
    duration: float | None = None
    fixed = renderer.get("fixedColumns")
    fixed = [c for c in fixed if isinstance(c, dict)] if isinstance(fixed, list) else []
    for index, column in enumerate(columns[1:] + fixed, start=1):
        # Column 1 reads "Artist, Other & Third • Album • 4:36": only the first artist reliably
        # carries a browse link (and sometimes none does), so there the bullet-separated
        # segments decide what a run is and the browse ids merely confirm it. Further columns
        # ("81M plays", a fixed duration column) only contribute linked or duration-shaped runs.
        segmented = index == 1
        segment = 0
        for run in _runs(column):
            text = str(run.get("text") or "").strip()
            if not text or _COUNT_TEXT_RE.match(text):
                continue
            if text == _BULLET:
                segment += 1
                continue
            browse_id = _browse_id(run)
            if browse_id.startswith("UC"):
                if text not in artists:
                    artists.append(text)
            elif browse_id.startswith("MPRE"):
                album = text
            elif duration is None and _DURATION_TEXT_RE.match(text):
                duration = _parse_duration_text(text)
            elif segmented and segment == 0 and text not in _ARTIST_SEPARATORS:
                # sometimes one run holds every name: "KAROL G, Judeline, & rusowsky"
                for name in _ARTIST_SPLIT_RE.split(text):
                    if name and name not in artists:
                        artists.append(name)
            elif segmented and segment == 1 and album is None:
                album = text
    return Candidate(
        video_id=video_id,
        title=title,
        channel=", ".join(artists) or None,
        artists=artists,
        album=album,
        duration=duration,
        auto_upload=True,
        source="music",
    )


def parse_music_search(data: Any) -> list[Candidate]:
    """Candidates from a YouTube Music search API response (the Songs shelf items)."""
    if not isinstance(data, dict):
        return []
    tabs = ((data.get("contents") or {}).get("tabbedSearchResultsRenderer") or {}).get("tabs")
    sections: list[Any] = []
    for tab in tabs if isinstance(tabs, list) else []:
        content = ((tab or {}).get("tabRenderer") or {}).get("content") or {}
        items = (content.get("sectionListRenderer") or {}).get("contents")
        if isinstance(items, list):
            sections.extend(items)
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for section in sections:
        shelf = section.get("musicShelfRenderer") if isinstance(section, dict) else None
        if not isinstance(shelf, dict):
            continue
        for item in shelf.get("contents") or []:
            renderer = (
                item.get("musicResponsiveListItemRenderer") if isinstance(item, dict) else None
            )
            candidate = _candidate_from_renderer(renderer)
            if candidate is not None and candidate.video_id not in seen:
                seen.add(candidate.video_id)
                candidates.append(candidate)
    return candidates


def _search_opts(settings: Settings | None) -> dict[str, Any]:
    names = list(settings.js_runtimes) if settings and settings.js_runtimes else None
    return {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": SEARCH_LIMIT,
        "js_runtimes": youtube._js_runtimes_opt(names),
        "logger": youtube._YdlLogger(quiet_warnings=True),
    }


def search_music(query: str, settings: Settings | None = None) -> list[Candidate]:
    """The YouTube Music "Songs" shelf for `query`, with artists, album and duration.

    yt-dlp's flat extraction of ``music.youtube.com/search`` only keeps id and title, so the
    same API call is made through yt-dlp's extractor plumbing and the shelf is parsed here.
    """
    with youtube.YoutubeDL(_search_opts(settings)) as ydl:
        ie = ydl.get_info_extractor("YoutubeMusicSearchURL")
        ie.initialize()
        data = ie._extract_response(
            item_id=f"search {query}",
            query={"query": query, "params": MUSIC_SONGS_PARAMS},
            ep="search",
            default_client="web_music",
            check_get_keys=("contents",),
            note="Searching YouTube Music",
        )
    return parse_music_search(data)[:SEARCH_LIMIT]


def _candidate_from_entry(entry: Any) -> Candidate | None:
    if not isinstance(entry, dict):
        return None
    video_id = entry.get("id")
    title = entry.get("title")
    if not isinstance(video_id, str) or not video_id or not isinstance(title, str) or not title:
        return None
    if entry.get("live_status") in ("is_live", "is_upcoming"):
        return None
    channel = entry.get("channel") or entry.get("uploader")
    channel = str(channel).strip() if channel else None
    description = str(entry.get("description") or "")
    duration = youtube._float_or_none(entry.get("duration"))
    artists_raw = entry.get("artists")
    artists = (
        [str(a) for a in artists_raw if a]
        if isinstance(artists_raw, list)
        else ([str(entry["artist"])] if entry.get("artist") else [])
    )
    return Candidate(
        video_id=video_id,
        title=title,
        channel=channel,
        artists=artists,
        album=str(entry["album"]) if entry.get("album") else None,
        duration=duration,
        auto_upload=bool(channel and _TOPIC_RE.search(channel))
        or description.startswith("Provided to YouTube"),
        source="youtube",
    )


def search_youtube(query: str, settings: Settings | None = None) -> list[Candidate]:
    """Plain YouTube search (``ytsearchN:``): title, channel and duration per entry."""
    with youtube.YoutubeDL(_search_opts(settings)) as ydl:
        info = ydl.extract_info(f"ytsearch{SEARCH_LIMIT}:{query}", download=False)
    entries = info.get("entries") if isinstance(info, dict) else None
    candidates: list[Candidate] = []
    for entry in entries if isinstance(entries, list) else []:
        candidate = _candidate_from_entry(entry)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def default_search(query: str, settings: Settings | None = None) -> list[Candidate]:
    """YouTube Music first; plain YouTube when that fails or finds nothing."""
    try:
        results = search_music(query, settings)
    except Exception as exc:  # noqa: BLE001 - the fallback search decides what is fatal
        log.warning("YouTube Music search failed for %r (%s); trying plain YouTube", query, exc)
        results = []
    if results:
        return results
    return search_youtube(query, settings)


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "?:??"
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _artists_of(ref: TrackRef) -> list[str]:
    artist = (ref.artist or "").strip()
    if not artist:
        return []
    names = [a.strip() for a in artist.split(",")]
    names = [a for a in names if a]
    # "Tyler, The Creator" is one artist: keep the joined form as a candidate name as well.
    if artist not in names:
        names.append(artist)
    return names


def find_match(
    ref: TrackRef,
    settings: Settings | None,
    cancel: threading.Event | None = None,
    search: SearchFn | None = None,
) -> Match | None:
    """The best acceptable YouTube video for `ref`, or None when nothing is good enough.

    Queries are tried in order ("Artist - Title", "Title Artist", "Title") and the first query
    that yields an accepted candidate wins. `search(query) -> list[Candidate]` defaults to
    yt-dlp (YouTube Music, then plain YouTube). Raises DownloadCancelled when `cancel` is set
    and ProviderError when the search itself fails.
    """
    title = (ref.title or "").strip()
    if not title:
        return None
    artists = _artists_of(ref)
    queries = build_queries(title, ref.artist)
    do_search: SearchFn = search if search is not None else (lambda q: default_search(q, settings))
    best_overall: Scored | None = None
    for query in queries:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("Download cancelled")
        try:
            candidates = do_search(query)
        except (DownloadCancelled, ProviderError):
            raise
        except yt_dlp.utils.YoutubeDLError as exc:
            raise ProviderError(youtube.friendly_error(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("YouTube Music search failed for %r", query)
            raise ProviderError(f"Could not search YouTube Music ({exc}).") from exc
        ranked = rank_candidates(list(candidates or []), title, artists, ref.duration)
        for scored in ranked[:3]:
            c = scored.candidate
            log.debug(
                "match %r: %.2f %s %r by %s (%s) title=%.2f artist=%.2f delta=%s penalties=%s",
                query,
                scored.score,
                c.video_id,
                c.title,
                c.channel or "?",
                _format_duration(c.duration),
                scored.title_similarity,
                scored.artist_similarity,
                None if scored.duration_delta is None else round(scored.duration_delta, 1),
                scored.penalties,
            )
        if ranked and (best_overall is None or ranked[0].score > best_overall.score):
            best_overall = ranked[0]
        for scored in ranked:
            if scored.accepted:
                c = scored.candidate
                log.info(
                    "Matched '%s - %s' to %s (%r by %s, %s, score %.2f, query %r)",
                    ref.artist,
                    title,
                    c.video_id,
                    c.title,
                    c.channel or "?",
                    _format_duration(c.duration),
                    scored.score,
                    query,
                )
                return Match(
                    video_id=c.video_id,
                    url=c.url,
                    title=c.title,
                    channel=c.channel,
                    duration=c.duration,
                    score=scored.score,
                    query=query,
                    album=c.album,
                )
    if best_overall is not None:
        c = best_overall.candidate
        log.info(
            "No acceptable match for '%s - %s'; best was %r by %s (%s) at %.2f",
            ref.artist,
            title,
            c.title,
            c.channel or "?",
            _format_duration(c.duration),
            best_overall.score,
        )
    return None
