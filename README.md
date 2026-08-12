# SugarMCP

An MCP server for SugarCRM, giving Claude read and write access to a Sugar instance.

**Nothing about modules or fields is hard-coded.** Sugar instances are heavily customized —
the development instance here has 80 custom fields on Accounts alone and 20 custom REST
endpoints — so the server discovers its surface at runtime from the Metadata API. A tool
written against `Accounts.industry` is worthless on the next instance; one that asks metadata
what `Accounts` has works everywhere.

Permissions are **not reimplemented**. Sugar has no service accounts, so the server logs in as
a real user and inherits that user's ACLs, which Sugar itself enforces.

## Status

| Step | | |
|---|---|---|
| 1 | Session, client, error mapping | done, verified live |
| 2 | Metadata: pruning, caching, labels, enums | done, verified live |
| 3 | Read tools over stdio | done, verified live |
| 4 | Response shaping | done, folded into the read tools |
| 5 | Write tools + pre-flight validation | done, verified live |
| 6 | Sugar-side package | built, **not installed** |
| 7 | Endpoint discovery, raw API escape hatches | done, verified live |

191 unit tests pass, plus six scripts that verify each layer against a live instance.

## Setup on a new machine

Requires Python 3.10+. This project uses [uv](https://docs.astral.sh/uv/), which manages its
own Python — nothing else needs installing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # if uv is not present
git clone <this-repo> SugarMCP && cd SugarMCP
uv sync                                            # creates .venv, installs deps
cp .env.example .env                               # then fill in URL + credentials
uv run scripts/check_session.py                    # confirm it can reach the instance
```

`check_session.py` is the fastest way to know whether a new environment is wired up
correctly: it logs in, refreshes, maps an error, and runs a bulk call, without any MCP
involvement.

### Register with Claude Code

```bash
claude mcp add sugar -- uv --directory "$(pwd)" run server.py
```

### Register with Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (Windows:
`%APPDATA%\Claude\claude_desktop_config.json`), substituting your own paths:

```json
{
  "mcpServers": {
    "sugar": {
      "command": "/absolute/path/to/uv",
      "args": ["--directory", "/absolute/path/to/SugarMCP", "run", "server.py"]
    }
  }
}
```

Two things that bite here:

- **Use the absolute path to `uv`.** Claude Desktop does not inherit your shell's `PATH`.
  Find it with `which uv` (commonly `~/.local/bin/uv`).
- **On macOS 15+, a private-address instance needs Local Network permission.** Every request
  fails with `[Errno 65] No route to host` without it, because macOS reports the denial as
  unreachability. Grant it to the binary in the `command` field (that is what opens the
  socket, not Claude itself) under System Settings > Privacy & Security > Local Network — or
  sidestep it entirely by pointing `SUGAR_URL` at `127.0.0.1`, which is exempt.

### Where credentials live

Three options, all machine-local — under stdio the server runs on your machine, so nothing is
ever held server-side.

**1. OS keychain (recommended).** No password in any file:

```bash
uv sync --extra keyring
uv run scripts/set_credentials.py      # prompts, stores in the keychain
```

Then the client config needs only the non-secret parts:

```json
"env": { "SUGAR_URL": "https://sugar.example.com", "SUGAR_USERNAME": "user" }
```

Encrypted at rest, access-controlled by the OS, and impossible to commit to a repo or sync to
a cloud drive by accident — which is the failure that actually happens. Entries are keyed by
`url|username`, so the same account on a sandbox and on production stay separate.

**2. Client config `env` block.** Put `SUGAR_PASSWORD` in the server block above. Machine-local,
but plaintext on disk.

**3. `.env` file.** Same trade-off; ignored by git.

Environment beats keychain, so anything configured today keeps working. If `keyring` is not
installed, or the keychain is locked or unavailable, the server logs a warning and falls back
to the environment rather than failing.

To run read-only, add `"env": {"SUGAR_READ_ONLY": "1"}` to the server block — the write tools
are then never registered.

## Tools

The fourteen read tools are annotated `readOnlyHint`, so the whole set can be "always allow"ed
without that decision ever granting a write. The six write tools carry no such hint, and the
two that destroy data — plus the raw write escape hatch — carry `destructiveHint` so a client
prompts every time.

| Tool | Purpose |
|---|---|
| `sugar_whoami` | Who the server is authenticated as, and what they are denied |
| `sugar_server_info` | Version, edition, negotiated platform and API version |
| `sugar_list_modules` | Accessible modules with labels; flags custom ones |
| `sugar_describe_module` | Fields, types, required/read-only, ACL — the schema tool |
| `sugar_get_enum` | Resolve a dropdown's valid values as customized here |
| `sugar_query_records` | Filtered queries using Sugar's filter DSL |
| `sugar_count_records` | Counts without fetching records |
| `sugar_get_record` | One record by id |
| `sugar_get_related` | Records related through a named link |
| `sugar_search` | Cross-module full-text search |
| `sugar_list_reports` | Find saved reports |
| `sugar_run_report` | Run a saved report, projected to its display columns |
| `sugar_list_endpoints` | Search the instance's REST surface, custom endpoints included |
| `sugar_api_get` | Raw GET for custom read endpoints — read-only by construction |

### Write — omitted entirely when `SUGAR_READ_ONLY` is set

| Tool | Purpose | |
|---|---|---|
| `sugar_create_record` | Create a record | |
| `sugar_update_record` | Update named fields on a record | |
| `sugar_link_records` | Relate records through a link | |
| `sugar_delete_record` | Delete a record | `destructiveHint` |
| `sugar_unlink_records` | Remove a relationship | `destructiveHint` |
| `sugar_api_call` | Raw POST/PUT/PATCH/DELETE for custom write endpoints | `destructiveHint` |

## Configuration

See `.env.example` for the annotated list. The three required values are `SUGAR_URL`,
`SUGAR_USERNAME` and `SUGAR_PASSWORD`.

Two settings are worth understanding:

**`SUGAR_PLATFORM`** (default `mcp`). `SugarOAuth2StorageBase` allows one session per
platform, so logging in on `base` runs `cleanupOldUserTokens()` and **logs the user out of the
Sugar web UI**. A dedicated platform gets its own session slot. If the instance has
`disable_unknown_platforms` enabled and the platform is not registered, login returns HTTP 422
and the server falls back to `base` with a loud warning.

**`SUGAR_READ_ONLY`**. When set, the write tools are *not registered at all*, rather than
registered and refused at call time. A tool that does not exist cannot be approved by mistake.

## Verification scripts

Each build step has a script that exercises it against a live instance:

```bash
uv run scripts/check_session.py    # login, refresh, retry, error mapping, bulk
uv run scripts/check_metadata.py   # pruning ratio, caching, labels, enums
uv run scripts/check_tools.py      # every read tool through MCP dispatch
uv run scripts/check_write.py      # validation, then a create/update/link/delete round trip
uv run scripts/check_discovery.py  # endpoint catalog and the raw-call guards
uv run scripts/check_stdio.py      # the real stdio transport, as a client runs it
uv run pytest                      # unit tests, offline
```

### Test fixtures are not committed

The unit tests assert against verbatim captures from a live instance, and those captures carry
customer-identifying detail — custom field and module names, instance identifiers, and sample
records that Sugar renders into its own help page. They are gitignored and regenerated on
demand:

```bash
uv run scripts/capture_fixtures.py            # capture from whatever .env points at
uv run scripts/capture_fixtures.py --list     # what exists, and how stale
uv run scripts/capture_acl_fixture.py <user_name> nonadmin   # a restricted user's ACL
```

Without them the suite reports **139 passed, 52 skipped**, with a message naming the command.
Everything synthetic still runs, including the whole ACL-inversion safety net; what skips is
the set asserting against real payloads. See [fixtures/README.md](fixtures/README.md).

## Design notes

The full design document is in [plan.md](plan.md). Findings from building against a live
instance that revised it:

**The ACL block lives on `GET /me`, not in module metadata.** One call returns ACLs for all
184 modules, so no per-module permission fetch is needed anywhere.

**ACLs list only denials.** `MetaDataManager::getAclForModule()` strips `yes` values, so
*absence of a key means allowed*. Reading it as an allowlist inverts every permission. All of
it goes through [`sugar/acl.py`](sugar/acl.py), which is the most heavily tested module here
for exactly that reason.

**`fields` in an ACL block is a JSON list when empty and an object when populated** — a PHP
array artifact. On the reference instance 175 modules send `[]` and 9 send `{}`.

**Pruning alone is not enough.** Dropping views/layouts/dependencies and projecting vardefs
gets a 299 KB module payload down to ~42 KB, which is still far too much. The rest comes from
the *shape* of the answer: `describe` returns one compact line per field by default and full
dicts only for fields you name. That is 299 KB → 10.6 KB, a 28× reduction.

**Always send `fields` on a query.** Three Accounts records with no projection is 23,964
bytes; with one it is 619.

**Link filters are GET-only.** `POST /<module>/:record/link/:link/filter` does not exist — it
instead matches `POST /<module>/:record/link/:link/:remote_id`, which *creates a
relationship*. Read tools encode link filters into the query string
(`sugar/shaping.py:encode_filter_params`) and send them by GET.

**Filtered counts use a different route** than unfiltered ones: `POST /<module>/filter/count`
versus `GET /<module>/count`.

**The report endpoint ignores every pagination parameter.** `GET /Reports/:id/records` returns
the complete result set with full beans regardless of `max_num`, `maxNum` or `limit` — one
ordinary report here is 940 KB across 109 rows at 220 fields each. `sugar_run_report` clamps
rows and projects to the report's own `display_columns` (read from `GET /Reports/:id/filter`),
which is 940 KB → 1.9 KB. No client-side instruction can fix this; the payload arrives first.

**Version negotiation is effectively dead code.** Sugar accepts any version string at or below
its maximum — `v11_30` returns 200 on a 25.2 instance — so the 301 `incorrect_version` path
never fires. The ladder stays as insurance for older instances.

**Logging goes to stderr.** Under stdio, stdout carries the JSON-RPC stream; a stray `print()`
corrupts it and the client reports the server as broken with no useful error.

**A dead token reports `invalid_grant`, not `need_login`.** When a session is evicted — another
login taking the same platform slot — the next call 401s with `invalid_grant`. From the token
endpoint that label means "credentials are wrong, do not retry"; from an ordinary API call it
means "this token is dead", which only a full re-login fixes, because the refresh token died
with it. The client distinguishes the two by where the response came from.

**`admin` and `developer` are not data permissions.** Sugar reports both on every module, and
a non-admin gets `"no"` for both on all 184. They grant Studio rights; counting them as
denials would report every module as restricted.

### Sugar does not validate writes

This is the most consequential finding, and the reason [`sugar/validation.py`](sugar/validation.py)
exists. Every one of these was **accepted and stored** by a live 25.2 instance:

| Write | What Sugar did |
|---|---|
| Create with no `name` (required) | Created a nameless Account |
| Field that does not exist | Silently discarded it |
| `industry: "NOT_A_VALID_CODE"` | Stored it verbatim |
| Write to read-only `date_entered` | Accepted it |
| 400 chars into a `len 150` column | Truncated silently |
| `"not-a-number"` into an int column | Stored as `"not-a-numb"` |

So pre-flight validation is not about producing friendlier errors than Sugar's — Sugar does
not produce an error at all. Without it, a model's guess becomes corrupted CRM data. Every
check runs against the instance's own metadata, so a field added in Studio is validated as
soon as it appears.

The same applies to reads: **filtering on a non-database field is silently dropped**. Filtering
`Contacts.name` (a computed full-name field) with `$starts` returned every contact in the
module rather than none — a query that looks like a successful match on everything. `sugar_query_records`
and `sugar_count_records` warn when a filter touches such a field. An *unknown* field is
different: Sugar rejects that with a 422, and the server pre-empts it to suggest the intended
name.

### Capturing ACL fixtures

ACL handling is the highest-risk code here, and an admin's own permission map exercises almost
none of it. `POST /oauth2/sudo/:user_name` gets a token as another user, which is how the
non-admin fixture was captured:

```bash
uv run scripts/capture_acl_fixture.py <user_name> <label>
```

Two things that endpoint will do if you let it. It defaults to platform `base`, which ends the
target user's web session; and passing the *caller's* platform evicts the caller's own token,
since there is one session per platform slot. The script passes a dedicated `mcp_fixture`
platform to avoid both. Only the permission map is written — no names, ids or emails.

## Known gaps

- Elasticsearch is enabled on the dev instance but its index is empty, so `sugar_search`
  returns nothing there. The tool detects this and points at `sugar_query_records`.
- `fixtures/metadata_*.json` and `help.html` came from a Sugar **26.1 cloud sandbox**, while
  the dev instance is **25.2**. Representative, not authoritative. The ACL fixtures are from
  the 25.2 instance.
