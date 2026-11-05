"""Video downloads: the media / quality options, their own track ids, the yt-dlp options and the
formats yt-dlp itself picks per quality, the YouTube and TikTok pipelines, the API and the CLI."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yt_dlp
from fake_provider import FakeProvider, FakeRegistry, playlist_url, track_url, write_tiny_audio
from mutagen.mp4 import MP4

from ultimate_playlist.config import (
    DEFAULT_VIDEO_QUALITY,
    VIDEO_QUALITIES,
    Settings,
    clean_media,
    clean_video_quality,
)
from ultimate_playlist.downloader import VIDEO_ID_SUFFIX, JobManager, as_video_ref
from ultimate_playlist.library import Library
from ultimate_playlist.models import JobStatus, Track, TrackRef
from ultimate_playlist.providers import tiktok, youtube
from ultimate_playlist.providers.youtube import VIDEO_FORMAT_SORT, YouTubeProvider, _ydl_opts

WAIT = 15.0


# ----------------------------------------------------------------------------------------------
# Options
# ----------------------------------------------------------------------------------------------


def test_clean_media() -> None:
    assert clean_media(None) == "audio"
    assert clean_media(" Video ") == "video"
    with pytest.raises(ValueError, match="audio or video"):
        clean_media("gif")


def test_clean_video_quality() -> None:
    assert clean_video_quality("") == DEFAULT_VIDEO_QUALITY == "best"
    assert clean_video_quality("720p") == "720"
    assert clean_video_quality(" MAX ") == "max"
    with pytest.raises(ValueError, match="best, max, 720, 480"):
        clean_video_quality("1080")
    assert set(VIDEO_FORMAT_SORT) == set(VIDEO_QUALITIES)  # every quality has a sort order


def test_as_video_ref() -> None:
    ref = TrackRef(provider="youtube", source_id="abc", url="u", extra={"original_url": "o"})
    video = as_video_ref(ref, "720")
    assert video.track_id == f"youtube:abc{VIDEO_ID_SUFFIX}" == "youtube:abc:video"
    assert video.extra == {"original_url": "o", "media": "video", "quality": "720"}
    assert ref.track_id == "youtube:abc" and ref.extra == {"original_url": "o"}  # untouched
    assert as_video_ref(video, "").track_id == "youtube:abc:video"  # never doubled
    assert as_video_ref(ref, "").extra["quality"] == "best"


# ----------------------------------------------------------------------------------------------
# Downloader
# ----------------------------------------------------------------------------------------------


class VideoFakeProvider(FakeProvider):
    """The fake provider with video support: a video ref gives an MP4 file."""

    supports_video = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.refs: list[TrackRef] = []

    def download(self, ref, dest_dir, settings, progress, cancel):  # type: ignore[no-untyped-def]
        self.refs.append(ref)
        if ref.extra.get("media") != "video":
            return super().download(ref, dest_dir, settings, progress, cancel)
        self.downloads.append(ref.source_id)
        dest_dir = Path(dest_dir)
        name = ref.source_id.replace(":", " ")  # ":" can't be in a Windows file name
        final = write_tiny_mp4(dest_dir / f"Fake Artist - {name}.mp4", ref.track_id)
        return Track(
            id=ref.track_id,
            provider=self.name,
            source_id=ref.source_id,
            source_url=ref.url,
            title=ref.source_id,
            artist="Fake Artist",
            path=final.relative_to(dest_dir).as_posix(),
        )


def write_tiny_mp4(path: Path, track_id: str) -> Path:
    """A tiny MP4 container (mutagen tags it like any MP4) under a video extension."""
    m4a = path.with_suffix(".m4a")
    write_tiny_audio(m4a, artist="Fake Artist", title=path.stem, track_id=track_id)
    m4a.replace(path)
    return path


@pytest.fixture
def make_jobs(
    tmp_settings: Settings, library: Library
) -> Iterator[Callable[..., tuple[JobManager, Any]]]:
    managers: list[JobManager] = []

    def factory(provider: Any = None) -> tuple[JobManager, Any]:
        fake = provider or VideoFakeProvider()
        manager = JobManager(replace(tmp_settings), library, providers=FakeRegistry(fake))
        manager.start()
        managers.append(manager)
        return manager, fake

    yield factory
    for manager in managers:
        manager.stop()


def test_a_video_job_downloads_a_video_ref(make_jobs: Any, library: Library) -> None:
    manager, fake = make_jobs()
    job = manager.submit(track_url("v1"), media="video", quality="720p")
    assert (job.media, job.quality) == ("video", "720")
    assert manager.wait_idle(WAIT)
    assert job.status == JobStatus.DONE
    (ref,) = fake.refs
    assert ref.track_id == "fake:v1:video"
    assert ref.extra["media"] == "video" and ref.extra["quality"] == "720"
    assert library.get("fake:v1:video").path == "Fake Artist - v1 video.mp4"


def test_the_audio_and_the_video_of_a_track_are_two_items(make_jobs: Any, library: Library) -> None:
    manager, _ = make_jobs()
    audio = manager.submit(track_url("both"))
    assert manager.wait_idle(WAIT)
    video = manager.submit(track_url("both"), media="video")
    assert manager.wait_idle(WAIT)
    assert audio.status == video.status == JobStatus.DONE  # the video is not "already there"
    assert library.has("fake:both") and library.has("fake:both:video")
    again = manager.submit(track_url("both"), media="video")
    assert manager.wait_idle(WAIT)
    assert again.status == JobStatus.SKIPPED and again.message == "Already in library"


def test_quality_is_ignored_for_audio(make_jobs: Any) -> None:
    manager, fake = make_jobs()
    job = manager.submit(track_url("a1"), media="audio", quality="max")
    assert job.quality == ""
    assert manager.wait_idle(WAIT)
    assert fake.refs[0].track_id == "fake:a1" and "media" not in fake.refs[0].extra


def test_a_playlist_of_videos_and_retry_keep_the_options(make_jobs: Any) -> None:
    manager, fake = make_jobs()
    parent = manager.submit(playlist_url(2), media="video", quality="480", folder="Flight")
    assert manager.wait_idle(WAIT)
    kids = [j for j in manager.list() if j.parent_id == parent.id]
    assert {(k.media, k.quality, k.folder) for k in kids} == {("video", "480", "Flight")}
    assert {r.track_id for r in fake.refs} == {"fake:pl2-1:video", "fake:pl2-2:video"}
    kid = kids[0]
    kid.status = JobStatus.ERROR  # pretend it failed
    again = manager.retry(kid.id)
    assert again is not None and (again.media, again.quality, again.folder) == (
        "video",
        "480",
        "Flight",
    )


def test_a_provider_without_video_saves_audio(make_jobs: Any, library: Library) -> None:
    """Spotify: its songs come from YouTube Music's audio, so Video means audio there."""
    manager, fake = make_jobs(FakeProvider())
    single = manager.submit(track_url("s1"), media="video")
    assert manager.wait_idle(WAIT)
    assert single.status == JobStatus.DONE and single.media == "audio"
    assert library.has("fake:s1") and not library.has("fake:s1:video")
    parent = manager.submit(playlist_url(2), media="video")
    assert manager.wait_idle(WAIT)
    assert parent.message.endswith("Fake (offline) links are saved as audio")
    kids = [j for j in manager.list() if j.parent_id == parent.id]
    assert {k.media for k in kids} == {"audio"}


def test_submit_refuses_bad_options(make_jobs: Any) -> None:
    manager, fake = make_jobs()
    with pytest.raises(ValueError, match="audio or video"):
        manager.submit(track_url("x"), media="gif")
    with pytest.raises(ValueError, match="Video quality"):
        manager.submit(track_url("x"), media="video", quality="8k")
    assert fake.resolved == []


# ----------------------------------------------------------------------------------------------
# yt-dlp options and the formats yt-dlp picks
# ----------------------------------------------------------------------------------------------


def test_video_ydl_opts(tmp_path: Path) -> None:
    settings = Settings(library_dir=tmp_path)
    opts = _ydl_opts(settings, tmp_path, lambda e: None, threading.Event(), "bv*+ba/b", "720")
    assert opts["format"] == "bv*+ba/b"
    assert opts["format_sort"] == ["res:720", "vcodec:h264", "acodec:aac"]
    assert opts["merge_output_format"] == "mp4"
    keys = [pp["key"] for pp in opts["postprocessors"]]
    assert keys[0] == "FFmpegVideoRemuxer" and "FFmpegExtractAudio" not in keys
    assert opts["postprocessors"][0]["preferedformat"] == "mp4"
    audio = _ydl_opts(settings, tmp_path, lambda e: None, threading.Event())
    assert "format_sort" not in audio and "merge_output_format" not in audio  # audio unchanged
    assert audio["postprocessors"][0]["key"] == "FFmpegExtractAudio"


# A YouTube-shaped format list: separate video (H.264 at 480/720/1080, AV1 and VP9 at 4K) and
# audio (AAC and Opus) streams, plus one combined low-resolution file.
YOUTUBE_FORMATS: list[dict[str, Any]] = [
    {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129},
    {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 160},
    {
        "format_id": "135",
        "ext": "mp4",
        "vcodec": "avc1.4d401e",
        "acodec": "none",
        "width": 854,
        "height": 480,
        "tbr": 1000,
        "fps": 30,
    },
    {
        "format_id": "136",
        "ext": "mp4",
        "vcodec": "avc1.4d401f",
        "acodec": "none",
        "width": 1280,
        "height": 720,
        "tbr": 2000,
        "fps": 30,
    },
    {
        "format_id": "137",
        "ext": "mp4",
        "vcodec": "avc1.640028",
        "acodec": "none",
        "width": 1920,
        "height": 1080,
        "tbr": 4000,
        "fps": 30,
    },
    {
        "format_id": "313",
        "ext": "webm",
        "vcodec": "vp9",
        "acodec": "none",
        "width": 3840,
        "height": 2160,
        "tbr": 15000,
        "fps": 30,
    },
    {
        "format_id": "401",
        "ext": "mp4",
        "vcodec": "av01.0.12M.08",
        "acodec": "none",
        "width": 3840,
        "height": 2160,
        "tbr": 12000,
        "fps": 30,
    },
    {
        "format_id": "18",
        "ext": "mp4",
        "vcodec": "avc1.42001E",
        "acodec": "mp4a.40.2",
        "width": 640,
        "height": 360,
        "tbr": 500,
    },
]


def _picked(formats: list[dict[str, Any]], selector: str, quality: str) -> dict[str, Any]:
    """The format(s) yt-dlp itself selects with our options: its sorting, its id handling and
    its selection all run; nothing is downloaded or probed."""
    info = {
        "id": "x",
        "title": "t",
        "extractor": "Youtube",
        "extractor_key": "Youtube",
        "webpage_url": "https://www.youtube.com/watch?v=x",
        "formats": [{"url": f"https://media.example/{f['format_id']}", **f} for f in formats],
    }
    opts = {
        "quiet": True,
        "simulate": True,
        "check_formats": False,
        "format": selector,
        "format_sort": VIDEO_FORMAT_SORT[quality],
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.process_ie_result(info, download=False)


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        ("best", "137+140"),  # 1080p H.264 with AAC: plays everywhere
        ("720", "136+140"),
        ("480", "135+140"),
    ],
)
def test_youtube_quality_picks(quality: str, expected: str) -> None:
    assert (
        _picked(YOUTUBE_FORMATS, YouTubeProvider.ydl_video_format, quality)["format_id"] == expected
    )


def test_youtube_max_picks_the_highest_resolution() -> None:
    picked = _picked(YOUTUBE_FORMATS, YouTubeProvider.ydl_video_format, "max")
    video = next(f for f in picked["requested_formats"] if f["vcodec"] != "none")
    assert video["height"] == 2160 and not video["vcodec"].startswith("avc1")


# TikTok's web formats: video and audio in one file, H.264 and H.265, the watermarked copy, and
# the separate sound.
TIKTOK_FORMATS: list[dict[str, Any]] = [
    {
        "format_id": "h264_540p_815015-0",
        "ext": "mp4",
        "vcodec": "h264",
        "acodec": "aac",
        "width": 576,
        "height": 1024,
        "tbr": 815,
    },
    {
        "format_id": "h264_720p_1046199-0",
        "ext": "mp4",
        "vcodec": "h264",
        "acodec": "aac",
        "width": 720,
        "height": 1280,
        "tbr": 1046,
    },
    {
        "format_id": "bytevc1_1080p_1181959-0",
        "ext": "mp4",
        "vcodec": "h265",
        "acodec": "aac",
        "width": 1080,
        "height": 1920,
        "tbr": 1181,
    },
    {
        "format_id": "download",
        "ext": "mp4",
        "vcodec": "h264",
        "acodec": "aac",
        "format_note": "watermarked",
        "preference": -2,
    },
    {"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": "aac"},
]


def test_tiktok_video_best_is_h264_without_the_watermark() -> None:
    picked = _picked(TIKTOK_FORMATS, tiktok.YDL_VIDEO_FORMAT, "best")
    assert picked["format_id"] == "h264_720p_1046199-0"
    picked = _picked(TIKTOK_FORMATS, tiktok.YDL_VIDEO_FORMAT, "max")
    assert picked["format_id"] == "bytevc1_1080p_1181959-0"


def test_tiktok_video_of_a_photo_slideshow_has_no_format() -> None:
    # A real download wraps it in DownloadError; called directly, yt-dlp raises ExtractorError.
    with pytest.raises(yt_dlp.utils.YoutubeDLError, match="Requested format is not available"):
        _picked([TIKTOK_FORMATS[-1]], tiktok.YDL_VIDEO_FORMAT, "best")
    assert "download it as Audio" in tiktok.friendly_error("Requested format is not available")


# ----------------------------------------------------------------------------------------------
# The YouTube pipeline in video mode (fake yt-dlp)
# ----------------------------------------------------------------------------------------------


class FakeYoutubeDL:
    instances: list[FakeYoutubeDL] = []
    info: dict[str, Any] = {}

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        FakeYoutubeDL.instances.append(self)

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> Any:
        info = FakeYoutubeDL.info
        out = Path(self.opts["outtmpl"] % {"id": info["id"], "ext": "mp4"})
        write_tiny_mp4(out, "ignored")
        for hook in self.opts["postprocessor_hooks"]:
            hook({"status": "started", "postprocessor": "Merger"})
        return info


def test_youtube_download_in_video_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeYoutubeDL.instances = []
    FakeYoutubeDL.info = {"id": "jNQXAC9IVRw", "title": "Me at the zoo", "channel": "jawed"}
    monkeypatch.setattr(youtube, "YoutubeDL", FakeYoutubeDL)
    settings = Settings(library_dir=tmp_path / "lib")
    ref = as_video_ref(
        TrackRef(provider="youtube", source_id="jNQXAC9IVRw", url="https://youtu.be/jNQXAC9IVRw"),
        "max",
    )
    events: list[Any] = []
    track = YouTubeProvider().download(
        ref, settings.library_dir, settings, events.append, threading.Event()
    )

    assert track.id == "youtube:jNQXAC9IVRw:video"
    assert track.path == "jawed - Me at the zoo.mp4"
    assert track.source_id == "jNQXAC9IVRw"  # the real video id, for links
    tags = MP4(str(settings.library_dir / track.path)).tags
    assert tags["\xa9ART"] == ["jawed"] and tags["\xa9nam"] == ["Me at the zoo"]
    stored = bytes(tags["----:com.apple.iTunes:ULTIMATE_PLAYLIST_ID"][0]).decode()
    assert stored == "youtube:jNQXAC9IVRw:video"  # a rescan finds the video under its own id
    (ydl,) = FakeYoutubeDL.instances
    assert ydl.opts["format"] == "bv*+ba/b" and ydl.opts["format_sort"] == VIDEO_FORMAT_SORT["max"]
    assert any(e.message == "Merging video and audio" for e in events)
    assert list((settings.library_dir / ".incoming").iterdir()) == []


# ----------------------------------------------------------------------------------------------
# API and CLI
# ----------------------------------------------------------------------------------------------


def test_api_jobs_take_media_and_quality(client: Any) -> None:
    response = client.post(
        "/api/jobs", json={"url": track_url("api-v"), "media": "video", "quality": "480"}
    )
    assert response.status_code == 202
    job = response.json()["jobs"][0]
    assert (job["media"], job["quality"], job["folder"]) == ("video", "480", "")
    assert client.post("/api/jobs", json={"url": track_url("x"), "media": "gif"}).status_code == 400
    bad = client.post("/api/jobs", json={"url": track_url("x"), "media": "video", "quality": "8k"})
    assert bad.status_code == 400 and "Video quality" in bad.json()["detail"]


def test_api_serves_videos_as_video(client: Any, library: Library) -> None:
    path = write_tiny_mp4(library.library_dir / "Flight" / "a.mp4", "fake:a:video")
    library.add(
        Track(
            id="fake:a:video",
            provider="fake",
            source_id="a:video",
            source_url="",
            title="a",
            artist="A",
            path="Flight/a.mp4",
        )
    )
    assert path.is_file()
    (track,) = client.get("/api/library").json()["tracks"]
    assert (track["media"], track["folder"]) == ("video", "Flight")
    response = client.get("/media/fake:a:video")
    assert response.status_code == 200 and response.headers["content-type"] == "video/mp4"


def test_cli_add_video(monkeypatch: pytest.MonkeyPatch, tmp_settings: Settings) -> None:
    from ultimate_playlist import cli

    calls: list[dict[str, Any]] = []

    def submit(self: JobManager, url: str, **options: Any) -> Any:
        calls.append({"url": url, **options})
        raise ValueError("stop here")

    monkeypatch.setattr(JobManager, "submit", submit)
    tmp_settings.save()
    cli.main(["add", "--video", "--quality", "720", "--folder", "Flight", "https://x.test/a"])
    cli.main(["add", "https://x.test/b"])
    assert calls == [
        {"url": "https://x.test/a", "media": "video", "quality": "720", "folder": "Flight"},
        {"url": "https://x.test/b", "media": "audio", "quality": "", "folder": ""},
    ]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["add", "--quality", "8k", "u"])


# ----------------------------------------------------------------------------------------------
# Review fixes: staging, progress, the 4K cap, notes, CLI
# ----------------------------------------------------------------------------------------------


def test_youtube_max_never_picks_8k() -> None:
    eight_k = {
        "format_id": "571",
        "ext": "mp4",
        "vcodec": "av01.0.17M.08",
        "acodec": "none",
        "width": 7680,
        "height": 4320,
        "tbr": 40000,
        "fps": 30,
    }
    picked = _picked([*YOUTUBE_FORMATS, eight_k], YouTubeProvider.ydl_video_format, "max")
    video = next(f for f in picked["requested_formats"] if f["vcodec"] != "none")
    assert video["height"] == 2160  # "Maximum (up to 4K)" means it


class FailingYoutubeDL(FakeYoutubeDL):
    """Writes the partial streams a real merge download leaves, then fails; with `cancel` set
    it is the user's Cancel that stops it (the provider then raises DownloadCancelled)."""

    cancel: threading.Event | None = None

    def extract_info(self, url: str, download: bool = True) -> Any:
        staging = Path(self.opts["outtmpl"]).parent
        (staging / "abc.f137.mp4.part").write_bytes(b"partial video")
        (staging / "abc.f140.m4a").write_bytes(b"finished audio stream")
        if FailingYoutubeDL.cancel is not None:
            FailingYoutubeDL.cancel.set()
        raise yt_dlp.utils.DownloadError("ERROR: unable to download video data: HTTP Error 403")


@pytest.mark.parametrize("cancelled", [False, True])
def test_a_failed_or_cancelled_video_download_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    from ultimate_playlist.providers.base import DownloadCancelled, ProviderError

    FakeYoutubeDL.instances = []
    cancel = threading.Event()
    FailingYoutubeDL.cancel = cancel if cancelled else None
    monkeypatch.setattr(youtube, "YoutubeDL", FailingYoutubeDL)
    settings = Settings(library_dir=tmp_path / "lib")
    plain = TrackRef(provider="youtube", source_id="abc", url="https://youtu.be/abc")
    expected = DownloadCancelled if cancelled else ProviderError
    with pytest.raises(expected):
        YouTubeProvider().download(
            as_video_ref(plain, "best"), settings.library_dir, settings, lambda e: None, cancel
        )
    assert list((settings.library_dir / ".incoming").iterdir()) == []


def test_two_downloads_of_one_id_work_in_separate_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MP3 and the MP4 of one video may download at the same time; yt-dlp names files
    after the video id, so each download needs a folder of its own."""
    FakeYoutubeDL.instances = []
    FakeYoutubeDL.info = {"id": "abc", "title": "T", "channel": "C"}
    monkeypatch.setattr(youtube, "YoutubeDL", FakeYoutubeDL)
    settings = Settings(library_dir=tmp_path / "lib")
    plain = TrackRef(provider="youtube", source_id="abc", url="https://youtu.be/abc")
    for ref in (as_video_ref(plain, "best"), as_video_ref(plain, "480")):
        try:
            YouTubeProvider().download(
                ref, settings.library_dir, settings, lambda e: None, threading.Event()
            )
        except Exception:  # noqa: BLE001 - only the staging folders matter here
            pass
    first, second = (Path(y.opts["outtmpl"]).parent for y in FakeYoutubeDL.instances)
    assert first != second and first.parent == second.parent == settings.library_dir / ".incoming"


def test_video_progress_names_the_stream(tmp_path: Path) -> None:
    events: list[Any] = []
    opts = _ydl_opts(
        Settings(library_dir=tmp_path),
        tmp_path,
        events.append,
        threading.Event(),
        "bv*+ba/b",
        "best",
    )
    hook = opts["progress_hooks"][0]
    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 1,
            "total_bytes": 2,
            "info_dict": {"vcodec": "avc1"},
        }
    )
    hook({"status": "finished"})
    hook(
        {
            "status": "downloading",
            "downloaded_bytes": 1,
            "total_bytes": 2,
            "info_dict": {"vcodec": "none"},
        }
    )
    assert [e.message for e in events] == [
        "Downloading video",
        "Saving as MP4",
        "Downloading audio",
    ]
    audio_events: list[Any] = []
    audio = _ydl_opts(
        Settings(library_dir=tmp_path), tmp_path, audio_events.append, threading.Event()
    )
    audio["progress_hooks"][0]({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})
    assert audio_events[0].message is None  # audio downloads are unchanged


def test_a_single_link_keeps_its_saved_as_audio_note(make_jobs: Any) -> None:
    manager, _ = make_jobs(FakeProvider())
    job = manager.submit(track_url("note"), media="video")
    assert manager.wait_idle(WAIT)
    assert job.status == JobStatus.DONE and job.message == "Fake (offline) links are saved as audio"


def test_cli_quality_implies_video(monkeypatch: pytest.MonkeyPatch, tmp_settings: Settings) -> None:
    from ultimate_playlist import cli

    calls: list[dict[str, Any]] = []

    def submit(self: JobManager, url: str, **options: Any) -> Any:
        calls.append(options)
        raise ValueError("stop here")

    monkeypatch.setattr(JobManager, "submit", submit)
    tmp_settings.save()
    cli.main(["add", "--quality", "720p", "https://x.test/a"])
    assert calls == [{"media": "video", "quality": "720", "folder": ""}]
