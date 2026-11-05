"""Library folders: real directories under the library folder, moving tracks between them, the
folder a download goes to, the /api/folders and /api/library/move routes and `up add --folder`."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fake_provider import FakeProvider, FakeRegistry, playlist_url, track_url, write_tiny_mp3

from ultimate_playlist import library as library_mod
from ultimate_playlist.config import Settings
from ultimate_playlist.downloader import JobManager
from ultimate_playlist.library import (
    Library,
    LibraryFolderError,
    LibraryUnavailable,
    clean_folder_name,
    folder_of,
    media_of,
)
from ultimate_playlist.models import JobStatus, Track

WAIT = 15.0


def add_file(library: Library, rel: str, track_id: str | None = None) -> Track:
    """A real tagged MP3 at `rel` (inside the library folder), indexed."""
    path = library.library_dir / Path(*rel.split("/"))
    track_id = track_id or f"fake:{Path(rel).stem.replace(' ', '_')}"
    write_tiny_mp3(path, artist="A", title=Path(rel).stem, track_id=track_id)
    track = Track(
        id=track_id,
        provider="fake",
        source_id=track_id.split(":", 1)[1],
        source_url="",
        title=Path(rel).stem,
        artist="A",
        path=rel,
    )
    library.add(track)
    return track


@pytest.fixture
def lib(tmp_path: Path) -> Library:
    root = tmp_path / "music"
    root.mkdir()
    return Library(root, tmp_path / "index.json")


# ----------------------------------------------------------------------------------------------
# Names and paths
# ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Flight", "Flight"),
        ("  Road   trip  ", "Road trip"),
        ("Музыка", "Музыка"),
        ("TikTok ♡", "TikTok ♡"),
        ("", ""),
        ("   ", ""),
        (None, ""),
    ],
)
def test_clean_folder_name(raw: str | None, expected: str) -> None:
    assert clean_folder_name(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "a/b",
        "a\\b",
        "what?",
        'say "hi"',
        "x:y",
        "bell\x07here",  # a control character (a tab is only whitespace and becomes a space)
        ".hidden",
        "trailing.",
        "..",
        "CON",
        "nul.txt",
        "Playlists",
        "playlists",
        ".incoming",
        "$RECYCLE.BIN",  # its tracks would never be listed: rescan skips these
        "@eaDir",
        "System Volume Information",
        "x" * 81,
    ],
)
def test_clean_folder_name_refuses(bad: str) -> None:
    with pytest.raises(LibraryFolderError):
        clean_folder_name(bad)
    assert issubclass(LibraryFolderError, ValueError)  # the API answers 400 with the message


def test_media_and_folder_of_a_path() -> None:
    assert media_of("Flight/a.mp4") == "video"
    assert media_of("a.MKV") == "video"
    assert media_of("a.mp3") == "audio" and media_of("a.m4a") == "audio"
    assert folder_of("a.mp3") == ""
    assert folder_of("Flight/a.mp3") == "Flight"
    assert folder_of("Flight/deeper/a.mp3") == "Flight"


# ----------------------------------------------------------------------------------------------
# Library operations
# ----------------------------------------------------------------------------------------------


def test_folders_lists_directories_and_track_folders(lib: Library) -> None:
    for name in ("Flight", "beats", "Playlists", ".incoming", "@eaDir", ".stversions"):
        (lib.library_dir / name).mkdir()
    (lib.library_dir / "loose.txt").write_text("not a folder")
    add_file(lib, "Gone/x.mp3")  # a folder the index knows even when listing misses it
    assert lib.folders() == ["beats", "Flight", "Gone"]


def test_create_folder(lib: Library) -> None:
    assert lib.create_folder(" Flight ") == "Flight"
    assert (lib.library_dir / "Flight").is_dir()
    assert lib.create_folder("flight") == "Flight"  # an existing one, matched case-insensitively
    with pytest.raises(LibraryFolderError, match="name"):
        lib.create_folder("  ")
    with pytest.raises(LibraryFolderError):
        lib.create_folder("Playlists")


def test_create_folder_on_a_fresh_install_creates_the_library_folder(tmp_path: Path) -> None:
    """Nothing downloaded yet, Music\\Ultimate Playlist does not exist: New folder just works."""
    lib = Library(tmp_path / "Music" / "Ultimate Playlist", tmp_path / "index.json")
    (tmp_path / "Music").mkdir()
    assert lib.create_folder("Flight") == "Flight"
    assert (tmp_path / "Music" / "Ultimate Playlist" / "Flight").is_dir()


def test_create_folder_never_invents_a_missing_drive_or_library(tmp_path: Path) -> None:
    unplugged = Library(tmp_path / "E" / "Music", tmp_path / "index.json")  # parent missing
    with pytest.raises(LibraryUnavailable):
        unplugged.create_folder("Flight")
    assert not (tmp_path / "E").exists()
    root = tmp_path / "lib"
    root.mkdir()
    moved_away = Library(root, tmp_path / "index2.json")
    add_file(moved_away, "a.mp3")
    root.rename(tmp_path / "lib-elsewhere")  # the library folder was moved: tracks are indexed
    with pytest.raises(LibraryUnavailable):
        moved_away.create_folder("Flight")
    assert not root.exists()  # an empty one would make the next rescan drop every track


def test_move_tracks_moves_files_and_keeps_ids(lib: Library) -> None:
    a = add_file(lib, "A - one.mp3", "fake:one")
    b = add_file(lib, "A - two.mp3", "fake:two")
    result = lib.move_tracks([a.id, b.id, a.id], "Flight")  # a duplicate id counts once
    assert result.moved == ["fake:one", "fake:two"] and result.failed == []
    assert lib.get("fake:one").path == "Flight/A - one.mp3"
    assert (lib.library_dir / "Flight" / "A - one.mp3").is_file()
    assert not (lib.library_dir / "A - one.mp3").exists()
    # The index on disk has the new paths, and a rescan finds nothing to change.
    assert Library(lib.library_dir, lib.index_path).get("fake:two").path == "Flight/A - two.mp3"
    assert lib.rescan() == 0
    # Back to Unsorted.
    assert lib.move_tracks(["fake:one"], "").moved == ["fake:one"]
    assert lib.get("fake:one").path == "A - one.mp3"
    assert lib.folder_counts() == {"": 1, "Flight": 1}


def test_move_tracks_into_an_existing_folder_by_another_case(lib: Library) -> None:
    (lib.library_dir / "Flight").mkdir()
    a = add_file(lib, "x.mp3")
    lib.move_tracks([a.id], "flight")
    assert lib.get(a.id).path == "Flight/x.mp3"


def test_move_tracks_never_overwrites(lib: Library) -> None:
    add_file(lib, "Flight/Same.mp3", "fake:there")
    a = add_file(lib, "Same.mp3", "fake:here")
    b = add_file(lib, "Other/Same.mp3", "fake:other")
    result = lib.move_tracks([a.id, b.id], "Flight")
    assert result.failed == []
    assert lib.get("fake:here").path == "Flight/Same (2).mp3"
    assert lib.get("fake:other").path == "Flight/Same (3).mp3"
    assert lib.get("fake:there").path == "Flight/Same.mp3"


def test_move_tracks_reports_what_could_not_move(
    lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Neutral names and error text, so each assertion can only pass for the right reason.
    ok = add_file(lib, "one.mp3", "fake:ok")
    missing = add_file(lib, "two.mp3", "fake:missing")
    (lib.library_dir / "two.mp3").unlink()
    busy = add_file(lib, "three.mp3", "fake:busy")
    real_replace = os.replace
    attempts: list[str] = []

    def flaky_replace(src: Any, dst: Any) -> None:
        if Path(src).name == "three.mp3":
            attempts.append(Path(src).name)
            raise PermissionError(32, "x")
        real_replace(src, dst)

    monkeypatch.setattr(library_mod.os, "replace", flaky_replace)
    monkeypatch.setattr(library_mod, "MOVE_RETRY_DELAY", 0.0)
    result = lib.move_tracks([ok.id, missing.id, busy.id, "fake:nope"], "Flight")
    assert result.moved == ["fake:ok"]
    reasons = dict(result.failed)
    assert reasons["fake:missing"] == "two.mp3 is missing from the folder."
    assert reasons["fake:busy"] == "three.mp3 is in use (playing?). Stop playback and try again."
    assert reasons["fake:nope"] == "Not in the library."
    assert len(attempts) == library_mod.MOVE_RETRIES  # retried before giving up
    assert lib.get("fake:busy").path == "three.mp3"  # left where it was


def test_a_move_retry_can_succeed(lib: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file the player streams is open for a moment on each request; the next try wins."""
    a = add_file(lib, "a.mp3")
    real_replace = os.replace
    calls: list[int] = []

    def busy_once(src: Any, dst: Any) -> None:
        if Path(src).suffix == ".mp3":  # the index's own atomic save goes through untouched
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError(32, "in use")
        real_replace(src, dst)

    monkeypatch.setattr(library_mod.os, "replace", busy_once)
    monkeypatch.setattr(library_mod, "MOVE_RETRY_DELAY", 0.0)
    assert lib.move_tracks([a.id], "Flight").moved == [a.id]
    assert len(calls) == 2


def test_rename_folder(lib: Library) -> None:
    add_file(lib, "Flight/a.mp3", "fake:a")
    add_file(lib, "Flightless/b.mp3", "fake:b")  # same prefix, other folder: untouched
    assert lib.rename_folder("flight", "Trip") == "Trip"
    assert lib.get("fake:a").path == "Trip/a.mp3"
    assert lib.get("fake:b").path == "Flightless/b.mp3"
    assert (lib.library_dir / "Trip" / "a.mp3").is_file()
    assert lib.rename_folder("Trip", "trip") == "trip"  # a case-only rename
    assert lib.get("fake:a").path == "trip/a.mp3"
    with pytest.raises(LibraryFolderError, match="already exists"):
        lib.rename_folder("trip", "FLIGHTLESS")
    with pytest.raises(LibraryFolderError, match="no folder"):
        lib.rename_folder("Nope", "X")
    with pytest.raises(LibraryFolderError):
        lib.rename_folder("trip", "bad/name")


def test_delete_folder_moves_tracks_back_and_removes_it(lib: Library) -> None:
    add_file(lib, "Flight/a.mp3", "fake:a")
    add_file(lib, "a.mp3", "fake:clash")  # the name is taken in Unsorted
    result = lib.delete_folder("Flight")
    assert result.moved == ["fake:a"] and result.failed == []
    assert lib.get("fake:a").path == "a (2).mp3"
    assert not (lib.library_dir / "Flight").exists()
    assert "Flight" not in lib.folders()


def test_delete_folder_keeps_files_the_library_does_not_know(lib: Library) -> None:
    add_file(lib, "Flight/a.mp3", "fake:a")
    (lib.library_dir / "Flight" / "notes.txt").write_text("mine")
    result = lib.delete_folder("Flight")
    assert (lib.library_dir / "Flight" / "notes.txt").is_file()  # nothing of the user's is lost
    assert result.note and "stays on disk" in result.note  # and the user is told why
    with pytest.raises(LibraryFolderError):
        lib.delete_folder("Nope")


def test_rescan_picks_up_folders_and_videos(lib: Library) -> None:
    from fake_provider import write_tiny_audio

    write_tiny_mp3(lib.library_dir / "Flight" / "A - song.mp3", artist="A", title="song")
    clip = lib.library_dir / "Flight" / "B - clip.m4a"
    write_tiny_audio(clip, artist="B", title="clip", track_id="youtube:x:video")
    clip.rename(clip.with_suffix(".mp4"))  # an MP4 container under its video extension
    assert lib.rescan() == 2
    paths = sorted(t.path for t in lib.all())
    assert paths == ["Flight/A - song.mp3", "Flight/B - clip.mp4"]
    assert lib.get("youtube:x:video").path == "Flight/B - clip.mp4"


def test_a_file_moved_outside_the_app_keeps_its_id(lib: Library) -> None:
    add_file(lib, "a.mp3", "fake:a")
    (lib.library_dir / "Flight").mkdir()
    os.replace(lib.library_dir / "a.mp3", lib.library_dir / "Flight" / "a.mp3")  # Explorer
    lib.rescan()
    assert lib.get("fake:a").path == "Flight/a.mp3"  # same id, so playlists still find it


def untagged_mp3(path: Path) -> None:
    """A file the user copied in: no ULTIMATE_PLAYLIST_ID tag, so its id comes from its path."""
    write_tiny_mp3(path, artist="Me", title=path.stem)


def test_a_new_file_never_takes_the_id_of_a_moved_track(lib: Library) -> None:
    untagged_mp3(lib.library_dir / "song.mp3")
    lib.rescan()
    (old,) = lib.all()
    lib.move_tracks([old.id], "Flight")
    untagged_mp3(lib.library_dir / "song.mp3")  # another file where the moved one was
    lib.rescan()
    assert lib.get(old.id).path == "Flight/song.mp3"  # the moved track keeps its id and place
    assert len(lib) == 2
    newcomer = next(t for t in lib.all() if t.id != old.id)
    assert newcomer.path == "song.mp3" and newcomer.id.startswith("local:")
    assert lib.rescan() == 0  # and it stays that way


def test_rename_and_remove_refuse_while_the_library_folder_is_missing(lib: Library) -> None:
    add_file(lib, "Flight/a.mp3", "fake:a")
    root = lib.library_dir
    root.rename(root.with_name("music-elsewhere"))  # unplugged / moved: the index knows Flight
    with pytest.raises(LibraryUnavailable):
        lib.rename_folder("Flight", "Trip")
    with pytest.raises(LibraryUnavailable):
        lib.delete_folder("Flight")
    assert not root.exists()  # not re-created empty for the next rescan to wipe the index
    assert lib.get("fake:a").path == "Flight/a.mp3"


@pytest.mark.parametrize("name", ["My  Mix", "x" * 90, "Con.Air (1997)"])
def test_folders_made_in_explorer_work_as_they_are(lib: Library, name: str) -> None:
    """Names the new-folder rule would refuse (or tidy) but Windows allows."""
    try:
        (lib.library_dir / name).mkdir()
    except OSError:
        pytest.skip(f"this file system refuses {name!r}")
    a = add_file(lib, "a.mp3", "fake:a")
    assert name in lib.folders()
    assert lib.resolve_folder(name) == name
    assert lib.move_tracks([a.id], name).moved == [a.id]
    assert lib.get(a.id).path == f"{name}/a.mp3"  # into that folder, not a tidied twin
    assert lib.folders() == [name]
    assert lib.rename_folder(name, "Mix") == "Mix"
    assert lib.get(a.id).path == "Mix/a.mp3"
    lib.move_tracks([a.id], "")
    (lib.library_dir / name).mkdir()
    assert lib.delete_folder(name).failed == []
    assert not (lib.library_dir / name).exists()


def test_hidden_and_app_folders_are_not_folders(lib: Library) -> None:
    for hidden in (".Trashes", ".stversions"):
        untagged_mp3(lib.library_dir / hidden / "old.mp3")
    untagged_mp3(lib.library_dir / "playlists" / "x.mp3")  # the app's folder, another case
    untagged_mp3(lib.library_dir / "Real" / "song.mp3")
    lib.rescan()
    assert [t.path for t in lib.all()] == ["Real/song.mp3"]  # trash and old versions stay out
    assert lib.folders() == ["Real"]


def _set_attributes(path: Path, attributes: int) -> None:
    import ctypes

    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), attributes)  # type: ignore[attr-defined]


@pytest.mark.skipif(os.name != "nt", reason="file attributes are a Windows thing")
def test_only_folders_windows_protects_are_skipped_by_attribute(lib: Library) -> None:
    """Hidden and system together (as $Recycle.Bin and System Volume Information are) keeps a
    folder out; hidden alone (Explorer's "Hide", a NAS hiding a leading dot) does not."""
    untagged_mp3(lib.library_dir / "Recycler" / "x.mp3")
    _set_attributes(lib.library_dir / "Recycler", 0x2 | 0x4)
    untagged_mp3(lib.library_dir / ".38 Special" / "y.mp3")
    _set_attributes(lib.library_dir / ".38 Special", 0x2)
    lib.rescan()
    assert lib.folders() == [".38 Special"]
    assert [t.path for t in lib.all()] == [".38 Special/y.mp3"]


@pytest.mark.skipif(os.name != "nt", reason="file attributes are a Windows thing")
def test_rescan_never_drops_tracks_for_a_folder_attribute(lib: Library) -> None:
    """Tracks already in the library stay, with their playlists, whatever the folder's
    attributes become; only trash folders (by name) lose theirs."""
    add_file(lib, "Kids/a.mp3", "fake:a")
    _set_attributes(lib.library_dir / "Kids", 0x2 | 0x4)
    assert lib.rescan() == 0
    assert lib.get("fake:a").path == "Kids/a.mp3"
    assert lib.folders() == ["Kids"]  # listed while it holds tracks: list and counts agree
    assert lib.folder_counts() == {"Kids": 1}


def test_one_folder_in_two_spellings(lib: Library) -> None:
    """Windows: "Flight" and "flight" are one folder; the index may hold both spellings."""
    add_file(lib, "Flight/a.mp3", "fake:a")
    lib.add(replace(add_file(lib, "Flight/b.mp3", "fake:b"), path="flight/b.mp3"))
    if not (lib.library_dir / "flight" / "b.mp3").is_file():
        pytest.skip("needs a case-insensitive file system")
    assert lib.folders() == ["Flight"]
    assert lib.folder_counts() == {"Flight": 2}
    assert lib.folder_name_of("flight/b.mp3") == "Flight"
    lib.rename_folder("Flight", "Trip")
    assert {lib.get("fake:a").path, lib.get("fake:b").path} == {"Trip/a.mp3", "Trip/b.mp3"}
    result = lib.delete_folder("trip")
    assert sorted(result.moved) == ["fake:a", "fake:b"]


def test_remove_folder_clears_the_empty_subfolders_a_move_leaves(lib: Library) -> None:
    add_file(lib, "Flight/deep/er/a.mp3", "fake:a")
    result = lib.delete_folder("Flight")
    assert result.moved == ["fake:a"] and result.note is None
    assert not (lib.library_dir / "Flight").exists()
    assert lib.get("fake:a").path == "a.mp3"


def test_folders_starting_with_dots_are_music(lib: Library) -> None:
    """A leading dot hides nothing on Windows, and these are real album and band names."""
    untagged_mp3(lib.library_dir / ".38 Special" / "Hold On Loosely.mp3")
    untagged_mp3(lib.library_dir / "Metallica" / "...And Justice for All" / "One.mp3")
    untagged_mp3(lib.library_dir / "...Baby One More Time" / "Sometimes.mp3")
    assert lib.rescan() == 3
    assert lib.folders() == ["...Baby One More Time", ".38 Special", "Metallica"]
    one = next(t for t in lib.all() if t.title == "One")
    assert lib.move_tracks([one.id], ".38 Special").moved == [one.id]  # an existing folder
    assert lib.get(one.id).path == ".38 Special/One.mp3"


def test_rescan_drops_what_an_older_version_indexed_in_trash(lib: Library) -> None:
    """0.3 indexed sync-tool and trash copies; they are not the user's music and their folder
    must not show up in the list."""
    add_file(lib, ".stversions/old.mp3", "fake:old")
    add_file(lib, "Album/.stversions/older.mp3", "fake:older")
    add_file(lib, "Real/song.mp3", "fake:song")
    assert ".stversions" not in lib.folders()
    assert lib.rescan() == 2
    assert lib.get("fake:old") is None and lib.get("fake:older") is None
    assert lib.folder_counts() == {"Real": 1}
    assert lib.rescan() == 0


def test_folder_names_compare_like_windows(lib: Library) -> None:
    """NTFS keeps "Straße" and "Strasse" apart; str.casefold() would take them for one."""
    (lib.library_dir / "Straße").mkdir()
    try:
        (lib.library_dir / "Strasse").mkdir()
    except FileExistsError:
        pytest.skip("this file system takes the two names for one")
    a = add_file(lib, "Straße/a.mp3", "fake:a")
    b = add_file(lib, "Strasse/b.mp3", "fake:b")
    assert sorted(lib.folders()) == ["Strasse", "Straße"]
    assert lib.folder_counts() == {"Straße": 1, "Strasse": 1}
    assert lib.move_tracks([b.id], "Straße").moved == [b.id]
    assert lib.get(b.id).path == "Straße/b.mp3"  # really moved, not "already there"
    assert lib.delete_folder("Strasse").moved == []  # the other folder's tracks stay
    assert lib.rename_folder("Straße", "Gasse") == "Gasse"
    assert {lib.get(a.id).path, lib.get(b.id).path} == {"Gasse/a.mp3", "Gasse/b.mp3"}


def _link_dir(target: Path, link: Path) -> None:
    """A junction on Windows (no admin rights needed), a symbolic link elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_remove_folder_never_follows_a_junction(lib: Library, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    (outside / "empty").mkdir(parents=True)
    (outside / "keep.txt").write_text("mine")
    add_file(lib, "Flight/a.mp3", "fake:a")
    link = lib.library_dir / "Flight" / "link"
    _link_dir(outside, link)
    result = lib.delete_folder("Flight")
    assert result.moved == ["fake:a"]
    assert (outside / "empty").is_dir()  # an empty folder outside the library stays
    assert (outside / "keep.txt").is_file()
    assert os.path.lexists(link)  # and so does the junction
    assert result.note and "stays on disk" in result.note


def test_a_move_for_a_download_follows_renames_and_removals(lib: Library) -> None:
    a = add_file(lib, "a.mp3", "fake:a")
    b = add_file(lib, "b.mp3", "fake:b")
    c = add_file(lib, "c.mp3", "fake:c")
    lib.create_folder("Flight")
    lib.rename_folder("Flight", "Plane")
    assert lib.move_tracks([a.id], "flight", follow_renames=True).moved == [a.id]
    assert lib.get(a.id).path == "Plane/a.mp3"  # followed the rename, any case
    assert not (lib.library_dir / "Flight").exists()
    lib.delete_folder("Plane")
    result = lib.move_tracks([b.id], "Flight", follow_renames=True)
    assert result.moved == [] and dict(result.failed)[b.id] == 'The folder "Flight" was removed.'
    assert not (lib.library_dir / "Flight").exists() and not (lib.library_dir / "Plane").exists()
    assert lib.get(b.id).path == "b.mp3"
    # A folder nobody renamed or removed is made as usual: the library folder was offline at
    # submit, a retry, a library folder changed in the settings.
    assert lib.move_tracks([c.id], "Gym", follow_renames=True).moved == [c.id]
    # Made again on purpose: downloads naming it go there again.
    lib.create_folder("Flight")
    assert lib.move_tracks([b.id], "Flight", follow_renames=True).moved == [b.id]
    assert lib.get(b.id).path == "Flight/b.mp3"
    # A plain move (Move to folder) never follows: the user picked that very name.
    lib.rename_folder("Flight", "Trip")
    assert lib.move_tracks([c.id], "Flight").moved == [c.id]
    assert lib.get(c.id).path == "Flight/c.mp3"
    assert lib.move_tracks([a.id], "Flight", follow_renames=True).moved == [a.id]
    assert lib.get(a.id).path == "Flight/a.mp3"  # ... and makes the name wanted again


def test_a_folder_picked_again_takes_new_downloads(lib: Library) -> None:
    """A removal or rename is remembered for downloads already on their way, also when the
    folder stays on disk or is made again outside the app; picking the name for new downloads
    ends it. Existence alone does not: it cannot tell an old download from a new one."""
    a = add_file(lib, "a.mp3", "fake:a")
    b = add_file(lib, "b.mp3", "fake:b")
    add_file(lib, "Gym/x.mp3", "fake:x")
    (lib.library_dir / "Gym" / "cover.jpg").write_bytes(b"jpg")
    assert lib.delete_folder("Gym").note  # its tracks left; cover.jpg keeps the folder
    assert "Gym" in lib.folders()
    late = lib.move_tracks([a.id], "Gym", follow_renames=True)  # a download that raced it
    assert dict(late.failed)[a.id] == 'The folder "Gym" was removed.'
    assert lib.pick_folder("gym") == "Gym"  # Save to: Gym for a new download
    assert lib.move_tracks([a.id], "Gym", follow_renames=True).moved == [a.id]
    assert lib.get(a.id).path == "Gym/a.mp3"
    lib.create_folder("Flight")
    lib.rename_folder("Flight", "Plane")
    (lib.library_dir / "Flight").mkdir()  # made again in Explorer
    assert lib.move_tracks([b.id], "Flight", follow_renames=True).moved == [b.id]
    assert lib.get(b.id).path == "Plane/b.mp3"  # on its way before the rename: follows it
    assert lib.pick_folder("Flight") == "Flight"
    assert lib.move_tracks([b.id], "Flight", follow_renames=True).moved == [b.id]
    assert lib.get(b.id).path == "Flight/b.mp3"


def test_look_alike_folders_never_catch_a_renamed_folders_download(lib: Library) -> None:
    """ "Road  trip" (made in Explorer) and "Road trip" are two folders: a download into the
    first follows its rename, it does not fall into the second."""
    a = add_file(lib, "a.mp3", "fake:a")
    try:
        (lib.library_dir / "Road  trip").mkdir()
    except OSError:
        pytest.skip("this file system refuses a double space")
    lib.create_folder("Road trip")
    assert sorted(lib.folders()) == ["Road  trip", "Road trip"]
    lib.rename_folder("Road  trip", "Holiday")
    assert lib.move_tracks([a.id], "Road  trip", follow_renames=True).moved == [a.id]
    assert lib.get(a.id).path == "Holiday/a.mp3"


def test_a_stale_copy_of_the_same_track_is_replaced_not_duplicated(lib: Library) -> None:
    """Remove from library (file kept), download again, the folder still has the old file."""
    add_file(lib, "Flight/A - song.mp3", "fake:song")
    lib.remove("fake:song")  # the file stays in Flight
    add_file(lib, "A - song.mp3", "fake:song")  # the new download, in Unsorted
    lib.move_tracks(["fake:song"], "Flight")
    assert lib.get("fake:song").path == "Flight/A - song.mp3"
    assert sorted(p.name for p in (lib.library_dir / "Flight").iterdir()) == ["A - song.mp3"]
    assert lib.rescan() == 0  # no "(2)" twin for a rescan to add as a second track


def test_a_different_file_with_the_same_name_is_kept(lib: Library) -> None:
    untagged_mp3(lib.library_dir / "Flight" / "A - song.mp3")  # the user's own, same name
    add_file(lib, "A - song.mp3", "fake:song")
    lib.move_tracks(["fake:song"], "Flight")
    assert lib.get("fake:song").path == "Flight/A - song (2).mp3"


# ----------------------------------------------------------------------------------------------
# Downloads into a folder
# ----------------------------------------------------------------------------------------------


@pytest.fixture
def make_jobs(
    tmp_settings: Settings, library: Library
) -> Iterator[Callable[..., tuple[JobManager, FakeProvider]]]:
    managers: list[JobManager] = []

    def factory(**fake_kwargs: Any) -> tuple[JobManager, FakeProvider]:
        fake = FakeProvider(**fake_kwargs)
        manager = JobManager(replace(tmp_settings), library, providers=FakeRegistry(fake))
        manager.start()
        managers.append(manager)
        return manager, fake

    yield factory
    for manager in managers:
        manager.stop()


def test_a_download_lands_in_its_folder(make_jobs: Any, library: Library) -> None:
    manager, _ = make_jobs()
    job = manager.submit(track_url("f1"), folder=" Flight ")
    assert job.folder == "Flight"
    assert manager.wait_idle(WAIT)
    assert job.status == JobStatus.DONE and job.message is None
    assert job.track is not None and job.track.path == "Flight/Fake Artist - Track f1.mp3"
    assert library.get("fake:f1").path == "Flight/Fake Artist - Track f1.mp3"
    assert (library.library_dir / "Flight" / "Fake Artist - Track f1.mp3").is_file()


def test_a_playlist_lands_in_its_folder_and_retry_keeps_it(
    make_jobs: Any, library: Library
) -> None:
    manager, _ = make_jobs(fail_ids={"pl2-2"})
    parent = manager.submit(playlist_url(2), folder="Trip")
    assert manager.wait_idle(WAIT)
    kids = [j for j in manager.list() if j.parent_id == parent.id]
    assert {k.folder for k in kids} == {"Trip"}
    assert library.get("fake:pl2-1").path.startswith("Trip/")
    failed = next(k for k in kids if k.status == JobStatus.ERROR)
    again = manager.retry(failed.id)
    assert again is not None and again.folder == "Trip"


def test_a_folder_move_that_fails_leaves_the_file_in_unsorted(
    make_jobs: Any, library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = make_jobs()
    monkeypatch.setattr(library_mod.os, "replace", _always_busy)
    monkeypatch.setattr(library_mod, "MOVE_RETRY_DELAY", 0.0)
    job = manager.submit(track_url("stuck"), folder="Flight")
    assert manager.wait_idle(WAIT)
    assert job.status == JobStatus.DONE
    assert job.message and job.message.startswith("Saved to Unsorted, not Flight")
    assert library.get("fake:stuck").path == "Fake Artist - Track stuck.mp3"


_real_replace = os.replace


def _always_busy(src: Any, dst: Any) -> None:
    """The media file is in use; the index's own atomic save still works."""
    if Path(src).suffix == ".mp3":
        raise PermissionError(32, "in use")
    _real_replace(src, dst)


def test_submit_refuses_a_bad_folder_name(make_jobs: Any) -> None:
    manager, fake = make_jobs()
    with pytest.raises(ValueError, match="characters"):
        manager.submit(track_url("x"), folder="a/b")
    assert fake.resolved == []


def test_a_track_already_in_unsorted_joins_the_folder(make_jobs: Any, library: Library) -> None:
    manager, fake = make_jobs()
    first = manager.submit(track_url("have"))
    assert manager.wait_idle(WAIT) and first.status == JobStatus.DONE
    again = manager.submit(track_url("have"), folder="Flight")
    assert manager.wait_idle(WAIT)
    assert again.status == JobStatus.SKIPPED
    assert again.message == "Already in library; moved to Flight"
    assert library.get("fake:have").path == "Flight/Fake Artist - Track have.mp3"
    assert fake.downloads == ["have"]  # moved, not downloaded twice


def test_a_track_in_another_folder_stays_there(make_jobs: Any, library: Library) -> None:
    manager, _ = make_jobs()
    manager.submit(track_url("gym"), folder="Gym")
    assert manager.wait_idle(WAIT)
    again = manager.submit(track_url("gym"), folder="Flight")
    assert manager.wait_idle(WAIT)
    assert again.status == JobStatus.SKIPPED and again.message == "Already in library (in Gym)"
    assert library.get("fake:gym").path.startswith("Gym/")  # Gym is not emptied to fill Flight
    same = manager.submit(track_url("gym"), folder="gym")
    assert manager.wait_idle(WAIT) and same.message == "Already in library"


def test_queued_downloads_follow_a_renamed_or_removed_folder(
    make_jobs: Any, library: Library
) -> None:
    manager, fake = make_jobs()
    fake.gate = threading.Event()  # downloads wait until the gate opens
    library.create_folder("Flight")
    parent = manager.submit(playlist_url(3), folder="Flight")
    assert fake.download_started.wait(WAIT)
    library.rename_folder("Flight", "Plane")
    assert manager.retarget_folder("flight", "Plane") >= 3  # case-insensitive, parent too
    fake.gate.set()
    assert manager.wait_idle(WAIT)
    paths = sorted(t.path for t in library.all())
    assert all(p.startswith("Plane/") for p in paths), paths
    assert not (library.library_dir / "Flight").exists()  # the old folder did not come back
    assert parent.folder == "Plane"
    assert manager.retarget_folder("Plane", "") >= 1  # a removal: Unsorted from now on
    assert parent.folder == ""


def test_a_download_never_brings_a_removed_folder_back(make_jobs: Any, library: Library) -> None:
    """A folder removed while the file downloads stays removed: the API re-targets the jobs
    only after the removal, which a finishing download can beat."""
    manager, fake = make_jobs()
    fake.gate = threading.Event()
    library.create_folder("Flight")
    job = manager.submit(track_url("late"), folder="Flight")
    assert fake.download_started.wait(WAIT)
    library.delete_folder("Flight")  # no retarget_folder yet: the race
    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert job.status == JobStatus.DONE
    assert job.message == 'Saved to Unsorted, not Flight: The folder "Flight" was removed.'
    assert library.get("fake:late").path == "Fake Artist - Track late.mp3"
    assert not (library.library_dir / "Flight").exists()


def test_a_folder_kept_by_other_files_still_counts_as_removed_for_a_racing_download(
    make_jobs: Any, library: Library
) -> None:
    """A desktop.ini keeps the removed folder on disk; the download that raced the removal goes
    to Unsorted with the rest of its playlist, and a new one picked for that folder goes in."""
    manager, fake = make_jobs()
    fake.gate = threading.Event()
    library.create_folder("Gym")
    (library.library_dir / "Gym" / "desktop.ini").write_text("[.ShellClassInfo]")
    job = manager.submit(track_url("late"), folder="Gym")
    assert fake.download_started.wait(WAIT)
    assert library.delete_folder("Gym").note  # kept on disk by desktop.ini
    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert job.message == 'Saved to Unsorted, not Gym: The folder "Gym" was removed.'
    assert library.get("fake:late").path == "Fake Artist - Track late.mp3"
    again = manager.submit(track_url("new"), folder="Gym")  # Save to: Gym, after the removal
    assert manager.wait_idle(WAIT)
    assert again.message is None
    assert library.get("fake:new").path == "Gym/Fake Artist - Track new.mp3"


def test_a_stale_folder_name_never_makes_the_old_folder_again(
    make_jobs: Any, library: Library
) -> None:
    """A second tab (or a link pasted while the rename ran) still offers the old name: a
    renamed folder's downloads go to its new name, a removed one's stay in Unsorted."""
    manager, _ = make_jobs()
    library.create_folder("Flight")
    library.rename_folder("Flight", "Plane")
    moved = manager.submit(track_url("stale1"), folder="Flight")
    assert moved.folder == "Plane"  # the queue row says where it goes
    library.create_folder("Gym")
    library.delete_folder("Gym")
    removed = manager.submit(track_url("stale2"), folder="Gym")
    assert manager.wait_idle(WAIT)
    assert library.get("fake:stale1").path == "Plane/Fake Artist - Track stale1.mp3"
    assert removed.message == 'Saved to Unsorted, not Gym: The folder "Gym" was removed.'
    assert library.get("fake:stale2").path == "Fake Artist - Track stale2.mp3"
    assert library.folders() == ["Plane"]  # neither Flight nor Gym came back
    library.create_folder("Gym")  # New folder... Gym: wanted again
    again = manager.submit(track_url("stale3"), folder="Gym")
    assert manager.wait_idle(WAIT) and again.message is None
    assert library.get("fake:stale3").path == "Gym/Fake Artist - Track stale3.mp3"


def test_a_download_follows_a_rename_it_races(make_jobs: Any, library: Library) -> None:
    manager, fake = make_jobs()
    fake.gate = threading.Event()
    library.create_folder("Flight")
    job = manager.submit(track_url("ren"), folder="Flight")
    assert fake.download_started.wait(WAIT)
    library.rename_folder("Flight", "Plane")  # no retarget_folder yet: the race
    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert job.status == JobStatus.DONE and job.message is None
    assert library.get("fake:ren").path == "Plane/Fake Artist - Track ren.mp3"
    assert not (library.library_dir / "Flight").exists()


def test_a_folder_is_made_when_the_first_file_lands(make_jobs: Any, library: Library) -> None:
    """Nothing is made for a download that may still fail, and a library folder that does
    not exist yet (first start, a drive plugged in later) is no reason to miss the folder."""
    manager, fake = make_jobs()
    fake.gate = threading.Event()
    library.library_dir.rmdir()  # a fresh install: nothing downloaded yet
    job = manager.submit(track_url("first"), folder="Rock")
    assert fake.download_started.wait(WAIT)
    assert not (library.library_dir / "Rock").exists()
    fake.gate.set()
    assert manager.wait_idle(WAIT)
    assert job.status == JobStatus.DONE and job.message is None
    assert library.get("fake:first").path == "Rock/Fake Artist - Track first.mp3"


def test_the_api_retargets_queued_downloads(client: Any, library: Library) -> None:
    jobs = client.app.state.jobs
    library.create_folder("Flight")
    job = jobs.submit(track_url("rt"), folder="Flight")
    client.patch("/api/folders/Flight", json={"name": "Plane"})
    assert job.folder == "Plane"
    assert jobs.wait_idle(WAIT)
    client.delete("/api/folders/Plane")
    assert job.folder == ""


def test_api_job_tracks_carry_media_and_folder(client: Any) -> None:
    client.post("/api/jobs", json={"url": track_url("jt"), "folder": "Flight"})
    assert client.app.state.jobs.wait_idle(WAIT)
    (job,) = client.get("/api/jobs").json()["jobs"]
    assert job["track"]["media"] == "audio" and job["track"]["folder"] == "Flight"


def test_api_rename_refuses_while_the_library_folder_is_missing(
    client: Any, library: Library
) -> None:
    add_file(library, "Flight/a.mp3", "fake:a")
    library.library_dir.rename(library.library_dir.with_name("gone"))
    response = client.patch("/api/folders/Flight", json={"name": "Trip"})
    assert response.status_code == 409 and "not reachable" in response.json()["detail"]
    assert client.delete("/api/folders/Flight").status_code == 409


# ----------------------------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------------------------


def test_api_folders_crud_and_move(client: Any, library: Library) -> None:
    add_file(library, "a.mp3", "fake:a")
    add_file(library, "b.mp3", "fake:b")
    assert client.get("/api/folders").json() == {"unsorted": 2, "folders": []}

    created = client.post("/api/folders", json={"name": "Flight"})
    assert created.status_code == 201
    assert created.json()["name"] == "Flight"
    assert created.json()["folders"] == [{"name": "Flight", "count": 0}]
    assert client.post("/api/folders", json={"name": "a/b"}).status_code == 400

    moved = client.post(
        "/api/library/move", json={"track_ids": ["fake:a", "fake:x"], "folder": "Flight"}
    )
    assert moved.status_code == 200
    body = moved.json()
    assert body["moved"] == ["fake:a"]
    assert body["failed"] == [{"id": "fake:x", "error": "Not in the library."}]
    assert body["unsorted"] == 1 and body["folders"] == [{"name": "Flight", "count": 1}]

    tracks = {t["id"]: t for t in client.get("/api/library").json()["tracks"]}
    assert tracks["fake:a"]["folder"] == "Flight" and tracks["fake:a"]["media"] == "audio"
    assert tracks["fake:b"]["folder"] == ""

    renamed = client.patch("/api/folders/Flight", json={"name": "Trip"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Trip"
    assert client.patch("/api/folders/Nope", json={"name": "X"}).status_code == 400
    assert library.get("fake:a").path == "Trip/a.mp3"

    removed = client.delete("/api/folders/Trip")
    assert removed.status_code == 200
    assert removed.json()["moved"] == ["fake:a"] and removed.json()["folders"] == []
    assert client.delete("/api/folders/Trip").status_code == 404
    assert library.get("fake:a").path == "a.mp3"


def test_api_jobs_take_a_folder(client: Any, library: Library) -> None:
    response = client.post("/api/jobs", json={"url": track_url("api1"), "folder": "Flight"})
    assert response.status_code == 202
    assert response.json()["jobs"][0]["folder"] == "Flight"
    assert client.app.state.jobs.wait_idle(WAIT)
    assert library.get("fake:api1").path.startswith("Flight/")
    bad = client.post("/api/jobs", json={"url": track_url("api2"), "folder": "CON"})
    assert bad.status_code == 400 and "reserves" in bad.json()["detail"]


def test_api_media_is_served_from_a_folder(client: Any, library: Library) -> None:
    add_file(library, "Flight/a.mp3", "fake:a")
    response = client.get("/media/fake:a")
    assert response.status_code == 200 and response.headers["content-type"] == "audio/mpeg"


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------


def test_cli_add_folder(tmp_settings: Settings, fake_provider: FakeProvider) -> None:
    from ultimate_playlist import cli

    tmp_settings.save()
    assert cli.main(["add", "--folder", "Flight", track_url("cli1")]) == 0
    lib = Library(tmp_settings.library_dir, tmp_settings.index_path)
    assert lib.get("fake:cli1").path == "Flight/Fake Artist - Track cli1.mp3"
    assert cli.main(["add", "--folder", "a/b", track_url("cli2")]) == 1  # refused, reported
