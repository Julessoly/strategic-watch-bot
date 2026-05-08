"""
Daily digest generator.
Pulls kept entries from the last 24h and asks Claude Sonnet to synthesise
a structured memo for Andreas.
"""

import os
import logging
from anthropic import Anthropic

from database import get_recent_kept

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-20250514"  # Sonnet for digest quality


def generate_daily_digest(hours: int = 24) -> str:
    entries = get_recent_kept(hours=hours, limit=80)
    if not entries:
        return f"📭 No relevant entries in the last {hours}h."

    import json
    lines = []
    for e in entries:
        tags = json.loads(e["tags"]) if e.get("tags") else []
        tag_str = ", ".join(tags[:3]) if tags else "—"
        score = f"{e['relevance_score']:.2f}" if e.get("relevance_score") else "?"
        author = e.get("author", "?")
        src = e.get("source_type", "?")
        summary = e.get("summary") or e.get("content", "")[:200]
        lines.append(f"[{src} | @{author} | {tag_str} | score={score}]\n{summary}")

    combined = "\n\n---\n\n".join(lines)
    label = f"last {hours}h"

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        system="""You are a strategic analyst for Blockchain.com.
Write crisp, direct memos for Andreas (CEO). No marketing language.
Concrete facts, named companies, actionable signals only.""",
        messages=[{
            "role": "user",
            "content": f"""Here are the strategic watch entries from {label}:

{combined}

---

Write a digest structured exactly like this:

📊 *Strategic Watch — {label}*

**Key signals** (3–5 bullets, most important developments)

**Recurring themes** (2–3 patterns you see across entries)

**Angles for Blockchain.com** (1–3 concrete, specific angles — name products)

**Watch list** (companies / projects worth tracking)

Be direct. No hype. Max 2800 characters."""
        }]
    )
    return response.content[0].text.strip()
