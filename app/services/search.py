"""Web search backends for SearchScraper."""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from ..config import settings


class SearchError(RuntimeError):
    pass


async def web_search(query: str, limit: int = 6) -> list[dict]:
    provider = settings.search_provider.lower()
    if provider == "brave":
        return await _brave(query, limit)
    if provider == "searxng":
        return await _searxng(query, limit)
    return await _duckduckgo(query, limit)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.fetch_timeout_s, follow_redirects=True,
        headers={"User-Agent": settings.user_agent}, proxy=settings.proxy_url or None,
    )


async def _duckduckgo(query: str, limit: int) -> list[dict]:
    async with _client() as c:
        try:
            r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
        except httpx.HTTPError as e:
            raise SearchError(f"DuckDuckGo search failed: {e}") from e
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        # DDG wraps links as //duckduckgo.com/l/?uddg=<encoded url>
        if "uddg=" in href:
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
        if href.startswith("http") and "duckduckgo.com/y.js" not in href:
            out.append({"url": href, "title": a.get_text(strip=True)})
        if len(out) >= limit:
            break
    return out


async def _searxng(query: str, limit: int) -> list[dict]:
    if not settings.searxng_url:
        raise SearchError("SEARCH_PROVIDER=searxng needs SEARXNG_URL")
    async with _client() as c:
        r = await c.get(settings.searxng_url.rstrip("/") + "/search", params={"q": query, "format": "json"})
        r.raise_for_status()
    return [{"url": x["url"], "title": x.get("title", "")} for x in r.json().get("results", [])[:limit]]


async def _brave(query: str, limit: int) -> list[dict]:
    if not settings.brave_api_key:
        raise SearchError("SEARCH_PROVIDER=brave needs BRAVE_API_KEY")
    async with _client() as c:
        r = await c.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(limit, 20)},
            headers={"X-Subscription-Token": settings.brave_api_key, "Accept": "application/json"},
        )
        r.raise_for_status()
    return [{"url": x["url"], "title": x.get("title", "")} for x in r.json().get("web", {}).get("results", [])[:limit]]
