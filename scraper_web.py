"""
Web scraper — 11 sources scraping HTML confirmées et testées.
"""

import logging
import asyncio
import aiohttp
import trafilatura
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from bs4 import BeautifulSoup
from urllib.parse import urljoin

from database import insert_entry, log_scrape_start, log_scrape_finish

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
}

# Chaque source : url listing, name, category, pattern regex, prefix pour liens relatifs
WEB_SOURCES = [
    # CEX
    {
        "url": "https://www.coinbase.com/blog",
        "name": "Coinbase",
        "category": "cex",
        "pattern": r"coinbase\.com/blog/[a-z0-9\-]{10,}",
        "prefix": "https://www.coinbase.com",
    },
    {
        "url": "https://www.gemini.com/blog",
        "name": "Gemini",
        "category": "cex",
        "pattern": r"gemini\.com/blog/[a-z0-9\-]{10,}",
        "prefix": "",
    },
    # Institutional
    {
        "url": "https://www.bullish.com/eu/news-insights",
        "name": "Bullish",
        "category": "institutional",
        "pattern": r"/eu/news-insights/[a-z0-9\-]{10,}",
        "prefix": "https://www.bullish.com",
    },
    # OTC
    {
        "url": "https://www.wintermute.com/insights/discover?category=announcements",
        "name": "Wintermute",
        "category": "otc",
        "pattern": r"wintermute\.com/insights/[a-z\-]+/[a-z\-]+/[a-z0-9\-]{10,}",
        "prefix": "https://www.wintermute.com",
    },
    {
        "url": "https://www.gsr.io/media",
        "name": "GSR",
        "category": "otc",
        "pattern": r"gsr\.io/insights/[a-z0-9\-]{10,}",
        "prefix": "",
    },
    {
        "url": "https://www.falconx.io/newsroom",
        "name": "FalconX",
        "category": "otc",
        "pattern": r"falconx\.io/newsroom/[a-z0-9\-]{10,}",
        "prefix": "",
    },
    # Stablecoins
    {
        "url": "https://www.circle.com/pressroom",
        "name": "Circle",
        "category": "stablecoins",
        "pattern": r"circle\.com/pressroom/[a-z0-9\-]{10,}",
        "prefix": "",
    },
    {
        "url": "https://tether.io/news/",
        "name": "Tether",
        "category": "stablecoins",
        "pattern": r"tether\.io/news/[a-z0-9\-]{10,}",
        "prefix": "",
    },
    {
        "url": "https://www.paxos.com/newsroom",
        "name": "Paxos",
        "category": "stablecoins",
        "pattern": r"paxos\.com/newsroom/[a-z0-9\-]{10,}",
        "prefix": "https://www.paxos.com",
    },
    # Prediction
    {
        "url": "https://news.kalshi.com/",
        "name": "Kalshi",
        "category": "prediction",
        "pattern": r"kalshi\.com/p/[a-z0-9\-]{5,}",
        "prefix": "https://kalshi.com",
    },
    # News
    {
        "url": "https://www.dlnews.com/articles/",
        "name": "DL News",
        "category": "news",
        "pattern": r"dlnews\.com/articles/[a-z\-]+/[a-z0-9\-]{10,}",
        "prefix": "",
    },
]


def _extract_links(html: str, source: dict) -> list[str]:
    pattern = re.compile(source["pattern"], re.IGNORECASE)
    prefix = source.get("prefix", "")
    base = source["url"]
    seen = set()
    links = []

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href.startswith("http"):
            full = href
        elif href.startswith("/"):
            full = prefix + href if prefix else urljoin(base, href)
        else:
            full = urljoin(base, href)

        # Nettoie query string
        full = full.split("?")[0].split("#")[0]

        if pattern.search(full) and full not in seen:
            seen.add(full)
            links.append(full)

    return links


def _extract_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    title = soup.find("title")
    return title.get_text(strip=True) if title else ""


def _extract_date(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("time"):
        dt = tag.get("datetime", "")
        if dt:
            try:
                return datetime.fromisoformat(dt.replace("Z", "+00:00")).isoformat()
            except Exception:
                pass
    for meta in soup.find_all("meta"):
        if meta.get("property") in ("article:published_time", "datePublished"):
            val = meta.get("content", "")
            if val:
                try:
                    return datetime.fromisoformat(val.replace("Z", "+00:00")).isoformat()
                except Exception:
                    pass
    return None


async def scrape_source(session, source: dict, cutoff: datetime) -> tuple[int, int]:
    name = source["name"]
    new = skipped = 0

    # Fetch listing page
    try:
        async with session.get(source["url"], headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                logger.warning(f"[{name}] Listing HTTP {r.status}")
                return 0, 0
            html = await r.text()
    except Exception as e:
        logger.warning(f"[{name}] Listing fetch failed: {e}")
        return 0, 0

    links = _extract_links(html, source)
    if not links:
        logger.warning(f"[{name}] 0 links matched")
        return 0, 0

    logger.debug(f"[{name}] {len(links)} links found")

    for url in links[:10]:
        try:
            async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    continue
                article_html = await r.text()
        except Exception:
            continue

        # Filtre date
        pub_date = _extract_date(article_html)
        if pub_date:
            try:
                pub_dt = datetime.fromisoformat(pub_date)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
            except Exception:
                pass

        text = trafilatura.extract(article_html, favor_recall=True, include_comments=False)
        if not text or len(text.strip()) < 100:
            continue

        title = _extract_title(article_html)

        row_id = insert_entry(
            source_type="scraping",
            source_category=source["category"],
            source_name=name,
            source_url=url,
            author=name,
            title=title,
            content=text[:4000],
            published_at=pub_date,
        )
        if row_id:
            new += 1
        else:
            skipped += 1

        await asyncio.sleep(1)

    logger.info(f"[{name}] new={new} skipped={skipped}")
    return new, skipped


async def scrape_web_sources() -> dict:
    run_id = log_scrape_start("web")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    new_total = skip_total = 0
    errors = []

    semaphore = asyncio.Semaphore(3)

    async def _bounded(source):
        async with semaphore:
            return await scrape_source(session, source, cutoff)

    async with aiohttp.ClientSession() as session:
        tasks = [_bounded(s) for s in WEB_SOURCES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for source, result in zip(WEB_SOURCES, results):
        if isinstance(result, Exception):
            msg = f"{source['name']}: {result}"
            logger.error(msg)
            errors.append(msg)
        else:
            new, skip = result
            new_total += new
            skip_total += skip

    log_scrape_finish(run_id, new_total, errors)
    return {"new": new_total, "skipped": skip_total, "errors": errors}
