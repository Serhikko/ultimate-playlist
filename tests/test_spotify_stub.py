"""The Spotify provider is a stub: it claims Spotify links and explains it is not built yet."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from ultimate_playlist import providers
from ultimate_playlist.config import Settings
from ultimate_playlist.models import TrackRef
from ultimate_playlist.providers.base import Provider, ProviderError, ProviderNotAvailable
from ultimate_playlist.providers.spotify import SpotifyProvider


def test_identity() -> None:
    provider = SpotifyProvider()
    assert provider.name == "spotify"
    assert provider.display_name == "Spotify"
    assert isinstance(provider, Provider)


@pytest.mark.parametrize(
    "url",
    [
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        "https://open.spotify.com/album/abc?si=xyz",
        "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "http://play.spotify.com/track/abc",
        "https://spotify.link/abc123",
        "open.spotify.com/track/abc",
        "  https://open.spotify.com/track/abc  ",
        "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        "spotify:album:abc",
        "SPOTIFY:playlist:abc",
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


def test_resolve_not_available() -> None:
    with pytest.raises(ProviderNotAvailable) as exc_info:
        SpotifyProvider().resolve("https://open.spotify.com/track/abc")
    assert (
        str(exc_info.value)
        == "Spotify support is not built yet. See docs/ARCHITECTURE.md to add it."
    )
    assert isinstance(exc_info.value, ProviderError)


def test_download_not_available(tmp_path: Path) -> None:
    ref = TrackRef(provider="spotify", source_id="abc", url="https://open.spotify.com/track/abc")
    settings = Settings(library_dir=tmp_path / "lib")
    with pytest.raises(ProviderNotAvailable) as exc_info:
        SpotifyProvider().download(
            ref, tmp_path / "lib", settings, lambda _e: None, threading.Event()
        )
    assert "not built yet" in str(exc_info.value)
    assert not (tmp_path / "lib").exists()  # the stub touches nothing


def test_doctor() -> None:
    checks = SpotifyProvider().doctor()
    assert len(checks) == 1
    ok, label, detail = checks[0]
    assert ok is False
    assert label == "Spotify"
    assert "spotDL" in detail
    assert "Not implemented yet" in detail


def test_registered_after_youtube() -> None:
    names = [p.name for p in providers.PROVIDERS]
    assert "spotify" in names
    assert names.index("youtube") < names.index("spotify")
    assert providers.get_provider("spotify:track:abc").name == "spotify"
    assert providers.get_provider("open.spotify.com/track/abc").name == "spotify"
    assert providers.provider_by_name("spotify") is not None


def test_module_documents_the_plan() -> None:
    from ultimate_playlist.providers import spotify

    assert spotify.__doc__ is not None
    assert "spotDL" in spotify.__doc__
    assert "ARCHITECTURE.md" in spotify.__doc__
