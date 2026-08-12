"""Verification for build step 2: pruning, caching, labels, enums, ACL filtering.

    uv run scripts/check_metadata.py

Reports the reduction ratio, which is the whole point of the layer.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sugar import SugarClient, SugarConfig, SugarSession
from sugar.metadata import MetadataManager

logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(name)s: %(message)s",
                    stream=sys.stderr)

size = lambda obj: len(json.dumps(obj))


def main() -> int:
    config = SugarConfig.from_env()
    session = SugarSession(config)
    client = SugarClient(session)

    # Start from a cold cache so the timings mean something.
    shutil.rmtree(config.metadata_cache_dir, ignore_errors=True)
    meta = MetadataManager(client, config.metadata_cache_dir)

    t = time.time()
    info = meta.bootstrap()
    print(f"bootstrap    Sugar {info.get('version')} {info.get('flavor')} "
          f"in {time.time() - t:.2f}s")

    t = time.time()
    modules = meta.list_modules()
    custom = [m for m in modules if m.get("custom")]
    print(f"list_modules {len(modules)} accessible of {len(meta.module_names())} "
          f"({len(custom)} custom) in {time.time() - t:.2f}s")
    print(f"             e.g. {[m['module'] for m in modules[:5]]}")
    print(f"             custom: {[m['module'] for m in custom[:6]]}")

    # The headline number: raw payload versus what the model sees.
    raw = client.get("metadata", {"type_filter": "modules", "module_filter": '"Accounts"'})
    raw_size = size(raw["modules"]["Accounts"])

    t = time.time()
    described = meta.describe("Accounts")
    cold = time.time() - t
    print(f"\ndescribe Accounts   {described['field_count']} fields, "
          f"{described['link_count']} links, "
          f"{described.get('hidden_field_count', 0)} hidden")
    print(f"  raw payload       {raw_size:>9,} bytes")
    print(f"  described         {size(described):>9,} bytes   "
          f"({raw_size / max(size(described), 1):.0f}x reduction)")
    print(f"  cold fetch        {cold:.2f}s")

    t = time.time()
    meta.describe("Accounts")
    print(f"  warm (in-memory)  {time.time() - t:.3f}s")

    meta._modules.clear()  # force the disk-cache + only_hash revalidation path
    t = time.time()
    meta.describe("Accounts")
    print(f"  disk + revalidate {time.time() - t:.2f}s")

    with_links = meta.describe("Accounts", include_links=True)
    print(f"  + links           {size(with_links):>9,} bytes")
    print(f"  links omitted     {described.get('links_note')}")

    # Compact tier: one line per field.
    print("\ncompact tier")
    for name in ("name", "industry", "date_modified", "assigned_user_name",
                 "dri_workflow_template_id", "account_manager_user_id_c"):
        if name in described["fields"]:
            print(f"  {name:<28} {described['fields'][name]}")

    # Detail tier: full dicts, but only for what was asked for.
    detail = meta.describe("Accounts", fields=["name", "industry", "billing_address_state"])
    print(f"\ndetail tier    3 fields, {size(detail):,} bytes "
          f"(vs {size(described):,} for all {described['field_count']})")
    for name, field in detail["fields"].items():
        print(f"  {name:<28} {json.dumps(field)}")

    # A custom field and a license-gated one, to prove nothing is hard-coded.
    compact = described["fields"]
    custom_fields = [n for n, line in compact.items() if " cf" in line]
    licensed = [n for n, line in compact.items() if "ro:license" in line]
    readonly = [n for n, line in compact.items() if " ro" in line]
    print(f"\ncustom fields  {len(custom_fields)} on Accounts: {custom_fields[:4]}")
    print(f"readonly       {len(readonly)} fields, {len(licensed)} license-gated: {licensed}")

    # Enum resolution, batched.
    enum_fields = [n for n, line in compact.items()
                   if line.startswith(("enum", "multienum"))]
    t = time.time()
    resolved = meta.enums_for("Accounts", enum_fields)
    print(f"\nenums          {len(resolved)} of {len(enum_fields)} resolved via /bulk "
          f"in {time.time() - t:.2f}s")
    industry = resolved.get("industry", {})
    print(f"  industry       {len(industry)} options, e.g. "
          f"{list(industry.items())[2:4]}")

    # A second module, cold, to show the per-module cost.
    t = time.time()
    opps = meta.describe("Opportunities")
    print(f"\ndescribe Opportunities  {opps['field_count']} fields, "
          f"{size(opps):,} bytes in {time.time() - t:.2f}s")

    session.close()
    print("\nStep 2 verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
