"""
Strategic Watch Bot — main entry point.
Telegram bot + APScheduler for daily scraping at 8h UTC.
"""

import os
import asyncio
import logging
from datetime import datetime
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import init_db, get_stats, search_entries, get_recent_kept, get_all_entries
from scraper_twitter import scrape_accounts, load_targets
from scraper_rss import scrape_rss_feeds
from enrichment import run_enrichment_batch, estimate_cost, load_author_registry
from digest import generate_daily_digest

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANDREAS_CHAT_ID = int(os.environ["ANDREAS_CHAT_ID"])
CONTRIBUTOR_IDS = set(map(int, os.environ.get("CONTRIBUTOR_IDS", "").split(","))) if os.environ.get("CONTRIBUTOR_IDS") else set()
DIGEST_HOUR     = int(os.environ.get("DIGEST_HOUR", "8"))
DIGEST_MINUTE   = int(os.environ.get("DIGEST_MINUTE", "0"))

ALL_ALLOWED = CONTRIBUTOR_IDS | {ANDREAS_CHAT_ID}


def is_allowed(update: Update) -> bool:
    return update.effective_user and update.effective_user.id in ALL_ALLOWED

def is_contributor(update: Update) -> bool:
    return update.effective_user and update.effective_user.id in CONTRIBUTOR_IDS


# ─── Scheduled jobs ───────────────────────────────────────────────────────────

async def job_scrape_twitter():
    logger.info("Scheduled: scrape Twitter accounts (last 24h)")
    result = await scrape_accounts(max_per_account=200)
    logger.info("Twitter scrape done — new=%s skipped=%s", result["new"], result["skipped"])

async def job_scrape_rss():
    logger.info("Scheduled: scrape RSS competitor blogs (last 24h)")
    result = await scrape_rss_feeds()
    logger.info("RSS scrape done — new=%s skipped=%s", result["new"], result["skipped"])

async def job_enrich():
    logger.info("Scheduled: enrich pending entries")
    stats = await run_enrichment_batch(batch_size=100)
    logger.info("Enrichment done — kept=%s filtered=%s tokens=%s", stats["kept"], stats["filtered"], stats["tokens_used"])

async def job_daily_digest(app: Application):
    logger.info("Scheduled: daily digest")
    text = generate_daily_digest(hours=24)
    await app.bot.send_message(chat_id=ANDREAS_CHAT_ID, text=text, parse_mode="Markdown")


# ─── Commands ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    role = "contributor" if is_contributor(update) else "reader"
    msg = "Use /scrape, /scrape_rss, /enrich, /export to manage the pipeline.\n" if role == "contributor" else ""
    await update.message.reply_text(
        "Strategic Watch Bot — logged in as *" + role + "*.\n\n" + msg +
        "Use /ask, /digest, /recent, /stats to query the knowledge base.",
        parse_mode="Markdown"
    )

async def cmd_scrape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_contributor(update):
        return await update.message.reply_text("Contributors only.")
    msg = await update.message.reply_text("Scraping Twitter accounts...")
    result = await scrape_accounts(max_per_account=200)
    await msg.edit_text(
        "Twitter scrape done.\n"
        "New: +" + str(result["new"]) + "\n"
        "Skipped (dedup): " + str(result["skipped"]) + "\n\n"
        "Run /enrich to process them."
    )

async def cmd_scrape_rss(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_contributor(update):
        return await update.message.reply_text("Contributors only.")
    msg = await update.message.reply_text("Scraping competitor blogs (RSS)...")
    result = await scrape_rss_feeds()
    await msg.edit_text(
        "RSS scrape done.\n"
        "New articles: +" + str(result["new"]) + "\n"
        "Skipped (dedup): " + str(result["skipped"]) + "\n\n"
        "Run /enrich to process them."
    )

async def cmd_enrich(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_contributor(update):
        return await update.message.reply_text("Contributors only.")
    stats = get_stats()
    pending = stats["pending_enrichment"]
    if pending == 0:
        return await update.message.reply_text("No pending entries.")
    est = estimate_cost(min(pending, 100))
    msg = await update.message.reply_text(
        "Enriching up to 100 entries (pending: " + str(pending) + ")...\n"
        "Est. cost: ~$" + str(round(est["estimated_cost_usd_haiku"], 4))
    )
    result = await run_enrichment_batch(batch_size=100)
    await msg.edit_text(
        "Enrichment done.\n"
        "Processed: " + str(result["processed"]) + "\n"
        "Kept (score >= 0.5): " + str(result["kept"]) + "\n"
        "Filtered out: " + str(result["filtered"]) + "\n"
        "Tokens used: " + str(result["tokens_used"]) + " (~$" + str(round(result.get("estimated_cost_usd", 0), 5)) + ")\n"
        "Errors: " + str(result["errors"])
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    s = get_stats()
    by_src = "\n".join("  " + k + ": " + str(v) for k, v in s["by_source"].items()) or "  (empty)"
    cost_est = round((s["total_tokens_used"] / 1_000_000) * 2.0, 3)
    await update.message.reply_text(
        "*Database stats*\n\n"
        "Total entries: " + str(s["total"]) + "\n"
        "Kept (relevant): " + str(s["kept"]) + "\n"
        "Pending enrichment: " + str(s["pending_enrichment"]) + "\n"
        "Filtered out: " + str(s["filtered_out"]) + "\n\n"
        "By source:\n" + by_src + "\n\n"
        "Total tokens used: " + str(s["total_tokens_used"]) + "\n"
        "Est. AI cost to date: ~$" + str(cost_est),
        parse_mode="Markdown"
    )

async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    msg = await update.message.reply_text("Generating digest...")
    text = generate_daily_digest(hours=24)
    await msg.edit_text(text, parse_mode="Markdown")

async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    import json
    entries = get_recent_kept(hours=48, limit=5)
    if not entries:
        return await update.message.reply_text("Nothing in the last 48h.")
    lines = []
    for e in entries:
        tags = json.loads(e["tags"]) if e.get("tags") else []
        tag_str = " ".join("#" + t for t in tags[:3])
        score = str(round(e["relevance_score"], 2)) if e.get("relevance_score") else "?"
        summary = (e.get("summary") or e.get("content", "")[:120]).replace("*", "").replace("_", "").replace("`", "")
        lines.append(
            "[" + e["source_type"] + " | @" + e["author"] + " | " + score + "] " + tag_str + "\n" +
            summary + "\n" +
            e["source_url"]
        )
    await update.message.reply_text(
        "Recent entries\n\n" + "\n\n".join(lines),
        disable_web_page_preview=True
    )

async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    question = " ".join(ctx.args).strip() if ctx.args else ""
    if not question:
        return await update.message.reply_text("Usage: /ask <your question>")
    msg = await update.message.reply_text("Searching...")
    keywords = " OR ".join(question.split()[:4])
    results = search_entries(keywords, limit=15)
    if not results:
        return await msg.edit_text("Nothing found in the knowledge base for that query.")
    import json
    from anthropic import Anthropic
    snippets = []
    for e in results[:10]:
        summary = e.get("summary") or e.get("content", "")[:200]
        snippets.append("[@" + e["author"] + " | " + e["source_type"] + "]\n" + summary + "\n" + e["source_url"])
    context = "\n\n---\n\n".join(snippets)
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        system="You are a strategic analyst for Blockchain.com. Answer questions using only the provided knowledge base entries. Be direct and cite sources with @author and URL. If the information is not there, say so.",
        messages=[{"role": "user", "content": "Knowledge base:\n\n" + context + "\n\n---\n\nQuestion: " + question}]
    )
    await msg.edit_text(response.content[0].text.strip(), parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_contributor(update):
        return await update.message.reply_text("Contributors only.")
    import csv, io, json as _json
    msg = await update.message.reply_text("Generating export...")
    entries = get_all_entries(limit=2000)
    if not entries:
        return await msg.edit_text("No entries in DB.")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "author", "source_type", "published_at", "relevance_score", "kept", "tags", "summary", "content", "source_url"])
    for e in entries:
        tags = e.get("tags") or "[]"
        try:
            tags = ", ".join(_json.loads(tags))
        except Exception:
            pass
        writer.writerow([
            e.get("id"), e.get("author"), e.get("source_type"),
            e.get("published_at"), e.get("relevance_score"), e.get("kept"),
            tags, e.get("summary", ""), (e.get("content") or "")[:200], e.get("source_url"),
        ])
    output.seek(0)
    await update.message.reply_document(
        document=output.read().encode("utf-8"),
        filename="watch_db_export.csv",
        caption=str(len(entries)) + " entries exported"
    )
    await msg.delete()


# ─── App setup ────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",      "Introduction"),
        BotCommand("digest",     "Daily digest (last 24h)"),
        BotCommand("ask",        "Ask a question from the knowledge base"),
        BotCommand("recent",     "5 most recent relevant entries"),
        BotCommand("stats",      "Database stats and cost"),
        BotCommand("scrape",     "[Contributors] Scrape Twitter accounts"),
        BotCommand("scrape_rss", "[Contributors] Scrape competitor blogs"),
        BotCommand("enrich",     "[Contributors] Trigger AI enrichment"),
        BotCommand("export",     "[Contributors] Export DB as CSV"),
    ])


def main():
    init_db()

    import json, os
    targets_path = "targets.json"
    if os.path.exists(targets_path):
        load_targets(targets_path)
        load_author_registry(targets_path)

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("scrape",     cmd_scrape))
    app.add_handler(CommandHandler("scrape_rss", cmd_scrape_rss))
    app.add_handler(CommandHandler("enrich",     cmd_enrich))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("digest",     cmd_digest))
    app.add_handler(CommandHandler("recent",     cmd_recent))
    app.add_handler(CommandHandler("ask",        cmd_ask))
    app.add_handler(CommandHandler("export",     cmd_export))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(job_scrape_twitter, "cron", hour=8, minute=0)
    scheduler.add_job(job_scrape_rss,     "cron", hour=8, minute=5)
    scheduler.add_job(job_enrich,         "cron", hour=8, minute=30)
    scheduler.add_job(job_daily_digest,   "cron", hour=9, minute=0, args=[app])
    scheduler.start()

    logger.info("Strategic Watch Bot started — daily scrape at 8h UTC")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
