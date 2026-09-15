# Changelog

## 0.2.0 (2026-09-13)

Spotify links.

- Paste a Spotify track, album or playlist link (or a `spotify:` URI): the song list and the
  metadata (title, artists, album, cover art, duration) come from Spotify, the audio is found on
  YouTube Music and downloaded through the existing YouTube pipeline, and the MP3 is named and
  tagged with Spotify's data and Spotify's cover art. No Spotify audio is touched. Library id
  `spotify:<track id>`; the matched YouTube video is shown in the queue and kept in the tags
  (`TXXX YOUTUBE_ID`).
- Works without an account (Spotify's public embed pages; a playlist is read up to 100 songs).
  With your own Spotify developer app (`spotify_client_id` / `spotify_client_secret` in the
  settings) playlists of any size are read through the Spotify Web API.
- Settings dialog (gear icon) in the web UI, backed by `PUT /api/settings`. The Spotify secret
  is masked wherever settings are shown (`GET /api/settings`, `up config show`).
- `up config show` and `up config set <key> <value>` edit `config.json` from the command line.
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
