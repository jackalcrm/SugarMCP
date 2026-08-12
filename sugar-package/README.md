# SugarMCP Support Package

Optional module-loadable package for the SugarMCP server. **The server works without it.**

Build: `dist/sugarmcp-0.1.0.zip` — install via **Admin → Module Loader**, then run
**Admin → Repair → Quick Repair and Rebuild**.

> **Status: passes Module Loader's package scan; not yet installed.** The scan is verified
> (see below). Runtime behaviour is not — treat the PHP as unproven until it has been
> installed once and `scripts/check_session.py` reports `/mcp/help: True`.

## Module Loader restrictions

Module Loader runs **two independent scans**, and a package can pass one and be rejected by
the other. This package failed each in turn before passing both:

| Scan | What it checks | Reported as |
|---|---|---|
| `ModuleScanner` | Function/class denylist — callbacks, filesystem, exec, network | "File Issues" |
| Rector | Code patterns that break on newer PHP | "PHP Compatibility Issues" |

### Rector (PHP compatibility)

Rules are in `src/Rector/config.php` on the instance. The one that caught this package was
`StringifyStrNeedlesRector`: an untyped `strpos()` needle, which PHP 7.3 stopped coercing.
Fixed by declaring `string $needle` rather than taking Rector's suggested `(string)` cast —
the type is the actual contract, the cast just silences the check.

Note this scan reports as a **downloadable report** rather than inline text, so the specific
finding is easy to miss. `tools/scan.sh` prints it directly.

### ModuleScanner (denylist)

Rejects, among other things, **every callback-taking function** (a callback can smuggle in
arbitrary code) and **every filesystem read**. The first build violated four of those rules.
What that cost:

| Rule | Consequence here |
|---|---|
| `usort` denied | Sorting uses a keyed array plus `ksort` |
| `array_map` denied | CSV argument parsing uses an explicit loop |
| `file_get_contents`, `is_readable` denied | `/mcp/help` **cannot inline** long-form help text; it reports the fragment's path instead |

The third is a genuine capability loss, not a workaround: a package cannot read files, so the
long-form help bodies stay out of reach. Little is lost in practice — several core `longHelp`
paths point at `include/api/html/`, which does not exist in 25.x.

Other categories worth knowing: only certain file extensions are permitted (this package ships
only `.php`), and `ReflectionClass`, `ZipArchive`, `SplFileObject`, `ob_start`, variable
functions `$f()` and backticks are all denied. SugarCloud instances cannot relax any of it;
on-site instances can, via `config_override.php` — but needing to is a smell.

### Verify before uploading

```bash
./tools/scan.sh          # both scans against dist/*.zip
```

```
== 1/2  ModuleScanner (denylist) ==
CLEAN — no package-scan issues

== 2/2  Rector (PHP compatibility) ==
Scanning 5 PHP file(s)
CLEAN — no PHP compatibility issues

PACKAGE OK — both scans clean
```

This runs the same two scanners Module Loader uses, against the same instance configuration,
so violations surface at build time instead of after upload. Both harnesses were checked
against deliberately bad files first and reproduced the real failures exactly, so a `CLEAN`
result means something.

Set `SUGAR_HOST` / `SUGAR_ROOT` to point at a different instance.

## What it does

**1. Registers an `mcp` API platform.** `SugarOAuth2StorageBase::$numSessions = 1`, so a login
on an existing platform evicts whatever session holds that slot — an integration logging in on
`base` logs the user out of the Sugar web UI every time it starts. A platform of its own gets
its own slot.

**2. Creates an OAuth consumer key** with `client_type = 'user'`. That type is mandatory:
`isClientAllowed()` accepts a key only when its `client_type` is `'user'` or matches the
platform store's `clientType`, and `SugarOAuth2StorageBase` — what any custom platform gets —
has `clientType = null`. The generated secret is printed once during install and is not
recoverable afterwards.

**3. Adds `GET /mcp/help`** — the endpoint catalog as JSON. Core's `HelpApi::getHelp()` already
builds this structure and then discards it rendering HTML. The HTML path is lossy in ways
parsing cannot recover:

- endpoints whose `shortHelp` is empty are **skipped entirely** by the renderer;
- 80 of 707 rows render without their source file, so custom endpoints cannot always be
  distinguished from stock ones;
- several core `longHelp` paths point at `include/api/html/`, which does not exist in 25.x;
- the response is ~3.9 MB of markup.

**4. Adds `GET /mcp/schema/:module`** — the field projection computed server-side, turning a
~299 KB metadata response into a few KB, with labels already resolved (saving the client a
separate ~1.7 MB `GET /lang/:lang`, which has no per-module variant).

## Value on a given instance

Can be lower than the design document assumed, which is worth knowing before installing.
This was measured on one customized 25.x ENT instance; yours may differ:

| Benefit | Observed |
|---|---|
| Avoid web-session eviction | **May not be needed** — if platform `mcp` is already accepted (`disable_unknown_platforms` off), the server logs in without eviction anyway |
| Complete JSON endpoint catalog | **Real** — the HTML fallback loses 80 source paths and any endpoint lacking a description |
| Smaller metadata transfer | **Modest** — client-side pruning already achieves 299 KB → 10.6 KB |

The endpoint catalog is the honest reason to install it here.

## Layout

```
src/
├─ manifest.php
├─ Extension/application/Ext/Platforms/mcp.php   → registers the platform
├─ clients/base/api/McpHelpApi.php               → GET /mcp/help
├─ clients/base/api/McpSchemaApi.php             → GET /mcp/schema/:module
└─ scripts/post_install.php                      → creates the OAuth key
```

## After installing

Put the printed credentials in the server's `.env`:

```
SUGAR_PLATFORM=mcp
SUGAR_CLIENT_ID=mcp
SUGAR_CLIENT_SECRET=<printed once during install>
```

Then confirm the endpoints are live:

```bash
uv run scripts/check_session.py     # expect: /mcp/help: True, /mcp/schema: True
uv run scripts/check_discovery.py   # expect: catalog_source "mcp/help", not "help-html"
```

The server detects both endpoints automatically and falls back if either is missing, so a
partial install degrades rather than breaks.
