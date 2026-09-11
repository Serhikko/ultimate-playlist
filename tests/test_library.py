"""Library: index persistence, has/remove/search, rescan and cover extraction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fake_provider import FAKE_PNG, write_tiny_audio, write_tiny_mp3
from mutagen.id3 import APIC, ID3

from ultimate_playlist.config import Settings
from ultimate_playlist.library import (
    Library,
    LibraryFileInUse,
    LibraryUnavailable,
    image_mime,
    local_track_id,
    read_cover,
)
from ultimate_playlist.models import Playlist, Track
from ultimate_playlist.playlists import m3u8_text

AUDIO_FORMATS = ["mp3", "m4a", "opus", "flac"]


def make_track(
    library: Library,
    source_id: str,
    *,
    title: str | None = None,
    artist: str = "Fake Artist",
    album: str | None = None,
    added_at: str | None = None,
    write_file: bool = True,
) -> Track:
    """Create a tagged MP3 in the library folder and the matching Track (not yet added)."""
    title = title or f"Track {source_id}"
    rel = f"{artist} - {title}.mp3"
    track_id = f"fake:{source_id}"
    if write_file:
        write_tiny_mp3(
            library.library_dir / rel,
            artist=artist,
            title=title,
            album=album,
            source_url=f"https://fake.test/track/{source_id}",
            track_id=track_id,
        )
    kwargs = {}
    if added_at:
        kwargs["added_at"] = added_at
    return Track(
        id=track_id,
        provider="fake",
        source_id=source_id,
        source_url=f"https://fake.test/track/{source_id}",
        title=title,
        artist=artist,
        album=album,
        path=rel,
        duration=0.3,
        file_size=1690,
        **kwargs,
    )


# -- add / get / has ---------------------------------------------------------------------------


def test_add_get_and_has(library: Library) -> None:
    track = make_track(library, "a")
    assert library.get("fake:a") is None
    assert not library.has("fake:a")

    library.add(track)

    assert library.get("fake:a") == track
    assert library.has("fake:a")
    assert len(library) == 1
    assert library.abs_path(track) == library.library_dir / track.path
    assert library.abs_path("fake:a") == library.library_dir / track.path


def test_add_is_an_upsert(library: Library) -> None:
    library.add(make_track(library, "a"))
    library.add(make_track(library, "a", title="Renamed"))
    assert len(library.all()) == 1
    assert library.get("fake:a").title == "Renamed"


def test_has_is_false_once_the_file_is_deleted(library: Library) -> None:
    track = make_track(library, "a")
    library.add(track)
    library.abs_path(track).unlink()

    assert library.get("fake:a") is not None  # still indexed...
    assert not library.has("fake:a")  # ...but not usable


def test_all_is_sorted_newest_first(library: Library) -> None:
    library.add(make_track(library, "old", added_at="2024-01-01T00:00:00+00:00"))
    library.add(make_track(library, "new", added_at="2025-01-01T00:00:00+00:00"))
    library.add(make_track(library, "mid", added_at="2024-06-01T00:00:00+00:00"))
    assert [t.id for t in library.all()] == ["fake:new", "fake:mid", "fake:old"]


# -- remove ----------------------------------------------------------------------------------


def test_remove_keeps_the_file_by_default(library: Library) -> None:
    track = make_track(library, "a")
    library.add(track)

    assert library.remove("fake:a") is True
    assert library.get("fake:a") is None
    assert library.abs_path(track).exists()


def test_remove_with_delete_file(library: Library) -> None:
    track = make_track(library, "a")
    library.add(track)
    path = library.abs_path(track)

    assert library.remove("fake:a", delete_file=True) is True
    assert not path.exists()
    assert library.get("fake:a") is None
    # and the change is persisted
    assert Library(library.library_dir, library.index_path).get("fake:a") is None


def test_remove_unknown_returns_false(library: Library) -> None:
    assert library.remove("fake:nope") is False
    assert library.remove("fake:nope", delete_file=True) is False


def test_remove_keeps_the_entry_when_the_file_cannot_be_deleted(
    library: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows refuses to unlink a file that is open (e.g. still playing in the browser)."""
    track = make_track(library, "busy")
    library.add(track)
    target = library.abs_path(track)
    real_unlink = Path.unlink

    def locked(self: Path, missing_ok: bool = False) -> None:
        if self == target:  # only the busy file; the index's .tmp files must still be removable
            raise PermissionError(32, "The process cannot access the file because it is in use")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(LibraryFileInUse) as excinfo:
        library.remove("fake:busy", delete_file=True)
    assert "in use" in str(excinfo.value)
    assert isinstance(excinfo.value, OSError)
    assert library.get("fake:busy") == track  # still indexed: no phantom "deleted" track
    monkeypatch.undo()
    assert library.abs_path(track).is_file()
    assert library.remove("fake:busy", delete_file=True) is True
    assert not library.abs_path(track).exists()


# -- search ----------------------------------------------------------------------------------


def test_search_is_case_insensitive_over_title_artist_album(library: Library) -> None:
    library.add(make_track(library, "1", title="Blue Monday", artist="New Order", album="Power"))
    library.add(make_track(library, "2", title="Hurt", artist="Johnny Cash", album="American IV"))
    library.add(make_track(library, "3", title="Silence", artist="Nobody"))

    assert [t.id for t in library.search("blue")] == ["fake:1"]
    assert [t.id for t in library.search("CASH")] == ["fake:2"]
    assert [t.id for t in library.search("american")] == ["fake:2"]
    assert {t.id for t in library.search("e")} == {"fake:1", "fake:2", "fake:3"}
    assert library.search("zzz") == []
    assert len(library.search("")) == 3
    assert len(library.search("   ")) == 3


# -- persistence -----------------------------------------------------------------------------


def test_save_is_atomic_and_versioned(library: Library) -> None:
    library.add(make_track(library, "a"))

    assert library.index_path.exists()
    assert not library.index_path.with_name(library.index_path.name + ".tmp").exists()
    data = json.loads(library.index_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert set(data["tracks"]) == {"fake:a"}
    assert data["tracks"]["fake:a"]["path"] == "Fake Artist - Track a.mp3"


def test_index_round_trips_through_a_new_instance(library: Library) -> None:
    track = make_track(library, "a", album="Album")
    library.add(track)

    again = Library(library.library_dir, library.index_path)
    assert again.get("fake:a") == track


def test_corrupt_index_is_moved_aside_and_library_starts_empty(tmp_settings: Settings) -> None:
    index = tmp_settings.index_path
    index.write_text("{this is not json", encoding="utf-8")

    library = Library(tmp_settings.library_dir, index)

    assert library.all() == []
    assert index.with_name(index.name + ".corrupt").exists()
    library.add(make_track(library, "a"))
    assert json.loads(index.read_text(encoding="utf-8"))["tracks"]["fake:a"]["id"] == "fake:a"


def test_index_with_wrong_shape_is_treated_as_corrupt(tmp_settings: Settings) -> None:
    index = tmp_settings.index_path
    index.write_text(json.dumps({"version": 1, "tracks": [1, 2, 3]}), encoding="utf-8")
    library = Library(tmp_settings.library_dir, index)
    assert library.all() == []


def test_unreadable_entries_are_skipped(tmp_settings: Settings) -> None:
    index = tmp_settings.index_path
    good = make_track(Library(tmp_settings.library_dir, index), "ok").to_dict()
    index.write_text(
        json.dumps({"version": 1, "tracks": {"fake:ok": good, "bad": {"id": "bad"}}}),
        encoding="utf-8",
    )
    library = Library(tmp_settings.library_dir, index)
    assert [t.id for t in library.all()] == ["fake:ok"]


def test_hand_edited_entries_with_null_fields_are_repaired(tmp_settings: Settings) -> None:
    """A null title/artist in library.json must not turn every search into a 500."""
    index = tmp_settings.index_path
    base = make_track(Library(tmp_settings.library_dir, index), "ok").to_dict()
    odd = dict(base, id="fake:odd", title=None, artist=None, album=7, path="sub/Some Song.mp3")
    no_path = dict(base, id="fake:nopath", path="")
    no_id = dict(base, id=None)
    index.write_text(
        json.dumps(
            {
                "version": 1,
                "tracks": {"fake:ok": base, "fake:odd": odd, "fake:nopath": no_path, "x": no_id},
            }
        ),
        encoding="utf-8",
    )
    library = Library(tmp_settings.library_dir, index)
    assert sorted(t.id for t in library.all()) == ["fake:odd", "fake:ok"]
    repaired = library.get("fake:odd")
    assert (repaired.title, repaired.artist, repaired.album) == ("Some Song", "Unknown Artist", "7")
    assert [t.id for t in library.search("some song")] == ["fake:odd"]
    assert [t.id for t in library.search("unknown")] == ["fake:odd"]
    assert library.search("zzz") == []


def test_hand_edited_null_added_at_and_odd_numbers_are_repaired(tmp_settings: Settings) -> None:
    """added_at: null used to make all()/search() raise (every library endpoint answered 500)
    and a string duration broke the M3U8 export."""
    index = tmp_settings.index_path
    probe = Library(tmp_settings.library_dir, index)
    base = make_track(probe, "ok").to_dict()
    odd = dict(
        base,
        id="fake:odd",
        path="odd.mp3",
        title="Odd one",
        added_at=None,
        duration="3.5",
        file_size="12",
        has_cover=None,
    )
    junk = dict(base, id="fake:junk", path="junk.mp3", duration="fast", file_size=None)
    index.write_text(
        json.dumps({"version": 1, "tracks": {"fake:ok": base, "fake:odd": odd, "fake:junk": junk}}),
        encoding="utf-8",
    )
    library = Library(tmp_settings.library_dir, index)
    ids = [t.id for t in library.all()]
    assert sorted(ids) == ["fake:junk", "fake:odd", "fake:ok"]
    assert [t.id for t in library.search("")] == ids
    assert [t.id for t in library.search("odd")] == ["fake:odd"]
    repaired = library.get("fake:odd")
    assert isinstance(repaired.added_at, str) and repaired.added_at
    assert repaired.duration == 3.5 and repaired.file_size == 12 and repaired.has_cover is False
    assert library.get("fake:junk").duration is None and library.get("fake:junk").file_size == 0
    text = m3u8_text(Playlist(name="p", track_ids=ids), library, relative_to=None)
    assert "#EXTINF:3,Fake Artist - Odd one" in text  # "3.5" -> 3
    assert "#EXTINF:-1,Fake Artist - Track ok" in text  # the "fast" duration
    assert text.count("#EXTINF:") == 3


def test_missing_library_dir_keeps_the_index_and_reports_it(
    tmp_path: Path, tmp_settings: Settings
) -> None:
    library = Library(tmp_path / "does-not-exist", tmp_settings.index_path)
    assert library.all() == []
    assert not library.has("fake:a")
    with pytest.raises(LibraryUnavailable) as excinfo:
        library.rescan()
    assert "does-not-exist" in str(excinfo.value)
    assert isinstance(excinfo.value, OSError)


def test_rescan_with_unplugged_folder_does_not_wipe_the_index(
    library: Library, tmp_path: Path
) -> None:
    """One click on Rescan while the drive is out must not drop every entry (and every playlist)."""
    library.add(make_track(library, "a"))
    library.add(make_track(library, "b"))
    saved = library.index_path.read_text(encoding="utf-8")

    library.set_library_dir(tmp_path / "unplugged-drive")
    with pytest.raises(LibraryUnavailable):
        library.rescan()

    assert sorted(t.id for t in library.all()) == ["fake:a", "fake:b"]
    assert library.index_path.read_text(encoding="utf-8") == saved  # nothing was written
    library.set_library_dir(tmp_path / "lib")  # drive plugged back in
    assert library.rescan() == 0
    assert library.has("fake:a") and library.has("fake:b")


# -- rescan ----------------------------------------------------------------------------------


def test_rescan_recovers_id_from_txxx_tag(library: Library) -> None:
    write_tiny_mp3(
        library.library_dir / "Someone - Something.mp3",
        artist="Someone",
        title="Something",
        album="Somewhere",
        track_id="youtube:abc123",
    )

    assert library.rescan() == 1

    track = library.get("youtube:abc123")
    assert track is not None
    assert track.provider == "youtube"
    assert track.source_id == "abc123"
    assert track.path == "Someone - Something.mp3"
    assert (track.artist, track.title, track.album) == ("Someone", "Something", "Somewhere")
    assert track.duration is not None and 0.2 < track.duration < 0.5
    assert track.file_size > 0
    assert track.has_cover is False
    # persisted
    assert Library(library.library_dir, library.index_path).get("youtube:abc123") is not None


def test_rescan_assigns_local_id_without_our_tag(library: Library) -> None:
    write_tiny_mp3(library.library_dir / "Some Band - Some Song.mp3", artist="", title="")
    (library.library_dir / "sub").mkdir()
    write_tiny_mp3(library.library_dir / "sub" / "untagged.mp3", artist="", title="")

    assert library.rescan() == 2

    expected_1 = local_track_id("Some Band - Some Song.mp3")
    expected_2 = local_track_id("sub/untagged.mp3")
    assert expected_1.startswith("local:") and len(expected_1) == len("local:") + 12
    one = library.get(expected_1)
    two = library.get(expected_2)
    assert one is not None and two is not None
    assert one.provider == "local"
    # no tags: artist/title come from the "Artist - Title" file name
    assert (one.artist, one.title) == ("Some Band", "Some Song")
    assert (two.artist, two.title) == ("Unknown Artist", "untagged")
    assert two.path == "sub/untagged.mp3"
    # a second rescan is a no-op
    assert library.rescan() == 0


def test_rescan_drops_entries_whose_file_vanished(library: Library) -> None:
    keep = make_track(library, "keep")
    gone = make_track(library, "gone")
    library.add(keep)
    library.add(gone)
    library.abs_path(gone).unlink()

    assert library.rescan() == 1
    assert library.get("fake:gone") is None
    assert library.has("fake:keep")


def test_rescan_skips_incoming_and_playlists_folders_and_non_audio(library: Library) -> None:
    write_tiny_mp3(library.library_dir / ".incoming" / "partial.mp3")
    write_tiny_mp3(library.library_dir / "Playlists" / "stray.mp3")
    (library.library_dir / "notes.txt").write_text("not audio", encoding="utf-8")
    (library.library_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    write_tiny_mp3(library.library_dir / "real.mp3", track_id="fake:real")

    assert library.rescan() == 1
    assert [t.id for t in library.all()] == ["fake:real"]


def test_rescan_does_not_clobber_an_existing_id(library: Library) -> None:
    library.add(make_track(library, "dup"))
    # a second copy carrying the same ULTIMATE_PLAYLIST_ID tag must not replace the original
    write_tiny_mp3(library.library_dir / "copy.mp3", track_id="fake:dup")

    assert library.rescan() == 1
    assert library.get("fake:dup").path == "Fake Artist - Track dup.mp3"
    assert library.get(local_track_id("copy.mp3")) is not None


def test_rescan_after_repointing_the_folder(library: Library, tmp_path: Path) -> None:
    library.add(make_track(library, "a"))
    other = tmp_path / "other"
    write_tiny_mp3(other / "x.mp3", track_id="fake:x")

    library.set_library_dir(other)
    assert library.rescan() == 2  # one dropped, one picked up
    assert [t.id for t in library.all()] == ["fake:x"]


def test_rescan_picks_up_m4a_opus_flac(library: Library) -> None:
    """Tags, duration, cover flag and our id tag are read from every supported container."""
    for fmt in ("m4a", "opus", "flac"):
        write_tiny_audio(
            library.library_dir / f"{fmt.upper()} Artist - {fmt} song.{fmt}",
            artist=f"{fmt.upper()} Artist",
            title=f"{fmt} song",
            album=f"{fmt} album",
            track_id=f"youtube:{fmt}0000000",
            cover=FAKE_PNG if fmt != "opus" else None,
        )
    write_tiny_audio(library.library_dir / "untagged.opus", artist="", title="", cover=FAKE_PNG)

    assert library.rescan() == 4

    for fmt in ("m4a", "opus", "flac"):
        track = library.get(f"youtube:{fmt}0000000")
        assert track is not None, fmt
        assert track.provider == "youtube" and track.source_id == f"{fmt}0000000"
        assert track.path == f"{fmt.upper()} Artist - {fmt} song.{fmt}"
        assert (track.artist, track.title, track.album) == (
            f"{fmt.upper()} Artist",
            f"{fmt} song",
            f"{fmt} album",
        )
        assert track.duration is not None and 0.05 < track.duration < 0.5, fmt
        assert track.has_cover is (fmt != "opus"), fmt
        assert track.file_size > 0
    untagged = library.get(local_track_id("untagged.opus"))
    assert untagged is not None
    assert (untagged.artist, untagged.title) == ("Unknown Artist", "untagged")
    assert untagged.has_cover is True  # METADATA_BLOCK_PICTURE counts
    assert library.rescan() == 0


# -- cover art -------------------------------------------------------------------------------


def test_cover_bytes_from_apic(library: Library) -> None:
    track = make_track(library, "a")
    tags = ID3(library.abs_path(track))
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover", data=FAKE_PNG))
    tags.save()
    library.add(track)

    result = library.cover_bytes("fake:a")
    assert result == (FAKE_PNG, "image/png")


@pytest.mark.parametrize("fmt", AUDIO_FORMATS)
def test_cover_bytes_per_format(library: Library, fmt: str) -> None:
    """APIC / MP4 covr / FLAC picture / Opus METADATA_BLOCK_PICTURE all yield (bytes, mime)."""
    write_tiny_audio(
        library.library_dir / f"With Art - Cover.{fmt}",
        track_id=f"fake:cover-{fmt}",
        cover=FAKE_PNG,
    )
    write_tiny_audio(library.library_dir / f"No Art - Plain.{fmt}", track_id=f"fake:plain-{fmt}")
    assert library.rescan() == 2
    assert library.get(f"fake:cover-{fmt}").has_cover is True
    assert library.get(f"fake:plain-{fmt}").has_cover is False
    assert library.cover_bytes(f"fake:cover-{fmt}") == (FAKE_PNG, "image/png")
    assert library.cover_bytes(f"fake:plain-{fmt}") is None


def test_cover_bytes_none_without_cover_or_track(library: Library) -> None:
    track = make_track(library, "a")
    library.add(track)
    assert library.cover_bytes("fake:a") is None
    assert library.cover_bytes("fake:unknown") is None
    library.abs_path(track).unlink()
    assert library.cover_bytes("fake:a") is None


def test_image_mime_trusts_only_image_types() -> None:
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 8
    assert image_mime("image/png", FAKE_PNG) == "image/png"
    assert image_mime("IMAGE/JPEG ", jpeg) == "image/jpeg"
    assert image_mime("text/html", b"<script>") == "image/jpeg"  # never a browser-runnable type
    assert image_mime("text/html", FAKE_PNG) == "image/png"  # sniffed from the bytes
    assert image_mime("", jpeg) == "image/jpeg"
    assert image_mime(None, b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert image_mime("application/octet-stream", b"GIF89a") == "image/gif"
    assert image_mime("image/png; charset=utf-8", b"???") == "image/jpeg"


def test_read_cover_replaces_a_dangerous_mime(library: Library) -> None:
    track = make_track(library, "a")
    tags = ID3(library.abs_path(track))
    tags.add(APIC(encoding=3, mime="text/html", type=3, desc="Cover", data=b"<html>"))
    tags.save()
    assert read_cover(library.abs_path(track)) == (b"<html>", "image/jpeg")


def test_rescan_detects_cover(library: Library) -> None:
    path = write_tiny_mp3(library.library_dir / "with cover.mp3", track_id="fake:c")
    tags = ID3(path)
    tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=b"\xff\xd8\xff\xd9"))
    tags.save()

    library.rescan()
    assert library.get("fake:c").has_cover is True
    assert library.cover_bytes("fake:c") == (b"\xff\xd8\xff\xd9", "image/jpeg")
