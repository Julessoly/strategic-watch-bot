"""
AI enrichment module.
For each unenriched entry (tags IS NULL):
- If noise (job listing, coin price, conversion page, market recap, etc.) → delete
- If relevant → generate free-form tags describing the article
"""

import os
import asyncio
import aiohttp
import json
import logging
from typing import Optional

from database import get_unenriched_entries, update_tags, delete_entry

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-20250514"

SYSTEM_PROMPT = """You are an analyst for Blockchain.com, a leading crypto company offering retail exchange, institutional OTC, custody, staking, and prime brokerage services.

Your job is to review articles scraped from crypto company blogs and news sources, and decide:
1. Is this article relevant to Blockchain.com's competitive intelligence? 
2. If yes, what free-form tags best describe its content?

RELEVANT articles include:
- Product launches, new features, partnerships
- Regulatory news, licenses, compliance updates
- Fundraising, M&A, company expansions
- Research reports, market analysis
- Technology innovations (AI, stablecoins, tokenization, DeFi, etc.)
- Industry trends relevant to exchanges, custody, OTC, staking

NOISE articles to DELETE include:
- Job listings / open roles
- Coin price updates, exchange rate conversions ("BTC to USD", "XRP to BBD")
- Token listing announcements on small exchanges
- Tutorial articles ("How to buy X in 3 steps")
- Daily market recaps with no strategic insight
- Terms of use, privacy policy, legal disclosures
- Generic product pages ("Spot OTC trading built for institutional execution")
- Promotional campaigns, giveaways, contests

For RELEVANT articles, generate 3-8 free-form lowercase tags that describe:
- Topics covered (e.g. "stablecoin", "bitcoin-etf", "defi", "tokenization")
- Key actors mentioned (e.g. "circle", "hyperliquid", "blackrock", "solana")
- Nature of the news (e.g. "partnership", "product-launch", "regulatory", "fundraising", "research")

Respond ONLY with valid JSON, no preamble:
{"relevant": true, "tags": "tag1,tag2,tag3"}
or
{"relevant": false, "tags": ""}"""


async def enrich_entry(session: aiohttp.ClientSession, entry: dict) -> tuple[bool, str]:
    """Call Claude to assess relevance and generate tags for one entry."""
    title = entry.get("title", "").strip()
    content = entry.get("content", "").strip()
    source = entry.get("source_name", "")
    category = entry.get("source_category", "")

    user_message = f"""Source: {source} ({category})
Title: {title}
Content: {content[:1000] if content else "No content available"}"""

    try:
        async with session.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 200,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}]
            },
            timeout=aiohttp.ClientTimeout(total=30)
        ) as r:
            if r.status != 200:
                logger.warning(f"API error {r.status} for entry {entry['id']}")
                return True, "untagged"
            data = await r.json()
            text = data["content"][0]["text"].strip()
            # Strip markdown code fences if present
            text = text.replace("```json", "").replace("```", "").strip()
            logger.info(f"API response for entry {entry['id']}: {text[:200]}")
            parsed = json.loads(text)
            relevant = parsed.get("relevant", True)
            tags = parsed.get("tags", "").strip()
            return relevant, tags
    except json.JSONDecodeError as e:
        logger.warning(f"JSON parse error for entry {entry['id']}: {e} — raw: {text[:200] if 'text' in dir() else 'N/A'}")
        return True, "untagged"
    except Exception as e:
        logger.error(f"Enrichment error for entry {entry['id']}: {e}")
        return True, "untagged"  # On error, keep the entry


async def enrich_entries(limit: int = 100) -> dict:
    """Enrich a batch of unenriched entries. Returns stats."""
    entries = get_unenriched_entries(limit=limit)
    if not entries:
        logger.info("No unenriched entries found")
        return {"processed": 0, "kept": 0, "deleted": 0, "errors": 0}

    kept = deleted = errors = 0

    async with aiohttp.ClientSession() as session:
        for entry in entries:
            try:
                relevant, tags = await enrich_entry(session, entry)
                if relevant:
                    update_tags(entry["id"], tags)
                    kept += 1
                    logger.debug(f"[KEEP] {entry['source_name']} — {entry['title'][:60]} | tags: {tags}")
                else:
                    delete_entry(entry["id"])
                    deleted += 1
                    logger.debug(f"[DELETE] {entry['source_name']} — {entry['title'][:60]}")
            except Exception as e:
                logger.error(f"Error processing entry {entry['id']}: {e}")
                errors += 1
            await asyncio.sleep(0.3)  # Rate limit

    result = {
        "processed": len(entries),
        "kept": kept,
        "deleted": deleted,
        "errors": errors,
    }
    logger.info(f"Enrichment done — {result}")
    return result
