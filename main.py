# -*- coding: utf-8 -*-

import os
import logging
import sqlite3
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = 7067124128
DB_FILE = "bot.db"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing in environment variables")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= DB =================
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            balance INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()

# ================= HELPERS =================
def ensure_user(user):
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT * FROM users WHERE telegram_id=?", (user.id,))
    row = c.fetchone()

    if not row:
        c.execute(
            "INSERT INTO users (telegram_id, first_name, username) VALUES (?,?,?)",
            (user.id, user.first_name, user.username),
        )
        conn.commit()

    conn.close()

# ================= KEYBOARDS =================
MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Menu")],
        [KeyboardButton("Balance")],
    ],
    resize_keyboard=True
)

ADMIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Admin Panel")],
    ],
    resize_keyboard=True
)

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user)

    await update.message.reply_text(
        f"Hello {user.first_name} 👋",
        reply_markup=MAIN_MENU
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    row = conn.execute(
        "SELECT balance FROM users WHERE telegram_id=?",
        (update.effective_user.id,)
    ).fetchone()
    conn.close()

    bal = row["balance"] if row else 0

    await update.message.reply_text(f"Your balance: {bal}")

# ================= MAIN =================
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
