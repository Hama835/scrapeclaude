"""SmartCrawler: follow links across a site, then extract one combined answer."""
from __future__ import annotations

import asyncio
import fnmatch
from typing import Callable
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from .. import cleaner, fetcher
from .common import PageContent, Usage, extract, join_pages, load_page
from .sitemap import sitemap as get_sitemap

SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".zip", ".mp4", ".mp3",
            ".css", ".js", ".ico", ".woff", ".woff2", ".xml", ".json", ".gz", ".dmg", ".exe")


def _matches(path: str, patterns: list[str] | None) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in (patterns or []))


async def _robots(root: str) -> RobotFileParser | None:
    try:
        r = await fetcher.http_client().get(root + "/robots.txt")
    except Exception:
        return None
    if r.status_code >= 400:
        return None
    rp = RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp


async def crawl(*, url: str, prompt: str | None = None, schema: dict | None = None,
                extraction_mode: bool = True, depth: int = 2, max_pages: int = 10,
                same_domain_only: bool = True, use_sitemap: bool = False,
                include_paths: list[str] | None = None, exclude_paths: list[str] | None = None,
                respect_robots: bool = True, render_heavy_js="auto",
                on_progress: Callable[[dict], None] | None = None) -> tuple[dict, Usage]:
    usage = Usage()
    max_pages = max(1, min(max_pages, 500))
    start = urlparse(url)
    root = f"{start.scheme}://{start.netloc}"
    robots = await _robots(root) if respect_robots else None

    queue: list[tuple[str, int]] = [(url, 0)]
    if use_sitemap:
        try:
            sm = await get_sitemap(website_url=url, max_urls=max_pages * 5)
            queue += [(u, 1) for u in sm["urls"]]
        except Exception:
            pass

    seen: set[str] = set()
    pages: list[PageContent] = []
    failed: list[dict] = []
    sem = asyncio.Semaphore(3)

    def allowed(u: str) -> bool:
        p = urlparse(u)
        if same_domain_only and p.netloc != start.netloc:
            return False
        if p.path.lower().endswith(SKIP_EXT):
            return False
        if include_paths and not _matches(p.path, include_paths):
            return u == url  # always allow the start page
        if _matches(p.path, exclude_paths):
            return False
        if robots and not robots.can_fetch("*", u):
            return False
        return True

    async def visit(u: str, d: int):
        async with sem:
            try:
                page = await load_page(u, render_heavy_js)
            except fetcher.FetchError as e:
                failed.append({"url": u, "error": str(e)})
                return []
        if page.status >= 400:
            failed.append({"url": u, "error": f"HTTP {page.status}"})
            return []
        pages.append(page)
        if on_progress:
            on_progress({"pages_crawled": len(pages), "last_url": page.url})
        if d >= depth:
            return []
        return [(link, d + 1) for link in cleaner.extract_links(page.html, page.url)]

    while queue and len(pages) < max_pages:
        batch = []
        while queue and len(batch) < max_pages - len(pages):
            u, d = queue.pop(0)
            if u in seen or not allowed(u):
                continue
            seen.add(u)
            batch.append((u, d))
        if not batch:
            break
        for found in await asyncio.gather(*(visit(u, d) for u, d in batch)):
            queue += [x for x in found if x[0] not in seen]

    pages = pages[:max_pages]
    out: dict = {
        "pages": [{"url": p.url, "title": p.title, "markdown": p.markdown} for p in pages],
        "crawled_urls": [p.url for p in pages],
        "failed": failed,
    }
    if extraction_mode and prompt and pages:
        full_prompt = prompt + "\n\nThe content comes from several pages of one website (each inside <page url=...>)."
        out["llm_result"] = await extract(join_pages(pages), full_prompt, schema, usage)
    return out, usage
