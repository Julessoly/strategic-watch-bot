"""
Daily digest generator.
Pulls recent entries from the last 24h and asks Claude to synthesise
a strategic memo for Andreas.
Company blogs are prioritised over media sources.
"""
import os
import logging
from anthropic import Anthropic
from database import get_recent_entries_by_published

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-20250514"


def generate_daily_digest(hours: int = 24) -> str:
    entries = get_recent_entries_by_published(hours=hours, limit=150)
    if not entries:
        return f"No entries in the last {hours}h."

    # Split company blogs vs media vs fundraising
    company_entries   = [e for e in entries if e.get("source_description") in ("company", "research")]
    media_entries     = [e for e in entries if e.get("source_description") == "media"]
    fundraising_entries = [e for e in entries if e.get("source_category") == "fundraising"]

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

    # Fundraising: all tweets from Crypto Dealflow
    fundraising_block = "\n\n---\n\n".join(format_entry(e, 300) for e in fundraising_entries)

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

=== FUNDRAISING (ALWAYS include as a separate section at the end) ===
These are fundraising announcements from @Crypto_Dealflow. Always include ALL of them in a dedicated 💰 Fundraising section at the end of the memo, even if there are many. List each as a bullet with company name, amount, round, and sector.

{fundraising_block if fundraising_block else "No fundraising news today."}

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
- Start with the header: 📊 *STRATEGIC WATCH — {label.upper()}*
- Group bullets under 2-4 dynamic section titles with relevant emojis. Section titles must be **bold** with a relevant emoji, formatted exactly like this: **🏦 Institutional Moves**. Choose titles based on what actually happened today — don't use fixed categories.
- Each bullet = just the fact (company, number, event). One sentence, no context sentence after each bullet.
- After a section's bullets, add a brief analysis line starting with ↳ only if there is something genuinely insightful to say about the section as a whole — skip it otherwise.
- Order sections by relevance — company announcements first
- No "Actionable" section
- Always end with a **💰 Fundraising** section. From the fundraising entries, select only the most relevant ones for Blockchain.com — focus on AI, payments, stablecoins, custody, exchanges, DeFi, institutional infrastructure. Skip generic or unrelated raises. For each bullet: bold company name, amount, round type, then a one-sentence description of what the company does — use your knowledge or the "company:" tag from the tags field. If you don't know the company, use the tweet context to infer what they do. Format: "• **CompanyName** ($Xm, Series A) — one sentence on what they do." If no relevant fundraising entries, skip this section.
- Max 3000 characters total"""
        }]
    )
    return response.content[0].text.strip()
