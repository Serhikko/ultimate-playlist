"""Locate ffmpeg. yt-dlp needs it to convert to MP3 and embed cover art."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class FfmpegInfo:
    path: str | None
    version: str | None

    @property
    def found(self) -> bool:
        return self.path is not None


def _version_of(path: str) -> str | None:
    try:
        out = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("ffmpeg -version failed for %s: %s", path, exc)
        return None
    first = (out or "").splitlines()[0] if out else ""
    # "ffmpeg version 8.1.1-full_build-www.gyan.dev Copyright ..." -> "8.1.1-full_build-www.gyan.dev"
    parts = first.split()
    if len(parts) >= 3 and parts[0] == "ffmpeg" and parts[1] == "version":
        return parts[2]
    return first or None


def find_ffmpeg(explicit: str | None = None) -> FfmpegInfo:
    """Explicit path -> PATH -> optional imageio-ffmpeg wheel -> not found."""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    on_path = shutil.which("ffmpeg")
    if on_path:
        candidates.append(on_path)
    try:  # optional dependency that bundles a static ffmpeg binary
        import imageio_ffmpeg  # type: ignore[import-not-found]

        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001 - absent or broken optional dep is fine
        pass

    for cand in candidates:
        resolved = shutil.which(cand) or cand
        version = _version_of(resolved)
        if version is not None:
            return FfmpegInfo(path=resolved, version=version)
    return FfmpegInfo(path=None, version=None)


def install_hint() -> str:
    if sys.platform.startswith("win"):
        return "Install ffmpeg: winget install Gyan.FFmpeg (then restart the app)"
    if sys.platform == "darwin":
        return "Install ffmpeg: brew install ffmpeg"
    return "Install ffmpeg: sudo apt install ffmpeg (or your distro's equivalent)"
