"""find_ffmpeg() against a fake PATH (no real ffmpeg needed)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from ultimate_playlist import ffmpeg as ffmpeg_mod
from ultimate_playlist.bundled import ENV_BIN
from ultimate_playlist.ffmpeg import (
    FfmpegInfo,
    ProbeTimeout,
    find_ffmpeg,
    install_hint,
    missing_bundled_hint,
    resolve_explicit,
)

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


@pytest.fixture(autouse=True)
def fresh_version_cache() -> None:
    """Probes are cached per file; every test starts without answers from an earlier one."""
    ffmpeg_mod.clear_version_cache()


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
    assert info.bundled is False


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
    missing = str(tmp_path / "does-not-exist" / "ffmpeg.exe")
    info = find_ffmpeg(missing)
    assert info.found is True
    assert Path(info.path).resolve() == stub.resolve()
    assert info.note == f"ffmpeg_path {missing} does not exist, using the one on PATH"
    assert info.describe() == f"{info.path} (9.9.9-test); {info.note}"


def test_explicit_that_cannot_run_is_noted(
    isolated_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "custom" / "ffmpeg.exe"
    broken.parent.mkdir()
    broken.write_bytes(b"MZ this is not a program")
    stub = make_stub(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(stub.parent))
    info = find_ffmpeg(str(broken))
    assert Path(info.path).resolve() == stub.resolve()
    assert info.note == f"ffmpeg_path {broken} cannot be run, using the one on PATH"


def test_bad_explicit_and_nothing_on_path(isolated_path: Path, tmp_path: Path) -> None:
    assert find_ffmpeg(str(tmp_path / "nope" / "ffmpeg.exe")) == FfmpegInfo(None, None)


def test_good_explicit_has_no_note(isolated_path: Path, tmp_path: Path) -> None:
    stub = make_stub(tmp_path / "custom")
    info = find_ffmpeg(str(stub))
    assert info.note is None
    assert info.describe() == f"{info.path} (9.9.9-test)"


def test_explicit_bare_name_is_looked_up_on_path(
    isolated_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg_path = "ffmpeg" means the copy on PATH, for doctor and for yt-dlp alike."""
    stub = make_stub(tmp_path / "onpath")
    monkeypatch.setenv("PATH", str(stub.parent))
    assert Path(resolve_explicit("ffmpeg")).resolve() == stub.resolve()
    info = find_ffmpeg("ffmpeg")
    assert Path(info.path).resolve() == stub.resolve()
    assert info.version == "9.9.9-test" and info.note is None


def test_resolve_explicit(isolated_path: Path, tmp_path: Path) -> None:
    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    folder = tmp_path / "ff"
    folder.mkdir()
    assert resolve_explicit(str(folder)) is None  # a folder without ffmpeg in it
    inside = folder / exe
    inside.write_bytes(b"stub")
    assert resolve_explicit(str(folder)) == str(inside)  # the folder -> the file in it
    assert resolve_explicit(str(inside)) == str(inside)  # the file itself
    assert resolve_explicit(str(tmp_path / "missing" / exe)) is None
    assert resolve_explicit("no-such-program-anywhere") is None  # bare name, not on PATH


def test_version_is_probed_once_per_file(
    isolated_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/api/status and the provider's doctor() both ask; the answer is shared per (path, mtime, size)."""
    stub = make_stub(tmp_path / "bin")
    calls: list[list[str]] = []
    real_run = ffmpeg_mod.subprocess.run

    def counting_run(cmd: list[str], **kwargs: object) -> object:
        calls.append(cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(ffmpeg_mod.subprocess, "run", counting_run)
    assert ffmpeg_mod._version_of(str(stub)) == "9.9.9-test"
    assert ffmpeg_mod._version_of(str(stub)) == "9.9.9-test"
    assert find_ffmpeg(str(stub)).version == "9.9.9-test"
    assert len(calls) == 1
    # a rewritten file (new size) is probed again
    make_stub(tmp_path / "bin", first_line="ffmpeg version 10.0.0-test Copyright")
    assert ffmpeg_mod._version_of(str(stub)) == "10.0.0-test"
    assert len(calls) == 2
    # failures are never cached
    assert ffmpeg_mod._version_of(str(tmp_path / "missing.exe")) is None
    assert ffmpeg_mod._version_of(str(tmp_path / "missing.exe")) is None
    assert len(calls) == 4


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


def test_install_hint_in_the_packaged_build_says_re_extract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zip user whose bin/ffmpeg.exe is gone must not be sent to winget."""
    app = tmp_path / "UltimatePlaylist"
    app.mkdir()
    (app / "UltimatePlaylist.exe").write_bytes(b"stub")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "UltimatePlaylist.exe"))
    expected = (
        app.resolve() / "bin" / ("ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg")
    )
    hint = install_hint()  # no bin folder at all
    assert hint.startswith("bin\\ffmpeg.exe is missing next to UltimatePlaylist.exe")
    assert str(expected) in hint
    assert "Extract the whole zip again" in hint and "winget" not in hint
    (app / "bin").mkdir()  # the folder exists but the file is gone (quarantine)
    assert str(expected) in install_hint()


def test_missing_bundled_hint_names_the_folder_when_no_file_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several runtimes are acceptable and the zip ships one of them: point at bin/, not at
    deno.exe (a build around node.exe never contained deno.exe)."""
    assert missing_bundled_hint("bin\\deno.exe or bin\\node.exe") is None  # from source
    app = tmp_path / "UltimatePlaylist"
    app.mkdir()
    (app / "UltimatePlaylist.exe").write_bytes(b"stub")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "UltimatePlaylist.exe"))
    hint = missing_bundled_hint("bin\\deno.exe or bin\\node.exe")
    assert hint.startswith("bin\\deno.exe or bin\\node.exe is missing next to UltimatePlaylist.exe")
    assert f"(expected in {app.resolve() / 'bin'})" in hint
    assert "expected at" not in hint and "deno.exe)" not in hint


# -- the bundled bin folder of the no-install package ------------------------------------------

EXE = ".exe" if sys.platform.startswith("win") else ""


@pytest.fixture(autouse=True)
def no_bundled_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tests above describe a source checkout: no bin folder unless a test makes one."""
    monkeypatch.delenv(ENV_BIN, raising=False)


@pytest.fixture
def bundled_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bin folder that already holds ffprobe (a complete package ships both)."""
    folder = tmp_path / "bundled"
    folder.mkdir()
    (folder / f"ffprobe{EXE}").write_bytes(b"stub")
    monkeypatch.setenv(ENV_BIN, str(folder))
    return folder


@pytest.fixture
def any_file_is_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stub named ffmpeg.exe cannot actually run; count every existing file as version 'stub'."""
    monkeypatch.setattr(
        ffmpeg_mod,
        "_version_of",
        lambda path, timeout=None: "stub" if Path(path).is_file() else None,
    )


def test_bundled_beats_path(
    isolated_path: Path,
    bundled_bin: Path,
    any_file_is_ffmpeg: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on_path = make_stub(tmp_path / "onpath")
    monkeypatch.setenv("PATH", str(on_path.parent))
    bundled = bundled_bin / f"ffmpeg{EXE}"
    bundled.write_bytes(b"stub")
    info = find_ffmpeg()
    assert Path(info.path).resolve() == bundled.resolve()
    assert info.bundled is True
    assert info.describe() == f"{info.path} (stub, bundled)"


def test_explicit_beats_bundled(
    isolated_path: Path, bundled_bin: Path, any_file_is_ffmpeg: None, tmp_path: Path
) -> None:
    (bundled_bin / f"ffmpeg{EXE}").write_bytes(b"stub")
    explicit = make_stub(tmp_path / "custom")
    info = find_ffmpeg(str(explicit))
    assert Path(info.path).resolve() == explicit.resolve()
    assert info.bundled is False


def test_explicit_folder_is_resolved_to_the_ffmpeg_inside(
    isolated_path: Path, bundled_bin: Path, any_file_is_ffmpeg: None, tmp_path: Path
) -> None:
    """ffmpeg_path pasted from Explorer names the folder: doctor must not call that broken."""
    (bundled_bin / f"ffmpeg{EXE}").write_bytes(b"stub")
    folder = tmp_path / "my ffmpeg"
    folder.mkdir()
    inside = folder / f"ffmpeg{EXE}"
    inside.write_bytes(b"stub")
    info = find_ffmpeg(str(folder))
    assert info.path == str(inside)
    assert info.bundled is False and info.note is None
    assert info.describe() == f"{inside} (stub)"


def test_explicit_folder_without_ffmpeg_is_noted(
    isolated_path: Path, bundled_bin: Path, any_file_is_ffmpeg: None, tmp_path: Path
) -> None:
    (bundled_bin / f"ffmpeg{EXE}").write_bytes(b"stub")
    folder = tmp_path / "empty folder"
    folder.mkdir()
    info = find_ffmpeg(str(folder))
    assert info.bundled is True
    assert info.note == (
        f"ffmpeg_path {folder} is a folder without ffmpeg{EXE}, using the bundled copy"
    )


def test_explicit_path_to_the_bundled_copy_counts_as_bundled(
    isolated_path: Path, bundled_bin: Path, any_file_is_ffmpeg: None
) -> None:
    bundled = bundled_bin / f"ffmpeg{EXE}"
    bundled.write_bytes(b"stub")
    info = find_ffmpeg(str(bundled))
    assert Path(info.path).resolve() == bundled.resolve()
    assert info.bundled is True


def test_broken_bundled_copy_falls_back_to_path(
    isolated_path: Path, bundled_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real execution: a bin/ffmpeg.exe that cannot run must not hide the working one on PATH."""
    broken = bundled_bin / f"ffmpeg{EXE}"
    broken.write_bytes(b"MZ this is not a program")
    if not sys.platform.startswith("win"):
        broken.chmod(0o755)
    on_path = make_stub(tmp_path / "onpath")
    monkeypatch.setenv("PATH", str(on_path.parent))
    info = find_ffmpeg()
    assert info.found is True
    assert Path(info.path).resolve() == on_path.resolve()
    assert info.version == "9.9.9-test"
    assert info.bundled is False


def test_bundled_folder_without_ffmpeg_changes_nothing(
    isolated_path: Path, bundled_bin: Path
) -> None:
    (bundled_bin / f"node{EXE}").write_bytes(b"stub")
    assert find_ffmpeg() == FfmpegInfo(None, None)


def test_bundled_ffmpeg_without_ffprobe_is_noted(
    isolated_path: Path, bundled_bin: Path, any_file_is_ffmpeg: None
) -> None:
    """Half a bin folder (ffprobe quarantined) still works, but doctor must say so."""
    bundled = bundled_bin / f"ffmpeg{EXE}"
    bundled.write_bytes(b"stub")
    (bundled_bin / f"ffprobe{EXE}").unlink()
    info = find_ffmpeg()
    assert Path(info.path).resolve() == bundled.resolve() and info.bundled is True
    assert info.note == (
        "bin\\ffprobe.exe is missing next to it (thumbnails/codec probing fall back to ffmpeg)"
    )
    assert info.describe() == f"{info.path} (stub, bundled); {info.note}"
    (bundled_bin / f"ffprobe{EXE}").write_bytes(b"stub")
    assert find_ffmpeg().note is None


def test_missing_explicit_falls_back_to_the_bundled_copy_with_a_note(
    isolated_path: Path, bundled_bin: Path, any_file_is_ffmpeg: None, tmp_path: Path
) -> None:
    """Doctor and the downloads (_ydl_opts) must name the same copy: the bundled one."""
    bundled = bundled_bin / f"ffmpeg{EXE}"
    bundled.write_bytes(b"stub")
    missing = str(tmp_path / "old" / "ffmpeg.exe")
    info = find_ffmpeg(missing)
    assert Path(info.path).resolve() == bundled.resolve()
    assert info.bundled is True
    assert info.note == f"ffmpeg_path {missing} does not exist, using the bundled copy"
    assert info.describe() == f"{info.path} (stub, bundled); {info.note}"


def test_bundled_copy_gets_the_long_timeout(
    isolated_path: Path, bundled_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, float]] = []

    def record(path: str, timeout: float = ffmpeg_mod.PROBE_TIMEOUT) -> str:
        seen.append((path, timeout))
        return "stub"

    monkeypatch.setattr(ffmpeg_mod, "_version_of", record)
    bundled = bundled_bin / f"ffmpeg{EXE}"
    bundled.write_bytes(b"stub")
    assert find_ffmpeg().bundled is True
    on_path = make_stub(tmp_path / "onpath")
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.delenv(ENV_BIN)
    assert find_ffmpeg().bundled is False
    assert [t for _p, t in seen] == [ffmpeg_mod.BUNDLED_PROBE_TIMEOUT, ffmpeg_mod.PROBE_TIMEOUT]
    assert ffmpeg_mod.BUNDLED_PROBE_TIMEOUT >= 20


def test_slow_bundled_copy_is_still_reported_as_present(
    isolated_path: Path, bundled_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cold-cache / Defender-scanned first start must not turn the ffmpeg line red."""
    bundled = bundled_bin / f"ffmpeg{EXE}"
    bundled.write_bytes(b"stub")

    def slow(path: str, timeout: float = ffmpeg_mod.PROBE_TIMEOUT) -> str:
        raise ProbeTimeout(path)

    monkeypatch.setattr(ffmpeg_mod, "_version_of", slow)
    info = find_ffmpeg()
    assert Path(info.path).resolve() == bundled.resolve()
    assert info.found is True and info.bundled is True and info.version is None
    assert info.note and "did not answer" in info.note
    assert info.describe().startswith(f"{info.path} (version not read, bundled); ")


def test_missing_explicit_and_slow_bundled_copy_keep_both_notes(
    isolated_path: Path, bundled_bin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout note must not erase the fact that the configured path was skipped."""
    bundled = bundled_bin / f"ffmpeg{EXE}"
    bundled.write_bytes(b"stub")

    def slow(path: str, timeout: float = ffmpeg_mod.PROBE_TIMEOUT) -> str:
        raise ProbeTimeout(path)

    monkeypatch.setattr(ffmpeg_mod, "_version_of", slow)
    missing = str(tmp_path / "old" / "ffmpeg.exe")
    info = find_ffmpeg(missing)
    assert Path(info.path).resolve() == bundled.resolve() and info.bundled is True
    assert info.note == (
        f"ffmpeg_path {missing} does not exist, using the bundled copy; "
        f"-version did not answer within {ffmpeg_mod.BUNDLED_PROBE_TIMEOUT:.0f} s, "
        "assuming it works"
    )
    (bundled_bin / f"ffprobe{EXE}").unlink()  # ...and the ffprobe note joins the list
    assert find_ffmpeg(missing).note.endswith("fall back to ffmpeg)")
    assert find_ffmpeg(missing).note.startswith(f"ffmpeg_path {missing} does not exist")


def test_slow_copy_on_path_is_skipped(
    isolated_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = make_stub(tmp_path / "slow")
    fast = make_stub(tmp_path / "fast")

    def probe(path: str, timeout: float = ffmpeg_mod.PROBE_TIMEOUT) -> str:
        if Path(path).resolve() == slow.resolve():
            raise ProbeTimeout(path)
        return "fast"

    monkeypatch.setattr(ffmpeg_mod, "_version_of", probe)
    monkeypatch.setenv("PATH", str(fast.parent))
    info = find_ffmpeg(str(slow))
    assert Path(info.path).resolve() == fast.resolve()
    assert info.note == f"ffmpeg_path {slow} cannot be run, using the one on PATH"


def test_describe() -> None:
    assert FfmpegInfo("C:/ff/ffmpeg.exe", "8.0").describe() == "C:/ff/ffmpeg.exe (8.0)"
    bundled = FfmpegInfo("C:/app/bin/ffmpeg.exe", "8.0", bundled=True)
    assert bundled.describe() == "C:/app/bin/ffmpeg.exe (8.0, bundled)"
    noted = FfmpegInfo("C:/app/bin/ffmpeg.exe", "8.0", bundled=True, note="why")
    assert noted.describe() == "C:/app/bin/ffmpeg.exe (8.0, bundled); why"
    assert FfmpegInfo(None, None).describe() == "not found"
    assert FfmpegInfo(None, None).bundled is False
    assert FfmpegInfo(None, None).note is None
