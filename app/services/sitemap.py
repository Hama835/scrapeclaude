"""Discover every URL a site lists in its sitemap(s)."""
from __future__ import annotations

import gzip
import io
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .. import fetcher
from ..config import settings


def gunzip_limited(data: bytes, limit: int) -> bytes:
    """Decompress at most `limit` bytes so a tiny .gz cannot expand into gigabytes."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
            out = f.read(limit + 1)
    except (OSError, EOFError) as e:
        raise fetcher.FetchError(f"Invalid gzip data: {e}") from e
    if len(out) > limit:
        raise fetcher.FetchError(f"Decompressed sitemap exceeds the {limit} byte limit")
    return out


async def _get(url: str) -> str | None:
    try:
        r, body = await fetcher.fetch_bytes(url)
    except fetcher.FetchError:
        return None
    if r.status_code >= 400:
        return None
    if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
        try:
            body = gunzip_limited(body, settings.max_fetch_bytes)
        except fetcher.FetchError:
            return None
    return body.decode("utf-8", errors="replace")


async def sitemap(*, website_url: str, max_urls: int = 5000) -> dict:
    parsed = urlparse(website_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    await fetcher.assert_public_url(root)

    candidates: list[str] = []
    robots = await _get(root + "/robots.txt")
    if robots:
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.append(urljoin(root, line.split(":", 1)[1].strip()))
    candidates += [root + "/sitemap.xml", root + "/sitemap_index.xml", root + "/sitemap-index.xml"]
    if website_url.endswith((".xml", ".xml.gz")):
        candidates.insert(0, website_url)

    urls: dict[str, None] = {}
    seen_maps: set[str] = set()
    queue = list(dict.fromkeys(candidates))
    found_maps: list[str] = []
    while queue and len(urls) < max_urls and len(seen_maps) < 200:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        xml = await _get(sm)
        if not xml or "<" not in xml:
            continue
        soup = BeautifulSoup(xml, "xml")
        if soup.find("sitemapindex"):
            found_maps.append(sm)
            queue += [loc.get_text(strip=True) for loc in soup.select("sitemap > loc")]
        elif soup.find("urlset"):
            found_maps.append(sm)
            for loc in soup.select("url > loc"):
                urls.setdefault(loc.get_text(strip=True), None)
                if len(urls) >= max_urls:
                    break
    return {"urls": list(urls), "count": len(urls), "sitemaps": found_maps}
