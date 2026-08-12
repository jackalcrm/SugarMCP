# Recorded fixtures

**Not committed.** These are verbatim captures from a live Sugar instance and carry
customer-identifying detail: custom field and module names, instance identifiers
(`site_id`, `si_name`), and sample record data that Sugar renders into its own help page.

Regenerate them against whatever instance `.env` points at:

```bash
uv run scripts/capture_fixtures.py           # the four main captures
uv run scripts/capture_fixtures.py --list    # what exists, and how stale
uv run scripts/capture_acl_fixture.py <user_name> nonadmin   # a restricted user's ACL
```

Without them `uv run pytest` reports **139 passed, 52 skipped** — every synthetic test still
runs, including the whole ACL-inversion safety net. What skips is the set that asserts against
real payloads: exact module and endpoint counts, projection over genuine vardefs, and the
license-gated and hidden-field shapes that only a real instance produces.

| Fixture | What it covers |
|---|---|
| `metadata_startup.json` | server_info and the full module list |
| `metadata_modules_Accounts.json` | real vardefs — field projection and write validation |
| `me_acl_admin.json` | an admin's ACL block: module and field permission shapes |
| `me_acl_nonadmin.json` | a restricted user: 49 denied modules, 20 hidden fields |
| `help.html` | the rendered endpoint catalog, for the HTML discovery parser |

The counts asserted in the tests come from a Sugar 25.2 instance. On a different instance they
will differ, and those assertions will need updating — they are pinned deliberately, so that a
regression in the parser or projection shows up as a change rather than hiding under a
tolerance.
