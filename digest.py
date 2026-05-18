"""
Daily digest generator.
Pulls recent entries from the last 24h and asks Claude to synthesise
a strategic memo for Andreas.
Company blogs are prioritised over media sources.
"""
import os
import logging
from anthropic import Anthropic
from database import get_recent_entries

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-20250514"


def generate_daily_digest(hours: int = 24) -> str:
    entries = get_recent_entries(hours=hours, limit=150)
    if not entries:
        return f"No entries in the last {hours}h."

    # Split company blogs vs media
    company_entries = [e for e in entries if e.get("source_description") in ("company", "research")]
    media_entries   = [e for e in entries if e.get("source_description") == "media"]

    def format_entry(e, content_limit=500):
        source  = e.get("source_name", "?")
        title   = e.get("title", "")
        content = (e.get("content") or "")[:content_limit]
        pub     = (e.get("published_at") or "")[:10]
        tags    = e.get("tags", "")
        return f"[{source} | {pub} | {tags}]\n{title}\n{content}"

    # Company blogs: all articles, full content
    company_block = "\n\n---\n\n".join(format_entry(e, 600) for e in company_entries)

    # Media: cap at 15 articles to avoid drowning company news
    media_block = "\n\n---\n\n".join(format_entry(e, 400) for e in media_entries[:15])

    label = f"last {hours}h"

    prompt = f"""Here are today's strategic watch entries ({label}).

=== COMPANY & RESEARCH BLOGS (PRIMARY SOURCE — prioritise these) ===
These are direct announcements from crypto companies (competitors, partners, ecosystem players).
They represent what companies are actually doing and should drive most of the memo.

{company_block if company_block else "No company articles today."}

=== INDUSTRY NEWS — The Block (SECONDARY SOURCE — complement only) ===
Use these to add market context, regulatory news, or macro events not covered by company blogs.
Do not let these dominate the memo.

{media_block if media_block else "No media articles today."}

---
Write a strategic intelligence memo following the format guidelines."""

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system="""You are a strategic analyst for Blockchain.com, a crypto company with retail exchange, institutional OTC, custody, staking, and prime brokerage products.
Your job is to write a daily intelligence memo for the leadership team. Rules:
- Write like a smart colleague summarising the day's news in a Slack message, not a consulting report
- Short sentences. Plain English. No buzzwords, no "leverage", no "ecosystem", no "space"
- Concrete facts only: company names, numbers, dates. No vague statements
- If something is important for Blockchain.com, say WHY in one plain sentence
- Do not invent or extrapolate facts not present in the source material""",
        messages=[{
            "role": "user",
            "content": f"""{prompt}

Format guidelines:
- Start with the emoji header: 📊 *Strategic Watch — {label}*
- Choose 2 to 4 sections based on what the news actually warrants today. Don't force sections that aren't supported by the data.
- Possible section titles (use only what's relevant): Key Developments, Regulatory, Stablecoins, Institutional Moves, Innovation, Market Structure, Actionable for Blockchain.com
- Always end with an "Actionable for Blockchain.com" section with 2-3 points. Each point = one sentence explaining what to watch or do, and why it matters. No corporate language.
- Use bullet points (•) for all items
- Max 2500 characters total
- No repetition across sections — if something fits in one section, don't mention it again elsewhere"""
        }]
    )
    return response.content[0].text.strip()
