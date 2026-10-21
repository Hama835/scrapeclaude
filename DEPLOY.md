# Deploying ScrapeClaude to a VPS

## VPS requirements

Measured while testing: the API server uses ~70 MB, headless Chromium ~500 MB plus ~100–200 MB per open
page, and each Claude CLI call ~300 MB while it runs. With the defaults (2 Claude calls and 3 browser pages
at once) peak memory is about 2 GB.

| | Minimum | Recommended |
|---|---|---|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB (for bigger crawls, more concurrency, JS-heavy sites) |
| Disk | 30 GB SSD | 60–80 GB SSD (Docker images ~2.5 GB, history database grows over time) |
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

## 1. Prepare the server

```bash
ssh root@YOUR_VPS_IP
apt update && apt upgrade -y
apt install -y ca-certificates curl git ufw
curl -fsSL https://get.docker.com | sh

ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw enable
```

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
SC_API_KEY=<output of: openssl rand -hex 24>
LLM_PROVIDER=claude_cli
CLAUDE_CODE_OAUTH_TOKEN=<token from step 3>
ANTHROPIC_API_KEY=
DOMAIN=scrape.yourdomain.com
```

Then `chmod 600 .env`.

Point the domain's DNS A record at the VPS IP before starting (Caddy needs it to get the HTTPS certificate).

## 5. Start

```bash
docker compose up -d --build
docker compose logs -f app        # look for "LLM: {'provider': 'claude_cli', ...}"
```

Check the Claude login inside the container:

```bash
docker compose exec app claude auth status   # authMethod should be "oauth_token"
```

Open `https://scrape.yourdomain.com`, click **API key**, paste your `SC_API_KEY`.

### Private access without a domain (most secure)

Delete the `caddy` service from `docker-compose.yml` and either:

- SSH tunnel from your computer: `ssh -L 8000:127.0.0.1:8000 root@YOUR_VPS_IP`, then open http://localhost:8000
- or install Tailscale on the VPS and your devices and open `http://<vps-tailscale-name>:8000`
  (change the port mapping to `"8000:8000"` and keep port 8000 closed in ufw).

## Updating

```bash
cd /opt/scrapeclaude && git -C /opt/src pull && cp -r /opt/src/projects/scrapeclaude/. .
docker compose up -d --build
```

## Backups

All history is in `data/scrapeclaude.db`. Copy that file (e.g. nightly with cron) to keep results.

## Without Docker (systemd)

```bash
apt install -y python3-venv nodejs npm
useradd -m scraper && chown -R scraper /opt/scrapeclaude
sudo -u scraper bash -c 'cd /opt/scrapeclaude && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt \
  && PLAYWRIGHT_BROWSERS_PATH=/opt/scrapeclaude/.browsers .venv/bin/playwright install chromium'
/opt/scrapeclaude/.venv/bin/playwright install-deps chromium
npm install -g @anthropic-ai/claude-code
cp deploy/scrapeclaude.service /etc/systemd/system/ && systemctl enable --now scrapeclaude
```

Then put Caddy or nginx in front of `127.0.0.1:8000` for HTTPS.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Claude CLI error: ... login` / 401 | Token missing or expired: run `claude setup-token` again and update `.env`. |
| Requests are billed to the API instead of your plan | `ANTHROPIC_API_KEY` must be empty in subscription mode (the app also strips it from the CLI's environment). |
| "usage limit" errors | You hit your plan's limit; wait for the reset, use a cheaper `LLM_MODEL`, or switch to an API key. |
| Container killed / out of memory | Lower `LLM_MAX_CONCURRENCY` and `BROWSER_MAX_PAGES`, or use a bigger VPS. |
| Empty results on JS sites | Send `"render_heavy_js": true` and, for infinite scroll, `"number_of_scrolls": 3`. |
| "Blocked private/internal address" | Expected for localhost/LAN URLs. Set `ALLOW_PRIVATE_URLS=true` only if you really need it. |
