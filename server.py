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

import tools.read as read_tools
from sugar.config import ConfigError, SugarConfig

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


def build_server() -> MCPServer:
    """Construct the server and register the tool sets.

    The write tools are *not registered at all* when SUGAR_READ_ONLY is set, rather than
    registered and refused at call time. A tool that does not exist cannot be approved by
    mistake.
    """
    mcp = MCPServer(
        name="SugarCRM",
        version="0.1.0",
        instructions=INSTRUCTIONS,
    )

    read_tools.register(mcp)
    registered = ["read"]

    read_only = (os.environ.get("SUGAR_READ_ONLY") or "").strip().lower() in ("1", "true", "yes", "on")
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


def main() -> int:
    try:
        # Validate configuration early so a missing variable is a clear log line rather
        # than a puzzling failure on the first tool call. The connection itself stays lazy.
        config = SugarConfig.from_env()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    log.info(
        "Target: %s as %s (platform=%s, api=%s, read_only=%s)",
        config.url, config.username, config.platform, config.api_version, config.read_only,
    )

    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
