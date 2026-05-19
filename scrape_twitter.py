"""
Twitter scraper using GetXAPI.
Fetches recent tweets from specified accounts and inserts them into the DB.
"""

import os
import logging
import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from database import insert_entry

logger = logging.getLogger(__name__)

GETXAPI_KEY = os.environ.get("GETXAPI_KEY", "")
GETXAPI_BASE = "https://api.getxapi.com/v2"

# Twitter accounts to follow
TWITTER_ACCOUNTS = [
    {"username": "Crypto_Dealflow", "name": "Crypto Dealflow", "category": "fundraising", "description": "media"},
]


async def fetch_user_id(session: aiohttp.ClientSession, username: str) -> Optional[str]:
    """Get Twitter user ID from username."""
    try:
        async with session.get(
            f"{GETXAPI_BASE}/user/by/username/{username}",
            headers={"x-api-key": GETXAPI_KEY},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                logger.warning(f"[Twitter] Failed to get user ID for {username}: {r.status}")
                return None
            data = await r.json()
            return data.get("data", {}).get("id")
    except Exception as e:
        logger.error(f"[Twitter] fetch_user_id error for {username}: {e}")
        return None


async def fetch_tweets(session: aiohttp.ClientSession, user_id: str, max_results: int = 20) -> list[dict]:
    """Fetch recent tweets for a user."""
    try:
        async with session.get(
            f"{GETXAPI_BASE}/user/{user_id}/tweets",
            headers={"x-api-key": GETXAPI_KEY},
            params={
                "max_results": max_results,
                "tweet.fields": "created_at,text,author_id",
                "exclude": "retweets,replies",
            },
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                logger.warning(f"[Twitter] Failed to fetch tweets for user {user_id}: {r.status}")
                return []
            data = await r.json()
            return data.get("data", [])
    except Exception as e:
        logger.error(f"[Twitter] fetch_tweets error for user {user_id}: {e}")
        return []


async def scrape_twitter_accounts(days: int = 1) -> dict:
    """Scrape tweets from all configured accounts."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    new_total = skip_total = errors = 0

    async with aiohttp.ClientSession() as session:
        for account in TWITTER_ACCOUNTS:
            username = account["username"]
            try:
                user_id = await fetch_user_id(session, username)
                if not user_id:
                    errors += 1
                    continue

                tweets = await fetch_tweets(session, user_id, max_results=20)
                if not tweets:
                    logger.info(f"[Twitter] @{username} — 0 tweets")
                    continue

                new = skipped = 0
                for tweet in tweets:
                    created_at = tweet.get("created_at", "")
                    if created_at:
                        try:
                            tweet_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                            if tweet_dt < cutoff:
                                continue
                        except Exception:
                            pass

                    tweet_id = tweet.get("id", "")
                    tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"
                    text = tweet.get("text", "").strip()

                    if not text or not tweet_id:
                        continue

                    row_id = insert_entry(
                        source_category=account["category"],
                        source_description=account["description"],
                        source_name=account["name"],
                        source_url=tweet_url,
                        author=f"@{username}",
                        title=text[:140],
                        content=text,
                        published_at=created_at or None,
                    )
                    if row_id:
                        new += 1
                    else:
                        skipped += 1

                new_total += new
                skip_total += skipped
                logger.info(f"[Twitter] @{username} — new={new} skipped={skipped}")
                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"[Twitter] Error scraping @{username}: {e}")
                errors += 1

    result = {"new": new_total, "skipped": skip_total, "errors": errors}
    logger.info(f"Twitter scrape done — {result}")
    return result
