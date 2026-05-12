"""
Daily digest generator.
Pulls recent entries from the last 24h and asks Claude to synthesise
a structured memo for Andreas.
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
        category = e.get("source_category", "?")
        title = e.get("title", "")
        content = (e.get("content") or "")[:300]
        pub = (e.get("published_at") or "")[:10]
        lines.append(f"[{source} | {category} | {pub}]\n{title}\n{content}")

    combined = "\n\n---\n\n".join(lines)
    label = f"last {hours}h"

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        system="""You are a strategic analyst for Blockchain.com.
Write crisp, direct memos for the leadership team. No marketing language.
Concrete facts, named companies, numbers when available. No fluff.""",
        messages=[{
            "role": "user",
            "content": f"""Here are the strategic watch entries from the {label}:

{combined}

---

Write a digest structured EXACTLY like this - nothing else:

📊 *Strategic Watch — {label}*

*Key information*
3 to 5 bullets. The most important factual developments. Named companies, numbers, concrete events. No opinions.

*Innovation*
2 to 3 bullets. New products, launches, technical moves, or business model shifts worth knowing about.

*Actionable for Blockchain.com*
2 to 3 bullets. Specific, concrete actions or strategic responses Blockchain.com should consider. Name the relevant Blockchain.com product or team when possible (Institutional Services, wallet, OTC desk, SnapMarkets, June AI).

Rules:
- Be direct. No hype. No "this is exciting" or "this represents a significant opportunity".
- Do not invent facts not present in the entries.
- Max 2500 characters total.
- Use plain Markdown bullet points (•)."""
        }]
    )
    return response.content[0].text.strip()
