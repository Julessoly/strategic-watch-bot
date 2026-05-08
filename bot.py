"""
Strategic Watch Bot — main entry point.
Telegram bot + APScheduler pour scraping quotidien à 8h UTC.
"""

import os
import asyncio
import logging
from datetime import datetime
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import init_db, get_stats, search_entries, get_recent_kept
from scraper_twitter import scrape_accounts, load_targets
from enrichment import run_enrichment_batch, estimate_cost, load_author_registry
from digest import generate_daily_digest

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANDREAS_CHAT_ID   = int(os.environ["ANDREAS_CHAT_ID"])
CONTRIBUTOR_IDS   = set(map(int, os.environ.get("CONTRIBUTOR_IDS", "").split(","))) if os.environ.get("CONTRIBUTOR_IDS") else set()
DIGEST_HOUR       = int(os.environ.get("DIGEST_HOUR", "8"))
DIGEST_MINUTE     = int(os.environ.get("DIGEST_MINUTE", "0"))

ALL_ALLOWED = CONTRIBUTOR_IDS | {ANDREAS_CHAT_ID}


def is_allowed(update: Update) -> bool:
    return update.effective_user and update.effective_user.id in ALL_ALLOWED

def is_contributor(update: Update) -> bool:
    return update.effective_user and update.effective_user.id in CONTRIBUTOR_IDS

def is_andreas(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ANDREAS_CHAT_ID


# ─── Scheduled jobs ───────────────────────────────────────────────────────────

async def job_scrape_twitter_accounts():
    logger.info("⏰ Scheduled: scrape Twitter accounts (last 24h)")
    result = await scrape_accounts(max_per_account=200)
    logger.info(f"   → new={result['new']} skipped={result['skipped']}")

async def job_enrich():
    logger.info("⏰ Scheduled: enrich pending entries")
    stats = await run_enrichment_batch(batch_size=100)
    logger.info(f"   → kept={stats['kept']} filtered={stats['filtered']} tokens={stats['tokens_used']}")

async def job_daily_digest(app: Application):
    logger.info("⏰ Scheduled: daily digest → Andreas")
    text = generate_daily_digest(hours=24)
    await app.bot.send_message(
        chat_id=ANDREAS_CHAT_ID,
        text=text,
        parse_mode="Markdown"
    )


# ─── Commands ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    role = "contributor" if is_contributor(update) else "reader"
    await update.message.reply_text(
        f"👋 Strategic Watch Bot — you're logged in as *{role}*.\n\n"
        f"{'Use /scrape, /enrich, /stats to manage the pipeline.' if role == 'contributor' else ''}\n"
        f"Use /ask <question>, /digest, /recent, /stats to query the knowledge base.",
        parse_mode="Markdown"
    )

async def cmd_scrape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually trigger a full scrape cycle."""
    if not is_contributor(update):
        return await update.message.reply_text("⛔ Contributors only.")
    msg = await update.message.reply_text("🔄 Scraping Twitter accounts…")
    tw_acc = await scrape_accounts(max_per_account=200)
    await msg.edit_text(
        f"✅ Scrape done.\n"
        f"• Accounts: +{tw_acc['new']} new\n"
        f"• Skipped (dedup): {tw_acc['skipped']}\n\n"
        f"Run /enrich to process them."
    )

async def cmd_enrich(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually trigger AI enrichment on pending entries."""
    if not is_contributor(update):
        return await update.message.reply_text("⛔ Contributors only.")
    stats = get_stats()
    pending = stats["pending_enrichment"]
    if pending == 0:
        return await update.message.reply_text("✅ No pending entries.")
    est = estimate_cost(min(pending, 100))
    msg = await update.message.reply_text(
        f"⚙️ Enriching up to 100 entries (pending: {pending})…\n"
        f"Est. cost: ~${est['estimated_cost_usd_haiku']:.4f}"
    )
    result = await run_enrichment_batch(batch_size=100)
    await msg.edit_text(
        f"✅ Enrichment done.\n"
        f"• Processed: {result['processed']}\n"
        f"• Kept (score ≥ 0.3): {result['kept']}\n"
        f"• Filtered out: {result['filtered']}\n"
        f"• Tokens used: {result['tokens_used']} (~${result.get('estimated_cost_usd', 0):.5f})\n"
        f"• Errors: {result['errors']}"
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    s = get_stats()
    by_src = "\n".join(f"  • {k}: {v}" for k, v in s["by_source"].items()) or "  (empty)"
    cost_est = (s["total_tokens_used"] / 1_000_000) * 2.0
    await update.message.reply_text(
        f"📊 *Database stats*\n\n"
        f"Total entries: {s['total']}\n"
        f"Kept (relevant): {s['kept']}\n"
        f"Pending enrichment: {s['pending_enrichment']}\n"
        f"Filtered out: {s['filtered_out']}\n\n"
        f"By source:\n{by_src}\n\n"
        f"Total tokens used: {s['total_tokens_used']:,}\n"
        f"Est. AI cost to date: ~${cost_est:.3f}",
        parse_mode="Markdown"
    )

async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    msg = await update.message.reply_text("⏳ Generating digest…")
    text = generate_daily_digest(hours=24)
    await msg.edit_text(text, parse_mode="Markdown")

async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the 5 most recent kept entries."""
    if not is_allowed(update):
        return
    import json
    entries = get_recent_kept(hours=48, limit=5)
    if not entries:
        return await update.message.reply_text("📭 Nothing in the last 48h.")
    lines = []
    for e in entries:
        tags = json.loads(e["tags"]) if e.get("tags") else []
        tag_str = " ".join(f"#{t}" for t in tags[:3])
        score = f"{e['relevance_score']:.2f}" if e.get("relevance_score") else "?"
        summary = e.get("summary") or e.get("content", "")[:120]
        lines.append(
            f"[{e['source_type']} | @{e['author']} | {score}] {tag_str}\n"
            f"{summary}\n"
            f"→ {e['source_url']}"
        )
    await update.message.reply_text(
        "🕐 *Recent entries*\n\n" + "\n\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Free-form question answered from the knowledge base."""
    if not is_allowed(update):
        return
    question = " ".join(ctx.args).strip() if ctx.args else ""
    if not question:
        return await update.message.reply_text("Usage: /ask <your question>")

    msg = await update.message.reply_text("🔍 Searching…")

    keywords = " OR ".join(question.split()[:4])
    results = search_entries(keywords, limit=15)

    if not results:
        return await msg.edit_text("🤷 Nothing found in the knowledge base for that query.")

    import json
    from anthropic import Anthropic
    snippets = []
    for e in results[:10]:
        summary = e.get("summary") or e.get("content", "")[:200]
        snippets.append(f"[@{e['author']} | {e['source_type']}]\n{summary}\n{e['source_url']}")

    context = "\n\n---\n\n".join(snippets)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system="You are a strategic analyst for Blockchain.com. Answer questions using only the provided knowledge base entries. Be direct and cite sources with @author and URL. If the information isn't there, say so.",
        messages=[{
            "role": "user",
            "content": f"Knowledge base:\n\n{context}\n\n---\n\nQuestion: {question}"
        }]
    )
    await msg.edit_text(response.content[0].text.strip(), parse_mode="Markdown", disable_web_page_preview=True)


# ─── App setup ────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    """Set bot commands visible in Telegram UI."""
    await app.bot.set_my_commands([
        BotCommand("start",   "Introduction"),
        BotCommand("digest",  "Daily digest (last 24h)"),
        BotCommand("ask",     "Ask a question from the knowledge base"),
        BotCommand("recent",  "5 most recent relevant entries"),
        BotCommand("stats",   "Database stats + cost"),
        BotCommand("scrape",  "[Contributors] Trigger manual scrape"),
        BotCommand("enrich",  "[Contributors] Trigger AI enrichment"),
    ])


def main():
    init_db()

    import json, os
    targets_path = "config/targets.json"
    if os.path.exists(targets_path):
        with open(targets_path) as f:
            targets = json.load(f)
        load_targets(targets_path)
        load_author_registry(targets_path)

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register handlers
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("scrape",  cmd_scrape))
    app.add_handler(CommandHandler("enrich",  cmd_enrich))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("digest",  cmd_digest))
    app.add_handler(CommandHandler("recent",  cmd_recent))
    app.add_handler(CommandHandler("ask",     cmd_ask))

    # ─── Scheduler : tout se passe à 8h UTC ───────────────────────────────────
    scheduler = AsyncIOScheduler()

    # 08:00 — scrape les tweets des dernières 24h (comptes)
    scheduler.add_job(job_scrape_twitter_accounts, "cron", hour=8, minute=0)
    # 08:30 — enrichissement IA de tous les tweets scrappés
    scheduler.add_job(job_enrich, "cron", hour=8, minute=30)
    # 09:00 — digest envoyé à Andreas (après enrichissement)
    scheduler.add_job(
        job_daily_digest,
        "cron",
        hour=9,
        minute=0,
        args=[app],
    )

    scheduler.start()

    logger.info("🚀 Strategic Watch Bot started — scrape quotidien à 8h UTC")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
