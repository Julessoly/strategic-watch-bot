"""
Twitter scraper using GetXAPI.
Fetches recent tweets from specified accounts via advanced search.
Docs: https://docs.getxapi.com
"""

import os
import logging
import aiohttp
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from database import insert_entry

logger = logging.getLogger(__name__)

GETXAPI_KEY  = os.environ.get("GETXAPI_KEY", "")
GETXAPI_BASE = "https://api.getxapi.com"

# Twitter accounts to follow
TWITTER_ACCOUNTS = [
    {"username": "Crypto_Dealflow", "name": "Crypto Dealflow", "category": "fundraising", "description": "media"},
]


async def fetch_tweets(session: aiohttp.ClientSession, username: str, days: int = 1) -> list[dict]:
    """Fetch recent tweets for a username via advanced search."""
    from datetime import datetime, timezone, timedelta
    since_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        async with session.get(
            f"{GETXAPI_BASE}/twitter/tweet/advanced_search",
            headers={"Authorization": f"Bearer {GETXAPI_KEY}"},
            params={
                "q": f"from:{username} since:{since_date} -filter:retweets -filter:replies",
                "product": "Latest",
            },
            timeout=aiohttp.ClientTimeout(total=20)
        ) as r:
            if r.status != 200:
                logger.warning(f"[Twitter] API error {r.status} for @{username}")
                return []
            data = await r.json()
            return data.get("tweets", [])
    except Exception as e:
        logger.error(f"[Twitter] fetch_tweets error for @{username}: {e}")
        return []


async def scrape_twitter_accounts(days: int = 1) -> dict:
    """Scrape tweets from all configured accounts."""
    new_total = skip_total = errors = 0

    async with aiohttp.ClientSession() as session:
        for account in TWITTER_ACCOUNTS:
            username = account["username"]
            try:
                tweets = await fetch_tweets(session, username, days=days)
                if not tweets:
                    logger.info(f"[Twitter] @{username} — 0 tweets")
                    continue

                new = skipped = 0
                for tweet in tweets:
                    tweet_id  = tweet.get("id", "")
                    text      = tweet.get("text", "").strip()
                    created_at = tweet.get("createdAt", "")
                    tweet_url = tweet.get("twitterUrl") or tweet.get("url") or f"https://twitter.com/{username}/status/{tweet_id}"

                    if not text or not tweet_id:
                        continue

                    # Parse date
                    published_at = None
                    if created_at:
                        try:
                            # Format: "Sun Jan 25 13:05:46 +0000 2026"
                            dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S +0000 %Y")
                            dt = dt.replace(tzinfo=timezone.utc)
                            published_at = dt.isoformat()
                        except Exception:
                            published_at = created_at

                    row_id = insert_entry(
                        source_category=account["category"],
                        source_description=account["description"],
                        source_name=account["name"],
                        source_url=tweet_url,
                        author=f"@{username}",
                        title=text[:140],
                        content=text,
                        published_at=published_at,
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
