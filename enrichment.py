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

Your job is to review articles scraped from crypto company blogs, news sources, and Twitter accounts, and decide:
1. Is this article relevant to Blockchain.com's competitive intelligence?
2. If yes, what free-form tags best describe its content?

There are three types of sources:

--- SOURCE TYPE: "The Block" (media) ---
Keep ALL articles from The Block. It is our general industry news source.
Always return {"relevant": true, ...} for The Block articles.

--- SOURCE TYPE: "Crypto Dealflow" (fundraising/Twitter) ---
Keep ALL tweets from Crypto Dealflow. They are fundraising announcements.
Always return {"relevant": true, ...} for Crypto Dealflow tweets.
For tags, include:
- The amount raised (e.g. "8m", "50m", "series-a")
- The sector (e.g. "defi", "payments", "ai", "custody", "stablecoin", "infrastructure")
- Key investors if mentioned (e.g. "a16z", "paradigm")
- A short description of what the company does based on your knowledge or web search (1 sentence max, e.g. "company:settlement-layer-for-ai-agents"). Use your knowledge of the crypto industry to describe the company — if you don't know it, infer from the tweet context.
Format the company description as a tag starting with "company:" so it can be extracted easily.

--- SOURCE TYPE: company blogs (all other sources) ---
For company blogs, ONLY keep articles that are directly about the company's own actions:
- Product launches, new features, platform updates
- Partnerships, integrations, collaborations
- Regulatory approvals, licenses, compliance news
- Fundraising, M&A, financial results
- Company expansions, new markets, new offices
- Technology innovations specific to that company

DELETE from company blogs:
- Job listings / open roles ("Open Role —", "We're hiring", "Join our team")
- General market recaps ("Markets Today", "Weekly recap", "Bitcoin price this week")
- Generic educational content not tied to a company announcement ("How to buy X", "What is DeFi")
- Promotional campaigns, giveaways, contests, prize pools
- Coin/token listing announcements (e.g. "Kraken lists AVA", "Bitget Lists ILITY")
- Terms of use, privacy policy, legal disclosures, cookie notices
- Generic product page descriptions ("Spot OTC trading built for institutional execution")
- Daily market analysis with no company-specific news
- Podcast episodes, AMAs with no company announcement

For RELEVANT articles, generate 3-8 free-form lowercase tags describing:
- Topics (e.g. "stablecoin", "bitcoin-etf", "defi", "tokenization", "custody")
- Key actors (e.g. "circle", "hyperliquid", "blackrock", "solana", "mica")
- Nature of news (e.g. "partnership", "product-launch", "regulatory", "fundraising", "earnings")

Respond ONLY with valid JSON, no preamble, no markdown:
{"relevant": true, "tags": "tag1,tag2,tag3"}
or
{"relevant": false, "tags": ""}"""


async def enrich_entry(session: aiohttp.ClientSession, entry: dict) -> tuple[bool, str]:
    """Call Claude to assess relevance and generate tags for one entry."""
    title = entry.get("title", "").strip()
    content = entry.get("content", "").strip()
    source = entry.get("source_name", "")
    category = entry.get("source_category", "")

    user_message = f"""Source name: {source}
Source type: {entry.get("source_description", "company")}
Category: {category}
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
                    update_tags(entry["id"], "noise")
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


async def deduplicate_cross_day() -> int:
    """Compares today's active news against yesterday's to flag straggler duplicates."""
    from database import get_conn, update_tags
    
    conn = get_conn()
    # Get Yesterday's approved news (24h to 48h ago)
    # yesterday = conn.execute("""
    #     SELECT id, source_name, title FROM entries 
    #     WHERE ingested_at >= datetime('now', '-48 hours') AND ingested_at < datetime('now', '-24 hours')
    #     AND tags IS NOT NULL AND tags NOT IN ('noise', 'duplicate', 'untagged')
    # """).fetchall()

    # Get Past week's approved news (24h to 7 days ago)
    past_news = conn.execute("""
        SELECT id, source_name, title FROM entries 
        WHERE ingested_at >= datetime('now', '-7 days') AND ingested_at < datetime('now', '-24 hours')
        AND tags IS NOT NULL AND tags NOT IN ('noise', 'duplicate', 'untagged')
    """).fetchall()
    
    # Get Today's approved news (0h to 24h ago)
    today = conn.execute("""
        SELECT id, source_name, title FROM entries 
        WHERE ingested_at >= datetime('now', '-24 hours')
        AND tags IS NOT NULL AND tags NOT IN ('noise', 'duplicate', 'untagged')
    """).fetchall()
    conn.close()

    if not past_news or not today:
        return 0

    past_text = "\n".join([f"ID: {r[0]} | Source: {r[1]} | Title: {r[2]}" for r in past_news])
    today_text = "\n".join([f"ID: {r[0]} | Source: {r[1]} | Title: {r[2]}" for r in today])

    prompt = f"""
                You are a data cleaner. 
                Here is YESTERDAY'S news (already reported):
                {past_text}

                Here is TODAY'S news:
                {today_text}

                Task: Identify any articles in TODAY's news that are reporting the exact same event as an article from YESTERDAY. (e.g. a media site reporting on a company blog from yesterday).
                Return ONLY a valid JSON list of IDs from TODAY's news that are duplicates. If none, return [].
                Example: [142, 145]
            """

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ANTHROPIC_API_URL,
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": 100, "system": "Output only valid JSON.", "messages": [{"role": "user", "content": prompt}]},
            ) as r:
                data = await r.json()
                text = data["content"][0]["text"].strip()
                
                # Parse the JSON list of IDs
                duplicate_ids = json.loads(text)
                
                for dup_id in duplicate_ids:
                    update_tags(dup_id, "duplicate")
                    logger.info(f"Flagged cross-day duplicate ID: {dup_id}")
                    
                return len(duplicate_ids)
    except Exception as e:
        logger.error(f"Cross-day deduplication failed: {e}")
        return 0