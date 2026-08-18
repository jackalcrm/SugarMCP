"""Progress notifications and the async tool_errors wrapper.

These tests do not need live Sugar or captured fixtures: the long-running tools are
registered against a stub context, and the MCP ``Context`` is injected by the SDK.
"""

from __future__ import annotations

import asyncio

from mcp import Client
from mcp.server.mcpserver import MCPServer

from sugar.errors import SugarError
from tools.progress import report
from tools.read import register as register_read
from tools.read import tool_errors


class _FakeConfig:
    url = "https://sugar.example"
    max_records = 20
    max_records_ceiling = 100


class _FakeClient:
    def get(self, path, params=None):
        if path.endswith("/record_count"):
            return {"record_count": 2}
        if path.endswith("/filter"):
            return {
                "reportDef": {
                    "display_columns": [
                        {"name": "name", "label": "Name"},
                        {"name": "id", "label": "ID"},
                    ]
                }
            }
        if path.endswith("/records"):
            return {
                "records": [
                    {"id": "a", "name": "Alpha", "noise": "x"},
                    {"id": "b", "name": "Beta", "noise": "y"},
                ]
            }
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path, body=None):
        raise AssertionError(f"unexpected POST {path}")


class _FakeContext:
    def __init__(self):
        self.config = _FakeConfig()
        self.client = _FakeClient()


def _read_server() -> MCPServer:
    mcp = MCPServer(name="SugarCRM-test", version="0")
    register_read(mcp, context_provider=lambda: _FakeContext())
    return mcp


# -- helper -----------------------------------------------------------------


def test_report_noops_without_context():
    asyncio.run(report(None, 1, total=2, message="ignored"))


def test_report_swallows_reporter_failures():
    class Broken:
        async def report_progress(self, progress, total=None, message=None):
            raise RuntimeError("no session")

    asyncio.run(report(Broken(), 1, total=2, message="should not raise"))


def test_report_forwards_to_context():
    seen: list[tuple] = []

    class Sink:
        async def report_progress(self, progress, total=None, message=None):
            seen.append((progress, total, message))

    asyncio.run(report(Sink(), 2, total=4, message="Fetching report records"))
    assert seen == [(2, 4, "Fetching report records")]


# -- tool_errors ------------------------------------------------------------


def test_tool_errors_maps_async_sugar_error():
    @tool_errors
    async def boom() -> dict:
        raise SugarError(label="not_authorized", message="denied")

    result = asyncio.run(boom())
    assert result["error_label"] == "not_authorized"
    assert "denied" in result["error"]


def test_tool_errors_still_wraps_sync_functions():
    @tool_errors
    def boom() -> dict:
        raise SugarError(label="not_found", message="gone")

    result = boom()
    assert result["error_label"] == "not_found"


# -- schema / dispatch ------------------------------------------------------


def test_mcp_context_is_not_in_tool_schemas():
    """The model must never see mcp_ctx — it is injected by the SDK, not filled in."""

    async def _run():
        from server import build_server

        mcp = build_server()
        for tool in await mcp.list_tools():
            props = (tool.input_schema or {}).get("properties") or {}
            assert "mcp_ctx" not in props, tool.name
            assert "context" not in props, tool.name

    asyncio.run(_run())


def test_run_report_emits_increasing_progress():
    async def _run():
        mcp = _read_server()
        seen: list[tuple] = []

        async def on_progress(progress, total, message):
            seen.append((progress, total, message))

        async with Client(mcp) as client:
            result = await client.call_tool(
                "sugar_run_report",
                {"report_id": "rep-1"},
                progress_callback=on_progress,
            )

        payload = result.structured_content or {}
        assert payload.get("rows_returned") == 2
        assert payload.get("columns") == ["name", "id"]
        assert [p for p, _, _ in seen] == [1, 2, 3, 4]
        assert seen[0][1] == 4
        assert "Counting" in (seen[0][2] or "")
        assert "Fetching" in (seen[2][2] or "")

    asyncio.run(_run())


def test_in_process_call_tool_survives_missing_request_context():
    """check_tools.py calls MCPServer.call_tool with no session; progress must no-op."""

    async def _run():
        mcp = _read_server()
        result = await mcp.call_tool("sugar_run_report", {"report_id": "rep-1"})
        payload = result.structured_content or {}
        assert payload.get("report_id") == "rep-1"
        assert payload.get("rows_returned") == 2

    asyncio.run(_run())
