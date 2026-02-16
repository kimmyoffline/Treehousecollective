import os
import sqlite3
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
DB_PATH = os.environ.get("DB_PATH", "/var/data/treehouse_users.sqlite3")

def init_db():
    # Ensure directory exists if using mounted disk path
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            registered_at TEXT,
            source TEXT
        )
        """)
        con.commit()

def upsert_user(u, source=""):
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        INSERT INTO users (user_id, username, first_name, last_name, registered_at, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            registered_at=excluded.registered_at,
            source=excluded.source
        """, (
            u.id,
            u.username or "",
            u.first_name or "",
            u.last_name or "",
            now,
            source or ""
        ))
        con.commit()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    source = context.args[0] if context.args else ""
    upsert_user(user, source)

    await update.message.reply_text(
        "✅ You’re registered.\n\n"
        "If we ever lose the main channel, we can re-invite you via this bot.\n"
        "We only store your Telegram ID + public profile name/username."
    )

async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Privacy: This bot stores only your Telegram user_id and public profile details "
        "(username/name) so we can re-contact you if the channel is lost."
    )

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("privacy", privacy))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
  main())
