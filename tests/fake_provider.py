"""An offline provider for the test-suite.

`FakeProvider` implements the full `Provider` protocol without touching the network or ffmpeg:
`download` writes a tiny silent MP3 (generated once with ffmpeg and embedded below) and tags it
with mutagen exactly like the real providers do (including the ULTIMATE_PLAYLIST_ID TXXX frame).
"""

from __future__ import annotations

import base64
import re
import threading
import time
from pathlib import Path

from mutagen.id3 import COMM, ID3, TALB, TIT2, TPE1, TXXX, ID3NoHeaderError
from mutagen.mp3 import MP3

from ultimate_playlist.config import Settings
from ultimate_playlist.models import JobStatus, ProgressEvent, Track, TrackRef
from ultimate_playlist.providers.base import (
    DownloadCancelled,
    ProgressCallback,
    Provider,
    ProviderError,
)

TAG_ID_KEY = "ULTIMATE_PLAYLIST_ID"
POLL_INTERVAL = 0.02  # how often a delayed download looks at the cancel event

# 0.3 s of silence, 22050 Hz mono, 32 kbit/s:
#   ffmpeg -f lavfi -i anullsrc=r=22050:cl=mono -t 0.3 -b:a 32k -y tiny.mp3
TINY_MP3_B64 = """
SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjYyLjEyLjEwMQAAAAAAAAAAAAAA//NwwAAAAAAAAAAA
AEluZm8AAAAPAAAADgAABm0ALCwsLCwsLDw8PDw8PDxNTU1NTU1NXV1dXV1dXW1tbW1tbW19fX19
fX19jo6Ojo6Ojp6enp6enp6erq6urq6urr6+vr6+vr7Pz8/Pz8/P39/f39/f3+/v7+/v7+//////
////AAAAAExhdmM2Mi4yOAAAAAAAAAAAAAAAACQDaQAAAAAAAAZtL+gBwAAAAAAAAAAAAAAAAAD/
80DEAAAAA0gAAAAATEFNRTMuMTAwVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/zQsRbAAADSAAA
AABVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/zQMSkAAADSAAAAABVVVVVVVVV
VVVVVVVVVVVVVVVVTEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NCxKMAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVV
VVVVTEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVV//NAxKQAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4x
MDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVX/80LEowAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/
80DEpAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/zQsSjAAADSAAA
AABVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/zQMSkAAADSAAAAABVVVVVVVVV
VVVVVVVVVVVVVVVVTEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//NCxKMAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVV
VVVVTEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVV//NAxKQAAANIAAAAAFVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4x
MDBVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVX/80LEowAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/
80DEpAAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/zQsSjAAADSAAA
AABVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV
VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVQ==
"""
TINY_MP3: bytes = base64.b64decode("".join(TINY_MP3_B64.split()))

# 0.1 s of silence, 8000 Hz mono, in the three other containers rescan() understands. Generated
# once with `ffmpeg -f lavfi -i anullsrc=r=8000:cl=mono -t 0.1 ...` (opus: -c:a libopus -b:a 6k,
# m4a: -c:a aac -b:a 8k) and stripped of tags/padding with mutagen, so no ffmpeg is needed here.
TINY_FLAC_B64 = """
ZkxhQwAAACICQAJAAAALAAAMAfQA8AAAAyBq0A9H2K1PEgBbIrLKpI94BAAAFQ0AAABMYXZmNjIu
MTIuMTAxAAAAAIEAAAD/+CQIAMoAAADOdv/4ZAgB3+UAAADPCg==
"""
TINY_OPUS_B64 = """
T2dnUwACAAAAAAAAAABMjk36AAAAAGouguwBE09wdXNIZWFkAQE4AUAfAAAAAABPZ2dTAAAAAAAA
AAAAAEyOTfoBAAAAPk9qhwEdT3B1c1RhZ3MNAAAATGF2ZjYyLjEyLjEwMQAAAABPZ2dTAAT4EwAA
AAAAAEyOTfoCAAAAav/cQAYHBgYGBgYIC+Y7I6tgCAissw7GCAissw7GCAissw7GCAissw7GCAis
sw7G
"""
TINY_M4A_B64 = """
AAAAHGZ0eXBNNEEgAAACAE00QSBpc29taXNvMgAAAuZtb292AAAAbG12aGQAAAAAAAAAAAAAAAAA
AAPoAAAAZAABAAABAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAACLXRyYWsAAABcdGtoZAAAAAMAAAAAAAAA
AAAAAAEAAAAAAAAAZAAAAAAAAAAAAAAAAQEAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAA
AAAAAEAAAAAAAAAAAAAAAAAAACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAAGQAAAQAAAEAAAAAAaVt
ZGlhAAAAIG1kaGQAAAAAAAAAAAAAAAAAAB9AAAAHIFXEAAAAAAAtaGRscgAAAAAAAAAAc291bgAA
AAAAAAAAAAAAAFNvdW5kSGFuZGxlcgAAAAFQbWluZgAAABBzbWhkAAAAAAAAAAAAAAAkZGluZgAA
ABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAAEUc3RibAAAAGpzdHNkAAAAAAAAAAEAAABabXA0
YQAAAAAAAAABAAAAAAAAAAAAAQAQAAAAAB9AAAAAAAA2ZXNkcwAAAAADgICAJQABAASAgIAXQBUA
AAAAAB9AAAADbQWAgIAFFYhW5QAGgICAAQIAAAAgc3R0cwAAAAAAAAACAAAAAQAABAAAAAABAAAD
IAAAABxzdHNjAAAAAAAAAAEAAAABAAAAAgAAAAEAAAAcc3RzegAAAAAAAAAAAAAAAgAAABUAAAAE
AAAAFHN0Y28AAAAAAAAAAQAAAxIAAAAac2dwZAEAAAByb2xsAAAAAgAAAAH//wAAABxzYmdwAAAA
AHJvbGwAAAABAAAAAgAAAAEAAABFdWR0YQAAAD1tZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGly
YXBwbAAAAAAAAAAAAAAAAAhpbHN0AAAACGZyZWUAAAAIZnJlZQAAACFtZGF03gIATGF2YzYyLjI4
LjEwMQACMEAOARggBw==
"""
TINY_FLAC: bytes = base64.b64decode("".join(TINY_FLAC_B64.split()))
TINY_OPUS: bytes = base64.b64decode("".join(TINY_OPUS_B64.split()))
TINY_M4A: bytes = base64.b64decode("".join(TINY_M4A_B64.split()))
TINY_AUDIO: dict[str, bytes] = {
    "mp3": TINY_MP3,
    "flac": TINY_FLAC,
    "opus": TINY_OPUS,
    "m4a": TINY_M4A,
}
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

_TRACK_RE = re.compile(r"^https?://fake\.test/track/([A-Za-z0-9_.~-]+)/?$")
_PLAYLIST_RE = re.compile(r"^https?://fake\.test/playlist/(\d+)/?$")


def track_url(source_id: str) -> str:
    return f"https://fake.test/track/{source_id}"


def playlist_url(count: int) -> str:
    return f"https://fake.test/playlist/{count}"


def playlist_source_ids(count: int) -> list[str]:
    """The source ids a `playlist/<count>` link expands to, in order."""
    return [f"pl{count}-{i}" for i in range(1, count + 1)]


def write_tiny_mp3(
    path: Path,
    *,
    artist: str = "Fake Artist",
    title: str = "Track",
    album: str | None = None,
    source_url: str = "",
    track_id: str | None = None,
) -> Path:
    """Write the embedded MP3 to `path` and tag it. Reusable by library/playlist tests."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(TINY_MP3)
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall("TPE1")
    tags.delall("TIT2")
    tags.delall("TALB")
    tags.delall("COMM")
    tags.delall("TXXX")
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TIT2(encoding=3, text=[title]))
    if album:
        tags.add(TALB(encoding=3, text=[album]))
    if source_url:
        tags.add(COMM(encoding=3, lang="eng", desc="", text=[source_url]))
    if track_id:
        tags.add(TXXX(encoding=3, desc=TAG_ID_KEY, text=[track_id]))
    tags.save(path)
    return path


def write_tiny_audio(
    path: Path,
    *,
    artist: str = "Fake Artist",
    title: str = "Track",
    album: str | None = None,
    track_id: str | None = None,
    cover: bytes | None = None,
) -> Path:
    """Write a tiny silent file in the format given by `path`'s suffix and tag it.

    Covers the four containers `Library.rescan()` picks up: MP3 (ID3), M4A (MP4 atoms, PNG
    `covr`), FLAC (Vorbis comments + picture block) and Opus (Vorbis comments +
    METADATA_BLOCK_PICTURE). `cover` is embedded as PNG when given.
    """
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import APIC
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggopus import OggOpus

    path = Path(path)
    fmt = path.suffix.lower().lstrip(".")
    if fmt == "mp3":
        write_tiny_mp3(path, artist=artist, title=title, album=album, track_id=track_id)
        if cover:
            tags = ID3(path)
            tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover", data=cover))
            tags.save(path)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(TINY_AUDIO[fmt])
    if fmt == "m4a":
        mp4 = MP4(path)
        mp4["\xa9ART"] = [artist]
        mp4["\xa9nam"] = [title]
        if album:
            mp4["\xa9alb"] = [album]
        if track_id:
            mp4[f"----:com.apple.iTunes:{TAG_ID_KEY}"] = [track_id.encode("utf-8")]
        if cover:
            mp4["covr"] = [MP4Cover(cover, MP4Cover.FORMAT_PNG)]
        mp4.save()
        return path
    picture = None
    if cover:
        picture = Picture()
        picture.type = 3
        picture.mime = "image/png"
        picture.data = cover
    if fmt == "flac":
        flac = FLAC(path)
        flac["artist"] = artist
        flac["title"] = title
        if album:
            flac["album"] = album
        if track_id:
            flac[TAG_ID_KEY] = track_id
        if picture is not None:
            flac.add_picture(picture)
        flac.save()
        return path
    if fmt == "opus":
        opus = OggOpus(path)
        opus["artist"] = artist
        opus["title"] = title
        if album:
            opus["album"] = album
        if track_id:
            opus[TAG_ID_KEY] = track_id
        if picture is not None:
            opus["metadata_block_picture"] = base64.b64encode(picture.write()).decode("ascii")
        opus.save()
        return path
    raise ValueError(f"unsupported fixture format: {fmt}")


class FakeProvider:
    """Matches https://fake.test/track/<id> and https://fake.test/playlist/<n>."""

    display_name = "Fake (offline)"

    def __init__(
        self,
        delay: float = 0.0,
        fail_ids: set[str] | None = None,
        name: str = "fake",
    ) -> None:
        self.name = name
        self.delay = delay
        self.fail_ids: set[str] = set(fail_ids or ())
        self.gate: threading.Event | None = None  # when set, downloads block until it is set
        self.download_started = threading.Event()  # set whenever a download begins
        self.downloads: list[str] = []  # source ids handed to download(), in order
        self.resolved: list[str] = []  # urls handed to resolve(), in order

    # -- Provider protocol -------------------------------------------------------------------

    def matches(self, url: str) -> bool:
        url = url.strip()
        return bool(_TRACK_RE.match(url) or _PLAYLIST_RE.match(url))

    def resolve(self, url: str) -> list[TrackRef]:
        url = url.strip()
        self.resolved.append(url)
        if m := _TRACK_RE.match(url):
            return [self._ref(m.group(1))]
        if m := _PLAYLIST_RE.match(url):
            return [self._ref(sid) for sid in playlist_source_ids(int(m.group(1)))]
        raise ProviderError("The fake provider does not understand this link")

    def download(
        self,
        ref: TrackRef,
        dest_dir: Path,
        settings: Settings,
        progress: ProgressCallback,
        cancel: threading.Event,
    ) -> Track:
        self.downloads.append(ref.source_id)
        self.download_started.set()
        if ref.source_id in self.fail_ids:
            raise ProviderError(f"Fake provider refused to download '{ref.source_id}'")
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        progress(ProgressEvent(JobStatus.DOWNLOADING, progress=0.0))
        self._wait(progress, cancel)
        progress(ProgressEvent(JobStatus.CONVERTING, progress=1.0, message="Tagging"))

        artist = ref.artist or "Fake Artist"
        title = ref.title or f"Track {ref.source_id}"
        final = dest_dir / f"{artist} - {title}.mp3"
        write_tiny_mp3(
            final,
            artist=artist,
            title=title,
            album=ref.album,
            source_url=ref.url,
            track_id=ref.track_id,
        )
        if cancel.is_set():
            final.unlink(missing_ok=True)
            raise DownloadCancelled("Download cancelled")
        return Track(
            id=ref.track_id,
            provider=self.name,
            source_id=ref.source_id,
            source_url=ref.url,
            title=title,
            artist=artist,
            path=final.relative_to(dest_dir).as_posix(),
            album=ref.album,
            duration=MP3(final).info.length,
            has_cover=False,
            file_size=final.stat().st_size,
        )

    def doctor(self) -> list[tuple[bool, str, str]]:
        return [(True, "Fake provider", "offline stub, always available")]

    # -- helpers -----------------------------------------------------------------------------

    def _ref(self, source_id: str) -> TrackRef:
        return TrackRef(
            provider=self.name,
            source_id=source_id,
            url=track_url(source_id),
            title=f"Track {source_id}",
            artist="Fake Artist",
            duration=0.3,
        )

    def _wait(self, progress: ProgressCallback, cancel: threading.Event) -> None:
        """Sleep `delay` seconds (and until `gate` opens) while polling the cancel event."""
        deadline = time.monotonic() + max(0.0, self.delay)
        while True:
            if cancel.is_set():
                raise DownloadCancelled("Download cancelled")
            now = time.monotonic()
            gate_closed = self.gate is not None and not self.gate.is_set()
            if now >= deadline and not gate_closed:
                return
            if self.delay > 0:
                done = 1.0 - max(0.0, deadline - now) / self.delay
                progress(
                    ProgressEvent(
                        JobStatus.DOWNLOADING,
                        progress=min(1.0, max(0.0, done)),
                        speed=1024.0,
                        eta=max(0.0, deadline - now),
                    )
                )
            time.sleep(POLL_INTERVAL)


class FakeRegistry:
    """A stand-in for the `ultimate_playlist.providers` module holding only the given providers."""

    def __init__(self, *providers: Provider) -> None:
        self.providers: list[Provider] = list(providers)

    @staticmethod
    def normalize_url(url: str) -> str:
        url = url.strip()
        if url and "://" not in url:
            url = "https://" + url
        return url

    def get_provider(self, url: str) -> Provider | None:
        url = self.normalize_url(url)
        for provider in self.providers:
            if provider.matches(url):
                return provider
        return None

    def provider_by_name(self, name: str) -> Provider | None:
        for provider in self.providers:
            if provider.name == name:
                return provider
        return None
