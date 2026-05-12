"""
Database layer — Turso (libSQL) for persistent storage across deployments.
Single table 'entries' covering all source types: twitter, rss, scraping, api.
AI is used only for /digest and /ask — no scoring or enrichment stored here.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import libsql_experimental as libsql

logger = logging.getLogger(__name__)

TURSO_URL  = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "")


def get_conn():
    if TURSO_URL and TURSO_TOKEN:
        conn = libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)
    else:
        # Local fallback for development
        os.makedirs("data", exist_ok=True)
        conn = libsql.connect("data/watch.db")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,

            -- Source metadata
            source_type          TEXT NOT NULL,  -- 'twitter' | 'rss' | 'scraping' | 'api'
            source_category      TEXT,           -- 'cex' | 'institutional' | 'otc' | 'stablecoins' | 'prediction' | 'tradfi' | 'research' | 'news' | 'fundraising'
            source_name          TEXT,           -- 'Coinbase' | 'Kraken' | 'The Block' | 'DeFiLlama' etc
            source_url           TEXT NOT NULL UNIQUE,  -- dedup key

            -- Content
            author               TEXT,           -- Twitter handle or company name
            title                TEXT,           -- Article title (rss/scraping)
            content              TEXT NOT NULL,  -- Full text content
            published_at         TEXT,           -- ISO8601 from source
            ingested_at          TEXT NOT NULL,  -- ISO8601 when we picked it up

            -- Fundraising specific columns (NULL for non-fundraising entries)
            raise_amount_usd     REAL,
            raise_round          TEXT,           -- 'Seed' | 'Series A' | 'Series B' | 'Strategic' etc
            raise_category       TEXT,           -- 'exchange' | 'defi' | 'infrastructure' | 'payments' | 'ai' etc
            raise_description    TEXT,
            raise_lead_investor  TEXT,
            raise_other_investors TEXT,          -- JSON array
            raise_valuation_usd  REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scrape_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type  TEXT NOT NULL,
            started_at   TEXT NOT NULL,
            finished_at  TEXT,
            new_entries  INTEGER DEFAULT 0,
            errors       TEXT
        )
    """)
    conn.commit()
    logger.info("Database initialized")


# ─── Write ────────────────────────────────────────────────────────────────────

def insert_entry(
    source_type: str,
    source_url: str,
    content: str,
    source_category: Optional[str] = None,
    source_name: Optional[str] = None,
    author: Optional[str] = None,
    title: Optional[str] = None,
    published_at: Optional[str] = None,
    # Fundraising fields
    raise_amount_usd: Optional[float] = None,
    raise_round: Optional[str] = None,
    raise_category: Optional[str] = None,
    raise_description: Optional[str] = None,
    raise_lead_investor: Optional[str] = None,
    raise_other_investors: Optional[list] = None,
    raise_valuation_usd: Optional[float] = None,
) -> Optional[int]:
    """Insert a raw entry. Returns new row id, or None if already exists (dedup)."""
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    try:
        cursor = conn.execute("""
            INSERT INTO entries (
                source_type, source_category, source_name, source_url,
                author, title, content, published_at, ingested_at,
                raise_amount_usd, raise_round, raise_category, raise_description,
                raise_lead_investor, raise_other_investors, raise_valuation_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            source_type, source_category, source_name, source_url,
            author, title, content, published_at, now,
            raise_amount_usd, raise_round, raise_category, raise_description,
            raise_lead_investor,
            json.dumps(raise_other_investors) if raise_other_investors else None,
            raise_valuation_usd,
        ))
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            return None  # already ingested
        logger.error(f"insert_entry error: {e}")
        return None


# ─── Read ─────────────────────────────────────────────────────────────────────

def get_recent_entries(hours: int = 24, limit: int = 200, source_category: Optional[str] = None) -> list[dict]:
    """Get recent entries for digest generation."""
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    conn = get_conn()
    if source_category:
        rows = conn.execute("""
            SELECT * FROM entries
            WHERE ingested_at >= ? AND source_category = ?
            ORDER BY ingested_at DESC LIMIT ?
        """, (cutoff, source_category, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT * FROM entries
            WHERE ingested_at >= ?
            ORDER BY ingested_at DESC LIMIT ?
        """, (cutoff, limit)).fetchall()
    return [_row_to_dict(row) for row in rows]


def search_entries(query: str, limit: int = 20) -> list[dict]:
    """Simple keyword search across title + content."""
    conn = get_conn()
    like = f"%{query}%"
    rows = conn.execute("""
        SELECT * FROM entries
        WHERE content LIKE ? OR title LIKE ? OR author LIKE ?
        ORDER BY ingested_at DESC LIMIT ?
    """, (like, like, like, limit)).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_all_entries(limit: int = 2000) -> list[dict]:
    """All entries for CSV export."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM entries ORDER BY ingested_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_stats() -> dict:
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    by_category = conn.execute(
        "SELECT source_category, COUNT(*) as n FROM entries GROUP BY source_category"
    ).fetchall()
    by_type = conn.execute(
        "SELECT source_type, COUNT(*) as n FROM entries GROUP BY source_type"
    ).fetchall()
    return {
        "total": total,
        "by_category": {row[0] or "unknown": row[1] for row in by_category},
        "by_type": {row[0]: row[1] for row in by_type},
    }


def _row_to_dict(row) -> dict:
    """Convert a libsql row to dict."""
    columns = [
        "id", "source_type", "source_category", "source_name", "source_url",
        "author", "title", "content", "published_at", "ingested_at",
        "raise_amount_usd", "raise_round", "raise_category", "raise_description",
        "raise_lead_investor", "raise_other_investors", "raise_valuation_usd",
    ]
    d = dict(zip(columns, row))
    if d.get("raise_other_investors"):
        try:
            d["raise_other_investors"] = json.loads(d["raise_other_investors"])
        except Exception:
            pass
    return d


# ─── Scrape run log ───────────────────────────────────────────────────────────

def log_scrape_start(source_type: str) -> int:
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    cursor = conn.execute(
        "INSERT INTO scrape_runs (source_type, started_at) VALUES (?, ?)",
        (source_type, now)
    )
    conn.commit()
    return cursor.lastrowid


def log_scrape_finish(run_id: int, new_entries: int, errors: list[str]):
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    conn.execute(
        "UPDATE scrape_runs SET finished_at=?, new_entries=?, errors=? WHERE id=?",
        (now, new_entries, json.dumps(errors), run_id)
    )
    conn.commit()
