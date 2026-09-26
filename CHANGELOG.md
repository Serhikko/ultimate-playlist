# Changelog

## 0.2.1 (2026-09-26)

- The Windows exe no longer sits on a blank window for over twenty seconds at every start. The
  check for an already running copy asked eleven ports one after the other, and Windows takes
  about two seconds to refuse each closed one; the ports are now checked at once (about half a
  second). "Starting Ultimate Playlist..." is printed before the web server code is loaded, with
  a note that the first start after unpacking can take a minute while Windows checks the files.

## 0.2.0 (2026-09-15)

Spotify links.

- Paste a Spotify track, album or playlist link (or a `spotify:` URI): the song list and the
  metadata (title, artists, album, cover art, duration) come from Spotify, the audio is found on
  YouTube Music and downloaded through the existing YouTube pipeline, and the MP3 is named and
  tagged with Spotify's data and Spotify's cover art. No Spotify audio is touched. Library id
  `spotify:<track id>`; the matched YouTube video is shown in the queue and kept in the tags
  (`TXXX YOUTUBE_ID`).
- Works without an account: Spotify's public pages give tracks, complete albums and the first
  100 songs of a playlist.
- **Connect Spotify** (Settings): with a free Spotify developer app of your own (only its Client
  ID; no client secret is needed or stored; the account that creates the developer app needs
  Spotify Premium, which is Spotify's rule for developer apps, while accounts added to it under
  User Management do not) and a
  one-time approval in the browser (Authorization Code with PKCE), your own and collaborative
  playlists of any size and your **Liked Songs** (a button in Settings, or paste
  `https://open.spotify.com/collection/tracks`) are read through the Spotify Web API. Other
  people's playlists stay at their first 100 songs: since March 2026 Spotify only shares the
  contents of playlists you own or collaborate on. The sign-in is kept in `spotify_auth.json`
  and is never shown or logged. New routes `GET /api/spotify`, `GET /api/spotify/login`,
  `GET /api/spotify/callback` and `POST /api/spotify/logout`; new commands
  `up spotify status|login|logout`. Whenever a Web API request fails (for example once the
  developer app's owner has no Premium any more), tracks, albums and playlists are read from the
  public pages instead.
- Settings dialog (gear icon) in the web UI, backed by `PUT /api/settings`: library folder,
  parallel downloads, and the Spotify section (Client ID, the Redirect URI with a Copy button,
  Connect / Disconnect).
- `up config show` and `up config set <key> <value>` from the command line. `config set` hands
  the change to the running app when there is one (`--port` for an app on a port of your
  choice), so it takes effect at once.
- The queue names what a link expanded into: "Album: Random Access Memories (13 tracks)" or
  "Playlist: <name> (N tracks)" instead of "Playlist: N tracks".
- Matching safeguards: a live, acoustic, unplugged, remix, demo or piano track is no longer
  matched to the studio recording, nor the other way round, while edition labels ("Remastered 2011",
  "Radio Edit", "Album Version") still match; an artist whose name is only part of another
  ("Nas" / "Lil Nas X") is refused; titles with extra words ("Stay" / "Stay With Me") need a
  near-identical spelling and "Pt. 2" / "Song 2" never pass for the plain title; with no
  Spotify duration, uploads longer than 15 minutes are refused. A variant word that is part of
  the song name ("Live Forever") does not let a live recording through; language, alternate
  and re-recorded versions ("Spanish Version", "Taylor's Version") are not taken for the
  original; a "(Clean)" catalogue entry counts as the same song.
- `requests` is a new runtime dependency (Spotify metadata).
- Docs: README rewritten for the public repository (quick start, Spotify section);
  `docs/ARCHITECTURE.md` describes the implemented Spotify design and the generic "add a
  provider" guide; the internal notes `docs/SPEC.md` and `docs/SPOTIFY.md` are gone.

## 0.1.1 (2026-09-13)

Maintenance release. The app itself is unchanged; the Windows zip is rebuilt with the same
yt-dlp (2026.08.19 is still the newest release).

- CI and release workflows moved to current GitHub Actions (checkout v7, setup-uv v10,
  upload-artifact v7); the Node 20 deprecation warnings are gone.
- `docs/SPOTIFY.md`: feasibility audit of downloading directly from Spotify, and why the
  metadata-from-Spotify, audio-from-YouTube-Music route is the one to build (removed in 0.2.0;
  the conclusion lives in `docs/ARCHITECTURE.md`).

## 0.1.0 (2026-09-11)

First release.

- YouTube provider: videos, YouTube Music tracks, playlists, Shorts and `youtu.be` links become
  tagged MP3s with cover art, named `Artist - Title.mp3`, never downloaded twice.
- Download queue with progress, cancel and retry; library with search; playlists with reorder
  and M3U8 export; built-in player.
- Web UI on `127.0.0.1:8765` plus the `up` command line (`add`, `list`, `doctor`, `export`, ...).
- Windows no-install zip: `UltimatePlaylist.exe` with ffmpeg and a JavaScript runtime bundled.
- Spotify provider stub and a contributor guide (`docs/ARCHITECTURE.md`).
