"""Download pages: plain HTTP first, headless Chromium when the page needs JavaScript."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .config import settings

log = logging.getLogger(__name__)

TEXT_CONTENT_MARKERS = ("text/", "html", "xml", "json", "javascript")
SKIPPED_RESOURCE_TYPES = {"image", "media", "font"}
BROWSER_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "PLAYWRIGHT_BROWSERS_PATH", "XDG_RUNTIME_DIR")


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

def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_multicast or ip.is_unspecified or ip.is_reserved or ip.is_loopback or ip.is_link_local:
        return False
    return ip.is_global and not ip.is_private


def url_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def url_netloc(url: str) -> str:
    """host[:port], used for navigation and header scoping."""
    return urlparse(url).netloc.lower()


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
    if not infos:
        raise FetchError(f"Could not resolve host {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not is_public_ip(ip):
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


async def fetch_bytes(url: str, headers: dict | None = None, max_bytes: int | None = None) -> tuple[httpx.Response, bytes]:
    """GET a URL, refusing bodies larger than max_bytes (default MAX_FETCH_BYTES)."""
    limit = max_bytes or settings.max_fetch_bytes
    try:
        async with http_client().stream("GET", url, headers=headers or None) as r:
            declared = r.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise FetchError(f"Response from {url} is {declared} bytes, over the {limit} byte limit")
            chunks: list[bytes] = []
            size = 0
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise FetchError(f"Response from {url} exceeds the {limit} byte limit")
                chunks.append(chunk)
            return r, b"".join(chunks)
    except FetchError:
        raise
    except httpx.HTTPError as e:
        raise FetchError(f"Request to {url} failed: {e}") from e


def _is_text(content_type: str) -> bool:
    ct = content_type.lower()
    return not ct or any(m in ct for m in TEXT_CONTENT_MARKERS)


async def fetch_http(url: str, headers: dict | None = None) -> FetchResult:
    r, body = await fetch_bytes(url, headers)
    content_type = r.headers.get("content-type", "")
    if not _is_text(content_type):
        raise FetchError(f"Unsupported content type {content_type.split(';')[0]} at {url}")
    return FetchResult(
        url=url, final_url=str(r.url), status=r.status_code,
        html=body.decode(r.encoding or "utf-8", errors="replace"), rendered=False, content_type=content_type,
    )


# ---------- Headless browser ----------

class ContextGuard:
    """Applies the URL policy to every request a browser context makes.

    Requests are checked (and aborted) before they leave; responses are checked after they arrive so a
    redirect hop that Playwright cannot intercept still fails the whole render instead of leaking.
    """

    def __init__(self, nav_hosts: set[str] | None = None) -> None:
        self.nav_hosts = nav_hosts
        self.violations: list[str] = []
        self.blocked_navigations: list[str] = []
        self._ok_hosts: set[str] = set()
        self._pending: set[asyncio.Task] = set()

    def nav_allowed(self, url: str) -> bool:
        return self.nav_hosts is None or url_netloc(url) in self.nav_hosts

    async def check(self, url: str) -> None:
        netloc = url_netloc(url)
        if netloc in self._ok_hosts:
            return
        await assert_public_url(url)
        self._ok_hosts.add(netloc)

    def watch(self, url: str) -> None:
        task = asyncio.create_task(self._watch(url))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _watch(self, url: str) -> None:
        try:
            await self.check(url)
        except FetchError as e:
            self.violations.append(str(e))

    async def verify(self) -> None:
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)
        if self.violations:
            raise FetchError(f"Page reached a blocked address: {self.violations[0]}")


def _browser_env() -> dict[str, str]:
    return {k: os.environ[k] for k in BROWSER_ENV_KEYS if k in os.environ}


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
                if settings.browser_ws_url:
                    self._browser = await self._pw.chromium.connect(
                        settings.browser_ws_url, timeout=settings.fetch_timeout_s * 1000
                    )
                else:
                    args = ["--disable-dev-shm-usage"]
                    if settings.chromium_no_sandbox:
                        args.append("--no-sandbox")
                    launch = {"headless": True, "args": args, "env": _browser_env()}
                    if settings.chromium_path:
                        launch["executable_path"] = settings.chromium_path
                    if settings.proxy_url:
                        launch["proxy"] = {"server": settings.proxy_url}
                    self._browser = await self._pw.chromium.launch(**launch)
        return self._browser

    async def new_context(self, headers: dict | None = None, headers_host: str = "",
                          nav_hosts: set[str] | None = None):
        """Returns (context, guard). `headers` are only sent to `headers_host` (host[:port]); `nav_hosts` limits navigations."""
        browser = await self._ensure()
        options: dict = {"user_agent": settings.user_agent, "viewport": {"width": 1366, "height": 900}}
        if settings.browser_ws_url and settings.proxy_url:
            options["proxy"] = {"server": settings.proxy_url}
        ctx = await browser.new_context(**options)
        guard = ContextGuard(nav_hosts)
        extra = dict(headers or {})
        header_host = headers_host.lower()

        async def _route(route):
            req = route.request
            if req.resource_type in SKIPPED_RESOURCE_TYPES:
                await route.abort()
                return
            if req.is_navigation_request() and not guard.nav_allowed(req.url):
                guard.blocked_navigations.append(req.url)
                await route.abort("blockedbyclient")
                return
            try:
                await guard.check(req.url)
            except FetchError:
                await route.abort("blockedbyclient")
                return
            if extra and url_netloc(req.url) == header_host:
                await route.continue_(headers={**req.headers, **extra})
            else:
                await route.continue_()

        await ctx.route("**/*", _route)
        # Redirect hops are not routed, so also verify every address that actually answered.
        ctx.on("response", lambda resp: guard.watch(resp.url))
        return ctx, guard

    async def render(self, url: str, wait_ms: int = 1000, scrolls: int = 0, headers: dict | None = None) -> FetchResult:
        await assert_public_url(url)
        async with self._sem:
            ctx, guard = await self.new_context(headers, headers_host=url_netloc(url))
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
                await assert_public_url(page.url)
                await guard.verify()
                html = await page.content()
                return FetchResult(
                    url=url, final_url=page.url, status=resp.status if resp else 0, html=html, rendered=True,
                )
            except FetchError:
                raise
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
