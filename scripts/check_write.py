"""Verification for build step 5: write tools and pre-flight validation.

Two halves:

1. **Validation** — replays the exact invalid writes that a live Sugar instance *accepted*
   during design probing, and asserts every one is now refused before any HTTP call.
2. **Round trip** — creates a real record, updates it, links it, then deletes it and
   confirms it is gone. Records are named with a recognisable prefix and cleaned up even
   if the script fails partway.

    uv run scripts/check_write.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import build_server
from sugar.context import get_context

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

PREFIX = "ZZ SugarMCP test"


def unwrap(result):
    payload = getattr(result, "structured_content", None)
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


async def main() -> int:
    mcp = build_server()
    failures = 0
    created: list[str] = []

    async def call(name: str, **kwargs):
        return unwrap(await mcp.call_tool(name, kwargs))

    def check(condition: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        if condition:
            print(f"PASS  {label}")
        else:
            print(f"FAIL  {label}  {detail}")
            failures += 1

    try:
        print("=" * 78)
        print("Validation — every one of these was ACCEPTED by Sugar during probing")
        print("=" * 78)

        # Each entry is (label, values) that Sugar itself accepted and stored.
        rejections = [
            ("missing required name", {"billing_address_city": "Springfield"}),
            ("field that does not exist", {"name": PREFIX, "not_a_field_xyz": "1"}),
            ("enum value outside dropdown", {"name": PREFIX, "industry": "NOT_A_VALID_CODE"}),
            ("write to read-only field", {"name": PREFIX, "date_entered": "2020-01-01T00:00:00+00:00"}),
            ("text longer than the column", {"name": "Z" * 400}),
            # nps_score_c is a genuine int. (Accounts.employees looks numeric but is a
            # varchar(10), so it exercises the length rule, not the type rule.)
            ("string into an int field", {"name": PREFIX, "nps_score_c": "not-a-number"}),
            ("wrong type for a checkbox", {"name": PREFIX, "ship_to_account_c": "maybe"}),
            ("malformed date", {"name": PREFIX, "next_renewal_date": "31/01/2026"}),
        ]

        for label, values in rejections:
            result = await call("sugar_create_record", module="Accounts", values=values)
            refused = isinstance(result, dict) and result.get("error_label") == "validation_failed"
            check(refused, f"refused: {label}")
            if refused:
                for problem in result["problems"]:
                    print(f"        {problem[:110]}")
            elif isinstance(result, dict) and result.get("id"):
                created.append(result["id"])  # so it still gets cleaned up

        # A near-miss field name should suggest the real one.
        result = await call("sugar_create_record", module="Accounts",
                            values={"name": PREFIX, "industy": "101"})
        suggested = (
            isinstance(result, dict)
            and any("industry" in p for p in result.get("problems", []))
        )
        check(suggested, "typo 'industy' suggests 'industry'")

        # Valid values must not be blocked.
        print("\n" + "=" * 78)
        print("Round trip — create, update, link, delete")
        print("=" * 78)

        result = await call("sugar_create_record", module="Accounts", values={
            "name": f"{PREFIX} account",
            "industry": "101",
            "billing_address_city": "Springfield",
            "employees": 42,
        })
        ok = isinstance(result, dict) and result.get("created")
        check(ok, "create with valid values", json.dumps(result)[:160] if not ok else "")
        if not ok:
            return 1
        account_id = result["id"]
        created.append(account_id)
        print(f"        id={account_id}")

        result = await call("sugar_update_record", module="Accounts", record_id=account_id,
                            values={"billing_address_city": "Shelbyville", "employees": 43})
        check(isinstance(result, dict) and result.get("updated"), "update")

        result = await call("sugar_get_record", module="Accounts", record_id=account_id,
                            fields=["id", "name", "billing_address_city", "employees",
                                    "industry"])
        record = (result or {}).get("record", {})
        check(record.get("billing_address_city") == "Shelbyville",
              "update persisted", f"got {record.get('billing_address_city')!r}")
        check(str(record.get("employees")) == "43", "int persisted",
              f"got {record.get('employees')!r}")
        print(f"        {json.dumps(record)[:160]}")

        # Link a new contact to the account. A customized Contacts module may require several
        # custom fields that stock Sugar does not — exactly the kind of thing the
        # validator surfaces and a hard-coded tool would never know about.
        result = await call("sugar_create_record", module="Contacts", values={
            "last_name": f"{PREFIX} contact",
            "first_name": "Test",
            "title": "Tester",
            "phone_work": "+1 555 0100",
            "primary_address_country": "US",
            "buyer_persona_c": ["Purchasing Agent"],
            "languages_spoken_c": ["English"],
        })
        contact_ok = isinstance(result, dict) and result.get("created")
        check(contact_ok, "create contact", json.dumps(result)[:160] if not contact_ok else "")
        contact_id = result.get("id") if contact_ok else None

        if contact_id:
            result = await call("sugar_link_records", module="Accounts",
                                record_id=account_id, link_name="contacts",
                                related_ids=[contact_id])
            check(result.get("linked_count") == 1, "link records", json.dumps(result)[:160])

            result = await call("sugar_get_related", module="Accounts",
                                record_id=account_id, link_name="contacts",
                                fields=["id", "name"])
            check(result.get("count") == 1, "related record visible")

            result = await call("sugar_unlink_records", module="Accounts",
                                record_id=account_id, link_name="contacts",
                                related_id=contact_id)
            check(bool(result.get("unlinked")), "unlink records")

            result = await call("sugar_get_related", module="Accounts",
                                record_id=account_id, link_name="contacts",
                                fields=["id", "name"])
            check(result.get("count") == 0, "relationship removed")

            result = await call("sugar_delete_record", module="Contacts",
                                record_id=contact_id)
            check(bool(result.get("deleted")), "delete contact")
            if result.get("deleted"):
                created.remove(contact_id) if contact_id in created else None

        result = await call("sugar_delete_record", module="Accounts", record_id=account_id)
        check(bool(result.get("deleted")), "delete account")
        print(f"        deleted name={result.get('name')!r}")
        if result.get("deleted"):
            created.remove(account_id)

        result = await call("sugar_get_record", module="Accounts", record_id=account_id,
                            fields=["id"])
        check(isinstance(result, dict) and bool(result.get("error")), "record is gone")

    finally:
        # Nothing should survive this script, including anything a failure left behind.
        if created:
            print(f"\nCleaning up {len(created)} leftover record(s)")
            context = get_context()
            for record_id in created:
                for module in ("Accounts", "Contacts"):
                    try:
                        context.client.delete(f"{module}/{record_id}")
                        print(f"  removed {module}/{record_id}")
                        break
                    except Exception:  # noqa: BLE001
                        continue

    print("=" * 78)
    print(f"Step 5: {'verified' if not failures else f'{failures} failure(s)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
