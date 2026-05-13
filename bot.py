"""
Strategic Watch Bot
Schedule UTC:
  08:00  RSS (34 sources — native feeds + Google News)
  08:30  Digest -> ANDREAS_CHAT_ID
  09:00  Health check -> alert if source broken
"""

import os
import logging
import anthropic
from datetime import datetime, timezone, timedelta
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import init_db, get_stats, search_entries, get_recent_entries, get_all_entries, get_last_ingested_per_source
from scraper_rss import scrape_rss_feeds, RSS_FEEDS
from digest import generate_daily_digest

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANDREAS_CHAT_ID   = int(os.environ["ANDREAS_CHAT_ID"])
CONTRIBUTOR_IDS   = set(map(int, os.environ["CONTRIBUTOR_IDS"].split(","))) if os.environ.get("CONTRIBUTOR_IDS") else set()
ALL_ALLOWED       = CONTRIBUTOR_IDS | {ANDREAS_CHAT_ID}

def is_allowed(u): return u.effective_user and u.effective_user.id in ALL_ALLOWED
def is_contributor(u): return u.effective_user and u.effective_user.id in CONTRIBUTOR_IDS


# --- Scheduled jobs ---

async def job_rss():
    r = await scrape_rss_feeds(days=1)
    logger.info(f"RSS done - new={r['new']} skipped={r['skipped']}")

async def job_digest(app):
    text = generate_daily_digest(hours=24)
    await app.bot.send_message(chat_id=ANDREAS_CHAT_ID, text=text, parse_mode="Markdown", disable_web_page_preview=True)

async def job_health_check(app):
    last_ingested = get_last_ingested_per_source()
    cutoff_48h = datetime.now(timezone.utc) - timedelta(hours=48)
    broken = []

    for feed in RSS_FEEDS:
        name = feed["name"]
        last = last_ingested.get(name)
        if last is None:
            broken.append(f"⚠️ *{name}* — never ingested")
        else:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if last_dt < cutoff_48h:
                    hours_ago = int((datetime.now(timezone.utc) - last_dt).total_seconds() / 3600)
                    broken.append(f"🔴 *{name}* — last article *{hours_ago}h ago*")
            except Exception:
                broken.append(f"⚠️ *{name}* — unreadable date")

    if broken:
        msg = "*Health Check — Broken sources*\n\n" + "\n".join(broken)
        msg += "\n\n_Check Railway logs or run /scrape\\_rss manually._"
        await app.bot.send_message(chat_id=ANDREAS_CHAT_ID, text=msg, parse_mode="Markdown")
        logger.warning(f"Health check: {len(broken)} broken sources")
    else:
        logger.info("Health check: all sources OK")


# --- Commands ---

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    role = "contributor" if is_contributor(update) else "reader"
    await update.message.reply_text(
        f"*Strategic Watch Bot* — {role}\n\n/ask · /digest · /recent · /stats · /scrape\\_rss",
        parse_mode="Markdown"
    )

async def cmd_scrape_rss(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_contributor(update): return await update.message.reply_text("Contributors only.")
    days = 1
    if ctx.args:
        try:
            days = max(1, min(int(ctx.args[0]), 30))
        except ValueError:
            return await update.message.reply_text("Usage: /scrape_rss [days] — e.g. /scrape_rss 7")
    msg = await update.message.reply_text(f"Scraping RSS ({len(RSS_FEEDS)} sources, last {days}d)...")
    r = await scrape_rss_feeds(days=days)
    await msg.edit_text(f"RSS done\nNew: +{r['new']}\nSkipped: {r['skipped']}\nErrors: {len(r['errors'])}")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    s = get_stats()
    by_cat  = "\n".join(f"  {k}: {v}" for k, v in s.get("by_category", {}).items()) or "  (empty)"
    by_type = "\n".join(f"  {k}: {v}" for k, v in s.get("by_type", {}).items()) or "  (empty)"
    await update.message.reply_text(
        f"*DB Stats*\nTotal: {s['total']}\nLast ingested: {s.get('last_ingested','N/A')}\n\nBy type:\n{by_type}\n\nBy category:\n{by_cat}",
        parse_mode="Markdown"
    )

async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg = await update.message.reply_text("Generating digest...")
    text = generate_daily_digest(hours=24)
    await msg.edit_text(text, parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    entries = get_recent_entries(limit=5)
    if not entries:
        return await update.message.reply_text("No entries in the last 24h.")
    lines = []
    for e in entries:
        pub = (e.get("published_at") or e.get("ingested_at") or "")[:10]
        lines.append(f"[{e['source_name']}] *{e['title'][:70]}*\n  {pub} — {e['source_url']}")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    question = " ".join(ctx.args) if ctx.args else ""
    if not question:
        return await update.message.reply_text("Usage: /ask <question>")
    msg = await update.message.reply_text("Searching knowledge base...")
    entries = search_entries(question, limit=15)
    if not entries:
        return await msg.edit_text("Nothing found in the knowledge base.")
    context = "\n\n---\n\n".join(
        f"[{e['source_name']} / {(e.get('published_at') or '')[:10]}]\n{e['title']}\n\n{(e.get('content') or '')[:800]}"
        for e in entries
    )
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system="You are a strategic intelligence assistant for Blockchain.com. Answer only using the provided knowledge base. Be concise. Cite sources with [SourceName].",
        messages=[{"role": "user", "content": f"Knowledge base:\n\n{context}\n\n---\n\nQuestion: {question}"}]
    )
    await msg.edit_text(response.content[0].text.strip(), parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_contributor(update): return await update.message.reply_text("Contributors only.")
    import csv, io
    msg = await update.message.reply_text("Generating export...")
    entries = get_all_entries(limit=2000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id","source_type","source_category","source_name","title","published_at","ingested_at","source_url","content_preview"])
    for e in entries:
        writer.writerow([e.get("id"),e.get("source_type"),e.get("source_category"),e.get("source_name"),e.get("title"),e.get("published_at"),e.get("ingested_at"),e.get("source_url"),(e.get("content") or "")[:200]])
    output.seek(0)
    await update.message.reply_document(document=output.read().encode(), filename="watch_export.csv", caption=f"{len(entries)} entries")
    await msg.delete()


# --- App setup ---

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start",      "Introduction"),
        BotCommand("digest",     "Daily digest"),
        BotCommand("ask",        "Ask a question"),
        BotCommand("recent",     "5 latest articles"),
        BotCommand("stats",      "DB stats"),
        BotCommand("scrape_rss", "[Contributors] Scrape RSS"),
        BotCommand("export",     "[Contributors] Export CSV"),
    ])

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("scrape_rss", cmd_scrape_rss))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("digest",     cmd_digest))
    app.add_handler(CommandHandler("recent",     cmd_recent))
    app.add_handler(CommandHandler("ask",        cmd_ask))
    app.add_handler(CommandHandler("export",     cmd_export))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(job_rss,          "cron", hour=8, minute=0)
    scheduler.add_job(job_digest,       "cron", hour=8, minute=30, args=[app])
    scheduler.add_job(job_health_check, "cron", hour=9, minute=0,  args=[app])
    scheduler.start()

    logger.info(f"Bot started - RSS 08:00, Digest 08:30, Health 09:00 UTC | {len(RSS_FEEDS)} RSS feeds")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
