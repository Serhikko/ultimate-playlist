"""find_ffmpeg() against a fake PATH (no real ffmpeg needed)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from ultimate_playlist import ffmpeg as ffmpeg_mod
from ultimate_playlist.ffmpeg import FfmpegInfo, find_ffmpeg, install_hint

FAKE_VERSION_LINE = "ffmpeg version 9.9.9-test Copyright (c) 2000-2026 the FFmpeg developers"


def make_stub(directory: Path, name: str = "ffmpeg", first_line: str = FAKE_VERSION_LINE) -> Path:
    """Create an executable that prints a fake `ffmpeg -version` banner."""
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        stub = directory / f"{name}.bat"
        stub.write_text(
            f"@echo off\r\necho {first_line}\r\necho built with gcc\r\n", encoding="ascii"
        )
    else:
        stub = directory / name
        stub.write_text(
            f'#!/bin/sh\necho "{first_line}"\necho "built with gcc"\n', encoding="ascii"
        )
        stub.chmod(0o755)
    return stub


@pytest.fixture
def isolated_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH containing only an empty directory; the optional imageio-ffmpeg wheel is disabled."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)  # makes `import imageio_ffmpeg` fail
    return empty


def test_found_on_fake_path(
    isolated_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    stub = make_stub(bin_dir)
    monkeypatch.setenv("PATH", os.pathsep.join([str(isolated_path), str(bin_dir)]))
    info = find_ffmpeg()
    assert info.found is True
    assert info.version == "9.9.9-test"
    assert Path(info.path).resolve() == stub.resolve()


def test_nothing_on_path(isolated_path: Path) -> None:
    info = find_ffmpeg()
    assert info == FfmpegInfo(path=None, version=None)
    assert info.found is False


def test_explicit_path_wins(isolated_path: Path, tmp_path: Path) -> None:
    stub = make_stub(tmp_path / "custom")
    info = find_ffmpeg(str(stub))
    assert info.found is True
    assert info.version == "9.9.9-test"
    assert Path(info.path).resolve() == stub.resolve()


def test_bad_explicit_falls_back_to_path(
    isolated_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    stub = make_stub(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir))
    info = find_ffmpeg(str(tmp_path / "does-not-exist" / "ffmpeg.exe"))
    assert info.found is True
    assert Path(info.path).resolve() == stub.resolve()


def test_bad_explicit_and_nothing_on_path(isolated_path: Path, tmp_path: Path) -> None:
    assert find_ffmpeg(str(tmp_path / "nope" / "ffmpeg.exe")) == FfmpegInfo(None, None)


def test_unparseable_banner_still_counts_as_found(isolated_path: Path, tmp_path: Path) -> None:
    stub = make_stub(tmp_path / "weird", first_line="some other banner")
    info = find_ffmpeg(str(stub))
    assert info.found is True
    assert info.version == "some other banner"


def test_version_of_handles_failures(tmp_path: Path) -> None:
    assert ffmpeg_mod._version_of(str(tmp_path / "missing.exe")) is None
    assert ffmpeg_mod._version_of(str(tmp_path)) is None  # a directory is not runnable


def test_imageio_ffmpeg_fallback(
    isolated_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types

    stub = make_stub(tmp_path / "wheel")
    fake_module = types.SimpleNamespace(get_ffmpeg_exe=lambda: str(stub))
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", fake_module)
    info = find_ffmpeg()
    assert info.found is True
    assert Path(info.path).resolve() == stub.resolve()


def test_install_hint() -> None:
    hint = install_hint()
    assert hint
    if sys.platform.startswith("win"):
        assert "winget install Gyan.FFmpeg" in hint
    elif sys.platform == "darwin":
        assert "brew install ffmpeg" in hint
    else:
        assert "ffmpeg" in hint
