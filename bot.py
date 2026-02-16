import os
import sqlite3
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("treehouse_register")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing (set it in Render Environment Variables)")

# IMPORTANT: Render’s filesystem is read-only except /tmp unless you attach a Disk.
# So default DB lives in /tmp.
DB_PATH = os.environ.get("DB_PATH", "/tmp/treehouse_register.sqlite").strip()

def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS registrations (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                source TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        con.commit()

def upsert_user(user, source: str) -> None:
    created_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO registrations (telegram_id, username, first_name, last_name, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                source=excluded.source
            """
            ,
            (
                user.id,
                user.username or "",
                user.first_name or "",
                user.last_name or "",
                source or "",
                created_at,
            ),
        )
        con.commit()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    # deep-link: /start treehouse  -> context.args[0] == "treehouse"
    source = context.args[0] if context.args else ""
    upsert_user(user, source)

    await update.message.reply_text(
        "✅ You're registered.\n\n"
        "If we ever lose the main channel, we can re-invite you via this bot.\n"
        "We only store your Telegram ID + public profile name/username."
    )

async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Privacy: This bot stores only your Telegram user_id and public profile details "
        "(username/name) so we can re-contact you if the channel is lost."
    )

def main() -> None:
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("privacy", privacy))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

