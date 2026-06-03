"""
Database module - SQLite local on /data/watch.db (Railway persistent volume).
Single table: entries (UNIQUE on source_url for automatic deduplication).

Schema:
- source_type removed
- source_description: "media" or "company" to distinguish news outlets from company blogs
- tags: AI-enriched comma-separated tags (e.g. "partnership,product_launch,regulatory")
  NULL = not yet enriched / DELETE = noise (removed by enrichment step)
"""

import os
import json
import sqlite3
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/watch.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            source_category       TEXT NOT NULL,
            source_description    TEXT,
            source_name           TEXT NOT NULL,
            source_url            TEXT NOT NULL UNIQUE,
            author                TEXT,
            title                 TEXT,
            content               TEXT,
            published_at          TEXT,
            ingested_at           TEXT NOT NULL DEFAULT (datetime('now')),
            tags                  TEXT,
            raise_amount_usd      REAL,
            raise_round           TEXT,
            raise_category        TEXT,
            raise_description     TEXT,
            raise_lead_investor   TEXT,
            raise_other_investors TEXT,
            raise_valuation_usd   REAL
        )
    """)
    # Migrations: add new columns if DB already exists
    for col, definition in [
        ("source_description", "TEXT"),
        ("tags",               "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE entries ADD COLUMN {col} {definition}")
            logger.info(f"Migration: added column {col}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Migration: drop source_type and is_relevant if still present
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(entries)").fetchall()]
        if "source_type" in cols or "is_relevant" in cols:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS entries_new (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_category       TEXT NOT NULL,
                    source_description    TEXT,
                    source_name           TEXT NOT NULL,
                    source_url            TEXT NOT NULL UNIQUE,
                    author                TEXT,
                    title                 TEXT,
                    content               TEXT,
                    published_at          TEXT,
                    ingested_at           TEXT NOT NULL DEFAULT (datetime('now')),
                    tags                  TEXT,
                    raise_amount_usd      REAL,
                    raise_round           TEXT,
                    raise_category        TEXT,
                    raise_description     TEXT,
                    raise_lead_investor   TEXT,
                    raise_other_investors TEXT,
                    raise_valuation_usd   REAL
                )
            """)
            conn.execute("""
                INSERT INTO entries_new (
                    id, source_category, source_name, source_url,
                    author, title, content, published_at, ingested_at,
                    tags, raise_amount_usd, raise_round, raise_category, raise_description,
                    raise_lead_investor, raise_other_investors, raise_valuation_usd
                )
                SELECT
                    id, source_category, source_name, source_url,
                    author, title, content, published_at, ingested_at,
                    NULL,
                    raise_amount_usd, raise_round, raise_category, raise_description,
                    raise_lead_investor, raise_other_investors, raise_valuation_usd
                FROM entries
            """)
            conn.execute("DROP TABLE entries")
            conn.execute("ALTER TABLE entries_new RENAME TO entries")
            logger.info("Migration: removed source_type and is_relevant columns")
    except Exception as e:
        logger.error(f"Migration error: {e}")

    conn.commit()
    conn.close()
    logger.info(f"DB initialized at {DB_PATH}")


def insert_entry(
    source_category: str,
    source_name: str,
    source_url: str,
    source_description: Optional[str] = None,
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
    conn = get_conn()
    try:
        cursor = conn.execute(
            """
            INSERT INTO entries (
                source_category, source_description, source_name, source_url,
                author, title, content, published_at, ingested_at,
                raise_amount_usd, raise_round, raise_category, raise_description,
                raise_lead_investor, raise_other_investors, raise_valuation_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_category, source_description, source_name, source_url,
                author, title, content, published_at,
                raise_amount_usd, raise_round, raise_category, raise_description,
                raise_lead_investor,
                json.dumps(raise_other_investors) if raise_other_investors else None,
                raise_valuation_usd,
            )
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None  # Duplicate URL - silently skip
    except Exception as e:
        logger.error(f"DB insert error [{source_url}]: {e}")
        return None
    finally:
        conn.close()


def reset_untagged() -> int:
    """Reset entries with tags='untagged' back to NULL so they get re-processed."""
    conn = get_conn()
    try:
        cursor = conn.execute("UPDATE entries SET tags = NULL WHERE tags = 'untagged'")
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def update_tags(entry_id: int, tags: str) -> bool:
    """Set AI-enriched tags on a relevant entry."""
    conn = get_conn()
    try:
        conn.execute("UPDATE entries SET tags = ? WHERE id = ?", (tags, entry_id))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"DB update_tags error [{entry_id}]: {e}")
        return False
    finally:
        conn.close()


def delete_entry(entry_id: int) -> bool:
    """Delete a noise entry identified by AI enrichment."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"DB delete error [{entry_id}]: {e}")
        return False
    finally:
        conn.close()


def get_unenriched_entries(limit: int = 100) -> list[dict]:
    """Return entries that haven't been through AI enrichment yet (tags IS NULL)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM entries WHERE tags IS NULL ORDER BY ingested_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_recent_entries_by_published(hours: int = 24, limit: int = 150) -> list[dict]:
    """Return active entries for the daily digest."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT * FROM entries 
               WHERE published_at >= datetime('now', ?)
               AND tags IS NOT NULL
               AND tags NOT IN ('noise', 'duplicate', 'untagged')
               ORDER BY published_at DESC LIMIT ?""",
            (f"-{hours} hours", limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_recent_entries(hours: int = 24, limit: int = 200, source_category: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    try:
        filters = ["ingested_at >= datetime('now', ?)"]
        params = [f"-{hours} hours"]
        if source_category:
            filters.append("source_category = ?")
            params.append(source_category)
        where = " AND ".join(filters)
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM entries WHERE {where} ORDER BY ingested_at DESC LIMIT ?",
            params
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_entries(query: str, limit: int = 20) -> list[dict]:
    conn = get_conn()
    try:
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT * FROM entries WHERE title LIKE ? OR content LIKE ? ORDER BY ingested_at DESC LIMIT ?",
            (like, like, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_all_entries(limit: int = 2000) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM entries ORDER BY ingested_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_stats() -> dict:
    conn = get_conn()
    try:
        total       = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        by_category = conn.execute("SELECT source_category, COUNT(*) FROM entries GROUP BY source_category").fetchall()
        enriched    = conn.execute("SELECT COUNT(*) FROM entries WHERE tags IS NOT NULL").fetchone()[0]
        last        = conn.execute("SELECT ingested_at FROM entries ORDER BY ingested_at DESC LIMIT 1").fetchone()
        return {
            "total":         total,
            "by_category":   {row[0] or "unknown": row[1] for row in by_category},
            "enriched":      enriched,
            "pending":       total - enriched,
            "last_ingested": (last[0] or "")[:19] if last else "N/A",
        }
    finally:
        conn.close()


def get_last_ingested_per_source() -> dict:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT source_name, MAX(ingested_at) FROM entries GROUP BY source_name"
        ).fetchall()
        return {row[0]: row[1] for row in rows}
    finally:
        conn.close()


def get_digest_stats() -> dict:
    """Calculate read, noise, and duplicate stats for the last 24h."""
    conn = get_conn()
    try:
        # Total read in the last 24h based on ingestion
        read = conn.execute("SELECT COUNT(*) FROM entries WHERE ingested_at >= datetime('now', '-24 hours')").fetchone()[0]
        # Noise filtered today
        noise = conn.execute("SELECT COUNT(*) FROM entries WHERE ingested_at >= datetime('now', '-24 hours') AND tags = 'noise'").fetchone()[0]
        # Duplicates flagged today (compared to the last 48h)
        duplicates = conn.execute("SELECT COUNT(*) FROM entries WHERE ingested_at >= datetime('now', '-24 hours') AND tags = 'duplicate'").fetchone()[0]
        
        return {"read": read, "noise": noise, "duplicates": duplicates}
    finally:
        conn.close()