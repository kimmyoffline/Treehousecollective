import os
import sqlite3
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("treehouse_collective")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

DB_PATH = os.environ.get("DB_PATH", "/tmp/treehousecollective.sqlite").strip()

ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))
if not ADMIN_CHAT_ID:
    raise RuntimeError("ADMIN_CHAT_ID is missing")

# Optional: comma-separated Telegram user IDs allowed to use admin commands
ADMIN_USER_IDS_RAW = os.environ.get("ADMIN_USER_IDS", "").strip()
ADMIN_USER_IDS = set()
if ADMIN_USER_IDS_RAW:
    for part in ADMIN_USER_IDS_RAW.split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_USER_IDS.add(int(part))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_conn()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                last_name   TEXT,
                started_at  TEXT,
                last_seen   TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def is_admin_user(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    if uid is None:
        return False
    # If you set ADMIN_USER_IDS, enforce it. If you didn't, allow only from ADMIN_CHAT_ID.
    if ADMIN_USER_IDS:
        return uid in ADMIN_USER_IDS
    # fallback: only allow admin commands inside the admin chat
    return (update.effective_chat and update.effective_chat.id == ADMIN_CHAT_ID)


def upsert_user(u) -> bool:
    """
    Returns True if this is a NEW user (first time seen), else False.
    """
    user_id = u.id
    username = (u.username or "").strip() or None
    first_name = (u.first_name or "").strip() or None
    last_name = (u.last_name or "").strip() or None
    now = utc_now_iso()

    conn = db_conn()
    try:
        row = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        is_new = row is None

        if is_new:
            conn.execute(
                """
                INSERT INTO users (user_id, username, first_name, last_name, started_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, first_name, last_name, now, now),
            )
        else:
            conn.execute(
                """
                UPDATE users
                SET username = ?, first_name = ?, last_name = ?, last_seen = ?
                WHERE user_id = ?
                """,
                (username, first_name, last_name, now, user_id),
            )

        conn.commit()
        return is_new
    finally:
        conn.close()


def format_user_line(row: sqlite3.Row) -> str:
    uname = row["username"] or ""
    name = " ".join([p for p in [row["first_name"], row["last_name"]] if p]) or ""
    uid = row["user_id"]
    started = row["started_at"] or ""
    last_seen = row["last_seen"] or ""
    handle = f"@{uname}" if uname else "(no username)"
    return f"• {handle} | {name} | id:{uid} | started:{started} | last_seen:{last_seen}"


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    is_new = upsert_user(u)

    await update.message.reply_text("✅ Treehousecollective bot is live. Type /start anytime.")

    # Notify admin only on first time seen
    if is_new:
        uname = f"@{u.username}" if u.username else "(no username)"
        name = " ".join([p for p in [u.first_name, u.last_name] if p]) or ""
        msg = (
            "🌲 New bot user:\n"
            f"• {uname}\n"
            f"• {name}\n"
            f"• id: {u.id}\n"
            f"• time: {utc_now_iso()}"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg)
        except Exception as e:
            logger.exception("Failed to notify admin: %s", e)


async def any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Track users even if they didn't use /start (but messaged)
    if update.effective_user:
        is_new = upsert_user(update.effective_user)
        if is_new:
            u = update.effective_user
            uname = f"@{u.username}" if u.username else "(no username)"
            name = " ".join([p for p in [u.first_name, u.last_name] if p]) or ""
            msg = (
                "🌲 New bot user (first seen via message):\n"
                f"• {uname}\n"
                f"• {name}\n"
                f"• id: {u.id}\n"
                f"• time: {utc_now_iso()}"
            )
            try:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg)
            except Exception as e:
                logger.exception("Failed to notify admin: %s", e)


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return

    # /users 50  -> show last 50
    n = 50
    if context.args and context.args[0].isdigit():
        n = max(1, min(500, int(context.args[0])))

    conn = db_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY datetime(started_at) DESC LIMIT ?",
            (n,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        await update.message.reply_text("No users recorded yet.")
        return

    lines = [f"👥 Last {len(rows)} users:"]
    lines += [format_user_line(r) for r in rows]

    # Telegram message length limit safety
    text = "\n".join(lines)
    if len(text) > 3500:
        text = "\n".join(lines[:80]) + "\n\n(Truncated — use /exportcsv for full list)"
    await update.message.reply_text(text)


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return

    conn = db_conn()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        last = conn.execute(
            "SELECT * FROM users ORDER BY datetime(last_seen) DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    if last:
        last_line = format_user_line(last)
    else:
        last_line = "No users yet."

    await update.message.reply_text(f"📊 Stats\n• Total users: {total}\n• Last seen:\n{last_line}")


async def admin_exportcsv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update):
        return

    # Create CSV in /tmp for Render
    csv_path = "/tmp/treehouse_users.csv"

    conn = db_conn()
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY datetime(started_at) DESC").fetchall()
    finally:
        conn.close()

    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("user_id,username,first_name,last_name,started_at,last_seen\n")
        for r in rows:
            def esc(v: Optional[str]) -> str:
                if v is None:
                    return ""
                v = str(v).replace('"', '""')
                if "," in v or "\n" in v:
                    return f'"{v}"'
                return v

            f.write(
                f"{r['user_id']},{esc(r['username'])},{esc(r['first_name'])},{esc(r['last_name'])},"
                f"{esc(r['started_at'])},{esc(r['last_seen'])}\n"
            )

    await update.message.reply_document(
        document=open(csv_path, "rb"),
        filename="treehouse_users.csv",
        caption="✅ Exported users list",
    )


async def main():
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("users", admin_users))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("exportcsv", admin_exportcsv))

    # Track any incoming messages (so you still capture users)
    app.add_handler(MessageHandler(filters.ALL, any_message))

    logger.info("Bot starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Bot running.")
    await asyncio.Event().wait()  # keep alive forever


if __name__ == "__main__":
    asyncio.run(main())
