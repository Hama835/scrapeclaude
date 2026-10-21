"""ScrapeClaude: a self-hosted, Claude-powered AI web scraper (ScrapeGraphAI-style API)."""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Union

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator

from . import db, fetcher
from .config import settings
from .llm import LLMError, provider_status
from .services import agentic as agentic_svc
from .services import crawler, scrapers, sitemap as sitemap_svc
from .services.scrapers import ServiceError
from .services.search import SearchError

log = logging.getLogger("scrapeclaude")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"
_jobs: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    if not settings.api_key:
        log.warning("SC_API_KEY is empty: the API is open to anyone who can reach it. Set it before deploying.")
    log.info("LLM: %s", provider_status())
    yield
    await fetcher.browser.close()


app = FastAPI(title="ScrapeClaude", version="1.0.0", lifespan=lifespan,
              description="AI web scraping powered by Claude. Send a URL and a prompt, get JSON.")


# ---------------- Auth ----------------

def require_key(
    x_api_key: str | None = Header(default=None),
    sgai_apikey: str | None = Header(default=None, alias="SGAI-APIKEY"),
    authorization: str | None = Header(default=None),
) -> None:
    if not settings.api_key:
        return
    supplied = x_api_key or sgai_apikey
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:]
    if not supplied or not hmac.compare_digest(supplied, settings.api_key):
        raise HTTPException(401, "Invalid or missing API key (send header X-API-Key).")


auth = [Depends(require_key)]

# ---------------- Request models ----------------

RenderJS = Union[bool, Literal["auto"]]


class SmartScraperReq(BaseModel):
    user_prompt: str = Field(..., min_length=1, examples=["Extract all product names and prices"])
    website_url: str | None = None
    website_html: str | None = None
    website_markdown: str | None = None
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
    user_prompt: str = Field(..., min_length=1)
    num_results: int = Field(3, ge=1, le=20)
    output_schema: dict | None = None
    extraction_mode: bool = True
    render_heavy_js: RenderJS = "auto"


class MarkdownifyReq(BaseModel):
    website_url: str
    render_heavy_js: RenderJS = "auto"
    main_content_only: bool = False
    number_of_scrolls: int = Field(0, ge=0, le=50)
    headers: dict[str, str] | None = None


class ScrapeReq(BaseModel):
    website_url: str
    render_heavy_js: RenderJS = "auto"
    number_of_scrolls: int = Field(0, ge=0, le=50)
    headers: dict[str, str] | None = None


class SitemapReq(BaseModel):
    website_url: str
    max_urls: int = Field(5000, ge=1, le=50000)


class CrawlReq(BaseModel):
    url: str
    prompt: str | None = None
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
    url: str
    steps: list[str] = Field(..., min_length=1, max_length=30)
    user_prompt: str | None = None
    output_schema: dict | None = None
    ai_extraction: bool = True
    headers: dict[str, str] | None = None


# ---------------- Runner ----------------

KNOWN_ERRORS = (ServiceError, fetcher.FetchError, LLMError, SearchError, ValueError)


async def run_sync(service: str, params: dict, coro_factory) -> dict:
    """Run a service, store it in history, and return a ScrapeGraph-style response."""
    rid = db.create(service, _redact(params))
    started = time.time()
    try:
        out = await coro_factory()
    except KNOWN_ERRORS as e:
        db.finish(rid, "failed", error=str(e))
        raise HTTPException(422, {"request_id": rid, "status": "failed", "error": str(e)})
    except Exception as e:  # unexpected: log with traceback
        log.exception("%s failed", service)
        db.finish(rid, "failed", error=f"Internal error: {e}")
        raise HTTPException(500, {"request_id": rid, "status": "failed", "error": f"Internal error: {e}"})
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


# ---------------- Endpoints ----------------

@app.post("/v1/smartscraper", dependencies=auth, tags=["scrape"])
async def smartscraper(req: SmartScraperReq):
    """Extract structured data from one page (or a few paginated pages) using a prompt."""
    return await run_sync("smartscraper", req.model_dump(), lambda: scrapers.smartscraper(**req.model_dump()))


@app.post("/v1/searchscraper", dependencies=auth, tags=["scrape"])
async def searchscraper(req: SearchScraperReq):
    """Search the web, read the top results, and answer the prompt with sources."""
    return await run_sync("searchscraper", req.model_dump(), lambda: scrapers.searchscraper(**req.model_dump()))


@app.post("/v1/markdownify", dependencies=auth, tags=["convert"])
async def markdownify(req: MarkdownifyReq):
    """Convert a web page to clean Markdown (no AI call)."""
    return await run_sync("markdownify", req.model_dump(), lambda: scrapers.markdownify(**req.model_dump()))


@app.post("/v1/scrape", dependencies=auth, tags=["convert"])
async def scrape(req: ScrapeReq):
    """Return the raw (optionally JavaScript-rendered) HTML of a page (no AI call)."""
    return await run_sync("scrape", req.model_dump(), lambda: scrapers.scrape(**req.model_dump()))


@app.post("/v1/sitemap", dependencies=auth, tags=["convert"])
async def sitemap(req: SitemapReq):
    """List every URL from a site's sitemap(s)."""
    return await run_sync("sitemap", req.model_dump(), lambda: sitemap_svc.sitemap(**req.model_dump()))


@app.post("/v1/agentic-scrapper", dependencies=auth, tags=["scrape"])
@app.post("/v1/agentic-scraper", dependencies=auth, tags=["scrape"], include_in_schema=False)
async def agentic(req: AgenticReq):
    """Perform plain-English browser steps (log in, search, click...), then extract."""
    return await run_sync("agentic", req.model_dump(), lambda: agentic_svc.agentic(**req.model_dump()))


@app.post("/v1/crawl", dependencies=auth, tags=["crawl"], status_code=202)
async def start_crawl(req: CrawlReq):
    """Start a multi-page crawl in the background. Poll GET /v1/crawl/{request_id}."""
    params = req.model_dump(by_alias=True)
    rid = db.create("crawl", params)

    async def job():
        def progress(p: dict) -> None:
            db.update_progress(rid, {"progress": p})

        try:
            result, usage = await crawler.crawl(
                url=req.url, prompt=req.prompt, schema=req.schema_, extraction_mode=req.extraction_mode,
                depth=req.depth, max_pages=req.max_pages, same_domain_only=req.same_domain_only,
                use_sitemap=req.sitemap, include_paths=req.include_paths, exclude_paths=req.exclude_paths,
                respect_robots=req.respect_robots, render_heavy_js=req.render_heavy_js, on_progress=progress,
            )
            db.finish(rid, "completed", result=result, usage=usage.as_dict())
        except KNOWN_ERRORS as e:
            db.finish(rid, "failed", error=str(e))
        except Exception as e:
            log.exception("crawl failed")
            db.finish(rid, "failed", error=f"Internal error: {e}")

    task = asyncio.create_task(job())
    _jobs.add(task)
    task.add_done_callback(_jobs.discard)
    return {"request_id": rid, "status": "processing"}


@app.get("/v1/crawl/{rid}", dependencies=auth, tags=["crawl"])
async def get_crawl(rid: str):
    return _get_or_404(rid)


@app.get("/v1/requests", dependencies=auth, tags=["history"])
async def list_requests(limit: int = 50, service: str | None = None):
    return {"requests": db.list_recent(min(limit, 500), service)}


@app.get("/v1/requests/{rid}", dependencies=auth, tags=["history"])
@app.get("/v1/smartscraper/{rid}", dependencies=auth, include_in_schema=False)
@app.get("/v1/searchscraper/{rid}", dependencies=auth, include_in_schema=False)
@app.get("/v1/markdownify/{rid}", dependencies=auth, include_in_schema=False)
async def get_request(rid: str):
    return _get_or_404(rid)


@app.delete("/v1/requests/{rid}", dependencies=auth, tags=["history"])
async def delete_request(rid: str):
    if not db.delete(rid):
        raise HTTPException(404, "Request not found")
    return {"deleted": rid}


@app.get("/v1/status", dependencies=auth, tags=["system"])
async def status():
    return {"llm": provider_status(), "browser_enabled": settings.browser_enabled,
            "search_provider": settings.search_provider, "stats": db.stats()}


@app.get("/health", tags=["system"])
async def health():
    return {"ok": True}


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


def _get_or_404(rid: str) -> dict:
    row = db.get(rid)
    if not row:
        raise HTTPException(404, "Request not found")
    return row


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    body: Any = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
    return JSONResponse(body, status_code=exc.status_code)
