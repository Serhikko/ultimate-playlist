"""Offline tests for spotify_auth: PKCE, the login round trip, the token file and renewals.

HTTP goes through the FakeSession from test_spotify_meta; the token file lives in the per-test
app data folder (conftest's `isolated_home`), and the clock is frozen. Tokens must never show
up in an account object, an error message or the log.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from test_spotify_meta import FakeResponse, FakeSession

from ultimate_playlist.config import app_data_dir
from ultimate_playlist.providers import spotify_auth as sa
from ultimate_playlist.providers.base import DownloadCancelled, ProviderError

CLIENT_ID = "0123456789abcdef0123456789abcdef"
REDIRECT = sa.redirect_uri(8781)
ACCESS = "access-token-SECRET-1"
REFRESH = "refresh-token-SECRET-1"
NEW_ACCESS = "access-token-SECRET-2"
NEW_REFRESH = "refresh-token-SECRET-2"
SECRETS = (ACCESS, REFRESH, NEW_ACCESS, NEW_REFRESH)
NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, float]]:
    """A frozen, adjustable clock; no pending logins leak between tests."""
    state = {"now": NOW}
    monkeypatch.setattr(sa, "_clock", lambda: state["now"])
    monkeypatch.setattr(sa, "_session", None)
    monkeypatch.setattr(sa, "_unsaved", None)
    sa._pending.clear()
    yield state
    sa._pending.clear()


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    fake = FakeSession()
    monkeypatch.setattr(sa, "_session", fake)
    return fake


def token_ok(
    access: str = ACCESS, refresh: str | None = REFRESH, expires_in: int = 3600
) -> FakeResponse:
    body: dict[str, Any] = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": sa.SCOPES,
    }
    if refresh is not None:
        body["refresh_token"] = refresh
    return FakeResponse(json_data=body)


def me_ok() -> FakeResponse:
    return FakeResponse(json_data={"id": "listener42", "display_name": "Test Listener"})


def start_login() -> tuple[str, dict[str, str]]:
    url = sa.begin_login(CLIENT_ID, REDIRECT)
    query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    return url, query


def token_file() -> Path:
    return app_data_dir() / sa.TOKEN_FILE_NAME


def store(**over: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "refresh_token": REFRESH,
        "access_token": ACCESS,
        "expires_at": NOW + 3600,
        "scope": sa.SCOPES,
        "user_id": "listener42",
        "display_name": "Test Listener",
        "client_id": CLIENT_ID,
    }
    record.update(over)
    token_file().write_text(json.dumps(record), encoding="utf-8")
    return record


def stored() -> dict[str, Any]:
    return json.loads(token_file().read_text(encoding="utf-8"))


def assert_no_secrets(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


# ----------------------------------------------------------------------------------------------
# PKCE and the authorize URL
# ----------------------------------------------------------------------------------------------


def test_redirect_uri_uses_the_loopback_ip() -> None:
    assert sa.redirect_uri(8781) == "http://127.0.0.1:8781/api/spotify/callback"
    assert sa.CALLBACK_PATH == "/api/spotify/callback"
    assert "localhost" not in sa.redirect_uri(8782)


def test_code_verifier_and_challenge() -> None:
    verifier = sa.make_code_verifier()
    assert re.fullmatch(r"[A-Za-z0-9_-]{64}", verifier)
    assert sa.make_code_verifier() != verifier
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    challenge = sa.code_challenge(verifier)
    assert challenge == expected and len(challenge) == 43 and "=" not in challenge
    # the worked example from RFC 7636, appendix B
    assert (
        sa.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_begin_login_builds_the_authorize_url() -> None:
    url, query = start_login()
    assert url.startswith(sa.AUTHORIZE_URL + "?")
    assert query["client_id"] == CLIENT_ID
    assert query["response_type"] == "code"
    assert query["redirect_uri"] == REDIRECT
    assert query["scope"] == sa.SCOPES
    assert query["code_challenge_method"] == "S256"
    assert "client_secret" not in query and "code_verifier" not in query
    pending = sa._pending[query["state"]]
    assert len(query["state"]) >= 32
    assert sa.code_challenge(pending.code_verifier) == query["code_challenge"]
    assert pending.code_verifier not in url and pending.code_verifier not in repr(pending)
    assert (pending.client_id, pending.redirect, pending.created) == (CLIENT_ID, REDIRECT, NOW)
    _, again = start_login()
    assert again["state"] != query["state"] and len(sa._pending) == 2


def test_begin_login_needs_a_client_id() -> None:
    with pytest.raises(sa.SpotifyAuthError, match="Client ID"):
        sa.begin_login("   ", REDIRECT)
    assert sa._pending == {}


# ----------------------------------------------------------------------------------------------
# finish_login
# ----------------------------------------------------------------------------------------------


def test_finish_login_exchanges_the_code_and_saves_the_tokens(
    session: FakeSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    session.add("POST", sa.TOKEN_URL, token_ok())
    session.add("GET", sa.ME_URL, me_ok())
    _, query = start_login()
    verifier = sa._pending[query["state"]].code_verifier

    account = sa.finish_login(query["state"], "the-code", None)

    assert account == sa.SpotifyAccount(
        user_id="listener42", display_name="Test Listener", scope=sa.SCOPES, expires_at=NOW + 3600
    )
    post, get = session.calls
    assert (post["method"], post["url"]) == ("POST", sa.TOKEN_URL)
    assert post["data"] == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": REDIRECT,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    }
    assert "Authorization" not in post["headers"]  # PKCE: no client secret, no Basic auth
    assert post["timeout"] == sa.HTTP_TIMEOUT
    assert (get["method"], get["url"]) == ("GET", sa.ME_URL)
    assert get["headers"]["Authorization"] == f"Bearer {ACCESS}"
    assert get["timeout"] == sa.HTTP_TIMEOUT
    assert stored() == {
        "refresh_token": REFRESH,
        "access_token": ACCESS,
        "expires_at": NOW + 3600,
        "scope": sa.SCOPES,
        "user_id": "listener42",
        "display_name": "Test Listener",
        "client_id": CLIENT_ID,
    }
    assert list(app_data_dir().glob("*.tmp")) == []  # written atomically, nothing left over
    assert sa.current_account() == account == sa.current_account(CLIENT_ID)
    assert sa._pending == {}
    assert_no_secrets(repr(account), str(account), caplog.text)


def test_account_carries_no_token() -> None:
    names = {f.name for f in dataclasses.fields(sa.SpotifyAccount)}
    assert names == {"user_id", "display_name", "scope", "expires_at"}


def test_a_login_link_works_once(session: FakeSession) -> None:
    session.add("POST", sa.TOKEN_URL, token_ok())
    session.add("GET", sa.ME_URL, me_ok())
    _, query = start_login()
    sa.finish_login(query["state"], "code", None)
    calls = len(session.calls)
    with pytest.raises(sa.SpotifyAuthError) as exc_info:
        sa.finish_login(query["state"], "code", None)
    assert str(exc_info.value) == sa.EXPIRED_LINK_MESSAGE
    assert len(session.calls) == calls  # refused before any request


def test_a_login_link_expires_after_ten_minutes(
    session: FakeSession, clock: dict[str, float]
) -> None:
    session.add("POST", sa.TOKEN_URL, token_ok())
    session.add("GET", sa.ME_URL, me_ok())
    _, old = start_login()
    clock["now"] += sa.PENDING_TTL - 5
    _, recent = start_login()
    sa.finish_login(old["state"], "code", None)  # 9 m 55 s: still fine

    _, stale = start_login()
    clock["now"] += sa.PENDING_TTL + 1
    for state in (stale["state"], recent["state"], "never-issued", ""):
        with pytest.raises(sa.SpotifyAuthError) as exc_info:
            sa.finish_login(state, "code", None)
        assert str(exc_info.value) == sa.EXPIRED_LINK_MESSAGE
    assert sa._pending == {}  # expired entries are pruned
    assert len(session.calls) == 2  # only the one good login talked to Spotify


def test_finish_login_denied(session: FakeSession) -> None:
    _, query = start_login()
    with pytest.raises(sa.SpotifyAuthError) as exc_info:
        sa.finish_login(query["state"], None, "access_denied")
    assert str(exc_info.value) == sa.CANCELLED_MESSAGE == "Spotify login was cancelled"
    with pytest.raises(sa.SpotifyAuthError, match="expired"):  # the link is used up
        sa.finish_login(query["state"], "code", None)
    with pytest.raises(sa.SpotifyAuthError) as exc_info:
        sa.finish_login("unknown", None, "access_denied")
    assert str(exc_info.value) == sa.CANCELLED_MESSAGE
    assert session.calls == [] and not token_file().exists()


def test_finish_login_other_callback_error_names_the_redirect_uri(session: FakeSession) -> None:
    _, query = start_login()
    with pytest.raises(sa.SpotifyAuthError) as exc_info:
        sa.finish_login(query["state"], None, "invalid_client")
    assert str(exc_info.value) == sa.REJECTED_MESSAGE.format(redirect=REDIRECT)
    assert REDIRECT in str(exc_info.value)
    assert session.calls == []


def test_finish_login_without_a_code(session: FakeSession) -> None:
    _, query = start_login()
    with pytest.raises(sa.SpotifyAuthError) as exc_info:
        sa.finish_login(query["state"], "", None)
    assert str(exc_info.value) == sa.NO_CODE_MESSAGE
    assert session.calls == []


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (400, {"error": "invalid_client", "error_description": "Invalid client"}, "rejected"),
        (
            400,
            {"error": "invalid_grant", "error_description": "Invalid redirect URI"},
            "rejected",
        ),
        (
            400,
            {"error": "invalid_grant", "error_description": "Invalid authorization code"},
            sa.EXPIRED_LINK_MESSAGE,
        ),
        (503, None, "Spotify refused the login (HTTP 503). Click Connect Spotify again."),
    ],
)
def test_finish_login_token_endpoint_errors(
    session: FakeSession, status: int, body: dict[str, str] | None, expected: str
) -> None:
    session.add("POST", sa.TOKEN_URL, FakeResponse(status, json_data=body))
    _, query = start_login()
    with pytest.raises(sa.SpotifyAuthError) as exc_info:
        sa.finish_login(query["state"], "code", None)
    if expected == "rejected":
        expected = sa.REJECTED_MESSAGE.format(redirect=REDIRECT)
    assert str(exc_info.value) == expected
    assert [c["method"] for c in session.calls] == ["POST"]  # /me is never asked
    assert not token_file().exists()


def test_finish_login_unreachable(session: FakeSession) -> None:
    session.add("POST", sa.TOKEN_URL, requests.ConnectionError("offline"))
    _, query = start_login()
    with pytest.raises(sa.SpotifyAuthError) as exc_info:
        sa.finish_login(query["state"], "code", None)
    assert str(exc_info.value) == sa.UNREACHABLE_MESSAGE
    assert exc_info.value.__cause__ is None and exc_info.value.__suppress_context__


def test_finish_login_account_not_allowlisted(
    session: FakeSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    session.add("POST", sa.TOKEN_URL, token_ok())
    session.add("GET", sa.ME_URL, FakeResponse(403, text="Check settings on developer dashboard"))
    _, query = start_login()
    with pytest.raises(sa.SpotifyAuthError) as exc_info:
        sa.finish_login(query["state"], "code", None)
    assert str(exc_info.value) == sa.NOT_ALLOWED_MESSAGE
    assert "User Management" in str(exc_info.value)
    assert not token_file().exists()
    assert_no_secrets(caplog.text, str(exc_info.value))


def test_finish_login_profile_error(session: FakeSession) -> None:
    session.add("POST", sa.TOKEN_URL, token_ok())
    session.add("GET", sa.ME_URL, FakeResponse(500, json_data={}))
    _, query = start_login()
    with pytest.raises(sa.SpotifyAuthError, match=r"HTTP 500"):
        sa.finish_login(query["state"], "code", None)
    assert not token_file().exists()


# ----------------------------------------------------------------------------------------------
# The token file: current_account, access_token, logout
# ----------------------------------------------------------------------------------------------


def test_not_connected(session: FakeSession) -> None:
    assert sa.current_account() is None
    assert sa.current_account(CLIENT_ID) is None
    assert sa.access_token(CLIENT_ID) is None
    assert sa.access_token("") is None
    assert session.calls == []


def test_fresh_access_token_needs_no_network(session: FakeSession) -> None:
    store()
    assert sa.access_token(CLIENT_ID) == ACCESS
    assert sa.access_token(f"  {CLIENT_ID} ") == ACCESS
    assert session.calls == []


def test_refresh_keeps_a_new_refresh_token(
    session: FakeSession, clock: dict[str, float], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store(expires_at=NOW + sa.EXPIRY_MARGIN - 1)  # about to expire: renewed now
    session.add("POST", sa.TOKEN_URL, token_ok(NEW_ACCESS, NEW_REFRESH, 1800))

    assert sa.access_token(CLIENT_ID) == NEW_ACCESS

    (post,) = session.calls
    assert post["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": REFRESH,
        "client_id": CLIENT_ID,
    }
    record = stored()
    assert record["access_token"] == NEW_ACCESS
    assert record["refresh_token"] == NEW_REFRESH
    assert record["expires_at"] == NOW + 1800
    assert sa.access_token(CLIENT_ID) == NEW_ACCESS and len(session.calls) == 1
    assert sa.current_account(CLIENT_ID).expires_at == NOW + 1800  # type: ignore[union-attr]
    assert_no_secrets(caplog.text)


def test_refresh_without_a_new_refresh_token_keeps_the_old_one(session: FakeSession) -> None:
    store(expires_at=NOW - 10)
    session.add("POST", sa.TOKEN_URL, token_ok(NEW_ACCESS, refresh=None))
    assert sa.access_token(CLIENT_ID) == NEW_ACCESS
    assert stored()["refresh_token"] == REFRESH


def test_rejected_refresh_deletes_the_connection(
    session: FakeSession, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store(expires_at=NOW - 10)
    session.add(
        "POST",
        sa.TOKEN_URL,
        FakeResponse(400, json_data={"error": "invalid_grant", "error_description": "revoked"}),
    )
    with pytest.raises(sa.SpotifyAuthError) as exc_info:
        sa.access_token(CLIENT_ID)
    assert str(exc_info.value) == sa.EXPIRED_CONNECTION_MESSAGE
    assert str(exc_info.value) == "Spotify connection expired, connect again in Settings"
    assert not token_file().exists()
    assert sa.current_account() is None and sa.access_token(CLIENT_ID) is None
    assert_no_secrets(caplog.text)


def test_stored_connection_without_refresh_token_counts_as_expired(session: FakeSession) -> None:
    store(expires_at=NOW - 10, refresh_token="")
    with pytest.raises(sa.SpotifyAuthError, match="connection expired"):
        sa.access_token(CLIENT_ID)
    assert not token_file().exists() and session.calls == []


THIRD_ACCESS = "access-token-SECRET-3"


def test_a_renewal_another_process_made_first_is_used(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The app and an `up` command share the file. When the other one renewed first, Spotify
    rotated the refresh token and refuses ours: no reason to disconnect."""
    caplog.set_level(logging.DEBUG)
    store(expires_at=NOW - 10)

    def post(url: str, **kwargs: Any) -> FakeResponse:
        session.calls.append({"method": "POST", "url": url, **kwargs})
        store(access_token=NEW_ACCESS, refresh_token=NEW_REFRESH, expires_at=NOW + 3600)
        return FakeResponse(400, json_data={"error": "invalid_grant"})

    monkeypatch.setattr(session, "post", post)
    assert sa.access_token(CLIENT_ID) == NEW_ACCESS
    assert len(session.calls) == 1
    assert stored()["refresh_token"] == NEW_REFRESH  # the other process's sign-in survives
    assert sa.current_account(CLIENT_ID) is not None
    assert_no_secrets(caplog.text)


def test_an_expired_renewal_from_another_process_is_renewed_once_more(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    store(expires_at=NOW - 10)
    answers = [
        FakeResponse(400, json_data={"error": "invalid_grant"}),
        token_ok(THIRD_ACCESS, refresh=None),
    ]

    def post(url: str, **kwargs: Any) -> FakeResponse:
        session.calls.append({"method": "POST", "url": url, **kwargs})
        if len(session.calls) == 1:  # the other process renewed, and that token is old too
            store(access_token=NEW_ACCESS, refresh_token=NEW_REFRESH, expires_at=NOW - 5)
        return answers.pop(0)

    monkeypatch.setattr(session, "post", post)
    assert sa.access_token(CLIENT_ID) == THIRD_ACCESS
    assert [c["data"]["refresh_token"] for c in session.calls] == [REFRESH, NEW_REFRESH]
    assert stored()["refresh_token"] == NEW_REFRESH
    assert stored()["access_token"] == THIRD_ACCESS


def test_a_refused_renewal_leaves_a_newer_sign_in_of_another_app_alone(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    store(expires_at=NOW - 10)

    def post(url: str, **kwargs: Any) -> FakeResponse:
        session.calls.append({"method": "POST", "url": url, **kwargs})
        store(client_id="another-developer-app", refresh_token=NEW_REFRESH)  # connected anew
        return FakeResponse(400, json_data={"error": "invalid_grant"})

    monkeypatch.setattr(session, "post", post)
    with pytest.raises(sa.SpotifyAuthError, match="connection expired"):
        sa.access_token(CLIENT_ID)
    assert stored()["client_id"] == "another-developer-app"


def test_a_renewal_that_could_not_be_saved_is_not_lost(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch, clock: dict[str, float]
) -> None:
    """Spotify rotated the refresh token but the file could not be written: the next renewal
    must use the new refresh token, not the dead one still in the file."""
    store(expires_at=NOW - 10)
    session.add(
        "POST",
        sa.TOKEN_URL,
        [token_ok(NEW_ACCESS, NEW_REFRESH), token_ok(THIRD_ACCESS, refresh=None)],
    )
    real_write = sa._write_record

    def full_disk(record: dict[str, Any]) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sa, "_write_record", full_disk)
    assert sa.access_token(CLIENT_ID) == NEW_ACCESS
    assert stored()["refresh_token"] == REFRESH  # the file still holds the old one
    assert sa.access_token(CLIENT_ID) == NEW_ACCESS and len(session.calls) == 1
    clock["now"] = NOW + 3600  # the renewed token has expired as well
    assert sa.access_token(CLIENT_ID) == THIRD_ACCESS
    assert session.calls[-1]["data"]["refresh_token"] == NEW_REFRESH
    monkeypatch.setattr(sa, "_write_record", real_write)  # room on the disk again
    assert sa.access_token(CLIENT_ID) == THIRD_ACCESS and len(session.calls) == 2
    assert stored()["refresh_token"] == NEW_REFRESH
    assert stored()["access_token"] == THIRD_ACCESS


def test_a_renewal_waits_for_another_process_and_uses_its_result(session: FakeSession) -> None:
    store(expires_at=NOW - 10)
    holding, release = threading.Event(), threading.Event()

    def other_process() -> None:
        with sa._file_lock():  # a second handle on the lock file, as another process has
            holding.set()
            release.wait(5)
            store(access_token=NEW_ACCESS, refresh_token=NEW_REFRESH, expires_at=NOW + 3600)

    other = threading.Thread(target=other_process)
    other.start()
    assert holding.wait(5)
    results: list[str | None] = []
    waiter = threading.Thread(target=lambda: results.append(sa.access_token(CLIENT_ID)))
    waiter.start()
    waiter.join(0.3)
    assert waiter.is_alive()  # waiting for the lock instead of renewing on its own
    release.set()
    waiter.join(5)
    other.join(5)
    assert results == [NEW_ACCESS]
    assert session.calls == []


def test_a_lock_that_is_never_released_does_not_block_a_renewal(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=sa.__name__)
    monkeypatch.setattr(sa, "LOCK_WAIT", 0.2)
    store(expires_at=NOW - 10)
    session.add("POST", sa.TOKEN_URL, token_ok(NEW_ACCESS, NEW_REFRESH))
    holding, release = threading.Event(), threading.Event()

    def stuck() -> None:
        with sa._file_lock():
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=stuck)
    thread.start()
    assert holding.wait(5)
    try:
        assert sa.access_token(CLIENT_ID) == NEW_ACCESS
    finally:
        release.set()
        thread.join(5)
    assert "kept the Spotify connection locked" in caplog.text


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (requests.ConnectionError("offline"), sa.UNREACHABLE_MESSAGE),
        (FakeResponse(503, json_data={}), "HTTP 503"),
        (FakeResponse(429, json_data={}), sa.RATE_LIMIT_MESSAGE),
    ],
)
def test_refresh_trouble_keeps_the_connection(
    session: FakeSession, handler: Any, message: str
) -> None:
    store(expires_at=NOW - 10)
    session.add("POST", sa.TOKEN_URL, handler)
    with pytest.raises(ProviderError, match=re.escape(message)) as exc_info:
        sa.access_token(CLIENT_ID)
    assert not isinstance(exc_info.value, sa.SpotifyAuthError)  # not a reason to reconnect
    assert stored()["refresh_token"] == REFRESH


def test_stale_token_is_renewed_once(session: FakeSession) -> None:
    store()  # fresh, but the API just answered 401 for it
    session.add("POST", sa.TOKEN_URL, token_ok(NEW_ACCESS, NEW_REFRESH))
    assert sa.access_token(CLIENT_ID, stale_token=ACCESS) == NEW_ACCESS
    # a second worker holding the same stale token gets the renewed one without a new request
    assert sa.access_token(CLIENT_ID, stale_token=ACCESS) == NEW_ACCESS
    assert len(session.calls) == 1


def test_cancel_before_a_refresh(session: FakeSession) -> None:
    store(expires_at=NOW - 10)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        sa.access_token(CLIENT_ID, cancel)
    assert session.calls == []


def test_concurrent_refreshes_are_serialised(
    session: FakeSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    store(expires_at=NOW - 10)
    session.add("POST", sa.TOKEN_URL, token_ok(NEW_ACCESS, NEW_REFRESH))
    original = session.post

    def slow_post(*args: Any, **kwargs: Any) -> FakeResponse:
        time.sleep(0.05)
        return original(*args, **kwargs)

    monkeypatch.setattr(session, "post", slow_post)
    results: list[str | None] = []
    threads = [
        threading.Thread(target=lambda: results.append(sa.access_token(CLIENT_ID)))
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert results == [NEW_ACCESS] * 6
    assert len(session.calls) == 1


def test_tokens_are_bound_to_their_client_id(session: FakeSession) -> None:
    store(client_id="another-developer-app")
    assert sa.access_token(CLIENT_ID) is None
    assert sa.current_account(CLIENT_ID) is None
    assert sa.current_account("another-developer-app") is not None
    assert sa.current_account() is not None  # without a client id: whatever is stored
    assert session.calls == []


def test_logout() -> None:
    store()
    assert sa.logout() is True
    assert not token_file().exists()
    assert sa.current_account() is None
    assert sa.logout() is False


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"access_token": ACCESS, "client_id": CLIENT_ID}),  # no user
        json.dumps({"access_token": ACCESS, "user_id": "u"}),  # no client id
    ],
)
def test_unreadable_token_file_means_not_connected(
    content: str, caplog: pytest.LogCaptureFixture
) -> None:
    token_file().write_text(content, encoding="utf-8")
    assert sa.current_account() is None
    assert sa.access_token(CLIENT_ID) is None
    assert_no_secrets(caplog.text)
