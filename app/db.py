"""SQLite history of every request (so the dashboard can show past results)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .config import settings

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _restrict_permissions(path: Path) -> None:
    try:
        os.chmod(path.parent, 0o700)
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                os.chmod(candidate, 0o600)
    except OSError:
        pass


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = Path(settings.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY,
                service TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                finished_at REAL,
                params TEXT,
                result TEXT,
                error TEXT,
                usage TEXT
            )"""
        )
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_req_created ON requests(created_at DESC)")
        _conn.commit()
        _restrict_permissions(path)
    return _conn


def create(service: str, params: dict) -> str:
    rid = str(uuid.uuid4())
    with _lock:
        conn().execute(
            "INSERT INTO requests (id, service, status, created_at, params) VALUES (?,?,?,?,?)",
            (rid, service, "processing", time.time(), json.dumps(params, default=str)),
        )
        conn().commit()
    return rid


def finish(rid: str, status: str, result=None, error: str | None = None, usage: dict | None = None) -> None:
    with _lock:
        conn().execute(
            "UPDATE requests SET status=?, finished_at=?, result=?, error=?, usage=? WHERE id=?",
            (status, time.time(), json.dumps(result, default=str) if result is not None else None,
             error, json.dumps(usage) if usage else None, rid),
        )
        conn().commit()


def update_progress(rid: str, result) -> None:
    with _lock:
        conn().execute("UPDATE requests SET result=? WHERE id=?", (json.dumps(result, default=str), rid))
        conn().commit()


def mark_stale_failed() -> int:
    """Jobs still 'processing' at startup were interrupted by a restart."""
    with _lock:
        cur = conn().execute(
            "UPDATE requests SET status='failed', finished_at=?, error=? WHERE status='processing'",
            (time.time(), "Interrupted by a server restart"),
        )
        conn().commit()
    return cur.rowcount


def purge_older_than(days: int) -> int:
    if days <= 0:
        return 0
    with _lock:
        cur = conn().execute("DELETE FROM requests WHERE created_at < ?", (time.time() - days * 86400,))
        conn().commit()
    return cur.rowcount


def _row(r: sqlite3.Row, full: bool = True) -> dict:
    d = {
        "request_id": r["id"],
        "service": r["service"],
        "status": r["status"],
        "created_at": r["created_at"],
        "finished_at": r["finished_at"],
        "duration_s": round(r["finished_at"] - r["created_at"], 2) if r["finished_at"] else None,
        "error": r["error"],
        "usage": json.loads(r["usage"]) if r["usage"] else None,
        "params": json.loads(r["params"]) if r["params"] else None,
    }
    if full:
        d["result"] = json.loads(r["result"]) if r["result"] else None
    return d


def get(rid: str) -> dict | None:
    r = conn().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    return _row(r) if r else None


def list_recent(limit: int = 50, service: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM requests", []
    if service:
        q += " WHERE service=?"
        args.append(service)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return [_row(r, full=False) for r in conn().execute(q, args).fetchall()]


def stats() -> dict:
    rows = conn().execute(
        "SELECT service, status, COUNT(*) c FROM requests GROUP BY service, status"
    ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["service"], {})[r["status"]] = r["c"]
    return out


def delete(rid: str) -> bool:
    with _lock:
        cur = conn().execute("DELETE FROM requests WHERE id=?", (rid,))
        conn().commit()
    return cur.rowcount > 0
