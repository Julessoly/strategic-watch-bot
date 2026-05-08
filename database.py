"""
Database layer — SQLite with FTS5
All scraped + enriched entries live here.
"""

import sqlite3
import json
import os
from datetime import datetime
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "data/watch.db")


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS entries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,

            -- Scraping fields
            source_type     TEXT NOT NULL,          -- 'twitter', 'rss'
            source_url      TEXT NOT NULL UNIQUE,   -- dedup key
            author          TEXT,                   -- @handle or site name
            content         TEXT NOT NULL,          -- raw text
            published_at    TEXT,                   -- ISO8601, from source
            ingested_at     TEXT NOT NULL,          -- ISO8601, when we picked it up

            -- AI enrichment fields (NULL until processed)
            tags            TEXT,                   -- JSON array of strings
            relevance_score REAL,                   -- 0.0 – 1.0
            summary         TEXT,                   -- 1-2 sentence summary
            ai_cost_tokens  INTEGER,                -- total tokens used for this entry
            enriched_at     TEXT,                   -- ISO8601

            -- Filtering
            kept            INTEGER DEFAULT NULL    -- 1=kept, 0=filtered out, NULL=pending
        );

        -- Full-text search index on content + summary
        CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
            content,
            summary,
            author,
            tags,
            content='entries',
            content_rowid='id'
        );

        -- Triggers to keep FTS in sync
        CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
            INSERT INTO entries_fts(rowid, content, summary, author, tags)
            VALUES (new.id, new.content, new.summary, new.author, new.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
            INSERT INTO entries_fts(entries_fts, rowid, content, summary, author, tags)
            VALUES ('delete', old.id, old.content, old.summary, old.author, old.tags);
            INSERT INTO entries_fts(rowid, content, summary, author, tags)
            VALUES (new.id, new.content, new.summary, new.author, new.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
            INSERT INTO entries_fts(entries_fts, rowid, content, summary, author, tags)
            VALUES ('delete', old.id, old.content, old.summary, old.author, old.tags);
        END;

        -- Scrape run log
        CREATE TABLE IF NOT EXISTS scrape_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type     TEXT NOT NULL,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            new_entries     INTEGER DEFAULT 0,
            errors          TEXT            -- JSON list of error strings
        );
        """)


# ─── Write ─────────────────────────────────────────────────────────────────────

def insert_entry(
    source_type: str,
    source_url: str,
    author: str,
    content: str,
    published_at: Optional[str] = None,
) -> Optional[int]:
    """
    Insert a raw entry. Returns the new row id, or None if already exists (dedup).
    """
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO entries
                    (source_type, source_url, author, content, published_at, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (source_type, source_url, author, content, published_at, now),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None  # already ingested


def update_enrichment(
    entry_id: int,
    tags: list[str],
    relevance_score: float,
    summary: str,
    ai_cost_tokens: int,
    relevance_threshold: float = 0.3,
):
    """Write AI enrichment results back. Sets kept=1 if score >= threshold."""
    now = datetime.utcnow().isoformat()
    kept = 1 if relevance_score >= relevance_threshold else 0
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE entries
            SET tags=?, relevance_score=?, summary=?, ai_cost_tokens=?,
                enriched_at=?, kept=?
            WHERE id=?
            """,
            (json.dumps(tags), relevance_score, summary, ai_cost_tokens, now, kept, entry_id),
        )


# ─── Read ───────────────────────────────────────────────────────────────────────

def get_pending_enrichment(limit: int = 50) -> list[dict]:
    """Entries not yet enriched by AI."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM entries WHERE enriched_at IS NULL ORDER BY ingested_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_kept(hours: int = 24, limit: int = 100) -> list[dict]:
    """Kept entries from the last N hours, for digest."""
    cutoff = datetime.utcnow()
    from datetime import timedelta
    cutoff = (cutoff.replace(microsecond=0) - timedelta(hours=hours)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM entries
            WHERE kept=1 AND ingested_at >= ?
            ORDER BY relevance_score DESC, ingested_at DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def search_entries(query: str, limit: int = 20) -> list[dict]:
    """Full-text search across content + summary + tags."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT e.* FROM entries e
            JOIN entries_fts f ON f.rowid = e.id
            WHERE entries_fts MATCH ? AND e.kept=1
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        kept = conn.execute("SELECT COUNT(*) FROM entries WHERE kept=1").fetchone()[0]
        pending = conn.execute("SELECT COUNT(*) FROM entries WHERE enriched_at IS NULL").fetchone()[0]
        filtered = conn.execute("SELECT COUNT(*) FROM entries WHERE kept=0").fetchone()[0]
        total_tokens = conn.execute("SELECT SUM(ai_cost_tokens) FROM entries").fetchone()[0] or 0
        by_source = conn.execute(
            "SELECT source_type, COUNT(*) as n FROM entries GROUP BY source_type"
        ).fetchall()
    return {
        "total": total,
        "kept": kept,
        "pending_enrichment": pending,
        "filtered_out": filtered,
        "total_tokens_used": total_tokens,
        "by_source": {r["source_type"]: r["n"] for r in by_source},
    }


# ─── Scrape run log ─────────────────────────────────────────────────────────────

def log_scrape_start(source_type: str) -> int:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO scrape_runs (source_type, started_at) VALUES (?, ?)",
            (source_type, now),
        )
        return cur.lastrowid


def log_scrape_finish(run_id: int, new_entries: int, errors: list[str]):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE scrape_runs SET finished_at=?, new_entries=?, errors=? WHERE id=?",
            (now, new_entries, json.dumps(errors), run_id),
        )

def get_all_entries(limit: int = 2000) -> list[dict]:
    """All entries ordered by ingested_at desc — for CSV export."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM entries ORDER BY ingested_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
