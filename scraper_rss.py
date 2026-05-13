"""
RSS scraper — native feeds + Google News RSS for JS-heavy sites.
Always fetches full article content via trafilatura.
"""

import logging
import asyncio
import aiohttp
import feedparser
import trafilatura
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from typing import Optional

from database import insert_entry

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
}

RSS_FEEDS = [
    # ── CEX — Native RSS ──────────────────────────────────────────────────────
    {"url": "https://blog.kraken.com/feed",              "name": "Kraken",        "category": "cex"},
    {"url": "https://blog.bitfinex.com/feed",            "name": "Bitfinex",      "category": "cex"},
    {"url": "https://blog.bitmex.com/feed/",             "name": "BitMEX",        "category": "cex"},

    # ── CEX — Google News RSS ─────────────────────────────────────────────────
    {"url": "https://news.google.com/rss/search?q=site:coinbase.com/blog&hl=en-US&gl=US&ceid=US:en",   "name": "Coinbase",   "category": "cex"},
    {"url": "https://news.google.com/rss/search?q=site:gemini.com/blog&hl=en-US&gl=US&ceid=US:en",     "name": "Gemini",     "category": "cex"},
    {"url": "https://news.google.com/rss/search?q=site:okx.com/en-us/learn&hl=en-US&gl=US&ceid=US:en", "name": "OKX",        "category": "cex"},
    {"url": "https://news.google.com/rss/search?q=site:crypto.com/en/company-news&hl=en-US&gl=US&ceid=US:en", "name": "Crypto.com", "category": "cex"},
    {"url": "https://news.google.com/rss/search?q=site:bitstamp.net/blog&hl=en-US&gl=US&ceid=US:en",   "name": "Bitstamp",   "category": "cex"},

    # ── CEX — API JSON (Binance handled in scraper_api.py) ────────────────────

    # ── Institutional — Native RSS ────────────────────────────────────────────
    {"url": "https://www.fireblocks.com/blog/feed",      "name": "Fireblocks",    "category": "institutional"},

    # ── Institutional — Google News RSS ──────────────────────────────────────
    {"url": "https://news.google.com/rss/search?q=site:bitgo.com/blog&hl=en-US&gl=US&ceid=US:en",       "name": "BitGo",      "category": "institutional"},
    {"url": "https://news.google.com/rss/search?q=site:anchorage.com/blog&hl=en-US&gl=US&ceid=US:en",   "name": "Anchorage",  "category": "institutional"},
    {"url": "https://news.google.com/rss/search?q=site:talos.com/insights&hl=en-US&gl=US&ceid=US:en",   "name": "Talos",      "category": "institutional"},
    {"url": "https://news.google.com/rss/search?q=site:ambergroup.io/news&hl=en-US&gl=US&ceid=US:en",   "name": "Amber",      "category": "institutional"},

    # ── OTC — Google News RSS ─────────────────────────────────────────────────
    {"url": "https://news.google.com/rss/search?q=site:flowdesk.co/insights&hl=en-US&gl=US&ceid=US:en", "name": "Flowdesk",   "category": "otc"},
    {"url": "https://news.google.com/rss/search?q=site:galaxy.com/newsroom&hl=en-US&gl=US&ceid=US:en",  "name": "Galaxy",     "category": "otc"},
    {"url": "https://news.google.com/rss/search?q=site:b2c2.com/news&hl=en-US&gl=US&ceid=US:en",        "name": "B2C2",       "category": "otc"},

    # ── Stablecoins — Google News RSS ────────────────────────────────────────
    {"url": "https://news.google.com/rss/search?q=site:ripple.com/press-releases&hl=en-US&gl=US&ceid=US:en", "name": "Ripple",  "category": "stablecoins"},
    {"url": "https://news.google.com/rss/search?q=site:newsroom.paypal-corp.com/news-cryptocurrency&hl=en-US&gl=US&ceid=US:en", "name": "PayPal", "category": "stablecoins"},

    # ── Research — Native RSS ─────────────────────────────────────────────────
    {"url": "https://a16zcrypto.substack.com/feed",      "name": "a16z Crypto",   "category": "research"},
    {"url": "https://multicoin.capital/rss.xml",         "name": "Multicoin",     "category": "research"},

    # ── Research — Google News RSS ────────────────────────────────────────────
    {"url": "https://news.google.com/rss/search?q=site:paradigm.xyz/writing&hl=en-US&gl=US&ceid=US:en", "name": "Paradigm",   "category": "research"},

    # ── News — Native RSS ─────────────────────────────────────────────────────
    {"url": "https://www.theblock.co/rss.xml",           "name": "The Block",     "category": "news"},
    {"url": "https://blockworks.co/feed",                "name": "Blockworks",    "category": "news"},
]


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


def _strip_html(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)


async def _fetch_article(session, url: str) -> Optional[str]:
    """Fetch full article content via trafilatura."""
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            html = await r.text()
        text = trafilatura.extract(html, favor_recall=True, include_comments=False, include_tables=False)
        return text[:4000] if text else None
    except Exception as e:
        logger.debug(f"Article fetch failed [{url}]: {e}")
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

        # Google News redirects — use the real URL from the feed
        title = entry.get("title", "").strip()

        # Fetch full article content
        content = await _fetch_article(session, article_url)
        if not content:
            raw_summary = entry.get("summary", "") or ""
            content = _strip_html(raw_summary) if raw_summary else ""
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
