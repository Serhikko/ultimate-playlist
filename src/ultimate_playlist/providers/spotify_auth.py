"""Connect Spotify: Authorization Code with PKCE for the user's own developer app.

Since March 2026 the Spotify Web API only returns the contents of playlists the signed-in user
owns or collaborates on, and a Client Credentials token (an app without a user) sees no playlist
contents at all. So instead of a client secret the user connects their account once:

1. `begin_login(client_id, redirect)` returns the ``accounts.spotify.com/authorize`` URL for the
   user's own developer app (Client ID only, PKCE with S256, no secret anywhere) and remembers
   ``state -> code_verifier`` in memory for ten minutes.
2. Spotify sends the browser back to ``http://127.0.0.1:<port>/api/spotify/callback`` (loopback
   redirect URIs must use an explicit IP, see `redirect_uri`), and `finish_login` exchanges the
   code for tokens, asks ``GET /v1/me`` who signed in and stores the result.
3. `access_token(client_id)` hands out a valid access token, renewing it with the refresh token
   when it is about to expire.

The tokens live in ``app_data_dir() / "spotify_auth.json"`` (written atomically) and are only
read back through `current_account()` (which never exposes them) and `access_token()`. They are
bound to the Client ID they were issued for: with another Client ID configured the user is not
connected. Nothing in this module logs, prints or returns a token; `SpotifyAccount` has none.
HTTP goes through `_session` (default: the `requests` module) so tests can hand in a fake.

The running app and an `up add` / `up doctor` in a terminal share the token file, and Spotify
rotates the refresh token on every renewal, so a renewal runs under a thread lock *and* an OS
lock on ``spotify_auth.lock`` (released by the OS if the process dies). A renewal Spotify
refuses only deletes the file when the file still holds the refused refresh token; when another
process has renewed meanwhile, its result is used instead.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from ..config import app_data_dir
from .base import DownloadCancelled, ProviderError

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

log = logging.getLogger(__name__)

SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"
CALLBACK_PATH = "/api/spotify/callback"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
ME_URL = "https://api.spotify.com/v1/me"
TOKEN_FILE_NAME = "spotify_auth.json"
LOCK_FILE_NAME = "spotify_auth.lock"  # empty; only its OS lock matters
LOCK_WAIT = 15.0  # seconds to wait for another process's renewal (one HTTP timeout and then some)
LOCK_POLL = 0.05
HTTP_TIMEOUT = 10.0
PENDING_TTL = 600.0  # seconds a login link stays valid
VERIFIER_BYTES = 48  # secrets.token_urlsafe(48) is exactly 64 URL-safe characters
EXPIRY_MARGIN = 60.0  # renew the access token this long before Spotify says it expires
DEFAULT_EXPIRES_IN = 3600.0

CANCELLED_MESSAGE = "Spotify login was cancelled"
EXPIRED_LINK_MESSAGE = "That login link has expired, click Connect Spotify again"
REJECTED_MESSAGE = (
    "Spotify rejected the login: check that the app's Redirect URI is exactly {redirect} "
    "and that the Client ID is right"
)
NOT_ALLOWED_MESSAGE = (
    "This Spotify account is not allowed to use that developer app (add it under User "
    "Management in the Spotify dashboard)"
)
EXPIRED_CONNECTION_MESSAGE = "Spotify connection expired, connect again in Settings"
NO_CLIENT_ID_MESSAGE = (
    "Enter the Client ID of your Spotify developer app in Settings first, then click "
    "Connect Spotify."
)
NO_CODE_MESSAGE = "Spotify did not send a login code. Click Connect Spotify again."
UNREADABLE_MESSAGE = "Spotify sent an answer we could not read. Click Connect Spotify again."
UNREACHABLE_MESSAGE = "Could not reach Spotify. Check your internet connection and try again."
RATE_LIMIT_MESSAGE = "Spotify is rate-limiting us, try again in a minute."

# Injectable for tests: anything with requests-style ``get``/``post``. Expiry times use the wall
# clock because they are stored in the token file and must survive a restart.
_session: Any = None
_clock = time.time


class SpotifyAuthError(ProviderError):
    """A Spotify login / connection problem; the message is written for the user."""


@dataclass(frozen=True)
class SpotifyAccount:
    """Who is connected. Deliberately carries no token, so it is safe to show and to log."""

    user_id: str
    display_name: str
    scope: str
    expires_at: float  # when the current access token expires (Unix time)


@dataclass
class _PendingLogin:
    code_verifier: str = field(repr=False)
    redirect: str
    client_id: str
    created: float


_pending: dict[str, _PendingLogin] = {}
_pending_lock = threading.Lock()
_token_lock = threading.RLock()
# A renewal whose write failed (a full disk, a virus scanner holding the file). Spotify may have
# rotated the refresh token, which leaves the one in the file dead, so the renewed record is
# kept here and preferred over the older file record until a write succeeds.
_unsaved: dict[str, Any] | None = None


# --------------------------------------------------------------------------------------------
# PKCE helpers
# --------------------------------------------------------------------------------------------


def redirect_uri(port: int) -> str:
    """The loopback redirect URI to register in the Spotify dashboard (an IP, not localhost)."""
    return f"http://127.0.0.1:{int(port)}{CALLBACK_PATH}"


def make_code_verifier() -> str:
    """64 random URL-safe characters (RFC 7636 allows 43-128 of ``[A-Za-z0-9-._~]``)."""
    return secrets.token_urlsafe(VERIFIER_BYTES)


def code_challenge(verifier: str) -> str:
    """``base64url(sha256(verifier))`` without padding (the S256 method)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _http() -> Any:
    return _session if _session is not None else requests


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _seconds(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


# --------------------------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------------------------


def _prune_pending(now: float) -> None:
    for state in [s for s, p in _pending.items() if now - p.created > PENDING_TTL]:
        del _pending[state]


def _take_pending(state: str) -> _PendingLogin | None:
    """The pending login for `state`, removed so it works once; None if unknown or expired."""
    now = _clock()
    with _pending_lock:
        _prune_pending(now)
        return _pending.pop(state, None) if state else None


def begin_login(client_id: str, redirect: str) -> str:
    """The URL that asks the user to allow access; its PKCE verifier is kept for 10 minutes."""
    client_id = _text(client_id)
    if not client_id:
        raise SpotifyAuthError(NO_CLIENT_ID_MESSAGE)
    verifier = make_code_verifier()
    state = secrets.token_urlsafe(24)
    now = _clock()
    with _pending_lock:
        _prune_pending(now)
        _pending[state] = _PendingLogin(verifier, redirect, client_id, now)
    query = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect,
        "state": state,
        "scope": SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge(verifier),
    }
    log.info("Starting a Spotify login (redirect %s)", redirect)
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def _post_token(form: dict[str, str], network_error: type[ProviderError]) -> tuple[int, Any]:
    """POST the token endpoint: (status, parsed JSON or {}). Never logs the form or the body."""
    try:
        resp = _http().post(
            TOKEN_URL,
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        log.debug("Spotify token request failed: %s", exc.__class__.__name__)
        raise network_error(UNREACHABLE_MESSAGE) from None
    status = int(getattr(resp, "status_code", 0) or 0)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


def _fetch_me(access: str) -> tuple[str, str]:
    """(user id, display name) of the account the access token belongs to."""
    try:
        resp = _http().get(
            ME_URL, headers={"Authorization": f"Bearer {access}"}, timeout=HTTP_TIMEOUT
        )
    except requests.RequestException as exc:
        log.debug("Spotify /me request failed: %s", exc.__class__.__name__)
        raise SpotifyAuthError(UNREACHABLE_MESSAGE) from None
    status = int(getattr(resp, "status_code", 0) or 0)
    if status == 403:
        # Development-mode apps only admit the accounts listed under User Management.
        log.warning("Spotify refused GET /me (HTTP 403): the account is not allowlisted")
        raise SpotifyAuthError(NOT_ALLOWED_MESSAGE)
    if status != 200:
        log.warning("Spotify answered GET /me with HTTP %s", status)
        raise SpotifyAuthError(
            f"Spotify accepted the login but would not say which account it is (HTTP {status}). "
            "Click Connect Spotify again."
        )
    try:
        me = resp.json()
    except ValueError:
        me = None
    if not isinstance(me, dict):
        raise SpotifyAuthError(UNREADABLE_MESSAGE)
    user_id = _text(me.get("id")) or _text(me.get("account_id"))
    if not user_id:
        raise SpotifyAuthError(UNREADABLE_MESSAGE)
    return user_id, _text(me.get("display_name")) or user_id


def finish_login(state: str, code: str | None, error: str | None) -> SpotifyAccount:
    """Handle the redirect back from Spotify: check `state`, trade the code for tokens, save them.

    Raises SpotifyAuthError with a message for the user when the login was cancelled, the link
    is unknown or older than ten minutes (every link works once), the token endpoint refuses,
    or the account is not allowed to use the developer app.
    """
    pending = _take_pending(_text(state))
    if error:
        log.info("Spotify login ended with error %r", str(error)[:80])
        if error == "access_denied":
            raise SpotifyAuthError(CANCELLED_MESSAGE)
        if pending is None:
            raise SpotifyAuthError(EXPIRED_LINK_MESSAGE)
        raise SpotifyAuthError(REJECTED_MESSAGE.format(redirect=pending.redirect))
    if pending is None:
        raise SpotifyAuthError(EXPIRED_LINK_MESSAGE)
    if not code:
        raise SpotifyAuthError(NO_CODE_MESSAGE)

    status, body = _post_token(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect,
            "client_id": pending.client_id,
            "code_verifier": pending.code_verifier,
        },
        SpotifyAuthError,
    )
    if status != 200:
        reason = _text(body.get("error"))
        description = _text(body.get("error_description"))
        log.warning(
            "Spotify refused the login code: HTTP %s %s %s", status, reason, description[:80]
        )
        if reason.lower() == "invalid_client" or "redirect" in description.lower():
            raise SpotifyAuthError(REJECTED_MESSAGE.format(redirect=pending.redirect))
        if reason == "invalid_grant":
            raise SpotifyAuthError(EXPIRED_LINK_MESSAGE)
        raise SpotifyAuthError(
            f"Spotify refused the login (HTTP {status}). Click Connect Spotify again."
        )
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise SpotifyAuthError(UNREADABLE_MESSAGE)
    refresh = body.get("refresh_token")
    expires_at = _clock() + _seconds(body.get("expires_in"), DEFAULT_EXPIRES_IN)
    user_id, display_name = _fetch_me(access)
    record = {
        "refresh_token": refresh if isinstance(refresh, str) else "",
        "access_token": access,
        "expires_at": expires_at,
        "scope": _text(body.get("scope")),
        "user_id": user_id,
        "display_name": display_name,
        "client_id": pending.client_id,
    }
    global _unsaved
    with _token_lock, _file_lock():
        try:
            _write_record(record)
        except OSError as exc:
            raise SpotifyAuthError(
                f"Could not save the Spotify connection ({exc.strerror or exc}). Try again."
            ) from None
        _unsaved = None
    log.info("Connected to Spotify as %s", display_name)
    return _account_from(record)


# --------------------------------------------------------------------------------------------
# The token file
# --------------------------------------------------------------------------------------------


def _token_path() -> Path:
    return app_data_dir() / TOKEN_FILE_NAME


def _write_record(record: dict[str, Any]) -> None:
    """Write the token file atomically (a private ``.tmp`` file, then ``os.replace``)."""
    path = _token_path()
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(record, indent=2)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _read_record() -> dict[str, Any] | None:
    path = _token_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("Could not read the Spotify connection file: %s", exc.strerror or exc)
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("Ignoring an unreadable Spotify connection file (connect again in Settings)")
        return None
    if not isinstance(data, dict) or not _text(data.get("client_id")):
        return None
    if not _text(data.get("user_id")):
        return None
    for key in ("refresh_token", "access_token", "scope", "display_name"):
        if not isinstance(data.get(key), str):
            data[key] = ""
    data["expires_at"] = _seconds(data.get("expires_at"), 0.0)
    return data


def _delete_record() -> bool:
    try:
        _token_path().unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Could not delete the Spotify connection file: %s", exc.strerror or exc)
        return False
    return True


def _load() -> dict[str, Any] | None:
    """The token record: the file's, or a newer renewal this process could not save yet."""
    global _unsaved
    record = _read_record()
    pending = _unsaved
    if pending is None:
        return record
    if (
        record is not None
        and record["client_id"] == pending["client_id"]
        and record["user_id"] == pending["user_id"]
        and pending["expires_at"] > record["expires_at"]
    ):
        try:
            _write_record(pending)
        except OSError:
            pass
        else:
            _unsaved = None
        return dict(pending)
    _unsaved = None  # disconnected, connected anew or renewed elsewhere since: the file wins
    return record


def _lock_fd(fd: int) -> None:
    """Take the OS lock on `fd` without waiting; OSError while another process holds it."""
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def _file_lock() -> Iterator[None]:
    """Keep other processes out of the token file while this one renews, saves or deletes it.

    The lock belongs to the OS (on ``spotify_auth.lock``), so a process that dies releases it.
    Best effort: when the lock file cannot be opened, or another process holds the lock for
    more than LOCK_WAIT seconds, the work goes ahead without it.
    """
    try:
        fd: int | None = os.open(app_data_dir() / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        log.debug("Could not open the Spotify lock file: %s", exc.strerror or exc)
        fd = None
    if fd is None:
        yield
        return
    locked = False
    deadline = time.monotonic() + LOCK_WAIT
    try:
        while True:
            try:
                _lock_fd(fd)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    log.warning("Another copy of the app kept the Spotify connection locked")
                    break
                time.sleep(LOCK_POLL)
        yield
    finally:
        if locked:
            with contextlib.suppress(OSError):
                _unlock_fd(fd)
        os.close(fd)


def _account_from(record: dict[str, Any]) -> SpotifyAccount:
    user_id = _text(record.get("user_id"))
    return SpotifyAccount(
        user_id=user_id,
        display_name=_text(record.get("display_name")) or user_id,
        scope=_text(record.get("scope")),
        expires_at=float(record.get("expires_at") or 0.0),
    )


def current_account(client_id: str | None = None) -> SpotifyAccount | None:
    """The connected account, or None when not connected.

    With `client_id` (the one configured in Settings) an account connected through a different
    developer app counts as not connected; without it the stored account is returned whatever
    app it came from.
    """
    with _token_lock:
        record = _read_record()
    if record is None:
        return None
    if client_id is not None and record["client_id"].strip() != _text(client_id):
        return None
    return _account_from(record)


def access_token(
    client_id: str,
    cancel: threading.Event | None = None,
    *,
    stale_token: str | None = None,
) -> str | None:
    """A valid access token for `client_id`; None when not connected (or connected elsewhere).

    Renews the token (``grant_type=refresh_token``) when it expires within a minute, or when
    `stale_token` is still the stored token (the API just answered 401 for it; a token another
    thread or process renewed meanwhile is handed out as it is). Renewals are serialised across
    threads and processes. A refresh token Spotify rejects deletes the stored connection and
    raises SpotifyAuthError; a network problem raises a plain ProviderError and keeps it.
    """
    client_id = _text(client_id)
    if not client_id:
        return None
    with _token_lock:
        record = _load()
        if record is None or record["client_id"].strip() != client_id:
            return None
        if _usable(record, stale_token):
            return record["access_token"]
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled("Download cancelled")
        with _file_lock():
            record = _load()  # another process may have renewed it while we waited
            if record is None or record["client_id"].strip() != client_id:
                return None
            if _usable(record, stale_token):
                return record["access_token"]
            return _refresh(record)


def _is_fresh(record: dict[str, Any]) -> bool:
    return bool(record["access_token"]) and record["expires_at"] - EXPIRY_MARGIN > _clock()


def _usable(record: dict[str, Any], stale_token: str | None) -> bool:
    return _is_fresh(record) and (stale_token is None or record["access_token"] != stale_token)


def _refresh(record: dict[str, Any], *, retry: bool = True) -> str:
    global _unsaved
    refresh = record["refresh_token"]
    if not refresh:
        _delete_record()
        _unsaved = None
        raise SpotifyAuthError(EXPIRED_CONNECTION_MESSAGE)
    status, body = _post_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": record["client_id"],
        },
        ProviderError,
    )
    if status in (400, 401, 403):
        # Look again before disconnecting: another process (the app and an `up` command share
        # the file) may have renewed with this refresh token first, and Spotify then rotated it.
        current = _read_record()
        if (
            current is not None
            and current["client_id"] == record["client_id"]
            and current["refresh_token"]
            and current["refresh_token"] != refresh
        ):
            log.info("The Spotify connection file holds a newer sign-in; using that")
            if _is_fresh(current):
                return str(current["access_token"])
            if retry:
                return _refresh(current, retry=False)
        log.warning(
            "Spotify refused to renew the connection (HTTP %s %s); disconnected",
            status,
            _text(body.get("error")),
        )
        if current is not None and current["refresh_token"] == refresh:
            _delete_record()  # only the refused sign-in, never one another process just saved
        _unsaved = None
        raise SpotifyAuthError(EXPIRED_CONNECTION_MESSAGE)
    if status == 429:
        raise ProviderError(RATE_LIMIT_MESSAGE)
    if status != 200:
        raise ProviderError(
            f"Spotify's login service answered with HTTP {status}. Try again later."
        )
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise ProviderError("Spotify sent an unreadable answer while renewing the connection.")
    record["access_token"] = access
    record["expires_at"] = _clock() + _seconds(body.get("expires_in"), DEFAULT_EXPIRES_IN)
    record["scope"] = _text(body.get("scope")) or record["scope"]
    new_refresh = body.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh:
        record["refresh_token"] = new_refresh  # Spotify may rotate it; the old one then dies
    try:
        _write_record(record)
    except OSError as exc:
        log.warning("Could not save the renewed Spotify connection: %s", exc.strerror or exc)
        _unsaved = dict(record)  # the file may hold a refresh token that no longer works
    else:
        _unsaved = None
    log.debug("Renewed the Spotify access token")
    return access


def logout() -> bool:
    """Forget the connection (delete the token file). True when there was one."""
    global _unsaved
    with _token_lock:
        _unsaved = None
        if not _token_path().exists():
            return False
        with _file_lock():
            removed = _delete_record()
    if removed:
        log.info("Disconnected from Spotify")
    return removed
