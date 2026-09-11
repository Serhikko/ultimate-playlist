"""PlaylistStore CRUD, ordering rules, pruning and M3U8 export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fake_provider import write_tiny_mp3

from ultimate_playlist.config import Settings
from ultimate_playlist.library import Library
from ultimate_playlist.models import Track
from ultimate_playlist.playlists import PlaylistNotFound, PlaylistStore, export_m3u8, m3u8_text


def add_track(library: Library, source_id: str, duration: float | None = 0.3) -> Track:
    rel = f"Fake Artist - Track {source_id}.mp3"
    write_tiny_mp3(
        library.library_dir / rel, title=f"Track {source_id}", track_id=f"fake:{source_id}"
    )
    track = Track(
        id=f"fake:{source_id}",
        provider="fake",
        source_id=source_id,
        source_url=f"https://fake.test/track/{source_id}",
        title=f"Track {source_id}",
        artist="Fake Artist",
        path=rel,
        duration=duration,
    )
    library.add(track)
    return track


# -- CRUD ------------------------------------------------------------------------------------


def test_create_strips_name_and_persists(playlists: PlaylistStore) -> None:
    playlist = playlists.create("  Road trip  ")

    assert playlist.name == "Road trip"
    assert playlist.track_ids == []
    assert playlists.get(playlist.id) == playlist
    assert playlists.all() == [playlist]

    data = json.loads(playlists.path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["playlists"][0]["name"] == "Road trip"
    assert not playlists.path.with_name(playlists.path.name + ".tmp").exists()


@pytest.mark.parametrize("name", ["", "   ", "\n\t"])
def test_create_rejects_empty_names(playlists: PlaylistStore, name: str) -> None:
    with pytest.raises(ValueError):
        playlists.create(name)
    assert playlists.all() == []


def test_duplicate_names_are_allowed(playlists: PlaylistStore) -> None:
    first = playlists.create("Mix")
    second = playlists.create("Mix")
    assert first.id != second.id
    assert [p.name for p in playlists.all()] == ["Mix", "Mix"]


def test_rename(playlists: PlaylistStore) -> None:
    playlist = playlists.create("Old")
    before = playlist.updated_at

    renamed = playlists.rename(playlist.id, "  New  ")

    assert renamed.name == "New"
    assert renamed.updated_at >= before
    assert PlaylistStore(playlists.path).get(playlist.id).name == "New"
    with pytest.raises(ValueError):
        playlists.rename(playlist.id, " ")


def test_delete(playlists: PlaylistStore) -> None:
    playlist = playlists.create("Bye")
    assert playlists.delete(playlist.id) is True
    assert playlists.get(playlist.id) is None
    assert playlists.delete(playlist.id) is False
    assert PlaylistStore(playlists.path).all() == []


def test_unknown_playlist_raises_a_keyerror(playlists: PlaylistStore) -> None:
    for call in (
        lambda: playlists.rename("nope", "x"),
        lambda: playlists.add_tracks("nope", ["a"]),
        lambda: playlists.remove_track("nope", "a"),
        lambda: playlists.set_order("nope", []),
    ):
        with pytest.raises(KeyError) as excinfo:
            call()
        assert isinstance(excinfo.value, PlaylistNotFound)
        assert str(excinfo.value) == "Playlist not found: nope"


def test_add_tracks_appends_and_skips_duplicates(playlists: PlaylistStore) -> None:
    playlist = playlists.create("Mix")

    playlists.add_tracks(playlist.id, ["a", "b"])
    result = playlists.add_tracks(playlist.id, ["b", "c", "a", "c"])

    assert result.track_ids == ["a", "b", "c"]
    assert PlaylistStore(playlists.path).get(playlist.id).track_ids == ["a", "b", "c"]


def test_remove_track(playlists: PlaylistStore) -> None:
    playlist = playlists.create("Mix")
    playlists.add_tracks(playlist.id, ["a", "b", "c"])

    assert playlists.remove_track(playlist.id, "b").track_ids == ["a", "c"]
    assert playlists.remove_track(playlist.id, "zzz").track_ids == ["a", "c"]  # no-op
    assert PlaylistStore(playlists.path).get(playlist.id).track_ids == ["a", "c"]


def test_set_order_accepts_a_permutation(playlists: PlaylistStore) -> None:
    playlist = playlists.create("Mix")
    playlists.add_tracks(playlist.id, ["a", "b", "c"])

    assert playlists.set_order(playlist.id, ["c", "a", "b"]).track_ids == ["c", "a", "b"]
    assert PlaylistStore(playlists.path).get(playlist.id).track_ids == ["c", "a", "b"]


@pytest.mark.parametrize(
    "bad_order",
    [
        ["a", "b"],  # missing one
        ["a", "b", "c", "d"],  # extra id
        ["a", "a", "b"],  # duplicate instead of c
        ["a", "b", "x"],  # unknown id
        [],
    ],
)
def test_set_order_rejects_non_permutations(playlists: PlaylistStore, bad_order: list[str]) -> None:
    playlist = playlists.create("Mix")
    playlists.add_tracks(playlist.id, ["a", "b", "c"])

    with pytest.raises(ValueError):
        playlists.set_order(playlist.id, bad_order)
    assert playlists.get(playlist.id).track_ids == ["a", "b", "c"]


def test_prune_drops_dangling_ids(playlists: PlaylistStore) -> None:
    one = playlists.create("One")
    two = playlists.create("Two")
    playlists.add_tracks(one.id, ["a", "gone", "b"])
    playlists.add_tracks(two.id, ["gone", "also-gone"])

    assert playlists.prune({"a", "b"}) == 3
    assert playlists.get(one.id).track_ids == ["a", "b"]
    assert playlists.get(two.id).track_ids == []
    assert playlists.prune({"a", "b"}) == 0
    assert PlaylistStore(playlists.path).get(one.id).track_ids == ["a", "b"]


def test_corrupt_store_starts_empty(tmp_settings: Settings) -> None:
    path = tmp_settings.playlists_path
    path.write_text("[not what we expect", encoding="utf-8")
    store = PlaylistStore(path)
    assert store.all() == []
    assert path.with_name(path.name + ".corrupt").exists()
    store.create("Works again")
    assert PlaylistStore(path).all()[0].name == "Works again"


def test_missing_store_file_starts_empty(tmp_path: Path) -> None:
    store = PlaylistStore(tmp_path / "nested" / "playlists.json")
    assert store.all() == []
    store.create("First")
    assert (tmp_path / "nested" / "playlists.json").exists()


# -- M3U8 export -----------------------------------------------------------------------------


def test_export_m3u8_relative_paths_skip_missing_tracks(
    library: Library, playlists: PlaylistStore, tmp_path: Path
) -> None:
    add_track(library, "a", duration=200.7)
    add_track(library, "b", duration=None)
    playlist = playlists.create("Mix")
    playlists.add_tracks(playlist.id, ["fake:a", "fake:missing", "fake:b"])
    out = library.library_dir / "Playlists" / "Mix.m3u8"

    result = export_m3u8(playlist, library, out, relative_to=out.parent)

    assert result == out
    lines = out.read_text(encoding="utf-8").split("\n")
    assert lines == [
        "#EXTM3U",
        "#EXTINF:200,Fake Artist - Track a",
        "../Fake Artist - Track a.mp3",
        "#EXTINF:-1,Fake Artist - Track b",
        "../Fake Artist - Track b.mp3",
        "",
    ]
    assert not out.with_name(out.name + ".tmp").exists()


def test_export_m3u8_absolute_paths(
    library: Library, playlists: PlaylistStore, tmp_path: Path
) -> None:
    track = add_track(library, "a", duration=59.9)
    playlist = playlists.create("Abs")
    playlists.add_tracks(playlist.id, ["fake:a"])
    out = tmp_path / "exports" / "abs.m3u8"

    export_m3u8(playlist, library, out)

    lines = out.read_text(encoding="utf-8").split("\n")
    assert lines == [
        "#EXTM3U",
        "#EXTINF:59,Fake Artist - Track a",
        str(library.library_dir / track.path),
        "",
    ]
    assert Path(lines[2]).is_absolute()


def test_export_m3u8_unicode_and_empty_playlist(
    library: Library, playlists: PlaylistStore, tmp_path: Path
) -> None:
    rel = "Björk - Jóga.mp3"
    write_tiny_mp3(library.library_dir / rel, artist="Björk", title="Jóga")
    library.add(
        Track(
            id="fake:u",
            provider="fake",
            source_id="u",
            source_url="",
            title="Jóga",
            artist="Björk",
            path=rel,
            duration=3.2,
        )
    )
    playlist = playlists.create("Ünïcode")
    playlists.add_tracks(playlist.id, ["fake:u"])
    out = tmp_path / "u.m3u8"
    export_m3u8(playlist, library, out, relative_to=library.library_dir)
    assert out.read_bytes().decode("utf-8") == (
        "#EXTM3U\n#EXTINF:3,Björk - Jóga\nBjörk - Jóga.mp3\n"
    )

    empty = playlists.create("Empty")
    export_m3u8(empty, library, tmp_path / "empty.m3u8")
    assert (tmp_path / "empty.m3u8").read_text(encoding="utf-8") == "#EXTM3U\n"


def test_m3u8_text_matches_the_written_file(
    library: Library, playlists: PlaylistStore, tmp_path: Path
) -> None:
    track = add_track(library, "a", duration=12.0)
    playlist = playlists.create("Mem")
    playlists.add_tracks(playlist.id, ["fake:a", "fake:missing"])
    out = tmp_path / "mem.m3u8"
    export_m3u8(playlist, library, out)
    assert m3u8_text(playlist, library) == out.read_text(encoding="utf-8")
    assert m3u8_text(playlist, library, relative_to=library.library_dir) == (
        f"#EXTM3U\n#EXTINF:12,Fake Artist - Track a\n{track.path}\n"
    )
    assert m3u8_text(playlists.create("Empty"), library) == "#EXTM3U\n"
