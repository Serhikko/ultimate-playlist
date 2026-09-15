"""Offline tests for spotify_meta: link parsing, embed page parsing and the Web API client.

No test talks to Spotify: HTTP goes through `FakeSession`, embed pages come from the sanitised
fixtures in tests/fixtures/spotify (real ``__NEXT_DATA__`` entities trimmed to what we use).
"""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
import requests

from ultimate_playlist.config import Settings
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


def api_client(session: FakeSession, **kw: Any) -> sm.WebApiClient:
    return sm.WebApiClient("my-id", "my-secret", session=session, sleep=lambda s: None, **kw)


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
# Web API client
# ----------------------------------------------------------------------------------------------


def test_token_request_and_caching() -> None:
    now = [1000.0]
    session = FakeSession()
    session.add(
        "POST", sm.TOKEN_URL, [token_response("first", 3600), token_response("second", 3600)]
    )
    session.add("GET", "/v1/tracks/", FakeResponse(json_data=api_track()))
    client = api_client(session, clock=lambda: now[0])

    client.fetch("track", TRACK_ID)
    client.fetch("track", TRACK_ID)
    posts = [c for c in session.calls if c["method"] == "POST"]
    assert len(posts) == 1  # cached
    expected = base64.b64encode(b"my-id:my-secret").decode("ascii")
    assert posts[0]["headers"]["Authorization"] == f"Basic {expected}"
    assert posts[0]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert posts[0]["data"] == {"grant_type": "client_credentials"}
    gets = [c for c in session.calls if c["method"] == "GET"]
    assert all(c["headers"]["Authorization"] == "Bearer first" for c in gets)

    now[0] += 3600  # past expires_in: a new token is requested
    client.fetch("track", TRACK_ID)
    assert len([c for c in session.calls if c["method"] == "POST"]) == 2
    assert session.calls[-1]["headers"]["Authorization"] == "Bearer second"


def test_token_errors() -> None:
    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, FakeResponse(400, json_data={"error": "invalid_client"}))
    with pytest.raises(ProviderError) as exc_info:
        api_client(session).fetch("track", TRACK_ID)
    assert str(exc_info.value) == sm.CREDENTIALS_MESSAGE

    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, requests.ConnectionError("offline"))
    with pytest.raises(ProviderError, match="Could not reach Spotify"):
        api_client(session).fetch("track", TRACK_ID)

    with pytest.raises(ProviderError) as exc_info:
        sm.WebApiClient("", "", session=FakeSession()).fetch("track", TRACK_ID)
    assert str(exc_info.value) == sm.CREDENTIALS_MESSAGE


def test_fetch_track() -> None:
    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, token_response())
    session.add("GET", f"/v1/tracks/{TRACK_ID}", FakeResponse(json_data=api_track()))
    entity = api_client(session).fetch("track", TRACK_ID)
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


def test_fetch_album_paginates_and_copies_album_fields() -> None:
    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, token_response())
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
    session.add(
        "GET",
        f"/v1/albums/{ALBUM_ID}/tracks?offset=2",
        FakeResponse(
            json_data={
                "items": [simple(3, is_playable=False), simple(4, is_local=True), simple(5)],
                "next": None,
            }
        ),
    )
    session.add("GET", f"/v1/albums/{ALBUM_ID}", FakeResponse(json_data=album))

    entity = api_client(session).fetch("album", ALBUM_ID)
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
    urls = [c["url"] for c in session.calls if c["method"] == "GET"]
    assert urls == [f"{sm.API_BASE}/albums/{ALBUM_ID}", page2_url]


def test_fetch_album_without_embedded_tracks_uses_the_tracks_endpoint() -> None:
    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, token_response())
    session.add(
        "GET",
        f"/v1/albums/{ALBUM_ID}/tracks?limit=50",
        FakeResponse(
            json_data={
                "items": [{"id": "a" * 22, "name": "Only", "duration_ms": 1000, "artists": []}],
                "next": None,
            }
        ),
    )
    session.add(
        "GET", f"/v1/albums/{ALBUM_ID}", FakeResponse(json_data={"id": ALBUM_ID, "name": "A"})
    )
    entity = api_client(session).fetch("album", ALBUM_ID)
    assert [t.name for t in entity.tracks] == ["Only"]
    assert entity.tracks[0].album == "A"


def test_fetch_playlist_paginates_and_skips_junk() -> None:
    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, token_response())
    page2_url = f"{sm.API_BASE}/playlists/{PLAYLIST_ID}/tracks?offset=50&limit=50"
    session.add(
        "GET",
        f"/v1/playlists/{PLAYLIST_ID}/tracks?offset=50",
        FakeResponse(
            json_data={
                "items": [
                    {"is_local": False, "item": api_track("c" * 22, name="Via item key")},
                ],
                "next": None,
            }
        ),
    )
    session.add(
        "GET",
        f"/v1/playlists/{PLAYLIST_ID}/tracks?",
        FakeResponse(
            json_data={
                "items": [
                    {"is_local": False, "track": api_track("a" * 22, name="First")},
                    {"is_local": False, "track": None},
                    {
                        "is_local": True,
                        "track": api_track("b" * 22, name="Local file", is_local=True),
                    },
                    {
                        "is_local": False,
                        "track": {"id": "e" * 22, "name": "Podcast", "type": "episode"},
                    },
                    {"is_local": False, "track": {"id": None, "name": "No id"}},
                ],
                "next": page2_url,
            }
        ),
    )
    session.add(
        "GET",
        f"/v1/playlists/{PLAYLIST_ID}?",
        FakeResponse(
            json_data={
                "id": PLAYLIST_ID,
                "name": "Road trip",
                "owner": {"display_name": "someone"},
                "images": [{"url": "https://i.scdn.co/image/pl", "width": None}],
            }
        ),
    )
    entity = api_client(session).fetch("playlist", PLAYLIST_ID)
    assert entity.kind == "playlist" and entity.name == "Road trip"
    assert entity.album_artist == "someone"
    assert entity.cover_url == "https://i.scdn.co/image/pl"
    assert [t.name for t in entity.tracks] == ["First", "Via item key"]
    assert entity.tracks[0].album == "Random Access Memories"
    assert entity.tracks[0].isrc == "USQX91300102"
    gets = [c for c in session.calls if c["method"] == "GET"]
    assert gets[0]["params"] == {"fields": "id,name,images,owner(display_name)"}
    assert gets[1]["params"]["limit"] == sm.API_PAGE_LIMIT
    assert "items(is_local,track(" in gets[1]["params"]["fields"]
    assert gets[2]["url"] == page2_url


@pytest.mark.parametrize(
    ("kind", "status", "expected"),
    [
        ("track", 400, sm.NOT_FOUND_MESSAGE),
        ("track", 404, sm.NOT_FOUND_MESSAGE),
        ("album", 404, sm.NOT_FOUND_MESSAGE),
        ("playlist", 404, sm.NOT_FOUND_MESSAGE),
        ("playlist", 403, sm.SPOTIFY_MADE_PLAYLIST_MESSAGE),
        ("track", 403, "Spotify refused this request (HTTP 403)."),
        ("track", 500, "Spotify is having problems right now (HTTP 500). Try again later."),
        ("track", 418, "Spotify answered with HTTP 418."),
    ],
)
def test_api_error_mapping(kind: str, status: int, expected: str) -> None:
    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, token_response())
    session.add("GET", "/v1/", FakeResponse(status, json_data={"error": {"status": status}}))
    with pytest.raises(ProviderError) as exc_info:
        api_client(session).fetch(kind, "x" * 22)
    assert str(exc_info.value) == expected
    if kind == "playlist":
        assert isinstance(exc_info.value, sm.SpotifyRefused)
    else:
        assert not isinstance(exc_info.value, sm.SpotifyRefused)


def test_api_401_refreshes_token_once_then_gives_up() -> None:
    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, [token_response("old"), token_response("new")])
    session.add(
        "GET", "/v1/tracks/", [FakeResponse(401, json_data={}), FakeResponse(json_data=api_track())]
    )
    entity = api_client(session).fetch("track", TRACK_ID)
    assert entity.name == "Give Life Back to Music"
    auths = [c["headers"]["Authorization"] for c in session.calls if c["method"] == "GET"]
    assert auths == ["Bearer old", "Bearer new"]

    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, token_response())
    session.add("GET", "/v1/tracks/", FakeResponse(401, json_data={}))
    with pytest.raises(ProviderError) as exc_info:
        api_client(session).fetch("track", TRACK_ID)
    assert str(exc_info.value) == sm.CREDENTIALS_MESSAGE


def test_api_429_honours_retry_after_once() -> None:
    slept: list[float] = []
    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, token_response())
    session.add(
        "GET",
        "/v1/tracks/",
        [
            FakeResponse(429, json_data={}, headers={"Retry-After": "2"}),
            FakeResponse(json_data=api_track()),
        ],
    )
    client = sm.WebApiClient("id", "secret", session=session, sleep=slept.append)
    assert client.fetch("track", TRACK_ID).name == "Give Life Back to Music"
    assert sum(slept) == pytest.approx(2.0)

    slept.clear()
    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, token_response())
    session.add(
        "GET", "/v1/tracks/", FakeResponse(429, json_data={}, headers={"Retry-After": "1000"})
    )
    client = sm.WebApiClient("id", "secret", session=session, sleep=slept.append)
    with pytest.raises(ProviderError) as exc_info:
        client.fetch("track", TRACK_ID)
    assert str(exc_info.value) == sm.RATE_LIMIT_MESSAGE
    assert sum(slept) == pytest.approx(sm.RETRY_AFTER_CAP)  # capped, and only waited once


def test_api_429_wait_is_cancellable() -> None:
    cancel = threading.Event()
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        cancel.set()  # the user cancels while we wait

    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, token_response())
    session.add(
        "GET", "/v1/tracks/", FakeResponse(429, json_data={}, headers={"Retry-After": "10"})
    )
    client = sm.WebApiClient("id", "secret", session=session, sleep=sleep)
    with pytest.raises(DownloadCancelled):
        client.fetch("track", TRACK_ID, cancel)
    assert len(slept) == 1


def test_api_network_error() -> None:
    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, token_response())
    session.add("GET", "/v1/", requests.ConnectionError("offline"))
    with pytest.raises(ProviderError) as exc_info:
        api_client(session).fetch("track", TRACK_ID)
    assert str(exc_info.value) == sm.UNREACHABLE_MESSAGE


# ----------------------------------------------------------------------------------------------
# get_metadata
# ----------------------------------------------------------------------------------------------


def settings_with(tmp_path: Path, client_id: str = "", client_secret: str = "") -> Settings:
    settings = Settings(library_dir=tmp_path / "lib")
    settings.spotify_client_id = client_id
    settings.spotify_client_secret = client_secret
    return settings


def test_get_metadata_uses_embed_without_credentials(tmp_path: Path) -> None:
    session = FakeSession()
    session.add(
        "GET", "open.spotify.com/embed/track/", FakeResponse(text=entity_html(load_entity("track")))
    )
    entity = sm.get_metadata("track", TRACK_ID, settings_with(tmp_path), session=session)
    assert entity.source == "embed"
    assert all("api.spotify.com" not in c["url"] for c in session.calls)


def test_get_metadata_uses_api_with_credentials(tmp_path: Path) -> None:
    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, token_response())
    session.add("GET", f"/v1/tracks/{TRACK_ID}", FakeResponse(json_data=api_track()))
    settings = settings_with(tmp_path, "id", "secret")
    entity = sm.get_metadata("track", TRACK_ID, settings, session=session)
    assert entity.source == "api" and entity.album == "Random Access Memories"
    assert all("embed" not in c["url"] for c in session.calls)


def test_get_metadata_falls_back_to_embed_for_refused_playlists(tmp_path: Path) -> None:
    session = FakeSession()
    session.add("POST", sm.TOKEN_URL, token_response())
    session.add("GET", f"/v1/playlists/{PLAYLIST_ID}", FakeResponse(404, json_data={}))
    session.add(
        "GET",
        f"open.spotify.com/embed/playlist/{PLAYLIST_ID}",
        FakeResponse(text=entity_html(load_entity("playlist"))),
    )
    settings = settings_with(tmp_path, "id", "secret")
    entity = sm.get_metadata("playlist", PLAYLIST_ID, settings, session=session)
    assert entity.source == "embed" and len(entity.tracks) == 6

    session = FakeSession()  # both refuse: the API's explanation wins
    session.add("POST", sm.TOKEN_URL, token_response())
    session.add("GET", f"/v1/playlists/{PLAYLIST_ID}", FakeResponse(403, json_data={}))
    session.add("GET", f"open.spotify.com/embed/playlist/{PLAYLIST_ID}", FakeResponse(404, text=""))
    with pytest.raises(ProviderError) as exc_info:
        sm.get_metadata("playlist", PLAYLIST_ID, settings, session=session)
    assert str(exc_info.value) == sm.SPOTIFY_MADE_PLAYLIST_MESSAGE

    session = FakeSession()  # a missing track is not retried through the page
    session.add("POST", sm.TOKEN_URL, token_response())
    session.add("GET", f"/v1/tracks/{TRACK_ID}", FakeResponse(404, json_data={}))
    with pytest.raises(ProviderError, match="does not exist or is private"):
        sm.get_metadata("track", TRACK_ID, settings, session=session)
    assert all("embed" not in c["url"] for c in session.calls)


def test_credentials_of_tolerates_old_settings_objects() -> None:
    class Old:
        pass

    assert sm.credentials_of(Old()) == ("", "")
    assert sm.credentials_of(None) == ("", "")
