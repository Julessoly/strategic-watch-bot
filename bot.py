"""
Strategic Watch Bot
Schedule UTC:
  08:00  RSS scrape (34 sources — native feeds + Google News)
  08:05  Twitter scrape
  08:15  AI enrichment (tag + delete noise)
  08:30  Digest -> GROUP_CHAT_ID
  09:00  Health check -> alert if source broken
"""

import os
import re
import logging
import anthropic
from datetime import datetime, timezone, timedelta
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from telegram.error import BadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import init_db, get_stats, search_entries, get_recent_entries, get_all_entries, get_last_ingested_per_source, reset_untagged, save_daily_watch, cleanup_old_watches
from scraper_rss import scrape_rss_feeds, RSS_FEEDS
from scrape_twitter import scrape_twitter_accounts, TWITTER_ACCOUNTS
from digest import generate_daily_digest, md_to_telegram_html
from enrichment import enrich_entries, deduplicate_cross_day

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANDREAS_CHAT_ID   = int(os.environ["ANDREAS_CHAT_ID"])
GROUP_CHAT_ID     = int(os.environ.get("GROUP_CHAT_ID", "0")) or None
CONTRIBUTOR_IDS   = set(map(int, os.environ["CONTRIBUTOR_IDS"].split(","))) if os.environ.get("CONTRIBUTOR_IDS") else set()
ALL_ALLOWED       = CONTRIBUTOR_IDS | {ANDREAS_CHAT_ID}


def strip_html_tags(text: str) -> str:
    """Fallback: retire les tags HTML pour un envoi en texte brut si le parsing échoue."""
    return re.sub(r"<[^>]+>", "", text)

def chunk_text(raw_text, limit=4000):
    """Splits text cleanly at newlines to avoid hitting Telegram's limits."""
    chunks = []
    while len(raw_text) > limit:
        split_at = raw_text.rfind('\n', 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(raw_text[:split_at])
        raw_text = raw_text[split_at:].strip()
    if raw_text:
        chunks.append(raw_text)
    return chunks

async def send_chunked_digest(text, target_chat_id, bot, placeholder_msg=None):
    """Handles safely sending a long digest, whether from a job or a user command."""
    try:
        chunks = chunk_text(text)
        
        # If there's a placeholder (manual command), edit it first
        if placeholder_msg:
            await placeholder_msg.edit_text(chunks[0], parse_mode="HTML", disable_web_page_preview=True)
            start_index = 1
        else:
            start_index = 0
            
        # Send remaining chunks (or all chunks if automatic job)
        for chunk in chunks[start_index:]:
            await bot.send_message(chat_id=target_chat_id, text=chunk, parse_mode="HTML", disable_web_page_preview=True)
            
    except BadRequest:
        logger.warning("Digest HTML parse failed, sending as plain text in chunks", exc_info=True)
        clean_text = strip_html_tags(text)
        chunks = chunk_text(clean_text)
        
        if placeholder_msg:
            await placeholder_msg.edit_text(chunks[0], disable_web_page_preview=True)
            start_index = 1
        else:
            start_index = 0
            
        for chunk in chunks[start_index:]:
            await bot.send_message(chat_id=target_chat_id, text=chunk, disable_web_page_preview=True)


def is_allowed(u):
    if not u.effective_user:
        return False
    # Allow individual authorized users
    if u.effective_user.id in ALL_ALLOWED:
        return True
    # Allow any message coming from the group
    if u.effective_chat and GROUP_CHAT_ID and u.effective_chat.id == GROUP_CHAT_ID:
        return True
    return False

def is_contributor(u):
    return u.effective_user and u.effective_user.id in CONTRIBUTOR_IDS


# --- Scheduled jobs ---

async def job_rss():
    r = await scrape_rss_feeds(days=1)
    logger.info(f"RSS done - new={r['new']} skipped={r['skipped']}")

async def job_twitter():
    r = await scrape_twitter_accounts(days=1)
    logger.info(f"Twitter done - new={r['new']} skipped={r['skipped']}")

async def job_enrich():
    r = await enrich_entries(limit=200)
    logger.info(f"Enrichment done - kept={r['kept']} deleted={r['deleted']} errors={r['errors']}")

async def job_dedup():
    """Cron wrapper for deduplication"""
    logger.info("Starting scheduled cross-day deduplication...")
    await deduplicate_cross_day()
    logger.info("Scheduled cross-day deduplication complete.")

async def job_digest(app):
    text = generate_daily_digest(hours=24)
    save_daily_watch(text)
    cleanup_old_watches(days=7)
    target = GROUP_CHAT_ID if GROUP_CHAT_ID else ANDREAS_CHAT_ID
    await send_chunked_digest(text, target, app.bot)

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
        f"*Strategic Watch Bot* — {role}\n\n/ask · /digest · /recent · /stats · /scrape\\_rss · /enrich",
        parse_mode="Markdown"
    )

async def cmd_scrape_twitter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_contributor(update): return await update.message.reply_text("Contributors only.")
    days = 1
    if ctx.args:
        try:
            days = max(1, min(int(ctx.args[0]), 30))
        except ValueError:
            pass
    msg = await update.message.reply_text(f"Scraping Twitter ({len(TWITTER_ACCOUNTS)} accounts, last {days}d)...")
    r = await scrape_twitter_accounts(days=days)
    await msg.edit_text(f"Twitter done\nNew: +{r['new']}\nSkipped: {r['skipped']}\nErrors: {r['errors']}")

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

async def cmd_reset_tags(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_contributor(update): return await update.message.reply_text("Contributors only.")
    n = reset_untagged()
    await update.message.reply_text(f"Reset {n} entries to NULL — run /enrich to re-process.")

async def cmd_enrich(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_contributor(update): return await update.message.reply_text("Contributors only.")
    limit = 200
    if ctx.args:
        try:
            limit = max(1, min(int(ctx.args[0]), 500))
        except ValueError:
            pass
    msg = await update.message.reply_text(f"Running AI enrichment (up to {limit} entries)...")
    r = await enrich_entries(limit=limit)
    await msg.edit_text(
        f"Enrichment done\n"
        f"Processed: {r['processed']}\n"
        f"Kept: ✅ {r['kept']}\n"
        f"Deleted: 🗑 {r['deleted']}\n"
        f"Errors: ⚠️ {r['errors']}"
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    s = get_stats()
    by_cat = "\n".join(f"  {k}: {v}" for k, v in s.get("by_category", {}).items()) or "  (empty)"
    await update.message.reply_text(
        f"*DB Stats*\n"
        f"Total: {s['total']}\n"
        f"Enriched: {s.get('enriched', 0)} | Pending: {s.get('pending', 0)}\n"
        f"Last ingested: {s.get('last_ingested', 'N/A')}\n\n"
        f"By category:\n{by_cat}",
        parse_mode="Markdown"
    )

async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logger.info(f"User {update.effective_user.id} tried to run /digest in chat {update.effective_chat.id}")
    
    if not is_allowed(update): 
        logger.warning(f"BLOCKED: User {update.effective_user.id} is not in ALL_ALLOWED ({ALL_ALLOWED})")
        return
    
    # 1. Send an updated status message
    msg = await update.message.reply_text("🧼 Running cross-day deduplication and filtering...")
    
    try:
        # 2. Run the deduplication script right now
        await deduplicate_cross_day()
        
        # 3. Update the message so the user knows it's moving to the next step
        await msg.edit_text("🤖 Generating digest with Claude...")
        
        # 4. Generate the final text
        text = generate_daily_digest(hours=24)
        target = update.effective_chat.id
        
        # 5. Send using the chunked logic we built earlier
        await send_chunked_digest(text, target, ctx.bot, placeholder_msg=msg)
        
    except Exception as e:
        logger.error(f"Manual digest generation failed: {e}", exc_info=True)
        await update.message.reply_text("❌ An error occurred while generating the manual digest.")


async def cmd_recent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    entries = get_recent_entries(limit=5)
    if not entries:
        return await update.message.reply_text("No entries in the last 24h.")
    lines = []
    for e in entries:
        pub = (e.get("published_at") or e.get("ingested_at") or "")[:10]
        tags = f"\n  🏷 {e['tags']}" if e.get("tags") else ""
        lines.append(f"[{e['source_name']}] *{e['title'][:70]}*\n  {pub} — {e['source_url']}{tags}")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    question = " ".join(ctx.args) if ctx.args else ""
    if not question:
        return await update.message.reply_text("Usage: /ask <question>")
    msg = await update.message.reply_text("Searching knowledge base and web...")

    # Build context from DB
    entries = search_entries(question, limit=15)
    db_context = ""
    if entries:
        db_context = "\n\n---\n\n".join(
            f"[{e['source_name']} / {(e.get('published_at') or '')[:10]}]\n{e['title']}\n\n{(e.get('content') or '')[:800]}"
            for e in entries
        )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        system="""You are a strategic intelligence assistant for Blockchain.com, a leading crypto company offering retail exchange, institutional OTC, custody, staking, and prime brokerage services.

Answer the user's question by combining the internal knowledge base and web search.

Format your answer exactly like a daily strategic watch memo:
- Start with a bold title line relevant to the question, e.g. **🔍 Circle — Recent Announcements**
- Group findings under dynamic section titles formatted exactly like this: **🚀 Product Launches**, **⚖️ Regulation**, **💵 Stablecoins** — only include sections relevant to the answer
- Each bullet (•) = the fact first (company, number, event), then one sentence of context if genuinely useful
- Short sentences, plain English, no buzzwords
- Cite sources inline as [SourceName] or [Web]

""" + ("Internal knowledge base:\n\n" + db_context if db_context else "No relevant articles found in internal knowledge base — rely on web search."),
        messages=[{"role": "user", "content": question}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}]
    )

    # Extract text from response (may contain tool_use blocks)
    answer = " ".join(
        block.text for block in response.content
        if hasattr(block, "text")
    ).strip()

    if not answer:
        answer = "No answer could be generated."

    answer = md_to_telegram_html(answer)
    try:
        await msg.edit_text(answer, parse_mode="HTML", disable_web_page_preview=True)
    except BadRequest:
        logger.warning("Ask HTML parse failed, sending as plain text", exc_info=True)
        await msg.edit_text(strip_html_tags(answer), disable_web_page_preview=True)

from database import insert_entry

# Conversation states for /add
ADD_TITLE, ADD_SOURCE, ADD_CONTENT, ADD_TAGS = range(4)

async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_contributor(update): return await update.message.reply_text("Contributors only.")
    await update.message.reply_text("📝 *Add a manual entry*\n\nStep 1/4 — What's the *title* of the article/report?", parse_mode="Markdown")
    return ADD_TITLE

async def add_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["add_title"] = update.message.text.strip()
    await update.message.reply_text("Step 2/4 — What's the *source* name? (e.g. JPMorgan Research, Goldman Sachs)", parse_mode="Markdown")
    return ADD_SOURCE

async def add_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["add_source"] = update.message.text.strip()
    await update.message.reply_text("Step 3/4 — Paste the *content* of the article (or a summary):", parse_mode="Markdown")
    return ADD_CONTENT

async def add_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["add_content"] = update.message.text.strip()
    await update.message.reply_text("Step 4/4 — Add *tags* (comma-separated, e.g. `iran,oil,macro,inflation`) or send /skip to leave empty:", parse_mode="Markdown")
    return ADD_TAGS

async def add_tags(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tags = update.message.text.strip() if update.message.text != "/skip" else ""
    title   = ctx.user_data.get("add_title", "")
    source  = ctx.user_data.get("add_source", "")
    content = ctx.user_data.get("add_content", "")

    row_id = insert_entry(
        source_category="research",
        source_description="research",
        source_name=source,
        source_url=f"manual:{title[:60].replace(' ', '-').lower()}-{int(datetime.now().timestamp())}",
        author=source,
        title=title,
        content=content[:4000],
        published_at=datetime.now(timezone.utc).isoformat(),
    )
    # Set tags directly if provided
    if row_id and tags:
        from database import update_tags
        update_tags(row_id, tags)

    if row_id:
        await update.message.reply_text(f"✅ Entry added (id={row_id})\n\n*{title}*\nSource: {source}\nTags: {tags or '(none)'}", parse_mode="Markdown")
    else:
        await update.message.reply_text("⚠️ Failed to add entry — duplicate URL?")

    ctx.user_data.clear()
    return ConversationHandler.END

async def add_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_contributor(update): return await update.message.reply_text("Contributors only.")
    import csv, io
    msg = await update.message.reply_text("Generating export...")
    entries = get_all_entries(limit=2000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "source_category", "source_description", "source_name", "title", "tags", "published_at", "ingested_at", "source_url", "content_preview"])
    for e in entries:
        writer.writerow([
            e.get("id"), e.get("source_category"), e.get("source_description"),
            e.get("source_name"), e.get("title"), e.get("tags"),
            e.get("published_at"), e.get("ingested_at"), e.get("source_url"),
            (e.get("content") or "")[:200]
        ])
    output.seek(0)
    await update.message.reply_document(document=output.read().encode(), filename="watch_export.csv", caption=f"{len(entries)} entries")
    await msg.delete()


# --- Error handler ---

async def error_handler(update, context):
    """Global handler: empêche les jobs/commandes de mourir en silence."""
    logger.error("Unhandled exception in handler/job", exc_info=context.error)


# --- App setup ---

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start",      "Introduction"),
        BotCommand("digest",     "Daily digest"),
        BotCommand("ask",        "Ask a question"),
        BotCommand("recent",     "5 latest articles"),
        BotCommand("stats",      "DB stats"),
        BotCommand("add",        "[Contributors] Add manual entry"),
        BotCommand("scrape_rss", "[Contributors] Scrape RSS"),
        BotCommand("enrich",     "[Contributors] AI enrichment"),
        BotCommand("export",     "[Contributors] Export CSV"),
    ])

def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", cmd_add)],
        states={
            ADD_TITLE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title)],
            ADD_SOURCE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_source)],
            ADD_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_content)],
            ADD_TAGS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, add_tags),
                          CommandHandler("skip", add_tags)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
    )
    app.add_handler(add_conv)
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("scrape_twitter", cmd_scrape_twitter))
    app.add_handler(CommandHandler("scrape_rss",     cmd_scrape_rss))
    app.add_handler(CommandHandler("reset_tags", cmd_reset_tags))
    app.add_handler(CommandHandler("enrich",     cmd_enrich))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("digest",     cmd_digest))
    app.add_handler(CommandHandler("recent",     cmd_recent))
    app.add_handler(CommandHandler("ask",        cmd_ask))
    app.add_handler(CommandHandler("export",     cmd_export))
    app.add_error_handler(error_handler)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(job_rss,          "cron", hour=8, minute=0)
    scheduler.add_job(job_twitter,      "cron", hour=8, minute=5)
    scheduler.add_job(job_enrich,       "cron", hour=8, minute=15)
    scheduler.add_job(job_dedup,        "cron", hour=8, minute=24)
    scheduler.add_job(job_digest,       "cron", hour=8, minute=30, args=[app])
    scheduler.add_job(job_health_check, "cron", hour=9, minute=0,  args=[app])
    scheduler.start()

    logger.info(f"Bot started - RSS 08:00, Enrich 08:15, Digest 08:30, Health 09:00 UTC | {len(RSS_FEEDS)} RSS feeds")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
