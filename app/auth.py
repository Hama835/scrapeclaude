"""API-key and dashboard-session authentication."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time

from fastapi import HTTPException, Request, Response

from .config import MIN_API_KEY_LENGTH, settings

log = logging.getLogger("scrapeclaude.auth")

SESSION_COOKIE = "sc_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PBKDF2_ITERATIONS = 600_000

_sessions: dict[str, float] = {}
_failures: dict[str, list[float]] = {}


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256:%d:%s:%s" % (
        iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split(":")
        if algo != "pbkdf2_sha256":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_login_enabled() -> bool:
    return bool(settings.dashboard_email and settings.dashboard_password_hash)


def credentials_match(email: str, password: str) -> bool:
    if not password_login_enabled():
        return False
    email_ok = hmac.compare_digest(email.strip().lower().encode(), settings.dashboard_email.encode())
    return verify_password(password, settings.dashboard_password_hash) and email_ok


def validate_startup() -> None:
    if bool(settings.dashboard_email) != bool(settings.dashboard_password_hash):
        raise RuntimeError("Set both DASHBOARD_EMAIL and DASHBOARD_PASSWORD_HASH (python -m app.hashpw), or neither.")
    if settings.dashboard_password_hash and not settings.dashboard_password_hash.startswith("pbkdf2_sha256:"):
        raise RuntimeError("DASHBOARD_PASSWORD_HASH is not a hash. Generate it with: python -m app.hashpw")
    if not password_login_enabled():
        log.warning("DASHBOARD_EMAIL/DASHBOARD_PASSWORD_HASH not set: the dashboard signs in with the API key.")
    if not settings.api_keys:
        if settings.allow_no_auth:
            log.warning("SC_API_KEY is empty and ALLOW_NO_AUTH=true: the API is open to anyone who can reach it.")
            return
        raise RuntimeError(
            "SC_API_KEY is empty. Set it (openssl rand -hex 32) or set ALLOW_NO_AUTH=true for local experiments."
        )
    short = [k for k in settings.api_keys if len(k) < MIN_API_KEY_LENGTH]
    if short:
        raise RuntimeError(f"SC_API_KEY entries must be at least {MIN_API_KEY_LENGTH} characters long.")


def key_matches(supplied: str) -> bool:
    candidate = supplied.encode("utf-8", errors="replace")
    return any(hmac.compare_digest(candidate, k.encode()) for k in settings.api_keys)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _prune(ip: str, now: float) -> list[float]:
    recent = [t for t in _failures.get(ip, []) if now - t < settings.auth_fail_window_s]
    if recent:
        _failures[ip] = recent
    else:
        _failures.pop(ip, None)
    return recent


def locked_out(ip: str) -> bool:
    return len(_prune(ip, time.time())) >= settings.auth_fail_limit


def record_failure(request: Request, reason: str) -> None:
    ip = client_ip(request)
    now = time.time()
    _prune(ip, now)
    _failures.setdefault(ip, []).append(now)
    log.warning("auth failed: %s ip=%s method=%s path=%s", reason, ip, request.method, request.url.path)


def reset_failures() -> None:
    _failures.clear()


def extract_key(request: Request) -> str | None:
    supplied = request.headers.get("x-api-key") or request.headers.get("sgai-apikey")
    if supplied:
        return supplied
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def create_session() -> str:
    now = time.time()
    for sid, exp in list(_sessions.items()):
        if exp <= now:
            _sessions.pop(sid, None)
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = now + settings.session_ttl_s
    return sid


def destroy_session(sid: str | None) -> None:
    if sid:
        _sessions.pop(sid, None)


def session_valid(request: Request) -> bool:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return False
    exp = _sessions.get(sid)
    if exp is None:
        return False
    if exp <= time.time():
        _sessions.pop(sid, None)
        return False
    return True


def set_session_cookie(response: Response, request: Request, sid: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, sid, max_age=settings.session_ttl_s, httponly=True, samesite="strict",
        secure=request.url.scheme == "https", path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def ensure_not_locked_out(request: Request) -> None:
    if locked_out(client_ip(request)):
        raise HTTPException(429, "Too many failed authentication attempts. Try again later.")


async def require_key(request: Request) -> None:
    if not settings.api_keys:
        return
    ensure_not_locked_out(request)
    key = extract_key(request)
    if key is not None:
        if key_matches(key):
            return
        record_failure(request, "invalid api key")
        raise HTTPException(401, "Invalid API key (send header X-API-Key).")
    if session_valid(request):
        if request.method in SAFE_METHODS or request.headers.get("x-requested-with"):
            return
        raise HTTPException(403, "Session requests must include the X-Requested-With header.")
    record_failure(request, "no credentials")
    raise HTTPException(401, "Missing API key (send header X-API-Key) or log in to the dashboard.")
