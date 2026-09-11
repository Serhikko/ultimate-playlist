# Architecture

For contributors, and in particular for whoever adds the Spotify provider. The binding contract
for every module is [SPEC.md](SPEC.md); this document explains how the pieces fit together and
walks through adding a new source.

Two rules keep the codebase small:

1. **Everything talks in the types from `models.py`** (`TrackRef`, `Track`, `Job`, `Playlist`,
   `ProgressEvent`). Those types, and the `Provider` protocol in `providers/base.py`, are frozen.
2. **Only the provider layer knows where audio comes from.** `downloader.py`, `library.py`,
   `server/`, `cli.py` never import `yt_dlp`, never look at a URL's hostname, never care whether a
   track came from YouTube or Spotify. Adding Spotify means adding one module under `providers/`
   plus tests. Nothing else should need to change (a couple of optional cosmetic touches are
   listed at the end).

## Module map

```text
src/ultimate_playlist/
  __init__.py            __version__ (shown by `up --version` and GET /api/status)
  __main__.py            `python -m ultimate_playlist` -> cli.main()
  cli.py                 argparse CLI, prog "up": serve, add, list, doctor, rescan, playlists, export
  config.py              Settings dataclass (config.json) and app_data_dir() (~/.ultimate-playlist)
  ffmpeg.py              find_ffmpeg() / install_hint(); nobody else shells out to ffmpeg
  logs.py                console + rotating app.log handlers; keeps yt-dlp chatter and UI polling off the terminal
  models.py              FROZEN: TrackRef, Track, JobStatus, ProgressEvent, Job, Playlist
  downloader.py          JobManager: resolver thread + worker threads, the job state machine
  library.py             Library: index of finished tracks (library.json), tag reading, rescan, covers
  playlists.py           PlaylistStore (playlists.json) and export_m3u8()
  providers/__init__.py  registry: PROVIDERS, register/unregister, get_provider, provider_by_name, doctor_all
  providers/base.py      FROZEN: Provider protocol, ProviderError / ProviderNotAvailable / DownloadCancelled
  providers/youtube.py   YouTubeProvider (yt-dlp). The only module that imports yt_dlp.
  providers/spotify.py   SpotifyProvider: a stub today (raises ProviderNotAvailable). This is where you work.
  server/app.py          create_app() -> FastAPI, serve() -> uvicorn bound to 127.0.0.1
  server/static/         index.html, app.js, style.css: vanilla, no build step, no CDN
tests/                   offline pytest suite; conftest.py fixtures; fake_provider.py
```

## Data flow: paste -> get_provider -> resolve -> jobs -> download -> Library -> UI

```text
 user pastes a URL          (web UI textbox -> POST /api/jobs {url}, or `up add URL`)
        |
        v
 JobManager.submit(url)
        |  providers.get_provider(url): normalises the URL (strips whitespace, adds https://
        |  when there is no scheme), then asks each provider in PROVIDERS order `matches(url)`;
        |  the first True wins. None -> ValueError("No provider for this link") -> HTTP 400 / CLI error.
        v
 Job(status=QUEUED, provider=<name>)  ->  resolve queue
        |
        v
 resolver thread: job.status = RESOLVING; refs = provider.resolve(url)
        |   0 refs -> ERROR  "Nothing to download at that link"
        |   1 ref  -> the same job gets job.track_ref and moves to the download queue
        |   N refs -> the job becomes a parent (status DONE, child_count=N,
        |             message "Playlist: N tracks") and N child jobs (parent_id set)
        |             are created and queued in playlist order
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
        |
        v
 UI: polls GET /api/jobs (1 s while something is active, else 5 s) and GET /api/library?q=;
     plays GET /media/{id}; shows GET /api/library/{id}/cover; playlists via /api/playlists
     and export_m3u8() -> <library>/Playlists/<name>.m3u8
```

Things worth knowing about that flow:

- `dest_dir` passed to `download()` is the **library root**. The provider writes intermediate
  files under `<library>/.incoming/` (short path, Windows MAX_PATH matters) and moves the finished
  file into the root with `os.replace`.
- De-duplication is by `TrackRef.track_id` (`"<provider>:<source_id>"`), checked right before the
  download. The same YouTube video is therefore downloaded once, no matter how many playlists
  contain it.
- The `Library` is thread-safe (one `RLock`) and every mutation on a `Job` happens under the
  `JobManager` lock. Providers themselves are called from worker threads, so `download()` must be
  re-entrant across instances of work (no shared mutable state on `self` without a lock).

## The frozen types (`models.py`)

| Type | Role |
| --- | --- |
| `TrackRef(provider, source_id, url, title?, artist?, album?, duration?, thumbnail_url?, extra={})` | "Something a provider can turn into exactly one audio file." Produced by `resolve()`, consumed by `download()`. `track_id` property = `f"{provider}:{source_id}"`. `extra` is a free dict for provider-private data (a Spotify ISRC, a matched YouTube id, ...) and is serialised into `/api/jobs` as-is, so keep it JSON-safe and small. |
| `Track(id, provider, source_id, source_url, title, artist, path, album?, duration?, has_cover, file_size, added_at)` | A finished file in the library. `id` must equal the ref's `track_id`. `path` is relative to the library root with forward slashes. `to_dict()` / `from_dict()` (unknown keys ignored) are what `library.json` stores. |
| `JobStatus` | `queued, resolving, downloading, converting, done, skipped, error, cancelled`; `is_terminal` for the last four. |
| `ProgressEvent(status, progress?, speed?, eta?, message?)` | What a provider emits through the `progress` callback. `progress` is 0.0..1.0, `speed` bytes/s, `eta` seconds. |
| `Job(url, provider?, id, status, progress, speed, eta, message, error, parent_id, child_count, track_ref?, track?, created_at, updated_at)` | One unit of queue work. A playlist becomes a parent job plus one child per track. `title` property falls back ref title -> URL. `to_dict()` is the `/api/jobs` payload. |
| `Playlist(name, id, track_ids, created_at, updated_at)` | Ordered list of track ids; dangling ids are pruned against the library. |

IDs for tracks are always `<provider>:<source_id>`; `local:<sha1 of relative path>[:12]` is used
by `Library.rescan()` for files that carry no `ULTIMATE_PLAYLIST_ID` tag. Job and playlist ids are
random 12-hex strings (`new_id()`).

## The Provider protocol (`providers/base.py`)

```python
ProgressCallback = Callable[[ProgressEvent], None]


# message must be readable by a non-programmer
class ProviderError(Exception): ...


# exists but cannot run (dependency / credentials / not built)
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
`resolve()` / `doctor()` depend on the live settings (ffmpeg path, JS runtimes, credentials) can
implement an optional `configure(self, settings)`; `run_doctor` passes `settings` to a
`doctor(self, settings)` that takes one and calls the plain `doctor(self)` otherwise.

`_load_builtin()` runs at import time and instantiates
`ultimate_playlist.providers.youtube.YouTubeProvider` and
`ultimate_playlist.providers.spotify.SpotifyProvider`, in that order. A provider whose import or
constructor raises is logged and **left out of the registry entirely**, so keep heavy or optional
imports (`spotdl`, `spotipy`) inside methods and report their absence through `doctor()` instead
of failing at import time.

## The JSON files

All three live in `app_data_dir()` (`~/.ultimate-playlist`, or `$ULTIMATE_PLAYLIST_HOME`), are
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
  "js_runtimes": ["deno", "node"]
}
```

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
    }
  }
}
```

`playlists.json` (`PlaylistStore`):

```json
{
  "version": 1,
  "playlists": [
    {
      "name": "Road trip",
      "id": "3f9c2a1b7d4e",
      "track_ids": ["youtube:dQw4w9WgXcQ", "local:9a1f0c3b2e7d"],
      "created_at": "2026-09-11T10:20:00+00:00",
      "updated_at": "2026-09-11T10:25:00+00:00"
    }
  ]
}
```

The MP3 itself carries enough to rebuild the index: `TPE1` artist, `TIT2` title, `TALB` album,
`COMM` = source URL, `APIC` cover, and a `TXXX` frame with description `ULTIMATE_PLAYLIST_ID`
holding the track id. `Library.rescan()` walks the library folder (skipping `.incoming/` and
`Playlists/`), reads those tags with mutagen, and recovers the id from the `TXXX` frame. Any
provider must write that frame, otherwise its tracks come back as `local:...` after a rescan.

## How the YouTube provider does it (the template to mirror)

`providers/youtube.py` is the reference implementation; read it once before writing Spotify.

- `matches`: hostname in `youtube.com`, `www.`, `m.`, `music.youtube.com`, `youtu.be`,
  `youtube-nocookie.com`; paths `/watch`, `/playlist`, `/shorts/<id>`, `/live/<id>`,
  `/embed/<id>`, `youtu.be/<id>`. Channel URLs return False.
- `resolve`: `yt_dlp.YoutubeDL(...).extract_info(url, download=False)` with `extract_flat`, so a
  playlist costs one request. `DownloadError` becomes `ProviderError(friendly_error(exc))`.
- `download`: yt-dlp downloads `bestaudio` into `<library>/.incoming/<id>.<ext>`, its
  post-processors convert to MP3, add metadata and embed the thumbnail. The provider then decides
  `artist, title = pick_artist_title(info)`, rewrites the tags on the file still inside
  `.incoming/` with `write_tags(path, artist, title, album, source_url, track_id)` (keeps the
  cover yt-dlp embedded; ID3 for MP3, MP4 atoms for M4A, Vorbis comments for Opus/FLAC), builds
  `safe_filename(f"{artist} - {title}") + <extension of the produced file>`, picks the final
  path with `destination_for(path, track_id)` (the plain name, or the ` (n)` sibling that
  already holds this very track so a re-download replaces it, else the first free ` (n)` name;
  `unique_path` remains a simpler helper that only looks for a free name) and moves the file
  with `os.replace`; name pick and move happen under one module lock so two workers finishing
  the same "Artist - Title" cannot overwrite each other. Leftovers in `.incoming/<id>.*` are
  removed in a `finally`.
- `doctor`: ffmpeg (via `ffmpeg.find_ffmpeg`), JavaScript runtime (Deno or Node), yt-dlp version.

The pure helpers (`split_artist_title`, `clean_title`, `safe_filename`, `unique_path`,
`destination_for`, `pick_artist_title`, `friendly_error`, `write_tags`, `stored_track_id`) are
plain functions you can import and reuse.

## Adding the Spotify provider

### What exists today

`providers/spotify.py` is a stub: `name = "spotify"`, `display_name = "Spotify"`, `matches()`
recognises `open.spotify.com`, `play.spotify.com`, `spotify.link` and `spotify:` URIs, and
`resolve()` / `download()` raise
`ProviderNotAvailable("Spotify support is not built yet. See docs/ARCHITECTURE.md to add it.")`.
`doctor()` reports one failing check. It is already listed in `_load_builtin()`, so pasting a
Spotify link today goes through the whole pipeline and ends in a friendly job error. You replace
the bodies; nothing needs registering.

### What Spotify can and cannot give us (DRM)

Spotify streams are DRM-protected (Widevine/PlayPlay); they cannot be downloaded and this project
does not try. What Spotify *does* give freely, with a developer app and the Web API, is
metadata: track name, artists, album, cover art, duration, ISRC, track number, release date, and
the contents of albums and playlists. So the design is the one spotDL uses:

> **metadata from Spotify, audio from YouTube Music, tags from Spotify.**

The audio download is the existing YouTube pipeline; the Spotify provider is a matcher and a
re-tagger around it.

### Step 1: dependencies

```text
uv add spotdl
```

spotDL brings `spotipy` (Spotify Web API client) and `ytmusicapi` (YouTube Music search) with it,
plus its own matching logic. Check the resolver output: spotDL pins its own `yt-dlp` range, and
`uv lock` may move the resolved `yt-dlp` version. Make sure `uv run pytest -q` still passes and
that it resolves on the Python the target machine uses (3.14 managed by uv; `requires-python` is
`>=3.11`). If spotDL does not resolve cleanly, the lighter route is `uv add spotipy ytmusicapi`
and doing the (small) matching step yourself; the design below works either way.

Keep the imports inside the methods that need them:

```python
def _client(self, settings: Settings):
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
    except ImportError as exc:
        raise ProviderNotAvailable("Spotify support needs spotDL: run `uv add spotdl`.") from exc
    ...
```

### Step 2: credentials in `Settings`

The Web API needs a free Spotify developer app (<https://developer.spotify.com/dashboard>, create
an app, copy Client ID and Client Secret; the Client Credentials flow needs no redirect URL and no
user login, and it is enough for public tracks, albums and playlists).

Add two optional fields to `Settings` in `config.py`:

```python
spotify_client_id: str | None = None
spotify_client_secret: str | None = None
```

`from_dict` ignores unknown keys and `to_dict` is `asdict`, so old config files keep working and
new ones persist the fields. `PUT /api/settings` accepts partial bodies, which gives the UI a way
to set them; a "Spotify" box on the status chip or a small settings dialog is enough. Fall back
to the `SPOTIPY_CLIENT_ID` / `SPOTIPY_CLIENT_SECRET` environment variables that spotipy reads by
default, so a developer can run without touching the config. Never log the secret, and the
app-data folder is already outside the repo.

### Step 3: `matches(url)`

```python
_HOSTS = {"open.spotify.com", "play.spotify.com", "spotify.link"}
_KINDS = ("track", "album", "playlist")


def matches(self, url: str) -> bool:
    if url.startswith("spotify:"):
        return url.split(":")[1] in _KINDS  # spotify:track:<id>
    p = urlparse(url)
    if p.hostname not in _HOSTS:
        return False
    parts = p.path.strip("/").split("/")
    if parts and parts[0].startswith("intl-"):  # /intl-de/track/<id>
        parts = parts[1:]
    return len(parts) >= 2 and parts[0] in _KINDS  # /artist/... -> False (like YouTube channels)
```

`spotify.link` short links redirect to `open.spotify.com`; accept them here and resolve the
redirect (one `HEAD` request) inside `resolve()`.

### Step 4: `resolve(url)`

Use the Client Credentials flow and paginate:

```python
sp = self._client(settings)                # spotipy.Spotify(auth_manager=SpotifyClientCredentials(...))
kind, sid = self._parse(url)               # ("track" | "album" | "playlist", "<spotify id>")
if kind == "track":
    items = [sp.track(sid)]
elif kind == "album":
    album = sp.album(sid)                  # album_tracks() items lack album/images; carry them over
    items = self._pages(sp, sp.album_tracks(sid)); attach album name + images to each
else:
    items = [it["track"] for it in self._pages(sp, sp.playlist_items(sid)) if it.get("track")]
refs = [self._to_ref(t) for t in items if self._usable(t)]
```

- `_pages` follows `page["next"]` with `sp.next(page)` (100 items per page for playlists).
- `_usable(t)`: has an `id`, `t.get("is_local")` is False, `t.get("type") != "episode"`
  (podcasts are not music). Drop the rest silently, like private YouTube videos.
- `_to_ref(t)`:

  ```python
  TrackRef(
      provider="spotify",
      source_id=t["id"],
      url=f"https://open.spotify.com/track/{t['id']}",
      title=t["name"],
      artist=", ".join(a["name"] for a in t["artists"]),
      album=t["album"]["name"],
      duration=t["duration_ms"] / 1000,
      thumbnail_url=(t["album"].get("images") or [{}])[0].get("url"),
      extra={
          "isrc": t.get("external_ids", {}).get("isrc"),
          "track_number": t.get("track_number"),
          "release_date": t["album"].get("release_date"),
      },
  )
  ```

- Error mapping (friendly text, original message appended in parentheses, like
  `youtube.friendly_error`): `spotipy.SpotifyException` with status 401/403 and no credentials
  -> `ProviderNotAvailable("Spotify needs a client id and secret. Create an app at
  developer.spotify.com/dashboard and enter them in Settings.")`; 404 -> `ProviderError("That
  Spotify link does not exist or is private.")`; 429 -> "Spotify is rate-limiting us, try again
  in a minute"; network errors -> "Could not reach Spotify. Check your internet connection."
- Playlist metadata (`sp.playlist(sid)["name"]`) is not part of a `TrackRef`; if you want the
  parent job to show the playlist name, put it into `refs[0].extra["playlist_name"]` and leave it
  at that. The `JobManager` derives its "Playlist: N tracks" message itself.

`Track.id` is therefore `spotify:<track id>`, and the same Spotify track is never downloaded
twice even if it appears in several Spotify playlists. Note that a song downloaded once from
YouTube and once from Spotify will be two files with two ids; that is acceptable in v0.x.

### Step 5: `download(ref, dest_dir, settings, progress, cancel)`

Three phases: match, download through YouTube, re-tag.

**5a. Match on YouTube Music.** Either use spotDL's matcher (`Spotdl.get_download_urls` on a
`Song` built from the ref) or search directly:

```python
from ytmusicapi import YTMusic

progress(ProgressEvent(JobStatus.DOWNLOADING, 0.0, message="Searching YouTube Music"))
if cancel.is_set():
    raise DownloadCancelled()
results = YTMusic().search(f"{ref.artist} - {ref.title}", filter="songs", limit=10)
video_id = best_match(results, ref)  # see below
if not video_id:
    raise ProviderError(f'Couldn\'t find "{ref.artist} - {ref.title}" on YouTube Music.')
```

`best_match` is the part worth unit-testing with canned results. A scoring that works well in
practice: reject candidates whose `duration_seconds` differs from `ref.duration` by more than
~8 s; prefer a candidate whose artist list contains the primary Spotify artist (case-insensitive,
accents folded); prefer a title match after stripping `(feat. ...)`, `- Remastered ...`;
penalise `live`, `cover`, `remix`, `karaoke`, `sped up`, `nightcore` in the candidate title when
the Spotify title does not contain them; tie-break on the album name. Return `None` when the best
score is below a threshold rather than downloading the wrong song. Store the choice in
`ref.extra["youtube_id"]` so it shows up in `/api/jobs` and is easy to debug.

**5b. Download through the YouTube provider.** Build a YouTube `TrackRef` and delegate:

```python
from .youtube import YouTubeProvider

yt_ref = TrackRef(
    provider="youtube",
    source_id=video_id,
    url=f"https://music.youtube.com/watch?v={video_id}",
    title=ref.title,
    artist=ref.artist,
    album=ref.album,
    duration=ref.duration,
)
yt_track = YouTubeProvider().download(yt_ref, dest_dir, settings, progress, cancel)
```

This gives you the whole pipeline for free: `.incoming/` handling, MP3 conversion with the user's
`audio_quality`, progress events, cancel handling (`DownloadCancelled` simply propagates),
friendly yt-dlp errors, cleanup. `yt_track` is a `Track` with `id="youtube:<video id>"`, a file
named from YouTube's idea of artist/title, and tags that say `youtube:<video id>`. Do not add it
to the library; the `JobManager` adds whatever *you* return.

**5c. Re-name and re-tag with Spotify metadata.**

```python
progress(ProgressEvent(JobStatus.CONVERTING, 1.0, message="Writing Spotify tags"))
src = dest_dir / yt_track.path
write_tags(src, ref.artist, ref.title, ref.album, source_url=ref.url, track_id=ref.track_id)
final = destination_for(
    dest_dir / (safe_filename(f"{ref.artist} - {ref.title}") + src.suffix), ref.track_id
)
if final != src:
    os.replace(src, final)
has_cover = self._embed_cover(final, ref.thumbnail_url, settings) or yt_track.has_cover
```

(Tag first, then pick the name: `destination_for` reads the id back from an existing file to
decide whether it may be replaced. `unique_path` works too if you never re-download.)

- `write_tags` (from `providers/youtube.py`) writes `TPE1`/`TIT2`/`TALB`/`COMM` and sets the
  `TXXX ULTIMATE_PLAYLIST_ID` frame to `spotify:<track id>`, which is what `rescan()` needs.
- `_embed_cover`: fetch `ref.thumbnail_url` (the Spotify album art, 640 px JPEG) with
  `urllib.request.urlopen(url, timeout=15)` (stdlib; `httpx` is only a dev dependency), then
  with mutagen's raw `ID3` API `delall("APIC")` and add
  `APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=bytes)`. Only when
  `settings.embed_cover` is True; on any failure keep the YouTube cover and log at debug level.
  Optionally add `TRCK` (track number), `TDRC` (release year), `TSRC` (ISRC) while you are there.
- Return the `Track` under the Spotify identity:

  ```python
  stat = final.stat()
  return Track(
      id=ref.track_id,
      provider="spotify",
      source_id=ref.source_id,
      source_url=ref.url,
      title=ref.title,
      artist=ref.artist,
      album=ref.album,
      duration=MP3(final).info.length or ref.duration,
      has_cover=has_cover,
      path=final.relative_to(dest_dir).as_posix(),
      file_size=stat.st_size,
  )
  ```

Error handling: everything that is not `DownloadCancelled` or already a `ProviderError` must be
wrapped as `ProviderError(f"Download failed: {exc}")`; if renaming or tagging fails after the
YouTube step, remove the file so a retry starts clean.

### Step 6: `doctor()`

Never raise. Suggested checks, in order:

```text
(spotdl_ok, "spotDL",              "spotdl <version>"  or  "Not installed: run `uv add spotdl`")
(creds_ok,  "Spotify credentials", "Client id set"     or  "Create an app at developer.spotify.com/dashboard
                                                            and enter the client id/secret in Settings")
*YouTubeProvider().doctor()        # ffmpeg, JavaScript runtime, yt-dlp: the audio still comes from YouTube
```

`GET /api/status` and `up doctor` render these automatically; the UI's status chips show the
`detail` of any failing check as the tooltip. `up doctor` exits 1 only on YouTube failures, so a
missing Spotify secret does not turn the whole health check red.

### Step 7: registration

Already done: `_load_builtin()` lists `("ultimate_playlist.providers.spotify", "SpotifyProvider")`
after YouTube. Order only matters when two providers claim the same URL, which they do not. Just
make sure importing the module and constructing the class cannot raise (no top-level `import
spotdl`), otherwise the registry logs a warning and drops the provider.

### Step 8: tests (offline, always)

Follow `tests/fake_provider.py` and `tests/conftest.py`:

- `FakeProvider` shows the shape of a provider that needs no network and no ffmpeg: it writes a
  tiny valid MP3 (a static silent MPEG frame kept as bytes in the file), tags it with mutagen,
  emits DOWNLOADING then CONVERTING, honours `cancel`, and can be told to fail. Reuse those bytes
  in your tests.
- `conftest.py` provides `tmp_settings` (library in `tmp_path`, `ULTIMATE_PLAYLIST_HOME` pointed
  at `tmp_path`), `fake_provider` (registered via `providers.register`, unregistered on
  teardown), `library`, `playlists`, and `client` (FastAPI `TestClient` with a `JobManager`).
  `JobManager.wait_idle(timeout)` lets a test wait for the queue without sleeping.
- Suggested `tests/test_spotify.py`:
  1. `matches`: track/album/playlist URLs, `intl-xx` paths, `spotify:` URIs, `spotify.link`;
     negatives: artist and user URLs, `open.spotify.com` alone, YouTube URLs.
  2. `resolve`: monkeypatch `SpotifyProvider._client` to return a fake object whose `track`,
     `album`, `album_tracks`, `playlist_items`, `next` return canned dicts (include a local file,
     an episode and a `None` track to prove they are dropped; two pages to prove pagination).
  3. `best_match`: canned `ytmusicapi` results; check the duration filter, the live/cover
     penalty, and that nothing below the threshold is returned.
  4. `download`: monkeypatch `YouTubeProvider.download` with a function that writes the fake MP3
     into `dest_dir` and returns a YouTube `Track`, and `_embed_cover` (or `urlopen`) with a stub;
     assert the final name is `Artist - Title.mp3`, the `TXXX` frame reads `spotify:<id>`,
     `Track.id`/`provider` are Spotify's, and that `DownloadCancelled` propagates when the fake
     raises it.
  5. `doctor` without credentials returns a failing "Spotify credentials" check and never raises.
  6. One end-to-end run through `JobManager` (or `client.post("/api/jobs", ...)`) with the
     patched pieces, asserting the job ends DONE and the track is in the library.

No test may reach Spotify, YouTube Music or YouTube. If a test needs the network it is wrong.

### Step 9: optional polish outside the provider

- `server/static/index.html`: the textbox placeholder says "Paste a YouTube link or playlist";
  make it "YouTube or Spotify". Everything else in the UI (status chips, provider checks, job
  rendering) is data-driven from `/api/status` and `/api/jobs`.
- A small settings dialog for the two Spotify fields, backed by `PUT /api/settings`.
- README: move Spotify from "Next" to "Now" and document the developer-app step.

### Known pitfalls

- **Wrong matches** are worse than no matches. Be strict in `best_match`, keep the chosen
  `youtube_id` in `extra` so users can report bad picks.
- **Rate limits**: playlist resolution is one request per 100 tracks, matching is one search per
  track. `settings.concurrency` (max 6) already bounds parallel downloads; do not add your own
  thread pool.
- **Regional catalogues**: a track may exist on Spotify but not on YouTube Music in your country.
  Report it as a per-track `ProviderError`; the rest of the playlist continues.
- **Local files and podcasts** in playlists: skip in `resolve`, never raise.
- **Windows paths**: use the YouTube provider's `.incoming/` handling and `safe_filename`; do not
  create sub-folders or temp files anywhere else.

## Testing approach (project-wide)

- Everything is offline. Real YouTube is exercised only by a manual integration run
  (`uv run up add <url>`), never by pytest.
- Unit tests cover the pure helpers in `providers/youtube.py`, `Settings`, `find_ffmpeg` (fake
  `PATH`), `Library`, `PlaylistStore` and `export_m3u8`.
- `JobManager` flows (single, playlist, skip, cancel, error, retry) run against `FakeProvider`
  with real threads and `wait_idle()`.
- Every API endpoint is tested through FastAPI's `TestClient` (`httpx`), including error paths
  (400 for unsupported links, 404 for unknown ids), the `/media/{id}` range request and settings
  validation.
- `uv run pytest -q` and `uv run ruff check .` must pass on Windows and Linux; CI runs both on
  `ubuntu-latest` and `windows-latest`. Tests must close files they open and never use `/tmp`
  (use pytest's `tmp_path`).

## Conventions

- `from __future__ import annotations`; type hints everywhere; `log = logging.getLogger(__name__)`.
- Library code never prints; the CLI prints. User-facing strings are friendly; no stack traces
  reach the UI.
- Paths inside JSON are strings; `Track.path` is relative POSIX. Files are written to a `.tmp`
  and moved into place with `os.replace`.
- Keep third-party imports that are optional or slow (`yt_dlp`, `spotdl`, `spotipy`) inside the
  provider module that needs them (module level is fine there: `providers/youtube.py` imports
  `yt_dlp` at the top and exposes `YoutubeDL` so tests can swap in a fake); nothing outside
  `providers/` imports them, and the registry guards a failing import by leaving that provider
  out. Keep them out of `models.py`, `library.py`, `downloader.py`, `server/` and `cli.py`.
