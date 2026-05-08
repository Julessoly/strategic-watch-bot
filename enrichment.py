"""
AI enrichment pipeline.
Claude reads each raw entry and produces:
  - tags (2–5 topic tags)
  - relevance_score (0.0 – 1.0 vs Blockchain.com)
  - summary (1–2 sentences)

Token costs are tracked per entry and stored in the DB.
Entries below RELEVANCE_THRESHOLD are marked kept=0 and ignored downstream.
"""

import os
import json
import logging
import asyncio
from anthropic import AsyncAnthropic

from database import get_pending_enrichment, update_enrichment

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
RELEVANCE_THRESHOLD = float(os.environ.get("RELEVANCE_THRESHOLD", "0.3"))
ENRICH_BATCH_SIZE   = int(os.environ.get("ENRICH_BATCH_SIZE", "20"))
MODEL               = "claude-haiku-4-5"  # Fast + cheap for bulk enrichment


# ─── Author registry ─────────────────────────────────────────────────────────
# Loaded from config/targets.json at startup.
# Maps handle (lowercase) → account metadata dict.
# Used to inject author context into the enrichment prompt.

AUTHOR_REGISTRY: dict[str, dict] = {}


def load_author_registry(targets_path: str = "config/targets.json"):
    """Build a handle → metadata lookup from targets.json. Call once at startup."""
    if not os.path.exists(targets_path):
        return
    with open(targets_path) as f:
        data = json.load(f)
    for account in data.get("accounts", []):
        if not isinstance(account, dict) or "handle" not in account:
            continue
        handle = account["handle"].lower().lstrip("@")
        AUTHOR_REGISTRY[handle] = {
            "name":     account.get("name", account["handle"]),
            "category": account.get("category", "unknown"),
            "tags":     account.get("tags", []),
            "notes":    account.get("notes", ""),
        }
    logger.info(f"Author registry loaded: {len(AUTHOR_REGISTRY)} accounts")

# Approximate cost reference (as of 2025):
# Haiku input:  $0.80 / 1M tokens
# Haiku output: $4.00 / 1M tokens


# ─── Blockchain.com relevance map ─────────────────────────────────────────────
# Used in the system prompt so Claude scores against our actual business.

BLOCKCHAIN_COM_CONTEXT = """
Blockchain.com is a crypto company with these core products and interests:
- Retail wallet (self-custody, 100M+ users)
- Exchange (spot trading, institutional desk)
- DeFi wallet (Web3 browser, dApps)
- Institutional services (prime brokerage, custody, OTC)
- Blockchain.com Institutional (analytics, data)

Highly relevant topics (score 0.7–1.0):
- Bitcoin, Ethereum, Layer-2 scaling, Solana
- Stablecoins, tokenised assets, RWA
- DeFi protocols, DEXs, lending, yield
- Crypto regulation (SEC, MiCA, FCA, MAS)
- Institutional adoption, ETFs, TradFi/crypto convergence
- Wallet UX, self-custody, account abstraction
- Exchange competition (Coinbase, Binance, Kraken, OKX)
- Crypto fundraising rounds (strategic intelligence)
- Agentic payments, AI x crypto
- Privacy, ZK proofs (when product-relevant)

Moderately relevant (score 0.4–0.7):
- NFTs, gaming (if institutional or infrastructure angle)
- Layer-1 launches (if significant market share)
- Macro economy affecting crypto (rates, dollar, ETF flows)
- Developer tooling, SDKs (indirect competitor intel)

Low relevance (score 0.0–0.4):
- Pure meme coins, celebrity tokens with no substance
- NFT drops, digital art with no infrastructure angle
- Unrelated tech, politics, sports
- Duplicate/rehash of already-known info
"""


# ─── Prompt ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""You are a strategic intelligence analyst for Blockchain.com.
You receive raw tweets or news articles and must return structured JSON enrichment.

{BLOCKCHAIN_COM_CONTEXT}

Return ONLY valid JSON — no markdown, no preamble, no explanation.
"""

def _get_author_context(handle: str) -> str:
    """Return a one-line author context string for the prompt, or empty string."""
    meta = AUTHOR_REGISTRY.get(handle.lower().lstrip("@"))
    if not meta:
        return ""
    category_label = {
        "competitor":   "⚠️  COMPETITOR",
        "journalist":   "📰 JOURNALIST",
        "media":        "📰 MEDIA",
        "protocol":     "🔧 PROTOCOL FOUNDER/TEAM",
        "institutional":"🏦 INSTITUTIONAL",
        "vc":           "💼 VC/INVESTOR",
        "research":     "🔬 RESEARCH/DATA",
        "tradfi":       "🏛️  TRADFI",
    }.get(meta["category"], meta["category"].upper())
    tags_str = ", ".join(meta["tags"]) if meta["tags"] else ""
    notes = meta["notes"]
    return f"Author context: {category_label} — {meta['name']} [{tags_str}]. {notes}"


def _build_user_prompt(entry: dict) -> str:
    source = entry.get("source_type", "unknown")
    author = entry.get("author", "unknown")
    content = entry.get("content", "")[:1500]  # cap to save tokens
    author_ctx = _get_author_context(author)

    author_line = f"Author: @{author}"
    if author_ctx:
        author_line += f"\n{author_ctx}"

    return f"""Source: {source}
{author_line}
Content: {content}

Return this JSON structure:
{{
  "tags": ["tag1", "tag2"],          // 2–5 lowercase topic tags, e.g. "stablecoins", "regulation", "defi"
  "relevance_score": 0.0,            // float 0.0–1.0 for Blockchain.com relevance — boost score if author is a competitor or key signal source
  "summary": "One or two sentences." // plain English, no hype — mention who said it if it adds context (e.g. "Coinbase CEO said...")
}}"""


# ─── Single entry enrichment ──────────────────────────────────────────────────

async def enrich_entry(client: AsyncAnthropic, entry: dict) -> dict | None:
    """
    Call Claude Haiku to enrich one entry.
    Returns enrichment dict or None on failure.
    """
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_prompt(entry)}],
        )

        raw_text = response.content[0].text.strip()

        # Strip any accidental markdown fences
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        enrichment = json.loads(raw_text)

        # Validate and clamp
        tags = enrichment.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).lower().strip() for t in tags[:5]]

        score = float(enrichment.get("relevance_score", 0.0))
        score = max(0.0, min(1.0, score))

        summary = str(enrichment.get("summary", "")).strip()

        total_tokens = response.usage.input_tokens + response.usage.output_tokens

        return {
            "tags": tags,
            "relevance_score": score,
            "summary": summary,
            "ai_cost_tokens": total_tokens,
        }

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error for entry {entry['id']}: {e}")
        return None
    except Exception as e:
        logger.error(f"Enrichment error for entry {entry['id']}: {e}")
        return None


# ─── Batch enrichment job ─────────────────────────────────────────────────────

async def run_enrichment_batch(batch_size: int = ENRICH_BATCH_SIZE) -> dict:
    """
    Pull up to batch_size pending entries, enrich, write back.
    Returns stats: {processed, kept, filtered, errors, tokens_used}
    """
    pending = get_pending_enrichment(limit=batch_size)
    if not pending:
        logger.info("No pending entries to enrich")
        return {"processed": 0, "kept": 0, "filtered": 0, "errors": 0, "tokens_used": 0}

    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    stats = {"processed": 0, "kept": 0, "filtered": 0, "errors": 0, "tokens_used": 0}

    # Process concurrently but with a small semaphore to avoid rate limits
    sem = asyncio.Semaphore(5)

    async def _process(entry):
        async with sem:
            result = await enrich_entry(client, entry)
            if result is None:
                stats["errors"] += 1
                return
            update_enrichment(
                entry_id=entry["id"],
                tags=result["tags"],
                relevance_score=result["relevance_score"],
                summary=result["summary"],
                ai_cost_tokens=result["ai_cost_tokens"],
                relevance_threshold=RELEVANCE_THRESHOLD,
            )
            stats["processed"] += 1
            stats["tokens_used"] += result["ai_cost_tokens"]
            if result["relevance_score"] >= RELEVANCE_THRESHOLD:
                stats["kept"] += 1
            else:
                stats["filtered"] += 1

    await asyncio.gather(*[_process(e) for e in pending])

    # Estimated cost (Haiku pricing)
    cost_usd = (stats["tokens_used"] / 1_000_000) * 2.0  # ~$2/1M blended
    logger.info(
        f"Enrichment done — processed={stats['processed']} kept={stats['kept']} "
        f"filtered={stats['filtered']} errors={stats['errors']} "
        f"tokens={stats['tokens_used']} (~${cost_usd:.4f})"
    )
    stats["estimated_cost_usd"] = round(cost_usd, 5)
    return stats


# ─── Cost estimator ───────────────────────────────────────────────────────────

def estimate_cost(n_entries: int, avg_content_chars: int = 400) -> dict:
    """
    Rough cost estimate before running enrichment.
    Input tokens ≈ system_prompt + content / 4 chars per token.
    Output tokens ≈ 80 (short JSON response).
    """
    system_tokens = len(SYSTEM_PROMPT) // 4
    content_tokens = avg_content_chars // 4
    output_tokens = 80

    input_total = (system_tokens + content_tokens) * n_entries
    output_total = output_tokens * n_entries
    total_tokens = input_total + output_total

    cost_haiku = (input_total / 1_000_000 * 0.80) + (output_total / 1_000_000 * 4.00)

    return {
        "n_entries": n_entries,
        "estimated_total_tokens": total_tokens,
        "estimated_cost_usd_haiku": round(cost_haiku, 5),
        "note": "Actual cost varies by content length",
    }
