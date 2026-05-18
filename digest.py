"""
Daily digest generator.
Pulls recent entries from the last 24h and asks Claude to synthesise
a strategic memo for Andreas.
"""
import os
import logging
from anthropic import Anthropic
from database import get_recent_entries

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-20250514"


def generate_daily_digest(hours: int = 24) -> str:
    entries = get_recent_entries(hours=hours, limit=80)
    if not entries:
        return f"No entries in the last {hours}h."

    lines = []
    for e in entries:
        source = e.get("source_name", "?")
        title = e.get("title", "")
        content = (e.get("content") or "")[:400]
        pub = (e.get("published_at") or "")[:10]
        tags = e.get("tags", "")
        lines.append(f"[{source} | {pub} | {tags}]\n{title}\n{content}")

    combined = "\n\n---\n\n".join(lines)
    label = f"last {hours}h"

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system="""You are a strategic analyst for Blockchain.com, a crypto company with retail exchange, institutional OTC, custody, staking, and prime brokerage products.

Your job is to write a daily intelligence memo for the leadership team. Rules:
- Direct, factual, no marketing language or hype
- Named companies, concrete numbers, specific events
- Do not invent or extrapolate facts not present in the source material
- The memo should feel like a senior analyst wrote it, not a template filler""",
        messages=[{
            "role": "user",
            "content": f"""Here are today's strategic watch entries ({label}):

{combined}

---

Write a strategic intelligence memo. 

Format guidelines:
- Start with the emoji header: 📊 *Strategic Watch — {label}*
- Choose 2 to 4 sections based on what the news actually warrants today. Don't force sections that aren't supported by the data.
- Possible section titles (use only what's relevant): Key Developments, Regulatory, Stablecoins, Institutional Moves, Innovation, Market Structure, Actionable for Blockchain.com
- Always end with an "Actionable for Blockchain.com" section with 2-3 specific, concrete recommendations tied to actual news from today. Name relevant Blockchain.com products or teams when possible (Institutional Services, OTC desk, wallet, SnapMarkets, June AI).
- Use bullet points (•) for all items
- Max 2500 characters total
- No repetition across sections — if something fits in one section, don't mention it again elsewhere"""
        }]
    )
    return response.content[0].text.strip()
