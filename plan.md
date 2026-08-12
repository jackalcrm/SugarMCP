# SugarMCP — Design Document

## Context

There is no general-purpose MCP server for SugarCRM. The goal is one that gives Claude (and any MCP
client) read/create/edit/delete access to a Sugar instance, modeled on the shape of the Salesforce
connector: a small set of tools split strictly by permission verb, so a user can "always allow" reads
while keeping writes on per-call approval.

The defining constraint is that **nothing about modules or fields may be hard-coded**. Sugar instances
are heavily customized — custom modules, custom fields, custom endpoints — so the server discovers its
own surface at runtime from the Metadata API and the service dictionary. A tool written against
`Accounts.industry` is worthless on the next instance; a tool that asks metadata what `Accounts` has
works everywhere.

Sugar's auth is per-user with no service accounts: `client_credentials` is not implemented
(`SugarOAuth2Storage` does not implement `IOAuth2GrantClient`), so the server logs in with
username/password and holds the resulting token. That token carries the user's ACLs, which means
permissions are enforced by Sugar rather than reimplemented here — a property worth leaning on.

**Decided:** Python + FastMCP; local stdio, layered so remote HTTP is a later transport swap;
ships with an optional Sugar-side package; verb-scoped generic tools. Target client is Claude
Code / Desktop. Development targets a Sugar 25.x ENT instance (API `v10`–`v11_26`).

---

## Architecture

```
Claude Code ──stdio/JSON-RPC──► server.py (FastMCP)
                                    │
                                tools/ (read.py, write.py)
                                    │
                                sugar/  session · client · metadata · discovery · shaping
                                    │
                                    └──HTTPS──► /rest/v11_26/*  (+ optional /mcp/* from the package)
```

Everything below `tools/` is transport-agnostic and holds **no module-level mutable token state**.
A `SugarSession` object owns credentials and tokens; tools obtain one from a provider function. Under
stdio there is exactly one session built from env. Moving to streamable-HTTP later replaces the
provider with a session-keyed map and adds an auth shim — no changes to tools, metadata, or shaping.

### Repo layout

```
SugarMCP/
├─ server.py                  # FastMCP instance, tool registration, transport
├─ sugar/
│  ├─ session.py              # OAuth2 password/refresh grant, token store, 401 retry
│  ├─ client.py               # REST verbs, error envelope mapping, /bulk batching
│  ├─ metadata.py             # module + field cache, pruning, ACL filtering, enum resolution
│  ├─ discovery.py            # endpoint catalog: /mcp/help JSON, else /help HTML fallback
│  ├─ shaping.py              # record trimming / result budgeting
│  └─ errors.py               # SugarApiException label → structured tool error
├─ tools/
│  ├─ read.py                 # readOnlyHint tools
│  └─ write.py                # create / update / delete, registered only if not read-only
├─ sugar-package/             # module-loadable package source (optional install)
├─ requirements.txt           # mcp>=1.0.0, httpx, python-dotenv
├─ .env.example
└─ README.md
```

Follows `mcp-starter` conventions — `@mcp.tool()` with docstring-driven schemas, `sugar_*` namespace,
thin MCP wrappers over pure modules that import nothing from `mcp` — with two deliberate deviations:

- **`logging` to stderr, never `print()`.** `mcp-starter/server_expanded.py` prints to stdout at
  startup, which corrupts the JSON-RPC stream under stdio. Do not copy that.
- **Tool annotations.** `readOnlyHint: True` on reads, `destructiveHint: True` on delete/unlink.
  This is what makes "always allow" on the read set legible and safe.

---

## Auth and session

`POST /rest/v11_26/oauth2/token`, `grant_type=password`, with `platform` from config.

```json
{"grant_type":"password","client_id":"mcp","client_secret":"...",
 "username":"...","password":"...","platform":"mcp"}
```

Response carries `access_token` (TTL 3600), `refresh_token` (TTL 1209600), `download_token`.
Subsequent calls send `OAuth-Token: <access_token>`.

Behaviors to implement in `sugar/session.py`:

- **Refresh rotation.** `OAuth2::createAccessToken()` deletes the old refresh token after issuing a
  new one. The newly returned refresh token must be persisted immediately or the session is lost.
- **401 retry.** On `need_login`, refresh once and replay the request. On a second failure, re-run the
  password grant. Never loop.
- **`max_session_lifetime`.** If `$sugar_config['oauth2']['max_session_lifetime']` is set, refreshed
  tokens inherit the *original* expiry, so refresh cannot continue forever. Fall back to a full login.
- **Token persistence.** Cache tokens at `~/.sugarmcp/tokens/<sha256(url|user|platform)>.json`, mode
  `0600`, so restarts don't re-login. The access token *is* the PHP session id — treat the file as a
  credential.
- **Never log tokens or passwords.** Redact `OAuth-Token`, `password`, `client_secret` in any debug
  logging of requests.

### Why a custom platform matters

`SugarOAuth2StorageBase::$numSessions = 1`. A password grant on platform `base` calls
`OAuthToken::cleanupOldUserTokens()` and **evicts the user's existing browser session** — they get
logged out of the Sugar web UI every time the MCP server starts. A dedicated `mcp` platform gets its
own session slot.

`disable_unknown_platforms` defaults to `true` in 25.1, so an unregistered platform fails hard:
`SugarApiExceptionInvalidParameter('EXCEPTION_INVALID_PLATFORM')`, HTTP 422. The server must detect
this and fall back to `base` with a prominent warning rather than dying — see graceful degradation.

The OAuth key for a custom platform **must have `client_type = 'user'`**. `isClientAllowed()` accepts
a key only when its `client_type` is `'user'` or matches the platform store's `clientType`, and
`SugarOAuth2StorageBase` (used for any custom platform) has `clientType = null`.

---

## Sugar-side package (optional)

A module-loadable package in `sugar-package/`. The server must work without it; installing it removes
the two worst rough edges (session eviction, HTML-only discovery) and cuts metadata transfer by ~99%.

**1. Platform registration** — `Extension/application/Ext/Platforms/mcp.php`:

```php
<?php
$platforms[] = 'mcp';
```

Compiles to `custom/application/Ext/Platforms/platforms.ext.php` on Rebuild Extensions. There is no
`$sugar_config['platforms']` key; this is the Extension framework.

**2. OAuth key** — post-install script creating an `OAuthKeys` bean: `c_key='mcp'`, generated
`c_secret`, `oauth_type='oauth2'`, `client_type='user'`. Print the secret once for `.env`.

**3. JSON help** — `custom/clients/base/api/McpHelpApi.php`, `GET /rest/v11_26/mcp/help`.

Core `HelpApi::getHelp()` already builds exactly the structure we want — it walks `$api->dict->dict`,
substitutes `?` path tokens with `:<pathVar>` to produce `fullPath`, resolves exception classes — and
then throws it away by rendering `include/api/help/extras/helpList.php` to HTML. This endpoint reuses
that walk and returns the array as JSON. Roughly 30 lines.

Two departures from core: **require login** (core `/help` is `noLoginRequired`; there is no reason to
expose a customer's custom endpoint surface anonymously), and support `?module=<Module>` /
`?q=<substring>` filtering so the catalog can be queried instead of dumped.

**4. Pruned schema** — `GET /rest/v11_26/mcp/schema/<module>`, returning the field projection defined
below, computed server-side. Turns a multi-hundred-KB metadata payload into a few KB over the wire.

### Graceful degradation

| Package absent | Behavior |
|---|---|
| No `mcp` platform (422 on login) | Warn loudly about web-session eviction, retry on `base` |
| No `mcp` OAuth key | Use `client_id='sugar'`, `client_secret=''` — auto-created by `SugarOAuth2Storage::getClientDetails()` |
| No `/mcp/help` (404) | Scrape `GET /help` HTML |
| No `/mcp/schema` (404) | `GET /metadata?type_filter=modules&module_filter=<M>` and prune client-side |

Probe once at startup, cache the capability flags on the session.

---

## Metadata layer

The core of the server, and where most of the engineering effort goes. `GET /metadata` unfiltered is
enormous; naive use will blow the context window on a single call.

**Startup (small).** `GET /metadata?type_filter=server_info,full_module_list,modules_info` — enough to
answer "what modules exist" and to record the Sugar version/flavor/edition for capability decisions.

**Per module (lazy).** On first `describe`/`query` touching a module:
`GET /metadata?type_filter=modules&module_filter=<Module>`, or `/mcp/schema/<Module>` when available.

**Caching.** Disk cache at `~/.sugarmcp/cache/<instance>/<user>/<Module>.json`, keyed by the `_hash`
Sugar returns per section. Revalidate cheaply with `only_hash=true`, or `POST /metadata` echoing known
hashes — Sugar omits unchanged chunks. `module_filter` is parsed with `str_getcsv()`, so module names
containing commas need quoting.

**Field projection.** Never hand a raw vardef to the model. Keep:

```
name · type · label · required · readonly · len · options (resolved)
link · relationship · related_module · id_name · calculated · source
```

Drop `dependency`, `validation`, `full_text_search`, `popupHelp`, display/styling keys, and the
`_hash` noise. Expect roughly 100× reduction.

**Enum resolution.** `GET /<module>/enum/<field>` returns a flat `{key: label}` map; cache it
(Sugar ETags it for 3600s, or 60s for function-backed vardefs). Prefer this over digging option keys
out of app_list_strings.

**ACL filtering — note the inversion.** `MetaDataManager::getAclForModule()` **strips `yes` values**
for brevity. *Absence of a key means allowed.* Reading these as a positive allowlist inverts every
permission, so centralize the check in one function and unit-test it. Field codes come from `SugarACL`:
omitted = read/write, `{write:no,create:no}` = read-only, `{read:no}` = hidden, all-no = no access.
Also honor `license: "no"` (license-gated fields).

Drop unreadable fields from `describe` output entirely — the model shouldn't ask for what it can't
have — and mark read-only ones so it doesn't attempt writes that will 403.

Record-level `_acl` on responses is only the *diff* from the module ACL (`array_diff_assoc`), so
`"_acl": {"fields": {}}` means "same as module" and is not interesting. Surface it only when non-empty.

---

## API discovery

`sugar/discovery.py` builds a catalog of endpoints — including custom ones, which appear automatically
because `HelpApi` walks the same service dictionary that routes real requests.

Preferred source is `/mcp/help` JSON. Fallback parses the HTML from `GET /rest/v11_26/help` — each
endpoint renders `reqType`, `fullPath`, `shortHelp`, and an inlined `longHelp` fragment when the file
exists on disk. Note that core endpoints whose `shortHelp` is empty are **skipped by the renderer**,
and several core `longHelp` paths point at `include/api/html/`, a directory that does not exist in
25.1 — so the HTML path is lossy. That lossiness is the argument for installing the package.

Cache the catalog per instance, invalidated on Sugar version change or on explicit refresh. Do not
load it at startup; fetch on first use of `sugar_list_endpoints` or `sugar_api_*`.

---

## Tool surface

Verb-scoped, module-as-parameter, no per-module tools. The split is exactly the approval boundary:
the read set is safe to "always allow"; the write set is not.

### Read — `readOnlyHint: True`

| Tool | Sugar call | Purpose |
|---|---|---|
| `sugar_whoami` | `GET /me` | User, roles, admin flag, ACL summary. Orientation. |
| `sugar_list_modules` | metadata `full_module_list` + `modules_info` | Accessible modules with labels; flags custom ones. |
| `sugar_describe_module` | metadata `modules` (pruned) | Fields, types, required, enums, links, ACL. The `getObjectSchema` analogue. |
| `sugar_search` | `POST /globalsearch` | Cross-module text search. The `find` analogue. |
| `sugar_query_records` | `POST /<module>/filter` | Filtered list with the filter DSL. The `soqlQuery` analogue. |
| `sugar_get_record` | `GET /<module>/<id>` | Single record, field-projected. |
| `sugar_get_related` | `GET /<module>/<id>/link/<link>/filter` | Related records. The `getRelatedRecords` analogue. |
| `sugar_list_endpoints` | discovery catalog | Documents the REST surface, custom endpoints included. |
| `sugar_api_get` | arbitrary `GET /rest/<ver>/<path>` | Escape hatch for custom read endpoints. Read-only by construction. |
| `sugar_server_info` | cached startup metadata | Version, flavor, platform in use, package-installed flags. |

### Write — registered only when `SUGAR_READ_ONLY` is unset

| Tool | Sugar call | Annotation |
|---|---|---|
| `sugar_create_record` | `POST /<module>` | — |
| `sugar_update_record` | `PUT /<module>/<id>` | — |
| `sugar_link_records` | `POST /<module>/<id>/link/<link>/<remote_id>` | — |
| `sugar_delete_record` | `DELETE /<module>/<id>` | `destructiveHint: True` |
| `sugar_unlink_records` | `DELETE /<module>/<id>/link/<link>/<remote_id>` | `destructiveHint: True` |
| `sugar_api_call` | arbitrary `POST/PUT/DELETE` | `destructiveHint: True` |

`sugar_api_get` / `sugar_api_call` are split by verb deliberately. A single combined raw-call tool
would mean one "always allow" silently grants delete; splitting it keeps the escape hatch inside the
same permission boundary as everything else. `sugar_api_call` should refuse paths it can reach through
a typed tool, so the model doesn't route around validation.

Write tools validate against cached metadata *before* calling Sugar — unknown field, wrong type, bad
enum value, or writing a read-only field returns a corrective error naming the valid options, rather
than a Sugar 422 the model has to guess at.

### Filter DSL

`sugar_query_records` exposes Sugar's filter syntax; the docstring must enumerate it, since the model
cannot discover it. Operators (from `FilterApi::addFieldFilter()`): `$equals`, `$not_equals`,
`$starts`, `$ends`, `$contains`, `$not_contains`, `$in`, `$not_in`, `$between`, `$dateBetween`,
`$is_null`, `$not_null`, `$empty`, `$not_empty`, `$lt`, `$lte`, `$gt`, `$gte`, `$dateRange`,
`$more_x_days_ago`, `$last_x_days`, `$next_x_days`, `$more_x_days_ahead`.
Macros (`FilterApi::addFilter()`): `$and`, `$or`, `$favorite`, `$owner`, `$creator`, `$tracker`,
`$following`. Related fields use `link_name.remote_field`. A bare scalar means `$equals`.

Use `POST /<module>/filter`, not GET — avoids URL-length limits and JSON-in-querystring escaping.

---

## Response shaping

A default `POST /<module>/filter` returns every list-view field on 20 records. Left alone this is the
single biggest context consumer in the server.

- **Always send `fields`.** Default to a small identity set (`id`, `name`, `date_modified`) plus
  whatever the caller names. Never let Sugar choose.
- **Clamp `max_num`**, default 20, ceiling ~100 (mirrors the `min(limit, 1000)` clamp in
  `mcp-starter`'s `mysql_query`).
- **Propagate `next_offset`** in the tool result and say so in the docstring; `-1` means exhausted.
  Include `total` from `/count` only when cheap.
- **Strip `_acl: {"fields": {}}`** and other empty scaffolding before returning.
- **Truncate long text fields** with an explicit `truncated: true` marker.
- **`GET /<module>/count`** for "how many" questions instead of fetching records.
- **`POST /bulk`** to batch related calls. Note its quirks: `url` must include the version prefix
  (`/v11_26/Accounts`) and `data` is a JSON **string**, not an object.

---

## Errors

Sugar returns `{"error": "<label>", "error_message": "..."}`. `sugar/errors.py` maps labels to
behavior rather than passing raw text through:

| Label | HTTP | Behavior |
|---|---|---|
| `need_login` | 401 | Refresh token, retry once |
| `invalid_grant` | 400 | Credentials wrong — actionable message, do not retry |
| `not_authorized` | 403 | Explain as an ACL denial, name module/field |
| `client_not_allowed` | 403 | `$sugar_config['api']['allowedClients']` blocks our User-Agent |
| `metadata_out_of_date` | 412 | Invalidate metadata cache, retry once |
| `edit_conflict` | 409 | Record changed underneath — report, do not auto-retry |
| `invalid_parameter` | 422 | Surface with the metadata-derived valid values |
| `incorrect_version` | 301 | Negotiate down the API version |

Follow `mcp-starter`'s convention of returning `{"error": ...}` as data rather than raising, so the
model can correct itself. Set a real User-Agent (`SugarMCP/<version>`) — some instances filter on it.

---

## Configuration

```
SUGAR_URL=https://sugar.example.com      # required, instance root (no /rest)
SUGAR_USERNAME=                          # required
SUGAR_PASSWORD=                          # required
SUGAR_PLATFORM=mcp                       # falls back to base if unregistered
SUGAR_CLIENT_ID=mcp                      # falls back to 'sugar'
SUGAR_CLIENT_SECRET=
SUGAR_API_VERSION=v11_26                 # negotiated down on 301
SUGAR_READ_ONLY=                         # set to 1 to omit write tools entirely
SUGAR_MAX_RECORDS=20
SUGAR_CACHE_DIR=~/.sugarmcp
SUGAR_VERIFY_SSL=1                        # 0 for local self-signed dev instances
```

`.env.example` committed with these plus inline comments; README carries the table and the
`claude mcp add` snippet.

---

## Build order

1. **`sugar/session.py` + `sugar/client.py`** — login, refresh, 401 retry, error mapping, token
   persistence. Verify with a throwaway script against Sugar251 before any MCP code exists.
2. **`sugar/metadata.py`** — module list, lazy per-module fetch, field projection, ACL filter, enum
   resolution, disk cache with hash revalidation.
3. **`server.py` + `tools/read.py`** — the read tool set, `readOnlyHint` annotations, stdio transport.
   This is the first genuinely usable milestone.
4. **`sugar/shaping.py`** — field projection defaults, clamping, truncation. Fold back into read tools.
5. **`tools/write.py`** — create/update/delete/link/unlink with pre-flight metadata validation and
   `SUGAR_READ_ONLY` conditional registration.
6. **`sugar-package/`** — platform extension, OAuth key installer, `McpHelpApi`, `McpSchemaApi`.
7. **`sugar/discovery.py`** — JSON path first, HTML fallback second, plus `sugar_api_get` /
   `sugar_api_call`.

Steps 1–5 deliver a working server against a stock instance. 6–7 are the polish that makes it good on
a customized one.

---

## Verification

**Against Sugar251 (local).** Install the package, Quick Repair and Rebuild, then confirm:
`curl -s "$SUGAR_URL/rest/v11_26/mcp/help" -H "OAuth-Token: $TOK" | jq '.endpoints | length'` returns
a non-empty catalog, and that a custom endpoint dropped into `custom/clients/base/api/` shows up in it
after a cache rebuild.

**Session eviction.** Log into the Sugar web UI, start the MCP server, confirm the browser session
survives on platform `mcp` and — as a control — that it *is* evicted on `base`. This is the whole
justification for the package; verify it rather than assuming it.

**ACL correctness — the highest-risk area.** Create a non-admin Sugar user with a role denying access
to one module and edit on one field. Confirm `sugar_list_modules` omits the module, `sugar_describe_module`
omits the field, and `sugar_update_record` on that field is refused *before* the HTTP call. Given that
`yes` values are stripped from the ACL payload, an inverted check would silently grant everything —
this needs unit tests over recorded fixtures, not just a manual pass.

**Metadata independence.** Add a custom module and a custom field in Studio, Quick Repair, then confirm
both appear in `describe` and are queryable with no code change. This is the central design claim.

**Token lifecycle.** Force expiry (or wait out `access_token_lifetime`), confirm one transparent
refresh and no re-login; delete the refresh token server-side and confirm clean fallback to password
grant.

**End to end in Claude Code.** `claude mcp add sugar -- python /path/to/SugarMCP/server.py`, then
exercise: "what modules are available", "describe Opportunities", "find accounts in Texas modified
this month", "create a task on that account". Confirm read tools can be always-allowed and that
delete prompts every time.

**Unit tests** over recorded HTTP fixtures for: ACL inversion, field projection, filter DSL
construction, error mapping, and refresh-token rotation. These are the parts where a bug is silent
rather than loud.
