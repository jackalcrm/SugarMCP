# SugarMCP remote auth & identity — design note

Status: **design, not built.** The HTTP transport spike ([DEPLOY.md](DEPLOY.md),
[transport.py](transport.py)) proved Claude can reach SugarMCP over public HTTPS with a static
bearer. This note is the next milestone: making SugarMCP addable as a *normal* Claude
connector and enforcing **per-calling-user** Sugar permissions. No code here — the plan, the
decision matrix, where it slots into the existing seams, and a testable reference rig.

## Problem

Two things the spike's static bearer cannot do:

1. **It isn't addable the way a regular user adds monday.com / Salesforce.** The claude.ai
   connector UI is built around OAuth — the user enters a URL and logs in; there is no field
   to paste a bearer token. A static-bearer server only works for header-capable clients
   (Claude Code, the API, curl).
2. **Every caller shares one Sugar user's permissions.** Today SugarMCP logs in as one real
   user ([sugar/session.py](sugar/session.py)); the bearer authenticates *the connector*, not
   the human. Respecting each user's roles/teams/field-security needs per-caller identity.

Storing per-user Sugar passwords (in `.env` or a login page) is not viable for companies, and
a shared service user doesn't give per-user permissions. The answer is an **identity
provider**.

## The two hops (the core mental model)

| Hop | Direction | Auth | Public? |
|---|---|---|---|
| Claude → SugarMCP | inbound | **OIDC** — user logs into an IdP | yes |
| SugarMCP → Sugar | outbound | admin login + `POST /oauth2/sudo/<user>` | no (just reachable from SugarMCP) |

Claude never talks to Sugar. The IdP authenticates the user *to SugarMCP*; SugarMCP then acts
in Sugar as that user via `sudo`. **Sugar is never configured to use the IdP** — the only link
between the two worlds is a matching identifier (email/username) on the Sugar user record.

## Why Sugar forces this shape

Sugar's `SugarOAuth2Storage` implements only the **password grant** (plus refresh and an
extension grant) — **not `authorization_code`**. So there is no Sugar-hosted login/consent
endpoint a third party can redirect a user to, and the only way to get a user's token is to
hold their password. That single missing grant is why SugarMCP can't federate *to Sugar* the
way monday/Salesforce federate to their own OAuth servers. Instead we federate SugarMCP to an
external IdP for identity, and use `sudo` for Sugar access.

`POST /oauth2/sudo/:user_name` is real and already exercised in this repo (non-admin ACL
fixtures were captured through it) — critically, on a **dedicated platform** it does not evict
the target's web session or the caller's token.

## Target architecture

SugarMCP becomes an **OAuth resource server** that trusts an external OIDC authorization
server (the customer's IdP). It does **not** implement its own authorization server.

```
User's browser ──login──▶ IdP (Keycloak / Entra / Okta / SugarIdentity)
Claude ──OIDC authorization_code + PKCE──▶ IdP ──▶ per-user token
Claude ──Bearer <IdP token>──▶ SugarMCP
                                   │  validate token against IdP JWKS (iss/aud/exp/sig)
                                   │  read identity claim (email / preferred_username)
                                   │  map → Sugar username
                                   └── admin session: POST /oauth2/sudo/<username>
                                                        └──▶ Sugar enforces that user's ACLs
```

SugarMCP advertises OAuth **protected-resource metadata** so Claude discovers which IdP to use;
Claude runs the whole OAuth dance against the IdP; SugarMCP only ever *validates* the resulting
token. This is less code than hosting an AS and keeps SugarMCP out of the password business.

## Decision matrix (the consulting playbook)

| Customer situation | SugarMCP identity source | Sugar access | Notes |
|---|---|---|---|
| Has a corporate IdP (Entra/Okta/Google/…), Sugar on local or **SAML** login | Federate SugarMCP to **their IdP** via OIDC | admin + `sudo` | "Same IdP" works even if Sugar uses SAML — use the IdP's OIDC endpoints for SugarMCP |
| On **SugarIdentity (OIDC)** | Federate to **SugarIdentity** | **real per-user Sugar token — no sudo** | True monday/Salesforce parity; on-demand only |
| **No IdP** | Ship **Keycloak** as fallback, or single service user (spike mode) | admin + `sudo` | Keycloak is also the test rig below |

**Default:** align SugarMCP to the customer's existing IdP whenever they have one. You inherit
their MFA, conditional access, and — the real selling point — **deprovisioning**: disable a
user in the IdP and both their Sugar login and their Claude→SugarMCP access die together.

Sharing the IdP mainly makes the **identity mapping trustworthy** (same directory → the claim
SugarMCP receives is the one Sugar stores) and unifies governance. It does **not** by itself
remove `sudo` on stock Sugar — only SugarIdentity does that.

## Identity → Sugar user mapping

- Map a stable IdP claim (prefer `email`, fall back to `preferred_username`) to a Sugar user.
  Configurable which claim, since providers differ.
- Reliable only when the Sugar user directory shares the IdP's identifiers — guaranteed when
  Sugar users are provisioned from the same IdP, which is the argument for a shared IdP.
- A caller whose claim matches no Sugar user gets a clean "no Sugar account for <identity>"
  error, never a fallback to another user.
- **A header/claim is identity only after signature validation** — never trust an unverified
  header (the MCP SDK's `Context.headers` doc warns of exactly this).

## Where it slots into the code

The codebase already anticipates this. [sugar/context.py](sugar/context.py) says a future HTTP
transport "replaces `get_context` with a session-keyed lookup and adds an auth shim; nothing in
`tools/` or `sugar/` changes." That is precisely this work.

- **[transport.py](transport.py) / [server.py](server.py) — become a resource server.**
  Use the MCP SDK's resource-server path: `MCPServer(auth=AuthSettings(issuer_url=<IdP>,
  resource_server_url=<SugarMCP public URL>), token_verifier=<our verifier>)`. The SDK then
  wires `BearerAuthBackend` + `RequireAuthMiddleware`, serves protected-resource metadata, and
  exposes the validated principal via `get_access_token()`. We implement a `TokenVerifier`
  whose `verify_token()` validates the IdP JWT against its JWKS (cached from the issuer's
  `.well-known`) and returns an `AccessToken` carrying `sub`/claims. The existing
  `StaticBearerMiddleware` stays as an option for ops/machine clients; OIDC supersedes it for
  users.

- **[sugar/session.py](sugar/session.py) — admin login + per-user `sudo` cache.**
  Add an admin `SugarSession` (password grant, as today) and a method to mint/cache a sudo
  token per user: `POST /oauth2/sudo/<username>` using the admin token, on a **dedicated
  platform** so nothing is evicted. Reuse the existing `Tokens`/`TokenStore` machinery and the
  refresh/`invalid_grant`/`max_session_lifetime` handling already documented there. Cache keyed
  by mapped identity; respect sudo-token expiry.

- **[sugar/context.py](sugar/context.py) — session-keyed provider.**
  `get_context` becomes a lookup from validated identity → a `SugarContext` whose `SugarSession`
  carries that user's sudo token. Read the identity inside the tool layer from the SDK's
  request-scoped auth (`get_access_token()`), pass it to the provider. `sugar/` still imports
  nothing from `mcp`; the auth read lives in `tools/`.

- **Config — new env.** OIDC issuer URL, expected audience, identity claim name, admin
  username/password + `sudo` platform, and the SugarMCP public resource URL. The service
  user's password in `.env` becomes the **admin** credential used for `sudo` — one credential
  you control, not per-user secrets.

## Security considerations

- **Trust concentration.** The admin credential can `sudo` as *anyone*, so the token-validation
  + identity-mapping becomes the entire security boundary. SugarMCP becomes a high-value
  target: protect the admin creds (secrets manager, not a world-readable file), audit-log every
  `sudo` (who, as-whom, when, which tool), and lock down the box.
- **Token validation must be strict.** Verify issuer, audience, expiry, and signature against
  the IdP's JWKS; reject tokens whose audience isn't this resource. Don't accept opaque tokens
  without introspection.
- **No password handling** in the OIDC path — that's the whole point. (The admin password is
  SugarMCP's own service credential, not a user's.)
- **Reads only, for now.** Writes stay off under HTTP by default; revisit the approval model
  before letting autonomous agents write as a user.

## Reference test rig (no SugarIdentity / no on-demand needed)

A self-contained stack you can stand up on the SugarMCP VPS, with your local Sugar tunnelled in.

**Topology for testing**
```
Claude ──▶ mcp.<domain>   (SugarMCP, VPS, behind Caddy)
browser ─▶ id.<domain>    (Keycloak,  VPS, behind Caddy)   ← must be public: browser + Claude hit it
SugarMCP ─▶ https://<ngrok>/sugar  (your local dockerized Sugar, tunnelled)
```

**Caddy (two subdomains, same proxy):**
```
mcp.example.com { reverse_proxy 127.0.0.1:8000 }
id.example.com  { reverse_proxy 127.0.0.1:8080 }
```

**Keycloak (container on the VPS):**
- Realm `sugar`; issuer `https://id.example.com/realms/sugar`, discovery at
  `/.well-known/openid-configuration`.
- A client for the connector: `authorization_code` + **PKCE**. Enable **dynamic client
  registration** (so Claude can self-register) *or* register the client manually with Claude's
  redirect URI — **capture the real redirect URI / whether Claude does DCR from the connector
  setup; verify, don't assume** (see open questions).
- A test user whose `email` matches a real Sugar user's email.

**Local Sugar via tunnel:**
- Point `SUGAR_URL` at the tunnel: `https://<ngrok-sub>.ngrok-free.app/sugar` (instance root +
  `/sugar`, not `/rest`). `SUGAR_VERIFY_SSL=1` (the tunnel gives valid TLS).
- **ngrok gotcha:** ngrok's free interstitial can intercept non-browser requests. SugarMCP's
  REST calls send a JSON `Accept`, which usually bypasses it, but if Sugar calls start coming
  back as HTML warning pages, send header `ngrok-skip-browser-warning: true` on the httpx
  client, or use `cloudflared` (no interstitial) instead.

## Phasing

1. **Resource-server validation against one IdP (Keycloak).** Implement the `TokenVerifier` +
   `AuthSettings`, prove Claude connects via OIDC and a tool call runs as a mapped user through
   `sudo`. This is the milestone that makes it a "normal connector."
2. **Provider setup docs** for the shortlist — Entra, Okta, Google, Auth0 (+ Keycloak). One
   short console click-path each; SugarMCP config is identical (issuer + claim). This is the
   "few top ones, same setup" answer — one integration, N configs.
3. **SugarIdentity no-`sudo` variant** for on-demand customers (true per-user Sugar token).
4. **Writes + autonomous agents** — later, with its own approval review.

## Assumptions to verify on the instance (don't reason from docs)

- Claude's connector OAuth: does it do **dynamic client registration**, or need a manual client
  + known redirect URI? What resource/audience does it expect? (Drives Keycloak client setup.)
- `POST /oauth2/sudo/<user>` from the admin on a dedicated platform: confirm no eviction and
  that the resulting token carries the target's ACLs (we have fixture evidence; re-confirm for
  the token-driven path).
- Does the chosen admin user actually have `sudo` rights on this instance? ENT-only?
- Which IdP claim is the right join key against this customer's Sugar user records.
- JWKS caching / key rotation behaviour from the IdP's discovery doc.
