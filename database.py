"""
Database module - Turso (libSQL) connection and all DB operations.
Single table: entries (UNIQUE on source_url for automatic deduplication).
"""

import os
import json
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN   = os.environ.get("TURSO_AUTH_TOKEN", "")


def get_conn():
    import libsql_experimental as libsql
    return libsql.connect(database=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type          TEXT NOT NULL,
            source_category      TEXT NOT NULL,
            source_name          TEXT NOT NULL,
            source_url           TEXT NOT NULL UNIQUE,
            author               TEXT,
            title                TEXT,
            content              TEXT,
            published_at         TEXT,
            ingested_at          TEXT NOT NULL DEFAULT (datetime('now')),
            raise_amount_usd     REAL,
            raise_round          TEXT,
            raise_category       TEXT,
            raise_description    TEXT,
            raise_lead_investor  TEXT,
            raise_other_investors TEXT,
            raise_valuation_usd  REAL
        )
    """)

    conn.commit()
    logger.info("DB initialized")


def insert_entry(
    source_type: str,
    source_category: str,
    source_name: str,
    source_url: str,
    author: str = "",
    title: str = "",
    content: str = "",
    published_at: Optional[str] = None,
    raise_amount_usd: Optional[float] = None,
    raise_round: Optional[str] = None,
    raise_category: Optional[str] = None,
    raise_description: Optional[str] = None,
    raise_lead_investor: Optional[str] = None,
    raise_other_investors: Optional[list] = None,
    raise_valuation_usd: Optional[float] = None,
) -> Optional[int]:
    """
    Insert a new entry. Returns row ID if inserted, None if duplicate (URL already exists).
    """
    conn = get_conn()
    try:
        cursor = conn.execute(
            """
            INSERT INTO entries (
                source_type, source_category, source_name, source_url,
                author, title, content, published_at, ingested_at,
                raise_amount_usd, raise_round, raise_category, raise_description,
                raise_lead_investor, raise_other_investors, raise_valuation_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_type, source_category, source_name, source_url,
                author, title, content, published_at,
                raise_amount_usd, raise_round, raise_category, raise_description,
                raise_lead_investor,
                json.dumps(raise_other_investors) if raise_other_investors else None,
                raise_valuation_usd,
            )
        )
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        if "UNIQUE constraint failed" in str(e):
            return None  # Duplicate - silently skip
        logger.error(f"DB insert error [{source_url}]: {e}")
        return None


def get_recent_entries(hours: int = 24, limit: int = 200, source_category: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    if source_category:
        rows = conn.execute(
            "SELECT * FROM entries WHERE ingested_at >= datetime('now', ?) AND source_category = ? ORDER BY ingested_at DESC LIMIT ?",
            (f"-{hours} hours", source_category, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM entries WHERE ingested_at >= datetime('now', ?) ORDER BY ingested_at DESC LIMIT ?",
            (f"-{hours} hours", limit)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def search_entries(query: str, limit: int = 20) -> list[dict]:
    conn = get_conn()
    like = f"%{query}%"
    rows = conn.execute(
        "SELECT * FROM entries WHERE title LIKE ? OR content LIKE ? ORDER BY ingested_at DESC LIMIT ?",
        (like, like, limit)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_all_entries(limit: int = 2000) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM entries ORDER BY ingested_at DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_stats() -> dict:
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    by_category = conn.execute("SELECT source_category, COUNT(*) FROM entries GROUP BY source_category").fetchall()
    by_type     = conn.execute("SELECT source_type, COUNT(*) FROM entries GROUP BY source_type").fetchall()
    last        = conn.execute("SELECT ingested_at FROM entries ORDER BY ingested_at DESC LIMIT 1").fetchone()
    return {
        "total":        total,
        "by_category":  {row[0] or "unknown": row[1] for row in by_category},
        "by_type":      {row[0]: row[1] for row in by_type},
        "last_ingested": (last[0] or "")[:19] if last else "N/A",
    }


def get_last_ingested_per_source() -> dict:
    """
    Returns {source_name: last_ingested_at} for all sources.
    Used by the health check to detect broken sources.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT source_name, MAX(ingested_at) FROM entries GROUP BY source_name"
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _row_to_dict(row) -> dict:
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
