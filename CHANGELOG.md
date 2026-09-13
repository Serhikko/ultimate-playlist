# Changelog

## 0.1.1 (2026-09-13)

Maintenance release. The app itself is unchanged; the Windows zip is rebuilt with the same
yt-dlp (2026.08.19 is still the newest release).

- CI and release workflows moved to current GitHub Actions (checkout v7, setup-uv v10,
  upload-artifact v7); the Node 20 deprecation warnings are gone.
- `docs/SPOTIFY.md`: feasibility audit of downloading directly from Spotify, and why the
  metadata-from-Spotify, audio-from-YouTube-Music route is the one to build.

## 0.1.0 (2026-09-11)

First release.

- YouTube provider: videos, YouTube Music tracks, playlists, Shorts and `youtu.be` links become
  tagged MP3s with cover art, named `Artist - Title.mp3`, never downloaded twice.
- Download queue with progress, cancel and retry; library with search; playlists with reorder
  and M3U8 export; built-in player.
- Web UI on `127.0.0.1:8765` plus the `up` command line (`add`, `list`, `doctor`, `export`, ...).
- Windows no-install zip: `UltimatePlaylist.exe` with ffmpeg and a JavaScript runtime bundled.
- Spotify provider stub and a contributor guide (`docs/ARCHITECTURE.md`).
