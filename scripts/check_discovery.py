"""Verification for build step 7: endpoint discovery and the raw-API escape hatches.

    uv run scripts/check_discovery.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import build_server
from sugar.context import get_context

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)


def unwrap(result):
    payload = getattr(result, "structured_content", None)
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


async def main() -> int:
    mcp = build_server()
    failures = 0

    async def call(name: str, **kwargs):
        return unwrap(await mcp.call_tool(name, kwargs))

    def check(condition: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        if condition:
            print(f"PASS  {label}")
        else:
            print(f"FAIL  {label}  {detail}")
            failures += 1

    # Cold catalog, so the fetch cost is real.
    context = get_context()
    shutil.rmtree(context.config.metadata_cache_dir / "endpoints", ignore_errors=True)

    print("=" * 78)
    started = time.time()
    result = await call("sugar_list_endpoints", limit=5)
    cold = time.time() - started
    check(result.get("total_available", 0) > 500,
          f"catalog built — {result.get('total_available')} endpoints in {cold:.1f}s "
          f"via {result.get('catalog_source')}")
    if result.get("note"):
        print(f"        {result['note'][:110]}")

    started = time.time()
    await call("sugar_list_endpoints", limit=5)
    print(f"        warm: {time.time() - started:.3f}s")

    # The payoff: endpoints that exist only on this instance.
    result = await call("sugar_list_endpoints", custom_only=True, limit=30)
    check(result.get("count", 0) >= 15,
          f"custom endpoints found — {result.get('count')}")
    for endpoint in (result.get("endpoints") or [])[:6]:
        print(f"        {endpoint['method']:<7} {endpoint['path']:<44} "
              f"{Path(endpoint.get('source', '')).name}")

    result = await call("sugar_list_endpoints", module="Accounts", limit=10)
    check(result.get("count", 0) > 0, f"module filter — {result.get('count')} for Accounts")

    result = await call("sugar_list_endpoints", query="convert", limit=5)
    check(any("convert" in e["path"].lower() for e in result.get("endpoints", [])),
          "text search finds /Leads/:leadId/convert")
    for endpoint in result.get("endpoints", [])[:3]:
        print(f"        {endpoint['method']:<7} {endpoint['path']}")

    result = await call("sugar_list_endpoints", method="DELETE", limit=5)
    check(all(e["method"] == "DELETE" for e in result.get("endpoints", [])),
          "method filter returns only DELETE")

    print("\n" + "=" * 78)
    print("Raw API escape hatches")
    print("=" * 78)

    result = await call("sugar_api_get", path="ping")
    check(result.get("response") == "pong", "sugar_api_get — GET /ping",
          json.dumps(result)[:120])

    # A real custom endpoint on this instance.
    account = await call("sugar_query_records", module="Accounts",
                         fields=["id", "name"], max_num=1)
    records = account.get("records") or []
    if records:
        record_id = records[0]["id"]
        result = await call("sugar_api_get",
                            path=f"Accounts/{record_id}/opportunity_stats")
        check("error" not in result, "sugar_api_get — a custom endpoint",
              json.dumps(result)[:150])
        print(f"        {json.dumps(result.get('response'))[:130]}")

    # Typed-tool paths must be refused so the model cannot bypass shaping and validation.
    for path in ("Accounts/filter", "Accounts/count", "globalsearch", "metadata"):
        result = await call("sugar_api_get", path=path)
        check(result.get("error_label") == "use_typed_tool", f"refuses GET {path}")

    result = await call("sugar_api_call", method="POST", path="Accounts/filter", body={})
    check(result.get("error_label") == "use_typed_tool", "refuses POST Accounts/filter")

    result = await call("sugar_api_call", method="GET", path="ping")
    check(result.get("error_label") == "wrong_tool", "sugar_api_call rejects GET")

    result = await call("sugar_api_call", method="TRACE", path="x")
    check(result.get("error_label") == "invalid_parameter", "rejects unsupported verb")

    print("=" * 78)
    print(f"Step 7: {'verified' if not failures else f'{failures} failure(s)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
