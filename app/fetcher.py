"""Download pages: plain HTTP first, headless Chromium when the page needs JavaScript."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .config import settings

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    html: str
    rendered: bool
    content_type: str = "text/html"


# ---------- URL safety ----------

async def assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError(f"Only http(s) URLs are supported: {url}")
    if settings.allow_private_urls:
        return
    host = parsed.hostname
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"Could not resolve host {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise FetchError(f"Blocked private/internal address for {host} ({ip})")


# ---------- Plain HTTP ----------

_http: httpx.AsyncClient | None = None


def http_client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(
            follow_redirects=True,
            timeout=settings.fetch_timeout_s,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            proxy=settings.proxy_url or None,
            # Checked on every hop, so a redirect can't lead to an internal address.
            event_hooks={"request": [_check_request]},
        )
    return _http


async def _check_request(request: httpx.Request) -> None:
    await assert_public_url(str(request.url))


async def fetch_http(url: str, headers: dict | None = None) -> FetchResult:
    try:
        r = await http_client().get(url, headers=headers or None)
    except FetchError:
        raise
    except httpx.HTTPError as e:
        raise FetchError(f"Request to {url} failed: {e}") from e
    return FetchResult(
        url=url, final_url=str(r.url), status=r.status_code, html=r.text, rendered=False,
        content_type=r.headers.get("content-type", ""),
    )


# ---------- Headless browser ----------

class Browser:
    """One shared Chromium process; each request gets its own isolated context."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(settings.browser_max_pages)

    async def _ensure(self):
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                from playwright.async_api import async_playwright

                self._pw = self._pw or await async_playwright().start()
                launch = {"headless": True, "args": ["--disable-dev-shm-usage", "--no-sandbox"]}
                if settings.chromium_path:
                    launch["executable_path"] = settings.chromium_path
                if settings.proxy_url:
                    launch["proxy"] = {"server": settings.proxy_url}
                self._browser = await self._pw.chromium.launch(**launch)
        return self._browser

    async def new_context(self, headers: dict | None = None):
        browser = await self._ensure()
        ctx = await browser.new_context(
            user_agent=settings.user_agent,
            extra_http_headers=headers or {},
            viewport={"width": 1366, "height": 900},
        )
        # Skip heavy assets we never read.
        async def _route(route):
            req = route.request
            if req.resource_type in {"image", "media", "font"}:
                await route.abort()
                return
            if req.is_navigation_request():
                try:  # clicks / redirects must not reach internal addresses either
                    await assert_public_url(req.url)
                except FetchError:
                    await route.abort("blockedbyclient")
                    return
            await route.continue_()

        await ctx.route("**/*", _route)
        return ctx

    async def render(self, url: str, wait_ms: int = 1000, scrolls: int = 0, headers: dict | None = None) -> FetchResult:
        await assert_public_url(url)
        async with self._sem:
            ctx = await self.new_context(headers)
            try:
                page = await ctx.new_page()
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=settings.fetch_timeout_s * 1000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(8000, settings.fetch_timeout_s * 1000))
                except Exception:
                    pass  # some pages never go idle; we still have the DOM
                await auto_scroll(page, scrolls)
                if wait_ms:
                    await page.wait_for_timeout(wait_ms)
                html = await page.content()
                return FetchResult(
                    url=url, final_url=page.url, status=resp.status if resp else 0, html=html, rendered=True,
                )
            except Exception as e:
                raise FetchError(f"Browser failed to load {url}: {e}") from e
            finally:
                await ctx.close()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None


async def auto_scroll(page, scrolls: int) -> None:
    for _ in range(max(0, scrolls)):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(800)


browser = Browser()


# ---------- Smart fetch ----------

def looks_js_rendered(html: str) -> bool:
    """Heuristic: very little visible text, or a typical empty SPA root."""
    from .cleaner import visible_text_len

    lower = html.lower()
    if visible_text_len(html) < 250:
        return True
    spa_markers = ('id="root"></div>', 'id="app"></div>', 'id="__next"></div>', "enable javascript to run")
    return any(m in lower for m in spa_markers)


async def fetch(url: str, render_js: str | bool = "auto", wait_ms: int = 1000, scrolls: int = 0,
                headers: dict | None = None) -> FetchResult:
    """render_js: True = always use browser, False = never, "auto" = only if the page needs it."""
    if render_js == "auto":
        mode = "auto"
    else:
        mode = "always" if render_js in (True, "true", "always") else "never"
    use_browser = settings.browser_enabled and (mode == "always" or scrolls > 0)

    if not use_browser:
        result = await fetch_http(url, headers)
        if (
            mode == "auto"
            and settings.browser_enabled
            and "html" in result.content_type
            and (result.status >= 400 or looks_js_rendered(result.html))
        ):
            try:
                return await browser.render(url, wait_ms, scrolls, headers)
            except FetchError as e:
                log.warning("browser fallback failed, using plain HTML: %s", e)
        return result
    return await browser.render(url, wait_ms, scrolls, headers)
