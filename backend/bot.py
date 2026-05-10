"""
Telegram bot for Novaex AI auto-registration.
Provides:
- Inline keyboard menu (Buat Akun, Export Akun, Kelola Proxy, Status, Reset CSV)
- Multi-step conversation to collect: count -> threads -> invite -> GA
- Live progress updates with a CANCEL button
- On cancel: already-saved accounts remain in CSV (append-on-success)
- Proxy management: upload .txt, view count, delete
- Export accounts.csv as a Telegram document

Designed for python-telegram-bot v21.x.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    BotCommand,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from registrar import (
    JobConfig,
    JobStats,
    DEFAULT_INVITE,
    MAX_THREADS,
    count_csv_rows,
    run_job,
    stats_snapshot,
)

log = logging.getLogger("novaex_bot")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PROXY_PATH = DATA_DIR / "proxies.txt"
CSV_PATH = DATA_DIR / "accounts.csv"

ADMIN_IDS_RAW = os.environ.get("ADMIN_USER_IDS", "").strip()
ADMIN_IDS = {int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()}


def is_admin(uid: int) -> bool:
    if not ADMIN_IDS:
        return True  # public mode if not configured
    return uid in ADMIN_IDS


# ---------------------------------------------------------------------------
# Per-user session state (in-memory)
# ---------------------------------------------------------------------------
@dataclass
class JobSession:
    cancel_event: threading.Event
    chat_id: int
    message_id: int
    config: JobConfig
    stats: JobStats
    last_render: float = 0.0


@dataclass
class WizardState:
    step: str                    # waiting for: count | threads | invite | ga | confirm | upload_proxy
    count: int = 0
    threads: int = 3
    invite: str = DEFAULT_INVITE
    no_ga: bool = False


# user_id -> WizardState
WIZARDS: dict[int, WizardState] = {}
# user_id -> JobSession
JOBS: dict[int, JobSession] = {}


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Buat Akun", callback_data="menu:create")],
        [
            InlineKeyboardButton("📤 Export Akun", callback_data="menu:export"),
            InlineKeyboardButton("📊 Status", callback_data="menu:status"),
        ],
        [InlineKeyboardButton("🌐 Kelola Proxy", callback_data="menu:proxy")],
        [
            InlineKeyboardButton("🗑 Reset CSV", callback_data="menu:reset_csv"),
            InlineKeyboardButton("🆔 My ID", callback_data="menu:myid"),
        ],
    ])


def proxy_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️ Upload proxy.txt", callback_data="proxy:upload")],
        [
            InlineKeyboardButton("👁 Lihat Info", callback_data="proxy:view"),
            InlineKeyboardButton("🗑 Hapus", callback_data="proxy:delete"),
        ],
        [InlineKeyboardButton("⬅️ Kembali", callback_data="menu:home")],
    ])


def confirm_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Ya", callback_data=f"{prefix}:yes"),
            InlineKeyboardButton("❌ Tidak", callback_data=f"{prefix}:no"),
        ],
    ])


def cancel_only_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel & Simpan", callback_data="job:cancel")],
    ])


def back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="menu:home")]])


def ga_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Bind GA", callback_data="ga:yes"),
            InlineKeyboardButton("⏭ Skip GA", callback_data="ga:no"),
        ],
        [InlineKeyboardButton("⬅️ Batal", callback_data="menu:home")],
    ])


def render_progress(sess: JobSession) -> str:
    s = sess.config
    snap = stats_snapshot(sess.stats)
    bar_total = 20
    pct = (snap["success"] + snap["failed"]) / max(1, snap["total"])
    filled = int(pct * bar_total)
    bar = "█" * filled + "░" * (bar_total - filled)
    state = "🟡 Berjalan"
    if snap["cancelled"]:
        state = "🟠 Cancelling..."
    return (
        f"<b>🚀 Pendaftaran Novaex AI</b>\n"
        f"<code>{bar}</code>  {int(pct*100)}%\n\n"
        f"Status     : {state}\n"
        f"Target     : <b>{snap['total']}</b> akun\n"
        f"Berhasil   : <b>{snap['success']}</b>\n"
        f"Gagal      : <b>{snap['failed']}</b>\n"
        f"Threads    : {s.threads} | Invite: <code>{s.invite}</code> | GA: {'off' if s.no_ga else 'on'}\n"
        f"Elapsed    : {snap['elapsed']}s\n\n"
        f"<i>Setiap akun yang berhasil sudah disimpan ke CSV — aman untuk cancel.</i>"
    )


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------
async def _gate(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        if update.callback_query:
            await update.callback_query.answer("Akses ditolak", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(
                f"⛔ Bot ini private. User ID Anda: <code>{uid}</code>\n"
                f"Minta admin untuk menambahkan ke <code>ADMIN_USER_IDS</code>.",
                parse_mode=ParseMode.HTML,
            )
        return False
    return True


# ---------------------------------------------------------------------------
# Command / callback handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _gate(update):
        return
    WIZARDS.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "<b>🤖 Novaex AI Auto-Register Bot</b>\n\n"
        "Pilih menu di bawah untuk mulai. Setiap akun yang berhasil dibuat akan langsung disimpan ke CSV "
        "sehingga aman jika Anda menekan Cancel di tengah jalan.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"User ID Anda: <code>{uid}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _gate(update):
        return
    uid = update.effective_user.id
    WIZARDS.pop(uid, None)
    sess = JOBS.get(uid)
    if sess:
        sess.cancel_event.set()
        await update.message.reply_text("⏹ Cancel dikirim ke job aktif. Akun yang sudah berhasil tetap tersimpan.")
    else:
        await update.message.reply_text("Tidak ada operasi aktif. /start untuk membuka menu.")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _gate(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = update.effective_user.id

    if data == "menu:home":
        WIZARDS.pop(uid, None)
        await q.edit_message_text(
            "<b>🤖 Novaex AI Auto-Register Bot</b>\nPilih menu:",
            parse_mode=ParseMode.HTML, reply_markup=main_menu_kb(),
        )
        return

    if data == "menu:myid":
        await q.edit_message_text(
            f"User ID Anda: <code>{uid}</code>",
            parse_mode=ParseMode.HTML, reply_markup=back_home_kb(),
        )
        return

    if data == "menu:create":
        if uid in JOBS:
            await q.edit_message_text(
                "Ada job aktif. Buka pesan progress lalu tekan Cancel terlebih dahulu.",
                reply_markup=back_home_kb(),
            )
            return
        WIZARDS[uid] = WizardState(step="count")
        await q.edit_message_text(
            "<b>🚀 Buat Akun</b>\n\nKirim <b>jumlah akun</b> yang ingin dibuat (1 - 200):",
            parse_mode=ParseMode.HTML, reply_markup=back_home_kb(),
        )
        return

    if data == "menu:status":
        n = count_csv_rows(str(CSV_PATH))
        proxies = 0
        if PROXY_PATH.exists():
            with open(PROXY_PATH) as f:
                proxies = sum(1 for line in f if line.strip() and not line.startswith("#"))
        await q.edit_message_text(
            f"<b>📊 Status</b>\n\n"
            f"Akun tersimpan : <b>{n}</b>\n"
            f"Proxy aktif    : <b>{proxies}</b>\n"
            f"Job aktif      : <b>{'Ya' if uid in JOBS else 'Tidak'}</b>\n"
            f"CSV path       : <code>{CSV_PATH}</code>",
            parse_mode=ParseMode.HTML, reply_markup=back_home_kb(),
        )
        return

    if data == "menu:export":
        if not CSV_PATH.exists() or count_csv_rows(str(CSV_PATH)) == 0:
            await q.edit_message_text(
                "Belum ada akun di CSV. Buat akun dulu via menu 🚀.",
                reply_markup=back_home_kb(),
            )
            return
        n = count_csv_rows(str(CSV_PATH))
        await q.edit_message_text(
            f"📤 Mengirim CSV ({n} akun)...",
            reply_markup=back_home_kb(),
        )
        with open(CSV_PATH, "rb") as f:
            await context.bot.send_document(
                chat_id=q.message.chat_id,
                document=InputFile(f, filename="accounts.csv"),
                caption=f"📦 accounts.csv — {n} akun",
            )
        return

    if data == "menu:reset_csv":
        if not CSV_PATH.exists():
            await q.edit_message_text("CSV belum ada.", reply_markup=back_home_kb())
            return
        await q.edit_message_text(
            "⚠️ Yakin ingin <b>menghapus seluruh accounts.csv</b>?",
            parse_mode=ParseMode.HTML, reply_markup=confirm_kb("reset"),
        )
        return

    if data == "reset:yes":
        try:
            CSV_PATH.unlink(missing_ok=True)
            await q.edit_message_text("✅ accounts.csv dihapus.", reply_markup=back_home_kb())
        except Exception as e:
            await q.edit_message_text(f"Gagal menghapus: {e}", reply_markup=back_home_kb())
        return

    if data == "reset:no":
        await q.edit_message_text("Dibatalkan.", reply_markup=main_menu_kb())
        return

    if data == "menu:proxy":
        info = "Belum ada file proxy."
        if PROXY_PATH.exists():
            with open(PROXY_PATH) as f:
                lines = [l for l in f if l.strip() and not l.startswith("#")]
            info = f"Proxy tersimpan: <b>{len(lines)}</b> baris"
        await q.edit_message_text(
            f"<b>🌐 Kelola Proxy</b>\n\n{info}\n\n"
            f"Format yang didukung: <code>user:pass@host:port</code>, <code>host:port</code>, "
            f"<code>host:port:user:pass</code>, atau <code>http://...</code>.",
            parse_mode=ParseMode.HTML, reply_markup=proxy_menu_kb(),
        )
        return

    if data == "proxy:upload":
        WIZARDS[uid] = WizardState(step="upload_proxy")
        await q.edit_message_text(
            "⬆️ Kirim file <code>.txt</code> berisi daftar proxy (1 baris per proxy).",
            parse_mode=ParseMode.HTML, reply_markup=back_home_kb(),
        )
        return

    if data == "proxy:view":
        if not PROXY_PATH.exists():
            await q.edit_message_text("Belum ada file proxy.", reply_markup=proxy_menu_kb())
            return
        with open(PROXY_PATH) as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        sample = "\n".join(lines[:10]) or "(kosong)"
        await q.edit_message_text(
            f"<b>👁 Info Proxy</b>\nTotal: <b>{len(lines)}</b>\n\nContoh 10 baris:\n<pre>{sample}</pre>",
            parse_mode=ParseMode.HTML, reply_markup=proxy_menu_kb(),
        )
        return

    if data == "proxy:delete":
        try:
            PROXY_PATH.unlink(missing_ok=True)
            await q.edit_message_text("🗑 File proxy dihapus.", reply_markup=proxy_menu_kb())
        except Exception as e:
            await q.edit_message_text(f"Gagal: {e}", reply_markup=proxy_menu_kb())
        return

    if data.startswith("ga:"):
        wiz = WIZARDS.get(uid)
        if not wiz or wiz.step != "ga":
            await q.edit_message_text("Sesi wizard kadaluarsa.", reply_markup=back_home_kb())
            return
        wiz.no_ga = (data == "ga:no")
        wiz.step = "confirm"
        await q.edit_message_text(_summary_text(wiz), parse_mode=ParseMode.HTML,
                                   reply_markup=confirm_kb("start"))
        return

    if data == "start:yes":
        wiz = WIZARDS.pop(uid, None)
        if not wiz:
            await q.edit_message_text("Sesi kadaluarsa.", reply_markup=back_home_kb())
            return
        await _start_job(update, context, wiz)
        return

    if data == "start:no":
        WIZARDS.pop(uid, None)
        await q.edit_message_text("Dibatalkan.", reply_markup=main_menu_kb())
        return

    if data == "job:cancel":
        sess = JOBS.get(uid)
        if not sess:
            await q.answer("Tidak ada job aktif", show_alert=True)
            return
        sess.cancel_event.set()
        await q.answer("Cancel dikirim — akun yang sudah berhasil tetap disimpan", show_alert=True)
        return


def _summary_text(wiz: WizardState) -> str:
    return (
        "<b>Konfirmasi pendaftaran</b>\n\n"
        f"Jumlah akun : <b>{wiz.count}</b>\n"
        f"Threads     : <b>{wiz.threads}</b>\n"
        f"Invite code : <code>{wiz.invite}</code>\n"
        f"Bind GA     : <b>{'tidak' if wiz.no_ga else 'ya'}</b>\n\n"
        f"Lanjutkan?"
    )


# ---------------------------------------------------------------------------
# Text + document handlers (wizard inputs / proxy upload)
# ---------------------------------------------------------------------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _gate(update):
        return
    uid = update.effective_user.id
    wiz = WIZARDS.get(uid)
    if not wiz:
        return  # ignore stray text
    text = (update.message.text or "").strip()

    if wiz.step == "count":
        if not text.isdigit() or not (1 <= int(text) <= 200):
            await update.message.reply_text("Masukkan angka 1 - 200.")
            return
        wiz.count = int(text)
        wiz.step = "threads"
        await update.message.reply_text(
            f"Jumlah <b>thread</b> paralel? (1 - {MAX_THREADS}, default 3)",
            parse_mode=ParseMode.HTML,
        )
        return

    if wiz.step == "threads":
        try:
            t = int(text)
        except ValueError:
            t = 3
        wiz.threads = max(1, min(MAX_THREADS, t))
        wiz.step = "invite"
        await update.message.reply_text(
            f"Masukkan <b>invite code</b> (default <code>{DEFAULT_INVITE}</code>). "
            f"Kirim <code>-</code> untuk pakai default.",
            parse_mode=ParseMode.HTML,
        )
        return

    if wiz.step == "invite":
        wiz.invite = DEFAULT_INVITE if text in ("", "-") else text
        wiz.step = "ga"
        await update.message.reply_text(
            "Bind <b>Google Authenticator</b> ke setiap akun?",
            parse_mode=ParseMode.HTML, reply_markup=ga_kb(),
        )
        return


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _gate(update):
        return
    uid = update.effective_user.id
    wiz = WIZARDS.get(uid)
    if not wiz or wiz.step != "upload_proxy":
        return
    doc = update.message.document
    if not doc:
        return
    if not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("Harus file .txt")
        return
    file = await doc.get_file()
    PROXY_PATH.parent.mkdir(parents=True, exist_ok=True)
    await file.download_to_drive(custom_path=str(PROXY_PATH))
    WIZARDS.pop(uid, None)
    with open(PROXY_PATH) as f:
        lines = [l for l in f if l.strip() and not l.startswith("#")]
    await update.message.reply_text(
        f"✅ Proxy disimpan ({len(lines)} baris).",
        reply_markup=main_menu_kb(),
    )


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------
async def _start_job(update: Update, context: ContextTypes.DEFAULT_TYPE, wiz: WizardState):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    cfg = JobConfig(
        count=wiz.count,
        threads=wiz.threads,
        invite=wiz.invite,
        invite_mode="random",
        no_ga=wiz.no_ga,
        retries=4,
        max_proxy_swaps=3,
        proxy_file=str(PROXY_PATH),
        csv_path=str(CSV_PATH),
    )

    cancel_event = threading.Event()
    stats = JobStats(total=cfg.count)

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text="<b>🚀 Mempersiapkan job...</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=cancel_only_kb(),
    )

    sess = JobSession(
        cancel_event=cancel_event,
        chat_id=chat_id,
        message_id=msg.message_id,
        config=cfg,
        stats=stats,
    )
    JOBS[uid] = sess

    loop = asyncio.get_running_loop()

    def progress_cb(event: dict):
        sess.stats = sess.stats  # in-place updates
        # Throttle UI updates to ~ once per 1.5s
        now = time.time()
        if event.get("type") in ("phase", "error") or (now - sess.last_render) > 1.5:
            sess.last_render = now
            asyncio.run_coroutine_threadsafe(_render_progress(context, sess, event), loop)

    def runner():
        try:
            final_stats = run_job(cfg, cancel_event, progress_cb)
            sess.stats = final_stats
        finally:
            asyncio.run_coroutine_threadsafe(_finish_job(context, sess), loop)

    threading.Thread(target=runner, name=f"novaex-job-{uid}", daemon=True).start()


async def _render_progress(context, sess: JobSession, event: dict):
    try:
        await context.bot.edit_message_text(
            chat_id=sess.chat_id, message_id=sess.message_id,
            text=render_progress(sess),
            parse_mode=ParseMode.HTML,
            reply_markup=cancel_only_kb(),
        )
    except Exception:
        pass  # rate-limit / no-change errors are fine


async def _finish_job(context, sess: JobSession):
    snap = stats_snapshot(sess.stats)
    state = "✅ Selesai"
    if snap["cancelled"]:
        state = "🟠 Dibatalkan"
    text = (
        f"<b>{state}</b>\n\n"
        f"Berhasil : <b>{snap['success']}</b>\n"
        f"Gagal    : <b>{snap['failed']}</b>\n"
        f"Total    : <b>{snap['total']}</b>\n"
        f"Durasi   : {snap['elapsed']}s\n\n"
        f"Akun tersimpan di <code>{sess.config.csv_path}</code>. "
        f"Gunakan menu <b>📤 Export Akun</b> untuk mengunduh."
    )
    try:
        await context.bot.edit_message_text(
            chat_id=sess.chat_id, message_id=sess.message_id,
            text=text, parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb(),
        )
    except Exception:
        await context.bot.send_message(chat_id=sess.chat_id, text=text,
                                        parse_mode=ParseMode.HTML,
                                        reply_markup=main_menu_kb())
    # remove from active jobs (find by session)
    for uid, s in list(JOBS.items()):
        if s is sess:
            JOBS.pop(uid, None)
            break


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
def build_application() -> Optional[Application]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot disabled.")
        return None

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    async def post_init(a: Application):
        await a.bot.set_my_commands([
            BotCommand("start", "Buka menu utama"),
            BotCommand("menu", "Buka menu utama"),
            BotCommand("myid", "Tampilkan user ID Anda"),
            BotCommand("cancel", "Cancel job aktif"),
        ])

    app.post_init = post_init
    return app
