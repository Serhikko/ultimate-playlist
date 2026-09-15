"""Download queue: one resolver thread expands links into tracks, worker threads download them."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .config import Settings
from .library import Library
from .models import Job, JobStatus, ProgressEvent, TrackRef, utc_now_iso
from .providers.base import DownloadCancelled, Provider, ProviderError

log = logging.getLogger(__name__)

PROGRESS_INTERVAL = 0.25  # seconds between progress copies onto the job (~4 updates/s)
STOP_JOIN_TIMEOUT = 30.0
QUEUE_POLL_INTERVAL = 1.0  # idle threads wake up this often to notice a shrink or a restart
MAX_WORKERS = 6

_Queue = queue.Queue[str | None]


class ProviderRegistry(Protocol):
    """What JobManager needs from the provider registry (the `providers` module fits)."""

    def get_provider(self, url: str) -> Provider | None: ...


def _drop_sentinels(q: _Queue) -> None:
    """Remove stop sentinels left over from an earlier stop(); job ids survive in order."""
    kept: list[str] = []
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            break
        if item is not None:
            kept.append(item)
    for item in kept:
        q.put(item)


def _parent_message(refs: list[TrackRef], count: int) -> str:
    """Queue text for a link that expanded into several tracks.

    Providers that know what the link was (Spotify puts "Album: <name>" or "Playlist: <name>" in
    ``TrackRef.extra["container"]``) get it named; everything else stays "Playlist: N tracks".
    """
    extra = refs[0].extra if refs else None
    container = extra.get("container") if isinstance(extra, dict) else None
    if isinstance(container, str) and container.strip():
        return f"{container.strip()} ({count} tracks)"
    return f"Playlist: {count} tracks"


class JobManager:
    """Queue + worker threads. Every job mutation happens under one lock."""

    def __init__(
        self,
        settings: Settings,
        library: Library,
        providers: ProviderRegistry | None = None,
    ) -> None:
        if providers is None:
            from . import providers as registry

            providers = registry
        self.settings = settings
        self.library = library
        self.providers = providers
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._jobs: dict[str, Job] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._in_flight: set[str] = set()  # track ids a worker is downloading right now
        # Jobs skipped with "Already in the queue", keyed by the track id they are waiting on.
        # They get their turn if the job that claimed the track does not end with the file in
        # the library (cancelled, failed), instead of staying a misleading dead end.
        self._deferred: dict[str, list[str]] = {}
        self._resolve_q: _Queue = queue.Queue()
        self._download_q: _Queue = queue.Queue()
        self._resolver: threading.Thread | None = None
        self._workers: list[threading.Thread] = []  # live workers of the current generation
        self._generation = 0  # bumped by every start(); stale threads retire themselves
        self._worker_seq = 0
        self._target_workers = self._clamp_workers(settings.concurrency)
        self._running = False

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def worker_count(self) -> int:
        """Live worker threads of the current pool (for tests and diagnostics)."""
        with self._lock:
            return len(self._workers)

    @staticmethod
    def _clamp_workers(n: object) -> int:
        try:
            value = int(n)  # type: ignore[call-overload]
        except (TypeError, ValueError):
            value = 1
        return max(1, min(value, MAX_WORKERS))

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._generation += 1
            generation = self._generation
            # A previous stop(wait=False) may have left sentinels behind; they must not kill
            # the threads we are about to start. Job ids queued while stopped are kept.
            _drop_sentinels(self._resolve_q)
            _drop_sentinels(self._download_q)
            self._target_workers = self._clamp_workers(self.settings.concurrency)
            self._resolver = threading.Thread(
                target=self._resolver_loop, args=(generation,), name="up-resolver", daemon=True
            )
            self._resolver.start()
            self._workers = []
            for _ in range(self._target_workers):
                self._spawn_worker(generation)
            started = len(self._workers)
        log.debug("JobManager started with %d worker(s)", started)

    def _spawn_worker(self, generation: int) -> threading.Thread:
        """Register and start a worker of `generation`. Call under the lock, so that stop() can
        never see (and try to join) a registered thread that has not been started yet."""
        self._worker_seq += 1
        thread = threading.Thread(
            target=self._worker_loop,
            args=(generation,),
            name=f"up-worker-{self._worker_seq}",
            daemon=True,
        )
        self._workers.append(thread)
        thread.start()
        return thread

    def set_concurrency(self, n: int) -> None:
        """Resize the worker pool at runtime: extra workers start now, surplus ones retire."""
        with self._lock:
            self._target_workers = self._clamp_workers(n)
            if not self._running:
                return
            grown = 0
            while len(self._workers) < self._target_workers:
                self._spawn_worker(self._generation)
                grown += 1
        if grown:
            log.debug("Worker pool grown by %d to %d", grown, self._target_workers)

    def stop(self, wait: bool = True) -> None:
        """Cancel queued/active jobs and shut the threads down. Safe to call more than once."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            threads = [t for t in (self._resolver, *self._workers) if t is not None]
            self._resolver = None
            self._workers = []
            for job in list(self._jobs.values()):
                if job.status == JobStatus.QUEUED:
                    self._update(job, status=JobStatus.CANCELLED, message="Stopped")
                    if job.track_ref is not None:
                        self._release_deferred(job.track_ref.track_id)
                elif not job.status.is_terminal:
                    self._event_for(job.id).set()
            self._resolve_q.put(None)
            for _ in range(len(threads) - 1):
                self._download_q.put(None)
        if wait:
            for thread in threads:
                thread.join(timeout=STOP_JOIN_TIMEOUT)
                if thread.is_alive():
                    log.warning("%s did not stop in time; giving up waiting", thread.name)

    # -- public API --------------------------------------------------------------------------

    def submit(self, url: str) -> Job:
        """Validate the link with the registry and queue it for resolving."""
        url = (url or "").strip()
        normalize = getattr(self.providers, "normalize_url", None)
        if callable(normalize):
            url = normalize(url)
        if not url:
            raise ValueError("Paste a link first")
        provider = self.providers.get_provider(url)
        if provider is None:
            raise ValueError("No provider for this link")
        job = Job(url=url, provider=provider.name)
        with self._lock:
            self._jobs[job.id] = job
            self._cancel_events[job.id] = threading.Event()
        self._resolve_q.put(job.id)
        return job

    def list(self, include_finished: bool = True) -> list[Job]:
        with self._lock:
            jobs = list(reversed(self._jobs.values()))
        if not include_finished:
            jobs = [j for j in jobs if not j.status.is_terminal]
        return jobs

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if not j.status.is_terminal)

    def cancel(self, job_id: str) -> bool:
        """Queued jobs are cancelled at once; active ones get their cancel event set."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status.is_terminal:
                return False
            self._event_for(job_id).set()
            if job.status == JobStatus.QUEUED:
                self._update(job, status=JobStatus.CANCELLED, message="Cancelled")
                if job.track_ref is not None:  # a duplicate waiting on this job gets its turn
                    self._release_deferred(job.track_ref.track_id)
            else:
                self._update(job, message="Cancelling…")
            return True

    def retry(self, job_id: str) -> Job | None:
        """Re-queue an ERROR/CANCELLED job as a brand-new job (same url / same track)."""
        with self._lock:
            old = self._jobs.get(job_id)
            if old is None or old.status not in (JobStatus.ERROR, JobStatus.CANCELLED):
                return None
            new = Job(
                url=old.url,
                provider=old.provider,
                parent_id=old.parent_id,
                track_ref=old.track_ref,
            )
            self._jobs[new.id] = new
            self._cancel_events[new.id] = threading.Event()
        if new.track_ref is not None:
            self._download_q.put(new.id)
        else:
            self._resolve_q.put(new.id)
        return new

    def clear_finished(self) -> int:
        with self._lock:
            finished = [jid for jid, job in self._jobs.items() if job.status.is_terminal]
            for jid in finished:
                del self._jobs[jid]
                self._cancel_events.pop(jid, None)
            gone = set(finished)
            for track_id, waiting in list(self._deferred.items()):
                waiting[:] = [jid for jid in waiting if jid not in gone]
                if not waiting:
                    del self._deferred[track_id]
            return len(finished)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [job.to_dict() for job in reversed(self._jobs.values())]

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until no job is queued or active. Returns False on timeout."""
        with self._idle:
            return self._idle.wait_for(self._is_idle, timeout)

    # -- internals ---------------------------------------------------------------------------

    def _is_idle(self) -> bool:
        return all(job.status.is_terminal for job in self._jobs.values())

    def _event_for(self, job_id: str) -> threading.Event:
        return self._cancel_events.setdefault(job_id, threading.Event())

    def _update(self, job: Job, **fields: object) -> None:
        """Set fields on a job, bump updated_at and wake wait_idle(). Call under the lock."""
        with self._lock:
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated_at = utc_now_iso()
            self._idle.notify_all()

    def _finish(self, job: Job, status: JobStatus, **fields: object) -> None:
        with self._lock:
            if job.status.is_terminal:
                return  # e.g. cancelled while we were finishing; first terminal state wins
            self._update(job, status=status, speed=None, eta=None, **fields)

    def _fail_unexpected(self, job_id: str, exc: BaseException) -> None:
        """A bug escaped the per-job handlers: the job must still end, or the UI polls forever."""
        job = self.get(job_id)
        if job is not None:
            self._finish(job, JobStatus.ERROR, error=f"Unexpected error: {exc}", message=None)

    def _provider_for(self, job: Job) -> Provider | None:
        by_name = getattr(self.providers, "provider_by_name", None)
        if job.provider and callable(by_name):
            provider = by_name(job.provider)
            if provider is not None:
                return provider
        return self.providers.get_provider(job.url)

    def _skip_as_duplicate(self, job: Job) -> None:
        """SKIPPED "Already in the queue", remembered so the job can be revived. Under the lock."""
        self._finish(job, JobStatus.SKIPPED, progress=1.0, message="Already in the queue")
        if job.track_ref is not None:
            self._deferred.setdefault(job.track_ref.track_id, []).append(job.id)

    def _release_deferred(self, track_id: str) -> None:
        """The job that claimed `track_id` is over: revive the duplicates it made skip, unless
        the track really is in the library now. Call under the lock."""
        waiting = self._deferred.pop(track_id, None)
        if not waiting:
            return
        try:
            present = self.library.has(track_id)
        except Exception as exc:  # noqa: BLE001 - treat an unreadable library as "not there"
            log.warning("Could not check the library for %s: %s", track_id, exc)
            present = False
        if present:
            return
        for job_id in waiting:
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.SKIPPED:
                continue
            if not self._running:
                self._update(job, status=JobStatus.CANCELLED, message="Stopped")
                continue
            log.info("Re-queueing %s: the download it was waiting on did not finish", job.title)
            self._update(job, status=JobStatus.QUEUED, progress=0.0, message=None, error=None)
            self._download_q.put(job.id)

    def _claimed_elsewhere(self, track_id: str, job: Job, include_queued: bool) -> bool:
        """Is another live job already downloading (or waiting to download) this track?

        Call under the lock. Jobs still waiting in the download queue only count when
        `include_queued` is set: a worker that just picked a job up must ignore the ones
        behind it, otherwise two duplicates would skip each other.
        """
        if track_id in self._in_flight:
            return True
        for other in self._jobs.values():
            if other is job or other.status.is_terminal or other.track_ref is None:
                continue
            if other.track_ref.track_id != track_id:
                continue
            if other.status == JobStatus.QUEUED and not include_queued:
                continue
            return True
        return False

    @staticmethod
    def _dedupe_refs(refs: list[TrackRef]) -> list[TrackRef]:
        """A playlist can list the same video twice; download it once (first occurrence wins)."""
        seen: set[str] = set()
        unique: list[TrackRef] = []
        for ref in refs:
            if ref.track_id in seen:
                continue
            seen.add(ref.track_id)
            unique.append(ref)
        return unique

    def _stale(self, generation: int) -> bool:
        with self._lock:
            return generation != self._generation

    # resolving ------------------------------------------------------------------------------

    def _resolver_loop(self, generation: int) -> None:
        while True:
            try:
                job_id = self._resolve_q.get(timeout=QUEUE_POLL_INTERVAL)
            except queue.Empty:
                if self._stale(generation):
                    return
                continue
            if job_id is None:
                return
            if self._stale(generation):
                self._resolve_q.put(job_id)  # a newer pool owns the queue now
                return
            try:
                self._resolve_one(job_id)
            except Exception as exc:  # noqa: BLE001 - keep the thread alive no matter what
                log.exception("Resolver failed on job %s", job_id)
                self._fail_unexpected(job_id, exc)

    def _resolve_one(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED:
                return
            cancel = self._event_for(job_id)
            self._update(job, status=JobStatus.RESOLVING, message="Looking up the link…")
        provider = self._provider_for(job)
        if provider is None:
            self._finish(job, JobStatus.ERROR, error="No provider for this link", message=None)
            return
        try:
            refs = self._dedupe_refs(list(provider.resolve(job.url)))
        except ProviderError as exc:
            self._finish(job, JobStatus.ERROR, error=str(exc), message=None)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error while resolving %s", job.url)
            self._finish(job, JobStatus.ERROR, error=f"Unexpected error: {exc}", message=None)
            return

        with self._lock:
            if cancel.is_set():
                self._finish(job, JobStatus.CANCELLED, message="Cancelled")
                return
            if not refs:
                self._finish(
                    job, JobStatus.ERROR, error="Nothing to download at that link", message=None
                )
                return
            if len(refs) == 1:
                job.track_ref = refs[0]
                if self._claimed_elsewhere(refs[0].track_id, job, include_queued=True):
                    self._skip_as_duplicate(job)
                    return
                self._update(job, status=JobStatus.QUEUED, message=None)
                self._download_q.put(job.id)
                return
            children: list[tuple[Job, TrackRef]] = []
            for ref in refs:
                child = Job(
                    url=ref.url,
                    provider=ref.provider or provider.name,
                    parent_id=job.id,
                    track_ref=ref,
                )
                self._jobs[child.id] = child
                self._cancel_events[child.id] = threading.Event()
                children.append((child, ref))
            self._update(
                job,
                status=JobStatus.DONE,
                child_count=len(children),
                progress=1.0,
                message=_parent_message(refs, len(children)),
            )
            for child, ref in children:
                if self._claimed_elsewhere(ref.track_id, child, include_queued=True):
                    self._skip_as_duplicate(child)
                    continue
                self._download_q.put(child.id)

    # downloading ----------------------------------------------------------------------------

    def _worker_loop(self, generation: int) -> None:
        try:
            while True:
                try:
                    job_id = self._download_q.get(timeout=QUEUE_POLL_INTERVAL)
                except queue.Empty:
                    if self._retire(generation):
                        return
                    continue
                if job_id is None:
                    return
                if self._stale(generation):
                    self._download_q.put(job_id)  # a newer pool owns the queue now
                    return
                try:
                    self._download_one(job_id)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Worker failed on job %s", job_id)
                    self._fail_unexpected(job_id, exc)
                if self._retire(generation):
                    return
        finally:
            with self._lock:
                me = threading.current_thread()
                if me in self._workers:
                    self._workers.remove(me)

    def _retire(self, generation: int) -> bool:
        """Leave the pool when this thread is stale or the pool was shrunk (atomic decision)."""
        with self._lock:
            me = threading.current_thread()
            if generation != self._generation or me not in self._workers:
                return True
            if self._running and len(self._workers) > self._target_workers:
                self._workers.remove(me)
                log.debug("%s retired; %d worker(s) left", me.name, len(self._workers))
                return True
            return False

    def _download_one(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED or job.track_ref is None:
                return
            ref = job.track_ref
            cancel = self._event_for(job_id)

        provider = self._provider_for(job)
        if provider is None:
            self._finish(job, JobStatus.ERROR, error="No provider for this link", message=None)
            return
        settings = self.settings
        dest_dir = Path(settings.library_dir)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._finish(
                job,
                JobStatus.ERROR,
                error=f"Cannot create the library folder {dest_dir}: {exc}",
                message=None,
            )
            return

        with self._lock:
            if job.status != JobStatus.QUEUED or cancel.is_set():
                self._finish(job, JobStatus.CANCELLED, message="Cancelled")
                return
            # Checked under the lock (a single stat), so a duplicate that was waiting behind
            # the download of this very track cannot slip in between "not in the library yet"
            # and its own claim and download the file a second time.
            if self.library.has(ref.track_id):
                self._finish(job, JobStatus.SKIPPED, progress=1.0, message="Already in library")
                return
            if self._claimed_elsewhere(ref.track_id, job, include_queued=False):
                self._skip_as_duplicate(job)
                return
            self._in_flight.add(ref.track_id)
            self._update(
                job, status=JobStatus.DOWNLOADING, progress=0.0, speed=None, eta=None, error=None
            )
        outcome: tuple[JobStatus, dict[str, object]] | None = None
        try:
            outcome = self._run_download(job, ref, provider, dest_dir, settings, cancel)
        finally:
            # Finishing the job, releasing the claim and reviving duplicates happen in one go
            # under the lock, so wait_idle() never sees a moment where everything looks done
            # while a revived duplicate is about to be queued.
            with self._lock:
                self._in_flight.discard(ref.track_id)
                if outcome is not None:
                    status, fields = outcome
                    self._finish(job, status, **fields)
                self._release_deferred(ref.track_id)

    def _run_download(
        self,
        job: Job,
        ref: TrackRef,
        provider: Provider,
        dest_dir: Path,
        settings: Settings,
        cancel: threading.Event,
    ) -> tuple[JobStatus, dict[str, object]]:
        """Run the provider and index the result; returns the terminal status and job fields."""
        try:
            track = provider.download(
                ref, dest_dir, settings, self._progress_callback(job.id), cancel
            )
        except DownloadCancelled:
            return JobStatus.CANCELLED, {"message": "Cancelled"}
        except ProviderError as exc:
            return JobStatus.ERROR, {"error": str(exc), "message": None}
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error while downloading %s", job.url)
            return JobStatus.ERROR, {"error": f"Unexpected error: {exc}", "message": None}

        try:
            self.library.add(track)
        except Exception as exc:  # noqa: BLE001
            log.exception("Could not add %s to the library index", track.path)
            return JobStatus.ERROR, {
                "error": f"Downloaded, but the library index could not be updated: {exc}",
                "message": None,
            }
        return JobStatus.DONE, {"track": track, "progress": 1.0, "message": None}

    def _progress_callback(self, job_id: str) -> Callable[[ProgressEvent], None]:
        last_copy = 0.0

        def on_progress(event: ProgressEvent) -> None:
            nonlocal last_copy
            now = time.monotonic()
            with self._lock:
                job = self._jobs.get(job_id)
                if job is None or job.status.is_terminal:
                    return
                status = event.status
                status_changed = (
                    status in (JobStatus.DOWNLOADING, JobStatus.CONVERTING) and status != job.status
                )
                message_changed = event.message is not None and event.message != job.message
                if (
                    not status_changed
                    and not message_changed
                    and now - last_copy < PROGRESS_INTERVAL
                ):
                    return
                last_copy = now
                fields: dict[str, object] = {"speed": event.speed, "eta": event.eta}
                if status_changed:
                    fields["status"] = status
                if event.progress is not None:
                    fields["progress"] = max(0.0, min(1.0, float(event.progress)))
                if event.message is not None:
                    fields["message"] = event.message
                self._update(job, **fields)

        return on_progress
