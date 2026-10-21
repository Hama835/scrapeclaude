import asyncio
import os

import pytest

from app import cleaner
from app.llm import get_llm
from app.llm.base import parse_json_loose
from app.services.common import chunk_text

REAL = bool(os.environ.get("SC_TEST_REAL_LLM"))


# ---------- unit ----------

def test_markdown_conversion_keeps_links_absolute():
    md = cleaner.html_to_markdown('<body><h1>T</h1><a href="/x">X</a><script>bad()</script></body>', "https://a.com/p/")
    assert "# T" in md and "(https://a.com/x)" in md and "bad()" not in md


def test_find_next_page():
    assert cleaner.find_next_page('<a href="/p2" rel="next">Next</a>', "https://a.com/") == "https://a.com/p2"
    assert cleaner.find_next_page('<a href="/p3">Next »</a>', "https://a.com/") == "https://a.com/p3"
    assert cleaner.find_next_page('<a href="/x">Contact</a>', "https://a.com/") is None


def test_parse_json_loose():
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('Here you go: {"a": [1,2]} done') == {"a": [1, 2]}


def test_chunking_respects_limit():
    text = "\n\n".join(["x" * 50] * 100)
    chunks = chunk_text(text, 500)
    assert len(chunks) > 1 and all(len(c) <= 500 for c in chunks)
    assert chunk_text("short", 500) == ["short"]


# ---------- API ----------

async def test_auth_required(client):
    r = await client.post("/v1/markdownify", json={"website_url": "http://x.y"}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


async def test_markdownify(client, site_url):
    r = await client.post("/v1/markdownify", json={"website_url": site_url + "/", "render_heavy_js": False})
    j = r.json()
    assert r.status_code == 200, j
    assert "# Mug Shop" in j["result"] and j["title"] == "Mug Shop"


async def test_smartscraper_with_pagination(client, site_url):
    r = await client.post("/v1/smartscraper", json={
        "website_url": site_url + "/", "user_prompt": "List products", "total_pages": 3, "render_heavy_js": False,
    })
    j = r.json()
    assert r.status_code == 200, j
    assert j["pages"] == [site_url + "/", site_url + "/page2"]
    if not REAL:
        assert "Mug Shop" in j["result"]["headings"] and "More mugs" in j["result"]["headings"]
    # stored in history
    h = (await client.get(f"/v1/requests/{j['request_id']}")).json()
    assert h["status"] == "completed" and h["service"] == "smartscraper"


async def test_smartscraper_from_html(client):
    r = await client.post("/v1/smartscraper", json={"website_html": "<h1>Hello</h1><p>World</p>", "user_prompt": "x"})
    assert r.status_code == 200, r.json()


async def test_scrape_renders_js(client, site_url):
    r = await client.post("/v1/scrape", json={"website_url": site_url + "/spa"})  # auto mode
    j = r.json()
    assert r.status_code == 200, j
    assert j["rendered_js"] is True and "Rendered by JS" in j["html"]


async def test_sitemap(client, site_url):
    j = (await client.post("/v1/sitemap", json={"website_url": site_url})).json()
    assert j["count"] == 3 and site_url + "/about" in j["urls"]


async def test_crawl_background_job(client, site_url):
    r = await client.post("/v1/crawl", json={
        "url": site_url + "/", "prompt": "Summarise the shop", "depth": 2, "max_pages": 10,
        "render_heavy_js": False,
    })
    assert r.status_code == 202
    rid = r.json()["request_id"]
    for _ in range(120):
        j = (await client.get(f"/v1/crawl/{rid}")).json()
        if j["status"] != "processing":
            break
        await asyncio.sleep(0.25)
    assert j["status"] == "completed", j
    urls = j["result"]["crawled_urls"]
    assert site_url + "/about" in urls and site_url + "/p/blue" in urls
    assert not any("/private" in u for u in urls)
    assert "llm_result" in j["result"]


async def test_agentic_clicks(client, site_url):
    r = await client.post("/v1/agentic-scrapper", json={
        "url": site_url + "/search", "steps": ["Click the Search button"], "user_prompt": "Get the product",
    })
    j = r.json()
    assert r.status_code == 200, j
    assert j["final_url"].endswith("/p/blue"), j["steps_log"]


async def test_private_urls_blocked_when_disabled():
    from app import fetcher
    from app.config import settings

    object.__setattr__(settings, "allow_private_urls", False)
    try:
        with pytest.raises(fetcher.FetchError):
            await fetcher.assert_public_url("http://127.0.0.1:8080/")
    finally:
        object.__setattr__(settings, "allow_private_urls", True)


async def test_status(client):
    j = (await client.get("/v1/status")).json()
    assert j["llm"]["provider"] == get_llm().name
