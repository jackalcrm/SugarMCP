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

## Provisioning a fresh Ubuntu VM (cloud-init, portable)

Prefer a **bare Ubuntu 24.04 LTS** image with the script below pasted into the provider's
"user data" field, over a provider-specific "Docker" marketplace image. The marketplace image
only saves the Docker install, and it exists only on that one provider — this script runs
unchanged on any Ubuntu VM (DigitalOcean, a client's AWS box, bare metal), which is the
portability you want for a repeatable client deployment. It also recreates the non-root
sudo/docker user a marketplace image would otherwise set up.

> **Secrets stay out of user-data.** Provider user-data is readable from the instance
> metadata service and stored by the provider. This script *generates* the bearer token on
> the box; you fill the Sugar credentials by hand after boot. Never paste `SUGAR_PASSWORD`,
> `SUGAR_CLIENT_SECRET`, or a real `MCP_AUTH_TOKEN` into user-data.

Edit the three values at the top, then paste as user data:

```bash
#!/bin/bash
# ------------------------- EDIT THESE -------------------------
DOMAIN="mcp.example.com"                                   # your subdomain (DNS set separately)
APP_USER="deploy"                                          # non-root user to own + run the app
REPO_URL="https://github.com/jackalcrm/SugarMCP.git"       # or leave and scp the code if private
# --------------------------------------------------------------
set -ux   # trace to /var/log/cloud-init-output.log; no -e so a transient mirror blip can't abort
export DEBIAN_FRONTEND=noninteractive

# 1. Patch + automatic security updates
apt-get update -y && apt-get upgrade -y
apt-get install -y ufw curl git ca-certificates openssl unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

# 2. Docker CE + Compose v2 from Docker's official repo (NOT the older apt docker.io)
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# 3. Non-root sudo + docker user, with the root SSH key copied so you can log in as them
id -u "$APP_USER" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$APP_USER"
usermod -aG sudo,docker "$APP_USER"
echo "$APP_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$APP_USER" && chmod 440 "/etc/sudoers.d/90-$APP_USER"
if [ -f /root/.ssh/authorized_keys ]; then
  install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/$APP_USER/.ssh"
  install -m 600 -o "$APP_USER" -g "$APP_USER" /root/.ssh/authorized_keys "/home/$APP_USER/.ssh/authorized_keys"
fi
# Optional hardening — uncomment ONLY after confirming you can SSH in as $APP_USER, or you lock yourself out:
# sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/; s/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && systemctl restart ssh

# 4. Host firewall. NOTE: Docker-published ports bypass UFW, so this alone will NOT block
#    8000 if it is published on 0.0.0.0 — step 6 binds it to localhost, and a provider
#    cloud firewall (edge) is the real control. UFW here protects non-Docker ports.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# 5. Caddy for automatic HTTPS (systemd-managed, auto-renewing certs)
install -m 0755 -d /usr/share/keyrings
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y && apt-get install -y caddy
if [ -n "$DOMAIN" ] && [ "$DOMAIN" != "mcp.example.com" ]; then
  printf '%s {\n    reverse_proxy 127.0.0.1:8000\n}\n' "$DOMAIN" > /etc/caddy/Caddyfile
  systemctl reload caddy || systemctl restart caddy   # retries until DNS points here
fi

# 6. App source, owned by $APP_USER
install -d -o "$APP_USER" -g "$APP_USER" /opt/sugarmcp
sudo -u "$APP_USER" git clone "$REPO_URL" /opt/sugarmcp || echo "clone failed (private repo?) — scp the code to /opt/sugarmcp"
# Bind the published port to localhost so only same-host Caddy reaches it (defeats the UFW bypass)
[ -f /opt/sugarmcp/docker-compose.yml ] && sed -i 's|"8000:8000"|"127.0.0.1:8000:8000"|' /opt/sugarmcp/docker-compose.yml

# 7. Pre-fill .env with a strong generated bearer token; Sugar creds are left blank for you
if [ -f /opt/sugarmcp/.env.example ] && [ ! -f /opt/sugarmcp/.env ]; then
  sudo -u "$APP_USER" cp /opt/sugarmcp/.env.example /opt/sugarmcp/.env
  TOKEN="$(openssl rand -hex 32)"
  sudo -u "$APP_USER" sed -i "s|^MCP_AUTH_TOKEN=.*|MCP_AUTH_TOKEN=$TOKEN|" /opt/sugarmcp/.env
fi

# 8. Leave the operator clear next steps
cat > /etc/motd <<EOF

SugarMCP provisioned. Finish setup:
  1. Point DNS A record for $DOMAIN at this droplet's IP.
  2. sudo -iu $APP_USER; cd /opt/sugarmcp
  3. Edit .env: fill SUGAR_URL / SUGAR_USERNAME / SUGAR_PASSWORD / SUGAR_CLIENT_SECRET
     (MCP_AUTH_TOKEN is already set — read it with: grep MCP_AUTH_TOKEN .env)
  4. docker compose up -d --build   &&   curl -s localhost:8000/health
  5. Add https://$DOMAIN/mcp as a Claude custom connector with that bearer token.

EOF
```

**What it does / doesn't do.** It installs and hardens the box and stages the app, but
deliberately does **not** start the container — the server refuses to run without Sugar
credentials, which only you can supply. Watch `/var/log/cloud-init-output.log` if anything
looks missing. Then follow the `motd` steps (they mirror sections 2–5 below).

**Also attach a provider cloud firewall** (DigitalOcean: Networking → Firewalls) allowing only
22/80/443. It sits at the network edge, so unlike UFW it *does* block any Docker-published
port — belt to the localhost-bind suspenders.

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
