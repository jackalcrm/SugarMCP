"""Throwaway verification for build step 1: login, refresh, retry, error mapping.

Runs against a real instance with no MCP code involved, per the design doc's instruction to
verify the session layer before any tools exist.

    uv run scripts/check_session.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sugar import ConfigError, SugarClient, SugarConfig, SugarError, SugarSession
from sugar.acl import AclIndex

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-7s %(name)s: %(message)s",
    stream=sys.stderr,
)


def main() -> int:
    try:
        config = SugarConfig.from_env()
    except ConfigError as exc:
        print(f"FAIL  {exc}")
        return 2

    print(f"Instance : {config.url}")
    print(f"User     : {config.username}")
    print(f"Platform : {config.platform}  Client: {config.client_id}")
    print(f"Tokens   : {config.token_path}")
    print("-" * 72)

    session = SugarSession(config)
    client = SugarClient(session)

    try:
        # 1. Password grant.
        token = session.access_token()
        print(f"PASS  login — token acquired ({len(token)} chars, not printed)")
        print(f"      platform in use: {session.caps.platform}"
              f"{'  (FELL BACK)' if session.caps.platform_fell_back else ''}")
        print(f"      client_id in use: {session.caps.client_id}"
              f"{'  (FELL BACK)' if session.caps.client_fell_back else ''}")

        # 2. An authenticated call.
        me = client.get("me")
        current = me.get("current_user", me)
        # Admin is reported as type == "admin"; there is no is_admin key on /me.
        acl_index = AclIndex.from_me(me)
        print(f"PASS  GET /me — {current.get('user_name')} "
              f"(type={current.get('type')}, admin={acl_index.is_admin})")
        denied = acl_index.denied_modules()
        print(f"      ACL: {len(current.get('acl') or {})} modules, "
              f"{len(denied)} denied{': ' + ', '.join(denied[:4]) + '…' if denied else ''}")

        # 3. Startup metadata, and the version actually negotiated.
        meta = client.get("metadata", {"type_filter": "server_info,full_module_list"})
        info = meta.get("server_info", {})
        modules = meta.get("full_module_list", {})
        session.caps.server_info = info
        print(f"PASS  GET /metadata — Sugar {info.get('version')} {info.get('flavor')} "
              f"build {info.get('build')}, {len(modules)} modules")
        print(f"      API version in use: {session.api_version}")

        # 4. Refresh path — force it rather than waiting an hour.
        refreshed = session.force_refresh()
        print(f"PASS  refresh — new token ({len(refreshed)} chars), "
              f"{'differs' if refreshed != token else 'IDENTICAL (unexpected)'}")
        client.get("ping")
        print("PASS  GET /ping after refresh — token still valid")

        # 5. Error mapping on a deliberate 404.
        try:
            client.get("Accounts/definitely-not-a-real-record-id")
            print("WARN  expected a 404 for a bogus record id, got a result")
        except SugarError as exc:
            print(f"PASS  error mapping — label={exc.label!r} status={exc.status_code} "
                  f"retry={exc.retry.value}")

        # 6. Capability probes for the optional Sugar-side package.
        session.caps.mcp_help = client.probe("mcp/help") is not None
        session.caps.mcp_schema = client.probe("mcp/schema/Accounts") is not None
        print(f"INFO  package endpoints — /mcp/help: {session.caps.mcp_help}, "
              f"/mcp/schema: {session.caps.mcp_schema}")

        # 7. Bulk quirks (version-prefixed url, data as a JSON string).
        results = client.bulk([
            {"method": "GET", "url": "ping"},
            {"method": "GET", "url": "Accounts/count"},
        ])
        print(f"PASS  POST /bulk — {len(results)} results: {results}")

    except SugarError as exc:
        print(f"FAIL  {exc.label}: {exc.message}")
        print(f"      {exc.spec.guidance}")
        return 1
    except Exception as exc:  # noqa: BLE001 - throwaway script, surface anything
        print(f"FAIL  {type(exc).__name__}: {exc}")
        return 1
    finally:
        session.close()

    print("-" * 72)
    print("Step 1 verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
