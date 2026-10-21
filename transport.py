"""HTTP transport assembly — kept in one module so the ``server.py`` diff stays small and
the ``sugar/`` layer keeps importing nothing from ``mcp``.

Three pieces, all opt-in via ``MCP_TRANSPORT=http``:

* :class:`RequestLogMiddleware` — an MCP ``ServerMiddleware`` that emits one structured
  stderr line per tool call (tool, actor, duration, outcome). Tool-aware, works under stdio
  and HTTP both; non-tool messages pass through untouched.
* :class:`StaticBearerMiddleware` — a tiny pure-ASGI shim that enforces a static
  ``Authorization: Bearer`` token, skipping the public ``/health`` path. Pure ASGI (not a
  Starlette middleware) so lifespan/websocket scopes forward cleanly and the SDK app's own
  lifespan — which starts the streamable-HTTP session manager — still runs.
* :func:`serve_http` — builds the SDK's Starlette app, wraps it, and runs uvicorn.

The static bearer is a spike smoke-test credential, not OAuth. The seam for real per-user
OAuth 2.1 is the SDK's ``token_verifier`` / ``auth=AuthSettings`` path plus a session-keyed
``context_provider`` — see the README "Future seams" note; nothing here forecloses it.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from dataclasses import dataclass

log = logging.getLogger("sugarmcp.transport")

# Path of the unauthenticated health route (registered in server.py). The bearer shim lets
# it through so an orchestrator's health probe never needs the token.
HEALTH_PATH = "/health"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class HttpSettings:
    """Transport/server configuration, read from the environment.

    Deliberately separate from :class:`sugar.config.SugarConfig`: that file owns how we talk
    to Sugar, this owns how we expose MCP over HTTP.
    """

    host: str = "0.0.0.0"
    port: int = 8000
    path: str = "/mcp"
    json_response: bool = False
    stateless: bool = False
    auth_token: str = ""
    allow_anonymous: bool = False

    @classmethod
    def from_env(cls) -> "HttpSettings":
        return cls(
            host=(os.environ.get("MCP_HTTP_HOST") or "0.0.0.0").strip(),
            port=_int("MCP_HTTP_PORT", 8000),
            path=(os.environ.get("MCP_HTTP_PATH") or "/mcp").strip(),
            json_response=_bool("MCP_JSON_RESPONSE", False),
            stateless=_bool("MCP_STATELESS_HTTP", False),
            auth_token=(os.environ.get("MCP_AUTH_TOKEN") or "").strip(),
            allow_anonymous=_bool("MCP_ALLOW_ANONYMOUS", False),
        )


class RequestLogMiddleware:
    """Structured per-tool-call logging as an MCP ``ServerMiddleware``.

    The SDK invokes this as ``(ctx, call_next) -> result`` around every inbound message. A
    request-side failure arrives here as a raised exception, which is exactly what lets a
    logging middleware record the outcome — we time, re-raise, and never swallow.
    """

    def __init__(self, actor: str = ""):
        # Under a static bearer there is no per-caller identity, so the actor is the one
        # configured Sugar user. Per-caller identity arrives with OAuth passthrough.
        self._actor = actor or os.environ.get("SUGAR_USERNAME") or "?"

    async def __call__(self, ctx, call_next):
        if ctx.method != "tools/call":
            return await call_next(ctx)

        params = ctx.params or {}
        tool = params.get("name") if isinstance(params, dict) else None
        started = time.perf_counter()
        outcome = "ok"
        try:
            result = await call_next(ctx)
        except Exception as exc:  # noqa: BLE001 - record then re-raise, never swallow
            outcome = f"error:{type(exc).__name__}"
            self._emit(tool, started, outcome)
            raise
        # Tools return {"error": ...} as data (is_error stays false); surface that too.
        if getattr(result, "is_error", False):
            outcome = "error"
        self._emit(tool, started, outcome)
        return result

    def _emit(self, tool, started, outcome):
        duration_ms = (time.perf_counter() - started) * 1000.0
        log.info(
            "tool=%s actor=%s duration_ms=%.1f outcome=%s",
            tool, self._actor, duration_ms, outcome,
        )


class StaticBearerMiddleware:
    """Pure-ASGI static bearer-token gate, skipping ``/health``.

    Pure ASGI rather than a Starlette middleware so that ``lifespan`` and any non-HTTP scope
    forward straight to the wrapped app — the SDK's streamable-HTTP app wires its session
    manager to its own lifespan, and that must still fire.
    """

    def __init__(self, app, token: str, public_paths: frozenset[str] = frozenset({HEALTH_PATH})):
        self.app = app
        self._token = token
        self._public = public_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") in self._public:
            await self.app(scope, receive, send)
            return

        if not self._authorized(scope):
            await self._unauthorized(send)
            return
        await self.app(scope, receive, send)

    def _authorized(self, scope) -> bool:
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                prefix = b"Bearer "
                if value.startswith(prefix):
                    presented = value[len(prefix):].decode("latin-1")
                    return hmac.compare_digest(presented, self._token)
                return False
        return False

    async def _unauthorized(self, send) -> None:
        body = b'{"error":"unauthorized"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def serve_http(mcp, settings: HttpSettings) -> None:
    """Build the SDK Starlette app, apply the bearer gate, and run uvicorn.

    Runs the SDK app as the *root* app (not mounted), so its built-in lifespan — which starts
    the streamable-HTTP session manager — runs normally.
    """
    import uvicorn

    app = mcp.streamable_http_app(
        streamable_http_path=settings.path,
        json_response=settings.json_response,
        stateless_http=settings.stateless,
        host=settings.host,
    )

    if settings.auth_token:
        app = StaticBearerMiddleware(app, settings.auth_token)
        log.info("Static bearer auth enabled")
    else:
        # main() already refused to get here without --allow-anonymous; this is the shout.
        log.warning(
            "MCP_AUTH_TOKEN is empty — serving UNAUTHENTICATED. Anyone who can reach %s:%s "
            "can use this Sugar connection. Set MCP_AUTH_TOKEN.",
            settings.host, settings.port,
        )

    log.info(
        "Serving MCP over streamable HTTP at http://%s:%s%s (health: %s, stateless=%s)",
        settings.host, settings.port, settings.path, HEALTH_PATH, settings.stateless,
    )
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=(os.environ.get("SUGAR_LOG_LEVEL", "info")).lower(),
    )
