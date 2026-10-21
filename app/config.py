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


@dataclass(frozen=True)
class Settings:
    # --- Access ---
    # Key that clients must send (header X-API-Key, SGAI-APIKEY, or Bearer).
    api_key: str = field(default_factory=lambda: os.environ.get("SC_API_KEY", ""))

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
    chromium_path: str = field(default_factory=lambda: os.environ.get("CHROMIUM_PATH", ""))
    browser_max_pages: int = field(default_factory=lambda: _int("BROWSER_MAX_PAGES", 3))
    fetch_timeout_s: int = field(default_factory=lambda: _int("FETCH_TIMEOUT_S", 30))
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


settings = Settings()
