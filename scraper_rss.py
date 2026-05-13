"""
RSS scraper — native feeds + Google News RSS for everything else.
Google News URLs include a dynamic after: filter (yesterday) to avoid old articles.
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

# Static native RSS feeds
NATIVE_FEEDS = [
    # CEX
    {"url": "https://blog.kraken.com/feed",         "name": "Kraken",      "category": "cex"},
    {"url": "https://blog.bitfinex.com/feed",       "name": "Bitfinex",    "category": "cex"},
    {"url": "https://blog.bitmex.com/feed/",        "name": "BitMEX",      "category": "cex"},
    # Institutional
    {"url": "https://www.fireblocks.com/blog/feed", "name": "Fireblocks",  "category": "institutional"},
    # Research
    {"url": "https://a16zcrypto.substack.com/feed", "name": "a16z Crypto", "category": "research"},
    {"url": "https://multicoin.capital/rss.xml",    "name": "Multicoin",   "category": "research"},
    # News
    {"url": "https://www.theblock.co/rss.xml",      "name": "The Block",   "category": "news"},
    {"url": "https://blockworks.co/feed",           "name": "Blockworks",  "category": "news"},
]

# Google News sources — URLs built dynamically at runtime with after: filter
GOOGLE_NEWS_SOURCES = [
    # CEX
    {"site": "coinbase.com",      "name": "Coinbase",   "category": "cex"},
    {"site": "gemini.com",        "name": "Gemini",     "category": "cex"},
    {"site": "binance.com",       "name": "Binance",    "category": "cex"},
    {"site": "okx.com",           "name": "OKX",        "category": "cex"},
    {"site": "crypto.com",        "name": "Crypto.com", "category": "cex"},
    {"site": "bitstamp.net",      "name": "Bitstamp",   "category": "cex"},
    {"site": "bybit.com",         "name": "Bybit",      "category": "cex"},
    {"site": "gate.io",           "name": "Gate.io",    "category": "cex"},
    {"site": "nexo.com",          "name": "Nexo",       "category": "cex"},
    {"site": "bitget.com",        "name": "Bitget",     "category": "cex"},
    {"site": "mexc.com",          "name": "MEXC",       "category": "cex"},
    # Institutional
    {"site": "bullish.com",       "name": "Bullish",    "category": "institutional"},
    {"site": "bitgo.com",         "name": "BitGo",      "category": "institutional"},
    {"site": "anchorage.com",     "name": "Anchorage",  "category": "institutional"},
    {"site": "talos.com",         "name": "Talos",      "category": "institutional"},
    {"site": "ambergroup.io",     "name": "Amber",      "category": "institutional"},
    # OTC
    {"site": "gsr.io",            "name": "GSR",        "category": "otc"},
    {"site": "falconx.io",        "name": "FalconX",    "category": "otc"},
    {"site": "wintermute.com",    "name": "Wintermute", "category": "otc"},
    {"site": "drw.com",           "name": "DRW",        "category": "otc"},
    {"site": "flowdesk.co",       "name": "Flowdesk",   "category": "otc"},
    {"site": "galaxy.com",        "name": "Galaxy",     "category": "otc"},
    {"site": "b2c2.com",          "name": "B2C2",       "category": "otc"},
    # Stablecoins
    {"site": "circle.com",        "name": "Circle",     "category": "stablecoins"},
    {"site": "tether.io",         "name": "Tether",     "category": "stablecoins"},
    {"site": "paxos.com",         "name": "Paxos",      "category": "stablecoins"},
    {"site": "ripple.com",        "name": "Ripple",     "category": "stablecoins"},
    # Research
    {"site": "paradigm.xyz",      "name": "Paradigm",   "category": "research"},
]


def _build_google_news_feeds() -> list[dict]:
    """Build Google News RSS URLs with a 30-day window to maximize available articles.
    The actual cutoff filtering is done in scrape_rss_feeds() based on the days parameter."""
    thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    feeds = []
    for s in GOOGLE_NEWS_SOURCES:
        url = f"https://news.google.com/rss/search?q=site:{s['site']}+after:{thirty_days_ago}&hl=en-US&gl=US&ceid=US:en"
        feeds.append({"url": url, "name": s["name"], "category": s["category"], "google_news": True})
    return feeds


def get_all_feeds() -> list[dict]:
    return NATIVE_FEEDS + _build_google_news_feeds()


# RSS_FEEDS exposed for health check in bot.py
RSS_FEEDS = NATIVE_FEEDS + [{"name": s["name"], "url": "", "category": s["category"]} for s in GOOGLE_NEWS_SOURCES]


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
        logger.debug(f"[{name}] 0 entries")
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

        # Only fetch full content for native RSS feeds, not Google News
        if feed.get("google_news"):
            content = ""
        else:
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


async def scrape_rss_feeds(days: int = 1) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    new_total = skip_total = 0
    errors = []

    feeds = get_all_feeds()  # URLs built fresh at runtime with today's date

    async with aiohttp.ClientSession() as session:
        for feed in feeds:
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
