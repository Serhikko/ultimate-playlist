"""Offline tests for the YouTube Music matcher: scoring, query order, search plumbing.

Scoring is pure, so the interesting cases (live versions, remixes, duration mismatches,
multi-artist credits, accents) run against canned candidates. The two yt-dlp searches run
against a fake YoutubeDL; nothing here touches the network.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
import yt_dlp.utils

from ultimate_playlist.config import Settings
from ultimate_playlist.models import TrackRef
from ultimate_playlist.providers import youtube
from ultimate_playlist.providers import ytmusic_match as ym
from ultimate_playlist.providers.base import DownloadCancelled, ProviderError
from ultimate_playlist.providers.ytmusic_match import (
    ACCEPT_SCORE,
    MAX_DURATION_DELTA,
    MIN_TITLE_SIMILARITY,
    Candidate,
    Scored,
    build_queries,
    find_match,
    normalize_text,
    score_candidate,
)

TITLE = "Give Life Back to Music"
ARTIST = "Daft Punk"
DURATION = 275.386


def ref(
    title: str | None = TITLE, artist: str | None = ARTIST, duration: float | None = DURATION
) -> TrackRef:
    return TrackRef(
        provider="spotify",
        source_id="0dEIca2nhcxDUV8C5QkPYb",
        url="https://open.spotify.com/track/0dEIca2nhcxDUV8C5QkPYb",
        title=title,
        artist=artist,
        duration=duration,
    )


def cand(
    video_id: str = "v1",
    title: str = TITLE,
    channel: str | None = f"{ARTIST} - Topic",
    duration: float | None = 275.0,
    artists: list[str] | None = None,
    auto_upload: bool = False,
    source: str = "youtube",
) -> Candidate:
    return Candidate(
        video_id=video_id,
        title=title,
        channel=channel,
        artists=list(artists or []),
        duration=duration,
        auto_upload=auto_upload,
        source=source,
    )


def score(
    candidate: Candidate,
    title: str = TITLE,
    artists: list[str] | None = None,
    duration: float | None = DURATION,
) -> Scored:
    return score_candidate(candidate, title, artists if artists is not None else [ARTIST], duration)


def match_with(candidates: list[Candidate], the_ref: TrackRef | None = None) -> ym.Match | None:
    return find_match(the_ref or ref(), None, threading.Event(), search=lambda q: candidates)


# ----------------------------------------------------------------------------------------------
# normalisation and similarities
# ----------------------------------------------------------------------------------------------


def test_normalize_text() -> None:
    assert normalize_text("Déjà Vu (feat. JAY-Z)") == "deja vu feat jay z"
    assert normalize_text("Rock & Roll") == "rock and roll"
    assert normalize_text("Song ft. Someone") == "song feat someone"
    assert normalize_text("  Many   spaces\t here ") == "many spaces here"
    assert normalize_text("") == ""
    assert normalize_text(None) == ""


def test_title_similarity_variants() -> None:
    assert ym.title_similarity(TITLE, TITLE, [ARTIST]) == 1.0
    # the artist prefix and "(Official Video)" are not part of the title
    assert (
        ym.title_similarity(TITLE, "Daft Punk - Give Life Back to Music (Official Video)", [ARTIST])
        == 1.0
    )
    assert (
        ym.title_similarity(TITLE, "Give Life Back to Music - Daft Punk [Lyrics]", [ARTIST]) == 1.0
    )
    # feat. credits on either side do not hurt
    assert (
        ym.title_similarity("Instant Crush (feat. Julian Casablancas)", "Instant Crush", [ARTIST])
        == 1.0
    )
    assert (
        ym.title_similarity("Instant Crush", "Instant Crush (feat. Julian Casablancas)", [ARTIST])
        == 1.0
    )
    # Spotify's " - Remastered 2011" style edition tail is optional
    assert ym.title_similarity("Song - Remastered 2011", "Song", ["A"]) == 1.0
    assert ym.title_similarity("Song - Radio Edit", "Artist - Song", ["Artist"]) == 1.0
    # a different song from the same album is far away
    assert ym.title_similarity(TITLE, "Giorgio by Moroder", [ARTIST]) < MIN_TITLE_SIMILARITY
    # short titles that merely look alike letter by letter are different songs
    assert ym.title_similarity("Halo", "Hello", ["Beyoncé"]) < MIN_TITLE_SIMILARITY
    assert ym.title_similarity("Song (Album Version)", "Song", ["A"]) >= MIN_TITLE_SIMILARITY
    # ...even when both carry the same feat. credit
    assert (
        ym.title_similarity(
            "Get Lucky (feat. Pharrell Williams and Nile Rodgers)",
            "Lose Yourself to Dance (feat. Pharrell Williams)",
            [ARTIST, "Pharrell Williams", "Nile Rodgers"],
        )
        < MIN_TITLE_SIMILARITY
    )
    # an unrelated " - Something" half is not stripped
    assert ym.title_similarity("Song", "Song - Piano Version", ["Artist"]) < 1.0
    # Spotify's "(with Artist)" collaboration credit is a credit, not part of the title (seen
    # live: "Rein Me In (with Olivia Dean)" vs YouTube Music's "Rein Me In")
    assert (
        ym.title_similarity(
            "Rein Me In (with Olivia Dean)", "Rein Me In", ["Sam Fender", "Olivia Dean"]
        )
        == 1.0
    )
    assert ym.title_similarity("Rein Me In", "Rein Me In [with Olivia Dean]", ["Sam Fender"]) == 1.0
    # ...but a plain "with" inside the title is part of it
    assert ym.title_similarity("Girl with the Tattoo", "Girl with the Tattoo", ["A"]) == 1.0
    assert ym.title_similarity("Girl with the Tattoo", "Girl", ["A"]) < 1.0


def test_artist_similarity() -> None:
    assert ym.artist_similarity(["Daft Punk"], ["Daft Punk - Topic"]) == 1.0
    assert ym.artist_similarity(["Daft Punk"], ["daft punk"]) == 1.0
    assert ym.artist_similarity(["Beyoncé"], ["Beyonce"]) == 1.0
    assert (
        ym.artist_similarity(["KAROL G", "Judeline", "rusowsky"], ["Vibe Music", "Judeline"]) == 1.0
    )
    assert ym.artist_similarity(["Ariana Grande"], ["ArianaGrandeVevo"]) >= 0.9
    assert ym.artist_similarity(["Daft Punk"], ["Some Random Uploader"]) < 0.5
    assert ym.artist_similarity([], ["x"]) == 0.0
    assert ym.artist_similarity(["x"], []) == 0.0


def test_duration_score_table() -> None:
    assert ym.duration_score(None) == ym.UNKNOWN_DURATION_SCORE
    assert ym.duration_score(0) == 1.0
    assert ym.duration_score(2) == 1.0
    assert ym.duration_score(4.9) == 0.8
    assert ym.duration_score(12) == 0.5
    assert ym.duration_score(20) == 0.2
    assert ym.duration_score(MAX_DURATION_DELTA + 0.1) == 0.0


def test_variant_penalties() -> None:
    assert ym.variant_penalties(TITLE, f"{TITLE} (Live at Coachella)") == ["live"]
    assert ym.variant_penalties(TITLE, f"{TITLE} (slowed + reverb)") == ["slowed", "reverb"]
    assert ym.variant_penalties(TITLE, f"{TITLE} 8D Audio") == ["8d"]
    assert ym.variant_penalties(TITLE, f"{TITLE} (sped up)") == ["sped up"]
    assert ym.variant_penalties(TITLE, f"{TITLE} - Alive") == []  # word boundaries
    assert ym.variant_penalties("Song - Radio Edit", "Song (Radio Edit)") == []  # in both
    assert ym.variant_penalties("Song - Remix", "Song (Club Remix)") == []
    assert ym.variant_penalties("Long Live", "Long Live (Official Video)") == []


# ----------------------------------------------------------------------------------------------
# scoring outcomes
# ----------------------------------------------------------------------------------------------


def test_perfect_topic_match_scores_one() -> None:
    scored = score(cand())
    assert scored.score == 1.0
    assert scored.title_similarity == 1.0
    assert scored.artist_similarity == 1.0
    assert scored.penalties == []
    assert scored.accepted


def test_exact_topic_match_wins_over_live_version() -> None:
    live = cand("live", f"{ARTIST} - {TITLE} (Live 2007)", channel="Daft Punk", duration=340)
    topic = cand("topic")
    match = match_with([live, topic])
    assert match is not None
    assert match.video_id == "topic"
    assert match.url == "https://www.youtube.com/watch?v=topic"
    assert match.channel == f"{ARTIST} - Topic"
    assert match.duration == 275.0
    assert match.query == f"{ARTIST} - {TITLE}"
    assert score(live).score < ACCEPT_SCORE


def test_with_credit_does_not_drag_the_title_below_the_floor() -> None:
    # Live case (Today's Top Hits, 2026-09-13): the catalogue entry is 'Rein Me In' by Sam
    # Fender alone; a long "(with ...)" credit on the Spotify side must not cost the match.
    the_ref = ref(
        title="Rein Me In (with Olivia Dean and Two More Names)",
        artist="Sam Fender, Olivia Dean",
        duration=339.0,
    )
    catalogue = cand(
        "0Go3jBZs-cc", "Rein Me In", channel="Sam Fender", duration=340, source="music"
    )
    scored = score_candidate(catalogue, the_ref.title or "", ["Sam Fender", "Olivia Dean"], 339.0)
    assert scored.title_similarity == 1.0
    assert scored.score == 1.0
    match = match_with([catalogue], the_ref)
    assert match is not None and match.video_id == "0Go3jBZs-cc"


def test_remix_rejected_unless_spotify_title_says_remix() -> None:
    remix = cand("remix", f"{TITLE} (Remix)")
    assert not score(remix).accepted
    assert match_with([remix]) is None
    # ...but a Spotify track that *is* the remix matches it fine
    assert match_with([remix], ref(title=f"{TITLE} - Remix")) is not None
    # a non-remix candidate does not match a remix track well
    assert score(cand("orig", TITLE), title=f"{TITLE} - Some Remix").title_similarity < 1.0


@pytest.mark.parametrize(
    "word", ["cover", "karaoke", "instrumental", "nightcore", "reaction", "tutorial", "mashup"]
)
def test_other_variants_rejected(word: str) -> None:
    assert match_with([cand("x", f"{TITLE} ({word})")]) is None


def test_duration_mismatch_rejected() -> None:
    assert match_with([cand("long", duration=DURATION + 40)]) is None
    assert match_with([cand("short", duration=DURATION - 30)]) is None
    close = match_with([cand("close", duration=DURATION + 20)])
    assert close is not None and close.video_id == "close"
    assert score(cand(duration=DURATION + 20)).duration_delta == pytest.approx(20)


def test_unknown_durations_are_neutral() -> None:
    scored = score(cand(duration=None))
    assert scored.duration_delta is None
    assert scored.accepted
    assert match_with([cand(duration=None)], ref(duration=None)) is not None


def test_multi_artist_matches_single_channel() -> None:
    the_ref = ref(title="BbY WOW", artist="KAROL G, Judeline, rusowsky", duration=225.8)
    for channel in ("KAROL G", "Judeline - Topic", "rusowsky"):
        scored = score_candidate(
            cand("v", "BbY WOW", channel=channel, duration=226),
            "BbY WOW",
            ["KAROL G", "Judeline", "rusowsky"],
            225.8,
        )
        assert scored.artist_similarity == 1.0, channel
    match = match_with([cand("v", "BbY WOW", channel="KAROL G", duration=226)], the_ref)
    assert match is not None


def test_artist_with_comma_in_name() -> None:
    the_ref = ref(title="EARFQUAKE", artist="Tyler, The Creator", duration=190.0)
    good = cand("v", "EARFQUAKE", channel="Tyler, The Creator - Topic", duration=190)
    match = match_with([good], the_ref)
    assert match is not None and match.score == 1.0


def test_accents_and_case_are_ignored() -> None:
    the_ref = ref(title="Déjà Vu", artist="Beyoncé", duration=240.0)
    match = match_with([cand("v", "DEJA VU", channel="beyonce", duration=241)], the_ref)
    assert match is not None and match.score == 1.0


def test_wrong_song_same_artist_same_length_rejected() -> None:
    other = cand("other", "Giorgio by Moroder", duration=DURATION)
    scored = score(other)
    assert not scored.accepted
    assert scored.title_similarity < MIN_TITLE_SIMILARITY
    assert match_with([other]) is None


def test_artist_named_in_title_on_a_random_channel_is_acceptable() -> None:
    lyrics = cand("lyr", f"{ARTIST} - {TITLE} (Lyrics)", channel="Vibe Music", duration=275)
    scored = score(lyrics)
    assert scored.artist_similarity == pytest.approx(0.8)
    assert scored.accepted
    # ...but a Topic upload of the same song still ranks above it
    match = match_with([lyrics, cand("topic")])
    assert match is not None and match.video_id == "topic"
    # a YouTube Music catalogue entry credits its artists itself: a mention in the title only
    # means somebody else's upload (a fan mix, a tribute) and is not accepted
    catalogue = cand(
        "cat", f"{ARTIST} - {TITLE} (Afro Mix)", channel="ALUNA RAY", duration=275, source="music"
    )
    assert score(catalogue).artist_similarity < ym.MIN_ARTIST_SIMILARITY
    assert not score(catalogue).accepted


def test_same_title_by_another_artist_is_rejected() -> None:
    tribute = cand("trib", TITLE, channel="Some Tribute Band", duration=275)
    scored = score(tribute)
    assert scored.title_similarity == 1.0
    assert scored.artist_similarity < ym.MIN_ARTIST_SIMILARITY
    assert not scored.accepted
    assert match_with([tribute]) is None
    lookalike = cand("look", "Smells Like Teen Spirit", channel="Nito-Onna", duration=302)
    the_ref = ref(title="Smells Like Teen Spirit", artist="Nirvana", duration=301.9)
    assert match_with([lookalike], the_ref) is None


def test_music_catalogue_entries_get_the_auto_upload_bonus() -> None:
    plain = cand("a", channel="Daft Punk", auto_upload=False)
    catalogue = cand("b", channel="Daft Punk", auto_upload=True, source="music")
    assert score(catalogue).score >= score(plain).score
    assert (
        score(cand("c", channel="Daft Punk", duration=290)).score
        < score(cand("d", channel="Daft Punk - Topic", duration=290)).score
    )


def test_rank_is_deterministic_and_keeps_search_order_on_ties() -> None:
    a, b = cand("a"), cand("b")
    ranked = ym.rank_candidates([a, b], TITLE, [ARTIST], DURATION)
    assert [s.candidate.video_id for s in ranked] == ["a", "b"]
    assert ranked == ym.rank_candidates([a, b], TITLE, [ARTIST], DURATION)
    ranked = ym.rank_candidates(
        [cand("worse", duration=300), cand("best")], TITLE, [ARTIST], DURATION
    )
    assert ranked[0].candidate.video_id == "best"


def test_threshold_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    def fixed(value: float, title_sim: float = 1.0, delta: float | None = 0.0) -> Any:
        def fake(candidate: Candidate, *args: Any) -> Scored:
            return Scored(candidate, value, title_sim, 1.0, delta, [])

        return fake

    monkeypatch.setattr(ym, "score_candidate", fixed(ACCEPT_SCORE))
    assert match_with([cand()]) is not None
    monkeypatch.setattr(ym, "score_candidate", fixed(ACCEPT_SCORE - 0.001))
    assert match_with([cand()]) is None
    monkeypatch.setattr(ym, "score_candidate", fixed(0.95, title_sim=MIN_TITLE_SIMILARITY - 0.01))
    assert match_with([cand()]) is None
    monkeypatch.setattr(ym, "score_candidate", fixed(0.95, delta=MAX_DURATION_DELTA))
    assert match_with([cand()]) is not None
    monkeypatch.setattr(ym, "score_candidate", fixed(0.95, delta=MAX_DURATION_DELTA + 0.01))
    assert match_with([cand()]) is None


# ----------------------------------------------------------------------------------------------
# regressions: variant recordings, look-alike artists, absurd lengths, longer titles
# ----------------------------------------------------------------------------------------------

STUDIO = "Song"


def studio(
    title: str = STUDIO, channel: str = "Artist - Topic", duration: float = 200.0
) -> Candidate:
    return cand("studio", title, channel=channel, duration=duration, source="music")


def scored_for(title: str, candidate: Candidate, artists: list[str] | None = None) -> Scored:
    return score_candidate(candidate, title, artists or ["Artist"], 200.0)


@pytest.mark.parametrize(
    "spotify_title",
    [
        "Song - Acoustic Version",
        "Song (Live Version)",
        "Song (Instrumental Version)",
        "Song - Live at Wembley 1986",
        "Song - Club Mix",
        "Song (DJ Y Remix)",
        "Song - Demo",
        "Song - 1995 Demo",
        "Song - Piano Version",
        "Song - Unplugged",
        "Song - Stripped",
        "Song - A Cappella",
        "Song - Orchestral Version",
    ],
)
def test_studio_recording_never_matches_a_variant_track(spotify_title: str) -> None:
    scored = scored_for(spotify_title, studio())
    assert not scored.accepted, scored
    assert any(p.startswith("missing ") for p in scored.penalties)
    assert match_with([studio()], ref(title=spotify_title, artist="Artist", duration=200.0)) is None


def test_variant_track_matches_its_own_recording() -> None:
    cases = [
        ("Song - Acoustic Version", "Song (Acoustic Version)"),
        ("Song (Live Version)", "Song (Live)"),
        ("Song - Live at Wembley 1986", "Song (Live at Wembley 1986)"),
        ("Song - Club Mix", "Song (Club Mix)"),
        ("Song (DJ Y Remix)", "Song (DJ Y Remix)"),
        ("Song - Demo", "Song (Demo)"),
        # live uploads spell the concert out; "Unplugged" counts as live
        ("Song - Live", "Song (Live On MTV Unplugged, 1993)"),
        ("Song - Live In Paris", "Song (Live In Paris, France, 27 February 1979)"),
    ]
    for spotify_title, candidate_title in cases:
        scored = scored_for(spotify_title, studio(candidate_title))
        assert scored.accepted, (spotify_title, scored)
        assert scored.penalties == []
    # when both are offered, the live track picks the live recording whatever the order
    the_ref = ref(title="Song - Live", artist="Artist", duration=200.0)
    match = match_with([studio(), cand("live", "Song (Live)", "Artist - Topic", 201)], the_ref)
    assert match is not None and match.video_id == "live"


@pytest.mark.parametrize(
    "spotify_title",
    [
        "Song - Remastered 2011",
        "Song (2015 Remaster)",
        "Song (Album Version)",
        "Song (Single Version)",
        "Song (Radio Edit)",
        "Song - Radio Edit",
        "Song - Edit",
        "Song - Extended Version",
        "Song - 2009 Stereo Mix",
        "Song - Original Mix",
        "Song - Mono",
        'Song - From "Some Film"',
        "Song - Explicit Ver.",
        "Song (Deluxe Edition)",
    ],
)
def test_edition_markers_still_match_the_plain_recording(spotify_title: str) -> None:
    scored = scored_for(spotify_title, studio())
    assert scored.accepted, scored
    assert scored.title_similarity == 1.0
    assert scored.penalties == [] and scored.extra_words == []


def test_edition_tail_after_a_bracket_is_stripped() -> None:
    # the last " - " counts, so a bracket in the song name does not hide the edition tail
    assert (
        ym.title_similarity(
            "I Want You (She's So Heavy) - Remastered 2009",
            "I Want You (She's So Heavy)",
            ["The Beatles"],
        )
        == 1.0
    )
    # ...and the candidate side drops one too
    assert ym.title_similarity("Hotel California", "Hotel California - 2013 Remaster", ["E"]) == 1.0


def test_variant_penalties_are_symmetric() -> None:
    assert ym.variant_penalties("Song - Live", "Song") == ["missing live"]
    assert ym.variant_penalties("Song (DJ Y Remix)", "Song") == ["missing remix"]
    assert ym.variant_penalties("Song - Club Mix", "Song") == ["missing mix"]
    assert ym.variant_penalties("Song - Live", "Song (Acoustic)") == ["acoustic", "missing live"]
    assert ym.variant_penalties("Song - Live", "Song (Live at MTV Unplugged)") == []
    assert ym.variant_penalties("Song - Remix", "Song (Club Mix)") == []
    # "edit" / "extended" on the Spotify side alone: the plain single may still be it
    assert ym.variant_penalties("Song - Edit", "Song") == []
    assert ym.variant_penalties("Song - Extended Version", "Song") == []
    # edition phrases are not variants on either side
    assert ym.variant_penalties("Song", "Song (Radio Edit)") == []
    assert ym.variant_penalties("Song - 2009 Stereo Mix", "Song") == []
    # a variant word that is part of the song name is in both titles
    assert ym.variant_penalties("Live Forever", "Live Forever") == []
    assert ym.variant_penalties("Golden Hour", "Golden Hour") == []
    # a credit or the band's name is not a variant ("Live" the band, "feat. Cover Drive")
    assert ym.variant_penalties("Lightning Crashes", "Live - Lightning Crashes", ["Live"]) == []
    assert ym.variant_penalties("Song (feat. Cover Drive)", "Song (feat. Cover Drive)") == []


def test_a_variant_word_in_the_song_name_does_not_hide_a_live_recording() -> None:
    """Occurrences are counted: the "live" of "Live Forever" does not excuse a second one."""
    assert ym.variant_penalties("Live Forever", "Live Forever (Live at Knebworth)") == ["live"]
    assert ym.variant_penalties("Live Forever - Live", "Live Forever") == ["missing live"]
    assert ym.variant_penalties("Live Forever - Live", "Live Forever (Live at Knebworth)") == []
    assert ym.variant_penalties("Live and Let Die", "Live and Let Die (Live)") == ["live"]
    assert ym.variant_penalties("Mix Tape", "Mix Tape (Club Mix)") == ["mix"]
    assert ym.variant_penalties("Demo Song", "Demo Song (Demo)") == ["demo"]

    knebworth = cand("kneb", "Live Forever (Live at Knebworth)", "Oasis - Topic", 280.0)
    scored = score(knebworth, "Live Forever", ["Oasis"], 276.0)
    assert scored.penalties == ["live"] and not scored.accepted
    assert match_with([knebworth], ref(title="Live Forever", artist="Oasis", duration=276)) is None
    studio_take = cand("studio", "Live Forever", "Oasis - Topic", 276.0)
    assert not score(studio_take, "Live Forever - Live", ["Oasis"], 276.0).accepted
    assert score(studio_take, "Live Forever", ["Oasis"], 276.0).accepted


@pytest.mark.parametrize(
    ("spotify_title", "candidate_title"),
    [
        ("Roar", "Roar (Spanish Version)"),
        ("Roar", "Roar - Spanish Version"),
        ("Roar - Spanish Version", "Roar"),
        ("Sweater Weather", "Sweater Weather (Alternate Version)"),
        ("Love Story", "Love Story (Taylor's Version)"),
        ("Love Story (Taylor's Version)", "Love Story"),
        ("Song", "Song (Japanese Ver.)"),
    ],
)
def test_another_version_is_another_recording(spotify_title: str, candidate_title: str) -> None:
    """A bare "Version" is no edition label: language, alternate and re-recorded versions
    stay in the title and count as extra words."""
    scored = scored_for(spotify_title, studio(candidate_title))
    assert not scored.accepted, scored
    assert scored.extra_words, scored


def test_qualified_version_labels_still_match() -> None:
    for label in ("Album Version", "Single Version", "2011 Version", "Clean Version"):
        scored = scored_for("Song", studio(f"Song ({label})"))
        assert scored.accepted and scored.extra_words == [], (label, scored)
    # a re-recording finds itself
    taylors = "Love Story (Taylor's Version)"
    scored = scored_for(taylors, studio(taylors))
    assert scored.accepted and scored.title_similarity == 1.0 and scored.extra_words == []


def test_clean_edition_matches_the_plain_title() -> None:
    clean = cand("clean", "Roar (Clean)", "Katy Perry - Topic", 223.0, source="music")
    scored = score(clean, "Roar", ["Katy Perry"], 223.0)
    assert scored.accepted and scored.extra_words == [] and scored.penalties == []
    assert scored_for("Song - Clean", studio()).accepted


@pytest.mark.parametrize(
    ("suffix", "word"),
    [
        ("(Demo)", "demo"),
        ("(Piano Version)", "piano"),
        ("(Unplugged)", "unplugged"),
        ("(Stripped)", "stripped"),
        ("(A Cappella)", "a cappella"),
        ("(Acapella)", "a cappella"),
        ("(Orchestral Version)", "orchestral"),
        ("(Lo-Fi)", "lofi"),
        ("lofi beats", "lofi"),
        ("(1 Hour)", "hour"),
        ("10 Hours", "hour"),
        ("(Loop)", "loop"),
    ],
)
def test_new_variant_words(suffix: str, word: str) -> None:
    assert word in ym.variant_penalties(TITLE, f"{TITLE} {suffix}")
    assert match_with([cand("x", f"{TITLE} {suffix}")]) is None


def test_endless_uploads_rejected_when_the_spotify_duration_is_unknown() -> None:
    loop = cand("loop", TITLE, duration=36000)  # a 10-hour upload titled like the song
    assert score(loop, duration=None).score == 1.0
    assert not score(loop, duration=None).accepted
    assert match_with([loop], ref(duration=None)) is None
    long_but_fine = cand("ok", TITLE, duration=ym.MAX_LENGTH_WITHOUT_DURATION)
    assert score(long_but_fine, duration=None).accepted
    # a known Spotify duration already rules such uploads out by the delta
    assert not score(loop).accepted


@pytest.mark.parametrize(
    ("spotify_artist", "channel"),
    [
        ("Adele", "Adeleine"),
        ("Nas", "Nasty C"),
        ("Nas", "Lil Nas X"),
        ("Kanye West", "Ye"),
        ("Queen", "Queen Latifah"),
        ("Future", "Future Islands"),
    ],
)
def test_a_name_inside_another_name_is_another_artist(spotify_artist: str, channel: str) -> None:
    assert ym.artist_similarity([spotify_artist], [channel]) < ym.MIN_ARTIST_SIMILARITY
    the_ref = ref(title="Song", artist=spotify_artist, duration=200.0)
    assert match_with([studio(channel=f"{channel} - Topic")], the_ref) is None
    assert match_with([studio(channel=channel)], the_ref) is None


@pytest.mark.parametrize(
    ("spotify_artist", "channel"),
    [
        ("Ariana Grande", "ArianaGrandeVevo"),
        ("Sam Fender", "Sam Fender Official"),
        ("Daft Punk", "Official Daft Punk"),
        ("The Weeknd", "Weeknd"),
        ("Bruce Springsteen & The E Street Band", "Bruce Springsteen"),
        ("Tom Petty", "Tom Petty and the Heartbreakers"),
        ("Yusuf / Cat Stevens", "Cat Stevens"),
    ],
)
def test_decorated_channel_names_still_match(spotify_artist: str, channel: str) -> None:
    assert ym.artist_similarity([spotify_artist], [channel]) >= 0.9
    the_ref = ref(title="Song", artist=spotify_artist, duration=200.0)
    assert match_with([studio(channel=channel)], the_ref) is not None


def test_longer_title_by_the_same_artist_is_another_song() -> None:
    for other in ("Stay With Me", "Stay, Pt. 2", "Stay (Part II)", "Stay 2"):
        scored = scored_for("Stay", studio(other))
        assert scored.extra_words, other
        assert not scored.accepted, (other, scored)
        assert match_with([studio(other)], ref(title="Stay", artist="Artist", duration=200)) is None
    # ...and the other way round ("Song 2" is its own song, even at 0.8 letter similarity)
    for spotify_title, other in (
        ("Stay With Me", "Stay"),
        ("Stay, Pt. 2", "Stay"),
        ("Song 2", "Song"),
    ):
        assert not scored_for(spotify_title, studio(other)).accepted, spotify_title
    assert ym.title_extra_words("Stay", "Stay With Me", ["Artist"]) == ["me", "with"]
    assert ym.title_extra_words("Stay, Pt. 2", "Stay", ["Artist"]) == ["2", "part"]
    # "Pt." and "Part", "II" and "2" are one spelling; a different part number is not
    assert ym.title_extra_words("Stay, Pt. 2", "Stay (Part II)", ["Artist"]) == []
    assert ym.title_extra_words("Stay, Pt. 1", "Stay (Part I)", ["Artist"]) == []
    assert not scored_for("Stay, Pt. 2", studio("Stay, Pt. 3")).accepted
    # the artist's own name in a title is not an extra word
    assert ym.title_extra_words("Stay", "Stay Artist", ["Artist"]) == []


def test_extra_words_that_do_not_name_another_song_are_fine() -> None:
    accepted = [
        ("Stay", "Stay (Official Video)", "Artist"),
        ("Stay", "Artist - Stay (Lyrics)", "Some Uploader"),
        ("Stay", "Stay (2015 Remaster)", "Artist - Topic"),
        ("Stay", "Stay (feat. Someone)", "Artist - Topic"),
        ("Stay", "Stay (with Someone)", "Artist - Topic"),
        ("Stay (feat. Someone)", "Stay", "Artist - Topic"),
        ("Stay - Live", "Stay (Live at Wembley 1986)", "Artist - Topic"),
        ("Stay (Part II)", "Stay, Pt. II", "Artist - Topic"),
        ("Stay, Pt. 2", "Stay (Part 2)", "Artist - Topic"),
        ("Don't Stop Me Now", "Dont Stop Me Now", "Artist - Topic"),
        ("Seven (feat. Someone)", "Seven (feat. Someone) (Explicit Ver.)", "Artist - Topic"),
    ]
    for spotify_title, candidate_title, channel in accepted:
        candidate = cand("v", candidate_title, channel=channel, duration=200)
        scored = scored_for(spotify_title, candidate)
        assert scored.accepted, (spotify_title, candidate_title, scored)


# ----------------------------------------------------------------------------------------------
# queries and find_match plumbing
# ----------------------------------------------------------------------------------------------


def test_build_queries() -> None:
    assert build_queries(TITLE, ARTIST) == [
        f"{ARTIST} - {TITLE}",
        f"{TITLE} {ARTIST}",
        TITLE,
    ]
    assert build_queries(TITLE, None) == [TITLE]
    assert build_queries(TITLE, "  ") == [TITLE]
    assert build_queries("  Song  ", "Same") == ["Same - Song", "Song Same", "Song"]


def test_query_fallback_order() -> None:
    seen: list[str] = []
    good = cand("good")

    def search(query: str) -> list[Candidate]:
        seen.append(query)
        if len(seen) < 3:
            return [cand("bad", "Something Else Entirely", duration=100)] if len(seen) == 1 else []
        return [good]

    match = find_match(ref(), None, threading.Event(), search=search)
    assert match is not None and match.video_id == "good"
    assert match.query == TITLE
    assert seen == [f"{ARTIST} - {TITLE}", f"{TITLE} {ARTIST}", TITLE]


def test_first_query_with_a_match_stops_the_search() -> None:
    seen: list[str] = []

    def search(query: str) -> list[Candidate]:
        seen.append(query)
        return [cand()]

    assert find_match(ref(), None, threading.Event(), search=search) is not None
    assert seen == [f"{ARTIST} - {TITLE}"]


def test_no_match_after_all_queries() -> None:
    seen: list[str] = []

    def search(query: str) -> list[Candidate]:
        seen.append(query)
        return [cand("x", "Unrelated", channel="Nobody", duration=10)]

    assert find_match(ref(), None, threading.Event(), search=search) is None
    assert len(seen) == 3


def test_find_match_without_title_returns_none() -> None:
    called = False

    def search(query: str) -> list[Candidate]:
        nonlocal called
        called = True
        return []

    assert find_match(ref(title=None), None, threading.Event(), search=search) is None
    assert find_match(ref(title="   "), None, threading.Event(), search=search) is None
    assert called is False


def test_find_match_checks_cancel_before_searching() -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        find_match(ref(), None, cancel, search=lambda q: [cand()])


def test_search_errors_become_friendly() -> None:
    def boom(query: str) -> list[Candidate]:
        raise yt_dlp.utils.DownloadError("ERROR: Unable to download API page: getaddrinfo failed")

    with pytest.raises(ProviderError) as exc_info:
        find_match(ref(), None, threading.Event(), search=boom)
    assert str(exc_info.value).startswith("Couldn't reach YouTube")

    def odd(query: str) -> list[Candidate]:
        raise RuntimeError("kaboom")

    with pytest.raises(ProviderError, match=r"Could not search YouTube Music \(kaboom\)"):
        find_match(ref(), None, threading.Event(), search=odd)

    def already(query: str) -> list[Candidate]:
        raise ProviderError("custom")

    with pytest.raises(ProviderError, match="^custom$"):
        find_match(ref(), None, threading.Event(), search=already)


def test_find_match_stores_nothing_and_logs_top_candidates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("DEBUG", logger="ultimate_playlist.providers.ytmusic_match")
    match_with([cand("a"), cand("b", duration=300), cand("c", "Other", duration=10), cand("d")])
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("match ")]
    assert len(lines) == 3  # top 3 only


# ----------------------------------------------------------------------------------------------
# yt-dlp searches (fake YoutubeDL)
# ----------------------------------------------------------------------------------------------


def _run(text: str, browse: str | None = None) -> dict[str, Any]:
    run: dict[str, Any] = {"text": text}
    if browse:
        run["navigationEndpoint"] = {"browseEndpoint": {"browseId": browse}}
    return run


def music_item(
    video_id: str | None,
    title: str,
    artists: list[str],
    album: str | None,
    duration_text: str | None,
    linked: int = 99,  # how many artist runs carry a browse link (YouTube varies this)
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for i, artist in enumerate(artists):
        if i:
            runs.append(_run(" & " if i == len(artists) - 1 else ", "))
        runs.append(_run(artist, "UC" + artist.replace(" ", "") if i < linked else None))
    if album:
        runs += [_run(" • "), _run(album, "MPREb_" + album.replace(" ", ""))]
    if duration_text:
        runs += [_run(" • "), _run(duration_text)]
    renderer: dict[str, Any] = {
        "flexColumns": [
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [_run(title)]}}},
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": runs}}},
            {"musicResponsiveListItemFlexColumnRenderer": {"text": {"runs": [_run("54M plays")]}}},
        ]
    }
    if video_id:
        renderer["playlistItemData"] = {"videoId": video_id}
    return {"musicResponsiveListItemRenderer": renderer}


def music_response(items: list[dict[str, Any]]) -> dict[str, Any]:
    shelf = {"musicShelfRenderer": {"title": {"runs": [{"text": "Songs"}]}, "contents": items}}
    section_list = {"sectionListRenderer": {"contents": [{"itemSectionRenderer": {}}, shelf]}}
    return {
        "contents": {
            "tabbedSearchResultsRenderer": {"tabs": [{"tabRenderer": {"content": section_list}}]}
        }
    }


def test_parse_music_search() -> None:
    data = music_response(
        [
            music_item("zKSsP2084nU", TITLE, ["Daft Punk"], "Random Access Memories", "4:36"),
            music_item(
                "4D7u5KF7SP8",
                "Get Lucky",
                ["Daft Punk", "Pharrell Williams", "Nile Rodgers"],
                "Random Access Memories",
                "6:10",
            ),
            music_item(None, "No video id", ["X"], None, "1:00"),
            music_item("zKSsP2084nU", "duplicate", ["Daft Punk"], None, "4:36"),
            music_item("long", "An hour", ["Y"], None, "1:02:03"),
            # only the first artist linked (the usual live shape), and none linked at all
            music_item(
                "ljAH6jNaApU",
                "BbY WOW",
                ["KAROL G", "Judeline", "rusowsky"],
                "NO ME ARREPIENTO",
                "3:46",
                linked=1,
            ),
            music_item("nolinks", "Plain", ["Solo Artist"], "Plain Album", "2:00", linked=0),
        ]
    )
    candidates = ym.parse_music_search(data)
    assert [c.video_id for c in candidates] == [
        "zKSsP2084nU",
        "4D7u5KF7SP8",
        "long",
        "ljAH6jNaApU",
        "nolinks",
    ]
    assert candidates[3].artists == ["KAROL G", "Judeline", "rusowsky"]
    assert candidates[3].album == "NO ME ARREPIENTO" and candidates[3].duration == 226.0
    assert candidates[4].artists == ["Solo Artist"]
    assert candidates[4].album == "Plain Album" and candidates[4].duration == 120.0
    # one unlinked run holding every name
    joined = music_response(
        [music_item("one", "BbY WOW", ["KAROL G, Judeline, & rusowsky"], "Album", "3:46", linked=0)]
    )
    (candidate,) = ym.parse_music_search(joined)
    assert candidate.artists == ["KAROL G", "Judeline", "rusowsky"]
    assert candidate.channel == "KAROL G, Judeline, rusowsky"
    first = candidates[0]
    assert first.title == TITLE
    assert first.artists == ["Daft Punk"]
    assert first.channel == "Daft Punk"
    assert first.album == "Random Access Memories"
    assert first.duration == 276.0
    assert first.auto_upload is True and first.source == "music"
    assert candidates[1].artists == ["Daft Punk", "Pharrell Williams", "Nile Rodgers"]
    assert candidates[1].channel == "Daft Punk, Pharrell Williams, Nile Rodgers"
    assert candidates[1].duration == 370.0
    assert candidates[2].duration == 3723.0
    assert ym.parse_music_search({}) == []
    assert ym.parse_music_search(None) == []
    assert (
        ym.parse_music_search({"contents": {"tabbedSearchResultsRenderer": {"tabs": "odd"}}}) == []
    )


class FakeIE:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.initialized = False

    def initialize(self) -> None:
        self.initialized = True

    def _extract_response(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(FakeYoutubeDL.response, Exception):
            raise FakeYoutubeDL.response
        return FakeYoutubeDL.response


class FakeYoutubeDL:
    response: Any = None
    instances: list[FakeYoutubeDL] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.ie = FakeIE()
        self.ie_name: str | None = None
        self.extracted: list[tuple[str, bool]] = []
        FakeYoutubeDL.instances.append(self)

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def get_info_extractor(self, name: str) -> FakeIE:
        self.ie_name = name
        return self.ie

    def extract_info(self, url: str, download: bool = False) -> Any:
        self.extracted.append((url, download))
        if isinstance(FakeYoutubeDL.response, Exception):
            raise FakeYoutubeDL.response
        return FakeYoutubeDL.response


@pytest.fixture
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> type[FakeYoutubeDL]:
    FakeYoutubeDL.response = None
    FakeYoutubeDL.instances = []
    monkeypatch.setattr(youtube, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


def test_search_music_uses_the_songs_shelf(fake_ydl: type[FakeYoutubeDL], tmp_path: Any) -> None:
    fake_ydl.response = music_response(
        [music_item("zKSsP2084nU", TITLE, ["Daft Punk"], "RAM", "4:36")]
    )
    settings = Settings(library_dir=tmp_path / "lib", js_runtimes=["node"])
    candidates = ym.search_music("Daft Punk - Give Life Back to Music", settings)
    assert [c.video_id for c in candidates] == ["zKSsP2084nU"]
    (ydl,) = fake_ydl.instances
    assert ydl.ie_name == "YoutubeMusicSearchURL"
    assert ydl.ie.initialized
    (call,) = ydl.ie.calls
    assert call["query"] == {
        "query": "Daft Punk - Give Life Back to Music",
        "params": ym.MUSIC_SONGS_PARAMS,
    }
    assert call["ep"] == "search" and call["default_client"] == "web_music"
    assert ydl.opts["extract_flat"] is True
    assert ydl.opts["skip_download"] is True
    assert ydl.opts["playlistend"] == ym.SEARCH_LIMIT
    assert list(ydl.opts["js_runtimes"]) == ["node"]
    assert ydl.opts["quiet"] is True


def test_search_youtube_maps_flat_entries(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.response = {
        "entries": [
            {
                "id": "IluRBvnYMoY",
                "title": f"{ARTIST} - {TITLE} (Official Audio)",
                "duration": 284,
                "channel": "Daft Punk",
            },
            {"id": "topic", "title": TITLE, "duration": 275, "uploader": "Daft Punk - Topic"},
            {
                "id": "prov",
                "title": TITLE,
                "duration": 275,
                "channel": "Daft Punk",
                "description": "Provided to YouTube by Columbia",
            },
            {"id": "live", "title": "Live now", "live_status": "is_live", "channel": "x"},
            {"id": "", "title": "no id"},
            {"title": "no id at all"},
            "garbage",
        ]
    }
    candidates = ym.search_youtube("q")
    assert [c.video_id for c in candidates] == ["IluRBvnYMoY", "topic", "prov"]
    assert candidates[0].channel == "Daft Punk" and candidates[0].duration == 284.0
    assert candidates[0].auto_upload is False and candidates[0].source == "youtube"
    assert candidates[1].channel == "Daft Punk - Topic" and candidates[1].auto_upload is True
    assert candidates[2].auto_upload is True
    (ydl,) = fake_ydl.instances
    assert ydl.extracted == [(f"ytsearch{ym.SEARCH_LIMIT}:q", False)]
    fake_ydl.response = None
    assert ym.search_youtube("q") == []


def test_default_search_falls_back_to_plain_youtube(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def music_ok(query: str, settings: Any = None) -> list[Candidate]:
        calls.append("music")
        return [cand("m", source="music")]

    def music_empty(query: str, settings: Any = None) -> list[Candidate]:
        calls.append("music")
        return []

    def music_boom(query: str, settings: Any = None) -> list[Candidate]:
        calls.append("music")
        raise RuntimeError("plumbing changed")

    def plain(query: str, settings: Any = None) -> list[Candidate]:
        calls.append("youtube")
        return [cand("y")]

    monkeypatch.setattr(ym, "search_youtube", plain)
    monkeypatch.setattr(ym, "search_music", music_ok)
    assert [c.video_id for c in ym.default_search("q")] == ["m"]
    assert calls == ["music"]
    calls.clear()
    monkeypatch.setattr(ym, "search_music", music_empty)
    assert [c.video_id for c in ym.default_search("q")] == ["y"]
    assert calls == ["music", "youtube"]
    calls.clear()
    monkeypatch.setattr(ym, "search_music", music_boom)
    assert [c.video_id for c in ym.default_search("q")] == ["y"]
    assert calls == ["music", "youtube"]


def test_find_match_default_search_goes_through_yt_dlp(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.response = music_response(
        [music_item("zKSsP2084nU", TITLE, ["Daft Punk"], "RAM", "4:36")]
    )
    match = find_match(ref(), None, threading.Event())
    assert match is not None and match.video_id == "zKSsP2084nU"
    assert match.channel == "Daft Punk"
