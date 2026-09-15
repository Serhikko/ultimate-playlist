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
# Per variant word (live, remix, ...) that one title has and the other lacks. 0.5 is chosen so
# that even a candidate that is perfect otherwise (1.0 + both bonuses) ends below ACCEPT_SCORE.
PENALTY_VARIANT = 0.5
PENALTY_CAP = 1.0
ACCEPT_SCORE = 0.62
MIN_TITLE_SIMILARITY = 0.5  # never accept a candidate whose title barely resembles the track
# ...and when one title has words the other lacks ("Stay" vs "Stay With Me"), only a
# near-identical spelling is accepted: those extra words usually name another song. An extra
# number or part word ("Stay, Pt. 2", "Song 2") is never accepted.
MIN_TITLE_SIMILARITY_WITH_EXTRA_WORDS = 0.8
MAX_SIMILARITY_WITHOUT_SHARED_WORD = 0.45  # titles with no word in common stay below that
MIN_ARTIST_SIMILARITY = 0.6  # nor one credited to somebody else (a cover band, a tribute act)
# One name hidden inside another ("Adele" / "Adeleine", "Nas" / "Lil Nas X") is somebody else:
# capped below MIN_ARTIST_SIMILARITY.
MAX_SIMILARITY_CONTAINED_NAME = 0.5
MAX_DURATION_DELTA = 25.0  # seconds; hard limit when both durations are known
# When the Spotify duration is unknown nothing longer than this is accepted (10-hour loops,
# full-album uploads): 15 minutes is longer than almost any song.
MAX_LENGTH_WITHOUT_DURATION = 900.0
SEARCH_LIMIT = 10
UNKNOWN_DURATION_SCORE = 0.5  # neutral when one side has no duration

# Words that mark a different recording. A candidate with one the Spotify title lacks is
# penalised, and so is a candidate that lacks one the Spotify title has ("Song - Live" must not
# match the studio "Song").
VARIANT_WORDS: tuple[str, ...] = (
    "live",
    "remix",
    "mix",
    "cover",
    "karaoke",
    "instrumental",
    "acoustic",
    "unplugged",
    "stripped",
    "piano",
    "orchestral",
    "a cappella",
    "demo",
    "sped up",
    "slowed",
    "nightcore",
    "8d",
    "reverb",
    "lofi",
    "reaction",
    "tutorial",
    "extended",
    "mashup",
    "edit",
    "hour",
    "loop",
)
# Spellings beyond the plain word (matched on normalize_text output: "Lo-Fi" -> "lo fi").
_VARIANT_SPELLINGS: dict[str, str] = {
    "remix": r"remix(?:ed)?",
    "a cappella": r"a ?capp?ella",
    "demo": r"demos?",
    "lofi": r"lo ?fi",
    "hour": r"hours?",
    "loop": r"loop(?:s|ed)?",
}
_VARIANT_RES: dict[str, re.Pattern[str]] = {
    word: re.compile(rf"(?<!\w)(?:{_VARIANT_SPELLINGS.get(word, re.escape(word))})(?!\w)")
    for word in VARIANT_WORDS
}
# Words that describe the same kind of recording: an "MTV Unplugged" upload is the live
# version, a "Club Mix" is a remix. A variant word is excused when the other title has a
# related one.
_RELATED_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"live", "unplugged"}),
    frozenset({"acoustic", "unplugged", "stripped"}),
    frozenset({"remix", "mix"}),
    frozenset({"slowed", "reverb"}),
)
_RELATED: dict[str, frozenset[str]] = {
    word: frozenset({word}).union(*(g for g in _RELATED_GROUPS if word in g))
    for word in VARIANT_WORDS
}
# A Spotify "Song - Edit" / "Song - Extended Version" may still match the plain single (the
# duration check decides), so these two are only penalised when the candidate has them.
_SPOTIFY_ONLY_EXEMPT = frozenset({"edit", "extended"})
# Mix / edit phrases that label an edition of the same recording, not a remix or a cut-down
# version: removed before looking for variant words ("2009 Stereo Mix", "Radio Edit").
_EDITION_PHRASE_RE = re.compile(
    r"(?<!\w)(?:(?:original|stereo|mono|album|single|radio|main|\d{4}|remaster(?:ed)?)\s+mix"
    r"|(?:radio|single|album)\s+edit)(?!\w)"
)
# A " - Tail" or bracket that labels the edition of the same recording ("Song - Remastered
# 2011", "Song (Album Version)", "Song - Radio Edit", "Song (Clean)"). One that also holds a
# variant word ("Live Version", "Club Mix", "1995 Demo") names a different recording and is
# never stripped.
_EDITION_TAIL_RE = re.compile(
    r"\b(?:remaster(?:ed)?|version|ver|edit|mix|mono|stereo|bonus|deluxe|single|radio|"
    r"original|album|anniversary|edition|explicit|clean|from|soundtrack|\d{4})\b",
    re.IGNORECASE,
)
# "Version" on its own is no edition label: "Spanish Version", "Alternate Version" and
# "Taylor's Version" are other recordings. It only counts after one of these qualifiers
# ("Album Version", "Extended Version", "2011 Version").
_BARE_VERSION_RE = re.compile(r"^ver(?:sion)?$", re.IGNORECASE)
_VERSION_QUALIFIER_RE = re.compile(
    r"\b(?:album|single|radio|original|remaster(?:ed)?|explicit|clean|edited|mono|stereo|"
    r"deluxe|extended|\d{4})\s+ver(?:sion)?\b",
    re.IGNORECASE,
)
_FEAT_RE = re.compile(r"\bfeat\b.*$")
# A bracketed credit: Spotify's "Rein Me In (with Olivia Dean)" (YouTube Music's catalogue
# title usually leaves it out) and "(feat. X)". Only the bracketed "with": a plain "with"
# inside a title ("Girl with the Tattoo") is part of the title.
_WITH_CREDIT_RE = re.compile(
    r"\s*[(\[]\s*(?:with|feat\.?|ft\.?|featuring)\s+[^()\[\]]*[)\]]", re.IGNORECASE
)
# The last " - " of a Spotify title: "I Want You (She's So Heavy) - Remastered 2009".
_LAST_DASH_RE = re.compile(r"^(.*\S)\s+[-–—]\s+(\S.*?)\s*$")
_FIRST_DASH_RE = re.compile(r"^(.*?\S)\s+[-–—]\s+\S")
_DASH_SPLIT_RE = re.compile(r"\s+[-–—]\s+")
# Title words that never tell two songs apart (articles, leftover upload junk).
_NEUTRAL_TITLE_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "of",
        "feat",
        "official",
        "video",
        "audio",
        "lyrics",
        "lyric",
        "visualizer",
        "visualiser",
        "hd",
        "hq",
        "4k",
        "mv",
    }
)
# Words that number a song within a series: "Pt. 2", "Part II", "Vol. 3".
_SEQUENCE_WORDS = frozenset({"part", "vol", "volume", "chapter", "episode"})
_ROMAN_NUMERALS = {
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
}
_UNAMBIGUOUS_ROMAN = frozenset({"ii", "iii", "iv", "vi", "vii", "viii", "ix"})
# Channel-name decoration around an artist name: "Sam Fender Official", "ArianaGrandeVevo".
_CHANNEL_FILLER = frozenset({"official", "vevo", "music", "tv", "channel"})
_COMPACT_PREFIX_RE = re.compile(r"^(?:official|the)")
_COMPACT_SUFFIX_RE = re.compile(r"(?:vevo|official|music|tv|channel)$")
# Separators between the credits of one artist string ("Bruce Springsteen & The E Street Band").
_CREDIT_SPLIT_RE = re.compile(
    r"\s*(?:[,&+/;]|\s(?:and|with|feat\.?|ft\.?|featuring|x|vs\.?)\s)\s*", re.IGNORECASE
)
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
    # words one title has and the other lacks ("with", "me" for "Stay" vs "Stay With Me")
    extra_words: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        if self.score < ACCEPT_SCORE or self.title_similarity < MIN_TITLE_SIMILARITY:
            return False
        if self.extra_words and (
            self.title_similarity < MIN_TITLE_SIMILARITY_WITH_EXTRA_WORDS
            or any(_is_sequence_word(word) for word in self.extra_words)
        ):
            return False
        if self.artist_similarity < MIN_ARTIST_SIMILARITY:
            return False
        if self.duration_delta is None:
            length = self.candidate.duration
            return length is None or length <= MAX_LENGTH_WITHOUT_DURATION
        return self.duration_delta <= MAX_DURATION_DELTA


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


def _variant_counts(normalized: str) -> dict[str, int]:
    """How often each of the VARIANT_WORDS occurs in an already normalised text (edition
    phrases such as "2009 Stereo Mix" or "Radio Edit" do not count). Counted, not just
    noticed: "Live Forever (Live at Knebworth)" has one "live" more than "Live Forever"."""
    text = _EDITION_PHRASE_RE.sub(" ", normalized)
    counts = {word: len(pattern.findall(text)) for word, pattern in _VARIANT_RES.items()}
    return {word: count for word, count in counts.items() if count}


def _variant_words(normalized: str) -> set[str]:
    """The VARIANT_WORDS in an already normalised text (see _variant_counts)."""
    return set(_variant_counts(normalized))


def _has_edition_word(text: str) -> bool:
    """Does `text` hold an edition word? A bare "Version" / "Ver." only counts after a
    qualifier ("Album Version" yes; "Spanish Version", "Taylor's Version" no)."""
    hits = [match.group(0) for match in _EDITION_TAIL_RE.finditer(text)]
    if not hits:
        return False
    if all(_BARE_VERSION_RE.match(hit) for hit in hits):
        return bool(_VERSION_QUALIFIER_RE.search(text))
    return True


def _is_edition_marker(text: str) -> bool:
    """True for "Remastered 2011", "2015 Remaster", "Album Version", "Radio Edit"; False for
    text that names a different recording ("Live Version", "Club Mix", "1995 Demo",
    "Spanish Version")."""
    if not _has_edition_word(text):
        return False
    return not (_variant_words(normalize_text(text)) - _SPOTIFY_ONLY_EXEMPT)


def _is_descriptor(text: str) -> bool:
    """A bracket / tail that describes the recording (edition, live, remix, ...) rather than
    naming the song: "Live at Wembley 1986", "2015 Remaster", "From 'Some Film'". "Spanish
    Version" is not one: it names another recording, so its words count as title words."""
    return _has_edition_word(text) or bool(_variant_words(normalize_text(text)))


def _names_another_version(text: str) -> bool:
    """ "Spanish Version", "Taylor's Version", "Alternate Version": a version that is neither
    an edition label nor a variant (live, acoustic, ...) the variant check already covers."""
    return (
        any(_BARE_VERSION_RE.match(word) for word in normalize_text(text).split())
        and not _is_edition_marker(text)
        and not _variant_words(normalize_text(text))
    )


def _strip_edition_tail(title: str) -> str | None:
    """ "Song - Remastered 2011" -> "Song"; None when there is no edition tail. The last
    " - " counts, so a bracket before it is fine ("I Want You (She's So Heavy) - Remastered
    2009"); "Song - Live at Wembley 1986" keeps its tail."""
    match = _LAST_DASH_RE.match(title or "")
    if not match:
        return None
    head, tail = match.group(1).strip(), match.group(2).strip()
    return head if head and _is_edition_marker(tail) else None


_BRACKET_GROUP_RE = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]")


def _strip_edition_brackets(title: str) -> str:
    """ "Song (Album Version)" / "Song [Remastered 2011]" -> "Song"; other brackets stay,
    including "(Live Version)" and "(DJ Y Remix)"."""

    def drop(match: re.Match[str]) -> str:
        return "" if _is_edition_marker(match.group(1)) else match.group(0)

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


def _name_core(normalized: str) -> str:
    """ "sam fender official" -> "sam fender", "the weeknd" -> "weeknd"."""
    words = [w for w in normalized.split() if w not in _CHANNEL_FILLER]
    if len(words) > 1 and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def _compact_core(normalized: str) -> str:
    """ "arianagrandevevo" -> "arianagrande": spaces and channel decoration dropped."""
    compact = normalized.replace(" ", "")
    for _ in range(2):  # "OfficialTheWeekndVEVO"
        compact = _COMPACT_SUFFIX_RE.sub("", _COMPACT_PREFIX_RE.sub("", compact))
    return compact


def _credit_parts(name: str) -> list[str]:
    """ "Bruce Springsteen & The E Street Band" -> ["bruce springsteen", "the e street band"]."""
    return [normalize_text(part) for part in _CREDIT_SPLIT_RE.split(name or "") if part.strip()]


def _name_similarity(artist: str, name: str) -> float:
    """0..1 for one Spotify artist against one candidate name (channel or credit)."""
    norm_artist, norm_name = normalize_text(artist), normalize_text(name)
    if not norm_artist or not norm_name:
        return 0.0
    if norm_artist == norm_name:
        return 1.0
    core_artist, core_name = _name_core(norm_artist), _name_core(norm_name)
    compact_artist = _compact_core(norm_artist)
    if (core_artist and core_artist == core_name) or (
        len(compact_artist) >= 3 and compact_artist == _compact_core(norm_name)
    ):
        return 0.9  # "Sam Fender Official", "ArianaGrandeVevo", "The Weeknd" / "Weeknd"
    # one side is a whole credit of the other ("Bruce Springsteen & The E Street Band")
    for short, other in ((core_artist, name), (core_name, artist)):
        if len(short.replace(" ", "")) >= 3 and any(
            _name_core(part) == short for part in _credit_parts(other)
        ):
            return 0.9
    ratio = SequenceMatcher(None, norm_artist, norm_name).ratio()
    plain_artist, plain_name = norm_artist.replace(" ", ""), norm_name.replace(" ", "")
    if plain_artist in plain_name or plain_name in plain_artist:
        # "Adele" / "Adeleine", "Nas" / "Nasty C" / "Lil Nas X", "Ye" / "Kanye West"
        return min(ratio, MAX_SIMILARITY_CONTAINED_NAME)
    return ratio


def artist_similarity(spotify_artists: list[str], names: list[str]) -> float:
    """Best match between any Spotify artist and any candidate name (channel or credit):
    1.0 for the same name, 0.9 for the same name decorated as a channel ("ArianaGrandeVevo",
    "Sam Fender Official") or as one credit of a longer one ("Bruce Springsteen & The E Street
    Band"), else a character ratio, capped low when one name merely contains the other."""
    best = 0.0
    for artist in spotify_artists:
        if not normalize_text(artist):
            continue
        for name in names:
            similarity = _name_similarity(artist, _TOPIC_RE.sub("", name or ""))
            if similarity >= 1.0:
                return 1.0
            best = max(best, similarity)
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
    raw = _candidate_forms(candidate_title, spotify_artists, 0.7, keep_full=True)
    for text in list(raw):
        core = _strip_edition_tail(text)  # "Hotel California - 2013 Remaster"
        if core and core not in raw:
            raw.append(core)
    return _without_feat(raw)


def _candidate_forms(
    candidate_title: str, spotify_artists: list[str], threshold: float, keep_full: bool
) -> list[str]:
    """The cleaned candidate title (junk dropped) and, when one half of "Artist - Title" names
    a Spotify artist (similarity >= threshold), the other half. keep_full=False returns only
    that other half when there is one."""
    cleaned = youtube.clean_title(candidate_title) or (candidate_title or "").strip()
    halves: list[str] = []
    parts = youtube.split_artist_title(cleaned)
    if parts is not None:
        left, right = parts
        if artist_similarity(spotify_artists, [left]) >= threshold:
            halves.append(right)
        if artist_similarity(spotify_artists, [right]) >= threshold:
            halves.append(left)
    if halves and not keep_full:
        return halves
    return [cleaned, *halves]


def title_similarity(spotify_title: str, candidate_title: str, spotify_artists: list[str]) -> float:
    targets = spotify_title_variants(spotify_title)
    variants = candidate_title_variants(candidate_title, spotify_artists)
    return max(
        (_text_similarity(t, v) for t in targets for v in variants),
        default=0.0,
    )


def _tokens(normalized: str) -> set[str]:
    """Words of a normalised title for the extra-word check, with "pt" read as "part" and
    roman numerals as digits ("Part II" = "Pt. 2"; a lone "I" or "X" only after "part")."""
    words: list[str] = []
    for word in normalized.split():
        word = "part" if word == "pt" else word
        if word in _ROMAN_NUMERALS and (
            word in _UNAMBIGUOUS_ROMAN or (words and words[-1] in _SEQUENCE_WORDS)
        ):
            word = _ROMAN_NUMERALS[word]
        words.append(word)
    return set(words)


def _words(text: str) -> set[str]:
    return _tokens(normalize_text(text))


def _is_sequence_word(word: str) -> bool:
    return word.isdigit() or word in _SEQUENCE_WORDS


def _spotify_core_words(title: str) -> set[str]:
    """Words of the song name proper: credits, every bracket and any " - tail" dropped,
    except a bracket or tail naming another version ("(Taylor's Version)", " - Spanish
    Version"): a candidate without those words is a different recording."""
    text = _BRACKET_GROUP_RE.sub(
        lambda m: f" {m.group(1)} " if _names_another_version(m.group(1)) else " ",
        _WITH_CREDIT_RE.sub(" ", title or ""),
    )
    match = _FIRST_DASH_RE.match(text)
    if match:
        tail = _DASH_SPLIT_RE.split(text, maxsplit=1)[-1]
        text = match.group(1) + (f" {tail}" if _names_another_version(tail) else "")
    return _tokens(_FEAT_RE.sub("", normalize_text(text)))


def _candidate_core_words(text: str) -> set[str]:
    """Words of one candidate title form without credits, descriptor brackets ("(Live at
    Wembley 1986)", "(2015 Remaster)") and a descriptor " - tail". A bracket that names
    something else ("(Part 2)") stays."""
    text = _WITH_CREDIT_RE.sub(" ", text)
    text = _BRACKET_GROUP_RE.sub(
        lambda m: " " if _is_descriptor(m.group(1)) else m.group(0), text
    ).strip()
    match = _LAST_DASH_RE.match(text)
    if match and _is_descriptor(match.group(2)):
        text = match.group(1)
    return _tokens(_FEAT_RE.sub("", normalize_text(text)))


def title_extra_words(
    spotify_title: str, candidate_title: str, spotify_artists: list[str]
) -> list[str]:
    """Words that tell the two titles apart once editions, credits, artist names and upload
    junk are set aside: what the candidate adds ("Stay" -> "Stay With Me": me, with; "Stay" ->
    "Stay, Pt. 2": 2, pt) and what it leaves out of the song name ("Stay With Me" -> "Stay").
    Empty for two spellings of the same song."""
    ignore = set(_NEUTRAL_TITLE_WORDS)
    for artist in spotify_artists:
        ignore |= _words(artist)
    spotify_all = _words(spotify_title)
    forms = _candidate_forms(candidate_title, spotify_artists, 0.9, keep_full=False)
    added = min(
        (_candidate_core_words(form) - spotify_all - ignore for form in forms),
        key=len,
        default=set(),
    )
    missing = _spotify_core_words(spotify_title) - _words(candidate_title) - ignore
    return sorted(added | missing)


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


def _variant_text(title: str, artists: list[str] | None) -> str:
    """Normalised title for the variant-word check, bracketed credits dropped ("(feat. Live
    Wire)"). Candidate side (artists given): upload junk dropped too, and an "Artist - " half
    that names the artist (the band called Live)."""
    text = _WITH_CREDIT_RE.sub(" ", title or "")
    if artists is not None:
        text = youtube.clean_title(text) or text
        parts = youtube.split_artist_title(text)
        if parts is not None:
            left, right = parts
            if artist_similarity(artists, [left]) >= 0.9:
                text = right
            elif artist_similarity(artists, [right]) >= 0.9:
                text = left
    return normalize_text(text)


def variant_penalties(
    spotify_title: str, candidate_title: str, artists: list[str] | None = None
) -> list[str]:
    """Variant words (VARIANT_WORDS) one title has more often than the other: "live" for a
    live candidate of a studio track, "missing live" for a studio candidate of a live track.
    Occurrences are counted, so a variant word that is part of the song name does not hide
    one that labels the recording ("Live Forever" vs "Live Forever (Live at Knebworth)"). A
    related word on the other side excuses one ("Live" vs "MTV Unplugged", "Remix" vs "Club
    Mix"); "edit" / "extended" on the Spotify side alone are not penalised."""
    spotify = _variant_counts(_variant_text(spotify_title, None))
    candidate = _variant_counts(_variant_text(candidate_title, artists or []))

    def excused(word: str, other: dict[str, int]) -> bool:
        return any(other.get(related) for related in _RELATED[word] - {word})

    found = [
        word
        for word in VARIANT_WORDS
        if candidate.get(word, 0) > spotify.get(word, 0) and not excused(word, spotify)
    ]
    found += [
        f"missing {word}"
        for word in VARIANT_WORDS
        if spotify.get(word, 0) > candidate.get(word, 0)
        and word not in _SPOTIFY_ONLY_EXEMPT
        and not excused(word, candidate)
    ]
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
    penalties = variant_penalties(title, candidate.title, artists)
    extra_words = title_extra_words(title, candidate.title, artists)
    score = W_TITLE * t_sim + W_ARTIST * a_sim + W_DURATION * duration_score(delta)
    if _is_auto_upload(candidate):
        score += BONUS_AUTO_UPLOAD
    if exact_artist:
        score += BONUS_ARTIST_EXACT
    score -= min(PENALTY_CAP, PENALTY_VARIANT * len(penalties))
    score = max(0.0, min(1.0, round(score, 4)))
    return Scored(candidate, score, t_sim, a_sim, delta, penalties, extra_words)


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
                "match %r: %.2f %s %r by %s (%s) title=%.2f artist=%.2f delta=%s penalties=%s "
                "extra=%s accepted=%s",
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
                scored.extra_words,
                scored.accepted,
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
