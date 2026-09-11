"""bundled.py: the bin folder next to the packaged exe (or ULTIMATE_PLAYLIST_BIN) and the
frozen-mode touches in the CLI. Nothing here runs a real tool."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from ultimate_playlist import cli
from ultimate_playlist.bundled import (
    ENV_BIN,
    app_dir,
    bin_dir,
    bundled_tool,
    bundled_tools,
    is_bundled,
    is_frozen,
)
from ultimate_playlist.ffmpeg import FfmpegInfo

WINDOWS = sys.platform.startswith("win")
EXE = ".exe" if WINDOWS else ""
windows_only = pytest.mark.skipif(not WINDOWS, reason="the .exe suffix is a Windows convention")


@pytest.fixture(autouse=True)
def from_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test as a plain source checkout: not frozen, no bin folder override."""
    monkeypatch.delenv(ENV_BIN, raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)


@pytest.fixture
def bin_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "bin"
    folder.mkdir()
    monkeypatch.setenv(ENV_BIN, str(folder))
    return folder


def touch(folder: Path, name: str) -> Path:
    path = folder / name
    path.write_bytes(b"stub")
    return path


def freeze(monkeypatch: pytest.MonkeyPatch, app: Path) -> Path:
    """Pretend PyInstaller started us from <app>/UltimatePlaylist.exe."""
    app.mkdir(parents=True, exist_ok=True)
    exe = touch(app, "UltimatePlaylist.exe")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    return exe


# -- from source ---------------------------------------------------------------------------------


def test_source_checkout_has_nothing_bundled() -> None:
    assert is_frozen() is False
    assert app_dir() is None
    assert bin_dir() is None
    assert bundled_tool("ffmpeg") is None
    assert bundled_tools() == {}
    assert is_bundled("ffmpeg", "C:/somewhere/ffmpeg.exe") is False


# -- ULTIMATE_PLAYLIST_BIN -----------------------------------------------------------------------


def test_env_override_names_the_bin_folder(bin_folder: Path) -> None:
    assert bin_dir() == bin_folder.absolute()


def test_env_override_expands_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "tools"
    folder.mkdir()
    monkeypatch.setenv("UP_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv(ENV_BIN, "$UP_TEST_ROOT/tools")
    assert bin_dir() == folder.absolute()


def test_env_override_that_is_not_a_directory_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from ultimate_playlist import bundled

    bundled._warned_env_values.clear()
    missing = str(tmp_path / "missing")
    monkeypatch.setenv(ENV_BIN, missing)
    with caplog.at_level(logging.WARNING, logger="ultimate_playlist.bundled"):
        assert bin_dir() is None
        assert bundled_tool("ffmpeg") is None
        assert bin_dir() is None
    # the user set the variable on purpose: one WARNING says why it is ignored, not one per probe
    warned = [r for r in caplog.records if "is not a directory; ignoring it" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING
    assert missing in warned[0].getMessage()
    a_file = touch(tmp_path, "not-a-folder")
    monkeypatch.setenv(ENV_BIN, str(a_file))
    assert bin_dir() is None
    monkeypatch.setenv(ENV_BIN, "")
    assert bin_dir() is None
    bundled._warned_env_values.clear()


def test_env_override_with_quotes_from_cmd(
    bin_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`set ULTIMATE_PLAYLIST_BIN="C:\\tools\\my bin"` in cmd keeps the quotes in the value."""
    monkeypatch.setenv(ENV_BIN, f'"{bin_folder}"')
    assert bin_dir() == bin_folder.absolute()
    monkeypatch.setenv(ENV_BIN, f'  "{bin_folder}"  ')
    assert bin_dir() == bin_folder.absolute()
    ffmpeg = touch(bin_folder, f"ffmpeg{EXE}")
    assert bundled_tool("ffmpeg") == str(ffmpeg)


# -- bundled_tool --------------------------------------------------------------------------------


@windows_only
def test_tool_with_exe_suffix(bin_folder: Path) -> None:
    ffmpeg = touch(bin_folder, "ffmpeg.exe")
    assert bundled_tool("ffmpeg") == str(ffmpeg)
    assert bundled_tool("ffmpeg.exe") == str(ffmpeg)  # a name that already carries the suffix
    assert bundled_tool("ffprobe") is None


def test_tool_with_bare_name(bin_folder: Path) -> None:
    deno = touch(bin_folder, "deno")
    assert bundled_tool("deno") == str(deno)
    assert bundled_tool("node") is None


@windows_only
def test_exe_suffix_wins_over_bare_name(bin_folder: Path) -> None:
    touch(bin_folder, "node")
    exe = touch(bin_folder, "node.exe")
    assert bundled_tool("node") == str(exe)


def test_directories_are_not_tools(bin_folder: Path) -> None:
    (bin_folder / f"node{EXE}").mkdir()
    assert bundled_tool("node") is None


def test_bundled_tools_lists_only_what_is_present(bin_folder: Path) -> None:
    ffmpeg = touch(bin_folder, f"ffmpeg{EXE}")
    node = touch(bin_folder, f"node{EXE}")
    touch(bin_folder, f"something-else{EXE}")
    assert bundled_tools() == {"ffmpeg": str(ffmpeg), "node": str(node)}


def test_is_bundled_compares_normalised_paths(bin_folder: Path) -> None:
    ffmpeg = touch(bin_folder, f"ffmpeg{EXE}")
    assert is_bundled("ffmpeg", str(ffmpeg)) is True
    assert is_bundled("ffmpeg", ffmpeg) is True
    assert is_bundled("ffmpeg", str(bin_folder / ".." / "bin" / ffmpeg.name)) is True
    if WINDOWS:
        assert is_bundled("ffmpeg", str(ffmpeg).upper()) is True
    assert is_bundled("ffmpeg", None) is False
    assert is_bundled("ffmpeg", str(bin_folder / "other" / ffmpeg.name)) is False
    assert is_bundled("ffprobe", str(ffmpeg)) is False  # present file, different tool


# -- frozen (PyInstaller) ------------------------------------------------------------------------


def test_frozen_app_dir_and_bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = tmp_path / "dist" / "UltimatePlaylist"
    freeze(monkeypatch, app)
    assert is_frozen() is True
    assert app_dir() == app.resolve()
    assert bin_dir() is None  # no bin folder shipped -> PATH lookups as usual
    assert bundled_tool("ffprobe") is None

    (app / "bin").mkdir()
    assert bin_dir() == app.resolve() / "bin"
    ffprobe = touch(app / "bin", f"ffprobe{EXE}")
    assert bundled_tool("ffprobe") == str(app.resolve() / "bin" / ffprobe.name)
    assert bundled_tools() == {"ffprobe": bundled_tool("ffprobe")}


def test_env_override_beats_the_frozen_bin_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "app"
    freeze(monkeypatch, app)
    (app / "bin").mkdir()
    touch(app / "bin", f"ffmpeg{EXE}")
    other = tmp_path / "other"
    other.mkdir()
    wanted = touch(other, f"ffmpeg{EXE}")
    monkeypatch.setenv(ENV_BIN, str(other))
    assert bundled_tool("ffmpeg") == str(wanted)

    # ...but a broken override falls back to the folder next to the exe
    monkeypatch.setenv(ENV_BIN, str(tmp_path / "missing"))
    assert bundled_tool("ffmpeg") == str(app.resolve() / "bin" / f"ffmpeg{EXE}")


def test_frozen_with_missing_executable_still_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "gone" / "UltimatePlaylist.exe"))
    assert app_dir() == (tmp_path / "gone").resolve()
    assert bin_dir() is None


# -- CLI touches ---------------------------------------------------------------------------------


def _stub_serve(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, bool]]:
    from ultimate_playlist.server import app as server_app

    calls: list[tuple[str, int, bool]] = []

    def fake_serve(settings: object, host: str, port: int, open_browser: bool) -> None:
        calls.append((host, port, open_browser))

    monkeypatch.setattr(server_app, "serve", fake_serve)
    return calls


def test_frozen_serve_prints_a_banner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _stub_serve(monkeypatch)
    monkeypatch.setattr(cli, "is_frozen", lambda: True)
    monkeypatch.setattr(cli, "running_instance", lambda host, port, timeout=1.0: None)
    monkeypatch.setattr(cli, "_set_console_title", lambda title: None)
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "Starting Ultimate Playlist" in out
    assert "Your browser will open." in out
    assert "Close this window to stop." in out
    assert calls == [("127.0.0.1", 8765, True)]

    assert cli.main(["serve", "--no-browser", "--port", "8791"]) == 0
    out = capsys.readouterr().out
    assert "Close this window to stop." in out and "browser" not in out
    assert calls[-1] == ("127.0.0.1", 8791, False)


def test_serve_from_source_prints_no_banner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_serve(monkeypatch)
    assert cli.main([]) == 0
    assert capsys.readouterr().out == ""


def test_doctor_says_bundled_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI's own ffmpeg line and the provider's agree, so the bundled copy is printed once."""
    from ultimate_playlist import providers
    from ultimate_playlist.providers import youtube
    from ultimate_playlist.providers.youtube import YouTubeProvider

    info = FfmpegInfo("C:/app/bin/ffmpeg.exe", "8.1.1", bundled=True)
    monkeypatch.setattr(cli, "find_ffmpeg", lambda explicit=None: info)
    monkeypatch.setattr(youtube, "find_ffmpeg", lambda explicit=None: info)
    monkeypatch.setattr(providers, "PROVIDERS", [YouTubeProvider()])
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "✓ ffmpeg: C:/app/bin/ffmpeg.exe (8.1.1, bundled)" in out
    assert out.count("ffmpeg:") == 1
