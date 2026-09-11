"""Settings persistence and the app data directory (offline, isolated via ULTIMATE_PLAYLIST_HOME)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ultimate_playlist import config
from ultimate_playlist.config import APP_NAME, ENV_HOME, Settings, app_data_dir


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv(ENV_HOME, str(path))
    return path


def test_app_name() -> None:
    assert APP_NAME == "ultimate-playlist"
    assert ENV_HOME == "ULTIMATE_PLAYLIST_HOME"


def test_app_data_dir_uses_env_and_creates(home: Path) -> None:
    assert not home.exists()
    assert app_data_dir() == home
    assert home.is_dir()


def test_app_data_dir_expands_env_and_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UP_TEST_BASE", str(tmp_path))
    monkeypatch.setenv(ENV_HOME, "$UP_TEST_BASE/expanded")
    assert app_data_dir() == tmp_path / "expanded"
    monkeypatch.setenv(ENV_HOME, "~/.ultimate-playlist-test-dir-do-not-create")
    monkeypatch.setattr(config.Path, "mkdir", lambda self, *a, **k: None)
    assert app_data_dir() == Path.home() / ".ultimate-playlist-test-dir-do-not-create"


def test_app_data_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_HOME, raising=False)
    monkeypatch.setattr(config.Path, "mkdir", lambda self, *a, **k: None)
    assert app_data_dir() == Path.home() / ".ultimate-playlist"


def test_defaults() -> None:
    s = Settings()
    assert s.library_dir == Path.home() / "Music" / "Ultimate Playlist"
    assert s.audio_format == "mp3"
    assert s.audio_quality == "0"
    assert s.ffmpeg_path is None
    assert s.concurrency == 2
    assert s.embed_cover is True
    assert s.js_runtimes == ["deno", "node"]


def test_derived_paths(home: Path, tmp_path: Path) -> None:
    s = Settings(library_dir=tmp_path / "lib")
    assert s.incoming_dir == tmp_path / "lib" / ".incoming"
    assert s.playlists_export_dir == tmp_path / "lib" / "Playlists"
    assert s.index_path == home / "library.json"
    assert s.playlists_path == home / "playlists.json"
    assert Settings.default_path() == home / "config.json"


def test_library_dir_expands_tilde_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UP_TEST_MUSIC", str(tmp_path))
    assert Settings(library_dir="$UP_TEST_MUSIC/music").library_dir == tmp_path / "music"
    assert Settings(library_dir="~/tunes").library_dir == Path.home() / "tunes"
    assert Settings(library_dir=str(tmp_path / "plain")).library_dir == tmp_path / "plain"
    assert Settings(ffmpeg_path="$UP_TEST_MUSIC/ffmpeg.exe").ffmpeg_path == str(
        tmp_path / "ffmpeg.exe"
    )
    assert Settings(ffmpeg_path="").ffmpeg_path is None


def test_concurrency_is_clamped() -> None:
    assert Settings(concurrency=0).concurrency == 1
    assert Settings(concurrency=99).concurrency == 6
    assert Settings(concurrency="3").concurrency == 3  # type: ignore[arg-type]


def test_round_trip_explicit_path(tmp_path: Path) -> None:
    original = Settings(
        library_dir=tmp_path / "lib",
        audio_quality="3",
        ffmpeg_path=str(tmp_path / "ffmpeg.exe"),
        concurrency=4,
        embed_cover=False,
        js_runtimes=["node"],
    )
    path = tmp_path / "cfg" / "config.json"
    original.save(path)
    assert path.is_file()
    assert not path.with_suffix(".json.tmp").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["library_dir"] == str(tmp_path / "lib")
    assert data["concurrency"] == 4
    assert data["embed_cover"] is False
    assert data["js_runtimes"] == ["node"]
    assert Settings.load(path) == original


def test_round_trip_default_path(home: Path, tmp_path: Path) -> None:
    original = Settings(library_dir=tmp_path / "lib", concurrency=5)
    original.save()
    assert (home / "config.json").is_file()
    loaded = Settings.load()
    assert loaded == original
    assert loaded.library_dir == tmp_path / "lib"


def test_load_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert Settings.load(tmp_path / "missing.json") == Settings()


def test_load_corrupt_json_gives_defaults(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ this is not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="ultimate_playlist.config"):
        loaded = Settings.load(path)
    assert loaded == Settings()
    assert any("config" in rec.getMessage().lower() for rec in caplog.records)
    assert path.read_text(encoding="utf-8") == "{ this is not json"  # never rewritten on load


def test_load_non_object_json_gives_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert Settings.load(path) == Settings()
    path.write_text("", encoding="utf-8")
    assert Settings.load(path) == Settings()


def test_load_bad_values_gives_defaults(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"concurrency": "lots"}), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="ultimate_playlist.config"):
        assert Settings.load(path) == Settings()


def test_from_dict_ignores_unknown_and_coerces(tmp_path: Path) -> None:
    s = Settings.from_dict(
        {
            "library_dir": str(tmp_path / "lib"),
            "unknown_key": 123,
            "concurrency": 3,
            "js_runtimes": ["node"],
        }
    )
    assert isinstance(s.library_dir, Path)
    assert s.library_dir == tmp_path / "lib"
    assert s.concurrency == 3
    assert s.js_runtimes == ["node"]
    assert not hasattr(s, "unknown_key")


def test_from_dict_bad_js_runtimes() -> None:
    assert Settings.from_dict({"js_runtimes": "deno"}).js_runtimes == ["deno", "node"]
    assert Settings.from_dict({"js_runtimes": [1, 2]}).js_runtimes == ["deno", "node"]
    assert Settings.from_dict({}) == Settings()


def test_to_dict_is_json_safe(tmp_path: Path) -> None:
    s = Settings(library_dir=tmp_path / "lib")
    data = s.to_dict()
    assert isinstance(data["library_dir"], str)
    assert json.loads(json.dumps(data)) == data
    assert set(data) == {
        "library_dir",
        "audio_format",
        "audio_quality",
        "ffmpeg_path",
        "concurrency",
        "embed_cover",
        "js_runtimes",
    }
    assert Settings.from_dict(data) == s


def test_save_is_atomic_and_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    Settings(library_dir=tmp_path / "a").save(path)
    Settings(library_dir=tmp_path / "b").save(path)
    assert Settings.load(path).library_dir == tmp_path / "b"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json"]
