"""Discover every URL a site lists in its sitemap(s)."""
from __future__ import annotations

import gzip
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .. import fetcher


async def _get(url: str) -> str | None:
    try:
        r = await fetcher.http_client().get(url)
    except Exception:
        return None
    if r.status_code >= 400:
        return None
    body = r.content
    if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError:
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
