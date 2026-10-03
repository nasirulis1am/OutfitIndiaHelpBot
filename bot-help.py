"""
Outfit India Help Bot (@OutfitIndiaHelp_Bot)
Standalone customer-support bot. No product catalog, no Supabase, no main-bot dependency.

Environment variables:
  BOT_TOKEN   — Support bot token from BotFather
  ADMIN_ID    — Telegram user id of the support admin
  PORT        — Render-assigned port (default 10000)
  RENDER_EXTERNAL_URL — public base URL for webhook (set automatically on Render)
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Without a configured root logger, Python only shows WARNING+ so aiohttp's
# access log and PTB's INFO logs never reach Render's log stream.

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
# httpx logs full request URLs at INFO, and Telegram API URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("outfit_india_help_bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
ADMIN_ID = (os.environ.get("ADMIN_ID") or "").strip()
DB_PATH = Path(os.environ.get("SUPPORT_DB_PATH") or Path(__file__).resolve().parent / "support.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID environment variable is required")

REPLY_TEXT = 1  # ConversationHandler state for admin reply input

# ---------------------------------------------------------------------------
# Database (SQLite)
# ---------------------------------------------------------------------------


def _connect():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (
                telegram_id   INTEGER PRIMARY KEY,
                first_name    TEXT,
                last_name     TEXT,
                username      TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                unread_count  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS messages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   INTEGER NOT NULL,
                direction     TEXT NOT NULL CHECK(direction IN ('in', 'out')),
                body          TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                FOREIGN KEY (telegram_id) REFERENCES customers(telegram_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_customer
                ON messages(telegram_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_customers_unread
                ON customers(unread_count DESC, updated_at DESC);
            """
        )


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def upsert_customer(user) -> None:
    tid = int(user.id)
    first = (user.first_name or "").strip() or None
    last = (user.last_name or "").strip() or None
    username = (user.username or "").strip() or None
    now = _now_iso()
    with db() as conn:
        existing = conn.execute(
            "SELECT telegram_id FROM customers WHERE telegram_id = ?", (tid,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE customers
                   SET first_name = ?, last_name = ?, username = ?, updated_at = ?
                 WHERE telegram_id = ?
                """,
                (first, last, username, now, tid),
            )
        else:
            conn.execute(
                """
                INSERT INTO customers
                    (telegram_id, first_name, last_name, username, created_at, updated_at, unread_count)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (tid, first, last, username, now, now),
            )


def save_inbound_message(user, body: str) -> None:
    tid = int(user.id)
    upsert_customer(user)
    now = _now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO messages (telegram_id, direction, body, created_at)
            VALUES (?, 'in', ?, ?)
            """,
            (tid, body, now),
        )
        conn.execute(
            """
            UPDATE customers
               SET unread_count = unread_count + 1,
                   updated_at = ?
             WHERE telegram_id = ?
            """,
            (now, tid),
        )


def save_outbound_message(telegram_id: int, body: str) -> None:
    now = _now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO messages (telegram_id, direction, body, created_at)
            VALUES (?, 'out', ?, ?)
            """,
            (int(telegram_id), body, now),
        )
        conn.execute(
            """
            UPDATE customers SET updated_at = ? WHERE telegram_id = ?
            """,
            (now, int(telegram_id)),
        )


def mark_read(telegram_id: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE customers SET unread_count = 0 WHERE telegram_id = ?",
            (int(telegram_id),),
        )


def list_customers_with_unread(limit: int = 30):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM customers
             WHERE unread_count > 0
             ORDER BY updated_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()


def list_all_customers(limit: int = 40):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM customers
             ORDER BY updated_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()


def get_customer(telegram_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM customers WHERE telegram_id = ?",
            (int(telegram_id),),
        ).fetchone()


def get_messages(telegram_id: int, limit: int = 20):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM messages
             WHERE telegram_id = ?
             ORDER BY id DESC
             LIMIT ?
            """,
            (int(telegram_id), limit),
        ).fetchall()
    return list(reversed(rows))


def customer_label(row) -> str:
    name = (row["first_name"] or "Customer").strip()
    if row["last_name"]:
        name = f"{name} {row['last_name']}".strip()
    return name


def customer_username_line(row) -> str:
    if row["username"]:
        return f"@{row['username']}"
    return "No username"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def is_admin(user_id) -> bool:
    return str(user_id) == str(ADMIN_ID)


_STALE_QUERY_MARKERS = ("query is too old", "query id is invalid")


def _is_stale_query_error(err) -> bool:
    if not isinstance(err, BadRequest):
        return False
    msg = str(err).lower()
    return any(marker in msg for marker in _STALE_QUERY_MARKERS)


async def safe_answer(query, *args, **kwargs) -> bool:
    """query.answer() that tolerates expired/invalid callback query ids.

    Telegram only accepts answerCallbackQuery for a short time. Late delivery
    (e.g. Render waking from sleep) makes it raise BadRequest. The button's real
    work (edit_message_text etc.) does not need the query id, so we log and
    carry on. Any other error is re-raised untouched.
    """
    try:
        await query.answer(*args, **kwargs)
        return True
    except BadRequest as e:
        if _is_stale_query_error(e):
            logger.warning("Stale callback query ignored (data=%r): %s", getattr(query, "data", None), e)
            return False
        raise


def admin_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 New Messages", callback_data="adm_new")],
            [InlineKeyboardButton("👥 Customers", callback_data="adm_customers")],
        ]
    )


# ---------------------------------------------------------------------------
# Customer handlers
# ---------------------------------------------------------------------------

START_TEXT = (
    "✨ OUTFIT INDIA SUPPORT\n\n"
    "Need help with something?\n\n"
    "Send your question or problem here and our support team will assist you."
)

HELP_TEXT = (
    "💬 Send your question or problem here and our support team will respond.\n\n"
    "Commands:\n"
    "/start — open support\n"
    "/help — this message\n"
    "/cancel — cancel an admin action (admin only)"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info("cmd_start: START user_id=%s admin=%s", getattr(user, "id", None), bool(user and is_admin(user.id)))
    if user is None:
        logger.warning("cmd_start: no effective_user, nothing to do")
        return
    upsert_customer(user)

    if is_admin(user.id):
        await update.message.reply_text(
            "✨ OUTFIT INDIA SUPPORT — Admin\n\n"
            "Choose an option:",
            reply_markup=admin_menu_keyboard(),
        )
        logger.info("cmd_start: FINISHED (admin menu sent) user_id=%s", user.id)
        return

    await update.message.reply_text(START_TEXT)
    logger.info("cmd_start: FINISHED (customer greeting sent) user_id=%s", user.id)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None:
        return
    if is_admin(user.id):
        await update.message.reply_text(
            HELP_TEXT + "\n\nAdmin: use the menu buttons or /start.",
            reply_markup=admin_menu_keyboard(),
        )
        return
    await update.message.reply_text(HELP_TEXT)


async def customer_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any plain text from a non-admin customer is treated as a support message."""
    user = update.effective_user
    if user is None or update.message is None:
        return

    # Admin text is handled by ConversationHandler when in REPLY_TEXT state;
    # if admin sends free text outside reply mode, show the menu.
    if is_admin(user.id):
        # Ignore if conversation handler will process it
        if context.user_data.get("reply_to"):
            return
        await update.message.reply_text(
            "Admin menu:",
            reply_markup=admin_menu_keyboard(),
        )
        return

    body = (update.message.text or "").strip()
    if not body:
        await update.message.reply_text("Please send your message as text.")
        return

    save_inbound_message(user, body)

    # Notify admin (best-effort)
    name = (user.full_name or user.first_name or "Customer").strip()
    uname = f"@{user.username}" if user.username else "Not Available"
    try:
        await context.bot.send_message(
            chat_id=int(ADMIN_ID),
            text=(
                "💬 NEW SUPPORT MESSAGE\n\n"
                f"From: {name}\n"
                f"Username: {uname}\n"
                f"ID: {user.id}\n\n"
                f"{body}"
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "↩️ Open conversation",
                            callback_data=f"adm_open_{user.id}",
                        )
                    ]
                ]
            ),
        )
    except Exception as e:
        print(f"ADMIN NOTIFY ERROR: {e}")

    await update.message.reply_text(
        "✅ Message received\n\n"
        "Our support team will get back to you soon."
    )


# ---------------------------------------------------------------------------
# Admin handlers
# ---------------------------------------------------------------------------


async def admin_denied(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await safe_answer(query, "Access denied.", show_alert=True)


async def admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(query.from_user.id):
        await safe_answer(query, "Access denied.", show_alert=True)
        return
    await query.edit_message_text(
        "✨ OUTFIT INDIA SUPPORT — Admin\n\nChoose an option:",
        reply_markup=admin_menu_keyboard(),
    )


def _format_customer_button(row) -> str:
    label = customer_label(row)
    unread = int(row["unread_count"] or 0)
    badge = f" ({unread})" if unread else ""
    return f"👤 {label}{badge}"


async def admin_new_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(query.from_user.id):
        await safe_answer(query, "Access denied.", show_alert=True)
        return

    rows = list_customers_with_unread()
    if not rows:
        await query.edit_message_text(
            "💬 New Messages\n\nNo unread messages.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Menu", callback_data="adm_home")]]
            ),
        )
        return

    buttons = []
    for row in rows:
        buttons.append(
            [
                InlineKeyboardButton(
                    _format_customer_button(row),
                    callback_data=f"adm_open_{row['telegram_id']}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("🔙 Menu", callback_data="adm_home")])
    await query.edit_message_text(
        "💬 New Messages\n\nSelect a conversation:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def admin_customers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(query.from_user.id):
        await safe_answer(query, "Access denied.", show_alert=True)
        return

    rows = list_all_customers()
    if not rows:
        await query.edit_message_text(
            "👥 Customers\n\nNo customers yet.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Menu", callback_data="adm_home")]]
            ),
        )
        return

    buttons = []
    for row in rows:
        buttons.append(
            [
                InlineKeyboardButton(
                    _format_customer_button(row),
                    callback_data=f"adm_open_{row['telegram_id']}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("🔙 Menu", callback_data="adm_home")])
    await query.edit_message_text(
        "👥 Customers\n\nSelect a conversation:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def _format_thread(row, messages) -> str:
    lines = [
        f"👤 {customer_label(row)}",
        customer_username_line(row),
        f"ID: {row['telegram_id']}",
        "",
    ]
    if not messages:
        lines.append("No messages yet.")
    else:
        for m in messages:
            prefix = "Customer" if m["direction"] == "in" else "Support"
            body = (m["body"] or "").strip()
            if len(body) > 400:
                body = body[:397] + "…"
            lines.append(f"[{m['created_at']}] {prefix}:\n{body}\n")
    text = "\n".join(lines)
    # Telegram message limit safety
    if len(text) > 3900:
        text = text[-3900:]
        text = "…\n" + text
    return text


async def admin_open_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(query.from_user.id):
        await safe_answer(query, "Access denied.", show_alert=True)
        return

    data = query.data or ""
    try:
        tid = int(data.split("_")[-1])
    except (ValueError, IndexError):
        await query.edit_message_text("Invalid conversation.")
        return

    row = get_customer(tid)
    if row is None:
        await query.edit_message_text(
            "Customer not found.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Menu", callback_data="adm_home")]]
            ),
        )
        return

    mark_read(tid)
    messages = get_messages(tid, limit=15)
    text = _format_thread(row, messages)
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("↩️ Reply", callback_data=f"adm_reply_{tid}")],
            [
                InlineKeyboardButton("💬 New Messages", callback_data="adm_new"),
                InlineKeyboardButton("👥 Customers", callback_data="adm_customers"),
            ],
            [InlineKeyboardButton("🔙 Menu", callback_data="adm_home")],
        ]
    )
    await query.edit_message_text(text, reply_markup=keyboard)


# ----- Admin reply conversation -----


async def admin_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(query.from_user.id):
        await safe_answer(query, "Access denied.", show_alert=True)
        return ConversationHandler.END

    data = query.data or ""
    try:
        tid = int(data.split("_")[-1])
    except (ValueError, IndexError):
        await query.edit_message_text("Invalid conversation.")
        return ConversationHandler.END

    context.user_data["reply_to"] = tid
    await query.edit_message_text(
        "✍️ Type your reply:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Cancel", callback_data="adm_reply_cancel")]]
        ),
    )
    return REPLY_TEXT


async def admin_reply_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or not is_admin(user.id):
        return ConversationHandler.END

    tid = context.user_data.get("reply_to")
    if not tid:
        await update.message.reply_text(
            "No customer selected.",
            reply_markup=admin_menu_keyboard(),
        )
        return ConversationHandler.END

    body = (update.message.text or "").strip()
    if not body:
        await update.message.reply_text("Please type a text reply.")
        return REPLY_TEXT

    customer_text_out = f"💬 OUTFIT INDIA SUPPORT\n\n{body}"
    try:
        await context.bot.send_message(chat_id=int(tid), text=customer_text_out)
    except Exception as e:
        print(f"REPLY DELIVER ERROR: {e}")
        await update.message.reply_text(
            "❌ Could not deliver the reply. The customer may have blocked the bot.",
            reply_markup=admin_menu_keyboard(),
        )
        context.user_data.pop("reply_to", None)
        return ConversationHandler.END

    save_outbound_message(int(tid), body)
    context.user_data.pop("reply_to", None)

    row = get_customer(int(tid))
    name = customer_label(row) if row else str(tid)
    await update.message.reply_text(
        f"✅ Reply sent to {name}.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("↩️ Reply again", callback_data=f"adm_reply_{tid}")],
                [InlineKeyboardButton("Open conversation", callback_data=f"adm_open_{tid}")],
                [InlineKeyboardButton("🔙 Menu", callback_data="adm_home")],
            ]
        ),
    )
    return ConversationHandler.END


async def admin_reply_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await safe_answer(query)
        if not is_admin(query.from_user.id):
            return ConversationHandler.END
        context.user_data.pop("reply_to", None)
        tid_data = (query.data or "")
        # Return to menu
        await query.edit_message_text(
            "Reply cancelled.",
            reply_markup=admin_menu_keyboard(),
        )
    context.user_data.pop("reply_to", None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("reply_to", None)
    if update.effective_user and is_admin(update.effective_user.id):
        await update.message.reply_text(
            "Cancelled.",
            reply_markup=admin_menu_keyboard(),
        )
    else:
        await update.message.reply_text("Nothing to cancel. Just send your message when ready.")
    return ConversationHandler.END


async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route non-conversation admin callbacks with auth check."""
    query = update.callback_query
    data = query.data or ""
    if not is_admin(query.from_user.id):
        await safe_answer(query, "Access denied.", show_alert=True)
        return

    if data == "adm_home":
        await admin_home(update, context)
    elif data == "adm_new":
        await admin_new_messages(update, context)
    elif data == "adm_customers":
        await admin_customers(update, context)
    elif data.startswith("adm_open_"):
        await admin_open_conversation(update, context)
    else:
        await safe_answer(query)


# ---------------------------------------------------------------------------
# Diagnostics + global error handler
# ---------------------------------------------------------------------------


def _update_kind(update: Update) -> str:
    try:
        return ",".join(k for k in update.to_dict() if k != "update_id") or "unknown"
    except Exception:
        return "unknown"


async def log_incoming_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs in group -1 for every update; only logs, never consumes the update."""
    msg = update.effective_message
    text = (msg.text or "")[:80] if msg is not None and msg.text else None
    cq = update.callback_query
    user = update.effective_user
    logger.info(
        "PTB received update_id=%s kind=%s user_id=%s text=%r callback_data=%r",
        update.update_id,
        _update_kind(update),
        getattr(user, "id", None),
        text,
        getattr(cq, "data", None),
    )


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Global PTB error handler. Logs every exception with its full traceback."""
    err = context.error
    update_id = getattr(update, "update_id", None)
    if _is_stale_query_error(err):
        # Expected when Telegram delivers an old button press; not a bug.
        logger.warning("Stale callback query (update_id=%s): %s", update_id, err)
        return
    logger.error(
        "Unhandled exception while processing update_id=%s",
        update_id,
        exc_info=err,
    )


# ---------------------------------------------------------------------------
# Application + webhook (Render)
# ---------------------------------------------------------------------------

init_db()

application = Application.builder().token(BOT_TOKEN).build()

reply_conversation = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(admin_reply_start, pattern=r"^adm_reply_\d+$"),
    ],
    states={
        REPLY_TEXT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reply_receive),
        ],
    },
    fallbacks=[
        CallbackQueryHandler(admin_reply_cancel, pattern=r"^adm_reply_cancel$"),
        CommandHandler("cancel", cmd_cancel),
        CallbackQueryHandler(admin_reply_cancel, pattern=r"^adm_home$"),
    ],
    allow_reentry=True,
)

application.add_error_handler(error_handler)
# Diagnostic only (group -1 runs before the real handlers and never consumes updates)
application.add_handler(TypeHandler(Update, log_incoming_update), group=-1)

application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("help", cmd_help))
application.add_handler(CommandHandler("cancel", cmd_cancel))
application.add_handler(reply_conversation)
application.add_handler(
    CallbackQueryHandler(
        admin_callbacks,
        pattern=r"^adm_(home|new|customers|open_\d+)$",
    )
)
# Block non-admin from any other adm_ callback
application.add_handler(CallbackQueryHandler(admin_denied, pattern=r"^adm_"))
# Customer (and admin free-text outside reply mode) messages
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, customer_text))


async def health(request):
    return web.Response(text="Outfit India Help Bot is running.")


async def telegram_webhook(request):
    try:
        data = await request.json()
    except Exception:
        logger.exception("Webhook POST /telegram had an unreadable body")
        return web.Response(status=400, text="Bad Request")

    update = Update.de_json(data, application.bot)
    if update is None:
        logger.warning("Webhook POST /telegram: could not build an Update from body")
        return web.Response(text="OK")

    logger.info("Webhook POST /telegram: update_id=%s kind=%s", update.update_id, _update_kind(update))
    try:
        await application.process_update(update)
    except Exception:
        # process_update normally routes handler errors to error_handler itself;
        # this is a safety net. Log fully, and still answer 200 so Telegram does
        # not keep re-sending the same failing update.
        logger.exception("process_update raised for update_id=%s", update.update_id)
    return web.Response(text="OK")


async def startup(app):
    await application.initialize()
    await application.start()
    logger.info("Bot identity: @%s (id=%s)", application.bot.username, application.bot.id)
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if external_url:
        webhook_url = f"{external_url.rstrip('/')}/telegram"
        # allowed_updates is explicit because Telegram keeps the PREVIOUS value when it
        # is omitted; a stale value could silently exclude "message" updates.
        await application.bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "callback_query"],
        )
        print(f"Webhook set: {webhook_url}")
        try:
            info = await application.bot.get_webhook_info()
            logger.info(
                "Webhook info: url=%s pending=%s allowed_updates=%s last_error_date=%s last_error_message=%s",
                info.url,
                info.pending_update_count,
                info.allowed_updates,
                info.last_error_date,
                info.last_error_message,
            )
        except Exception:
            logger.exception("get_webhook_info failed (diagnostic only)")
    else:
        logger.warning("RENDER_EXTERNAL_URL not set — webhook not configured")


async def shutdown(app):
    await application.stop()
    await application.shutdown()


web_app = web.Application()
web_app.router.add_get("/", health)
web_app.router.add_post("/telegram", telegram_webhook)
web_app.on_startup.append(startup)
web_app.on_cleanup.append(shutdown)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    web.run_app(web_app, host="0.0.0.0", port=port)
