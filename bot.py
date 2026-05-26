#!/usr/bin/env python3
"""
Telegram bot — Anime Metadata Enricher
Features: dry-run, test, full run, single-title fix, search preview,
          DB stats, scheduled auto-runs, progress updates, cancel.
"""

import os, sys, logging, threading, asyncio, json
from datetime import datetime, time as dtime
from pathlib import Path

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode
import enrich_metadata as enricher

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID  = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
PORT             = int(os.environ.get("PORT", 8080))
SCHEDULE_FILE    = Path("/tmp/schedule.json")

# ── job state ─────────────────────────────────────────────────────────────────
_job_lock      = threading.Lock()
_job_running   = False
_job_label     = ""
_job_start_ts  = None
_job_cancel    = threading.Event()
_job_progress  = {"i": 0, "total": 0, "current": ""}

# ─────────────────────────────────────────────────────────────────────────────
# Auth guard
# ─────────────────────────────────────────────────────────────────────────────
def is_allowed(update: Update) -> bool:
    return (update.effective_chat.id if update.effective_chat else None) == ALLOWED_CHAT_ID

async def deny(update: Update) -> None:
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text("⛔ Unauthorized.")

# ─────────────────────────────────────────────────────────────────────────────
# Keyboards
# ─────────────────────────────────────────────────────────────────────────────
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Dry Run (preview only)",   callback_data="dry_run")],
        [InlineKeyboardButton("🧪 Test — first 10 rows",     callback_data="test_run")],
        [InlineKeyboardButton("🚀 Full Run (all content)",   callback_data="full_run_ask")],
        [InlineKeyboardButton("📊 DB Stats",                 callback_data="stats"),
         InlineKeyboardButton("📋 Status",                   callback_data="status")],
        [InlineKeyboardButton("⏰ Schedule",                  callback_data="schedule_menu"),
         InlineKeyboardButton("❌ Cancel Job",                callback_data="cancel_job")],
    ])

def back_btn() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Main Menu", callback_data="main_menu")]])

def schedule_menu() -> InlineKeyboardMarkup:
    sched = load_schedule()
    enabled = sched.get("enabled", False)
    freq    = sched.get("frequency", "daily")
    toggle  = "🔴 Disable" if enabled else "🟢 Enable"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{toggle} auto-run",          callback_data="sched_toggle")],
        [InlineKeyboardButton("📅 Daily  (midnight UTC)",    callback_data="sched_daily"),
         InlineKeyboardButton("📅 Weekly (Mon midnight)",    callback_data="sched_weekly")],
        [InlineKeyboardButton("« Back",                      callback_data="main_menu")],
    ])

# ─────────────────────────────────────────────────────────────────────────────
# Schedule persistence
# ─────────────────────────────────────────────────────────────────────────────
def load_schedule() -> dict:
    if SCHEDULE_FILE.exists():
        try: return json.loads(SCHEDULE_FILE.read_text())
        except: pass
    return {"enabled": False, "frequency": "daily"}

def save_schedule(data: dict) -> None:
    SCHEDULE_FILE.write_text(json.dumps(data))

# ─────────────────────────────────────────────────────────────────────────────
# Job runner (background thread)
# ─────────────────────────────────────────────────────────────────────────────
def _run_job(bot, chat_id: int, dry_run: bool, limit: int | None,
             title_filter: str | None, loop: asyncio.AbstractEventLoop) -> None:
    global _job_running, _job_label, _job_start_ts, _job_progress

    def send(text: str) -> None:
        asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id=chat_id, text=text,
                             parse_mode=ParseMode.MARKDOWN),
            loop
        ).result(timeout=15)

    def progress_cb(i: int, total: int, current_title: str) -> None:
        _job_progress.update({"i": i, "total": total, "current": current_title})
        if _job_cancel.is_set():
            raise InterruptedError("Job cancelled by user")
        # send progress every 25 items
        if i % 25 == 0 or i == 1:
            pct = int(i / total * 100) if total else 0
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            send(f"⚙️ [{bar}] {pct}%  ({i}/{total})\n`{current_title[:60]}`")

    try:
        label = _job_label
        send(f"▶️ *{label}* started — {limit or 'all'} rows\n"
             f"{'_(no DB writes)_' if dry_run else '_Updating your database..._'}")

        stats = enricher.run_enrichment(
            dry_run=dry_run, limit=limit,
            title_filter=title_filter,
            force_images=True,
            progress_cb=progress_cb,
        )

        # send results in chunks (Telegram 4096 char limit)
        lines = stats["summaries"]
        chunk, chunk_len = [], 0
        for line in lines:
            if chunk_len + len(line) > 3200:
                send("\n\n".join(chunk))
                chunk, chunk_len = [], 0
            chunk.append(line)
            chunk_len += len(line)
        if chunk:
            send("\n\n".join(chunk))

        emoji = "🔎" if dry_run else "✅"
        send(
            f"{emoji} *{label} complete!*\n"
            f"Total: {stats['total']} | "
            f"Updated: {stats['updated']} | "
            f"Skipped: {stats['skipped']} | "
            f"Failed: {stats['failed']}"
        )

    except InterruptedError:
        send("🛑 Job was *cancelled* by you.")
    except Exception as e:
        send(f"❌ Job crashed: `{e}`")
        log.exception("Job thread error")
    finally:
        _job_running = False
        _job_label   = ""
        _job_start_ts = None
        _job_cancel.clear()
        _job_progress.update({"i": 0, "total": 0, "current": ""})


def start_job(bot, chat_id: int, dry_run: bool, limit: int | None,
              label: str, loop: asyncio.AbstractEventLoop,
              title_filter: str | None = None) -> bool:
    global _job_running, _job_label, _job_start_ts
    with _job_lock:
        if _job_running:
            return False
        _job_running  = True
        _job_label    = label
        _job_start_ts = datetime.now()
        _job_cancel.clear()
        t = threading.Thread(
            target=_run_job,
            args=(bot, chat_id, dry_run, limit, title_filter, loop),
            daemon=True,
        )
        t.start()
        return True

# ─────────────────────────────────────────────────────────────────────────────
# Status helpers
# ─────────────────────────────────────────────────────────────────────────────
def status_text() -> str:
    if _job_running and _job_start_ts:
        elapsed = (datetime.now() - _job_start_ts).seconds
        m, s = divmod(elapsed, 60)
        p = _job_progress
        prog = (f"\nProgress: {p['i']}/{p['total']} — `{p['current'][:50]}`"
                if p["total"] else "")
        return f"⚙️ *Running:* {_job_label}\nElapsed: {m}m {s}s{prog}"
    return "💤 *No job running.* Use the menu to start one."

# ─────────────────────────────────────────────────────────────────────────────
# /start  /help
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update): await deny(update); return
    await update.message.reply_text(
        "🎌 *Anime Metadata Enricher*\n\n"
        "I keep your Supabase anime/manga database up-to-date using "
        "*AniList* and *MyAnimeList* — fixing images, ratings, "
        "descriptions, and genres automatically.\n\n"
        "*Commands:*\n"
        "/start — Main menu\n"
        "/dryrun — Preview what would change\n"
        "/test — Update first 10 rows\n"
        "/run — Full enrichment (all rows)\n"
        "/search <title> — Preview metadata for any title\n"
        "/fix <title> — Fix one specific title in DB\n"
        "/stats — Database statistics\n"
        "/status — Current job progress\n"
        "/cancel — Stop running job\n"
        "/schedule — Auto-run settings\n"
        "/help — This message",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)

# ─────────────────────────────────────────────────────────────────────────────
# /stats
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update): await deny(update); return
    msg = update.message or update.callback_query.message
    wait = await msg.reply_text("⏳ Fetching stats...")
    try:
        s = enricher.get_db_stats()
        text = (
            "📊 *Database Statistics*\n\n"
            f"📁 Total content: *{s['total']}*\n"
            f"  ├ Series: {s['series']}\n"
            f"  └ Movies: {s['movies']}\n\n"
            f"🖼 Missing poster:      *{s['no_poster']}*\n"
            f"🎨 Missing banner:      *{s['no_banner']}*\n"
            f"📝 Missing description: *{s['no_description']}*\n"
            f"⭐ Rating = 0:          *{s['no_rating']}*\n"
            f"🏷 Genres in DB:        *{s['genres']}*"
        )
        await wait.edit_text(text, parse_mode=ParseMode.MARKDOWN,
                             reply_markup=back_btn())
    except Exception as e:
        await wait.edit_text(f"❌ Error: `{e}`", parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────────────────────
# /status
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update): await deny(update); return
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text(status_text(), parse_mode=ParseMode.MARKDOWN,
                             reply_markup=main_menu())

# ─────────────────────────────────────────────────────────────────────────────
# /cancel
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update): await deny(update); return
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg: return
    if _job_running:
        _job_cancel.set()
        await msg.reply_text("🛑 Cancel signal sent — stopping after current item...",
                             reply_markup=main_menu())
    else:
        await msg.reply_text("💤 No job is running.", reply_markup=main_menu())

# ─────────────────────────────────────────────────────────────────────────────
# /dryrun  /test  /run  (command shortcuts)
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_dryrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update): await deny(update); return
    await _trigger(update, ctx, dry_run=True, limit=None, label="Dry Run")

async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update): await deny(update); return
    await _trigger(update, ctx, dry_run=False, limit=10, label="Test (10 rows)")

async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update): await deny(update); return
    await _trigger(update, ctx, dry_run=False, limit=None, label="Full Run")

async def _trigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                   dry_run: bool, limit: int | None, label: str,
                   title_filter: str | None = None) -> None:
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg: return
    if _job_running:
        m, s = divmod((datetime.now() - _job_start_ts).seconds, 60)
        await msg.reply_text(
            f"⚠️ *{_job_label}* is already running ({m}m {s}s).\n"
            "Use /cancel to stop it first.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    loop = asyncio.get_event_loop()
    ok   = start_job(ctx.bot, ALLOWED_CHAT_ID, dry_run, limit, label, loop, title_filter)
    if not ok:
        await msg.reply_text("⚠️ Could not start — try again.")

# ─────────────────────────────────────────────────────────────────────────────
# /search <title>  — preview metadata without touching DB
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update): await deny(update); return
    title = " ".join(ctx.args or []).strip()
    if not title:
        await update.message.reply_text(
            "Usage: `/search <title>`\nExample: `/search Frieren`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    wait = await update.message.reply_text(f"🔍 Searching for *{title}*...",
                                           parse_mode=ParseMode.MARKDOWN)
    try:
        result = enricher.preview_title(title)
        f      = result["fields"]
        genres = result["genres"]
        sources = []
        if result["jikan"]:   sources.append("MAL/Jikan")
        if result["anilist"]: sources.append("AniList")

        if not f:
            await wait.edit_text(f"❌ No metadata found for *{title}*",
                                 parse_mode=ParseMode.MARKDOWN)
            return

        lines = [
            f"🔎 *Search result for:* `{title}`",
            f"📡 Source: {' + '.join(sources) or 'none'}",
            "",
            f"🖼 Poster: {f.get('poster_url') or '—'}",
            f"🎨 Banner: {f.get('banner_url') or '—'}",
            f"⭐ Rating: {f.get('rating') or '—'}",
            f"📅 Year:   {f.get('release_year') or '—'}",
            f"📌 Status: {f.get('status') or '—'}",
            f"⏱ Duration: {f.get('duration_minutes') or '—'} min",
            f"🏷 Genres: {', '.join(genres) or '—'}",
            "",
            f"📝 *Description:*\n{(f.get('description') or '—')[:400]}{'…' if len(f.get('description') or '') > 400 else ''}",
        ]
        await wait.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await wait.edit_text(f"❌ Error: `{e}`", parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────────────────────
# /fix <title>  — fix one specific title in the DB
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update): await deny(update); return
    title = " ".join(ctx.args or []).strip()
    if not title:
        await update.message.reply_text(
            "Usage: `/fix <title>`\nExample: `/fix Fire Force`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if _job_running:
        await update.message.reply_text("⚠️ A job is running. Try after it finishes.")
        return

    wait = await update.message.reply_text(
        f"🔧 Fixing *{title}* in DB...", parse_mode=ParseMode.MARKDOWN
    )
    # find matching rows
    try:
        rows = (enricher.supabase.table("content")
                .select("id,title,type,description,release_year,rating,"
                        "poster_url,banner_url,thumbnail_url,duration_minutes,status")
                .ilike("title", f"%{title}%")
                .execute().data or [])
        if not rows:
            await wait.edit_text(f"❌ No content found matching `{title}`",
                                 parse_mode=ParseMode.MARKDOWN)
            return
        results = []
        for row in rows[:5]:  # cap at 5 matches
            changed, summary = enricher.enrich_item(row, dry_run=False, force_images=True)
            results.append(summary)

        await wait.edit_text("\n\n".join(results) or "Nothing changed.",
                             parse_mode=ParseMode.MARKDOWN,
                             reply_markup=back_btn())
    except Exception as e:
        await wait.edit_text(f"❌ Error: `{e}`", parse_mode=ParseMode.MARKDOWN)

# ─────────────────────────────────────────────────────────────────────────────
# /schedule
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update): await deny(update); return
    sched = load_schedule()
    status_str = "🟢 Enabled" if sched.get("enabled") else "🔴 Disabled"
    freq_str   = sched.get("frequency", "daily").capitalize()
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text(
            f"⏰ *Auto-run Schedule*\n\nStatus: {status_str}\nFrequency: {freq_str}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=schedule_menu(),
        )

# ─────────────────────────────────────────────────────────────────────────────
# Inline button router
# ─────────────────────────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not is_allowed(update): await deny(update); return

    d = q.data

    if d == "main_menu":
        await q.message.reply_text("🏠 Main Menu", reply_markup=main_menu())

    elif d == "dry_run":
        await _trigger(update, ctx, dry_run=True, limit=None, label="Dry Run")

    elif d == "test_run":
        await _trigger(update, ctx, dry_run=False, limit=10, label="Test (10 rows)")

    elif d == "full_run_ask":
        await q.message.reply_text(
            "⚠️ *Full Run* will update ALL 491+ content rows.\n"
            "This can take 20-30 minutes. Proceed?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, run it!",  callback_data="full_run_go"),
                 InlineKeyboardButton("❌ Cancel",        callback_data="main_menu")],
            ]),
        )

    elif d == "full_run_go":
        await _trigger(update, ctx, dry_run=False, limit=None, label="Full Run")

    elif d == "stats":
        await cmd_stats(update, ctx)

    elif d == "status":
        await cmd_status(update, ctx)

    elif d == "cancel_job":
        await cmd_cancel(update, ctx)

    elif d == "schedule_menu":
        await cmd_schedule(update, ctx)

    elif d == "sched_toggle":
        sched = load_schedule()
        sched["enabled"] = not sched.get("enabled", False)
        save_schedule(sched)
        state = "🟢 Enabled" if sched["enabled"] else "🔴 Disabled"
        await q.message.reply_text(f"Auto-run is now *{state}*.",
                                   parse_mode=ParseMode.MARKDOWN,
                                   reply_markup=schedule_menu())

    elif d == "sched_daily":
        sched = load_schedule()
        sched["frequency"] = "daily"
        save_schedule(sched)
        await q.message.reply_text("📅 Frequency set to *Daily* (midnight UTC).",
                                   parse_mode=ParseMode.MARKDOWN,
                                   reply_markup=schedule_menu())

    elif d == "sched_weekly":
        sched = load_schedule()
        sched["frequency"] = "weekly"
        save_schedule(sched)
        await q.message.reply_text("📅 Frequency set to *Weekly* (Monday midnight UTC).",
                                   parse_mode=ParseMode.MARKDOWN,
                                   reply_markup=schedule_menu())

# ─────────────────────────────────────────────────────────────────────────────
# Scheduler loop (runs in background thread, checks every minute)
# ─────────────────────────────────────────────────────────────────────────────
_last_scheduled_run: datetime | None = None

def scheduler_loop(bot_app, loop: asyncio.AbstractEventLoop) -> None:
    global _last_scheduled_run
    import time as _time
    while True:
        _time.sleep(60)
        sched = load_schedule()
        if not sched.get("enabled"):
            continue
        now = datetime.utcnow()
        freq = sched.get("frequency", "daily")
        should_run = False
        if freq == "daily" and now.hour == 0 and now.minute == 0:
            should_run = True
        elif freq == "weekly" and now.weekday() == 0 and now.hour == 0 and now.minute == 0:
            should_run = True
        if should_run:
            if _last_scheduled_run and (now - _last_scheduled_run).total_seconds() < 3600:
                continue  # debounce
            _last_scheduled_run = now
            log.info("⏰ Scheduled auto-run triggered")
            asyncio.run_coroutine_threadsafe(
                bot_app.bot.send_message(
                    chat_id=ALLOWED_CHAT_ID,
                    text="⏰ *Scheduled auto-run starting...*",
                    parse_mode=ParseMode.MARKDOWN,
                ),
                loop,
            ).result(timeout=10)
            start_job(bot_app.bot, ALLOWED_CHAT_ID,
                      dry_run=False, limit=None,
                      label="Scheduled Auto-Run", loop=loop)

# ─────────────────────────────────────────────────────────────────────────────
# Flask keep-alive (Render free tier needs HTTP traffic to stay up)
# ─────────────────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return {
        "status": "ok",
        "job": _job_label if _job_running else "idle",
        "progress": _job_progress,
    }, 200

@flask_app.route("/ping")
def ping():
    return "pong", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False)

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    if not enricher.SUPABASE_URL or not enricher.SUPABASE_KEY:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_ANON_KEY must be set.")

    # Flask keep-alive
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("Flask keep-alive on port %d", PORT)

    # Build Telegram app
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("dryrun",   cmd_dryrun))
    app.add_handler(CommandHandler("test",     cmd_test))
    app.add_handler(CommandHandler("run",      cmd_run))
    app.add_handler(CommandHandler("search",   cmd_search))
    app.add_handler(CommandHandler("fix",      cmd_fix))
    app.add_handler(CommandHandler("stats",    cmd_stats))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("cancel",   cmd_cancel))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Scheduler
    loop = asyncio.get_event_loop()
    threading.Thread(target=scheduler_loop, args=(app, loop), daemon=True).start()

    log.info("Bot polling started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
