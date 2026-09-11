"""The provider registry: register / unregister / get_provider / doctor_all."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from ultimate_playlist import providers
from ultimate_playlist.config import Settings
from ultimate_playlist.models import Track, TrackRef
from ultimate_playlist.providers.base import Provider


class RegProvider:
    """Minimal provider used only by these tests (distinct name from the shared FakeProvider)."""

    display_name = "Registry Test"

    def __init__(
        self, name: str = "regtest", host: str = "regtest.example", broken_doctor: bool = False
    ) -> None:
        self.name = name
        self.host = host
        self.broken_doctor = broken_doctor

    def matches(self, url: str) -> bool:
        return f"://{self.host}/" in url or url.endswith(f"://{self.host}")

    def resolve(self, url: str) -> list[TrackRef]:
        return [TrackRef(provider=self.name, source_id="1", url=url)]

    def download(
        self,
        ref: TrackRef,
        dest_dir: Path,
        settings: Settings,
        progress: providers.base.ProgressCallback,
        cancel: threading.Event,
    ) -> Track:
        raise NotImplementedError

    def doctor(self) -> list[tuple[bool, str, str]]:
        if self.broken_doctor:
            raise RuntimeError("doctor exploded")
        return [(True, "Registry Test", "fine")]


@pytest.fixture
def reg() -> Iterator[RegProvider]:
    """A test provider at the front of the registry (the autouse `reset_provider_settings`
    fixture in conftest.py undoes any configure() side effect)."""
    provider = RegProvider()
    providers.register(provider)
    yield provider
    providers.unregister(provider.name)
    providers.unregister("regtest2")


def test_builtins_are_registered() -> None:
    names = [p.name for p in providers.PROVIDERS]
    assert names[:2] == ["youtube", "spotify"] or names[-2:] == ["youtube", "spotify"]
    assert all(isinstance(p, Provider) for p in providers.PROVIDERS)
    assert providers.provider_by_name("youtube").display_name == "YouTube"
    assert providers.provider_by_name("spotify").display_name == "Spotify"
    assert providers.provider_by_name("nope") is None


def test_register_inserts_at_front_and_unregister_removes(reg: RegProvider) -> None:
    assert providers.PROVIDERS[0] is reg
    assert providers.provider_by_name("regtest") is reg
    assert isinstance(reg, Provider)
    providers.unregister("regtest")
    assert providers.provider_by_name("regtest") is None
    assert "regtest" not in [p.name for p in providers.PROVIDERS]
    providers.unregister("regtest")  # unknown name is a no-op


def test_register_replaces_same_name(reg: RegProvider) -> None:
    replacement = RegProvider()
    providers.register(replacement)
    matching = [p for p in providers.PROVIDERS if p.name == "regtest"]
    assert matching == [replacement]
    assert providers.PROVIDERS[0] is replacement


def test_registered_provider_wins_over_builtins(reg: RegProvider) -> None:
    hijack = RegProvider(name="regtest2", host="www.youtube.com")
    providers.register(hijack)
    assert providers.get_provider("https://www.youtube.com/watch?v=jNQXAC9IVRw") is hijack
    providers.unregister("regtest2")
    assert providers.get_provider("https://www.youtube.com/watch?v=jNQXAC9IVRw").name == "youtube"


def test_get_provider_with_and_without_scheme(reg: RegProvider) -> None:
    assert providers.get_provider("https://regtest.example/track/1") is reg
    assert providers.get_provider("regtest.example/track/1") is reg
    assert providers.get_provider("  regtest.example/track/1 \n") is reg
    assert providers.get_provider("https://www.youtube.com/watch?v=jNQXAC9IVRw").name == "youtube"
    assert providers.get_provider("youtu.be/jNQXAC9IVRw").name == "youtube"
    assert providers.get_provider("www.youtube.com/playlist?list=PLabc").name == "youtube"
    assert providers.get_provider("spotify:track:abc").name == "spotify"
    assert providers.get_provider("open.spotify.com/track/abc").name == "spotify"


def test_get_provider_unknown() -> None:
    assert providers.get_provider("https://example.com/song.mp3") is None
    assert providers.get_provider("https://www.youtube.com/@channel") is None
    assert providers.get_provider("") is None
    assert providers.get_provider("   ") is None
    assert providers.get_provider("just words") is None


def test_normalize_url() -> None:
    assert providers.normalize_url("  youtu.be/x ") == "https://youtu.be/x"
    assert providers.normalize_url("http://a.b/c") == "http://a.b/c"
    assert providers.normalize_url("spotify:track:x") == "spotify:track:x"
    assert providers.normalize_url("") == ""


def test_get_provider_survives_broken_matches(
    reg: RegProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(url: str) -> bool:
        raise ValueError("bad matcher")

    monkeypatch.setattr(reg, "matches", explode)
    assert providers.get_provider("https://www.youtube.com/watch?v=jNQXAC9IVRw").name == "youtube"
    assert providers.get_provider("https://regtest.example/track/1") is None


def test_doctor_all(reg: RegProvider) -> None:
    result = providers.doctor_all()
    assert set(result) >= {"regtest", "youtube", "spotify"}
    assert result["regtest"] == [(True, "Registry Test", "fine")]
    assert result["spotify"][0][:2] == (False, "Spotify")
    labels = [label for _ok, label, _detail in result["youtube"]]
    assert labels == ["ffmpeg", "JavaScript runtime", "yt-dlp"]
    for checks in result.values():
        for ok, label, detail in checks:
            assert isinstance(ok, bool) and isinstance(label, str) and isinstance(detail, str)


def test_doctor_all_survives_broken_doctor(reg: RegProvider) -> None:
    broken = RegProvider(name="regtest2", broken_doctor=True)
    providers.register(broken)
    result = providers.doctor_all()
    assert result["regtest2"] == [(False, "Registry Test", "doctor() failed: doctor exploded")]


class SettingsAwareProvider(RegProvider):
    """A provider whose doctor()/configure() want the live settings."""

    def __init__(self) -> None:
        super().__init__(name="regtest2", host="aware.example")
        self.doctor_calls: list[Settings | None] = []
        self.configured: list[Settings] = []

    def configure(self, settings: Settings) -> None:
        self.configured.append(settings)

    def doctor(self, settings: Settings | None = None) -> list[tuple[bool, str, str]]:
        self.doctor_calls.append(settings)
        return [(True, "settings", "seen" if settings is not None else "none")]


def test_doctor_all_passes_settings_only_to_providers_that_take_them(
    reg: RegProvider, tmp_path: Path
) -> None:
    aware = SettingsAwareProvider()
    providers.register(aware)
    settings = Settings(library_dir=tmp_path / "lib")

    result = providers.doctor_all(settings)
    assert result["regtest2"] == [(True, "settings", "seen")]
    assert result["regtest"] == [(True, "Registry Test", "fine")]  # no-arg doctor still works
    assert aware.doctor_calls == [settings]
    assert providers.doctor_all()["regtest2"] == [(True, "settings", "none")]
    assert providers.run_doctor(aware, settings) == [(True, "settings", "seen")]
    assert providers.run_doctor(RegProvider(broken_doctor=True), settings)[0][0] is False


def test_configure_reaches_providers_that_support_it(reg: RegProvider, tmp_path: Path) -> None:
    aware = SettingsAwareProvider()
    providers.register(aware)
    settings = Settings(library_dir=tmp_path / "lib")
    providers.configure(settings)  # RegProvider has no configure(): silently skipped
    assert aware.configured == [settings]
    youtube = providers.provider_by_name("youtube")
    assert youtube is not None and youtube._settings is settings


def test_configure_does_not_leak_between_tests(tmp_path: Path) -> None:
    """Runs after the test above: the autouse fixture must have restored the provider."""
    youtube = providers.provider_by_name("youtube")
    assert youtube is not None
    leaked = getattr(youtube, "_settings", None)
    assert leaked is None or "pytest" not in str(getattr(leaked, "library_dir", ""))
