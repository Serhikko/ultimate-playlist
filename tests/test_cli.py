"""The `up` CLI, run in-process through cli.main([...]) with the FakeProvider."""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from fake_provider import FakeProvider, playlist_url, track_url, write_tiny_mp3

from ultimate_playlist import __version__, cli, logs
from ultimate_playlist.config import Settings, app_data_dir
from ultimate_playlist.ffmpeg import FfmpegInfo
from ultimate_playlist.library import Library
from ultimate_playlist.playlists import PlaylistStore


def run(*args: str, library: Path | None = None) -> int:
    argv = ["--library", str(library), *args] if library is not None else list(args)
    return cli.main(argv)


@pytest.fixture(autouse=True)
def no_real_app_is_ever_contacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A copy of the app may really be running on this machine: no test may find it, let
    alone change its settings. Every loopback request the CLI makes is refused instead; the
    tests that need a "running app" fake the probe and the call themselves."""

    def refused(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(cli._opener, "open", refused)


def pretend_running(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    monkeypatch.setattr(
        cli, "find_running_instance", lambda host, port, attempts=cli.PORT_ATTEMPTS: url
    )


# -- global options ----------------------------------------------------------------------------


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"up {__version__}"


def test_frozen_exe_uses_its_own_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is no `up` in the zip: usage and errors must say UltimatePlaylist.exe."""
    monkeypatch.setattr(cli, "is_frozen", lambda: True)
    # A native path: a hard-coded Windows path has no separators on Linux, so `.name` would be
    # the whole string there.
    exe = tmp_path / "Apps" / "UltimatePlaylist" / "UltimatePlaylist.exe"
    monkeypatch.setattr(sys, "executable", str(exe))
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"UltimatePlaylist.exe {__version__}"
    assert cli.main(["bogus"]) == 2
    assert "UltimatePlaylist.exe: error" in capsys.readouterr().err


def test_help_and_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    assert "COMMAND" in capsys.readouterr().out
    assert cli.main(["bogus"]) == 2
    assert "invalid choice" in capsys.readouterr().err


def test_no_subcommand_runs_serve(tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from ultimate_playlist.server import app as server_app

    calls: list[dict[str, object]] = []

    def fake_serve(settings: Settings, host: str, port: int, open_browser: bool) -> None:
        calls.append(
            {"host": host, "port": port, "open_browser": open_browser, "lib": settings.library_dir}
        )

    monkeypatch.setattr(server_app, "serve", fake_serve)
    assert cli.main([]) == 0  # no config.json in the (isolated) app data dir: the default library
    assert calls[-1] == {
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
        "lib": Settings().library_dir,
    }
    tmp_settings.save()  # a saved config.json is what `up` picks up
    assert cli.main([]) == 0
    assert calls[-1]["lib"] == tmp_settings.library_dir
    assert (
        cli.main(
            [
                "--library",
                "C:/tmp/other" if sys.platform == "win32" else "/tmp/other",
                "serve",
                "--port",
                "9001",
                "--no-browser",
                "--host",
                "127.0.0.2",
            ]
        )
        == 0
    )
    assert calls[-1]["port"] == 9001 and calls[-1]["open_browser"] is False
    assert calls[-1]["host"] == "127.0.0.2"
    assert Path(str(calls[-1]["lib"])).name == "other"


def test_library_override_is_made_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`up --library music ...` must not store a folder that depends on the current directory."""
    monkeypatch.chdir(tmp_path)
    settings = cli.load_settings("music")
    assert settings.library_dir.is_absolute()
    assert settings.library_dir == tmp_path / "music"
    monkeypatch.setenv("UP_TEST_LIB", str(tmp_path / "env"))
    env_form = "$UP_TEST_LIB" if sys.platform != "win32" else "%UP_TEST_LIB%"
    assert cli.load_settings(env_form) == cli.load_settings(str(tmp_path / "env"))
    assert cli.load_settings("~").library_dir == Path.home()


def test_frozen_second_start_joins_the_running_instance(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second double-click must not start a second server (and queue) on the next port."""
    from ultimate_playlist.server import app as server_app

    served: list[int] = []
    opened: list[str] = []
    probes: list[tuple[str, int]] = []
    monkeypatch.setattr(
        server_app, "serve", lambda s, host, port, open_browser: served.append(port)
    )
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cli, "is_frozen", lambda: True)
    monkeypatch.setattr(cli, "_set_console_title", lambda title: None)

    def running(host: str, port: int, timeout: float = 1.0) -> str | None:
        probes.append((host, port))
        return "http://127.0.0.1:8765/"

    monkeypatch.setattr(cli, "running_instance", running)
    monkeypatch.setattr(cli, "JOIN_PAUSE", 0.0)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "already running at http://127.0.0.1:8765/ - opening it in your browser." in out
    assert "closes by itself" in out and "close this window" not in out  # it closes at once
    assert served == [] and opened == ["http://127.0.0.1:8765/"] and probes == [("127.0.0.1", 8765)]

    assert cli.main(["serve", "--no-browser"]) == 0  # joins too, but opens nothing
    assert served == [] and opened == ["http://127.0.0.1:8765/"]

    assert cli.main(["serve", "--port", "8791"]) == 0  # an explicit port is the user's choice
    assert served == [8791] and len(probes) == 2

    monkeypatch.setattr(cli, "running_instance", lambda host, port, timeout=1.0: None)
    assert cli.main([]) == 0  # nothing of ours on 8765: start as usual
    assert served == [8791, 8765]


def test_second_start_finds_the_instance_on_a_fallback_port(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A foreign program on 8765 pushed the first start to 8766: the second must join it there
    instead of starting a third server on 8767."""
    from ultimate_playlist.server import app as server_app

    served: list[int] = []
    opened: list[str] = []
    probes: list[int] = []
    monkeypatch.setattr(
        server_app, "serve", lambda s, host, port, open_browser: served.append(port)
    )
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(cli, "is_frozen", lambda: True)
    monkeypatch.setattr(cli, "_set_console_title", lambda title: None)
    monkeypatch.setattr(cli, "JOIN_PAUSE", 0.0)

    def running(host: str, port: int, timeout: float = cli.JOIN_TIMEOUT) -> str | None:
        probes.append(port)
        return f"http://{host}:{port}/" if port == cli.DEFAULT_PORT + 1 else None

    monkeypatch.setattr(cli, "running_instance", running)
    assert cli.main([]) == 0
    assert served == [] and opened == ["http://127.0.0.1:8766/"]
    assert probes == [8765, 8766]
    assert "already running at http://127.0.0.1:8766/" in capsys.readouterr().out

    probes.clear()
    monkeypatch.setattr(cli, "running_instance", lambda host, port, timeout=0.0: None)
    assert cli.main([]) == 0  # the whole range is scanned before a server is started
    assert served == [8765]
    assert cli.find_running_instance("127.0.0.1", 8765) is None


def test_port_range_matches_the_server() -> None:
    """cli.PORT_ATTEMPTS is a copy (importing server.app would load FastAPI for `up list`)."""
    from ultimate_playlist.server import app as server_app

    assert cli.PORT_ATTEMPTS == server_app.PORT_ATTEMPTS == 11


def test_installed_metadata_version_matches_the_package() -> None:
    """pyproject.toml and ultimate_playlist.__version__ are hand-maintained copies; the tag
    check reads one and the zip name the other, so they must never drift apart."""
    import importlib.metadata

    try:
        installed = importlib.metadata.version("ultimate-playlist")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("ultimate-playlist is not installed in this environment")
    assert installed == __version__


def test_running_instance_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import json
    import urllib.error
    import urllib.request

    answers: dict[str, object] = {}

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            self.close()

    def fake_urlopen(url: str, timeout: float = 0) -> Response:
        answers["url"] = url
        answers["timeout"] = timeout
        payload = answers.get("payload")
        if isinstance(payload, Exception):
            raise payload
        return Response(json.dumps(payload).encode("utf-8"))

    # the probe must not go through HTTP_PROXY: the opener carries no proxy handler at all
    assert not any(isinstance(h, urllib.request.ProxyHandler) for h in cli._opener.handlers)
    monkeypatch.setattr(cli._opener, "open", fake_urlopen)
    answers["payload"] = {"version": __version__}
    assert cli.running_instance("127.0.0.1", 8765) == "http://127.0.0.1:8765/"
    assert answers["url"] == "http://127.0.0.1:8765/api/version"  # probe-free endpoint
    assert answers["timeout"] == cli.JOIN_TIMEOUT == 3.0
    assert cli.running_instance("127.0.0.1", 8766, timeout=0.5) == "http://127.0.0.1:8766/"
    assert answers["url"] == "http://127.0.0.1:8766/api/version" and answers["timeout"] == 0.5
    answers["payload"] = {"version": "0.0.0-other"}  # an older copy still running: leave it alone
    assert cli.running_instance("127.0.0.1", 8765) is None
    answers["payload"] = ["not", "an", "object"]
    assert cli.running_instance("127.0.0.1", 8765) is None
    answers["payload"] = urllib.error.URLError("connection refused")
    assert cli.running_instance("127.0.0.1", 8765) is None
    answers["payload"] = TimeoutError("timed out")
    assert cli.running_instance("127.0.0.1", 8765) is None


def test_serve_reports_a_port_problem_and_exits_1(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ultimate_playlist.server import app as server_app

    def refuse(settings: Settings, host: str, port: int, open_browser: bool) -> None:
        raise OSError("No free port between 8765 and 8775 on 127.0.0.1.")

    monkeypatch.setattr(server_app, "serve", refuse)
    assert cli.main(["serve", "--no-browser"]) == 1
    err = capsys.readouterr().err
    assert "Could not start the server" in err and "No free port" in err


def test_add_interrupted_cancels_and_stops_the_queue(
    tmp_settings: Settings,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ctrl-C during `up add`: cancel the active jobs, stop the workers, exit 130."""
    import threading

    from ultimate_playlist.downloader import JobManager

    fake_provider.gate = threading.Event()  # the download would block forever otherwise
    managers: list[JobManager] = []
    real_init = JobManager.__init__

    def recording_init(self: JobManager, *args: object, **kwargs: object) -> None:
        real_init(self, *args, **kwargs)
        managers.append(self)

    real_wait_idle = JobManager.wait_idle

    def interrupted(self: JobManager, timeout: float | None = None) -> bool:
        if not fake_provider.download_started.wait(10.0):
            raise AssertionError("the fake download never started")
        raise KeyboardInterrupt

    monkeypatch.setattr(JobManager, "__init__", recording_init)
    monkeypatch.setattr(JobManager, "wait_idle", interrupted)
    try:
        assert run("add", track_url("slow"), library=tmp_settings.library_dir) == 130
    finally:
        monkeypatch.setattr(JobManager, "wait_idle", real_wait_idle)
        for manager in managers:
            manager.stop()
    assert "Interrupted" in capsys.readouterr().err
    (manager,) = managers
    assert not manager.running
    assert all(j.status.is_terminal for j in manager.list())
    assert not any(t.name.startswith("up-") and t.is_alive() for t in threading.enumerate())
    assert not (tmp_settings.library_dir / "Fake Artist - Track slow.mp3").exists()


def test_main_maps_keyboard_interrupt_to_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_list", interrupted)
    assert cli.main(["list"]) == 130
    assert "Interrupted." in capsys.readouterr().err


def test_python_m_entry_point() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ultimate_playlist", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"up {__version__}"


# -- add ---------------------------------------------------------------------------------------


def test_add_downloads_a_fake_track(
    tmp_settings: Settings, fake_provider: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    lib = tmp_settings.library_dir
    assert run("add", track_url("abc"), library=lib) == 0
    out = capsys.readouterr().out
    assert (lib / "Fake Artist - Track abc.mp3").is_file()
    assert fake_provider.downloads == ["abc"]
    lines = [line for line in out.splitlines() if line.strip()]
    statuses = [line.split()[0] for line in lines[:-1]]
    assert statuses[0] == "queued"
    assert statuses[-1] == "done"
    assert "Fake Artist - Track abc.mp3" in lines[-2]
    assert lines[-1].startswith("Finished: 1 done")
    # the index was written, so `list` sees the track
    assert run("list", library=lib) == 0
    assert "Track abc" in capsys.readouterr().out


def test_add_unsupported_url_exits_1(
    tmp_settings: Settings, fake_provider: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("add", "https://example.com/nothing", library=tmp_settings.library_dir) == 1
    captured = capsys.readouterr()
    assert "No provider for this link" in captured.err
    assert "Finished: nothing to do" in captured.out


def test_add_failed_download_exits_1(
    tmp_settings: Settings, fake_provider: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_provider.fail_ids.add("bad")
    assert run("add", track_url("bad"), track_url("good"), library=tmp_settings.library_dir) == 1
    out = capsys.readouterr().out
    assert "error" in out and "refused" in out
    assert "Finished: 1 done, 1 error" in out
    assert (tmp_settings.library_dir / "Fake Artist - Track good.mp3").is_file()


def test_add_playlist_and_skip_existing(
    tmp_settings: Settings, fake_provider: FakeProvider, capsys: pytest.CaptureFixture[str]
) -> None:
    lib = tmp_settings.library_dir
    assert run("add", playlist_url(2), library=lib) == 0
    out = capsys.readouterr().out
    assert "Playlist: 2 tracks" in out
    assert "Finished: 3 done" in out
    assert run("add", track_url("pl2-1"), library=lib) == 0
    out = capsys.readouterr().out
    assert "skipped" in out and "Already in library" in out
    assert "Finished: 1 skipped" in out


@pytest.mark.parametrize("flag", ["-q", "--quiet", "--no-wait"])
def test_add_quiet_prints_only_the_summary(
    tmp_settings: Settings, fake_provider: FakeProvider, capsys: pytest.CaptureFixture[str], flag
) -> None:
    """`--quiet` (and its old spelling `--no-wait`) still waits for the download to finish."""
    assert run("add", flag, track_url("quiet"), library=tmp_settings.library_dir) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == ["Finished: 1 done"]
    assert (tmp_settings.library_dir / "Fake Artist - Track quiet.mp3").is_file()


def test_add_help_documents_quiet_and_hides_no_wait(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["add", "--help"]) == 0
    out = capsys.readouterr().out
    assert "-q, --quiet" in out and "still waits" in out
    assert "--no-wait" not in out


# -- list / rescan / playlists -----------------------------------------------------------------


def test_list_empty_and_query(
    tmp_settings: Settings, library: Library, capsys: pytest.CaptureFixture[str]
) -> None:
    lib = tmp_settings.library_dir
    assert run("list", library=lib) == 0
    assert "empty" in capsys.readouterr().out
    write_tiny_mp3(lib / "Alpha - One.mp3", artist="Alpha", title="One", track_id="fake:one")
    write_tiny_mp3(lib / "Beta - Two.mp3", artist="Beta", title="Two", track_id="fake:two")
    library.rescan()
    assert run("list", library=lib) == 0
    out = capsys.readouterr().out
    assert "fake:one" in out and "fake:two" in out
    assert out.splitlines()[0].split() == ["id", "artist", "title", "duration", "path"]
    assert "0:00" in out  # 0.3 s rounds to 0:00
    assert run("list", "-q", "beta", library=lib) == 0
    out = capsys.readouterr().out
    assert "fake:two" in out and "fake:one" not in out
    assert run("list", "-q", "zzz", library=lib) == 0
    assert "No tracks found" in capsys.readouterr().out


def test_rescan(tmp_settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    lib = tmp_settings.library_dir
    write_tiny_mp3(lib / "Someone - Song.mp3", artist="Someone", title="Song")
    assert run("rescan", library=lib) == 0
    assert "1 change(s), 1 track(s)" in capsys.readouterr().out
    assert run("rescan", library=lib) == 0
    assert "0 change(s), 1 track(s)" in capsys.readouterr().out


def test_rescan_with_unreachable_folder_fails_without_touching_anything(
    tmp_settings: Settings,
    library: Library,
    playlists: PlaylistStore,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_tiny_mp3(tmp_settings.library_dir / "A - B.mp3", track_id="fake:ab")
    library.rescan()
    playlist = playlists.create("Mix")
    playlists.add_tracks(playlist.id, ["fake:ab"])

    assert run("rescan", library=tmp_path / "unplugged") == 1
    captured = capsys.readouterr()
    assert "not reachable" in captured.err and "unplugged" in captured.err
    assert Library(tmp_settings.library_dir, tmp_settings.index_path).get("fake:ab") is not None
    assert PlaylistStore(tmp_settings.playlists_path).get(playlist.id).track_ids == ["fake:ab"]


def test_playlists_listing(
    tmp_settings: Settings, playlists: PlaylistStore, capsys: pytest.CaptureFixture[str]
) -> None:
    lib = tmp_settings.library_dir
    assert run("playlists", library=lib) == 0
    assert "No playlists yet" in capsys.readouterr().out
    created = playlists.create("Road trip")
    playlists.add_tracks(created.id, ["fake:a", "fake:b"])
    assert run("playlists", library=lib) == 0
    out = capsys.readouterr().out
    assert created.id in out and "Road trip" in out
    assert out.splitlines()[-1].split()[-1] == "2"


# -- doctor ------------------------------------------------------------------------------------


class StubProvider:
    def __init__(self, name: str, display_name: str, checks: list[tuple[bool, str, str]]) -> None:
        self.name = name
        self.display_name = display_name
        self.checks = checks

    def matches(self, url: str) -> bool:
        return False

    def doctor(self) -> list[tuple[bool, str, str]]:
        return self.checks


def _run_doctor(monkeypatch: pytest.MonkeyPatch, youtube_ok: bool, lib: Path) -> int:
    from ultimate_playlist import providers

    youtube = StubProvider(
        "youtube",
        "YouTube",
        [
            (True, "ffmpeg", "C:/ffmpeg.exe (8.0)"),
            (
                youtube_ok,
                "JavaScript runtime",
                "node 24" if youtube_ok else "Install Deno or Node.js",
            ),
            (True, "yt-dlp", "2026.08.19"),
        ],
    )
    spotify = StubProvider("spotify", "Spotify", [(False, "Spotify", "Not implemented yet")])
    monkeypatch.setattr(providers, "PROVIDERS", [youtube, spotify])
    monkeypatch.setattr(
        cli, "find_ffmpeg", lambda explicit=None: FfmpegInfo("C:/ffmpeg.exe", "8.0")
    )
    return run("doctor", library=lib)


def test_doctor_all_good(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_doctor(monkeypatch, youtube_ok=True, lib=tmp_settings.library_dir) == 0
    out = capsys.readouterr().out
    assert f"Ultimate Playlist {__version__}" in out
    assert str(tmp_settings.library_dir) in out
    assert "App data:" in out
    assert "✓ ffmpeg: C:/ffmpeg.exe (8.0)" in out
    assert out.count("ffmpeg:") == 1  # the provider's identical ffmpeg check is not repeated
    assert "YouTube:" in out and "  ✓ JavaScript runtime: node 24" in out
    assert "Spotify:" in out and "  ✗ Spotify: Not implemented yet" in out  # spotify ✗ is not fatal


def test_doctor_passes_the_live_settings_to_providers(
    tmp_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The provider checks see ffmpeg_path/js_runtimes from config.json, like downloads do."""
    from ultimate_playlist import providers

    seen: list[Settings] = []

    class Aware(StubProvider):
        def doctor(self, settings: Settings) -> list[tuple[bool, str, str]]:
            seen.append(settings)
            path = settings.ffmpeg_path or "missing"
            return [(True, "ffmpeg", f"{path} (8.0)"), (True, "JavaScript runtime", "node 24")]

    custom = str(tmp_path / "custom" / "ffmpeg.exe")
    tmp_settings.ffmpeg_path = custom
    tmp_settings.save()
    monkeypatch.setattr(providers, "PROVIDERS", [Aware("youtube", "YouTube", [])])
    monkeypatch.setattr(cli, "find_ffmpeg", lambda explicit=None: FfmpegInfo(explicit, "8.0"))
    assert run("doctor") == 0
    out = capsys.readouterr().out
    assert seen and seen[0].ffmpeg_path == custom
    assert out.count("ffmpeg:") == 1  # both agree: one line
    assert f"✓ ffmpeg: {custom} (8.0)" in out

    # when the two disagree, both lines are shown so the user sees the conflict
    monkeypatch.setattr(cli, "find_ffmpeg", lambda explicit=None: FfmpegInfo("C:/other", "7.0"))
    assert run("doctor") == 0
    out = capsys.readouterr().out
    assert "✓ ffmpeg: C:/other (7.0)" in out and f"  ✓ ffmpeg: {custom} (8.0)" in out


def test_doctor_youtube_problem_exits_1(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_doctor(monkeypatch, youtube_ok=False, lib=tmp_settings.library_dir) == 1
    out = capsys.readouterr().out
    assert "  ✗ JavaScript runtime: Install Deno or Node.js" in out
    assert "will not work" in out


def test_doctor_with_missing_ffmpeg(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ultimate_playlist import providers

    monkeypatch.setattr(providers, "PROVIDERS", [])
    monkeypatch.setattr(cli, "find_ffmpeg", lambda explicit=None: FfmpegInfo(None, None))
    assert (
        run("doctor", library=tmp_settings.library_dir) == 0
    )  # no youtube provider -> nothing fatal
    assert "✗ ffmpeg: Install ffmpeg" in capsys.readouterr().out


# -- config ------------------------------------------------------------------------------------


def test_config_show_lists_every_setting(
    tmp_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("config", "show") == 0  # no config.json yet: the defaults
    out = capsys.readouterr().out
    assert "Config file:" in out and str(Settings.default_path()) in out
    assert "concurrency = 2" in out
    assert "ffmpeg_path = (not set)" in out
    assert "embed_cover = true" in out
    assert "js_runtimes = deno, node" in out
    assert "spotify_client_id = (not set)" in out

    # a config.json from 0.2 still holds a client secret: it is neither used nor shown
    old = {**tmp_settings.to_dict(), "spotify_client_id": "abc123"}
    old["spotify_client_secret"] = "hunter2-value"
    Settings.default_path().write_text(json.dumps(old), encoding="utf-8")
    assert run("config", "show") == 0
    out = capsys.readouterr().out
    assert f"library_dir = {tmp_settings.library_dir}" in out
    assert "spotify_client_id = abc123" in out
    assert "secret" not in out and "hunter2-value" not in out
    for key in cli.CONFIG_KEYS:  # every setting is listed
        assert f"{key} = " in out


def test_config_set_saves_the_value(
    tmp_settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_settings.save()
    assert run("config", "set", "concurrency", "3") == 0
    assert capsys.readouterr().out.strip() == "concurrency = 3"
    assert Settings.load().concurrency == 3
    assert Settings.load().library_dir == tmp_settings.library_dir  # everything else kept

    new_dir = tmp_path / "moved" / "lib"
    assert not new_dir.exists()
    assert run("config", "set", "library_dir", str(new_dir)) == 0
    assert new_dir.is_dir()  # created
    assert Settings.load().library_dir == new_dir

    capsys.readouterr()
    assert run("config", "set", "spotify-client-id", " abc123 ") == 0  # dashes are fine too
    assert Settings.load().spotify_client_id == "abc123"  # stripped
    assert "spotify_client_id = abc123" in capsys.readouterr().out
    assert run("config", "set", "spotify_client_id", "") == 0  # an empty value clears
    assert Settings.load().spotify_client_id == ""
    assert "spotify_client_id = (not set)" in capsys.readouterr().out

    assert run("config", "set", "embed_cover", "no") == 0
    assert Settings.load().embed_cover is False
    assert run("config", "set", "js_runtimes", "Node, deno") == 0
    assert Settings.load().js_runtimes == ["node", "deno"]
    assert run("config", "set", "audio_quality", "192k") == 0
    assert Settings.load().audio_quality == "192"
    assert run("config", "set", "audio_format", "M4A") == 0
    assert Settings.load().audio_format == "m4a"
    assert run("config", "set", "ffmpeg_path", "") == 0
    assert Settings.load().ffmpeg_path is None


def test_config_set_rejects_bad_values(
    tmp_settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_settings.save()
    cases = [
        (("concurrency", "9"), "between 1 and 6"),
        (("concurrency", "lots"), "whole number"),
        (("colour", "purple"), "Unknown setting 'colour'"),
        (("spotify_client_id", "ünïcode"), "plain ASCII"),
        (("spotify_client_id", "x" * 101), "too long"),
        (("spotify_client_secret", "hunter2"), "no longer needs a client secret"),
        (("spotify-client-secret", ""), "only spotify_client_id is used"),
        (("embed_cover", "maybe"), "true or false"),
        (("js_runtimes", "foo"), "'foo'"),
        (("audio_format", "wma"), "mp3, m4a, opus, flac"),
        (("audio_quality", "banana"), "Audio quality"),
        (("library_dir", "   "), "cannot be empty"),
        (("ffmpeg_path", str(tmp_path / "nope" / "ffmpeg.exe")), "ffmpeg was not found"),
    ]
    for args, fragment in cases:
        assert run("config", "set", *args) == 2, args
        err = capsys.readouterr().err
        assert fragment in err, (args, err)
    err_for_unknown = None
    assert run("config", "set", "nope", "x") == 2
    err_for_unknown = capsys.readouterr().err
    for key in cli.CONFIG_KEYS:  # the error lists what can be set
        assert key in err_for_unknown
    assert Settings.load() == tmp_settings  # nothing was written by any of the failures
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder", encoding="utf-8")
    assert run("config", "set", "library_dir", str(blocker / "lib")) == 2
    assert "Cannot create the library folder" in capsys.readouterr().err


def test_config_set_hands_the_settings_to_providers(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ultimate_playlist import providers

    seen: list[Settings] = []
    monkeypatch.setattr(providers, "configure", lambda settings: seen.append(settings))
    assert run("config", "set", "spotify_client_id", "abc123") == 0
    assert len(seen) == 1 and seen[0].spotify_client_id == "abc123"
    assert run("config", "set", "concurrency", "99") == 2
    assert len(seen) == 1  # nothing configured on a rejected value


def test_config_set_ignores_the_library_override(
    tmp_settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`up --library X config set concurrency 5` must not quietly persist X as the library."""
    tmp_settings.save()
    assert run("config", "set", "concurrency", "5", library=tmp_path / "elsewhere") == 0
    assert "--library is ignored" in capsys.readouterr().err
    loaded = Settings.load()
    assert loaded.concurrency == 5
    assert loaded.library_dir == tmp_settings.library_dir


def test_config_requires_an_action(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["config"]) == 2
    assert "ACTION" in capsys.readouterr().err
    assert cli.main(["config", "set", "concurrency"]) == 2  # VALUE is required
    assert cli.main(["config", "--help"]) == 0
    assert "show" in capsys.readouterr().out


# -- config set while the app is running -------------------------------------------------------


def test_config_set_goes_through_the_running_app(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The running app writes config.json from memory: a file written behind its back would
    be lost at its next save, so the change is handed to the app instead."""
    tmp_settings.save()
    before = Settings.default_path().read_text(encoding="utf-8")
    calls: list[tuple[str, str, str, Any]] = []

    def call_running_app(base_url: str, method: str, path: str, body: Any = None) -> Any:
        calls.append((base_url, method, path, body))
        return {**tmp_settings.to_dict(), "concurrency": 3}

    pretend_running(monkeypatch, "http://127.0.0.1:8766/")
    monkeypatch.setattr(cli, "call_running_app", call_running_app)
    assert run("config", "set", "concurrency", "3") == 0
    out = capsys.readouterr().out
    assert "concurrency = 3" in out and "running app at http://127.0.0.1:8766/" in out
    assert calls == [("http://127.0.0.1:8766/", "PUT", "api/settings", {"concurrency": 3})]
    assert Settings.default_path().read_text(encoding="utf-8") == before  # the app saves it

    # values travel as the API expects them
    calls.clear()
    assert run("config", "set", "ffmpeg_path", "") == 0
    assert run("config", "set", "js_runtimes", "node") == 0
    assert [c[3] for c in calls] == [{"ffmpeg_path": ""}, {"js_runtimes": ["node"]}]


def test_config_set_reports_what_the_running_app_answered(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_settings.save()
    before = Settings.default_path().read_text(encoding="utf-8")
    pretend_running(monkeypatch, "http://127.0.0.1:8765/")
    answers: list[BaseException] = [
        cli.RunningAppError("Wait for the current downloads to finish.", 409),
        cli.RunningAppError("Cannot create the library folder X: denied", 400),
        TimeoutError("timed out"),
    ]

    def call_running_app(base_url: str, method: str, path: str, body: Any = None) -> Any:
        raise answers.pop(0)

    monkeypatch.setattr(cli, "call_running_app", call_running_app)
    assert run("config", "set", "concurrency", "4") == 1
    assert "Wait for the current downloads to finish." in capsys.readouterr().err
    assert run("config", "set", "concurrency", "4") == 2  # a value the app refused
    assert "Cannot create the library folder" in capsys.readouterr().err
    assert run("config", "set", "concurrency", "4") == 1
    assert "Nothing was changed" in capsys.readouterr().err
    assert Settings.default_path().read_text(encoding="utf-8") == before


def test_config_set_checks_the_value_before_asking_the_app(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pretend_running(monkeypatch, "http://127.0.0.1:8765/")

    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("an invalid value must not reach the running app")

    monkeypatch.setattr(cli, "call_running_app", never)
    assert run("config", "set", "concurrency", "9") == 2
    assert run("config", "set", "spotify_client_secret", "x") == 2
    assert "between 1 and 6" in capsys.readouterr().err


def test_call_running_app(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Response(io.BytesIO):
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            self.close()

    def answer(request: Any, timeout: float = 0) -> Response:
        seen.update(
            url=request.full_url,
            method=request.get_method(),
            body=request.data,
            content_type=request.get_header("Content-type"),
            timeout=timeout,
        )
        return Response(b'{"concurrency": 3}')

    monkeypatch.setattr(cli._opener, "open", answer)
    reply = cli.call_running_app(
        "http://127.0.0.1:8765/", "PUT", "api/settings", {"concurrency": 3}
    )
    assert reply == {"concurrency": 3}
    assert cli.RUNNING_APP_TIMEOUT == 5.0  # the documented limit
    assert seen == {
        "url": "http://127.0.0.1:8765/api/settings",
        "method": "PUT",
        "body": b'{"concurrency": 3}',
        "content_type": "application/json",
        "timeout": 5.0,
    }

    def refuse(request: Any, timeout: float = 0) -> Response:
        body = io.BytesIO(b'{"detail": "Concurrency must be between 1 and 6."}')
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, body)  # type: ignore[arg-type]

    monkeypatch.setattr(cli._opener, "open", refuse)
    with pytest.raises(cli.RunningAppError, match="between 1 and 6") as info:
        cli.call_running_app("http://127.0.0.1:8765/", "PUT", "api/settings", {"concurrency": 9})
    assert info.value.status == 400


def test_api_value() -> None:
    assert cli.api_value("library_dir", Path("lib")) == str(Path("lib"))
    assert cli.api_value("ffmpeg_path", None) == ""  # "" = look on PATH again; null = unchanged
    assert cli.api_value("js_runtimes", ["node"]) == ["node"]
    assert cli.api_value("embed_cover", False) is False


# -- spotify -----------------------------------------------------------------------------------

ACCESS_TOKEN = "ACCESS-token-0123456789"
REFRESH_TOKEN = "REFRESH-token-9876543210"


def write_spotify_sign_in(client_id: str = "abc123") -> Path:
    """A token file exactly as spotify_auth writes it (its format is part of that contract)."""
    path = app_data_dir() / "spotify_auth.json"
    record = {
        "refresh_token": REFRESH_TOKEN,
        "access_token": ACCESS_TOKEN,
        "expires_at": time.time() + 3600,
        "scope": "playlist-read-private playlist-read-collaborative user-library-read",
        "user_id": "listener1",
        "display_name": "Some Listener",
        "client_id": client_id,
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_spotify_status(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_settings.save()
    assert run("spotify", "status") == 0
    out = capsys.readouterr().out
    assert "Client ID:    (not set)" in out and "not connected" in out
    assert "Redirect URI: http://127.0.0.1:8765/api/spotify/callback" in out

    write_spotify_sign_in(client_id="abc123")
    tmp_settings.spotify_client_id = "abc123"
    tmp_settings.save()
    assert run("spotify", "status") == 0
    out = capsys.readouterr().out
    assert "connected as Some Listener (Spotify user listener1)" in out
    assert "user-library-read" in out
    assert ACCESS_TOKEN not in out and REFRESH_TOKEN not in out

    tmp_settings.spotify_client_id = "another-app"  # the sign-in belongs to a different app
    tmp_settings.save()
    assert run("spotify", "status") == 0
    assert "not connected (open Settings" in capsys.readouterr().out

    pretend_running(monkeypatch, "http://127.0.0.1:8767/")  # the app landed on a fallback port
    assert run("spotify", "status") == 0
    out = capsys.readouterr().out
    assert "Redirect URI: http://127.0.0.1:8767/api/spotify/callback" in out


def test_spotify_login_prints_the_steps_and_opens_the_running_app(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))
    tmp_settings.save()  # no Client ID yet: the developer-app steps come first
    assert run("spotify", "login") == 0
    out = capsys.readouterr().out
    assert "https://developer.spotify.com/dashboard" in out
    assert "http://127.0.0.1:8765/api/spotify/callback" in out
    assert "config set spotify_client_id" in out and "Connect Spotify" in out
    # from source the command is `uv run up` (a bare `up` is only on PATH in an active venv)
    assert "Run `uv run up config set spotify_client_id <its Client ID>`" in out
    assert "(run `uv run up`)" in out
    assert "the app's owner needs Spotify Premium" in out  # not every account that connects
    assert opened == []

    tmp_settings.spotify_client_id = "abc123"
    tmp_settings.save()
    assert run("spotify", "login") == 0  # no running app: the steps only
    out = capsys.readouterr().out
    assert "Connect Spotify" in out and "dashboard" not in out
    assert opened == []

    pretend_running(monkeypatch, "http://127.0.0.1:8766/")
    assert run("spotify", "login") == 0
    assert "opening http://127.0.0.1:8766/api/spotify/login" in capsys.readouterr().out
    assert opened == ["http://127.0.0.1:8766/api/spotify/login"]


def test_spotify_logout_without_a_running_app(
    tmp_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = write_spotify_sign_in()
    assert run("spotify", "logout") == 0
    assert "Disconnected from Spotify" in capsys.readouterr().out
    assert not token_file.exists()
    assert run("spotify", "logout") == 0
    assert "was not connected" in capsys.readouterr().out


def test_spotify_logout_goes_through_the_running_app(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, str, str]] = []

    def call_running_app(base_url: str, method: str, path: str, body: Any = None) -> Any:
        calls.append((base_url, method, path))
        return {"ok": True}

    pretend_running(monkeypatch, "http://127.0.0.1:8765/")
    monkeypatch.setattr(cli, "call_running_app", call_running_app)
    assert run("spotify", "logout") == 0
    assert "Disconnected from Spotify" in capsys.readouterr().out
    assert calls == [("http://127.0.0.1:8765/", "POST", "api/spotify/logout")]

    def unreachable(*args: object, **kwargs: object) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr(cli, "call_running_app", unreachable)
    assert run("spotify", "logout") == 1
    assert "could not disconnect" in capsys.readouterr().err


def test_spotify_requires_an_action(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["spotify"]) == 2
    assert "ACTION" in capsys.readouterr().err
    assert cli.main(["spotify", "--help"]) == 0
    out = capsys.readouterr().out
    assert "status" in out and "login" in out and "logout" in out


def test_port_option_names_the_running_app(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An app started with `serve --port N` is not on the ports the scan tries: `--port N`
    points `config set` and `spotify` at it, and only that port is asked."""
    tmp_settings.save()
    probed: list[int] = []

    def running(host: str, port: int, timeout: float = cli.JOIN_TIMEOUT) -> str | None:
        probed.append(port)
        return f"http://{host}:{port}/" if port == 9000 else None

    def scan(*args: object, **kwargs: object) -> None:
        raise AssertionError("--port must not scan the default ports")

    calls: list[tuple[str, str, str, Any]] = []

    def call_running_app(base_url: str, method: str, path: str, body: Any = None) -> Any:
        calls.append((base_url, method, path, body))
        return {"concurrency": 3}

    monkeypatch.setattr(cli, "running_instance", running)
    monkeypatch.setattr(cli, "find_running_instance", scan)
    monkeypatch.setattr(cli, "call_running_app", call_running_app)
    assert run("config", "set", "concurrency", "3", "--port", "9000") == 0
    assert calls == [("http://127.0.0.1:9000/", "PUT", "api/settings", {"concurrency": 3})]
    assert "running app at http://127.0.0.1:9000/" in capsys.readouterr().out
    assert run("spotify", "status", "--port", "9000") == 0
    assert "Redirect URI: http://127.0.0.1:9000/api/spotify/callback" in capsys.readouterr().out

    # nothing on that port: the file is written, and the Redirect URI still names the port
    assert run("spotify", "status", "--port", "9001") == 0
    assert "Redirect URI: http://127.0.0.1:9001/api/spotify/callback" in capsys.readouterr().out
    assert run("config", "set", "concurrency", "4", "--port", "9001") == 0
    assert Settings.load().concurrency == 4
    assert probed == [9000, 9000, 9001, 9001]
    assert len(calls) == 1

    assert cli.main(["config", "set", "concurrency", "3", "--port", "70000"]) == 2
    assert "1 to 65535" in capsys.readouterr().err


# -- export ------------------------------------------------------------------------------------


@pytest.fixture
def exported_setup(
    tmp_settings: Settings, library: Library, playlists: PlaylistStore
) -> tuple[Path, str]:
    lib = tmp_settings.library_dir
    write_tiny_mp3(lib / "Alpha - One.mp3", artist="Alpha", title="One", track_id="fake:one")
    write_tiny_mp3(lib / "Beta - Two.mp3", artist="Beta", title="Two", track_id="fake:two")
    library.rescan()
    playlist = playlists.create("Road trip")
    playlists.add_tracks(playlist.id, ["fake:one", "fake:two", "fake:gone"])
    return lib, playlist.id


def test_export_by_name_relative_paths(
    exported_setup: tuple[Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    lib, _pid = exported_setup
    assert run("export", "road TRIP", library=lib) == 0
    out = capsys.readouterr().out
    target = lib / "Playlists" / "Road trip.m3u8"
    assert str(target) in out and "2 track(s)" in out and "Skipped 1" in out
    lines = target.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#EXTINF:0,Alpha - One"
    assert lines[2] == "../Alpha - One.mp3"
    assert lines[4] == "../Beta - Two.mp3"
    assert len(lines) == 5  # the missing track was skipped


def test_export_by_id_to_explicit_path_uses_absolute_paths(
    exported_setup: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lib, pid = exported_setup
    out_file = tmp_path / "elsewhere" / "trip.m3u8"
    assert run("export", pid, str(out_file), library=lib) == 0
    assert str(out_file) in capsys.readouterr().out
    lines = out_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[2] == str(lib / "Alpha - One.mp3")


def test_export_to_a_directory(exported_setup: tuple[Path, str], tmp_path: Path) -> None:
    lib, pid = exported_setup
    out_dir = tmp_path / "outdir"
    out_dir.mkdir()
    assert run("export", pid, str(out_dir), library=lib) == 0
    assert (out_dir / "Road trip.m3u8").read_text(encoding="utf-8").startswith("#EXTM3U")


def test_export_unknown_and_ambiguous(
    exported_setup: tuple[Path, str], playlists: PlaylistStore, capsys: pytest.CaptureFixture[str]
) -> None:
    lib, _pid = exported_setup
    assert run("export", "No such list", library=lib) == 1
    assert "No playlist named 'No such list'" in capsys.readouterr().err
    playlists.create("Road trip")  # duplicate name
    assert run("export", "Road trip", library=lib) == 1
    assert "use an id instead" in capsys.readouterr().err


# -- helpers -----------------------------------------------------------------------------------


def test_format_duration() -> None:
    assert cli.format_duration(None) == "-"
    assert cli.format_duration(0.3) == "0:00"
    assert cli.format_duration(65) == "1:05"
    assert cli.format_duration(3725) == "1:02:05"


def test_cli_logging_installs_handlers_only_on_a_bare_root(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pytest always leaves handlers on the root logger, so this wiring is otherwise untested."""
    calls: list[dict[str, int]] = []

    def record(console_level: int, file_level: int) -> list[logging.Handler]:
        calls.append({"console_level": console_level, "file_level": file_level})
        return []

    monkeypatch.setattr(logs, "install_handlers", record)
    root = logging.getLogger()
    original = list(root.handlers)
    try:
        root.handlers = []
        cli._configure_cli_logging()
        assert calls == [{"console_level": logging.WARNING, "file_level": logging.INFO}]
        root.handlers = [logging.NullHandler()]
        cli._configure_cli_logging()  # an embedding program already configured logging
        assert len(calls) == 1
    finally:
        root.handlers = original
