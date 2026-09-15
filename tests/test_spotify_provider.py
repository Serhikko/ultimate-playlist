"""Offline tests for the Spotify provider: matching, resolving, downloading, doctor.

Spotify metadata, the YouTube Music matcher and the YouTube download are all replaced by fakes;
the real pieces under test are the ref building, the file naming, the tags and the cleanup.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fake_provider import TINY_MP3, FakeRegistry, write_tiny_mp3
from mutagen.id3 import APIC, ID3
from mutagen.mp3 import MP3

from ultimate_playlist import providers
from ultimate_playlist.config import Settings
from ultimate_playlist.downloader import JobManager
from ultimate_playlist.library import Library
from ultimate_playlist.models import JobStatus, ProgressEvent, Track, TrackRef
from ultimate_playlist.providers import spotify
from ultimate_playlist.providers import spotify_meta as sm
from ultimate_playlist.providers.base import DownloadCancelled, Provider, ProviderError
from ultimate_playlist.providers.spotify import SpotifyProvider
from ultimate_playlist.providers.youtube import stored_track_id
from ultimate_playlist.providers.ytmusic_match import Match

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "spotify"
TRACK_ID = "0dEIca2nhcxDUV8C5QkPYb"
TRACK_URL = f"https://open.spotify.com/track/{TRACK_ID}"
VIDEO_ID = "zKSsP2084nU"
YT_COVER = b"\xff\xd8\xff\xe0" + b"yt" * 8
SPOTIFY_COVER = b"\xff\xd8\xff\xe1" + b"sp" * 16
COVER_IMAGE_ID = "ab67616d0000b2739b9b36b0e22870b9f542d937"  # the 640 px cover in the fixtures
COVER_URL = f"https://i.scdn.co/image/{COVER_IMAGE_ID}"
WAIT = 15.0


def load_entity(name: str) -> sm.SpotifyEntity:
    data = json.loads((FIXTURES / f"embed_{name}.json").read_text(encoding="utf-8"))
    return sm.entity_from_embed(data)


def a_match(video_id: str = VIDEO_ID, album: str | None = None) -> Match:
    return Match(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title="Give Life Back to Music",
        channel="Daft Punk",
        duration=275.0,
        score=1.0,
        query="Daft Punk - Give Life Back to Music",
        album=album,
    )


def a_ref(**over: Any) -> TrackRef:
    values: dict[str, Any] = {
        "provider": "spotify",
        "source_id": TRACK_ID,
        "url": TRACK_URL,
        "title": "Give Life Back to Music",
        "artist": "Daft Punk",
        "album": "Random Access Memories",
        "duration": 275.386,
        "thumbnail_url": COVER_URL,
        "extra": {
            "source": "api",
            "release_year": "2013",
            "track_number": 1,
            "album_artist": "Daft Punk",
            "isrc": "USQX91300102",
        },
    }
    values.update(over)
    return TrackRef(**values)


class FakeYouTube:
    """Stands in for YouTubeProvider.download: writes a tagged tiny MP3 with a cover."""

    name = "youtube"

    def __init__(self) -> None:
        self.calls: list[tuple[TrackRef, Path]] = []
        self.raise_cancel = False
        self.fail: str | None = None
        self.set_cancel_after = False
        self.filename = "YT Artist - YT Title.mp3"

    def download(
        self,
        ref: TrackRef,
        dest_dir: Path,
        settings: Settings,
        progress: Any,
        cancel: threading.Event,
    ) -> Track:
        self.calls.append((ref, Path(dest_dir)))
        if self.raise_cancel or cancel.is_set():
            raise DownloadCancelled("Download cancelled")
        if self.fail:
            raise ProviderError(self.fail)
        progress(ProgressEvent(JobStatus.DOWNLOADING, progress=0.5))
        progress(ProgressEvent(JobStatus.CONVERTING, progress=1.0, message="Converting to MP3"))
        final = Path(dest_dir) / self.filename
        write_tiny_mp3(
            final,
            artist="YT Artist",
            title="YT Title",
            album="YT Album",
            source_url=ref.url,
            track_id=ref.track_id,
        )
        tags = ID3(str(final))
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=YT_COVER))
        tags.save(str(final))
        if self.set_cancel_after:
            cancel.set()
        return Track(
            id=ref.track_id,
            provider="youtube",
            source_id=ref.source_id,
            source_url=ref.url,
            title="YT Title",
            artist="YT Artist",
            path=final.relative_to(dest_dir).as_posix(),
            album="YT Album",
            duration=MP3(str(final)).info.length,
            has_cover=True,
            file_size=final.stat().st_size,
        )


class FakeCoverResponse:
    def __init__(
        self,
        status: int = 200,
        content: bytes = SPOTIFY_COVER,
        ctype: str = "image/jpeg",
        length: int | None = None,
    ) -> None:
        self.status_code = status
        self.content = content
        self.headers = {"Content-Type": ctype}
        if length is not None:
            self.headers["Content-Length"] = str(length)


@pytest.fixture
def fake_youtube(monkeypatch: pytest.MonkeyPatch) -> FakeYouTube:
    fake = FakeYouTube()
    monkeypatch.setattr(SpotifyProvider, "_youtube_provider", staticmethod(lambda settings: fake))
    return fake


@pytest.fixture
def cover_requests(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"urls": [], "response": FakeCoverResponse()}

    def fake_get(url: str, timeout: float, headers: dict[str, str]) -> Any:
        state["urls"].append(url)
        state["timeout"] = timeout
        state["headers"] = headers
        response = state["response"]
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(spotify, "requests", SimpleNamespace(get=fake_get))
    return state


@pytest.fixture
def matched(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"match": a_match(), "calls": []}

    def fake_find_match(ref: TrackRef, settings: Settings, cancel: threading.Event) -> Match | None:
        state["calls"].append(ref)
        result = state["match"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(spotify, "find_match", fake_find_match)
    return state


# ----------------------------------------------------------------------------------------------
# identity and matches()
# ----------------------------------------------------------------------------------------------


def test_identity() -> None:
    provider = SpotifyProvider()
    assert provider.name == "spotify"
    assert provider.display_name == "Spotify"
    assert isinstance(provider, Provider)


@pytest.mark.parametrize(
    "url",
    [
        f"https://open.spotify.com/track/{TRACK_ID}",
        "https://open.spotify.com/album/abc?si=xyz",
        "https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "http://play.spotify.com/track/abc",
        "https://spotify.link/abc123",
        "https://spotify.app.link/abc123",
        "open.spotify.com/track/abc",
        f"  https://open.spotify.com/track/{TRACK_ID}  ",
        f"spotify:track:{TRACK_ID}",
        "spotify:album:abc",
        "SPOTIFY:playlist:abc",
        "https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi",  # claimed; resolve() explains
    ],
)
def test_matches_positive(url: str) -> None:
    assert SpotifyProvider().matches(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://youtu.be/jNQXAC9IVRw",
        "https://example.com/spotify",
        "https://spotify.com.evil.example/track/abc",
        "https://notspotify.link/abc",
        "spotifyx:track:abc",
        "not a url",
        "",
    ],
)
def test_matches_negative(url: str) -> None:
    assert SpotifyProvider().matches(url) is False


def test_registered_after_youtube() -> None:
    names = [p.name for p in providers.PROVIDERS]
    assert names.index("youtube") < names.index("spotify")
    assert providers.get_provider("spotify:track:abc").name == "spotify"
    assert providers.get_provider("open.spotify.com/track/abc").name == "spotify"


# ----------------------------------------------------------------------------------------------
# resolve()
# ----------------------------------------------------------------------------------------------


@pytest.fixture
def metadata(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"entity": load_entity("track"), "calls": []}

    def fake_get_metadata(
        kind: str, entity_id: str, settings: Settings, cancel: Any = None, session: Any = None
    ) -> sm.SpotifyEntity:
        state["calls"].append((kind, entity_id, settings))
        entity = state["entity"]
        if isinstance(entity, Exception):
            raise entity
        return entity

    monkeypatch.setattr(spotify, "get_metadata", fake_get_metadata)
    return state


def test_resolve_track(metadata: dict[str, Any], tmp_path: Path) -> None:
    settings = Settings(library_dir=tmp_path / "lib")
    provider = SpotifyProvider(settings)
    refs = provider.resolve(f"https://open.spotify.com/intl-fr/track/{TRACK_ID}?si=1")
    assert metadata["calls"] == [("track", TRACK_ID, settings)]
    (ref,) = refs
    assert ref.provider == "spotify"
    assert ref.source_id == TRACK_ID
    assert ref.track_id == f"spotify:{TRACK_ID}"
    assert ref.url == TRACK_URL
    assert ref.title == "Give Life Back to Music"
    assert ref.artist == "Daft Punk"
    assert ref.album is None  # not on the embed page of a single track
    assert ref.duration == pytest.approx(275.386)
    assert ref.thumbnail_url is not None and ref.thumbnail_url.endswith(COVER_IMAGE_ID)
    assert ref.extra == {"source": "embed", "release_year": "2013"}


def test_resolve_album(metadata: dict[str, Any]) -> None:
    metadata["entity"] = load_entity("album")
    refs = SpotifyProvider(Settings()).resolve("spotify:album:4m2880jivSbbyEGAKfITCa")
    assert len(refs) == 4
    first = refs[0]
    assert first.track_id == f"spotify:{TRACK_ID}"
    assert first.album == "Random Access Memories"
    assert first.artist == "Daft Punk"
    assert first.thumbnail_url is not None and first.thumbnail_url.endswith(COVER_IMAGE_ID)
    assert first.extra["container"] == "Album: Random Access Memories"
    assert first.extra["track_number"] == 1
    assert first.extra["album_artist"] == "Daft Punk"
    assert first.extra["source"] == "embed"
    assert "isrc" not in first.extra
    assert [r.extra["track_number"] for r in refs] == [1, 2, 3, 4]


def test_resolve_playlist(metadata: dict[str, Any]) -> None:
    metadata["entity"] = load_entity("playlist")
    refs = SpotifyProvider(Settings()).resolve(
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
    )
    assert len(refs) == 6
    first = refs[0]
    assert first.title == "BbY WOW"
    assert first.artist == "KAROL G, Judeline, rusowsky"
    assert first.album is None and first.thumbnail_url is None
    assert first.extra["container"].startswith("Playlist: Today")
    assert "track_number" not in first.extra
    assert refs[-1].artist == "Tyler, The Creator"
    assert len({r.track_id for r in refs}) == 6


def test_resolve_api_entity_keeps_isrc_and_year() -> None:
    entity = sm.SpotifyEntity(
        kind="track",
        id=TRACK_ID,
        name="Give Life Back to Music",
        artists=["Daft Punk"],
        album="Random Access Memories",
        album_artist="Daft Punk",
        duration=275.386,
        cover_url=COVER_URL,
        release_year="2013",
        track_number=1,
        isrc="USQX91300102",
        source="api",
    )
    (ref,) = spotify.refs_from_entity(entity)
    assert ref.album == "Random Access Memories"
    assert ref.extra == {
        "source": "api",
        "release_year": "2013",
        "track_number": 1,
        "album_artist": "Daft Punk",
        "isrc": "USQX91300102",
    }


def test_resolve_unsupported_link_does_not_hit_the_network(metadata: dict[str, Any]) -> None:
    with pytest.raises(ProviderError) as exc_info:
        SpotifyProvider(Settings()).resolve(
            "https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi"
        )
    assert str(exc_info.value) == sm.UNSUPPORTED_KINDS_MESSAGE
    assert metadata["calls"] == []


def test_resolve_passes_metadata_errors_through(metadata: dict[str, Any]) -> None:
    metadata["entity"] = ProviderError(sm.NOT_FOUND_MESSAGE)
    with pytest.raises(ProviderError, match="does not exist or is private"):
        SpotifyProvider(Settings()).resolve(TRACK_URL)


def test_resolve_uses_configured_settings(metadata: dict[str, Any], tmp_path: Path) -> None:
    provider = SpotifyProvider()
    settings = Settings(library_dir=tmp_path / "lib")
    provider.configure(settings)
    provider.resolve(TRACK_URL)
    assert metadata["calls"][0][2] is settings


# ----------------------------------------------------------------------------------------------
# download()
# ----------------------------------------------------------------------------------------------


def test_download_success(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    ref = a_ref()
    events: list[ProgressEvent] = []
    cancel = threading.Event()

    track = SpotifyProvider(tmp_settings).download(
        ref, tmp_settings.library_dir, tmp_settings, events.append, cancel
    )

    final = tmp_settings.library_dir / "Daft Punk - Give Life Back to Music.mp3"
    assert final.is_file()
    assert track.id == f"spotify:{TRACK_ID}"
    assert track.provider == "spotify"
    assert track.source_id == TRACK_ID
    assert track.source_url == TRACK_URL
    assert track.title == "Give Life Back to Music"
    assert track.artist == "Daft Punk"
    assert track.album == "Random Access Memories"
    assert track.path == "Daft Punk - Give Life Back to Music.mp3"
    assert track.has_cover is True
    assert track.file_size == final.stat().st_size > 0
    assert track.duration is not None and 0 < track.duration < 5  # from the tiny file, not Spotify

    tags = ID3(str(final))
    assert tags["TPE1"].text == ["Daft Punk"]
    assert tags["TIT2"].text == ["Give Life Back to Music"]
    assert tags["TALB"].text == ["Random Access Memories"]
    assert tags["TPE2"].text == ["Daft Punk"]
    assert tags["TRCK"].text == ["1"]
    assert str(tags["TDRC"].text[0]) == "2013"
    assert tags["TSRC"].text == ["USQX91300102"]
    assert tags.getall("COMM")[0].text == [TRACK_URL]
    assert tags.getall("TXXX:ULTIMATE_PLAYLIST_ID")[0].text == [f"spotify:{TRACK_ID}"]
    assert tags.getall("TXXX:YOUTUBE_ID")[0].text == [VIDEO_ID]
    assert tags.getall("TXXX:ISRC")[0].text == ["USQX91300102"]
    (apic,) = tags.getall("APIC")
    assert apic.data == SPOTIFY_COVER and apic.mime == "image/jpeg"
    assert stored_track_id(final) == f"spotify:{TRACK_ID}"

    # the YouTube step ran inside .incoming and nothing is left behind there
    (yt_ref, yt_dest) = fake_youtube.calls[0]
    assert yt_dest == tmp_settings.library_dir / ".incoming"
    assert yt_ref.provider == "youtube" and yt_ref.source_id == VIDEO_ID
    assert yt_ref.url == f"https://www.youtube.com/watch?v={VIDEO_ID}"
    assert yt_ref.title == ref.title and yt_ref.artist == ref.artist
    assert [p.name for p in yt_dest.iterdir()] == []
    assert ref.extra["youtube_id"] == VIDEO_ID
    assert matched["calls"] == [ref]

    assert cover_requests["urls"] == [COVER_URL]
    assert cover_requests["timeout"] == spotify.COVER_TIMEOUT
    assert cover_requests["headers"]["User-Agent"].startswith("Mozilla/5.0")

    messages = [e.message for e in events if e.message]
    assert messages[0] == "Searching YouTube Music…"
    assert messages[1] == "Matched: Give Life Back to Music (Daft Punk, 4:35)"
    assert "Writing Spotify tags" in messages
    assert events[0].status == JobStatus.DOWNLOADING
    assert any(e.status == JobStatus.CONVERTING for e in events)


def test_download_without_album_or_extras(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    ref = a_ref(album=None, thumbnail_url=None, extra={"source": "embed"})
    track = SpotifyProvider(tmp_settings).download(
        ref, tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
    )
    tags = ID3(str(tmp_settings.library_dir / track.path))
    assert track.album is None
    assert not tags.getall("TALB") and not tags.getall("TPE2") and not tags.getall("TRCK")
    assert not tags.getall("TDRC") and not tags.getall("TSRC") and not tags.getall("TXXX:ISRC")
    assert tags.getall("TXXX:YOUTUBE_ID")[0].text == [VIDEO_ID]
    assert cover_requests["urls"] == []  # no cover URL: nothing fetched
    (apic,) = tags.getall("APIC")
    assert apic.data == YT_COVER  # the YouTube cover stays
    assert track.has_cover is True


def test_download_takes_the_album_from_youtube_music_when_spotify_has_none(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    matched["match"] = a_match(album="Random Access Memories")
    ref = a_ref(album=None)
    track = SpotifyProvider(tmp_settings).download(
        ref, tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
    )
    assert track.album == "Random Access Memories"
    assert ref.album == "Random Access Memories"
    tags = ID3(str(tmp_settings.library_dir / track.path))
    assert tags["TALB"].text == ["Random Access Memories"]
    # ...but Spotify's own album name wins when it is known
    ref = a_ref(album="Spotify Says")
    track = SpotifyProvider(tmp_settings).download(
        ref, tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
    )
    assert track.album == "Spotify Says"


@pytest.mark.parametrize(
    "response",
    [
        FakeCoverResponse(status=404),
        FakeCoverResponse(content=b"<html>not an image</html>", ctype="text/html"),
        FakeCoverResponse(content=b"", ctype="image/jpeg"),
        FakeCoverResponse(length=spotify.MAX_COVER_BYTES + 1),
        FakeCoverResponse(content=b"\xff\xd8\xff" + b"x" * (spotify.MAX_COVER_BYTES + 1)),
        RuntimeError("network down"),
    ],
)
def test_download_keeps_youtube_cover_when_spotify_cover_fails(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
    response: Any,
) -> None:
    cover_requests["response"] = response
    track = SpotifyProvider(tmp_settings).download(
        a_ref(), tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
    )
    (apic,) = ID3(str(tmp_settings.library_dir / track.path)).getall("APIC")
    assert apic.data == YT_COVER
    assert track.has_cover is True


def test_download_sniffs_png_covers_without_content_type(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    cover_requests["response"] = FakeCoverResponse(content=png, ctype="application/octet-stream")
    track = SpotifyProvider(tmp_settings).download(
        a_ref(), tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
    )
    (apic,) = ID3(str(tmp_settings.library_dir / track.path)).getall("APIC")
    assert apic.data == png and apic.mime == "image/png"


def test_download_respects_embed_cover_setting(
    tmp_path: Path,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    settings = Settings(library_dir=tmp_path / "lib", embed_cover=False)
    SpotifyProvider(settings).download(
        a_ref(), settings.library_dir, settings, lambda e: None, threading.Event()
    )
    assert cover_requests["urls"] == []


def test_download_no_match(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    matched["match"] = None
    with pytest.raises(ProviderError) as exc_info:
        SpotifyProvider(tmp_settings).download(
            a_ref(), tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
        )
    assert (
        str(exc_info.value)
        == "Couldn't find 'Daft Punk - Give Life Back to Music' on YouTube Music "
        "(no close enough match)."
    )
    assert fake_youtube.calls == []
    assert list(tmp_settings.library_dir.glob("*.mp3")) == []


def test_download_match_errors_propagate(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    matched["match"] = ProviderError(
        "Couldn't reach YouTube. Check your internet connection and try again."
    )
    with pytest.raises(ProviderError, match="Couldn't reach YouTube"):
        SpotifyProvider(tmp_settings).download(
            a_ref(), tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
        )
    matched["match"] = DownloadCancelled("Download cancelled")
    with pytest.raises(DownloadCancelled):
        SpotifyProvider(tmp_settings).download(
            a_ref(), tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
        )


def test_download_youtube_errors_propagate(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    fake_youtube.fail = "This video is private, so it can't be downloaded."
    with pytest.raises(ProviderError, match="This video is private"):
        SpotifyProvider(tmp_settings).download(
            a_ref(), tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
        )
    assert list(tmp_settings.library_dir.glob("*.mp3")) == []


def test_download_cancel_before_start(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        SpotifyProvider(tmp_settings).download(
            a_ref(), tmp_settings.library_dir, tmp_settings, lambda e: None, cancel
        )
    assert matched["calls"] == [] and fake_youtube.calls == []


def test_download_cancel_propagates_from_youtube(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    fake_youtube.raise_cancel = True
    with pytest.raises(DownloadCancelled):
        SpotifyProvider(tmp_settings).download(
            a_ref(), tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
        )
    assert list(tmp_settings.library_dir.rglob("*.mp3")) == []


def test_download_cancel_after_youtube_step_removes_the_file(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    fake_youtube.set_cancel_after = True
    with pytest.raises(DownloadCancelled):
        SpotifyProvider(tmp_settings).download(
            a_ref(), tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
        )
    assert list(tmp_settings.library_dir.rglob("*.mp3")) == []


def test_download_cleans_up_when_tagging_fails(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*args: Any, **kwargs: Any) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(spotify, "write_spotify_tags", explode)
    with pytest.raises(ProviderError) as exc_info:
        SpotifyProvider(tmp_settings).download(
            a_ref(), tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
        )
    assert str(exc_info.value) == "Spotify download failed: disk full"
    assert list(tmp_settings.library_dir.rglob("*.mp3")) == []


def test_download_never_clobbers_a_different_track(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    other = tmp_settings.library_dir / "Daft Punk - Give Life Back to Music.mp3"
    write_tiny_mp3(
        other, artist="Daft Punk", title="Give Life Back to Music", track_id="youtube:IluRBvnYMoY"
    )
    before = other.read_bytes()
    provider = SpotifyProvider(tmp_settings)
    track = provider.download(
        a_ref(), tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
    )
    assert track.path == "Daft Punk - Give Life Back to Music (2).mp3"
    assert other.read_bytes() == before
    # a re-download of the same Spotify track replaces its own file instead of minting " (3)"
    again = provider.download(
        a_ref(), tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
    )
    assert again.path == "Daft Punk - Give Life Back to Music (2).mp3"
    assert sorted(p.name for p in tmp_settings.library_dir.glob("*.mp3")) == [
        "Daft Punk - Give Life Back to Music (2).mp3",
        "Daft Punk - Give Life Back to Music.mp3",
    ]


def test_download_safe_filename(
    tmp_settings: Settings,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
) -> None:
    ref = a_ref(title='What: "Is" This?', artist="AC/DC")
    track = SpotifyProvider(tmp_settings).download(
        ref, tmp_settings.library_dir, tmp_settings, lambda e: None, threading.Event()
    )
    assert track.path == "ACDC - What Is This.mp3"
    assert track.title == 'What: "Is" This?' and track.artist == "AC/DC"


def test_write_spotify_tags_on_other_containers(tmp_path: Path) -> None:
    from fake_provider import FAKE_PNG, write_tiny_audio

    for fmt in ("m4a", "flac", "opus"):
        path = write_tiny_audio(tmp_path / f"song.{fmt}", artist="yt", title="yt", cover=FAKE_PNG)
        has_cover = spotify.write_spotify_tags(
            path, a_ref(), VIDEO_ID, (SPOTIFY_COVER, "image/jpeg")
        )
        assert has_cover is True
        from ultimate_playlist.library import read_cover, read_tags

        info = read_tags(path)
        assert info.artist == "Daft Punk" and info.title == "Give Life Back to Music"
        assert info.album == "Random Access Memories"
        assert info.track_id == f"spotify:{TRACK_ID}"
        cover = read_cover(path)
        assert cover is not None and cover[0] == SPOTIFY_COVER


# ----------------------------------------------------------------------------------------------
# doctor()
# ----------------------------------------------------------------------------------------------


def test_doctor_without_credentials(tmp_path: Path) -> None:
    settings = Settings(library_dir=tmp_path / "lib")
    (ok, label, detail) = SpotifyProvider(settings).doctor()[0]
    assert ok is True and label == "Spotify"
    assert detail.startswith("No API credentials: using Spotify's public pages")
    assert f"about {sm.EMBED_LIST_CAP} tracks" in detail
    assert "client id and secret in Settings" in detail
    assert SpotifyProvider().doctor(settings) == [(ok, label, detail)]


def test_doctor_with_credentials(tmp_path: Path) -> None:
    settings = Settings(library_dir=tmp_path / "lib")
    settings.spotify_client_id = "id"
    settings.spotify_client_secret = "secret"
    assert SpotifyProvider(settings).doctor() == [
        (True, "Spotify", "Web API credentials set (playlists of any size)")
    ]
    assert (
        SpotifyProvider().doctor(settings)[0][2]
        == "Web API credentials set (playlists of any size)"
    )
    assert providers.run_doctor(SpotifyProvider(), settings)[0][0] is True


def test_doctor_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Settings:
        raise RuntimeError("no settings for you")

    monkeypatch.setattr(Settings, "load", broken)
    checks = SpotifyProvider().doctor()
    assert checks[0][:2] == (True, "Spotify")


# ----------------------------------------------------------------------------------------------
# end to end through the JobManager
# ----------------------------------------------------------------------------------------------


def test_job_manager_end_to_end(
    tmp_settings: Settings,
    library: Library,
    fake_youtube: FakeYouTube,
    cover_requests: dict[str, Any],
    matched: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    provider = SpotifyProvider(tmp_settings)
    manager = JobManager(tmp_settings, library, providers=FakeRegistry(provider))
    manager.start()
    try:
        job = manager.submit(TRACK_URL)
        assert manager.wait_idle(WAIT)
    finally:
        manager.stop()
    assert job.status == JobStatus.DONE, job.error
    assert job.track is not None and job.track.id == f"spotify:{TRACK_ID}"
    assert job.track_ref is not None and job.track_ref.extra["youtube_id"] == VIDEO_ID
    stored = library.get(f"spotify:{TRACK_ID}")
    assert stored is not None
    assert stored.path == "Daft Punk - Give Life Back to Music.mp3"
    assert (tmp_settings.library_dir / stored.path).stat().st_size > len(TINY_MP3)
