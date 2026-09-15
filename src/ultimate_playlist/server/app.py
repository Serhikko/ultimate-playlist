"""FastAPI application: JSON API for the web UI plus the static files and `serve()`.

Every error is returned as ``{"detail": "..."}`` with a proper 4xx status so the UI can show the
message as-is. Track ids are always looked up in the library index and never turned into paths
directly, so nothing in here can be used to read files outside the library folder.
"""

from __future__ import annotations

import dataclasses
import html
import json
import logging
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import __version__, providers
from ..config import (
    Settings,
    app_data_dir,
    clean_audio_format,
    clean_audio_quality,
    clean_client_id,
    clean_concurrency,
    clean_js_runtimes,
)
from ..downloader import JobManager
from ..ffmpeg import find_ffmpeg
from ..library import Library, LibraryFileInUse, LibraryUnavailable
from ..logs import install_handlers, log_file_path
from ..models import Job, Playlist
from ..playlists import PlaylistNotFound, PlaylistStore, export_m3u8, m3u8_text
from ..providers import run_doctor
from ..providers.base import ProviderError

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8765  # what `up` serves on (and create_app assumes) unless told otherwise
PORT_ATTEMPTS = 11  # the requested port plus the next 10
M3U8_MEDIA_TYPE = "audio/x-mpegurl"
# The only Host / Origin values a local app should ever see. Anything else is a DNS-rebinding
# page or a cross-site form trying to drive the API (see LoopbackOnlyMiddleware).
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
ANY_HOST = "*"
WILDCARD_BIND_HOSTS = frozenset({"0.0.0.0", "::", ""})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<rect width="24" height="24" rx="6" fill="#0d0f14"/>'
    '<g fill="none" stroke="#5d8dff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>'
    "</g></svg>"
)
_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
}
_ILLEGAL_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# Device names Windows reserves whatever the extension ("CON.m3u8" opens the console).
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
# Browsers must not second-guess the declared type of a cover or an audio file.
NOSNIFF = {"X-Content-Type-Options": "nosniff"}

# Connecting a Spotify account (Authorization Code + PKCE, see providers/spotify_auth.py).
# The callback path is part of the Redirect URI the user registers, so it never changes
# (test_api checks it equals spotify_auth.CALLBACK_PATH).
SPOTIFY_CALLBACK_PATH = "/api/spotify/callback"
SPOTIFY_TOKEN_FILE = "spotify_auth.json"  # in app_data_dir(); written by spotify_auth only
SPOTIFY_NO_CLIENT_ID = (
    "Add your Spotify app's Client ID in Settings first, then click Connect Spotify."
)
SPOTIFY_CONNECT_FAILED = "Could not connect Spotify. See app.log for details."
SPOTIFY_NEEDS_LOOPBACK = (
    "Connect Spotify only works while the app listens on 127.0.0.1, where Spotify sends the "
    "browser back. Start the app without --host (or with --host 0.0.0.0) to connect."
)
_SPOTIFY_MESSAGE_MAX = 300  # the message ends up in a toast; keep it short
# The login and callback answers carry a one-time state / code: never cache or refer them.
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_MASK = "********"


# -- request bodies ----------------------------------------------------------------------------


class JobRequest(BaseModel):
    url: str


class SettingsPatch(BaseModel):
    """Partial settings update; every field is optional and unknown keys are ignored (so a page
    from a pre-release 0.2 build that still sends `spotify_client_secret` does no harm)."""

    model_config = ConfigDict(extra="ignore")

    library_dir: str | None = None
    audio_format: str | None = None
    audio_quality: str | None = None
    ffmpeg_path: str | None = None
    concurrency: int | None = None
    embed_cover: bool | None = None
    js_runtimes: list[str] | None = None
    spotify_client_id: str | None = None


class PlaylistCreate(BaseModel):
    name: str


class PlaylistPatch(BaseModel):
    name: str | None = None
    track_ids: list[str] | None = None


class TrackIds(BaseModel):
    track_ids: list[str]


# -- helpers -----------------------------------------------------------------------------------


def safe_playlist_filename(name: str) -> str:
    """File-system-safe stem for an exported playlist (Windows rules, max 100 chars)."""
    text = _ILLEGAL_NAME_RE.sub("", name or "")
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ").strip()
    text = text[:100].rstrip(". ").strip() or "playlist"
    if text.split(".", 1)[0].strip().upper() in _WINDOWS_RESERVED:
        text = "_" + text
    return text


def media_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _MEDIA_TYPES:
        return _MEDIA_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def open_folder(path: Path) -> None:
    """Show a folder in the OS file manager (Explorer / Finder / xdg-open)."""
    startfile = getattr(os, "startfile", None)
    if startfile is not None:  # Windows
        startfile(str(path))
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def split_links(text: str) -> list[str]:
    """Split pasted text into individual links (whitespace/newline separated, order kept)."""
    seen: set[str] = set()
    links: list[str] = []
    for raw in (text or "").split():
        link = raw.strip()
        if link and link not in seen:
            seen.add(link)
            links.append(link)
    return links


def host_of(header: str) -> str:
    """The host part of a Host header ("[::1]:8765" -> "::1", "localhost:8765" -> "localhost")."""
    value = (header or "").strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value
    return value.split(":", 1)[0]


def origin_host(origin: str) -> str | None:
    """The host of an Origin header, or None when it has none ("null", garbage)."""
    try:
        return (urlsplit(origin.strip()).hostname or "").lower() or None
    except ValueError:
        return None


class LoopbackOnlyMiddleware:
    """Refuse requests that do not come from the local browser.

    * Host must be a loopback name: a DNS-rebinding page (attacker.example resolving to
      127.0.0.1) would otherwise read the library and, via the settings + rescan + delete
      endpoints, delete the user's files.
    * For non-GET requests an Origin header, when present, must be loopback too: that stops
      plain cross-site form POSTs to the body-less endpoints (/api/library/open, /rescan).

    Pure ASGI (no BaseHTTPMiddleware) so streaming file responses are untouched.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: Sequence[str]) -> None:
        self.app = app
        self.allowed = {h.strip("[]").lower() for h in allowed_hosts}
        self.any_host = ANY_HOST in self.allowed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self.any_host:
            headers = Headers(scope=scope)
            if host_of(headers.get("host", "")) not in self.allowed:
                response = JSONResponse(
                    {"detail": "This app only answers requests addressed to 127.0.0.1."},
                    status_code=400,
                )
                await response(scope, receive, send)
                return
            origin = headers.get("origin")
            if scope.get("method", "GET").upper() not in SAFE_METHODS and origin is not None:
                if origin_host(origin) not in self.allowed:
                    response = JSONResponse(
                        {"detail": "Cross-site requests are not allowed."}, status_code=403
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def _spotify_auth() -> Any:
    """providers/spotify_auth.py, imported on first use (tests monkeypatch its functions)."""
    from ..providers import spotify_auth

    return spotify_auth


def _stored_spotify_tokens() -> list[str]:
    """The saved Spotify tokens, read only to scrub them out of provider texts. Never raises."""
    try:
        data = json.loads((app_data_dir() / SPOTIFY_TOKEN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    values = (data.get("access_token"), data.get("refresh_token"))
    return [v for v in values if isinstance(v, str) and len(v) >= 8]


def provider_status(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Doctor checks of every registered provider, in registry order. Never raises.

    Check texts are free-form provider output; the stored Spotify tokens are scrubbed out of
    them as a safety net, so no provider can ever echo one into the status page.
    """
    tokens = _stored_spotify_tokens()

    def scrub(text: object) -> str:
        value = str(text)
        for token in tokens:
            value = value.replace(token, _MASK)
        return value

    result: list[dict[str, Any]] = []
    for provider in list(providers.PROVIDERS):
        try:
            checks = run_doctor(provider, settings)
        except Exception as exc:  # noqa: BLE001 - a broken provider must not break /api/status
            checks = [
                (False, getattr(provider, "display_name", provider.name), f"Check failed: {exc}")
            ]
        result.append(
            {
                "name": provider.name,
                "display_name": getattr(provider, "display_name", provider.name),
                "checks": [
                    {"ok": bool(ok), "label": scrub(label), "detail": scrub(detail)}
                    for ok, label, detail in checks
                ],
            }
        )
    return result


def requested_library_dir(raw: str) -> Path:
    """The absolute folder a settings body asks for (no side effects; 400 when blank).

    Absolute, or the folder would silently depend on where `up serve` is started next time.
    """
    text = (raw or "").strip()
    if not text:
        raise HTTPException(400, "The library folder cannot be empty.")
    return Path(os.path.abspath(Path(os.path.expandvars(text)).expanduser()))


def _validate_settings_patch(patch: SettingsPatch) -> dict[str, Any]:
    """Turn a partial body into concrete field values, raising 400 with a friendly message."""
    changes: dict[str, Any] = {}
    if patch.library_dir is not None:
        new_dir = requested_library_dir(patch.library_dir)
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"Cannot create the library folder {new_dir}: {exc}") from exc
        if not new_dir.is_dir():
            raise HTTPException(400, f"{new_dir} is not a folder.")
        changes["library_dir"] = new_dir
    if patch.audio_format is not None:
        changes["audio_format"] = _clean(clean_audio_format, patch.audio_format)
    if patch.audio_quality is not None:
        changes["audio_quality"] = _clean(clean_audio_quality, patch.audio_quality)
    if patch.ffmpeg_path is not None:
        raw = patch.ffmpeg_path.strip()
        if raw:
            ffmpeg_path = Path(os.path.expandvars(raw)).expanduser()
            if not ffmpeg_path.is_file():
                # A bare "ffmpeg" / "ffmpeg.exe" that PATH resolves is fine too.
                on_path = shutil.which(str(ffmpeg_path))
                if on_path is None:
                    raise HTTPException(400, f"ffmpeg was not found at {ffmpeg_path}.")
                ffmpeg_path = Path(on_path)
            changes["ffmpeg_path"] = str(ffmpeg_path)
        else:
            changes["ffmpeg_path"] = None
    if patch.concurrency is not None:
        changes["concurrency"] = _clean(clean_concurrency, patch.concurrency)
    if patch.embed_cover is not None:
        changes["embed_cover"] = patch.embed_cover
    if patch.js_runtimes is not None:
        changes["js_runtimes"] = _clean(clean_js_runtimes, patch.js_runtimes)
    if patch.spotify_client_id is not None:
        changes["spotify_client_id"] = _clean(clean_client_id, patch.spotify_client_id)
    return changes


def _clean(rule: Any, *args: Any) -> Any:
    """Apply one of config.py's value rules; its ValueError becomes a 400 with the same text."""
    try:
        return rule(*args)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def back_to_app(app_state: Any, *, error: str | None = None) -> RedirectResponse:
    """303 to the page, which shows the outcome as a toast and then cleans its URL.

    An error message stays on the server (`GET /api/spotify` hands it out once as
    `last_error`): were it in the URL, any web site could link to the app with a message of its
    choosing and have it shown as if the app had said it.
    """
    if error is None:
        app_state.spotify_last_error = None
        target = "/?spotify=connected"
    else:
        text = " ".join(error.split())[:_SPOTIFY_MESSAGE_MAX] or SPOTIFY_CONNECT_FAILED
        app_state.spotify_last_error = text
        target = "/?spotify=error"
    return RedirectResponse(target, status_code=303, headers=NO_STORE)


def spotify_callback_reachable(host: str) -> bool:
    """Can Spotify's redirect to 127.0.0.1 reach a server bound to `host`? True for loopback
    and for every-interface binds; False for one LAN address ("192.168.1.5") or "::1"."""
    name = (host or "").strip().strip("[]").lower()
    return name in WILDCARD_BIND_HOSTS or name in ("127.0.0.1", "localhost")


def friendly_error(request: Request, message: str, status_code: int) -> Response:
    """A top-level browser navigation gets a small page; everything else the usual JSON."""
    if "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": message}, status_code=status_code)
    page = (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Ultimate Playlist</title></head>"
        '<body style="margin:0;padding:32px 16px;font:15px/1.5 system-ui,sans-serif;'
        'background:#0d0f14;color:#e8eaf0">'
        f"<p>{html.escape(message)}</p>"
        '<p><a href="/" style="color:#5d8dff">Back to Ultimate Playlist</a></p>'
        "</body></html>"
    )
    return HTMLResponse(page, status_code=status_code, headers=NO_STORE)


# -- the application ---------------------------------------------------------------------------


def create_app(
    settings: Settings | None = None,
    library: Library | None = None,
    playlists: PlaylistStore | None = None,
    jobs: JobManager | None = None,
    allowed_hosts: Sequence[str] = LOOPBACK_HOSTS,
    port: int = DEFAULT_PORT,
    spotify_connect_ok: bool = True,
) -> FastAPI:
    """Build the app. Objects not passed in are created from `settings` (default: config.json).

    `allowed_hosts` are the Host / Origin names the API answers to (loopback by default; pass
    `["*"]` to accept any, e.g. when deliberately serving on a LAN address). `port` is where
    the server listens (serve() passes the one it picked): the Spotify Redirect URI uses it.
    `spotify_connect_ok` is False when the server does not listen on 127.0.0.1, where Spotify
    sends the browser back after Connect Spotify (see spotify_callback_reachable).
    """
    settings = settings if settings is not None else Settings.load()
    library = library if library is not None else Library(settings.library_dir, settings.index_path)
    playlists = playlists if playlists is not None else PlaylistStore(settings.playlists_path)
    jobs = jobs if jobs is not None else JobManager(settings, library)
    providers.configure(settings)  # doctor()/resolve() must see the same settings as downloads

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        jobs.start()
        try:
            yield
        finally:
            jobs.stop()

    app = FastAPI(title="Ultimate Playlist", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.library = library
    app.state.playlists = playlists
    app.state.jobs = jobs
    app.state.port = port
    app.state.spotify_connect_ok = spotify_connect_ok
    app.state.spotify_last_error = None  # set by back_to_app, handed out once by /api/spotify
    app.add_middleware(LoopbackOnlyMiddleware, allowed_hosts=list(allowed_hosts))

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.exception_handler(PlaylistNotFound)
    async def _playlist_not_found(_request: Request, exc: PlaylistNotFound) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(LibraryUnavailable)
    async def _library_unavailable(_request: Request, exc: LibraryUnavailable) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(LibraryFileInUse)
    async def _library_file_in_use(_request: Request, exc: LibraryFileInUse) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def _bad_request_body(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic reports a list of error objects; the spec promises {"detail": "<text>"}.
        parts: list[str] = []
        for error in exc.errors():
            loc = [str(p) for p in error.get("loc", ())]
            if loc and loc[0] in ("body", "query", "path", "header"):
                loc = loc[1:]
            field = ".".join(loc)
            message = str(error.get("msg", "invalid value"))
            parts.append(f"{field}: {message}" if field else message)
        detail = "; ".join(parts) or "The request body is not valid."
        return JSONResponse({"detail": detail}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, _exc: Exception) -> JSONResponse:
        # The traceback goes to app.log; the browser gets the spec's {"detail": ...} shape and
        # nothing internal.
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            {"detail": "Something went wrong on the server. See app.log for details."},
            status_code=500,
        )

    def require_playlist(pid: str) -> Playlist:
        playlist = playlists.get(pid)
        if playlist is None:
            raise HTTPException(404, f"Playlist not found: {pid}")
        return playlist

    def require_track_file(track_id: str) -> Path:
        track = library.get(track_id)
        if track is None:
            raise HTTPException(404, "Track not found")
        path = library.abs_path(track)
        if not path.is_file():
            raise HTTPException(404, "The audio file is missing from the library folder")
        return path

    def require_job(job_id: str) -> Job:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    # -- UI ----------------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        index_html = STATIC_DIR / "index.html"
        if not index_html.is_file():
            raise HTTPException(404, "The web UI files are missing (server/static/index.html).")
        return FileResponse(str(index_html), media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(
            content=FAVICON_SVG,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # -- status & settings -------------------------------------------------------------------

    @app.get("/api/version")
    def version() -> dict[str, str]:
        """Only the version: no tool probes, no library count. A second UltimatePlaylist.exe
        asks this to find the running copy (cli.running_instance), so it must answer at once
        even while the first start is still probing ffmpeg."""
        return {"version": __version__}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        ffmpeg = find_ffmpeg(settings.ffmpeg_path)
        return {
            "version": __version__,
            "library_dir": str(settings.library_dir),
            "providers": provider_status(settings),
            "ffmpeg": {
                "path": ffmpeg.path,
                "version": ffmpeg.version,
                "bundled": ffmpeg.bundled,  # the copy in the package's bin folder
            },
            "jobs_active": jobs.active_count(),
            "tracks": len(library),
        }

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return settings.to_dict()

    @app.put("/api/settings")
    def put_settings(patch: SettingsPatch) -> dict[str, Any]:
        old_dir = Path(settings.library_dir)
        moving = (
            patch.library_dir is not None and requested_library_dir(patch.library_dir) != old_dir
        )
        # Refuse before validation creates the new folder: a rejected request must not leave
        # an empty directory behind.
        if moving and jobs.active_count():
            raise HTTPException(
                409, "Wait for the current downloads to finish before moving the library folder."
            )
        changes = _validate_settings_patch(patch)
        new_dir = Path(changes.get("library_dir", old_dir))
        moving = new_dir != old_dir
        # Save a copy first: a failed write must not leave the running app on settings that
        # were never persisted (and the Library pointing somewhere else than the JobManager).
        # The copy keeps the library folder config.json already has unless this request moves
        # the library: a one-off `up --library X serve` must not be persisted because the user
        # changed something else in the dialog (the running app keeps using X, though).
        candidate = dataclasses.replace(settings, **changes)
        if "library_dir" not in changes:
            candidate = dataclasses.replace(candidate, library_dir=Settings.load().library_dir)
        try:
            candidate.save()
        except OSError as exc:
            raise HTTPException(500, f"Could not save the settings: {exc}") from exc
        for key, value in changes.items():
            setattr(settings, key, value)
        # Providers keep their own reference to the Settings object, but a provider that caches
        # a derived client (a Spotify API token, say) needs the nudge to rebuild it.
        providers.configure(settings)
        if "concurrency" in changes:
            jobs.set_concurrency(settings.concurrency)
        if moving:
            library.set_library_dir(new_dir)
            try:
                changed = library.rescan()
            except LibraryUnavailable as exc:
                log.warning("Library moved to %s but it could not be scanned: %s", new_dir, exc)
                changed = 0
            # Playlists are deliberately NOT pruned here: pointing the app at a folder before
            # the files are copied over (or at the wrong folder) must not wipe the user's
            # curation. Track ids are stable, the UI shows the entries as "missing" until the
            # files turn up, and an explicit rescan / delete is where dangling ids are dropped.
            log.info("Library moved to %s (%d change(s) after rescan)", new_dir, changed)
        return settings.to_dict()

    # -- Spotify account ---------------------------------------------------------------------

    def spotify_redirect_uri() -> str:
        return str(_spotify_auth().redirect_uri(int(app.state.port)))

    @app.get("/api/spotify")
    def spotify_state() -> dict[str, Any]:
        """What the Settings dialog shows. Never a token: SpotifyAccount carries none.

        A sign-in is bound to the Client ID it was issued for: with another one configured
        the user is not connected (and connected again when the old ID comes back).
        `connect_ok` is False when Spotify's redirect to 127.0.0.1 cannot reach this server.
        `last_error` is the message of the last failed Connect Spotify, handed out once (the
        page shows it after `/?spotify=error`).
        """
        account = None
        if settings.spotify_client_id:
            try:
                account = _spotify_auth().current_account(settings.spotify_client_id)
            except Exception as exc:  # noqa: BLE001 - an unreadable sign-in = not connected
                log.warning("Could not read the saved Spotify sign-in: %s", exc)
        last_error, app.state.spotify_last_error = app.state.spotify_last_error, None
        return {
            "client_id_set": bool(settings.spotify_client_id),
            "connected": account is not None,
            "display_name": (account.display_name or account.user_id) if account else None,
            "redirect_uri": spotify_redirect_uri(),
            "connect_ok": bool(app.state.spotify_connect_ok),
            "last_error": last_error,
        }

    @app.get("/api/spotify/login", include_in_schema=False)
    def spotify_login(request: Request) -> Response:
        """The "Connect Spotify" button navigates here; we send the browser on to Spotify."""
        if not app.state.spotify_connect_ok:
            return friendly_error(request, SPOTIFY_NEEDS_LOOPBACK, 400)
        if not settings.spotify_client_id:
            return friendly_error(request, SPOTIFY_NO_CLIENT_ID, 400)
        try:
            url = _spotify_auth().begin_login(settings.spotify_client_id, spotify_redirect_uri())
        except ProviderError as exc:
            return back_to_app(app.state, error=str(exc))
        return RedirectResponse(url, status_code=302, headers=NO_STORE)

    @app.get(SPOTIFY_CALLBACK_PATH, include_in_schema=False)
    def spotify_callback(
        state: str = "", code: str | None = None, error: str | None = None
    ) -> Response:
        """Spotify sends the browser back here from its consent page (a plain top-level GET,
        which LoopbackOnlyMiddleware lets through like any other GET to 127.0.0.1). The
        user always lands on the app again; the code never appears in a response or a log."""

        def scrub(text: str) -> str:
            return text.replace(code, _MASK) if code else text

        try:
            account = _spotify_auth().finish_login(state, code, error)
        except ProviderError as exc:
            message = scrub(str(exc)) or SPOTIFY_CONNECT_FAILED
            log.info("Connecting Spotify failed: %s", message)
            return back_to_app(app.state, error=message)
        except Exception as exc:  # noqa: BLE001 - the user gets a message, app.log the reason
            log.error("Connecting Spotify failed: %s", scrub(f"{type(exc).__name__}: {exc}"))
            return back_to_app(app.state, error=SPOTIFY_CONNECT_FAILED)
        log.info("Spotify connected as %s", account.display_name or account.user_id)
        providers.configure(settings)  # a provider caching a client picks up the account
        return back_to_app(app.state)

    @app.post("/api/spotify/logout")
    def spotify_logout() -> dict[str, Any]:
        _spotify_auth().logout()
        providers.configure(settings)
        return {"ok": True}

    # -- jobs --------------------------------------------------------------------------------

    @app.post("/api/jobs", status_code=202)
    def post_jobs(body: JobRequest) -> dict[str, Any]:
        links = split_links(body.url)
        if not links:
            raise HTTPException(400, "Paste a link first")
        if len(links) > 1:  # validate everything before queueing anything
            for link in links:
                if jobs.providers.get_provider(link) is None:
                    raise HTTPException(400, f"No provider for this link: {link}")
        created = []
        for link in links:
            try:
                created.append(jobs.submit(link).to_dict())
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        return {"jobs": created}

    @app.get("/api/jobs")
    def get_jobs() -> dict[str, Any]:
        return {"jobs": jobs.snapshot()}

    @app.delete("/api/jobs/finished")
    def delete_finished_jobs() -> dict[str, Any]:
        return {"removed": jobs.clear_finished()}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        job = require_job(job_id)
        cancelled = jobs.cancel(job_id)
        return {"cancelled": cancelled, "job": job.to_dict()}

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str) -> dict[str, Any]:
        job = require_job(job_id)
        new_job = jobs.retry(job_id)
        if new_job is None:
            raise HTTPException(
                409,
                f"Only failed or cancelled jobs can be retried (this one is {job.status.value}).",
            )
        return {"job": new_job.to_dict()}

    # -- library -----------------------------------------------------------------------------

    @app.get("/api/library")
    def get_library(
        q: str = Query("", description="Search over title, artist and album"),
    ) -> dict[str, Any]:
        return {"tracks": [t.to_dict() for t in library.search(q)]}

    @app.post("/api/library/rescan")
    def rescan_library() -> dict[str, Any]:
        changed = library.rescan()
        # A folder with no audio at all is the tell-tale of a wrong / not-yet-populated folder;
        # keep the playlists intact then rather than emptying every one of them.
        if len(library):
            pruned = playlists.prune({t.id for t in library.all()})
            if pruned:
                log.info("Removed %d missing track(s) from playlists after rescan", pruned)
        return {"changed": changed, "tracks": len(library)}

    @app.post("/api/library/open")
    def open_library_folder() -> dict[str, Any]:
        folder = Path(settings.library_dir)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            open_folder(folder)
        except OSError as exc:
            raise HTTPException(500, f"Could not open {folder}: {exc}") from exc
        return {"ok": True}

    @app.get("/api/library/{track_id}/cover")
    def get_cover(track_id: str) -> Response:
        if library.get(track_id) is None:
            raise HTTPException(404, "Track not found")
        cover = library.cover_bytes(track_id)
        if cover is None:
            raise HTTPException(404, "No cover art for this track")
        data, mime = cover
        if not mime.lower().startswith("image/"):  # belt and braces; read_cover already sniffs
            mime = "image/jpeg"
        return Response(
            content=data,
            media_type=mime,
            headers={"Cache-Control": "private, max-age=3600", **NOSNIFF},
        )

    @app.delete("/api/library/{track_id}")
    def delete_track(track_id: str, delete_file: bool = False) -> dict[str, Any]:
        if not library.remove(track_id, delete_file=delete_file):  # LibraryFileInUse -> 409
            raise HTTPException(404, "Track not found")
        playlists.prune({t.id for t in library.all()})
        return {"removed": True, "id": track_id, "file_deleted": delete_file}

    @app.api_route("/media/{track_id}", methods=["GET", "HEAD"], include_in_schema=False)
    def media(track_id: str) -> FileResponse:
        path = require_track_file(track_id)
        return FileResponse(str(path), media_type=media_type_for(path), headers=NOSNIFF)

    # -- playlists ---------------------------------------------------------------------------

    @app.get("/api/playlists")
    def get_playlists() -> dict[str, Any]:
        return {"playlists": [p.to_dict() for p in playlists.all()]}

    @app.post("/api/playlists", status_code=201)
    def create_playlist(body: PlaylistCreate) -> dict[str, Any]:
        try:
            playlist = playlists.create(body.name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"playlist": playlist.to_dict()}

    @app.patch("/api/playlists/{pid}")
    def patch_playlist(pid: str, body: PlaylistPatch) -> dict[str, Any]:
        playlist = require_playlist(pid)
        try:
            if body.name is not None:
                PlaylistStore.clean_name(body.name)  # validate before touching anything
            if body.track_ids is not None:  # validates the order and only then persists it
                playlist = playlists.set_order(pid, body.track_ids)
            if body.name is not None:
                playlist = playlists.rename(pid, body.name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"playlist": playlist.to_dict()}

    @app.delete("/api/playlists/{pid}")
    def delete_playlist(pid: str) -> dict[str, Any]:
        if not playlists.delete(pid):
            raise HTTPException(404, f"Playlist not found: {pid}")
        return {"removed": True, "id": pid}

    @app.post("/api/playlists/{pid}/tracks")
    def add_playlist_tracks(pid: str, body: TrackIds) -> dict[str, Any]:
        require_playlist(pid)
        unknown = [tid for tid in body.track_ids if library.get(tid) is None]
        if unknown:
            raise HTTPException(400, f"Track not in the library: {unknown[0]}")
        playlist = playlists.add_tracks(pid, body.track_ids)
        return {"playlist": playlist.to_dict()}

    @app.delete("/api/playlists/{pid}/tracks/{track_id}")
    def remove_playlist_track(pid: str, track_id: str) -> dict[str, Any]:
        require_playlist(pid)
        playlist = playlists.remove_track(pid, track_id)
        return {"playlist": playlist.to_dict()}

    @app.post("/api/playlists/{pid}/export")
    def export_playlist(pid: str) -> dict[str, Any]:
        playlist = require_playlist(pid)
        out_dir = Path(settings.playlists_export_dir)
        out_path = out_dir / f"{safe_playlist_filename(playlist.name)}.m3u8"
        try:
            export_m3u8(playlist, library, out_path, relative_to=out_dir)
        except OSError as exc:
            raise HTTPException(500, f"Could not write {out_path}: {exc}") from exc
        return {"path": str(out_path)}

    @app.get("/api/playlists/{pid}/export.m3u8", include_in_schema=False)
    def download_playlist(pid: str) -> Response:
        playlist = require_playlist(pid)
        text = m3u8_text(playlist, library, relative_to=None)  # built in memory: no stray files
        filename = f"{safe_playlist_filename(playlist.name)}.m3u8"
        return Response(
            content=text.encode("utf-8"),
            media_type=M3U8_MEDIA_TYPE,
            headers={"Content-Disposition": content_disposition("attachment", filename)},
        )

    return app


def content_disposition(kind: str, filename: str) -> str:
    """RFC 6266 header value; non-ASCII names use the filename* form like Starlette does."""
    quoted = quote(filename)
    if quoted != filename:
        return f"{kind}; filename*=utf-8''{quoted}"
    return f'{kind}; filename="{filename}"'


# -- serving -----------------------------------------------------------------------------------

_logging_configured = False


def configure_logging(level: int = logging.INFO) -> Path:
    """Log to stderr and to a rotating app.log in the app data dir. Safe to call twice.

    The console stays calm (no routine access lines, no yt-dlp chatter); app.log gets everything
    except the successful-request access lines, see `ultimate_playlist.logs`.
    """
    global _logging_configured
    log_path = log_file_path()
    if _logging_configured:
        return log_path
    install_handlers(console_level=level, file_level=logging.INFO)
    _logging_configured = True
    return log_path


def port_is_free(host: str, port: int) -> bool:
    """Can `port` be bound on every address `host` resolves to (IPv4 and/or IPv6)?

    uvicorn binds each resolved address ("localhost" is 127.0.0.1 *and* ::1 on most machines),
    so the probe must do the same, with a socket of the matching family: an AF_INET socket can
    never bind "::1" and would report every port as busy.
    """
    name = (host or "").strip().strip("[]") or None
    try:
        infos = socket.getaddrinfo(
            name, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE if name is None else 0
        )
    except socket.gaierror:
        return False
    if not infos:
        return False
    for family, socktype, proto, _canonical, sockaddr in infos:
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                sock.bind(sockaddr)
        except OSError:
            return False
    return True


def pick_port(host: str, port: int, attempts: int = PORT_ATTEMPTS) -> int:
    """The requested port if free, else the next free one within `attempts` tries."""
    for candidate in range(port, port + attempts):
        if port_is_free(host, candidate):
            return candidate
    raise OSError(f"No free port between {port} and {port + attempts - 1} on {host}.")


def open_browser_later(url: str, delay: float = 1.0) -> threading.Timer:
    timer = threading.Timer(delay, webbrowser.open, args=(url,))
    timer.daemon = True
    timer.start()
    return timer


def serve(
    settings: Settings | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> None:
    """Run the web app with uvicorn (blocking). Falls back to the next free port if `port` is busy."""
    import uvicorn

    configure_logging()
    settings = settings if settings is not None else Settings.load()
    chosen = pick_port(host, port)
    if chosen != port:
        log.warning("Port %d is busy, using %d instead", port, chosen)
    if host in WILDCARD_BIND_HOSTS:
        allowed_hosts: list[str] = [ANY_HOST]  # the user asked for LAN access on purpose
        log.warning("Serving on every network interface (%s); any host name is accepted", host)
    else:
        allowed_hosts = [*LOOPBACK_HOSTS, host]
    connect_ok = spotify_callback_reachable(host)
    app = create_app(
        settings, allowed_hosts=allowed_hosts, port=chosen, spotify_connect_ok=connect_ok
    )
    if not connect_ok and settings.spotify_client_id:
        log.warning(
            "Connect Spotify is off: Spotify sends the browser back to http://127.0.0.1:%d%s, "
            "and the app only listens on %s. Start it without --host to connect.",
            chosen,
            SPOTIFY_CALLBACK_PATH,
            host,
        )
    elif chosen != DEFAULT_PORT and settings.spotify_client_id:
        log.warning(
            "To connect Spotify on this port, add http://127.0.0.1:%d%s as a Redirect URI "
            "of your Spotify app.",
            chosen,
            SPOTIFY_CALLBACK_PATH,
        )
    # A wildcard bind address is not something a browser can open; loopback always works.
    browser_host = "127.0.0.1" if host in WILDCARD_BIND_HOSTS else host
    url = f"http://{browser_host}:{chosen}/"
    log.info("Ultimate Playlist %s at %s (library: %s)", __version__, url, settings.library_dir)
    if open_browser:
        open_browser_later(url)
    uvicorn.run(app, host=host, port=chosen, log_config=None, log_level="info")
