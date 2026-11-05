"""The TikTok provider: link shapes, naming, error texts, resolve() and download() with a fake
yt-dlp. Offline: yt-dlp and the short-link HEAD request are replaced."""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any

import pytest
import requests
import yt_dlp
import yt_dlp.utils
from fake_provider import write_tiny_mp3
from mutagen.id3 import ID3
from yt_dlp.extractor.tiktok import TikTokIE

from ultimate_playlist import providers
from ultimate_playlist.config import Settings
from ultimate_playlist.models import JobStatus, ProgressEvent, TrackRef
from ultimate_playlist.providers import tiktok, youtube
from ultimate_playlist.providers.base import ProviderError
from ultimate_playlist.providers.tiktok import (
    CAPTION_MAX_LEN,
    YDL_FORMAT,
    TikTokLink,
    TikTokProvider,
    caption_title,
    clean_caption,
    friendly_error,
    is_generic_sound,
    join_artists,
    parse_link,
    pick_tags,
    post_url,
    unsupported_message,
)

POST_ID = "7698805564884733198"


# ----------------------------------------------------------------------------------------------
# Links
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "post_id", "user"),
    [
        (f"https://www.tiktok.com/@nasa/video/{POST_ID}", POST_ID, "nasa"),
        (f"https://www.tiktok.com/@nasa/video/{POST_ID}/", POST_ID, "nasa"),
        (f"https://www.tiktok.com/@nasa/video/{POST_ID}?is_from_webapp=1&lang=en", POST_ID, "nasa"),
        (f"https://www.tiktok.com/@nasa/photo/{POST_ID}", POST_ID, "nasa"),  # photo slideshow
        (f"https://tiktok.com/@the.user_1/video/{POST_ID}", POST_ID, "the.user_1"),
        (f"tiktok.com/@nasa/video/{POST_ID}", POST_ID, "nasa"),  # no scheme
        (f"  www.tiktok.com/@nasa/video/{POST_ID}  ", POST_ID, "nasa"),
        (f"https://m.tiktok.com/@nasa/video/{POST_ID}", POST_ID, "nasa"),
        (f"https://www.tiktok.com/@/video/{POST_ID}?_r=1", POST_ID, None),  # short-link target
        (f"https://www.tiktok.com/share/video/{POST_ID}/", POST_ID, None),
        (f"https://www.tiktok.com/video/{POST_ID}", POST_ID, None),
        (f"https://www.tiktok.com/embed/{POST_ID}", POST_ID, None),
        (f"https://www.tiktok.com/embed/v2/{POST_ID}", POST_ID, None),
        (f"https://m.tiktok.com/v/{POST_ID}.html", POST_ID, None),
        (f"https://www.tiktokv.com/share/video/{POST_ID}/", POST_ID, None),
        (f"HTTPS://WWW.TIKTOK.COM/@nasa/video/{POST_ID}", POST_ID, "nasa"),
    ],
)
def test_parse_link_posts(url: str, post_id: str, user: str | None) -> None:
    link = parse_link(url)
    assert link is not None and link.kind == "post"
    assert (link.post_id, link.user) == (post_id, user)


@pytest.mark.parametrize(
    "url",
    [
        "https://vm.tiktok.com/ZMabc123/",
        "https://vt.tiktok.com/ZS9c687pa/",
        "vm.tiktok.com/ZMabc123",
        "https://www.tiktok.com/t/ZTRC5xgJp/",
        "https://tiktok.com/t/ZTRC5xgJp",
    ],
)
def test_parse_link_short_links(url: str) -> None:
    link = parse_link(url)
    assert link is not None and link.kind == "short" and link.post_id is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@nasa",
        "https://www.tiktok.com/@nasa/",
        "https://www.tiktok.com/music/City-Nights-7036843036118469381",
        "https://www.tiktok.com/@nasa/live",
        "https://www.tiktok.com/@nasa/collection/space-7371330159376370462",
        "https://www.tiktok.com/",
        "https://www.tiktok.com/foryou",
        f"https://www.tiktok.com/@nasa/video/{POST_ID}abc",  # not a numeric id
        "https://vm.tiktok.com/",
    ],
)
def test_parse_link_other_tiktok_pages(url: str) -> None:
    link = parse_link(url)
    assert link is not None and link.kind == "other" and link.post_id is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://open.spotify.com/track/abc",
        "https://www.tiktok.com.evil.example/@nasa/video/1",
        "https://nottiktok.com/@nasa/video/1",
        "https://example.com/?u=https://www.tiktok.com/@nasa/video/1",
        "ftp://www.tiktok.com/@nasa/video/1",
        "spotify:track:abc",
        "",
        "just words",
        "https://[broken",
    ],
)
def test_parse_link_rejects_other_hosts(url: str) -> None:
    assert parse_link(url) is None
    assert TikTokProvider().matches(url) is False


def test_matches_owns_every_tiktok_page() -> None:
    """Profiles and sound pages are owned too, so resolve() can explain them."""
    provider = TikTokProvider()
    assert provider.matches(f"https://www.tiktok.com/@nasa/video/{POST_ID}")
    assert provider.matches("https://vm.tiktok.com/ZMabc123/")
    assert provider.matches("https://www.tiktok.com/@nasa")


def test_registry_routes_tiktok_links() -> None:
    provider = providers.get_provider(f"tiktok.com/@nasa/video/{POST_ID}")
    assert provider is not None and provider.name == "tiktok"
    assert providers.get_provider("https://vt.tiktok.com/ZS9c687pa/").name == "tiktok"
    assert providers.get_provider("https://www.youtube.com/watch?v=jNQXAC9IVRw").name == "youtube"
    assert providers.provider_by_name("tiktok").display_name == "TikTok"


def test_post_url_is_the_shape_yt_dlp_accepts() -> None:
    assert post_url(POST_ID, "nasa") == f"https://www.tiktok.com/@nasa/video/{POST_ID}"
    assert post_url(POST_ID) == f"https://www.tiktok.com/@_/video/{POST_ID}"
    assert post_url(POST_ID, "bad/handle") == f"https://www.tiktok.com/@_/video/{POST_ID}"
    # Asked of the installed yt-dlp itself, for every handle shape post_url() can produce.
    for user in ("nasa", "the.user_1", "a-b", None, "bad/handle"):
        url = post_url(POST_ID, user)
        assert TikTokIE.suitable(url), url
        assert TikTokIE._match_valid_url(url).group("id") == POST_ID


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.tiktok.com/@nasa/photo/{POST_ID}",
        f"https://tiktok.com/@nasa/video/{POST_ID}",
        f"https://www.tiktok.com/embed/v2/{POST_ID}",
        f"https://m.tiktok.com/v/{POST_ID}.html",
    ],
)
def test_link_forms_yt_dlp_does_not_know_are_rewritten(url: str) -> None:
    """Why post_url() exists: yt-dlp refuses these, the rewritten link works."""
    link = parse_link(url)
    assert link is not None and link.post_id == POST_ID
    assert not TikTokIE.suitable(url)
    assert TikTokIE.suitable(post_url(link.post_id, link.user))


@pytest.mark.parametrize(
    ("path", "needle"),
    [
        ("/@nasa", "profile"),
        ("/music/City-Nights-1", "sound page"),
        ("/@nasa/live", "live streams"),
        ("/live", "live streams"),
        ("/@nasa/collection/space-1", "isn't a video or photo post"),
        ("/", "isn't a video or photo post"),
    ],
)
def test_unsupported_message(path: str, needle: str) -> None:
    assert needle in unsupported_message(TikTokLink("other", path=path))


# ----------------------------------------------------------------------------------------------
# Naming
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "track",
    [
        "original sound",
        "Original Sound",
        "original sound - NASA",
        "original sound – someone else",
        "оригинальный звук - Кто-то",
        "son original - quelqu'un",
        "Promoted Music",
        "  ",
    ],
)
def test_generic_sounds(track: str) -> None:
    assert is_generic_sound(track, "Hank Green", "hankgreen1")


def test_a_sound_named_after_the_creator_is_generic() -> None:
    assert is_generic_sound("som da NASA - NASA", "NASA", "nasa")
    assert is_generic_sound("my voice - hankgreen1", "Hank Green", "hankgreen1")


@pytest.mark.parametrize(
    "track",
    ["Strings", "City Nights - Instrumental", "Where Is My Mind?", "original soundtrack theme"],
)
def test_named_sounds(track: str) -> None:
    assert not is_generic_sound(track, "NASA", "nasa")


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        ("Ready for liftoff? #space #nasa", "Ready for liftoff?"),
        ("#prank ##funny#fyp #chihuahua What is THAT??", "What is THAT??"),
        ("It’s all connected #saturnworship #worshippingsaturn #saturn ", "It’s all connected"),
        ("Replying to @AlaniDaGoat Do you like Shelly’s costume?", "Do you like Shelly’s costume?"),
        ("#only #hashtags\nThe real title\nmore text", "The real title"),
        ("I code in C# all day", "I code in C# all day"),
        ("😸😸😸", "😸😸😸"),
        ("Zero​width‏ chars", "Zerowidth chars"),
        ("#fyp #foryou", ""),
        ("", ""),
        (
            "Going back to the Moon means figuring out how to get around. 🌕 Here’s a look at "
            "the vehicles that will help us explore",
            "Going back to the Moon means figuring out how to get around.",
        ),
        # A tag ends where TikTok ends it; the sentence keeps its punctuation and emoji.
        ("Happy birthday #mom! Love you so much", "Happy birthday! Love you so much"),
        ("Best day ever #summer. Next year again!", "Best day ever. Next year again!"),
        ("My cat #cat😻 is the best", "My cat 😻 is the best"),
        ("Hello (#fyp) world", "Hello () world"),
        ("मेरा गाना #प्यारहै आज", "मेरा गाना आज"),  # a Devanagari tag with its vowel signs
        ("เพลงนี้ #เพลงไทย ดีมาก", "เพลงนี้ ดีมาก"),  # Thai
        ("Tom &#38; Jerry #tag", "Tom &#38; Jerry"),  # "&#" starts no tag
        # A tag glued to a word is still a tag (TikTok links it); "C#" with nothing after is not.
        ("СЕРИАЛ: МЕНТАЛИСТ#recommendations", "СЕРИАЛ: МЕНТАЛИСТ"),
        ("Dance#fyp#viral time", "Dance time"),
        ("Hello# world", "Hello# world"),
        # A musical key is not a tag, glued "#" or not.
        ("Piano cover in C#m #piano", "Piano cover in C#m"),
        ("F#minor etude", "F#minor etude"),
        ("Bach Prelude in C#major", "Bach Prelude in C#major"),
        ("Riff in G#7 and D#sus4", "Riff in G#7 and D#sus4"),
        ("price #1 #fyp", "price"),
        # A tag at the start of a line, or after a finished sentence, takes its punctuation along.
        ("#mom! Love you", "Love you"),
        ("#tbt. Summer vibes", "Summer vibes"),
        ("Great day. #blessed! Next week again", "Great day. Next week again"),
        # A first line with nothing left to read gives way to the next one.
        ("#fyp #viral.\nMy cover of Yesterday", "My cover of Yesterday"),
        ("(#ad)\nMy new song is out", "My new song is out"),
        ("⠀\nReal caption here", "Real caption here"),  # Braille blank as an "empty" line
        ("ㅤ", ""),  # Hangul filler as an "empty" caption
        # A filler used as a spacer is a space, also to the tag and @mention rules.
        ("ㅤ#fyp #viral\nReal caption", "Real caption"),
        ("#fypㅤLove this", "Love this"),
        ("Love this songㅤ#fyp", "Love this song"),
        ("Replying to @xㅤHello", "Hello"),
        ("ㅤ‍Hello", "Hello"),  # the joiner the filler leaves behind joins nothing
        # Punctuation glued to more than the tag stays: ":D" is an emoticon.
        ("Done! #tbt:D love", "Done!:D love"),
        ("So happy! #blessed;) see you", "So happy!;) see you"),
        # Tags spelled with a zero-width joiner go entirely.
        ("Beautiful day #ශ්‍රීලංකා #fyp", "Beautiful day"),
        ("#می‌خواهم", ""),
        # Emoji sequences and Persian spelling keep their (zero-width) joiners.
        ("Pride 🏳️‍🌈 #pride", "Pride 🏳️‍🌈"),
        ("Family 👨‍👩‍👧", "Family 👨‍👩‍👧"),
        ("می‌خواهم", "می‌خواهم"),
    ],
)
def test_clean_caption(caption: str, expected: str) -> None:
    assert clean_caption(caption) == expected


def test_clean_caption_keeps_a_word_that_ends_at_the_limit() -> None:
    caption = "We drove all the way down to the coast to watch the sunrise, worth it every time"
    caption += " and again tomorrow"
    assert caption[CAPTION_MAX_LEN] == " "  # "time" ends exactly at the limit
    assert clean_caption(caption) == caption[:CAPTION_MAX_LEN]


def test_clean_caption_hard_cuts_one_endless_word() -> None:
    caption = "a" * 200
    assert clean_caption(caption) == "a" * CAPTION_MAX_LEN


def test_clean_caption_cuts_long_text_at_a_word() -> None:
    caption = "word " * 40
    cleaned = clean_caption(caption)
    assert 0 < len(cleaned) <= CAPTION_MAX_LEN
    assert cleaned.endswith("word") and "  " not in cleaned


def test_clean_caption_ignores_an_early_sentence_end() -> None:
    """ "Wow. " is too short to be the whole title; the cut happens at a word instead."""
    caption = "Wow. " + "this is a very long caption without any other sentence end " * 3
    cleaned = clean_caption(caption)
    assert cleaned.startswith("Wow. this is") and len(cleaned) <= CAPTION_MAX_LEN


def test_caption_title_fallbacks() -> None:
    assert caption_title({"id": "1", "description": "Hello #fyp"}) == "Hello"
    # No description: yt-dlp's title is the caption cut to 72 characters, fine as a fallback ...
    assert caption_title({"id": "1", "title": "Short caption"}) == "Short caption"
    # ... unless it is yt-dlp's placeholder for a post without a caption.
    assert caption_title({"id": "7047", "title": "TikTok video #7047"}) == "TikTok post 7047"
    assert caption_title({"id": "7047", "description": "#fyp"}) == "TikTok post 7047"
    assert caption_title({"description": None}) == "TikTok post"


@pytest.mark.parametrize("caption", ["...", "?", "?!", "***", ". . .", "(#fyp)"])
def test_a_caption_of_only_punctuation_is_no_title(caption: str) -> None:
    """Otherwise the file would be "NASA -.mp3" (safe_filename drops "?" and trailing dots)."""
    assert caption_title({"id": "7047", "description": caption}) == "TikTok post 7047"
    assert caption_title({"id": "7047", "description": "😸"}) == "😸"  # an emoji is content


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["Pixies"], "Pixies"),
        (["Simon", "Garfunkel"], "Simon & Garfunkel"),
        (["Mumford", "Sons"], "Mumford & Sons"),
        (["Earth", "Wind", "Fire"], "Earth, Wind & Fire"),
        (["Crosby", "Stills", "Nash", "Young"], "Crosby, Stills, Nash & Young"),
    ],
)
def test_join_artists_undoes_yt_dlps_split(names: list[str], expected: str) -> None:
    assert join_artists(names) == expected


def test_pick_tags_named_sound() -> None:
    info = {
        "id": POST_ID,
        "channel": "NASA",
        "uploader": "nasa",
        "description": "Going back to the Moon",
        "track": "Strings",
        "artists": ["柏恩"],
        "artist": "柏恩",
    }
    assert pick_tags(info) == ("柏恩", "Strings", None)
    info.update(track="Where Is My Mind?", artists=["Pixies", "Pixies", " "], album="Doolittle")
    assert pick_tags(info) == ("Pixies", "Where Is My Mind?", "Doolittle")
    info.update(track="The Boxer", artists=["Simon", "Garfunkel"], artist="Simon, Garfunkel")
    assert pick_tags(info)[0] == "Simon & Garfunkel"  # yt-dlp split "Simon & Garfunkel"
    info.update(artists=None, artist="Pixies")
    assert pick_tags(info)[0] == "Pixies"  # only the joined compat field
    info.update(artists=None, artist=None)
    assert pick_tags(info) == ("NASA", "The Boxer", "Doolittle")  # sound without artist


def test_pick_tags_original_sound_uses_creator_and_caption() -> None:
    info = {
        "id": "7702679553667304718",
        "channel": "tate mcrae",
        "uploader": "tatemcrae",
        "description": "Happy halloweeeeeeeeen #halloween",
        "track": "original sound",
        "artists": ["nutella"],
        "album": "ignored for an original sound",
    }
    assert pick_tags(info) == ("tate mcrae", "Happy halloweeeeeeeeen", None)
    info.update(track="original sound - NASA")
    assert pick_tags(info)[1] == "Happy halloweeeeeeeeen"
    info.pop("track")
    assert pick_tags(info) == ("tate mcrae", "Happy halloweeeeeeeeen", None)
    info.update(channel=None)
    assert pick_tags(info)[0] == "tatemcrae"  # no display name: the @handle
    info.update(channel="ㅤ")
    assert pick_tags(info)[0] == "tatemcrae"  # a "blank" display name is no name either
    info.update(channel="♡")
    assert pick_tags(info)[0] == "tatemcrae"  # only symbols: the @handle reads better
    info.update(channel="♡", uploader=None)
    assert pick_tags(info)[0] == "♡"  # unless there is nothing else
    info.update(channel="Anna ♡", uploader="anna")
    assert pick_tags(info)[0] == "Anna ♡"
    assert pick_tags({"id": "1"}) == ("TikTok", "TikTok post 1", None)


# ----------------------------------------------------------------------------------------------
# Error texts
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        ("ERROR: [TikTok] 1: Your IP address is blocked from accessing this post", "your country"),
        ("[TikTok] 1: Your IP address is blocked from accessing this post", "VPN for the whole"),
        (
            "[TikTok] 1: You do not have permission to view this post. Log into an account "
            "that has access",
            "private",
        ),
        ("[TikTok] 1: TikTok is requiring login for access to this content", "signed-in"),
        ("This post may not be comfortable for some audiences. Log in for access", "signed-in"),
        ("[TikTok] 1: Video not available, status code 10231", "deleted"),
        ("[TikTok] 1: Unable to extract universal data for rehydration", "newer yt-dlp"),
        # TikTok moved its data: no data script at all, or one without the post (status 0).
        ("[TikTok] 1: Unexpected response from webpage request", "newer yt-dlp"),
        ("[TikTok] 1: Video not available, status code 0", "newer yt-dlp"),
        ("[TikTok] 1: Requested format is not available", "download it as Audio"),
        ("[TikTok] 1: No video formats found!", "anything to download"),
        ("ERROR: Unsupported URL: https://www.tiktok.com/?_r=1", "doesn't lead to a video"),
        ("[Errno 11001] getaddrinfo failed", "Couldn't reach TikTok"),
        ("Postprocessing: audio conversion failed: ffmpeg exited", "Converting the download"),
        ("unable to download video data: HTTP Error 403: Forbidden", "refused to serve"),
        ("HTTP Error 429: Too Many Requests", "rate-limiting"),
        ("HTTP Error 404: Not Found", "deleted"),
        ("HTTP Error 503: Service Unavailable", "problems right now"),
        ("something nobody has seen before", "TikTok download failed."),
    ],
)
def test_friendly_error(raw: str, needle: str) -> None:
    message = friendly_error(raw)
    assert needle in message
    assert "YouTube" not in message


def test_friendly_error_keeps_the_original_text_short() -> None:
    message = friendly_error(yt_dlp.utils.DownloadError("ERROR: " + "x" * 500))
    assert message.startswith("TikTok download failed. (")
    assert len(message) < 260 and message.endswith("…)")
    assert friendly_error("") == "TikTok download failed."


# ----------------------------------------------------------------------------------------------
# resolve() / download() with a fake yt-dlp
# ----------------------------------------------------------------------------------------------


class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL: `result` is the info dict (or an exception to raise);
    `on_extract(ydl, url)` runs first (a download writes the produced file there)."""

    instances: list[FakeYoutubeDL] = []
    result: Any = None
    on_extract: Any = None

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
        if isinstance(FakeYoutubeDL.result, BaseException):
            raise FakeYoutubeDL.result
        return FakeYoutubeDL.result


@pytest.fixture
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> type[FakeYoutubeDL]:
    FakeYoutubeDL.instances = []
    FakeYoutubeDL.result = None
    FakeYoutubeDL.on_extract = None
    monkeypatch.setattr(youtube, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


class FakeHead:
    """Stands in for requests.head: records the call, answers with `target` (or raises)."""

    def __init__(
        self, target: str | BaseException, status: int = 200, redirected: bool = True
    ) -> None:
        self.target = target
        self.status = status
        self.redirected = redirected
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        if isinstance(self.target, BaseException):
            raise self.target
        fields = {
            "url": self.target,
            "status_code": self.status,
            "history": [object()] if self.redirected else [],
        }
        return type("Response", (), fields)()


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> FakeHead:
    """A HEAD request is a test failure unless the test installs its own FakeHead."""
    head = FakeHead(AssertionError("unexpected HEAD request"))
    monkeypatch.setattr(tiktok.requests, "head", head)
    return head


NAMED_SOUND_INFO: dict[str, Any] = {
    "id": POST_ID,
    "uploader": "nasa",
    "channel": "NASA",
    "description": "Going back to the Moon means figuring out how to get around. #moon",
    "track": "Strings",
    "artists": ["柏恩"],
    "duration": 45,
    "thumbnails": [{"url": "https://p16/cover.jpeg"}, {"url": "https://p16/origin.jpeg"}],
}


def test_resolve_a_post(fake_ydl: type[FakeYoutubeDL], no_network: FakeHead) -> None:
    fake_ydl.result = dict(NAMED_SOUND_INFO)
    refs = TikTokProvider().resolve(f"  tiktok.com/@nasa/photo/{POST_ID}?lang=en ")
    assert len(refs) == 1
    ref = refs[0]
    assert ref.provider == "tiktok" and ref.source_id == POST_ID
    assert ref.track_id == f"tiktok:{POST_ID}"
    assert ref.url == f"https://www.tiktok.com/@nasa/video/{POST_ID}"
    assert (ref.artist, ref.title, ref.album) == ("柏恩", "Strings", None)
    assert ref.duration == 45.0
    assert ref.thumbnail_url == "https://p16/origin.jpeg"
    assert ref.extra == {"original_url": f"https://tiktok.com/@nasa/photo/{POST_ID}?lang=en"}

    (ydl,) = fake_ydl.instances
    # A /photo/ link is fetched through the /video/ address yt-dlp knows.
    assert ydl.calls == [(f"https://www.tiktok.com/@nasa/video/{POST_ID}", False)]
    assert ydl.opts["skip_download"] is True and ydl.opts["noplaylist"] is True
    assert no_network.calls == []


def test_resolve_names_the_post_after_its_real_uploader(fake_ydl: type[FakeYoutubeDL]) -> None:
    fake_ydl.result = dict(NAMED_SOUND_INFO, uploader="nasa")
    (ref,) = TikTokProvider().resolve(f"https://www.tiktok.com/share/video/{POST_ID}")
    assert fake_ydl.instances[0].calls[0][0] == f"https://www.tiktok.com/@_/video/{POST_ID}"
    assert ref.url == f"https://www.tiktok.com/@nasa/video/{POST_ID}"


def test_resolve_expands_a_short_link(
    fake_ydl: type[FakeYoutubeDL], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = FakeHead(f"https://www.tiktok.com/@/video/{POST_ID}?_r=1&u_code=secret")
    monkeypatch.setattr(tiktok.requests, "head", head)
    fake_ydl.result = dict(NAMED_SOUND_INFO)
    (ref,) = TikTokProvider().resolve("https://vt.tiktok.com/ZS9c687pa/")
    assert ref.source_id == POST_ID
    assert ref.url == f"https://www.tiktok.com/@nasa/video/{POST_ID}"
    assert ref.extra["original_url"] == "https://vt.tiktok.com/ZS9c687pa/"
    ((url, kwargs),) = head.calls
    assert url == "https://vt.tiktok.com/ZS9c687pa"  # rebuilt from the checked host and path
    assert kwargs["allow_redirects"] is True and kwargs["timeout"] > 0
    assert kwargs["headers"]["User-Agent"] == "facebookexternalhit/1.1"
    assert fake_ydl.instances[0].calls == [(f"https://www.tiktok.com/@_/video/{POST_ID}", False)]


def test_resolve_a_short_link_to_a_deleted_post(
    fake_ydl: type[FakeYoutubeDL],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """TikTok sends a short link whose post is gone to its home page."""
    monkeypatch.setattr(tiktok.requests, "head", FakeHead("https://www.tiktok.com/?_r=1&u=x"))
    with caplog.at_level(logging.INFO, logger="ultimate_playlist.providers.tiktok"):
        with pytest.raises(ProviderError, match="doesn't lead to a video or photo post any more"):
            TikTokProvider().resolve("https://vm.tiktok.com/ZTR45GpSF/")
    assert fake_ydl.instances == []
    assert "u=x" not in caplog.text  # the share-tracking query stays out of the log


def test_resolve_a_short_link_without_network(
    fake_ydl: type[FakeYoutubeDL], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = FakeHead(requests.ConnectionError("no route"))
    monkeypatch.setattr(tiktok.requests, "head", head)
    with pytest.raises(ProviderError, match="Couldn't reach TikTok"):
        TikTokProvider().resolve("https://vm.tiktok.com/ZMabc123/")
    monkeypatch.setattr(tiktok.requests, "head", FakeHead(requests.TooManyRedirects("loop")))
    with pytest.raises(ProviderError, match="doesn't lead to a video or photo post any more"):
        TikTokProvider().resolve("https://vm.tiktok.com/ZMabc123/")


@pytest.mark.parametrize(
    ("target", "needle"),
    [
        ("https://www.tiktok.com/@nasa?_t=8abc&_r=1", "profile"),
        ("https://www.tiktok.com/music/City-Nights-7036843036118469381?_r=1", "sound page"),
        ("https://www.tiktok.com/@nasa/live?_r=1", "live streams"),
        ("https://www.tiktok.com/@nasa/collection/space-123?_r=1", "isn't a video or photo post"),
        ("https://www.tiktok.com/?_r=1", "any more"),  # TikTok's home page: the post is gone
        ("https://vm.tiktok.com/ZMabc123/", "any more"),  # no redirect at all
        ("https://example.com/somewhere", "any more"),
    ],
)
def test_short_links_to_pages_that_are_not_posts(
    fake_ydl: type[FakeYoutubeDL], monkeypatch: pytest.MonkeyPatch, target: str, needle: str
) -> None:
    """A profile or a sound shared from the app is a short link too: it gets the same
    explanation as its long link, not "deleted"."""
    monkeypatch.setattr(tiktok.requests, "head", FakeHead(target))
    with pytest.raises(ProviderError, match=needle):
        TikTokProvider().resolve("https://vm.tiktok.com/ZMabc123/")
    assert fake_ydl.instances == []


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        (429, "rate-limiting"),
        (503, "problems right now"),
        (404, "any more"),
        (403, "wouldn't open that short link (HTTP 403)"),
    ],
)
def test_a_short_link_answered_with_an_error(
    fake_ydl: type[FakeYoutubeDL], monkeypatch: pytest.MonkeyPatch, status: int, needle: str
) -> None:
    head = FakeHead("https://vm.tiktok.com/ZMabc123", status=status, redirected=False)
    monkeypatch.setattr(tiktok.requests, "head", head)
    with pytest.raises(ProviderError, match=re.escape(needle)):
        TikTokProvider().resolve("https://vm.tiktok.com/ZMabc123/")


def test_a_short_link_redirected_to_an_error_page_still_counts(
    fake_ydl: type[FakeYoutubeDL], monkeypatch: pytest.MonkeyPatch
) -> None:
    """TikTok answers a plain HEAD on the post page with 403 after the redirect; the post id is
    in the redirect's URL, so that is a success."""
    head = FakeHead(f"https://www.tiktok.com/@/video/{POST_ID}?_r=1", status=403)
    monkeypatch.setattr(tiktok.requests, "head", head)
    fake_ydl.result = dict(NAMED_SOUND_INFO)
    (ref,) = TikTokProvider().resolve("https://vm.tiktok.com/ZMabc123/")
    assert ref.source_id == POST_ID


@pytest.mark.parametrize(
    "pasted",
    [
        r"https://evil.example\@vm.tiktok.com/ZMabc123",
        r"evil.example\@vm.tiktok.com/ZMabc123",
        "https://user:pw@vm.tiktok.com:8443/ZMabc123/",
    ],
)
def test_a_short_link_is_only_ever_sent_to_tiktok(
    fake_ydl: type[FakeYoutubeDL], monkeypatch: pytest.MonkeyPatch, pasted: str
) -> None:
    """urlsplit and requests disagree on where a backslash ends the host; the HEAD request goes
    to the host that was checked, rebuilt without userinfo or port."""
    head = FakeHead("https://www.tiktok.com/?_r=1")
    monkeypatch.setattr(tiktok.requests, "head", head)
    with pytest.raises(ProviderError):
        TikTokProvider().resolve(pasted)
    assert [url for url, _ in head.calls] == ["https://vm.tiktok.com/ZMabc123"]


def test_resolve_explains_pages_that_are_not_posts(
    fake_ydl: type[FakeYoutubeDL], no_network: FakeHead
) -> None:
    with pytest.raises(ProviderError, match="profile"):
        TikTokProvider().resolve("https://www.tiktok.com/@nasa")
    with pytest.raises(ProviderError, match="sound page"):
        TikTokProvider().resolve("https://www.tiktok.com/music/City-Nights-1")
    with pytest.raises(ProviderError, match="isn't a TikTok link"):
        TikTokProvider().resolve("https://example.com/")
    assert fake_ydl.instances == [] and no_network.calls == []


def test_resolve_errors_are_friendly(fake_ydl: type[FakeYoutubeDL], no_network: FakeHead) -> None:
    fake_ydl.result = yt_dlp.utils.DownloadError(
        "ERROR: [TikTok] 1: You do not have permission to view this post"
    )
    with pytest.raises(ProviderError, match="private"):
        TikTokProvider().resolve(f"https://www.tiktok.com/@a/video/{POST_ID}")

    fake_ydl.result = None  # yt-dlp reported the error through the logger instead

    def log_error(ydl: FakeYoutubeDL, url: str) -> None:
        ydl.opts["logger"].error("ERROR: [TikTok] 1: Video not available, status code 10231")

    fake_ydl.on_extract = log_error
    with pytest.raises(ProviderError, match="deleted"):
        TikTokProvider().resolve(f"https://www.tiktok.com/@a/video/{POST_ID}")

    fake_ydl.on_extract = None
    fake_ydl.result = RuntimeError("boom")
    with pytest.raises(ProviderError, match="Could not read that TikTok link: boom"):
        TikTokProvider().resolve(f"https://www.tiktok.com/@a/video/{POST_ID}")


def _ref() -> TrackRef:
    return TrackRef(
        provider="tiktok", source_id=POST_ID, url=f"https://www.tiktok.com/@nasa/video/{POST_ID}"
    )


def _fake_download(info: dict[str, Any]) -> Any:
    """An on_extract callback that behaves like a successful yt-dlp run producing an MP3."""

    def on_extract(ydl: FakeYoutubeDL, url: str) -> None:
        out = Path(ydl.opts["outtmpl"] % {"id": info["id"], "ext": "mp3"})
        write_tiny_mp3(out, artist="yt", title="yt")
        (out.parent / f"{info['id']}.mp4.part").write_bytes(b"partial")
        for hook in ydl.opts["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})
            hook({"status": "finished", "filename": str(out)})

    return on_extract


def test_download_names_and_tags_the_post(fake_ydl: type[FakeYoutubeDL], tmp_path: Path) -> None:
    settings = Settings(library_dir=tmp_path / "lib")
    info = dict(NAMED_SOUND_INFO, track="original sound - NASA")
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info)
    events: list[ProgressEvent] = []

    track = TikTokProvider().download(
        _ref(), settings.library_dir, settings, events.append, threading.Event()
    )

    name = "NASA - Going back to the Moon means figuring out how to get around.mp3"
    final = settings.library_dir / name
    assert final.is_file()
    assert track.path == name
    assert (track.id, track.provider, track.source_id) == (f"tiktok:{POST_ID}", "tiktok", POST_ID)
    assert (track.artist, track.title, track.album) == (
        "NASA",
        "Going back to the Moon means figuring out how to get around.",
        None,
    )
    assert track.source_url == f"https://www.tiktok.com/@nasa/video/{POST_ID}"
    tags = ID3(str(final))
    assert tags["TPE1"].text == ["NASA"]
    assert tags.getall("TXXX:ULTIMATE_PLAYLIST_ID")[0].text == [f"tiktok:{POST_ID}"]
    assert tags.getall("COMM")[0].text == [f"https://www.tiktok.com/@nasa/video/{POST_ID}"]
    assert list((settings.library_dir / ".incoming").iterdir()) == []
    assert JobStatus.CONVERTING in [e.status for e in events]

    (ydl,) = fake_ydl.instances
    assert ydl.calls == [(f"https://www.tiktok.com/@nasa/video/{POST_ID}", True)]
    assert ydl.opts["format"] == YDL_FORMAT  # what the yt-dlp selection tests below exercise


def _fmt(format_id: str, vcodec: str, **extra: Any) -> dict[str, Any]:
    """A format shaped like the ones yt-dlp's TikTok web extractor builds."""
    video = vcodec != "none"
    return {
        "format_id": format_id,
        "url": f"https://v16-webapp.tiktok.example/{format_id}/{len(extra)}",
        "ext": "mp4" if video else "m4a",
        "vcodec": vcodec,
        "acodec": "aac",
        **({"width": 576, "height": 1024} if video else {}),
        **extra,
    }


_VIDEOS = [
    _fmt("h264_540p_815015-0", "h264", tbr=815),
    _fmt("h264_540p_1895168-0", "h264", tbr=1895),
    _fmt("bytevc1_720p_606824-0", "h265", tbr=606, width=720, height=1280),
]
_WATERMARKED = _fmt("download", "h264", format_note="watermarked", preference=-2)
_SOUND = _fmt("audio", "none")


def _chosen(formats: list[dict[str, Any]]) -> str:
    """The format yt-dlp itself picks for YDL_FORMAT: its sorting, its renaming of duplicate
    ids and its selection all run; nothing is downloaded or probed."""
    info = {
        "id": POST_ID,
        "title": "t",
        "extractor": "TikTok",
        "extractor_key": "TikTok",
        "webpage_url": f"https://www.tiktok.com/@nasa/video/{POST_ID}",
        "formats": [dict(f) for f in formats],
    }
    opts = {"quiet": True, "simulate": True, "check_formats": False, "format": YDL_FORMAT}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return str(ydl.process_ie_result(info, download=False)["format_id"])


def test_format_a_video_gives_its_own_soundtrack() -> None:
    chosen = _chosen([*_VIDEOS, _WATERMARKED, _SOUND])
    assert chosen in {f["format_id"] for f in _VIDEOS}  # not the outro'd copy, not the sound


def test_format_a_photo_slideshow_gives_its_sound() -> None:
    assert _chosen([_SOUND]) == "audio"


def test_format_two_watermarked_copies_lose_to_the_sound() -> None:
    """Two different watermarked URLs both come as "download"; yt-dlp renames them
    "download-0" / "download-1" before choosing, which an exact != filter would miss."""
    second = _fmt("download", "h264", format_note="watermarked", preference=-2, other=1)
    assert _chosen([_WATERMARKED, second, _SOUND]) == "audio"


def test_format_the_watermarked_copy_is_the_last_resort() -> None:
    assert _chosen([_WATERMARKED]) == "download"


def test_download_errors_are_tiktok_errors(fake_ydl: type[FakeYoutubeDL], tmp_path: Path) -> None:
    settings = Settings(library_dir=tmp_path / "lib")
    fake_ydl.result = yt_dlp.utils.DownloadError(
        "ERROR: [TikTok] 1: Your IP address is blocked from accessing this post"
    )
    with pytest.raises(ProviderError, match="your country") as caught:
        TikTokProvider().download(
            _ref(), settings.library_dir, settings, lambda e: None, threading.Event()
        )
    assert "YouTube" not in str(caught.value)

    fake_ydl.result = None
    with pytest.raises(ProviderError, match="TikTok returned no information"):
        TikTokProvider().download(
            _ref(), settings.library_dir, settings, lambda e: None, threading.Event()
        )


def test_youtube_downloads_keep_their_own_format(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    """The format hook is per provider: YouTube still asks for the best audio-only stream."""
    settings = Settings(library_dir=tmp_path / "lib")
    info = {"id": "jNQXAC9IVRw", "title": "Artist - Song", "channel": "c"}
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info)
    ref = TrackRef(provider="youtube", source_id="jNQXAC9IVRw", url="https://youtu.be/x")
    youtube.YouTubeProvider().download(
        ref, settings.library_dir, settings, lambda e: None, threading.Event()
    )
    assert fake_ydl.instances[0].opts["format"] == "bestaudio/best"


# ----------------------------------------------------------------------------------------------
# doctor() and logging
# ----------------------------------------------------------------------------------------------


def test_doctor_needs_ffmpeg_yt_dlp_and_impersonation() -> None:
    checks = TikTokProvider(Settings()).doctor()
    assert [label for _, label, _ in checks] == ["ffmpeg", "yt-dlp", "Browser impersonation"]
    assert all(ok for ok, _, _ in checks)  # conftest stubs ffmpeg; curl_cffi is a dependency
    assert checks[2][2].startswith("curl_cffi ")


def test_impersonation_is_a_real_dependency() -> None:
    """yt-dlp loads its curl_cffi handler (installed, in a supported version) and offers
    browser targets; without it TikTok refuses more posts as "blocked"."""
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        assert ydl._get_available_impersonate_targets()


def test_doctor_explains_missing_impersonation(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = tiktok.importlib.import_module

    def no_curl_cffi(name: str, *args: Any) -> Any:
        if name == "yt_dlp.networking._curlcffi":
            raise ImportError("curl_cffi is not installed")
        return real_import(name, *args)

    monkeypatch.setattr(tiktok.importlib, "import_module", no_curl_cffi)
    ok, label, detail = tiktok.impersonation_check()
    assert (ok, label) == (False, "Browser impersonation")
    assert "curl_cffi is not installed" in detail and "uv sync" in detail

    def broken(name: str, *args: Any) -> Any:
        raise RuntimeError("weird")

    monkeypatch.setattr(tiktok.importlib, "import_module", broken)
    assert tiktok.impersonation_check() == (
        False,
        "Browser impersonation",
        "Could not check: weird",
    )


def test_doctor_reports_a_missing_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    from ultimate_playlist.ffmpeg import FfmpegInfo, install_hint

    missing = FfmpegInfo(path=None, version=None)
    monkeypatch.setattr(youtube, "find_ffmpeg", lambda explicit=None: missing)
    (ffmpeg, *_) = TikTokProvider(Settings()).doctor()
    assert ffmpeg == (False, "ffmpeg", install_hint())

    def broken(explicit: str | None = None) -> FfmpegInfo:
        raise OSError("probe exploded")

    monkeypatch.setattr(youtube, "find_ffmpeg", broken)
    (ffmpeg, *_) = TikTokProvider(Settings()).doctor()  # doctor never raises
    assert ffmpeg == (False, "ffmpeg", "Could not check ffmpeg: probe exploded")


def test_impersonation_notice_is_not_a_console_warning(caplog: pytest.LogCaptureFixture) -> None:
    """yt-dlp asks for curl_cffi on every TikTok page; that must not reach the console."""
    logger = youtube._YdlLogger()
    with caplog.at_level(logging.DEBUG, logger="ultimate_playlist.youtube"):
        logger.warning(
            "WARNING: [TikTok] The extractor is attempting impersonation, but no impersonate "
            "target is available."
        )
        logger.warning("WARNING: something else")
    levels = {r.getMessage()[:20]: r.levelno for r in caplog.records}
    assert levels["[TikTok] The extract"] == logging.DEBUG
    assert levels["something else"] == logging.WARNING
