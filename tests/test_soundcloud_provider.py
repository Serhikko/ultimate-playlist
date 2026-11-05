"""The SoundCloud provider: link shapes, short links, naming, error texts, resolve() and
download() with a fake yt-dlp, and the format and artwork choices made by the real yt-dlp.
Offline: yt-dlp's network side and the short-link HEAD request are replaced."""

from __future__ import annotations

import gc
import logging
import re
import shutil
import threading
import weakref
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
import yt_dlp
import yt_dlp.utils
from fake_provider import FakeRegistry, write_tiny_mp3
from mutagen.id3 import ID3
from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.extractor.soundcloud import (
    SoundcloudEmbedIE,
    SoundcloudIE,
    SoundcloudPlaylistIE,
    SoundcloudSetIE,
    SoundcloudUserIE,
)

from ultimate_playlist import providers
from ultimate_playlist.config import Settings
from ultimate_playlist.downloader import JobManager
from ultimate_playlist.library import Library
from ultimate_playlist.models import Job, JobStatus, ProgressEvent, TrackRef
from ultimate_playlist.providers import soundcloud, youtube
from ultimate_playlist.providers.base import DownloadCancelled, ProviderError
from ultimate_playlist.providers.soundcloud import (
    GO_PLUS,
    SHORT_LINK_DEAD,
    YDL_FORMAT,
    SoundCloudLink,
    SoundCloudProvider,
    TrimArtworkPP,
    clean_sc_title,
    friendly_error,
    is_preview_only,
    parse_link,
    pick_tags,
    trim_artwork,
    unsupported_message,
)

WAIT = 15.0
TRACK_URL = "https://soundcloud.com/the80m/the-following"
PRIVATE_URL = "https://soundcloud.com/jaimemf/youtube-dl-test-video-a-y-baw/s-8Pjrp"
SET_URL = "https://soundcloud.com/leviryan/sets/out-of-spite"
EP_URL = "https://soundcloud.com/the-concept-band/sets/the-royal-concept-ep"


# ----------------------------------------------------------------------------------------------
# Real data (trimmed info dicts that yt-dlp 2026.08.19 returned for these links)
# ----------------------------------------------------------------------------------------------


def _artwork(base: str) -> list[dict[str, Any]]:
    """SoundCloud's artwork list as yt-dlp builds it: nine sizes, then the original upload."""
    sizes = {
        "mini": 16,
        "tiny": 20,
        "small": 32,
        "badge": 47,
        "t67x67": 67,
        "large": 100,
        "t300x300": 300,
        "crop": 400,
        "t500x500": 500,
    }
    thumbs: list[dict[str, Any]] = [
        {
            "id": key,
            "url": f"https://i1.sndcdn.com/artworks-{base}-{key}.jpg",
            "width": s,
            "height": s,
        }
        for key, s in sizes.items()
    ]
    thumbs.append(
        {
            "id": "original",
            "url": f"https://i1.sndcdn.com/artworks-{base}-original.jpg",
            "preference": 10,
        }
    )
    return thumbs


def _fmt(
    format_id: str,
    ext: str,
    acodec: str,
    abr: int,
    protocol: str,
    quality: int,
    preference: int | None = None,
) -> dict[str, Any]:
    """A format shaped like the ones yt-dlp's SoundCloud extractor builds."""
    return {
        "format_id": format_id,
        "url": f"https://cf-media.sndcdn.example/{format_id}",
        "ext": ext,
        "acodec": acodec,
        "vcodec": "none",
        "abr": abr,
        "protocol": protocol,
        "quality": quality,
        "preference": preference,
        **({"container": "m4a_dash"} if ext == "m4a" else {}),
    }


AAC_FORMATS = [
    _fmt("hls_aac_96k", "m4a", "mp4a.40.2", 96, "m3u8_native", -1),
    _fmt("hls_aac_160k", "m4a", "mp4a.40.2", 160, "m3u8_native", 0),
]
MP3_FORMATS = [  # legacy and private tracks
    _fmt("hls_mp3_0_0", "mp3", "mp3", 128, "m3u8_native", -1),
    _fmt("http_mp3_0_0", "mp3", "mp3", 128, "http", -1),
]
PREVIEW_FORMATS = [  # a Go+ track without a subscription: 30 seconds, nothing else
    _fmt("hls_mp3_0_1_preview", "mp3", "mp3", 128, "m3u8_native", -1, -10),
    _fmt("http_mp3_0_1_preview", "mp3", "mp3", 128, "http", -1, -10),
]

TRACK_80M: dict[str, Any] = {
    "id": "343609555",
    "title": "The Following",
    "track": "The Following",
    "artist": "80M",
    "artists": ["80M"],
    "uploader": "80M",
    "uploader_id": "312384765",
    "webpage_url": TRACK_URL,
    "duration": 397.175,
    "genres": ["Dance & EDM"],
    "formats": AAC_FORMATS,
    "thumbnails": _artwork("000245155417-0jqdd2"),
}
TRACK_WW: dict[str, Any] = {
    "id": "257939729",
    "title": "The Chainsmokers ft. Daya - Don't Let Me Down (W&W Remix)",
    "track": "The Chainsmokers ft. Daya - Don't Let Me Down (W&W Remix)",
    "artist": None,
    "artists": None,
    "uploader": "W&W",
    "webpage_url": "https://soundcloud.com/wandw/the-chainsmokers-ft-daya-dont-let-me-down-ww-remix-1",
    "duration": 216.0,
    "formats": AAC_FORMATS,
    "thumbnails": _artwork("000156447452-yq0ppq"),
}
TRACK_PRIVATE: dict[str, Any] = {
    "id": "123998367",
    "title": "Youtube - Dl Test Video '' Ä↭",
    "uploader": "jaimemf",
    "webpage_url": PRIVATE_URL,
    "duration": 9.874,
    "formats": MP3_FORMATS,
    "thumbnails": [{"id": "0", "url": "https://a1.sndcdn.com/images/default_avatar_large.png"}],
}
TRACK_GOPLUS: dict[str, Any] = {
    "id": "339400425",
    "title": "Look What You Made Me Do",
    "artist": "Taylor Swift",
    "artists": ["Taylor Swift"],
    "uploader": "Taylor Swift",
    "webpage_url": "https://soundcloud.com/taylorswiftofficial/look-what-you-made-me-do",
    "duration": 30.0,
    "formats": PREVIEW_FORMATS,
}


def _entry(track_id: str, url: str, album: str, album_artist: str, album_type: str) -> dict:
    return {
        "_type": "url_transparent",
        "ie_key": "Soundcloud",
        "id": track_id,
        "url": url,
        "album": album,
        "album_artist": album_artist,
        "album_type": album_type,
    }


_SPITE_TRACKS = [
    ("1378985008", "https://soundcloud.com/leviryan/crackshot-wgl0wrm"),
    ("1378983277", "https://soundcloud.com/leviryan/matrix-wdavid-shawty"),
    ("1378981744", "https://soundcloud.com/leviryan/silkroad-wlonewolfprie"),
    ("1378980004", "https://soundcloud.com/leviryan/purextc"),
    ("1378978405", "https://soundcloud.com/leviryan/bright-eyes-wfuneral"),
    # SoundCloud sends full track objects for the first five only; yt-dlp links the rest by id.
    ("1378976746", "https://api-v2.soundcloud.com/tracks/1378976746"),
    ("1378973569", "https://api-v2.soundcloud.com/tracks/1378973569"),
    ("1378971022", "https://api-v2.soundcloud.com/tracks/1378971022"),
]
SET_ALBUM: dict[str, Any] = {  # extract_flat="in_playlist": the entries carry no title
    "_type": "playlist",
    "id": "1524158182",
    "title": "out of spite",
    "album": "out of spite",
    "album_artist": "Levi Ryan",
    "album_type": "album",
    "uploader": "Levi Ryan",
    "webpage_url": SET_URL,
    "playlist_count": 8,
    "entries": [_entry(i, u, "out of spite", "Levi Ryan", "album") for i, u in _SPITE_TRACKS],
}
_EP_TRACKS = [
    ("75206121", "https://soundcloud.com/the-concept-band/world-on-fire-1"),
    ("47127625", "https://soundcloud.com/the-concept-band/gimme-twice-mastered"),
    ("47127627", "https://soundcloud.com/the-concept-band/goldrushed-mastered"),
    ("30510138", "https://soundcloud.com/the-concept-band/the-concept-d-d-dance"),
    ("47127629", "https://soundcloud.com/the-concept-band/in-the-end-mastered"),
    ("47127631", "https://api-v2.soundcloud.com/tracks/47127631"),
]
SET_EP: dict[str, Any] = {
    "_type": "playlist",
    "id": "2284613",
    "title": "The Royal Concept EP",
    "album": "The Royal Concept EP",
    "album_artist": "The Royal Concept",
    "album_type": "ep",
    "uploader": "The Royal Concept",
    "webpage_url": EP_URL,
    "entries": [
        _entry(i, u, "The Royal Concept EP", "The Royal Concept", "ep") for i, u in _EP_TRACKS
    ],
}


# ----------------------------------------------------------------------------------------------
# Links
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "clean"),
    [
        (TRACK_URL, TRACK_URL),
        ("http://soundcloud.com/the80m/the-following", TRACK_URL),
        ("soundcloud.com/the80m/the-following", TRACK_URL),  # no scheme
        ("  www.soundcloud.com/the80m/the-following  ", TRACK_URL),
        ("https://m.soundcloud.com/the80m/the-following", TRACK_URL),
        ("https://soundcloud.com/the80m/the-following/", TRACK_URL),
        ("https://soundcloud.com/the80m/the-following#t=0:30", TRACK_URL),
        (
            "https://soundcloud.com/the80m/the-following?in=the80m/sets/x&si=0123abcd"
            "&utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing",
            TRACK_URL,
        ),
        ("HTTPS://SOUNDCLOUD.COM/the80m/the-following", TRACK_URL),
        ("https://user:pw@soundcloud.com:8443/the80m/the-following", TRACK_URL),
        (r"https://evil.example\@soundcloud.com/the80m/the-following", TRACK_URL),
        (PRIVATE_URL, PRIVATE_URL),  # a private track keeps its secret part
        (f"{PRIVATE_URL}?si=abc&utm_source=clipboard", PRIVATE_URL),
        (
            "https://api.soundcloud.com/tracks/343609555",
            "https://api.soundcloud.com/tracks/343609555",
        ),
        (
            "https://api-v2.soundcloud.com/tracks/1378976746",
            "https://api-v2.soundcloud.com/tracks/1378976746",
        ),
        (
            "https://api.soundcloud.com/tracks/soundcloud%3Atracks%3A1083788353",
            "https://api.soundcloud.com/tracks/1083788353",
        ),
        (  # yt-dlp would hand the unencoded form to its generic extractor
            "https://api.soundcloud.com/tracks/soundcloud:tracks:1083788353",
            "https://api.soundcloud.com/tracks/1083788353",
        ),
        (
            "https://api.soundcloud.com/tracks/123998367?secret_token=s-8Pjrp",
            "https://api.soundcloud.com/tracks/123998367?secret_token=s-8Pjrp",
        ),
        (  # the embedded player: its inner link, never the player itself
            "https://w.soundcloud.com/player/?url=https%3A//api.soundcloud.com/tracks/343609555"
            "&color=%23ff5500&auto_play=false&hide_related=false",
            "https://api.soundcloud.com/tracks/343609555",
        ),
        (
            "https://w.soundcloud.com/player/?url=https%3A%2F%2Fsoundcloud.com%2Fthe80m%2Fthe-following",
            TRACK_URL,
        ),
    ],
)
def test_parse_link_tracks(url: str, clean: str) -> None:
    link = parse_link(url)
    assert link is not None and link.kind == "track"
    assert link.clean_url == clean


@pytest.mark.parametrize(
    ("url", "clean"),
    [
        (SET_URL, SET_URL),
        (
            "https://m.soundcloud.com/leviryan/sets/out-of-spite?si=abc&utm_source=clipboard",
            SET_URL,
        ),
        ("https://www.soundcloud.com/leviryan/sets/out-of-spite/", SET_URL),
        (
            "https://soundcloud.com/svart-records/sets/station-to-station/s-ziYey",
            "https://soundcloud.com/svart-records/sets/station-to-station/s-ziYey",
        ),
        (
            "https://api.soundcloud.com/playlists/4110309",
            "https://api.soundcloud.com/playlists/4110309",
        ),
        (
            "https://api-v2.soundcloud.com/playlists/soundcloud:playlists:4110309",
            "https://api-v2.soundcloud.com/playlists/4110309",
        ),
        (
            "https://api.soundcloud.com/playlists/922213810?secret_token=s-ziYey",
            "https://api.soundcloud.com/playlists/922213810?secret_token=s-ziYey",
        ),
        (  # the player passes a private playlist's token next to its url
            "https://w.soundcloud.com/player/?url=https%3A//api.soundcloud.com/playlists/922213810"
            "&secret_token=s-ziYey&visual=true",
            "https://api.soundcloud.com/playlists/922213810?secret_token=s-ziYey",
        ),
    ],
)
def test_parse_link_sets(url: str, clean: str) -> None:
    link = parse_link(url)
    assert link is not None and link.kind == "set"
    assert link.clean_url == clean


@pytest.mark.parametrize(
    "url",
    [
        "https://on.soundcloud.com/9zRpu",
        "on.soundcloud.com/9zRpu/",
        "https://on.soundcloud.com/9zRpu?si=abc&utm_source=clipboard#x",
        "https://user:pw@on.soundcloud.com:8443/9zRpu",
    ],
)
def test_parse_link_short_links(url: str) -> None:
    link = parse_link(url)
    assert link is not None and link.kind == "short"
    assert link.clean_url == "https://on.soundcloud.com/9zRpu"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://soundcloud.com/the80m", "profile"),
        ("https://soundcloud.com/the80m/", "profile"),
        ("https://soundcloud.com/soft-cell-official?si=abc", "profile"),
        ("https://m.soundcloud.com/the80m/tracks", "profile"),
        ("https://soundcloud.com/the80m/albums", "profile"),
        ("https://soundcloud.com/the80m/sets", "profile"),
        ("https://soundcloud.com/the80m/popular-tracks", "profile"),
        ("https://soundcloud.com/the80m/spotlight", "profile"),
        ("https://soundcloud.com/the80m/followers", "profile"),
        ("https://soundcloud.com/the80m/following", "profile"),
        ("https://soundcloud.com/the80m/comments", "profile"),
        ("https://api.soundcloud.com/users/30909869", "profile"),
        ("https://soundcloud.com/the80m/likes", "likes"),
        ("https://soundcloud.com/the80m/reposts", "likes"),
        ("https://soundcloud.com/stations/track/the80m/the-following", "station"),
        ("https://soundcloud.com/stations/artist/the80m", "station"),
        ("https://soundcloud.com/the80m/the-following/recommended", "station"),
        ("https://soundcloud.com/the80m/the-following/albums", "station"),
        ("https://soundcloud.com/the80m/the-following/sets", "station"),
        ("https://soundcloud.com/discover", "discover"),
        ("https://soundcloud.com/discover/sets/charts-top:all-music:de", "discover"),
        ("https://soundcloud.com/discover/sets/personalized-tracks::user:123", "discover"),
        ("https://soundcloud.com/charts/top", "discover"),
        ("https://soundcloud.com/search?q=lofi", "search"),
        ("https://soundcloud.com/search/sounds?q=lofi", "search"),
        ("https://soundcloud.com/you/likes", "account"),
        ("https://soundcloud.com/you/library", "account"),
        ("https://soundcloud.com/", "home"),
        ("https://soundcloud.com/tags/lofi", "page"),
        ("https://soundcloud.com/tags/lofi/popular-tracks", "page"),
        ("https://soundcloud.com/pages/privacy", "page"),
        ("https://soundcloud.com/settings/content", "page"),
        ("https://soundcloud.com/messages/abc-def", "page"),
        ("https://soundcloud.com/stations/sets/x", "station"),
        ("https://soundcloud.com/you/sets/x", "account"),
        ("https://soundcloud.com/upload", "page"),
        ("https://soundcloud.com/library", "page"),
        ("https://soundcloud.com/library/", "page"),
        ("https://soundcloud.com/library/likes", "page"),
        ("https://soundcloud.com/library/sets", "page"),
        ("https://soundcloud.com/library/tracks/x", "page"),
        ("https://soundcloud.com/the80m/the-following/likes", "page"),
        ("https://soundcloud.com/the80m/the-following/s-8Pjrp/more", "page"),
        ("https://soundcloud.com/the80m/sets/x/not-a-token", "page"),
        ("https://soundcloud.com/the80m/the following", "page"),
        ("https://soundcloud.com/the80m/the-following%0A", "page"),
        ("https://api.soundcloud.com/me", "page"),
        ("https://api.soundcloud.com/tracks/١٢٣", "page"),  # Arabic-Indic digits
        ("https://api.soundcloud.com/tracks/123/comments", "page"),
        ("https://on.soundcloud.com/", "page"),
        ("https://on.soundcloud.com/a/b", "page"),
        ("https://w.soundcloud.com/", "embed"),
        ("https://w.soundcloud.com/player/?url=https%3A%2F%2Fevil.example%2Fa%2Fb", "embed"),
        ("https://w.soundcloud.com/player/?url=https%3A%2F%2Fsoundcloud.com%2Fthe80m", "embed"),
        (  # a player inside a player is not followed
            "https://w.soundcloud.com/player/?url=https%3A%2F%2Fw.soundcloud.com%2Fplayer%2F"
            "%3Furl%3Dhttps%253A%252F%252Fsoundcloud.com%252Fthe80m%252Fthe-following",
            "embed",
        ),
    ],
)
def test_parse_link_other_soundcloud_pages(url: str, reason: str) -> None:
    link = parse_link(url)
    assert link is not None and link.kind == "other"
    assert link.reason == reason


@pytest.mark.parametrize(
    ("url", "kind", "clean"),
    [
        (  # a real account named "library", with public tracks and sets
            "https://soundcloud.com/library/huh-nipsey-husslerick-ross-remix",
            "track",
            "https://soundcloud.com/library/huh-nipsey-husslerick-ross-remix",
        ),
        (
            "https://m.soundcloud.com/library/sets/num1?si=abc",
            "set",
            "https://soundcloud.com/library/sets/num1",
        ),
        (
            "https://soundcloud.com/library/sets/num1/s-AbC12",
            "set",
            "https://soundcloud.com/library/sets/num1/s-AbC12",
        ),
        (
            "https://soundcloud.com/library/a-track/s-AbC12",
            "track",
            "https://soundcloud.com/library/a-track/s-AbC12",
        ),
        ("https://soundcloud.com/home/a-track", "track", "https://soundcloud.com/home/a-track"),
        ("https://soundcloud.com/jobs/sets/x", "set", "https://soundcloud.com/jobs/sets/x"),
    ],
)
def test_users_named_like_a_site_page_keep_their_tracks_and_sets(
    url: str, kind: str, clean: str
) -> None:
    """ "library", "home" and "jobs" are SoundCloud pages and real accounts too: only the bare
    name and its profile tabs are refused (above), not the account's tracks and sets."""
    link = parse_link(url)
    assert link is not None and link.kind == kind
    assert link.clean_url == clean


@pytest.mark.parametrize("name", sorted(soundcloud._SITE_PAGES))
def test_site_page_names_refuse_only_page_shaped_paths(name: str) -> None:
    for path in (name, f"{name}/likes", f"{name}/tracks", f"{name}/sets"):
        link = parse_link(f"https://soundcloud.com/{path}")
        assert link is not None and (link.kind, link.reason) == ("other", "page"), path
    assert parse_link(f"https://soundcloud.com/{name}/a-track").kind == "track"
    assert parse_link(f"https://soundcloud.com/{name}/sets/a-set").kind == "set"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://open.spotify.com/track/abc",
        "https://www.tiktok.com/@nasa/video/7698805564884733198",
        "https://soundcloud.com.evil.example/the80m/the-following",
        "https://evil.example/soundcloud.com/the80m/the-following",
        "https://notsoundcloud.com/the80m/the-following",
        "https://evilsoundcloud.com/the80m/the-following",
        "https://on.soundcloud.com.evil.example/9zRpu",
        "https://example.com/?u=https://soundcloud.com/the80m/the-following",
        "https://soundcloud.com@evil.example/the80m/the-following",
        "ftp://soundcloud.com/the80m/the-following",
        "file://soundcloud.com/the80m/the-following",
        "https://soundcloud.app.goo.gl/abc",
        "https://snd.sc/abc",
        "spotify:track:abc",
        "",
        "just words",
        "https://[broken",
    ],
)
def test_parse_link_rejects_other_hosts(url: str) -> None:
    assert parse_link(url) is None
    assert SoundCloudProvider().matches(url) is False


def test_matches_owns_every_soundcloud_page() -> None:
    """Profiles, stations and the like are owned too, so resolve() can explain them."""
    provider = SoundCloudProvider()
    assert provider.matches(TRACK_URL)
    assert provider.matches(SET_URL)
    assert provider.matches("https://on.soundcloud.com/9zRpu")
    assert provider.matches("https://soundcloud.com/the80m")
    assert provider.matches("https://soundcloud.com/discover")


def test_registry_routes_soundcloud_links() -> None:
    assert providers.get_provider("soundcloud.com/the80m/the-following").name == "soundcloud"
    assert providers.get_provider("https://on.soundcloud.com/9zRpu").name == "soundcloud"
    assert providers.get_provider("https://api-v2.soundcloud.com/tracks/1").name == "soundcloud"
    assert providers.get_provider("https://www.youtube.com/watch?v=jNQXAC9IVRw").name == "youtube"
    provider = providers.provider_by_name("soundcloud")
    assert isinstance(provider, SoundCloudProvider)
    assert provider.display_name == "SoundCloud" and provider.supports_video is False


@pytest.mark.parametrize(
    "url",
    [
        TRACK_URL,
        "https://m.soundcloud.com/the80m/the-following?in=x/sets/y",
        "https://soundcloud.com/the80m/the-following/",
        "https://soundcloud.com/the80m/the-following#t=0:30",
        PRIVATE_URL,
        "https://api.soundcloud.com/tracks/soundcloud:tracks:1083788353",
        "https://api-v2.soundcloud.com/tracks/1378976746?secret_token=s-abc12",
        "https://w.soundcloud.com/player/?url=https%3A//api.soundcloud.com/tracks/343609555",
        "https://soundcloud.com/library/huh-nipsey-husslerick-ross-remix",
        "https://soundcloud.com/u/recommended",  # a track whose name is a relation's
        "https://soundcloud.com/u/albums-1",
    ],
)
def test_track_links_are_what_yt_dlps_track_extractor_takes(url: str) -> None:
    """Asked of the installed yt-dlp itself: our rebuilt track link goes to its track
    extractor, never to the profile one; a private link keeps its token."""
    link = parse_link(url)
    assert link is not None and link.kind == "track"
    clean = link.clean_url
    assert SoundcloudIE.suitable(clean), clean
    assert not SoundcloudUserIE.suitable(clean)
    assert not SoundcloudSetIE.suitable(clean)
    assert not SoundcloudEmbedIE.suitable(clean)
    groups = SoundcloudIE._match_valid_url(clean).groupdict()
    if link.is_api:
        assert groups["track_id"] == link.path.rsplit("/", 1)[1]
        assert groups["secret_token"] == link.secret
    else:
        assert groups["token"] == link.secret


@pytest.mark.parametrize(
    "url",
    [
        "https://soundcloud.com/the80m/the-following/",
        "https://soundcloud.com/the80m/the-following#t=0:30",
        "https://api.soundcloud.com/tracks/soundcloud:tracks:1083788353",
    ],
)
def test_link_forms_yt_dlps_track_extractor_misses_are_rebuilt(url: str) -> None:
    """Why clean_url exists: yt-dlp hands these to its slow generic extractor (or, for the
    unencoded API form, to an API that always answers 429)."""
    assert not SoundcloudIE.suitable(url)
    assert SoundcloudIE.suitable(parse_link(url).clean_url)


@pytest.mark.parametrize(
    "url",
    [
        SET_URL,
        "https://m.soundcloud.com/leviryan/sets/out-of-spite?si=abc",
        "https://soundcloud.com/svart-records/sets/station-to-station/s-ziYey",
        "https://api.soundcloud.com/playlists/4110309",
        "https://api.soundcloud.com/playlists/922213810?secret_token=s-ziYey",
        "https://w.soundcloud.com/player/?url=https%3A//api.soundcloud.com/playlists/922213810"
        "&secret_token=s-ziYey",
        "https://soundcloud.com/library/sets/num1",
        "https://soundcloud.com/elulgospel/sets/recommended",
        "https://soundcloud.com/u/sets/albums-x/s-AbC12",
    ],
)
def test_set_links_are_what_yt_dlps_set_extractors_take(url: str) -> None:
    link = parse_link(url)
    assert link is not None and link.kind == "set"
    clean = link.clean_url
    extractor = SoundcloudPlaylistIE if link.is_api else SoundcloudSetIE
    assert extractor.suitable(clean), clean
    assert not SoundcloudIE.suitable(clean)
    assert not SoundcloudUserIE.suitable(clean)
    token = extractor._match_valid_url(clean).group("token")
    assert token == link.secret


# Sets named like one of the lists SoundCloud builds around a track (/<user>/<track>/albums):
# real ones exist by the dozen ("recommended", "albums", "sets-1", "albums-i-love").
RELATION_NAMED_SETS = [
    "https://soundcloud.com/elulgospel/sets/recommended",
    "https://soundcloud.com/dengue/sets/albums",
    "https://soundcloud.com/u/sets/albums-x",
    "https://soundcloud.com/u/sets/sets-1",
    "https://soundcloud.com/u/sets/recommended/s-AbC12",
]


@pytest.mark.parametrize("url", RELATION_NAMED_SETS)
def test_a_set_named_like_a_related_list_is_read_by_the_set_extractor(
    fake_ydl: type[FakeYoutubeDL], no_network: FakeHead, url: str
) -> None:
    """Left to choose, yt-dlp takes its first matching extractor, SoundcloudRelated (which lists
    the tracks related to a *track* and would read "/<user>/sets" as one); the set's own
    extractor takes the link, and resolve() pins it."""
    link = parse_link(url)
    assert link is not None and link.kind == "set"
    clean = link.clean_url
    first = next(ie for ie in gen_extractor_classes() if ie.suitable(clean))
    assert first.ie_key() == "SoundcloudRelated"  # why the extractor is pinned
    assert SoundcloudSetIE.suitable(clean)
    assert SoundcloudSetIE.ie_key() == "SoundcloudSet"

    fake_ydl.result = dict(SET_ALBUM, webpage_url=clean)
    assert len(SoundCloudProvider().resolve(url)) == 8
    (ydl,) = fake_ydl.instances
    assert ydl.calls == [(clean, False)]
    assert ydl.ie_keys == ["SoundcloudSet"]


@pytest.mark.parametrize(
    ("url", "ie_key", "info"),
    [
        (TRACK_URL, "Soundcloud", TRACK_80M),
        (PRIVATE_URL, "Soundcloud", TRACK_PRIVATE),
        ("https://api-v2.soundcloud.com/tracks/343609555", "Soundcloud", TRACK_80M),
        (SET_URL, "SoundcloudSet", SET_ALBUM),
        (
            "https://api.soundcloud.com/playlists/922213810?secret_token=s-ziYey",
            "SoundcloudPlaylist",
            SET_ALBUM,
        ),
    ],
)
def test_resolve_pins_the_extractor_of_the_links_kind(
    fake_ydl: type[FakeYoutubeDL],
    no_network: FakeHead,
    url: str,
    ie_key: str,
    info: dict[str, Any],
) -> None:
    fake_ydl.result = dict(info)
    SoundCloudProvider().resolve(url)
    (ydl,) = fake_ydl.instances
    assert ydl.ie_keys == [ie_key]
    ie = next(ie for ie in gen_extractor_classes() if ie.ie_key() == ie_key)
    assert ie.suitable(ydl.calls[0][0])  # yt-dlp checks it once more and would refuse


def test_the_real_yt_dlp_reads_a_recommended_set_as_that_set(
    monkeypatch: pytest.MonkeyPatch, no_network: FakeHead
) -> None:
    """Through the real YoutubeDL (its extractors' network side replaced): the set's own
    tracks, never the related tracks of the user that the "/<user>/sets" part names."""
    from yt_dlp.extractor import soundcloud as soundcloud_ies

    url = "https://soundcloud.com/elulgospel/sets/recommended"
    seen: list[tuple[str, str]] = []

    def read_set(ie: Any, page: str) -> dict[str, Any]:
        seen.append(("set", page))
        return {
            "_type": "playlist",
            "id": "1999",
            "title": "Elul Recommended",
            "album_type": "playlist",
            "entries": [
                {
                    "_type": "url",
                    "ie_key": "Soundcloud",
                    "id": "77",
                    "url": "https://soundcloud.com/elulgospel/a-song",
                }
            ],
        }

    def read_related(ie: Any, page: str) -> dict[str, Any]:
        seen.append(("related", page))
        entry = {"_type": "url", "id": "5", "url": "https://soundcloud.com/x/sets/other"}
        return {"_type": "playlist", "id": "elulgospel/sets", "title": "x", "entries": [entry]}

    monkeypatch.setattr(soundcloud_ies.SoundcloudSetIE, "_real_extract", read_set)
    monkeypatch.setattr(soundcloud_ies.SoundcloudRelatedIE, "_real_extract", read_related)
    (ref,) = SoundCloudProvider().resolve(url)
    assert seen == [("set", url)]
    assert (ref.source_id, ref.url) == ("77", "https://soundcloud.com/elulgospel/a-song")
    assert ref.extra == {"container": "Playlist: Elul Recommended"}


@pytest.mark.parametrize(
    ("reason", "needle"),
    [
        ("profile", "That's a SoundCloud profile, not a track or a playlist."),
        ("likes", "likes or reposts"),
        ("station", "on the fly"),
        ("discover", "Discover"),
        ("search", "search"),
        ("account", "signed-in SoundCloud account"),
        ("embed", "isn't a track or a playlist"),
        ("home", "isn't a track or a playlist"),
        ("page", "isn't a track or a playlist"),
    ],
)
def test_unsupported_message(reason: str, needle: str) -> None:
    message = unsupported_message(SoundCloudLink("other", reason=reason))
    assert needle in message
    assert "(Share, then Copy link)" in message
    assert "YouTube" not in message and "video" not in message.lower()


# ----------------------------------------------------------------------------------------------
# Naming
# ----------------------------------------------------------------------------------------------

# (yt-dlp's fields for real tracks, what the file is called): naming_infos of the research.
_REAL_NAMES: list[tuple[dict[str, Any], tuple[str, str]]] = [
    (  # a label upload: the label is the listed artist, the title says who it is
        {
            "id": "2432173806",
            "title": "angelrot, skrta - brb [NCS Release]",
            "artist": "NCS",
            "artists": ["NCS"],
            "uploader": "NCS",
        },
        ("angelrot, skrta", "brb"),
    ),
    (
        {
            "id": "2432433813",
            "title": "Pirapus - DANCE DISRUPTOR",
            "artist": "Monstercat",
            "artists": ["Monstercat"],
            "uploader": "Monstercat",
        },
        ("Pirapus", "DANCE DISRUPTOR"),
    ),
    (  # the label lists the artists with commas, the title joins them with "&"
        {
            "id": "1950053443",
            "title": "Tobu & Diviners - Geometry [Geometry Dash]",
            "artist": "Tobu\N{FULLWIDTH COMMA} Diviners",
            "artists": ["Tobu, Diviners"],
            "uploader": "NCS",
        },
        ("Tobu & Diviners", "Geometry [Geometry Dash]"),
    ),
    (
        {
            "title": "Boom Kitty & Waterflame - Citadel [Geometry Dash]",
            "artists": ["Boom Kitty, Waterflame"],
            "uploader": "NCS",
        },
        ("Boom Kitty & Waterflame", "Citadel [Geometry Dash]"),
    ),
    (  # ... or with "x", and the featured singer is listed last
        {
            "title": "Robin Hustin x Tobimorrow - Light It Up (feat. Jex) [NCS Release]",
            "artists": ["Robin Hustin, Tobimorrow and Jex"],
            "uploader": "NCS",
        },
        ("Robin Hustin x Tobimorrow", "Light It Up (feat. Jex)"),
    ),
    (
        {
            "title": "Unknown Brain x Rival - Control (feat. Jex) [NCS Release]",
            "artists": ["Unknown Brain, Rival & Jex"],
            "uploader": "NCS",
        },
        ("Unknown Brain x Rival", "Control (feat. Jex)"),
    ),
    (  # the remixers are listed after the first artists
        {
            "title": "Egzod, Maestro Chives, Neoni - Royalty (Wiguez & Alltair Remix) "
            "[NCS Release]",
            "artists": ["Egzod, Maestro Chives, Wiguez, Alltair"],
            "uploader": "NCS",
        },
        ("Egzod, Maestro Chives, Neoni", "Royalty (Wiguez & Alltair Remix)"),
    ),
    (  # an episode number is no artist: the label (listed and uploader) is
        {
            "id": "2428985835",
            "title": "634 - Monstercat Call of the Wild: VANDA Takeover",
            "artist": "Monstercat",
            "artists": ["Monstercat"],
            "uploader": "Monstercat",
        },
        ("Monstercat", "634 - Monstercat Call of the Wild: VANDA Takeover"),
    ),
    (  # no listed artist: the title's "Artist - Title"
        {
            "id": "62986583",
            "title": "Lostin Powers - She so Heavy (SneakPreview) Adrian Ackers Blueprint 1",
            "artist": None,
            "artists": None,
            "uploader": "E.T. ExTerrestrial Music",
        },
        ("Lostin Powers", "She so Heavy (SneakPreview) Adrian Ackers Blueprint 1"),
    ),
    (
        {
            "id": "343609555",
            "title": "The Following",
            "artist": "80M",
            "artists": ["80M"],
            "uploader": "80M",
        },
        ("80M", "The Following"),
    ),
    (  # the listed artist is not the uploader
        {
            "id": "309699954",
            "title": "Sideways (Prod. Mad Real)",
            "artist": "MadReal",
            "artists": ["MadReal"],
            "uploader": "garyvee",
        },
        ("MadReal", "Sideways (Prod. Mad Real)"),
    ),
    (  # an independent artist: the uploader
        {
            "id": "583011102",
            "title": "Mezzo Valzer",
            "artist": None,
            "artists": None,
            "uploader": "Giovanni Sarani",
        },
        ("Giovanni Sarani", "Mezzo Valzer"),
    ),
    (  # artists[0], not yt-dlp's "artist", which has a FULLWIDTH COMMA in place of the comma
        {
            "id": "1083788353",
            "title": "Still Alive By Boss, Harp | Coin Digital | New Punjabi Songs 2021",
            "artist": "Boss\N{FULLWIDTH COMMA} Harp",
            "artists": ["Boss, Harp"],
            "uploader": "COIN DIGITAL",
        },
        ("Boss, Harp", "Still Alive By Boss, Harp | Coin Digital | New Punjabi Songs 2021"),
    ),
    (  # the title pasted into the artist field (with a trailing space) is no artist
        {
            "id": "309858375",
            "title": "BENDY AND THE INK MACHINE SONG (Build Our Machine) INSTRUMENTAL by DAGAMES",
            "artist": "BENDY AND THE INK MACHINE SONG (Build Our Machine) INSTRUMENTAL ",
            "artists": ["BENDY AND THE INK MACHINE SONG (Build Our Machine) INSTRUMENTAL "],
            "uploader": "xXwolffykittyXx",
        },
        (
            "xXwolffykittyXx",
            "BENDY AND THE INK MACHINE SONG (Build Our Machine) INSTRUMENTAL by DAGAMES",
        ),
    ),
    (  # a remixer's upload: the original artist from the title
        {
            "id": "257939729",
            "title": "The Chainsmokers ft. Daya - Don't Let Me Down (W&W Remix)",
            "artist": None,
            "artists": None,
            "uploader": "W&W",
        },
        ("The Chainsmokers ft. Daya", "Don't Let Me Down (W&W Remix)"),
    ),
    (
        {
            "id": "2420230881",
            "title": "Patient Zero (Acoustic Version)",
            "artist": "Taylor Swift",
            "artists": ["Taylor Swift"],
            "uploader": "Taylor Swift",
        },
        ("Taylor Swift", "Patient Zero (Acoustic Version)"),
    ),
    (  # the private test track
        {"id": "123998367", "title": "Youtube - Dl Test Video '' Ä↭", "uploader": "jaimemf"},
        ("Youtube", "Dl Test Video '' Ä↭"),
    ),
]


@pytest.mark.parametrize(("info", "expected"), _REAL_NAMES)
def test_pick_tags_on_real_tracks(info: dict[str, Any], expected: tuple[str, str]) -> None:
    assert pick_tags(info) == (*expected, None)


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        # The listed artist starts the title: no "Adele - Adele - Hello".
        ({"title": "Adele - Hello", "artists": ["Adele"], "uploader": "Adele"}, ("Adele", "Hello")),
        (
            {"title": "Adele - Hello", "artists": ["Adele"], "uploader": "XL Recordings"},
            ("Adele", "Hello"),
        ),
        (  # the remixer is the listed artist and the title's dash belongs to the title
            {"title": "Song - W&W Remix", "artists": ["The Chainsmokers"], "uploader": "W&W"},
            ("The Chainsmokers", "Song - W&W Remix"),
        ),
        (  # whole words only: "MO" is not in "Mountain Echo"
            {"title": "Mountain Echo - Live", "artists": ["MO"], "uploader": "label"},
            ("MO", "Mountain Echo - Live"),
        ),
        (  # only the first listed name counts: the remixer listed second is not the artist
            {"title": "Song - W&W Remix", "artists": ["Someone, W&W"], "uploader": "W&W"},
            ("Someone, W&W", "Song - W&W Remix"),
        ),
        (  # "&", "x", "and", "feat." between the listed names, any case
            {"title": "A & B - Song", "artists": ["A feat. B"], "uploader": "u"},
            ("A & B", "Song"),
        ),
        ({"title": "A x B - Song", "artists": ["A and B"], "uploader": "u"}, ("A x B", "Song")),
        ({"title": "A X B - Song", "artists": ["A, B"], "uploader": "u"}, ("A X B", "Song")),
        (  # a band named with digits only is still the artist when it is the listed one
            {"title": "311 - Amber", "artists": ["311"], "uploader": "311"},
            ("311", "Amber"),
        ),
        ({"title": "311 - Amber", "uploader": "311"}, ("311", "Amber")),
        ({"title": "311 - Amber", "artists": ["311"], "uploader": "label"}, ("311", "Amber")),
        ({"title": "01 - Intro", "uploader": "Label"}, ("Label", "01 - Intro")),
        ({"title": "2019 - Song", "artists": ["A"], "uploader": "A"}, ("A", "2019 - Song")),
        (  # an artist that starts the title is still the artist
            {"title": "Adele Hello Remix", "artists": ["Adele"], "uploader": "u"},
            ("Adele", "Adele Hello Remix"),
        ),
        ({"title": "Song", "artist": "X", "uploader": "U"}, ("U", "Song")),  # artists missing
        ({"title": "Song", "artists": [" - "], "uploader": "U"}, ("U", "Song")),
        ({"title": "Song", "artists": [], "uploader": "U"}, ("U", "Song")),
        (
            {"title": "A | Live at Wembley", "uploader": "U"},
            ("U", "A | Live at Wembley"),
        ),  # no "|" split
        ({"title": "Song (Official Audio)", "uploader": "U"}, ("U", "Song")),
        ({"title": "Song [Free Download]", "uploader": "U"}, ("U", "Song")),
        ({"title": "A - Song (FREE DL)", "uploader": "U"}, ("A", "Song")),
        ({"title": "A - Song [OUT NOW]", "uploader": "U"}, ("A", "Song")),
        ({"title": "A - Song [Monstercat Release]", "uploader": "U"}, ("A", "Song")),
        ({"title": "Song (2019 Release)", "uploader": "U"}, ("U", "Song (2019 Release)")),
        ({"title": "[NCS Release]", "uploader": "U"}, ("U", "[NCS Release]")),  # all "junk"
        ({"id": "1", "uploader": "U"}, ("U", "SoundCloud track 1")),
        ({"id": 7, "title": "  "}, ("SoundCloud", "SoundCloud track 7")),
        ({}, ("SoundCloud", "SoundCloud track")),
    ],
)
def test_pick_tags_rules(info: dict[str, Any], expected: tuple[str, str]) -> None:
    assert pick_tags(info)[:2] == expected


def test_pick_tags_album_only_from_a_release() -> None:
    info = {"title": "x", "uploader": "u", "album": "out of spite", "album_type": "album"}
    assert pick_tags(info)[2] == "out of spite"
    for album_type in ("ep", "single", "compilation", "EP"):
        assert pick_tags(dict(info, album_type=album_type))[2] == "out of spite"
    assert pick_tags(dict(info, album_type="playlist"))[2] is None
    assert pick_tags(dict(info, album_type=None))[2] is None


def test_clean_sc_title_strips_repeated_tails() -> None:
    assert clean_sc_title("Song [OUT NOW] [Free Download]") == "Song"
    assert clean_sc_title("Song (Original Mix)") == "Song (Original Mix)"


@pytest.mark.parametrize(
    ("title", "cleaned"),
    [
        ("Song - [NCS Release]", "Song"),
        ("Song | [Free Download]", "Song"),
        ("Song – (FREE DL)", "Song"),
        ("Song (Official Audio) - [OUT NOW]", "Song"),
        ("Artist - Song - [Free Download]", "Artist - Song"),
        ("Song | Lyrics [Free Download]", "Song"),  # the junk the tail was hiding goes too
        ("Song - Live [Free Download]", "Song - Live"),
    ],
)
def test_clean_sc_title_leaves_no_separator_behind(title: str, cleaned: str) -> None:
    assert clean_sc_title(title) == cleaned


def test_a_dangling_separator_never_reaches_the_name() -> None:
    assert pick_tags({"title": "Song - [NCS Release]", "uploader": "Label"})[:2] == (
        "Label",
        "Song",
    )
    assert pick_tags({"title": "A - Song - [Free Download]", "uploader": "U"})[:2] == ("A", "Song")


# ----------------------------------------------------------------------------------------------
# Error texts
# ----------------------------------------------------------------------------------------------


def _friendly_part(message: str, raw: str) -> str:
    """The explanation without the original text explain_error() appends in parentheses."""
    detail = youtube._clean_error_text(raw)
    if len(detail) > youtube._ERROR_DETAIL_LEN:
        detail = detail[: youtube._ERROR_DETAIL_LEN - 1].rstrip() + "…"
    suffix = f" ({detail})" if detail else ""
    assert message.endswith(suffix)
    return message[: len(message) - len(suffix)]


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        (
            "ERROR: [soundcloud] the80m/this-track-does-not-exist-zz9: Unable to download JSON "
            "metadata: HTTP Error 404: Not Found (caused by <HTTPError 404: Not Found>)",
            "doesn't exist",
        ),
        (
            "ERROR: [soundcloud:set] leviryan/sets/nope: Unable to download JSON metadata: "
            "HTTP Error 404: Not Found",
            "s-... part",
        ),
        (
            "[soundcloud] 1: Unable to download JSON metadata: HTTP Error 429: Too Many",
            "Wait a few",
        ),
        (
            "You have reached the API rate limit, which is ~600 requests per 10 minutes. Use the "
            "--extractor-retries and --retry-sleep options",
            "retry the failed tracks",
        ),
        ("ERROR: [soundcloud] 718846078: This video is DRM protected", "DRM"),
        (
            "ERROR: [soundcloud] 1: This video is not available from your location due to geo "
            "restriction",
            "isn't available in your country",
        ),
        (
            "ERROR: [soundcloud] 2420230881: Requested format is not available. Use "
            "--list-formats for a list of available formats",
            "30-second preview",
        ),
        ("ERROR: [soundcloud] Unable to extract client id", "newer yt-dlp"),
        (
            "ERROR: [soundcloud] 207965082: No video formats found!; please report this issue on "
            " https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate template",
            "isn't a track or a playlist",
        ),
        ("ERROR: Unsupported URL: https://soundcloud.com/x", "isn't a track or a playlist"),
        ("[Errno 11001] getaddrinfo failed", "Couldn't reach SoundCloud"),
        ("Unable to download webpage: <urlopen error timed out>", "Couldn't reach SoundCloud"),
        ("Postprocessing: audio conversion failed: ffmpeg exited", "Converting the download"),
        ("unable to download: HTTP Error 403: Forbidden", "refused"),
        ("HTTP Error 401: Unauthorized", "refused"),
        (  # SoundCloud's rate limit, as it arrived after ~590 requests in 10 minutes
            "ERROR: [soundcloud:set] nocopyrightsounds/sets/ncs-top-100-nocopyrightsounds: "
            "Unable to download JSON metadata: HTTP Error 403: Forbidden (caused by "
            "<HTTPError 403: Forbidden>)",
            "wait a few minutes, then retry the failed tracks",
        ),
        ("HTTP Error 410: Gone", "doesn't exist"),
        ("HTTP Error 503: Service Unavailable", "problems right now"),
        ("something nobody has seen before", "SoundCloud download failed."),
    ],
)
def test_friendly_error(raw: str, needle: str) -> None:
    message = friendly_error(raw)
    assert needle in message
    friendly = _friendly_part(message, raw)
    assert "SoundCloud" in friendly or "track" in friendly or "download" in friendly
    assert "YouTube" not in friendly and "video" not in friendly.lower()


@pytest.mark.parametrize(
    "raw",
    [
        "ERROR: [soundcloud:set] nocopyrightsounds/sets/ncs-top-100-nocopyrightsounds: Unable to "
        "download JSON metadata: HTTP Error 403: Forbidden (caused by <HTTPError 403: Forbidden>)",
        "ERROR: [soundcloud] 1950053443: Unable to download JSON metadata: HTTP Error 403: "
        "Forbidden",
        "HTTP Error 401: Unauthorized",
    ],
)
def test_a_403_says_to_wait_first(raw: str) -> None:
    """SoundCloud's rate limit often comes back as a 403 on its JSON calls (it cleared by
    itself within minutes): waiting comes first, the yt-dlp update only if it never clears."""
    message = _friendly_part(friendly_error(raw), raw)
    assert message.startswith("SoundCloud refused the request for this track or playlist.")
    assert "this track or playlist" in message
    wait = message.index("wait a few minutes, then retry the failed tracks")
    assert wait < message.index("If it still happens much later, yt-dlp needs an update")
    assert "within a few minutes" in message


def test_friendly_error_keeps_the_original_text_short() -> None:
    message = friendly_error(yt_dlp.utils.DownloadError("ERROR: " + "x" * 500))
    assert message.startswith("SoundCloud download failed. (")
    assert len(message) < 260 and message.endswith("…)")
    assert friendly_error("") == "SoundCloud download failed."


# ----------------------------------------------------------------------------------------------
# Fakes: yt-dlp and the short-link HEAD request
# ----------------------------------------------------------------------------------------------


class FakeYoutubeDL:
    """Stands in for yt_dlp.YoutubeDL: `result` is the info dict, an exception to raise, or a
    callable(url) giving one; `on_extract(ydl, url, download)` runs first (a download writes
    the produced file there)."""

    instances: list[FakeYoutubeDL] = []
    result: Any = None
    on_extract: Any = None

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.calls: list[tuple[str, bool]] = []
        self.ie_keys: list[str | None] = []  # the extractor each call pinned
        self.post_processors: list[tuple[Any, str]] = []
        FakeYoutubeDL.instances.append(self)

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def add_post_processor(self, pp: Any, when: str = "post_process") -> None:
        self.post_processors.append((pp, when))

    def extract_info(self, url: str, download: bool = True, ie_key: str | None = None) -> Any:
        self.calls.append((url, download))
        self.ie_keys.append(ie_key)
        if FakeYoutubeDL.on_extract is not None:
            FakeYoutubeDL.on_extract(self, url, download)
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


class FakeHead:
    """Stands in for requests.head: records the call and answers `status` with `location` as
    its Location header (or raises `location` when it is an exception)."""

    def __init__(self, location: str | BaseException | None, status: int = 302) -> None:
        self.location = location
        self.status = status
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> Any:
        self.calls.append((url, kwargs))
        if isinstance(self.location, BaseException):
            raise self.location
        headers = {"Location": self.location} if self.location is not None else {}
        return SimpleNamespace(status_code=self.status, headers=headers, url=url)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> FakeHead:
    """A HEAD request is a test failure unless the test installs its own FakeHead."""
    head = FakeHead(AssertionError("unexpected HEAD request"))
    monkeypatch.setattr(soundcloud.requests, "head", head)
    return head


def _install_head(monkeypatch: pytest.MonkeyPatch, head: FakeHead) -> FakeHead:
    monkeypatch.setattr(soundcloud.requests, "head", head)
    return head


# ----------------------------------------------------------------------------------------------
# resolve(): tracks
# ----------------------------------------------------------------------------------------------


def test_resolve_a_track(fake_ydl: type[FakeYoutubeDL], no_network: FakeHead) -> None:
    fake_ydl.result = dict(TRACK_80M)
    pasted = "  soundcloud.com/the80m/the-following?si=abc&utm_source=clipboard  "
    (ref,) = SoundCloudProvider().resolve(pasted)
    assert ref.provider == "soundcloud" and ref.source_id == "343609555"
    assert ref.track_id == "soundcloud:343609555"
    assert ref.url == TRACK_URL
    assert (ref.artist, ref.title, ref.album) == ("80M", "The Following", None)
    assert ref.duration == 397.175
    assert ref.thumbnail_url == "https://i1.sndcdn.com/artworks-000245155417-0jqdd2-t500x500.jpg"
    assert ref.extra == {"original_url": "https://" + pasted.strip()}

    (ydl,) = fake_ydl.instances
    assert ydl.calls == [(TRACK_URL, False)]  # the query never reaches yt-dlp
    assert ydl.opts["extract_flat"] == "in_playlist"
    assert ydl.opts["skip_download"] is True and ydl.opts["noplaylist"] is True
    assert "cachedir" not in ydl.opts  # yt-dlp's cache keeps SoundCloud's client id
    assert ydl.post_processors == []
    assert no_network.calls == []


def test_resolve_a_private_track_keeps_its_secret(
    fake_ydl: type[FakeYoutubeDL], no_network: FakeHead
) -> None:
    fake_ydl.result = dict(TRACK_PRIVATE)
    (ref,) = SoundCloudProvider().resolve(f"{PRIVATE_URL}?si=abc")
    assert fake_ydl.instances[0].calls == [(PRIVATE_URL, False)]
    assert ref.url == PRIVATE_URL and ref.track_id == "soundcloud:123998367"
    assert ref.thumbnail_url is None  # SoundCloud's default avatar is no cover


def test_resolve_an_api_or_embed_link_gives_the_tracks_page(
    fake_ydl: type[FakeYoutubeDL], no_network: FakeHead
) -> None:
    fake_ydl.result = dict(TRACK_80M)
    (ref,) = SoundCloudProvider().resolve(
        "https://api.soundcloud.com/tracks/soundcloud%3Atracks%3A343609555"
    )
    assert fake_ydl.instances[0].calls == [("https://api.soundcloud.com/tracks/343609555", False)]
    assert ref.url == TRACK_URL  # webpage_url: the permalink
    (ref,) = SoundCloudProvider().resolve(
        "https://w.soundcloud.com/player/?url=https%3A//api.soundcloud.com/tracks/343609555"
    )
    assert fake_ydl.instances[1].calls == [("https://api.soundcloud.com/tracks/343609555", False)]
    assert ref.url == TRACK_URL
    fake_ydl.result = dict(TRACK_80M, webpage_url="https://evil.example/the80m/the-following")
    (ref,) = SoundCloudProvider().resolve("https://api.soundcloud.com/tracks/343609555")
    assert ref.url == "https://api.soundcloud.com/tracks/343609555"  # not a SoundCloud page


@pytest.mark.parametrize(
    "url",
    [
        "https://soundcloud.com/library/huh-nipsey-husslerick-ross-remix",
        "https://soundcloud.com/jobs/a-track",
    ],
)
def test_resolve_a_track_of_a_user_named_like_a_site_page(
    fake_ydl: type[FakeYoutubeDL], no_network: FakeHead, url: str
) -> None:
    fake_ydl.result = {
        "id": "438133569",
        "title": "mark my words rework",
        "uploader": "/library",
        "webpage_url": url,
        "formats": AAC_FORMATS,
    }
    (ref,) = SoundCloudProvider().resolve(url)
    assert (ref.source_id, ref.url) == ("438133569", url)
    assert fake_ydl.instances[0].calls == [(url, False)]
    assert fake_ydl.instances[0].ie_keys == ["Soundcloud"]


def test_resolve_a_set_of_the_user_library(
    fake_ydl: type[FakeYoutubeDL], no_network: FakeHead
) -> None:
    url = "https://soundcloud.com/library/sets/num1"
    fake_ydl.result = dict(SET_ALBUM, webpage_url=url)
    assert len(SoundCloudProvider().resolve(url)) == 8
    assert fake_ydl.instances[0].ie_keys == ["SoundcloudSet"]


def test_resolve_refuses_a_go_plus_preview(
    fake_ydl: type[FakeYoutubeDL], no_network: FakeHead
) -> None:
    """A Go+ track would otherwise be saved as its 30-second preview."""
    fake_ydl.result = dict(TRACK_GOPLUS)
    with pytest.raises(ProviderError) as caught:
        SoundCloudProvider().resolve(TRACK_GOPLUS["webpage_url"])
    assert str(caught.value) == GO_PLUS


def test_is_preview_only() -> None:
    assert is_preview_only({"formats": PREVIEW_FORMATS})
    assert is_preview_only({"formats": [{"format_id": "hls_mp3_1_0", "preference": -10}]})
    assert not is_preview_only({"formats": PREVIEW_FORMATS + AAC_FORMATS})
    assert not is_preview_only({"formats": AAC_FORMATS})
    assert not is_preview_only({"formats": []})
    assert not is_preview_only({})


def test_resolve_errors_are_friendly(fake_ydl: type[FakeYoutubeDL], no_network: FakeHead) -> None:
    fake_ydl.result = yt_dlp.utils.DownloadError(
        "ERROR: [soundcloud] theweeknd/blinding-lights: This video is DRM protected"
    )
    with pytest.raises(ProviderError, match="DRM") as caught:
        SoundCloudProvider().resolve("https://soundcloud.com/theweeknd/blinding-lights")
    assert "YouTube" not in str(caught.value)

    fake_ydl.result = yt_dlp.utils.DownloadError(
        "ERROR: [soundcloud] the80m/nope: Unable to download JSON metadata: HTTP Error 404: Not "
        "Found (caused by <HTTPError 404: Not Found>)"
    )
    with pytest.raises(ProviderError, match="doesn't exist"):
        SoundCloudProvider().resolve("https://soundcloud.com/the80m/nope")

    fake_ydl.result = None  # yt-dlp reported the error through the logger instead

    def log_error(ydl: FakeYoutubeDL, url: str, download: bool) -> None:
        ydl.opts["logger"].error("ERROR: [soundcloud] 1: HTTP Error 429: Too Many Requests")

    fake_ydl.on_extract = log_error
    with pytest.raises(ProviderError, match="rate-limiting"):
        SoundCloudProvider().resolve(TRACK_URL)

    fake_ydl.on_extract = None
    fake_ydl.result = RuntimeError("boom")
    with pytest.raises(ProviderError, match="Could not read that SoundCloud link: boom"):
        SoundCloudProvider().resolve(TRACK_URL)

    fake_ydl.result = {"title": "no id"}
    with pytest.raises(ProviderError, match="no track id"):
        SoundCloudProvider().resolve(TRACK_URL)


@pytest.mark.parametrize(
    ("url", "needle"),
    [
        ("https://soundcloud.com/the80m", "SoundCloud profile"),
        ("https://soundcloud.com/the80m/likes", "likes or reposts"),
        ("https://soundcloud.com/stations/track/the80m/the-following", "on the fly"),
        ("https://soundcloud.com/discover/sets/charts-top:all-music:de", "Discover"),
        ("https://soundcloud.com/search?q=lofi", "search"),
        ("https://soundcloud.com/you/likes", "signed-in"),
        ("https://soundcloud.com/", "isn't a track or a playlist"),
        ("https://soundcloud.com/library", "isn't a track or a playlist"),
        ("https://soundcloud.com/tags/lofi/x", "isn't a track or a playlist"),
        ("https://example.com/", "isn't a SoundCloud link"),
    ],
)
def test_resolve_explains_pages_that_are_not_tracks(
    fake_ydl: type[FakeYoutubeDL], no_network: FakeHead, url: str, needle: str
) -> None:
    with pytest.raises(ProviderError, match=needle):
        SoundCloudProvider().resolve(url)
    assert fake_ydl.instances == [] and no_network.calls == []


# ----------------------------------------------------------------------------------------------
# resolve(): sets
# ----------------------------------------------------------------------------------------------


def test_resolve_an_album(fake_ydl: type[FakeYoutubeDL], no_network: FakeHead) -> None:
    fake_ydl.result = SET_ALBUM
    refs = SoundCloudProvider().resolve(f"{SET_URL}?si=abc&utm_source=clipboard")
    (ydl,) = fake_ydl.instances  # one request for the whole set
    assert ydl.calls == [(SET_URL, False)]
    assert ydl.opts["extract_flat"] == "in_playlist"
    assert [(r.source_id, r.url) for r in refs] == _SPITE_TRACKS  # stubs kept as yt-dlp gave them
    assert {r.provider for r in refs} == {"soundcloud"}
    assert refs[0].track_id == "soundcloud:1378985008"
    assert [r.title for r in refs] == [f"out of spite · track {n}" for n in range(1, 9)]
    assert {r.album for r in refs} == {"out of spite"}
    assert {r.artist for r in refs} == {None}
    assert all(r.extra == {"container": "Album: out of spite"} for r in refs)


def test_resolve_an_ep(fake_ydl: type[FakeYoutubeDL], no_network: FakeHead) -> None:
    fake_ydl.result = SET_EP
    refs = SoundCloudProvider().resolve(EP_URL)
    assert [(r.source_id, r.url) for r in refs] == _EP_TRACKS
    assert {r.album for r in refs} == {"The Royal Concept EP"}
    assert refs[0].extra["container"] == "EP: The Royal Concept EP"
    assert refs[5].title == "The Royal Concept EP · track 6"


@pytest.mark.parametrize(
    ("album_type", "container", "album"),
    [
        ("single", "Single: out of spite", "out of spite"),
        ("compilation", "Compilation: out of spite", "out of spite"),
        ("playlist", "Playlist: out of spite", None),
        ("", "Playlist: out of spite", None),
        (None, "Playlist: out of spite", None),
    ],
)
def test_resolve_set_types(
    fake_ydl: type[FakeYoutubeDL],
    no_network: FakeHead,
    album_type: str | None,
    container: str,
    album: str | None,
) -> None:
    """Only a release (album, EP, single, compilation) names the album of its tracks."""
    fake_ydl.result = dict(SET_ALBUM, album_type=album_type)
    refs = SoundCloudProvider().resolve(SET_URL)
    assert len(refs) == 8
    assert {r.extra["container"] for r in refs} == {container}
    assert {r.album for r in refs} == {album}


def test_resolve_an_api_playlist_and_odd_entries(
    fake_ydl: type[FakeYoutubeDL], no_network: FakeHead
) -> None:
    fake_ydl.result = {
        "_type": "playlist",
        "id": "922213810",
        "title": "A private playlist",
        "album_type": "playlist",
        "entries": [
            _entry(
                "1",
                "https://soundcloud.com/svart-records/station-to-station-to-3/s-2fSTu",
                "A private playlist",
                "Svart",
                "playlist",
            ),
            _entry(
                "2",
                "https://api-v2.soundcloud.com/tracks/2?secret_token=s-ziYey",
                "A private playlist",
                "Svart",
                "playlist",
            ),
            {"_type": "url_transparent", "url": "https://soundcloud.com/a/no-id"},  # skipped
            {"_type": "url_transparent", "id": 3, "url": "https://evil.example/3"},
            "not a dict",
            _entry("4", "https://soundcloud.com/a/four", "A private playlist", "Svart", "x"),
        ],
    }
    refs = SoundCloudProvider().resolve(
        "https://api.soundcloud.com/playlists/922213810?secret_token=s-ziYey"
    )
    assert fake_ydl.instances[0].calls == [
        ("https://api.soundcloud.com/playlists/922213810?secret_token=s-ziYey", False)
    ]
    assert [(r.source_id, r.url) for r in refs] == [
        ("1", "https://soundcloud.com/svart-records/station-to-station-to-3/s-2fSTu"),
        ("2", "https://api-v2.soundcloud.com/tracks/2?secret_token=s-ziYey"),
        ("4", "https://soundcloud.com/a/four"),  # entry 3 is no SoundCloud track link
    ]
    assert [r.title for r in refs] == [
        "A private playlist · track 1",
        "A private playlist · track 2",
        "A private playlist · track 6",  # the position in the set
    ]
    assert {r.album for r in refs} == {None}


@pytest.mark.parametrize(
    "url",
    [
        "https://soundcloud.com/x/sets/other",  # a set, as a relation's listing gives them
        "https://api-v2.soundcloud.com/playlists/5",
        "https://soundcloud.com/x",
        "https://evil.example/tracks/5",
        "",
        None,
    ],
)
def test_set_entries_that_are_no_track_are_skipped(
    fake_ydl: type[FakeYoutubeDL], no_network: FakeHead, url: str | None
) -> None:
    """An entry's id is a track id only when its link is a track's: a playlist id turned into
    api-v2.soundcloud.com/tracks/<id> would download some unrelated track."""
    entries = [{"_type": "url", "id": "5", "url": url}, _entry("6", TRACK_URL, "s", "a", "x")]
    fake_ydl.result = dict(SET_ALBUM, entries=entries)
    refs = SoundCloudProvider().resolve(SET_URL)
    assert [(r.source_id, r.url) for r in refs] == [("6", TRACK_URL)]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Jessie Ware's album has a track called "Spotlight": its page reads as a profile tab,
        # and yt-dlp's own track pattern refuses it too. Its id still downloads it.
        (
            "https://soundcloud.com/jessieware/spotlight",
            "https://api-v2.soundcloud.com/tracks/842902687",
        ),
        (
            "https://soundcloud.com/jessieware/spotlight/s-AbC123",  # a private one keeps its s-
            "https://api-v2.soundcloud.com/tracks/842902687?secret_token=s-AbC123",
        ),
        (
            "https://soundcloud.com/jessieware/likes",
            "https://api-v2.soundcloud.com/tracks/842902687",
        ),
    ],
)
def test_a_set_track_named_like_a_profile_tab_is_kept_by_its_id(
    fake_ydl: type[FakeYoutubeDL], no_network: FakeHead, url: str, expected: str
) -> None:
    entries = [_entry("842902687", url, "What's Your Pleasure?", "Jessie Ware", "album")]
    fake_ydl.result = dict(SET_ALBUM, entries=entries)
    refs = SoundCloudProvider().resolve(SET_URL)
    assert [(r.source_id, r.url) for r in refs] == [("842902687", expected)]
    assert SoundCloudProvider().matches(expected)  # a link the app takes


@pytest.mark.parametrize("track_id", ["", "abc", "١٢٣", None])
def test_a_track_entry_without_a_usable_id_is_still_skipped(
    fake_ydl: type[FakeYoutubeDL], no_network: FakeHead, track_id: str | None
) -> None:
    entries = [dict(_entry("x", "https://soundcloud.com/a/spotlight", "s", "a", "x"), id=track_id)]
    fake_ydl.result = dict(SET_ALBUM, entries=entries)
    assert SoundCloudProvider().resolve(SET_URL) == []


def test_resolve_an_empty_set(fake_ydl: type[FakeYoutubeDL], no_network: FakeHead) -> None:
    """The downloader says "Nothing to download at that link"."""
    fake_ydl.result = dict(SET_ALBUM, entries=[])
    assert SoundCloudProvider().resolve(SET_URL) == []
    fake_ydl.result = dict(SET_ALBUM, entries=None)
    assert SoundCloudProvider().resolve(SET_URL) == []


# ----------------------------------------------------------------------------------------------
# resolve(): short links
# ----------------------------------------------------------------------------------------------

SHORT_TRACK_TARGET = (
    "https://soundcloud.com/user-261546761/liigan-puskafarssi"
    "?utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing"
)


def test_resolve_expands_a_short_link_to_a_track(
    fake_ydl: type[FakeYoutubeDL],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    head = _install_head(monkeypatch, FakeHead(SHORT_TRACK_TARGET))
    fake_ydl.result = dict(
        TRACK_80M,
        id="1633827945",
        webpage_url="https://soundcloud.com/user-261546761/liigan-puskafarssi",
    )
    with caplog.at_level(logging.INFO, logger="ultimate_playlist.providers.soundcloud"):
        (ref,) = SoundCloudProvider().resolve("on.soundcloud.com/9zRpu?si=abc")
    ((url, kwargs),) = head.calls
    assert url == "https://on.soundcloud.com/9zRpu"  # rebuilt: no query
    assert kwargs["allow_redirects"] is False and kwargs["timeout"] > 0
    assert fake_ydl.instances[0].calls == [
        ("https://soundcloud.com/user-261546761/liigan-puskafarssi", False)
    ]
    assert ref.source_id == "1633827945"
    assert ref.extra["original_url"] == "https://on.soundcloud.com/9zRpu?si=abc"
    assert "led to https://soundcloud.com/user-261546761/liigan-puskafarssi" in caplog.text
    assert "utm_" not in caplog.text and "si=abc" not in caplog.text


def test_resolve_expands_a_short_link_to_a_set(
    fake_ydl: type[FakeYoutubeDL], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = "https://soundcloud.com/leviryan/sets/out-of-spite?utm_source=mobi&utm_campaign=x"
    _install_head(monkeypatch, FakeHead(target))
    fake_ydl.result = SET_ALBUM
    refs = SoundCloudProvider().resolve("https://on.soundcloud.com/KPfBadGrMoV5Jxze7")
    assert len(refs) == 8
    assert fake_ydl.instances[0].calls == [(SET_URL, False)]


@pytest.mark.parametrize(
    ("location", "status", "needle"),
    [
        (None, 404, "any more"),
        (None, 410, "any more"),
        (None, 429, "rate-limiting"),
        (None, 503, "problems right now"),
        (None, 403, "wouldn't open that short link (HTTP 403)"),
        (None, 200, "any more"),  # answered without leading anywhere
        (None, 302, "any more"),  # a redirect without a target
        ("https://evil.example/the80m/the-following", 302, "any more"),
        ("https://soundcloud.com.evil.example/the80m/the-following", 302, "any more"),
        ("https://api.soundcloud.com/tracks/1", 302, "any more"),
        ("https://on.soundcloud.com/other", 302, "any more"),
        ("/somewhere", 302, "any more"),  # relative: still on.soundcloud.com
        ("https://soundcloud.com/", 301, "any more"),
        ("https://soundcloud.com/pages/privacy", 302, "any more"),
        ("https://soundcloud.com/the80m?utm_source=clipboard", 302, "SoundCloud profile"),
        ("https://m.soundcloud.com/the80m/likes?utm_source=clipboard", 302, "likes or reposts"),
        ("https://soundcloud.com/discover/sets/weekly::the80m", 302, "Discover"),
    ],
)
def test_short_links_that_lead_nowhere_downloadable(
    fake_ydl: type[FakeYoutubeDL],
    monkeypatch: pytest.MonkeyPatch,
    location: str | None,
    status: int,
    needle: str,
) -> None:
    _install_head(monkeypatch, FakeHead(location, status=status))
    with pytest.raises(ProviderError, match=re.escape(needle)) as caught:
        SoundCloudProvider().resolve("https://on.soundcloud.com/9zRpu")
    if needle == "any more":
        assert str(caught.value) == SHORT_LINK_DEAD
    assert fake_ydl.instances == []


@pytest.mark.parametrize(
    "error",
    [requests.ConnectionError("no route"), requests.ReadTimeout("slow")],
)
def test_a_short_link_without_network(
    fake_ydl: type[FakeYoutubeDL], monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _install_head(monkeypatch, FakeHead(error))
    with pytest.raises(ProviderError, match="Couldn't reach SoundCloud") as caught:
        SoundCloudProvider().resolve("https://on.soundcloud.com/9zRpu")
    assert f"({type(error).__name__})" in str(caught.value)
    assert fake_ydl.instances == []


@pytest.mark.parametrize(
    "pasted",
    [
        r"https://evil.example\@on.soundcloud.com/9zRpu",
        r"evil.example\@on.soundcloud.com/9zRpu",
        "https://user:pw@on.soundcloud.com:8443/9zRpu/",
        "on.soundcloud.com/9zRpu?si=secret#fragment",
    ],
)
def test_a_short_link_is_only_ever_sent_to_on_soundcloud_com(
    fake_ydl: type[FakeYoutubeDL], monkeypatch: pytest.MonkeyPatch, pasted: str
) -> None:
    """urlsplit and requests disagree on where a backslash ends the host; the HEAD request goes
    to the host that was checked, rebuilt without userinfo, port or query."""
    head = _install_head(monkeypatch, FakeHead(None, status=404))
    with pytest.raises(ProviderError):
        SoundCloudProvider().resolve(pasted)
    assert [url for url, _ in head.calls] == ["https://on.soundcloud.com/9zRpu"]


# ----------------------------------------------------------------------------------------------
# download() with a fake yt-dlp
# ----------------------------------------------------------------------------------------------


def _fake_download(infos: dict[str, dict[str, Any]] | dict[str, Any]) -> Any:
    """An on_extract callback that behaves like a successful yt-dlp run producing an MP3, for
    one info dict or for {url: info}."""

    def on_extract(ydl: FakeYoutubeDL, url: str, download: bool) -> None:
        if not download:
            return
        info = infos[url] if url in infos else infos
        out = Path(ydl.opts["outtmpl"] % {"id": info["id"], "ext": "mp3"})
        write_tiny_mp3(out, artist="yt", title="yt")
        (out.parent / f"{info['id']}.m4a.part").write_bytes(b"partial")
        for hook in ydl.opts["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})
            hook({"status": "finished", "filename": str(out)})

    return on_extract


def _download(ref: TrackRef, tmp_path: Path, events: list[ProgressEvent] | None = None) -> Any:
    settings = Settings(library_dir=tmp_path / "lib")
    sink = events.append if events is not None else (lambda e: None)
    return SoundCloudProvider().download(
        ref, settings.library_dir, settings, sink, threading.Event()
    )


def test_download_names_and_tags_the_track(fake_ydl: type[FakeYoutubeDL], tmp_path: Path) -> None:
    fake_ydl.result = dict(TRACK_WW)
    fake_ydl.on_extract = _fake_download(TRACK_WW)
    ref = TrackRef(provider="soundcloud", source_id="257939729", url=TRACK_WW["webpage_url"])
    events: list[ProgressEvent] = []

    track = _download(ref, tmp_path, events)

    library = tmp_path / "lib"
    name = "The Chainsmokers ft. Daya - Don't Let Me Down (W&W Remix).mp3"
    assert (library / name).is_file() and track.path == name
    assert (track.id, track.provider, track.source_id) == (
        "soundcloud:257939729",
        "soundcloud",
        "257939729",
    )
    assert (track.artist, track.title, track.album) == (
        "The Chainsmokers ft. Daya",
        "Don't Let Me Down (W&W Remix)",
        None,
    )
    assert track.source_url == TRACK_WW["webpage_url"]
    tags = ID3(str(library / name))
    assert tags["TPE1"].text == ["The Chainsmokers ft. Daya"]
    assert tags["TIT2"].text == ["Don't Let Me Down (W&W Remix)"]
    assert "TALB" not in tags
    assert tags.getall("TXXX:ULTIMATE_PLAYLIST_ID")[0].text == ["soundcloud:257939729"]
    assert tags.getall("COMM")[0].text == [TRACK_WW["webpage_url"]]
    assert list((library / ".incoming").iterdir()) == []
    assert JobStatus.CONVERTING in [e.status for e in events]

    (ydl,) = fake_ydl.instances
    assert ydl.calls == [(TRACK_WW["webpage_url"], True)]
    assert ydl.opts["format"] == YDL_FORMAT  # what the yt-dlp selection tests below exercise
    ((pp, when),) = ydl.post_processors
    assert isinstance(pp, TrimArtworkPP) and when == "pre_process"


def test_download_of_a_set_track_by_its_api_link(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    """The later tracks of a set come as api-v2 links: the tags and the library get the track's
    page instead, and the set's album."""
    stub = "https://api-v2.soundcloud.com/tracks/1378976746"
    info = {
        "id": "1378976746",
        "title": "misery w/lor2mg",
        "artists": ["Levi Ryan"],
        "uploader": "Levi Ryan",
        "webpage_url": "https://soundcloud.com/leviryan/misery-wlor2mg",
        "original_url": stub,
    }
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info)
    ref = TrackRef(
        provider="soundcloud",
        source_id="1378976746",
        url=stub,
        title="out of spite · track 6",
        album="out of spite",
        extra={"container": "Album: out of spite"},
    )
    track = _download(ref, tmp_path)
    assert fake_ydl.instances[0].calls == [(stub, True)]
    assert track.path == "Levi Ryan - misery wlor2mg.mp3"
    assert (track.artist, track.title, track.album) == (
        "Levi Ryan",
        "misery w/lor2mg",
        "out of spite",
    )
    assert track.source_url == "https://soundcloud.com/leviryan/misery-wlor2mg"
    tags = ID3(str(tmp_path / "lib" / track.path))
    assert tags["TALB"].text == ["out of spite"]
    assert tags.getall("COMM")[0].text == ["https://soundcloud.com/leviryan/misery-wlor2mg"]
    assert tags.getall("TXXX:ULTIMATE_PLAYLIST_ID")[0].text == ["soundcloud:1378976746"]


def test_download_of_a_private_track_keeps_its_secret_in_the_source_url(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    api = "https://api-v2.soundcloud.com/tracks/123998367?secret_token=s-8Pjrp"
    fake_ydl.result = dict(TRACK_PRIVATE)
    fake_ydl.on_extract = _fake_download(TRACK_PRIVATE)
    track = _download(TrackRef(provider="soundcloud", source_id="123998367", url=api), tmp_path)
    assert track.source_url == PRIVATE_URL


@pytest.mark.parametrize(
    ("ref_url", "page", "track_id", "expected"),
    [
        # A set lists Jessie Ware's "Spotlight" by its id; its page reads as a profile tab.
        (
            "https://api-v2.soundcloud.com/tracks/842902687",
            "https://soundcloud.com/jessieware/spotlight",
            "842902687",
            "https://soundcloud.com/jessieware/spotlight",
        ),
        (
            "https://api-v2.soundcloud.com/tracks/842902687?secret_token=s-AbC12",
            "https://soundcloud.com/jessieware/spotlight/s-AbC12?utm_source=x",
            "842902687",
            "https://soundcloud.com/jessieware/spotlight/s-AbC12",
        ),
        # Not this very track, or not a track page's shape: the API link stays.
        (
            "https://api-v2.soundcloud.com/tracks/842902687",
            "https://soundcloud.com/jessieware/spotlight",
            "111",
            "https://api-v2.soundcloud.com/tracks/842902687",
        ),
        (
            "https://api-v2.soundcloud.com/tracks/842902687",
            "https://soundcloud.com:8443/jessieware/spotlight",
            "842902687",
            "https://api-v2.soundcloud.com/tracks/842902687",
        ),
        (
            "https://api-v2.soundcloud.com/tracks/842902687",
            "https://x@soundcloud.com/jessieware/spotlight",
            "842902687",
            "https://api-v2.soundcloud.com/tracks/842902687",
        ),
        (
            "https://api-v2.soundcloud.com/tracks/842902687",
            "https://soundcloud.com/jessieware/spotlight/extra",
            "842902687",
            "https://api-v2.soundcloud.com/tracks/842902687",
        ),
    ],
)
def test_a_set_track_kept_by_its_id_gets_its_page_as_source_url(
    fake_ydl: type[FakeYoutubeDL],
    tmp_path: Path,
    ref_url: str,
    page: str,
    track_id: str,
    expected: str,
) -> None:
    info = {
        "id": track_id,
        "title": "Spotlight",
        "artists": ["Jessie Ware"],
        "uploader": "Jessie Ware",
        "webpage_url": page,
    }
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info)
    ref = TrackRef(provider="soundcloud", source_id="842902687", url=ref_url)
    track = _download(ref, tmp_path)
    assert track.source_url == expected
    tags = ID3(str(tmp_path / "lib" / track.path))
    assert tags.getall("COMM")[0].text == [expected]


def test_download_keeps_the_ref_url_when_the_page_is_no_soundcloud_track(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    api = "https://api-v2.soundcloud.com/tracks/343609555"
    for page in (
        None,
        "https://evil.example/the80m/the-following",
        "https://soundcloud.com/the80m",
    ):
        fake_ydl.result = dict(TRACK_80M, webpage_url=page)
        fake_ydl.on_extract = _fake_download(TRACK_80M)
        track = _download(TrackRef(provider="soundcloud", source_id="343609555", url=api), tmp_path)
        assert track.source_url == api


def test_an_album_in_yt_dlps_info_comes_before_the_refs(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    info = dict(TRACK_80M, album="From the set", album_type="ep")
    fake_ydl.result = info
    fake_ydl.on_extract = _fake_download(info)
    ref = TrackRef(provider="soundcloud", source_id="343609555", url=TRACK_URL, album="Ref album")
    assert _download(ref, tmp_path).album == "From the set"


def test_download_errors_are_soundcloud_errors(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path
) -> None:
    ref = TrackRef(provider="soundcloud", source_id="2420230881", url=TRACK_URL)
    fake_ydl.result = yt_dlp.utils.DownloadError(
        "ERROR: [soundcloud] 2420230881: Requested format is not available. Use --list-formats "
        "for a list of available formats"
    )
    with pytest.raises(ProviderError, match="30-second preview") as caught:
        _download(ref, tmp_path)
    assert "YouTube" not in str(caught.value)

    fake_ydl.result = None
    with pytest.raises(ProviderError) as caught:
        _download(ref, tmp_path)
    assert "SoundCloud returned no information for this link" in str(caught.value)
    assert "video" not in str(caught.value).lower()


def _fragment_download(part: Path, error: BaseException, streams: list[Any]) -> None:
    """What yt-dlp's HLS downloader (FragmentFD, SoundCloud's streams) does when a progress hook
    cancels it: the .part file stays open in its context dict, which a closure (its fragment
    progress hook, whose frame is in the traceback) and a reference cycle (context and
    downloader point at each other) keep alive."""
    ctx: dict[str, Any] = {"dest_stream": part.open("wb")}
    ctx["dest_stream"].write(b"partial")
    streams.append(weakref.ref(ctx["dest_stream"]))

    def frag_progress_hook(status: dict[str, Any]) -> None:
        ctx["fragment_index"] = status.get("fragment_index")
        raise error

    ctx["dl"] = SimpleNamespace(progress_hooks=[frag_progress_hook])  # the cycle
    frag_progress_hook({"fragment_index": 1})


@pytest.fixture
def no_automatic_gc() -> Iterator[None]:
    """Only an explicit gc.collect() frees a reference cycle during the test."""
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (DownloadCancelled("Download cancelled"), DownloadCancelled),
        (yt_dlp.utils.DownloadError("ERROR: fragment 3 not found"), ProviderError),
        (RuntimeError("disk on fire"), ProviderError),
    ],
)
def test_a_failed_download_closes_and_removes_its_open_part_file(
    fake_ydl: type[FakeYoutubeDL],
    tmp_path: Path,
    no_automatic_gc: None,
    error: Exception,
    raised: type[Exception],
) -> None:
    """Windows can't delete an open file: before the download's folder is removed, the
    exception lets go of the frames below the download and the garbage is collected, so the
    .part file is closed."""
    streams: list[weakref.ref[Any]] = []

    def open_part_and_fail(ydl: FakeYoutubeDL, url: str, download: bool) -> None:
        part = Path(ydl.opts["outtmpl"]).parent / "343609555.m4a.part"
        _fragment_download(part, error, streams)

    fake_ydl.on_extract = open_part_and_fail
    ref = TrackRef(provider="soundcloud", source_id="343609555", url=TRACK_URL)
    with pytest.raises(raised):
        _download(ref, tmp_path)
    (stream,) = streams
    assert stream() is None  # closed now, not whenever the garbage collector runs
    assert list((tmp_path / "lib" / ".incoming").iterdir()) == []


def test_the_download_folder_removal_is_tried_twice(
    fake_ydl: type[FakeYoutubeDL], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A virus scanner can hold a file a moment longer: one more try after a short pause."""
    real_rmtree = shutil.rmtree
    tries: list[Path] = []

    def held_once(path: Any, *args: Any, **kwargs: Any) -> None:
        tries.append(Path(path))
        if len(tries) > 1:
            real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(youtube, "STAGING_RETRY_DELAY", 0.0)
    monkeypatch.setattr(youtube.shutil, "rmtree", held_once)
    fake_ydl.result = DownloadCancelled("Download cancelled")
    with pytest.raises(DownloadCancelled):
        _download(TrackRef(provider="soundcloud", source_id="1", url=TRACK_URL), tmp_path)
    assert len(tries) == 2 and tries[0] == tries[1]
    assert tries[0].parent == tmp_path / "lib" / ".incoming"
    assert not tries[0].exists()


# ----------------------------------------------------------------------------------------------
# What the real yt-dlp does with our choices (no network)
# ----------------------------------------------------------------------------------------------


def _processed(formats: list[dict[str, Any]], *, fmt: str = YDL_FORMAT, **params: Any) -> dict:
    """yt-dlp's own sorting, selection and pre-processing on a SoundCloud-shaped info dict;
    nothing is downloaded or probed."""
    info = {
        "id": "343609555",
        "title": "t",
        "extractor": "soundcloud",
        "extractor_key": "Soundcloud",
        "webpage_url": TRACK_URL,
        "formats": [dict(f) for f in formats],
        "thumbnails": params.pop("thumbnails", None) or _artwork("000245155417-0jqdd2"),
    }
    pps = params.pop("pps", [])
    opts = {"quiet": True, "simulate": True, "check_formats": False, "format": fmt, **params}
    with yt_dlp.YoutubeDL(opts) as ydl:
        for pp in pps:
            ydl.add_post_processor(pp, when="pre_process")
        return ydl.process_ie_result(info, download=False)


def test_format_a_track_gives_its_best_stream() -> None:
    assert _processed(AAC_FORMATS)["format_id"] == "hls_aac_160k"
    assert _processed(MP3_FORMATS)["format_id"] == "http_mp3_0_0"  # copied, not re-encoded


def test_format_a_preview_never_wins_over_the_track() -> None:
    assert _processed(PREVIEW_FORMATS + AAC_FORMATS)["format_id"] == "hls_aac_160k"


def test_format_a_go_plus_track_is_refused_not_cut_to_30_seconds() -> None:
    # What yt-dlp would do without the filter:
    assert _processed(PREVIEW_FORMATS, fmt="bestaudio/best")["format_id"].endswith("_preview")
    with pytest.raises(yt_dlp.utils.YoutubeDLError, match="Requested format is not available") as e:
        _processed(PREVIEW_FORMATS)
    assert friendly_error(e.value).startswith(GO_PLUS)


def test_the_artwork_written_is_500x500_not_the_original() -> None:
    events: list[dict[str, Any]] = []
    plain = _processed(AAC_FORMATS)
    assert plain["thumbnails"][-1]["id"] == "original"  # what yt-dlp would write and embed
    trimmed = _processed(AAC_FORMATS, pps=[TrimArtworkPP()], postprocessor_hooks=[events.append])
    assert trimmed["thumbnails"][-1]["id"] == "t500x500"
    assert trimmed["thumbnail"].endswith("-t500x500.jpg")
    assert "original" not in {t["id"] for t in trimmed["thumbnails"]}
    assert events == []  # no "Processing (TrimArtwork)" in the queue
    assert TrimArtworkPP.pp_key() == "TrimArtwork"


def test_the_default_avatar_is_no_cover() -> None:
    avatar = [{"url": "https://a1.sndcdn.com/images/default_avatar_large.png"}]
    out = _processed(MP3_FORMATS, thumbnails=avatar, pps=[TrimArtworkPP()])
    assert out["thumbnails"] == [] and "thumbnail" not in out


def test_trim_artwork() -> None:
    art = _artwork("x")
    assert [t["id"] for t in trim_artwork(art)] == [t["id"] for t in art[:-1]]
    only_original = [art[-1]]
    assert trim_artwork(only_original) == only_original  # better than no cover at all
    avatar = {"url": "https://i1.sndcdn.com/avatars-abc-large.jpg"}  # an uploader's picture
    assert trim_artwork([avatar]) == [avatar]
    placeholder = {"url": "https://a1.sndcdn.com/images/default_avatar_large.png"}
    assert trim_artwork([placeholder]) == []
    assert trim_artwork(None) == [] and trim_artwork(["junk"]) == []


# ----------------------------------------------------------------------------------------------
# Through the downloader
# ----------------------------------------------------------------------------------------------


@pytest.fixture
def jobs(tmp_settings: Settings, library: Library) -> Iterator[Callable[[], JobManager]]:
    managers: list[JobManager] = []

    def factory() -> JobManager:
        provider = SoundCloudProvider(tmp_settings)
        manager = JobManager(replace(tmp_settings), library, providers=FakeRegistry(provider))
        manager.start()
        managers.append(manager)
        return manager

    yield factory
    for manager in managers:
        manager.stop()


def _page_of(track_id: str, url: str) -> str:
    """The track's page: its set link, or for an API link a page named after its id."""
    return url if "api-v2" not in url else f"https://soundcloud.com/leviryan/song-{track_id}"


def _set_track_info(url: str) -> dict[str, Any]:
    track_id = next(i for i, u in _SPITE_TRACKS if u == url)
    return {
        "id": track_id,
        "title": f"Song {track_id}",
        "artists": ["Levi Ryan"],
        "uploader": "Levi Ryan",
        "webpage_url": _page_of(track_id, url),
    }


def test_a_video_download_of_a_track_is_saved_as_audio(
    fake_ydl: type[FakeYoutubeDL], jobs: Callable[[], JobManager], library: Library
) -> None:
    fake_ydl.result = dict(TRACK_80M)
    fake_ydl.on_extract = _fake_download(TRACK_80M)
    manager = jobs()
    job = manager.submit(TRACK_URL, media="video")
    assert manager.wait_idle(WAIT)
    assert job.status == JobStatus.DONE, job.error
    assert job.media == "audio" and job.message == "SoundCloud links are saved as audio"
    assert library.has("soundcloud:343609555") and not library.has("soundcloud:343609555:video")
    assert fake_ydl.instances[-1].opts["format"] == YDL_FORMAT


def test_a_set_through_the_downloader(
    fake_ydl: type[FakeYoutubeDL], jobs: Callable[[], JobManager], library: Library
) -> None:
    infos = {url: _set_track_info(url) for _, url in _SPITE_TRACKS}
    fake_ydl.result = lambda url: SET_ALBUM if url == SET_URL else infos[url]
    fake_ydl.on_extract = _fake_download(infos)
    manager = jobs()
    parent = manager.submit(SET_URL, media="video")
    assert manager.wait_idle(WAIT)
    assert parent.status == JobStatus.DONE
    assert parent.message == "Album: out of spite (8 tracks); SoundCloud links are saved as audio"
    kids = [j for j in manager.list() if j.parent_id == parent.id]
    assert len(kids) == 8 and {k.status for k in kids} == {JobStatus.DONE}
    assert {k.media for k in kids} == {"audio"}
    for track_id, url in _SPITE_TRACKS:
        track = library.get(f"soundcloud:{track_id}")
        assert track is not None and track.album == "out of spite"
        assert track.source_url == _page_of(track_id, url)  # never an API link
    stubs = [k for k in kids if k.url.startswith("https://api-v2.soundcloud.com/")]
    assert len(stubs) == 3  # downloaded by their API link, named by their page


def test_queue_rows_of_a_set_have_a_title_before_they_download() -> None:
    ref = TrackRef(provider="soundcloud", source_id="1", url="u", title="out of spite · track 6")
    assert Job(url="https://api-v2.soundcloud.com/tracks/1", track_ref=ref).title == (
        "out of spite · track 6"
    )


# ----------------------------------------------------------------------------------------------
# doctor() and logging
# ----------------------------------------------------------------------------------------------


def test_doctor_needs_ffmpeg_and_yt_dlp() -> None:
    checks = SoundCloudProvider(Settings()).doctor()
    assert [label for _, label, _ in checks] == ["ffmpeg", "yt-dlp"]
    assert all(ok for ok, _, _ in checks)  # conftest stubs ffmpeg


def test_doctor_reports_a_missing_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    from ultimate_playlist.ffmpeg import FfmpegInfo, install_hint

    missing = FfmpegInfo(path=None, version=None)
    monkeypatch.setattr(youtube, "find_ffmpeg", lambda explicit=None: missing)
    (ffmpeg, _) = SoundCloudProvider(Settings()).doctor()
    assert ffmpeg == (False, "ffmpeg", install_hint())


def test_registered_users_notice_is_not_a_console_warning(caplog: pytest.LogCaptureFixture) -> None:
    """yt-dlp warns on every track whose uploader allows downloads of the original file; the
    stream is what Ultimate Playlist saves anyway."""
    logger = youtube._YdlLogger()
    with caplog.at_level(logging.DEBUG, logger="ultimate_playlist.youtube"):
        logger.warning(
            "WARNING: [soundcloud] 343609555: Original download format is only available for "
            "registered users. Use --cookies, --cookies-from-browser, --username and --password"
        )
        logger.warning("WARNING: something else")
    levels = {r.getMessage().split(":", 1)[0]: r.levelno for r in caplog.records}
    assert levels["[soundcloud] 343609555"] == logging.DEBUG
    assert levels["something else"] == logging.WARNING
