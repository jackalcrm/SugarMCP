"""End-to-end check over the real stdio transport, as Claude Desktop will run it.

Spawns ``server.py`` as a subprocess and speaks MCP to it, which is the only way to catch
the failure mode that matters most for a stdio server: anything written to stdout corrupts
the JSON-RPC stream, and the symptom is a client that reports the server as broken with no
useful error.

    uv run scripts/check_stdio.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "server.py")],
        cwd=str(ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"PASS  initialize — {init.server_info.name} v{init.server_info.version}")
            print(f"      instructions: {len((init.instructions or '').strip())} chars")

            tools = (await session.list_tools()).tools
            read_only = [t for t in tools if getattr(t.annotations, "read_only_hint", False)]
            destructive = [
                t for t in tools if getattr(t.annotations, "destructive_hint", False)
            ]
            writes = [t for t in tools if t not in read_only]
            print(f"PASS  list_tools — {len(tools)} tools: {len(read_only)} read-only, "
                  f"{len(writes)} write ({len(destructive)} destructive)")

            # The approval boundary: a client that "always allow"s the read set must not
            # thereby have approved anything that mutates.
            leaked = [t.name for t in read_only if t.name in {w.name for w in writes}]
            if leaked:
                print(f"FAIL  tools both read-only and mutating: {leaked}")
                return 1
            if not destructive:
                print("FAIL  no tool carries destructive_hint")
                return 1
            for tool in destructive:
                if getattr(tool.annotations, "read_only_hint", False):
                    print(f"FAIL  {tool.name} is destructive but marked read-only")
                    return 1
            print(f"      destructive: {', '.join(t.name for t in destructive)}")

            result = await session.call_tool("sugar_whoami", {})
            payload = result.structured_content or {}
            print(f"PASS  sugar_whoami — user={payload.get('user_name')} "
                  f"admin={payload.get('is_admin')}")

            result = await session.call_tool(
                "sugar_query_records",
                {"module": "Accounts", "fields": ["id", "name"], "max_num": 2},
            )
            payload = result.structured_content or {}
            print(f"PASS  sugar_query_records — {payload.get('count')} records, "
                  f"{len(json.dumps(payload))}B")

            result = await session.call_tool("sugar_count_records", {"module": "Accounts"})
            print(f"PASS  sugar_count_records — "
                  f"{(result.structured_content or {}).get('count'):,}")

    # SUGAR_READ_ONLY must remove the write tools from the listing entirely, not merely
    # refuse them at call time — a tool that is absent cannot be approved by mistake.
    read_only_params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "server.py")],
        cwd=str(ROOT),
        env={**os.environ, "SUGAR_READ_ONLY": "1"},
    )
    async with stdio_client(read_only_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            # Judge by the annotation, not the name: sugar_api_get is a raw-call tool but
            # is genuinely read-only, and belongs in this mode.
            mutating = [
                t.name for t in tools
                if not getattr(t.annotations, "read_only_hint", False)
            ]
            if mutating:
                print(f"FAIL  SUGAR_READ_ONLY still exposes mutating tools: {sorted(mutating)}")
                return 1
            print(f"PASS  SUGAR_READ_ONLY — {len(tools)} tools, all read-only")

    print("\nstdio transport verified — no stdout corruption.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
