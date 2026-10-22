"""ScrapeClaude: a self-hosted, Claude-powered AI web scraper (ScrapeGraphAI-style API)."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Union

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from . import auth, db, fetcher
from .config import settings
from .llm import LLMError, provider_status
from .services import agentic as agentic_svc
from .services import crawler, scrapers, sitemap as sitemap_svc
from .services.scrapers import ServiceError
from .services.search import SearchError

log = logging.getLogger("scrapeclaude")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"
MAX_INLINE_CHARS = 5_000_000
_jobs: set[asyncio.Task] = set()

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
CSP_DEFAULT = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "font-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
CSP_DOCS = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' "
    "https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.validate_startup()
    db.conn()
    stale = db.mark_stale_failed()
    if stale:
        log.info("marked %d interrupted job(s) as failed", stale)
    purged = db.purge_older_than(settings.history_retention_days)
    if purged:
        log.info("purged %d history row(s) older than %d days", purged, settings.history_retention_days)
    log.info("LLM: %s", provider_status())
    yield
    await fetcher.browser.close()


app = FastAPI(title="ScrapeClaude", version="1.1.0", lifespan=lifespan,
              description="AI web scraping powered by Claude. Send a URL and a prompt, get JSON.",
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    response.headers.setdefault("Content-Security-Policy", CSP_DOCS if request.url.path == "/docs" else CSP_DEFAULT)
    if request.url.path.startswith(("/v1/", "/auth/")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


require_auth = [Depends(auth.require_key)]

# ---------- Request models ----------

RenderJS = Union[bool, Literal["auto"]]


class SmartScraperReq(BaseModel):
    user_prompt: str = Field(..., min_length=1, max_length=20_000, examples=["Extract all product names and prices"])
    website_url: str | None = Field(None, max_length=4096)
    website_html: str | None = Field(None, max_length=MAX_INLINE_CHARS)
    website_markdown: str | None = Field(None, max_length=MAX_INLINE_CHARS)
    output_schema: dict | None = None
    render_heavy_js: RenderJS = "auto"
    number_of_scrolls: int = Field(0, ge=0, le=50)
    total_pages: int = Field(1, ge=1, le=20)
    main_content_only: bool = False
    headers: dict[str, str] | None = None

    @model_validator(mode="after")
    def _source(self):
        if not (self.website_url or self.website_html or self.website_markdown):
            raise ValueError("Provide website_url, website_html or website_markdown")
        return self


class SearchScraperReq(BaseModel):
    user_prompt: str = Field(..., min_length=1, max_length=20_000)
    num_results: int = Field(3, ge=1, le=20)
    output_schema: dict | None = None
    extraction_mode: bool = True
    render_heavy_js: RenderJS = "auto"


class MarkdownifyReq(BaseModel):
    website_url: str = Field(..., max_length=4096)
    render_heavy_js: RenderJS = "auto"
    main_content_only: bool = False
    number_of_scrolls: int = Field(0, ge=0, le=50)
    headers: dict[str, str] | None = None


class ScrapeReq(BaseModel):
    website_url: str = Field(..., max_length=4096)
    render_heavy_js: RenderJS = "auto"
    number_of_scrolls: int = Field(0, ge=0, le=50)
    headers: dict[str, str] | None = None


class SitemapReq(BaseModel):
    website_url: str = Field(..., max_length=4096)
    max_urls: int = Field(5000, ge=1, le=50000)


class CrawlReq(BaseModel):
    url: str = Field(..., max_length=4096)
    prompt: str | None = Field(None, max_length=20_000)
    schema_: dict | None = Field(None, alias="schema")
    extraction_mode: bool = True
    depth: int = Field(2, ge=0, le=10)
    max_pages: int = Field(10, ge=1, le=500)
    same_domain_only: bool = True
    sitemap: bool = False
    include_paths: list[str] | None = None
    exclude_paths: list[str] | None = None
    respect_robots: bool = True
    render_heavy_js: RenderJS = "auto"

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _prompt(self):
        if self.extraction_mode and not self.prompt:
            raise ValueError("prompt is required when extraction_mode is true")
        return self


class AgenticReq(BaseModel):
    url: str = Field(..., max_length=4096)
    steps: list[str] = Field(..., min_length=1, max_length=30)
    user_prompt: str | None = Field(None, max_length=20_000)
    output_schema: dict | None = None
    ai_extraction: bool = True
    headers: dict[str, str] | None = None
    allow_other_domains: bool = False


class LoginReq(BaseModel):
    email: str | None = Field(None, max_length=254)
    password: str | None = Field(None, max_length=512)
    key: str | None = Field(None, max_length=512)


# ---------- Runner ----------

KNOWN_ERRORS = (ServiceError, fetcher.FetchError, LLMError, SearchError, ValueError)
INTERNAL_ERROR = "Internal error. Details were written to the server log."


async def run_sync(service: str, params: dict, coro_factory) -> dict:
    """Run a service, store it in history, and return a ScrapeGraph-style response."""
    rid = db.create(service, _redact(params))
    started = time.time()
    try:
        out = await coro_factory()
    except KNOWN_ERRORS as e:
        db.finish(rid, "failed", error=str(e))
        raise HTTPException(422, {"request_id": rid, "status": "failed", "error": str(e)})
    except Exception:
        log.exception("%s failed (request %s)", service, rid)
        db.finish(rid, "failed", error=INTERNAL_ERROR)
        raise HTTPException(500, {"request_id": rid, "status": "failed", "error": INTERNAL_ERROR})
    result, usage = out if isinstance(out, tuple) else (out, None)
    usage_d = usage.as_dict() if usage else None
    db.finish(rid, "completed", result=result, usage=usage_d)
    return {"request_id": rid, "status": "completed", **result, "usage": usage_d,
            "duration_s": round(time.time() - started, 2)}


def _redact(params: dict) -> dict:
    p = dict(params)
    if p.get("website_html"):
        p["website_html"] = f"<{len(p['website_html'])} chars of HTML>"
    if p.get("website_markdown"):
        p["website_markdown"] = f"<{len(p['website_markdown'])} chars of markdown>"
    if p.get("headers"):
        p["headers"] = {k: "***" for k in p["headers"]}
    return p


# ---------- Auth endpoints ----------

@app.get("/auth/mode", tags=["auth"])
async def auth_mode():
    """Tells the dashboard which sign-in form to show."""
    if not settings.api_keys and not auth.password_login_enabled():
        return {"mode": "none"}
    return {"mode": "password" if auth.password_login_enabled() else "key"}


@app.post("/auth/login", tags=["auth"])
async def login(req: LoginReq, request: Request, response: Response):
    """Sign the dashboard in (email + password, or the API key when no password is configured)."""
    if not settings.api_keys and not auth.password_login_enabled():
        return {"authenticated": True, "auth_disabled": True}
    auth.ensure_not_locked_out(request)
    if auth.password_login_enabled():
        ok = auth.credentials_match(req.email or "", req.password or "")
        reason = "login with invalid email/password"
        message = "Invalid email or password."
    else:
        ok = bool(req.key) and auth.key_matches(req.key)
        reason = "login with invalid key"
        message = "Invalid API key."
    if not ok:
        auth.record_failure(request, reason)
        raise HTTPException(401, message)
    auth.set_session_cookie(response, request, auth.create_session())
    return {"authenticated": True}


@app.post("/auth/logout", tags=["auth"])
async def logout(request: Request, response: Response):
    auth.destroy_session(request.cookies.get(auth.SESSION_COOKIE))
    auth.clear_session_cookie(response)
    return {"authenticated": False}


@app.get("/auth/me", dependencies=require_auth, tags=["auth"])
async def me():
    return {"authenticated": True}


# ---------- Scraping endpoints ----------

@app.post("/v1/smartscraper", dependencies=require_auth, tags=["scrape"])
async def smartscraper(req: SmartScraperReq):
    """Extract structured data from one page (or a few paginated pages) using a prompt."""
    return await run_sync("smartscraper", req.model_dump(), lambda: scrapers.smartscraper(**req.model_dump()))


@app.post("/v1/searchscraper", dependencies=require_auth, tags=["scrape"])
async def searchscraper(req: SearchScraperReq):
    """Search the web, read the top results, and answer the prompt with sources."""
    return await run_sync("searchscraper", req.model_dump(), lambda: scrapers.searchscraper(**req.model_dump()))


@app.post("/v1/markdownify", dependencies=require_auth, tags=["convert"])
async def markdownify(req: MarkdownifyReq):
    """Convert a web page to clean Markdown (no AI call)."""
    return await run_sync("markdownify", req.model_dump(), lambda: scrapers.markdownify(**req.model_dump()))


@app.post("/v1/scrape", dependencies=require_auth, tags=["convert"])
async def scrape(req: ScrapeReq):
    """Return the raw (optionally JavaScript-rendered) HTML of a page (no AI call)."""
    return await run_sync("scrape", req.model_dump(), lambda: scrapers.scrape(**req.model_dump()))


@app.post("/v1/sitemap", dependencies=require_auth, tags=["convert"])
async def sitemap(req: SitemapReq):
    """List every URL from a site's sitemap(s)."""
    return await run_sync("sitemap", req.model_dump(), lambda: sitemap_svc.sitemap(**req.model_dump()))


@app.post("/v1/agentic-scrapper", dependencies=require_auth, tags=["scrape"])
@app.post("/v1/agentic-scraper", dependencies=require_auth, tags=["scrape"], include_in_schema=False)
async def agentic(req: AgenticReq):
    """Perform plain-English browser steps (log in, search, click...), then extract."""
    return await run_sync("agentic", req.model_dump(), lambda: agentic_svc.agentic(**req.model_dump()))


@app.post("/v1/crawl", dependencies=require_auth, tags=["crawl"], status_code=202)
async def start_crawl(req: CrawlReq):
    """Start a multi-page crawl in the background. Poll GET /v1/crawl/{request_id}."""
    params = req.model_dump(by_alias=True)
    rid = db.create("crawl", params)

    async def job():
        def progress(p: dict) -> None:
            db.update_progress(rid, {"progress": p})

        try:
            result, usage = await asyncio.wait_for(
                crawler.crawl(
                    url=req.url, prompt=req.prompt, schema=req.schema_, extraction_mode=req.extraction_mode,
                    depth=req.depth, max_pages=req.max_pages, same_domain_only=req.same_domain_only,
                    use_sitemap=req.sitemap, include_paths=req.include_paths, exclude_paths=req.exclude_paths,
                    respect_robots=req.respect_robots, render_heavy_js=req.render_heavy_js, on_progress=progress,
                ),
                timeout=settings.crawl_timeout_s,
            )
            db.finish(rid, "completed", result=result, usage=usage.as_dict())
        except asyncio.TimeoutError:
            db.finish(rid, "failed", error=f"Crawl exceeded the {settings.crawl_timeout_s}s limit (CRAWL_TIMEOUT_S)")
        except KNOWN_ERRORS as e:
            db.finish(rid, "failed", error=str(e))
        except Exception:
            log.exception("crawl failed (request %s)", rid)
            db.finish(rid, "failed", error=INTERNAL_ERROR)

    task = asyncio.create_task(job())
    _jobs.add(task)
    task.add_done_callback(_jobs.discard)
    return {"request_id": rid, "status": "processing"}


@app.get("/v1/crawl/{rid}", dependencies=require_auth, tags=["crawl"])
async def get_crawl(rid: str):
    return _get_or_404(rid)


@app.get("/v1/requests", dependencies=require_auth, tags=["history"])
async def list_requests(limit: int = 50, service: str | None = None):
    return {"requests": db.list_recent(min(limit, 500), service)}


@app.get("/v1/requests/{rid}", dependencies=require_auth, tags=["history"])
@app.get("/v1/smartscraper/{rid}", dependencies=require_auth, include_in_schema=False)
@app.get("/v1/searchscraper/{rid}", dependencies=require_auth, include_in_schema=False)
@app.get("/v1/markdownify/{rid}", dependencies=require_auth, include_in_schema=False)
async def get_request(rid: str):
    return _get_or_404(rid)


@app.delete("/v1/requests/{rid}", dependencies=require_auth, tags=["history"])
async def delete_request(rid: str):
    if not db.delete(rid):
        raise HTTPException(404, "Request not found")
    return {"deleted": rid}


@app.get("/v1/status", dependencies=require_auth, tags=["system"])
async def status():
    return {"llm": provider_status(), "browser_enabled": settings.browser_enabled,
            "search_provider": settings.search_provider, "stats": db.stats()}


@app.get("/health", tags=["system"])
async def health():
    return {"ok": True}


# ---------- Dashboard and docs (docs need a login or an API key) ----------

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/openapi.json", dependencies=require_auth, include_in_schema=False)
async def openapi_schema():
    return JSONResponse(get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes))


@app.get("/docs", dependencies=require_auth, include_in_schema=False)
async def docs():
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} API docs")


def _get_or_404(rid: str) -> dict:
    row = db.get(rid)
    if not row:
        raise HTTPException(404, "Request not found")
    return row


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    body: Any = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
    return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))
