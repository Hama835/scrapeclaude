"""Shared helpers: load page content and run (possibly chunked) extraction."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from .. import cleaner, fetcher
from ..config import settings
from ..llm import LLMResult, get_llm


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    provider: str = ""
    model: str = ""

    def add(self, r: LLMResult) -> None:
        self.calls += 1
        self.input_tokens += r.input_tokens
        self.output_tokens += r.output_tokens
        self.cost_usd += r.cost_usd or 0.0
        self.provider, self.model = r.provider, r.model

    def as_dict(self) -> dict:
        return {
            "llm_calls": self.calls,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd_estimate": round(self.cost_usd, 6),
        }


@dataclass
class PageContent:
    url: str
    markdown: str
    title: str = ""
    rendered: bool = False
    status: int = 200
    html: str = ""
    warnings: list[str] = field(default_factory=list)


async def load_page(url: str, render_js="auto", scrolls: int = 0, wait_ms: int = 1000,
                    headers: dict | None = None, main_content_only: bool = False) -> PageContent:
    res = await fetcher.fetch(url, render_js=render_js, wait_ms=wait_ms, scrolls=scrolls, headers=headers)
    md = cleaner.html_to_markdown(res.html, res.final_url, main_content_only=main_content_only)
    meta = cleaner.page_meta(res.html)
    page = PageContent(url=res.final_url, markdown=md, title=meta["title"], rendered=res.rendered,
                       status=res.status, html=res.html)
    if res.status >= 400:
        page.warnings.append(f"{res.final_url} returned HTTP {res.status}")
    return page


def chunk_text(text: str, limit: int) -> list[str]:
    """Split on paragraph boundaries into pieces no longer than `limit` characters."""
    if len(text) <= limit:
        return [text]
    chunks, cur, size = [], [], 0
    for para in text.split("\n\n"):
        while len(para) > limit:  # one gigantic paragraph
            if cur:
                chunks.append("\n\n".join(cur))
                cur, size = [], 0
            chunks.append(para[:limit])
            para = para[limit:]
        if size + len(para) + 2 > limit and cur:
            chunks.append("\n\n".join(cur))
            cur, size = [], 0
        cur.append(para)
        size += len(para) + 2
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


MERGE_PROMPT = (
    "The content below contains several partial JSON results, each extracted from a different part "
    "of the same source(s) for this original instruction:\n\n{prompt}\n\n"
    "Merge them into one final answer to the original instruction: combine lists, remove duplicates, "
    "and prefer non-null values."
)


async def extract(content: str, prompt: str, schema: dict | None, usage: Usage) -> object:
    """Extract with Claude; content larger than MAX_CONTENT_CHARS is chunked and merged."""
    llm = get_llm()
    chunks = chunk_text(content, settings.max_content_chars)
    if len(chunks) == 1:
        r = await llm.extract(content, prompt, schema)
        usage.add(r)
        return r.data

    results = await asyncio.gather(*(llm.extract(c, prompt, schema) for c in chunks))
    for r in results:
        usage.add(r)
    partials = "\n\n".join(
        f"<partial index=\"{i}\">\n{json.dumps(r.data, ensure_ascii=False)}\n</partial>" for i, r in enumerate(results)
    )
    merged = await llm.extract(partials, MERGE_PROMPT.format(prompt=prompt), schema)
    usage.add(merged)
    return merged.data


def join_pages(pages: list[PageContent]) -> str:
    if len(pages) == 1:
        return pages[0].markdown
    return "\n\n".join(
        f"<page url=\"{p.url}\" title=\"{p.title}\">\n{p.markdown}\n</page>" for p in pages
    )
