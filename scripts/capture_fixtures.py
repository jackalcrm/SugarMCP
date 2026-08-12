"""Capture the test fixtures from a live Sugar instance.

The fixtures are **not committed**: they are verbatim captures from a real instance and carry
customer-identifying detail — custom field and module names, instance identifiers, and in the
help page, sample record data. They are regenerated on demand instead.

The cost of that choice: without them, the ~143 tests that assert against real payloads skip.
Run this once after cloning to restore the full suite.

    uv run scripts/capture_fixtures.py            # capture everything
    uv run scripts/capture_fixtures.py --list     # show what exists and how stale
    uv run scripts/capture_fixtures.py --clean    # delete captures

The non-admin ACL fixture needs a second user and is captured separately, because it requires
choosing whose permissions to record:

    uv run scripts/capture_acl_fixture.py <user_name> nonadmin
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sugar import SugarClient, SugarConfig, SugarError, SugarSession

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# name -> (what the tests need it for, how to fetch it)
CAPTURES = {
    "metadata_startup.json": "server_info and the full module list",
    "metadata_modules_Accounts.json": "real vardefs for field projection and validation",
    "me_acl_admin.json": "an admin's ACL block — module and field permission shapes",
    "help.html": "the rendered endpoint catalog, for the HTML discovery parser",
}


def capture(client: SugarClient) -> dict[str, int]:
    """Fetch each fixture and write it. Returns name -> bytes written."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}

    def write_json(name: str, payload) -> None:
        path = FIXTURES / name
        path.write_text(json.dumps(payload, indent=1, sort_keys=True))
        written[name] = path.stat().st_size

    print("metadata_startup.json ...", flush=True)
    write_json(
        "metadata_startup.json",
        client.get("metadata", {"type_filter": "server_info,full_module_list"}),
    )

    print("metadata_modules_Accounts.json ...", flush=True)
    write_json(
        "metadata_modules_Accounts.json",
        client.get("metadata", {"type_filter": "modules", "module_filter": '"Accounts"'}),
    )

    print("me_acl_admin.json ...", flush=True)
    me = client.get("me")
    acl = (me.get("current_user") or {}).get("acl") or {}
    # Permission map only — never the surrounding user record, which is full of personal data.
    write_json("me_acl_admin.json", acl)

    print("help.html ...", flush=True)
    page = client.get("help")
    if not isinstance(page, str):
        page = str(page)
    path = FIXTURES / "help.html"
    path.write_text(page, encoding="utf-8")
    written["help.html"] = path.stat().st_size

    return written


def show() -> int:
    print(f"{'fixture':<34} {'size':>10}  {'age':>10}  purpose")
    print("-" * 100)
    missing = 0
    for name, purpose in CAPTURES.items():
        path = FIXTURES / name
        if path.exists():
            age = (time.time() - path.stat().st_mtime) / 86400
            print(f"{name:<34} {path.stat().st_size:>10,}  {age:>7.1f} d  {purpose}")
        else:
            missing += 1
            print(f"{name:<34} {'-':>10}  {'-':>10}  {purpose}")

    extra = FIXTURES / "me_acl_nonadmin.json"
    if extra.exists():
        print(f"{extra.name:<34} {extra.stat().st_size:>10,}  {'':>10}  "
              "a restricted user's ACL — hidden fields and broad denial")
    else:
        print(f"{extra.name:<34} {'-':>10}  {'-':>10}  "
              "capture with scripts/capture_acl_fixture.py <user> nonadmin")

    if missing:
        print(f"\n{missing} fixture(s) missing — the tests that need them will skip.")
        print("Run: uv run scripts/capture_fixtures.py")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="show what exists")
    parser.add_argument("--clean", action="store_true", help="delete captured fixtures")
    args = parser.parse_args()

    if args.list:
        return show()

    if args.clean:
        removed = 0
        for path in list(FIXTURES.glob("*.json")) + list(FIXTURES.glob("*.html")):
            path.unlink()
            removed += 1
        print(f"Removed {removed} fixture file(s).")
        return 0

    config = SugarConfig.from_env()
    session = SugarSession(config)
    client = SugarClient(session)
    print(f"Capturing from {config.url} as {config.username}\n")

    try:
        written = capture(client)
    except SugarError as exc:
        print(f"\nFAIL  {exc.label}: {exc.message}")
        print(f"      {exc.spec.guidance}")
        return 1
    finally:
        session.close()

    print()
    total = 0
    for name, size in written.items():
        print(f"  {name:<34} {size:>10,} B")
        total += size
    print(f"  {'total':<34} {total:>10,} B")
    print(
        "\nCaptured. These are gitignored — they contain instance-specific detail.\n"
        "For the restricted-user ACL fixture (hidden fields, broad module denial):\n"
        "    uv run scripts/capture_acl_fixture.py <user_name> nonadmin\n"
        "\nThen: uv run pytest"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
