# Deploying SugarMCP over HTTP

A checklist for standing up one SugarMCP instance reachable by a Claude custom connector.
The server is a plain Docker container that speaks streamable HTTP; you put TLS in front of
it. Nothing here assumes a specific cloud — it runs the same on a DigitalOcean droplet, a
client VM, or ECS.

See [README.md](README.md) for how the transport works; this file is the operational runbook.

## Recommended shape

**A small VM running `docker compose up`, with Caddy in front for automatic TLS on a real
subdomain.** This is the portable path you can replicate inside any client's infrastructure:
plain Docker + a standard reverse proxy, no vendor lock-in. Use **Cloudflare Tunnel** instead
only when you can't open an inbound port or get public DNS (NAT, locked-down client LAN).

## 1. Prerequisites

- A host with Docker + Docker Compose.
- One of:
  - a DNS name you control pointing at the host's public IP (for the Caddy path), **or**
  - a Cloudflare account (for the Tunnel path).
- The Sugar side prepared **for this instance** (per-instance, every time):
  - an API platform named `mcp` registered (Admin → Configure API Platforms), and
  - an OAuth key `mcp` with client type **Sugar User** (`client_type='user'`); secret goes
    in `.env` as `SUGAR_CLIENT_SECRET`.
  - Without these the server falls back to platform `base`, which evicts the user's web
    session on every login — fine for a quick test, not for a shared instance.

## 2. Configure

```bash
git clone git@github.com:jackalcrm/SugarMCP.git && cd SugarMCP
cp .env.example .env
```

Edit `.env`:

- `SUGAR_URL`, `SUGAR_USERNAME`, `SUGAR_PASSWORD` (or keychain), `SUGAR_CLIENT_SECRET` — the
  Sugar user this instance acts as. It inherits that user's roles/teams/field security.
- `MCP_AUTH_TOKEN` — **required.** Generate a strong value:
  ```bash
  openssl rand -hex 32
  ```
  This is the bearer token the connector sends. Keep it out of git and out of the image.
- Leave `SUGAR_READ_ONLY` unset for the spike — under HTTP, writes are off by default.

`.env` is gitignored. Never bake any of these into the image.

## 3. Run

```bash
docker compose up -d --build
curl -s localhost:8000/health        # {"status":"ok","transport":"http","read_only":true}
```

The container binds `0.0.0.0:8000` and runs unprivileged. `docker compose logs -f` shows one
structured line per tool call (`tool=… actor=… duration_ms=… outcome=…`).

## 4. Put TLS in front

Claude connectors require HTTPS. Pick one:

### Option A — Caddy (recommended)

Point a subdomain's DNS at the host, then run Caddy with this `Caddyfile`:

```
mcp.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
# with Docker:
docker run -d --name caddy --network host \
  -v "$PWD/Caddyfile:/etc/caddy/Caddyfile" \
  -v caddy_data:/data \
  caddy:2
```

Caddy obtains and renews the certificate automatically. The connector URL is
`https://mcp.example.com/mcp`.

### Option B — Cloudflare Tunnel

No inbound port or public DNS required:

```bash
cloudflared tunnel --url http://localhost:8000     # quick, ephemeral hostname
# or a named tunnel bound to a route for a stable hostname (see Cloudflare docs)
```

Cloudflare terminates TLS and the connector URL is the tunnel hostname + `/mcp`.

## 5. Register the connector in Claude

Add a custom connector pointing at `https://<your-host>/mcp` — the `/mcp` path, **not** the
root — with the `MCP_AUTH_TOKEN` value as the bearer token. Confirm it connects and lists the
14 read tools.

> **If the connector is rejected for lacking OAuth:** Claude's custom-connector flow may
> expect OAuth rather than a static bearer. That's the signal to build the per-user OAuth
> passthrough seam (see README "Future seams"), not a transport bug.

## 6. Verify end to end

- `curl https://<host>/health` → 200, no auth.
- `curl -X POST https://<host>/mcp` with no bearer → 401; with the right bearer → reaches MCP.
- Connector handshake succeeds and tools are listed.
- `docker compose logs` shows the tool-call lines with the expected outcomes.

## 7. Operations

- **Logs:** `docker compose logs -f` (stderr; stdout is unused under HTTP).
- **Update:** `git pull && docker compose up -d --build`.
- **Rotate the token:** change `MCP_AUTH_TOKEN` in `.env`, `docker compose up -d`, update the
  connector. Old token stops working immediately.
- **Restart policy:** compose sets `restart: unless-stopped`.

## 8. One instance per client (consulting model)

Each client runs their own container with their own `.env`:

- separate Sugar instance, user, and `SUGAR_CLIENT_SECRET`;
- separate `mcp` platform + OAuth key **on that Sugar instance** (per-instance, Admin or the
  optional package);
- separate `MCP_AUTH_TOKEN`.

Nothing is shared between clients; the process holds no cross-instance state.

## 9. Security checklist

- [ ] `MCP_AUTH_TOKEN` set to a strong random value; never anonymous on a reachable address.
- [ ] TLS terminated in front (Caddy/proxy or Cloudflare) — never expose `:8000` directly.
- [ ] `.env` not committed; secrets not in the image (`docker history` to confirm).
- [ ] Writes off (`SUGAR_READ_ONLY` unset → defaults to read-only under HTTP) unless a client
      explicitly needs writes.
- [ ] Every caller currently shares one Sugar user's permissions — acceptable for a single
      trusted operator; revisit before exposing to multiple end users (OAuth passthrough).
