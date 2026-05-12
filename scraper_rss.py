"""
RSS scraper — 9 confirmed and tested sources.
"""

import logging
import asyncio
import aiohttp
import feedparser
import trafilatura
from datetime import datetime, timezone, timedelta
from typing import Optional

from database import insert_entry

logger = logging.getLogger(__name__)

RSS_FEEDS = [
    # CEX
    {"url": "https://blog.kraken.com/feed",              "name": "Kraken",        "category": "cex"},
    {"url": "https://blog.bitfinex.com/feed",            "name": "Bitfinex",      "category": "cex"},
    {"url": "https://blog.bitmex.com/feed/",             "name": "BitMEX",        "category": "cex"},
    # Institutional
    {"url": "https://www.fireblocks.com/blog/feed",      "name": "Fireblocks",    "category": "institutional"},
    # Research
    {"url": "https://a16zcrypto.substack.com/feed",      "name": "a16z Crypto",   "category": "research"},
    {"url": "https://multicoin.capital/rss.xml",         "name": "Multicoin",     "category": "research"},
    # News
    {"url": "https://www.theblock.co/rss.xml",           "name": "The Block",     "category": "news"},
    {"url": "https://cointelegraph.com/rss",             "name": "Cointelegraph", "category": "news"},
    {"url": "https://blockworks.co/feed",                "name": "Blockworks",    "category": "news"},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
}


def _parse_date(entry) -> Optional[str]:
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        try:
            return datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    return None


async def _fetch_content(session, url: str) -> Optional[str]:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            html = await r.text()
        text = trafilatura.extract(html, favor_recall=True, include_comments=False)
        return text[:4000] if text else None
    except Exception as e:
        logger.debug(f"Content fetch failed [{url}]: {e}")
        return None


async def scrape_feed(session, feed: dict, cutoff: datetime) -> tuple[int, int]:
    name = feed["name"]
    new = skipped = 0

    try:
        async with session.get(feed["url"], headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                logger.warning(f"[{name}] HTTP {r.status}")
                return 0, 0
            raw = await r.text()
    except Exception as e:
        logger.warning(f"[{name}] Feed fetch failed: {e}")
        return 0, 0

    parsed = feedparser.parse(raw)
    if not parsed.entries:
        logger.warning(f"[{name}] 0 entries")
        return 0, 0

    for entry in parsed.entries:
        pub_date = _parse_date(entry)
        if pub_date:
            pub_dt = datetime.fromisoformat(pub_date)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue

        article_url = entry.get("link", "")
        if not article_url:
            continue

        title = entry.get("title", "").strip()
        content = entry.get("summary", "") or ""
        if not content or len(content) < 200:
            content = await _fetch_content(session, article_url) or content
        content = content[:4000]

        row_id = insert_entry(
            source_type="rss",
            source_category=feed["category"],
            source_name=name,
            source_url=article_url,
            author=entry.get("author", name),
            title=title,
            content=content,
            published_at=pub_date,
        )
        if row_id:
            new += 1
        else:
            skipped += 1
        await asyncio.sleep(0.5)

    logger.info(f"[{name}] new={new} skipped={skipped}")
    return new, skipped


async def scrape_rss_feeds() -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    new_total = skip_total = 0
    errors = []

    async with aiohttp.ClientSession() as session:
        for feed in RSS_FEEDS:
            try:
                new, skip = await scrape_feed(session, feed, cutoff)
                new_total += new
                skip_total += skip
            except Exception as e:
                msg = f"{feed['name']}: {e}"
                logger.error(msg)
                errors.append(msg)
            await asyncio.sleep(1)

    logger.info(f"RSS done - new={new_total} skipped={skip_total} errors={len(errors)}")
    return {"new": new_total, "skipped": skip_total, "errors": errors}
