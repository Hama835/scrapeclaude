"""Tests for the security controls: URL policy, browser guard, auth, limits, headers."""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from urllib.parse import quote

import httpx
import pytest

from app import auth, db, fetcher
from app.config import settings
from app.services import sitemap as sitemap_svc
from tests.conftest import TEST_KEY


@contextmanager
def setting(name: str, value):
    old = getattr(settings, name)
    object.__setattr__(settings, name, value)
    try:
        yield
    finally:
        object.__setattr__(settings, name, old)


@pytest.fixture(autouse=True)
def _clean_auth_state():
    auth.reset_failures()
    yield
    auth.reset_failures()


# ---------- URL policy ----------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://0.0.0.0/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
    "http://2130706433/", "http://0x7f000001/", "http://169.254.169.254/latest/meta-data/", "http://10.0.0.1/",
    "http://192.168.1.1/", "http://100.64.0.1/", "http://100.100.100.100/", "http://192.0.0.1/",
    "http://198.18.0.1/", "http://[fd00::1]/", "http://[fe80::1]/", "http://[64:ff9b::7f00:1]/",
    "http://224.0.0.1/", "file:///etc/passwd", "gopher://x/",
])
async def test_private_and_special_addresses_blocked(url):
    with setting("allow_private_urls", False):
        with pytest.raises(fetcher.FetchError):
            await fetcher.assert_public_url(url)


def test_public_ip_classification():
    import ipaddress
    assert fetcher.is_public_ip(ipaddress.ip_address("93.184.216.34"))
    assert fetcher.is_public_ip(ipaddress.ip_address("2606:4700::1111"))
    for ip in ("100.64.0.1", "127.0.0.1", "10.1.1.1", "169.254.1.1", "0.0.0.0", "224.0.0.1", "240.0.0.1", "::1"):
        assert not fetcher.is_public_ip(ipaddress.ip_address(ip)), ip


# ---------- Browser guard ----------

async def test_browser_redirect_to_blocked_address_is_refused(site_url, blocked_internal):
    with pytest.raises(fetcher.FetchError) as e:
        await fetcher.browser.render(f"{site_url}/redirect-to/{quote(blocked_internal + '/', safe='')}", wait_ms=200)
    assert "INTERNAL-SECRET" not in str(e.value)


async def test_browser_in_page_fetch_to_blocked_address_is_aborted(site_url, blocked_internal):
    res = await fetcher.browser.render(f"{site_url}/fetch-from/{quote(blocked_internal + '/', safe='')}", wait_ms=1500)
    assert "INTERNAL-SECRET" not in res.html
    assert "textContent='EXFIL:'" in res.html and ">EXFIL:" not in res.html
    assert ">ERR:" in res.html


async def test_browser_in_page_fetch_to_allowed_address_still_works(site_url, internal_url):
    res = await fetcher.browser.render(f"{site_url}/fetch-from/{quote(internal_url + '/', safe='')}", wait_ms=1500)
    assert "INTERNAL-SECRET-DATA-42" in res.html


async def test_http_redirect_to_blocked_address_is_refused(site_url, blocked_internal):
    with pytest.raises(fetcher.FetchError):
        await fetcher.fetch_http(f"{site_url}/redirect-to/{quote(blocked_internal + '/', safe='')}")


async def test_custom_headers_only_sent_to_target_host(site_url):
    same = await fetcher.browser.render(f"{site_url}/echo-secret", wait_ms=100, headers={"X-Secret": "token123"})
    assert "secret=token123" in same.html
    ctx, _ = await fetcher.browser.new_context({"X-Secret": "token123"}, headers_host="example.invalid:80")
    try:
        page = await ctx.new_page()
        await page.goto(f"{site_url}/echo-secret")
        assert "secret=token123" not in await page.content()
    finally:
        await ctx.close()


async def test_agentic_click_to_other_host_blocked(client, site_url, internal_url):
    r = await client.post("/v1/agentic-scrapper", json={
        "url": f"{site_url}/other-link", "steps": ["Click the Other site link"], "ai_extraction": False,
    })
    j = r.json()
    assert r.status_code == 200, j
    assert j["final_url"].startswith(site_url)
    assert j.get("blocked_navigations"), j


async def test_agentic_click_to_other_host_allowed_when_requested(client, site_url, internal_url):
    r = await client.post("/v1/agentic-scrapper", json={
        "url": f"{site_url}/other-link", "steps": ["Click the Other site link"], "ai_extraction": False,
        "allow_other_domains": True,
    })
    j = r.json()
    assert r.status_code == 200, j
    assert j["final_url"].startswith(internal_url)


# ---------- Size limits ----------

async def test_response_size_cap(site_url):
    with setting("max_fetch_bytes", 100_000):
        with pytest.raises(fetcher.FetchError, match="byte limit"):
            await fetcher.fetch_http(f"{site_url}/big")


async def test_unsupported_content_type_rejected(site_url):
    with pytest.raises(fetcher.FetchError, match="Unsupported content type"):
        await fetcher.fetch_http(f"{site_url}/binary.bin")


async def test_gzip_bomb_limited(site_url):
    with setting("max_fetch_bytes", 200_000):
        assert await sitemap_svc._get(f"{site_url}/bomb.xml.gz") is None
    with setting("max_fetch_bytes", 10_000_000):
        xml = await sitemap_svc._get(f"{site_url}/bomb.xml.gz")
        assert xml and xml.count("<loc>") == 60_000


async def test_inline_html_size_limit(client):
    r = await client.post("/v1/smartscraper", json={"website_html": "x" * 5_000_001, "user_prompt": "x"})
    assert r.status_code == 422


# ---------- Authentication ----------

async def test_wrong_key_401_and_logged(client, site_url):
    r = await client.post("/v1/markdownify", json={"website_url": site_url}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


async def test_non_ascii_key_is_401_not_500(client):
    r = await client.get("/v1/status", headers=httpx.Headers({b"X-API-Key": "caf\xe9".encode("latin-1")}))
    assert r.status_code == 401


async def test_missing_key_401(client):
    r = await client.get("/v1/status", headers={"X-API-Key": ""})
    assert r.status_code == 401


async def test_bearer_and_sgai_headers(client):
    assert (await client.get("/v1/status", headers={"X-API-Key": "", "Authorization": f"Bearer {TEST_KEY}"})).status_code == 200
    assert (await client.get("/v1/status", headers={"X-API-Key": "", "SGAI-APIKEY": TEST_KEY})).status_code == 200


async def test_multiple_keys_supported(client):
    with setting("api_keys", (TEST_KEY, "second-key-0123456789abcdef0123456789")):
        r = await client.get("/v1/status", headers={"X-API-Key": "second-key-0123456789abcdef0123456789"})
        assert r.status_code == 200


async def test_lockout_after_repeated_failures(client):
    with setting("auth_fail_limit", 3):
        for _ in range(3):
            assert (await client.get("/v1/status", headers={"X-API-Key": "nope"})).status_code == 401
        assert (await client.get("/v1/status", headers={"X-API-Key": "nope"})).status_code == 429
        assert (await client.get("/v1/status")).status_code == 429
        r = await client.post("/auth/login", json={"key": TEST_KEY}, headers={"X-API-Key": ""})
        assert r.status_code == 429


async def test_cookie_session_flow(client, site_url):
    anon = {"X-API-Key": ""}
    assert (await client.get("/auth/me", headers=anon)).status_code == 401
    bad = await client.post("/auth/login", json={"key": "wrong"}, headers=anon)
    assert bad.status_code == 401 and auth.SESSION_COOKIE not in bad.cookies

    ok = await client.post("/auth/login", json={"key": TEST_KEY}, headers=anon)
    assert ok.status_code == 200 and auth.SESSION_COOKIE in ok.cookies
    set_cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie
    cookies = {auth.SESSION_COOKIE: ok.cookies[auth.SESSION_COOKIE]}

    assert (await client.get("/auth/me", headers=anon, cookies=cookies)).status_code == 200
    assert (await client.get("/v1/status", headers=anon, cookies=cookies)).status_code == 200
    no_header = await client.post("/v1/markdownify", json={"website_url": site_url + "/"}, headers=anon, cookies=cookies)
    assert no_header.status_code == 403
    with_header = await client.post("/v1/markdownify", json={"website_url": site_url + "/", "render_heavy_js": False},
                                    headers={**anon, "X-Requested-With": "fetch"}, cookies=cookies)
    assert with_header.status_code == 200, with_header.json()

    out = await client.post("/auth/logout", json={}, headers={**anon, "X-Requested-With": "fetch"}, cookies=cookies)
    assert out.status_code == 200
    assert (await client.get("/auth/me", headers=anon, cookies=cookies)).status_code == 401


async def test_expired_session_rejected(client):
    anon = {"X-API-Key": ""}
    with setting("session_ttl_s", 1):
        ok = await client.post("/auth/login", json={"key": TEST_KEY}, headers=anon)
        cookies = {auth.SESSION_COOKIE: ok.cookies[auth.SESSION_COOKIE]}
        assert (await client.get("/auth/me", headers=anon, cookies=cookies)).status_code == 200
        await asyncio.sleep(1.1)
        assert (await client.get("/auth/me", headers=anon, cookies=cookies)).status_code == 401


async def test_forged_session_cookie_rejected(client):
    r = await client.get("/auth/me", headers={"X-API-Key": ""}, cookies={auth.SESSION_COOKIE: "a" * 43})
    assert r.status_code == 401


async def test_docs_require_auth(client):
    anon = {"X-API-Key": ""}
    assert (await client.get("/docs", headers=anon)).status_code == 401
    assert (await client.get("/openapi.json", headers=anon)).status_code == 401
    assert (await client.get("/redoc", headers=anon)).status_code == 404
    assert (await client.get("/docs")).status_code == 200
    assert (await client.get("/openapi.json")).json()["info"]["title"] == "ScrapeClaude"
    assert (await client.get("/health", headers=anon)).status_code == 200


def test_startup_refuses_empty_or_short_key():
    with setting("api_keys", ()), setting("allow_no_auth", False):
        with pytest.raises(RuntimeError, match="SC_API_KEY is empty"):
            auth.validate_startup()
    with setting("api_keys", ()), setting("allow_no_auth", True):
        auth.validate_startup()
    with setting("api_keys", ("short",)):
        with pytest.raises(RuntimeError, match="at least 32"):
            auth.validate_startup()
    with setting("api_keys", (TEST_KEY,)):
        auth.validate_startup()


# ---------- Headers and error handling ----------

async def test_security_headers_and_csp(client):
    r = await client.get("/", headers={"X-API-Key": ""})
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert "onclick=" not in r.text and "<script>" not in r.text
    api = await client.get("/v1/status")
    assert api.headers["cache-control"] == "no-store"
    docs = await client.get("/docs")
    assert "cdn.jsdelivr.net" in docs.headers["content-security-policy"]


async def test_static_assets_served(client):
    anon = {"X-API-Key": ""}
    assert (await client.get("/static/app.js", headers=anon)).status_code == 200
    assert (await client.get("/static/style.css", headers=anon)).status_code == 200
    assert (await client.get("/static/../main.py", headers=anon)).status_code in (400, 404)


async def test_internal_errors_are_not_echoed(client, monkeypatch):
    from app.services import scrapers

    async def boom(**kw):
        raise KeyError("secret /opt/path detail")

    monkeypatch.setattr(scrapers, "markdownify", boom)
    r = await client.post("/v1/markdownify", json={"website_url": "http://127.0.0.1:1/"})
    assert r.status_code == 500
    assert "secret" not in r.text and "Internal error" in r.json()["error"]


# ---------- History hygiene ----------

def test_stale_jobs_marked_failed_and_retention():
    rid = db.create("crawl", {"url": "x"})
    assert db.get(rid)["status"] == "processing"
    assert db.mark_stale_failed() >= 1
    assert db.get(rid)["status"] == "failed"
    old = db.create("scrape", {"website_url": "y"})
    db.conn().execute("UPDATE requests SET created_at=? WHERE id=?", (time.time() - 40 * 86400, old))
    db.conn().commit()
    assert db.purge_older_than(0) == 0
    assert db.purge_older_than(30) >= 1
    assert db.get(old) is None
    db.delete(rid)


def test_db_file_permissions():
    path = settings.db_path
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(os.path.dirname(path)).st_mode & 0o777) == "0o700"


# ---------- Remote browser (separate container mode) ----------

@pytest.fixture()
def playwright_server():
    exe = shutil.which("playwright", path=os.path.dirname(sys.executable))
    if not exe:
        pytest.skip("playwright CLI not available")
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen([exe, "run-server", "--port", str(port), "--host", "127.0.0.1"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 30
    line = ""
    while time.time() < deadline:
        line = proc.stdout.readline()
        if "Listening on" in line:
            break
    else:
        proc.kill()
        pytest.skip("playwright run-server did not start")
    yield line.split("Listening on", 1)[1].strip()
    proc.kill()


async def test_remote_browser_connect(site_url, playwright_server):
    remote = fetcher.Browser()
    with setting("browser_ws_url", playwright_server):
        try:
            res = await remote.render(f"{site_url}/spa", wait_ms=300)
            assert "Rendered by JS" in res.html and res.rendered
        finally:
            await remote.close()


# ---------- Dashboard email + password sign-in ----------

def test_password_hash_roundtrip():
    h = auth.hash_password("correct horse battery", iterations=1000)
    assert h.startswith("pbkdf2_sha256:1000:") and "$" not in h
    assert auth.verify_password("correct horse battery", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", "garbage")
    assert auth.hash_password("a", 1000) != auth.hash_password("a", 1000)


async def test_password_login_mode(client, site_url):
    anon = {"X-API-Key": ""}
    assert (await client.get("/auth/mode", headers=anon)).json()["mode"] == "key"
    h = auth.hash_password("s3cret-password", iterations=1000)
    with setting("dashboard_email", "me@example.com"), setting("dashboard_password_hash", h):
        auth.validate_startup()
        assert (await client.get("/auth/mode", headers=anon)).json()["mode"] == "password"
        r = await client.post("/auth/login", json={"key": TEST_KEY}, headers=anon)
        assert r.status_code == 401, "API key must not sign in to the dashboard in password mode"
        r = await client.post("/auth/login", json={"email": "me@example.com", "password": "nope"}, headers=anon)
        assert r.status_code == 401
        r = await client.post("/auth/login", json={"email": "other@example.com", "password": "s3cret-password"}, headers=anon)
        assert r.status_code == 401
        r = await client.post("/auth/login", json={"email": "ME@example.com ", "password": "s3cret-password"}, headers=anon)
        assert r.status_code == 200 and auth.SESSION_COOKIE in r.cookies
        cookies = {auth.SESSION_COOKIE: r.cookies[auth.SESSION_COOKIE]}
        assert (await client.get("/v1/status", headers=anon, cookies=cookies)).status_code == 200
        assert (await client.get("/v1/status")).status_code == 200, "API key header still works for scripts"
    with setting("dashboard_email", "me@example.com"), setting("dashboard_password_hash", ""):
        with pytest.raises(RuntimeError, match="both"):
            auth.validate_startup()
    with setting("dashboard_email", "me@example.com"), setting("dashboard_password_hash", "plaintext"):
        with pytest.raises(RuntimeError, match="not a hash"):
            auth.validate_startup()


async def test_password_login_lockout(client):
    anon = {"X-API-Key": ""}
    h = auth.hash_password("s3cret-password", iterations=1000)
    with setting("dashboard_email", "me@example.com"), setting("dashboard_password_hash", h), setting("auth_fail_limit", 3):
        for _ in range(3):
            await client.post("/auth/login", json={"email": "me@example.com", "password": "bad"}, headers=anon)
        r = await client.post("/auth/login", json={"email": "me@example.com", "password": "s3cret-password"}, headers=anon)
        assert r.status_code == 429
