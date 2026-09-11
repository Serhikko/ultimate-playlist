"""Spotify provider stub: owns Spotify links but cannot download yet.

How Spotify support is meant to work (v0.2 idea, not built):

Spotify does not let anyone download audio, so the plan is the spotDL approach:

1. Metadata from Spotify. Resolve the pasted link (track / album / playlist / ``spotify:`` URI)
   through the Spotify Web API (``spotipy`` with a client-credentials app, or spotDL's own
   ``SpotifyClient``) into one ``TrackRef`` per song: ``source_id`` = Spotify track id,
   ``title`` / ``artist`` / ``album`` / ``duration`` / ``thumbnail_url`` (album cover) filled in
   from the API. This is what ``resolve()`` should return.
2. Audio from YouTube Music. For each ``TrackRef``, search YouTube Music for
   ``"<artist> - <title>"`` (spotDL's ``YouTubeMusic`` audio provider does exactly this, with
   duration matching to avoid live versions and covers), then hand the chosen YouTube URL to
   yt-dlp with the same options ``providers.youtube._ydl_opts`` builds.
3. Tag the MP3 with the Spotify metadata and the Spotify cover art (better than YouTube
   thumbnails), write ``TXXX ULTIMATE_PLAYLIST_ID = spotify:<track id>`` so rescans can recover
   the identity, and return a ``Track`` with ``provider="spotify"``.

Exact steps to wire it in (see docs/ARCHITECTURE.md as well):

* Add the dependency (``uv add spotdl`` or ``uv add spotipy``) and read the client id/secret
  from ``Settings`` (add ``spotify_client_id`` / ``spotify_client_secret`` fields; they are
  stored in ``config.json`` under the app data directory).
* Implement ``resolve()`` and ``download()`` in this class following the ``Provider`` protocol
  in ``providers/base.py``: raise ``ProviderError`` with a friendly message on failure, honour
  the ``cancel`` event (raise ``DownloadCancelled``), emit ``ProgressEvent`` on status changes,
  and return ``Track.path`` relative to ``dest_dir`` with forward slashes.
* Make ``doctor()`` report ``(True, "Spotify", "...")`` once credentials are configured.
* The registry (``providers/__init__.py``) already lists ``SpotifyProvider`` after
  ``YouTubeProvider``; ``matches()`` below already claims Spotify links, so nothing else in the
  app (queue, library, UI) needs to change.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from urllib.parse import urlsplit

from ..config import Settings
from ..models import Track, TrackRef
from .base import ProgressCallback, ProviderNotAvailable

log = logging.getLogger(__name__)

NOT_AVAILABLE_MESSAGE = "Spotify support is not built yet. See docs/ARCHITECTURE.md to add it."
_HOSTS = frozenset({"open.spotify.com", "play.spotify.com", "spotify.link"})


class SpotifyProvider:
    name = "spotify"
    display_name = "Spotify"

    def matches(self, url: str) -> bool:
        url = (url or "").strip()
        if not url:
            return False
        if url.lower().startswith("spotify:"):
            return True
        if "://" not in url:
            url = "https://" + url
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return False
        if host.startswith("www."):
            host = host[4:]
        return host in _HOSTS

    def resolve(self, url: str) -> list[TrackRef]:
        raise ProviderNotAvailable(NOT_AVAILABLE_MESSAGE)

    def download(
        self,
        ref: TrackRef,
        dest_dir: Path,
        settings: Settings,
        progress: ProgressCallback,
        cancel: threading.Event,
    ) -> Track:
        raise ProviderNotAvailable(NOT_AVAILABLE_MESSAGE)

    def doctor(self) -> list[tuple[bool, str, str]]:
        return [
            (
                False,
                "Spotify",
                "Not implemented yet — planned via spotDL (metadata from Spotify, audio from YouTube Music).",
            )
        ]
