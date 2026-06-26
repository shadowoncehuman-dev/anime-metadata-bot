#!/usr/bin/env python3
"""
AniVault Telegram Bot
─────────────────────
Admin : full metadata enrichment control, episode enrichment, user management,
        stats, scheduling, progress broadcasting.
Users : professional anime browser — list, search, detail views with posters.
        Users see this as a streaming-library browse bot, NOT a metadata tool.
"""

import os, sys, logging, threading, asyncio, json, math
from datetime import datetime, time as dtime
from pathlib import Path

from flask import Flask
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest

import enrich_metadata as enricher
import user_manager    as um
import image_api       as imgs

# ── logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────
ADMIN_ID      = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])
BOT_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
PORT          = int(os.environ.get("PORT", 8080))
SCHEDULE_FILE = Path("/tmp/schedule.json")
PAGE_SIZE     = 10           # anime list page size

# ── job state ─────────────────────────────────────────────────────────────────
_job_lock     = threading.Lock()
_job_running  = False
_job_label    = ""
_job_start_ts = None
_job_cancel   = threading.Event()
_job_progress = {"i": 0, "total": 0, "current": ""}

# ── schedule ──────────────────────────────────────────────────────────────────
def load_schedule() -> dict:
    if SCHEDULE_FILE.exists():
        try: return json.loads(SCHEDULE_FILE.read_text())
        except: pass
    return {"enabled": False, "frequency": "daily"}

def save_schedule(data: dict) -> None:
    SCHEDULE_FILE.write_text(json.dumps(data))

# ── helpers ───────────────────────────────────────────────────────────────────
def status_text() -> str:
    if _job_running and _job_start_ts:
        elapsed = (datetime.now() - _job_start_ts).seconds
        m, s = divmod(elapsed, 60)
        p    = _job_progress
        prog = (f"\nProgress: {p['i']}/{p['total']} — `{p['current'][:50]}`"
                if p["total"] else "")
        return f"⚙️ *Running:* {_job_label}\nElapsed: {m}m {s}s{prog}"
    return "💤 *No job running.*"

def make_progress_bar(i: int, total: int) -> str:
    pct   = int(i / total * 100) if total else 0
    filled = pct // 10
    bar   = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {pct}%  ({i}/{total})"

async def safe_send_photo(bot, chat_id: int, url: str, caption: str,
                          parse_mode=ParseMode.MARKDOWN,
                          reply_markup=None) -> bool:
    """Try to send a photo; fall back to text if the URL fails."""
    if not url:
        await bot.send_message(chat_id=chat_id, text=caption,
                               parse_mode=parse_mode, reply_markup=reply_markup)
        return False
    try:
        await bot.send_photo(chat_id=chat_id, photo=url, caption=caption,
                             parse_mode=parse_mode, reply_markup=reply_markup)
        return True
    except BadRequest:
        await bot.send_message(chat_id=chat_id, text=caption,
                               parse_mode=parse_mode, reply_markup=reply_markup)
        return False

async def broadcast_progress(bot, text: str) -> None:
    """Send progress update to admin + all allowed users."""
    targets = [ADMIN_ID] + [u["id"] for u in um.list_users()]
    for cid in targets:
        try:
            await bot.send_message(chat_id=cid, text=text,
                                   parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════════════════

def admin_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Dry Run",          callback_data="dry_run"),
         InlineKeyboardButton("🧪 Test (10 rows)",   callback_data="test_run")],
        [InlineKeyboardButton("🚀 Full Run",          callback_data="full_run_ask"),
         InlineKeyboardButton("📺 Enrich Episodes",  callback_data="ep_run_ask")],
        [InlineKeyboardButton("📊 DB Stats",          callback_data="stats"),
         InlineKeyboardButton("📋 Status",            callback_data="job_status")],
        [InlineKeyboardButton("⏰ Schedule",           callback_data="schedule_menu"),
         InlineKeyboardButton("👥 Users",             callback_data="users_menu")],
        [InlineKeyboardButton("❌ Cancel Job",         callback_data="cancel_job")],
    ])

def back_btn(target: str = "admin_main") -> InlineKeyboardMarkup:
    label = "« Main Menu" if target == "admin_main" else "« Back"
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=target)]])

def schedule_menu_kb() -> InlineKeyboardMarkup:
    sched   = load_schedule()
    enabled = sched.get("enabled", False)
    freq    = sched.get("frequency", "daily")
    toggle  = "🔴 Disable" if enabled else "🟢 Enable"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{toggle} auto-run",        callback_data="sched_toggle")],
        [InlineKeyboardButton("📅 Daily  (midnight UTC)",  callback_data="sched_daily"),
         InlineKeyboardButton("📅 Weekly (Mon midnight)", callback_data="sched_weekly")],
        [InlineKeyboardButton("« Back",                    callback_data="admin_main")],
    ])

def users_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add User",    callback_data="user_add"),
         InlineKeyboardButton("➖ Remove User", callback_data="user_remove")],
        [InlineKeyboardButton("📋 List Users",  callback_data="user_list")],
        [InlineKeyboardButton("« Back",         callback_data="admin_main")],
    ])

# ── user-facing keyboards ─────────────────────────────────────────────────────
def user_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Browse Anime",   callback_data="browse_1"),
         InlineKeyboardButton("🎬 Browse Movies",  callback_data="browse_movies_1")],
        [InlineKeyboardButton("🔍 Search",          callback_data="search_prompt"),
         InlineKeyboardButton("🎲 Random Pick",    callback_data="random_pick")],
        [InlineKeyboardButton("📊 Library Stats",  callback_data="library_stats")],
    ])

def browse_keyboard(page: int, total: int, prefix: str = "browse") -> InlineKeyboardMarkup:
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"{prefix}_{page-1}"))
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"{prefix}_{page+1}"))
    return InlineKeyboardMarkup([nav, [InlineKeyboardButton("« Menu", callback_data="user_main")]])

def anime_detail_keyboard(content_id: str, back_data: str = "browse_1") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("« Back to List", callback_data=back_data)],
        [InlineKeyboardButton("🏠 Menu",          callback_data="user_main")],
    ])

# ══════════════════════════════════════════════════════════════════════════════
# JOB RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def _run_content_job(bot, chat_id: int, dry_run: bool, limit: int | None,
                     title_filter: str | None, loop: asyncio.AbstractEventLoop) -> None:
    global _job_running, _job_label, _job_start_ts, _job_progress

    def send(text: str) -> None:
        asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN),
            loop
        ).result(timeout=15)

    def send_all(text: str) -> None:
        asyncio.run_coroutine_threadsafe(broadcast_progress(bot, text), loop).result(timeout=30)

    def progress_cb(i: int, total: int, current_title: str) -> None:
        _job_progress.update({"i": i, "total": total, "current": current_title})
        if _job_cancel.is_set():
            raise InterruptedError("Job cancelled")
        if i % 25 == 0 or i == 1:
            bar = make_progress_bar(i, total)
            send_all(f"⚙️ *{_job_label}*\n{bar}\n`{current_title[:60]}`")

    try:
        label = _job_label
        send(f"▶️ *{label}* started — {limit or 'all'} rows\n"
             f"{'_(preview only — no DB writes)_' if dry_run else '_Updating your database…_'}")

        stats = enricher.run_enrichment(
            dry_run=dry_run, limit=limit,
            title_filter=title_filter, force_images=True,
            progress_cb=progress_cb,
        )

        lines      = stats["summaries"]
        chunk, cl  = [], 0
        for line in lines:
            if cl + len(line) > 3200:
                send("\n\n".join(chunk)); chunk, cl = [], 0
            chunk.append(line); cl += len(line)
        if chunk:
            send("\n\n".join(chunk))

        emoji = "🔎" if dry_run else "✅"
        send(f"{emoji} *{label} complete!*\n"
             f"Total: {stats['total']} | Updated: {stats['updated']} | "
             f"Skipped: {stats['skipped']} | Failed: {stats['failed']}")
        if not dry_run:
            send_all(f"✅ *{label}* finished — "
                     f"{stats['updated']} titles updated.")

    except InterruptedError:
        send("🛑 Job *cancelled* by you.")
    except Exception as e:
        send(f"❌ Job crashed: `{e}`")
        log.exception("content job error")
    finally:
        _job_running  = False
        _job_label    = ""
        _job_start_ts = None
        _job_cancel.clear()
        _job_progress.update({"i": 0, "total": 0, "current": ""})


def _run_episode_job(bot, chat_id: int, dry_run: bool, limit: int | None,
                     title_filter: str | None, loop: asyncio.AbstractEventLoop) -> None:
    global _job_running, _job_label, _job_start_ts, _job_progress

    def send(text: str) -> None:
        asyncio.run_coroutine_threadsafe(
            bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN),
            loop
        ).result(timeout=15)

    def send_all(text: str) -> None:
        asyncio.run_coroutine_threadsafe(broadcast_progress(bot, text), loop).result(timeout=30)

    def progress_cb(i: int, total: int, current_title: str) -> None:
        _job_progress.update({"i": i, "total": total, "current": current_title})
        if _job_cancel.is_set():
            raise InterruptedError("Job cancelled")
        if i % 10 == 0 or i == 1:
            bar = make_progress_bar(i, total)
            send_all(f"📺 *Episode Enrichment*\n{bar}\n`{current_title[:60]}`")

    try:
        label = _job_label
        send(f"📺 *{label}* started — enriching episodes…\n"
             f"{'_(preview only)_' if dry_run else '_Updating episodes…_'}")

        stats = enricher.run_episode_enrichment(
            dry_run=dry_run, limit=limit, title_filter=title_filter,
            force_images=True, progress_cb=progress_cb,
        )

        lines     = stats["summaries"]
        chunk, cl = [], 0
        for line in lines:
            if cl + len(line) > 3200:
                send("\n\n".join(chunk)); chunk, cl = [], 0
            chunk.append(line); cl += len(line)
        if chunk:
            send("\n\n".join(chunk))

        emoji = "🔎" if dry_run else "✅"
        send(f"{emoji} *{label} complete!*\n"
             f"Series processed: {stats['content_done']}\n"
             f"Episodes updated: {stats['ep_updated']} | "
             f"Skipped: {stats['ep_skipped']} | Failed: {stats['ep_failed']}")
        if not dry_run:
            send_all(f"📺 *Episode Enrichment* done — "
                     f"{stats['ep_updated']} episodes updated.")

    except InterruptedError:
        send("🛑 Job *cancelled* by you.")
    except Exception as e:
        send(f"❌ Episode job crashed: `{e}`")
        log.exception("episode job error")
    finally:
        _job_running  = False
        _job_label    = ""
        _job_start_ts = None
        _job_cancel.clear()
        _job_progress.update({"i": 0, "total": 0, "current": ""})


def start_job(bot, chat_id: int, dry_run: bool, limit: int | None,
              label: str, loop: asyncio.AbstractEventLoop,
              title_filter: str | None = None,
              job_type: str = "content") -> bool:
    global _job_running, _job_label, _job_start_ts
    with _job_lock:
        if _job_running:
            return False
        _job_running  = True
        _job_label    = label
        _job_start_ts = datetime.now()
        _job_cancel.clear()
        target = _run_content_job if job_type == "content" else _run_episode_job
        threading.Thread(
            target=target,
            args=(bot, chat_id, dry_run, limit, title_filter, loop),
            daemon=True,
        ).start()
        return True

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def admin_trigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                        dry_run: bool, limit: int | None, label: str,
                        title_filter: str | None = None,
                        job_type: str = "content") -> None:
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg: return
    if _job_running:
        m, s = divmod((datetime.now() - _job_start_ts).seconds, 60)
        await msg.reply_text(
            f"⚠️ *{_job_label}* is already running ({m}m {s}s).\n"
            "Use ❌ Cancel to stop it first.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    loop = asyncio.get_event_loop()
    start_job(ctx.bot, ADMIN_ID, dry_run, limit, label, loop, title_filter, job_type)


async def cmd_admin_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    gif = imgs.get_reaction_gif("wave")
    caption = (
        "🎌 *AniVault Admin Panel*\n\n"
        "Welcome back! Full metadata control at your fingertips.\n\n"
        "📡 Sources: *AniList* + *MyAnimeList*\n"
        "🗄 Database: *Supabase*\n\n"
        "Choose an action below:"
    )
    await safe_send_photo(ctx.bot, update.effective_chat.id,
                          gif, caption, reply_markup=admin_main_menu())


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg   = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg: return
    wait  = await msg.reply_text("⏳ Fetching database stats…")
    try:
        s   = enricher.get_db_stats()
        txt = (
            "📊 *Database Statistics*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📁 Total content:  *{s['total']}*\n"
            f"   ├ Series:      {s['series']}\n"
            f"   └ Movies:      {s['movies']}\n"
            f"⭐ Featured:       *{s['featured']}*\n\n"
            f"🎬 Episodes:       *{s['episodes']}*\n"
            f"   └ Missing thumb: {s['no_ep_thumb']}\n\n"
            f"🖼 Missing poster: *{s['no_poster']}*\n"
            f"🎨 Missing banner: *{s['no_banner']}*\n"
            f"📝 Missing desc:   *{s['no_description']}*\n"
            f"⭐ Rating = 0:     *{s['no_rating']}*\n"
            f"🏷 Genres in DB:   *{s['genres']}*"
        )
        await wait.edit_text(txt, parse_mode=ParseMode.MARKDOWN,
                             reply_markup=back_btn("admin_main"))
    except Exception as e:
        await wait.edit_text(f"❌ Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text(status_text(), parse_mode=ParseMode.MARKDOWN,
                             reply_markup=admin_main_menu())


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg: return
    if _job_running:
        _job_cancel.set()
        await msg.reply_text("🛑 Cancel signal sent — stopping after current item…",
                             reply_markup=admin_main_menu())
    else:
        await msg.reply_text("💤 No job is running.", reply_markup=admin_main_menu())


async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    sched  = load_schedule()
    s_txt  = "🟢 Enabled" if sched.get("enabled") else "🔴 Disabled"
    f_txt  = sched.get("frequency", "daily").capitalize()
    msg    = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text(
            f"⏰ *Auto-Run Schedule*\n\nStatus: {s_txt}\nFrequency: {f_txt}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=schedule_menu_kb(),
        )


async def cmd_search_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    title = " ".join(ctx.args or []).strip()
    if not title:
        await update.message.reply_text(
            "Usage: `/search <title>`\nExample: `/search Frieren`",
            parse_mode=ParseMode.MARKDOWN)
        return
    wait = await update.message.reply_text(f"🔍 Searching *{title}*…", parse_mode=ParseMode.MARKDOWN)
    try:
        result = enricher.preview_title(title)
        f      = result["fields"]
        genres = result["genres"]
        sources = []
        if result["jikan"]:   sources.append("MAL/Jikan")
        if result["anilist"]: sources.append("AniList")
        if not f:
            await wait.edit_text(f"❌ No metadata found for *{title}*", parse_mode=ParseMode.MARKDOWN)
            return
        lines = [
            f"🔎 *{title}*",
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
            f"📝 *Desc:*\n{(f.get('description') or '—')[:400]}",
        ]
        await wait.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await wait.edit_text(f"❌ Error: `{e}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    title = " ".join(ctx.args or []).strip()
    if not title:
        await update.message.reply_text(
            "Usage: `/fix <title>`\nExample: `/fix Fire Force`",
            parse_mode=ParseMode.MARKDOWN)
        return
    if _job_running:
        await update.message.reply_text("⚠️ A job is running. Wait for it to finish.")
        return
    wait = await update.message.reply_text(f"🔧 Fixing *{title}*…", parse_mode=ParseMode.MARKDOWN)
    try:
        rows = (enricher.supabase.table("content")
                .select("id,title,type,description,release_year,rating,"
                        "poster_url,banner_url,thumbnail_url,duration_minutes,status,featured")
                .ilike("title", f"%{title}%")
                .execute().data or [])
        if not rows:
            await wait.edit_text(f"❌ No content matching `{title}`", parse_mode=ParseMode.MARKDOWN)
            return
        results = []
        for row in rows[:5]:
            _, summary = enricher.enrich_item(row, dry_run=False, force_images=True)
            results.append(summary)
        await wait.edit_text("\n\n".join(results) or "Nothing changed.",
                             parse_mode=ParseMode.MARKDOWN, reply_markup=back_btn("admin_main"))
    except Exception as e:
        await wait.edit_text(f"❌ Error: `{e}`", parse_mode=ParseMode.MARKDOWN)

# ── user management ────────────────────────────────────────────────────────────
_pending_action: dict = {}   # chat_id -> {"action": "add"/"remove"}

async def show_users_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    count = um.user_count()
    msg   = update.message or (update.callback_query.message if update.callback_query else None)
    if msg:
        await msg.reply_text(
            f"👥 *User Management*\n\n"
            f"Allowed users: *{count}*\n"
            f"_(Admin is always allowed)_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=users_menu_kb(),
        )


async def show_user_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    users = um.list_users()
    msg   = update.callback_query.message if update.callback_query else update.message
    if not users:
        await msg.reply_text("📋 No allowed users yet.\nUse ➕ Add User to grant access.",
                             reply_markup=users_menu_kb())
        return
    lines = ["📋 *Allowed Users*\n"]
    for u in users:
        name = u.get("name") or "—"
        uname = f"@{u['username']}" if u.get("username") else ""
        lines.append(f"• `{u['id']}` {name} {uname}")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                         reply_markup=users_menu_kb())


# ══════════════════════════════════════════════════════════════════════════════
# USER-FACING HANDLERS  (non-admin allowed users)
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_user_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user  = update.effective_user
    name  = user.first_name or "there"
    img   = imgs.get_welcome_image()
    caption = (
        f"🎌 *Welcome to AniVault, {name}!*\n\n"
        "Your personal anime & manga library browser.\n\n"
        "🔍 Search titles, browse the collection,\n"
        "📖 read descriptions, check ratings and more.\n\n"
        "Use the menu below to get started:"
    )
    await safe_send_photo(ctx.bot, update.effective_chat.id,
                          img, caption, reply_markup=user_main_menu())


async def send_browse_page(bot, chat_id: int, page: int,
                           content_type: str | None = None,
                           search: str | None = None,
                           edit_msg=None) -> None:
    prefix   = f"browse_movies" if content_type == "movie" else "browse"
    if search:
        prefix = f"search_res"

    rows, total = enricher.browse_content(
        page=page, page_size=PAGE_SIZE,
        search=search, content_type=content_type
    )
    total_pages = max(1, math.ceil(total / PAGE_SIZE))

    if not rows:
        text = "😕 Nothing found." if search else "📭 Library is empty."
        kb   = InlineKeyboardMarkup([[InlineKeyboardButton("« Menu", callback_data="user_main")]])
        if edit_msg:
            await edit_msg.edit_text(text, reply_markup=kb)
        else:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        return

    header  = "🎬 *Movies*" if content_type == "movie" else "📚 *Anime Library*"
    if search:
        header = f"🔍 *Results for:* `{search}`"

    lines   = [f"{header}  —  Page {page}/{total_pages}  ({total} total)\n"]
    for i, r in enumerate(rows, 1):
        num    = (page - 1) * PAGE_SIZE + i
        star   = f"⭐{r['rating']}" if r.get("rating") else ""
        status = f"[{r['status']}]" if r.get("status") else ""
        year   = f"({r['release_year']})" if r.get("release_year") else ""
        lines.append(f"`{num:3d}.` *{r['title']}*  {star} {year} {status}")

    # inline buttons: tap title number → detail
    btns = []
    row_btns = []
    for i, r in enumerate(rows, 1):
        num = (page - 1) * PAGE_SIZE + i
        row_btns.append(
            InlineKeyboardButton(str(num), callback_data=f"detail_{r['id']}_{prefix}_{page}")
        )
        if len(row_btns) == 5:
            btns.append(row_btns); row_btns = []
    if row_btns:
        btns.append(row_btns)

    # nav row
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀", callback_data=f"{prefix}_{page-1}"))
    nav.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("▶", callback_data=f"{prefix}_{page+1}"))
    btns.append(nav)
    btns.append([InlineKeyboardButton("🏠 Menu", callback_data="user_main")])

    text = "\n".join(lines)
    kb   = InlineKeyboardMarkup(btns)
    if edit_msg:
        try:
            await edit_msg.edit_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except BadRequest:
            await bot.send_message(chat_id=chat_id, text=text,
                                   parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await bot.send_message(chat_id=chat_id, text=text,
                               parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def send_anime_detail(bot, chat_id: int, content_id: str,
                             back_data: str = "browse_1",
                             reply_to=None) -> None:
    wait_msg = None
    if reply_to:
        wait_msg = await reply_to.reply_text("⏳ Loading details…")

    row = enricher.get_content_detail(content_id)
    if not row:
        txt = "❌ Could not load details."
        if wait_msg:
            await wait_msg.edit_text(txt)
        else:
            await bot.send_message(chat_id=chat_id, text=txt)
        return

    genres     = ", ".join(row.get("genres") or []) or "—"
    status_ico = {"ongoing": "🟢", "completed": "✅", "upcoming": "🔵",
                  "hiatus": "⏸", "cancelled": "❌"}.get(row.get("status") or "", "❓")
    type_ico   = "🎬" if row.get("type") == "movie" else "📺"
    rating     = f"⭐ {row['rating']}/10" if row.get("rating") else "⭐ N/A"
    year       = str(row.get("release_year") or "Unknown")
    duration   = (f"⏱ {row['duration_minutes']} min/ep" if row.get("duration_minutes")
                  and row.get("type") != "movie" else
                  f"⏱ {row['duration_minutes']} min" if row.get("duration_minutes") else "")
    ep_count   = (f"📺 {row['episode_count']} episodes" if row.get("episode_count") else "")
    featured   = "✨ *Featured*\n" if row.get("featured") else ""

    desc       = (row.get("description") or "No description available.")[:600]
    if len(row.get("description") or "") > 600:
        desc += "…"

    caption = (
        f"{featured}"
        f"{type_ico} *{row['title']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{rating}  {status_ico} {(row.get('status') or '').capitalize()}\n"
        f"📅 {year}  {duration}  {ep_count}\n"
        f"🏷 {genres}\n\n"
        f"📝 {desc}"
    )

    poster = row.get("poster_url") or row.get("thumbnail_url") or ""
    kb     = anime_detail_keyboard(content_id, back_data)

    if wait_msg:
        try:
            await wait_msg.delete()
        except Exception:
            pass

    await safe_send_photo(bot, chat_id, poster, caption, reply_markup=kb)


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED /start  + /help
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if um.is_admin(chat_id):
        await cmd_admin_start(update, ctx)
    elif um.is_allowed(chat_id):
        await cmd_user_start(update, ctx)
    else:
        gif = imgs.get_reaction_gif("wave")
        caption = (
            "🎌 *AniVault*\n\n"
            "This is a private anime library bot.\n"
            "Please contact the admin for access.\n\n"
            f"Your Chat ID: `{chat_id}`"
        )
        await safe_send_photo(ctx.bot, chat_id, gif, caption)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, ctx)

# ── /dryrun /test /run /eprn shortcuts ────────────────────────────────────────
async def cmd_dryrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await admin_trigger(update, ctx, dry_run=True,  limit=None, label="Dry Run")

async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await admin_trigger(update, ctx, dry_run=False, limit=10,   label="Test (10 rows)")

async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await admin_trigger(update, ctx, dry_run=False, limit=None, label="Full Run")

async def cmd_eprun(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await admin_trigger(update, ctx, dry_run=False, limit=None,
                        label="Episode Enrichment", job_type="episodes")

async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Usage: `/adduser <user_id> [name]`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    try:
        uid  = int(args[0])
        name = " ".join(args[1:]) if len(args) > 1 else ""
        um.add_user(uid, name=name)
        await update.message.reply_text(f"✅ User `{uid}` added.", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Usage: `/removeuser <user_id>`",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    try:
        uid = int(args[0])
        ok  = um.remove_user(uid)
        msg = f"✅ User `{uid}` removed." if ok else f"❌ User `{uid}` not found."
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")

# ── user /search command ───────────────────────────────────────────────────────
async def cmd_search_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(ctx.args or []).strip()
    if not query:
        await update.message.reply_text(
            "🔍 *Search AniVault*\n\nUsage: `/search <title>`\n\nExample: `/search Naruto`",
            parse_mode=ParseMode.MARKDOWN)
        return
    ctx.user_data["search_query"] = query
    await send_browse_page(ctx.bot, update.effective_chat.id, 1, search=query)

# ══════════════════════════════════════════════════════════════════════════════
# INLINE BUTTON ROUTER
# ══════════════════════════════════════════════════════════════════════════════

async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q    = update.callback_query
    await q.answer()
    d    = q.data
    cid  = update.effective_chat.id

    # ── noop ──────────────────────────────────────────────────────────────────
    if d == "noop":
        return

    # ══════════════════ ADMIN ═════════════════════════════════════════════════
    if um.is_admin(cid):

        if d == "admin_main":
            await cmd_admin_start(update, ctx)
            return

        if d == "dry_run":
            await admin_trigger(update, ctx, dry_run=True, limit=None, label="Dry Run")
            return

        if d == "test_run":
            await admin_trigger(update, ctx, dry_run=False, limit=10, label="Test (10 rows)")
            return

        if d == "full_run_ask":
            await q.message.reply_text(
                "⚠️ *Full Run* will update ALL content rows.\n"
                "This can take 20–40 minutes. Proceed?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes, run it!", callback_data="full_run_go"),
                     InlineKeyboardButton("❌ Cancel",       callback_data="admin_main")],
                ]),
            )
            return

        if d == "full_run_go":
            await admin_trigger(update, ctx, dry_run=False, limit=None, label="Full Run")
            return

        if d == "ep_run_ask":
            await q.message.reply_text(
                "📺 *Episode Enrichment* will update thumbnails and durations "
                "for all 16 000+ episodes.\nThis can take a long time. Proceed?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes, enrich!", callback_data="ep_run_go"),
                     InlineKeyboardButton("❌ Cancel",        callback_data="admin_main")],
                ]),
            )
            return

        if d == "ep_run_go":
            await admin_trigger(update, ctx, dry_run=False, limit=None,
                                label="Episode Enrichment", job_type="episodes")
            return

        if d == "stats":
            await cmd_stats(update, ctx)
            return

        if d == "job_status":
            await cmd_status(update, ctx)
            return

        if d == "cancel_job":
            await cmd_cancel(update, ctx)
            return

        if d == "schedule_menu":
            await cmd_schedule(update, ctx)
            return

        if d == "sched_toggle":
            sched = load_schedule()
            sched["enabled"] = not sched.get("enabled", False)
            save_schedule(sched)
            state = "🟢 Enabled" if sched["enabled"] else "🔴 Disabled"
            await q.message.reply_text(f"Auto-run is now *{state}*.",
                                       parse_mode=ParseMode.MARKDOWN,
                                       reply_markup=schedule_menu_kb())
            return

        if d == "sched_daily":
            sched = load_schedule(); sched["frequency"] = "daily"; save_schedule(sched)
            await q.message.reply_text("📅 Frequency → *Daily* (midnight UTC).",
                                       parse_mode=ParseMode.MARKDOWN,
                                       reply_markup=schedule_menu_kb())
            return

        if d == "sched_weekly":
            sched = load_schedule(); sched["frequency"] = "weekly"; save_schedule(sched)
            await q.message.reply_text("📅 Frequency → *Weekly* (Mon midnight).",
                                       parse_mode=ParseMode.MARKDOWN,
                                       reply_markup=schedule_menu_kb())
            return

        if d == "users_menu":
            await show_users_menu(update, ctx)
            return

        if d == "user_list":
            await show_user_list(update, ctx)
            return

        if d == "user_add":
            _pending_action[cid] = {"action": "add"}
            await q.message.reply_text(
                "✏️ Send the *Telegram user ID* you want to allow.\n"
                "_(Tip: they can use @userinfobot to find their ID)_\n\n"
                "Format: `<user_id> [optional name]`\n"
                "Example: `123456789 John`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=back_btn("users_menu"),
            )
            return

        if d == "user_remove":
            _pending_action[cid] = {"action": "remove"}
            await q.message.reply_text(
                "✏️ Send the *Telegram user ID* to remove:\n"
                "Example: `123456789`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=back_btn("users_menu"),
            )
            return

    # ══════════════════ ALLOWED USER (+ admin browsing) ═══════════════════════
    if not um.is_allowed(cid):
        await q.message.reply_text("⛔ You don't have access to this bot.")
        return

    if d == "user_main":
        gif     = imgs.get_reaction_gif("happy")
        caption = "🎌 *AniVault* — What would you like to do?"
        await safe_send_photo(ctx.bot, cid, gif, caption, reply_markup=user_main_menu())
        return

    if d == "search_prompt":
        await q.message.reply_text(
            "🔍 *Search AniVault*\n\nSend me the title to search:\n"
            "_(or use /search <title>)_",
            parse_mode=ParseMode.MARKDOWN,
        )
        ctx.user_data["awaiting_search"] = True
        return

    if d == "random_pick":
        import random
        rows, total = enricher.browse_content(page=1, page_size=1)
        # pick a random page then random item
        if total > 1:
            rand_page = random.randint(1, min(total, 500))
            rows, _   = enricher.browse_content(page=rand_page, page_size=1)
        if rows:
            await send_anime_detail(ctx.bot, cid, rows[0]["id"], back_data="browse_1",
                                    reply_to=q.message)
        return

    if d == "library_stats":
        try:
            s   = enricher.get_db_stats()
            gif = imgs.get_reaction_gif("happy")
            txt = (
                "📊 *AniVault Library*\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📁 Total titles: *{s['total']}*\n"
                f"   ├ Anime/Series: {s['series']}\n"
                f"   └ Movies:       {s['movies']}\n"
                f"🎬 Episodes:     *{s['episodes']:,}*\n"
                f"🏷 Genres:       *{s['genres']}*"
            )
            await safe_send_photo(ctx.bot, cid, gif, txt,
                                  reply_markup=InlineKeyboardMarkup(
                                      [[InlineKeyboardButton("« Menu", callback_data="user_main")]]))
        except Exception as e:
            await q.message.reply_text(f"❌ Error: {e}")
        return

    # Browse pages: browse_<page>  or  browse_movies_<page>  or  search_res_<page>
    if d.startswith("browse_movies_"):
        page = int(d.split("_")[-1])
        await send_browse_page(ctx.bot, cid, page, content_type="movie",
                               edit_msg=q.message)
        return

    if d.startswith("browse_") and not d.startswith("browse_movies"):
        page = int(d.split("_")[-1])
        await send_browse_page(ctx.bot, cid, page, edit_msg=q.message)
        return

    if d.startswith("search_res_"):
        page  = int(d.split("_")[-1])
        query = ctx.user_data.get("search_query", "")
        await send_browse_page(ctx.bot, cid, page, search=query, edit_msg=q.message)
        return

    # Detail: detail_<content_id>_<back_prefix>_<back_page>
    if d.startswith("detail_"):
        parts      = d.split("_", 3)   # ["detail", id, prefix_part, page]
        content_id = parts[1]
        back_data  = "_".join(parts[2:]) if len(parts) > 2 else "browse_1"
        await send_anime_detail(ctx.bot, cid, content_id,
                                back_data=back_data, reply_to=q.message)
        return


# ── free-text message handler ─────────────────────────────────────────────────
async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cid  = update.effective_chat.id
    text = (update.message.text or "").strip()

    # Admin: handle pending user-management input
    if um.is_admin(cid) and cid in _pending_action:
        action = _pending_action.pop(cid)["action"]
        parts  = text.split(None, 1)
        try:
            uid  = int(parts[0])
            name = parts[1] if len(parts) > 1 else ""
            if action == "add":
                um.add_user(uid, name=name)
                await update.message.reply_text(
                    f"✅ User `{uid}` (*{name or 'no name'}*) added to allowed list.",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=users_menu_kb())
            else:
                ok = um.remove_user(uid)
                msg = f"✅ User `{uid}` removed." if ok else f"❌ User `{uid}` not found."
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN,
                                                reply_markup=users_menu_kb())
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID. Please send a number.",
                                            reply_markup=users_menu_kb())
        return

    # User: handle search input
    if um.is_allowed(cid) and ctx.user_data.get("awaiting_search"):
        ctx.user_data["awaiting_search"] = False
        ctx.user_data["search_query"]    = text
        wait = await update.message.reply_text(f"🔍 Searching for *{text}*…",
                                               parse_mode=ParseMode.MARKDOWN)
        await send_browse_page(ctx.bot, cid, 1, search=text, edit_msg=wait)
        return

    # fallback: show appropriate start menu
    await cmd_start(update, ctx)

# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

_last_scheduled_run: datetime | None = None

def scheduler_loop(bot_app, loop: asyncio.AbstractEventLoop) -> None:
    global _last_scheduled_run
    import time as _time
    while True:
        _time.sleep(60)
        sched = load_schedule()
        if not sched.get("enabled"):
            continue
        now  = datetime.utcnow()
        freq = sched.get("frequency", "daily")
        should_run = (
            (freq == "daily"  and now.hour == 0 and now.minute == 0) or
            (freq == "weekly" and now.weekday() == 0 and now.hour == 0 and now.minute == 0)
        )
        if not should_run:
            continue
        if _last_scheduled_run and (now - _last_scheduled_run).total_seconds() < 3600:
            continue
        _last_scheduled_run = now
        log.info("⏰ Scheduled auto-run triggered")
        asyncio.run_coroutine_threadsafe(
            bot_app.bot.send_message(
                chat_id=ADMIN_ID,
                text="⏰ *Scheduled auto-run starting…*",
                parse_mode=ParseMode.MARKDOWN,
            ),
            loop,
        ).result(timeout=10)
        loop2 = loop
        start_job(bot_app.bot, ADMIN_ID, dry_run=False, limit=None,
                  label="Scheduled Auto-Run", loop=loop2)

# ══════════════════════════════════════════════════════════════════════════════
# FLASK KEEP-ALIVE
# ══════════════════════════════════════════════════════════════════════════════

flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return {"status": "ok",
            "job": _job_label if _job_running else "idle",
            "progress": _job_progress}, 200

@flask_app.route("/ping")
def ping():
    return "pong", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False)

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if not enricher.SUPABASE_URL or not enricher.SUPABASE_KEY:
        sys.exit("ERROR: SUPABASE_URL and SUPABASE_ANON_KEY must be set.")

    threading.Thread(target=run_flask, daemon=True).start()
    log.info("Flask health-check on port %d", PORT)

    app = Application.builder().token(BOT_TOKEN).build()

    # ── shared (all users see /start + /help) ─────────────────────────────────
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("search", cmd_search_user))

    # ── admin-only commands (guards inside each handler) ──────────────────────
    app.add_handler(CommandHandler("dryrun",      cmd_dryrun))
    app.add_handler(CommandHandler("test",        cmd_test))
    app.add_handler(CommandHandler("run",         cmd_run))
    app.add_handler(CommandHandler("eprun",       cmd_eprun))
    app.add_handler(CommandHandler("search_meta", cmd_search_admin))
    app.add_handler(CommandHandler("fix",         cmd_fix))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("cancel",      cmd_cancel))
    app.add_handler(CommandHandler("schedule",    cmd_schedule))
    app.add_handler(CommandHandler("adduser",     cmd_adduser))
    app.add_handler(CommandHandler("removeuser",  cmd_removeuser))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Scheduler
    loop = asyncio.get_event_loop()
    threading.Thread(target=scheduler_loop, args=(app, loop), daemon=True).start()

    log.info("🚀 AniVault Bot started — polling.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
