"""SmartScraper, Markdownify, Scrape and SearchScraper."""
from __future__ import annotations

import asyncio

from .. import cleaner, fetcher
from . import search
from .common import PageContent, Usage, extract, join_pages, load_page


class ServiceError(RuntimeError):
    pass


async def smartscraper(*, user_prompt: str, website_url: str | None = None, website_html: str | None = None,
                       website_markdown: str | None = None, output_schema: dict | None = None,
                       render_heavy_js="auto", number_of_scrolls: int = 0, total_pages: int = 1,
                       headers: dict | None = None, main_content_only: bool = False) -> tuple[dict, Usage]:
    usage = Usage()
    pages: list[PageContent] = []
    if website_markdown:
        pages.append(PageContent(url=website_url or "", markdown=website_markdown))
    elif website_html:
        md = cleaner.html_to_markdown(website_html, website_url or "", main_content_only=main_content_only)
        pages.append(PageContent(url=website_url or "", markdown=md))
    elif website_url:
        url = website_url
        for _ in range(max(1, min(total_pages, 20))):
            page = await load_page(url, render_heavy_js, number_of_scrolls, headers=headers,
                                   main_content_only=main_content_only)
            pages.append(page)
            nxt = cleaner.find_next_page(page.html, page.url)
            if not nxt or nxt in {p.url for p in pages}:
                break
            url = nxt
    else:
        raise ServiceError("Provide website_url, website_html or website_markdown.")

    content = join_pages(pages)
    if not content.strip():
        raise ServiceError("The page has no readable content (try render_heavy_js=true).")
    data = await extract(content, user_prompt, output_schema, usage)
    warnings = [w for p in pages for w in p.warnings]
    return {
        "result": data,
        "pages": [p.url for p in pages],
        "rendered_js": any(p.rendered for p in pages),
        "warnings": warnings,
    }, usage


async def markdownify(*, website_url: str, render_heavy_js="auto", main_content_only: bool = False,
                      number_of_scrolls: int = 0, headers: dict | None = None) -> dict:
    page = await load_page(website_url, render_heavy_js, number_of_scrolls, headers=headers,
                           main_content_only=main_content_only)
    return {"result": page.markdown, "title": page.title, "url": page.url,
            "rendered_js": page.rendered, "warnings": page.warnings}


async def scrape(*, website_url: str, render_heavy_js="auto", number_of_scrolls: int = 0,
                 headers: dict | None = None) -> dict:
    res = await fetcher.fetch(website_url, render_js=render_heavy_js, scrolls=number_of_scrolls, headers=headers)
    return {"html": res.html, "status_code": res.status, "url": res.final_url, "rendered_js": res.rendered}


async def searchscraper(*, user_prompt: str, num_results: int = 3, output_schema: dict | None = None,
                        extraction_mode: bool = True, render_heavy_js="auto") -> tuple[dict, Usage]:
    usage = Usage()
    num_results = max(1, min(num_results, 20))
    hits = await search.web_search(user_prompt, num_results * 2)
    if not hits:
        raise ServiceError("Web search returned no results.")

    async def _load(url):
        try:
            return await load_page(url, render_heavy_js, main_content_only=True)
        except fetcher.FetchError:
            return None

    pages: list[PageContent] = []
    for page in await asyncio.gather(*(_load(h["url"]) for h in hits)):
        if page and page.markdown.strip() and page.status < 400:
            pages.append(page)
        if len(pages) >= num_results:
            break
    if not pages:
        raise ServiceError("None of the search results could be loaded.")

    refs = [p.url for p in pages]
    if not extraction_mode:
        return {"result": [{"url": p.url, "title": p.title, "markdown": p.markdown} for p in pages],
                "reference_urls": refs}, usage
    prompt = (
        user_prompt
        + "\n\nThe content comes from several web pages (each inside <page url=...>). "
        "Combine the information to answer, and when sources disagree prefer the most specific one."
    )
    data = await extract(join_pages(pages), prompt, output_schema, usage)
    return {"result": data, "reference_urls": refs}, usage
