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
RELEVANCE_THRESHOLD = float(os.environ.get("RELEVANCE_THRESHOLD", "0.5"))
ENRICH_BATCH_SIZE   = int(os.environ.get("ENRICH_BATCH_SIZE", "20"))
MODEL               = "claude-haiku-4-5-20251001"


# ─── Author registry ──────────────────────────────────────────────────────────

AUTHOR_REGISTRY: dict[str, dict] = {}


def load_author_registry(targets_path: str = "targets.json"):
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


# ─── Blockchain.com context ───────────────────────────────────────────────────

BLOCKCHAIN_COM_CONTEXT = """
Blockchain.com is a full-stack crypto financial services company founded in 2011. 
94M+ wallets created. $1.2T+ in transactions processed. Present in 150+ countries.

CORE PRODUCTS & BUSINESS LINES:

1. RETAIL WALLET & EXCHANGE
   - Self-custody DeFi Wallet (non-custodial, user controls private keys)
   - Hosted wallet (custodial, 39M verified users)
   - Spot trading & brokerage (executes trades using internal liquidity)
   - 5,700+ tradable assets across BTC, ETH, and major networks
   - Staking on supported PoS chains
   - Block explorer (original product since 2011)

2. INSTITUTIONAL SERVICES
   - 24/7 OTC trading desk (spot + options, block trades, minimal slippage)
   - Prime brokerage & custody (institutional-grade security)
   - Lending, borrowing, yield generation
   - Treasury management for token foundations, family offices, hedge funds
   - Token launch & listing services (liquidity provision for new tokens)
   - Co-marketing & retail distribution for crypto projects

3. INNOVATION PRODUCTS (launched 2025-2026)
   - June (askjune.ai): privacy-first AI assistant launched Aug 2025. 500K+ accounts, 650K weekly interactions. No conversation storage, no training on user data. Supports GPT-5, Claude, Gemini, DeepSeek. June Pro payable in crypto. Being integrated into the Blockchain.com wallet as an AI financial advisor.
   - SnapMarkets (snapmarkets.com): prediction market platform launched May 6 2026. 30-second BTC price prediction rounds starting at $1. Incubated by Blockchain.com, deployed on Arbitrum. Not available in US/UK yet. Direct competitors: Kalshi, Polymarket, Robinhood Predictions, Crypto.com OG.

4. STRATEGIC PRIORITIES IN 2026
   - June AI: grow user base, wallet integration, AI agent payments
   - SnapMarkets: expand to new assets beyond BTC, navigate US/UK regulatory path
   - Stablecoins & payments infrastructure
   - RWA (Real World Assets) tokenization
   - Regulatory compliance as competitive advantage (MiCA, FCA, SEC, CLARITY Act)
   - Institutional adoption acceleration (ETFs, TradFi convergence)
   - Self-custody UX improvements & account abstraction

SCORING GUIDE — be precise, not generous:

SCORE 0.8–1.0 (direct strategic impact):
- Prediction markets: regulation, new entrants, major platforms (Kalshi, Polymarket, Robinhood), legal developments
- AI x crypto: agentic payments platforms, AI agents executing transactions, AI wallet features, AI x DeFi infrastructure
- Competitor moves: Coinbase, Kraken, Binance, Gemini, Bullish, BitGo launching new products, acquiring companies, or reporting financials
- Stablecoin regulation or major stablecoin developments (USDC, USDT, PYUSD, new entrants, Fidelity stablecoin)
- Crypto regulation with direct business impact: US CLARITY Act, MiCA enforcement, SEC actions on exchanges/wallets, FCA rulings
- Institutional adoption: ETF flows, TradFi entering crypto (banks, asset managers), prime brokerage deals
- RWA tokenization milestones, major partnerships or regulatory approvals
- Security incidents at major exchanges or wallets (competitive intelligence + risk)
- Major exchange acquisitions or M&A in crypto
- Bitcoin or Ethereum protocol-level changes affecting wallets or exchanges
- Token launches with major institutional backing (listing intelligence)

SCORE 0.5–0.8 (relevant, worth monitoring):
- DeFi protocol news with institutional angle (lending, yield, liquidity)
- On-chain data showing significant institutional flows or whale activity
- Macro signals affecting crypto markets (Fed rates, dollar, ETF flows)
- Layer-2 scaling news relevant to wallet UX or transaction costs
- Crypto VC funding rounds (strategic landscape intelligence)
- Self-custody trends, hardware wallet developments
- Payments companies entering crypto (PayPal, Stripe, Visa)
- Regulatory developments in non-US jurisdictions (EU, UK, Singapore, UAE)

SCORE 0.2–0.5 (low signal, marginal relevance):
- General crypto market price commentary without institutional context
- DeFi protocol governance votes without major impact
- NFT news (Blockchain.com had NFT beta with OpenSea but it's not a core focus)
- Gaming and metaverse crypto unless infrastructure angle
- Layer-1 launches without significant market share
- Developer tooling with no direct competitive relevance

SCORE 0.0–0.2 (irrelevant, filter out):
- Meme coins, celebrity tokens, pump-and-dump schemes
- Pure NFT art drops with no infrastructure angle
- Personal opinions without actionable intelligence
- Sports, entertainment, politics unrelated to crypto regulation
- Duplicate/reposted content with no new information
- Promotional marketing content from competitors (product ads, job listings)

IMPORTANT SCORING RULES:
- A breaking regulatory news from Eleanor Terrett should score 0.8+
- A Coinbase product launch should score 0.85+
- A generic "Bitcoin is up today" tweet should score 0.1–0.2
- A competitor acquisition (e.g. Kraken buys Reap) should score 0.9+
- An on-chain data thread from Glassnode about institutional BTC flows: 0.65
- A ZachXBT hack investigation on a major exchange: 0.75
- An a16z crypto investment thesis post: 0.55
- A prediction markets regulatory development (Kalshi, CFTC): 0.85+
- An AI agent executing crypto payments news: 0.80+
- A Polymarket or Kalshi new product launch: 0.80+
- Any news about June (askjune.ai) competitors (privacy AI, crypto AI assistants): 0.75+
- Any news about SnapMarkets competitors (Robinhood Predictions, Crypto.com OG, Polymarket): 0.80+
- Arbitrum ecosystem news (SnapMarkets runs on Arbitrum): 0.50
"""

SYSTEM_PROMPT = f"""You are a strategic intelligence analyst for Blockchain.com.
You receive raw tweets or blog articles from crypto industry sources and must return structured JSON enrichment.

{BLOCKCHAIN_COM_CONTEXT}

Return ONLY valid JSON — no markdown, no preamble, no explanation.
"""


def _get_author_context(handle: str) -> str:
    meta = AUTHOR_REGISTRY.get(handle.lower().lstrip("@"))
    if not meta:
        return ""
    category_label = {
        "competitor":   "⚠️  COMPETITOR",
        "journalist":   "📰 JOURNALIST",
        "media":        "📰 MEDIA",
        "protocol":     "🔧 PROTOCOL",
        "institutional":"🏦 INSTITUTIONAL",
        "vc":           "💼 VC/INVESTOR",
        "research":     "🔬 RESEARCH/DATA",
        "tradfi":       "🏛️  TRADFI",
    }.get(meta["category"], meta["category"].upper())
    tags_str = ", ".join(meta["tags"]) if meta["tags"] else ""
    return f"Author context: {category_label} — {meta['name']} [{tags_str}]. {meta['notes']}"


def _build_user_prompt(entry: dict) -> str:
    source  = entry.get("source_type", "unknown")
    author  = entry.get("author", "unknown")
    content = entry.get("content", "")[:2000]
    author_ctx = _get_author_context(author)

    author_line = f"Author: @{author}"
    if author_ctx:
        author_line += f"\n{author_ctx}"

    return f"""Source: {source}
{author_line}
Content: {content}

Return this JSON:
{{
  "tags": ["tag1", "tag2"],          // 2–5 lowercase topic tags
  "relevance_score": 0.0,            // float 0.0–1.0 — use the scoring guide strictly
  "summary": "One or two sentences." // plain English, mention key facts and who said it
}}"""


# ─── Single entry enrichment ──────────────────────────────────────────────────

async def enrich_entry(client: AsyncAnthropic, entry: dict) -> dict | None:
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_prompt(entry)}],
        )

        raw_text = response.content[0].text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        raw_text = raw_text.strip()

        enrichment = json.loads(raw_text)

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


# ─── Batch enrichment ─────────────────────────────────────────────────────────

async def run_enrichment_batch(batch_size: int = ENRICH_BATCH_SIZE) -> dict:
    pending = get_pending_enrichment(limit=batch_size)
    if not pending:
        logger.info("No pending entries to enrich")
        return {"processed": 0, "kept": 0, "filtered": 0, "errors": 0, "tokens_used": 0}

    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    stats  = {"processed": 0, "kept": 0, "filtered": 0, "errors": 0, "tokens_used": 0}
    sem    = asyncio.Semaphore(5)

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

    cost_usd = (stats["tokens_used"] / 1_000_000) * 2.0
    logger.info(
        f"Enrichment done — processed={stats['processed']} kept={stats['kept']} "
        f"filtered={stats['filtered']} errors={stats['errors']} "
        f"tokens={stats['tokens_used']} (~${cost_usd:.4f})"
    )
    stats["estimated_cost_usd"] = round(cost_usd, 5)
    return stats


# ─── Cost estimator ───────────────────────────────────────────────────────────

def estimate_cost(n_entries: int, avg_content_chars: int = 400) -> dict:
    system_tokens  = len(SYSTEM_PROMPT) // 4
    content_tokens = avg_content_chars // 4
    output_tokens  = 80
    input_total    = (system_tokens + content_tokens) * n_entries
    output_total   = output_tokens * n_entries
    total_tokens   = input_total + output_total
    cost_haiku     = (input_total / 1_000_000 * 0.80) + (output_total / 1_000_000 * 4.00)
    return {
        "n_entries": n_entries,
        "estimated_total_tokens": total_tokens,
        "estimated_cost_usd_haiku": round(cost_haiku, 5),
        "note": "Actual cost varies by content length",
    }
