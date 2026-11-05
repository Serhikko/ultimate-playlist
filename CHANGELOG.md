# Changelog

## 0.4.0 (2026-11-05)

Folders and videos.

- **Folders**: the Library tab lists All tracks, Unsorted and your folders. Make one with the +
  button, drag tracks onto it or use Move to folder (selection bar and every row), rename or
  remove it (its tracks go back to Unsorted; nothing is deleted). Folders are real directories
  in the library folder, so one can be copied to a phone or a USB stick as it is; moving a
  track moves its file and keeps it in its playlists.
- **Save to**: a menu next to the Download button picks the folder new downloads land in,
  remembered between visits; a playlist link fills that folder directly (tracks you already
  have move in from Unsorted; one sorted into another folder stays there and its queue row
  says where). Downloads still on their way follow a renamed or removed folder.
- **Videos**: an Audio / Video toggle next to the link box downloads YouTube and TikTok videos
  as MP4, in Best (the highest resolution in H.264, plays everywhere), Maximum (up to 4K),
  720p or 480p (about that size). A video is its own library item (`youtube:<id>:video`), next to the MP3 of the
  same video. Spotify links stay audio; a TikTok photo slideshow has no video and says so.
- The player plays videos in a panel over the library: click to pause, double-click or F for
  full screen, × hides it while it keeps playing (the video button in the player bar brings it
  back). MP4, M4V, WebM, MKV and MOV files in the library folder are picked up by Rescan.
- Rescan no longer walks into trash and sync-tool folders (`$RECYCLE.BIN` when the library is
  a drive's root, `.stversions`, `.Trashes`, a NAS's `#recycle`...) or folders Windows
  protects (hidden and system), and on macOS and Linux not into folders named with a dot
  followed by a letter (".cache"); old copies an earlier version indexed from a trash folder
  leave the library. A folder you hid in Explorer, and one whose name starts with a dot not
  followed by a letter ("...And Justice for All", ".38 Special"), is music as before.
- Queue rows say what a link becomes ("Video · 720p → Flight"); library rows tag videos (MP4)
  and, in All tracks, their folder.
- `up add --video --quality best|max|720|480 --folder NAME`. API: `POST /api/jobs` takes
  `media`, `quality` and `folder`; new `/api/folders` routes and `POST /api/library/move`;
  tracks carry `media` and `folder`.
- TikTok: a hashtag glued to a word ("МЕНТАЛИСТ#recommendations") is removed from the title too,
  and a display name made only of symbols ("♡") gives way to the @handle.

## 0.3.0 (2026-11-05)

TikTok links.

- Paste a TikTok video or photo slideshow link (`tiktok.com/@user/video/...`, `.../photo/...`,
  `vm.tiktok.com/...` / `vt.tiktok.com/...` / `tiktok.com/t/...` short links, `m.tiktok.com`
  and embed links): the MP3 is the audio of the video itself (not the watermarked copy with
  TikTok's outro), or a slideshow's sound. No account needed. Library id `tiktok:<post id>`;
  the same post pasted through another link form is not downloaded twice.
- Naming: a post set to a named sound is "Sound artist - Sound title" (several artists as
  "A & B" / "A, B & C"); an "original sound" is "Creator - Caption", the caption without
  hashtags and cut to about 80 characters.
- Profiles, sound pages and collections are refused with a message saying what to paste
  instead, also when they come as a short link shared from the app; live streams can't be
  downloaded. Private, signed-in-only, deleted and region-blocked posts get their own messages.
- Browser impersonation: curl_cffi is now a dependency (yt-dlp's `curl-cffi` extra, bundled in
  the Windows zip), so yt-dlp's requests look like a real browser's to TikTok's bot checks.
  `doctor` shows it under TikTok. A post TikTok still refuses with "blocked" (status 10204) is
  refused to your connection's location; the message now says so and points at a VPN for the
  whole computer (a VPN browser extension does not cover the app).
- Titles keep their zero-width joiners (YouTube titles too): emoji sequences such as 🏳️‍🌈 and
  family emoji no longer fall apart, and Persian spelling stays intact. A stray joiner at
  either end of a word is still removed, and the Spotify to YouTube Music matching ignores
  joiners, so a title spelled with or without one finds the same song.
- For contributors: `YouTubeProvider` has three hooks (`ydl_format`, `_tags_for`, `_explain`)
  that a provider for another yt-dlp site overrides; `TikTokProvider` is the example.
  `youtube.explain_error`, `ffmpeg_check` and `ytdlp_check` are shared helpers.

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
