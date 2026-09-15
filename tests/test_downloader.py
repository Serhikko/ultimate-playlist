"""JobManager flows with the offline FakeProvider. No sleeps: everything waits on events."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fake_provider import FakeProvider, FakeRegistry, playlist_source_ids, playlist_url, track_url

from ultimate_playlist import downloader
from ultimate_playlist.config import Settings
from ultimate_playlist.downloader import PROGRESS_INTERVAL, JobManager
from ultimate_playlist.library import Library
from ultimate_playlist.models import Job, JobStatus, ProgressEvent, TrackRef
from ultimate_playlist.providers.base import DownloadCancelled

WAIT = 15.0  # generous upper bound; every wait returns as soon as the condition holds


@pytest.fixture
def make_jobs(
    tmp_settings: Settings, library: Library
) -> Iterator[Callable[..., tuple[JobManager, FakeProvider]]]:
    """Factory: a started JobManager wired to a fresh FakeProvider via an injected registry."""
    managers: list[JobManager] = []

    def factory(
        *, concurrency: int = 1, delay: float = 0.0, fail_ids: set[str] | None = None
    ) -> tuple[JobManager, FakeProvider]:
        fake = FakeProvider(delay=delay, fail_ids=fail_ids)
        settings = replace(tmp_settings, concurrency=concurrency)
        manager = JobManager(settings, library, providers=FakeRegistry(fake))
        manager.start()
        managers.append(manager)
        return manager, fake

    yield factory
    for manager in managers:
        manager.stop()


def children_of(manager: JobManager, parent: Job) -> list[Job]:
    """Children in creation (= playlist) order; list() is newest first."""
    return [j for j in reversed(manager.list()) if j.parent_id == parent.id]


def wait_until(predicate: Callable[[], bool], timeout: float = WAIT) -> bool:
    """Poll a condition in 10 ms steps (returns as soon as it holds; never a fixed sleep)."""
    pause = threading.Event()
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            return False
        pause.wait(0.01)
    return True


# -- submit --------------------------------------------------------------------------------


def test_submit_unsupported_url_raises_value_error(make_jobs) -> None:
    manager, _ = make_jobs()
    with pytest.raises(ValueError, match="No provider for this link"):
        manager.submit("https://example.com/whatever")
    with pytest.raises(ValueError):
        manager.submit("   ")
    assert manager.list() == []


def test_submit_creates_a_queued_job(make_jobs) -> None:
    manager, _ = make_jobs()
    job = manager.submit(f"  {track_url('a')}  ")
    assert job.url == track_url("a")
    assert job.provider == "fake"
    assert manager.get(job.id) is job
    assert manager.wait_idle(WAIT)


# -- single track ----------------------------------------------------------------------------


def test_single_track_flow(make_jobs, library: Library) -> None:
    manager, fake = make_jobs()

    job = manager.submit(track_url("abc"))
    assert manager.wait_idle(WAIT)

    assert job.status is JobStatus.DONE
    assert job.progress == 1.0
    assert job.error is None
    assert job.child_count == 0 and job.parent_id is None
    assert job.track_ref is not None and job.track_ref.track_id == "fake:abc"
    assert job.track is not None
    assert job.track.id == "fake:abc"
    assert job.track.path == "Fake Artist - Track abc.mp3"
    assert job.title == "Fake Artist - Track abc"
    assert fake.resolved == [track_url("abc")]
    assert fake.downloads == ["abc"]
    assert library.has("fake:abc")
    assert (library.library_dir / "Fake Artist - Track abc.mp3").is_file()
    assert manager.list() == [job]
    assert manager.list(include_finished=False) == []


def test_progress_is_copied_onto_the_job(make_jobs) -> None:
    manager, fake = make_jobs(delay=0.4)
    fake.gate = threading.Event()

    job = manager.submit(track_url("p"))
    assert fake.download_started.wait(WAIT)
    # the first DOWNLOADING event is a status change (applied at once); later ones carry speed/eta
    assert wait_until(lambda: job.status is JobStatus.DOWNLOADING and job.speed == 1024.0)
    assert 0.0 <= job.progress <= 1.0
    assert job.eta is not None

    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert job.status is JobStatus.DONE
    assert job.speed is None and job.eta is None


def test_progress_updates_are_throttled_but_status_changes_are_not(
    tmp_settings: Settings, library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """~4 copies/s: same-status events inside the window are dropped, status/message pass."""
    clock = {"now": 100.0}
    # Patch the downloader's view of `time` only: freezing the stdlib clock itself would stall
    # queue timeouts / Condition waits in any thread still alive.
    monkeypatch.setattr(
        downloader, "time", SimpleNamespace(monotonic=lambda: clock["now"], sleep=time.sleep)
    )
    manager = JobManager(tmp_settings, library, providers=FakeRegistry(FakeProvider()))
    job = manager.submit(track_url("t"))
    job.track_ref = TrackRef(provider="fake", source_id="t", url=track_url("t"))
    job.status = JobStatus.DOWNLOADING
    cb = manager._progress_callback(job.id)

    cb(ProgressEvent(JobStatus.DOWNLOADING, progress=0.1, speed=10.0, eta=9.0))
    assert (job.progress, job.speed, job.eta) == (0.1, 10.0, 9.0)
    clock["now"] += PROGRESS_INTERVAL / 2
    cb(ProgressEvent(JobStatus.DOWNLOADING, progress=0.5, speed=20.0, eta=5.0))
    assert (job.progress, job.speed) == (0.1, 10.0)  # inside the window: dropped
    clock["now"] += PROGRESS_INTERVAL  # window over
    cb(ProgressEvent(JobStatus.DOWNLOADING, progress=0.6, speed=30.0, eta=4.0))
    assert (job.progress, job.speed, job.eta) == (0.6, 30.0, 4.0)
    clock["now"] += 0.01
    cb(ProgressEvent(JobStatus.CONVERTING, progress=1.0, message="Converting to MP3"))
    assert job.status is JobStatus.CONVERTING and job.progress == 1.0  # status change: at once
    assert job.message == "Converting to MP3"
    clock["now"] += 0.01
    cb(ProgressEvent(JobStatus.CONVERTING, progress=1.0, message="Writing tags"))
    assert job.message == "Writing tags"  # a new message passes too
    clock["now"] += 0.01
    cb(ProgressEvent(JobStatus.CONVERTING, progress=0.5, message="Writing tags"))
    assert job.progress == 1.0  # same status, same message, inside the window: dropped
    cb(ProgressEvent(JobStatus.DONE, progress=1.0))  # providers do not decide DONE
    assert job.status is JobStatus.CONVERTING
    manager.stop()


# -- playlists -------------------------------------------------------------------------------


def test_parent_names_the_container_when_the_provider_gives_one(make_jobs) -> None:
    """A Spotify album must not show up in the queue as "Playlist: 13 tracks"."""
    manager, fake = make_jobs(concurrency=1)
    original = fake.resolve

    def resolve_with_container(url: str) -> list[TrackRef]:
        return [
            replace(r, extra={**r.extra, "container": "Album: Test Album"}) for r in original(url)
        ]

    fake.resolve = resolve_with_container  # type: ignore[method-assign]
    parent = manager.submit(playlist_url(3))
    assert manager.wait_idle(WAIT)
    assert parent.message == "Album: Test Album (3 tracks)"
    assert parent.child_count == 3


def test_playlist_expands_into_children_in_order(make_jobs, library: Library) -> None:
    manager, fake = make_jobs(concurrency=1)

    parent = manager.submit(playlist_url(3))
    assert manager.wait_idle(WAIT)

    assert parent.status is JobStatus.DONE
    assert parent.child_count == 3
    assert parent.message == "Playlist: 3 tracks"
    assert parent.track_ref is None and parent.track is None
    assert parent.parent_id is None

    children = children_of(manager, parent)
    assert len(children) == 3
    expected_ids = playlist_source_ids(3)
    assert [c.track_ref.source_id for c in children] == expected_ids
    for child, sid in zip(children, expected_ids, strict=True):
        assert child.status is JobStatus.DONE
        assert child.provider == "fake"
        assert child.url == track_url(sid)
        assert child.track is not None and child.track.id == f"fake:{sid}"
        assert child.child_count == 0
    # with one worker the downloads happen in playlist order
    assert fake.downloads == expected_ids
    assert {t.id for t in library.all()} == {f"fake:{sid}" for sid in expected_ids}
    assert len(manager.list()) == 4


def test_playlist_with_two_workers_still_finishes_everything(make_jobs, library: Library) -> None:
    manager, fake = make_jobs(concurrency=2)
    parent = manager.submit(playlist_url(5))
    assert manager.wait_idle(WAIT)
    assert all(c.status is JobStatus.DONE for c in children_of(manager, parent))
    assert sorted(fake.downloads) == sorted(playlist_source_ids(5))
    assert len(library) == 5


def test_empty_playlist_is_an_error(make_jobs) -> None:
    manager, _ = make_jobs()
    job = manager.submit(playlist_url(0))
    assert manager.wait_idle(WAIT)
    assert job.status is JobStatus.ERROR
    assert job.error == "Nothing to download at that link"
    assert job.child_count == 0


# -- skip ------------------------------------------------------------------------------------


def test_track_already_in_library_is_skipped(make_jobs, library: Library) -> None:
    manager, fake = make_jobs()
    first = manager.submit(track_url("dup"))
    assert manager.wait_idle(WAIT)
    assert first.status is JobStatus.DONE

    second = manager.submit(track_url("dup"))
    assert manager.wait_idle(WAIT)

    assert second.status is JobStatus.SKIPPED
    assert second.message == "Already in library"
    assert second.track is None
    assert fake.downloads == ["dup"]  # downloaded exactly once


def test_skip_only_if_the_file_still_exists(make_jobs, library: Library) -> None:
    manager, fake = make_jobs()
    first = manager.submit(track_url("again"))
    assert manager.wait_idle(WAIT)
    library.abs_path(first.track).unlink()

    second = manager.submit(track_url("again"))
    assert manager.wait_idle(WAIT)

    assert second.status is JobStatus.DONE
    assert fake.downloads == ["again", "again"]


def test_same_track_submitted_twice_is_downloaded_once(make_jobs, library: Library) -> None:
    """Two workers must never download the same track at the same time (shared .incoming)."""
    manager, fake = make_jobs(concurrency=2)
    fake.gate = threading.Event()  # the first download stays open until we say so
    first = manager.submit(track_url("dup"))
    second = manager.submit(track_url("dup"))
    assert wait_until(lambda: second.status.is_terminal)
    assert second.status is JobStatus.SKIPPED
    assert second.message == "Already in the queue"

    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert first.status is JobStatus.DONE
    assert second.status is JobStatus.SKIPPED  # the track landed: the skip stays right
    assert second.track is None
    assert fake.downloads == ["dup"]
    assert library.has("fake:dup")


def test_duplicate_gets_its_turn_when_the_download_it_waited_on_is_cancelled(
    make_jobs, library: Library
) -> None:
    """A job skipped as 'Already in the queue' must not be a dead end when the other job dies."""
    manager, fake = make_jobs(concurrency=1)
    fake.gate = threading.Event()
    first = manager.submit(track_url("dup"))
    assert fake.download_started.wait(WAIT)
    second = manager.submit(track_url("dup"))
    assert wait_until(lambda: second.status is JobStatus.SKIPPED)
    assert second.message == "Already in the queue"

    assert manager.cancel(first.id)
    assert wait_until(lambda: first.status is JobStatus.CANCELLED)
    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert second.status is JobStatus.DONE, (second.status, second.message, second.error)
    assert second.track is not None and second.track.id == "fake:dup"
    assert fake.downloads == ["dup", "dup"]
    assert library.has("fake:dup")


def test_duplicate_gets_its_turn_when_the_download_it_waited_on_fails(
    make_jobs, library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, fake = make_jobs(concurrency=1)
    from ultimate_playlist.providers.base import ProviderError

    release = threading.Event()
    real_download = fake.download

    def flaky(ref, dest_dir, settings, progress, cancel):
        fake.downloads.append(ref.source_id)
        fake.download_started.set()
        release.wait(WAIT)  # a slow failure: long enough for the duplicate to be submitted
        monkeypatch.setattr(fake, "download", real_download)  # the retry succeeds
        raise ProviderError("network hiccup")

    monkeypatch.setattr(fake, "download", flaky)
    first = manager.submit(track_url("flaky"))
    assert fake.download_started.wait(WAIT)
    second = manager.submit(track_url("flaky"))
    assert wait_until(lambda: second.status is JobStatus.SKIPPED)

    release.set()
    assert manager.wait_idle(WAIT)
    assert first.status is JobStatus.ERROR and first.error == "network hiccup"
    assert second.status is JobStatus.DONE
    assert fake.downloads == ["flaky", "flaky"]
    assert library.has("fake:flaky")


def test_duplicate_behind_a_queued_job_that_is_cancelled_is_revived(
    make_jobs, library: Library
) -> None:
    manager, fake = make_jobs(concurrency=1)
    fake.gate = threading.Event()
    busy = manager.submit(track_url("busy"))
    assert fake.download_started.wait(WAIT)
    queued = manager.submit(track_url("later"))  # resolved, waiting for the single worker
    assert wait_until(lambda: queued.track_ref is not None and queued.status is JobStatus.QUEUED)
    duplicate = manager.submit(track_url("later"))
    assert wait_until(lambda: duplicate.status is JobStatus.SKIPPED)

    assert manager.cancel(queued.id)  # cancelled while still queued: never reaches a worker
    assert wait_until(lambda: duplicate.status is JobStatus.QUEUED)  # revived at once
    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert busy.status is JobStatus.DONE
    assert queued.status is JobStatus.CANCELLED
    assert duplicate.status is JobStatus.DONE
    assert fake.downloads == ["busy", "later"]


def test_playlist_child_waiting_on_a_cancelled_single_is_revived(make_jobs) -> None:
    manager, fake = make_jobs(concurrency=1)
    fake.gate = threading.Event()
    single = manager.submit(track_url("pl2-1"))
    assert fake.download_started.wait(WAIT)
    parent = manager.submit(playlist_url(2))  # pl2-1 (busy) and pl2-2
    assert wait_until(lambda: parent.child_count == 2)
    children = children_of(manager, parent)
    assert wait_until(lambda: children[0].status is JobStatus.SKIPPED)

    assert manager.cancel(single.id)
    assert wait_until(lambda: single.status is JobStatus.CANCELLED)
    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert all(c.status is JobStatus.DONE for c in children), [c.status for c in children]
    assert sorted(fake.downloads) == ["pl2-1", "pl2-1", "pl2-2"]


def test_stop_marks_revived_duplicates_as_stopped(make_jobs) -> None:
    manager, fake = make_jobs(concurrency=1, delay=30.0)
    first = manager.submit(track_url("dup"))
    assert fake.download_started.wait(WAIT)
    second = manager.submit(track_url("dup"))
    assert wait_until(lambda: second.status is JobStatus.SKIPPED)

    manager.stop()
    assert first.status is JobStatus.CANCELLED
    assert second.status is JobStatus.CANCELLED and second.message == "Stopped"
    assert fake.downloads == ["dup"]


def test_clear_finished_forgets_deferred_duplicates(make_jobs) -> None:
    manager, fake = make_jobs(concurrency=1)
    fake.gate = threading.Event()
    first = manager.submit(track_url("dup"))
    assert fake.download_started.wait(WAIT)
    second = manager.submit(track_url("dup"))
    assert wait_until(lambda: second.status is JobStatus.SKIPPED)
    assert manager.clear_finished() == 1  # the skipped duplicate is gone from the queue
    assert manager.cancel(first.id)
    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert first.status is JobStatus.CANCELLED
    assert manager.list() == [first]  # nothing revived a job the user cleared away
    assert fake.downloads == ["dup"]


def test_duplicate_waiting_behind_a_download_is_skipped(make_jobs) -> None:
    manager, fake = make_jobs(concurrency=1)
    fake.gate = threading.Event()
    first = manager.submit(track_url("held"))
    assert fake.download_started.wait(WAIT)
    assert wait_until(lambda: first.status is JobStatus.DOWNLOADING)
    second = manager.submit(track_url("held"))
    assert wait_until(lambda: second.status.is_terminal)  # decided at resolve time, no waiting
    assert second.status is JobStatus.SKIPPED
    assert second.message == "Already in the queue"

    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert first.status is JobStatus.DONE
    assert fake.downloads == ["held"]


def test_playlist_with_a_repeated_video_downloads_it_once(
    make_jobs, library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, fake = make_jobs(
        concurrency=2
    )  # the repeat is dropped at resolve time: no delay needed
    refs = [fake._ref("same"), fake._ref("same"), fake._ref("other")]
    monkeypatch.setattr(fake, "resolve", lambda url: refs)

    parent = manager.submit(playlist_url(3))
    assert manager.wait_idle(WAIT)

    assert parent.status is JobStatus.DONE
    assert parent.child_count == 2  # the repeat is dropped while resolving
    children = children_of(manager, parent)
    assert [c.track_ref.source_id for c in children] == ["same", "other"]
    assert all(c.status is JobStatus.DONE for c in children)
    assert sorted(fake.downloads) == ["other", "same"]
    assert len(library) == 2


def test_playlist_repeating_a_track_that_is_already_queued(make_jobs) -> None:
    manager, fake = make_jobs(concurrency=1)
    fake.gate = threading.Event()
    single = manager.submit(track_url("pl2-1"))
    assert fake.download_started.wait(WAIT)
    parent = manager.submit(playlist_url(2))  # pl2-1 (busy) and pl2-2
    assert wait_until(lambda: parent.child_count == 2)
    children = children_of(manager, parent)
    assert wait_until(lambda: children[0].status is JobStatus.SKIPPED)
    assert children[0].message == "Already in the queue"

    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert single.status is JobStatus.DONE
    assert children[1].status is JobStatus.DONE
    assert fake.downloads == ["pl2-1", "pl2-2"]


# -- cancel ----------------------------------------------------------------------------------


def test_cancel_while_queued_before_start(tmp_settings: Settings, library: Library) -> None:
    fake = FakeProvider()
    manager = JobManager(tmp_settings, library, providers=FakeRegistry(fake))
    job = manager.submit(track_url("q"))
    assert job.status is JobStatus.QUEUED

    assert manager.cancel(job.id) is True
    assert job.status is JobStatus.CANCELLED
    assert manager.cancel(job.id) is False  # already terminal

    manager.start()
    assert manager.wait_idle(WAIT)
    manager.stop()
    assert fake.resolved == []
    assert fake.downloads == []


def test_cancel_while_queued_behind_another_download(make_jobs, library: Library) -> None:
    manager, fake = make_jobs(concurrency=1)
    fake.gate = threading.Event()

    first = manager.submit(track_url("first"))
    assert fake.download_started.wait(WAIT)
    second = manager.submit(track_url("second"))
    # `second` is resolved by the resolver thread and then waits for the busy worker:
    # QUEUED with a track_ref. Cancelling a queued job must be immediate.
    assert wait_until(lambda: second.track_ref is not None and second.status is JobStatus.QUEUED)
    assert manager.cancel(second.id) is True
    assert second.status is JobStatus.CANCELLED

    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert first.status is JobStatus.DONE
    assert second.status is JobStatus.CANCELLED
    assert fake.downloads == ["first"]
    assert not library.has("fake:second")


def test_cancel_while_downloading(make_jobs, library: Library) -> None:
    manager, fake = make_jobs(delay=30.0)  # far longer than the test; cancel cuts it short

    job = manager.submit(track_url("slow"))
    assert fake.download_started.wait(WAIT)
    assert job.status in (JobStatus.DOWNLOADING, JobStatus.QUEUED)

    assert manager.cancel(job.id) is True
    assert manager.wait_idle(WAIT)

    assert job.status is JobStatus.CANCELLED
    assert job.message == "Cancelled"
    assert job.track is None
    assert not library.has("fake:slow")
    assert not (library.library_dir / "Fake Artist - Track slow.mp3").exists()


def test_cancel_unknown_or_finished_returns_false(make_jobs) -> None:
    manager, _ = make_jobs()
    job = manager.submit(track_url("done"))
    assert manager.wait_idle(WAIT)
    assert manager.cancel(job.id) is False
    assert manager.cancel("no-such-job") is False


# -- errors ----------------------------------------------------------------------------------


def test_provider_error_message_lands_on_the_job(make_jobs, library: Library) -> None:
    manager, _ = make_jobs(fail_ids={"bad"})

    job = manager.submit(track_url("bad"))
    assert manager.wait_idle(WAIT)

    assert job.status is JobStatus.ERROR
    assert job.error == "Fake provider refused to download 'bad'"
    assert job.track is None
    assert not library.has("fake:bad")


def test_resolve_error_lands_on_the_job(make_jobs, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, fake = make_jobs()
    from ultimate_playlist.providers.base import ProviderError

    def boom(url: str) -> list:
        raise ProviderError("This video is private")

    monkeypatch.setattr(fake, "resolve", boom)
    job = manager.submit(track_url("private"))
    assert manager.wait_idle(WAIT)
    assert job.status is JobStatus.ERROR
    assert job.error == "This video is private"


def test_unexpected_exception_becomes_a_friendly_error(make_jobs, monkeypatch) -> None:
    manager, fake = make_jobs()

    def explode(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(fake, "download", explode)
    job = manager.submit(track_url("x"))
    assert manager.wait_idle(WAIT)
    assert job.status is JobStatus.ERROR
    assert job.error == "Unexpected error: kaboom"


def test_duplicate_finishing_just_before_the_claim_is_not_downloaded_twice(
    make_jobs, library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """library.has() is evaluated under the claim lock, so a track that landed a moment ago is
    skipped instead of downloaded again by a worker that had already passed an earlier check."""
    manager, fake = make_jobs(concurrency=1)
    first = manager.submit(track_url("once"))
    assert manager.wait_idle(WAIT)
    assert first.status is JobStatus.DONE

    calls: list[str] = []
    real_has = library.has

    def counting_has(track_id: str) -> bool:
        calls.append(track_id)
        return real_has(track_id)

    monkeypatch.setattr(library, "has", counting_has)
    second = manager.submit(track_url("once"))
    assert manager.wait_idle(WAIT)
    assert second.status is JobStatus.SKIPPED and second.message == "Already in library"
    assert calls == ["fake:once"]  # exactly one stat, inside the locked claim
    assert fake.downloads == ["once"]


def test_bug_outside_the_download_handlers_still_ends_the_job(
    make_jobs, library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception before/after provider.download() must not leave a job non-terminal forever."""
    manager, fake = make_jobs()

    def bad_has(track_id: str) -> bool:
        raise ValueError("embedded null byte")

    monkeypatch.setattr(library, "has", bad_has)
    job = manager.submit(track_url("nul"))
    assert manager.wait_idle(WAIT)  # would hang forever before the fix
    assert job.status is JobStatus.ERROR
    assert job.error == "Unexpected error: embedded null byte"
    assert fake.downloads == []
    assert manager.running  # the worker thread survived


def test_bug_while_resolving_still_ends_the_job(make_jobs, monkeypatch) -> None:
    manager, _ = make_jobs()

    def bad_provider(job: Job):
        raise RuntimeError("registry broke")

    monkeypatch.setattr(manager, "_provider_for", bad_provider)
    job = manager.submit(track_url("r"))
    assert manager.wait_idle(WAIT)
    assert job.status is JobStatus.ERROR
    assert job.error == "Unexpected error: registry broke"


# -- retry -----------------------------------------------------------------------------------


def test_retry_creates_a_new_job(make_jobs, library: Library) -> None:
    manager, fake = make_jobs(fail_ids={"flaky"})
    failed = manager.submit(track_url("flaky"))
    assert manager.wait_idle(WAIT)
    assert failed.status is JobStatus.ERROR

    fake.fail_ids.clear()
    retried = manager.retry(failed.id)

    assert retried is not None
    assert retried.id != failed.id
    assert retried.url == failed.url
    assert retried.track_ref is failed.track_ref  # goes straight to the download queue
    assert manager.wait_idle(WAIT)
    assert retried.status is JobStatus.DONE
    assert failed.status is JobStatus.ERROR  # the old job is untouched
    assert library.has("fake:flaky")
    assert manager.list()[0] is retried  # newest first


def test_retry_of_a_cancelled_unresolved_job_resolves_again(make_jobs) -> None:
    manager, fake = make_jobs()
    manager.stop()  # keep the job queued so it can be cancelled before resolving
    job = manager.submit(track_url("later"))
    assert manager.cancel(job.id)
    assert job.track_ref is None

    retried = manager.retry(job.id)
    assert retried is not None and retried.track_ref is None and retried.parent_id is None
    manager.start()
    assert manager.wait_idle(WAIT)
    assert retried.status is JobStatus.DONE
    assert fake.resolved == [track_url("later")]


def test_retry_keeps_the_parent_link(make_jobs) -> None:
    manager, fake = make_jobs(fail_ids={"pl2-2"})
    parent = manager.submit(playlist_url(2))
    assert manager.wait_idle(WAIT)
    failed = next(c for c in children_of(manager, parent) if c.status is JobStatus.ERROR)

    fake.fail_ids.clear()
    retried = manager.retry(failed.id)
    assert retried is not None and retried.parent_id == parent.id
    assert manager.wait_idle(WAIT)
    assert retried.status is JobStatus.DONE


def test_retry_refuses_jobs_that_are_not_error_or_cancelled(make_jobs) -> None:
    manager, _ = make_jobs()
    done = manager.submit(track_url("ok"))
    assert manager.wait_idle(WAIT)
    assert done.status is JobStatus.DONE
    assert manager.retry(done.id) is None
    assert manager.retry("missing") is None


# -- housekeeping ----------------------------------------------------------------------------


def test_clear_finished(make_jobs) -> None:
    manager, fake = make_jobs(fail_ids={"bad"})
    manager.submit(track_url("bad"))
    manager.submit(playlist_url(2))
    assert manager.wait_idle(WAIT)
    assert len(manager.list()) == 4  # error + parent + 2 children

    fake.gate = threading.Event()  # closed: the next download blocks until we open it
    active = manager.submit(track_url("busy"))
    assert fake.download_started.wait(WAIT)
    # ensure the active job really is mid-download before clearing
    assert wait_until(lambda: active.status is JobStatus.DOWNLOADING)

    assert manager.clear_finished() == 4
    assert [j.id for j in manager.list()] == [active.id]

    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert manager.clear_finished() == 1
    assert manager.list() == []
    assert manager.clear_finished() == 0


def test_snapshot_shape(make_jobs) -> None:
    manager, _ = make_jobs()
    job = manager.submit(track_url("snap"))
    assert manager.wait_idle(WAIT)

    snapshot = manager.snapshot()
    assert isinstance(snapshot, list) and len(snapshot) == 1
    entry = snapshot[0]
    assert entry == job.to_dict()
    assert set(entry) == {
        "id",
        "url",
        "provider",
        "status",
        "progress",
        "speed",
        "eta",
        "message",
        "error",
        "parent_id",
        "child_count",
        "title",
        "track_ref",
        "track",
        "created_at",
        "updated_at",
    }
    assert entry["status"] == "done"
    assert entry["title"] == "Fake Artist - Track snap"
    assert entry["track"]["id"] == "fake:snap"
    assert entry["track_ref"]["source_id"] == "snap"
    assert entry["provider"] == "fake"


def test_snapshot_is_newest_first(make_jobs) -> None:
    manager, _ = make_jobs()
    a = manager.submit(track_url("a"))
    b = manager.submit(track_url("b"))
    assert manager.wait_idle(WAIT)
    assert [e["id"] for e in manager.snapshot()] == [b.id, a.id]


def test_wait_idle_times_out_when_nothing_runs(tmp_settings: Settings, library: Library) -> None:
    manager = JobManager(tmp_settings, library, providers=FakeRegistry(FakeProvider()))
    assert manager.wait_idle(0.05) is True  # nothing queued at all
    manager.submit(track_url("never"))
    assert manager.wait_idle(0.05) is False  # not started, so the job never finishes
    manager.stop()  # never started: still safe


def test_stop_is_idempotent_and_start_restarts(make_jobs) -> None:
    manager, _ = make_jobs()
    assert manager.running
    manager.stop()
    assert not manager.running
    manager.stop()
    manager.stop(wait=False)
    assert not manager.running

    manager.start()
    manager.start()
    assert manager.running
    job = manager.submit(track_url("after-restart"))
    assert manager.wait_idle(WAIT)
    assert job.status is JobStatus.DONE
    manager.stop()


def test_stop_cancels_queued_jobs_and_active_downloads(make_jobs, library: Library) -> None:
    manager, fake = make_jobs(concurrency=1, delay=30.0)
    active = manager.submit(track_url("active"))
    assert fake.download_started.wait(WAIT)
    queued = manager.submit(track_url("queued"))

    manager.stop()

    assert active.status is JobStatus.CANCELLED
    assert queued.status is JobStatus.CANCELLED
    assert not library.has("fake:active")


def test_restart_after_stop_without_waiting_does_not_lose_workers(
    make_jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker stuck in a provider that ignores cancel must not poison the next pool.

    Before the fix the stop sentinel it never consumed killed the freshly started worker."""
    manager, fake = make_jobs(concurrency=1)
    release = threading.Event()
    started = threading.Event()

    def stuck(ref, dest_dir, settings, progress, cancel):
        started.set()
        release.wait(WAIT)  # ignores `cancel` until released, like a hung ffmpeg
        raise DownloadCancelled("finally noticed")

    monkeypatch.setattr(fake, "download", stuck)
    old_workers = list(manager._workers)
    first = manager.submit(track_url("stuck"))
    assert started.wait(WAIT)

    manager.stop(wait=False)  # returns at once; the old worker is still inside `stuck`
    assert first.status is JobStatus.DOWNLOADING
    manager.start()
    assert manager.worker_count == 1
    assert all(t.is_alive() for t in old_workers)

    monkeypatch.setattr(fake, "download", FakeProvider.download.__get__(fake))
    second = manager.submit(track_url("fresh"))
    assert wait_until(lambda: second.status is JobStatus.DONE)  # served by the new worker

    release.set()
    assert wait_until(lambda: first.status is JobStatus.CANCELLED)
    assert wait_until(lambda: not any(t.is_alive() for t in old_workers))  # old pool retired
    assert manager.worker_count == 1


def test_set_concurrency_grows_and_shrinks_the_pool(make_jobs) -> None:
    manager, fake = make_jobs(concurrency=1)
    fake.gate = threading.Event()
    first = manager.submit(track_url("one"))
    second = manager.submit(track_url("two"))
    assert wait_until(lambda: first.status is JobStatus.DOWNLOADING)
    assert wait_until(lambda: second.track_ref is not None and second.status is JobStatus.QUEUED)
    assert fake.downloads == ["one"]
    assert manager.worker_count == 1

    manager.set_concurrency(2)
    assert wait_until(lambda: second.status is JobStatus.DOWNLOADING)  # a new worker took it
    assert manager.worker_count == 2
    assert sorted(fake.downloads) == ["one", "two"]

    manager.set_concurrency(1)
    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert first.status is JobStatus.DONE and second.status is JobStatus.DONE
    assert wait_until(lambda: manager.worker_count == 1)  # the surplus worker retired itself
    third = manager.submit(track_url("three"))
    assert manager.wait_idle(WAIT)
    assert third.status is JobStatus.DONE
    assert manager.worker_count == 1

    manager.stop()
    manager.set_concurrency(3)  # remembered, but nothing is spawned while stopped
    assert manager.worker_count == 0


def test_default_registry_is_the_providers_module(tmp_settings: Settings, library: Library) -> None:
    from ultimate_playlist import providers

    manager = JobManager(tmp_settings, library)
    assert manager.providers is providers
    manager.stop()


def test_uses_global_registry_with_registered_fake(
    tmp_settings: Settings, library: Library, fake_provider: FakeProvider
) -> None:
    manager = JobManager(tmp_settings, library)
    manager.start()
    try:
        job = manager.submit(track_url("global"))
        assert manager.wait_idle(WAIT)
        assert job.status is JobStatus.DONE
        assert fake_provider.downloads == ["global"]
    finally:
        manager.stop()
