# Architecture

For contributors. `models.py` and `providers/base.py` are frozen: the types every module talks
in and the `Provider` protocol. Everything else is described here and in the module docstrings.
The second half of this page is the guide for adding a source, with YouTube and Spotify as the
two worked examples.

Two rules keep the codebase small:

1. **Everything talks in the types from `models.py`** (`TrackRef`, `Track`, `Job`, `Playlist`,
   `ProgressEvent`). Those types, and the `Provider` protocol in `providers/base.py`, are frozen.
2. **Only the provider layer knows where audio comes from.** `downloader.py`, `library.py`,
   `server/`, `cli.py` never import `yt_dlp`, never look at a URL's hostname, never care whether a
   track came from YouTube or Spotify. Spotify support is three modules under `providers/` plus
   tests; the queue, the library, the playlists and the player did not change for it. (The
   settings dialog and `up config` arrived in the same release because the Spotify credentials
   needed a home, but they are generic.)

## Module map

```text
src/ultimate_playlist/
  __init__.py                 __version__ (shown by `up --version` and GET /api/status)
  __main__.py                 `python -m ultimate_playlist` -> cli.main()
  cli.py                      argparse CLI, prog "up": serve, add, list, doctor, rescan, playlists, export, config
  config.py                   Settings dataclass (config.json), the value rules shared by the API and
                              `up config set`, app_data_dir() (~/.ultimate-playlist), SECRET_MASK
  bundled.py                  tools shipped next to the packaged exe (<exe dir>/bin) win over PATH
  ffmpeg.py                   find_ffmpeg() / install_hint(); nobody else shells out to ffmpeg
  logs.py                     console + rotating app.log handlers; keeps yt-dlp chatter and UI polling off the terminal
  models.py                   FROZEN: TrackRef, Track, JobStatus, ProgressEvent, Job, Playlist
  downloader.py               JobManager: resolver thread + worker threads, the job state machine
  library.py                  Library: index of finished tracks (library.json), tag reading, rescan, covers
  playlists.py                PlaylistStore (playlists.json) and export_m3u8()
  providers/__init__.py       registry: PROVIDERS, register/unregister, get_provider, provider_by_name, configure, doctor_all
  providers/base.py           FROZEN: Provider protocol, ProviderError / ProviderNotAvailable / DownloadCancelled
  providers/youtube.py        YouTubeProvider (yt-dlp). The only module that imports yt_dlp.
  providers/spotify.py        SpotifyProvider: Spotify links -> metadata -> YouTube Music match -> YouTubeProvider.download -> re-tag
  providers/spotify_meta.py   Spotify metadata: link parsing, the public embed pages, the Web API (client credentials)
  providers/ytmusic_match.py  YouTube Music search (through yt-dlp) + candidate scoring (find_match); the scoring is pure and offline-testable
  server/app.py               create_app() -> FastAPI, serve() -> uvicorn bound to 127.0.0.1
  server/static/              index.html, app.js, style.css: vanilla, no build step, no CDN; settings dialog
tests/                        offline pytest suite; conftest.py fixtures; fake_provider.py
```

## Data flow: paste -> get_provider -> resolve -> jobs -> download -> Library -> UI

```text
 user pastes a URL          (web UI textbox -> POST /api/jobs {url}, or `up add URL`)
        |
        v
 JobManager.submit(url)
        |  providers.get_provider(url): normalises the URL (strips whitespace, adds https://
        |  when there is no scheme; `spotify:` URIs pass through), then asks each provider in
        |  PROVIDERS order `matches(url)`; the first True wins.
        |  None -> ValueError("No provider for this link") -> HTTP 400 / CLI error.
        v
 Job(status=QUEUED, provider=<name>)  ->  resolve queue
        |
        v
 resolver thread: job.status = RESOLVING; refs = provider.resolve(url)
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
| `Job(url, provider?, id, status, progress, speed, eta, message, error, parent_id, child_count, track_ref?, track?, created_at, updated_at)` | One unit of queue work. A playlist becomes a parent job plus one child per track. `title` property falls back ref title -> URL. `to_dict()` is the `/api/jobs` payload. |
| `Playlist(name, id, track_ids, created_at, updated_at)` | Ordered list of track ids; dangling ids are pruned against the library. |

IDs for tracks are always `<provider>:<source_id>` (`youtube:<video id>`, `spotify:<track id>`);
`local:<sha1 of relative path>[:12]` is used by `Library.rescan()` for files that carry no
`ULTIMATE_PLAYLIST_ID` tag. Job and playlist ids are random 12-hex strings (`new_id()`).

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
`resolve()` / `doctor()` depend on the live settings (ffmpeg path, JS runtimes, Spotify
credentials) can implement an optional `configure(self, settings)`; `run_doctor` passes
`settings` to a `doctor(self, settings)` that takes one and calls the plain `doctor(self)`
otherwise.

`_load_builtin()` runs at import time and instantiates
`ultimate_playlist.providers.youtube.YouTubeProvider` and
`ultimate_playlist.providers.spotify.SpotifyProvider`, in that order. A provider whose import or
constructor raises is logged and **left out of the registry entirely**, so keep heavy or optional
imports inside the provider's own modules and report their absence through `doctor()` instead of
failing at import time.

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
  "js_runtimes": ["deno", "node"],
  "spotify_client_id": "",
  "spotify_client_secret": ""
}
```

The two Spotify fields are optional (both `""` when unset; `Settings.has_spotify_credentials`
says whether both are filled). The secret is stored as-is in this local per-user file, like a
browser's cookie jar, but it never leaves the process: `Settings.public_dict()` replaces it with
`SECRET_MASK` (`********`) and that is what `GET /api/settings`, the settings dialog and
`up config show` hand out. A settings update that sends the mask back means "keep the stored
secret". The value rules (`AUDIO_FORMATS`, `clean_audio_quality`, `clean_concurrency`,
`clean_js_runtimes`, `clean_credential`, ...) live in `config.py` so `PUT /api/settings` and
`up config set` can never disagree.

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

The audio file itself carries enough to rebuild the index: `TPE1` artist, `TIT2` title, `TALB`
album, `COMM` = source URL, `APIC` cover, and a `TXXX` frame with description
`ULTIMATE_PLAYLIST_ID` holding the track id (Spotify tracks add a second `TXXX` frame,
`YOUTUBE_ID`, with the video the audio came from). `Library.rescan()` walks the library folder
(skipping `.incoming/` and `Playlists/`), reads those tags with mutagen, and recovers the id from
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
plain functions other providers import and reuse; the Spotify provider is built on them.

## How the Spotify provider does it (the composite example)

### The idea

Spotify streams are DRM-protected and this project never touches them. What Spotify gives
freely is metadata: track name, artists, album, cover art, duration, and the contents of albums
and playlists. So the design is

> **metadata from Spotify, audio from YouTube Music, tags from Spotify.**

The audio download is the existing YouTube pipeline; the Spotify provider is a metadata reader,
a matcher and a re-tagger around it. Three modules:

| Module | Job |
| --- | --- |
| `providers/spotify.py` | `SpotifyProvider`: `matches`, `resolve`, `download`, `doctor`, `configure(settings)`. Orchestrates the other two and delegates the audio to `YouTubeProvider`. |
| `providers/spotify_meta.py` | Everything Spotify-side: parsing links into `(kind, id)`, the public embed pages, the Web API (client credentials flow), pagination, mapping both shapes onto one plain record per track. Takes an HTTP session so tests can hand in a fake. |
| `providers/ytmusic_match.py` | The YouTube Music search and the scoring: `find_match(ref, settings, cancel, search)` tries the queries in order, `rank_candidates` / `score_candidate` score `Candidate` objects, `Scored.accepted` applies the thresholds. Everything but the two `search_*` functions is pure; tests hand `find_match` a canned `search` callable. |

### Links accepted (`matches`)

`matches` is a hostname check only: `open.spotify.com` (with or without `www.`),
`play.spotify.com`, the `spotify.link` / `spotify.app.link` short-link hosts, and anything that
starts with `spotify:`. What kind of link it is (`track`, `album`, `playlist`; an optional
`/intl-xx/` prefix, an `/embed/` segment, the old `/user/<name>/playlist/<id>` shape and
`spotify:user:<name>:playlist:<id>` URIs are all understood) is decided by
`spotify_meta.parse_spotify_url` inside `resolve()`. An artist, show, episode or profile link
therefore becomes a job that fails at once with "Only Spotify tracks, albums and playlists are
supported (not artists, podcasts or profiles)." rather than the generic "No provider for this
link": the specific sentence is worth more to the user than an offline rejection, and the
web UI shows it on the job. `spotify.link` short links are followed inside `resolve()` (a
`HEAD`, then a `GET`, then a scan of the page for an `open.spotify.com` URL), never in
`matches()`.

### Two metadata sources (`spotify_meta.py`)

**(a) Public embed pages, no credentials.** `GET https://open.spotify.com/embed/{track|album|playlist}/{id}`
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

**(b) The Spotify Web API with the user's own developer app.** Used automatically when
`settings.has_spotify_credentials` is true. Client Credentials flow:
`POST https://accounts.spotify.com/api/token` with `grant_type=client_credentials` and HTTP
Basic auth (client id : client secret); the bearer token is cached until 60 s before
`expires_in` runs out, a 401 is retried once with a fresh token, and a 429 waits for
`Retry-After` (capped at 30 s) once. Endpoints (`limit=50` is the documented maximum for both
paging endpoints):

- `GET /v1/tracks/{id}`;
- `GET /v1/albums/{id}` (name, images, release date, the first page of tracks) plus
  `GET /v1/albums/{id}/tracks?limit=50`, following `next` until it is null (the album items lack
  `album` / `images`, so the album's own values are attached to every item; items with
  `is_playable: false` are skipped);
- `GET /v1/playlists/{id}?fields=id,name,images,owner(display_name)` plus
  `GET /v1/playlists/{id}/tracks?limit=50&fields=...` (the `fields` filter names both the
  `track` key and the newer `item` key of the same object), following `next`. `items[].track`
  may be `null` or have `is_local: true`, and may be an episode: all three are skipped. A track
  has `name`, `artists[{name}]`, `album{name, images[{url,width}], release_date}`,
  `duration_ms`, `track_number`, `external_ids{isrc}`.

The API removes the playlist cap and fills in the album name, cover, release date, track number
and ISRC for playlist items. Its known limitation: personal developer apps cannot read
Spotify's own editorial and algorithmic playlists (ids starting with `37i9dQZF1`). The client
raises `SpotifyRefused` for a 403, or a 404, on a playlist; `get_metadata` retries that
playlist through the public page (the first 100 songs) and only when that fails too raises the
API's explanation ("Spotify does not let personal apps read this Spotify-made playlist; add
the songs to a playlist of your own and paste that link.").

**Error mapping** (constants at the top of `spotify_meta.py`; all of them are plain
`ProviderError`s): connection failures -> "Could not reach Spotify. Check your internet
connection and try again."; a 404 (embed or API) -> "That Spotify link does not exist or is
private."; a 429 -> "Spotify is rate-limiting us, try again in a minute."; the token endpoint
rejecting the credentials (400/401/403) -> "Spotify rejected the client id/secret in Settings.
Check them at developer.spotify.com/dashboard and try again." (the credentials are the user's
explicit choice, so a bad pair is reported, not silently worked around); an embed page without
`__NEXT_DATA__` -> "Could not read that Spotify page (it did not contain track data)."
Nothing in the module logs the secret or the token.

### `resolve(url)`

Parse the link, pick source (b) when credentials are set and (a) otherwise, fetch, and return
one `TrackRef` per track:

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
        "container": "Album: Random Access Memories" | "Playlist: <name>",
    },
)
```

Only keys with a value go into `extra`, and everything in it is JSON-safe (it is serialised into
`/api/jobs`); `download()` adds `"youtube_id"` once the match is known. `Track.id` is therefore
`spotify:<track id>`, and the same Spotify track is never downloaded twice even if it appears in
several Spotify playlists. `resolve()` logs one line per album / playlist ("Spotify playlist
'<name>': 50 playable track(s) via embed"), with a note when the list may be capped.

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
`YouTubeProvider`'s `download(yt_ref, <library>/.incoming, settings, progress, cancel)`. That
gives the whole pipeline for free: MP3 conversion with the user's `audio_quality`, progress
events, cancel handling (`DownloadCancelled` simply propagates), friendly yt-dlp errors,
cleanup. Note the `dest_dir`: the YouTube step runs with the **staging folder**
`<library>/.incoming/` as its "library root", so its own temporary files land in
`.incoming/.incoming/<video id>.*` and its finished, YouTube-named file in `.incoming/`. Had it
been given the real library root, an existing YouTube-provider file of the same video would
have been replaced and then renamed away. Downloads of one video id are serialised with a
per-id lock (two Spotify tracks, a single and an album version say, can map to the same
video). The returned `Track` has a YouTube identity; it is **not** added to the library (the
`JobManager` adds whatever the Spotify provider returns).

**3. Re-name and re-tag with the Spotify metadata.** `progress(CONVERTING, 1.0, "Writing
Spotify tags")`, then `write_spotify_tags(path, ref, video_id, cover)` on the staged file:

- `youtube.write_tags(path, artist, title, album, source_url=ref.url, track_id=ref.track_id)`:
  artist, title, album, comment = the Spotify track URL, and `TXXX ULTIMATE_PLAYLIST_ID =
  spotify:<track id>`, which is what `rescan()` needs.
- Then, in each container's own format (ID3 frames for MP3, MP4 atoms for M4A, Vorbis
  comments for Opus / FLAC): `TPE2` album artist, `TRCK` track number, `TDRC` release year,
  `TXXX ISRC` plus `TSRC`, each only when the metadata source provided it, and always
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
YouTube step, the staged file is removed so a retry starts clean. (Only a hard kill of the
process leaves yt-dlp's temporary files behind, under `.incoming/.incoming/`; they are safe to
delete.)

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
`0.5` per variant word, clamped to `0..1`:

1. **Title similarity.** `normalize_text` on both sides (casefold, accents stripped, `&` ->
   `and`, `ft.` / `featuring` -> `feat`, punctuation dropped); "feat ..." credits and
   bracketed "(with ...)" credits are removed (they are compared as artists, and a shared
   `feat.` credit must not make two songs of one album look alike); on the Spotify side an
   edition tail such as ` - Remastered 2011` / `(Album Version)` is optional, but ` - Live`,
   ` - Remix`, ` - Acoustic` and the like are kept because they name a different recording; on
   the candidate side `youtube.clean_title` drops `(Official Video)` junk and an
   `Artist - ` half is removed when it names the artist. Then the best of the character ratio
   and a token containment / Jaccard blend, with titles that share no word capped at `0.45`
   ("Halo" vs "Hello").
2. **Artist.** The best of any Spotify artist against the candidate's credited artists and
   channel (` - Topic` stripped): `1.0` for an exact name, `0.9` when one contains the other
   ("ArianaGrandeVevo"), else a character ratio. For plain-YouTube results only, an artist
   named in the video title counts `0.8` ("Artist - Title (Lyrics)" on a random channel).
3. **Duration.** `1.0` within 2 s, `0.8` within 5 s, `0.5` within 12 s, `0.2` within 25 s,
   `0.0` beyond; `0.5` when either side has no duration.
4. **Variant words** in the candidate title that the Spotify title lacks: `live`, `remix`,
   `cover`, `karaoke`, `instrumental`, `acoustic`, `sped up`, `slowed`, `nightcore`, `8d`,
   `reverb`, `reaction`, `tutorial`, `extended`, `mashup`, `edit` (word-boundary matches). At
   `0.5` each, even a candidate that is perfect otherwise ends below the threshold.
5. **Acceptance** (`Scored.accepted`): score `>= 0.62`, title similarity `>= 0.5`, artist
   similarity `>= 0.6` (a tribute act with the same title and length is rejected) and a
   duration delta `<= 25 s` when both durations are known. Otherwise `find_match` returns
   `None` and the track fails with the "Couldn't find ..." message rather than downloading
   something else. The top three candidates of every query are logged at debug level, the
   winner at info level ("Matched 'Artist - Title' to <video id> (...)").

Because the matched video id is kept in `TrackRef.extra["youtube_id"]` and in the file's
`YOUTUBE_ID` tag, a bad pick can be reported with the exact candidate that won. The remedy for
the user is to paste the right YouTube link; the app keeps both files (no cross-provider
de-duplication).

### `doctor()`

Never raises and never turns the whole health check red (`up doctor` exits 1 only on YouTube
failures, because that is what stops downloads). It reports one check, labelled `Spotify`,
saying which metadata route is active: without credentials "No API credentials: using
Spotify's public pages (tracks, albums, playlists up to about 100 tracks). Add a client id and
secret in Settings for bigger playlists.", with them "Web API credentials set (playlists of any
size)". The audio-side checks (ffmpeg, JavaScript runtime, yt-dlp) are the YouTube provider's
and are printed under YouTube. The web UI's Spotify chip is fed from this check (label
matching `spotify`); `PUT /api/settings` calls `providers.configure(settings)` so the chip
flips as soon as credentials are saved.

### Known limitations

- **Embed cap**: 100 tracks per playlist without a developer app (albums are complete). The
  cap is logged and named by `doctor`, but the parent job in the queue only says
  "Playlist: <name> (100 tracks)"; the UI does not flag a capped list.
- **No Spotify album name or per-track cover for playlist items** read from embed pages, and
  no album name for a single track: YouTube Music's album and yt-dlp's thumbnail stand in. The
  Web API fills both in.
- **Editorial playlists** cannot be read through the Web API by personal developer apps; the
  embed route gives their first 100 songs. Copy the songs into your own playlist for the rest.
- **No cross-provider de-duplication**: `youtube:<id>` and `spotify:<id>` for the same song are
  two files. Two Spotify tracks that match the same video (single and album version) are two
  files as well.
- **Private playlists** are out of reach (no user login; the client credentials flow has no
  user context). Podcast episodes and local files in playlists are skipped.
- **Regional catalogues**: a track that exists on Spotify may be missing from YouTube Music in
  your country; it fails per track, the rest of the playlist continues.
- **Match quality** depends on YouTube Music's search and the heuristics above; the threshold
  prefers a miss over a wrong song. Known blind spots: a Spotify title `Song - Live` still
  matches a studio `Song` of the same length (the studio candidate carries no penalty word),
  and artist names written in another script (Korean vs. Latin) fail the artist floor.
- **yt-dlp's private plumbing**: `search_music` relies on the extractor's `_extract_response`;
  if a yt-dlp update changes it, the plain-YouTube fallback takes over (weaker ranking, same
  scoring) and a warning is logged.

## Adding a provider

The checklist, with the two built-in providers as examples: YouTube is the self-contained kind
(one library does resolve, download and conversion), Spotify the composite kind (metadata from
one place, audio delegated to another provider, tags rewritten afterwards).

1. **One module under `providers/`** with a class that has `name` (short lowercase slug; it
   becomes the first half of every track id and must never change afterwards) and
   `display_name`. Optional and slow imports stay inside that module (module level is fine
   there; `youtube.py` imports `yt_dlp` at the top and exposes `YoutubeDL` so tests can swap in
   a fake) and nothing outside `providers/` imports them.
2. **`matches(url)`**: hostname and path regexes, offline, no exceptions. Say False to link
   kinds you cannot download (channels, artists, users) so the user gets the "No provider"
   message straight away.
3. **`resolve(url)`**: one `TrackRef` per downloadable track, cheap metadata filled in, silent
   drops for unavailable items, `ProviderError` with a sentence a non-programmer understands
   for everything else. Keep `extra` small and JSON-safe.
4. **`download(...)`**: produce exactly one tagged file in `dest_dir`, working in
   `<library>/.incoming/` and moving into place with `os.replace` under `youtube._move_lock`
   via `destination_for`; emit progress on every status change; check `cancel` and raise
   `DownloadCancelled` after cleaning up; write the `ULTIMATE_PLAYLIST_ID` tag (use
   `youtube.write_tags`); return a `Track` whose `id` equals `ref.track_id`. If the audio comes
   from another provider, delegate to its `download()` and re-tag, like Spotify does.
5. **`doctor()`**: `(ok, label, detail)` triples, never raising, with the fix in `detail`.
   Optional `configure(settings)` if `resolve()` / `doctor()` need live settings.
6. **Settings**, if the provider needs any: add fields to `Settings` (old config files keep
   working, unknown keys are ignored), a value rule in `config.py`, a line in `SettingsPatch`
   in `server/app.py`, the settings dialog and `up config set`. Mask secrets in
   `Settings.public_dict()`.
7. **Register**: add the `(module, class)` pair to `_load_builtin()` in `providers/__init__.py`.
   Order only matters when two providers claim the same URL. A constructor that raises drops
   the provider from the registry with a warning.
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

- Everything is offline. Real YouTube and Spotify are exercised only by a manual integration run
  (`uv run up add <url>`), never by pytest.
- Unit tests cover the pure helpers in `providers/youtube.py`, `Settings` and the value rules
  in `config.py`, `find_ffmpeg` (fake `PATH`), `Library`, `PlaylistStore` and `export_m3u8`.
- `JobManager` flows (single, playlist, skip, cancel, error, retry) run against `FakeProvider`
  (`tests/fake_provider.py`: writes a tiny valid MP3 kept as bytes, tags it with mutagen, emits
  DOWNLOADING then CONVERTING, honours `cancel`, can be told to fail) with real threads and
  `wait_idle()`.
- Spotify: `matches` over every link shape; `spotify_meta` against a fake HTTP session that
  serves the real, trimmed `__NEXT_DATA__` entities in `tests/fixtures/spotify/embed_*.json`
  and canned Web API JSON (two pages to prove pagination, a `null` track, a local file and an
  episode to prove they are dropped, a 401 from the token endpoint, a refused playlist);
  `score_candidate` / `find_match` against canned candidates (live versions, remixes, the
  duration limit, multi-artist credits, accents, the title and artist floors; each real wrong
  pick seen live has a regression test) and `search_music` / `search_youtube` against a fake
  `YoutubeDL`; `download` with the matcher and `YouTubeProvider.download` replaced by fakes
  that write the tiny MP3 and the cover fetch stubbed, asserting the final name is
  `Artist - Title.mp3`, the `TXXX` frames read `spotify:<id>` and the video id, the Spotify
  cover replaced the YouTube one, and that `DownloadCancelled` propagates; one end-to-end run
  through `JobManager`.
- Every API endpoint is tested through FastAPI's `TestClient` (`httpx`), including error paths
  (400 for unsupported links, 404 for unknown ids), the `/media/{id}` range request, settings
  validation and the secret mask.
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
