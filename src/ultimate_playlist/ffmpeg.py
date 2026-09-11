"""Locate ffmpeg. yt-dlp needs it to convert to MP3 and embed cover art."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass

from .bundled import app_dir, bin_dir, bundled_tool, executable_names, is_bundled, is_frozen

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 5.0
# The bundled gyan.dev build is a 100-230 MB static exe: on a cold cache, with Defender scanning a
# freshly unzipped binary on its first start, `-version` can take well over five seconds.
BUNDLED_PROBE_TIMEOUT = 20.0

# `ffmpeg -version` answers, keyed by (normalised path, mtime_ns, size): /api/status and the
# provider's doctor() both probe, so a successful answer is shared instead of run twice per call.
_version_cache: dict[tuple[str, int, int], str] = {}
_cache_lock = threading.Lock()


class ProbeTimeout(Exception):
    """`ffmpeg -version` did not answer in time (the file may still be a working ffmpeg)."""


@dataclass
class FfmpegInfo:
    path: str | None
    version: str | None
    bundled: bool = False  # the copy shipped in the package's bin folder was chosen
    note: str | None = None  # why this copy and not the configured one, for doctor output

    @property
    def found(self) -> bool:
        return self.path is not None

    def describe(self) -> str:
        """One line for doctor output: ``<path> (8.1.1)`` or ``<path> (8.1.1, bundled)``.

        A note ("ffmpeg_path X does not exist, using the bundled copy") is appended after a
        semicolon so the configured path and the copy that is really used never disagree silently.
        """
        if not self.found:
            return "not found"
        origin = ", bundled" if self.bundled else ""
        version = self.version or "version not read"
        text = f"{self.path} ({version}{origin})"
        return f"{text}; {self.note}" if self.note else text


def _cache_key(path: str) -> tuple[str, int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (os.path.normcase(os.path.abspath(path)), st.st_mtime_ns, st.st_size)


def clear_version_cache() -> None:
    with _cache_lock:
        _version_cache.clear()


def _version_of(path: str, timeout: float = PROBE_TIMEOUT) -> str | None:
    """Parsed `ffmpeg -version` of `path`; None when it cannot run. Raises ProbeTimeout.

    Successful answers are cached per file (path, mtime, size), so the same copy is probed once
    no matter how many callers ask; failures are never cached (a Defender scan or a half-written
    file is temporary).
    """
    key = _cache_key(path)
    if key is not None:
        with _cache_lock:
            cached = _version_cache.get(key)
        if cached is not None:
            return cached
    try:
        out = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        ).stdout
    except subprocess.TimeoutExpired as exc:
        log.warning("ffmpeg -version did not answer within %.0f s: %s", timeout, path)
        raise ProbeTimeout(path) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("ffmpeg -version failed for %s: %s", path, exc)
        return None
    first = (out or "").splitlines()[0] if out else ""
    # "ffmpeg version 8.1.1-full_build-www.gyan.dev Copyright ..." -> "8.1.1-full_build-www.gyan.dev"
    parts = first.split()
    if len(parts) >= 3 and parts[0] == "ffmpeg" and parts[1] == "version":
        version: str | None = parts[2]
    else:
        version = first or None
    if version and key is not None:
        with _cache_lock:
            _version_cache[key] = version
    return version


def resolve_explicit(path: str) -> str | None:
    """The file a configured ``ffmpeg_path`` names, or None when there is no such file.

    Accepts the executable itself, the folder that holds it (what Explorer's address bar gives:
    ``<folder>/ffmpeg.exe`` is used) or a bare name looked up on PATH. ``find_ffmpeg()`` and the
    provider's ``_ffmpeg_location()`` both resolve through here, so doctor and yt-dlp always
    talk about the same copy.
    """
    if os.path.isdir(path):
        for filename in executable_names("ffmpeg"):
            candidate = os.path.join(path, filename)
            if os.path.isfile(candidate):
                return candidate
        return None
    if os.path.isfile(path):
        return path
    return shutil.which(path)


def _missing_explicit_note(explicit: str) -> str:
    if os.path.isdir(explicit):
        return f"ffmpeg_path {explicit} is a folder without {executable_names('ffmpeg')[0]}"
    return f"ffmpeg_path {explicit} does not exist"


def _bundled_ffprobe_note() -> str | None:
    """The bundled ffmpeg without its ffprobe: half a bin folder is worth a line in doctor."""
    if bundled_tool("ffprobe") is not None:
        return None
    return "bin\\ffprobe.exe is missing next to it (thumbnails/codec probing fall back to ffmpeg)"


def _join_notes(*notes: str | None) -> str | None:
    kept = [n for n in notes if n]
    return "; ".join(kept) if kept else None


def find_ffmpeg(explicit: str | None = None) -> FfmpegInfo:
    """Explicit path -> bundled bin folder -> PATH -> optional imageio-ffmpeg wheel -> not found.

    An explicit `ffmpeg_path` that does not exist or cannot run is skipped with a note, the same
    way `_ydl_opts` skips it, so doctor and the downloads always talk about the same copy.
    """
    candidates: list[str] = []
    note: str | None = None
    explicit_file: str | None = None
    if explicit:
        explicit_file = resolve_explicit(explicit)
        if explicit_file:
            candidates.append(explicit_file)
        else:
            note = _missing_explicit_note(explicit)
    bundled = bundled_tool("ffmpeg")
    if bundled:
        candidates.append(bundled)
    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append(on_path)
    try:  # optional dependency that bundles a static ffmpeg binary
        import imageio_ffmpeg  # type: ignore[import-not-found]

        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001 - absent or broken optional dep is fine
        pass

    seen: set[str] = set()
    for cand in candidates:
        resolved = shutil.which(cand) or cand
        key = os.path.normcase(os.path.abspath(resolved))
        if key in seen:  # ffmpeg_path naming the PATH copy: probe it once
            continue
        seen.add(key)
        from_bin = is_bundled("ffmpeg", resolved)
        is_explicit = cand == explicit_file
        timeout = BUNDLED_PROBE_TIMEOUT if from_bin else PROBE_TIMEOUT
        try:
            version = _version_of(resolved, timeout)
        except ProbeTimeout:
            if from_bin:
                # The shipped copy is a known-good build; a slow first start must not turn the
                # doctor line red (downloads run it through yt-dlp regardless).
                if note and not is_explicit:
                    note += ", using the bundled copy"
                return FfmpegInfo(
                    path=resolved,
                    version=None,
                    bundled=True,
                    note=_join_notes(
                        note,
                        f"-version did not answer within {timeout:.0f} s, assuming it works",
                        _bundled_ffprobe_note(),
                    ),
                )
            version = None
        if version is None:
            if is_explicit:
                note = f"ffmpeg_path {resolved} cannot be run"
            continue
        if note and not is_explicit:
            note += ", using the bundled copy" if from_bin else ", using the one on PATH"
        if from_bin:
            note = _join_notes(note, _bundled_ffprobe_note())
        return FfmpegInfo(path=resolved, version=version, bundled=from_bin, note=note)
    return FfmpegInfo(path=None, version=None)


def _expected_bundled_dir() -> os.PathLike[str] | None:
    """The packaged build's bin folder (``<app>/bin``, existing or not); None from source."""
    if not is_frozen():
        return None
    folder = bin_dir()
    if folder is None:
        app = app_dir()
        if app is None:
            return None
        folder = app / "bin"
    return folder


def _expected_bundled_path(name: str) -> str | None:
    """Where the packaged build expects ``name`` (``<app>/bin/<name>.exe``), or None from source."""
    folder = _expected_bundled_dir()
    if folder is None:
        return None
    suffix = ".exe" if sys.platform.startswith("win") else ""
    return os.path.join(folder, f"{name}{suffix}")


def missing_bundled_hint(display: str, name: str | None = None) -> str | None:
    """The message for a tool that should have shipped in bin/ but is not there; None from source.

    The no-install user cannot fix this with winget: the folder is incomplete (partial extraction,
    only the exe copied out of the zip, an antivirus quarantine), so say that instead. With a
    ``name`` the exact file is named ("expected at <bin>/ffmpeg.exe"); without one only the
    folder is (several runtimes are acceptable and the build ships one of them).
    """
    if name is not None:
        expected = _expected_bundled_path(name)
        where = f"expected at {expected}" if expected else None
    else:
        folder = _expected_bundled_dir()
        where = f"expected in {folder}" if folder is not None else None
    if where is None:
        return None
    return (
        f"{display} is missing next to UltimatePlaylist.exe ({where}). "
        "Extract the whole zip again, or restore the file if your antivirus quarantined it."
    )


def install_hint() -> str:
    bundled_hint = missing_bundled_hint("bin\\ffmpeg.exe", "ffmpeg")
    if bundled_hint:
        return bundled_hint
    if sys.platform.startswith("win"):
        return "Install ffmpeg: winget install Gyan.FFmpeg (then restart the app)"
    if sys.platform == "darwin":
        return "Install ffmpeg: brew install ffmpeg"
    return "Install ffmpeg: sudo apt install ffmpeg (or your distro's equivalent)"
