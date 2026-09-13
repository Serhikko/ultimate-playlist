# Spotify: can we download directly? (feasibility audit, September 2026)

**Short answer: no, not in a way that is stable, safe for the account, or something this project
should ship.** The practical route is the one `docs/ARCHITECTURE.md` already describes: read the
track list and metadata from Spotify, fetch the audio from YouTube Music, tag it with Spotify's
data. That is a weekend of work on top of the existing provider layer.

## What "direct" would mean

Spotify streams audio as encrypted OGG Vorbis (and AAC) files from its CDN. The decryption key
for each track is handed out per request by Spotify's servers, and only to clients Spotify
recognises. Every "download from Spotify" tool falls into one of three buckets.

### 1. Pretend to be a Spotify client (librespot family: zotify, spotify-dl, ...)

`librespot` is an open-source implementation of Spotify's client protocol. Tools built on it log
in with a Premium account, ask Spotify's audio-key service for the per-track key, download the
file and decrypt it. This is what people mean by "direct download".

State in 2026:

- Key delivery keeps breaking. Premium accounts get `audio key error 0x0001` even though login
  works ([librespot #1649](https://github.com/librespot-org/librespot/issues/1649),
  [spotify-player #910](https://github.com/aome510/spotify-player/issues/910),
  [music-assistant #4624](https://github.com/music-assistant/support/issues/4624)).
- Login broke on 10 August 2026: the access point accepts the client, then
  `login5.spotify.com` answers `INVALID_CREDENTIALS`
  ([mopidy-spotify #437](https://github.com/mopidy/mopidy-spotify/issues/437)).
- Spotify bans accounts it catches. Zotify users report e-mails from Spotify about
  "unauthorized copies" followed by a ban
  ([zotify #316](https://github.com/zotify-dev/zotify/issues/316)).
- Spotify's own clients moved key handling behind a DRM scheme ("PlayPlay") whose secrets live
  inside the official client. Unofficial clients now depend on reverse-engineered secrets that
  Spotify rotates, so every fix lives weeks to months.

Verdict: one to two weeks to build, then a permanent maintenance treadmill, with the user's
Premium account as collateral. Also a clear Terms of Service violation, and the key-extraction
part is circumvention of a technical protection measure in most jurisdictions.

### 2. Rip the web player (Widevine)

The browser player uses Google's Widevine DRM. Getting the audio out means extracting content
keys from the browser's CDM. That is textbook DRM circumvention (DMCA §1201 in the US, article 6
of the EU InfoSoc directive), independent of any Terms of Service, and the public tools for it
get shut down. Not something to build, and not something this project will help with.

### 3. Record what plays (the "analog hole")

Capture the Windows audio output (WASAPI loopback) while Spotify plays, split the recording into
tracks with the Web API's "currently playing" endpoint, tag with Spotify metadata. This is what
most commercial "Spotify converters" do.

- Not circumvention: no keys are touched, it records the decoded audio like a tape deck.
- Real time only: a 60-track playlist takes about four hours; the PC must stay unmuted and idle.
- Quality is fine (Spotify's 320 kbps stream, decoded, re-encoded to MP3) but track boundaries
  are fragile: crossfade must be off, Free-tier ads land in the recording, a notification sound
  ends up in the song.
- Effort: three to five days for capture, boundary detection, tagging and UI.

## Effort and risk at a glance

| Route | Build effort | Longevity | Account risk | Legal position |
|---|---|---|---|---|
| Spotify metadata + YouTube Music audio (spotDL style) | 1 to 2 days on the existing `Provider` layer | Good: spotDL is maintained (4.5.2, July 2026) and uses the same yt-dlp + Deno stack we ship | None: read-only Web API access | Same grey zone as the YouTube side today |
| Loopback recording | 3 to 5 days | Good: nothing to reverse-engineer | Low | Grey; not circumvention |
| librespot-style client | 1 to 2 weeks, then ongoing | Poor: broke in August 2026, breaks every few months | High: bans documented | ToS violation; circumvention exposure |
| Widevine key extraction | Not applicable | Not applicable | Not applicable | Illegal circumvention; out of scope |

## Recommendation

1. Build the spotDL-style provider now (the plan in `docs/ARCHITECTURE.md`). It needs a free
   Spotify developer app (client id and secret) for the metadata calls. spotDL itself can be a
   dependency or a reference implementation; either way `Track.id` stays `spotify:<track id>` so
   the library and playlists treat Spotify tracks like any other.
2. Show a clear "not found on YouTube Music" state per track in the queue so the truly
   Spotify-only songs are visible instead of silently mismatched.
3. If those leftovers matter, consider loopback recording as a later, opt-in "capture" mode, or
   just buy them (Bandcamp, Amazon, iTunes). In practice most "Spotify-only" tracks are on
   YouTube Music as label auto-uploads under the artist's "Topic" channel.

Sources: [librespot #1649](https://github.com/librespot-org/librespot/issues/1649),
[librespot #1236](https://github.com/librespot-org/librespot/issues/1236),
[mopidy-spotify #437](https://github.com/mopidy/mopidy-spotify/issues/437),
[zotify #316](https://github.com/zotify-dev/zotify/issues/316),
[spotDL on PyPI](https://pypi.org/project/spotdl/), [spotDL docs](https://spotdl.readthedocs.io/),
[VideoHelp: downloading audio from Spotify](https://forum.videohelp.com/threads/410215-Downloading-audio-from-Spotify).
