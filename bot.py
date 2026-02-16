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

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("treehouse_bot")

# ----------------------------
# Env
# ----------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# IMPORTANT for Render:
# - /var/data is NOT writable unless you attach a Disk and mount it there.
# - So default to /tmp which is writable.
DB_PATH = os.environ.get("DB_PATH", "/tmp/treehousecollective.sqlite").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing. Set it in Render -> Environment.")

# ----------------------------
# DB helpers
# ----------------------------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True) if os.path.dirname(DB_PATH) else None
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                last_name   TEXT,
                source      TEXT,
                created_at  TEXT,
                updated_at  TEXT
            )
            """
        )
        conn.commit()

def upsert_user(user, source: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    username = user.username or ""
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    source = source or ""

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (user.id, username, first_name, last_name, source, now, now),
        )
        conn.commit()

# ----------------------------
# Handlers
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    source = context.args[0] if context.args else ""

    try:
        upsert_user(user, source)
    except Exception:
        logger.exception("DB write failed")
        await update.message.reply_text(
            "⚠️ Sorry — I couldn’t save you right now. Try again in a minute."
        )
        return

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

async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Quick test endpoint via /health
    await update.message.reply_text("✅ Bot is running.")

# ----------------------------
# Main
# ----------------------------
def main() -> None:
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("privacy", privacy))
    application.add_handler(CommandHandler("health", health))

    # PTB v20+ uses this (NO Updater)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
