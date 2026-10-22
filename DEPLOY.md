# Deploying ScrapeClaude to a VPS

## VPS requirements

Measured while testing: the API server uses ~70 MB, headless Chromium ~500 MB plus ~100–200 MB per open
page, and each Claude CLI call ~300 MB while it runs. With the defaults (2 Claude calls and 3 browser pages
at once) peak memory is about 2 GB.

| | Minimum | Recommended |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB (for bigger crawls, more concurrency, JS-heavy sites) |
| Disk | 30 GB SSD | 60–80 GB SSD (Docker images ~3 GB, history database grows over time) |
| OS | Ubuntu 24.04 LTS (or Debian 12) | same |
| Network | 1 public IPv4, ~1 TB/month traffic | same |
| Extras | none | a domain name for HTTPS, or Tailscale for private access |

Notes:

- 2 GB RAM VPSes will run out of memory under load. If you must, set `LLM_MAX_CONCURRENCY=1`,
  `BROWSER_MAX_PAGES=1` and add 2 GB of swap.
- Typical matching plans: Hetzner CX32 / CPX31 (4 vCPU, 8 GB), DigitalOcean / Vultr 4–8 GB droplets,
  Contabo VPS S/M. A European region (e.g. Germany or Finland) gives good latency to the Middle East.
- Claude does the heavy thinking remotely, so no GPU is needed.
- Some sites block datacenter IPs. If that matters, put a residential proxy in `SCRAPE_PROXY_URL`.

## Security model (read this once)

- Every API call needs the `SC_API_KEY` header (scripts) or a dashboard session (HttpOnly cookie). The dashboard
  signs in with `DASHBOARD_EMAIL` + a password whose PBKDF2 hash is in `DASHBOARD_PASSWORD_HASH`. The server
  refuses to start with an empty or short API key.
- Chromium runs in its own container (`browser`) with no secrets in it, with its sandbox enabled through the
  seccomp profile in `deploy/seccomp_profile.json`. The `app` container holds the Claude token but never
  renders untrusted pages.
- Requests to private networks are blocked in both the HTTP fetcher and the browser (including redirects and
  in-page requests). The Tailscale range (100.64.0.0/10) is blocked too.
- Response bodies are capped (`MAX_FETCH_BYTES`), request bodies are capped by Caddy (20 MB), failed logins
  are rate limited per IP and logged, and history is purged after `HISTORY_RETENTION_DAYS`.
- Recommended for a personal instance: do not expose it to the internet at all. Use the Tailscale or SSH
  tunnel variants below.

## 1. Prepare the server

```bash
ssh root@YOUR_VPS_IP
apt update && apt upgrade -y
apt install -y ca-certificates curl git ufw
curl -fsSL https://get.docker.com | sh

ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
```

**Docker and ufw:** Docker publishes ports with its own iptables rules that run *before* ufw. A port published
as `"8000:8000"` is reachable from the internet even if ufw "denies" it. Every port mapping in
`docker-compose.yml` is therefore bound to `127.0.0.1` (or to a Tailscale IP) unless it is meant to be public.

## 2. Get the code onto the server

```bash
git clone <your repo> /opt/src && cp -r /opt/src/projects/scrapeclaude /opt/scrapeclaude
cd /opt/scrapeclaude
```

## 3. Get your Claude subscription token (subscription mode)

On **your own computer** (it opens a browser to log in):

```bash
npm install -g @anthropic-ai/claude-code
claude setup-token
```

It prints a long-lived token. Copy it; it is not saved anywhere.

(API key mode instead: create a key at console.anthropic.com and use `LLM_PROVIDER=anthropic_api`.)

## 4. Configure

```bash
cp .env.example .env
nano .env
```

Set at least:

```
SC_API_KEY=<output of: openssl rand -hex 32>
DASHBOARD_EMAIL=you@example.com
DASHBOARD_PASSWORD_HASH=<output of: docker run --rm scrapeclaude-app python -m app.hashpw 'your password'>
LLM_PROVIDER=claude_cli
CLAUDE_CODE_OAUTH_TOKEN=<token from step 3>
ANTHROPIC_API_KEY=
DOMAIN=scrape.yourdomain.com
```

Then:

```bash
chmod 600 .env
mkdir -p data && chown 1000:1000 data && chmod 700 data
```

Point the domain's DNS A record at the VPS IP before starting (Caddy needs it to get the HTTPS certificate).

## 5. Start

```bash
docker compose up -d --build
docker compose logs -f app        # look for "LLM: {'provider': 'claude_cli', ...}"
docker compose logs browser       # should show "Listening on ws://..."
```

Check the Claude login inside the container:

```bash
docker compose exec app claude auth status   # authMethod should be "oauth_token"
```

Open `https://scrape.yourdomain.com` and sign in with `DASHBOARD_EMAIL` and your password. The API docs at
`/docs` need the same login. To change the password later: generate a new hash with
`sudo docker compose exec app python -m app.hashpw`, put it in `.env`, and `sudo docker compose up -d app`.

### Private access without a domain (most secure)

Delete the `caddy` service from `docker-compose.yml` and either:

- SSH tunnel from your computer: `ssh -L 8000:127.0.0.1:8000 root@YOUR_VPS_IP`, then open http://localhost:8000
- or install Tailscale on the VPS and your devices, then bind the app to the VPS's Tailscale IP only:

  ```yaml
  ports:
    - "100.x.y.z:8000:8000"     # the VPS's own Tailscale IP (tailscale ip -4)
  ```

  and open `http://100.x.y.z:8000`. Do **not** use `"8000:8000"`: that binds to all interfaces and bypasses
  ufw (see step 1). Alternatively keep `127.0.0.1:8000:8000` and run `tailscale serve 8000`, which also adds
  HTTPS.

The dashboard login cookie is only marked `Secure` when served over HTTPS; over a plain-HTTP Tailscale or
SSH-tunnel connection the traffic is still encrypted by Tailscale/SSH.

## Updating

```bash
cd /opt/scrapeclaude && git -C /opt/src pull && cp -r /opt/src/projects/scrapeclaude/. .
docker compose up -d --build
```

## Backups

All history is in `data/scrapeclaude.db` (mode 600). It contains scraped HTML and results, so treat backups
as sensitive: copy it with cron to an encrypted location, and set `HISTORY_RETENTION_DAYS` so it does not
grow forever.

## Without Docker (systemd)

```bash
apt install -y python3-venv nodejs npm
useradd -m scraper && chown -R scraper /opt/scrapeclaude
sudo -u scraper bash -c 'cd /opt/scrapeclaude && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt \
  && PLAYWRIGHT_BROWSERS_PATH=/opt/scrapeclaude/.browsers .venv/bin/playwright install chromium'
/opt/scrapeclaude/.venv/bin/playwright install-deps chromium
npm install -g @anthropic-ai/claude-code@2.1.303
chmod 600 .env && mkdir -p data && chown scraper data && chmod 700 data
cp deploy/scrapeclaude.service /etc/systemd/system/ && systemctl enable --now scrapeclaude
```

Here Chromium runs on the host as the `scraper` user with its normal sandbox (leave `CHROMIUM_NO_SANDBOX`
false). Then put Caddy or nginx in front of `127.0.0.1:8000` for HTTPS, reusing the `header` and
`request_body` blocks from `deploy/Caddyfile`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| App exits at start with `SC_API_KEY is empty` / `at least 32 characters` | Set a proper key (`openssl rand -hex 32`). `ALLOW_NO_AUTH=true` is for local experiments only. |
| `Claude CLI error: ... login` / 401 | Token missing or expired: run `claude setup-token` again and update `.env`. |
| Requests are billed to the API instead of your plan | `ANTHROPIC_API_KEY` must be empty in subscription mode (the app also strips it from the CLI's environment). |
| "usage limit" errors | You hit your plan's limit; wait for the reset, use a cheaper `LLM_MODEL`, or switch to an API key. |
| Container killed / out of memory | Lower `LLM_MAX_CONCURRENCY` and `BROWSER_MAX_PAGES`, or use a bigger VPS. |
| `browser` container logs show sandbox / `clone` / namespace errors | The host blocks unprivileged user namespaces (common on Ubuntu 24.04 with AppArmor). Run `sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` and persist it in `/etc/sysctl.d/`. The browser container holds no secrets, so this is still far safer than running Chromium next to the app. |
| `Browser failed to load ...: connect ECONNREFUSED` | The `browser` service is not up; `docker compose logs browser`. |
| Empty results on JS sites | Send `"render_heavy_js": true` and, for infinite scroll, `"number_of_scrolls": 3`. |
| "Blocked private/internal address" or "Page reached a blocked address" | Expected for localhost / LAN / Tailscale URLs, including pages that redirect or fetch from them. Set `ALLOW_PRIVATE_URLS=true` only if you really need it. |
| "Response ... exceeds the ... byte limit" | Raise `MAX_FETCH_BYTES` if the page really is that large. |
| 429 "Too many failed authentication attempts" | Wait `AUTH_FAIL_WINDOW_S` seconds, or restart the app to clear the counters. |
