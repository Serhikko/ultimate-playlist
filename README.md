# Ultimate Playlist

Paste YouTube or Spotify links, get tagged MP3s with cover art in one folder, sort them into
playlists, and export the playlists as M3U8 files for a car USB stick, your phone or VLC. A
built-in player lets you listen without leaving the app.

It is a small desktop tool: a local web app (served on `127.0.0.1` only) plus an `up` command
line. Nothing is uploaded anywhere; the music, the index and the settings stay on your computer.

## Quick start (Windows, nothing to install)

1. Download `UltimatePlaylist-<version>-windows-x64.zip` from the
   [Releases page](https://github.com/Serhikko/ultimate-playlist/releases) and unzip it anywhere.
2. Double-click `UltimatePlaylist.exe`. If Windows says "Windows protected your PC", click
   **More info**, then **Run anyway** (the program is not signed). Your browser opens the app.
3. Paste a YouTube or Spotify link into the box at the top and press Enter. Finished songs land
   in `Music\Ultimate Playlist`.

Updating, uninstalling and the zip's own troubleshooting: [No-install version](#no-install-version-windows).
macOS, Linux, or running from source: [Requirements](#requirements) onward.

## What it does

- Accepts YouTube videos, YouTube Music tracks, playlists, Shorts and `youtu.be` links, and
  Spotify tracks, albums, playlists and your Liked Songs (`open.spotify.com` links and
  `spotify:` URIs, see [Spotify](#spotify)). A YouTube `watch?v=...&list=...` link downloads only that one video; a
  `/playlist?list=...` link downloads the whole playlist (private and deleted entries are
  skipped, the rest are reported per track).
- Downloads the best available audio and converts it to MP3 (VBR, best quality) with ffmpeg.
- Writes ID3 tags (artist, title, album, source URL) and embeds the cover art.
- Keeps everything in one flat folder as `Artist - Title.mp3`. The same track is never
  downloaded twice.
- Playlists: create, reorder, export as `.m3u8` with relative paths, so the whole folder can be
  copied to a USB stick or a phone as it is. Built-in player with queue, shuffle and repeat.

## Spotify

Paste a Spotify track, album or playlist link (a `spotify:track:...` URI or a `spotify.link`
short link works too) exactly like a YouTube link. What happens:

1. The song list and the metadata (title, artists, album, cover art, duration) are read from
   Spotify.
2. For every song the app looks for the same recording on YouTube Music (same title and
   artist, about the same length; live versions, covers, remixes and karaoke uploads are
   avoided unless the Spotify title asks for them) and downloads the audio from there, through
   the same pipeline a YouTube link uses. An artist, podcast or profile link is refused with a
   message saying so.
3. The MP3 is named and tagged with Spotify's metadata and gets Spotify's cover art. The
   matched YouTube video is shown in the queue and kept in the file's tags, so a wrong pick is
   easy to spot and to report.

No Spotify audio is downloaded and no DRM is touched: Spotify is only used for metadata, as if
you had typed the artist and the title into YouTube Music yourself.

### Without a Spotify account

Nothing to set up. The song lists come from Spotify's public pages:

- **tracks** and **albums**: complete;
- **playlists**: the first 100 songs (the public page lists no more).

### Connect Spotify: your own playlists of any size and your Liked Songs

Connecting your Spotify account lets the app read the playlists you own or collaborate on,
whatever their size, and your Liked Songs. Connecting works through a small developer app of
your own. The Spotify account that creates the developer app needs **Spotify Premium**
(Spotify's rule for developer apps); accounts added to it under User Management do not. You set
it up once; it is free and takes a few minutes:

1. Open <https://developer.spotify.com/dashboard>, sign in with your Spotify account and click
   **Create app**.
2. Enter any name and description.
3. Add the Redirect URI, exactly: `http://127.0.0.1:8765/api/spotify/callback`
4. Tick **Web API**, accept Spotify's developer terms and click **Save**.
5. Copy the **Client ID** shown on the app's page.
6. In Ultimate Playlist open **Settings** (the gear icon), paste the Client ID, click
   **Connect Spotify** and approve on Spotify's page. You land back in the app, connected.

No client secret is needed and none is stored: the app uses Spotify's sign-in for apps that
cannot keep a secret (Authorization Code with PKCE). It only asks to read your playlists and
your Liked Songs and cannot change anything in your account. The sign-in is saved in
`spotify_auth.json` in the app data folder (see [Where files go](#where-files-go)); it is only
ever sent to Spotify and is never shown or logged. **Disconnect** in Settings (or
`up spotify logout`) deletes it.

Once connected:

- **Liked Songs**: click **Download my Liked Songs** in Settings, or paste
  `https://open.spotify.com/collection/tracks`. Without a connection that link answers
  "Connect Spotify in Settings to download your Liked Songs".
- **Your own playlists**, and the ones you collaborate on, are read completely, with album,
  track number, release year and ISRC for every song. Tracks and albums come from the Web API
  too. Whenever the Web API refuses or fails (for example once the developer app's owner has no
  Premium any more), tracks, albums and playlists are read from the public pages instead, as
  without an account.
- **Other people's playlists stay capped at 100 songs**, and so do Spotify's own ("Today's Top
  Hits" and the other editorial or algorithmic playlists): Spotify only lets apps read the
  contents of playlists you own or collaborate on, so the app reads those from the public page.
  For the whole list, copy the songs into a playlist of your own (in Spotify: select all,
  right-click, *Add to playlist*) and paste that one.

If the app runs on a port other than 8765 (8765 was busy, or you started it with
`up serve --port`), Settings shows the Redirect URI for that port: add it to your developer app
as well (an app can have several). Connecting only works in a browser on the computer that
runs the app, and only while the app listens on `127.0.0.1` (the default; with
`up serve --host <a LAN address>` the Connect button is off). From a terminal, `up spotify status` shows the Client ID, the connected account
and the Redirect URI, and `up spotify login` prints these steps and opens the running app's
sign-in (`.\UltimatePlaylist.exe spotify status` in the no-install version).

Good to know:

- **Wrong song matched?** Paste the YouTube link of the right video instead; the app keeps
  both, so delete the wrong one from the Library tab afterwards (tick "also delete file").
- **"Couldn't find ... on YouTube Music"**: the song is not on YouTube Music in your country,
  or is listed under a different name. The rest of the playlist continues; paste a YouTube link
  for that song.
- **Spotify shows "INVALID_CLIENT: Invalid redirect URI" (or "Invalid client")** and never
  sends you back: the Redirect URI in your developer app must be exactly the one Settings shows
  (`127.0.0.1`, not `localhost`, with the port and `/api/spotify/callback`), and the Client ID
  must be that app's. The app's own "Spotify rejected the login" message appears when Spotify
  refuses the login afterwards.
- **"This Spotify account is not allowed to use that developer app"**: you signed in with an
  account other than the one that created the developer app. Add it under **User Management**
  on the dashboard (a new developer app admits up to five accounts), or connect with the
  owner's account.
- **"Spotify connection expired"**: the sign-in was ended (for example you removed the app's
  access in your Spotify account). Click **Connect Spotify** again.
- Podcast episodes and local files in a playlist are skipped.

## No-install version (Windows)

Nothing to install: not uv, not Python, not ffmpeg, not Node. Extract the whole zip before
starting the program (double-clicking the exe inside the zip preview does not work); the folder
can live anywhere (Desktop, `D:\Apps`, a USB stick).

A console window opens together with the browser tab. If no tab appears, open
<http://127.0.0.1:8765> yourself (the address is also printed in the console window). Close the
console window to stop the app. Double-clicking the exe again while the app runs only reopens
it in the browser.

Everything is inside that folder (ffmpeg and a JavaScript runtime are in `bin/`); delete the
folder to uninstall. Your music goes to your Music folder, in a sub-folder called
`Ultimate Playlist` (`C:\Users\<you>\Music\Ultimate Playlist`, or under `OneDrive\Music` when
OneDrive manages your Music folder), and your settings to `C:\Users\<you>\.ultimate-playlist`.
To update, unzip the new version and delete the old folder. To remove every trace, also delete
`C:\Users\<you>\.ultimate-playlist` (settings, index, log). The MP3s in `Music\Ultimate Playlist`
are ordinary files: keep or delete them as you like.

If something goes wrong:

- **Every download suddenly fails**: YouTube changed something and the bundled yt-dlp needs an
  update. Download the newest zip from the [Releases page](https://github.com/Serhikko/ultimate-playlist/releases)
  and replace the folder.
- **To see what is wrong**, open a terminal in the folder (right-click an empty spot inside the
  folder and choose "Open in Terminal"; on Windows 10: Shift + right-click > "Open PowerShell
  window here") and run `.\UltimatePlaylist.exe doctor`.
- **`doctor` says ffmpeg or the JavaScript runtime is missing**: the `bin` folder is
  incomplete (a partial extraction, or your antivirus quarantined a file). Extract the zip again.
- **`UltimatePlaylist.exe` disappears, or Windows Security reports a threat**: a false positive
  that is common for unsigned programs built with PyInstaller. Open Windows Security >
  Protection history, choose Restore (or Allow), then run it again.

The sections below are for running from source.

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
git clone https://github.com/Serhikko/ultimate-playlist.git
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
uv run up add https://open.spotify.com/album/<id>            # Spotify links work the same way
uv run up add <url> <url> <url>                              # several links (or a playlist link)
uv run up add -q <url>                                       # quiet: only the final summary (still waits for the downloads)
uv run up doctor                                             # check ffmpeg, JS runtime, yt-dlp, folders; show which Spotify route is used
uv run up list                                               # tracks in the library (id, artist, title, duration, path)
uv run up list -q daft                                       # search title / artist / album
uv run up playlists                                          # list playlists
uv run up export "My playlist"                               # write <library>/Playlists/My playlist.m3u8
uv run up export "My playlist" D:\Music\mine.m3u8            # ...or to a path of your choice (name or playlist id)
uv run up rescan                                             # index files you copied into the library folder by hand
uv run up config show                                        # the current settings
uv run up config set concurrency 3                           # change one setting (`up config set --help` lists the keys)
uv run up spotify status                                     # Client ID, connected account, Redirect URI
uv run up spotify login                                      # how to connect Spotify; opens the running app's sign-in
uv run up spotify logout                                     # disconnect Spotify (deletes the saved sign-in)
uv run up --library D:\Music\UP list                         # use another library folder for this one command
uv run up --version
```

`up config set` and the `up spotify` commands look for the running app on port 8765 and the
next ten; for an app started with `up serve --port 9000`, add `--port 9000` (`spotify status`
and `spotify login` then show that port's Redirect URI and open that app's sign-in).
`config set` and `spotify logout` let the running app make the change, so it takes effect at
once and the app does not overwrite it with its own copy of the settings. With no app running,
`config set` writes `config.json` directly.

`uv run up --help` and `uv run up <command> --help` list every flag. `uv run ultimate-playlist`
and `uv run python -m ultimate_playlist` are the same as `uv run up`.

## Where files go

| What | Where |
| --- | --- |
| Music library (the MP3s) | `~/Music/Ultimate Playlist` (Windows: the Music folder Explorer shows, so `C:\Users\<you>\Music\Ultimate Playlist` or `C:\Users\<you>\OneDrive\Music\Ultimate Playlist` when OneDrive manages it) |
| Exported playlists | `<library>/Playlists/<name>.m3u8` |
| Temporary download files | `<library>/.incoming/` (cleaned up automatically; safe to empty while the app is closed) |
| Config, index, playlists, log | `~/.ultimate-playlist/` : `config.json`, `library.json`, `playlists.json`, `app.log` |
| Spotify sign-in (only while connected) | `~/.ultimate-playlist/spotify_auth.json` (plus an empty `spotify_auth.lock`) |

Set the environment variable `ULTIMATE_PLAYLIST_HOME` to move the config/index folder somewhere
else. The library folder is a setting (`library_dir` in `config.json`, the Settings dialog, or
`PUT /api/settings`); changing it re-points the library and rescans the new folder.

`config.json` is created with defaults the first time settings are saved. Keys: `library_dir`,
`audio_format` (`mp3`; the only format exercised end to end), `audio_quality` (`"0"` = best
VBR), `ffmpeg_path` (only if ffmpeg is not on `PATH`; a path that no longer exists is ignored
with a warning and `doctor` says so), `concurrency` (parallel downloads, 1..6; a change through
the Settings dialog resizes the running download pool at once), `embed_cover`, `js_runtimes`
(`["deno", "node"]`; `bun` and `quickjs` are accepted too) and `spotify_client_id` (`""`
until you create a Spotify developer app, see [Spotify](#spotify); only the Client ID is
stored, never a secret).
A missing or broken file means defaults; the app never refuses to start because of it. Edit it
while the app is closed, or use `up config set`. The library folder cannot be moved while a
download is running (the API answers 409).

The web server only answers requests addressed to `127.0.0.1` / `localhost` and refuses
non-GET requests that carry a foreign `Origin`, so a web page you happen to visit cannot drive
the API (DNS rebinding, cross-site forms). `up serve --host 0.0.0.0` deliberately lifts that
restriction for LAN use.

## How naming and tagging work

- Files are named `Artist - Title.mp3`, flat in the library folder (no per-artist sub-folders).
  For YouTube links, artist and title come from YouTube's music metadata when the video has it
  (YouTube Music tracks, "Topic" uploads); otherwise junk such as `(Official Video)`,
  `[Official Audio]`, `(Lyrics)`, `(HD)`, a trailing `- Official Video` / `| Lyrics` and a
  `| Some Label Records` style tail is removed from the video title first, and what is left is
  split on `Artist - Title` (or `Artist | Title`); otherwise the channel name (minus ` - Topic`)
  is the artist and the cleaned video title is the title. A `| ...` tail that could be part of
  the title (`Song | Live at Wembley`) is kept.
- For Spotify links, artist (all artists, joined with `, `), title, album and cover come from
  Spotify, whatever the YouTube video was called. Spotify's public pages carry no album name
  for a single track or for the songs of a playlist; there the album of the YouTube Music
  recording is used (with Spotify connected, the album of your own playlists' songs comes from
  Spotify too).
- Characters Windows does not allow in file names are dropped, names are capped at 150
  characters, and a clash with a different track gets ` (2)`, ` (3)`, ... appended.
- Tags are ID3v2: artist, title, album (when known), comment = the source URL (the YouTube
  video or the Spotify track page), the cover (the video thumbnail, or Spotify's album art),
  and a hidden `ULTIMATE_PLAYLIST_ID` tag (`youtube:<video id>` or `spotify:<track id>`) so a
  rescan can recognise a file even after you rename or move it inside the folder. Spotify
  tracks also carry a hidden `YOUTUBE_ID` tag with the video the audio came from, plus album
  artist, track number, release year and ISRC when Spotify provided them. With
  `audio_format` set to `m4a`, `opus` or `flac` the same fields are written in that container's
  own tag format (MP4 atoms / Vorbis comments); only MP3 is exercised end to end.
- Files you copy into the folder from elsewhere are picked up by `rescan` (MP3, M4A, Opus, FLAC),
  with their existing tags, under a `local:` id.

## Troubleshooting (running from source)

Using the no-install zip? The remedies for that are in the [No-install version](#no-install-version-windows)
section above; the commands below assume a source checkout.

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
  <https://github.com/yt-dlp/yt-dlp/releases>. Spotify links are affected too: their audio comes
  from YouTube Music.
- **"Private video", "unavailable", "age-restricted", "not available in your country"**: the
  message says what YouTube said. The app does not sign in to YouTube, so private and
  age-restricted videos cannot be downloaded.
- **Spotify: "Couldn't find ... on YouTube Music"**, a wrong song, a playlist that stops at 100
  songs, or trouble connecting your Spotify account: see [Spotify](#spotify).
- **"Could not reach Spotify"**: no internet, or Spotify is down; the YouTube side is not
  involved yet at that point. Try again in a minute.
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
  shows the startup lines, one line per Spotify track naming the YouTube video it was matched
  to, plus warnings and errors; the CLI commands only print warnings and errors. yt-dlp's own
  messages go to the log file only; successful web requests are not logged at all (failed ones
  are). Please attach the relevant part when reporting a problem (the Spotify sign-in is never
  written to it).

## Legal

Downloading audio from YouTube may violate YouTube's Terms of Service and, depending on the
content and the country you are in, copyright law. Spotify is used for metadata only (its public
embed pages, or the Web API through your own developer app under Spotify's developer terms);
no Spotify audio is downloaded and no copy protection is circumvented. This tool is intended for
personal use with content you have the right to download. You are responsible for how you use
it. The app uploads nothing and shares nothing; everything stays on your machine.

## Status

- **0.2**: YouTube (videos, YouTube Music tracks, playlists, Shorts) and Spotify (tracks, albums,
  playlists, Liked Songs with a connected account; audio from YouTube Music). Local web UI with
  a settings dialog, the `up` command line, playlists, M3U8 export, built-in player, Windows
  no-install zip.
- Not yet: a song downloaded once from a YouTube link and once from a Spotify link is two files
  (no cross-source de-duplication); whole YouTube channels; signing in to YouTube (private and
  age-restricted videos); other people's Spotify playlists beyond their first 100 songs (a
  Spotify rule); formats other than MP3 (the setting exists but only MP3 is tested); per-artist
  sub-folders.

## Development

```text
uv sync --all-groups      # runtime + dev dependencies (pytest, httpx, ruff)
uv run pytest -q          # offline test suite (no network, no real YouTube or Spotify)
uv run ruff check .       # lint
uv run ruff format .      # format
```

CI (`.github/workflows/ci.yml`) runs the same lint and tests on `ubuntu-latest` and
`windows-latest` with Python 3.11 (the oldest supported) and 3.14; the suite needs neither ffmpeg
nor a JavaScript runtime (tool detection is stubbed, see `tests/conftest.py`). How the pieces
fit together, how the Spotify provider is built on top of the YouTube one, and how to add
another source: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Release notes:
[CHANGELOG.md](https://github.com/Serhikko/ultimate-playlist/blob/main/CHANGELOG.md).

The Windows no-install zip is built with `uv run python scripts/build_windows.py` (PyInstaller
plus bundled ffmpeg and Deno/Node) and published by `.github/workflows/release.yml` when a `v*`
tag is pushed; see [docs/PACKAGING.md](docs/PACKAGING.md).

## License

MIT, see [LICENSE](LICENSE).
