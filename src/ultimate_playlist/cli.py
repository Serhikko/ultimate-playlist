"""Command line interface (`up`). No subcommand starts the web app."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .bundled import is_frozen
from .config import Settings, app_data_dir
from .ffmpeg import find_ffmpeg, install_hint
from .library import Library, LibraryUnavailable
from .models import JobStatus, Playlist

log = logging.getLogger(__name__)

OK_MARK = "✓"
BAD_MARK = "✗"
DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
# The ports serve() may end up on: the requested one plus the next ten. Kept equal to
# server.app.PORT_ATTEMPTS (test_cli guards that) instead of imported, so the CLI does not load
# FastAPI for `up list`.
PORT_ATTEMPTS = 11
JOIN_TIMEOUT = 3.0  # seconds to wait for /api/version; a closed port refuses at once
JOIN_PAUSE = 1.5  # seconds the "already running" line stays readable before the window closes
POLL_INTERVAL = 0.2  # seconds between progress checks in `add`
EXIT_OK, EXIT_FAILURE, EXIT_INTERRUPTED = 0, 1, 130

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


def find_running_instance(host: str, port: int, attempts: int = PORT_ATTEMPTS) -> str | None:
    """The running copy on any port serve() could have chosen: `port` or the next free ones.

    When a foreign program holds 8765 the first double-click serves on 8766; the second must
    find it there instead of starting a third server. A closed port refuses the connection in
    milliseconds, so the scan costs nothing on a normal start.
    """
    for candidate in range(port, port + attempts):
        url = running_instance(host, candidate)
        if url:
            return url
    return None


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
    from .server.app import serve

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
