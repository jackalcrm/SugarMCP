"""Progress notifications for long-running tools.

The Academy lesson covers two channels: protocol logging (``ctx.info`` /
``notifications/message``) and progress (``ctx.report_progress``). Logging on the
wire is deprecated as of 2026-07-28 (SEP-2577) — operational logs stay on stderr
via the ``logging`` module, which is what this server already does. Progress is
not deprecated: a client that opts in sees a status line while the tool is still
working, instead of looking stalled.

``sugar/`` stays free of ``mcp`` imports. Tools pass the injected MCP ``Context``
in; this helper no-ops when there is none, or when the call is not on a real
request (``MCPServer.call_tool`` in check scripts). A failed notification must
never fail the tool.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger("sugarmcp.tools.progress")


class ProgressReporter(Protocol):
    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None: ...


async def report(
    mcp_ctx: ProgressReporter | Any | None,
    progress: float,
    *,
    total: float | None = None,
    message: str | None = None,
) -> None:
    """Send a progress notification if the client asked for one.

    Progress values must increase with every call; never repeat or go backwards.
    Omit ``total`` when the denominator is unknown.
    """
    if mcp_ctx is None:
        return
    reporter = getattr(mcp_ctx, "report_progress", None)
    if not callable(reporter):
        return
    try:
        await reporter(progress, total, message)
    except Exception:  # noqa: BLE001 - progress is best-effort
        log.debug("progress notification failed", exc_info=True)
