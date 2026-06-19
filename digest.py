"""
Daily digest generator.
Pulls recent entries from the last 24h and asks Claude to synthesise
a strategic memo for Andreas.
Company blogs are prioritised over media sources.
"""
import os
import re
import html as _html
import logging
from anthropic import Anthropic
from database import get_recent_entries_by_published, get_digest_stats, get_past_watches
from datetime import date
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Opus for synthesis-heavy tasks (digest, /ask); Haiku for cheap/fast tagging + dedup
MODEL_DIGEST = "claude-opus-4-8"
MODEL_ENRICH = "claude-haiku-4-5-20251001"


def md_to_telegram_html(text: str) -> str:
    text = _html.escape(text)
    # remove markdown horizontal rules (lines that are only --- / *** / ___)
    text = re.sub(r'(?m)^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$\n?', '', text)
    # turn markdown list markers (- or *) at line start into • bullets (keeps indentation)
    text = re.sub(r'(?m)^([ \t]*)[-*][ \t]+', r'\1• ', text)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^)\s]+)\)', r'<a href="\2">\1</a>', text)
    text = re.sub(r'\*\*([^*\n]+)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'\*([^*\n]+)\*', r'<b>\1</b>', text)
    return text


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

    # Media: pass ALL articles (no cap) — model will dedupe against company blogs
    # and decide what's relevant. Slightly more content to help dedup judgement.
    media_block = "\n\n---\n\n".join(format_entry(e, 500) for e in media_entries)

    # Fundraising: all tweets from Crypto Dealflow
    fundraising_block = "\n\n---\n\n".join(format_entry(e, 300) for e in fundraising_entries)

    past_watches = get_past_watches(days=7)
    past_watches_text = ""
    if past_watches:
        past_watches_text = "\n\n=== PAST 7 DAYS WATCHES ===\n" + "\n\n---\n\n".join(past_watches)
    label = f"{date.today()}"

    stats = get_digest_stats()

    header_stats = f"📊 {stats['read']} articles read"

    prompt = f"""
Here are the past watches: 

=== PAST WATCHES ===
{past_watches_text}
    
Here are today's strategic watch entries ({label}).

=== COMPANY & RESEARCH BLOGS (PRIMARY SOURCE — direct from the company) ===
These are direct announcements from crypto companies (competitors, partners, ecosystem players).
When a company blog and a media article cover the same event, prefer the company source.

{company_block if company_block else "No company articles today."}

=== INDUSTRY NEWS — The Block (RICH SOURCE — most events come from here) ===
These cover the broader market: company actions, regulation, exploits, fundraises, macro.
Many of these will NOT have a matching company blog and must be included on their own.
DEDUPLICATION RULE: If a Block article covers the same event as a company blog above
(e.g. "Kraken Bitcoin Vault" in The Block + Kraken blog announcing Bitcoin Vault),
merge them into ONE bullet — do not produce two bullets for the same event.
Use the company blog as the source of truth and let The Block add market context if useful.

{media_block if media_block else "No media articles today."}

=== FUNDRAISING (ALWAYS include as a separate section at the end) ===
These are fundraising announcements from @Crypto_Dealflow. Always include ALL of them in a dedicated Fundraising section at the end of the memo, even if there are many. List each as a bullet with company name, amount, round, and sector.

{fundraising_block if fundraising_block else "No fundraising news today."}

---
Write a strategic intelligence memo following the format guidelines."""

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL_DIGEST,
        max_tokens=4000,
        system="""You are a strategic analyst for Blockchain.com, a crypto company with retail exchange, institutional OTC, custody, staking, and prime brokerage products.
Your job is to write a daily intelligence memo for the leadership team. Rules:
- Write like a smart colleague summarising the day's news in a Slack message, not a consulting report
- Aim for comprehensive coverage: leadership wants to see EVERY meaningful event from the day, not a curated highlight reel. Err on the side of including rather than skipping.
- Short sentences. Plain English. No buzzwords, no "leverage", no "ecosystem", no "space"
- Concrete facts only: company names, numbers, dates. No vague statements
- Bullets state facts ONLY — never append analysis, significance, or a "why it matters" clause inside a bullet. Any interpretation (including why something matters for Blockchain.com) goes ONLY in the section's ↳ analysis line.
- Do not invent or extrapolate facts not present in the source material
- When two source entries describe the same event, merge into one bullet (do not duplicate)
- CLUSTERING: If a single company (e.g., Gate.io) launches multiple minor features or products on the same day, DO NOT write a separate bullet for each one. Merge them into a single, comma-separated bullet summarizing the company's overall product push.
- DO NOT REPEAT anything, if you see an event in TODAY'S source material that was ALREADY REPORTED in the "PAST WATCHES" section, you MUST completely ignore it today. Do not write a bullet for it. Only report genuinely new developments.""",
        messages=[{
            "role": "user",
            "content": f"""{prompt}

Format guidelines:
- Start with the header exactly like this (do not change it):
*STRATEGIC WATCH — {label.upper()}*
{header_stats}
- COVERAGE: Aim for near-exhaustive coverage. Mention every distinct event from the source material. Only skip clear noise: minor protocol version upgrades (e.g. "Bybit supports Network X v1.7.0"), illiquid token delistings, marketing/sponsorship events, internal change logs, and routine fee/UI updates. Everything else gets a bullet.
- DEDUPLICATION: If two source entries describe the same event (typically one company blog + one The Block article), produce ONE bullet, not two. The company blog is the source of truth.
- Group bullets under dynamic section titles (typically 4-7 sections depending on what happened). Section titles must be **bold** with no emojis, formatted exactly like this: **Institutional Moves**. Choose titles based on what actually happened today — don't use fixed categories. Do NOT use emojis anywhere in the memo (no emojis in headers, section titles, bullets, or analysis lines).
- Each bullet = ONE purely factual sentence: who (company), what (action/event), the numbers, and the date. Nothing else. Do NOT append analysis, interpretation, significance, or framing such as "signals…", "positioning itself as…", "a direct continuation of…", "relevant to our strategy…", "raising its profile…". If you catch yourself explaining why a fact matters inside a bullet, cut that clause — it belongs in the ↳ line. An em dash is allowed only when it is part of the fact itself, never to bolt on a comment.
- End each section with exactly one short analysis line starting with ↳. This is the ONLY place analysis, significance, or implications for Blockchain.com may appear. Keep it to a single sentence about the section as a whole — not a recap of the bullets.
- Order sections by relevance — company announcements and major regulatory/market events first; routine product updates later.
- No "Actionable" section.
- Always end with a **Fundraising** section. From the fundraising entries, select only the most relevant ones for Blockchain.com — focus on AI, payments, stablecoins, custody, exchanges, DeFi, institutional infrastructure. Skip generic or unrelated raises. For each bullet: bold company name, amount, round type, then a one-sentence description of what the company does — use your knowledge or the "company:" tag from the tags field. If you don't know the company, use the tweet context to infer what they do. Format: "• **CompanyName** ($Xm, Series A) — one sentence on what they do." If no relevant fundraising entries, skip this section.
- No strict length limit — prioritise readability and completeness over brevity, but stay concise within each bullet."""
        }]
    )

    raw = response.content[0].text.strip()
    return md_to_telegram_html(raw)
