import os
import csv
import io
import sqlite3
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("treehouse_inbox_bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))
DB_PATH = os.environ.get("DB_PATH", "/tmp/treehouse.sqlite").strip()

ADMIN_USER_IDS_RAW = os.environ.get("ADMIN_USER_IDS", "").strip()
ADMIN_USER_IDS: set[int] = set()
if ADMIN_USER_IDS_RAW:
    for part in ADMIN_USER_IDS_RAW.split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_USER_IDS.add(int(part))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")
if not ADMIN_CHAT_ID:
    raise RuntimeError("ADMIN_CHAT_ID missing")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_connect() -> sqlite3.Connection:
    # Ensure dir exists (works if DB_PATH like /var/data/treehouse.sqlite)
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)

    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            first_seen_utc TEXT,
            last_seen_utc TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS admin_map (
            admin_msg_id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_utc TEXT NOT NULL
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen_utc)")
    return con


def upsert_user(u) -> None:
    con = db_connect()
    t = now_utc_iso()
    con.execute("""
        INSERT INTO users (user_id, username, first_name, last_name, first_seen_utc, last_seen_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            last_seen_utc=excluded.last_seen_utc
    """, (u.id, u.username or "", u.first_name or "", u.last_name or "", t, t))
    con.commit()
    con.close()


def map_admin_msg(admin_msg_id: int, user_id: int) -> None:
    con = db_connect()
    con.execute("""
        INSERT OR REPLACE INTO admin_map (admin_msg_id, user_id, created_utc)
        VALUES (?, ?, ?)
    """, (admin_msg_id, user_id, now_utc_iso()))
    con.commit()
    con.close()


def get_user_for_admin_reply(reply_to_admin_msg_id: int) -> int | None:
    con = db_connect()
    row = con.execute(
        "SELECT user_id FROM admin_map WHERE admin_msg_id=?",
        (reply_to_admin_msg_id,)
    ).fetchone()
    con.close()
    return int(row[0]) if row else None


def is_admin(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    # If ADMIN_USER_IDS not provided, allow anyone who can write in ADMIN_CHAT_ID
    if not ADMIN_USER_IDS:
        return update.effective_chat and update.effective_chat.id == ADMIN_CHAT_ID
    return u.id in ADMIN_USER_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if u:
        upsert_user(u)
    await update.message.reply_text(
        "🌲 Tree House Collective\n\n"
        "Message me here and admin will respond.\n"
        "Type /help for info."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send your message here and it will go to admin.\n\n"
        "Admin commands:\n"
        "/users\n"
        "/export_users\n"
        "/reply <user_id> <message>"
    )


# ---- USER -> ADMIN (inbox) ----
async def user_message_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    chat = update.effective_chat
    if not chat or chat.type != "private":
        return

    u = update.effective_user
    if not u:
        return

    upsert_user(u)

    # 1) Send a header to admin (clean + includes user id)
    handle = f"@{u.username}" if u.username else "(no username)"
    name = " ".join([p for p in [u.first_name, u.last_name] if p]) or "(no name)"
    header = (
        f"📩 New message\n"
        f"User: {name} | {handle}\n"
        f"User ID: {u.id}\n"
        f"Time: {now_utc_iso()}"
    )

    header_msg = await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=header)
    map_admin_msg(header_msg.message_id, u.id)

    # 2) Forward the actual message to admin and map its message_id too
    fwd = await context.bot.forward_message(
        chat_id=ADMIN_CHAT_ID,
        from_chat_id=chat.id,
        message_id=update.message.message_id
    )
    map_admin_msg(fwd.message_id, u.id)

    # Optional: confirm to user (keep it subtle)
    await update.message.reply_text("✅ Sent to admin. Reply will come here.")


# ---- ADMIN -> USER (reply relay) ----
async def admin_reply_relay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin replies to the forwarded/header message in ADMIN_CHAT_ID.
    Bot finds the user_id linked to that message_id and sends the admin’s reply to the user.
    """
    if not update.message or not update.message.reply_to_message:
        return

    if update.effective_chat is None or update.effective_chat.id != ADMIN_CHAT_ID:
        return

    if not is_admin(update):
        return

    replied_id = update.message.reply_to_message.message_id
    user_id = get_user_for_admin_reply(replied_id)
    if not user_id:
        await update.message.reply_text("⚠️ I couldn’t find the user for that reply. Reply to the header or forwarded message.")
        return

    # Copy admin message to the user (supports text/media)
    try:
        await context.bot.copy_message(
            chat_id=user_id,
            from_chat_id=ADMIN_CHAT_ID,
            message_id=update.message.message_id,
        )
    except Exception:
        logger.exception("Failed to send admin reply to user")
        await update.message.reply_text("❌ Failed sending to user (they may have blocked the bot).")


async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /reply <user_id> <message>
    """
    if update.effective_chat is None or update.effective_chat.id != ADMIN_CHAT_ID:
        return
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only.")
        return
    if not update.message:
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /reply <user_id> <message>")
        return

    user_id_str = context.args[0]
    if not user_id_str.isdigit():
        await update.message.reply_text("First argument must be numeric user_id.")
        return

    user_id = int(user_id_str)
    msg = " ".join(context.args[1:]).strip()

    try:
        await context.bot.send_message(chat_id=user_id, text=msg)
        await update.message.reply_text("✅ Sent.")
    except Exception:
        logger.exception("Failed /reply send")
        await update.message.reply_text("❌ Failed (user may have blocked bot).")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.id != ADMIN_CHAT_ID:
        return
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only.")
        return

    con = db_connect()
    total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    rows = con.execute("""
        SELECT user_id, username, first_name, last_name, last_seen_utc
        FROM users
        ORDER BY last_seen_utc DESC
        LIMIT 20
    """).fetchall()
    con.close()

    lines = [f"👥 Users total: {total}", "", "Last 20:"]
    for user_id, username, first_name, last_name, last_seen in rows:
        handle = f"@{username}" if username else "(no username)"
        name = " ".join([p for p in [first_name, last_name] if p]) or "(no name)"
        lines.append(f"- {user_id} | {handle} | {name} | {last_seen}")

    await update.message.reply_text("\n".join(lines))


async def export_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_chat.id != ADMIN_CHAT_ID:
        return
    if not is_admin(update):
        await update.message.reply_text("⛔ Admin only.")
        return

    con = db_connect()
    rows = con.execute("""
        SELECT user_id, username, first_name, last_name, first_seen_utc, last_seen_utc
        FROM users
        ORDER BY first_seen_utc ASC
    """).fetchall()
    con.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "username", "first_name", "last_name", "first_seen_utc", "last_seen_utc"])
    w.writerows(rows)

    data = buf.getvalue().encode("utf-8")
    buf.close()

    filename = f"treehouse_users_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    await update.message.reply_document(document=data, filename=filename, caption="✅ Users export")


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    # Admin tools
    app.add_handler(CommandHandler("reply", reply_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("export_users", export_users_cmd))

    # Admin reply relay (admin replies to forwarded/header message)
    app.add_handler(
        MessageHandler(
            filters.Chat(chat_id=ADMIN_CHAT_ID) & filters.REPLY,
            admin_reply_relay
        )
    )

    # User inbox -> admin (private chats only, non-commands)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            user_message_to_admin
        )
    )

    logger.info("Treehouse Inbox Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
