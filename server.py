"""SugarMCP — an MCP server for SugarCRM.

Discovers its entire surface at runtime from Sugar's metadata API. Nothing about modules or
fields is hard-coded, because Sugar instances are heavily customized: a tool written against
``Accounts.industry`` is worthless on the next instance, while one that asks metadata what
``Accounts`` has works everywhere.

Run it::

    uv run server.py

Or register it with a client::

    claude mcp add sugar -- uv --directory /path/to/SugarMCP run server.py

Logging goes to **stderr**, never stdout: under the stdio transport, stdout carries the
JSON-RPC stream and a stray ``print()`` corrupts it. (The mcp-starter this follows does print
at startup — do not copy that.)
"""

from __future__ import annotations

import logging
import os
import sys

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

import tools.read as read_tools
from sugar.config import ConfigError, SugarConfig
from transport import HEALTH_PATH, HttpSettings, RequestLogMiddleware, serve_http

logging.basicConfig(
    level=os.environ.get("SUGAR_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("sugarmcp")

INSTRUCTIONS = """\
This server provides access to a SugarCRM instance as one specific Sugar user, inheriting
that user's permissions — Sugar enforces them, so a permission denial is real.

Nothing about this instance's schema is hard-coded. Start with sugar_list_modules to see
what exists, then sugar_describe_module before reading or writing any field: modules here
carry large numbers of custom fields that exist on no other Sugar instance.

Enum fields report a dropdown name, not values. Resolve them with sugar_get_enum before
filtering or writing.

Prefer sugar_count_records for "how many" questions and always name the fields you need on
queries — this instance holds hundreds of thousands of records with 175+ fields each.
"""


def build_server(transport: str = "stdio") -> MCPServer:
    """Construct the server and register the tool sets.

    The write tools are *not registered at all* when SUGAR_READ_ONLY is set, rather than
    registered and refused at call time. A tool that does not exist cannot be approved by
    mistake.

    ``transport`` is used only for the /health payload and the structured log actor; it does
    not change which tools register.
    """
    mcp = MCPServer(
        name="SugarCRM",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        # One structured stderr line per tool call (tool, actor, duration, outcome). Harmless
        # under stdio, where it is simply never triggered by an HTTP request logger upstream.
        middleware=[RequestLogMiddleware()],
    )

    read_tools.register(mcp)
    registered = ["read"]

    read_only = (os.environ.get("SUGAR_READ_ONLY") or "").strip().lower() in ("1", "true", "yes", "on")

    # Liveness only: no Sugar call, so a down instance (the connection is lazy) still reports
    # healthy. Registered unconditionally; it only receives traffic under HTTP. The SDK leaves
    # this route unauthenticated by design.
    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(_request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(
            {"status": "ok", "transport": transport, "read_only": read_only}
        )

    if read_only:
        log.info("SUGAR_READ_ONLY is set — write tools will not be registered")
    else:
        try:
            import tools.write as write_tools

            write_tools.register(mcp)
            registered.append("write")
        except ImportError:
            log.info("Write tools not yet implemented; running read-only")

    # Discovery carries both a read tool and a raw-write escape hatch, so it registers last
    # and is told which half it may expose.
    try:
        import tools.discovery as discovery_tools

        discovery_tools.register(mcp, read_only=read_only)
        registered.append("discovery")
    except ImportError:
        log.info("Discovery tools not available")

    log.info("SugarMCP ready — tool sets registered: %s", ", ".join(registered))
    return mcp


def _select_transport(argv: list[str]) -> str:
    """stdio (default) unless MCP_TRANSPORT=http or a --http flag is given."""
    if "--http" in argv:
        return "http"
    if "--stdio" in argv:
        return "stdio"
    value = (os.environ.get("MCP_TRANSPORT") or "stdio").strip().lower()
    return "http" if value in ("http", "streamable-http") else "stdio"


def main() -> int:
    transport = _select_transport(sys.argv[1:])

    # The remote surface defaults to read-only: writes are off unless explicitly re-enabled.
    # stdio is untouched. Set the env var before SugarConfig/build_server read it, so write
    # tools are genuinely never registered — not merely refused.
    if transport == "http" and (os.environ.get("SUGAR_READ_ONLY") or "") == "":
        os.environ["SUGAR_READ_ONLY"] = "1"
        log.info("HTTP transport: defaulting to read-only (set SUGAR_READ_ONLY=0 to allow writes)")

    http_settings: HttpSettings | None = None
    if transport == "http":
        http_settings = HttpSettings.from_env()
        if not http_settings.auth_token and not http_settings.allow_anonymous:
            log.error(
                "Refusing to start HTTP transport without authentication. Set MCP_AUTH_TOKEN "
                "to a strong secret, or set MCP_ALLOW_ANONYMOUS=1 to override (local testing "
                "only — never on a reachable address)."
            )
            return 2

    try:
        # Validate configuration early so a missing variable is a clear log line rather
        # than a puzzling failure on the first tool call. The connection itself stays lazy.
        config = SugarConfig.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    log.info(
        "Target: %s as %s (platform=%s, api=%s, read_only=%s, transport=%s)",
        config.url, config.username, config.platform, config.api_version, config.read_only,
        transport,
    )

    mcp = build_server(transport=transport)
    if transport == "http":
        assert http_settings is not None
        serve_http(mcp, http_settings)
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
