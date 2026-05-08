"""
Twitter scraper via GetXAPI — comptes uniquement.
Récupère uniquement les tweets des dernières 24h via l'opérateur since:.
Exclut les retweets et les réponses.
Dédup automatique par source_url dans la DB.
"""

import os
import logging
import aiohttp
import json
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator

from database import insert_entry, log_scrape_start, log_scrape_finish

logger = logging.getLogger(__name__)

GETXAPI_KEY  = os.environ.get("GETXAPI_KEY", "")
GETXAPI_BASE = "https://api.getxapi.com"

# Liste des comptes à scraper — chargée depuis targets.json
WATCH_ACCOUNTS: list[dict] = []


def load_targets(path: str = "targets.json"):
    """Charge la liste des comptes depuis targets.json."""
    if not os.path.exists(path):
        logger.warning(f"Pas de fichier targets à {path}")
        return
    with open(path) as f:
        data = json.load(f)
    WATCH_ACCOUNTS.clear()
    for account in data.get("accounts", []):
        if isinstance(account, dict) and "handle" in account:
            WATCH_ACCOUNTS.append(account)
    logger.info(f"Comptes chargés : {len(WATCH_ACCOUNTS)}")


# ─── GetXAPI ──────────────────────────────────────────────────────────────────

async def _getx_get(session: aiohttp.ClientSession, endpoint: str, params: dict) -> dict:
    headers = {"Authorization": f"Bearer {GETXAPI_KEY}"}
    async with session.get(
        f"{GETXAPI_BASE}{endpoint}",
        headers=headers,
        params=params,
        timeout=aiohttp.ClientTimeout(total=30)
    ) as resp:
        resp.raise_for_status()
        return await resp.json()


def _since_date() -> str:
    """Retourne la date d'hier au format YYYY-MM-DD pour l'opérateur since:"""
    yesterday = datetime.now(timezone.utc) - timedelta(hours=24)
    return yesterday.strftime("%Y-%m-%d")


async def fetch_user_tweets(
    session: aiohttp.ClientSession,
    handle: str,
    max_results: int = 50,
) -> AsyncGenerator[dict, None]:
    """
    Récupère les tweets originaux des dernières 24h d'un compte via advanced_search.
    Exclut les retweets (-is:retweet) et les réponses (-is:reply) côté serveur.
    """
    since = _since_date()
    cursor = None
    fetched = 0

    while fetched < max_results:
        params = {
            "q": f"from:{handle} since:{since} -is:retweet -is:reply",
            "product": "Latest",
        }
        if cursor:
            params["cursor"] = cursor

        try:
            data = await _getx_get(session, "/twitter/tweet/advanced_search", params)
        except aiohttp.ClientResponseError as e:
            logger.error(f"GetXAPI error @{handle}: {e.status} {e.message}")
            return
        except Exception as e:
            logger.error(f"GetXAPI network error @{handle}: {e}")
            return

        tweets = data.get("tweets") or []
        if not tweets:
            break

        for t in tweets:
            tweet = _normalise_tweet(t, handle)
            fetched += 1
            yield tweet

        # Pagination
        has_more = data.get("has_more", False)
        cursor   = data.get("next_cursor")
        if not has_more or not cursor:
            break


def _normalise_tweet(raw: dict, author: str) -> dict:
    """Normalise un tweet GetXAPI vers notre format interne."""
    tweet_id = raw.get("id") or raw.get("tweet_id", "")
    handle   = raw.get("author", {}).get("userName") or author
    text     = raw.get("text") or raw.get("full_text") or ""

    created = raw.get("createdAt") or raw.get("created_at") or raw.get("publishedAt")
    if isinstance(created, (int, float)):
        created = datetime.fromtimestamp(created, tz=timezone.utc).isoformat()

    return {
        "source_type":  "twitter",
        "source_url":   f"https://twitter.com/{handle}/status/{tweet_id}",
        "author":       handle,
        "content":      text,
        "published_at": created,
    }


# ─── Job principal ────────────────────────────────────────────────────────────

async def scrape_accounts(max_per_account: int = 50) -> dict:
    """
    Scrape tous les comptes de WATCH_ACCOUNTS.
    Récupère uniquement les tweets originaux des dernières 24h via since:.
    Retourne {new, skipped, errors}.
    """
    if not WATCH_ACCOUNTS:
        logger.info("Aucun compte configuré — rien à scraper")
        return {"new": 0, "skipped": 0, "errors": []}

    if not GETXAPI_KEY:
        logger.error("GETXAPI_KEY manquante — scrape annulé")
        return {"new": 0, "skipped": 0, "errors": ["GETXAPI_KEY not set"]}

    run_id = log_scrape_start("twitter")
    new_total, skipped_total, errors = 0, 0, []

    async with aiohttp.ClientSession() as session:
        for account in WATCH_ACCOUNTS:
            handle = account["handle"]
            try:
                async for tweet in fetch_user_tweets(session, handle, max_results=max_per_account):
                    row_id = insert_entry(
                        source_type=tweet["source_type"],
                        source_url=tweet["source_url"],
                        author=tweet["author"],
                        content=tweet["content"],
                        published_at=tweet["published_at"],
                    )
                    if row_id:
                        new_total += 1
                    else:
                        skipped_total += 1
            except Exception as e:
                msg = f"@{handle}: {e}"
                logger.error(msg)
                errors.append(msg)

    log_scrape_finish(run_id, new_total, errors)
    logger.info(
        f"Scrape terminé — {len(WATCH_ACCOUNTS)} comptes · "
        f"new={new_total} skipped={skipped_total} errors={len(errors)}"
    )
    return {"new": new_total, "skipped": skipped_total, "errors": errors}
