# Architecture

For contributors. `models.py` and `providers/base.py` are frozen: the types every module talks
in and the `Provider` protocol. Everything else is described here and in the module docstrings.
The second half of this page is the guide for adding a source, with YouTube, Spotify, TikTok
and SoundCloud as the worked examples.

Two rules keep the codebase small:

1. **Everything talks in the types from `models.py`** (`TrackRef`, `Track`, `Job`, `Playlist`,
   `ProgressEvent`). Those types, and the `Provider` protocol in `providers/base.py`, are frozen.
2. **Only the provider layer knows where audio comes from.** `downloader.py`, `library.py`,
   `server/`, `cli.py` never import `yt_dlp`, never look at a URL's hostname, never care whether a
   track came from YouTube or Spotify. Spotify support is four modules under `providers/` plus
   tests; the library, the playlists and the player did not change for it, and the queue only
   learned to label a parent job with the provider's `TrackRef.extra["container"]`. (The
   settings dialog and `up config` arrived in the same release because the Spotify Client ID
   needed a home, but they are generic. The one Spotify-specific piece outside `providers/` is
   the "Connect Spotify" sign-in: the `/api/spotify` routes in `server/app.py` and
   `up spotify` in `cli.py`, both thin callers of `providers/spotify_auth.py`.)

## Module map

```text
src/ultimate_playlist/
  __init__.py                 __version__ (shown by `up --version` and GET /api/status)
  __main__.py                 `python -m ultimate_playlist` -> cli.main()
  cli.py                      argparse CLI, prog "up": serve, add, list, doctor, rescan, playlists, export,
                              config, spotify; `config set` / `spotify logout` go through a running app
  config.py                   Settings dataclass (config.json), the value rules shared by the API and
                              `up config set` (clean_client_id, ...), app_data_dir() (~/.ultimate-playlist)
  bundled.py                  tools shipped next to the packaged exe (<exe dir>/bin) win over PATH
  ffmpeg.py                   find_ffmpeg() / install_hint(); nobody else shells out to ffmpeg
  logs.py                     console + rotating app.log handlers; keeps yt-dlp chatter and UI polling off the terminal
  models.py                   FROZEN: TrackRef, Track, JobStatus, ProgressEvent, Job, Playlist
  downloader.py               JobManager: resolver thread + worker threads, the job state machine; a parent
                              job is labelled with the provider's container name (TrackRef.extra["container"])
  library.py                  Library: index of finished tracks (library.json), tag reading, rescan, covers
  playlists.py                PlaylistStore (playlists.json) and export_m3u8()
  providers/__init__.py       registry: PROVIDERS, register/unregister, get_provider, provider_by_name, configure, doctor_all
  providers/base.py           FROZEN: Provider protocol, ProviderError / ProviderNotAvailable / DownloadCancelled
  providers/youtube.py        YouTubeProvider (yt-dlp): resolve, download, convert, tag; the pipeline TikTok and
                              SoundCloud reuse
  providers/tiktok.py         TikTokProvider(YouTubeProvider): TikTok link shapes, short-link expansion, naming, error texts
  providers/soundcloud.py     SoundCloudProvider(YouTubeProvider): SoundCloud link shapes, short links, sets, naming,
                              the Go+ refusal, the 500x500 cover, error texts; audio only
  providers/spotify.py        SpotifyProvider: Spotify links -> metadata -> YouTube Music match -> YouTubeProvider.download -> re-tag
  providers/spotify_meta.py   Spotify metadata: link parsing, the public embed pages, the Web API as the connected user
  providers/spotify_auth.py   "Connect Spotify": Authorization Code + PKCE with the user's Client ID, the token file, renewal
  providers/ytmusic_match.py  YouTube Music search (imports yt_dlp too) + candidate scoring (find_match); the scoring is pure
  server/app.py               create_app() -> FastAPI (incl. the /api/spotify routes), serve() -> uvicorn bound to 127.0.0.1
  server/static/              index.html, app.js, style.css: vanilla, no build step, no CDN; settings dialog
tests/                        offline pytest suite; conftest.py fixtures; fake_provider.py
```

## Data flow: paste -> get_provider -> resolve -> jobs -> download -> Library -> UI

```text
 user pastes a URL          (web UI textbox -> POST /api/jobs {url, media, quality, folder},
        |                    or `up add [--video --quality Q --folder F] URL`)
        v
 JobManager.submit(url, media=, quality=, folder=)   (bad values -> ValueError -> HTTP 400)
        |  providers.get_provider(url): normalises the URL (strips whitespace, adds https://
        |  when there is no scheme; `spotify:` URIs pass through), then asks each provider in
        |  PROVIDERS order `matches(url)`; the first True wins.
        |  None -> ValueError("No provider for this link") -> HTTP 400 / CLI error.
        v
 Job(status=QUEUED, provider=<name>)  ->  resolve queue
        |
        v
 resolver thread: job.status = RESOLVING; refs = provider.resolve(url)
        |   media "video": refs become as_video_ref(ref, quality) (track id + ":video",
        |                  extra media/quality); a provider without `supports_video` (Spotify,
        |                  SoundCloud) keeps audio and the job says "<Provider> links are saved
        |                  as audio"
        |   0 refs -> ERROR  "Nothing to download at that link"
        |   1 ref  -> the same job gets job.track_ref and moves to the download queue
        |   N refs -> the job becomes a parent (status DONE, child_count=N,
        |             message "Playlist: N tracks", or the provider's container
        |             label such as "Album: <name> (N tracks)") and N child jobs
        |             (parent_id set) are created and queued in playlist order
        v
 worker threads (settings.concurrency of them)
        |   library.has(ref.track_id)  -> SKIPPED "Already in library"   (no download)
        |   another live job owns the same track_id -> SKIPPED "Already in the queue"
        |                                 (revived automatically if that job does not end DONE)
        |   job.status = DOWNLOADING
        |   track = provider.download(ref, settings.library_dir, settings, progress, cancel)
        |        progress(ProgressEvent) -> copied onto the job (status/progress/speed/eta/message),
        |                                   throttled to ~4 updates/s
        |        cancel: a per-job threading.Event; cancel(job_id) sets it, the provider raises
        |                DownloadCancelled -> job CANCELLED
        |        ProviderError -> ERROR with str(exc); anything else -> ERROR "Unexpected error: ..."
        v
 library.add(track)  -> library.json (atomic write)      job.track = track; job.status = DONE
        |   job.folder: library.move_tracks([track.id], folder); a failed move leaves the file in
        |               Unsorted and the job DONE with "Saved to Unsorted, not <folder>: <why>"
        |
        v
 UI: polls GET /api/jobs (1 s while something is active, else 5 s) and GET /api/library?q=;
     plays GET /media/{id}; shows GET /api/library/{id}/cover; playlists via /api/playlists
     and export_m3u8() -> <library>/Playlists/<name>.m3u8
```

Things worth knowing about that flow:

- `dest_dir` passed to `download()` is the **library root**. The provider writes intermediate
  files under `<library>/.incoming/dl-*/` (a folder of its own per download; short path,
  Windows MAX_PATH matters) and moves the finished file into the root with `os.replace`.
  `JobManager.start()` first runs `sweep_stale_staging(library)`: it removes the `dl-*` and
  `sp-*` folders in `.incoming` that a crash, a hard kill or a file Windows would not let go of
  left behind, but only those in which nothing (file or folder, at any depth) was modified for
  `STALE_STAGING_AGE`, 12 hours. The age is what keeps it safe: `up add` may be downloading
  into the same library next to the server, and a running download writes all the time.
  Anything else in `.incoming` is left alone, and a folder that can't be read is kept.
- De-duplication is by `TrackRef.track_id` (`"<provider>:<source_id>"`), checked right before the
  download. The same YouTube video, or the same Spotify track, is therefore downloaded once, no
  matter how many playlists contain it. There is no de-duplication *across* providers: the same
  song reached through a YouTube link and through a Spotify link is two ids and two files.
- The `Library` is thread-safe (one `RLock`) and every mutation on a `Job` happens under the
  `JobManager` lock. Providers themselves are called from worker threads, so `download()` must be
  re-entrant across instances of work (no shared mutable state on `self` without a lock).

## The frozen types (`models.py`)

| Type | Role |
| --- | --- |
| `TrackRef(provider, source_id, url, title?, artist?, album?, duration?, thumbnail_url?, extra={})` | "Something a provider can turn into exactly one audio file." Produced by `resolve()`, consumed by `download()`. `track_id` property = `f"{provider}:{source_id}"`. `extra` is a free dict for provider-private data (a Spotify ISRC, the matched YouTube id, ...) and is serialised into `/api/jobs` as-is, so keep it JSON-safe and small. |
| `Track(id, provider, source_id, source_url, title, artist, path, album?, duration?, has_cover, file_size, added_at)` | A finished file in the library. `id` must equal the ref's `track_id`. `path` is relative to the library root with forward slashes. `to_dict()` / `from_dict()` (unknown keys ignored) are what `library.json` stores. |
| `JobStatus` | `queued, resolving, downloading, converting, done, skipped, error, cancelled`; `is_terminal` for the last four. |
| `ProgressEvent(status, progress?, speed?, eta?, message?)` | What a provider emits through the `progress` callback. `progress` is 0.0..1.0, `speed` bytes/s, `eta` seconds. |
| `Job(url, provider?, id, status, progress, speed, eta, message, error, parent_id, child_count, track_ref?, track?, created_at, updated_at, media="audio", quality="", folder="")` | One unit of queue work. A playlist becomes a parent job plus one child per track. `title` property falls back ref title -> URL. `to_dict()` is the `/api/jobs` payload. |
| `Playlist(name, id, track_ids, created_at, updated_at)` | Ordered list of track ids; dangling ids are pruned against the library. |

IDs for tracks are always `<provider>:<source_id>` (`youtube:<video id>`, `spotify:<track id>`);
`local:<sha1 of relative path>[:12]` is used by `Library.rescan()` for files that carry no
`ULTIMATE_PLAYLIST_ID` tag. Job and playlist ids are random 12-hex strings (`new_id()`).

The three `Job` fields at the end (0.4) are the download options picked next to the Download
button: `media` ("audio" / "video"), `quality` (a key of `config.VIDEO_QUALITIES`, "" for
audio) and `folder` (where the finished file goes, "" = Unsorted). Children of a playlist and a
retried job copy them. `Track` did not change: whether a track is a video, and which folder it
is in, follow from its `path` (`library.media_of`, `library.folder_of`), and the API adds them
to every track it hands out (`server.app.track_json`: `media`, `folder`).

## Folders and videos (0.4)

- **Folders** are real directories directly under the library folder; Unsorted (`""`) is the
  library folder itself; a track's folder is the first part of its `path`. `Library.folders()`
  lists them (directories, plus any folder a track path names; never the app's own, never
  trash, sync-tool or system ones: a short list of names (`$RECYCLE.BIN`, `System Volume
  Information`, `.Trashes`, `.stversions`, `@eaDir`, `#recycle`...) and on Windows a
  directory that is both hidden and system, as the folders Windows protects are; elsewhere
  also a dot followed by a letter. Hidden alone, or a dot alone, hides nothing: a folder
  hidden in Explorer or by a NAS, "...And Justice for All" and ".38 Special" are folders.
  A rescan does not walk into the skipped ones. It drops index entries only inside a folder
  on the name list (copies an older version indexed there), never because of a folder's
  attributes, so no track loses its place in a playlist that way; a folder the index names
  stays listed while it holds tracks). Folder names
  match the way Windows compares them (`library.folder_key`: case-insensitive one character
  at a time, so "Flight" is "flight" but "Straße" is not "Strasse", which `str.casefold`
  would merge); the spelling on disk wins, and `canonical_folders()` / `folder_name_of()`
  give every track that spelling. `create_folder`, `rename_folder` (renames the directory,
  rewrites the paths under it in any case), `delete_folder` (moves its tracks back to
  Unsorted, removes the directory and the empty subfolders that leaves without following or
  removing a junction or link, and sets `MoveResult.note` when other files keep it) and
  `move_tracks(ids, folder, follow_renames=False)`. A move never overwrites (" (2)", " (3)"...), except a stale copy
  of the very track (same id in its tags, not in the index: a re-download after "Remove from
  library"), retries a few times on Windows' "file in use" (the player streams with range
  requests, so a file is open only for moments) and reports per track what could not move.
  Existing folders are looked up by their listed name first (`resolve_folder`), so a folder
  made in Explorer ("My  Mix", 90 characters) works as it is; `clean_folder_name` applies to
  new names only (Windows-illegal characters, reserved device names, a leading or trailing
  dot, the app's own folders, 80 characters), and its `LibraryFolderError` is a `ValueError`
  whose message the API returns with 400. Rename and remove refuse while the library folder
  is missing (409), so an unplugged drive is never re-created empty for the next rescan to
  wipe; `create_folder` creates the library folder only on a fresh install (an empty index).
  Ids of files that carry none in their tags are `local:<hash of the path>`; a new file at a
  path a moved track left gets another one. Providers still write into the library folder
  itself; a download with a folder is moved there afterwards by the `JobManager`, which also
  moves a track the job finds already in Unsorted (one in another folder stays, and the job
  says where) and re-targets live jobs when a folder is renamed or removed
  (`JobManager.retarget_folder`, called by the API routes). The routes can only do that
  after the library has renamed or removed the folder, and a download may finish in between,
  so the library itself remembers the folders renamed or removed while the app runs
  (`folder_key(old) -> new name`, "" when removed, also when other files keep the folder
  on disk). The downloader's moves pass `follow_renames=True`: under the library's lock they
  follow a rename to the new name, and for a removed folder they fail, so the file stays in
  Unsorted and the queue row says why, instead of bringing the old folder back. The memory
  of a name ends when the user picks an existing folder of that name for new downloads
  (`JobManager.submit` calls `Library.pick_folder`), makes it, moves tracks into it or renames
  a folder to it, and when the library folder changes. A picked name that is not a folder now
  is a stale one (a second tab, a link pasted while the rename ran): `pick_folder` gives a
  renamed folder's new name, and a removed one stays removed. Whether a folder of that name exists is not the test: a folder
  kept by a `desktop.ini`, or made again in Explorer, cannot tell a download that raced the
  removal from one picked afterwards. Otherwise the folder is made when the first file lands
  (nothing is made for a download that fails, and a library folder that was missing at
  submit or a retry is no problem).
- **Videos**: `as_video_ref` gives the video its own id (`youtube:<id>:video`), so the MP3 and
  the MP4 of one video are two library items and de-duplication still works per kind.
  `YouTubeProvider.download` reads `ref.extra["media"]` / `["quality"]`; for a video it asks
  yt-dlp for `ydl_video_format` (YouTube `bv*+ba/b`: best video plus best audio, merged; TikTok
  one file with picture and sound, not the watermarked copy, never sound only) with
  `format_sort` from `VIDEO_FORMAT_SORT` (best: `vcodec:h264,res,acodec:aac`, the highest
  resolution in H.264 with AAC, which plays everywhere; max: `res:2160,fps`, never 8K;
  720 / 480: `res:N,vcodec:h264,acodec:aac`, a preference, so a site with nothing that small
  gives its smallest), `merge_output_format` mp4 and `FFmpegVideoRemuxer` instead of
  `FFmpegExtractAudio`. Naming, tagging (MP4 atoms) and the move into place are the audio
  pipeline's. Every download (audio or video) works in its own `.incoming/dl-*` folder,
  removed in a `finally` (`remove_staging_dir`): the MP3 and the MP4 of one video may run at
  once, and yt-dlp names its files after the video id alone. After a failure or a cancel a file
  in it may still be open: yt-dlp's HLS downloader (FragmentFD, which SoundCloud's streams
  use) does not close its `.part` when a progress hook raises `DownloadCancelled`, and the file
  stays reachable from the exception's frames (its fragment progress hook is a closure over the
  context that holds the file, and a frame keeps its function even after `frame.clear()`) and
  from a reference cycle. Windows can't delete an open file, so `_release_frames` clears the
  frames of the exception and of those it came from (cause, context, yt-dlp's `exc_info`) and
  cuts their tracebacks below `download()`, then `gc.collect()` runs (only when the folder holds
  something) before the `rmtree`, which is tried once more after `STAGING_RETRY_DELAY` if the
  folder is still there. An unexpected error is logged before that, so no traceback anyone
  reads is lost. Whatever still stays is swept by a start at least 12 hours later (above). The tests run each quality through yt-dlp's own
  selection on YouTube- and TikTok-shaped format lists. `Library` picks up
  `.mp4 .m4v .webm .mkv .mov` on a rescan. A provider whose site has no video says so with
  `supports_video = False`: SoundCloud sets it (a `YouTubeProvider` subclass inherits `True`),
  and Spotify has no such attribute, which the `JobManager` reads as False. The resolver then
  turns a Video request into audio (`media` "audio", no quality) and the job keeps the note
  "<display name> links are saved as audio", on a set's parent row after its container label
  ("Album: out of spite (8 tracks); SoundCloud links are saved as audio").
- **API**: `GET /api/folders` -> `{unsorted, folders: [{name, count}]}`; `POST /api/folders
  {name}`; `PATCH /api/folders/{name} {name}`; `DELETE /api/folders/{name}` (tracks back to
  Unsorted; `note` when the folder stays on disk); `POST /api/library/move {track_ids,
  folder}` -> `{moved, failed: [{id, error}], note, unsorted, folders}`. Rename and remove
  answer 409 while the library folder is missing. Tracks in `/api/library` and in the job
  routes carry `media` and `folder`. `/media/{id}` serves videos as `video/mp4` (or webm /
  matroska / quicktime).
- **UI**: the download bar has the Audio / Video toggle, the quality menu (video only) and Save
  to (remembered in `localStorage`, key `ultimate-playlist.download`). The Library tab has the
  folder rail (All tracks, Unsorted, folders; rows drop onto it), Move to folder in the
  selection bar and on every row. The player's one media element is a `<video>` inside
  `#video-panel`, which covers the library while a video plays; with the panel hidden it plays
  audio and video alike.

## The Provider protocol (`providers/base.py`)

```python
ProgressCallback = Callable[[ProgressEvent], None]


# message must be readable by a non-programmer
class ProviderError(Exception): ...


# exists but cannot run (dependency / credentials)
class ProviderNotAvailable(ProviderError): ...


# raised inside download() when cancel.is_set()
class DownloadCancelled(ProviderError): ...


@runtime_checkable
class Provider(Protocol):
    name: str  # short lowercase slug, e.g. "youtube"; first half of every Track id
    display_name: str  # e.g. "YouTube"

    def matches(self, url: str) -> bool: ...
    def resolve(self, url: str) -> list[TrackRef]: ...
    def download(
        self,
        ref: TrackRef,
        dest_dir: Path,
        settings: Settings,
        progress: ProgressCallback,
        cancel: threading.Event,
    ) -> Track: ...
    def doctor(self) -> list[tuple[bool, str, str]]: ...
```

The contract, method by method:

- **`matches(url)`** must be fast and offline: a regex on hostname and path, nothing more. Return
  True only for URLs this provider should own. `get_provider` has already normalised the URL
  (whitespace stripped, `https://` prepended when there is no scheme; `spotify:` URIs are passed
  through untouched). If `matches` raises, the registry logs and treats it as False.
- **`resolve(url)`** may hit the network. Return one `TrackRef` per track: one element for a
  single track, one per entry for a playlist or album. Drop unavailable entries silently (private,
  deleted, local files); raise `ProviderError` with a friendly message for a bad or unsupported
  URL. Fill in `title`/`artist`/`album`/`duration`/`thumbnail_url` when they are cheap to get:
  the UI shows the title while the download is queued.
- **`download(ref, dest_dir, settings, progress, cancel)`** produces exactly one finished, tagged
  audio file inside `dest_dir` and returns its `Track`. It must call `progress()` at least on every
  status change (DOWNLOADING -> CONVERTING), check `cancel.is_set()` regularly and raise
  `DownloadCancelled` after cleaning up partial files, and raise `ProviderError` (friendly text)
  on any failure. Anything else that escapes is reported as "Unexpected error" and logged with a
  stack trace, which is a bug in the provider.
- **`doctor()`** returns `(ok, label, detail)` checks explaining what is missing and how to fix
  it. It must never raise; the registry wraps it anyway, but a raise turns into an ugly message.

The registry (`providers/__init__.py`):

```python
PROVIDERS: list[Provider]                       # order matters: first match wins
def register(provider: Provider) -> None        # insert at the front (tests, plugins)
def unregister(name: str) -> None
def normalize_url(url: str) -> str
def get_provider(url: str) -> Provider | None
def provider_by_name(name: str) -> Provider | None
def configure(settings: Settings) -> None       # calls provider.configure(settings) where it exists
def run_doctor(provider: Provider, settings: Settings | None = None) -> list[tuple[bool, str, str]]
def doctor_all(settings: Settings | None = None) -> dict[str, list[tuple[bool, str, str]]]
```

`configure(settings)` is called once by `create_app()` and by `up add`, so a provider whose
`resolve()` / `doctor()` depend on the live settings (ffmpeg path, JS runtimes, the Spotify
Client ID) can implement an optional `configure(self, settings)`; `run_doctor` passes
`settings` to a `doctor(self, settings)` that takes one and calls the plain `doctor(self)`
otherwise.

`_load_builtin()` runs at import time and instantiates
`ultimate_playlist.providers.youtube.YouTubeProvider`,
`ultimate_playlist.providers.spotify.SpotifyProvider`,
`ultimate_playlist.providers.tiktok.TikTokProvider` and
`ultimate_playlist.providers.soundcloud.SoundCloudProvider`, in that order (no two of them
claim the same host, so the order decides nothing between them). A provider whose import or
constructor raises is logged and **left out of the registry entirely**, so keep heavy or optional
imports inside the provider's own modules and report their absence through `doctor()` instead of
failing at import time.

## The JSON files

All of them live in `app_data_dir()` (`~/.ultimate-playlist`, or `$ULTIMATE_PLAYLIST_HOME`), are
written atomically (`.tmp` then `os.replace`), and store paths as strings.

`config.json` (`Settings.to_dict()`; a missing or corrupt file means defaults, never a crash;
unknown keys are ignored on load, so adding fields is backwards compatible):

```json
{
  "library_dir": "C:\\Users\\you\\Music\\Ultimate Playlist",
  "audio_format": "mp3",
  "audio_quality": "0",
  "ffmpeg_path": null,
  "concurrency": 2,
  "embed_cover": true,
  "js_runtimes": ["deno", "node"],
  "spotify_client_id": ""
}
```

`spotify_client_id` is optional (`""` when unset): the Client ID of the user's own Spotify
developer app. A Client ID is not a secret (it travels in the browser's address bar during the
sign-in), so nothing in `config.json` is sensitive and `GET /api/settings`, the settings dialog
and `up config show` hand out `Settings.to_dict()` as it is. Files from pre-release 0.2 builds
(development builds from the main branch before the 0.2.0 release) may still carry
`spotify_client_secret`: `from_dict` ignores unknown keys and the next save drops
it. The value rules (`AUDIO_FORMATS`, `clean_audio_quality`, `clean_concurrency`,
`clean_js_runtimes`, `clean_client_id`, ...) live in `config.py` so `PUT /api/settings` and
`up config set` can never disagree. A running app keeps its settings in memory and saves them
from there, so `up config set` hands the change to it (`PUT /api/settings`) when it finds one
(port 8765 and the next ten, or `--port`), and writes the file itself only when none runs.

`library.json` (`Library`; keyed by track id):

```json
{
  "version": 1,
  "tracks": {
    "youtube:dQw4w9WgXcQ": {
      "id": "youtube:dQw4w9WgXcQ",
      "provider": "youtube",
      "source_id": "dQw4w9WgXcQ",
      "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
      "title": "Never Gonna Give You Up",
      "artist": "Rick Astley",
      "path": "Rick Astley - Never Gonna Give You Up.mp3",
      "album": "Whenever You Need Somebody",
      "duration": 213.1,
      "has_cover": true,
      "file_size": 5123456,
      "added_at": "2026-09-11T10:15:00+00:00"
    },
    "spotify:4PTG3Z6ehGkBFwjybzWkR8": {
      "id": "spotify:4PTG3Z6ehGkBFwjybzWkR8",
      "provider": "spotify",
      "source_id": "4PTG3Z6ehGkBFwjybzWkR8",
      "source_url": "https://open.spotify.com/track/4PTG3Z6ehGkBFwjybzWkR8",
      "title": "Never Gonna Give You Up",
      "artist": "Rick Astley",
      "path": "Rick Astley - Never Gonna Give You Up (2).mp3",
      "album": "Whenever You Need Somebody",
      "duration": 213.6,
      "has_cover": true,
      "file_size": 5130000,
      "added_at": "2026-09-13T18:40:00+00:00"
    }
  }
}
```

(The second entry shows the cross-provider case: the same song from a Spotify link is a second
id and a second file, named ` (2)` because the plain name was taken by a different track id.)

`playlists.json` (`PlaylistStore`):

```json
{
  "version": 1,
  "playlists": [
    {
      "name": "Road trip",
      "id": "3f9c2a1b7d4e",
      "track_ids": ["youtube:dQw4w9WgXcQ", "spotify:4PTG3Z6ehGkBFwjybzWkR8", "local:9a1f0c3b2e7d"],
      "created_at": "2026-09-11T10:20:00+00:00",
      "updated_at": "2026-09-11T10:25:00+00:00"
    }
  ]
}
```

`spotify_auth.json` exists only while a Spotify account is connected. `providers/spotify_auth.py`
alone reads and writes it (a `.tmp` file created with mode `0o600`, then `os.replace`):

```json
{
  "refresh_token": "...",
  "access_token": "...",
  "expires_at": 1789500000.0,
  "scope": "playlist-read-private playlist-read-collaborative user-library-read",
  "user_id": "...",
  "display_name": "...",
  "client_id": "..."
}
```

The tokens leave this file only in requests to Spotify: no API returns them, nothing logs or
prints them, `SpotifyAccount` (what the rest of the app gets) has no field for them, and
`provider_status` scrubs both out of provider texts as a safety net. They are bound to the
`client_id` they were issued for: with another Client ID in the settings the user counts as not
connected (and is connected again when the old ID comes back). Deleting the file (Disconnect,
`up spotify logout`) disconnects. Renewing, saving and deleting it happen under an OS lock on an
empty `spotify_auth.lock` next to it, because the running app and an `up` command in a terminal
may renew at the same moment (see step 4 below).

The audio file itself carries enough to rebuild the index: `TPE1` artist, `TIT2` title, `TALB`
album, `COMM` = source URL, `APIC` cover, and a `TXXX` frame with description
`ULTIMATE_PLAYLIST_ID` holding the track id (Spotify tracks add a second `TXXX` frame,
`YOUTUBE_ID`, with the video the audio came from). `Library.rescan()` walks the library folder
(skipping `.incoming/`, `Playlists/`, trash and sync-tool folders such as `$RECYCLE.BIN` and
`.stversions`, on Windows any folder both hidden and system, elsewhere any folder named with a
dot followed by a letter), reads those tags with mutagen, and recovers the id from
the `TXXX` frame. Any provider must write that frame, otherwise its tracks come back as
`local:...` after a rescan.

## How the YouTube provider does it (the self-contained example)

`providers/youtube.py` is the reference implementation of a provider that does everything itself.

- `matches`: hostname in `youtube.com`, `www.`, `m.`, `music.youtube.com`, `youtu.be`,
  `youtube-nocookie.com`; paths `/watch`, `/playlist`, `/shorts/<id>`, `/live/<id>`,
  `/embed/<id>`, `youtu.be/<id>`, `music.youtube.com/browse/<album or playlist id>`. Channel URLs
  return False.
- `resolve`: `yt_dlp.YoutubeDL(...).extract_info(url, download=False)` with `extract_flat`, so a
  playlist costs one request. `DownloadError` becomes `ProviderError(friendly_error(exc))`.
- `download`: yt-dlp downloads `bestaudio` into `<library>/.incoming/dl-*/<id>.<ext>`, its
  post-processors convert to MP3, add metadata and embed the thumbnail. The provider then decides
  `artist, title = pick_artist_title(info)`, rewrites the tags on the file still inside
  `.incoming/` with `write_tags(path, artist, title, album, source_url, track_id)` (keeps the
  cover yt-dlp embedded; ID3 for MP3, MP4 atoms for M4A, Vorbis comments for Opus/FLAC), builds
  `safe_filename(f"{artist} - {title}") + <extension of the produced file>`, picks the final
  path with `destination_for(path, track_id)` (the plain name, or the ` (n)` sibling that
  already holds this very track so a re-download replaces it, else the first free ` (n)` name;
  `unique_path` remains a simpler helper that only looks for a free name) and moves the file
  with `os.replace`; name pick and move happen under one module lock so two workers finishing
  the same "Artist - Title" cannot overwrite each other. The `dl-*` folder (made with
  `tempfile.mkdtemp`) and whatever is left in it are removed in a `finally`; only a hard kill
  of the process leaves one behind, and it is safe to delete.
- `doctor`: ffmpeg (via `ffmpeg.find_ffmpeg`), JavaScript runtime (Deno or Node), yt-dlp version.

The pure helpers (`split_artist_title`, `clean_title`, `safe_filename`, `unique_path`,
`destination_for`, `pick_artist_title`, `friendly_error`, `write_tags`, `stored_track_id`) are
plain functions other providers import and reuse; the Spotify provider is built on them.

## How the Spotify provider does it (the composite example)

### The idea

Spotify streams are DRM-protected and this project never touches them. What Spotify gives
freely is metadata: track name, artists, album, cover art, duration, and the contents of albums
and playlists. So the design is

> **metadata from Spotify, audio from YouTube Music, tags from Spotify.**

The audio download is the existing YouTube pipeline; the Spotify provider is a metadata reader,
a matcher and a re-tagger around it. Four modules:

| Module | Job |
| --- | --- |
| `providers/spotify.py` | `SpotifyProvider`: `matches`, `resolve`, `download`, `doctor`, `configure(settings)`. Orchestrates the others and delegates the audio to `YouTubeProvider`. |
| `providers/spotify_meta.py` | Everything Spotify-side except the sign-in: parsing links into `(kind, id)`, the public embed pages (`EmbedClient`), the Web API as the connected user (`UserApiClient`), pagination, mapping both shapes onto one `SpotifyEntity` per track, and `get_metadata`, which picks the route. Takes an HTTP session so tests can hand in a fake. |
| `providers/spotify_auth.py` | "Connect Spotify": the PKCE login (`begin_login`, `finish_login`), the token file, `access_token` (renewal), `current_account`, `logout`. HTTP and the clock are injectable for tests. |
| `providers/ytmusic_match.py` | The YouTube Music search and the scoring: `find_match(ref, settings, cancel, search)` tries the queries in order, `rank_candidates` / `score_candidate` score `Candidate` objects, `Scored.accepted` applies the thresholds. Everything but the two `search_*` functions is pure; tests hand `find_match` a canned `search` callable. |

### Links accepted (`matches`)

`matches` is a hostname check only: `open.spotify.com` (with or without `www.`),
`play.spotify.com`, the `spotify.link` / `spotify.app.link` short-link hosts, and anything that
starts with `spotify:`. What kind of link it is (`track`, `album`, `playlist`; an optional
`/intl-xx/` prefix, an `/embed/` segment, the old `/user/<name>/playlist/<id>` shape and
`spotify:user:<name>:playlist:<id>` URIs are all understood; Liked Songs,
`/collection/tracks` or `spotify:collection:tracks`, becomes `("liked", "tracks")`) is decided
by `spotify_meta.parse_spotify_url` inside `resolve()`. An artist, show, episode, profile or
other `/collection/...` link therefore becomes a job that fails at once with "Only Spotify
tracks, albums, playlists and your Liked Songs are supported (not artists, podcasts or
profiles)." rather than the generic "No provider for this link": the specific sentence is worth more to the user than an offline rejection, and the
web UI shows it on the job. `spotify.link` short links are followed inside `resolve()` (a
`HEAD`, then a `GET`, then a scan of the page for an `open.spotify.com` URL), never in
`matches()`.

### Two metadata sources (`spotify_meta.py`)

**(a) Public embed pages, no account.** `GET https://open.spotify.com/embed/{track|album|playlist}/{id}`
with a desktop browser `User-Agent` returns HTML with a `<script id="__NEXT_DATA__"
type="application/json">` block; the entity sits at `props.pageProps.state.data.entity`.

- A **track** entity has `type`, `name`/`title`, `uri`, `id`, `artists[{name, uri}]`,
  `releaseDate{isoString}`, `duration` (ms), `isPlayable`, `isExplicit`, `visualIdentity`
  (`image[]` with `url` and `maxWidth`: the largest one is the cover) and `relatedEntityUri`
  (the album URI).
- An **album** entity adds `subtitle` (the artist) and `trackList[]`; each item has `uri`
  (`spotify:track:<id>`), `title`, `subtitle` (artist names joined with `, ` followed by a
  non-breaking space, which is normalised to a plain space), `duration` (ms), `isPlayable`,
  `playabilityReason`. The album name comes from the album entity itself; its cover from
  `visualIdentity.image[]`.
- A **playlist** entity has `name`/`title`, `subtitle` (the owner), `coverArt{sources[{url}]}`
  and the same `trackList[]` item shape. The items do not carry an album name or per-track
  cover, so `TrackRef.album` and `thumbnail_url` stay `None` for playlist items read this way.
  A single track's embed page names no album either. In both cases the album name is filled
  in from YouTube Music's catalogue entry once the track is matched (`Match.album`), and the
  cover stays the one yt-dlp embedded (the video thumbnail, which for catalogue uploads is the
  album art). The album embed's `releaseDate` was `null` when checked, so `release_year` (the
  `TDRC` tag) is only reliable for single tracks and through the API.
- **Cap:** the embed page lists at most **100** tracks of a playlist (`EMBED_LIST_CAP`).
  Verified on 2026-09-13 against three editorial playlists with 150+ songs each: exactly 100
  items came back, and `?offset=` on the embed URL is ignored. `SpotifyEntity.truncated` is
  set when a list has 100 items or more (a playlist of exactly 100 is flagged too) and a
  warning is logged. Albums are complete. Editorial ("Spotify-made") playlists *do* work
  through this route.
- Items with `isPlayable == false` (removed, region-locked) are dropped silently, like private
  YouTube videos. A bogus id answers HTTP 200 with `pageProps.status == 404`; that is mapped
  to "That Spotify link does not exist or is private." like a real 404.

**(b) The Spotify Web API as the connected user** (`UserApiClient`). Used when the settings
hold a Client ID and `spotify_auth.current_account(client_id)` finds an account connected
through it (see [Connecting an account](#connecting-an-account-spotify_authpy) below). Every
request carries `Authorization: Bearer <access token>` from
`spotify_auth.access_token(client_id, cancel)`, which renews the token when it expires within
a minute. A 401 renews once more (`stale_token=` forces the renewal even when the stored token
still looks fresh) and then gives up with "Spotify connection expired, connect again in
Settings"; a 429 waits for `Retry-After` once (capped at 30 s, cancellable). `next` links are
followed only when they point at `https://api.spotify.com/v1/`, so the token never goes
anywhere else, and for at most `MAX_PAGES` (400) pages. Endpoints (`limit=50` is the documented
maximum):

- `GET /v1/tracks/{id}`;
- `GET /v1/albums/{id}` (name, images, release date, artists) plus
  `GET /v1/albums/{id}/tracks?limit=50`, following `next` (the simplified items lack `album` /
  `images`, so the album's own values are attached to every item; items with
  `is_playable: false` are skipped);
- `GET /v1/playlists/{id}` (no `fields` filter, see below), then
  `GET /v1/playlists/{id}/items?limit=50`, following `next`. Each entry holds its track under
  `item` (`track` in older responses); `null` entries, `is_local` files and podcast episodes
  are skipped;
- `GET /v1/me/tracks?limit=50` for Liked Songs.

A track has `name`, `artists[{name}]`, `album{name, images[{url,width}], release_date,
artists}`, `duration_ms`, `track_number` and `external_ids{isrc}`, so the API fills in the
album name, cover, release year, track number and ISRC that the embed pages lack.

**The Web API rules since February 2026** shaped this design (Spotify's February 2026
changelog and migration guide, applied to new Development Mode apps from 2026-02-11 and to all
existing ones from 2026-03-09):

- `GET /playlists/{id}/tracks` was removed and `GET /playlists/{id}/items` replaces it; in the
  playlist object `tracks` became `items` and `items[].track` became `items[].item`.
- Playlist contents are returned only for playlists the current user **owns or collaborates
  on**. For any other playlist, Spotify's own editorial and algorithmic ones included, only the
  metadata comes back, without `items`. So `GET /playlists/{id}` is sent without a `fields`
  filter, and a missing `items` (or legacy `tracks`) object is the "not yours" signal. That, or
  a 403 / 404 on the playlist or its items, raises `NotYourPlaylist`: `get_metadata` logs
  "Spotify only shares the first 100 songs of playlists you don't own; reading playlist <id>
  from its public page" and reads the embed page (the first 100 songs). The items are always
  read through `/items` from offset 0, never from the first page embedded in
  `GET /playlists/{id}`, whose `next` may still point at the removed endpoint.
- Development Mode apps (every personal developer app) need an app owner with **Spotify
  Premium**; new ones admit at most five users, listed under User Management on the dashboard.
- `external_ids` (the ISRC) was announced for removal in February and restored in March 2026.

**Why the Client Credentials flow is gone.** Pre-release 0.2 builds (development builds from
the main branch, never tagged) read big playlists with a client id *and secret* (Client Credentials: an app token without a user). Under the rules above such a
token belongs to nobody, so it can read no playlist contents at all, and Spotify is moving away
from Client Credentials for metadata endpoints. Asking people to store a secret for that made no
sense, so the secret, its setting, its mask and its code path were removed. Users who want more
than the public pages connect their own account instead, with a Client ID only.

**Choosing the route** (`get_metadata(kind, id, settings)`): no Client ID, or no account
connected through it -> the embed page; Liked Songs without a connection ->
`SpotifyAuthError("Connect Spotify in Settings to download your Liked Songs")` (there is no
public page for them); connected -> the Web API, falling back to the embed page for
`NotYourPlaylist` and, for tracks, albums and playlists, for any other `ProviderError` of the
Web API as well (an expired connection, a 403 once the developer app's owner has no Premium
any more, a 429 after the one wait, a 5xx, a network error; a warning names it). The embed
page needs no account, so being connected never breaks a link that works without a
connection. `DownloadCancelled` is passed on, and for Liked Songs every error is reported.

**Error mapping** (constants at the top of `spotify_meta.py` and `spotify_auth.py`; all of
them are `ProviderError`s, the connection problems its `SpotifyAuthError` subclass): connection
failures -> "Could not reach Spotify. Check your internet connection and try again."; a 404
(embed or API) -> "That Spotify link does not exist or is private."; a 429 -> "Spotify is
rate-limiting us, try again in a minute."; a 403 on Liked Songs (a track or an album is
read from its public page instead, see above) ->
"Spotify refused this request (HTTP 403). Check that your Spotify account is listed under User
Management of your developer app and that the app owner has Spotify Premium."; an embed page
without `__NEXT_DATA__` -> "Could not read that Spotify page (it did not contain track data)."
Neither module logs a token.

### Connecting an account (`spotify_auth.py`)

Authorization Code with PKCE, Spotify's flow for apps that cannot keep a secret: only the
user's own Client ID is involved, and no secret exists anywhere.

1. The Settings dialog saves the Client ID and navigates to `GET /api/spotify/login`. The
   server calls `begin_login(client_id, redirect_uri(port))`, which makes a 64-character
   `code_verifier` (`secrets.token_urlsafe(48)`), its S256 challenge and a random `state`, and
   keeps `state -> (verifier, redirect, client_id, created)` in memory for ten minutes (each
   state works once). The route answers 302 to `https://accounts.spotify.com/authorize` with
   `response_type=code`, the scopes `playlist-read-private playlist-read-collaborative
   user-library-read`, the Redirect URI and the challenge, sent with `Cache-Control: no-store`
   and `Referrer-Policy: no-referrer`. Without a Client ID it answers 400 with a sentence (a
   small HTML page when a browser asks).
2. Spotify sends the browser back to `GET /api/spotify/callback?code=...&state=...`. The
   Redirect URI is `http://127.0.0.1:<port>/api/spotify/callback`: Spotify does not accept
   `localhost` for loopback redirects, so the IP is explicit, and the port is the one the
   server really listens on (`create_app(port=...)`). When `serve()` had to fall back from 8765
   and a Client ID is set, it logs the Redirect URI to register; the dialog shows it too, with
   a warning.
3. `finish_login(state, code, error)` checks the state, POSTs
   `https://accounts.spotify.com/api/token` (`grant_type=authorization_code`, `code`,
   `redirect_uri`, `client_id`, `code_verifier`), asks `GET /v1/me` who signed in and writes
   the token file. The callback always answers 303, to `/?spotify=connected` or
   `/?spotify=error`. An error message stays on the server and `GET /api/spotify` hands it out
   once as `last_error` (were it in the URL, any web site could make the app show text of its
   choosing); the page shows a toast and cleans its address bar. The code never appears in a
   response or a log: the 303 is not logged (successful requests never are), and the access
   line of a callback that failed is written without its query string
   (`logs.RoutineAccessFilter`). Failures are sentences: the login was cancelled, the link
   expired or was already used, Spotify refused the code for this Redirect URI or Client ID,
   or the account may not use the app (403 on `/me`: not listed under User Management). No
   tokens are saved on any failure. A Redirect URI that is not registered (`localhost`
   instead of `127.0.0.1`, a missing port) or an unknown Client ID never gets this far:
   Spotify stops on its own page ("INVALID_CLIENT: Invalid redirect URI" / "Invalid client")
   and does not send the browser back.
4. `access_token(client_id)` returns a token valid for at least another minute, renewing it
   (`grant_type=refresh_token`, `refresh_token`, `client_id`; a new refresh token in the
   answer replaces the old one). The running app and an `up add` / `up doctor` in a terminal
   share the file, and Spotify rotates the refresh token, so a renewal runs under a thread
   lock and an OS lock on `spotify_auth.lock` (`msvcrt.locking` / `fcntl.flock`, released by
   the OS when a process dies; after 15 s of waiting the renewal goes ahead without it). A
   process that waited re-reads the file and uses the other one's renewal. A renewal Spotify
   refuses (400/401/403) re-reads the file too: when it holds another refresh token, another
   process renewed first and that sign-in is used (renewed once more if its access token is
   old); only when the file still holds the refused token is it deleted, with "Spotify
   connection expired, connect again in Settings". A network error, a 429 or a 5xx keeps the
   connection and raises a plain `ProviderError`. A renewal whose write fails is kept in
   memory and preferred over the older file record until a write succeeds, so the rotated
   refresh token is not lost.

`GET /api/spotify` returns `{client_id_set, connected, display_name, redirect_uri, connect_ok,
last_error}` for the dialog. `connect_ok` is False when `serve --host` names one address other
than loopback (a LAN IP, `::1`): Spotify's redirect to `127.0.0.1` would find nothing
listening, so the dialog disables Connect Spotify, `/api/spotify/login` answers 400 with a
sentence and `serve()` logs why. `POST /api/spotify/logout` deletes the token file. These routes pass the loopback guard
like every other request (the callback is a top-level GET to `127.0.0.1`). On the command line
`up spotify status` prints the same state, `up spotify login` prints the steps and opens the
running app's `/api/spotify/login`, and `up spotify logout` disconnects (through the running
app when there is one). No token is ever printed.

### `resolve(url)`

Parse the link, let `get_metadata` pick the source (above), fetch, and return one `TrackRef`
per track:

```python
TrackRef(
    provider="spotify",
    source_id="<track id>",
    url="https://open.spotify.com/track/<track id>",
    title=<name>,
    artist=", ".join(artist names),
    album=<album name or None>,
    duration=<ms> / 1000,
    thumbnail_url=<largest cover image, or None>,
    extra={
        "source": "embed" | "api",
        "release_year": "2013",            # when known
        "track_number": 5,                 # album items and API tracks
        "album_artist": "Daft Punk",       # album items and API tracks
        "isrc": "USQX91300102",            # API only
        "container": "Album: Random Access Memories" | "Playlist: <name>" | "Playlist: Liked Songs",
    },
)
```

Only keys with a value go into `extra`, and everything in it is JSON-safe (it is serialised into
`/api/jobs`); `download()` adds `"youtube_id"` once the match is known. `Track.id` is therefore
`spotify:<track id>`, and the same Spotify track is never downloaded twice even if it appears in
several Spotify playlists. `resolve()` logs one line per album / playlist ("Spotify playlist
'<name>': 50 playable track(s) via embed"), with a note when the list may be capped. The
queue names the parent job after the first ref's `container` ("Album: Random Access Memories
(13 tracks)", `downloader._parent_message`); links without one keep "Playlist: N tracks".

### `download(ref, dest_dir, settings, progress, cancel)`

Three phases; the YouTube provider does the heavy lifting in the middle one.

**1. Match on YouTube Music** (`ytmusic_match.py`). `progress(DOWNLOADING, 0.0, "Searching
YouTube Music…")`, check `cancel`, then `find_match(ref, settings, cancel)` searches and scores
(below) and returns the best acceptable candidate. `None` ->
`ProviderError("Couldn't find '<artist> - <title>' on YouTube Music (no close enough match).")`,
which the queue shows on that one track while the rest of the playlist continues. The chosen
video id goes into `ref.extra["youtube_id"]` before the download starts (visible in
`/api/jobs` and in bug reports), a missing `ref.album` is filled from YouTube Music's album
(`Match.album`), and the queue message becomes "Matched: <video title> (<channel>, m:ss)".

**2. Download through the YouTube provider.** Build a YouTube `TrackRef`
(`provider="youtube"`, `source_id=<video id>`, `url=https://www.youtube.com/watch?v=<id>`,
title/artist/album/duration copied from the Spotify ref) and call the registered
`YouTubeProvider`'s `download(yt_ref, <staging folder>, settings, progress, cancel)`. That
gives the whole pipeline for free: MP3 conversion with the user's `audio_quality`, progress
events, cancel handling (`DownloadCancelled` simply propagates), friendly yt-dlp errors,
cleanup. Note the `dest_dir`: each Spotify download gets a **staging folder of its own**,
`<library>/.incoming/sp-<spotify id>-<8 random hex>/`, and the YouTube step runs with that as
its "library root", so yt-dlp's temporary files land in `<staging>/.incoming/dl-*/<video id>.*` and
its finished, YouTube-named file in `<staging>/`. Had it been given the real library root, an
existing YouTube-provider file of the same video would have been replaced and then renamed
away; had two Spotify downloads shared one staging folder, two tracks matched to the same video
would have fought over one file. The staging folder is removed (`shutil.rmtree`) in a
`finally`, whatever happened. Downloads of one video id are also serialised with a per-id lock
(two Spotify tracks, a single and an album version say, can map to the same video). The returned `Track` has a YouTube identity; it is **not** added to the library (the
`JobManager` adds whatever the Spotify provider returns).

**3. Re-name and re-tag with the Spotify metadata.** `progress(CONVERTING, 1.0, "Writing
Spotify tags")`, then `write_spotify_tags(path, ref, video_id, cover)` on the staged file:

- `youtube.write_tags(path, artist, title, album, source_url=ref.url, track_id=ref.track_id)`:
  artist, title, album, comment = the Spotify track URL, and `TXXX ULTIMATE_PLAYLIST_ID =
  spotify:<track id>`, which is what `rescan()` needs.
- Then, in each container's own format (ID3 frames for MP3, MP4 atoms for M4A, Vorbis
  comments for Opus / FLAC): `TPE2` album artist, `TRCK` track number, `TDRC` release year,
  `TXXX ISRC` plus `TSRC`, each only when the metadata source provided it (a value yt-dlp
  wrote is removed otherwise), and always
  `TXXX YOUTUBE_ID = <video id>` so a wrong match can be traced from the file alone.
- The cover: when `settings.embed_cover` is on and Spotify gave a cover URL, `fetch_cover`
  gets it (10 s timeout, JPEG or PNG by content type or magic bytes, at most 5 MB) and it
  replaces the `APIC` frame (Spotify's covers are 640 px JPEGs). On any failure the YouTube
  thumbnail stays and the reason is logged at debug level; the cover is never the reason a
  download fails.
- Rename to `safe_filename(f"{ref.artist} - {ref.title}") + <suffix>` with `destination_for(...)`
  under `youtube._move_lock`, so a re-download replaces the earlier copy of this very track and
  two workers cannot overwrite each other.
- Return `Track(id=ref.track_id, provider="spotify", source_id=<track id>,
  source_url=<Spotify URL>, title, artist, album, duration (read from the file), has_cover,
  path relative to dest_dir, file_size)`.

Failure handling: `DownloadCancelled` and `ProviderError` propagate as they are; anything else
becomes `ProviderError("Spotify download failed: ...")`. If tagging or renaming fails after the
YouTube step, the staging folder goes with it, so a retry starts clean. (Only a hard kill of
the process leaves a `.incoming/sp-...` folder behind; it is safe to delete, and
`sweep_stale_staging` removes it at a start 12 hours later.)

### Matching heuristics and the acceptance threshold (`ytmusic_match.py`)

Wrong matches are worse than no matches, so the scoring is strict and every rule is a plain
function with canned-result tests. The constants (weights, bonuses, penalty, thresholds, the
variant word list) sit at the top of the module.

**Search.** `build_queries` gives `"Artist - Title"`, `"Title Artist"`, `"Title"`; the first
query that yields an accepted candidate wins, so a good catalogue entry costs one search
(~0.5 s). `default_search` asks the YouTube Music **Songs** shelf first (`search_music`): the
catalogue tracks labels upload themselves, i.e. the "Artist - Topic" / "Provided to YouTube by
..." audio, with proper artist, album and duration fields. yt-dlp's flat extraction of
`music.youtube.com/search` keeps only id and title, so `search_music` makes the same InnerTube
call through yt-dlp's own `YoutubeMusicSearchURL` extractor (`_extract_response` with the songs
filter) and `parse_music_search` reads the shelf (`musicResponsiveListItemRenderer`: title,
bullet-separated artists / album / duration). If that private plumbing raises or returns
nothing, `search_youtube` runs `ytsearch10:` on plain YouTube, whose flat entries do carry
title, channel and duration (a warning is logged; the scoring is the same, the ranking weaker).

**Score** = `0.5 * title + 0.3 * artist + 0.2 * duration`, plus `0.05` for a catalogue /
"- Topic" upload, plus `0.05` when a candidate name equals a Spotify artist exactly, minus
`0.5` per variant-word mismatch (at most `1.0`), clamped to `0..1`:

1. **Title similarity.** `normalize_text` on both sides (casefold, accents stripped, `&` ->
   `and`, `ft.` / `featuring` -> `feat`, punctuation dropped); "feat ..." credits and
   bracketed "(with ...)" / "(feat. ...)" credits are removed (they are compared as artists,
   and a shared `feat.` credit must not make two songs of one album look alike). Edition
   labels are optional on both sides: a Spotify " - Remastered 2011" tail (the last " - "
   counts, so "I Want You (She's So Heavy) - Remastered 2009" works), a "(2015 Remaster)",
   "(Album Version)" or "(Radio Edit)" bracket, a candidate's "Hotel California - 2013
   Remaster", a "(Clean)" bracket. A tail or bracket is an edition label (`_is_edition_marker`)
   only when it holds an edition word (remaster, edit, mix, mono, stereo, deluxe, edition,
   explicit, clean, from, a year, ...; a bare "version" only after a qualifier such as album,
   single, radio, original, remastered, extended or a year) and no variant word other than
   edit / extended: "Live Version", "Acoustic Version", "Club Mix", "(DJ Y Remix)", "Live at
   Wembley 1986" and "1995 Demo" name a different recording and stay, and so do "Spanish
   Version", "Alternate Version" and "(Taylor's Version)". On the candidate side `youtube.clean_title` drops
   `(Official Video)` junk and an `Artist - ` half is removed when it names the artist. Then
   the best of the character ratio and a token containment / Jaccard blend, with titles that
   share no word capped at `0.45` ("Halo" vs "Hello").
2. **Artist** (`_name_similarity`; the best of any Spotify artist against the candidate's
   credited artists and channel, ` - Topic` stripped): `1.0` for the same normalised name;
   `0.9` for the same name decorated as a channel (official / vevo / music / tv / channel, a
   leading "the", the compact "ArianaGrandeVevo") or when one name is a whole credit of the
   other, split on `, & + / ;` and the words and / with / feat / ft / x / vs ("Bruce
   Springsteen & The E Street Band", "Yusuf / Cat Stevens"); otherwise the character ratio,
   capped at `0.5`, below the `0.6` floor, when one name merely contains the other ("Adele" /
   "Adeleine", "Nas" / "Lil Nas X", "Ye" / "Kanye West"). For plain-YouTube results only, an
   artist named in the video title counts `0.8` ("Artist - Title (Lyrics)" on a random
   channel).
3. **Duration.** `1.0` within 2 s, `0.8` within 5 s, `0.5` within 12 s, `0.2` within 25 s,
   `0.0` beyond; `0.5` when either side has no duration.
4. **Variant words**, checked both ways (`variant_penalties`): `live`, `remix`, `mix`,
   `cover`, `karaoke`, `instrumental`, `acoustic`, `unplugged`, `stripped`, `piano`,
   `orchestral`, `a cappella`, `demo`, `sped up`, `slowed`, `nightcore`, `8d`, `reverb`,
   `lofi`, `reaction`, `tutorial`, `extended`, `mashup`, `edit`, `hour`, `loop` (word-boundary
   matches, with spellings such as "remixed", "acapella", "demos", "lo-fi", "hours",
   "looped"). Occurrences are counted: a word the candidate has more often than the Spotify
   title costs `0.5` ("live": a live upload for a studio track, and "Live Forever (Live at
   Knebworth)" for "Live Forever", whose own "live" does not cover the second one), and so
   does a word the Spotify title has more often ("missing live": the studio recording for
   "Song - Live"), except `edit` and `extended`, where the duration check decides. A related
   word on the other side excuses one: live ~ unplugged, acoustic ~ unplugged ~ stripped, remix ~ mix,
   slowed ~ reverb ("MTV Unplugged" is a live recording). Edition phrases ("2009 Stereo Mix",
   "Original Mix", "Radio Edit", ...) are removed before the check, bracketed credits are
   ignored, and on the candidate side upload junk and an "Artist - " half that names the
   artist are dropped first (the band called Live). One penalty is enough to push an otherwise
   perfect candidate below the threshold.
5. **Extra words** (`title_extra_words`): the words the candidate's title adds beyond the
   Spotify title, plus the words of the Spotify song name it leaves out, once credits,
   descriptor brackets and tails, the artist's name, articles and upload junk are set aside
   ("pt" reads as "part" and roman numerals as digits, so "Part II" equals "Pt. 2"). "Stay" vs
   "Stay With Me" has two extra words. A Spotify bracket or tail that names another version
   ("(Taylor's Version)", " - Spanish Version") counts as part of the song name, so a
   candidate without it has extra words.
6. **Acceptance** (`Scored.accepted`): score `>= 0.62`; title similarity `>= 0.5`, or
   `>= 0.8` when there are extra words, and never when an extra word is a number or a part /
   vol / chapter word ("Stay, Pt. 2", "Song 2"); artist similarity `>= 0.6` (a tribute act
   with the same title and length is rejected); a duration delta `<= 25 s` when both durations
   are known, and a candidate of at most 15 minutes when Spotify gave no duration (no 10-hour
   loops). Otherwise `find_match` returns `None` and the track fails with the "Couldn't find
   ..." message rather than downloading something else. The top three candidates of every
   query are logged at debug level with their penalties, extra words and verdict, the winner
   at info level ("Matched 'Artist - Title' to <video id> (...)").

Because the matched video id is kept in `TrackRef.extra["youtube_id"]` and in the file's
`YOUTUBE_ID` tag, a bad pick can be reported with the exact candidate that won. The remedy for
the user is to paste the right YouTube link; the app keeps both files (no cross-provider
de-duplication).

### `doctor()`

Never raises and never turns the whole health check red (`up doctor` exits 1 only on YouTube
failures, because that is what stops downloads). It reports one check, labelled `Spotify`,
saying which metadata route is active: without a Client ID "Public pages only (tracks,
albums, playlists up to 100 songs). Connect Spotify in Settings for your own bigger playlists
and Liked Songs."; with a Client ID but no connection "Client ID set, not connected: click
Connect Spotify in Settings"; connected "Connected as <name> (your playlists of any size and
Liked Songs)". It stays offline except for one token renewal when the stored access token has
expired, which is how a revoked connection shows up (the check is then not ok and says to
connect again; a network failure during that renewal still reports the account as
connected). The audio-side checks (ffmpeg, JavaScript runtime, yt-dlp) are the YouTube
provider's and are printed under YouTube. The web UI's Spotify chip is fed from this check
(label matching `spotify`); `PUT /api/settings` and the login callback call
`providers.configure(settings)` so the chip changes as soon as the Client ID is saved or the
account connected.

### Known limitations

- **Embed cap**: 100 songs per playlist on the public pages (albums are complete). A connected
  user gets their own and collaborative playlists in full; other people's playlists, Spotify's
  editorial ones included, stay at 100 because the Web API shares no contents for them (copy
  the songs into a playlist of your own for the rest). The cap is logged and named by
  `doctor`, but the parent job in the queue only says "Playlist: <name> (100 tracks)"; the UI
  does not flag a capped list.
- **Connecting needs a developer app of the user's own whose owner has Spotify Premium**
  (Spotify's rule for Development Mode apps; accounts added under User Management need none);
  a new app admits at most five accounts. When the owner's Premium lapses, the Web API answers
  403: tracks, albums and playlists then fall back to the public pages, Liked Songs fail.
- **The Redirect URI names a port.** A server that fell back from 8765, or `up serve --port`,
  needs that port's URI registered as well. With `serve --host 0.0.0.0` the Redirect URI still
  says `127.0.0.1`, so Connect Spotify only works in a browser on the machine running the app;
  with one LAN address (`--host 192.168.1.5`) nothing listens on `127.0.0.1` at all, so the
  dialog disables Connect Spotify and `serve()` logs why.
- **Pending logins live in memory**: restarting the app between Connect and Spotify's redirect
  gives "That login link has expired". The token file is plain JSON protected by the user
  profile's permissions (mode `0o600` on POSIX); there is no OS keyring.
- **No Spotify album name or per-track cover for playlist items** read from embed pages, and
  no album name for a single track: YouTube Music's album and yt-dlp's thumbnail stand in. The
  Web API fills both in.
- **No cross-provider de-duplication**: `youtube:<id>` and `spotify:<id>` for the same song are
  two files. Two Spotify tracks that match the same video (single and album version) are two
  files as well.
- **Podcast episodes and local files** in playlists are skipped.
- **Regional catalogues**: a track that exists on Spotify may be missing from YouTube Music in
  your country; it fails per track, the rest of the playlist continues.
- **Match quality** depends on YouTube Music's search and the heuristics above; the threshold
  prefers a miss over a wrong song. Known blind spots: a variant word inside the song name
  still excuses a related one on the candidate ("Live Forever" vs "Live Forever (MTV
  Unplugged)": the title's "live" excuses "unplugged"); a Spotify
  "Song - Acoustic" whose only upload is called "Song (MTV Unplugged)" is missed; artist names
  written in another script (Korean vs. Latin) fail the artist floor; for plain-YouTube results
  an artist found as a whole word in the video title ("Nas" in "Lil Nas X - ...") still counts
  `0.8`, although title and duration must match as well.
- **yt-dlp's private plumbing**: `search_music` relies on the extractor's `_extract_response`;
  if a yt-dlp update changes it, the plain-YouTube fallback takes over (weaker ranking, same
  scoring) and a warning is logged.

## How the TikTok provider does it (the subclass example)

`providers/tiktok.py` adds a second yt-dlp site without a second download pipeline:
`TikTokProvider` subclasses `YouTubeProvider` and inherits `download()` (`.incoming/`,
conversion, `write_tags`, `destination_for`, cleanup) unchanged. `YouTubeProvider` exposes
hooks for that, and YouTube's own behaviour is their default. TikTok overrides these three (and
`ydl_video_format`, see the videos paragraph above; SoundCloud, below, uses three more):

- `ydl_format`: the yt-dlp format selector. TikTok has no audio-only stream of the video, so it
  asks for `best[format_id!^=download]/bestaudio/best`: the soundtrack of the best
  non-watermarked video (the watermarked copy ends in TikTok's outro jingle; a prefix match,
  because yt-dlp renames two of them `download-0` / `download-1`); for a photo slideshow,
  which has no video, the sound; anything as a last resort. Plain `bestaudio` would pick
  TikTok's separate "sound" file, which is not what plays in the video. The tests run this
  selector through yt-dlp's own sorting and selection on TikTok-shaped format lists.
- `_tags_for(info) -> (artist, title, album)`: `pick_tags`. A named sound gives
  "<sound artists> - <sound title>" and its album; an "original sound" (`is_generic_sound`:
  the English name and its translations, `- <owner>` suffix or not, or a sound named after the
  post's creator) gives "<creator display name> - <caption>", the caption without hashtags
  (`strip_hashtags`: a tag ends at anything but a letter, mark, digit or "_", so the sentence
  keeps its punctuation and emoji) or a "Replying to @user" prefix, its first non-empty line,
  cut after the first sentence or at a word near 80 characters (`clean_caption`), "TikTok post
  <id>" when no letter, digit or symbol is left. yt-dlp splits the sound's author on ", " and
  " & " alike; `join_artists` puts two names back as "A & B" and more as "A, B & C".
- `_explain(error)`: `tiktok.friendly_error`, built on `youtube.explain_error` with TikTok's
  rules (blocked in your country / private / signed-in only / deleted / page unreadable) and
  the shared `NETWORK_ERRORS` and `CONVERSION_ERRORS` substrings.

The rest is TikTok's own:

- `matches`: any link on `tiktok.com`, `www.`, `m.`, `vm.`, `vt.tiktok.com` or `tiktokv.com`, so
  a profile, sound page or live stream reaches `resolve()` and is refused with a sentence
  saying why and, where there is one, what to paste (`unsupported_message`) instead of the bare
  "No provider" message.
- `resolve`: `parse_link` (offline) finds the post id in `/@user/video/<id>`, `/@user/photo/<id>`,
  `/share/video/<id>`, `/embed/v2/<id>`, `m.tiktok.com/v/<id>.html`; a short link
  (`vm.`/`vt.tiktok.com/<code>`, `tiktok.com/t/<code>`) is expanded first with one HEAD request
  (`expand_short_link`, the link-preview User-Agent yt-dlp uses too). That request goes to
  `TikTokLink.clean_url`, the link rebuilt from the checked host and path: `urlsplit` and
  requests disagree on where `evil.example\@vm.tiktok.com` ends its host, and userinfo or a
  port must not reach the network either. An error status without a redirect becomes the
  rate-limit / try-later text; a short link whose post is gone lands on TikTok's home page and
  is refused as gone; one that leads to a profile or sound page (shared from the app) gets that
  page's explanation. Every post is then fetched as `https://www.tiktok.com/@<user>/video/<id>`
  (`post_url`): the one shape yt-dlp's TikTok extractor accepts for every link form above (it
  does not know `/photo/`, `/embed/v2/`, `m.tiktok.com/v/<id>.html` nor the bare `tiktok.com`
  host), and TikTok serves a photo post on its `/video/` address too. One
  `extract_info(download=False)` gives the queue its title.
- `doctor`: ffmpeg, yt-dlp (`youtube.ffmpeg_check`, `youtube.ytdlp_check`) and
  `impersonation_check`; no JavaScript runtime, no account.
- Browser impersonation: yt-dlp's TikTok extractor fetches every page with `impersonate=True`,
  which needs curl_cffi. It is a dependency (`yt-dlp[default,curl-cffi]`, bundled in the exe),
  so requests carry a real browser's TLS and HTTP/2 fingerprint and TikTok's bot checks treat
  the app like a browser. `impersonation_check` imports `yt_dlp.networking._curlcffi`, which
  fails exactly when yt-dlp could not use it (missing, or a curl_cffi version yt-dlp does not
  support). Impersonation does not change where the request comes from: TikTok's status 10204
  ("Your IP address is blocked from accessing this post") is about the connection's location
  and stays; its message points at a computer-wide VPN. Where curl_cffi cannot load, yt-dlp's
  "no impersonate target" notice is logged at DEBUG instead of printed for every download.

## How the SoundCloud provider does it (the subclass with sets)

`providers/soundcloud.py` is the second `YouTubeProvider` subclass. `SoundCloudProvider`
inherits `download()` too and changes it through hooks only. Like TikTok it overrides:

- `ydl_format`: `bestaudio[format_id!*=preview]/best[format_id!*=preview]`, the best stream
  that is not one of SoundCloud's 30-second previews (yt-dlp gives those a `format_id` ending
  in `_preview`). A Go+ track, which SoundCloud only previews to listeners without a
  subscription, has nothing else, so yt-dlp fails with "Requested format is not available",
  which the error rules below turn into `GO_PLUS` instead of a 30-second file. The tests run
  this selector through yt-dlp's own selection on SoundCloud-shaped format lists: the AAC HLS
  streams give `hls_aac_160k`, a legacy MP3 track `http_mp3_0_0`, previews next to the full
  track never win, previews alone are refused.
- `_tags_for(info)`: `pick_tags` (naming, below).
- `_explain(error)`: `soundcloud.friendly_error` (errors, below).

It also uses three hooks `YouTubeProvider.download` gained for it. Their defaults keep YouTube
and TikTok exactly as they were (`test_the_subclass_hooks_change_nothing_for_youtube`):

- `_tags_for_ref(ref, info) -> (artist, title, album)`, by default `self._tags_for(info)`.
  SoundCloud's is `pick_tags(info)` with `album or ref.album`: a track of a set is downloaded
  on its own, so yt-dlp's info names no album and only the ref knows the set's.
- `_source_url(ref, info) -> str`, the link written into the comment tag and
  `Track.source_url`, by default `ref.url`. The later tracks of a set come as
  `api-v2.soundcloud.com/tracks/<id>` links; for an API `ref.url`, SoundCloud's override
  returns the track's page instead: `info["webpage_url"]` when `parse_link` reads it as a
  track on `soundcloud.com`, rebuilt by `clean_url`, so a private track keeps its `/s-...`
  part. A track named like a profile tab (`/<user>/spotlight`, which a set lists by its id)
  has a page `parse_link` does not take as a track link; it is still used, rebuilt from its
  `/<user>/<name>[/s-...]` shape, when yt-dlp read this very track (`info["id"]` equals
  `ref.source_id`). Anything else keeps `ref.url`.
- `_prepare_ydl(ydl)`, called on the download's `YoutubeDL` right after it is entered, before
  `extract_info`; by default it does nothing. SoundCloud adds `TrimArtworkPP` there with
  `when="pre_process"` (cover, below).

And `supports_video = False` (`YouTubeProvider` has True): a Video request for a SoundCloud
link is saved as audio, with the note (see the videos paragraph above).

The rest is SoundCloud's own:

- **Links** (`parse_link`, offline) give a `SoundCloudLink(kind, host, path, secret, reason)`
  with kind `track`, `set`, `short` or `other`, or None off SoundCloud. Only http and https;
  the host is compared exactly with `soundcloud.com`, `www.`, `m.`, `on.`, `api.`, `api-v2.`
  and `w.soundcloud.com` (`soundcloud.com.evil.example` is none of them), and `www.` and `m.`
  become `soundcloud.com`.
  - A track is `/<user>/<track>` or `/<user>/<track>/s-<token>`; a set is `/<user>/sets/<set>`
    with an optional `/s-<token>`. Names must match `[\w-]+` (what yt-dlp's extractor
    accepts). A first segment under which every page is SoundCloud's own (`_SITE_SECTIONS`:
    `discover`, `charts`, `search`, `stations`, `you`, `tags`, `pages`, `settings`,
    `messages`; SoundCloud's API knows no user of these names) is never a user. The names of
    SoundCloud's other top-level pages (`_SITE_PAGES`: `upload`, `feed`, `library`, `home`,
    `jobs`...) are refused only in a page's shape, the name alone or with a profile tab
    (`/library`, `/library/likes`): `library`, `home` and `jobs` are real accounts too, and
    `library` has public tracks and sets, so `/<name>/<track>` and `/<name>/sets/<set>` are
    read like any user's (a site page of that shape that is no track fails with SoundCloud's
    404).
  - On `api.` and `api-v2.`: `/tracks/<id>` and `/playlists/<id>` (also the
    `soundcloud:tracks:<id>` and `soundcloud%3Atracks%3A<id>` forms, rewritten to
    `/tracks/<id>`), with `?secret_token=s-...` kept; ids are ASCII digits. `/users/...` is a
    profile.
  - The player, `w.soundcloud.com/player/?url=<inner>`, stands for its inner link when that
    is itself a track or a set (the player's own `secret_token` is passed on to an inner API
    link without one). A player inside a player is not followed, and the player link itself
    never reaches yt-dlp.
  - `on.soundcloud.com/<code>` is `short`.
  - Everything else is `other`, with a `reason`: `profile` (a profile or one of its tabs, an
    API user), `likes` (likes and reposts), `station` (`/stations/...` and
    `/<user>/<track>/recommended|albums|sets`), `discover` (`/discover...`, `/charts...`),
    `search`, `account` (`/you/...`), `embed`, `home` or `page`. `unsupported_message(link)`
    gives each reason its sentence, which ends with what to paste instead and "(Share, then
    Copy link)"; `embed`, `home` and `page` share "That SoundCloud link isn't a track or a
    playlist".
- **`matches`** is `parse_link(url) is not None`: every page on a SoundCloud host belongs to
  this provider, so a profile reaches `resolve()` and is refused with its sentence (as on
  TikTok) rather than the bare "No provider" message.
- **`SoundCloudLink.clean_url`** is the link rebuilt from the checked host, path and secret
  (`https://soundcloud.com/<user>/<track>/s-...`, or `?secret_token=s-...` on an API host),
  and the only form of a pasted link that yt-dlp and the HEAD request see (a set's tracks are
  then downloaded by the URLs yt-dlp itself gave). The share-tracking query (`?si=`,
  `utm_*`, `?in=`), a fragment such as `#t=0:30`, a trailing `/`, userinfo, a port and
  backslash tricks are gone. It also fixes two forms yt-dlp's track extractor does not take:
  with a trailing `/` or a fragment yt-dlp falls back to its slow generic extractor, and the
  unencoded `soundcloud:tracks:` form reaches an API that always answers 429. The tests ask the
  installed yt-dlp's `SoundcloudIE`, `SoundcloudSetIE` and `SoundcloudPlaylistIE` (`suitable()`
  and the token in their URL pattern) about every rebuilt link.
- **Short links** (`expand_short_link`): one `requests.head(clean_url,
  allow_redirects=False, timeout=15)`, sent to `on.soundcloud.com` only, and its `Location`
  read (a relative one is joined to the short link). The target must parse as a track or a
  set on `soundcloud.com` (`www.` and `m.` included). One of the pages with a sentence of its
  own (profile, likes, station, Discover, search, account: a profile shared from the app, say)
  gets that sentence; anything else (SoundCloud's home page, another host, an API link) is
  `SHORT_LINK_DEAD`, "That SoundCloud link doesn't lead to a track or a playlist any more (it
  may have been deleted)." So is an answer that leads nowhere (no `Location` and a status
  below 400) and a 404 / 410; a 429 says SoundCloud is rate-limiting, a 5xx that it has
  problems, any other status of 400 or more that it "wouldn't open that short link (HTTP n)",
  and a network error "Couldn't reach SoundCloud ... (<exception class>)". The target is
  logged at info level without its query (SoundCloud's `utm_*` share tracking).
- **`resolve`** calls `_extract(link)`: one `extract_info(clean_url, download=False,
  ie_key=_extractor_for(link))` with `extract_flat="in_playlist"`, `noplaylist`,
  `skip_download` and the quiet logger. The extractor is pinned by the parsed kind:
  `SoundcloudSet` for a set on `soundcloud.com`, `SoundcloudPlaylist` for an API playlist,
  `Soundcloud` for a track (yt-dlp still checks its `suitable()`). Left to itself yt-dlp takes
  the first extractor that matches, and `SoundcloudRelated` (the lists built around a track,
  `/<user>/<track>/recommended|albums|sets`) comes before `SoundcloudSet` and has no end anchor:
  it takes every set whose name starts with `recommended`, `albums` or `sets`
  (`/<user>/sets/recommended`, `/<user>/sets/albums-i-love`; real ones are common), reads
  `/<user>/sets` as a track and lists something else entirely. The tests ask the installed
  yt-dlp which extractor it would pick for such links, and run one through the real
  `YoutubeDL` with the extractors' network side replaced. No
  `cachedir` is set: yt-dlp scrapes SoundCloud's public client id from `soundcloud.com` and
  keeps it in its own cache (`$XDG_CACHE_HOME/yt-dlp`, else `~/.cache/yt-dlp`), so every later
  `YoutubeDL` the app makes skips that lookup.
  - A track comes back complete, formats included (flat extraction only flattens lists).
    `is_preview_only(info)` (every format a preview: `format_id` ending in `_preview`, or
    `preference == -10`) raises `ProviderError(GO_PLUS)` before anything is queued; a
    DRM-protected track already fails in the extraction (yt-dlp's "This video is DRM
    protected"). The one `TrackRef`: `source_id` is the numeric track id; `url` is the track's
    page (`webpage_url` through `parse_link` and `clean_url`, so an API, player or short link
    is downloaded and recorded as the page, a private one with its `/s-...`), else the pasted
    link's `clean_url`; artist, title and album from `pick_tags`; `duration`; `thumbnail_url`,
    the last artwork `trim_artwork` keeps (500x500); `extra={"original_url": <the pasted
    link, normalised>}`.
  - A set (`_type` "playlist") goes to `_refs_from_set`: one request for the whole set (two
    for a private set of more than five tracks: SoundCloud sends full track objects for the
    first five only, and yt-dlp then fetches the rest with the set's token), not one per track,
    and the flat entries carry an id and a URL but no title. Each entry with an id becomes
    `TrackRef(provider="soundcloud", source_id=str(id), url=<the entry's URL as yt-dlp gave
    it>, title="<set title> · track <n>", album=<the set title or None>,
    extra={"container": "<label>: <set title>"})`. yt-dlp gives the track's page for the first
    tracks of a set and `https://api-v2.soundcloud.com/tracks/<id>` for the later ones. A
    track yt-dlp marks as one (`ie_key` Soundcloud) whose page `parse_link` does not take as a
    track link, a track named like a profile tab such as `/<user>/spotlight`, is listed by its
    id as `api-v2.soundcloud.com/tracks/<id>` (with `?secret_token=` from a private page's
    `s-...` part). Any other entry whose URL is not a SoundCloud track link is skipped
    (`_entry_url` gives None, logged): its id is then not known to be a track's, and a
    playlist id made into `tracks/<id>` would download some unrelated track. Entries without an id are skipped too (`n` stays the position in the
    set). The label follows yt-dlp's `album_type`:
    `album`, `ep`, `single` and `compilation` give Album, EP, Single and Compilation and make
    the set's title the album of its tracks; anything else, `""` included, is a Playlist
    without an album. `downloader._parent_message` turns the container into the parent row's
    "Album: out of spite (8 tracks)". An empty set gives `[]`, which the downloader reports as
    "Nothing to download at that link".
  - The placeholder title is safe because `TrackRef.title` of these refs is read through
    `Job.title` only: the queue row's label (web UI, `up add`'s progress lines, a log line)
    until the download returns a `Track`, after which it is "Artist - Title". The download
    names and tags the file from yt-dlp's info of the track itself, never from the ref's title,
    and the only other reader of ref titles, the Spotify provider, reads its own refs. Without
    it a row would show the api-v2 link.
- **Naming** (`pick_tags(info) -> (artist, title, album)`, offline):
  1. "Artist - Title" split from the title on a dash (` - `, ` – `, ` — `; never on `|`,
     which on SoundCloud separates a label or a tail: "Still Alive By Boss, Harp | Coin
     Digital | New Punjabi Songs 2021"), when SoundCloud lists no artist, or the listed artist
     is the uploader (a label: NCS, Monstercat), or the listed artist and the left side
     contain one another as whole words, case ignored ("Adele - Hello" by Adele does not
     become "Adele - Adele - Hello", and "MO" is not in "Mountain Echo"), or the first of the
     listed names occurs in the left side as a whole word. SoundCloud lists several artists
     joined with commas ("Tobu, Diviners", "Robin Hustin, Tobimorrow and Jex") where the
     title joins them with "&", "x" or "and" ("Tobu & Diviners - Geometry"), so the listed
     artist is split on `,`, ` & `, ` and `, ` x `, ` feat. ` and ` ft. ` (`_first_listed_name`);
     only the first name counts, so a remixer listed second ("Song - W&W Remix" listed as
     "Someone, W&W") does not make the title's dash a split. A left side holding "(" is no
     artist, and neither is one without a letter ("634 - Monstercat Call of the Wild: ...", an
     episode number; "01 - Intro") unless it is the listed artist or the uploader, as for the
     band 311 (`_left_names_artist`);
  2. else the listed artist and the whole cleaned title ("MadReal" and "Sideways (Prod. Mad
     Real)", uploaded by garyvee);
  3. else the uploader ("SoundCloud" when there is none) and the title, or "SoundCloud track
     <id>" when no title is left.

  The listed artist is `artists[0]`, stripped (SoundCloud's publisher metadata), never
  yt-dlp's `artist`, which joins the list with a FULLWIDTH COMMA in place of each comma
  ("Boss， Harp"). One without a letter or digit, or one that echoes the title (all of it, or
  the start of it, more than half of it and not followed by a separator: "BENDY AND THE INK
  MACHINE SONG (Build Our Machine) INSTRUMENTAL" for a title that goes on "by DAGAMES") is
  ignored. `clean_sc_title` removes YouTube-style junk (`youtube.clean_title`) and, as often as
  they repeat, SoundCloud's tails: a `[... Release]` in square brackets only ("(2019 Release)"
  is a version), `[Free Download]`, `(FREE DL)`, `[OUT NOW]`. After each tail it runs
  `youtube.clean_title` again, so a separator the tail leaves behind goes too ("Song - [NCS
  Release]" is "Song", not "Song -", which would reach the tag and the file name); when nothing
  is left, the raw title stays. The album is yt-dlp's `album` when `album_type` is album, ep,
  single or compilation; a track resolved on its own has none. The tests hold the trimmed info
  dicts of real tracks (labels, NCS's comma-listed artists, a remixer, the FULLWIDTH COMMA, the
  DAGAMES echo, a Monstercat episode, a private track) next to the rule cases.
- **Errors** (`friendly_error`, on `youtube.explain_error`, where an HTTP status in the
  message beats the substring rules):
  - HTTP 401 / 403: `_REFUSED`, "SoundCloud refused the request for this track or playlist.
    This usually clears up by itself within a few minutes (SoundCloud limits how many requests
    one computer makes in a short time): wait a few minutes, then retry the failed tracks. If
    it still happens much later, yt-dlp needs an update". The transient cause comes first
    because SoundCloud's rate limit often arrives as exactly this: after about 590 API calls in
    10 minutes every JSON call answered "Unable to download JSON metadata: HTTP Error 403:
    Forbidden" (resolving a set as well as a track), yt-dlp's retry with a fresh client id got
    the same, and it cleared by itself within minutes. 429: `_RATE_LIMITED`, which ends "Wait a
    few minutes, then retry the failed tracks."; 404 / 410: `_NOT_FOUND`, deleted, or private
    and the link lost its `s-...` part (SoundCloud answers both alike); 5xx: "having problems
    right now".
  - The rules, in order: the shared `NETWORK_ERRORS` ("Couldn't reach SoundCloud");
    `rate limit` / `too many requests` (yt-dlp's own "You have reached the API rate limit,
    which is ~600 requests per 10 minutes"); `drm protected`; geo restriction (several
    spellings): "This track isn't available in your country."; `requested format is not
    available` (the selector above): `GO_PLUS`; `unable to extract client id`: SoundCloud
    changed its page and a newer yt-dlp is needed; `no video formats found` / `unsupported
    url`: "That SoundCloud link isn't a track or a playlist."; the shared `CONVERSION_ERRORS`.
    The fallback is "SoundCloud download failed." The explanation never says YouTube or video
    (a test checks every rule and status), while yt-dlp's original text, which `explain_error`
    appends in brackets, may ("This video is DRM protected").
- **Cover** (`TrimArtworkPP`): yt-dlp writes, and `EmbedThumbnail` embeds, the last
  thumbnail of its sorted list, which for SoundCloud is the `original` upload, at whatever size
  it was uploaded and often over a megabyte. As a `pre_process` post-processor `TrimArtworkPP` runs before that
  choice, and `trim_artwork` drops the original (unless it is all there is) and SoundCloud's
  grey `default_avatar` placeholder (yt-dlp takes the uploader's picture when a track has no
  artwork, and that placeholder when the uploader has none). So `t500x500` is written, or no
  cover at all. Its `_hook_progress` does nothing, so the queue never shows "Processing
  (TrimArtwork)". `resolve()` takes `thumbnail_url` from the same trimmed list.
- **Downloads** are `YouTubeProvider.download` with the hooks above: the track id
  `soundcloud:<id>`, the name from `pick_tags` on the track's own info, `album or ref.album`,
  the page as source URL. Some `youtube.py` changes came with them: the queue shows
  "Finishing the download" for yt-dlp's `FixupM3u8` / `FixupM4a` step (SoundCloud's AAC
  arrives over HLS and is put into a proper container before the conversion); yt-dlp's
  "Original download format is only available for registered users" warning, which comes with
  every track whose uploader allows downloads of the original file, is logged at DEBUG like the
  impersonation notice (the stream is what the app saves anyway); a download yt-dlp returns no
  information for says "<site> returned no information for this link" (it said "video", wrong
  for a SoundCloud track or a TikTok post); and a failed or cancelled download closes its open
  `.part` before its folder is removed (`remove_staging_dir`, see the videos paragraph above),
  which only SoundCloud's HLS streams left open.
- **`doctor`**: `youtube.ffmpeg_check` and `ytdlp_check`; no JavaScript runtime, no account.
  `up doctor` prints them in a SoundCloud block; the web UI has no SoundCloud chip.

Known limitations:

- A Go+ or DRM-protected track inside a set is found only when its own download starts (the
  flat listing has no formats), so it fails on its own row while the rest of the set
  continues.
- No sign-in: Go+ tracks in full and the original files uploaders offer to registered users are
  out of reach, and a private track needs its secret link.
- Nothing waits out SoundCloud's rate limit (the provider sets no extractor retries or pauses,
  and the queue does not pause); a set of a few hundred tracks can run into it, and the failed
  tracks say to wait and retry: as the rate-limit text (a 429 or yt-dlp's own "API rate
  limit"), or, as SoundCloud more often answers, as the 403 text, which puts waiting first.
- Profiles, likes, reposts, stations and Discover are refused, not expanded into their tracks.

## Adding a provider

The checklist, with the built-in providers as examples: YouTube is the self-contained kind
(one library does resolve, download and conversion), Spotify the composite kind (metadata from
one place, audio delegated to another provider, tags rewritten afterwards), TikTok and
SoundCloud the subclass kind (another site yt-dlp supports: subclass `YouTubeProvider`, override
its hooks).

1. **One module under `providers/`** with a class that has `name` (short lowercase slug; it
   becomes the first half of every track id and must never change afterwards) and
   `display_name`. Optional and slow imports stay inside that module (module level is fine
   there; `youtube.py` imports `yt_dlp` at the top and exposes `YoutubeDL` so tests can swap in
   a fake) and nothing outside `providers/` imports them.
2. **`matches(url)`**: hostname and path regexes, offline, no exceptions. For link kinds you
   cannot download (channels, artists, users) either say False, so the user gets the generic
   "No provider" message (YouTube), or own the host and refuse them in `resolve()` with a
   sentence saying what to paste instead (Spotify, TikTok, SoundCloud), which is friendlier.
3. **`resolve(url)`**: one `TrackRef` per downloadable track, cheap metadata filled in, silent
   drops for unavailable items, `ProviderError` with a sentence a non-programmer understands
   for everything else. Keep `extra` small and JSON-safe.
4. **`download(...)`**: produce exactly one tagged file in `dest_dir`, working in
   `<library>/.incoming/` and moving into place with `os.replace` under `youtube._move_lock`
   via `destination_for`; emit progress on every status change; check `cancel` and raise
   `DownloadCancelled` after cleaning up; write the `ULTIMATE_PLAYLIST_ID` tag (use
   `youtube.write_tags`); return a `Track` whose `id` equals `ref.track_id`. If the audio comes
   from another provider, delegate to its `download()` and re-tag, like Spotify does. A
   `YouTubeProvider` subclass inherits all of that and changes it through the hooks only:
   `ydl_format` and `ydl_video_format` (the yt-dlp format selectors), `_tags_for(info)` or,
   when the ref knows more than yt-dlp's info, `_tags_for_ref(ref, info)` (the name and tags),
   `_source_url(ref, info)` (the link in the comment tag and `Track.source_url`),
   `_prepare_ydl(ydl)` (called on the `YoutubeDL` before it runs, e.g. to add a
   post-processor) and `_explain(error)` (the error texts, usually `youtube.explain_error`
   with the site's rules). It also inherits `supports_video = True`; a site without video sets
   it to False, as SoundCloud does, or a Video request would ask yt-dlp for a video.
5. **`doctor()`**: `(ok, label, detail)` triples, never raising, with the fix in `detail`.
   Optional `configure(settings)` if `resolve()` / `doctor()` need live settings.
6. **Settings**, if the provider needs any: add fields to `Settings` (old config files keep
   working, unknown keys are ignored), a value rule in `config.py`, a line in `SettingsPatch`
   in `server/app.py`, the settings dialog and `up config set`. Keep secrets out of
   `Settings`: `GET /api/settings` and `up config show` hand out `config.json` as it is.
   Tokens belong in a file of their own that only the provider reads, like
   `spotify_auth.json`.
7. **Register**: add the `(module, class)` pair to `_load_builtin()` in `providers/__init__.py`.
   Order only matters when two providers claim the same URL. A constructor that raises drops
   the provider from the registry with a warning. Add the module to `hiddenimports` in
   `packaging/ultimate-playlist.spec` too: PyInstaller cannot see an import by name, and the
   packaged exe would silently lack the provider (`test_registry` checks it).
8. **Tests, offline, always** (see below). No test may reach the network; if a test needs it,
   it is wrong.
9. **Docs**: a section on this page, the README's "What it does" list, the textbox placeholder
   in `server/static/index.html` if the user should know the new kind of link is welcome.

Pitfalls seen so far: wrong matches are worse than no matches (be strict, keep the evidence in
`extra` and in the tags); rate limits (resolution is one request per page of a playlist,
matching one search per track; `settings.concurrency` already bounds parallel downloads, do not
add a thread pool of your own); regional catalogues (fail per track, never the whole playlist);
Windows paths (use `.incoming/` and `safe_filename`, create no other folders or temp files).

## Testing approach (project-wide)

- Everything is offline. Real YouTube, Spotify, TikTok and SoundCloud are exercised only by a
  manual integration run (`uv run up add <url>`), never by pytest.
- Unit tests cover the pure helpers in `providers/youtube.py`, `Settings` and the value rules
  in `config.py`, `find_ffmpeg` (fake `PATH`), `Library`, `PlaylistStore` and `export_m3u8`.
- `JobManager` flows (single, playlist, skip, cancel, error, retry) run against `FakeProvider`
  (`tests/fake_provider.py`: writes a tiny valid MP3 kept as bytes, tags it with mutagen, emits
  DOWNLOADING then CONVERTING, honours `cancel`, can be told to fail) with real threads and
  `wait_idle()`.
- Spotify: `matches` over every link shape; `spotify_meta` against a fake HTTP session that
  serves the real, trimmed `__NEXT_DATA__` entities in `tests/fixtures/spotify/embed_*.json`
  and canned Web API JSON (two pages to prove pagination, a `null` track, a local file and an
  episode to prove they are dropped, a playlist without `items` and a 403 that both fall back
  to the embed page, a 403 / 5xx / network error on a track, album or playlist that falls back
  too while a cancel does not, Liked Songs, a 401 that renews once); `spotify_auth` against a
  fake token endpoint and `/me` (PKCE values, state expiry and single use, every error mapping,
  renewal under a lock with concurrent callers, refresh-token rotation, a refused renewal
  deleting the file, a renewal another process made first being used instead, waiting for
  another process's lock, a renewal that could not be saved, no token in any log line);
  `score_candidate` / `find_match` against canned candidates (live versions, remixes, the
  duration limit, multi-artist credits, accents, the title and artist floors; each real wrong
  pick seen live has a regression test) and `search_music` / `search_youtube` against a fake
  `YoutubeDL`; `download` with the matcher and `YouTubeProvider.download` replaced by fakes
  that write the tiny MP3 and the cover fetch stubbed, asserting the final name is
  `Artist - Title.mp3`, the `TXXX` frames read `spotify:<id>` and the video id, the Spotify
  cover replaced the YouTube one, and that `DownloadCancelled` propagates; one end-to-end run
  through `JobManager`.
- SoundCloud (`tests/test_soundcloud_provider.py`): `parse_link` over every link shape and
  every refusal reason (hosts compared exactly, userinfo, port and backslash tricks, private
  tokens, the player), with every rebuilt link put to the installed yt-dlp's own SoundCloud
  extractors (`suitable()`); `pick_tags` on the trimmed info dicts of real tracks plus the rule
  cases; `friendly_error` on real yt-dlp error texts, whose explanation may never say YouTube
  or video (yt-dlp's original text appended in brackets may); `resolve()` (the extractor it
  pins included) and `download()` against a fake `YoutubeDL` and a fake `requests.head`
  (a HEAD request the test did not set up fails it; a short link is only ever sent to
  `on.soundcloud.com`); the format selector and `TrimArtworkPP` run through the real yt-dlp's
  selection and pre-processing on SoundCloud-shaped format and artwork lists (nothing is
  downloaded); a set and a Video download through `JobManager`; a failed or cancelled download
  whose fake fragment downloader leaves its `.part` open (in a closure and a reference cycle,
  as FragmentFD does) with the automatic garbage collector off, after which the file must be
  closed and the folder gone. `test_youtube_provider.py` checks that the three new hooks
  change nothing for YouTube; `test_downloader.py` the start-up sweep of `.incoming`.
- `tests/test_app_js.py` runs functions of `static/app.js` in Node (skipped without Node) with
  their surroundings stubbed: which job transitions reload the library.
- Every API endpoint is tested through FastAPI's `TestClient` (`httpx`), including error paths
  (400 for unsupported links, 404 for unknown ids), the `/media/{id}` range request, settings
  validation, the `/api/spotify` routes (login redirect, every callback outcome, the code never
  in a response) and the token scrub in `/api/status`.
- `uv run pytest -q` and `uv run ruff check .` must pass on Windows and Linux; CI runs both on
  `ubuntu-latest` and `windows-latest`. Tests must close files they open and never use `/tmp`
  (use pytest's `tmp_path`).

## Conventions

- `from __future__ import annotations`; type hints everywhere; `log = logging.getLogger(__name__)`.
- Library code never prints; the CLI prints. User-facing strings are friendly; no stack traces
  reach the UI. Secrets are never logged.
- Paths inside JSON are strings; `Track.path` is relative POSIX. Files are written to a `.tmp`
  and moved into place with `os.replace`.
- Third-party imports that belong to one source (`yt_dlp`, the HTTP session for Spotify) stay
  inside `providers/`; nothing in `models.py`, `library.py`, `downloader.py`, `server/` or
  `cli.py` imports them, and the registry guards a failing import by leaving that provider out.
