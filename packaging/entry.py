"""PyInstaller entry point for the Windows no-install build.

Frozen with packaging/ultimate-playlist.spec into dist/UltimatePlaylist/UltimatePlaylist.exe.
Behaves exactly like `up`: no arguments starts the web app and opens the browser.

A double-clicked exe owns its console window, and Windows closes that window the instant the
process exits, so a startup failure (a port problem, a broken config folder, a missing DLL) would
flash by unread. The wrapper below prints one line and waits for Enter in that case only; the
success path is untouched, so closing the window still stops the server at once.
"""

from __future__ import annotations

import logging
import multiprocessing
import sys
import traceback


def owns_console() -> bool:
    """True when this process is the only one attached to its console (started by double-click).

    From a terminal the shell is attached too, so the list has more than one entry and the
    prompt to press Enter is skipped: the user can already read the output.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        pids = (ctypes.c_uint * 2)()
        return kernel32.GetConsoleProcessList(pids, 2) == 1
    except Exception:  # noqa: BLE001 - if in doubt, do not pause
        return False


def pause_if_own_window() -> None:
    if not owns_console():
        return
    try:
        input("Press Enter to close this window.")
    except (EOFError, OSError, KeyboardInterrupt):
        pass


def report_crash(exc: BaseException) -> None:
    """The traceback, then one readable line. Nothing here may raise.

    When serve() got as far as installing the log handlers, logging the exception puts the
    traceback both on the console and in app.log; before that point there is no app.log to
    point at, so the traceback goes straight to stderr (the window pauses, so it can be read).
    """
    try:
        if logging.getLogger().handlers:
            logging.getLogger("ultimate_playlist.entry").exception(
                "Ultimate Playlist could not start"
            )
        else:
            traceback.print_exc()
    except Exception:  # noqa: BLE001 - a broken log handler must not hide the message below
        traceback.print_exc()
    print(f"Ultimate Playlist could not start: {exc}", file=sys.stderr, flush=True)
    print(
        "The full error is printed above. app.log (in %USERPROFILE%\\.ultimate-playlist, or the "
        "folder named by ULTIMATE_PLAYLIST_HOME) has more when the app got that far.",
        file=sys.stderr,
        flush=True,
    )


def run() -> int:
    from ultimate_playlist.cli import main

    try:
        code = int(main())
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:  # noqa: BLE001 - the last line the user sees must be readable
        report_crash(exc)
        code = 1
    if code not in (0, 130):
        pause_if_own_window()
    return code


if __name__ == "__main__":
    # The app only uses threads, but a frozen Windows exe that ever spawns a child interpreter
    # would otherwise re-run this script in the child; the guard is free and standard.
    multiprocessing.freeze_support()
    sys.exit(run())
