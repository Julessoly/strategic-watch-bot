"""
API scraper — 2 sources API JSON confirmées : Binance + DRW.
"""

import logging
import asyncio
import aiohttp
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from bs4 import BeautifulSoup

from database import insert_entry

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
}


# ─── Binance ──────────────────────────────────────────────────────────────────

async def scrape_binance(session: aiohttp.ClientSession, cutoff: datetime) -> tuple[int, int]:
    url = "https://www.binance.com/bapi/apex/v1/public/apex/cms/blog/list?category=2&size=20&page=1"
    new = skipped = 0

    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json()
    except Exception as e:
        logger.warning(f"[Binance] API fetch failed: {e}")
        return 0, 0

    articles = data.get("data", {}).get("blogList", [])
    for article in articles:
        # Date filter
        post_time_ms = article.get("postTimeUTC") or article.get("postTime")
        if post_time_ms:
            pub_dt = datetime.fromtimestamp(post_time_ms / 1000, tz=timezone.utc)
            if pub_dt < cutoff:
                continue
            pub_date = pub_dt.isoformat()
        else:
            pub_date = None

        article_id = article.get("idStr") or str(article.get("id", ""))
        article_url = f"https://www.binance.com/en/blog/{article_id}"
        title = article.get("title", "")
        content = article.get("brief", "")  # résumé court

        row_id = insert_entry(
            source_type="api",
            source_category="cex",
            source_name="Binance",
            source_url=article_url,
            author="Binance",
            title=title,
            content=content,
            published_at=pub_date,
        )
        if row_id:
            new += 1
        else:
            skipped += 1

    logger.info(f"[Binance] new={new} skipped={skipped}")
    return new, skipped


# ─── DRW ──────────────────────────────────────────────────────────────────────

async def _get_drw_hash(session: aiohttp.ClientSession) -> Optional[str]:
    """
    Extrait le hash Next.js depuis le HTML de la page DRW.
    Ce hash change à chaque déploiement de leur site.
    """
    try:
        async with session.get("https://www.drw.com/updates", headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
            html = await r.text()
        # Cherche le hash dans les liens _next/data
        match = re.search(r'/_next/data/([a-zA-Z0-9_\-]+)/en/updates', html)
        return match.group(1) if match else None
    except Exception as e:
        logger.warning(f"[DRW] Could not extract hash: {e}")
        return None


async def scrape_drw(session: aiohttp.ClientSession, cutoff: datetime) -> tuple[int, int]:
    new = skipped = 0

    hash_id = await _get_drw_hash(session)
    if not hash_id:
        logger.warning("[DRW] Could not get Next.js hash, skipping")
        return 0, 0

    url = f"https://www.drw.com/_next/data/{hash_id}/en/updates/page/1.json?page=1"

    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                logger.warning(f"[DRW] API HTTP {r.status}")
                return 0, 0
            data = await r.json()
    except Exception as e:
        logger.warning(f"[DRW] API fetch failed: {e}")
        return 0, 0

    articles = data.get("pageProps", {}).get("articles", [])
    for article in articles:
        pub_date_str = article.get("date")
        if pub_date_str:
            try:
                pub_dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
                if pub_dt < cutoff:
                    continue
                pub_date = pub_dt.isoformat()
            except Exception:
                pub_date = None
        else:
            pub_date = None

        slug = article.get("__url") or f"/updates/insights/{article.get('slug', '')}"
        article_url = f"https://www.drw.com{slug}"
        title = article.get("title", "")
        content = article.get("lead", "")

        row_id = insert_entry(
            source_type="api",
            source_category="otc",
            source_name="DRW",
            source_url=article_url,
            author="DRW",
            title=title,
            content=content,
            published_at=pub_date,
        )
        if row_id:
            new += 1
        else:
            skipped += 1

    logger.info(f"[DRW] new={new} skipped={skipped}")
    return new, skipped


# ─── Main ─────────────────────────────────────────────────────────────────────

async def scrape_api_sources() -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    new_total = skip_total = 0
    errors = []

    async with aiohttp.ClientSession() as session:
        for name, func in [("Binance", scrape_binance), ("DRW", scrape_drw)]:
            try:
                new, skip = await func(session, cutoff)
                new_total += new
                skip_total += skip
            except Exception as e:
                msg = f"{name}: {e}"
                logger.error(msg)
                errors.append(msg)

    return {"new": new_total, "skipped": skip_total, "errors": errors}
