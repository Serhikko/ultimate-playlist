"""Offline tests for spotify_meta: link parsing, embed page parsing and the Web API client.

No test talks to Spotify: HTTP goes through `FakeSession`, embed pages come from the sanitised
fixtures in tests/fixtures/spotify (real ``__NEXT_DATA__`` entities trimmed to what we use), and
a Spotify connection is a token file written into the per-test app data folder.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
import requests

from ultimate_playlist.config import Settings, app_data_dir
from ultimate_playlist.providers import spotify_auth as sa
from ultimate_playlist.providers import spotify_meta as sm
from ultimate_playlist.providers.base import DownloadCancelled, ProviderError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "spotify"
TRACK_ID = "0dEIca2nhcxDUV8C5QkPYb"
ALBUM_ID = "4m2880jivSbbyEGAKfITCa"
PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"
NBSP = "\xa0"


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------


def load_entity(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"embed_{name}.json").read_text(encoding="utf-8"))


def embed_html(page_props: dict[str, Any]) -> str:
    page = {"props": {"pageProps": page_props}}
    return (
        "<html><head><title>x</title></head><body><div>ignored</div>"
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(page, ensure_ascii=False)
        + "</script></body></html>"
    )


def entity_html(entity: dict[str, Any]) -> str:
    return embed_html({"state": {"data": {"entity": entity}}})


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        *,
        json_data: Any = None,
        text: str | None = None,
        url: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self._json = json_data
        self.text = text if text is not None else (json.dumps(json_data) if json_data else "")
        self.url = url
        self.headers = headers or {}
        self.content = self.text.encode("utf-8")

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json body")
        return self._json


Handler = FakeResponse | list[FakeResponse] | Exception


class FakeSession:
    """Routes (method, url-substring) -> response, a list of responses (consumed in order) or
    an exception to raise. Records every call."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Handler]] = []
        self.calls: list[dict[str, Any]] = []

    def add(self, method: str, url_part: str, handler: Handler) -> None:
        self.routes.append((method.upper(), url_part, handler))

    def _dispatch(self, method: str, url: str, **kw: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kw})
        for m, part, handler in self.routes:
            if m == method and part in url:
                if isinstance(handler, Exception):
                    raise handler
                if isinstance(handler, list):
                    if not handler:
                        raise AssertionError(f"no responses left for {method} {url}")
                    return handler.pop(0) if len(handler) > 1 else handler[0]
                return handler
        raise AssertionError(f"unexpected request {method} {url}")

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_redirects: bool = True,
    ) -> FakeResponse:
        full = url + ("?" + urlencode(params) if params else "")
        return self._dispatch("GET", full, params=params, headers=headers, timeout=timeout)

    def head(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_redirects: bool = True,
    ) -> FakeResponse:
        return self._dispatch("HEAD", url, headers=headers, timeout=timeout)

    def post(
        self,
        url: str,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> FakeResponse:
        return self._dispatch("POST", url, data=data, headers=headers, timeout=timeout)


def token_response(token: str = "tok1", expires_in: int = 3600) -> FakeResponse:
    return FakeResponse(
        json_data={"access_token": token, "token_type": "Bearer", "expires_in": expires_in}
    )


def api_track(track_id: str = TRACK_ID, **over: Any) -> dict[str, Any]:
    track: dict[str, Any] = {
        "id": track_id,
        "name": "Give Life Back to Music",
        "type": "track",
        "is_local": False,
        "duration_ms": 275386,
        "track_number": 1,
        "external_ids": {"isrc": "USQX91300102"},
        "artists": [{"name": "Daft Punk"}],
        "album": {
            "name": "Random Access Memories",
            "release_date": "2013-05-17",
            "artists": [{"name": "Daft Punk"}],
            "images": [
                {"url": "https://i.scdn.co/image/300", "width": 300, "height": 300},
                {"url": "https://i.scdn.co/image/640", "width": 640, "height": 640},
                {"url": "https://i.scdn.co/image/64", "width": 64, "height": 64},
            ],
        },
    }
    track.update(over)
    return track


CLIENT_ID = "my-client-id"


def store_connection(client_id: str = CLIENT_ID, expires_in: float = 3600) -> None:
    """Pretend the user clicked Connect Spotify: a token file bound to `client_id`."""
    record = {
        "refresh_token": "refresh1",
        "access_token": "tok1",
        "expires_at": time.time() + expires_in,
        "scope": sa.SCOPES,
        "user_id": "listener42",
        "display_name": "Test Listener",
        "client_id": client_id,
    }
    (app_data_dir() / sa.TOKEN_FILE_NAME).write_text(json.dumps(record), encoding="utf-8")


@pytest.fixture(autouse=True)
def no_real_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A client that would open a real HTTP session fails the test instead."""

    def refuse() -> Any:
        raise AssertionError("a test tried to open a real HTTP session")

    monkeypatch.setattr(sm.requests, "Session", refuse)


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    """One FakeSession for everything: explicit sessions, the sessions the clients would open
    themselves and spotify_auth's token renewals."""
    fake = FakeSession()
    monkeypatch.setattr(sa, "_session", fake)
    monkeypatch.setattr(sm.requests, "Session", lambda: fake)
    return fake


@pytest.fixture
def connected(session: FakeSession) -> FakeSession:
    store_connection()
    return session


def api_client(session: FakeSession) -> sm.UserApiClient:
    return sm.UserApiClient(CLIENT_ID, session=session, sleep=lambda s: None)


# ----------------------------------------------------------------------------------------------
# parse_spotify_url
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://open.spotify.com/track/{TRACK_ID}", ("track", TRACK_ID)),
        (f"https://open.spotify.com/intl-de/track/{TRACK_ID}?si=abc123", ("track", TRACK_ID)),
        (f"https://open.spotify.com/intl-pt-br/album/{ALBUM_ID}/", ("album", ALBUM_ID)),
        (f"http://play.spotify.com/playlist/{PLAYLIST_ID}", ("playlist", PLAYLIST_ID)),
        (f"open.spotify.com/track/{TRACK_ID}", ("track", TRACK_ID)),
        (f"www.open.spotify.com/track/{TRACK_ID}", ("track", TRACK_ID)),
        (f"  https://open.spotify.com/track/{TRACK_ID}#fragment  ", ("track", TRACK_ID)),
        (f"https://open.spotify.com/embed/track/{TRACK_ID}?utm_source=x", ("track", TRACK_ID)),
        (
            f"https://open.spotify.com/user/someone/playlist/{PLAYLIST_ID}",
            ("playlist", PLAYLIST_ID),
        ),
        (f"https://open.spotify.com/track/{TRACK_ID}/extra/segments", ("track", TRACK_ID)),
        (f"spotify:track:{TRACK_ID}", ("track", TRACK_ID)),
        (f"SPOTIFY:ALBUM:{ALBUM_ID}", ("album", ALBUM_ID)),
        (f"spotify:user:someone:playlist:{PLAYLIST_ID}", ("playlist", PLAYLIST_ID)),
        ("https://open.spotify.com/collection/tracks", ("liked", "tracks")),
        ("https://open.spotify.com/intl-de/collection/tracks?si=1", ("liked", "tracks")),
        ("open.spotify.com/collection/tracks/", ("liked", "tracks")),
        ("spotify:collection:tracks", ("liked", "tracks")),
    ],
)
def test_parse_supported_links(url: str, expected: tuple[str, str]) -> None:
    assert sm.parse_spotify_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi",
        "https://open.spotify.com/intl-de/artist/4tZwfgrHOc3mvqYlEYSvVi",
        "https://open.spotify.com/show/4rOoJ6Egrf8K2IrywzwOMk",
        "https://open.spotify.com/episode/512ojhOuo1ktJprKbVcKyQ",
        "https://open.spotify.com/user/someone",
        "https://open.spotify.com/concert/abc123def456",
        "spotify:artist:4tZwfgrHOc3mvqYlEYSvVi",
        "spotify:show:4rOoJ6Egrf8K2IrywzwOMk",
        "spotify:nonsense",
        "https://open.spotify.com/collection",
        "https://open.spotify.com/collection/albums",
        "https://open.spotify.com/intl-fr/collection/your-episodes",
        "spotify:collection:albums",
    ],
)
def test_parse_unsupported_kinds(url: str) -> None:
    with pytest.raises(ProviderError) as exc_info:
        sm.parse_spotify_url(url)
    assert str(exc_info.value) == sm.UNSUPPORTED_KINDS_MESSAGE


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "https://open.spotify.com/",
        "https://open.spotify.com/track/",
        "https://open.spotify.com/track/not a valid id!",
        "https://example.com/track/0dEIca2nhcxDUV8C5QkPYb",
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "just words",
    ],
)
def test_parse_not_a_spotify_link(url: str) -> None:
    with pytest.raises(ProviderError) as exc_info:
        sm.parse_spotify_url(url)
    assert str(exc_info.value) == sm.NOT_A_LINK_MESSAGE


def test_short_link_resolved_by_head() -> None:
    session = FakeSession()
    session.add(
        "HEAD",
        "spotify.link/abc",
        FakeResponse(url=f"https://open.spotify.com/track/{TRACK_ID}?si=x"),
    )
    assert sm.parse_spotify_url("https://spotify.link/abc", session=session) == ("track", TRACK_ID)
    (call,) = session.calls
    assert call["method"] == "HEAD"
    assert call["headers"]["User-Agent"].startswith("Mozilla/5.0")
    assert call["timeout"] == sm.SHORT_LINK_TIMEOUT


def test_short_link_falls_back_to_get_then_page_text() -> None:
    session = FakeSession()
    session.add("HEAD", "spotify.app.link/xyz", FakeResponse(url="https://spotify.app.link/xyz"))
    session.add(
        "GET",
        "spotify.app.link/xyz",
        FakeResponse(url=f"https://open.spotify.com/album/{ALBUM_ID}"),
    )
    assert sm.parse_spotify_url("https://spotify.app.link/xyz", session=session) == (
        "album",
        ALBUM_ID,
    )
    assert [c["method"] for c in session.calls] == ["HEAD", "GET"]

    session = FakeSession()
    session.add("HEAD", "spotify.link/p", FakeResponse(url="https://spotify.link/p"))
    session.add(
        "GET",
        "spotify.link/p",
        FakeResponse(
            url="https://spotify.link/p",
            text=f'<a href="https://open.spotify.com/playlist/{PLAYLIST_ID}?si=1">open</a>',
        ),
    )
    assert sm.parse_spotify_url("spotify.link/p", session=session) == ("playlist", PLAYLIST_ID)


def test_short_link_errors_are_friendly() -> None:
    session = FakeSession()
    session.add("HEAD", "spotify.link/dead", requests.ConnectionError("boom"))
    with pytest.raises(ProviderError) as exc_info:
        sm.parse_spotify_url("https://spotify.link/dead", session=session)
    assert "Could not open that Spotify short link" in str(exc_info.value)

    session = FakeSession()
    session.add(
        "HEAD", "spotify.link/nowhere", FakeResponse(url="https://www.spotify.com/download")
    )
    session.add(
        "GET",
        "spotify.link/nowhere",
        FakeResponse(url="https://www.spotify.com/download", text="nope"),
    )
    with pytest.raises(ProviderError) as exc_info:
        sm.parse_spotify_url("https://spotify.link/nowhere", session=session)
    assert "did not lead to a track, album or playlist" in str(exc_info.value)

    session = FakeSession()  # a short link to an artist page is still an unsupported kind
    session.add(
        "HEAD",
        "spotify.link/art",
        FakeResponse(url="https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi"),
    )
    with pytest.raises(ProviderError) as exc_info:
        sm.parse_spotify_url("https://spotify.link/art", session=session)
    assert str(exc_info.value) == sm.UNSUPPORTED_KINDS_MESSAGE


# ----------------------------------------------------------------------------------------------
# embed parsing
# ----------------------------------------------------------------------------------------------


def test_split_artists() -> None:
    assert sm.split_artists(f"KAROL G,{NBSP}Judeline,{NBSP}rusowsky") == [
        "KAROL G",
        "Judeline",
        "rusowsky",
    ]
    assert sm.split_artists("Tyler, The Creator") == ["Tyler, The Creator"]
    assert sm.split_artists("Daft Punk") == ["Daft Punk"]
    assert sm.split_artists(f"  A ,{NBSP} B  ") == ["A", "B"]
    assert sm.split_artists("") == []
    assert sm.split_artists(None) == []


def test_parse_track_entity() -> None:
    entity = sm.parse_embed_html(entity_html(load_entity("track")))
    assert entity.kind == "track"
    assert entity.id == TRACK_ID
    assert entity.name == "Give Life Back to Music"
    assert entity.artists == ["Daft Punk"]
    assert entity.album is None  # the embed page of a track does not name the album
    assert entity.duration == pytest.approx(275.386)
    assert entity.cover_url is not None and entity.cover_url.endswith(
        "ab67616d0000b2739b9b36b0e22870b9f542d937"
    )
    assert entity.release_year == "2013"
    assert entity.track_number is None
    assert entity.isrc is None
    assert entity.tracks == []
    assert entity.truncated is False
    assert entity.source == "embed"
    assert entity.url == f"https://open.spotify.com/track/{TRACK_ID}"


def test_parse_album_entity() -> None:
    entity = sm.parse_embed_html(entity_html(load_entity("album")))
    assert entity.kind == "album"
    assert entity.id == ALBUM_ID
    assert entity.name == "Random Access Memories"
    assert entity.album == "Random Access Memories"
    assert entity.album_artist == "Daft Punk"
    assert entity.artists == ["Daft Punk"]
    assert entity.cover_url is not None and "0000b273" in entity.cover_url  # the 640 px image
    assert entity.truncated is False
    assert len(entity.tracks) == 4  # the fixture has 5 items, one of them not playable
    first = entity.tracks[0]
    assert first.id == TRACK_ID
    assert first.name == "Give Life Back to Music"
    assert first.artists == ["Daft Punk"]
    assert first.album == "Random Access Memories"
    assert first.album_artist == "Daft Punk"
    assert first.duration == pytest.approx(275.386)
    assert first.cover_url == entity.cover_url
    assert first.track_number == 1
    assert [t.track_number for t in entity.tracks] == [1, 2, 3, 4]
    assert [t.name for t in entity.tracks] == [
        "Give Life Back to Music",
        "The Game of Love",
        "Giorgio by Moroder",
        "Within",
    ]
    assert all(t.source == "embed" for t in entity.tracks)


def test_parse_playlist_entity() -> None:
    entity = sm.parse_embed_html(entity_html(load_entity("playlist")))
    assert entity.kind == "playlist"
    assert entity.id == PLAYLIST_ID
    assert entity.name.startswith("Today")
    assert entity.album is None
    assert entity.album_artist == "Spotify"  # the owner
    assert entity.cover_url is not None and entity.cover_url.endswith(
        "ab67706f00000003aeedf02d103abf921c9a1868"
    )
    assert entity.truncated is False
    names = [t.name for t in entity.tracks]
    assert "Ain't In LA" not in names  # not playable -> dropped
    assert len(entity.tracks) == 6
    first = entity.tracks[0]
    assert first.name == "BbY WOW"
    assert first.artists == ["KAROL G", "Judeline", "rusowsky"]  # NBSP-joined subtitle
    assert NBSP not in ", ".join(first.artists)
    assert first.album is None and first.cover_url is None and first.track_number is None
    assert first.duration == pytest.approx(225.834)
    tyler = entity.tracks[-1]
    assert tyler.name == "EARFQUAKE"
    assert tyler.artists == ["Tyler, The Creator"]  # a comma inside one name is not a separator


def test_truncated_flag_at_the_embed_cap() -> None:
    def playlist_with(count: int) -> dict[str, Any]:
        return {
            "type": "playlist",
            "name": "Big",
            "id": PLAYLIST_ID,
            "uri": f"spotify:playlist:{PLAYLIST_ID}",
            "trackList": [
                {
                    "uri": f"spotify:track:{i:022d}",
                    "title": f"Song {i}",
                    "subtitle": "Someone",
                    "duration": 1000 * (100 + i),
                    "isPlayable": True,
                }
                for i in range(count)
            ],
        }

    full = sm.entity_from_embed(playlist_with(sm.EMBED_LIST_CAP))
    assert full.truncated is True
    assert len(full.tracks) == sm.EMBED_LIST_CAP
    assert full.cover_url is None
    short = sm.entity_from_embed(playlist_with(sm.EMBED_LIST_CAP - 1))
    assert short.truncated is False


def test_cover_prefers_largest_visual_identity_then_cover_art() -> None:
    entity = {
        "type": "track",
        "id": TRACK_ID,
        "name": "x",
        "artists": [],
        "visualIdentity": {
            "image": [
                {"url": "small", "maxWidth": 64},
                {"url": "big", "maxWidth": 640},
                {"url": "mid", "maxWidth": 300},
            ]
        },
        "coverArt": {"sources": [{"url": "cover-art"}]},
    }
    assert sm.entity_from_embed(entity).cover_url == "big"
    entity["visualIdentity"] = {"image": []}
    assert sm.entity_from_embed(entity).cover_url == "cover-art"
    entity["coverArt"] = None
    assert sm.entity_from_embed(entity).cover_url is None


def test_parse_embed_html_errors() -> None:
    with pytest.raises(ProviderError) as exc_info:
        sm.parse_embed_html(embed_html({"status": 404, "title": "Page not found"}))
    assert str(exc_info.value) == sm.NOT_FOUND_MESSAGE
    with pytest.raises(ProviderError) as exc_info:
        sm.parse_embed_html("<html><body>no script here</body></html>")
    assert str(exc_info.value).startswith("Could not read that Spotify page")
    with pytest.raises(ProviderError) as exc_info:
        sm.parse_embed_html('<script id="__NEXT_DATA__" type="application/json">{broken</script>')
    assert str(exc_info.value).startswith("Could not read that Spotify page")
    with pytest.raises(ProviderError) as exc_info:
        sm.parse_embed_html(entity_html({"type": "artist", "id": "x", "name": "y"}))
    assert str(exc_info.value) == sm.UNSUPPORTED_KINDS_MESSAGE
    with pytest.raises(ProviderError):
        sm.parse_embed_html(entity_html({"type": "track", "id": "", "name": ""}))


def test_embed_client_fetch() -> None:
    session = FakeSession()
    session.add(
        "GET",
        f"open.spotify.com/embed/track/{TRACK_ID}",
        FakeResponse(text=entity_html(load_entity("track"))),
    )
    entity = sm.EmbedClient(session=session).fetch("track", TRACK_ID)
    assert entity.name == "Give Life Back to Music"
    (call,) = session.calls
    assert call["url"] == f"https://open.spotify.com/embed/track/{TRACK_ID}"
    assert call["headers"]["User-Agent"].startswith("Mozilla/5.0")
    assert call["timeout"] == sm.HTTP_TIMEOUT


def test_embed_client_errors() -> None:
    session = FakeSession()
    session.add("GET", "/embed/track/gone", FakeResponse(404, text="nope"))
    session.add("GET", "/embed/album/soft404", FakeResponse(text=embed_html({"status": 404})))
    session.add("GET", "/embed/track/down", FakeResponse(503, text="oops"))
    session.add("GET", "/embed/track/net", requests.ConnectionError("offline"))
    client = sm.EmbedClient(session=session)
    with pytest.raises(ProviderError, match="does not exist or is private"):
        client.fetch("track", "gone000000")
    with pytest.raises(ProviderError, match="does not exist or is private"):
        client.fetch("album", "soft404000")
    with pytest.raises(ProviderError, match=r"Could not read that Spotify page \(HTTP 503\)"):
        client.fetch("track", "down000000")
    with pytest.raises(ProviderError, match="Could not reach Spotify"):
        client.fetch("track", "net0000000")
    with pytest.raises(ProviderError, match="Only Spotify tracks"):
        client.fetch("artist", "x000000000")


# ----------------------------------------------------------------------------------------------
# Web API client (as the connected user)
# ----------------------------------------------------------------------------------------------


def playlist_meta(**over: Any) -> dict[str, Any]:
    """GET /playlists/{id} for a playlist the user owns: metadata plus an `items` object."""
    data: dict[str, Any] = {
        "id": PLAYLIST_ID,
        "name": "Road trip",
        "owner": {"id": "listener42", "display_name": "someone"},
        "images": [{"url": "https://i.scdn.co/image/pl", "width": None}],
        "items": {"href": "x", "total": 7, "items": [], "next": None},
    }
    data.update(over)
    return data


def test_fetch_track(connected: FakeSession) -> None:
    connected.add("GET", f"/v1/tracks/{TRACK_ID}", FakeResponse(json_data=api_track()))
    entity = api_client(connected).fetch("track", TRACK_ID)
    assert entity.kind == "track" and entity.id == TRACK_ID
    assert entity.name == "Give Life Back to Music"
    assert entity.artists == ["Daft Punk"]
    assert entity.album == "Random Access Memories"
    assert entity.album_artist == "Daft Punk"
    assert entity.duration == pytest.approx(275.386)
    assert entity.cover_url == "https://i.scdn.co/image/640"  # the widest image
    assert entity.release_year == "2013"
    assert entity.track_number == 1
    assert entity.isrc == "USQX91300102"
    assert entity.source == "api"
    assert entity.truncated is False
    (call,) = connected.calls
    assert call["url"] == f"{sm.API_BASE}/tracks/{TRACK_ID}"
    assert call["headers"]["Authorization"] == "Bearer tok1"
    assert call["timeout"] == sm.HTTP_TIMEOUT


def test_fetch_album_paginates_and_copies_album_fields(connected: FakeSession) -> None:
    page2_url = f"{sm.API_BASE}/albums/{ALBUM_ID}/tracks?offset=2&limit=2"

    def simple(i: int, **over: Any) -> dict[str, Any]:
        item = {
            "id": f"track{i:018d}",
            "name": f"Song {i}",
            "type": "track",
            "duration_ms": 1000 * (200 + i),
            "track_number": i,
            "is_local": False,
            "artists": [{"name": "Daft Punk"}],
        }
        item.update(over)
        return item

    album = {
        "id": ALBUM_ID,
        "name": "Random Access Memories",
        "release_date": "2013-05-17",
        "artists": [{"name": "Daft Punk"}],
        "images": [{"url": "https://i.scdn.co/image/640", "width": 640}],
        "tracks": {"items": [simple(1), simple(2)], "next": page2_url},
    }
    connected.add(
        "GET",
        f"/v1/albums/{ALBUM_ID}/tracks?offset=2",
        FakeResponse(
            json_data={
                "items": [simple(3, is_playable=False), simple(4, is_local=True), simple(5)],
                "next": None,
            }
        ),
    )
    connected.add("GET", f"/v1/albums/{ALBUM_ID}", FakeResponse(json_data=album))

    entity = api_client(connected).fetch("album", ALBUM_ID)
    assert entity.kind == "album" and entity.name == "Random Access Memories"
    assert entity.album_artist == "Daft Punk" and entity.release_year == "2013"
    assert [t.name for t in entity.tracks] == ["Song 1", "Song 2", "Song 5"]
    for track in entity.tracks:
        assert track.album == "Random Access Memories"
        assert track.album_artist == "Daft Punk"
        assert track.cover_url == "https://i.scdn.co/image/640"
        assert track.release_year == "2013"
        assert track.source == "api"
    assert [t.track_number for t in entity.tracks] == [1, 2, 5]
    urls = [c["url"] for c in connected.calls if c["method"] == "GET"]
    assert urls == [f"{sm.API_BASE}/albums/{ALBUM_ID}", page2_url]


def test_fetch_album_without_embedded_tracks_uses_the_tracks_endpoint(
    connected: FakeSession,
) -> None:
    connected.add(
        "GET",
        f"/v1/albums/{ALBUM_ID}/tracks?limit=50",
        FakeResponse(
            json_data={
                "items": [{"id": "a" * 22, "name": "Only", "duration_ms": 1000, "artists": []}],
                "next": None,
            }
        ),
    )
    connected.add(
        "GET", f"/v1/albums/{ALBUM_ID}", FakeResponse(json_data={"id": ALBUM_ID, "name": "A"})
    )
    entity = api_client(connected).fetch("album", ALBUM_ID)
    assert [t.name for t in entity.tracks] == ["Only"]
    assert entity.tracks[0].album == "A"


def test_fetch_playlist_items_paginates_and_skips_junk(connected: FakeSession) -> None:
    page2_url = f"{sm.API_BASE}/playlists/{PLAYLIST_ID}/items?offset=50&limit=50"
    connected.add(
        "GET",
        f"/v1/playlists/{PLAYLIST_ID}/items?offset=50",
        FakeResponse(
            json_data={
                "items": [
                    {"is_local": False, "item": api_track("c" * 22, name="Via item key")},
                    {"is_local": False, "track": api_track("d" * 22, name="Via old track key")},
                ],
                "next": None,
            }
        ),
    )
    connected.add(
        "GET",
        f"/v1/playlists/{PLAYLIST_ID}/items?",
        FakeResponse(
            json_data={
                "items": [
                    {"is_local": False, "item": api_track("a" * 22, name="First")},
                    {"is_local": False, "item": None, "track": None},
                    {
                        "is_local": True,
                        "item": api_track("b" * 22, name="Local file", is_local=True),
                    },
                    {
                        "is_local": False,
                        "item": {"id": "e" * 22, "name": "Podcast", "type": "episode"},
                    },
                    {"is_local": False, "item": {"id": None, "name": "No id"}},
                ],
                "next": page2_url,
            }
        ),
    )
    connected.add("GET", f"/v1/playlists/{PLAYLIST_ID}", FakeResponse(json_data=playlist_meta()))

    entity = api_client(connected).fetch("playlist", PLAYLIST_ID)

    assert entity.kind == "playlist" and entity.name == "Road trip" and entity.source == "api"
    assert entity.album_artist == "someone"
    assert entity.cover_url == "https://i.scdn.co/image/pl"
    assert entity.truncated is False
    assert [t.name for t in entity.tracks] == ["First", "Via item key", "Via old track key"]
    assert entity.tracks[0].album == "Random Access Memories"
    assert entity.tracks[0].isrc == "USQX91300102"
    gets = [c for c in connected.calls if c["method"] == "GET"]
    assert gets[0]["url"] == f"{sm.API_BASE}/playlists/{PLAYLIST_ID}"
    assert gets[1]["url"] == f"{sm.API_BASE}/playlists/{PLAYLIST_ID}/items?limit=50"
    assert gets[1]["params"] == {"limit": sm.API_PAGE_LIMIT}
    assert gets[2]["url"] == page2_url
    assert not any("/tracks" in c["url"] for c in gets)  # the removed endpoint is never used


def test_playlist_without_items_is_not_yours() -> None:
    session = FakeSession()
    session.add("GET", f"/v1/playlists/{PLAYLIST_ID}", FakeResponse(json_data={"id": "x"}))
    store_connection()
    with pytest.raises(sm.NotYourPlaylist):
        sm.UserApiClient(CLIENT_ID, session=session).fetch("playlist", PLAYLIST_ID)


def test_not_owned_playlist_falls_back_to_the_embed_page(
    connected: FakeSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Metadata only, no `items`: someone else's playlist since the 2026 API change.
    meta = playlist_meta()
    del meta["items"]
    connected.add("GET", f"/v1/playlists/{PLAYLIST_ID}", FakeResponse(json_data=meta))
    connected.add(
        "GET",
        f"open.spotify.com/embed/playlist/{PLAYLIST_ID}",
        FakeResponse(text=entity_html(load_entity("playlist"))),
    )
    with caplog.at_level(logging.WARNING, logger=sm.__name__):
        entity = sm.get_metadata("playlist", PLAYLIST_ID, settings_with(tmp_path, CLIENT_ID))
    assert entity.source == "embed" and len(entity.tracks) == 6
    assert "Spotify only shares the first 100 songs of playlists you don't own" in caplog.text
    assert not any("/items" in c["url"] for c in connected.calls)


@pytest.mark.parametrize("status", [403, 404])
def test_refused_playlist_items_fall_back_to_the_embed_page(
    connected: FakeSession, tmp_path: Path, status: int
) -> None:
    connected.add("GET", f"/v1/playlists/{PLAYLIST_ID}/items", FakeResponse(status, json_data={}))
    connected.add("GET", f"/v1/playlists/{PLAYLIST_ID}", FakeResponse(json_data=playlist_meta()))
    connected.add(
        "GET",
        f"open.spotify.com/embed/playlist/{PLAYLIST_ID}",
        FakeResponse(text=entity_html(load_entity("playlist"))),
    )
    entity = sm.get_metadata("playlist", PLAYLIST_ID, settings_with(tmp_path, CLIENT_ID))
    assert entity.source == "embed"


def test_spotify_made_playlist_and_missing_playlist(connected: FakeSession, tmp_path: Path) -> None:
    # The API answers 404 for Spotify's own playlists; the public page still has them.
    connected.add("GET", f"/v1/playlists/{PLAYLIST_ID}", FakeResponse(404, json_data={}))
    connected.add("GET", f"open.spotify.com/embed/playlist/{PLAYLIST_ID}", FakeResponse(404))
    with pytest.raises(ProviderError) as exc_info:
        sm.get_metadata("playlist", PLAYLIST_ID, settings_with(tmp_path, CLIENT_ID))
    assert str(exc_info.value) == sm.NOT_FOUND_MESSAGE  # gone from both: the page's answer


def test_fetch_liked_songs(connected: FakeSession) -> None:
    page2_url = f"{sm.API_BASE}/me/tracks?offset=50&limit=50"
    connected.add(
        "GET",
        "/v1/me/tracks?offset=50",
        FakeResponse(
            json_data={
                "items": [{"added_at": "2026-01-01T00:00:00Z", "track": api_track("f" * 22)}],
                "next": None,
            }
        ),
    )
    connected.add(
        "GET",
        "/v1/me/tracks?limit=50",
        FakeResponse(
            json_data={
                "items": [
                    {"added_at": "2026-01-02T00:00:00Z", "track": api_track("a" * 22, name="A")},
                    {"added_at": "2026-01-03T00:00:00Z", "track": None},
                    {"track": api_track("b" * 22, name="Local", is_local=True)},
                    {"item": api_track("c" * 22, name="Item key")},
                ],
                "next": page2_url,
            }
        ),
    )
    entity = api_client(connected).fetch(sm.LIKED_KIND, sm.LIKED_ID)
    assert entity.kind == "liked" and entity.id == "tracks" and entity.name == "Liked Songs"
    assert entity.url == "https://open.spotify.com/collection/tracks"
    assert entity.source == "api" and entity.truncated is False
    assert [t.name for t in entity.tracks] == ["A", "Item key", "Give Life Back to Music"]
    assert [c["url"] for c in connected.calls] == [
        f"{sm.API_BASE}/me/tracks?limit=50",
        page2_url,
    ]


def test_liked_songs_need_a_connection(session: FakeSession, tmp_path: Path) -> None:
    for settings in (settings_with(tmp_path), settings_with(tmp_path, CLIENT_ID)):
        with pytest.raises(sa.SpotifyAuthError) as exc_info:
            sm.get_metadata(sm.LIKED_KIND, sm.LIKED_ID, settings, session=session)
        assert str(exc_info.value) == sm.LIKED_NEEDS_CONNECTION_MESSAGE
        assert str(exc_info.value) == "Connect Spotify in Settings to download your Liked Songs"
        assert isinstance(exc_info.value, ProviderError)
    store_connection(client_id="another-developer-app")  # connected, but through another app
    with pytest.raises(sa.SpotifyAuthError, match="Connect Spotify in Settings"):
        sm.get_metadata(sm.LIKED_KIND, sm.LIKED_ID, settings_with(tmp_path, CLIENT_ID))
    with pytest.raises(sa.SpotifyAuthError, match="Connect Spotify in Settings"):
        sm.EmbedClient(session=session).fetch(sm.LIKED_KIND, sm.LIKED_ID)
    assert session.calls == []


@pytest.mark.parametrize(
    ("kind", "status", "expected"),
    [
        ("track", 400, sm.NOT_FOUND_MESSAGE),
        ("track", 404, sm.NOT_FOUND_MESSAGE),
        ("album", 404, sm.NOT_FOUND_MESSAGE),
        ("track", 403, sm.FORBIDDEN_MESSAGE),
        ("album", 403, sm.FORBIDDEN_MESSAGE),
        ("liked", 403, sm.FORBIDDEN_MESSAGE),
        ("track", 500, "Spotify is having problems right now (HTTP 500). Try again later."),
        ("track", 418, "Spotify answered with HTTP 418."),
    ],
)
def test_api_error_mapping(connected: FakeSession, kind: str, status: int, expected: str) -> None:
    connected.add("GET", "/v1/", FakeResponse(status, json_data={"error": {"status": status}}))
    with pytest.raises(ProviderError) as exc_info:
        api_client(connected).fetch(kind, "x" * 22)
    assert str(exc_info.value) == expected
    assert not isinstance(exc_info.value, sm.NotYourPlaylist)


@pytest.mark.parametrize("status", [403, 404])
def test_refused_playlist_is_not_yours(connected: FakeSession, status: int) -> None:
    connected.add("GET", "/v1/playlists/", FakeResponse(status, json_data={}))
    with pytest.raises(sm.NotYourPlaylist):
        api_client(connected).fetch("playlist", PLAYLIST_ID)


def test_api_401_renews_the_token_once_then_gives_up(connected: FakeSession) -> None:
    connected.add("POST", sa.TOKEN_URL, token_response("tok2"))
    connected.add(
        "GET", "/v1/tracks/", [FakeResponse(401, json_data={}), FakeResponse(json_data=api_track())]
    )
    entity = api_client(connected).fetch("track", TRACK_ID)
    assert entity.name == "Give Life Back to Music"
    auths = [c["headers"]["Authorization"] for c in connected.calls if c["method"] == "GET"]
    assert auths == ["Bearer tok1", "Bearer tok2"]
    (post,) = [c for c in connected.calls if c["method"] == "POST"]
    assert post["data"]["grant_type"] == "refresh_token"

    session = FakeSession()  # still 401 with the renewed token: reconnect
    sa._session = session
    store_connection()
    session.add("POST", sa.TOKEN_URL, token_response("tok2"))
    session.add("GET", "/v1/tracks/", FakeResponse(401, json_data={}))
    with pytest.raises(sa.SpotifyAuthError) as exc_info:
        api_client(session).fetch("track", TRACK_ID)
    assert str(exc_info.value) == sa.EXPIRED_CONNECTION_MESSAGE
    assert [c["method"] for c in session.calls] == ["GET", "POST", "GET"]


def test_api_429_honours_retry_after_once(connected: FakeSession) -> None:
    slept: list[float] = []
    connected.add(
        "GET",
        "/v1/tracks/",
        [
            FakeResponse(429, json_data={}, headers={"Retry-After": "2"}),
            FakeResponse(json_data=api_track()),
        ],
    )
    client = sm.UserApiClient(CLIENT_ID, session=connected, sleep=slept.append)
    assert client.fetch("track", TRACK_ID).name == "Give Life Back to Music"
    assert sum(slept) == pytest.approx(2.0)

    slept.clear()
    session = FakeSession()
    session.add(
        "GET", "/v1/tracks/", FakeResponse(429, json_data={}, headers={"Retry-After": "1000"})
    )
    client = sm.UserApiClient(CLIENT_ID, session=session, sleep=slept.append)
    with pytest.raises(ProviderError) as exc_info:
        client.fetch("track", TRACK_ID)
    assert str(exc_info.value) == sm.RATE_LIMIT_MESSAGE
    assert sum(slept) == pytest.approx(sm.RETRY_AFTER_CAP)  # capped, and only waited once


def test_api_429_wait_is_cancellable(connected: FakeSession) -> None:
    cancel = threading.Event()
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        cancel.set()  # the user cancels while we wait

    connected.add(
        "GET", "/v1/tracks/", FakeResponse(429, json_data={}, headers={"Retry-After": "10"})
    )
    client = sm.UserApiClient(CLIENT_ID, session=connected, sleep=sleep)
    with pytest.raises(DownloadCancelled):
        client.fetch("track", TRACK_ID, cancel)
    assert len(slept) == 1


def test_api_network_error(connected: FakeSession) -> None:
    connected.add("GET", "/v1/", requests.ConnectionError("offline"))
    with pytest.raises(ProviderError) as exc_info:
        api_client(connected).fetch("track", TRACK_ID)
    assert str(exc_info.value) == sm.UNREACHABLE_MESSAGE


def test_max_pages_guard_stops_a_next_loop(
    connected: FakeSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sm, "MAX_PAGES", 3)
    loop_url = f"{sm.API_BASE}/me/tracks?offset=0&limit=50"
    connected.add(
        "GET",
        "/v1/me/tracks",
        FakeResponse(json_data={"items": [{"track": api_track()}], "next": loop_url}),
    )
    with caplog.at_level(logging.WARNING, logger=sm.__name__):
        entity = api_client(connected).fetch(sm.LIKED_KIND, sm.LIKED_ID)
    assert len(connected.calls) == 3
    assert len(entity.tracks) == 3
    assert "Stopped reading Spotify after 3 pages" in caplog.text


def test_next_link_to_another_host_is_not_followed(
    connected: FakeSession, caplog: pytest.LogCaptureFixture
) -> None:
    connected.add(
        "GET",
        "/v1/me/tracks",
        FakeResponse(
            json_data={"items": [{"track": api_track()}], "next": "https://evil.example/steal"}
        ),
    )
    entity = api_client(connected).fetch(sm.LIKED_KIND, sm.LIKED_ID)
    assert len(entity.tracks) == 1
    assert [c["url"] for c in connected.calls] == [f"{sm.API_BASE}/me/tracks?limit=50"]
    assert "Not following an unexpected Spotify page link" in caplog.text


def test_api_without_a_connection_raises(session: FakeSession) -> None:
    with pytest.raises(sa.SpotifyAuthError) as exc_info:
        sm.UserApiClient(CLIENT_ID, session=session).fetch("track", TRACK_ID)
    assert str(exc_info.value) == sm.NOT_CONNECTED_MESSAGE
    assert session.calls == []


# ----------------------------------------------------------------------------------------------
# get_metadata
# ----------------------------------------------------------------------------------------------


def settings_with(tmp_path: Path, client_id: str = "") -> Settings:
    settings = Settings(library_dir=tmp_path / "lib")
    settings.spotify_client_id = client_id
    return settings


def test_get_metadata_uses_embed_when_not_connected(session: FakeSession, tmp_path: Path) -> None:
    session.add(
        "GET", "open.spotify.com/embed/track/", FakeResponse(text=entity_html(load_entity("track")))
    )
    for settings in (settings_with(tmp_path), settings_with(tmp_path, CLIENT_ID)):
        entity = sm.get_metadata("track", TRACK_ID, settings, session=session)
        assert entity.source == "embed"
    assert all("api.spotify.com" not in c["url"] for c in session.calls)


def test_get_metadata_uses_the_api_when_connected(connected: FakeSession, tmp_path: Path) -> None:
    connected.add("GET", f"/v1/tracks/{TRACK_ID}", FakeResponse(json_data=api_track()))
    entity = sm.get_metadata("track", TRACK_ID, settings_with(tmp_path, CLIENT_ID))
    assert entity.source == "api" and entity.album == "Random Access Memories"
    assert all("embed" not in c["url"] for c in connected.calls)


def test_get_metadata_ignores_a_connection_made_with_another_client_id(
    session: FakeSession, tmp_path: Path
) -> None:
    store_connection(client_id="another-developer-app")
    session.add(
        "GET", "open.spotify.com/embed/track/", FakeResponse(text=entity_html(load_entity("track")))
    )
    entity = sm.get_metadata("track", TRACK_ID, settings_with(tmp_path, CLIENT_ID))
    assert entity.source == "embed"
    assert all("api.spotify.com" not in c["url"] for c in session.calls)


def test_get_metadata_expired_connection(
    session: FakeSession, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = settings_with(tmp_path, CLIENT_ID)
    store_connection(expires_in=-100)
    session.add("POST", sa.TOKEN_URL, FakeResponse(400, json_data={"error": "invalid_grant"}))
    session.add(
        "GET", "open.spotify.com/embed/track/", FakeResponse(text=entity_html(load_entity("track")))
    )
    # a track still works through the public page, with a warning saying why
    with caplog.at_level(logging.WARNING, logger=sm.__name__):
        entity = sm.get_metadata("track", TRACK_ID, settings)
    assert entity.source == "embed"
    assert sa.EXPIRED_CONNECTION_MESSAGE in caplog.text
    assert sa.current_account() is None  # the rejected connection is gone
    # ...Liked Songs cannot
    store_connection(expires_in=-100)
    with pytest.raises(sa.SpotifyAuthError) as exc_info:
        sm.get_metadata(sm.LIKED_KIND, sm.LIKED_ID, settings)
    assert str(exc_info.value) == sa.EXPIRED_CONNECTION_MESSAGE


@pytest.mark.parametrize(
    ("kind", "api_path", "handler"),
    [
        ("track", "/v1/tracks/", FakeResponse(403, json_data={})),  # the owner lost Premium
        ("track", "/v1/tracks/", FakeResponse(500, json_data={})),
        ("track", "/v1/tracks/", requests.ConnectionError("offline")),
        ("album", "/v1/albums/", FakeResponse(403, json_data={})),
        ("album", "/v1/albums/", FakeResponse(502, json_data={})),
        ("playlist", "/v1/playlists/", FakeResponse(500, json_data={})),
    ],
)
def test_get_metadata_falls_back_to_the_public_page_when_the_api_fails(
    connected: FakeSession,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    api_path: str,
    handler: Any,
) -> None:
    """Being connected must never make a link fail that works without a connection."""
    connected.add("GET", api_path, handler)
    connected.add(
        "GET", f"open.spotify.com/embed/{kind}/", FakeResponse(text=entity_html(load_entity(kind)))
    )
    with caplog.at_level(logging.WARNING, logger=sm.__name__):
        entity = sm.get_metadata(kind, "x" * 22, settings_with(tmp_path, CLIENT_ID))
    assert entity.source == "embed"
    assert "Spotify's Web API failed" in caplog.text
    assert any("api.spotify.com" in c["url"] for c in connected.calls)


def test_get_metadata_passes_a_cancel_on(connected: FakeSession, tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        sm.get_metadata("track", TRACK_ID, settings_with(tmp_path, CLIENT_ID), cancel)
    assert connected.calls == []  # neither the API nor the public page


def test_liked_songs_report_web_api_errors(connected: FakeSession, tmp_path: Path) -> None:
    connected.add("GET", "/v1/me/tracks", FakeResponse(403, json_data={}))
    with pytest.raises(ProviderError) as exc_info:
        sm.get_metadata(sm.LIKED_KIND, sm.LIKED_ID, settings_with(tmp_path, CLIENT_ID))
    assert str(exc_info.value) == sm.FORBIDDEN_MESSAGE


def test_client_id_of_tolerates_old_settings_objects(tmp_path: Path) -> None:
    class Old:
        pass

    assert sm.client_id_of(Old()) == ""
    assert sm.client_id_of(None) == ""
    assert sm.client_id_of(settings_with(tmp_path, " abc ")) == "abc"
    assert sm.connected_account(Old()) is None
