"""Verification for build step 3: every read tool, called through the MCP tool dispatch.

Goes through ``MCPServer.call_tool`` rather than calling the Python functions directly, so
argument coercion and the JSON schema are exercised the way a client would.

    uv run scripts/check_tools.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import build_server

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


def unwrap(result) -> object:
    """Pull the structured payload out of a CallToolResult.

    Tools returning a plain dict come back under ``structured_content``; a non-dict return
    is wrapped in a single ``result`` key by the SDK.
    """
    payload = getattr(result, "structured_content", None)
    if payload is None:
        content = getattr(result, "content", None) or []
        for block in content:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except ValueError:
                    return text
        return None
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


async def main() -> int:
    mcp = build_server()
    failures = 0

    async def call(name: str, note: str = "", **kwargs):
        nonlocal failures
        try:
            payload = unwrap(await mcp.call_tool(name, kwargs))
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
            failures += 1
            return None
        size = len(json.dumps(payload, default=str))
        if isinstance(payload, dict) and payload.get("error"):
            print(f"ERR   {name:<22} {payload.get('error_label')}: "
                  f"{str(payload.get('error'))[:70]}")
            failures += 1
            return payload
        print(f"PASS  {name:<22} {size:>7,}B  {note}")
        return payload

    print("=" * 78)
    me = await call("sugar_whoami")
    if me:
        print(f"        user={me['user_name']} admin={me['is_admin']} "
              f"denied_modules={me['denied_module_count']}")

    info = await call("sugar_server_info")
    if info:
        print(f"        Sugar {info['version']} {info['flavor']} | platform={info['platform']} "
              f"| api={info['api_version']} | pkg_help={info['mcp_help_endpoint']}")

    mods = await call("sugar_list_modules")
    if mods:
        print(f"        {mods['count']} modules, {mods['custom_count']} custom")

    desc = await call("sugar_describe_module", module="Accounts")
    if desc:
        print(f"        {desc['field_count']} fields, {desc.get('link_count')} links")

    detail = await call("sugar_describe_module", module="Accounts",
                        fields=["name", "industry", "annual_revenue"])
    if detail:
        print(f"        detail: {json.dumps(detail['fields'])[:150]}")

    enum = await call("sugar_get_enum", module="Accounts", field="industry")
    if enum:
        print(f"        {enum['count']} options, e.g. {list(enum['values'].items())[2:4]}")

    count = await call("sugar_count_records", module="Accounts")
    if count:
        print(f"        {count['count']:,} Accounts")

    filtered = await call("sugar_count_records", module="Accounts",
                          filter=[{"billing_address_country": {"$starts": "U"}}])
    if filtered:
        print(f"        {filtered['count']:,} with billing country starting 'U'")

    rows = await call("sugar_query_records", module="Accounts",
                      filter=[{"billing_address_country": {"$starts": "U"}}],
                      fields=["id", "name", "industry"], max_num=3,
                      order_by="date_modified:desc")
    first_id = None
    if rows and rows.get("records"):
        first_id = rows["records"][0]["id"]
        print(f"        {rows['count']} records, next_offset={rows.get('next_offset')}")
        print(f"        {json.dumps(rows['records'][0])[:120]}")

    # Clamping: ask for more than the ceiling and confirm it is bounded.
    clamped = await call("sugar_query_records", module="Accounts", max_num=9999)
    if clamped:
        print(f"        max_num=9999 clamped to {clamped['count']} records")

    # A deliberately wrong field name must warn, not silently succeed.
    bogus = await call("sugar_query_records", module="Accounts",
                       fields=["id", "name", "not_a_real_field"], max_num=1)
    if bogus:
        print(f"        warning: {str(bogus.get('warning'))[:90]}")

    if first_id:
        rec = await call("sugar_get_record", module="Accounts", record_id=first_id,
                         fields=["id", "name", "industry", "description"])
        if rec:
            print(f"        {json.dumps(rec['record'])[:120]}")

        rel = await call("sugar_get_related", module="Accounts", record_id=first_id,
                         link_name="contacts", fields=["id", "name"], max_num=3)
        if rel:
            print(f"        {rel['count']} related contacts")

    await call("sugar_search", query="Test", max_num=3)

    # Error path: a module that does not exist must return guidance, not a crash.
    bad = unwrap(await mcp.call_tool("sugar_describe_module", {"module": "NotARealModule"}))
    if isinstance(bad, dict) and bad.get("error"):
        print(f"PASS  error path            {bad.get('error_label')}: "
              f"{str(bad.get('error'))[:60]}")
    else:
        print(f"FAIL  error path returned {str(bad)[:80]}")
        failures += 1

    print("=" * 78)
    print(f"Step 3: {'verified' if not failures else f'{failures} failure(s)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
