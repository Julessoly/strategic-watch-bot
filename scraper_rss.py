"""
RSS scraper — competitor blogs and research sources.
Fetches articles from the last 24h, extracts full content via trafilatura.
Inserts into Turso DB with full source metadata.
"""

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
# Only confirmed RSS-native feeds here.
# Sources without RSS go in scraper_web.py.

RSS_FEEDS = [
    # CEX — RSS natif confirmed
    {"url": "https://www.coinbase.com/blog/feed",  "name": "Coinbase",  "category": "cex"},
    {"url": "https://blog.kraken.com/feed",        "name": "Kraken",    "category": "cex"},
    {"url": "https://blog.bitfinex.com/feed",      "name": "Bitfinex",  "category": "cex"},
    {"url": "https://blog.bitstamp.net/feed",      "name": "Bitstamp",  "category": "cex"},
    {"url": "https://blog.bitmex.com/feed/",       "name": "BitMEX",    "category": "cex"},
    # General news — RSS natif confirmed
    {"url": "https://www.theblock.co/rss.xml",     "name": "The Block",   "category": "news"},
    {"url": "https://blockworks.co/feed",          "name": "Blockworks",  "category": "news"},
    {"url": "https://www.dlnews.com/feed",         "name": "DL News",     "category": "news"},
    {"url": "https://cointelegraph.com/rss",       "name": "Cointelegraph","category": "news"},
    # Research / Innovation
    {"url": "https://a16zcrypto.com/feed",         "name": "a16z Crypto", "category": "research"},
    {"url": "https://paradigm.xyz/feed",           "name": "Paradigm",    "category": "research"},
    {"url": "https://multicoin.capital/feed/",     "name": "Multicoin",   "category": "research"},
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


async def _fetch_content(session: aiohttp.ClientSession, url: str, max_chars: int = 4000) -> Optional[str]:
    """Fetch full article text via trafilatura. Returns None if blocked or failed."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.debug(f"HTTP {resp.status} for {url}")
                return None
            html = await resp.text()
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
            favor_recall=True,
        )
        return text[:max_chars] if text else None
    except Exception as e:
        logger.debug(f"Content fetch failed for {url}: {e}")
        return None


# ─── Per-feed scraper ─────────────────────────────────────────────────────────

async def scrape_feed(session: aiohttp.ClientSession, feed: dict, cutoff: datetime) -> tuple[int, int]:
    """Scrape one RSS feed. Returns (new, skipped)."""
    url      = feed["url"]
    name     = feed["name"]
    category = feed["category"]

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            raw = await resp.text()
        parsed = feedparser.parse(raw)
    except Exception as e:
        logger.error(f"RSS fetch error [{name}]: {e}")
        return 0, 0

    if not parsed.entries:
        logger.warning(f"[{name}] No entries found in feed")
        return 0, 0

    new_count = skip_count = 0

    for entry in parsed.entries:
        if not _is_recent(entry, cutoff):
            continue

        article_url = entry.get("link", "").strip()
        title       = entry.get("title", "").strip()
        rss_summary = entry.get("summary", "") or entry.get("description", "")

        if not article_url:
            continue

        # Try full article content, fallback to RSS title + summary
        content = await _fetch_content(session, article_url)
        if not content:
            content = f"{title}\n\n{rss_summary}"[:2000]
        if not content.strip():
            continue

        row_id = insert_entry(
            source_type="rss",
            source_category=category,
            source_name=name,
            source_url=article_url,
            author=name,
            title=title,
            content=content,
            published_at=_parse_date(entry),
        )
        if row_id:
            new_count += 1
        else:
            skip_count += 1

    logger.info(f"[{name}] new={new_count} skipped={skip_count}")
    return new_count, skip_count


# ─── Main job ─────────────────────────────────────────────────────────────────

async def scrape_rss_feeds() -> dict:
    """Scrape all RSS feeds. Returns {new, skipped, errors}."""
    run_id    = log_scrape_start("rss")
    cutoff    = _cutoff()
    new_total = skip_total = 0
    errors    = []

    async with aiohttp.ClientSession() as session:
        tasks   = [scrape_feed(session, feed, cutoff) for feed in RSS_FEEDS]
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
