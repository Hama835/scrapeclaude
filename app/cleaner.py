"""Turn raw HTML into compact Markdown that Claude can read cheaply."""
from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup, Comment
from markdownify import MarkdownConverter

DROP_TAGS = ["script", "style", "noscript", "svg", "canvas", "iframe", "template", "link", "meta"]
BOILERPLATE_TAGS = ["nav", "footer", "aside", "form"]


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def visible_text_len(html: str) -> int:
    soup = _soup(html)
    for t in soup(DROP_TAGS):
        t.decompose()
    return len(soup.get_text(" ", strip=True))


def page_meta(html: str) -> dict:
    soup = _soup(html)
    title = soup.title.get_text(strip=True) if soup.title else ""
    desc = soup.find("meta", attrs={"name": "description"})
    return {"title": title, "description": desc.get("content", "") if desc else ""}


def _absolutize(soup: BeautifulSoup, base_url: str) -> None:
    for tag, attr in (("a", "href"), ("img", "src")):
        for el in soup.find_all(tag):
            val = el.get(attr)
            if val and not val.startswith(("data:", "javascript:", "mailto:", "tel:")):
                el[attr] = urljoin(base_url, val)


class _MD(MarkdownConverter):
    def convert_img(self, el, text, parent_tags):
        alt = el.attrs.get("alt", "") or ""
        src = el.attrs.get("src", "") or ""
        return f"![{alt}]({src})" if src and not src.startswith("data:") else ""


def html_to_markdown(html: str, base_url: str = "", main_content_only: bool = False,
                     keep_images: bool = True) -> str:
    if main_content_only:
        md = _main_content(html, base_url)
        if md:
            return md
    soup = _soup(html)
    for t in soup(DROP_TAGS):
        t.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    if not keep_images:
        for img in soup.find_all("img"):
            img.decompose()
    if base_url:
        _absolutize(soup, base_url)
    root = soup.body or soup
    md = _MD(heading_style="ATX", bullets="-", strip=["span"]).convert_soup(root)
    return tidy_markdown(md)


def _main_content(html: str, base_url: str) -> str:
    try:
        import trafilatura

        out = trafilatura.extract(
            html, url=base_url or None, output_format="markdown", include_links=True,
            include_images=True, include_tables=True, favor_recall=True,
        )
        return tidy_markdown(out or "")
    except Exception:
        return ""


def tidy_markdown(md: str) -> str:
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def strip_boilerplate(html: str) -> str:
    soup = _soup(html)
    for t in soup(BOILERPLATE_TAGS):
        t.decompose()
    return str(soup)


def extract_links(html: str, base_url: str) -> list[str]:
    soup = _soup(html)
    seen: dict[str, None] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url, _ = urldefrag(urljoin(base_url, href))
        if urlparse(url).scheme in ("http", "https"):
            seen.setdefault(url, None)
    return list(seen)


NEXT_TEXT = re.compile(r"^\s*(next|next page|older|more|›|»|→|>)\s*(›|»|→|>)?\s*$", re.I)


def find_next_page(html: str, base_url: str) -> str | None:
    """Find a pagination 'next' link (rel=next or a link labelled Next/»)."""
    soup = _soup(html)
    link = soup.find(["a", "link"], rel=lambda r: r and "next" in (r if isinstance(r, list) else [r]))
    if link and link.get("href"):
        return urljoin(base_url, link["href"])
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True) or a.get("aria-label", "")
        if NEXT_TEXT.match(label or ""):
            return urljoin(base_url, a["href"])
    return None
