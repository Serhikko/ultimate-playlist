# Ultimate Playlist — build spec (v0.1, YouTube side)

This is the contract every module is written against. `models.py` and `providers/base.py` already
exist and are FROZEN: code against them, do not change their public shapes (adding a helper is ok).

## Goal

A personal desktop tool. Paste a YouTube link (video, Music track, playlist, Shorts, `youtu.be`),
get a tagged MP3 with cover art in one local library folder, organise tracks into playlists, play
them in the app, export playlists as M3U8. A friend will add Spotify later by implementing the
same `Provider` protocol, so the provider layer must be the only place that knows about YouTube.

## Environment (facts, verified on the target machine)

- Windows 11. Python 3.14 managed by `uv`. Run everything as `uv run ...` from the repo root.
- `ffmpeg` 8.x is on PATH. `node` 24 is on PATH, `deno` is NOT.
- yt-dlp 2026.08.19 (`yt-dlp[default]`, includes `yt-dlp-ejs`). YouTube extraction needs a JS
  runtime; yt-dlp only auto-enables Deno, so we MUST pass
  `"js_runtimes": {"deno": {"path": None}, "node": {"path": None}}` in the YoutubeDL options.
  Verified: with that option and `-x --audio-format mp3 --audio-quality 0 --embed-thumbnail
  --embed-metadata` a download + convert + cover embed works end to end.
- Windows MAX_PATH bites: yt-dlp failed writing a thumbnail into a ~250-char path. Keep intermediate
  files in a short path (`<library>/.incoming/<source_id>.<ext>`), never in deep temp dirs.
- Tests must not touch the network. Real YouTube is exercised only by the integration step.

## Layout

```
src/ultimate_playlist/
  __init__.py            __version__
  __main__.py            -> cli.main()
  cli.py                 argparse CLI (see below)
  config.py              Settings + app data dir
  ffmpeg.py              locate ffmpeg
  models.py              FROZEN
  downloader.py          JobManager (queue + worker threads)
  library.py             Library (index of finished tracks, tag reading, rescan)
  playlists.py           PlaylistStore + M3U8 export
  providers/
    __init__.py          registry: PROVIDERS, get_provider(url), register(provider)
    base.py              FROZEN
    youtube.py           YouTubeProvider (yt-dlp)
    spotify.py           SpotifyProvider stub (raises ProviderNotAvailable, has doctor())
  server/
    __init__.py
    app.py               create_app(settings=None, ...) -> FastAPI
    static/index.html, app.js, style.css
tests/                   pytest, offline, uses FakeProvider (see Testing)
docs/SPEC.md             this file
docs/ARCHITECTURE.md     for contributors (the Spotify friend): how a provider is wired in
README.md                user-facing
.github/workflows/ci.yml ruff + pytest on ubuntu-latest and windows-latest
```

## config.py

```python
APP_NAME = "ultimate-playlist"

def app_data_dir() -> Path
    # env ULTIMATE_PLAYLIST_HOME if set, else ~/.ultimate-playlist. Created on demand.

@dataclass
class Settings:
    library_dir: Path = Path.home() / "Music" / "Ultimate Playlist"
    audio_format: str = "mp3"          # only mp3 is exercised in v0.1 but keep it a setting
    audio_quality: str = "0"           # yt-dlp preferredquality; "0" = VBR best
    ffmpeg_path: str | None = None     # explicit override, else PATH
    concurrency: int = 2               # parallel downloads
    embed_cover: bool = True
    js_runtimes: list[str] = field(default_factory=lambda: ["deno", "node"])

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings"   # path default app_data_dir()/config.json; missing file -> defaults
    def save(self, path: Path | None = None) -> None
    def to_dict(self) -> dict            # JSON-safe (paths as str)
    @classmethod
    def from_dict(cls, d: dict) -> "Settings"   # ignores unknown keys, coerces library_dir to Path
    @property
    def incoming_dir(self) -> Path       # library_dir / ".incoming"
    @property
    def index_path(self) -> Path         # app_data_dir() / "library.json"
    @property
    def playlists_path(self) -> Path     # app_data_dir() / "playlists.json"
    @property
    def playlists_export_dir(self) -> Path   # library_dir / "Playlists"
```

Settings must expand `~` and env vars in `library_dir`. Never crash on a corrupt config.json:
log a warning and fall back to defaults.

## ffmpeg.py

```python
@dataclass
class FfmpegInfo: path: str | None; version: str | None
def find_ffmpeg(explicit: str | None = None) -> FfmpegInfo
    # order: explicit -> shutil.which("ffmpeg") -> optional `imageio_ffmpeg.get_ffmpeg_exe()` (import guarded) -> not found
    # version: parse first line of `ffmpeg -version` (timeout 5s); any failure -> version None
def install_hint() -> str   # platform-specific one-liner, Windows: "winget install Gyan.FFmpeg"
```

## providers/__init__.py

```python
PROVIDERS: list[Provider]          # order matters: first match wins. [YouTubeProvider(), SpotifyProvider()]
def register(provider: Provider) -> None   # insert at front (used by tests with FakeProvider)
def unregister(name: str) -> None
def get_provider(url: str) -> Provider | None
def provider_by_name(name: str) -> Provider | None
def doctor_all() -> dict[str, list[tuple[bool, str, str]]]
```

`get_provider` strips whitespace and accepts URLs without scheme (`youtu.be/abc`).

## providers/youtube.py

`class YouTubeProvider` with `name = "youtube"`, `display_name = "YouTube"`.

`matches(url)`: hosts `youtube.com`, `www.youtube.com`, `m.youtube.com`, `music.youtube.com`,
`youtu.be`, `youtube-nocookie.com`. Paths: `/watch`, `/playlist`, `/shorts/<id>`, `/live/<id>`,
`/embed/<id>`, `youtu.be/<id>`. A pure channel URL (`/@name`, `/channel/...`) -> False (we don't
scrape whole channels in v0.1).

`resolve(url)`:
- `YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "noplaylist": True,
  "skip_download": True, "ignoreerrors": True, "js_runtimes": ...}).extract_info(url, download=False)`.
  `noplaylist=True` means `watch?v=X&list=Y` downloads only X; a `/playlist?list=` URL expands.
- If result `_type == "playlist"`: one TrackRef per entry with `entries[i]` not None and
  `entry.get("id")`; skip entries whose `title` is `[Private video]`/`[Deleted video]`. `url` =
  `https://www.youtube.com/watch?v=<id>`. Fill title/artist/duration/thumbnail from the flat entry
  when present (`title`, `channel` or `uploader`, `duration`, `thumbnails[-1].url`).
- Else a single TrackRef from the info dict.
- Map `yt_dlp.utils.DownloadError` and friends to `ProviderError` with a friendly message via
  `friendly_error(exc) -> str` (private / unavailable / age-restricted / geo / no internet / generic).
  Keep the original text appended in parentheses, trimmed to ~200 chars.

`download(ref, dest_dir, settings, progress, cancel)`:
- Ensure `dest_dir` and `dest_dir/.incoming` exist.
- Options (build via `_ydl_opts(settings, incoming_dir, progress, cancel)` so tests can inspect them):
  ```
  format: "bestaudio/best", outtmpl: str(incoming / "%(id)s.%(ext)s"),
  postprocessors: [ {key: FFmpegExtractAudio, preferredcodec: settings.audio_format, preferredquality: settings.audio_quality},
                    {key: FFmpegMetadata, add_metadata: True},
                    {key: EmbedThumbnail, already_have_thumbnail: False}  # only if settings.embed_cover ],
  writethumbnail: settings.embed_cover, noplaylist: True, quiet: True, no_warnings: False, noprogress: True,
  windowsfilenames: True, retries: 3, fragment_retries: 3, overwrites: True, continuedl: False,
  js_runtimes: {rt: {"path": None} for rt in settings.js_runtimes},
  ffmpeg_location: settings.ffmpeg_path  (only if set),
  logger: a small adapter forwarding to logging.getLogger("ultimate_playlist.youtube"),
  progress_hooks: [hook], postprocessor_hooks: [pp_hook]
  ```
- `hook(d)`: if `cancel.is_set()` raise `DownloadCancelled`. On `d["status"] == "downloading"`:
  progress = downloaded_bytes / (total_bytes or total_bytes_estimate) if available; emit
  `ProgressEvent(DOWNLOADING, progress, speed, eta)`. On `"finished"`: emit
  `ProgressEvent(CONVERTING, 1.0, message="Converting to MP3")`.
- `pp_hook(d)`: on `started` emit CONVERTING with message from `d["postprocessor"]`.
- `info = ydl.extract_info(ref.url, download=True)`; the produced file is
  `incoming / f"{info['id']}.{settings.audio_format}"` (verify exists; if not, look for
  `info.get("requested_downloads", [{}])[0].get("filepath")`).
- Naming: `artist, title = pick_artist_title(info)`:
  1. if `info.get("artist")` and `info.get("track")` -> use them as-is;
  2. else parse `info["title"]` with `split_artist_title(title)`: regex `^\s*(.+?)\s+[-–—|]\s+(.+?)\s*$`
     where the left side has no "(" and both sides are non-empty; strip trailing junk from the title:
     `(Official Video)`, `[Official Audio]`, `(Lyrics)`, `(Lyric Video)`, `(Audio)`, `(HD)`, `(4K)`,
     `(Visualizer)`, `| Official ...`, `(Official Music Video)`, case-insensitive, plus `ft./feat.`
     stays. Implement as `clean_title(title) -> str` and test it;
  3. else artist = `info.get("channel") or info.get("uploader") or "Unknown Artist"` with a trailing
     ` - Topic` removed; title = cleaned info title.
  album = `info.get("album")` or None.
- Final filename: `safe_filename(f"{artist} - {title}") + ".mp3"` in `dest_dir` (flat, no subfolders
  in v0.1). `safe_filename` removes `<>:"/\|?*`, control chars, trailing dots/spaces, collapses
  whitespace, max 150 chars. If the target exists and is a different track, append ` (2)`, ` (3)`...
  (`unique_path(path) -> Path`).
- Move (`os.replace`) the incoming file to the final path. Clean up any leftover
  `incoming/<id>.*` files (thumbnails, .part) in a `finally`.
- Tag with mutagen (`write_tags(path, artist, title, album, source_url, track_id)`): ID3 `TPE1`,
  `TIT2`, `TALB` (if album), `COMM` = source_url, and a `TXXX` frame with desc
  `ULTIMATE_PLAYLIST_ID` = `youtube:<id>` so a rescan can recover identity. Keep the cover that
  yt-dlp embedded (`APIC`); `has_cover` = any APIC frame present. Use the raw `ID3` API (not
  EasyID3); if the file has no ID3 header, add one.
- duration: `mutagen.mp3.MP3(path).info.length` (fallback `info.get("duration")`).
- Return `Track(id=ref.track_id, provider="youtube", source_id=info["id"], source_url=ref.url, ...,
  path=final.relative_to(dest_dir).as_posix(), file_size=stat.st_size)`.
- Errors: `DownloadCancelled` propagates; `yt_dlp.utils.DownloadError` -> `ProviderError(friendly)`;
  anything else -> `ProviderError(f"Download failed: {exc}")`. Always clean up `.incoming/<id>.*`.

`doctor()`: `[ (ffmpeg found?, "ffmpeg", path/version or install_hint()),
  (js runtime found?, "JavaScript runtime", "node 24.x" / "Install Deno: winget install DenoLand.Deno (or Node.js)"),
  (True, "yt-dlp", version) ]`. Detect runtimes with
  `yt_dlp.utils._jsruntime.DenoJsRuntime().info()` / `NodeJsRuntime().info()` guarded by try/except
  (fall back to `shutil.which("deno"/"node")`).

Keep every yt-dlp import inside the module (`import yt_dlp`), and keep pure helpers
(`split_artist_title`, `clean_title`, `safe_filename`, `unique_path`, `pick_artist_title`,
`friendly_error`, `_ydl_opts`) importable and testable without network.

## providers/spotify.py

```python
class SpotifyProvider:
    name = "spotify"; display_name = "Spotify"
    def matches(url): hosts open.spotify.com / spotify.link / play.spotify.com or "spotify:" URIs
    def resolve(url): raise ProviderNotAvailable("Spotify support is not built yet. See docs/ARCHITECTURE.md to add it.")
    def download(...): same
    def doctor(): [(False, "Spotify", "Not implemented yet — planned via spotDL (metadata from Spotify, audio from YouTube Music).")]
```
Include a docstring sketching the intended spotDL approach and the exact steps to wire it in.

## library.py

```python
class Library:
    def __init__(self, library_dir: Path, index_path: Path)
    def load() / save()                      # index JSON: {"version": 1, "tracks": {id: Track dict}}
    def all() -> list[Track]                 # sorted by added_at desc
    def get(track_id) -> Track | None
    def has(track_id) -> bool                # true only if index has it AND the file still exists
    def add(track: Track) -> None            # upsert + save
    def remove(track_id, delete_file=False) -> bool
    def search(q: str) -> list[Track]        # case-insensitive substring over title/artist/album
    def abs_path(track) -> Path              # library_dir / track.path
    def rescan() -> int                      # walk library_dir (skip .incoming, Playlists), pick up
                                             # *.mp3/*.m4a/*.opus/*.flac not in index; read tags with
                                             # mutagen; recover id from TXXX ULTIMATE_PLAYLIST_ID else
                                             # id = "local:<sha1 of relative path>[:12]"; drop index
                                             # entries whose file vanished. Returns number of changes.
    def cover_bytes(track_id) -> tuple[bytes, str] | None   # (data, mime) from APIC / MP4 covr / FLAC picture
```
Thread-safe (one `threading.RLock`). Atomic saves (write `.tmp` then `os.replace`).

## playlists.py

```python
class PlaylistStore:
    def __init__(self, path: Path)
    def load() / save()                      # {"version": 1, "playlists": [Playlist dict...]}
    def all() -> list[Playlist]
    def get(pid) -> Playlist | None
    def create(name) -> Playlist             # name stripped, non-empty, duplicates allowed
    def rename(pid, name) -> Playlist
    def delete(pid) -> bool
    def add_tracks(pid, track_ids: list[str]) -> Playlist   # append, skip ids already present
    def remove_track(pid, track_id) -> Playlist
    def set_order(pid, track_ids: list[str]) -> Playlist    # must be a permutation of current ids, else ValueError
    def prune(existing_ids: set[str]) -> int  # drop dangling track ids, return count removed

def export_m3u8(playlist: Playlist, library: Library, out_path: Path, relative_to: Path | None = None) -> Path
    # writes UTF-8 with "#EXTM3U", then per track "#EXTINF:<int duration or -1>,<artist> - <title>"
    # and the path (relative to `relative_to` if given, posix separators; else absolute OS path).
    # Missing tracks (not in library) are skipped. Returns out_path.
```

## downloader.py

```python
class JobManager:
    def __init__(self, settings: Settings, library: Library, providers=None)   # providers default: the registry module
    def start() / stop(wait=True)            # `concurrency` worker threads + 1 resolver thread; idempotent
    def submit(url: str) -> Job              # validates via get_provider; unknown -> raise ValueError("No provider for this link")
                                             # creates a Job(status=QUEUED) and puts it on the resolve queue
    def list(include_finished=True) -> list[Job]   # newest first
    def get(job_id) -> Job | None
    def cancel(job_id) -> bool               # queued -> CANCELLED immediately; active -> set cancel event
    def retry(job_id) -> Job | None          # ERROR/CANCELLED -> new job with same url (or same track_ref)
    def clear_finished() -> int
    def snapshot() -> list[dict]             # job.to_dict() for the API, under the lock
    def wait_idle(timeout: float | None = None) -> bool   # for tests and the CLI
```
Flow: resolver thread takes a job, sets RESOLVING, calls `provider.resolve(url)`:
- 0 refs -> ERROR "Nothing to download at that link".
- 1 ref -> attach `track_ref` to the same job, then queue it for download.
- N refs -> the job becomes a parent (`child_count=N`, status DONE, message "Playlist: N tracks"),
  and N child jobs with `parent_id` are created and queued (in playlist order).
Before downloading: if `library.has(ref.track_id)` -> SKIPPED with message "Already in library".
Worker: sets DOWNLOADING, calls `provider.download(...)` with a per-job `threading.Event` and a
progress callback that copies fields onto the job (throttle updates to ~4/s). On success: `job.track`
set, `library.add(track)`, DONE. `DownloadCancelled` -> CANCELLED. `ProviderError` -> ERROR with
`str(exc)`. Any other exception -> ERROR with "Unexpected error: ..." and `log.exception`.
Every mutation updates `updated_at` and happens under one lock.

## server/app.py

`create_app(settings: Settings | None = None, library: Library | None = None,
playlists: PlaylistStore | None = None, jobs: JobManager | None = None) -> FastAPI`. Objects not
passed are created from settings. `app.state.settings/library/playlists/jobs` are set. The JobManager
starts on startup and stops on shutdown (lifespan). Bind to 127.0.0.1 only (in `serve()`).

Endpoints (JSON; errors as `{"detail": "..."}` with proper 4xx):
- `GET  /` -> static/index.html. `GET /static/*` -> files.
- `GET  /api/status` -> `{version, library_dir, providers:[{name, display_name, checks:[{ok,label,detail}]}], ffmpeg:{path,version}, jobs_active:int, tracks:int}`
- `GET  /api/settings` -> Settings.to_dict(); `PUT /api/settings` (partial body) -> validates
  `library_dir` (created if missing), `concurrency` 1..6, saves, returns new settings. Changing
  `library_dir` re-points the Library and rescans.
- `POST /api/jobs` body `{url}` -> 202 `{"jobs":[...]}`. 400 for unsupported link (message from ValueError).
  Accept multiple links separated by whitespace/newlines: returns `{"jobs":[...]}` always (even for one).
- `GET  /api/jobs` -> `{"jobs":[...]}`; `POST /api/jobs/{id}/cancel`; `POST /api/jobs/{id}/retry`;
  `DELETE /api/jobs/finished` -> `{"removed": n}`.
- `GET  /api/library?q=` -> `{"tracks":[...]}`; `DELETE /api/library/{id}?delete_file=false`;
  `POST /api/library/rescan` -> `{"changed": n, "tracks": n}`; `GET /api/library/{id}/cover` ->
  image bytes or 404; `POST /api/library/open` -> opens the folder in the OS file manager
  (`os.startfile` on Windows, `open`/`xdg-open` elsewhere), returns `{"ok": true}`.
- `GET  /media/{id}` -> FileResponse of the audio file (Starlette handles Range for `<audio>`);
  404 if unknown. The id is looked up in the index, never used to build a path, so no traversal.
- `GET  /api/playlists` -> `{"playlists":[...]}`; `POST /api/playlists {name}`; `PATCH /api/playlists/{id}
  {name?, track_ids?}` (track_ids = full new order); `DELETE /api/playlists/{id}`;
  `POST /api/playlists/{id}/tracks {track_ids:[...]}`; `DELETE /api/playlists/{id}/tracks/{track_id}`;
  `POST /api/playlists/{id}/export` -> writes `<library>/Playlists/<safe name>.m3u8` (relative
  paths) and returns `{"path": "..."}`; `GET /api/playlists/{id}/export.m3u8` -> the file as a
  download with absolute paths (for VLC on this machine).

`serve(settings=None, host="127.0.0.1", port=8765, open_browser=True)` runs uvicorn; if the port is
busy try the next 10. Opens `http://127.0.0.1:<port>/` with `webbrowser` after a short delay in a
thread. Log to stderr and to `app_data_dir()/app.log` (RotatingFileHandler, 1 MB x 3).

## Web UI (server/static)

Vanilla HTML/CSS/JS, no build step, no CDN (must work offline). Dark theme, modern, compact.
Structure:
- Top bar: app name, a wide input ("Paste a YouTube link or playlist…", accepts multiple lines
  when pasted, Enter submits), "Download" button, and status chips: ffmpeg ✓/✗, JS runtime ✓/✗,
  library folder (click -> POST /api/library/open). A ✗ chip shows the fix hint in a tooltip.
- Left column "Queue": each job = title (or URL while resolving), status pill, progress bar with
  % / speed / ETA, error text in red, Cancel (active) / Retry (error, cancelled) buttons.
  "Clear finished" button. Parent playlist jobs show "Playlist: N tracks". Poll `/api/jobs` every
  1 s while any job is non-terminal, else every 5 s.
- Main area with two tabs: "Library" and "Playlists".
  - Library: search box; table rows: cover thumb (from /api/library/{id}/cover, lazy, fallback
    icon), title, artist, duration mm:ss, added date, ▶ play, "+ playlist" menu (lists playlists,
    click to add; "New playlist…" prompt), 🗑 delete (confirm; checkbox "also delete file").
    Click a row to play. Multi-select with checkboxes + "Add selected to playlist".
  - Playlists: left list of playlists (+ New, rename via double-click, delete), right: tracks of the
    selected one with ▲▼ reorder buttons (and HTML5 drag-and-drop if cheap), remove, "Play all",
    "Export M3U8" (calls POST export and shows the path + a link to the GET download).
- Bottom player bar: `<audio>` element, cover, title/artist, prev / play-pause / next, shuffle and
  repeat toggles, seek slider, time, volume. The "current list" is whatever view started playback
  (library results or a playlist). Keyboard: Space toggles play when focus is not in an input.
- Toasts for errors (e.g. unsupported link). All fetches go through one `api()` helper.
- Responsive enough for a half-screen window (min 900px wide is fine).

## cli.py

argparse, `prog="up"`. No subcommand -> `serve`. Subcommands:
- `serve [--port 8765] [--no-browser] [--host 127.0.0.1]`
- `add URL [URL...] [--no-wait]` : submits, prints progress lines per job, exits 0 if
  all DONE/SKIPPED else 1. Uses JobManager directly (no server).
- `list [-q QUERY]` : prints tracks as a table (id, artist, title, duration, path).
- `doctor` : prints provider checks + ffmpeg + library dir + app data dir, exits 1 if any ✗ for youtube.
- `rescan`, `playlists`, `export PLAYLIST_NAME_OR_ID [OUT_PATH]`.
- `--version`. `--library DIR` global override.
`python -m ultimate_playlist` == `up`.

## Testing (offline)

- `tests/conftest.py`: fixtures `tmp_settings` (Settings with `library_dir=tmp_path/"lib"` and
  `ULTIMATE_PLAYLIST_HOME` env pointed at `tmp_path/"home"` via monkeypatch), `fake_provider`
  (see below, registered via `providers.register` and unregistered on teardown), `library`,
  `playlists`, `client` (FastAPI TestClient with a JobManager using the fake provider).
- `tests/fake_provider.py`: `FakeProvider(name="fake")` matching `https://fake.test/track/<id>` and
  `https://fake.test/playlist/<n>` (n tracks). `download` writes a tiny valid MP3 (a static
  ~1 KB byte string of a silent MPEG frame generated once and embedded as bytes in the file, so no
  ffmpeg is needed) named `Fake Artist - Track <id>.mp3`, tags it with mutagen, emits DOWNLOADING
  then CONVERTING, honours `cancel` via a configurable `delay`, and can be told to fail
  (`fail_ids`).
- Cover: unit tests for `split_artist_title`, `clean_title`, `safe_filename`, `unique_path`,
  `pick_artist_title`, `friendly_error`, `_ydl_opts` (asserts js_runtimes, postprocessors, outtmpl),
  `YouTubeProvider.matches` (incl. negatives), `Settings` load/save/corrupt, `find_ffmpeg` with a
  fake PATH, `Library` add/has/remove/search/rescan/cover, `PlaylistStore` CRUD + set_order
  validation + prune + `export_m3u8` content, `JobManager` single/playlist/skip/cancel/error/retry
  flows, and every API endpoint via TestClient (including 404/400 paths, `/media` range request,
  settings validation).
- `uv run pytest -q` and `uv run ruff check .` must both pass. Windows-safe (no `/tmp`, close files).

## Conventions

- `from __future__ import annotations`, type hints everywhere, `logging.getLogger(__name__)`.
- Never print from library code; the CLI prints.
- User-facing strings are friendly; no stack traces in the UI.
- Paths inside JSON are always strings; `Track.path` is relative POSIX.
- Small focused functions; docstrings on public functions only where the contract isn't obvious.
