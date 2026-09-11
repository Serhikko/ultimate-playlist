"""Shared fixtures. Everything is offline: the FakeProvider replaces YouTube.

Three autouse fixtures keep the suite hermetic:

* `isolated_home` points ULTIMATE_PLAYLIST_HOME at a throw-away folder for every test, so no
  test can read or create anything under the developer's real ~/.ultimate-playlist.
* `reset_provider_settings` restores the Settings object `providers.configure()` stored on the
  global providers, so the registry's YouTube provider never sees another test's library.
* `stub_tool_detection` replaces the `ffmpeg -version` subprocess and yt-dlp's JavaScript
  runtime probe with fixed answers. Doctor checks then take microseconds instead of seconds and
  do not depend on what happens to be installed on the machine. A test that wants the real
  detection opts out with `@pytest.mark.real_tools`.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:  # so `from fake_provider import ...` works in every test file
    sys.path.insert(0, str(TESTS_DIR))

from fake_provider import FakeProvider  # noqa: E402

from ultimate_playlist.bundled import ENV_BIN  # noqa: E402
from ultimate_playlist.config import ENV_HOME, Settings  # noqa: E402
from ultimate_playlist.downloader import JobManager  # noqa: E402
from ultimate_playlist.ffmpeg import FfmpegInfo  # noqa: E402
from ultimate_playlist.library import Library  # noqa: E402
from ultimate_playlist.playlists import PlaylistStore  # noqa: E402

STUB_FFMPEG = FfmpegInfo(path="ffmpeg", version="test")
STUB_NODE = SimpleNamespace(name="node", path="node", version="24.0.0-test", supported=True)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own app data dir; the real home is never touched.

    ULTIMATE_PLAYLIST_BIN is cleared too, so a developer who points it at a real tools
    folder in their shell does not make doctor / status tests see bundled binaries.
    """
    home = tmp_path / "home"
    monkeypatch.setenv(ENV_HOME, str(home))
    monkeypatch.delenv(ENV_BIN, raising=False)
    return home


@pytest.fixture(autouse=True)
def reset_provider_settings() -> Iterator[None]:
    """`providers.configure(settings)` (create_app, `up add`) stores a test's Settings on the
    global providers; put whatever was there back so no later test runs against a deleted
    tmp_path library from an earlier one."""
    from ultimate_playlist import providers

    before = [(p, getattr(p, "_settings", None)) for p in list(providers.PROVIDERS)]
    yield
    for provider, value in before:
        if hasattr(provider, "_settings"):
            provider._settings = value


@pytest.fixture(autouse=True)
def stub_tool_detection(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fast, machine-independent ffmpeg / JS runtime detection (see the module docstring)."""
    if request.node.get_closest_marker("real_tools"):
        return
    from ultimate_playlist import cli
    from ultimate_playlist.providers import youtube
    from ultimate_playlist.server import app as server_app

    def fake_find_ffmpeg(explicit: str | None = None) -> FfmpegInfo:
        return FfmpegInfo(path=explicit, version="test") if explicit else STUB_FFMPEG

    monkeypatch.setattr(youtube, "find_ffmpeg", fake_find_ffmpeg)
    monkeypatch.setattr(server_app, "find_ffmpeg", fake_find_ffmpeg)
    monkeypatch.setattr(cli, "find_ffmpeg", fake_find_ffmpeg)
    monkeypatch.setattr(
        youtube, "_runtime_info", lambda name: STUB_NODE if name == "node" else None
    )


@pytest.fixture
def tmp_settings(tmp_path: Path, isolated_home: Path) -> Settings:
    """Settings with a throw-away library folder and app data dir."""
    isolated_home.mkdir(exist_ok=True)
    library_dir = tmp_path / "lib"
    library_dir.mkdir()
    return Settings(library_dir=library_dir)


@pytest.fixture
def fake_provider() -> Iterator[FakeProvider]:
    """A FakeProvider registered at the front of the global registry for the test's duration."""
    from ultimate_playlist import providers

    fake = FakeProvider()
    providers.register(fake)
    try:
        yield fake
    finally:
        providers.unregister(fake.name)


@pytest.fixture
def library(tmp_settings: Settings) -> Library:
    return Library(tmp_settings.library_dir, tmp_settings.index_path)


@pytest.fixture
def playlists(tmp_settings: Settings) -> PlaylistStore:
    return PlaylistStore(tmp_settings.playlists_path)


@pytest.fixture
def client(
    tmp_settings: Settings,
    fake_provider: FakeProvider,
    library: Library,
    playlists: PlaylistStore,
) -> Iterator:
    """FastAPI TestClient wired to the fake provider. Lifespan runs (JobManager start/stop).

    Requests carry `Host: 127.0.0.1` like a real browser would; the default `testserver` is
    (rightly) refused by the loopback-only guard.
    """
    from fastapi.testclient import TestClient

    from ultimate_playlist import providers
    from ultimate_playlist.server import app as server_app

    jobs = JobManager(tmp_settings, library, providers=providers)
    app = server_app.create_app(
        settings=tmp_settings, library=library, playlists=playlists, jobs=jobs
    )
    try:
        with TestClient(app, base_url="http://127.0.0.1") as test_client:
            yield test_client
    finally:
        jobs.stop()
