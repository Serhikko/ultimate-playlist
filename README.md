# Ultimate Playlist

Paste a YouTube link, get a tagged MP3 with cover art in one local music folder. Organise the
tracks into playlists, play them in the built-in player, export playlists as M3U8 for VLC or
your phone.

It is a personal desktop tool: a small local web app (served on `127.0.0.1` only) plus a CLI.
YouTube is the only source in v0.1. Spotify is planned and will plug into the same provider
interface (see [Status and roadmap](#status-and-roadmap)).

What it does:

- Accepts YouTube videos, YouTube Music tracks, playlists, Shorts and `youtu.be` links. A
  `watch?v=...&list=...` link downloads only that one video; a `/playlist?list=...` link downloads
  the whole playlist (private and deleted entries are skipped, the rest are reported per track).
- Downloads the best available audio and converts it to MP3 (VBR, best quality) with ffmpeg.
- Writes ID3 tags (artist, title, album, source URL) and embeds the cover art.
- Keeps everything in one flat folder as `Artist - Title.mp3`. The same video is never downloaded
  twice.
- Playlists: create, reorder, export as `.m3u8`. Built-in player with queue, shuffle and repeat.

## Requirements

Developed and tested on Windows 11; macOS and Linux should work with the equivalents below.

1. **uv** (manages Python and dependencies; you do not need to install Python yourself).
   Windows: `winget install astral-sh.uv`. macOS: `brew install uv`. Linux:
   `curl -LsSf https://astral.sh/uv/install.sh | sh`. Docs: <https://docs.astral.sh/uv/>.
2. **ffmpeg** (converts to MP3 and embeds the cover).
   Windows: `winget install Gyan.FFmpeg`. macOS: `brew install ffmpeg`.
   Linux: `sudo apt install ffmpeg` (or your distro's equivalent).
3. **A JavaScript runtime** for yt-dlp. YouTube requires one to work out the audio URLs. Either:
   - Deno: Windows `winget install DenoLand.Deno`, macOS `brew install deno`, Linux
     `curl -fsSL https://deno.land/install.sh | sh`, or
   - Node.js: Windows `winget install OpenJS.NodeJS.LTS`, macOS `brew install node`, Linux: your
     distro's `nodejs` package or <https://nodejs.org>.

   yt-dlp on its own only picks up Deno automatically; this app enables both Deno and Node, so
   whichever one you already have is fine.

After installing anything with winget, open a **new** terminal so the updated `PATH` is picked up.

## Install

```text
git clone <repo-url> ultimate-playlist
cd ultimate-playlist
uv sync
```

`uv sync` creates a private `.venv` in the project folder with Python and every dependency.
Nothing is installed globally.

## Run

```text
uv run up
```

This starts the local server and opens <http://127.0.0.1:8765> in your browser. Paste a link (or
several, one per line) into the box at the top and press Enter. The queue on the left shows
progress; finished tracks appear in the Library tab. `Ctrl+C` in the terminal stops the app.
If port 8765 is busy the next free port is used and opened instead.

Options: `uv run up serve --port 9000 --no-browser --host 127.0.0.1`.

### Command line

The `up` command works without the web UI:

```text
uv run up add https://www.youtube.com/watch?v=dQw4w9WgXcQ    # download, print progress, exit 0 if all done/skipped
uv run up add <url> <url> <url>                              # several links (or a playlist link)
uv run up add -q <url>                                       # quiet: only the final summary (still waits for the downloads)
uv run up doctor                                             # check ffmpeg, JS runtime, yt-dlp, folders
uv run up list                                               # tracks in the library (id, artist, title, duration, path)
uv run up list -q daft                                       # search title / artist / album
uv run up playlists                                          # list playlists
uv run up export "My playlist"                               # write <library>/Playlists/My playlist.m3u8
uv run up export "My playlist" D:\Music\mine.m3u8            # ...or to a path of your choice (name or playlist id)
uv run up rescan                                             # index files you copied into the library folder by hand
uv run up --library D:\Music\UP list                         # use another library folder for this one command
uv run up --version
```

`uv run up --help` and `uv run up <command> --help` list every flag. `uv run ultimate-playlist`
and `uv run python -m ultimate_playlist` are the same as `uv run up`.

## Where files go

| What | Where |
| --- | --- |
| Music library (the MP3s) | `~/Music/Ultimate Playlist` (Windows: `C:\Users\<you>\Music\Ultimate Playlist`) |
| Exported playlists | `<library>/Playlists/<name>.m3u8` |
| Temporary download files | `<library>/.incoming/` (cleaned up automatically; safe to empty while the app is closed) |
| Config, index, playlists, log | `~/.ultimate-playlist/` : `config.json`, `library.json`, `playlists.json`, `app.log` |

Set the environment variable `ULTIMATE_PLAYLIST_HOME` to move the config/index folder somewhere
else. The library folder is a setting (`library_dir` in `config.json`, or `PUT /api/settings`);
changing it re-points the library and rescans the new folder.

`config.json` is created with defaults the first time settings are saved. Keys: `library_dir`,
`audio_format` (`mp3`; the only format exercised in v0.1), `audio_quality` (`"0"` = best VBR),
`ffmpeg_path` (only if ffmpeg is not on `PATH`), `concurrency` (parallel downloads, 1..6; a
change through `PUT /api/settings` resizes the running download pool at once), `embed_cover`,
`js_runtimes` (`["deno", "node"]`; `bun` and `quickjs` are accepted too). A missing or broken
file means defaults; the app never refuses to start because of it. Edit it while the app is
closed. The library folder cannot be moved while a download is running (the API answers 409).

The web server only answers requests addressed to `127.0.0.1` / `localhost` and refuses
non-GET requests that carry a foreign `Origin`, so a web page you happen to visit cannot drive
the API (DNS rebinding, cross-site forms). `up serve --host 0.0.0.0` deliberately lifts that
restriction for LAN use.

## How naming and tagging work

- Files are named `Artist - Title.mp3`, flat in the library folder (no per-artist sub-folders in
  v0.1). Artist and title come from YouTube's music metadata when the video has it (YouTube Music
  tracks, "Topic" uploads); otherwise junk such as `(Official Video)`, `[Official Audio]`,
  `(Lyrics)`, `(HD)`, a trailing `- Official Video` / `| Lyrics` and a `| Some Label Records`
  style tail is removed from the video title first, and what is left is split on
  `Artist - Title` (or `Artist | Title`); otherwise the channel name (minus ` - Topic`) is the
  artist and the cleaned video title is the title. A `| ...` tail that could be part of the
  title (`Song | Live at Wembley`) is kept.
- Characters Windows does not allow in file names are dropped, names are capped at 150
  characters, and a clash with a different track gets ` (2)`, ` (3)`, ... appended.
- Tags are ID3v2: artist, title, album (when YouTube knows it), comment = the source URL, the
  cover (from the video thumbnail), and a hidden `ULTIMATE_PLAYLIST_ID` tag (`youtube:<video id>`)
  so a rescan can recognise a file even after you rename or move it inside the folder. With
  `audio_format` set to `m4a`, `opus` or `flac` the same fields are written in that container's
  own tag format (MP4 atoms / Vorbis comments); only MP3 is exercised end to end in v0.1.
- Files you copy into the folder from elsewhere are picked up by `rescan` (MP3, M4A, Opus, FLAC),
  with their existing tags, under a `local:` id.

## Troubleshooting

- **First stop: `uv run up doctor`.** It runs the same checks as the status chips at the top of
  the web UI and prints how to fix whatever is missing.
- **"No supported JavaScript runtime"** or every download fails right after "Resolving": install
  Deno or Node.js (see Requirements), open a new terminal, start the app again.
- **"ffmpeg not found"**: install it (`winget install Gyan.FFmpeg`), open a new terminal. If it
  lives somewhere unusual, put the full path in `ffmpeg_path` in `config.json`.
- **Everything worked last week and now every YouTube download fails**: YouTube changed
  something and yt-dlp needs an update. Run

  ```text
  uv lock --upgrade-package yt-dlp && uv sync
  ```

  and try again. If it still fails, yt-dlp probably has not caught up yet; check
  <https://github.com/yt-dlp/yt-dlp/releases>.
- **"Private video", "unavailable", "age-restricted", "not available in your country"**: the
  message says what YouTube said. The app does not sign in to YouTube, so private and
  age-restricted videos cannot be downloaded in v0.1.
- **Long paths on Windows**: Windows refuses paths longer than 260 characters unless long paths
  are enabled. The app keeps temporary files short (`<library>/.incoming/<video id>.mp3`) and caps
  file names at 150 characters, but a deeply nested library folder can still hit the limit. Use a
  short library path (for example `D:\Music\UP`), or enable long paths: Group Policy
  `Computer Configuration > Administrative Templates > System > Filesystem > Enable Win32 long
  paths`, or registry `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1`,
  then reboot.
- **"Already in library" (Skipped)**: the link points to a track you already have. Delete it from
  the Library tab (tick "also delete file") if you want it downloaded again.
- **Port already in use**: the app tries the next ten ports automatically; or pass
  `uv run up serve --port 9000`.
- **"Rescan" says the library folder is not reachable**: the folder was renamed or lives on a
  drive that is unplugged. Nothing is changed in that case (the index and your playlists are
  kept); plug the drive in, or point `library_dir` at the new place, and rescan again.
- **"HTTP Error 403" / "YouTube refused to serve the audio"**: not a network problem; YouTube
  changed something. Update yt-dlp as described above.
- **Logs**: `~/.ultimate-playlist/app.log` (rotated, 1 MB x 3). With `up serve` the terminal
  shows the startup lines plus warnings and errors; the CLI commands only print warnings and
  errors. yt-dlp's own messages and routine request lines go to the log file only. Please attach
  the relevant part when reporting a problem.

## Legal

Downloading audio from YouTube may violate YouTube's Terms of Service and, depending on the
content and the country you are in, copyright law. This tool is intended for personal use with
content you have the right to download. You are responsible for how you use it. The app uploads
nothing and shares nothing; everything stays on your machine.

## Status and roadmap

- **v0.1 (now)**: YouTube videos, YouTube Music tracks, playlists, Shorts. Local web UI, CLI,
  playlists, M3U8 export, built-in player.
- **Next: Spotify.** Track/album/playlist metadata from the Spotify Web API, audio matched on
  YouTube Music and downloaded through the existing YouTube pipeline. Spotify's own streams are
  DRM-protected and will not be touched. Pasting a Spotify link today gives a friendly "not built
  yet" message. Design and step-by-step guide: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- Not in v0.1: whole channels, YouTube login/cookies, formats other than MP3 (the setting exists
  but only MP3 is tested), per-artist sub-folders.

## Development

```text
uv sync --all-groups      # runtime + dev dependencies (pytest, httpx, ruff)
uv run pytest -q          # offline test suite (no network, no real YouTube)
uv run ruff check .       # lint
uv run ruff format .      # format
```

CI (`.github/workflows/ci.yml`) runs the same lint and tests on `ubuntu-latest` and
`windows-latest` with Python 3.12 and 3.14; the suite needs neither ffmpeg nor a JavaScript
runtime (tool detection is stubbed, see `tests/conftest.py`). The contract every module is
written against is
[docs/SPEC.md](docs/SPEC.md); how the pieces fit together, and how to add a provider, is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

MIT, see [LICENSE](LICENSE).
