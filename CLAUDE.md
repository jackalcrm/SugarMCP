# SugarMCP — working notes

MCP server for SugarCRM. Read [README.md](README.md) first; this file covers what a new
session needs to avoid re-deriving or contradicting.

## Commands

```bash
uv run pytest                      # 191 with fixtures; 139 pass / 52 skip without
uv run scripts/capture_fixtures.py # restore the gitignored fixtures from a live instance
uv run server.py                   # the MCP server (stdio)

uv run scripts/check_session.py    # login, refresh, retry, error mapping, bulk
uv run scripts/check_metadata.py   # pruning ratio, caching, labels, enums
uv run scripts/check_tools.py      # every read tool through MCP dispatch
uv run scripts/check_write.py      # validation + create/update/link/delete round trip
uv run scripts/check_discovery.py  # endpoint catalog and raw-call guards
uv run scripts/check_stdio.py      # real stdio transport + SUGAR_READ_ONLY

sugar-package/tools/scan.sh        # Module Loader policy scans (both of them)
```

`uv` lives at `~/.local/bin/uv` and is not on the default PATH in every context.

Development runs against a Sugar 25.x ENT dev instance; point the server at it with
`SUGAR_URL` in `.env`. Do not assume the instance is local — it is reached over HTTP and is
expected to become remote. Everything the server does goes through the REST API.

Two things need the instance *filesystem* rather than the API, and only work where that
filesystem is reachable (currently a `~/Sites` web root behind a vhost; over SSH/OrbStack
otherwise): writing the PHP package against the real `HelpApi`, and running the Module Loader
scanners. Point those at the web root with `SUGAR_ROOT`, and set `SUGAR_RUN` if it lives on
another host (see `sugar-package/tools/scan.sh`). The dev instance is heavily customized and
large — six-figure record counts on the core modules — which is why context-budget shaping
matters.

## Fixtures are not in the repo

`fixtures/` is gitignored — the captures carry customer-identifying detail. `capture_fixtures.py`
regenerates them; `tests/conftest.py:load_fixture` skips cleanly when they are absent, so a
fresh clone still runs 139 tests including every ACL-inversion check.

Counts asserted in the tests (184 ACL modules, 11/49 denied, 707 endpoints, 627 with a source
path) come from the 25.2 dev instance and will differ elsewhere. They are pinned deliberately
so a parser or projection regression shows up as a change rather than hiding under a tolerance.

## Verify against the instance, don't reason from the docs

Nearly every significant finding in this project contradicted a reasonable assumption, and
was only caught by probing the live instance. The design document ([plan.md](plan.md)) was
carefully researched and still got several things wrong. Prefer a five-line probe script over
an inference.

**Sugar does not validate writes.** A create with no required `name` succeeds. An unknown
field is silently dropped. An enum value outside the dropdown is stored verbatim. A string
into an int column is stored truncated. [`sugar/validation.py`](sugar/validation.py) is
therefore load-bearing, not polish — it is the only validation that exists.

**Filtering a non-db field is silently ignored.** `Contacts.name` with `$starts` returned
every contact in the module instead of 0. Unknown filter fields *do* 422, so the two cases are handled
differently — see `check_filter`.

**ACLs list only denials; absence means allowed.** Reading them as an allowlist inverts every
permission. All ACL logic goes through [`sugar/acl.py`](sugar/acl.py) and is the most heavily
tested module here. `fields` is a JSON list when empty and an object when populated.

**The ACL block is on `GET /me`**, not in module metadata. One call covers all 184 modules.

**Some routes are not what you would guess.** `POST /<module>/:record/link/:link/filter` does
not exist — that path matches the relationship-*creating* endpoint. Link filters are GET with
bracket-encoded query params. Filtered counts are `POST /<module>/filter/count`, not
`POST /<module>/count`.

**A dead token reports `invalid_grant`, not `need_login`.** Only a full re-login recovers.

## Context budget is a first-class constraint

The instance is large and heavily customized, so unshaped responses destroy the context
window. Three numbers worth remembering:

- `GET /Reports/:id/records` ignores all pagination params: 940 KB for 109 rows, 220 fields
  each. `sugar_run_report` projects to the report's `display_columns` → **1.9 KB**.
- `describe Accounts`: 299 KB raw → **10.6 KB** returned. Pruning alone only got 42 KB; the
  rest came from *shape* — one compact line per field, links behind a flag, detail only for
  named fields.
- A query for 3 records with no `fields` projection is **23,964 B**; with one, **619 B**.
- Metadata revalidation is 117 B via `only_hash`.

If you add a tool, measure what it returns before considering it done.

## Credentials and multi-user

Under stdio the server runs on the user's machine, so nothing is held server-side. Password
resolution is environment first, then OS keychain (`scripts/set_credentials.py`), so existing
setups keep working. A keychain that is missing, locked or broken must never take startup
down — `_keyring_lookup` wraps the import too, not just the read.

Verified Sugar session constraints, which govern any multi-user design:

- Different users on the same platform coexist.
- **The same user twice on the same platform evicts the first**, and kills its refresh token.
  Per-client platforms (`mcp_desktop`, `mcp_web`) avoid it; platforms can be added in Admin,
  the package is not required for that.
- `authorization_code` and `client_credentials` are **not supported** — `SugarOAuth2Storage`
  implements only `IOAuth2GrantUser`, `IOAuth2RefreshTokens` and `IOAuth2GrantExtension`. So
  an OAuth connector flow is not available unless SugarIdentity (OIDC) is enabled.
- Access token 3600 s, refresh token 1209600 s. `max_session_lifetime` is unset here, so
  refreshing rolls indefinitely; where it *is* set, refresh tokens inherit the original
  expiry and a full re-login eventually becomes mandatory.

## Conventions

- Nothing about modules or fields is hard-coded, ever. That is the whole design premise.
- `sugar/` imports nothing from `mcp`; `tools/` is a thin wrapper. Keeps the layer testable
  and makes the stdio → HTTP transport swap a change to session provisioning only.
- Logging to **stderr**, never `print()` — stdout carries the JSON-RPC stream.
- Tools return `{"error": ...}` as data rather than raising, so the model can self-correct.
- Read tools carry `read_only_hint`; writes do not; destructive ones carry
  `destructive_hint`. That split is the approval boundary — do not blur it. `sugar_api_get`
  is read-only *by construction*, which is why it may sit with the reads.
- `SUGAR_READ_ONLY` omits write tools from registration entirely rather than refusing at call
  time. A tool that does not exist cannot be approved by mistake.
- Tool descriptions are captured by `@mcp.tool` at decoration time — assigning `__doc__`
  afterwards changes nothing the model sees. Pass `description=` for a computed one.

## Environment gotchas

- MCP SDK is **2.0**: `mcp.server.fastmcp` no longer exists, it is
  `mcp.server.mcpserver.MCPServer`, and model fields are snake_case
  (`structured_content`, `server_info`, `input_schema`). `mcp-starter` and plan.md both
  assume the old API.
- Verification scripts write real records to a real CRM. Prefix them `ZZ SugarMCP test` and
  clean up in a `finally` block.
- Test records: check leftovers on `Contacts.last_name`, not `Contacts.name` — the latter
  silently matches everything.

## Sugar-side package (sugar-package/)

Optional; the server works without it and probes at startup. Module Loader runs **two**
independent scans — `ModuleScanner` (function/class denylist) and Rector (PHP compatibility).
A package can pass one and fail the other; `tools/scan.sh` runs both. Every callback-taking
function and every filesystem read is denied.

Currently **built and scan-clean but never installed**, so the PHP is unproven at runtime.
