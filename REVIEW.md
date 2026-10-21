# SugarMCP — technical review

A walkthrough for someone who already knows Sugar or MCP. Setup, client config, and
credential options live in [README.md](README.md); the original design is [plan.md](plan.md).
This note is the argument, the layering, and the findings that revised the design.

## What it is

An MCP server that gives Claude (or any MCP client) read and write access to a SugarCRM
instance as one real Sugar user. It is not a Salesforce-style connector with a hard-coded
schema. The development instance has 80 custom fields on Accounts, 20 custom REST endpoints,
and six-figure record counts — so the server discovers its surface at runtime from Sugar's
Metadata API and inherits the logged-in user's ACLs rather than reimplementing permissions.

**Status.** Steps 1–5 and 7 are done and verified live. The optional Sugar-side package
(step 6) is built and scan-clean, **not installed**. 201 unit tests; 149 pass without the
gitignored fixtures.

## Architecture

```
Claude  ──stdio / JSON-RPC──►  server.py          FastMCP, tool registration
                                    │
                               tools/             thin wrappers, MCP annotations
                                    │
                               sugar/             transport-agnostic, imports nothing from mcp
                                    │
                                    └── HTTPS ──►  SUGAR_URL /rest/v11_*
```

| Layer | Owns | Look at |
|---|---|---|
| `server.py` | MCP instance, instructions, `SUGAR_READ_ONLY` registration | tool set assembly |
| `tools/read.py`, `tools/write.py` | verb-scoped tools, hints, `{"error": ...}` returns | the model-facing surface |
| `sugar/session.py` | password grant, refresh rotation, 401 retry, token file | auth |
| `sugar/client.py` | REST verbs, error envelope, `/bulk` | HTTP |
| `sugar/metadata.py` | cache, prune, compact describe | schema |
| `sugar/acl.py` | the one place denials are interpreted | permissions |
| `sugar/validation.py` | pre-flight writes against live metadata | why writes are safe |
| `sugar/shaping.py` | field projection, row clamp, filter encoding | context budget |
| `sugar/discovery.py` | endpoint catalog (`/mcp/help` or `/help` HTML) | custom API surface |
| `sugar-package/` | optional platform + JSON help + pruned schema | not required at runtime |

`sugar/` holds **no module-level token state**. A `SugarSession` is injected via a provider;
stdio uses one session from env. A later HTTP transport swaps the provider, not the tools.

Logging is **stderr only**. Stdout is the JSON-RPC stream.

## Three constraints that drive everything

1. **Nothing about modules or fields is hard-coded.** A tool written against
   `Accounts.industry` is worthless on the next instance. Every tool asks metadata.
2. **Permissions are Sugar's.** No service accounts. Login is `grant_type=password` as a
   real user; Sugar enforces ACLs on every call. This server must not invert them.
3. **Context budget is a first-class constraint.** Unshaped responses destroy the window.
   Shaping is not polish.

Those three are why the code looks the way it does. The rest of this note is the
consequences.

## Auth and session

`POST /oauth2/token` with a dedicated platform (`mcp` by default). Sugar allows **one session
per user per platform**. Logging in on `base` evicts the user's web UI. Same user twice on
the same custom platform evicts the first and kills its refresh token.

A dead token reports `invalid_grant`, not `need_login`. From the token endpoint that means
"credentials are wrong"; from an ordinary API call it means "this session is gone" — only a
full re-login recovers, because the refresh token died with it. `sugar/session.py`
distinguishes the two by where the response came from.

`authorization_code` and `client_credentials` are not implemented in Sugar
(`SugarOAuth2Storage`). OAuth connector flow is not available unless SugarIdentity is on.

The OAuth key for a custom platform must be `client_type='user'`. The default type fails
with `invalid_client`. Platform + key are per instance (Admin, or the optional package).

## ACLs — highest-risk code

The ACL block is on **`GET /me`**, not in module metadata. One call covers every module.

ACLs list **only denials**. Absence means allowed. Reading the payload as an allowlist
inverts every permission. `fields` is a JSON **list** when empty and an **object** when
populated (PHP array artifact). `admin` / `developer` are Studio rights, not data
permissions — a non-admin is `"no"` for both on every module.

All of that goes through [`sugar/acl.py`](sugar/acl.py). An admin's own map exercises almost
none of it; the non-admin fixture was captured via `POST /oauth2/sudo/:user_name` on a
dedicated `mcp_fixture` platform so neither the target's web session nor the caller's token
is evicted.

## Context budget (measured, not guessed)

| Call | Raw | After shaping |
|---|---|---|
| `describe Accounts` | 299 KB | 10.6 KB (prune → 42 KB; the rest is *shape*: one compact line per field) |
| 3 records, no `fields` | 23,964 B | 619 B with a projection |
| `GET /Reports/:id/records` | 940 KB (pagination params ignored) | 1.9 KB (`display_columns` only) |
| metadata revalidation | — | 117 B via `only_hash` |

`GET /Reports/:id/records` ignores `max_num` / `limit`. The full bean set arrives before
any client-side instruction can help — clamping has to happen in the tool.

## Tools and the approval boundary

Fourteen reads carry `readOnlyHint` — the set can be "always allow"ed. Six writes do not.
Delete, unlink, and the raw write hatch carry `destructiveHint`.

`SUGAR_READ_ONLY` **omits write tools from registration**. A tool that does not exist cannot
be approved by mistake.

Reads include schema (`list_modules`, `describe_module`, `get_enum`), records (`query`,
`count`, `get`, `get_related`, `search`), reports, and a read-only raw GET. Writes are
create / update / link / delete / unlink plus `sugar_api_call`.

Tools return `{"error": ...}` as data so the model can self-correct. Long-running work
reports **progress** via MCP `Context`; protocol `notifications/message` is deprecated.

`sugar_api_get` is read-only *by construction*, which is why it sits with the reads.

## Sugar does not validate writes

This is the load-bearing finding. Every one of these was **accepted and stored** on a live
25.2 instance:

| Write | What Sugar did |
|---|---|
| Create with no required `name` | Created a nameless Account |
| Unknown field | Silently dropped |
| Enum outside the dropdown | Stored verbatim |
| Write to read-only `date_entered` | Accepted |
| 400 chars into `len 150` | Truncated |
| `"not-a-number"` into an int | Stored as `"not-a-numb"` |

[`sugar/validation.py`](sugar/validation.py) is the only validation that exists. It runs
against the instance's own metadata, so a Studio field is validated as soon as it appears.

Same class of bug on reads: **filtering a non-db field is silently ignored**.
`Contacts.name` `$starts` returned every contact, not zero. Unknown fields 422. Query and
count tools warn on the first case.

## Routes that are not what you would guess

Probe the instance; the docs were wrong on several of these.

- Link filters are **GET** with bracket-encoded query params.
  `POST /<module>/:record/link/:link/filter` does not exist — that path *creates* a
  relationship.
- Filtered counts are `POST /<module>/filter/count`, not `POST /<module>/count`.
- Version negotiation is effectively dead: Sugar accepts any version at or below its max
  (`v11_30` returns 200 on 25.2). The ladder stays as insurance for older instances.

## Verification

Live scripts, one per layer — they write real records (prefixed `ZZ SugarMCP test`, cleaned
up in `finally`):

```
check_session → check_metadata → check_tools → check_write → check_discovery → check_stdio
```

`uv run pytest` is offline. Fixtures are gitignored (customer-identifying). Without them:
149 pass, 52 skip, including every ACL-inversion check. Counts pinned to this instance
(184 ACL modules, 707 endpoints, …) so a parser regression is a failure, not a tolerance.

## Known gaps / unproven

- Optional PHP package: built, Module-Loader-scan-clean, **never installed**. Runtime
  unproven. Everything it does can be done in Admin (platform + OAuth key); that is how
  the running setup actually works.
- Elasticsearch is on but empty here; `sugar_search` detects that and points at
  `sugar_query_records`.
- Metadata fixtures mixed 26.1 cloud + 25.2 dev; ACL fixtures are 25.2.

## Suggested walkthrough order

1. `server.py` — registration, read-only split, instructions the model sees.
2. `tools/read.py` / `tools/write.py` — hints, error-as-data, describe compact vs full.
3. `sugar/acl.py` + `tests/test_acl.py` — denial semantics, list-vs-object `fields`.
4. `sugar/validation.py` — why it exists; then `sugar/shaping.py` for the budget numbers.
5. `sugar/session.py` — platform slot, `invalid_grant`, refresh vs re-login.
6. `sugar/discovery.py` — catalog without the package; what the package would add.
7. One live script if the instance is up: `check_session.py` then `check_tools.py`.
