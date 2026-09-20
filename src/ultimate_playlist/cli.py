"""Command line interface (`up`). No subcommand starts the web app."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .bundled import is_frozen
from .config import (
    MAX_CONCURRENCY,
    MIN_CONCURRENCY,
    Settings,
    app_data_dir,
    clean_audio_format,
    clean_audio_quality,
    clean_client_id,
    clean_concurrency,
    clean_js_runtimes,
)
from .ffmpeg import find_ffmpeg, install_hint
from .library import Library, LibraryUnavailable
from .models import JobStatus, Playlist

log = logging.getLogger(__name__)

OK_MARK = "✓"
BAD_MARK = "✗"
DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
CONFIG_KEYS: tuple[str, ...] = tuple(f.name for f in fields(Settings))
_BOOL_WORDS = {
    "true": True,
    "yes": True,
    "on": True,
    "1": True,
    "false": False,
    "no": False,
    "off": False,
    "0": False,
}
# The ports serve() may end up on: the requested one plus the next ten. Kept equal to
# server.app.PORT_ATTEMPTS (test_cli guards that) instead of imported, so the CLI does not load
# FastAPI for `up list`.
PORT_ATTEMPTS = 11
JOIN_TIMEOUT = 3.0  # seconds to wait for /api/version on a port that listens
LISTEN_TIMEOUT = 0.5  # seconds for the TCP connect that tells a listening port from a closed one
JOIN_PAUSE = 1.5  # seconds the "already running" line stays readable before the window closes
POLL_INTERVAL = 0.2  # seconds between progress checks in `add`
RUNNING_APP_TIMEOUT = 5.0  # seconds a running app gets to answer `config set` / `spotify logout`
EXIT_OK, EXIT_FAILURE, EXIT_INTERRUPTED = 0, 1, 130
EXIT_USAGE = 2  # what argparse uses for a bad command line; `config set` errors are the same kind
NO_SECRET_MESSAGE = "Spotify no longer needs a client secret; only spotify_client_id is used."
SPOTIFY_DASHBOARD_URL = "https://developer.spotify.com/dashboard"

# A loopback probe must never go through HTTP_PROXY (set on corporate machines, usually without
# NO_PROXY=127.0.0.1): the default opener would send it to the proxy, which cannot answer.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# -- small helpers -----------------------------------------------------------------------------


def _ensure_unicode_stdout() -> None:
    """Piped output on Windows defaults to cp1252; switch to UTF-8 so ✓/✗ never crash the CLI."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        reconfigure = getattr(stream, "reconfigure", None)
        if encoding != "utf8" and callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _configure_cli_logging() -> None:
    """Warnings and errors on stderr (minus yt-dlp chatter), the full story in app.log.

    A no-op when logging is already set up (pytest, or an embedding program), like basicConfig.
    """
    if logging.getLogger().handlers:
        return
    from .logs import install_handlers

    install_handlers(console_level=logging.WARNING, file_level=logging.INFO)


def say(text: str = "") -> None:
    print(text, flush=True)  # progress lines must show up promptly even when piped


def complain(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def shorten(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    say(line.rstrip())
    say("  ".join("-" * w for w in widths))
    for row in rows:
        say("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())


def load_settings(library_override: str | None) -> Settings:
    settings = Settings.load()
    if library_override:
        # Absolute, like every other place that stores library_dir: a relative folder would
        # depend on the current directory and make every settings save look like a move.
        folder = Path(os.path.abspath(Path(os.path.expandvars(library_override)).expanduser()))
        settings = replace(settings, library_dir=folder)
    return settings


def find_playlist(playlists: Sequence[Playlist], name_or_id: str) -> Playlist:
    """Match by id first, then by name (case-insensitive). Raises ValueError with a friendly text."""
    for playlist in playlists:
        if playlist.id == name_or_id:
            return playlist
    wanted = name_or_id.strip().casefold()
    matches = [p for p in playlists if p.name.casefold() == wanted]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            f"No playlist named '{name_or_id}'. Run `up playlists` to see the available ones."
        )
    ids = ", ".join(p.id for p in matches)
    raise ValueError(f"{len(matches)} playlists are named '{name_or_id}'; use an id instead: {ids}")


# -- commands ----------------------------------------------------------------------------------


def running_instance(host: str, port: int, timeout: float = JOIN_TIMEOUT) -> str | None:
    """URL of an Ultimate Playlist of this version already serving on host:port, else None.

    Only a `/api/version` that answers with our own version counts: a foreign program on the
    port is left alone (the caller then falls back to the next free port, as before). That
    endpoint does no work (no ffmpeg probe, no library count), so a copy that is still busy
    with its first `/api/status` answers it at once.
    """
    url = f"http://{host}:{port}/"
    try:
        with _opener.open(url + "api/version", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):  # connection refused, timeout, not JSON: nothing of ours
        return None
    return url if isinstance(data, dict) and data.get("version") == __version__ else None


def _is_listening(host: str, port: int, timeout: float = LISTEN_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def listening_ports(host: str, ports: Sequence[int]) -> list[int]:
    """The ports something accepts connections on, checked all at once.

    Windows takes about two seconds to refuse a connection to a closed loopback port (it
    retries the SYN), so asking eleven closed ports one after the other kept the exe's window
    blank for over twenty seconds on every start. A listening port accepts at once even when
    the app behind it is busy, so a short timeout, in parallel, loses nothing.
    """
    if not ports:
        return []
    with ThreadPoolExecutor(max_workers=len(ports)) as pool:
        flags = list(pool.map(lambda candidate: _is_listening(host, candidate), ports))
    return [candidate for candidate, open_ in zip(ports, flags, strict=True) if open_]


def find_running_instance(host: str, port: int, attempts: int = PORT_ATTEMPTS) -> str | None:
    """The running copy on any port serve() could have chosen: `port` or the next free ones.

    When a foreign program holds 8765 the first double-click serves on 8766; the second must
    find it there instead of starting a third server. Only ports that listen at all are asked
    for their version, so the scan costs a fraction of a second on a normal start.
    """
    for candidate in listening_ports(host, range(port, port + attempts)):
        url = running_instance(host, candidate)
        if url:
            return url
    return None


class RunningAppError(Exception):
    """The running app answered with an error; the message is its own `detail` text."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def call_running_app(
    base_url: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = RUNNING_APP_TIMEOUT,
) -> Any:
    """One JSON request to a running copy of the app (loopback, never through a proxy).

    Returns the decoded answer. Raises RunningAppError for an error answer and OSError when
    the app cannot be reached or does not answer in time.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with _opener.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except (OSError, ValueError, AttributeError):
            detail = None
        raise RunningAppError(str(detail or f"HTTP {exc.code}"), exc.code) from None
    return json.loads(text) if text else None


def _set_console_title(title: str) -> None:
    """Name the console window (Task Manager and the taskbar show the exe path otherwise)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(title)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - cosmetic; never block the start
        log.debug("Could not set the console title: %s", exc)


def cmd_serve(args: argparse.Namespace) -> int:
    settings = load_settings(args.library)
    if is_frozen():  # the packaged exe: a console window that must explain itself
        _set_console_title("Ultimate Playlist")
        # A second double-click (the browser was slow to appear) must not start a second server
        # with its own queue on the next port; join the running one instead.
        if args.port == DEFAULT_PORT and args.host == DEFAULT_HOST:
            url = find_running_instance(args.host, args.port)
            if url:
                opening = "" if args.no_browser else " - opening it in your browser"
                say(f"Ultimate Playlist is already running at {url}{opening}.")
                say("This window closes by itself.")
                if not args.no_browser:
                    webbrowser.open(url)
                time.sleep(JOIN_PAUSE)  # long enough to read the line above
                return EXIT_OK
        browser = "" if args.no_browser else " Your browser will open."
        say(f"Starting Ultimate Playlist...{browser} Close this window to stop.")
        say("The first start after unpacking can take a minute while Windows checks the files.")
    # Imported only now: loading FastAPI takes seconds in the exe, and the lines above must be
    # on screen before that (a second start that joins a running copy never needs it at all).
    from .server.app import serve

    try:
        serve(settings, host=args.host, port=args.port, open_browser=not args.no_browser)
    except OSError as exc:
        complain(f"Could not start the server: {exc}")
        return EXIT_FAILURE
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def _job_line(job: dict[str, Any]) -> str:
    status = job["status"]
    title = job.get("title") or job.get("url") or ""
    detail = ""
    if status == JobStatus.ERROR.value:
        detail = job.get("error") or "failed"
    elif status == JobStatus.DONE.value:
        track = job.get("track")
        detail = track["path"] if track else (job.get("message") or "")
    elif job.get("message"):
        detail = job["message"]
    line = f"{status:<12} {title}"
    return f"{line}  ({detail})" if detail else line


def _report_changes(snapshot: list[dict[str, Any]], seen: dict[str, str]) -> None:
    """Print one line for every job whose status changed since the last call (oldest first)."""
    for job in reversed(snapshot):
        if seen.get(job["id"]) != job["status"]:
            seen[job["id"]] = job["status"]
            say(_job_line(job))


def cmd_add(args: argparse.Namespace) -> int:
    from . import providers
    from .downloader import JobManager

    settings = load_settings(args.library)
    providers.configure(settings)
    library = Library(settings.library_dir, settings.index_path)
    jobs = JobManager(settings, library)
    jobs.start()
    submit_failures = 0
    seen: dict[str, str] = {}
    for url in args.urls:
        try:
            job = jobs.submit(url)
        except ValueError as exc:
            complain(f"{BAD_MARK} {url}: {exc}")
            submit_failures += 1
            continue
        if not args.quiet:
            _report_changes([job.to_dict()], seen)
    try:
        while True:
            idle = jobs.wait_idle(timeout=POLL_INTERVAL)
            if not args.quiet:
                _report_changes(jobs.snapshot(), seen)
            if idle:
                break
    except KeyboardInterrupt:
        complain("Interrupted, cancelling…")
        for job in jobs.list(include_finished=False):
            jobs.cancel(job.id)
        jobs.stop()
        return EXIT_INTERRUPTED
    jobs.stop()
    snapshot = jobs.snapshot()
    good = {JobStatus.DONE.value, JobStatus.SKIPPED.value}
    counts: dict[str, int] = {}
    for job in snapshot:
        counts[job["status"]] = counts.get(job["status"], 0) + 1
    summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    say(f"Finished: {summary or 'nothing to do'}")
    all_ok = submit_failures == 0 and bool(snapshot) and all(j["status"] in good for j in snapshot)
    return EXIT_OK if all_ok else EXIT_FAILURE


def cmd_list(args: argparse.Namespace) -> int:
    settings = load_settings(args.library)
    library = Library(settings.library_dir, settings.index_path)
    tracks = library.search(args.query or "")
    if not tracks:
        say(
            "No tracks found."
            if args.query
            else "The library is empty. Add something with `up add <link>`."
        )
        return EXIT_OK
    rows = [
        (
            track.id,
            shorten(track.artist, 30),
            shorten(track.title, 45),
            format_duration(track.duration),
            track.path,
        )
        for track in tracks
    ]
    print_table(("id", "artist", "title", "duration", "path"), rows)
    say(f"{len(tracks)} track(s) in {settings.library_dir}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import providers

    settings = load_settings(args.library)
    say(f"Ultimate Playlist {__version__}")
    say(f"App data:  {app_data_dir()}")
    exists = "exists" if settings.library_dir.is_dir() else "will be created on first download"
    say(f"Library:   {settings.library_dir} ({exists})")
    ffmpeg = find_ffmpeg(settings.ffmpeg_path)
    if ffmpeg.found:
        ffmpeg_line = (True, ffmpeg.describe())
    else:
        ffmpeg_line = (False, install_hint())
    say(f"{OK_MARK if ffmpeg_line[0] else BAD_MARK} ffmpeg: {ffmpeg_line[1]}")
    youtube_ok = True
    checks = providers.doctor_all(settings)  # the same settings downloads use (ffmpeg_path...)
    for provider in list(providers.PROVIDERS):
        say(f"{getattr(provider, 'display_name', provider.name)}:")
        for ok, label, detail in checks.get(provider.name, []):
            if provider.name == "youtube" and not ok:
                youtube_ok = False
            if label == "ffmpeg" and (ok, detail) == ffmpeg_line:
                continue  # identical to the line above; only print it when they disagree
            say(f"  {OK_MARK if ok else BAD_MARK} {label}: {detail}")
    if not youtube_ok:
        say(f"{BAD_MARK} YouTube downloads will not work until the items above are fixed.")
    return EXIT_OK if youtube_ok else EXIT_FAILURE


def format_setting(value: object) -> str:
    """One setting as `config show` prints it: lists comma-joined, booleans lower-case."""
    if value is None or value == "":
        return "(not set)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def parse_setting(key: str, raw: str) -> Any:
    """Turn the text of `up config set KEY VALUE` into the typed value (ValueError if not).

    The same rules as `PUT /api/settings` (they share config.py's clean_* helpers); creating
    the library folder is the one side effect, so a typo is caught before anything is saved.
    """
    text = raw.strip()
    if key == "library_dir":
        if not text:
            raise ValueError("The library folder cannot be empty.")
        folder = Path(os.path.abspath(Path(os.path.expandvars(text)).expanduser()))
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValueError(f"Cannot create the library folder {folder}: {exc}") from exc
        if not folder.is_dir():
            raise ValueError(f"{folder} is not a folder.")
        return folder
    if key == "audio_format":
        return clean_audio_format(text)
    if key == "audio_quality":
        return clean_audio_quality(text)
    if key == "ffmpeg_path":
        if not text:
            return None  # back to "look on PATH"
        path = Path(os.path.expandvars(text)).expanduser()
        if not path.is_file():
            on_path = shutil.which(str(path))  # a bare "ffmpeg" that PATH resolves is fine
            if on_path is None:
                raise ValueError(f"ffmpeg was not found at {path}.")
            path = Path(on_path)
        return str(path)
    if key == "concurrency":
        try:
            value = int(text)
        except ValueError:
            raise ValueError(
                f"Concurrency must be a whole number between {MIN_CONCURRENCY} and "
                f"{MAX_CONCURRENCY}."
            ) from None
        return clean_concurrency(value)
    if key == "embed_cover":
        if text.lower() not in _BOOL_WORDS:
            raise ValueError("embed_cover must be true or false.")
        return _BOOL_WORDS[text.lower()]
    if key == "js_runtimes":
        return clean_js_runtimes(re.split(r"[,\s]+", text))
    if key == "spotify_client_id":
        return clean_client_id(text)
    raise ValueError(f"Unknown setting '{key}'. Settings you can change: {', '.join(CONFIG_KEYS)}")


def api_value(key: str, value: Any) -> Any:
    """A parsed setting as `PUT /api/settings` expects it in its JSON body."""
    if isinstance(value, Path):
        return str(value)
    if key == "ffmpeg_path" and value is None:
        return ""  # the API reads "" as "look on PATH again"; null means "not in this update"
    return value


def cmd_config(args: argparse.Namespace) -> int:
    if args.config_command == "show":
        settings = load_settings(args.library)
        say(f"Config file: {Settings.default_path()}")
        for key, value in settings.to_dict().items():
            say(f"{key} = {format_setting(value)}")
        return EXIT_OK

    key = args.key.strip().lower().replace("-", "_")
    if key == "spotify_client_secret":  # pre-release 0.2 builds had it; connecting needs none
        complain(NO_SECRET_MESSAGE)
        return EXIT_USAGE
    if key not in CONFIG_KEYS:
        complain(f"Unknown setting '{args.key}'. Settings you can change: {', '.join(CONFIG_KEYS)}")
        return EXIT_USAGE
    try:
        value = parse_setting(key, args.value)
    except ValueError as exc:
        complain(str(exc))
        return EXIT_USAGE
    if args.library:
        complain("Note: --library is ignored by `config set`; it changes the saved config.json.")
    # A running app keeps its settings in memory and writes config.json from there: a file
    # written behind its back would be overwritten by its next save. So it gets the change
    # instead (it validates, saves and applies it at once) and only without one is the file
    # written here.
    url = _running_app(args)
    if url:
        return _config_set_on_running_app(url, key, value)
    return _config_set_in_file(key, value)


def _config_set_on_running_app(url: str, key: str, value: Any) -> int:
    try:
        answer = call_running_app(url, "PUT", "api/settings", {key: api_value(key, value)})
    except RunningAppError as exc:
        complain(f"The running app at {url} did not accept the change: {exc}")
        return EXIT_USAGE if exc.status in (400, 422) else EXIT_FAILURE
    except OSError as exc:
        complain(
            f"Ultimate Playlist is running at {url} but did not answer ({exc}). Nothing was "
            "changed; try again, or use the Settings dialog."
        )
        return EXIT_FAILURE
    shown = answer.get(key, value) if isinstance(answer, dict) else value
    say(f"{key} = {format_setting(shown)}")
    say(f"Saved and applied by the running app at {url}.")
    return EXIT_OK


def _config_set_in_file(key: str, value: Any) -> int:
    settings = Settings.load()  # the file, not a --library override that must not be persisted
    setattr(settings, key, value)
    try:
        settings.save()
    except OSError as exc:
        complain(f"Could not save the settings: {exc}")
        return EXIT_FAILURE
    from . import providers

    providers.configure(settings)
    say(f"{key} = {format_setting(value)}")
    return EXIT_OK


def _running_app(args: argparse.Namespace) -> str | None:
    """The running copy `config set` / `spotify` talk to: the one on `--port` when given (an
    app started with `serve --port N`), else one on 8765 or a port serve() falls back to."""
    port = getattr(args, "port", None)
    if port:
        return running_instance(DEFAULT_HOST, port)
    return find_running_instance(DEFAULT_HOST, DEFAULT_PORT)


def _redirect_uri(spotify_auth: Any, running_url: str | None, port: int | None = None) -> str:
    """The Redirect URI for the running app's port, else for `port` (--port), else 8765."""
    running_port = urlsplit(running_url).port if running_url else None
    return str(spotify_auth.redirect_uri(running_port or port or DEFAULT_PORT))


def cmd_spotify(args: argparse.Namespace) -> int:
    from .providers import spotify_auth

    settings = Settings.load()
    url = _running_app(args)
    port = getattr(args, "port", None)
    if args.spotify_command == "status":
        return _spotify_status(spotify_auth, settings, url, port)
    if args.spotify_command == "login":
        return _spotify_login(spotify_auth, settings, url, port)
    return _spotify_logout(spotify_auth, url)


def _spotify_status(
    spotify_auth: Any, settings: Settings, url: str | None, port: int | None = None
) -> int:
    """The connection state, from config.json and the saved sign-in. Never a token."""
    client_id = settings.spotify_client_id
    say(f"Client ID:    {client_id or '(not set)'}")
    # A sign-in made through another Client ID does not count (spotify_auth binds them).
    account = spotify_auth.current_account(client_id) if client_id else None
    if account is not None:
        name = account.display_name or account.user_id
        say(f"Connection:   connected as {name} (Spotify user {account.user_id})")
        say(f"Access:       {account.scope or '(none)'}")
    elif client_id:
        say("Connection:   not connected (open Settings in the app and click Connect Spotify)")
    else:
        say("Connection:   not connected (Spotify links use Spotify's public pages)")
    note = f" (the app is running at {url})" if url else ""
    say(f"Redirect URI: {_redirect_uri(spotify_auth, url, port)}{note}")
    return EXIT_OK


def _spotify_login(
    spotify_auth: Any, settings: Settings, url: str | None, port: int | None = None
) -> int:
    start = "double-click UltimatePlaylist.exe" if is_frozen() else f"run `{run_hint()}`"
    steps: list[str] = []
    if not settings.spotify_client_id:
        steps.append(
            f"Create a free app at {SPOTIFY_DASHBOARD_URL} (the app's owner needs Spotify "
            f"Premium): add the Redirect URI {_redirect_uri(spotify_auth, url, port)}, tick "
            "Web API, save."
        )
        steps.append(f"Run `{run_hint()} config set spotify_client_id <its Client ID>`.")
    steps.append(f"Start the app ({start}) if it is not running yet.")
    steps.append("Open Settings (the gear icon at the top right) and click Connect Spotify.")
    steps.append("Allow access on Spotify's page; you land back in the app, connected.")
    say("Connecting Spotify happens in your browser:")
    for number, step in enumerate(steps, 1):
        say(f"  {number}. {step}")
    if url and settings.spotify_client_id:
        login = url + "api/spotify/login"
        say(f"The app is running: opening {login} in your browser.")
        webbrowser.open(login)
    return EXIT_OK


def _spotify_logout(spotify_auth: Any, url: str | None) -> int:
    if url:  # let the running app do it, so nothing it holds in memory outlives the sign-in
        try:
            call_running_app(url, "POST", "api/spotify/logout")
        except (RunningAppError, OSError) as exc:
            complain(f"The running app at {url} could not disconnect Spotify: {exc}")
            return EXIT_FAILURE
        say("Disconnected from Spotify: the saved sign-in was deleted.")
        return EXIT_OK
    if spotify_auth.logout():
        say("Disconnected from Spotify: the saved sign-in was deleted.")
    else:
        say("Spotify was not connected; nothing to do.")
    return EXIT_OK


def cmd_rescan(args: argparse.Namespace) -> int:
    from .playlists import PlaylistStore

    settings = load_settings(args.library)
    library = Library(settings.library_dir, settings.index_path)
    try:
        changes = library.rescan()
    except LibraryUnavailable as exc:
        complain(str(exc))
        return EXIT_FAILURE
    pruned = 0
    if len(library):  # an empty folder is a wrong folder more often than a wiped library
        pruned = PlaylistStore(settings.playlists_path).prune({t.id for t in library.all()})
    say(f"Rescan done: {changes} change(s), {len(library)} track(s) in {settings.library_dir}")
    if pruned:
        say(f"Removed {pruned} missing track reference(s) from playlists.")
    return EXIT_OK


def cmd_playlists(args: argparse.Namespace) -> int:
    from .playlists import PlaylistStore

    settings = load_settings(args.library)
    playlists = PlaylistStore(settings.playlists_path).all()
    if not playlists:
        say("No playlists yet. Create one in the web app (`up`).")
        return EXIT_OK
    rows = [(p.id, shorten(p.name, 50), str(len(p.track_ids))) for p in playlists]
    print_table(("id", "name", "tracks"), rows)
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    from .playlists import PlaylistStore, export_m3u8
    from .server.app import safe_playlist_filename

    settings = load_settings(args.library)
    store = PlaylistStore(settings.playlists_path)
    try:
        playlist = find_playlist(store.all(), args.playlist)
    except ValueError as exc:
        complain(str(exc))
        return EXIT_FAILURE
    library = Library(settings.library_dir, settings.index_path)
    if args.out:
        out_path = Path(args.out).expanduser()
        if out_path.is_dir():
            out_path = out_path / f"{safe_playlist_filename(playlist.name)}.m3u8"
    else:
        out_path = settings.playlists_export_dir / f"{safe_playlist_filename(playlist.name)}.m3u8"
    try:
        inside_library = out_path.resolve().is_relative_to(settings.library_dir.resolve())
    except OSError:
        inside_library = False
    relative_to = out_path.parent if inside_library else None
    try:
        export_m3u8(playlist, library, out_path, relative_to=relative_to)
    except OSError as exc:
        complain(f"Could not write {out_path}: {exc}")
        return EXIT_FAILURE
    present = sum(1 for tid in playlist.track_ids if library.get(tid) is not None)
    missing = len(playlist.track_ids) - present
    kind = "relative" if relative_to else "absolute"
    say(f"Exported '{playlist.name}' ({present} track(s), {kind} paths) to {out_path}")
    if missing:
        say(f"Skipped {missing} track(s) that are no longer in the library.")
    return EXIT_OK


# -- parser ------------------------------------------------------------------------------------


def prog_name() -> str:
    """`up` from source; the exe's own file name in the packaged build (there is no `up` there)."""
    return Path(sys.executable).name if is_frozen() else "up"


def run_hint() -> str:
    """The command as a user types it: `uv run up` in a source checkout (the README's
    spelling; a bare `up` is only on PATH inside an activated venv), the exe's name otherwise."""
    return prog_name() if is_frozen() else "uv run up"


def _port_number(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("a port is a number from 1 to 65535")
    return port


def _add_app_port(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """`--port` for the commands that talk to a running app (`config set`, `spotify ...`)."""
    parser.add_argument(
        "--port",
        type=_port_number,
        default=None,
        help="the port of the running app, if you started it with `serve --port` "
        f"(default: look on {DEFAULT_PORT} and the next {PORT_ATTEMPTS - 1})",
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog_name(),
        description="Ultimate Playlist: paste links, get tagged MP3s, build playlists.",
        epilog="Run without a subcommand to start the web app.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--library", metavar="DIR", help="use this library folder instead of the configured one"
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    serve = sub.add_parser("serve", help="start the web app (default)")
    serve.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})"
    )
    serve.add_argument(
        "--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})"
    )
    serve.add_argument("--no-browser", action="store_true", help="do not open the browser")
    serve.set_defaults(func=cmd_serve)

    add = sub.add_parser("add", help="download one or more links into the library")
    add.add_argument("urls", metavar="URL", nargs="+", help="track or playlist links")
    add.add_argument(
        "-q",
        "--quiet",
        dest="quiet",
        action="store_true",
        help="print only the final summary instead of a line per status change "
        "(the command still waits for every download to finish)",
    )
    add.add_argument("--no-wait", dest="quiet", action="store_true", help=argparse.SUPPRESS)
    add.set_defaults(func=cmd_add)

    lst = sub.add_parser("list", help="list the tracks in the library")
    lst.add_argument("-q", "--query", help="filter by title, artist or album")
    lst.set_defaults(func=cmd_list)

    sub.add_parser(
        "doctor", help="check ffmpeg, the JavaScript runtime and providers"
    ).set_defaults(func=cmd_doctor)

    config = sub.add_parser("config", help="show or change the saved settings (config.json)")
    config_sub = config.add_subparsers(dest="config_command", metavar="ACTION")
    config_sub.required = True
    config_sub.add_parser("show", help="print every setting").set_defaults(func=cmd_config)
    config_set = config_sub.add_parser(
        "set",
        help="change one setting, e.g. `config set concurrency 3`",
        description="Change one setting in config.json (through the app when it is running, "
        'so it takes effect at once). Text settings are cleared with an empty value (""). '
        "The library folder is created if it does not exist.",
    )
    config_set.add_argument("key", metavar="KEY", help=f"one of: {', '.join(CONFIG_KEYS)}")
    config_set.add_argument(
        "value",
        metavar="VALUE",
        help="the new value (true/false for embed_cover, a comma-separated list for js_runtimes)",
    )
    _add_app_port(config_set)
    config_set.set_defaults(func=cmd_config)

    spotify = sub.add_parser(
        "spotify",
        help="connect your Spotify account (status, login, logout)",
        description="Connecting a Spotify account lets the app read your own playlists of any "
        "size and your Liked Songs. It needs your own free Spotify developer app "
        "(`config set spotify_client_id`) and happens in the browser.",
    )
    spotify_sub = spotify.add_subparsers(dest="spotify_command", metavar="ACTION")
    spotify_sub.required = True
    for action, text in (
        ("status", "show whether a Spotify account is connected"),
        ("login", "how to connect (opens the running app's Spotify sign-in)"),
        ("logout", "disconnect and delete the saved Spotify sign-in"),
    ):
        _add_app_port(spotify_sub.add_parser(action, help=text)).set_defaults(func=cmd_spotify)

    sub.add_parser("rescan", help="sync the index with the library folder").set_defaults(
        func=cmd_rescan
    )
    sub.add_parser("playlists", help="list playlists").set_defaults(func=cmd_playlists)

    export = sub.add_parser("export", help="write a playlist as an M3U8 file")
    export.add_argument("playlist", metavar="PLAYLIST", help="playlist name or id")
    export.add_argument(
        "out",
        metavar="OUT_PATH",
        nargs="?",
        help="output file (default: <library>/Playlists/<name>.m3u8 with relative paths)",
    )
    export.set_defaults(func=cmd_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `up`, `ultimate-playlist` and `python -m ultimate_playlist`."""
    _ensure_unicode_stdout()
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:  # --help / --version / usage errors
        code = exc.code
        return code if isinstance(code, int) else (EXIT_OK if code is None else EXIT_FAILURE)
    if args.command is None:
        args.command = "serve"
        args.port, args.host, args.no_browser = DEFAULT_PORT, DEFAULT_HOST, False
        args.func = cmd_serve
    if args.command != "serve":
        _configure_cli_logging()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        complain("Interrupted.")
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
