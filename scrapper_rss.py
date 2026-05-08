"""
RSS scraper — competitor blogs with full article content extraction.
Fetches articles from the last 24h, extracts full content via trafilatura,
inserts into the same DB as Twitter entries (source_type='rss').
"""

import os
import json
import logging
import asyncio
import aiohttp
import feedparser
import trafilatura
from datetime import datetime, timezone, timedelta
from typing import Optional

from database import insert_entry, log_scrape_start, log_scrape_finish

logger = logging.getLogger(__name__)

# ─── Feed registry ────────────────────────────────────────────────────────────

RSS_FEEDS = [
    # CEX competitors
    {"url": "https://www.coinbase.com/blog/feed",   "name": "Coinbase",  "category": "competitor", "tags": ["coinbase", "competitor", "exchange"]},
    {"url": "https://blog.kraken.com/feed",         "name": "Kraken",    "category": "competitor", "tags": ["kraken", "competitor", "exchange"]},
    {"url": "https://blog.gemini.com/feed",         "name": "Gemini",    "category": "competitor", "tags": ["gemini", "competitor", "exchange"]},
    {"url": "https://blog.bitfinex.com/feed",       "name": "Bitfinex",  "category": "competitor", "tags": ["bitfinex", "competitor", "exchange"]},
    {"url": "https://blog.bitstamp.net/feed",       "name": "Bitstamp",  "category": "competitor", "tags": ["bitstamp", "competitor", "exchange"]},
    {"url": "https://blog.bitmex.com/feed/",        "name": "BitMEX",    "category": "competitor", "tags": ["bitmex", "competitor", "derivatives"]},
    # Custody / Prime Brokerage
    {"url": "https://blog.bitgo.com/feed",          "name": "BitGo",     "category": "competitor", "tags": ["bitgo", "custody", "institutional"]},
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _cutoff() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=24)


def _parse_date(entry) -> Optional[str]:
    import time as _time
    for field in ("published_parsed", "updated_parsed"):
        val = getattr(entry, field, None)
        if val:
            try:
                return datetime.fromtimestamp(_time.mktime(val), tz=timezone.utc).isoformat()
            except Exception:
                pass
    return None


def _is_recent(entry, cutoff: datetime) -> bool:
    import time as _time
    for field in ("published_parsed", "updated_parsed"):
        val = getattr(entry, field, None)
        if val:
            try:
                pub = datetime.fromtimestamp(_time.mktime(val), tz=timezone.utc)
                return pub >= cutoff
            except Exception:
                pass
    return True  # no date → include by default


async def _fetch_content(session: aiohttp.ClientSession, url: str, max_chars: int = 3000) -> Optional[str]:
    """Fetch full article text via trafilatura. Falls back to None if blocked."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; StrategicWatchBot/1.0)"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
        text = trafilatura.extract(html, include_comments=False, include_tables=False)
        return text[:max_chars] if text else None
    except Exception as e:
        logger.debug(f"Content fetch failed for {url}: {e}")
        return None


# ─── Per-feed scraper ─────────────────────────────────────────────────────────

async def scrape_feed(session: aiohttp.ClientSession, feed: dict, cutoff: datetime) -> tuple[int, int]:
    """Scrape one RSS feed. Returns (new, skipped)."""
    url  = feed["url"]
    name = feed.get("name", url)

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            raw = await resp.text()
        parsed = feedparser.parse(raw)
    except Exception as e:
        logger.error(f"RSS fetch error [{name}]: {e}")
        return 0, 0

    new_count = skip_count = 0

    for entry in parsed.entries:
        if not _is_recent(entry, cutoff):
            continue

        article_url = entry.get("link", "")
        title       = entry.get("title", "").strip()
        summary     = entry.get("summary", "") or entry.get("description", "")

        if not article_url:
            continue

        # Try full content, fallback to title + summary
        content = await _fetch_content(session, article_url)
        if not content:
            content = f"{title}\n\n{summary}"[:1500]
        if not content.strip():
            continue

        published_at = _parse_date(entry)
        author       = name  # use feed name as author

        row_id = insert_entry(
            source_type="rss",
            source_url=article_url,
            author=author,
            content=content,
            published_at=published_at,
        )
        if row_id:
            new_count += 1
        else:
            skip_count += 1

    logger.info(f"[{name}] new={new_count} skipped={skip_count}")
    return new_count, skip_count


# ─── Job principal ────────────────────────────────────────────────────────────

async def scrape_rss_feeds() -> dict:
    """
    Scrape all RSS feeds from RSS_FEEDS.
    Returns {new, skipped, errors}.
    """
    run_id = log_scrape_start("rss")
    cutoff = _cutoff()
    new_total = skip_total = 0
    errors = []

    async with aiohttp.ClientSession() as session:
        tasks = [scrape_feed(session, feed, cutoff) for feed in RSS_FEEDS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for feed, result in zip(RSS_FEEDS, results):
        if isinstance(result, Exception):
            msg = f"{feed['name']}: {result}"
            logger.error(msg)
            errors.append(msg)
        else:
            new, skip = result
            new_total  += new
            skip_total += skip

    log_scrape_finish(run_id, new_total, errors)
    logger.info(
        f"RSS scrape done — {len(RSS_FEEDS)} feeds · "
        f"new={new_total} skipped={skip_total} errors={len(errors)}"
    )
    return {"new": new_total, "skipped": skip_total, "errors": errors}
