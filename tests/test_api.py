"""Every API endpoint through the FastAPI TestClient, offline, with the FakeProvider."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import pytest
from fake_provider import FakeProvider, FakeRegistry, playlist_url, track_url, write_tiny_mp3
from fastapi.testclient import TestClient
from mutagen.id3 import APIC, ID3

from ultimate_playlist import __version__
from ultimate_playlist.config import Settings, app_data_dir
from ultimate_playlist.downloader import JobManager
from ultimate_playlist.library import Library
from ultimate_playlist.models import JobStatus, Track
from ultimate_playlist.playlists import PlaylistStore
from ultimate_playlist.server import app as server_app

WAIT = 15.0
TERMINAL = {s.value for s in JobStatus if s.is_terminal}
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
LOOPBACK = "http://127.0.0.1"


# -- helpers -----------------------------------------------------------------------------------


def add_track(
    library: Library, source_id: str, *, cover: bool = False, artist: str = "Fake Artist"
) -> Track:
    """Write a tagged tiny MP3 into the library folder and index it."""
    rel = f"{artist} - Track {source_id}.mp3"
    path = write_tiny_mp3(
        library.library_dir / rel,
        artist=artist,
        title=f"Track {source_id}",
        track_id=f"fake:{source_id}",
    )
    if cover:
        tags = ID3(path)
        tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover", data=PNG_BYTES))
        tags.save(path)
    track = Track(
        id=f"fake:{source_id}",
        provider="fake",
        source_id=source_id,
        source_url=track_url(source_id),
        title=f"Track {source_id}",
        artist=artist,
        path=rel,
        duration=0.3,
        has_cover=cover,
        file_size=path.stat().st_size,
    )
    library.add(track)
    return track


def wait_until(predicate: Callable[[], bool], timeout: float = WAIT) -> bool:
    pause = threading.Event()
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            return False
        pause.wait(0.02)
    return True


def get_job(client: TestClient, job_id: str) -> dict[str, Any] | None:
    for job in client.get("/api/jobs").json()["jobs"]:
        if job["id"] == job_id:
            return job
    return None


def wait_for_status(client: TestClient, job_id: str, statuses: set[str]) -> dict[str, Any]:
    """Poll GET /api/jobs until the job reaches one of `statuses`."""
    assert wait_until(lambda: (get_job(client, job_id) or {}).get("status") in statuses), (
        f"job {job_id} never reached {statuses}: {get_job(client, job_id)}"
    )
    job = get_job(client, job_id)
    assert job is not None
    return job


def wait_terminal(client: TestClient, job_id: str) -> dict[str, Any]:
    return wait_for_status(client, job_id, TERMINAL)


def submit(client: TestClient, url: str) -> dict[str, Any]:
    response = client.post("/api/jobs", json={"url": url})
    assert response.status_code == 202, response.text
    jobs = response.json()["jobs"]
    assert len(jobs) == 1
    return jobs[0]


# -- UI files ----------------------------------------------------------------------------------


def test_index_serves_html(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<html" in response.text.lower()


def test_static_files_are_served(client: TestClient) -> None:
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/does-not-exist.js").status_code == 404


def test_favicon_is_served(client: TestClient) -> None:
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text.startswith("<svg")


# -- status & settings -------------------------------------------------------------------------


def test_status_shape(client: TestClient, library: Library) -> None:
    add_track(library, "s1")
    data = client.get("/api/status").json()
    assert data["version"] == __version__
    assert data["library_dir"] == str(library.library_dir)
    assert data["tracks"] == 1
    assert data["jobs_active"] == 0
    assert set(data["ffmpeg"]) == {"path", "version", "bundled"}
    assert data["ffmpeg"]["bundled"] is False
    names = [p["name"] for p in data["providers"]]
    assert "fake" in names
    fake = next(p for p in data["providers"] if p["name"] == "fake")
    assert fake["display_name"] == "Fake (offline)"
    assert fake["checks"] == [
        {"ok": True, "label": "Fake provider", "detail": "offline stub, always available"}
    ]


def test_version_endpoint_does_no_work(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second UltimatePlaylist.exe asks /api/version to find the running copy; it must
    answer at once even while ffmpeg / the JS runtime are being probed for /api/status."""

    def never(explicit: str | None = None) -> None:
        raise AssertionError("/api/version must not probe ffmpeg")

    monkeypatch.setattr(server_app, "find_ffmpeg", never)
    monkeypatch.setattr(server_app, "provider_status", never)
    response = client.get("/api/version")
    assert response.status_code == 200
    assert response.json() == {"version": __version__}


def test_status_survives_a_broken_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Broken:
        name = "broken"
        display_name = "Broken"

        def matches(self, url: str) -> bool:
            return False

        def doctor(self) -> list[tuple[bool, str, str]]:
            raise RuntimeError("boom")

    # patch the registry as seen by the server module only, so the global list stays untouched
    monkeypatch.setattr(server_app, "providers", SimpleNamespace(PROVIDERS=[Broken()]))
    data = client.get("/api/status").json()
    assert data["providers"][0]["checks"][0]["ok"] is False
    assert "boom" in data["providers"][0]["checks"][0]["detail"]


def test_get_settings(client: TestClient, tmp_settings: Settings) -> None:
    assert client.get("/api/settings").json() == tmp_settings.to_dict()


def test_put_settings_partial_update_is_saved(client: TestClient, tmp_settings: Settings) -> None:
    response = client.put("/api/settings", json={"concurrency": 3, "embed_cover": False})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["concurrency"] == 3
    assert data["embed_cover"] is False
    assert data["library_dir"] == str(tmp_settings.library_dir)  # untouched
    assert tmp_settings.concurrency == 3  # the app's Settings object was updated in place
    saved = json.loads(Settings.default_path().read_text(encoding="utf-8"))
    assert saved["concurrency"] == 3 and saved["embed_cover"] is False
    assert client.app.state.jobs.settings is tmp_settings


@pytest.mark.parametrize("concurrency", [0, 7, -1])
def test_put_settings_rejects_concurrency_out_of_range(
    client: TestClient, concurrency: int
) -> None:
    response = client.put("/api/settings", json={"concurrency": concurrency})
    assert response.status_code == 400
    assert "between 1 and 6" in response.json()["detail"]


def test_put_settings_rejects_wrong_types(client: TestClient) -> None:
    assert client.put("/api/settings", json={"concurrency": "lots"}).status_code == 422
    assert client.put("/api/settings", json=["not", "an", "object"]).status_code == 422


def test_validation_errors_use_the_detail_string_shape(client: TestClient) -> None:
    """Pydantic's list of error objects is flattened to the spec's {"detail": "<text>"}."""
    response = client.put("/api/settings", json={"concurrency": 2.5})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str) and detail.startswith("concurrency: ")
    response = client.put("/api/settings", json={"library_dir": 123, "js_runtimes": "node"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str) and "library_dir: " in detail and "js_runtimes: " in detail
    response = client.post("/api/jobs", json={})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str) and "url" in response.json()["detail"]
    response = client.post(
        "/api/jobs", content=b"not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)
    response = client.delete("/api/library/x", params={"delete_file": "maybe"})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str) and "delete_file" in response.json()["detail"]


def test_put_settings_rejects_bad_values(client: TestClient, tmp_path: Path) -> None:
    assert client.put("/api/settings", json={"library_dir": "   "}).status_code == 400
    assert client.put("/api/settings", json={"audio_format": "wma"}).status_code == 400
    assert client.put("/api/settings", json={"audio_quality": ""}).status_code == 400
    assert client.put("/api/settings", json={"js_runtimes": []}).status_code == 400
    missing = tmp_path / "nope" / "ffmpeg.exe"
    response = client.put("/api/settings", json={"ffmpeg_path": str(missing)})
    assert response.status_code == 400
    assert "ffmpeg" in response.json()["detail"]


@pytest.mark.parametrize("quality", ["banana", "-1", "1.5", "1234", "k"])
def test_put_settings_rejects_nonsense_audio_quality(client: TestClient, quality: str) -> None:
    response = client.put("/api/settings", json={"audio_quality": quality})
    assert response.status_code == 400
    assert "Audio quality" in response.json()["detail"]


@pytest.mark.parametrize(("quality", "stored"), [("0", "0"), (" 5 ", "5"), ("192K", "192")])
def test_put_settings_accepts_vbr_levels_and_bitrates(
    client: TestClient, quality: str, stored: str
) -> None:
    response = client.put("/api/settings", json={"audio_quality": quality})
    assert response.status_code == 200, response.text
    assert response.json()["audio_quality"] == stored  # yt-dlp wants a bare number


def test_put_settings_accepts_ffmpeg_found_on_path(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "tools" / "ffmpeg.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(
        server_app.shutil,
        "which",
        lambda name: str(exe) if name in ("ffmpeg", "ffmpeg.exe") else None,
    )
    response = client.put("/api/settings", json={"ffmpeg_path": "ffmpeg"})
    assert response.status_code == 200, response.text
    assert response.json()["ffmpeg_path"] == str(exe)
    assert client.put("/api/settings", json={"ffmpeg_path": "ffmpeg-nope"}).status_code == 400
    response = client.put("/api/settings", json={"ffmpeg_path": ""})
    assert response.status_code == 200 and response.json()["ffmpeg_path"] is None


def test_put_settings_rejects_unknown_js_runtime(
    client: TestClient, tmp_settings: Settings
) -> None:
    response = client.put("/api/settings", json={"js_runtimes": ["foo"]})
    assert response.status_code == 400
    assert "foo" in response.json()["detail"] and "deno" in response.json()["detail"]
    assert tmp_settings.js_runtimes == ["deno", "node"]  # untouched
    response = client.put("/api/settings", json={"js_runtimes": ["Node", "node", "deno"]})
    assert response.status_code == 200, response.text
    assert response.json()["js_runtimes"] == ["node", "deno"]  # lower-cased, de-duplicated


def test_put_settings_ignores_unknown_keys(client: TestClient) -> None:
    response = client.put("/api/settings", json={"colour": "purple"})
    assert response.status_code == 200
    assert "colour" not in response.json()


# -- Spotify credentials -----------------------------------------------------------------------

MASK = "********"


def saved_config() -> dict[str, Any]:
    return json.loads(Settings.default_path().read_text(encoding="utf-8"))


def test_get_settings_masks_the_spotify_secret(client: TestClient, tmp_settings: Settings) -> None:
    data = client.get("/api/settings").json()
    assert data["spotify_client_id"] == "" and data["spotify_client_secret"] == ""
    tmp_settings.spotify_client_id = "abc123"
    tmp_settings.spotify_client_secret = "hunter2"
    response = client.get("/api/settings")
    data = response.json()
    assert data["spotify_client_id"] == "abc123"  # the id is not sensitive
    assert data["spotify_client_secret"] == MASK
    assert "hunter2" not in response.text


def test_put_settings_sets_and_keeps_the_spotify_secret(
    client: TestClient, tmp_settings: Settings
) -> None:
    response = client.put(
        "/api/settings",
        json={"spotify_client_id": "  abc123 ", "spotify_client_secret": " hunter2\n"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["spotify_client_id"] == "abc123"  # stripped
    assert response.json()["spotify_client_secret"] == MASK  # never echoed back
    assert "hunter2" not in response.text
    assert tmp_settings.spotify_client_id == "abc123"
    assert tmp_settings.spotify_client_secret == "hunter2"
    assert saved_config()["spotify_client_secret"] == "hunter2"  # config.json holds it as-is

    # the form sends the whole thing back, mask included: the secret stays what it was
    response = client.put(
        "/api/settings",
        json={"spotify_client_id": "abc123", "spotify_client_secret": MASK, "concurrency": 3},
    )
    assert response.status_code == 200, response.text
    assert tmp_settings.spotify_client_secret == "hunter2"
    assert tmp_settings.concurrency == 3
    assert saved_config()["spotify_client_secret"] == "hunter2"

    # a new secret replaces the old one
    response = client.put("/api/settings", json={"spotify_client_secret": "newer"})
    assert response.status_code == 200, response.text
    assert response.json()["spotify_client_secret"] == MASK
    assert tmp_settings.spotify_client_secret == "newer"
    assert saved_config()["spotify_client_secret"] == "newer"

    # an empty string clears either field
    response = client.put(
        "/api/settings", json={"spotify_client_id": "", "spotify_client_secret": ""}
    )
    assert response.status_code == 200, response.text
    assert response.json()["spotify_client_id"] == ""
    assert response.json()["spotify_client_secret"] == ""
    assert tmp_settings.spotify_client_id == "" and tmp_settings.spotify_client_secret == ""
    assert client.get("/api/settings").json()["spotify_client_secret"] == ""


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("spotify_client_id", "ünïcode", "plain ASCII"),
        ("spotify_client_secret", "tab\there", "plain ASCII"),
        ("spotify_client_secret", "line\nbreak", "plain ASCII"),
        ("spotify_client_id", "x" * 201, "too long"),
        ("spotify_client_secret", "y" * 201, "too long"),
    ],
)
def test_put_settings_rejects_bad_spotify_credentials(
    client: TestClient, tmp_settings: Settings, field: str, value: str, fragment: str
) -> None:
    tmp_settings.spotify_client_id = "keep-id"
    tmp_settings.spotify_client_secret = "keep-secret"
    response = client.put("/api/settings", json={field: value})
    assert response.status_code == 400
    assert fragment in response.json()["detail"]
    assert tmp_settings.spotify_client_id == "keep-id"  # untouched
    assert tmp_settings.spotify_client_secret == "keep-secret"
    assert client.put("/api/settings", json={field: 123}).status_code == 422
    assert client.put("/api/settings", json={field: ["a"]}).status_code == 422


def test_status_never_contains_the_spotify_secret(
    client: TestClient, tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_settings.spotify_client_id = "abc123"
    tmp_settings.spotify_client_secret = "hunter2-secret"

    class Leaky:  # a provider that (wrongly) echoes what it was configured with
        name = "spotify"
        display_name = "Spotify"

        def matches(self, url: str) -> bool:
            return False

        def doctor(self, settings: Settings) -> list[tuple[bool, str, str]]:
            return [(True, f"Spotify ({settings.spotify_client_secret})", "ok, id abc123")]

    monkeypatch.setattr(server_app, "providers", SimpleNamespace(PROVIDERS=[Leaky()]))
    response = client.get("/api/status")
    assert response.status_code == 200
    assert "hunter2-secret" not in response.text
    check = response.json()["providers"][0]["checks"][0]
    assert check["label"] == f"Spotify ({MASK})"
    assert check["detail"] == "ok, id abc123"
    assert "spotify_client_secret" not in response.json()  # settings are not part of status


def test_put_settings_hands_the_live_settings_to_providers(
    client: TestClient, tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Spotify provider must see new credentials at once, without a restart."""
    from ultimate_playlist import providers

    seen: list[Settings] = []
    monkeypatch.setattr(providers, "configure", lambda settings: seen.append(settings))
    response = client.put("/api/settings", json={"spotify_client_id": "abc123"})
    assert response.status_code == 200, response.text
    assert seen == [tmp_settings]  # the very object the JobManager downloads with
    assert seen[0].spotify_client_id == "abc123"
    # a rejected update configures nothing
    assert client.put("/api/settings", json={"concurrency": 99}).status_code == 400
    assert len(seen) == 1


def test_put_settings_creates_missing_library_dir(
    client: TestClient, tmp_path: Path, tmp_settings: Settings, library: Library
) -> None:
    new_dir = tmp_path / "fresh" / "lib"
    assert not new_dir.exists()
    response = client.put("/api/settings", json={"library_dir": str(new_dir)})
    assert response.status_code == 200, response.text
    assert new_dir.is_dir()
    assert response.json()["library_dir"] == str(new_dir)
    assert library.library_dir == new_dir and tmp_settings.library_dir == new_dir
    assert client.get("/api/library").json()["tracks"] == []


def test_put_settings_rejects_library_dir_that_cannot_be_created(
    client: TestClient, tmp_path: Path, tmp_settings: Settings
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder", encoding="utf-8")
    response = client.put("/api/settings", json={"library_dir": str(blocker / "lib")})
    assert response.status_code == 400
    assert "Cannot create the library folder" in response.json()["detail"]
    assert tmp_settings.library_dir == tmp_path / "lib"


def test_put_settings_library_dir_repoints_and_rescans(
    client: TestClient, tmp_path: Path, tmp_settings: Settings, library: Library
) -> None:
    new_dir = tmp_path / "moved" / "lib"
    write_tiny_mp3(new_dir / "Someone - Song.mp3", artist="Someone", title="Song")
    response = client.put("/api/settings", json={"library_dir": str(new_dir)})
    assert response.status_code == 200, response.text
    assert response.json()["library_dir"] == str(new_dir)
    assert library.library_dir == new_dir
    assert tmp_settings.library_dir == new_dir
    tracks = client.get("/api/library").json()["tracks"]
    assert [t["title"] for t in tracks] == ["Song"]
    assert client.get("/api/status").json()["library_dir"] == str(new_dir)


def test_put_settings_relative_library_dir_is_made_absolute(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    response = client.put("/api/settings", json={"library_dir": "rel-lib-probe"})
    assert response.status_code == 200, response.text
    stored = Path(response.json()["library_dir"])
    assert stored.is_absolute()
    assert stored == tmp_path / "rel-lib-probe"
    saved = json.loads(Settings.default_path().read_text(encoding="utf-8"))
    assert saved["library_dir"] == str(stored)


def test_put_settings_library_dir_move_keeps_playlists(
    client: TestClient, tmp_path: Path, library: Library, playlists: PlaylistStore
) -> None:
    """Pointing the app at an empty (or wrong) folder must not wipe the user's playlists."""
    old = add_track(library, "old")
    original = library.library_dir
    playlist = playlists.create("Mix")
    playlists.add_tracks(playlist.id, [old.id])
    response = client.put("/api/settings", json={"library_dir": str(tmp_path / "elsewhere")})
    assert response.status_code == 200, response.text
    assert client.get("/api/library").json()["tracks"] == []
    assert playlists.get(playlist.id).track_ids == [old.id]  # kept: shown as "missing" in the UI
    assert client.get("/api/playlists").json()["playlists"][0]["track_ids"] == [old.id]
    # export simply skips the dangling id instead of failing
    assert client.get(f"/api/playlists/{playlist.id}/export.m3u8").text == "#EXTM3U\n"

    # moving back (or copying the files over) makes the same ids resolve again
    response = client.put("/api/settings", json={"library_dir": str(original)})
    assert response.status_code == 200, response.text
    assert [t["id"] for t in client.get("/api/library").json()["tracks"]] == [old.id]
    assert playlists.get(playlist.id).track_ids == [old.id]
    lines = client.get(f"/api/playlists/{playlist.id}/export.m3u8").text.splitlines()
    assert lines[2] == str(library.abs_path(old))


def test_rescan_of_an_empty_folder_keeps_playlists(
    client: TestClient, library: Library, playlists: PlaylistStore, tmp_path: Path
) -> None:
    """An explicit rescan prunes dangling ids, except when the folder holds no audio at all."""
    track = add_track(library, "keep")
    playlist = playlists.create("Mix")
    playlists.add_tracks(playlist.id, [track.id])
    library.abs_path(track).unlink()  # the only file is gone: looks like the wrong folder
    response = client.post("/api/library/rescan")
    assert response.status_code == 200
    assert response.json() == {"changed": 1, "tracks": 0}
    assert playlists.get(playlist.id).track_ids == [track.id]

    # with other audio present, a vanished track really is gone and is pruned
    other = add_track(library, "other")
    playlists.add_tracks(playlist.id, [other.id])
    assert client.post("/api/library/rescan").json() == {"changed": 0, "tracks": 1}
    assert playlists.get(playlist.id).track_ids == [other.id]


def test_put_settings_refuses_to_move_library_mid_download(
    client: TestClient, fake_provider: FakeProvider, tmp_path: Path, tmp_settings: Settings
) -> None:
    fake_provider.gate = threading.Event()
    job = submit(client, track_url("busy"))
    assert fake_provider.download_started.wait(WAIT)
    wait_for_status(client, job["id"], {"downloading"})

    response = client.put("/api/settings", json={"library_dir": str(tmp_path / "later")})
    assert response.status_code == 409
    assert "downloads to finish" in response.json()["detail"]
    assert tmp_settings.library_dir == tmp_path / "lib"
    assert not (tmp_path / "later").exists()  # refused before anything was created on disk
    # other settings can still be changed while a download runs
    assert client.put("/api/settings", json={"embed_cover": False}).status_code == 200

    fake_provider.gate.set()
    wait_terminal(client, job["id"])
    assert (
        client.put("/api/settings", json={"library_dir": str(tmp_path / "later")}).status_code
        == 200
    )


def test_put_settings_failed_save_changes_nothing(
    client: TestClient, tmp_path: Path, tmp_settings: Settings, library: Library, monkeypatch
) -> None:
    def refuse(self: Settings, path: Path | None = None) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Settings, "save", refuse)
    response = client.put(
        "/api/settings", json={"library_dir": str(tmp_path / "new"), "concurrency": 5}
    )
    assert response.status_code == 500
    assert "disk full" in response.json()["detail"]
    # nothing was committed: in-memory settings, the Library and the JobManager still agree
    assert tmp_settings.library_dir == tmp_path / "lib"
    assert tmp_settings.concurrency == 2
    assert library.library_dir == tmp_path / "lib"
    assert client.get("/api/settings").json() == tmp_settings.to_dict()


def test_put_settings_concurrency_resizes_the_running_pool(
    tmp_settings: Settings, library: Library, playlists: PlaylistStore
) -> None:
    fake = FakeProvider()
    fake.gate = threading.Event()  # downloads block until we open it
    settings = replace(tmp_settings, concurrency=1)
    jobs = JobManager(settings, library, providers=FakeRegistry(fake))
    app = server_app.create_app(settings=settings, library=library, playlists=playlists, jobs=jobs)
    try:
        with TestClient(app, base_url=LOOPBACK) as client:
            assert jobs.worker_count == 1
            first = submit(client, track_url("one"))
            second = submit(client, track_url("two"))
            wait_for_status(client, first["id"], {"downloading"})
            assert wait_until(lambda: (get_job(client, second["id"]) or {}).get("track_ref"))
            assert get_job(client, second["id"])["status"] == "queued"  # only one worker

            response = client.put("/api/settings", json={"concurrency": 2})
            assert response.status_code == 200, response.text
            wait_for_status(client, second["id"], {"downloading"})  # a second worker appeared
            assert jobs.worker_count == 2
            assert sorted(fake.downloads) == ["one", "two"]

            fake.gate.set()
            wait_terminal(client, first["id"])
            wait_terminal(client, second["id"])
    finally:
        jobs.stop()


def test_status_passes_live_settings_to_provider_doctor(
    client: TestClient, tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Settings | None] = []

    class Aware:
        name = "aware"
        display_name = "Aware"

        def matches(self, url: str) -> bool:
            return False

        def doctor(self, settings: Settings | None = None) -> list[tuple[bool, str, str]]:
            seen.append(settings)
            return [(True, "settings", "ok")]

    monkeypatch.setattr(server_app, "providers", SimpleNamespace(PROVIDERS=[Aware()]))
    assert client.get("/api/status").json()["providers"][0]["checks"][0]["ok"] is True
    assert seen == [tmp_settings]


# -- jobs --------------------------------------------------------------------------------------


def test_submit_unsupported_link_is_400(client: TestClient) -> None:
    response = client.post("/api/jobs", json={"url": "https://example.com/nothing"})
    assert response.status_code == 400
    assert response.json()["detail"] == "No provider for this link"
    assert client.get("/api/jobs").json()["jobs"] == []


def test_submit_empty_is_400(client: TestClient) -> None:
    response = client.post("/api/jobs", json={"url": "  \n "})
    assert response.status_code == 400
    assert response.json()["detail"] == "Paste a link first"
    assert client.post("/api/jobs", json={}).status_code == 422


def test_submit_multiple_links_with_one_bad_queues_nothing(client: TestClient) -> None:
    text = f"{track_url('a')}\nhttps://example.com/bad\n"
    response = client.post("/api/jobs", json={"url": text})
    assert response.status_code == 400
    assert "https://example.com/bad" in response.json()["detail"]
    assert client.get("/api/jobs").json()["jobs"] == []


def test_job_lifecycle_downloads_into_the_library(
    client: TestClient, library: Library, fake_provider: FakeProvider
) -> None:
    job = submit(client, track_url("abc"))
    assert job["status"] == "queued" and job["provider"] == "fake"
    done = wait_terminal(client, job["id"])
    assert done["status"] == "done", done
    assert done["track"]["id"] == "fake:abc"
    assert done["progress"] == 1.0
    assert fake_provider.downloads == ["abc"]
    tracks = client.get("/api/library").json()["tracks"]
    assert [t["id"] for t in tracks] == ["fake:abc"]
    assert (library.library_dir / tracks[0]["path"]).is_file()
    assert client.get("/api/status").json()["tracks"] == 1


def test_submit_multiple_links_creates_one_job_each(client: TestClient) -> None:
    response = client.post("/api/jobs", json={"url": f"{track_url('m1')} \n {track_url('m2')}\n\n"})
    assert response.status_code == 202
    jobs = response.json()["jobs"]
    assert [j["url"] for j in jobs] == [track_url("m1"), track_url("m2")]
    for job in jobs:
        assert wait_terminal(client, job["id"])["status"] == "done"
    assert {t["id"] for t in client.get("/api/library").json()["tracks"]} == {"fake:m1", "fake:m2"}


def test_playlist_link_becomes_parent_and_children(client: TestClient) -> None:
    parent = submit(client, playlist_url(3))
    assert wait_until(lambda: (get_job(client, parent["id"]) or {}).get("child_count") == 3)
    parent_now = get_job(client, parent["id"])
    assert parent_now is not None
    assert parent_now["status"] == "done"
    assert parent_now["message"] == "Playlist: 3 tracks"
    assert wait_until(lambda: client.app.state.jobs.wait_idle(timeout=0.05))
    jobs = client.get("/api/jobs").json()["jobs"]
    children = [j for j in jobs if j["parent_id"] == parent["id"]]
    assert len(children) == 3
    assert all(j["status"] == "done" for j in children)
    assert len(client.get("/api/library").json()["tracks"]) == 3


def test_already_in_library_is_skipped(client: TestClient, library: Library) -> None:
    add_track(library, "dup")
    job = submit(client, track_url("dup"))
    done = wait_terminal(client, job["id"])
    assert done["status"] == "skipped"
    assert done["message"] == "Already in library"


def test_failed_download_reports_error(client: TestClient, fake_provider: FakeProvider) -> None:
    fake_provider.fail_ids.add("bad")
    job = submit(client, track_url("bad"))
    done = wait_terminal(client, job["id"])
    assert done["status"] == "error"
    assert "refused" in done["error"]


def test_cancel_and_retry(client: TestClient, fake_provider: FakeProvider) -> None:
    fake_provider.gate = threading.Event()  # closed: downloads block until we open it
    job = submit(client, track_url("slow"))
    assert fake_provider.download_started.wait(WAIT)
    wait_for_status(client, job["id"], {"downloading"})

    response = client.post(f"/api/jobs/{job['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["cancelled"] is True
    cancelled = wait_terminal(client, job["id"])
    assert cancelled["status"] == "cancelled"
    assert client.get("/api/library").json()["tracks"] == []

    # cancelling again is a no-op, not an error
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["cancelled"] is False

    fake_provider.gate.set()
    response = client.post(f"/api/jobs/{job['id']}/retry")
    assert response.status_code == 200, response.text
    new_job = response.json()["job"]
    assert new_job["id"] != job["id"]
    assert new_job["url"] == job["url"]
    assert wait_terminal(client, new_job["id"])["status"] == "done"
    assert [t["id"] for t in client.get("/api/library").json()["tracks"]] == ["fake:slow"]

    # a finished job cannot be retried
    response = client.post(f"/api/jobs/{new_job['id']}/retry")
    assert response.status_code == 409
    assert "retried" in response.json()["detail"]


def test_unknown_job_is_404(client: TestClient) -> None:
    assert client.post("/api/jobs/nope/cancel").status_code == 404
    assert client.post("/api/jobs/nope/retry").status_code == 404


def test_delete_finished_jobs(client: TestClient) -> None:
    job = submit(client, track_url("f1"))
    wait_terminal(client, job["id"])
    response = client.delete("/api/jobs/finished")
    assert response.status_code == 200
    assert response.json() == {"removed": 1}
    assert client.get("/api/jobs").json()["jobs"] == []
    assert client.delete("/api/jobs/finished").json() == {"removed": 0}


# -- library -----------------------------------------------------------------------------------


def test_library_list_and_search(client: TestClient, library: Library) -> None:
    add_track(library, "one", artist="Alpha")
    add_track(library, "two", artist="Beta")
    ids = {t["id"] for t in client.get("/api/library").json()["tracks"]}
    assert ids == {"fake:one", "fake:two"}
    found = client.get("/api/library", params={"q": "alpha"}).json()["tracks"]
    assert [t["id"] for t in found] == ["fake:one"]
    assert client.get("/api/library", params={"q": "zzz"}).json()["tracks"] == []


def test_delete_track(client: TestClient, library: Library, playlists: PlaylistStore) -> None:
    keep = add_track(library, "keep")
    gone = add_track(library, "gone")
    playlist = playlists.create("Mix")
    playlists.add_tracks(playlist.id, [keep.id, gone.id])

    response = client.delete(f"/api/library/{gone.id}")
    assert response.status_code == 200
    assert response.json() == {"removed": True, "id": gone.id, "file_deleted": False}
    assert library.abs_path(gone).is_file()  # delete_file defaults to false
    assert playlists.get(playlist.id).track_ids == [keep.id]  # pruned from playlists

    response = client.delete(f"/api/library/{keep.id}", params={"delete_file": "true"})
    assert response.status_code == 200
    assert response.json() == {"removed": True, "id": keep.id, "file_deleted": True}
    assert not library.abs_path(keep).exists()
    assert client.get("/api/library").json()["tracks"] == []
    assert client.delete("/api/library/fake:nope").status_code == 404


def test_delete_track_whose_file_is_in_use_is_409(
    client: TestClient, library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    track = add_track(library, "playing")
    target = library.abs_path(track)
    real_unlink = Path.unlink

    def locked(self: Path, missing_ok: bool = False) -> None:
        if self == target:  # only the "playing" file is locked; index .tmp files etc. still work
            raise PermissionError(32, "The process cannot access the file because it is in use")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)
    response = client.delete(f"/api/library/{track.id}", params={"delete_file": "true"})
    assert response.status_code == 409
    assert "in use" in response.json()["detail"]
    # the entry is kept, so the track does not silently reappear at the next rescan
    assert [t["id"] for t in client.get("/api/library").json()["tracks"]] == [track.id]


def test_rescan(client: TestClient, library: Library) -> None:
    write_tiny_mp3(
        library.library_dir / "New Artist - New Song.mp3", artist="New Artist", title="New Song"
    )
    response = client.post("/api/library/rescan")
    assert response.status_code == 200
    assert response.json() == {"changed": 1, "tracks": 1}
    tracks = client.get("/api/library").json()["tracks"]
    assert tracks[0]["title"] == "New Song" and tracks[0]["id"].startswith("local:")
    assert client.post("/api/library/rescan").json() == {"changed": 0, "tracks": 1}


def test_rescan_with_unreachable_folder_is_409_and_keeps_everything(
    client: TestClient, library: Library, playlists: PlaylistStore, tmp_path: Path
) -> None:
    track = add_track(library, "keep")
    playlist = playlists.create("Mix")
    playlists.add_tracks(playlist.id, [track.id])
    library.set_library_dir(tmp_path / "unplugged-drive")  # e.g. a USB drive pulled out

    response = client.post("/api/library/rescan")
    assert response.status_code == 409
    assert "not reachable" in response.json()["detail"]
    assert [t["id"] for t in client.get("/api/library").json()["tracks"]] == [track.id]
    assert playlists.get(playlist.id).track_ids == [track.id]  # not pruned


def test_cover(client: TestClient, library: Library) -> None:
    with_art = add_track(library, "art", cover=True)
    without = add_track(library, "plain")
    response = client.get(f"/api/library/{with_art.id}/cover")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.content == PNG_BYTES
    response = client.get(f"/api/library/{without.id}/cover")
    assert response.status_code == 404
    assert "cover" in response.json()["detail"].lower()
    assert client.get("/api/library/fake:missing/cover").status_code == 404


def test_cover_never_serves_a_non_image_content_type(client: TestClient, library: Library) -> None:
    """A copied-in file can carry any MIME in its APIC frame; it must never run as HTML here."""
    track = add_track(library, "evil")
    tags = ID3(library.abs_path(track))
    tags.add(
        APIC(encoding=3, mime="text/html", type=3, desc="Cover", data=b"<script>alert(1)</script>")
    )
    tags.save(library.abs_path(track))
    response = client.get(f"/api/library/{track.id}/cover")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert response.headers["x-content-type-options"] == "nosniff"
    # a wrong MIME on a real image is corrected from the bytes
    tags = ID3(library.abs_path(track))
    tags.delall("APIC")
    tags.add(
        APIC(encoding=3, mime="application/octet-stream", type=3, desc="Cover", data=PNG_BYTES)
    )
    tags.save(library.abs_path(track))
    response = client.get(f"/api/library/{track.id}/cover")
    assert response.headers["content-type"] == "image/png"


def test_open_library_folder(
    client: TestClient, tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(server_app, "open_folder", lambda path: opened.append(Path(path)))
    response = client.post("/api/library/open")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert opened == [tmp_settings.library_dir]


# -- media -------------------------------------------------------------------------------------


def test_media_full_and_range_requests(client: TestClient, library: Library) -> None:
    track = add_track(library, "play")
    size = library.abs_path(track).stat().st_size
    response = client.get(f"/media/{track.id}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert len(response.content) == size
    assert response.headers.get("accept-ranges") == "bytes"
    assert "content-disposition" not in response.headers  # inline for <audio>, not a download

    response = client.get(f"/media/{track.id}", headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 0-99/{size}"
    assert len(response.content) == 100

    response = client.head(f"/media/{track.id}")  # media prefetch / external players probe
    assert response.status_code == 200
    assert response.headers["content-length"] == str(size)
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b""
    assert client.head("/media/fake:unknown").status_code == 404


def test_media_404s(client: TestClient, library: Library) -> None:
    assert client.get("/media/fake:unknown").status_code == 404
    assert client.get("/media/..%2F..%2Fsecret.mp3").status_code == 404
    track = add_track(library, "vanished")
    library.abs_path(track).unlink()
    response = client.get(f"/media/{track.id}")
    assert response.status_code == 404
    assert "missing" in response.json()["detail"]


# -- playlists ---------------------------------------------------------------------------------


def create_playlist(client: TestClient, name: str) -> dict[str, Any]:
    response = client.post("/api/playlists", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["playlist"]


def test_playlist_crud(client: TestClient) -> None:
    assert client.get("/api/playlists").json() == {"playlists": []}
    created = create_playlist(client, "  Road trip ")
    assert created["name"] == "Road trip" and created["track_ids"] == []
    assert client.post("/api/playlists", json={"name": "   "}).status_code == 400
    assert client.post("/api/playlists", json={}).status_code == 422

    listed = client.get("/api/playlists").json()["playlists"]
    assert [p["id"] for p in listed] == [created["id"]]

    response = client.patch(f"/api/playlists/{created['id']}", json={"name": "Night drive"})
    assert response.status_code == 200
    assert response.json()["playlist"]["name"] == "Night drive"
    assert client.patch(f"/api/playlists/{created['id']}", json={"name": ""}).status_code == 400
    assert client.patch("/api/playlists/nope", json={"name": "x"}).status_code == 404

    response = client.delete(f"/api/playlists/{created['id']}")
    assert response.status_code == 200
    assert response.json()["removed"] is True
    assert client.delete(f"/api/playlists/{created['id']}").status_code == 404
    assert client.get("/api/playlists").json() == {"playlists": []}


def test_playlist_tracks_add_remove_reorder(client: TestClient, library: Library) -> None:
    a, b, c = (add_track(library, sid) for sid in ("a", "b", "c"))
    playlist = create_playlist(client, "Mix")
    pid = playlist["id"]

    response = client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [a.id, b.id, a.id]})
    assert response.status_code == 200
    assert response.json()["playlist"]["track_ids"] == [a.id, b.id]
    response = client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [c.id, "fake:ghost"]})
    assert response.status_code == 400
    assert "fake:ghost" in response.json()["detail"]
    assert client.post("/api/playlists/nope/tracks", json={"track_ids": [a.id]}).status_code == 404
    client.post(f"/api/playlists/{pid}/tracks", json={"track_ids": [c.id]})

    response = client.patch(f"/api/playlists/{pid}", json={"track_ids": [c.id, a.id, b.id]})
    assert response.status_code == 200
    assert response.json()["playlist"]["track_ids"] == [c.id, a.id, b.id]
    response = client.patch(f"/api/playlists/{pid}", json={"track_ids": [a.id, b.id]})
    assert response.status_code == 400
    assert "exactly the tracks" in response.json()["detail"]

    # name + order in one request is all-or-nothing
    response = client.patch(f"/api/playlists/{pid}", json={"name": "Renamed", "track_ids": ["x"]})
    assert response.status_code == 400
    assert client.get("/api/playlists").json()["playlists"][0]["name"] == "Mix"
    response = client.patch(
        f"/api/playlists/{pid}", json={"name": " ", "track_ids": [b.id, a.id, c.id]}
    )
    assert response.status_code == 400
    assert client.get("/api/playlists").json()["playlists"][0]["track_ids"] == [c.id, a.id, b.id]
    response = client.patch(
        f"/api/playlists/{pid}", json={"name": "Both", "track_ids": [c.id, a.id, b.id]}
    )
    assert response.status_code == 200
    assert response.json()["playlist"]["name"] == "Both"
    assert response.json()["playlist"]["track_ids"] == [c.id, a.id, b.id]

    response = client.delete(f"/api/playlists/{pid}/tracks/{a.id}")
    assert response.status_code == 200
    assert response.json()["playlist"]["track_ids"] == [c.id, b.id]
    assert client.delete(f"/api/playlists/{pid}/tracks/{a.id}").status_code == 200  # idempotent
    assert client.delete(f"/api/playlists/nope/tracks/{a.id}").status_code == 404


def test_playlist_export_writes_relative_m3u8(
    client: TestClient, library: Library, tmp_settings: Settings
) -> None:
    a = add_track(library, "x1")
    b = add_track(library, "x2")
    playlist = create_playlist(client, 'Late <night> drive: "vol. 2"')
    client.post(f"/api/playlists/{playlist['id']}/tracks", json={"track_ids": [a.id, b.id]})

    response = client.post(f"/api/playlists/{playlist['id']}/export")
    assert response.status_code == 200, response.text
    out = Path(response.json()["path"])
    assert out == tmp_settings.playlists_export_dir / "Late night drive vol. 2.m3u8"
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#EXTINF:0,Fake Artist - Track x1"
    assert lines[2] == "../Fake Artist - Track x1.mp3"  # relative to the Playlists folder
    assert lines[4] == "../Fake Artist - Track x2.mp3"
    assert client.post("/api/playlists/nope/export").status_code == 404


def test_playlist_export_download_uses_absolute_paths(client: TestClient, library: Library) -> None:
    a = add_track(library, "d1")
    playlist = create_playlist(client, "For VLC")
    client.post(f"/api/playlists/{playlist['id']}/tracks", json={"track_ids": [a.id]})

    response = client.get(f"/api/playlists/{playlist['id']}/export.m3u8")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/x-mpegurl")
    disposition = unquote(response.headers["content-disposition"])
    assert disposition.startswith("attachment")
    assert "For VLC.m3u8" in disposition
    lines = response.text.splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[2] == str(library.abs_path(a))
    assert client.get("/api/playlists/nope/export.m3u8").status_code == 404
    # built in memory: nothing is left behind in the app data folder
    assert not (app_data_dir() / "exports").exists()


# -- app construction & serve helpers ----------------------------------------------------------


def test_create_app_builds_missing_objects(tmp_settings: Settings) -> None:
    app = server_app.create_app(tmp_settings)
    assert app.state.settings is tmp_settings
    assert isinstance(app.state.library, Library)
    assert isinstance(app.state.playlists, PlaylistStore)
    jobs = app.state.jobs
    assert jobs.settings is tmp_settings and jobs.library is app.state.library
    assert not jobs.running
    with TestClient(app, base_url=LOOPBACK) as test_client:
        assert jobs.running  # lifespan started the JobManager
        assert test_client.get("/api/status").json()["tracks"] == 0
    assert not jobs.running  # ...and stopped it on shutdown


# -- loopback-only guard -----------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8765", "localhost", "[::1]:8765"])
def test_loopback_hosts_are_accepted(client: TestClient, host: str) -> None:
    assert client.get("/api/library", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["attacker.example", "evil.example:8765", "testserver", ""])
def test_foreign_host_is_rejected(client: TestClient, library: Library, host: str) -> None:
    add_track(library, "secret")
    response = client.get("/api/library", headers={"Host": host})
    assert response.status_code == 400
    assert "127.0.0.1" in response.json()["detail"]
    assert "secret" not in response.text
    # a DNS-rebinding page cannot drive the destructive endpoints either
    assert client.post("/api/library/rescan", headers={"Host": host}).status_code == 400
    assert (
        client.delete(
            "/api/library/fake:secret", headers={"Host": host}, params={"delete_file": "true"}
        ).status_code
        == 400
    )
    assert library.get("fake:secret") is not None


def test_cross_site_origin_is_rejected_for_unsafe_methods(
    client: TestClient,
    library: Library,
    playlists: PlaylistStore,
    tmp_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(server_app, "open_folder", lambda path: opened.append(Path(path)))
    track = add_track(library, "guarded")
    pid = create_playlist(client, "Mine")["id"]
    for origin in ("https://evil.example", "http://127.0.0.1.evil.example", "null"):
        headers = {"Origin": origin}
        response = client.post("/api/library/open", headers=headers)
        assert response.status_code == 403, origin
        assert response.json() == {"detail": "Cross-site requests are not allowed."}
        assert client.post("/api/library/rescan", headers=headers).status_code == 403
        # every non-safe method is covered, not only POST
        assert (
            client.put("/api/settings", json={"concurrency": 1}, headers=headers).status_code == 403
        )
        assert (
            client.patch(f"/api/playlists/{pid}", json={"name": "x"}, headers=headers).status_code
            == 403
        )
        assert (
            client.delete(
                f"/api/library/{track.id}", params={"delete_file": "true"}, headers=headers
            ).status_code
            == 403
        )
        assert client.delete(f"/api/playlists/{pid}", headers=headers).status_code == 403
    assert opened == []
    assert tmp_settings.concurrency == 2
    assert library.get(track.id) is not None and library.abs_path(track).is_file()
    assert playlists.get(pid) is not None and playlists.get(pid).name == "Mine"
    # the app's own pages send a loopback Origin (or none at all); both keep working
    for origin in ("http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"):
        assert client.post("/api/library/open", headers={"Origin": origin}).status_code == 200
    assert client.post("/api/library/open").status_code == 200
    # GET with a foreign Origin is harmless (the browser blocks reading it) and stays allowed
    assert client.get("/api/status", headers={"Origin": "https://evil.example"}).status_code == 200


def test_any_host_can_be_allowed_explicitly(
    tmp_settings: Settings, library: Library, playlists: PlaylistStore
) -> None:
    jobs = JobManager(tmp_settings, library, providers=FakeRegistry(FakeProvider()))
    app = server_app.create_app(
        settings=tmp_settings, library=library, playlists=playlists, jobs=jobs, allowed_hosts=["*"]
    )
    try:
        with TestClient(app, base_url="http://192.168.1.20:8765") as test_client:
            assert test_client.get("/api/status").status_code == 200
    finally:
        jobs.stop()


def test_unhandled_error_is_a_json_500(
    tmp_settings: Settings, library: Library, playlists: PlaylistStore, monkeypatch
) -> None:
    def boom() -> int:
        raise RuntimeError("index exploded")

    monkeypatch.setattr(library, "rescan", boom)
    jobs = JobManager(tmp_settings, library, providers=FakeRegistry(FakeProvider()))
    app = server_app.create_app(
        settings=tmp_settings, library=library, playlists=playlists, jobs=jobs
    )
    try:
        with TestClient(app, base_url=LOOPBACK, raise_server_exceptions=False) as test_client:
            response = test_client.post("/api/library/rescan")
            assert response.status_code == 500
            assert response.headers["content-type"].startswith("application/json")
            assert response.json() == {
                "detail": "Something went wrong on the server. See app.log for details."
            }
            assert "index exploded" not in response.text  # nothing internal leaks
    finally:
        jobs.stop()


def test_split_links_and_safe_names() -> None:
    assert server_app.split_links(" a\nb \r\n\n a\tc ") == ["a", "b", "c"]
    assert server_app.split_links("") == []
    assert server_app.safe_playlist_filename('  Late <night> / "drive" ...') == "Late night drive"
    assert server_app.safe_playlist_filename("///") == "playlist"
    assert server_app.media_type_for(Path("x.MP3")) == "audio/mpeg"
    assert server_app.media_type_for(Path("x.m4a")) == "audio/mp4"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("con", "_con"),
        ("CON", "_CON"),
        ("aux.old", "_aux.old"),
        ("COM1", "_COM1"),
        ("lpt9", "_lpt9"),
        ("Console", "Console"),
        ("con artist", "con artist"),
        ("NUL.", "_NUL"),
    ],
)
def test_safe_playlist_filename_avoids_windows_device_names(name: str, expected: str) -> None:
    assert server_app.safe_playlist_filename(name) == expected


def test_playlist_export_with_a_reserved_name_writes_a_file(
    client: TestClient, library: Library, tmp_settings: Settings
) -> None:
    a = add_track(library, "r1")
    playlist = create_playlist(client, "con")
    client.post(f"/api/playlists/{playlist['id']}/tracks", json={"track_ids": [a.id]})
    response = client.post(f"/api/playlists/{playlist['id']}/export")
    assert response.status_code == 200, response.text
    out = Path(response.json()["path"])
    assert out == tmp_settings.playlists_export_dir / "_con.m3u8"
    assert out.read_text(encoding="utf-8").startswith("#EXTM3U")


def test_pick_port_skips_a_busy_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        chosen = server_app.pick_port("127.0.0.1", port)
        assert chosen != port
        assert port < chosen <= port + 10
    assert server_app.pick_port("127.0.0.1", port) == port  # free again


def _ipv6_loopback_socket() -> socket.socket:
    if not socket.has_ipv6:
        pytest.skip("IPv6 is not available on this machine")
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        sock.bind(("::1", 0))
    except OSError:
        sock.close()
        pytest.skip("IPv6 loopback is not available on this machine")
    return sock


@pytest.mark.parametrize("host", ["::1", "[::1]", "::"])
def test_pick_port_handles_ipv6_hosts(host: str) -> None:
    """`up serve --host ::1` used to die with 'No free port' because the probe was IPv4-only."""
    with _ipv6_loopback_socket() as free:
        port = free.getsockname()[1]
    assert server_app.pick_port(host, port) == port


def test_pick_port_sees_a_busy_ipv6_port() -> None:
    with _ipv6_loopback_socket() as busy:
        busy.listen(1)
        port = busy.getsockname()[1]
        assert not server_app.port_is_free("::1", port)
        chosen = server_app.pick_port("::1", port)
        assert port < chosen <= port + 10
        # "localhost" resolves to ::1 as well as 127.0.0.1 on most machines; when it does, a
        # busy ::1 port must count as busy for "localhost" too (uvicorn binds both).
        resolved = {
            info[4][0] for info in socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM)
        }
        if "::1" in resolved:
            assert not server_app.port_is_free("localhost", port)
    assert server_app.pick_port("::1", port) == port


def test_port_is_free_rejects_unresolvable_hosts() -> None:
    assert server_app.port_is_free("no-such-host.invalid", 8765) is False


def test_pick_port_gives_up_after_the_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_app, "port_is_free", lambda host, port: False)
    with pytest.raises(OSError, match="No free port"):
        server_app.pick_port("127.0.0.1", 8765)


def test_open_browser_later(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(server_app.webbrowser, "open", lambda url: opened.append(url))
    timer = server_app.open_browser_later("http://127.0.0.1:1/", delay=0.0)
    timer.join(WAIT)
    assert opened == ["http://127.0.0.1:1/"]


def test_configure_logging_writes_app_log(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ultimate_playlist.config import app_data_dir

    monkeypatch.setattr(server_app, "_logging_configured", False)
    root = logging.getLogger()
    before = list(root.handlers)
    level_before = root.level  # install_handlers() lowers it; put it back afterwards
    log_path = server_app.configure_logging()
    added = [h for h in root.handlers if h not in before]
    try:
        assert log_path == app_data_dir() / "app.log"
        assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in added)
        logging.getLogger("ultimate_playlist.test").info("hello from the test")
        for handler in added:
            handler.flush()
        assert "hello from the test" in log_path.read_text(encoding="utf-8")
        assert server_app.configure_logging() == log_path  # idempotent: no second set of handlers
        assert len(root.handlers) == len(before) + len(added)
    finally:
        for handler in added:
            root.removeHandler(handler)
            handler.close()
        root.setLevel(level_before)
        monkeypatch.setattr(server_app, "_logging_configured", False)


def test_serve_wires_everything(tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    calls: dict[str, Any] = {}
    monkeypatch.setattr(server_app, "configure_logging", lambda: Path("app.log"))
    monkeypatch.setattr(server_app, "pick_port", lambda host, port: port + 1)
    monkeypatch.setattr(
        server_app, "open_browser_later", lambda url: calls.setdefault("browser", url)
    )
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kwargs: calls.setdefault("run", (app, kwargs))
    )
    server_app.serve(tmp_settings, port=8765, open_browser=True)
    app, kwargs = calls["run"]
    assert app.state.settings is tmp_settings
    assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == 8766
    assert calls["browser"] == "http://127.0.0.1:8766/"


def test_serve_on_all_interfaces_opens_the_browser_on_loopback(
    tmp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    calls: dict[str, Any] = {}
    monkeypatch.setattr(server_app, "configure_logging", lambda: Path("app.log"))
    monkeypatch.setattr(server_app, "pick_port", lambda host, port: port)
    monkeypatch.setattr(
        server_app, "open_browser_later", lambda url: calls.setdefault("browser", url)
    )
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kwargs: calls.setdefault("run", (app, kwargs))
    )
    server_app.serve(tmp_settings, host="0.0.0.0", port=9000, open_browser=True)
    app, kwargs = calls["run"]
    assert kwargs["host"] == "0.0.0.0"  # uvicorn still binds where the user asked
    assert (
        calls["browser"] == "http://127.0.0.1:9000/"
    )  # 0.0.0.0 is not something a browser can open
    with TestClient(app, base_url="http://192.168.1.20:9000") as test_client:
        assert (
            test_client.get("/api/status").status_code == 200
        )  # LAN hosts are accepted on purpose
