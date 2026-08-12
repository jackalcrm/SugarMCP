"""OAuth2 session management: password grant, refresh rotation, persistence.

Sugar has no service accounts — ``client_credentials`` is not implemented, because
``SugarOAuth2Storage`` does not implement ``IOAuth2GrantClient``. So the server logs in as a
real user and carries that user's ACLs. Permissions are therefore enforced by Sugar, which
is the property the rest of the design leans on.

Three behaviours here are non-obvious and each one has bitten somebody:

* **Refresh tokens rotate.** ``OAuth2::createAccessToken()`` deletes the old refresh token
  after issuing a new one. Persist the new token immediately or the session is lost on the
  next restart.
* **``max_session_lifetime`` caps refreshing.** When set, a refreshed token inherits the
  *original* expiry, so refreshing cannot continue indefinitely. Fall back to a full login.
* **Platform choice evicts browser sessions.** ``SugarOAuth2StorageBase::$numSessions = 1``,
  so a password grant on platform ``base`` runs ``OAuthToken::cleanupOldUserTokens()`` and
  logs the user out of the Sugar web UI. A dedicated ``mcp`` platform gets its own slot.

The access token *is* the PHP session id. The token file is a credential; it is written 0600
and nothing in this module ever logs a token, password or client secret.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import FALLBACK_CLIENT_ID, SugarConfig
from .errors import SugarError, classify

log = logging.getLogger("sugarmcp.session")

# Refresh this many seconds before the token actually expires, so a request in flight
# doesn't land on the far side of the boundary.
EXPIRY_SKEW = 60.0


@dataclass
class Capabilities:
    """What this instance supports, probed once and cached on the session.

    Populated lazily: `platform` and `client_id` are settled by the first login, the
    `/mcp/*` flags by the first call that wants them.
    """

    platform: str = ""
    client_id: str = ""
    platform_fell_back: bool = False
    client_fell_back: bool = False
    api_version: str = ""
    mcp_help: bool | None = None
    mcp_schema: bool | None = None
    server_info: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "platform_fell_back": self.platform_fell_back,
            "client_id": self.client_id,
            "client_fell_back": self.client_fell_back,
            "api_version": self.api_version,
            "mcp_help_endpoint": self.mcp_help,
            "mcp_schema_endpoint": self.mcp_schema,
            "sugar_version": self.server_info.get("version"),
            "sugar_flavor": self.server_info.get("flavor"),
        }


@dataclass
class Tokens:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    download_token: str = ""
    platform: str = ""

    @property
    def expired(self) -> bool:
        return time.time() >= (self.expires_at - EXPIRY_SKEW)

    @classmethod
    def from_grant(cls, payload: dict[str, Any], platform: str) -> "Tokens":
        expires_in = float(payload.get("expires_in") or 3600)
        return cls(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or ""),
            expires_at=time.time() + expires_in,
            download_token=str(payload.get("download_token") or ""),
            platform=platform,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "download_token": self.download_token,
            "platform": self.platform,
        }


class TokenStore:
    """Persists tokens to disk at 0600 so restarts don't force a re-login."""

    def __init__(self, path):
        self.path = path

    def load(self) -> Tokens | None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        token = str(raw.get("access_token") or "")
        if not token:
            return None
        return Tokens(
            access_token=token,
            refresh_token=str(raw.get("refresh_token") or ""),
            expires_at=float(raw.get("expires_at") or 0),
            download_token=str(raw.get("download_token") or ""),
            platform=str(raw.get("platform") or ""),
        )

    def save(self, tokens: Tokens) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Create with 0600 from the start rather than chmod-ing after writing, which
            # would leave a window where the token is world-readable.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(tokens.to_dict(), fh)
        except OSError as exc:
            # A cache we cannot write is a warning, not a failure — we just re-login.
            log.warning("Could not persist tokens to %s: %s", self.path, exc)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


class SugarSession:
    """Owns credentials and tokens for one (instance, user, platform).

    Holds no module-level state: under stdio there is exactly one instance built from env,
    and a future streamable-HTTP transport swaps the provider for a session-keyed map
    without touching anything above this layer.
    """

    def __init__(self, config: SugarConfig, http: httpx.Client | None = None):
        self.config = config
        self.caps = Capabilities(
            platform=config.platform,
            client_id=config.client_id,
            api_version=config.api_version,
        )
        self._store = TokenStore(config.token_path)
        self._tokens: Tokens | None = self._store.load()
        self._lock = threading.RLock()
        self._owns_http = http is None
        self._http = http or httpx.Client(
            verify=config.verify_ssl,
            timeout=config.timeout,
            follow_redirects=False,
            headers={"User-Agent": "SugarMCP/0.1.0"},
        )
        if self._tokens and self._tokens.platform:
            # A cached token pins the platform it was issued for; honour it so we don't
            # silently switch platforms (and evict a web session) on restart.
            self.caps.platform = self._tokens.platform

    # -- public -------------------------------------------------------------

    @property
    def http(self) -> httpx.Client:
        return self._http

    @property
    def api_version(self) -> str:
        return self.caps.api_version

    def access_token(self) -> str:
        """Return a valid access token, refreshing or logging in as needed."""
        with self._lock:
            if self._tokens and not self._tokens.expired:
                return self._tokens.access_token
            if self._tokens and self._tokens.refresh_token:
                try:
                    return self._do_refresh().access_token
                except SugarError as exc:
                    log.info("Refresh failed (%s); falling back to password grant", exc.label)
            return self._do_login().access_token

    def force_refresh(self) -> str:
        """Called by the client after a 401. Refresh once, else re-login."""
        with self._lock:
            if self._tokens and self._tokens.refresh_token:
                try:
                    return self._do_refresh().access_token
                except SugarError as exc:
                    log.info("Refresh rejected (%s); re-running password grant", exc.label)
            return self._do_login().access_token

    def invalidate(self) -> None:
        with self._lock:
            self._tokens = None
            self._store.clear()

    def set_api_version(self, version: str) -> None:
        self.caps.api_version = version

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # -- grants -------------------------------------------------------------

    def _do_login(self) -> Tokens:
        """Password grant, with fallbacks for an instance lacking our package."""
        platform = self.caps.platform
        client_id = self.caps.client_id
        client_secret = self.config.client_secret

        try:
            payload = self._token_request(
                {
                    "grant_type": "password",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "username": self.config.username,
                    "password": self.config.password,
                    "platform": platform,
                }
            )
        except SugarError as exc:
            if exc.is_invalid_platform and platform != "base":
                # disable_unknown_platforms defaults to true, so an instance without our
                # package rejects platform=mcp outright. Fall back, but say so loudly:
                # on `base` the user's browser session gets evicted every login.
                log.warning(
                    "Platform %r is not registered on this instance (HTTP 422). Falling "
                    "back to platform 'base'. WARNING: logging in on 'base' will end the "
                    "user's Sugar web UI session, because SugarOAuth2StorageBase allows "
                    "only one session per platform. Install the SugarMCP package to "
                    "register the %r platform and avoid this.",
                    platform,
                    self.config.platform,
                )
                self.caps.platform = platform = "base"
                self.caps.platform_fell_back = True
                return self._do_login()

            if exc.label == "invalid_client" and client_id != FALLBACK_CLIENT_ID:
                # SugarOAuth2Storage::getClientDetails() auto-creates the 'sugar' key, so
                # it always exists even where ours does not.
                log.warning(
                    "OAuth client %r was rejected; falling back to %r. Note a custom "
                    "platform's key must have client_type='user'.",
                    client_id,
                    FALLBACK_CLIENT_ID,
                )
                self.caps.client_id = FALLBACK_CLIENT_ID
                self.caps.client_fell_back = True
                return self._do_login()

            raise

        tokens = Tokens.from_grant(payload, platform)
        self._tokens = tokens
        self._store.save(tokens)
        log.info("Logged in as %s on platform %r", self.config.username, platform)
        return tokens

    def _do_refresh(self) -> Tokens:
        assert self._tokens is not None
        payload = self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.caps.client_id,
                "client_secret": self.config.client_secret,
                "refresh_token": self._tokens.refresh_token,
                "platform": self.caps.platform,
            }
        )
        # Rotation: the old refresh token is now dead server-side. Persist immediately.
        tokens = Tokens.from_grant(payload, self.caps.platform)
        if not tokens.refresh_token:
            tokens.refresh_token = self._tokens.refresh_token
        self._tokens = tokens
        self._store.save(tokens)
        log.debug("Refreshed access token")
        return tokens

    def _token_request(self, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.config.rest_base()}/oauth2/token"
        response = self._http.post(url, json=body)
        try:
            payload = response.json()
        except ValueError:
            payload = response.text

        if response.status_code >= 300 or not isinstance(payload, dict) or "access_token" not in payload:
            raise classify(
                response.status_code,
                payload,
                method="POST",
                path="/oauth2/token",
            )
        return payload

    def __repr__(self) -> str:  # never leak tokens into a traceback
        return (
            f"SugarSession(url={self.config.url!r}, user={self.config.username!r}, "
            f"platform={self.caps.platform!r}, authenticated={self._tokens is not None})"
        )
