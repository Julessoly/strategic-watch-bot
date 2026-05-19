"""
RSS scraper — native feeds + Google News RSS.
Native feeds: trafilatura for full content.
Google News: RSS summary as content. No url_filter — noise filtering handled by AI enrichment step.
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
    {"url": "https://blog.kraken.com/feed",         "name": "Kraken",      "category": "cex",           "description": "company"},
    {"url": "https://blog.bitfinex.com/feed",       "name": "Bitfinex",    "category": "cex",           "description": "company"},
    {"url": "https://blog.bitmex.com/feed/",        "name": "BitMEX",      "category": "cex",           "description": "company"},
    # Institutional
    {"url": "https://www.fireblocks.com/blog/feed", "name": "Fireblocks",  "category": "institutional", "description": "company"},
    # Research
    {"url": "https://a16zcrypto.substack.com/feed", "name": "a16z Crypto", "category": "research",      "description": "research"},
    {"url": "https://multicoin.capital/rss.xml",    "name": "Multicoin",   "category": "research",      "description": "research"},
    # News
    {"url": "https://www.theblock.co/rss.xml",      "name": "The Block",   "category": "news",          "description": "media"},
    {"url": "https://blockworks.co/feed",           "name": "Blockworks",  "category": "news",          "description": "media"},
]

# Google News sources — sous-chemins validés manuellement pour limiter le bruit
# Le filtrage fin (coin updates, tutorials, job listings, etc.) est géré par l'étape d'enrichissement IA
GOOGLE_NEWS_SOURCES = [
    # CEX — sous-chemins directs validés
    {"site": "coinbase.com/blog",          "name": "Coinbase",        "category": "cex", "description": "company"},
    {"site": "gemini.com/blog",            "name": "Gemini",          "category": "cex", "description": "company"},
    {"site": "binance.com/en/blog",        "name": "Binance",         "category": "cex", "description": "company"},
    {"site": "okx.com/learn",              "name": "OKX",             "category": "cex", "description": "company"},
    {"site": "crypto.com/en/company-news", "name": "Crypto.com",      "category": "cex", "description": "company"},
    {"site": "bitstamp.net/blog",          "name": "Bitstamp",        "category": "cex", "description": "company"},
    {"site": "announcements.bybit.com",    "name": "Bybit",           "category": "cex", "description": "company"},
    {"site": "gate.com/blog",              "name": "Gate.io",         "category": "cex", "description": "company"},
    {"site": "nexo.com/blog",              "name": "Nexo",            "category": "cex", "description": "company"},
    {"site": "bitget.com/blog",            "name": "Bitget",          "category": "cex", "description": "company"},
    # Institutional — domaine racine (peu d'articles, peu de bruit)
    {"site": "bullish.com",                "name": "Bullish",         "category": "institutional", "description": "company"},
    {"site": "bitgo.com",                  "name": "BitGo",           "category": "institutional", "description": "company"},
    {"site": "anchorage.com",              "name": "Anchorage",       "category": "institutional", "description": "company"},
    {"site": "talos.com",                  "name": "Talos",           "category": "institutional", "description": "company"},
    {"site": "ambergroup.io",              "name": "Amber",           "category": "institutional", "description": "company"},
    # OTC — domaine racine
    {"site": "gsr.io",                     "name": "GSR",             "category": "otc", "description": "company"},
    {"site": "falconx.io",                 "name": "FalconX",         "category": "otc", "description": "company"},
    {"site": "wintermute.com",             "name": "Wintermute",      "category": "otc", "description": "company"},
    {"site": "drw.com",                    "name": "DRW",             "category": "otc", "description": "company"},
    {"site": "flowdesk.co",                "name": "Flowdesk",        "category": "otc", "description": "company"},
    {"site": "galaxy.com",                 "name": "Galaxy",          "category": "otc", "description": "company"},
    {"site": "b2c2.com",                   "name": "B2C2",            "category": "otc", "description": "company"},
    # Stablecoins — sous-chemins validés
    {"site": "circle.com/blog",            "name": "Circle",          "category": "stablecoins", "description": "company"},
    {"site": "tether.io/news",             "name": "Tether",          "category": "stablecoins", "description": "company"},
    {"site": "paxos.com",                  "name": "Paxos",           "category": "stablecoins", "description": "company"},
    {"site": "ripple.com",                 "name": "Ripple",          "category": "stablecoins", "description": "company"},
    {"site": "treasury.ripple.com",        "name": "Ripple Treasury", "category": "stablecoins", "description": "company"},
    # Prediction Markets
    {"site": "predictionnews.com",         "name": "Prediction News",  "category": "prediction_markets", "description": "media"},
    # Research — domaine racine (peu d'articles)
    {"site": "paradigm.xyz",               "name": "Paradigm",        "category": "research", "description": "research"},
]


def _build_google_news_feeds() -> list[dict]:
    """Build Google News RSS URLs with a 30-day window."""
    thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    feeds = []
    for s in GOOGLE_NEWS_SOURCES:
        url = f"https://news.google.com/rss/search?q=site:{s['site']}+after:{thirty_days_ago}&hl=en-US&gl=US&ceid=US:en"
        feeds.append({
            "url": url,
            "name": s["name"],
            "category": s["category"],
            "description": s.get("description", "company"),
            "google_news": True,
        })
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
    """Fetch full article content via trafilatura. Only used for native RSS feeds."""
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
    is_google = feed.get("google_news", False)
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

        if is_google:
            # Google News: use RSS summary as content (no trafilatura — most sites are React/blocked)
            raw_summary = entry.get("summary", "") or ""
            content = _strip_html(raw_summary) if raw_summary else ""
        else:
            # Native feed: fetch full content via trafilatura
            content = await _fetch_article(session, article_url)
            if not content:
                raw_summary = entry.get("summary", "") or ""
                content = _strip_html(raw_summary) if raw_summary else ""

        content = content[:4000]

        row_id = insert_entry(
            source_category=feed["category"],
            source_description=feed.get("description"),
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

    feeds = get_all_feeds()

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
