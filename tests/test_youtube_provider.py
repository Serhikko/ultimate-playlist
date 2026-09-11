"""Offline unit tests for the YouTube provider (yt-dlp is replaced by a fake)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import mutagen
import pytest
import yt_dlp.utils
from fake_provider import FAKE_PNG, write_tiny_audio
from mutagen.id3 import APIC, ID3, TXXX

from ultimate_playlist.config import Settings
from ultimate_playlist.library import read_tags
from ultimate_playlist.models import JobStatus, ProgressEvent, TrackRef
from ultimate_playlist.providers import youtube
from ultimate_playlist.providers.base import DownloadCancelled, ProviderError
from ultimate_playlist.providers.youtube import (
    YouTubeProvider,
    _ydl_opts,
    clean_title,
    friendly_error,
    pick_artist_title,
    safe_filename,
    split_artist_title,
    stored_track_id,
    unique_path,
    write_tags,
)

WAIT = 15.0
OTHER_FORMATS = ["m4a", "opus", "flac"]

# MPEG-1 Layer III, 128 kbps, 44.1 kHz, no padding -> 417-byte frames of silence (zeros).
_MP3_FRAME = bytes([0xFF, 0xFB, 0x90, 0x64]) + bytes(417 - 4)
TINY_MP3 = _MP3_FRAME * 4


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"library_dir": tmp_path / "lib"}
    values.update(overrides)
    return Settings(**values)


# ----------------------------------------------------------------------------------------------
# matches()
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "http://youtube.com/watch?v=jNQXAC9IVRw",
        "https://m.youtube.com/watch?v=jNQXAC9IVRw",
        "https://music.youtube.com/watch?v=jNQXAC9IVRw&list=RDAMVM",
        "https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PLbpi6ZahtOH6Blw3RGYpWkSByi_T7Rygb&index=2",
        "https://www.youtube.com/watch?list=PLbpi6ZahtOH6Blw3RGYpWkSByi_T7Rygb",
        "https://www.youtube.com/playlist?list=PLbpi6ZahtOH6Blw3RGYpWkSByi_T7Rygb",
        "https://music.youtube.com/playlist?list=OLAK5uy_abc",
        "https://www.youtube.com/shorts/jNQXAC9IVRw",
        "https://www.youtube.com/live/jNQXAC9IVRw",
        "https://www.youtube.com/embed/jNQXAC9IVRw",
        "https://www.youtube.com/embed/videoseries?list=PLbpi6ZahtOH6Blw3RGYpWkSByi_T7Rygb",
        "https://www.youtube-nocookie.com/embed/jNQXAC9IVRw",
        "https://youtu.be/jNQXAC9IVRw",
        "https://youtu.be/jNQXAC9IVRw?t=5",
        "youtu.be/jNQXAC9IVRw",
        "www.youtube.com/watch?v=jNQXAC9IVRw",
        "  https://www.youtube.com/watch?v=jNQXAC9IVRw  ",
        # shapes yt-dlp resolves that the strict matcher used to refuse
        "https://music.youtube.com/browse/MPREb_4pL8gzRtl1u",  # YouTube Music album page
        "https://music.youtube.com/browse/VLPLbpi6ZahtOH6Blw3RGYpWkSByi_T7Rygb",
        "https://www.youtube.com/v/jNQXAC9IVRw",  # legacy embed
        "https://www.youtube.com/clip/UgkxqRiEeNgtXFhwCBzQ5bhCBuOi-TXn1gk_",
        "youtu.be/abc",  # a typo'd id: YouTube itself says "looks incomplete"
        "https://www.youtube.com/watch?v=abc",
        # watch-URL variants yt-dlp's own matcher accepts
        "https://www.youtube.com/watch/?v=jNQXAC9IVRw",
        "https://www.youtube.com/?v=jNQXAC9IVRw",
        "https://www.youtube.com/e/jNQXAC9IVRw",
        "https://www.youtube.com/shorts/jNQXAC9IVRw/",
    ],
)
def test_matches_positive(url: str) -> None:
    assert YouTubeProvider().matches(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@rickastley",
        "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw",
        "https://www.youtube.com/c/RickAstley",
        "https://www.youtube.com/user/RickAstley",
        "https://www.youtube.com/",
        "https://www.youtube.com/?list=PLbpi6ZahtOH6Blw3RGYpWkSByi_T7Rygb",  # only /watch takes list=
        "https://www.youtube.com/watch",
        "https://www.youtube.com/watch/",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com/e/",
        "https://www.youtube.com/playlist",
        "https://www.youtube.com/feed/subscriptions",
        "https://www.youtube.com/results?search_query=rick",
        "https://www.youtube.com/browse/FEmusic_home",  # a YT Music feed, not an album
        "https://www.youtube.com/v/",
        "https://www.youtube.com/clip/",
        "https://youtu.be/",
        "https://vimeo.com/123456",
        "https://open.spotify.com/track/abc",
        "https://www.google.com/search?q=youtube.com/watch?v=jNQXAC9IVRw",
        "https://notyoutube.com/watch?v=jNQXAC9IVRw",
        "https://youtube.com.evil.example/watch?v=jNQXAC9IVRw",
        "not a url",
        "",
        "   ",
    ],
)
def test_matches_negative(url: str) -> None:
    assert YouTubeProvider().matches(url) is False


# ----------------------------------------------------------------------------------------------
# split_artist_title / clean_title
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Artist - Title", ("Artist", "Title")),
        ("Artist – Title", ("Artist", "Title")),  # en dash
        ("Artist — Title", ("Artist", "Title")),  # em dash
        ("Artist | Title", ("Artist", "Title")),
        ("  Artist  -  Title  ", ("Artist", "Title")),
        ("A - B - C", ("A", "B - C")),
        ("Daft Punk - Get Lucky (Official Video)", ("Daft Punk", "Get Lucky (Official Video)")),
        ("Artist - Title ft. Someone", ("Artist", "Title ft. Someone")),
        ("Just a title", None),
        ("", None),
        ("Artist-Title", None),  # no whitespace around the dash
        ("Artist -Title", None),
        (" - Title", None),
        ("Artist - ", None),
        ("Title (feat. X) - Artist", None),  # "(" on the left side
        ("- ", None),
    ],
)
def test_split_artist_title(title: str, expected: tuple[str, str] | None) -> None:
    assert split_artist_title(title) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Song (Official Video)", "Song"),
        ("Song (official video)", "Song"),
        ("Song (OFFICIAL VIDEO)", "Song"),
        ("Song [Official Video]", "Song"),
        ("Song (Official Music Video)", "Song"),
        ("Song [Official Audio]", "Song"),
        ("Song (Official Audio)", "Song"),
        ("Song (Official Lyric Video)", "Song"),
        ("Song (Official Visualizer)", "Song"),
        ("Song (Lyrics)", "Song"),
        ("Song (Lyric Video)", "Song"),
        ("Song (Audio)", "Song"),
        ("Song (HD)", "Song"),
        ("Song (4K)", "Song"),
        ("Song (Visualizer)", "Song"),
        ("Song (Visualiser)", "Song"),
        ("Song | Official Video", "Song"),
        ("Song | Official Music Video | Some Label", "Song"),
        ("Song | Lyrics", "Song"),
        ("Song - Official Video", "Song"),
        ("Song [Official Audio] (Lyrics)", "Song"),
        ("Song (Official Video) (HD)", "Song"),
        ("Song (4K) [Visualizer]", "Song"),
        ("Song (feat. Someone)", "Song (feat. Someone)"),
        ("Song (ft. Someone) (Official Video)", "Song (ft. Someone)"),
        ("Song feat. Someone [Official Audio]", "Song feat. Someone"),
        ("Song (Live at Wembley)", "Song (Live at Wembley)"),
        ("Song (Remix)", "Song (Remix)"),
        ("Song (2019 Remaster)", "Song (2019 Remaster)"),
        ("Song", "Song"),
        ("   Song   Name  ", "Song Name"),
        ("(Official Video)", ""),
        ("", ""),
        # junk that sits before a real bracket group or a label, and more bracket variants
        (
            "Never Gonna Give You Up (Official Video) (4K Remaster)",
            "Never Gonna Give You Up (4K Remaster)",
        ),
        ("Title [Official HD Video]", "Title"),
        ("Title (Official 4K Video)", "Title"),
        ("Title (Official HD Music Video)", "Title"),
        ("Title (Official Video) | Label", "Title"),
        ("Title | Some Label Records", "Title"),
        ("Title [Official Video] (feat. X)", "Title (feat. X)"),
        ("Title [Lyrics/Lyric Video]", "Title"),
        ("Title (Official Audio) ft. Someone", "Title ft. Someone"),
        ("Title (HD Remaster)", "Title (HD Remaster)"),  # "HD" alone is junk, "HD Remaster" is not
        ("Title (Audio Book)", "Title (Audio Book)"),
        ("Ti\u200btle\u202e (Official Video)\ufeff", "Title"),  # invisible characters
        # a "| tail" is only dropped when it reads like a label / channel / format marker
        ("Song | Vevo Presents", "Song"),
        ("Song | Some Channel TV", "Song"),
        ("Song | Live at Wembley", "Song | Live at Wembley"),
        ("Artist | Title", "Artist | Title"),
        ("Song | Acoustic Version", "Song | Acoustic Version"),
    ],
)
def test_clean_title(title: str, expected: str) -> None:
    assert clean_title(title) == expected


# ----------------------------------------------------------------------------------------------
# safe_filename / unique_path
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ('AC/DC: "Back" <in> Black?', "ACDC Back in Black"),
        ("a*b|c\\d", "abcd"),
        ("Artist - Title.", "Artist - Title"),
        ("Artist - Title...   ", "Artist - Title"),
        ("  lots   of\t\nspace  ", "lots of space"),
        ("ctrl\x00\x01\x1f\x7fchars", "ctrlchars"),
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("nul", "_nul"),
        ("LPT1", "_LPT1"),
        ("Console", "Console"),
        ("CON - Song", "CON - Song"),
        ("", "untitled"),
        ("???", "untitled"),
        ("Ünïcödé – ok", "Ünïcödé – ok"),
        ("Art\u202eist - T\ufeffitle", "Artist - Title"),  # bidi override, BOM
        ("A - B\u200b", "A - B"),  # zero-width space: same file as "A - B"
        ("\u200e\u200fA\u2060B\u2064", "AB"),
    ],
)
def test_safe_filename(name: str, expected: str) -> None:
    assert safe_filename(name) == expected


def test_pick_artist_title_strips_invisible_characters() -> None:
    info = {"title": "Art\u202eist - Ti\u200btle (Official Video)", "channel": "C"}
    assert pick_artist_title(info) == ("Artist", "Title")
    info = {"title": "x", "artist": "Real\ufeff Artist", "track": "Tr\u200back"}
    assert pick_artist_title(info) == ("Real Artist", "Track")
    info = {"title": "So\u200bng", "channel": "Ch\u200ban - Topic"}
    assert pick_artist_title(info) == ("Chan", "Song")


def test_safe_filename_max_length() -> None:
    result = safe_filename("x" * 200)
    assert len(result) == 150
    padded = safe_filename("y" * 149 + "." + "z" * 20)
    assert len(padded) <= 150
    assert not padded.endswith(".")
    assert not padded.endswith(" ")
    assert len(safe_filename("a" * 300, max_len=10)) == 10


def test_unique_path(tmp_path: Path) -> None:
    target = tmp_path / "Artist - Title.mp3"
    assert unique_path(target) == target
    target.write_bytes(b"x")
    second = unique_path(target)
    assert second == tmp_path / "Artist - Title (2).mp3"
    second.write_bytes(b"x")
    assert unique_path(target) == tmp_path / "Artist - Title (3).mp3"


# ----------------------------------------------------------------------------------------------
# pick_artist_title
# ----------------------------------------------------------------------------------------------


def test_pick_artist_title_prefers_music_metadata() -> None:
    info = {
        "title": "Rick Astley - Never Gonna Give You Up (Official Video)",
        "artist": "Rick Astley",
        "track": "Never Gonna Give You Up",
        "channel": "Some Channel",
    }
    assert pick_artist_title(info) == ("Rick Astley", "Never Gonna Give You Up")


def test_pick_artist_title_ignores_partial_music_metadata() -> None:
    info = {"title": "Artist - Title (Official Video)", "artist": "Only Artist", "channel": "Chan"}
    assert pick_artist_title(info) == ("Artist", "Title")
    info = {"title": "Artist - Title", "artist": "   ", "track": "T", "channel": "Chan"}
    assert pick_artist_title(info) == ("Artist", "Title")


def test_pick_artist_title_parses_title() -> None:
    info = {"title": "Daft Punk - Get Lucky (Official Audio)", "channel": "DaftPunkVEVO"}
    assert pick_artist_title(info) == ("Daft Punk", "Get Lucky")


def test_pick_artist_title_falls_back_to_channel() -> None:
    info = {"title": "Get Lucky (Official Video)", "channel": "Daft Punk", "uploader": "x"}
    assert pick_artist_title(info) == ("Daft Punk", "Get Lucky")


@pytest.mark.parametrize(
    "title",
    [
        "Song - Lyrics",
        "Song - Official Video",
        "Song | Official Music Video",
        "Song | Lyrics",
        "Song - Visualizer",
        "Song - Official Music Video (2024)",
        "Song | Some Label Records",
    ],
)
def test_pick_artist_title_cleans_before_splitting(title: str) -> None:
    """A junk tail after the separator is not an artist-title pair: 'Song - Lyrics' used to
    become artist 'Song', title 'Lyrics'."""
    info = {"title": title, "channel": "Channel Name"}
    assert pick_artist_title(info) == ("Channel Name", "Song")


def test_pick_artist_title_still_splits_real_pairs_after_cleaning() -> None:
    assert pick_artist_title({"title": "Artist - Title - Official Video", "channel": "C"}) == (
        "Artist",
        "Title",
    )
    assert pick_artist_title({"title": "Artist | Title", "channel": "C"}) == ("Artist", "Title")
    assert pick_artist_title({"title": "Artist - Title (Lyrics) | Label", "channel": "C"}) == (
        "Artist",
        "Title",
    )
    assert pick_artist_title({"title": "A - B - C", "channel": "Chan"}) == ("A", "B - C")


def test_pick_artist_title_strips_topic_channel() -> None:
    info = {"title": "Get Lucky", "channel": "Daft Punk - Topic"}
    assert pick_artist_title(info) == ("Daft Punk", "Get Lucky")
    info = {"title": "Get Lucky", "uploader": "Daft Punk - Topic"}
    assert pick_artist_title(info) == ("Daft Punk", "Get Lucky")


def test_pick_artist_title_uses_uploader_then_unknown() -> None:
    assert pick_artist_title({"title": "Song", "uploader": "Uploader"}) == ("Uploader", "Song")
    assert pick_artist_title({"title": "Song"}) == ("Unknown Artist", "Song")
    assert pick_artist_title({}) == ("Unknown Artist", "Unknown Title")


def test_pick_artist_title_keeps_raw_title_when_cleaning_empties_it() -> None:
    assert pick_artist_title({"title": "(Official Video)", "channel": "C"}) == (
        "C",
        "(Official Video)",
    )
    assert pick_artist_title({"title": "Artist - (Official Video)"}) == (
        "Artist",
        "(Official Video)",
    )


# ----------------------------------------------------------------------------------------------
# friendly_error
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_start"),
    [
        (
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access to this video",
            "This video is private",
        ),
        ("ERROR: [youtube] abc: Video unavailable", "This video is unavailable"),
        (
            "ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader",
            "This video is unavailable",
        ),
        (
            "ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate for some users.",
            "This video is age-restricted",
        ),
        (
            "ERROR: [youtube] abc: The uploader has not made this video available in your country",
            "This video isn't available in your country",
        ),
        (
            "ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>",
            "Couldn't reach YouTube",
        ),
        ("ERROR: [youtube] abc: Failed to resolve 'www.youtube.com'", "Couldn't reach YouTube"),
        (
            "ERROR: [youtube] abc: Sign in to confirm you're not a bot.",
            "YouTube is asking for a sign-in",
        ),
        (
            "ERROR: [youtube] abc: Join this channel to get access to members-only content",
            "This video is only available to channel members",
        ),
        (
            "ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install or provide the path",
            "Converting the audio failed",
        ),
        ("ERROR: Unsupported URL: https://example.com", "This link isn't a YouTube video"),
        ("ERROR: something nobody expected", "YouTube download failed."),
        ("", "YouTube download failed."),
        # HTTP statuses during the media download are not connectivity problems
        (
            "ERROR: unable to download video data: HTTP Error 403: Forbidden",
            "YouTube refused to serve the audio",
        ),
        (
            "ERROR: [youtube] abc: Unable to download API page: HTTP Error 429: Too Many Requests",
            "YouTube is rate-limiting this computer",
        ),
        (
            "ERROR: unable to download video data: HTTP Error 503: Service Unavailable",
            "YouTube is having problems right now",
        ),
        (
            "ERROR: unable to download video data: HTTP Error 404: Not Found",
            "This video is unavailable",
        ),
        # ...while a real network failure inside the same wrapper still is one
        (
            "ERROR: unable to download video data: <urlopen error [Errno 11001] getaddrinfo failed>",
            "Couldn't reach YouTube",
        ),
        # narrowed needles: format / playlist / member-tier / typo'd id messages
        (
            "ERROR: [youtube] abc: Requested format is not available. Use --list-formats for a list of available formats",
            "YouTube didn't offer a downloadable audio stream",
        ),
        ("ERROR: [youtube] abc: No video formats found!", "YouTube didn't offer"),
        ("ERROR: [youtube:tab] PL123: The playlist does not exist.", "That playlist doesn't exist"),
        ("ERROR: [youtube:tab] PL123: The playlist is private.", "That playlist doesn't exist"),
        (
            "ERROR: [youtube] abc: This video is available to this channel's members on level: Fan",
            "This video is only available to channel members",
        ),
        ("ERROR: [youtube] abc: Only images are available for download.", "That link has no audio"),
        (
            "ERROR: [youtube] abc: Incomplete YouTube ID abc. URL https://youtu.be/abc looks truncated.",
            "That link looks incomplete",
        ),
        (
            "ERROR: [youtube] abc: Video unavailable. This content is not available in this country",
            "This video is unavailable",
        ),
        # live streams / premieres that have not finished yet
        (
            "ERROR: [youtube] abc: This live event will begin in 3 hours.",
            "This is a live stream or premiere",
        ),
        ("ERROR: [youtube] abc: Premieres in 2 hours", "This is a live stream or premiere"),
        (
            "ERROR: [youtube] abc: This livestream is not available",
            "This is a live stream or premiere",
        ),
        # HTTP codes without a dedicated message fall through to the substring rules
        (
            "ERROR: unable to download video data: HTTP Error 401: Unauthorized",
            "YouTube download failed.",
        ),
        (
            "ERROR: [youtube] abc: HTTP Error 410: Gone. Video unavailable",
            "This video is unavailable",
        ),
    ],
)
def test_friendly_error_mapping(raw: str, expected_start: str) -> None:
    message = friendly_error(yt_dlp.utils.DownloadError(raw))
    assert message.startswith(expected_start)
    assert friendly_error(raw) == message  # plain strings are accepted too


def test_friendly_error_keeps_original_text_in_parentheses() -> None:
    message = friendly_error("ERROR: [youtube] abc: Private video. Sign in please")
    assert message.endswith("([youtube] abc: Private video. Sign in please)")
    assert "ERROR:" not in message
    assert friendly_error("") == "YouTube download failed."


def test_friendly_error_trims_long_text_and_boilerplate() -> None:
    long = "x" * 500
    message = friendly_error(long)
    inner = message[message.index("(") + 1 : -1]
    assert len(inner) <= 200
    assert inner.endswith("…")
    boiler = (
        "ERROR: [youtube] abc: Unable to extract yt initial data; please report this issue on "
        "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue template."
    )
    assert friendly_error(boiler).endswith("(Unable to extract yt initial data)") or friendly_error(
        boiler
    ).endswith("([youtube] abc: Unable to extract yt initial data)")
    assert friendly_error("ERROR:  multi\n line   text ").endswith("(multi line text)")
    assert friendly_error("\x1b[0;31mERROR:\x1b[0m coloured").endswith("(coloured)")


# ----------------------------------------------------------------------------------------------
# _ydl_opts and the hooks
# ----------------------------------------------------------------------------------------------


def test_ydl_opts_contents(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, audio_quality="5", js_runtimes=["deno", "node"])
    incoming = tmp_path / "lib" / ".incoming"
    opts = _ydl_opts(settings, incoming, lambda _e: None, threading.Event())

    assert opts["format"] == "bestaudio/best"
    assert opts["outtmpl"] == str(incoming / "%(id)s.%(ext)s")
    assert opts["js_runtimes"] == {"deno": {"path": None}, "node": {"path": None}}
    assert [pp["key"] for pp in opts["postprocessors"]] == [
        "FFmpegExtractAudio",
        "FFmpegMetadata",
        "EmbedThumbnail",
    ]
    extract = opts["postprocessors"][0]
    assert extract["preferredcodec"] == "mp3"
    assert extract["preferredquality"] == "5"
    assert opts["postprocessors"][1]["add_metadata"] is True
    assert opts["postprocessors"][2]["already_have_thumbnail"] is False
    assert opts["writethumbnail"] is True
    assert opts["noplaylist"] is True
    assert opts["quiet"] is True
    assert opts["no_warnings"] is False
    assert opts["noprogress"] is True
    assert opts["windowsfilenames"] is True
    assert opts["retries"] == 3
    assert opts["fragment_retries"] == 3
    assert opts["overwrites"] is True
    assert opts["continuedl"] is False
    assert "ffmpeg_location" not in opts
    assert len(opts["progress_hooks"]) == 1 and callable(opts["progress_hooks"][0])
    assert len(opts["postprocessor_hooks"]) == 1 and callable(opts["postprocessor_hooks"][0])
    logger = opts["logger"]
    for method in ("debug", "warning", "error"):
        assert callable(getattr(logger, method))
    # the postprocessor keys must exist in the installed yt-dlp
    import yt_dlp.postprocessor

    for pp in opts["postprocessors"]:
        assert yt_dlp.postprocessor.get_postprocessor(pp["key"]) is not None


def test_ydl_opts_without_cover_and_with_ffmpeg_path(tmp_path: Path) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    settings = make_settings(
        tmp_path, embed_cover=False, ffmpeg_path=str(ffmpeg), js_runtimes=["node"]
    )
    opts = _ydl_opts(settings, tmp_path, lambda _e: None, threading.Event())
    assert [pp["key"] for pp in opts["postprocessors"]] == ["FFmpegExtractAudio", "FFmpegMetadata"]
    assert opts["writethumbnail"] is False
    assert opts["ffmpeg_location"] == str(ffmpeg)
    assert opts["js_runtimes"] == {"node": {"path": None}}


def test_ydl_opts_empty_js_runtimes_falls_back_to_defaults(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, js_runtimes=[])
    opts = _ydl_opts(settings, tmp_path, lambda _e: None, threading.Event())
    assert opts["js_runtimes"] == {"deno": {"path": None}, "node": {"path": None}}


def test_ydl_opts_are_accepted_by_real_youtubedl(tmp_path: Path) -> None:
    """Constructing YoutubeDL validates option names/values; no network involved."""
    settings = make_settings(tmp_path)
    opts = _ydl_opts(settings, tmp_path / ".incoming", lambda _e: None, threading.Event())
    with yt_dlp.YoutubeDL(opts) as ydl:
        assert ydl.params["js_runtimes"] == {"deno": {"path": None}, "node": {"path": None}}


def test_progress_hook_events(tmp_path: Path) -> None:
    events: list[ProgressEvent] = []
    settings = make_settings(tmp_path)
    opts = _ydl_opts(settings, tmp_path, events.append, threading.Event())
    hook = opts["progress_hooks"][0]

    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 50,
            "total_bytes": 200,
            "speed": 1000.0,
            "eta": 3,
        }
    )
    assert events[-1] == ProgressEvent(JobStatus.DOWNLOADING, progress=0.25, speed=1000.0, eta=3.0)

    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 100,
            "total_bytes": None,
            "total_bytes_estimate": 400,
        }
    )
    assert events[-1].progress == 0.25

    hook({"status": "downloading", "downloaded_bytes": 10})
    assert events[-1].status == JobStatus.DOWNLOADING
    assert events[-1].progress is None

    hook({"status": "downloading", "downloaded_bytes": 500, "total_bytes": 200})
    assert events[-1].progress == 1.0  # clamped

    hook({"status": "finished", "filename": "x"})
    assert events[-1] == ProgressEvent(
        JobStatus.CONVERTING, progress=1.0, message="Converting to MP3"
    )

    before = len(events)
    hook({"status": "error"})
    hook({"status": "unknown-future-status"})
    assert len(events) == before


def test_postprocessor_hook_events(tmp_path: Path) -> None:
    events: list[ProgressEvent] = []
    opts = _ydl_opts(make_settings(tmp_path), tmp_path, events.append, threading.Event())
    pp_hook = opts["postprocessor_hooks"][0]

    # yt-dlp passes PostProcessor.pp_key(): "ExtractAudio", "Metadata", "EmbedThumbnail", "MoveFiles"
    pp_hook({"status": "started", "postprocessor": "ExtractAudio"})
    assert events[-1].status == JobStatus.CONVERTING
    assert events[-1].message == "Converting to MP3"
    pp_hook({"status": "started", "postprocessor": "Metadata"})
    assert events[-1].message == "Writing tags"
    pp_hook({"status": "started", "postprocessor": "EmbedThumbnail"})
    assert events[-1].message == "Embedding cover art"
    pp_hook({"status": "started", "postprocessor": "MoveFiles"})
    assert events[-1].message == "Finishing up"
    pp_hook({"status": "started", "postprocessor": "FFmpegExtractAudio"})  # option-key spelling
    assert events[-1].message == "Converting to MP3"
    pp_hook({"status": "started", "postprocessor": "SomethingNew"})
    assert "SomethingNew" in (events[-1].message or "")
    before = len(events)
    pp_hook({"status": "finished", "postprocessor": "SomethingNew"})
    pp_hook({"status": "processing", "postprocessor": "SomethingNew"})
    assert len(events) == before
    # yt-dlp registers postprocessor hooks twice per PP: identical consecutive events collapse
    pp_hook({"status": "started", "postprocessor": "ExtractAudio"})
    pp_hook({"status": "started", "postprocessor": "ExtractAudio"})
    assert len(events) == before + 1
    pp_hook({"status": "finished", "postprocessor": "ExtractAudio"})
    pp_hook({"status": "started", "postprocessor": "ExtractAudio"})  # a new run is not a dupe
    assert len(events) == before + 2


def test_hooks_raise_when_cancelled(tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    opts = _ydl_opts(make_settings(tmp_path), tmp_path, lambda _e: None, cancel)
    with pytest.raises(DownloadCancelled):
        opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 1})
    with pytest.raises(DownloadCancelled):
        opts["postprocessor_hooks"][0]({"status": "started", "postprocessor": "FFmpegExtractAudio"})


# ----------------------------------------------------------------------------------------------
# resolve() with a fake YoutubeDL
# ----------------------------------------------------------------------------------------------


class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL; `result` may be a dict, None, an exception to raise, or
    a callable(url) returning one of those (for tests that download several videos)."""

    instances: list[FakeYoutubeDL] = []
    result: Any = None
    on_extract: Any = None  # optional callable(self, url) invoked before returning

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.calls: list[tuple[str, bool]] = []
        FakeYoutubeDL.instances.append(self)

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> Any:
        self.calls.append((url, download))
        if FakeYoutubeDL.on_extract is not None:
            FakeYoutubeDL.on_extract(self, url)
        result = FakeYoutubeDL.result
        if callable(result) and not isinstance(result, BaseException):
            result = result(url)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> type[FakeYoutubeDL]:
    FakeYoutubeDL.instances = []
    FakeYoutubeDL.result = None
    FakeYoutubeDL.on_extract = None
    monkeypatch.setattr(youtube, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


def test_resolve_single_video(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.result = {
        "id": "jNQXAC9IVRw",
        "title": "Me at the zoo",
        "channel": "jawed",
        "uploader": "jawed",
        "duration": 19,
        "thumbnails": [{"url": "https://i/small.jpg"}, {"url": "https://i/big.jpg"}],
    }
    refs = YouTubeProvider().resolve("  youtu.be/jNQXAC9IVRw ")
    assert len(refs) == 1
    ref = refs[0]
    assert ref.provider == "youtube"
    assert ref.source_id == "jNQXAC9IVRw"
    assert ref.track_id == "youtube:jNQXAC9IVRw"
    assert ref.url == "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    assert ref.title == "Me at the zoo"
    assert ref.artist == "jawed"
    assert ref.duration == 19.0
    assert ref.thumbnail_url == "https://i/big.jpg"
    assert ref.extra["original_url"] == "https://youtu.be/jNQXAC9IVRw"

    (ydl,) = fake_ydl.instances
    assert ydl.calls == [("https://youtu.be/jNQXAC9IVRw", False)]
    assert ydl.opts["extract_flat"] == "in_playlist"
    assert ydl.opts["noplaylist"] is True
    assert ydl.opts["skip_download"] is True
    assert ydl.opts["ignoreerrors"] is True
    assert ydl.opts["quiet"] is True
    assert ydl.opts["no_warnings"] is True
    assert ydl.opts["js_runtimes"] == {"deno": {"path": None}, "node": {"path": None}}


def test_resolve_uses_settings_js_runtimes(fake_ydl: type[FakeYoutubeDL], tmp_path: Path) -> None:
    fake_ydl.result = {"id": "jNQXAC9IVRw", "title": "t"}
    provider = YouTubeProvider(make_settings(tmp_path, js_runtimes=["node"]))
    provider.resolve("https://youtu.be/jNQXAC9IVRw")
    assert fake_ydl.instances[-1].opts["js_runtimes"] == {"node": {"path": None}}


def test_resolve_and_doctor_honour_config_json_and_configure(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path, tmp_settings: Settings
) -> None:
    """A provider built without settings (the registry) still follows the user's config.json."""
    fake_ydl.result = {"id": "jNQXAC9IVRw", "title": "t"}
    saved_ffmpeg = str(tmp_path / "x" / "ffmpeg.exe")
    Settings(library_dir=tmp_path / "lib", js_runtimes=["node"], ffmpeg_path=saved_ffmpeg).save()
    provider = YouTubeProvider()
    provider.resolve("https://youtu.be/jNQXAC9IVRw")
    assert fake_ydl.instances[-1].opts["js_runtimes"] == {"node": {"path": None}}
    ok, label, detail = provider.doctor()[0]
    assert (ok, label) == (True, "ffmpeg") and saved_ffmpeg in detail

    live_ffmpeg = str(tmp_path / "y" / "ffmpeg.exe")
    live = Settings(library_dir=tmp_path / "lib", js_runtimes=["deno"], ffmpeg_path=live_ffmpeg)
    provider.configure(live)  # what providers.configure(settings) does at app start
    provider.resolve("https://youtu.be/jNQXAC9IVRw")
    assert fake_ydl.instances[-1].opts["js_runtimes"] == {"deno": {"path": None}}
    assert live_ffmpeg in provider.doctor()[0][2]
    live.js_runtimes = ["node"]  # the same object the app mutates in PUT /api/settings
    provider.resolve("https://youtu.be/jNQXAC9IVRw")
    assert fake_ydl.instances[-1].opts["js_runtimes"] == {"node": {"path": None}}


def test_resolve_single_video_prefers_music_artist_and_strips_topic(
    fake_ydl: type[FakeYoutubeDL],
) -> None:
    fake_ydl.result = {
        "id": "abcdefghijk",
        "title": "T",
        "artist": "Real Artist",
        "channel": "X - Topic",
        "thumbnail": "https://t",
    }
    ref = YouTubeProvider().resolve("https://youtu.be/abcdefghijk")[0]
    assert ref.artist == "Real Artist"
    assert ref.thumbnail_url == "https://t"
    fake_ydl.result = {"id": "abcdefghijk", "title": "T", "channel": "Some Artist - Topic"}
    ref = YouTubeProvider().resolve("https://youtu.be/abcdefghijk")[0]
    assert ref.artist == "Some Artist"


def test_resolve_playlist_filters_entries(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.result = {
        "_type": "playlist",
        "id": "PL1",
        "title": "My list",
        "entries": [
            {
                "id": "aaaaaaaaaaa",
                "title": "First - Song",
                "channel": "Chan",
                "duration": 100,
                "thumbnails": [{"url": "u1"}],
            },
            None,
            {"id": "bbbbbbbbbbb", "title": "[Private video]"},
            {"id": "ccccccccccc", "title": "[Deleted video]"},
            {"title": "no id at all"},
            {"id": "ddddddddddd", "title": "Second", "uploader": "Up - Topic", "album": "Alb"},
        ],
    }
    refs = YouTubeProvider().resolve("https://www.youtube.com/playlist?list=PL1")
    assert [r.source_id for r in refs] == ["aaaaaaaaaaa", "ddddddddddd"]
    first, second = refs
    assert first.url == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert first.title == "First - Song"
    assert first.artist == "Chan"
    assert first.duration == 100.0
    assert first.thumbnail_url == "u1"
    assert second.artist == "Up"
    assert second.album == "Alb"
    assert second.duration is None
    assert second.thumbnail_url is None


def test_resolve_empty_playlist(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.result = {"_type": "playlist", "id": "PL1", "entries": []}
    assert YouTubeProvider().resolve("https://www.youtube.com/playlist?list=PL1") == []


def test_resolve_maps_download_error(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.result = yt_dlp.utils.DownloadError(
        "ERROR: [youtube] abc: Private video. Sign in if you've been granted access"
    )
    with pytest.raises(ProviderError) as exc_info:
        YouTubeProvider().resolve("https://youtu.be/abcdefghijk")
    assert str(exc_info.value).startswith("This video is private")
    assert "Private video" in str(exc_info.value)


def test_resolve_maps_extractor_error(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.result = yt_dlp.utils.ExtractorError("Video unavailable", expected=True)
    with pytest.raises(ProviderError) as exc_info:
        YouTubeProvider().resolve("https://youtu.be/abcdefghijk")
    assert str(exc_info.value).startswith("This video is unavailable")


def test_resolve_none_result_uses_logged_error(fake_ydl: type[FakeYoutubeDL]) -> None:
    def log_error(ydl: FakeYoutubeDL, url: str) -> None:  # what yt-dlp does with ignoreerrors=True
        ydl.opts["logger"].error("ERROR: [youtube] abc: Video unavailable")

    fake_ydl.on_extract = log_error
    fake_ydl.result = None
    with pytest.raises(ProviderError) as exc_info:
        YouTubeProvider().resolve("https://youtu.be/abcdefghijk")
    assert str(exc_info.value).startswith("This video is unavailable")
    assert "abc: Video unavailable" in str(exc_info.value)


def test_resolve_none_result_without_logged_error(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.result = None
    with pytest.raises(ProviderError) as exc_info:
        YouTubeProvider().resolve("https://youtu.be/abcdefghijk")
    assert "no information" in str(exc_info.value)


def test_resolve_unexpected_exception(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.result = RuntimeError("boom")
    with pytest.raises(ProviderError) as exc_info:
        YouTubeProvider().resolve("https://youtu.be/abcdefghijk")
    assert "boom" in str(exc_info.value)
    assert not isinstance(exc_info.value, DownloadCancelled)


# ----------------------------------------------------------------------------------------------
# download() with a fake YoutubeDL
# ----------------------------------------------------------------------------------------------


def _write_mp3(path: Path, track_id: str | None = None, cover: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(TINY_MP3)
    if track_id or cover:
        tags = ID3()
        if track_id:
            tags.add(TXXX(encoding=3, desc="ULTIMATE_PLAYLIST_ID", text=[track_id]))
        if cover:
            tags.add(
                APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=b"\xff\xd8\xff\xd9")
            )
        tags.save(str(path))
    return path


def _ref(video_id: str = "jNQXAC9IVRw") -> TrackRef:
    return TrackRef(
        provider="youtube", source_id=video_id, url=f"https://www.youtube.com/watch?v={video_id}"
    )


def _fake_download(
    info: dict[str, Any], *, cover: bool = False, leftovers: bool = True, fmt: str = "mp3"
) -> Any:
    """Return an on_extract callback that behaves like a successful yt-dlp run producing `fmt`."""

    def on_extract(ydl: FakeYoutubeDL, url: str) -> None:
        video_id = info["id"]
        template = ydl.opts["outtmpl"]
        out = Path(template % {"id": video_id, "ext": fmt})
        if fmt == "mp3":
            _write_mp3(out, cover=cover)
        else:  # yt-dlp's own tags (artist/title) are overwritten by ours, the cover is kept
            write_tiny_audio(out, artist="yt", title="yt", cover=FAKE_PNG if cover else None)
        if leftovers:
            (out.parent / f"{video_id}.webp").write_bytes(b"thumb")
            (out.parent / f"{video_id}.webm.part").write_bytes(b"partial")
        for hook in ydl.opts["progress_hooks"]:
            hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": 10,
                    "total_bytes": 100,
                    "speed": 5.0,
                    "eta": 1,
                }
            )
            hook({"status": "finished", "filename": str(out)})
        for pp_hook in ydl.opts["postprocessor_hooks"]:
            pp_hook({"status": "started", "postprocessor": "FFmpegExtractAudio"})
            pp_hook({"status": "finished", "postprocessor": "FFmpegExtractAudio"})

    return on_extract


def test_download_success(fake_ydl: type[FakeYoutubeDL], tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    info = {
        "id": "jNQXAC9IVRw",
        "title": "Rick Astley - Never Gonna Give You Up (Official Video)",
        "channel": "Rick Astley",
        "album": "Whenever You Need Somebody",
        "duration": 213,
    }
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info, cover=True)
    events: list[ProgressEvent] = []
    cancel = threading.Event()

    track = YouTubeProvider().download(
        _ref(), settings.library_dir, settings, events.append, cancel
    )

    final = settings.library_dir / "Rick Astley - Never Gonna Give You Up.mp3"
    assert final.is_file()
    assert track.id == "youtube:jNQXAC9IVRw"
    assert track.provider == "youtube"
    assert track.source_id == "jNQXAC9IVRw"
    assert track.source_url == "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    assert track.artist == "Rick Astley"
    assert track.title == "Never Gonna Give You Up"
    assert track.album == "Whenever You Need Somebody"
    assert track.path == "Rick Astley - Never Gonna Give You Up.mp3"
    assert track.has_cover is True
    assert track.file_size == final.stat().st_size > 0
    assert track.duration is not None and track.duration > 0

    tags = ID3(str(final))
    assert tags["TPE1"].text == ["Rick Astley"]
    assert tags["TIT2"].text == ["Never Gonna Give You Up"]
    assert tags["TALB"].text == ["Whenever You Need Somebody"]
    assert tags.getall("COMM")[0].text == ["https://www.youtube.com/watch?v=jNQXAC9IVRw"]
    assert tags.getall("TXXX:ULTIMATE_PLAYLIST_ID")[0].text == ["youtube:jNQXAC9IVRw"]
    assert tags.getall("APIC")
    assert stored_track_id(final) == "youtube:jNQXAC9IVRw"

    incoming = settings.library_dir / ".incoming"
    assert incoming.is_dir()
    assert list(incoming.iterdir()) == []  # mp3 moved, thumbnail and .part removed

    statuses = [e.status for e in events]
    assert statuses[0] == JobStatus.DOWNLOADING
    assert JobStatus.CONVERTING in statuses
    assert statuses.index(JobStatus.DOWNLOADING) < statuses.index(JobStatus.CONVERTING)
    assert any(e.message == "Converting to MP3" for e in events)

    (ydl,) = fake_ydl.instances
    assert ydl.calls == [("https://www.youtube.com/watch?v=jNQXAC9IVRw", True)]
    assert ydl.opts["outtmpl"] == str(incoming / "%(id)s.%(ext)s")


@pytest.mark.parametrize("fmt", OTHER_FORMATS)
def test_download_in_other_containers_tags_without_corrupting(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path, fmt: str
) -> None:
    """audio_format m4a/opus/flac used to get ID3 frames written into the container (unplayable
    M4A, broken Opus, FLAC with tags nobody reads)."""
    settings = make_settings(tmp_path, audio_format=fmt)
    info = {"id": "jNQXAC9IVRw", "title": "Some Artist - Fmt Song (Official Video)", "album": "Alb"}
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info, cover=True, fmt=fmt)
    events: list[ProgressEvent] = []

    track = YouTubeProvider().download(
        _ref(), settings.library_dir, settings, events.append, threading.Event()
    )

    final = settings.library_dir / f"Some Artist - Fmt Song.{fmt}"
    assert track.path == final.name and final.is_file()
    assert (track.artist, track.title, track.album) == ("Some Artist", "Fmt Song", "Alb")
    assert track.has_cover is True
    assert track.duration is not None and track.duration > 0
    assert not final.read_bytes().startswith(b"ID3")  # no ID3 header glued onto the container
    audio = mutagen.File(str(final))
    assert audio is not None and audio.info.length > 0  # the container is still recognised
    tags = read_tags(final)  # what a rescan sees
    assert (tags.artist, tags.title, tags.album) == ("Some Artist", "Fmt Song", "Alb")
    assert tags.track_id == "youtube:jNQXAC9IVRw"
    assert tags.has_cover is True
    assert stored_track_id(final) == "youtube:jNQXAC9IVRw"
    assert any(e.message == f"Converting to {fmt.upper()}" for e in events)
    assert list((settings.library_dir / ".incoming").iterdir()) == []

    # a re-download of the same track replaces the file instead of minting " (2)"
    fake_ydl.on_extract = _fake_download(info, cover=False, fmt=fmt)
    again = YouTubeProvider().download(
        _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
    )
    assert again.path == track.path and again.has_cover is False
    assert not (settings.library_dir / f"Some Artist - Fmt Song (2).{fmt}").exists()


def test_download_names_the_file_after_the_real_container(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    """`alac` makes yt-dlp produce an .m4a: the extension follows the file, not the setting."""
    settings = make_settings(tmp_path, audio_format="alac")
    info = {"id": "jNQXAC9IVRw", "title": "A - B"}
    fake_ydl.result = info

    def on_extract(ydl: FakeYoutubeDL, url: str) -> None:
        out = Path(ydl.opts["outtmpl"] % {"id": "jNQXAC9IVRw", "ext": "m4a"})
        write_tiny_audio(out)
        info["requested_downloads"] = [{"filepath": str(out)}]

    fake_ydl.on_extract = on_extract
    track = YouTubeProvider().download(
        _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
    )
    assert track.path == "A - B.m4a"
    assert read_tags(settings.library_dir / track.path).track_id == "youtube:jNQXAC9IVRw"


def test_concurrent_downloads_with_the_same_name_do_not_overwrite_each_other(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    """Two different videos cleaning to 'Artist - Title' finishing at the same moment must end
    up as two files (the name pick and the move are one critical section)."""
    settings = make_settings(tmp_path)
    ids = ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    infos = {vid: {"id": vid, "title": "Same Artist - Same Title"} for vid in ids}
    barrier = threading.Barrier(len(ids), timeout=WAIT)

    def on_extract(ydl: FakeYoutubeDL, url: str) -> None:
        vid = url.rsplit("=", 1)[1]
        _fake_download(infos[vid], leftovers=False)(ydl, url)
        barrier.wait()  # both leave "yt-dlp" together and race for the file name

    fake_ydl.on_extract = on_extract
    fake_ydl.result = lambda url: infos[url.rsplit("=", 1)[1]]
    results: dict[str, Any] = {}
    errors: list[BaseException] = []

    def worker(vid: str) -> None:
        try:
            results[vid] = YouTubeProvider().download(
                _ref(vid), settings.library_dir, settings, lambda _e: None, threading.Event()
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(vid,)) for vid in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)
    assert not errors, errors
    assert sorted(t.path for t in results.values()) == [
        "Same Artist - Same Title (2).mp3",
        "Same Artist - Same Title.mp3",
    ]
    for vid, track in results.items():
        assert stored_track_id(settings.library_dir / track.path) == f"youtube:{vid}"


def test_download_unwraps_a_playlist_shaped_result(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    """yt-dlp can hand back a playlist dict even with noplaylist; the first real entry counts."""
    settings = make_settings(tmp_path)
    fake_ydl.result = {
        "_type": "playlist",
        "id": "PL1",
        "entries": [None, {"id": "jNQXAC9IVRw", "title": "A - B"}],
    }
    fake_ydl.on_extract = _fake_download({"id": "jNQXAC9IVRw"}, leftovers=False)
    track = YouTubeProvider().download(
        _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
    )
    assert track.source_id == "jNQXAC9IVRw"
    assert track.path == "A - B.mp3"
    assert (track.artist, track.title) == ("A", "B")


def test_download_of_an_empty_playlist_shaped_result_is_an_error(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    fake_ydl.result = {"_type": "playlist", "entries": []}
    fake_ydl.on_extract = _fake_download({"id": "jNQXAC9IVRw"})  # a stray file to clean up
    with pytest.raises(ProviderError) as exc_info:
        YouTubeProvider().download(
            _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
        )
    assert "no information" in str(exc_info.value)
    assert list((settings.library_dir / ".incoming").iterdir()) == []


def test_resolve_unwraps_multi_video(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.result = {
        "_type": "multi_video",
        "id": "PL1",
        "entries": [{"id": "aaaaaaaaaaa", "title": "One"}, {"id": "bbbbbbbbbbb", "title": "Two"}],
    }
    refs = YouTubeProvider().resolve("https://www.youtube.com/watch?v=aaaaaaaaaaa")
    assert [r.source_id for r in refs] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def test_download_without_cover_and_album(fake_ydl: type[FakeYoutubeDL], tmp_path: Path) -> None:
    settings = make_settings(tmp_path, embed_cover=False)
    info = {"id": "jNQXAC9IVRw", "title": "Me at the zoo", "uploader": "jawed"}
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info, cover=False)
    track = YouTubeProvider().download(
        _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
    )
    assert track.has_cover is False
    assert track.album is None
    assert track.artist == "jawed"
    assert track.title == "Me at the zoo"
    tags = ID3(str(settings.library_dir / track.path))
    assert "TALB" not in tags
    assert not tags.getall("APIC")


def test_download_falls_back_to_requested_downloads_filepath(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    other = tmp_path / "elsewhere" / "file.mp3"
    info = {
        "id": "jNQXAC9IVRw",
        "title": "A - B",
        "requested_downloads": [{"filepath": str(other)}],
    }
    fake_ydl.result = info

    def on_extract(ydl: FakeYoutubeDL, url: str) -> None:
        _write_mp3(other)

    fake_ydl.on_extract = on_extract
    track = YouTubeProvider().download(
        _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
    )
    assert (settings.library_dir / track.path).is_file()
    assert not other.exists()


def test_download_missing_output_is_friendly(fake_ydl: type[FakeYoutubeDL], tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake_ydl.result = {"id": "jNQXAC9IVRw", "title": "A - B"}
    with pytest.raises(ProviderError) as exc_info:
        YouTubeProvider().download(
            _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
        )
    assert "ffmpeg" in str(exc_info.value)


def test_download_unique_name_for_different_track(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    existing = _write_mp3(settings.library_dir / "A - B.mp3", track_id="youtube:other000000")
    info = {"id": "jNQXAC9IVRw", "title": "A - B"}
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info)
    track = YouTubeProvider().download(
        _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
    )
    assert track.path == "A - B (2).mp3"
    assert stored_track_id(existing) == "youtube:other000000"  # untouched


def test_download_overwrites_same_track(fake_ydl: type[FakeYoutubeDL], tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    _write_mp3(settings.library_dir / "A - B.mp3", track_id="youtube:jNQXAC9IVRw")
    info = {"id": "jNQXAC9IVRw", "title": "A - B"}
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info)
    track = YouTubeProvider().download(
        _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
    )
    assert track.path == "A - B.mp3"
    assert not (settings.library_dir / "A - B (2).mp3").exists()


def test_download_replaces_its_own_numbered_file(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    """X lives at 'A - B.mp3', this track at 'A - B (2).mp3': a re-download must reuse (2)."""
    settings = make_settings(tmp_path)
    _write_mp3(settings.library_dir / "A - B.mp3", track_id="youtube:other000000")
    _write_mp3(settings.library_dir / "A - B (2).mp3", track_id="youtube:jNQXAC9IVRw")
    _write_mp3(settings.library_dir / "A - B (3).mp3", track_id="youtube:third000000")
    info = {"id": "jNQXAC9IVRw", "title": "A - B"}
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info)
    track = YouTubeProvider().download(
        _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
    )
    assert track.path == "A - B (2).mp3"
    assert not (settings.library_dir / "A - B (4).mp3").exists()
    assert stored_track_id(settings.library_dir / "A - B.mp3") == "youtube:other000000"
    assert stored_track_id(settings.library_dir / "A - B (3).mp3") == "youtube:third000000"
    # one id, one file: a rescan will not see duplicates
    ids = [stored_track_id(p) for p in sorted(settings.library_dir.glob("*.mp3"))]
    assert ids.count("youtube:jNQXAC9IVRw") == 1


def test_download_replace_of_a_file_in_use_is_friendly(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows: os.replace onto a file the player is streaming raises WinError 32."""
    settings = make_settings(tmp_path)
    _write_mp3(settings.library_dir / "A - B.mp3", track_id="youtube:jNQXAC9IVRw")
    info = {"id": "jNQXAC9IVRw", "title": "A - B"}
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info)

    real_replace = youtube.os.replace

    def locked(src: object, dst: object) -> None:
        if Path(str(dst)).name == "A - B.mp3":  # only the library file is "playing"
            raise PermissionError(32, "The process cannot access the file because it is being used")
        real_replace(src, dst)

    monkeypatch.setattr(youtube.os, "replace", locked)
    with pytest.raises(ProviderError) as exc_info:
        YouTubeProvider().download(
            _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
        )
    message = str(exc_info.value)
    assert "in use" in message and "A - B.mp3" in message
    assert "WinError" not in message and "Download failed:" not in message
    assert list((settings.library_dir / ".incoming").iterdir()) == []  # cleaned up


def test_download_cancelled_before_start(fake_ydl: type[FakeYoutubeDL], tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        YouTubeProvider().download(_ref(), settings.library_dir, settings, lambda _e: None, cancel)
    assert fake_ydl.instances == []


def test_download_cancelled_via_hook_cleans_up(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    cancel = threading.Event()
    incoming = settings.library_dir / ".incoming"

    def on_extract(ydl: FakeYoutubeDL, url: str) -> None:
        incoming.mkdir(parents=True, exist_ok=True)
        (incoming / "jNQXAC9IVRw.webm.part").write_bytes(b"partial")
        cancel.set()  # the user clicked Cancel mid-download
        ydl.opts["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 1})

    fake_ydl.on_extract = on_extract
    with pytest.raises(DownloadCancelled):
        YouTubeProvider().download(_ref(), settings.library_dir, settings, lambda _e: None, cancel)
    assert list(incoming.iterdir()) == []


def test_download_error_becomes_friendly_and_cleans_up(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    incoming = settings.library_dir / ".incoming"

    def on_extract(ydl: FakeYoutubeDL, url: str) -> None:
        (incoming / "jNQXAC9IVRw.webm.part").write_bytes(b"partial")
        (incoming / "jNQXAC9IVRw.webp").write_bytes(b"thumb")
        (incoming / "unrelated.mp3").write_bytes(b"keep me")
        raise yt_dlp.utils.DownloadError("ERROR: [youtube] jNQXAC9IVRw: Video unavailable")

    fake_ydl.on_extract = on_extract
    with pytest.raises(ProviderError) as exc_info:
        YouTubeProvider().download(
            _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
        )
    assert str(exc_info.value).startswith("This video is unavailable")
    assert not isinstance(exc_info.value, DownloadCancelled)
    assert [p.name for p in incoming.iterdir()] == ["unrelated.mp3"]


def test_download_error_while_cancelled_is_cancellation(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    cancel = threading.Event()

    def on_extract(ydl: FakeYoutubeDL, url: str) -> None:
        cancel.set()
        raise yt_dlp.utils.DownloadError("ERROR: unable to download video data: connection reset")

    fake_ydl.on_extract = on_extract
    with pytest.raises(DownloadCancelled):
        YouTubeProvider().download(_ref(), settings.library_dir, settings, lambda _e: None, cancel)


def test_download_unexpected_error(fake_ydl: type[FakeYoutubeDL], tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    fake_ydl.result = RuntimeError("disk on fire")
    with pytest.raises(ProviderError) as exc_info:
        YouTubeProvider().download(
            _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
        )
    assert str(exc_info.value) == "Download failed: disk on fire"


def test_download_none_info_uses_logged_error(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)

    def on_extract(ydl: FakeYoutubeDL, url: str) -> None:
        ydl.opts["logger"].error("ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm your age")

    fake_ydl.on_extract = on_extract
    fake_ydl.result = None
    with pytest.raises(ProviderError) as exc_info:
        YouTubeProvider().download(
            _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
        )
    assert str(exc_info.value).startswith("This video is age-restricted")


def test_download_creates_dest_and_incoming(fake_ydl: type[FakeYoutubeDL], tmp_path: Path) -> None:
    settings = make_settings(tmp_path / "deep" / "er")
    info = {"id": "jNQXAC9IVRw", "title": "A - B"}
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info, leftovers=False)
    assert not settings.library_dir.exists()
    YouTubeProvider().download(
        _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
    )
    assert (settings.library_dir / ".incoming").is_dir()


def test_download_safe_filename_for_nasty_title(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    info = {"id": "jNQXAC9IVRw", "title": 'AC/DC - "Back" <in> Black? (Official Video)'}
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info, leftovers=False)
    track = YouTubeProvider().download(
        _ref(), settings.library_dir, settings, lambda _e: None, threading.Event()
    )
    assert track.path == "ACDC - Back in Black.mp3"
    assert track.artist == "AC/DC"  # tags keep the real name
    assert track.title == '"Back" <in> Black?'


# ----------------------------------------------------------------------------------------------
# write_tags / stored_track_id
# ----------------------------------------------------------------------------------------------


def test_write_tags_adds_header_and_keeps_cover(tmp_path: Path) -> None:
    bare = _write_mp3(tmp_path / "bare.mp3")
    assert write_tags(bare, "Artist", "Title", None, "https://src", "youtube:x") is False
    tags = ID3(str(bare))
    assert tags["TPE1"].text == ["Artist"]
    assert tags["TIT2"].text == ["Title"]
    assert "TALB" not in tags
    assert tags.getall("COMM")[0].text == ["https://src"]
    assert stored_track_id(bare) == "youtube:x"

    with_cover = _write_mp3(tmp_path / "cover.mp3", cover=True)
    assert write_tags(with_cover, "A", "T", "Album", "https://src", "youtube:y") is True
    tags = ID3(str(with_cover))
    assert tags["TALB"].text == ["Album"]
    assert tags.getall("APIC")

    # re-tagging replaces rather than duplicates frames
    write_tags(with_cover, "A2", "T2", None, "https://src2", "youtube:z")
    tags = ID3(str(with_cover))
    assert tags["TPE1"].text == ["A2"]
    assert "TALB" not in tags
    assert len(tags.getall("COMM")) == 1
    assert len(tags.getall("TXXX:ULTIMATE_PLAYLIST_ID")) == 1
    assert stored_track_id(with_cover) == "youtube:z"


def test_stored_track_id_missing(tmp_path: Path) -> None:
    assert stored_track_id(tmp_path / "nope.mp3") is None
    assert stored_track_id(tmp_path / "nope.m4a") is None
    assert stored_track_id(_write_mp3(tmp_path / "untagged.mp3")) is None
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not an mp3")
    assert stored_track_id(junk) is None
    junk = tmp_path / "junk.flac"
    junk.write_bytes(b"not a flac")
    assert stored_track_id(junk) is None


@pytest.mark.parametrize("fmt", OTHER_FORMATS)
def test_write_tags_uses_the_native_tag_format_of_each_container(tmp_path: Path, fmt: str) -> None:
    plain = write_tiny_audio(tmp_path / f"plain.{fmt}", artist="yt", title="yt", album="old")
    assert (
        write_tags(plain, "Artist", "Title", None, "https://src", f"youtube:{fmt}0000000") is False
    )
    assert not plain.read_bytes().startswith(b"ID3")
    audio = mutagen.File(str(plain))
    assert audio is not None and audio.info.length > 0
    info = read_tags(plain)
    assert (info.artist, info.title, info.album) == ("Artist", "Title", None)  # album cleared
    assert info.track_id == f"youtube:{fmt}0000000"
    assert info.has_cover is False
    assert stored_track_id(plain) == f"youtube:{fmt}0000000"
    if fmt == "m4a":
        assert audio.tags["\xa9cmt"] == ["https://src"]
    else:
        assert audio.tags["comment"] == ["https://src"]

    with_cover = write_tiny_audio(tmp_path / f"cover.{fmt}", cover=FAKE_PNG)
    assert write_tags(with_cover, "A", "T", "Album", "https://src", "youtube:y") is True
    info = read_tags(with_cover)
    assert (info.artist, info.title, info.album, info.has_cover) == ("A", "T", "Album", True)
    # re-tagging replaces rather than duplicates
    write_tags(with_cover, "A2", "T2", "Album 2", "https://src2", "youtube:z")
    info = read_tags(with_cover)
    assert (info.artist, info.title, info.album, info.track_id) == (
        "A2",
        "T2",
        "Album 2",
        "youtube:z",
    )
    assert info.has_cover is True


def test_write_tags_refuses_an_unknown_container(tmp_path: Path) -> None:
    odd = tmp_path / "song.xyz"
    odd.write_bytes(b"definitely not audio")
    with pytest.raises(ProviderError) as exc_info:
        write_tags(odd, "A", "T", None, "https://src", "youtube:x")
    assert ".xyz" in str(exc_info.value) and "mp3" in str(exc_info.value)


# ----------------------------------------------------------------------------------------------
# doctor()
# ----------------------------------------------------------------------------------------------


@pytest.mark.real_tools  # the one smoke test that shells out to the real ffmpeg / node probes
def test_doctor_shape_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    checks = YouTubeProvider().doctor()
    assert [label for _ok, label, _detail in checks] == ["ffmpeg", "JavaScript runtime", "yt-dlp"]
    assert all(
        isinstance(ok, bool) and isinstance(detail, str) and detail for ok, _l, detail in checks
    )
    assert checks[2] == (True, "yt-dlp", yt_dlp.version.__version__)


def test_doctor_uses_the_configured_ffmpeg_path(tmp_path: Path, monkeypatch) -> None:
    """README: 'put the full path in ffmpeg_path' must satisfy the doctor even off PATH."""
    from ultimate_playlist.ffmpeg import FfmpegInfo

    explicit = tmp_path / "tools" / "ffmpeg.EXE"

    def only_explicit(path: str | None = None) -> FfmpegInfo:
        return FfmpegInfo(path, "8.1.1") if path == str(explicit) else FfmpegInfo(None, None)

    monkeypatch.setattr(youtube, "find_ffmpeg", only_explicit)
    with_path = make_settings(tmp_path, ffmpeg_path=str(explicit))
    assert YouTubeProvider().doctor(with_path)[0] == (True, "ffmpeg", f"{explicit} (8.1.1)")
    assert YouTubeProvider(with_path).doctor()[0][0] is True
    assert YouTubeProvider().doctor(make_settings(tmp_path))[0][0] is False


def test_doctor_reports_missing_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    from ultimate_playlist.ffmpeg import FfmpegInfo

    monkeypatch.setattr(youtube, "find_ffmpeg", lambda explicit=None: FfmpegInfo(None, None))
    monkeypatch.setattr(youtube, "install_hint", lambda: "install it like so")
    checks = YouTubeProvider().doctor()
    assert checks[0] == (False, "ffmpeg", "install it like so")


def test_doctor_reports_found_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    from ultimate_playlist.ffmpeg import FfmpegInfo

    monkeypatch.setattr(
        youtube, "find_ffmpeg", lambda explicit=None: FfmpegInfo("C:/ff/ffmpeg.exe", "8.0")
    )
    ok, label, detail = YouTubeProvider().doctor()[0]
    assert (ok, label) == (True, "ffmpeg")
    assert "C:/ff/ffmpeg.exe" in detail and "8.0" in detail


def test_doctor_reports_missing_js_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "_runtime_info", lambda name: None)
    monkeypatch.setattr(youtube.shutil, "which", lambda name: None)
    ok, label, detail = YouTubeProvider().doctor()[1]
    assert (ok, label) == (False, "JavaScript runtime")
    assert "winget install DenoLand.Deno" in detail


def test_doctor_reports_js_runtime_details(monkeypatch: pytest.MonkeyPatch) -> None:
    class Info:
        name = "node"
        path = "C:/nodejs/node.exe"
        version = "24.1.0"
        supported = True

    monkeypatch.setattr(youtube, "_runtime_info", lambda name: Info() if name == "node" else None)
    ok, _label, detail = YouTubeProvider().doctor()[1]
    assert ok is True
    assert detail.startswith("node 24.1.0")

    Info.supported = False
    ok, _label, detail = YouTubeProvider().doctor()[1]
    assert ok is False
    assert "too old" in detail


def test_doctor_falls_back_to_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "_runtime_info", lambda name: None)
    monkeypatch.setattr(
        youtube.shutil, "which", lambda name: "/usr/bin/deno" if name == "deno" else None
    )
    ok, _label, detail = YouTubeProvider().doctor()[1]
    assert ok is True
    assert "deno" in detail


def test_doctor_survives_broken_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("kaput")

    monkeypatch.setattr(youtube, "find_ffmpeg", boom)
    monkeypatch.setattr(youtube, "_js_runtime_check", boom)
    checks = YouTubeProvider().doctor()
    assert checks[0][0] is False and "kaput" in checks[0][2]
    assert checks[1][0] is False and "kaput" in checks[1][2]
    assert checks[2][0] is True
