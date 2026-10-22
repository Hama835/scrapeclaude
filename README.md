# ScrapeClaude

A self-hosted, personal AI web scraper modelled on [ScrapeGraphAI](https://scrapegraphai.com), powered by Claude.
Give it a URL (or a question) and a plain-English prompt; get clean JSON back.

It runs on **your Claude Pro/Max subscription** (through the official Claude Code CLI) or on an
**Anthropic API key**. Switch with one setting.

## Features

| Service | Endpoint | What it does | Uses Claude |
|---|---|---|---|
| SmartScraper | `POST /v1/smartscraper` | URL/HTML + prompt (+ optional JSON Schema) → JSON. Follows "Next" links with `total_pages`. | yes |
| SearchScraper | `POST /v1/searchscraper` | Searches the web, reads the top N results, answers with `reference_urls`. | yes |
| SmartCrawler | `POST /v1/crawl` → `GET /v1/crawl/{id}` | Crawls a site (depth, max pages, include/exclude paths, robots.txt, sitemap) and extracts one combined answer. Background job. | yes |
| AgenticScraper | `POST /v1/agentic-scrapper` | Plain-English browser steps ("type X in search", "click Login"), then extraction. | yes |
| Markdownify | `POST /v1/markdownify` | Page → clean Markdown. | no |
| Scrape | `POST /v1/scrape` | Page → raw HTML (optionally rendered with JavaScript). | no |
| Sitemap | `POST /v1/sitemap` | All URLs from robots.txt / sitemap.xml (indexes and .gz supported). | no |
| History | `GET /v1/requests`, `GET /v1/requests/{id}` | Every request and result is saved in SQLite. | – |

Also included:

- A web dashboard at `/` (playground for every service, history, copy-as-cURL).
- Interactive API docs at `/docs`.
- Automatic JavaScript rendering: plain HTTP first, headless Chromium when the page needs it (`render_heavy_js: "auto" | true | false`), infinite-scroll support (`number_of_scrolls`).
- Large pages are split into chunks and the partial results merged, so nothing is silently cut off.
- Protection against requests to localhost or private networks (on by default), including redirects and requests made by a rendered page's JavaScript.
- Dashboard sign-in with your email and password (single user, hashed with PBKDF2) and an HttpOnly session cookie; the API key is for scripts only. API docs are behind the same login.
- Size limits on fetched pages, request bodies and decompressed sitemaps; per-IP lockout after repeated failed logins; configurable history retention.

Not included compared to the hosted ScrapeGraphAI: billing/credits, teams, SDK packages, and
built-in rotating residential proxies (you can set your own with `SCRAPE_PROXY_URL`).

## How the Claude subscription works here

When `LLM_PROVIDER=claude_cli`, every extraction runs the official `claude` command in headless
mode (`claude -p ... --json-schema ...`), logged in with **your** subscription token. Anthropic's
terms allow an individual to use the unmodified Claude Code CLI with their own subscription; they do
**not** allow offering subscription-backed access to other people. So:

- Personal use only. Don't share the API key or open the app to others.
- Calls count against your plan's normal usage limits (shared with your Claude app and Claude Code usage).
  Heavy crawling can use those limits up quickly.
- If you ever need to share it or run it at volume, switch to `LLM_PROVIDER=anthropic_api` with an API key.

## Run locally (testing)

```bash
cd projects/scrapeclaude
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
playwright install --with-deps chromium
npm install -g @anthropic-ai/claude-code   # for subscription mode
claude                                      # log in once (or set CLAUDE_CODE_OAUTH_TOKEN)

cp .env.example .env    # set SC_API_KEY (32+ chars, e.g. openssl rand -hex 32); ALLOW_PRIVATE_URLS=true is handy for local testing
uvicorn app.main:app --reload --port 8000
# set DASHBOARD_EMAIL and DASHBOARD_PASSWORD_HASH (python -m app.hashpw) for the dashboard sign-in,
# or leave them empty locally and sign in with SC_API_KEY at http://localhost:8000
```

Tests (use a local test site and a stand-in model, so they make no Claude calls):

```bash
pytest
```

## API examples

```bash
curl -X POST http://localhost:8000/v1/smartscraper \
  -H "Content-Type: application/json" -H "X-API-Key: $SC_API_KEY" \
  -d '{
    "website_url": "https://news.ycombinator.com",
    "user_prompt": "Top 5 stories with title, link and points",
    "output_schema": {
      "type": "object",
      "properties": {"stories": {"type": "array", "items": {"type": "object",
        "properties": {"title": {"type": "string"}, "url": {"type": "string"}, "points": {"type": "integer"}},
        "required": ["title", "url", "points"]}}},
      "required": ["stories"]
    }
  }'
```

Response:

```json
{
  "request_id": "…",
  "status": "completed",
  "result": {"stories": [{"title": "…", "url": "…", "points": 312}]},
  "pages": ["https://news.ycombinator.com/"],
  "rendered_js": false,
  "usage": {"llm_calls": 1, "provider": "claude_cli", "model": "claude-opus-5-5", "input_tokens": 5120, "output_tokens": 410},
  "duration_s": 7.4
}
```

Crawl (background job):

```bash
curl -X POST http://localhost:8000/v1/crawl -H "Content-Type: application/json" -H "X-API-Key: $SC_API_KEY" \
  -d '{"url": "https://example.com", "prompt": "List all services with prices", "depth": 2, "max_pages": 20}'
# → {"request_id": "abc…", "status": "processing"}
curl http://localhost:8000/v1/crawl/abc… -H "X-API-Key: $SC_API_KEY"
```

Agentic:

```bash
curl -X POST http://localhost:8000/v1/agentic-scrapper -H "Content-Type: application/json" -H "X-API-Key: $SC_API_KEY" \
  -d '{"url": "https://example.com", "steps": ["Type laptop in the search box and press Enter", "Click the first product"],
       "user_prompt": "Product name, price and specs"}'
```

The auth header can be `X-API-Key`, `SGAI-APIKEY` (ScrapeGraphAI style) or `Authorization: Bearer …`. `SC_API_KEY` may hold several comma-separated keys. The dashboard signs in with email + password (`POST /auth/login`) and then uses a session cookie; the API key is never typed into the browser when a password is configured.

## Project layout

```
app/
  main.py            API routes, request history, security headers
  auth.py            API keys, dashboard sessions, failed-login lockout
  config.py          settings (env vars / .env)
  fetcher.py         HTTP + headless Chromium, URL safety checks
  cleaner.py         HTML → Markdown, links, pagination detection
  db.py              SQLite history
  llm/               claude_cli.py (subscription), anthropic_api.py (API key), fake.py (tests)
  services/          scrapers.py, crawler.py, agentic.py, sitemap.py, search.py
  static/            dashboard (index.html, app.js, style.css)
tests/               local test site + API tests
deploy/              Caddyfile, systemd unit
```

Deployment and VPS sizing: see [DEPLOY.md](DEPLOY.md).
