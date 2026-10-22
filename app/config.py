"""All settings come from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so we don't need python-dotenv. Real env vars win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(Path(os.environ.get("SC_ENV_FILE", Path(__file__).resolve().parent.parent / ".env")))


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _keys(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(k.strip() for k in raw.split(",") if k.strip())


MIN_API_KEY_LENGTH = 32


@dataclass(frozen=True)
class Settings:
    # --- Access ---
    # Keys that clients must send (header X-API-Key, SGAI-APIKEY, or Bearer). Comma-separated for several.
    api_keys: tuple[str, ...] = field(default_factory=lambda: _keys("SC_API_KEY"))
    # Only for local experiments: start even when SC_API_KEY is empty.
    allow_no_auth: bool = field(default_factory=lambda: _bool("ALLOW_NO_AUTH", False))
    # Failed authentication attempts per client IP before it is locked out for the window.
    auth_fail_limit: int = field(default_factory=lambda: _int("AUTH_FAIL_LIMIT", 20))
    auth_fail_window_s: int = field(default_factory=lambda: _int("AUTH_FAIL_WINDOW_S", 600))
    # Dashboard login session lifetime.
    session_ttl_s: int = field(default_factory=lambda: _int("SESSION_TTL_S", 12 * 3600))
    # Dashboard sign-in (single user). Hash made with: python -m app.hashpw
    dashboard_email: str = field(default_factory=lambda: os.environ.get("DASHBOARD_EMAIL", "").strip().lower())
    dashboard_password_hash: str = field(default_factory=lambda: os.environ.get("DASHBOARD_PASSWORD_HASH", "").strip())

    # --- LLM ---
    # "claude_cli"   -> uses the official `claude` CLI, logged in with your Claude subscription
    # "anthropic_api"-> uses an Anthropic API key (ANTHROPIC_API_KEY)
    # "auto"         -> API key if ANTHROPIC_API_KEY is set, otherwise the CLI
    llm_provider: str = field(default_factory=lambda: os.environ.get("LLM_PROVIDER", "auto"))
    llm_model: str = field(default_factory=lambda: os.environ.get("LLM_MODEL", "claude-opus-5-5"))
    # Effort for the API provider and the CLI (low|medium|high|xhigh|max).
    llm_effort: str = field(default_factory=lambda: os.environ.get("LLM_EFFORT", "low"))
    llm_max_concurrency: int = field(default_factory=lambda: _int("LLM_MAX_CONCURRENCY", 2))
    llm_timeout_s: int = field(default_factory=lambda: _int("LLM_TIMEOUT_S", 300))
    claude_cli_path: str = field(default_factory=lambda: os.environ.get("CLAUDE_CLI_PATH", "claude"))
    # Max characters of page content sent to the model in one call (~4 chars per token).
    max_content_chars: int = field(default_factory=lambda: _int("MAX_CONTENT_CHARS", 300_000))

    # --- Fetching ---
    browser_enabled: bool = field(default_factory=lambda: _bool("BROWSER_ENABLED", True))
    # Connect to a Playwright browser server (e.g. ws://browser:3000/) instead of launching Chromium locally.
    browser_ws_url: str = field(default_factory=lambda: os.environ.get("BROWSER_WS_URL", ""))
    chromium_path: str = field(default_factory=lambda: os.environ.get("CHROMIUM_PATH", ""))
    # Only when the environment cannot provide a Chromium sandbox. Keep false wherever possible.
    chromium_no_sandbox: bool = field(default_factory=lambda: _bool("CHROMIUM_NO_SANDBOX", False))
    browser_max_pages: int = field(default_factory=lambda: _int("BROWSER_MAX_PAGES", 3))
    fetch_timeout_s: int = field(default_factory=lambda: _int("FETCH_TIMEOUT_S", 30))
    # Largest response body (and largest decompressed sitemap) we accept from a target site.
    max_fetch_bytes: int = field(default_factory=lambda: _int("MAX_FETCH_BYTES", 10_000_000))
    user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "USER_AGENT",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36",
        )
    )
    proxy_url: str = field(default_factory=lambda: os.environ.get("SCRAPE_PROXY_URL", ""))
    # Block localhost / private network targets (protects your VPS internals).
    allow_private_urls: bool = field(default_factory=lambda: _bool("ALLOW_PRIVATE_URLS", False))

    # --- Jobs ---
    crawl_timeout_s: int = field(default_factory=lambda: _int("CRAWL_TIMEOUT_S", 1800))

    # --- Search (for SearchScraper) ---
    # duckduckgo (free, no key) | searxng (SEARXNG_URL) | brave (BRAVE_API_KEY)
    search_provider: str = field(default_factory=lambda: os.environ.get("SEARCH_PROVIDER", "duckduckgo"))
    searxng_url: str = field(default_factory=lambda: os.environ.get("SEARXNG_URL", ""))
    brave_api_key: str = field(default_factory=lambda: os.environ.get("BRAVE_API_KEY", ""))

    # --- Storage ---
    db_path: str = field(
        default_factory=lambda: os.environ.get(
            "DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "scrapeclaude.db")
        )
    )
    # Delete history rows older than this many days (0 = keep forever).
    history_retention_days: int = field(default_factory=lambda: _int("HISTORY_RETENTION_DAYS", 0))


settings = Settings()
