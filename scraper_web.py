"""
Web scraper v1 — listing pages only.
Extracts title + date + URL from listing pages. No article content fetch.
"""

import logging
import asyncio
import aiohttp
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from database import insert_entry

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
}


def _parse_date_text(text: str) -> Optional[str]:
    """
    Parse various date text formats into ISO string.
    Handles: 'May 11, 2026' / 'May 11th, 2026' / 'April 14th, 2026' /
             'MAY 12, 2026' / 'MAY 5, 2026'
    """
    if not text:
        return None
    text = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', text).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    return None


# ─── Coinbase ─────────────────────────────────────────────────────────────────

async def scrape_coinbase(session, cutoff: datetime) -> tuple[int, int]:
    url = "https://www.coinbase.com/blog"
    new = skipped = 0

    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                logger.warning(f"[Coinbase] HTTP {r.status}")
                return 0, 0
            html = await r.text()
    except Exception as e:
        logger.warning(f"[Coinbase] Fetch failed: {e}")
        return 0, 0

    soup = BeautifulSoup(html, "html.parser")

    # Find "Most recent" h2 and take articles after it
    most_recent = soup.find("h2", string=re.compile(r"most recent", re.IGNORECASE))
    if not most_recent:
        logger.warning("[Coinbase] Could not find 'Most recent' section")
        return 0, 0

    cards = []
    for sibling in most_recent.find_next_siblings():
        cards.extend(sibling.find_all("div", {"data-testid": "card-article"}))
    
    if not cards:
        logger.warning("[Coinbase] 0 articles found after 'Most recent'")
        return 0, 0

    for card in cards[:10]:
        link = card.find("a", {"data-testid": "card-article-link-overlay"})
        if not link:
            continue
        href = link.get("href", "")
        article_url = f"https://www.coinbase.com{href}" if href.startswith("/") else href

        title_tag = card.find("h3", {"data-testid": "card-article-title"})
        title = title_tag.get_text(strip=True) if title_tag else ""

        # Date is the last span in the card
        spans = card.find_all("span")
        date_text = ""
        for span in reversed(spans):
            t = span.get_text(strip=True)
            if re.match(r"[A-Za-z]+ \d+,? \d{4}", t):
                date_text = t
                break
        pub_date = _parse_date_text(date_text)

        if pub_date:
            pub_dt = datetime.fromisoformat(pub_date)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue

        row_id = insert_entry(
            source_type="scraping",
            source_category="cex",
            source_name="Coinbase",
            source_url=article_url,
            author="Coinbase",
            title=title,
            content="",
            published_at=pub_date,
        )
        if row_id:
            new += 1
        else:
            skipped += 1

    logger.info(f"[Coinbase] new={new} skipped={skipped}")
    return new, skipped


# ─── Gemini ───────────────────────────────────────────────────────────────────

async def scrape_gemini(session, cutoff: datetime) -> tuple[int, int]:
    url = "https://www.gemini.com/blog/type/company"
    new = skipped = 0

    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                logger.warning(f"[Gemini] HTTP {r.status}")
                return 0, 0
            html = await r.text()
    except Exception as e:
        logger.warning(f"[Gemini] Fetch failed: {e}")
        return 0, 0

    soup = BeautifulSoup(html, "html.parser")

    # Each article is wrapped in <a class="group block" href="/blog/...">
    articles = soup.find_all("a", class_=lambda c: c and "group" in c and "block" in c)

    for article in articles[:10]:
        href = article.get("href", "")
        if not href.startswith("/blog/"):
            continue
        article_url = f"https://www.gemini.com{href}"

        # Title: <p class="...body-lg-regular...">
        title_tag = article.find("p", class_=lambda c: c and "body-lg-regular" in c)
        title = title_tag.get_text(strip=True) if title_tag else ""

        # Date: <p class="...text-gray-700...">
        date_tag = article.find("p", class_=lambda c: c and "text-gray-700" in c)
        date_text = date_tag.get_text(strip=True) if date_tag else ""
        pub_date = _parse_date_text(date_text)

        if pub_date:
            pub_dt = datetime.fromisoformat(pub_date)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue

        row_id = insert_entry(
            source_type="scraping",
            source_category="cex",
            source_name="Gemini",
            source_url=article_url,
            author="Gemini",
            title=title,
            content="",
            published_at=pub_date,
        )
        if row_id:
            new += 1
        else:
            skipped += 1

    logger.info(f"[Gemini] new={new} skipped={skipped}")
    return new, skipped


# ─── Bullish ──────────────────────────────────────────────────────────────────

async def scrape_bullish(session, cutoff: datetime) -> tuple[int, int]:
    url = "https://www.bullish.com/eu/news-insights"
    new = skipped = 0

    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                logger.warning(f"[Bullish] HTTP {r.status}")
                return 0, 0
            html = await r.text()
    except Exception as e:
        logger.warning(f"[Bullish] Fetch failed: {e}")
        return 0, 0

    soup = BeautifulSoup(html, "html.parser")

    # Each article: <div role="listitem" class="w-dyn-item">
    items = soup.find_all("div", {"role": "listitem", "class": lambda c: c and "w-dyn-item" in c})

    for item in items[:10]:
        link = item.find("a", href=True)
        if not link:
            continue
        href = link.get("href", "")
        
        # Skip monthly metrics
        if "monthly-metrics" in href:
            continue

        article_url = f"https://www.bullish.com{href}" if href.startswith("/") else href

        title_tag = item.find("div", class_=lambda c: c and "h4-style" in c)
        title = title_tag.get_text(strip=True) if title_tag else ""

        date_tag = item.find("div", class_=lambda c: c and "date-format-2" in c)
        date_text = date_tag.get_text(strip=True) if date_tag else ""
        pub_date = _parse_date_text(date_text)

        if pub_date:
            pub_dt = datetime.fromisoformat(pub_date)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue

        row_id = insert_entry(
            source_type="scraping",
            source_category="institutional",
            source_name="Bullish",
            source_url=article_url,
            author="Bullish",
            title=title,
            content="",
            published_at=pub_date,
        )
        if row_id:
            new += 1
        else:
            skipped += 1

    logger.info(f"[Bullish] new={new} skipped={skipped}")
    return new, skipped


# ─── Placeholder scrapers (to be implemented after inspection) ─────────────────

async def scrape_wintermute(session, cutoff): return 0, 0
async def scrape_gsr(session, cutoff): return 0, 0
async def scrape_falconx(session, cutoff): return 0, 0
async def scrape_circle(session, cutoff): return 0, 0
async def scrape_tether(session, cutoff): return 0, 0
async def scrape_paxos(session, cutoff): return 0, 0
async def scrape_kalshi(session, cutoff): return 0, 0


# ─── Main ─────────────────────────────────────────────────────────────────────

SCRAPERS = [
    ("Coinbase",   scrape_coinbase),
    ("Gemini",     scrape_gemini),
    ("Bullish",    scrape_bullish),
    ("Wintermute", scrape_wintermute),
    ("GSR",        scrape_gsr),
    ("FalconX",    scrape_falconx),
    ("Circle",     scrape_circle),
    ("Tether",     scrape_tether),
    ("Paxos",      scrape_paxos),
    ("Kalshi",     scrape_kalshi),
]


async def scrape_web_sources() -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)  # 30 days for testing
    new_total = skip_total = 0
    errors = []

    async with aiohttp.ClientSession() as session:
        for name, func in SCRAPERS:
            try:
                new, skip = await func(session, cutoff)
                new_total += new
                skip_total += skip
            except Exception as e:
                msg = f"{name}: {e}"
                logger.error(msg)
                errors.append(msg)
            await asyncio.sleep(1)

    logger.info(f"Web done - new={new_total} skipped={skip_total} errors={len(errors)}")
    return {"new": new_total, "skipped": skip_total, "errors": errors}
