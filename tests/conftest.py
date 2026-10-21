"""Test setup: a tiny local website + the API wired to a fake LLM (no network, no Claude calls).

Set SC_TEST_REAL_LLM=1 to run the marked tests against the real provider (claude CLI / API key).
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

TMP = tempfile.mkdtemp(prefix="sc-test-")
os.environ.update({
    "SC_ENV_FILE": "/nonexistent",
    "SC_API_KEY": "test-key",
    "ALLOW_PRIVATE_URLS": "true",
    "DB_PATH": str(Path(TMP) / "test.db"),
    "LLM_PROVIDER": os.environ.get("SC_TEST_PROVIDER", "fake") if os.environ.get("SC_TEST_REAL_LLM") else "fake",
})
if os.path.exists("/opt/pw-browsers/chromium"):
    os.environ.setdefault("CHROMIUM_PATH", "/opt/pw-browsers/chromium")

SITE = {
    "/": """<html><head><title>Mug Shop</title><meta name="description" content="Best mugs"></head><body>
        <nav><a href="/">Home</a> <a href="/about">About</a></nav>
        <h1>Mug Shop</h1>
        <div class="product"><h2>Blue Mug</h2><span class="price">$12.50</span><a href="/p/blue">View</a></div>
        <div class="product"><h2>Red Mug</h2><span class="price">$9.99</span><a href="/p/red">View</a></div>
        <p>Free shipping on orders over $30. We ship worldwide from Erbil.</p>
        <a href="/page2" rel="next">Next</a> <a href="/private">Private</a>
        <footer>© Mug Shop</footer></body></html>""",
    "/page2": """<html><head><title>Mug Shop p2</title></head><body><h1>More mugs</h1>
        <div class="product"><h2>Green Mug</h2><span class="price">$15.00</span></div>
        <p>Second page of the catalogue with more items for everyone.</p>
        </body></html>""",
    "/about": "<html><head><title>About</title></head><body><h1>About us</h1><p>Family business since 1999. "
              "We make handmade ceramic mugs in small batches, and every one is glazed by hand.</p></body></html>",
    "/p/blue": "<html><body><h1>Blue Mug</h1><p>350ml ceramic mug, dishwasher safe. Price $12.50. "
               "Ships in two days from our workshop, wrapped in recycled paper.</p></body></html>",
    "/p/red": "<html><body><h1>Red Mug</h1><p>300ml mug. Price $9.99. A bright red glaze with a matte "
              "finish that looks great on any desk or kitchen shelf.</p></body></html>",
    "/spa": """<html><body><div id="app"></div><script>
        document.getElementById('app').innerHTML = '<h1>Rendered by JS</h1><p>' + 'Dynamic content here. '.repeat(40) + '</p>';
        </script></body></html>""",
    "/search": """<html><body><h1>Search</h1><input name="q" placeholder="Search mugs">
        <button onclick="location.href='/p/blue'">Search</button></body></html>""",
    "/robots.txt": "User-agent: *\nDisallow: /private\nSitemap: {base}/sitemap.xml\n",
    "/sitemap.xml": """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>{base}/</loc></url><url><loc>{base}/about</loc></url><url><loc>{base}/p/blue</loc></url></urlset>""",
    "/private": "<html><body><h1>Secret</h1></body></html>",
}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def site_url():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = SITE.get(self.path.split("?")[0])
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            data = body.replace("{base}", base).encode()
            self.send_response(200)
            ctype = "text/plain" if self.path.endswith(".txt") else (
                "application/xml" if self.path.endswith(".xml") else "text/html; charset=utf-8")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.1)
    yield base
    srv.shutdown()


@pytest.fixture()
async def client():
    import httpx

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 headers={"X-API-Key": "test-key"}, timeout=120) as c:
        yield c
