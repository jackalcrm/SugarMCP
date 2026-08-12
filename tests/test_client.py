"""Tests for the client's retry policy, over mocked HTTP.

The retry rules are the part of the transport where a bug is silent: a missing retry looks
like an intermittent failure, and an unbounded one looks like a hang.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from sugar.client import SugarClient, _next_version
from sugar.config import SugarConfig
from sugar.errors import SugarError
from sugar.session import SugarSession, Tokens

BASE = "https://sugar.test"
REST = f"{BASE}/rest/v11_27"


@pytest.fixture
def config(tmp_path: Path) -> SugarConfig:
    return SugarConfig(
        url=BASE,
        username="tester",
        password="secret",
        platform="mcp",
        client_id="mcp",
        cache_dir=tmp_path,
    )


@pytest.fixture
def session(config: SugarConfig) -> SugarSession:
    sugar_session = SugarSession(config)
    # Start authenticated so tests exercise request retries rather than initial login.
    sugar_session._tokens = Tokens(
        access_token="token-1", refresh_token="refresh-1",
        expires_at=2**31, platform="mcp",
    )
    return sugar_session


@respx.mock
def test_successful_request_passes_through(session: SugarSession):
    respx.get(f"{REST}/ping").mock(return_value=httpx.Response(200, json="pong"))
    assert SugarClient(session).get("ping") == "pong"


@respx.mock
def test_need_login_refreshes_and_replays_once(session: SugarSession):
    route = respx.get(f"{REST}/me").mock(side_effect=[
        httpx.Response(401, json={"error": "need_login", "error_message": "expired"}),
        httpx.Response(200, json={"current_user": {"user_name": "tester"}}),
    ])
    respx.post(f"{REST}/oauth2/token").mock(return_value=httpx.Response(
        200, json={"access_token": "token-2", "refresh_token": "refresh-2", "expires_in": 3600}
    ))

    result = SugarClient(session).get("me")
    assert result["current_user"]["user_name"] == "tester"
    assert route.call_count == 2


@respx.mock
def test_invalid_grant_on_an_api_call_triggers_relogin(session: SugarSession):
    """An evicted token reports `invalid_grant`, not `need_login`.

    Sugar allows one session per platform, so another login on the same platform slot kills
    this token. The refresh token dies with it, so only a full re-login recovers — and
    without this rule the server would surface a spurious "credentials are wrong".
    """
    route = respx.get(f"{REST}/ping").mock(side_effect=[
        httpx.Response(401, json={"error": "invalid_grant",
                                  "error_message": "The access token provided is invalid."}),
        httpx.Response(200, json="pong"),
    ])
    token_route = respx.post(f"{REST}/oauth2/token").mock(return_value=httpx.Response(
        200, json={"access_token": "token-3", "refresh_token": "refresh-3", "expires_in": 3600}
    ))

    assert SugarClient(session).get("ping") == "pong"
    assert route.call_count == 2
    # A password grant, not a refresh — the refresh token went down with the session.
    assert token_route.calls.last.request.read().decode().find("password") != -1


@respx.mock
def test_retries_are_attempted_at_most_once(session: SugarSession):
    """A server that always 401s must fail, not loop forever."""
    route = respx.get(f"{REST}/ping").mock(
        return_value=httpx.Response(401, json={"error": "need_login"})
    )
    respx.post(f"{REST}/oauth2/token").mock(return_value=httpx.Response(
        200, json={"access_token": "t", "refresh_token": "r", "expires_in": 3600}
    ))

    with pytest.raises(SugarError) as caught:
        SugarClient(session).get("ping")
    assert caught.value.label == "need_login"
    assert route.call_count == 2  # original + one retry, then give up


@respx.mock
def test_metadata_stale_invalidates_cache_and_replays(session: SugarSession):
    calls: list[str] = []
    route = respx.get(f"{REST}/metadata").mock(side_effect=[
        httpx.Response(412, json={"error": "metadata_out_of_date"}),
        httpx.Response(200, json={"modules": {}}),
    ])

    client = SugarClient(session, on_metadata_stale=lambda: calls.append("cleared"))
    assert client.get("metadata") == {"modules": {}}
    assert calls == ["cleared"]
    assert route.call_count == 2


@respx.mock
def test_version_negotiated_down_on_301(session: SugarSession):
    respx.get(f"{REST}/ping").mock(
        return_value=httpx.Response(301, json={"error": "incorrect_version"})
    )
    respx.get(f"{BASE}/rest/v11_26/ping").mock(return_value=httpx.Response(200, json="pong"))

    assert SugarClient(session).get("ping") == "pong"
    assert session.api_version == "v11_26"


@respx.mock
def test_unretryable_error_is_raised_with_guidance(session: SugarSession):
    respx.get(f"{REST}/Accounts/nope").mock(
        return_value=httpx.Response(404, json={"error": "not_found",
                                               "error_message": "No such record"})
    )
    with pytest.raises(SugarError) as caught:
        SugarClient(session).get("Accounts/nope")

    error = caught.value.as_tool_error()
    assert error["error_label"] == "not_found"
    assert error["status"] == 404
    assert "guidance" in error
    assert error["request"] == "GET Accounts/nope"


@respx.mock
def test_probe_returns_none_for_a_missing_optional_endpoint(session: SugarSession):
    """The /mcp/* capability probes: absence is the expected case on a stock instance."""
    respx.get(f"{REST}/mcp/help").mock(
        return_value=httpx.Response(404, json={"error": "no_method"})
    )
    assert SugarClient(session).probe("mcp/help") is None


@respx.mock
def test_probe_still_raises_on_a_real_failure(session: SugarSession):
    respx.get(f"{REST}/mcp/help").mock(
        return_value=httpx.Response(500, json={"error": "internal_error"})
    )
    with pytest.raises(SugarError):
        SugarClient(session).probe("mcp/help")


@respx.mock
def test_bulk_adds_version_prefix_and_stringifies_data(session: SugarSession):
    """Both are undocumented requirements of /bulk that break naive callers."""
    route = respx.post(f"{REST}/bulk").mock(return_value=httpx.Response(
        200, json=[{"status": 200, "contents": "pong"}]
    ))

    results = SugarClient(session).bulk([
        {"method": "POST", "url": "Accounts/filter", "data": {"max_num": 1}}
    ])

    sent = route.calls.last.request.read().decode()
    assert '"/v11_27/Accounts/filter"' in sent
    assert '"data": "{\\"max_num\\": 1}"' in sent or '"data":"{\\"max_num\\": 1}"' in sent
    assert results == ["pong"]


@respx.mock
def test_bulk_surfaces_per_call_errors(session: SugarSession):
    respx.post(f"{REST}/bulk").mock(return_value=httpx.Response(200, json=[
        {"status": 200, "contents": "pong"},
        {"status": 404, "contents": {"error": "not_found", "error_message": "gone"}},
    ]))

    results = SugarClient(session).bulk([
        {"method": "GET", "url": "ping"},
        {"method": "GET", "url": "Accounts/nope"},
    ])
    assert results[0] == "pong"
    assert results[1]["error_label"] == "not_found"


def test_version_ladder_steps_down_then_stops():
    assert _next_version("v11_27") == "v11_26"
    assert _next_version("v10") is None
    assert _next_version("nonsense") == "v11_27"


# -- transport failures ------------------------------------------------------


@respx.mock
def test_connect_error_is_diagnosed_not_left_raw(session: SugarSession):
    """A transport failure never reaches Sugar, so it has no error envelope.

    Left raw the model sees an opaque "ConnectError" and tends to retry pointlessly.
    """
    respx.get(f"{REST}/me").mock(side_effect=httpx.ConnectError("[Errno 65] No route to host"))

    with pytest.raises(SugarError) as caught:
        SugarClient(session).get("me")

    error = caught.value
    assert error.label == "connection_failed"
    assert error.retry.value == "none"  # retrying cannot help
    assert BASE in error.message
    hint = error.payload["hints"][0]
    assert "Local Network" in hint  # the macOS 15 permission case


@respx.mock
def test_dns_failure_gets_a_different_hint(session: SugarSession):
    respx.get(f"{REST}/me").mock(
        side_effect=httpx.ConnectError("[Errno 8] nodename nor servname provided")
    )
    with pytest.raises(SugarError) as caught:
        SugarClient(session).get("me")
    assert "could not be resolved" in caught.value.payload["hints"][0]


@respx.mock
def test_timeout_is_diagnosed(session: SugarSession):
    respx.get(f"{REST}/me").mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(SugarError) as caught:
        SugarClient(session).get("me")
    assert caught.value.label == "connection_failed"


# -- refresh-token rotation --------------------------------------------------
#
# Called out in the design doc as one of the places a bug is silent rather than loud:
# OAuth2::createAccessToken() deletes the old refresh token after issuing a new one, so a
# rotated token that is not persisted immediately leaves the session unrecoverable on the
# next restart — and nothing fails until then.


@respx.mock
def test_rotated_refresh_token_is_persisted_immediately(session: SugarSession, config):
    respx.post(f"{REST}/oauth2/token").mock(return_value=httpx.Response(200, json={
        "access_token": "token-2", "refresh_token": "refresh-ROTATED", "expires_in": 3600,
    }))

    session.force_refresh()

    # In memory...
    assert session._tokens.refresh_token == "refresh-ROTATED"
    # ...and on disk, before any further call can fail.
    persisted = json.loads(config.token_path.read_text())
    assert persisted["refresh_token"] == "refresh-ROTATED"
    assert persisted["access_token"] == "token-2"


@respx.mock
def test_token_file_is_not_world_readable(session: SugarSession, config):
    """The access token *is* the PHP session id — the file is a credential."""
    respx.post(f"{REST}/oauth2/token").mock(return_value=httpx.Response(
        200, json={"access_token": "t", "refresh_token": "r", "expires_in": 3600}
    ))
    session.force_refresh()
    assert oct(config.token_path.stat().st_mode)[-3:] == "600"


@respx.mock
def test_refresh_keeps_the_old_token_when_sugar_returns_none(session: SugarSession):
    """Some grants echo no refresh token; dropping it would strand the session."""
    respx.post(f"{REST}/oauth2/token").mock(return_value=httpx.Response(
        200, json={"access_token": "token-2", "expires_in": 3600}
    ))
    session.force_refresh()
    assert session._tokens.refresh_token == "refresh-1"


@respx.mock
def test_failed_refresh_falls_back_to_a_password_grant(session: SugarSession):
    """max_session_lifetime makes refreshing stop working eventually; only login recovers."""
    route = respx.post(f"{REST}/oauth2/token").mock(side_effect=[
        httpx.Response(400, json={"error": "invalid_grant",
                                  "error_message": "refresh token expired"}),
        httpx.Response(200, json={"access_token": "token-fresh",
                                  "refresh_token": "refresh-fresh", "expires_in": 3600}),
    ])

    assert session.force_refresh() == "token-fresh"
    assert route.call_count == 2
    assert "password" in route.calls.last.request.read().decode()
