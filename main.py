import os
import json
import sqlite3
import random
import logging
from pathlib import Path
from urllib.parse import quote

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_ID     = 7067124128
PROMO_IMAGE  = "promo.jpg"

EARN_PER_NUMBER  = 270    # paise → ₹2.70 per approved number
NUMBERS_PER_TASK = 5
MIN_WITHDRAW_INR    = 100   # paise → ₹1   (UPI / QR withdrawals)
MIN_WITHDRAW_CRYPTO = 9643  # paise → ~$1 USDT  (BEP20 / Binance withdrawals)
REFER_BONUS_L1   = 500    # ₹5 — fixed join bonus, direct referral
REFER_BONUS_L2   = 200    # ₹2 — fixed join bonus, level 2
REFER_BONUS_L3   = 100    # ₹1 — fixed join bonus, level 3

REFER_PCT_L1     = 10     # 10% of earner's earning → level 1 referrer
REFER_PCT_L2     = 7      #  7% of earner's earning → level 2 referrer
REFER_PCT_L3     = 4      #  4% of earner's earning → level 3 referrer

SUBSCRIBE_EARN    = 30    # paise → ₹0.30 per approved subscription
SUBSCRIBE_CHANNEL = "https://youtube.com/@theevolveeducation-m4y?si=QmPx-HjrLgqElPee"

USDT_PER_NUMBER = 0.028   # USDT earned per approved number (5 msgs = $0.14)

DB_FILE = "bot.db"

STATE_WAITING_SCREENSHOT          = "waiting_screenshot"
STATE_WAITING_UPI                 = "waiting_upi"
STATE_WAITING_UPI_SETUP           = "waiting_upi_setup"
STATE_WAITING_UPI_QR              = "waiting_upi_qr"
STATE_WAITING_CRYPTO              = "waiting_crypto"
STATE_WAITING_REVERIFY_NUMBER     = "waiting_reverify_number"
STATE_WAITING_REVERIFY_SCREENSHOT = "waiting_reverify_screenshot"
STATE_WAITING_SUBSCRIBE_SCREENSHOT = "waiting_subscribe_screenshot"
STATE_ADMIN_WAITING_ADD_NUMBER    = "admin_waiting_add_number"
STATE_ADMIN_WAITING_DEL_NUMBER    = "admin_waiting_del_number"
STATE_ADMIN_WAITING_UPLOAD        = "admin_waiting_upload"

PROMO_TEXT = (
    "🎉 Parabéns! Você recebeu um bônus aleatório exclusivo! 🎉\n\n"
    "💰 Deposite agora e tenha a chance de multiplicar seu saldo em até 500x!\n"
    "Quanto maior a sorte, maior o ganho!\n\n"
    "💸Não perca essa oportunidade — faça seu depósito e descubra sua recompensa agora!\n\n"
    "Link com problema? Mande \"Hi\"\n"
    "Participe agora 👉 https://36tt6.com?ch=74480"
)

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CURRENCY HELPERS
# ─────────────────────────────────────────────
def to_usdt(paise: int) -> float:
    """Convert paise to USDT using USDT_PER_NUMBER / EARN_PER_NUMBER ratio."""
    return paise * USDT_PER_NUMBER / EARN_PER_NUMBER


def fmt(paise: int) -> str:
    """Format paise as INR + USDT."""
    return f"₹{paise/100:.2f} (${to_usdt(paise):.3f} USDT)"


def fmt_usdt_only(paise: int) -> str:
    return f"${to_usdt(paise):.3f} USDT"


# ─────────────────────────────────────────────
# NUMBER POOL  (loaded from 700.txt at startup)
# ─────────────────────────────────────────────
NUMBER_POOL: list[str] = []


def load_numbers() -> list[str]:
    path = Path("700.txt")
    if not path.exists():
        logger.warning("700.txt not found")
        return []
    with open(path) as f:
        return [ln.strip() for ln in f if ln.strip()]


def save_numbers(numbers: list[str]) -> None:
    with open("700.txt", "w") as f:
        f.write("\n".join(numbers) + ("\n" if numbers else ""))


def reload_pool() -> None:
    global NUMBER_POOL
    NUMBER_POOL = load_numbers()


# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id      INTEGER PRIMARY KEY,
            first_name       TEXT    NOT NULL,
            username         TEXT,
            upi_id           TEXT,
            balance          INTEGER NOT NULL DEFAULT 0,
            total_earned     INTEGER NOT NULL DEFAULT 0,
            total_withdrawn  INTEGER NOT NULL DEFAULT 0,
            referral_earnings INTEGER NOT NULL DEFAULT 0,
            referred_by      INTEGER,
            state            TEXT,
            pending_upi      TEXT,
            current_task_id  INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL,
            task_type        TEXT    NOT NULL DEFAULT 'send_earn',
            numbers          TEXT    NOT NULL,
            selected         TEXT    NOT NULL DEFAULT '[]',
            screenshot_file_id TEXT,
            status           TEXT    NOT NULL DEFAULT 'waiting_selection',
            earn_amount      INTEGER NOT NULL DEFAULT 0,
            created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
            reviewed_at      DATETIME
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER NOT NULL,
            level       INTEGER NOT NULL,
            bonus       INTEGER NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            amount          INTEGER NOT NULL,
            upi             TEXT,
            payment_method  TEXT    NOT NULL DEFAULT 'upi',
            crypto_address  TEXT,
            status          TEXT    NOT NULL DEFAULT 'pending',
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS reverifications (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL,
            number           TEXT    NOT NULL,
            screenshot_file_id TEXT,
            status           TEXT    NOT NULL DEFAULT 'pending',
            earn_amount      INTEGER NOT NULL DEFAULT 0,
            created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
            reviewed_at      DATETIME
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id            INTEGER NOT NULL UNIQUE,
            screenshot_file_id TEXT,
            status             TEXT    NOT NULL DEFAULT 'pending',
            earn_amount        INTEGER NOT NULL DEFAULT 0,
            created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
            reviewed_at        DATETIME
        )
    """)

    # Migrations for pre-existing DBs
    existing_cols = {row[1] for row in c.execute("PRAGMA table_info(users)")}
    for col, defn in [
        ("upi_id",           "TEXT"),
        ("upi_qr_file_id",   "TEXT"),
        ("total_withdrawn",  "INTEGER NOT NULL DEFAULT 0"),
        ("referral_earnings","INTEGER NOT NULL DEFAULT 0"),
        ("current_task_id",  "INTEGER"),
        ("reverify_number",  "TEXT"),
        ("crypto_address",   "TEXT"),
        ("pending_crypto",   "TEXT"),
    ]:
        if col not in existing_cols:
            c.execute(f"ALTER TABLE users ADD COLUMN {col} {defn}")

    wd_cols = {row[1] for row in c.execute("PRAGMA table_info(withdrawals)")}
    for col, defn in [
        ("payment_method", "TEXT NOT NULL DEFAULT 'upi'"),
        ("crypto_address", "TEXT"),
    ]:
        if col not in wd_cols:
            c.execute(f"ALTER TABLE withdrawals ADD COLUMN {col} {defn}")

    conn.commit()
    conn.close()


# ────── user helpers ──────

def get_user(telegram_id) -> dict | None:
    conn = get_db()
    # Support lookup by numeric ID or by @username / username
    identifier = str(telegram_id).strip().lstrip("@")
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (int(identifier),)
        ).fetchone()
    except ValueError:
        row = None
    if not row:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (identifier,)
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def ensure_user(update: Update) -> dict:
    u = update.effective_user
    user = get_user(u.id)
    if not user:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, first_name, username) VALUES (?,?,?)",
            (u.id, u.first_name, u.username),
        )
        conn.commit()
        conn.close()
        user = get_user(u.id)
    return user


def set_state(telegram_id, state: str | None, upi: str | None = None, crypto: str | None = None) -> None:
    conn = get_db()
    if upi is not None and crypto is not None:
        conn.execute(
            "UPDATE users SET state=?, pending_upi=?, pending_crypto=? WHERE telegram_id=?",
            (state, upi, crypto, int(telegram_id)),
        )
    elif upi is not None:
        conn.execute(
            "UPDATE users SET state=?, pending_upi=? WHERE telegram_id=?",
            (state, upi, int(telegram_id)),
        )
    elif crypto is not None:
        conn.execute(
            "UPDATE users SET state=?, pending_crypto=? WHERE telegram_id=?",
            (state, crypto, int(telegram_id)),
        )
    else:
        conn.execute("UPDATE users SET state=? WHERE telegram_id=?", (state, int(telegram_id)))
    conn.commit()
    conn.close()


def set_current_task(telegram_id, task_id: int | None) -> None:
    conn = get_db()
    conn.execute("UPDATE users SET current_task_id=? WHERE telegram_id=?", (task_id, int(telegram_id)))
    conn.commit()
    conn.close()


def add_balance(telegram_id, paise: int) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE users SET balance=balance+?, total_earned=total_earned+? WHERE telegram_id=?",
        (paise, paise, int(telegram_id)),
    )
    conn.commit()
    conn.close()


def deduct_balance_db(telegram_id, paise: int) -> None:
    conn = get_db()
    conn.execute(
        "UPDATE users SET balance=balance-?, total_withdrawn=total_withdrawn+? WHERE telegram_id=?",
        (paise, paise, int(telegram_id)),
    )
    conn.commit()
    conn.close()


def set_balance(telegram_id, paise: int) -> None:
    conn = get_db()
    conn.execute("UPDATE users SET balance=? WHERE telegram_id=?", (paise, int(telegram_id)))
    conn.commit()
    conn.close()


# ────── task helpers ──────

def get_task(task_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_used_numbers(user_id: int) -> set[str]:
    conn = get_db()
    rows = conn.execute("SELECT numbers FROM tasks WHERE user_id=?", (int(user_id),)).fetchall()
    conn.close()
    used: set[str] = set()
    for row in rows:
        used.update(json.loads(row["numbers"]))
    return used


def pick_numbers(user_id: int) -> list[str] | None:
    used = get_used_numbers(user_id)
    available = [n for n in NUMBER_POOL if n not in used]
    if len(available) < NUMBERS_PER_TASK:
        # Recycle: allow numbers from rejected tasks
        conn = get_db()
        rejected = conn.execute(
            "SELECT numbers FROM tasks WHERE user_id=? AND status='rejected'", (int(user_id),)
        ).fetchall()
        conn.close()
        recycled: set[str] = set()
        for row in rejected:
            recycled.update(json.loads(row["numbers"]))
        available = [n for n in NUMBER_POOL if n not in (used - recycled)]
    if len(available) < NUMBERS_PER_TASK:
        return None
    return random.sample(available, NUMBERS_PER_TASK)


def create_task(user_id: int, numbers: list[str], task_type: str = "send_earn") -> int:
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO tasks (user_id, task_type, numbers, selected, status) VALUES (?,?,?,?,?)",
        (int(user_id), task_type, json.dumps(numbers), "[]", "waiting_selection"),
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return task_id


def update_task_selected(task_id: int, selected: list[int]) -> None:
    conn = get_db()
    conn.execute("UPDATE tasks SET selected=? WHERE id=?", (json.dumps(selected), task_id))
    conn.commit()
    conn.close()


def get_task_stats(user_id: int) -> dict:
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT
            COUNT(CASE WHEN status IN ('pending_review','approved','rejected') THEN 1 END) AS total_submitted,
            COUNT(CASE WHEN status='approved'        THEN 1 END) AS approved,
            COUNT(CASE WHEN status='pending_review'  THEN 1 END) AS pending,
            COUNT(CASE WHEN status='rejected'        THEN 1 END) AS rejected
        FROM tasks WHERE user_id=?
    """, (int(user_id),))
    stats = dict(c.fetchone())
    # total numbers sent = sum of selected counts for approved tasks
    c.execute("SELECT selected FROM tasks WHERE user_id=? AND status='approved'", (int(user_id),))
    nums_sent = sum(len(json.loads(r["selected"])) for r in c.fetchall())
    stats["numbers_sent"] = nums_sent
    conn.close()
    return stats


def get_referral_stats(user_id: int) -> dict:
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT level, COUNT(*) as cnt FROM referrals WHERE referrer_id=? GROUP BY level", (int(user_id),))
    levels = {row["level"]: row["cnt"] for row in c.fetchall()}
    c.execute("SELECT COALESCE(SUM(bonus),0) as total FROM referrals WHERE referrer_id=?", (int(user_id),))
    earned = c.fetchone()["total"]
    conn.close()
    return {
        "total": sum(levels.values()),
        "l1": levels.get(1, 0),
        "l2": levels.get(2, 0),
        "l3": levels.get(3, 0),
        "earned": earned,
    }


# ────── referral helpers ──────

def _pay_referral(referrer_id: int, referred_id: int, level: int, bonus: int, bot_send) -> None:
    add_balance(referrer_id, bonus)
    conn = get_db()
    conn.execute(
        "UPDATE users SET referral_earnings=referral_earnings+? WHERE telegram_id=?",
        (bonus, int(referrer_id)),
    )
    conn.execute(
        "INSERT INTO referrals (referrer_id, referred_id, level, bonus) VALUES (?,?,?,?)",
        (int(referrer_id), int(referred_id), level, bonus),
    )
    conn.commit()
    conn.close()


async def handle_referral_chain(new_user_id: int, referrer_id: int, new_name: str, bot) -> None:
    chain = [(referrer_id, 1, REFER_BONUS_L1)]
    ref = get_user(referrer_id)
    if ref and ref.get("referred_by"):
        chain.append((ref["referred_by"], 2, REFER_BONUS_L2))
        ref2 = get_user(ref["referred_by"])
        if ref2 and ref2.get("referred_by"):
            chain.append((ref2["referred_by"], 3, REFER_BONUS_L3))

    for rid, level, bonus in chain:
        r = get_user(rid)
        if not r:
            continue
        _pay_referral(rid, new_user_id, level, bonus, None)
        label = {1: "direct referral", 2: "Level 2 referral", 3: "Level 3 referral"}[level]
        try:
            await bot.send_message(
                rid,
                f"🎉 *{new_name}* joined as your {label}!\n"
                f"You earned *₹{bonus/100:.0f}* join bonus! 💰",
                parse_mode="Markdown",
            )
        except Exception:
            pass


async def pay_earn_commissions(earner_id: int, earn_amount: int, earner_name: str, bot) -> None:
    """Pay L1/L2/L3 referrers a % of earn_amount whenever the earner gets credited."""
    earner = get_user(earner_id)
    if not earner or not earner.get("referred_by"):
        return

    pcts   = {1: REFER_PCT_L1, 2: REFER_PCT_L2, 3: REFER_PCT_L3}
    labels = {1: "Level 1 (direct)", 2: "Level 2", 3: "Level 3"}

    chain = []
    l1 = get_user(earner["referred_by"])
    if l1:
        chain.append((l1, 1))
        if l1.get("referred_by"):
            l2 = get_user(l1["referred_by"])
            if l2:
                chain.append((l2, 2))
                if l2.get("referred_by"):
                    l3 = get_user(l2["referred_by"])
                    if l3:
                        chain.append((l3, 3))

    for ref_user, level in chain:
        pct        = pcts[level]
        commission = int(earn_amount * pct / 100)
        if commission <= 0:
            continue
        add_balance(ref_user["telegram_id"], commission)
        conn = get_db()
        conn.execute(
            "UPDATE users SET referral_earnings=referral_earnings+? WHERE telegram_id=?",
            (commission, ref_user["telegram_id"]),
        )
        conn.execute(
            "INSERT INTO referrals (referrer_id, referred_id, level, bonus) VALUES (?,?,?,?)",
            (ref_user["telegram_id"], earner_id, level, commission),
        )
        conn.commit()
        conn.close()
        try:
            await bot.send_message(
                ref_user["telegram_id"],
                f"💸 *Referral Commission — {labels[level]}*\n\n"
                f"👤 *{earner_name}* (your referral) just earned {fmt(earn_amount)}\n"
                f"🎯 Your {pct}% commission: *{fmt(commission)}*\n"
                f"💳 Added to your balance!",
                parse_mode="Markdown",
            )
        except Exception:
            pass


# ─────────────────────────────────────────────
# KEYBOARDS
# ─────────────────────────────────────────────
MAIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📤 Send Text & Earn"), KeyboardButton("📲 Click & Send")],
        [KeyboardButton("👥 Refer & Earn"),     KeyboardButton("💰 Withdraw")],
        [KeyboardButton("👤 Account"),          KeyboardButton("🔄 Re-verify")],
        [KeyboardButton("📺 Subscribe & Earn")],
    ],
    resize_keyboard=True,
)

ADMIN_MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📤 Send Text & Earn"), KeyboardButton("📲 Click & Send")],
        [KeyboardButton("👥 Refer & Earn"),     KeyboardButton("💰 Withdraw")],
        [KeyboardButton("👤 Account"),          KeyboardButton("🔄 Re-verify")],
        [KeyboardButton("📺 Subscribe & Earn")],
        [KeyboardButton("➕ Add Number"),        KeyboardButton("🗑️ Delete Number")],
        [KeyboardButton("📎 Upload Numbers"),   KeyboardButton("🔢 Pool Status")],
    ],
    resize_keyboard=True,
)

CANCEL_KB = ReplyKeyboardMarkup([[KeyboardButton("❌ Cancel")]], resize_keyboard=True)


def get_menu(user_id: int) -> ReplyKeyboardMarkup:
    return ADMIN_MENU if user_id == ADMIN_ID else MAIN_MENU


def build_task_keyboard(task_id: int, selected: list[int]) -> InlineKeyboardMarkup:
    action_row = [
        InlineKeyboardButton("✅ Done",     callback_data=f"tdone_{task_id}"),
        InlineKeyboardButton("❌ Not Done", callback_data=f"tcancel_{task_id}"),
    ]
    return InlineKeyboardMarkup([action_row])


def build_admin_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"tapprove_{task_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"treject_{task_id}"),
    ]])


# ─────────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u    = update.effective_user
    user = get_user(u.id)
    referred_by = None

    if ctx.args and not user:
        try:
            referred_by = int(ctx.args[0])
            if referred_by == u.id:
                referred_by = None
        except ValueError:
            pass

    if not user:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, first_name, username, referred_by) VALUES (?,?,?,?)",
            (u.id, u.first_name, u.username, referred_by),
        )
        conn.commit()
        conn.close()
        if referred_by:
            await handle_referral_chain(u.id, referred_by, u.first_name, ctx.bot)

    await update.message.reply_text(
        f"👋 Welcome, *{u.first_name}*!\n\n"
        f"📤 *Send Text & Earn* — send promo to 5 numbers\n"
        f"    → ₹2.70 each ($0.028 USDT) | Total: ₹13.50 ($0.14 USDT)\n"
        f"📲 *Click & Send* — tap to open WhatsApp & send\n"
        f"    → ₹2.70 each ($0.028 USDT) | Total: ₹13.50 ($0.14 USDT)\n"
        f"👥 *Refer & Earn* — 10% of every earning your referral makes\n"
        f"💰 *Withdraw* — UPI min ₹{MIN_WITHDRAW_INR/100:.0f} | Crypto min $1 USDT\n"
        f"👤 *Account* — stats & balance",
        parse_mode="Markdown",
        reply_markup=get_menu(u.id),
    )


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🏓 Pong!")


# ─────────────────────────────────────────────
# NUMBER POOL MANAGEMENT COMMANDS
# ─────────────────────────────────────────────

async def cmd_addnumber(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /addnumber <num1> [num2 ...] — add numbers to the pool."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/addnumber <number> [number2 ...]`\n"
            "Example: `/addnumber 9876543210 9123456789`",
            parse_mode="Markdown",
        )
        return

    existing = set(NUMBER_POOL)
    added, dupes = [], []
    for num in ctx.args:
        n = num.strip()
        if not n:
            continue
        if n in existing:
            dupes.append(n)
        else:
            added.append(n)
            existing.add(n)

    if added:
        new_pool = list(existing)
        save_numbers(new_pool)
        reload_pool()

    parts = []
    if added:
        parts.append(f"✅ Added {len(added)} number(s):\n" + "\n".join(f"  `{n}`" for n in added))
    if dupes:
        parts.append(f"⚠️ Already in pool ({len(dupes)}):\n" + "\n".join(f"  `{n}`" for n in dupes))
    parts.append(f"\n📊 Pool total: *{len(NUMBER_POOL)}* numbers")
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


async def cmd_delnumber(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /delnumber <num1> [num2 ...] — remove numbers from the pool."""
    if update.effective_user.id != ADMIN_ID:
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/delnumber <number> [number2 ...]`\n"
            "Example: `/delnumber 9876543210`",
            parse_mode="Markdown",
        )
        return

    pool_set = set(NUMBER_POOL)
    removed, not_found = [], []
    for num in ctx.args:
        n = num.strip()
        if n in pool_set:
            pool_set.discard(n)
            removed.append(n)
        else:
            not_found.append(n)

    if removed:
        save_numbers(list(pool_set))
        reload_pool()

    parts = []
    if removed:
        parts.append(f"🗑️ Removed {len(removed)} number(s):\n" + "\n".join(f"  `{n}`" for n in removed))
    if not_found:
        parts.append(f"❌ Not found ({len(not_found)}):\n" + "\n".join(f"  `{n}`" for n in not_found))
    parts.append(f"\n📊 Pool total: *{len(NUMBER_POOL)}* numbers")
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


async def cmd_uploadnumbers(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: send a .txt file with one number per line to bulk-add to the pool."""
    if update.effective_user.id != ADMIN_ID:
        return

    doc = update.message.document
    if not doc:
        await update.message.reply_text(
            "📎 Send a `.txt` file with one phone number per line "
            "using the `/uploadnumbers` command (attach the file and type the command in the caption).\n\n"
            "Or just *reply to this message* with the file attached.",
            parse_mode="Markdown",
        )
        return

    if doc.mime_type not in ("text/plain", "application/octet-stream") and not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Please send a plain `.txt` file.")
        return

    file = await ctx.bot.get_file(doc.file_id)
    raw = await file.download_as_bytearray()
    lines = raw.decode("utf-8", errors="ignore").splitlines()
    new_nums = [ln.strip() for ln in lines if ln.strip()]

    if not new_nums:
        await update.message.reply_text("❌ The file is empty or has no valid numbers.")
        return

    existing = set(NUMBER_POOL)
    added = [n for n in new_nums if n not in existing]
    dupes = len(new_nums) - len(added)

    existing.update(added)
    save_numbers(list(existing))
    reload_pool()

    await update.message.reply_text(
        f"✅ Bulk upload complete!\n\n"
        f"➕ Added: *{len(added)}* numbers\n"
        f"⚠️ Skipped (duplicates): *{dupes}*\n"
        f"📊 Pool total: *{len(NUMBER_POOL)}* numbers",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────
# ADMIN MENU BUTTON HANDLERS
# ─────────────────────────────────────────────

async def handle_admin_add_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    set_state(ADMIN_ID, STATE_ADMIN_WAITING_ADD_NUMBER)
    await update.message.reply_text(
        f"➕ *Add Numbers*\n\n"
        f"Send the number(s) you want to add, one per line or space-separated.\n\n"
        f"📊 Current pool: *{len(NUMBER_POOL)}* numbers",
        parse_mode="Markdown",
        reply_markup=CANCEL_KB,
    )


async def handle_admin_del_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    set_state(ADMIN_ID, STATE_ADMIN_WAITING_DEL_NUMBER)
    await update.message.reply_text(
        f"🗑️ *Delete Numbers*\n\n"
        f"Send the number(s) you want to remove, one per line or space-separated.\n\n"
        f"📊 Current pool: *{len(NUMBER_POOL)}* numbers",
        parse_mode="Markdown",
        reply_markup=CANCEL_KB,
    )


async def handle_admin_upload_numbers(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    set_state(ADMIN_ID, STATE_ADMIN_WAITING_UPLOAD)
    await update.message.reply_text(
        f"📎 *Bulk Upload Numbers*\n\n"
        f"Send a `.txt` file with one phone number per line.\n"
        f"Duplicates are skipped automatically.\n\n"
        f"📊 Current pool: *{len(NUMBER_POOL)}* numbers",
        parse_mode="Markdown",
        reply_markup=CANCEL_KB,
    )


async def handle_admin_pool_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    conn = get_db()
    used = conn.execute(
        "SELECT COUNT(DISTINCT json_each.value) FROM tasks, json_each(tasks.numbers)"
    ).fetchone()[0]
    conn.close()
    available = len(NUMBER_POOL) - used
    await update.message.reply_text(
        f"🔢 *Number Pool Status*\n\n"
        f"📋 Total in pool: *{len(NUMBER_POOL)}*\n"
        f"✅ Used in tasks: *{used}*\n"
        f"🟢 Available: *{available}*\n\n"
        f"Use ➕ *Add Number* to add more, or 📎 *Upload Numbers* for bulk import.",
        parse_mode="Markdown",
        reply_markup=ADMIN_MENU,
    )


NOT_FOUND_MSG = (
    "❌ User `{q}` not found.\n\n"
    "You can look up by:\n"
    "• Telegram ID: `/addbal 123456789 5`\n"
    "• Username: `/addbal @john 5`\n\n"
    "⚠️ Users must have sent /start to the bot at least once to be registered."
)


async def cmd_addbal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(f"❌ Admin only. Your ID: {update.effective_user.id}")
        return
    if len(ctx.args) != 2:
        await update.message.reply_text("Usage: /addbal <user_id|@username> <amount_rupees>")
        return
    try:
        paise = int(float(ctx.args[1]) * 100)
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.")
        return
    user = get_user(ctx.args[0])
    if not user:
        await update.message.reply_text(NOT_FOUND_MSG.format(q=ctx.args[0]), parse_mode="Markdown")
        return
    add_balance(user["telegram_id"], paise)
    await update.message.reply_text(
        f"✅ Added {fmt(paise)} to *{user['first_name']}* (`{user['telegram_id']}`).\n"
        f"💳 New balance: {fmt(user['balance'] + paise)}",
        parse_mode="Markdown",
    )


async def cmd_deductbal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(f"❌ Admin only. Your ID: {update.effective_user.id}")
        return
    if len(ctx.args) != 2:
        await update.message.reply_text("Usage: /deductbal <user_id|@username> <amount_rupees>")
        return
    try:
        paise = int(float(ctx.args[1]) * 100)
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.")
        return
    user = get_user(ctx.args[0])
    if not user:
        await update.message.reply_text(NOT_FOUND_MSG.format(q=ctx.args[0]), parse_mode="Markdown")
        return
    deduct_balance_db(user["telegram_id"], paise)
    await update.message.reply_text(
        f"✅ Deducted {fmt(paise)} from *{user['first_name']}* (`{user['telegram_id']}`).\n"
        f"💳 New balance: {fmt(max(0, user['balance'] - paise))}",
        parse_mode="Markdown",
    )


async def cmd_checkbal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(f"❌ Admin only. Your ID: {update.effective_user.id}")
        return
    if len(ctx.args) != 1:
        await update.message.reply_text("Usage: /checkbal <user_id|@username>")
        return
    user = get_user(ctx.args[0])
    if not user:
        await update.message.reply_text(NOT_FOUND_MSG.format(q=ctx.args[0]), parse_mode="Markdown")
        return
    await update.message.reply_text(
        f"👤 *{user['first_name']}* (`{user['telegram_id']}`)\n"
        f"🔗 @{user['username'] or '—'}\n"
        f"💰 Balance: {fmt(user['balance'])}\n"
        f"📈 Total earned: {fmt(user['total_earned'])}\n"
        f"📤 Total withdrawn: {fmt(user['total_withdrawn'])}",
        parse_mode="Markdown",
    )


async def cmd_setbal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(f"❌ Admin only. Your ID: {update.effective_user.id}")
        return
    if len(ctx.args) != 2:
        await update.message.reply_text("Usage: /setbal <user_id|@username> <amount_rupees>")
        return
    try:
        paise = int(float(ctx.args[1]) * 100)
    except ValueError:
        await update.message.reply_text("❌ Invalid amount.")
        return
    user = get_user(ctx.args[0])
    if not user:
        await update.message.reply_text(NOT_FOUND_MSG.format(q=ctx.args[0]), parse_mode="Markdown")
        return
    set_balance(user["telegram_id"], paise)
    await update.message.reply_text(
        f"✅ Set *{user['first_name']}* (`{user['telegram_id']}`) balance to {fmt(paise)}",
        parse_mode="Markdown",
    )


async def cmd_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: full details of a user — balance, tasks, withdrawals."""
    if update.effective_user.id != ADMIN_ID:
        return
    if len(ctx.args) != 1:
        await update.message.reply_text("Usage: /user <user_id|@username>")
        return

    user = get_user(ctx.args[0])
    if not user:
        await update.message.reply_text(NOT_FOUND_MSG.format(q=ctx.args[0]), parse_mode="Markdown")
        return

    uid  = user["telegram_id"]
    conn = get_db()
    c    = conn.cursor()

    # Task summary
    c.execute("""
        SELECT
            COUNT(*) AS total,
            COUNT(CASE WHEN status='approved'       THEN 1 END) AS approved,
            COUNT(CASE WHEN status='pending_review' THEN 1 END) AS pending,
            COUNT(CASE WHEN status='rejected'       THEN 1 END) AS rejected,
            COUNT(CASE WHEN status='cancelled'      THEN 1 END) AS cancelled
        FROM tasks WHERE user_id=?
    """, (uid,))
    ts = dict(c.fetchone())

    approved_nums = sum(
        len(json.loads(r["selected"]))
        for r in c.execute("SELECT selected FROM tasks WHERE user_id=? AND status='approved'", (uid,)).fetchall()
    )

    # Withdrawal summary
    c.execute("""
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(amount), 0) AS total_amount,
            COUNT(CASE WHEN status='pending' THEN 1 END) AS pending_count,
            COALESCE(SUM(CASE WHEN status='pending' THEN amount END), 0) AS pending_amount
        FROM withdrawals WHERE user_id=?
    """, (uid,))
    ws = dict(c.fetchone())

    # Referral summary
    c.execute("SELECT level, COUNT(*) as cnt FROM referrals WHERE referrer_id=? GROUP BY level", (uid,))
    ref_levels = {row["level"]: row["cnt"] for row in c.fetchall()}

    conn.close()

    upi    = user.get("upi_id") or "Not set"
    ref_by = user.get("referred_by") or "—"

    msg = (
        f"👤 *User Details*\n\n"
        f"🆔 ID: `{uid}`\n"
        f"📛 Name: {user['first_name']}\n"
        f"🔗 Username: @{user['username'] or '—'}\n"
        f"👥 Referred by: `{ref_by}`\n"
        f"🏦 UPI: `{upi}`\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 *Balance & Earnings*\n\n"
        f"💳 Current balance: {fmt(user['balance'])}\n"
        f"📈 Total earned: {fmt(user['total_earned'])}\n"
        f"📤 Total withdrawn: {fmt(user['total_withdrawn'])}\n"
        f"🤝 Referral earnings: {fmt(user.get('referral_earnings', 0))}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📋 *Tasks*\n\n"
        f"Total: {ts['total']}  |  ✅ {ts['approved']}  |  ⏳ {ts['pending']}  |  ❌ {ts['rejected']}  |  🚫 {ts['cancelled']}\n"
        f"📞 Numbers approved: {approved_nums}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💸 *Withdrawals*\n\n"
        f"Total requests: {ws['total']}\n"
        f"Total amount: {fmt(ws['total_amount'])}\n"
        f"⏳ Pending: {ws['pending_count']} ({fmt(ws['pending_amount'])})\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👥 *Referrals*\n\n"
        f"🥇 L1: {ref_levels.get(1,0)}  |  🥈 L2: {ref_levels.get(2,0)}  |  🥉 L3: {ref_levels.get(3,0)}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_listusers(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: list all registered users."""
    if update.effective_user.id != ADMIN_ID:
        return
    conn = get_db()
    rows = conn.execute(
        "SELECT telegram_id, first_name, username, balance FROM users ORDER BY rowid DESC LIMIT 50"
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No users registered yet.")
        return
    lines = [f"👥 *Registered Users ({len(rows)}):*\n"]
    for r in rows:
        uname = f"@{r['username']}" if r['username'] else "—"
        lines.append(f"• `{r['telegram_id']}` {r['first_name']} {uname} — {fmt(r['balance'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(f"❌ Admin only. Your ID: {update.effective_user.id}")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    text  = " ".join(ctx.args)
    conn  = get_db()
    users = [r["telegram_id"] for r in conn.execute("SELECT telegram_id FROM users").fetchall()]
    conn.close()
    sent, failed = 0, 0
    for uid in users:
        try:
            await ctx.bot.send_message(uid, f"📢 *Announcement*\n\n{text}", parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"📢 Done!\n✅ Sent: {sent}\n❌ Failed: {failed}")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: full bot statistics."""
    if update.effective_user.id != ADMIN_ID:
        return
    conn = get_db()
    c    = conn.cursor()

    total_users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_tasks = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    c.execute("""
        SELECT
            COUNT(CASE WHEN status='pending_review' THEN 1 END) AS pending,
            COUNT(CASE WHEN status='approved'       THEN 1 END) AS approved,
            COUNT(CASE WHEN status='rejected'       THEN 1 END) AS rejected,
            COUNT(CASE WHEN status='cancelled'      THEN 1 END) AS cancelled
        FROM tasks
    """)
    ts = dict(c.fetchone())

    approved_rows  = c.execute("SELECT selected FROM tasks WHERE status='approved'").fetchall()
    numbers_sent   = sum(len(json.loads(r["selected"])) for r in approved_rows)
    total_earned   = c.execute("SELECT COALESCE(SUM(earn_amount),0) FROM tasks WHERE status='approved'").fetchone()[0]
    total_paidout  = c.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='pending'").fetchone()[0]
    total_referrals= c.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
    pool_used      = c.execute("SELECT COUNT(DISTINCT json_each.value) FROM tasks, json_each(tasks.numbers)").fetchone()[0]

    approval_rate  = (ts['approved'] / (ts['approved'] + ts['rejected']) * 100) if (ts['approved'] + ts['rejected']) > 0 else 0

    conn.close()

    sep   = "─" * 38
    rate  = f"{approval_rate:.1f}%"
    pool  = f"{pool_used}/{len(NUMBER_POOL)}"
    earn  = f"Rs{total_earned/100:.2f}"
    paid  = f"Rs{total_paidout/100:.2f}"

    def row(label, value):
        return f"  {label:<30} {value}"

    lines = [
        "📊 *Bot Statistics*\n",
        "```",
        f"{'Stat':<32} Count",
        sep,
        row("Total users",            total_users),
        row("Total referrals",        total_referrals),
        sep,
        row("Tasks created",          total_tasks),
        row("Tasks pending review",   ts["pending"]),
        row("Tasks approved",         ts["approved"]),
        row("Tasks rejected",         ts["rejected"]),
        row("Tasks cancelled",        ts["cancelled"]),
        row("Approval rate",          rate),
        sep,
        row("Numbers approved/sent",  numbers_sent),
        row("Unique numbers used",    pool),
        sep,
        row("Total earned by users",  earn),
        row("Pending withdrawals",    paid),
        "```",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: list pending review tasks."""
    if update.effective_user.id != ADMIN_ID:
        return
    conn  = get_db()
    tasks = conn.execute(
        "SELECT t.id, t.user_id, u.first_name, t.selected, t.task_type "
        "FROM tasks t JOIN users u ON u.telegram_id=t.user_id "
        "WHERE t.status='pending_review' ORDER BY t.created_at"
    ).fetchall()
    conn.close()
    if not tasks:
        await update.message.reply_text("✅ No pending tasks.")
        return
    lines = [f"📋 *Pending tasks ({len(tasks)}):*\n"]
    for t in tasks:
        sel = len(json.loads(t["selected"]))
        lines.append(f"• Task #{t['id']} — {t['first_name']} (`{t['user_id']}`) — {sel} numbers — {t['task_type']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─────────────────────────────────────────────
# POOL STATUS COMMAND
# ─────────────────────────────────────────────
async def cmd_pool(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: show number pool usage."""
    if update.effective_user.id != ADMIN_ID:
        return

    conn = get_db()
    rows = conn.execute("SELECT numbers FROM tasks").fetchall()
    conn.close()

    assigned: set[str] = set()
    for row in rows:
        assigned.update(json.loads(row["numbers"]))

    used      = [n for n in NUMBER_POOL if n in assigned]
    remaining = [n for n in NUMBER_POOL if n not in assigned]

    used_lines      = "\n".join(f"  {i}. +{n}" for i, n in enumerate(used, 1)) or "  None"
    remaining_lines = "\n".join(f"  {i}. +{n}" for i, n in enumerate(remaining, 1)) or "  None"

    sep = "─" * 30
    msg = (
        f"📊 *Number Pool Status*\n\n"
        f"`{sep}`\n"
        f"✅ *Used: {len(used)}/{len(NUMBER_POOL)}*\n"
        f"`{used_lines}`\n\n"
        f"`{sep}`\n"
        f"🟢 *Remaining: {len(remaining)}/{len(NUMBER_POOL)}*\n"
        f"`{remaining_lines}`\n"
        f"`{sep}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_listnumbers(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: list every number in the pool with its usage status."""
    if update.effective_user.id != ADMIN_ID:
        return

    if not NUMBER_POOL:
        await update.message.reply_text("⚠️ Number pool is empty. Use ➕ *Add Number* to add some.", parse_mode="Markdown")
        return

    conn = get_db()

    # Approved numbers (appeared in at least one approved task)
    approved_rows = conn.execute(
        "SELECT numbers FROM tasks WHERE status='approved'"
    ).fetchall()
    approved_nums: set[str] = set()
    for row in approved_rows:
        approved_nums.update(json.loads(row["numbers"]))

    # Pending-review numbers
    pending_rows = conn.execute(
        "SELECT numbers FROM tasks WHERE status='pending_review'"
    ).fetchall()
    pending_nums: set[str] = set()
    for row in pending_rows:
        pending_nums.update(json.loads(row["numbers"]))

    # In-progress (waiting_selection / waiting_screenshot)
    active_rows = conn.execute(
        "SELECT numbers FROM tasks WHERE status IN ('waiting_selection','waiting_screenshot')"
    ).fetchall()
    active_nums: set[str] = set()
    for row in active_rows:
        active_nums.update(json.loads(row["numbers"]))

    conn.close()

    lines = []
    for i, n in enumerate(NUMBER_POOL, 1):
        if n in approved_nums:
            flag = "✅"   # used & approved
        elif n in pending_nums:
            flag = "⏳"   # pending review
        elif n in active_nums:
            flag = "🔄"   # currently in an active task
        else:
            flag = "🟢"   # free / available
        lines.append(f"{flag} {i}. `+{n}`")

    legend = (
        "✅ Approved  |  ⏳ Pending  |  🔄 Active  |  🟢 Free\n"
        f"📊 Total: *{len(NUMBER_POOL)}*  •  "
        f"Free: *{sum(1 for n in NUMBER_POOL if n not in approved_nums | pending_nums | active_nums)}*"
    )

    # Telegram has a 4096-char limit per message; split if needed
    chunk, chunks = [], []
    for line in lines:
        chunk.append(line)
        if len("\n".join(chunk)) > 3500:
            chunks.append(chunk[:-1])
            chunk = [line]
    chunks.append(chunk)

    for idx, part in enumerate(chunks):
        header = f"📋 *Number Pool* ({idx+1}/{len(chunks)})\n\n" if len(chunks) > 1 else "📋 *Number Pool*\n\n"
        body   = "\n".join(part)
        footer = f"\n\n{legend}" if idx == len(chunks) - 1 else ""
        await update.message.reply_text(header + body + footer, parse_mode="Markdown")


# ─────────────────────────────────────────────
# MENU HANDLERS
# ─────────────────────────────────────────────
async def handle_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user  = ensure_user(update)
    uid   = update.effective_user.id
    stats = get_task_stats(uid)
    refs  = get_referral_stats(uid)

    upi_display    = user.get("upi_id") or "Not set"
    crypto_display = user.get("crypto_address") or "Not set"
    has_qr         = bool(user.get("upi_qr_file_id"))
    qr_status      = "✅ Saved" if has_qr else "❌ Not uploaded"

    upi_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Set UPI / QR Code", callback_data="upi_setup_start"),
    ]])

    text = (
        f"👤 *My Account*\n\n"
        f"👋 Name: {user['first_name']}\n"
        f"🆔 Telegram ID: `{uid}`\n"
        f"🏦 UPI ID: `{upi_display}`\n"
        f"🖼 QR Code: {qr_status}\n"
        f"💎 BEP20/Binance Wallet: `{crypto_display}`\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 *Task Statistics*\n\n"
        f"📲 Total tasks submitted: {stats['total_submitted']}\n"
        f"✅ Tasks approved: {stats['approved']}\n"
        f"⏳ Tasks pending: {stats['pending']}\n"
        f"❌ Tasks rejected: {stats['rejected']}\n"
        f"📞 Total numbers sent: {stats['numbers_sent']}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💰 *Earnings*\n\n"
        f"💵 Total earned: {fmt(user['total_earned'])}\n"
        f"🏦 Total withdrawn: {fmt(user['total_withdrawn'])}\n"
        f"💳 Current balance: {fmt(user['balance'])}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"👥 *Referral Summary*\n\n"
        f"🔗 Total referrals: {refs['total']}\n"
        f"🥇 Level 1: {refs['l1']} | 🥈 Level 2: {refs['l2']} | 🥉 Level 3: {refs['l3']}\n"
        f"💸 Earned from referrals: {fmt(refs['earned'])}"
    )

    # If user has a saved QR, send it as photo with account text as caption
    if has_qr:
        await update.message.reply_photo(
            photo=user["upi_qr_file_id"],
            caption=text,
            parse_mode="Markdown",
            reply_markup=upi_kb,
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=upi_kb,
        )
    # Always send main menu keyboard separately so user doesn't lose navigation
    await update.message.reply_text("👇 Main menu:", reply_markup=MAIN_MENU)


async def _start_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE, task_type: str) -> None:
    user = ensure_user(update)
    uid  = update.effective_user.id

    # Block if user already has an active task
    if user.get("current_task_id"):
        existing = get_task(user["current_task_id"])
        if existing and existing["status"] in ("waiting_selection", "waiting_screenshot"):
            await update.message.reply_text(
                "⚠️ You already have an active task. Please complete or cancel it first.",
                reply_markup=CANCEL_KB,
            )
            return

    numbers = pick_numbers(uid)
    if not numbers:
        await update.message.reply_text(
            "⚠️ No new numbers available for you right now. Please try again later.",
            reply_markup=MAIN_MENU,
        )
        return

    task_id = create_task(uid, numbers, task_type)
    set_current_task(uid, task_id)
    set_state(uid, None)

    plain_promo = PROMO_TEXT.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    total_paise = NUMBERS_PER_TASK * EARN_PER_NUMBER
    earn_str    = f"&#8377;{total_paise/100:.2f} (${NUMBERS_PER_TASK * USDT_PER_NUMBER:.2f} USDT)"

    if task_type == "send_earn":
        num_lines = "".join(f"{i}. <code>+{n}</code>\n" for i, n in enumerate(numbers, 1))
        header = (
            f"📤 <b>Send Text &amp; Earn</b> — Task #{task_id}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📞 <b>Your Numbers:</b>\n"
            f"{num_lines}"
            f"━━━━━━━━━━━━━━━\n"
            f"📝 <b>Promo text (tap to copy):</b>\n\n"
            f"<pre>{plain_promo}</pre>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Send the promo to all {NUMBERS_PER_TASK} numbers, then tap ✅ <b>Done</b>.\n"
            f"You earn <b>{earn_str}</b> once all numbers are approved."
        )
        kb = build_task_keyboard(task_id, [])
        promo_path = Path(PROMO_IMAGE)
        if promo_path.exists():
            with open(promo_path, "rb") as img:
                await update.message.reply_photo(
                    photo=img, caption=header, parse_mode="HTML", reply_markup=kb,
                )
        else:
            await update.message.reply_text(
                header, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True,
            )
    else:
        encoded   = quote(PROMO_TEXT.replace("*", "").replace("_", ""), safe="")
        num_lines = "".join(
            f'{i}. <a href="https://wa.me/{n}?text={encoded}">Open WhatsApp ➜ +{n}</a>\n'
            for i, n in enumerate(numbers, 1)
        )
        header = (
            f"📲 <b>Click &amp; Send</b> — Task #{task_id}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📞 <b>Tap each link to open WhatsApp &amp; send:</b>\n"
            f"{num_lines}"
            f"━━━━━━━━━━━━━━━\n"
            f"📝 <b>Promo text (tap to copy):</b>\n\n"
            f"<pre>{plain_promo}</pre>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Send to all {NUMBERS_PER_TASK} numbers, then tap ✅ <b>Done</b>.\n"
            f"You earn <b>{earn_str}</b> once all numbers are approved."
        )
        kb = build_task_keyboard(task_id, [])

        # Send example video first
        video_path = Path("bot/example_task.mp4")
        if video_path.exists():
            with open(video_path, "rb") as vid:
                await update.message.reply_video(
                    video=vid,
                    caption="🎬 <b>Watch this example</b> — open each link, send the promo text, then tap ✅ Done.",
                    parse_mode="HTML",
                )

        promo_path = Path(PROMO_IMAGE)
        if promo_path.exists():
            with open(promo_path, "rb") as img:
                await update.message.reply_photo(
                    photo=img, caption=header, parse_mode="HTML", reply_markup=kb,
                )
        else:
            await update.message.reply_text(
                header, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True,
            )


async def handle_send_earn(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_task(update, ctx, "send_earn")


async def handle_click_send(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_task(update, ctx, "click_send")


async def handle_refer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update)
    uid      = update.effective_user.id
    bot_info = await ctx.bot.get_me()
    link     = f"https://t.me/{bot_info.username}?start={uid}"
    await update.message.reply_text(
        f"👥 *Refer & Earn*\n\n"
        f"Earn every time your referrals earn money:\n\n"
        f"🥇 *Level 1* (direct) — *{REFER_PCT_L1}%* of their earnings\n"
        f"🥈 *Level 2* — *{REFER_PCT_L2}%* of their earnings\n"
        f"🥉 *Level 3* — *{REFER_PCT_L3}%* of their earnings\n\n"
        f"➕ Plus join bonuses when they sign up:\n"
        f"  L1 ₹{REFER_BONUS_L1/100:.0f}  |  L2 ₹{REFER_BONUS_L2/100:.0f}  |  L3 ₹{REFER_BONUS_L3/100:.0f}\n\n"
        f"🔗 Your link:\n`{link}`\n\n"
        f"Tap to copy and share!",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def handle_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = ensure_user(update)
    uid  = update.effective_user.id

    bal = user["balance"]
    can_upi    = bal >= MIN_WITHDRAW_INR
    can_crypto = bal >= MIN_WITHDRAW_CRYPTO

    if not can_upi and not can_crypto:
        await update.message.reply_text(
            f"❌ *Insufficient balance*\n\n"
            f"Your balance: *{fmt(bal)}*\n\n"
            f"Minimum for withdrawal:\n"
            f"🏦 UPI / QR Code — *₹{MIN_WITHDRAW_INR/100:.0f}*\n"
            f"💎 Crypto (BEP20) — *$1.000 USDT* (~{fmt(MIN_WITHDRAW_CRYPTO)})\n\n"
            f"Keep earning and come back!",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )
        return

    upi_hint    = f"\n🏦 Last UPI: `{user['upi_id']}`" if user.get("upi_id") else ""
    crypto_hint = f"\n💎 Last Wallet: `{user['crypto_address']}`" if user.get("crypto_address") else ""

    buttons = []
    if can_upi:
        buttons.append(InlineKeyboardButton("🏦 UPI / QR Code", callback_data="wd_method_upi"))
    if can_crypto:
        buttons.append(InlineKeyboardButton("💎 Crypto (BEP20/USDT)", callback_data="wd_method_crypto"))
    method_kb = InlineKeyboardMarkup([buttons])

    upi_min_note    = "" if can_upi    else f"\n_(UPI needs ₹{MIN_WITHDRAW_INR/100:.0f} — not enough yet)_"
    crypto_min_note = "" if can_crypto else f"\n_(Crypto needs ~{fmt(MIN_WITHDRAW_CRYPTO)} — not enough yet)_"

    await update.message.reply_text(
        f"💰 *Withdraw {fmt(bal)}*\n\n"
        f"Choose your withdrawal method:{upi_hint}{crypto_hint}"
        f"{upi_min_note}{crypto_min_note}",
        parse_mode="Markdown",
        reply_markup=method_kb,
    )


async def handle_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    user = ensure_user(update)

    # Cancel any active task
    if user.get("current_task_id"):
        task = get_task(user["current_task_id"])
        if task and task["status"] in ("waiting_selection", "waiting_screenshot"):
            conn = get_db()
            conn.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task["id"],))
            conn.commit()
            conn.close()
    set_current_task(uid, None)
    set_state(uid, None)
    await update.message.reply_text("✅ Cancelled.", reply_markup=MAIN_MENU)


# ─────────────────────────────────────────────
# TASK CALLBACKS  (number selection + done/cancel)
# ─────────────────────────────────────────────
async def handle_task_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    uid  = update.effective_user.id

    if data.startswith("sel_"):
        _, task_id_s, idx_s = data.split("_")
        task_id = int(task_id_s)
        idx     = int(idx_s)
        task    = get_task(task_id)
        if not task or task["user_id"] != uid or task["status"] != "waiting_selection":
            return
        selected: list[int] = json.loads(task["selected"])
        if idx in selected:
            selected.remove(idx)
        else:
            selected.append(idx)
        selected.sort()
        update_task_selected(task_id, selected)
        await query.edit_message_reply_markup(build_task_keyboard(task_id, selected))

    elif data.startswith("tdone_"):
        task_id = int(data.split("_")[1])
        task    = get_task(task_id)
        if not task or task["user_id"] != uid or task["status"] != "waiting_selection":
            return
        # Auto-select all numbers — no manual selection needed
        all_selected = list(range(1, NUMBERS_PER_TASK + 1))
        update_task_selected(task_id, all_selected)
        conn = get_db()
        conn.execute("UPDATE tasks SET status='waiting_screenshot' WHERE id=?", (task_id,))
        conn.commit()
        conn.close()
        set_state(uid, STATE_WAITING_SCREENSHOT)
        await query.edit_message_reply_markup(None)
        total_paise = NUMBERS_PER_TASK * EARN_PER_NUMBER
        example_path = Path("bot/example_screenshot.jpg")
        caption = (
            f"📸 *Great! Now send your screenshot.*\n\n"
            f"Take a screenshot of your WhatsApp showing all {NUMBERS_PER_TASK} messages sent "
            f"(like the example above) and send it here.\n\n"
            f"💰 You can earn *{fmt(total_paise)}* once approved!"
        )
        if example_path.exists():
            with open(example_path, "rb") as img:
                await ctx.bot.send_photo(
                    uid,
                    photo=img,
                    caption=caption,
                    parse_mode="Markdown",
                    reply_markup=CANCEL_KB,
                )
        else:
            await ctx.bot.send_message(
                uid, caption, parse_mode="Markdown", reply_markup=CANCEL_KB,
            )

    elif data.startswith("tcancel_"):
        task_id = int(data.split("_")[1])
        task    = get_task(task_id)
        if not task or task["user_id"] != uid:
            return
        conn = get_db()
        conn.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task_id,))
        conn.commit()
        conn.close()
        set_current_task(uid, None)
        set_state(uid, None)
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_reply_markup(None)
        await ctx.bot.send_message(uid, "❌ Task cancelled.", reply_markup=MAIN_MENU)


# ─────────────────────────────────────────────
# ADMIN APPROVAL CALLBACKS
# ─────────────────────────────────────────────
async def handle_admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != ADMIN_ID:
        await query.answer("❌ Admin only.", show_alert=True)
        return

    data    = query.data
    task_id = int(data.split("_")[1])
    task    = get_task(task_id)

    if not task:
        await query.edit_message_caption("❌ Task not found.")
        return

    if task["status"] != "pending_review":
        await query.edit_message_reply_markup(None)
        return

    user = get_user(task["user_id"])

    if data.startswith("tapprove_"):
        selected = json.loads(task["selected"])
        earn     = len(selected) * EARN_PER_NUMBER
        conn     = get_db()
        conn.execute(
            "UPDATE tasks SET status='approved', earn_amount=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (earn, task_id),
        )
        conn.commit()
        conn.close()
        add_balance(task["user_id"], earn)
        set_current_task(task["user_id"], None)
        await pay_earn_commissions(task["user_id"], earn, user["first_name"], ctx.bot)

        await query.edit_message_caption(
            f"✅ *Approved* — Task #{task_id}\n"
            f"👤 {user['first_name']} (`{task['user_id']}`)\n"
            f"📞 Numbers: {len(selected)} | 💰 Earned: {fmt(earn)}",
            parse_mode="Markdown",
        )
        try:
            await ctx.bot.send_message(
                task["user_id"],
                f"🎉 *Task Approved!*\n\n"
                f"✅ {len(selected)} number(s) approved\n"
                f"💰 You earned *{fmt(earn)}*!\n"
                f"💳 New balance: *{fmt(user['balance'] + earn)}*",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
        except Exception:
            pass

    elif data.startswith("treject_"):
        conn = get_db()
        conn.execute(
            "UPDATE tasks SET status='rejected', reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (task_id,),
        )
        conn.commit()
        conn.close()
        set_current_task(task["user_id"], None)
        set_state(task["user_id"], None)

        await query.edit_message_caption(
            f"❌ *Rejected* — Task #{task_id}\n"
            f"👤 {user['first_name']} (`{task['user_id']}`)",
            parse_mode="Markdown",
        )
        try:
            await ctx.bot.send_message(
                task["user_id"],
                "❌ *Task Rejected*\n\n"
                "Your screenshot was not approved this time.\n"
                "Please try again with a clear screenshot!",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
        except Exception:
            pass


# ─────────────────────────────────────────────
# SUBSCRIBE & EARN HANDLER
# ─────────────────────────────────────────────
async def handle_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = ensure_user(update)
    uid  = update.effective_user.id

    conn = get_db()
    existing = conn.execute(
        "SELECT status FROM subscriptions WHERE user_id=?", (uid,)
    ).fetchone()
    conn.close()

    if existing:
        status = existing["status"]
        if status == "approved":
            await update.message.reply_text(
                "✅ You have already earned your subscription bonus!\n"
                f"💰 {fmt(SUBSCRIBE_EARN)} was credited to your account.",
                reply_markup=MAIN_MENU,
            )
        elif status == "pending":
            await update.message.reply_text(
                "⏳ Your subscription screenshot is already under review. Please wait for approval.",
                reply_markup=MAIN_MENU,
            )
        elif status == "rejected":
            await update.message.reply_text(
                "❌ Your previous subscription was rejected.\n\n"
                "📸 Send a new screenshot to try again:",
                parse_mode="Markdown",
                reply_markup=CANCEL_KB,
            )
            set_state(uid, STATE_WAITING_SUBSCRIBE_SCREENSHOT)
        return

    set_state(uid, STATE_WAITING_SUBSCRIBE_SCREENSHOT)
    await update.message.reply_text(
        f"📺 *Subscribe & Earn*\n\n"
        f"1️⃣ Subscribe to our channel:\n{SUBSCRIBE_CHANNEL}\n\n"
        f"2️⃣ Take a screenshot showing you are subscribed.\n\n"
        f"3️⃣ Send the screenshot here to earn *{fmt(SUBSCRIBE_EARN)}*!",
        parse_mode="Markdown",
        reply_markup=CANCEL_KB,
        disable_web_page_preview=True,
    )


# ─────────────────────────────────────────────
# ADMIN SUBSCRIBE CALLBACK
# ─────────────────────────────────────────────
async def handle_subscribe_admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != ADMIN_ID:
        await query.answer("❌ Admin only.", show_alert=True)
        return

    data   = query.data
    sub_id = int(data.split("_")[1])
    action = data.split("_")[0]   # "subapprove" or "subreject"

    conn = get_db()
    sub  = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    conn.close()

    if not sub:
        await query.edit_message_caption("❌ Subscription request not found.")
        return

    sub  = dict(sub)
    if sub["status"] != "pending":
        await query.edit_message_reply_markup(None)
        return

    user = get_user(sub["user_id"])

    if action == "subapprove":
        conn = get_db()
        conn.execute(
            "UPDATE subscriptions SET status='approved', earn_amount=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (SUBSCRIBE_EARN, sub_id),
        )
        conn.commit()
        conn.close()
        add_balance(sub["user_id"], SUBSCRIBE_EARN)

        await query.edit_message_caption(
            f"✅ *Subscription Approved* — #{sub_id}\n"
            f"👤 {user['first_name']} (`{sub['user_id']}`)\n"
            f"💰 Credited: {fmt(SUBSCRIBE_EARN)}",
            parse_mode="Markdown",
        )
        try:
            await ctx.bot.send_message(
                sub["user_id"],
                f"🎉 *Subscription Approved!*\n\n"
                f"💰 You earned *{fmt(SUBSCRIBE_EARN)}* for subscribing!\n"
                f"💳 Added to your balance.",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
        except Exception:
            pass

    elif action == "subreject":
        conn = get_db()
        conn.execute(
            "UPDATE subscriptions SET status='rejected', reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (sub_id,),
        )
        conn.commit()
        conn.close()

        await query.edit_message_caption(
            f"❌ *Subscription Rejected* — #{sub_id}\n"
            f"👤 {user['first_name']} (`{sub['user_id']}`)",
            parse_mode="Markdown",
        )
        try:
            await ctx.bot.send_message(
                sub["user_id"],
                "❌ *Subscription Rejected*\n\n"
                "Your screenshot was not approved. Please send a clearer screenshot showing you are subscribed.",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
        except Exception:
            pass


# ─────────────────────────────────────────────
# RE-VERIFY MENU HANDLER
# ─────────────────────────────────────────────
async def handle_reverify(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = ensure_user(update)
    uid  = update.effective_user.id

    if user.get("state") in (STATE_WAITING_REVERIFY_NUMBER, STATE_WAITING_REVERIFY_SCREENSHOT):
        await update.message.reply_text(
            "⚠️ You already have a re-verification in progress. Please send your screenshot.",
            reply_markup=CANCEL_KB,
        )
        return

    set_state(uid, STATE_WAITING_REVERIFY_NUMBER)
    await update.message.reply_text(
        "🔄 *Re-Verification*\n\n"
        "Send *all 5 numbers* you messaged (one per line or comma-separated):\n\n"
        "`5542988039116\n5545991050379\n5562993811215\n5583994002077\n5588993837663`",
        parse_mode="Markdown",
        reply_markup=CANCEL_KB,
    )


# ─────────────────────────────────────────────
# ADMIN RE-VERIFY CALLBACK
# ─────────────────────────────────────────────
async def handle_reverify_admin_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data  = query.data

    rv_id   = int(data.split("_")[1])
    action  = data.split("_")[0]   # "rvapprove" or "rvreject"

    conn = get_db()
    rv   = conn.execute("SELECT * FROM reverifications WHERE id=?", (rv_id,)).fetchone()
    conn.close()

    if not rv:
        await query.edit_message_caption("❌ Re-verification request not found.")
        return

    rv = dict(rv)
    if rv["status"] != "pending":
        await query.edit_message_reply_markup(None)
        return

    user = get_user(rv["user_id"])

    if action == "rvapprove":
        earn = EARN_PER_NUMBER
        conn = get_db()
        conn.execute(
            "UPDATE reverifications SET status='approved', earn_amount=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (earn, rv_id),
        )
        conn.commit()
        conn.close()
        add_balance(rv["user_id"], earn)
        await pay_earn_commissions(rv["user_id"], earn, user["first_name"], ctx.bot)

        await query.edit_message_caption(
            f"✅ *Re-verify Approved* — RV #{rv_id}\n"
            f"👤 {user['first_name']} (`{rv['user_id']}`)\n"
            f"📞 Number: +{rv['number']}\n"
            f"💰 Credited: {fmt(earn)}",
            parse_mode="Markdown",
        )
        try:
            await ctx.bot.send_message(
                rv["user_id"],
                f"🎉 *Re-verification Approved!*\n\n"
                f"📞 Number: `+{rv['number']}`\n"
                f"💰 You earned *{fmt(earn)}*!\n"
                f"💳 New balance: *{fmt(user['balance'] + earn)}*",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
        except Exception:
            pass

    elif action == "rvreject":
        conn = get_db()
        conn.execute(
            "UPDATE reverifications SET status='rejected', reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (rv_id,),
        )
        conn.commit()
        conn.close()

        await query.edit_message_caption(
            f"❌ *Re-verify Rejected* — RV #{rv_id}\n"
            f"👤 {user['first_name']} (`{rv['user_id']}`)\n"
            f"📞 Number: +{rv['number']}",
            parse_mode="Markdown",
        )
        try:
            await ctx.bot.send_message(
                rv["user_id"],
                "❌ *Re-verification Rejected*\n\n"
                "Your re-verification request was not approved.\n"
                "Make sure your screenshot clearly shows the sent message.",
                parse_mode="Markdown",
                reply_markup=MAIN_MENU,
            )
        except Exception:
            pass


# ─────────────────────────────────────────────
# WITHDRAWAL METHOD SELECTION CALLBACK
# ─────────────────────────────────────────────
async def handle_wd_method(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid  = update.effective_user.id
    user = get_user(uid)
    if not user:
        return

    if query.data == "wd_method_upi":
        if user["balance"] < MIN_WITHDRAW_INR:
            await query.edit_message_text(
                f"❌ Minimum for UPI withdrawal is *₹{MIN_WITHDRAW_INR/100:.0f}*.\n"
                f"Your balance: *{fmt(user['balance'])}*",
                parse_mode="Markdown",
            )
            return
        upi_hint = f" (last: `{user['upi_id']}`)" if user.get("upi_id") else ""
        set_state(uid, STATE_WAITING_UPI)
        await query.edit_message_text(
            f"🏦 *UPI Withdrawal*\n\n"
            f"Minimum: *₹{MIN_WITHDRAW_INR/100:.0f}*\n\n"
            f"Enter your UPI ID{upi_hint}:\n"
            f"_(e.g. `name@upi`, `number@paytm`)_",
            parse_mode="Markdown",
        )

    elif query.data == "wd_method_crypto":
        if user["balance"] < MIN_WITHDRAW_CRYPTO:
            await query.edit_message_text(
                f"❌ Minimum for Crypto withdrawal is *$1.000 USDT* (~{fmt(MIN_WITHDRAW_CRYPTO)}).\n"
                f"Your balance: *{fmt(user['balance'])}*",
                parse_mode="Markdown",
            )
            return
        crypto_hint = f" (last: `{user['crypto_address']}`)" if user.get("crypto_address") else ""
        set_state(uid, STATE_WAITING_CRYPTO)
        await query.edit_message_text(
            f"💎 *Crypto Withdrawal (BEP20/USDT)*\n\n"
            f"Minimum: *$1.000 USDT* (~{fmt(MIN_WITHDRAW_CRYPTO)})\n\n"
            f"Enter your BEP20 / Binance wallet address{crypto_hint}:\n"
            f"_(starts with `0x`, 42 characters)_\n\n"
            f"⚠️ Only send to a *BEP20 / Binance Smart Chain* address. "
            f"Sending to wrong network will result in loss of funds.",
            parse_mode="Markdown",
        )


async def handle_upi_setup_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id

    if query.data == "upi_setup_start":
        set_state(uid, STATE_WAITING_UPI_SETUP)
        await ctx.bot.send_message(
            uid,
            "🏦 *Set Your UPI ID*\n\nPlease type your UPI ID (e.g. `name@upi`):",
            parse_mode="Markdown",
            reply_markup=CANCEL_KB,
        )

    elif query.data == "upi_skip_qr":
        set_state(uid, None)
        await query.edit_message_reply_markup(None)
        await ctx.bot.send_message(
            uid,
            "✅ UPI ID saved! You can add a QR code anytime from 👤 Account.",
            reply_markup=MAIN_MENU,
        )


async def handle_wd_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid  = update.effective_user.id
    user = get_user(uid)

    if not user:
        return

    if query.data == "wd_change_upi":
        set_state(uid, STATE_WAITING_UPI)
        await query.edit_message_text("Enter your new UPI ID:")
        return

    if query.data == "wd_change_crypto":
        set_state(uid, STATE_WAITING_CRYPTO)
        await query.edit_message_text("Enter your new BEP20 / Binance wallet address:")
        return

    if query.data in ("wd_confirm_upi", "wd_confirm_crypto"):
        amount  = user["balance"]
        min_req = MIN_WITHDRAW_INR if query.data == "wd_confirm_upi" else MIN_WITHDRAW_CRYPTO
        if amount < min_req:
            await query.edit_message_text("❌ Insufficient balance.")
            return

        if query.data == "wd_confirm_upi":
            upi    = user.get("pending_upi") or user.get("upi_id", "")
            conn   = get_db()
            conn.execute(
                "INSERT INTO withdrawals (user_id, amount, upi, payment_method) VALUES (?,?,?,'upi')",
                (uid, amount, upi),
            )
            conn.execute("UPDATE users SET upi_id=?, pending_upi=NULL WHERE telegram_id=?", (upi, uid))
            conn.commit()
            conn.close()
            deduct_balance_db(uid, amount)
            set_state(uid, None)

            await query.edit_message_text(
                f"✅ *Withdrawal submitted!*\n\n"
                f"💰 Amount: *{fmt(amount)}*\n"
                f"🏦 Method: UPI\n"
                f"🆔 UPI ID: `{upi}`\n\n"
                f"Payment within 24 hours. ⏳",
                parse_mode="Markdown",
            )
            try:
                await ctx.bot.send_message(
                    ADMIN_ID,
                    f"💸 *Withdrawal Request — UPI*\n\n"
                    f"👤 {user['first_name']} (`{uid}`)\n"
                    f"💰 Amount: {fmt(amount)}\n"
                    f"🏦 Method: UPI\n"
                    f"🆔 UPI ID: `{upi}`",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

        else:  # wd_confirm_crypto
            addr = user.get("pending_crypto") or user.get("crypto_address", "")
            conn = get_db()
            conn.execute(
                "INSERT INTO withdrawals (user_id, amount, payment_method, crypto_address) VALUES (?,?,'crypto',?)",
                (uid, amount, addr),
            )
            conn.execute(
                "UPDATE users SET crypto_address=?, pending_crypto=NULL WHERE telegram_id=?",
                (addr, uid),
            )
            conn.commit()
            conn.close()
            deduct_balance_db(uid, amount)
            set_state(uid, None)

            usdt_amt = to_usdt(amount)
            await query.edit_message_text(
                f"✅ *Withdrawal submitted!*\n\n"
                f"💰 Amount: *{fmt(amount)}*\n"
                f"💎 Method: Crypto (BEP20/USDT)\n"
                f"📬 Wallet: `{addr}`\n\n"
                f"Payment within 24 hours. ⏳",
                parse_mode="Markdown",
            )
            try:
                await ctx.bot.send_message(
                    ADMIN_ID,
                    f"💸 *Withdrawal Request — Crypto BEP20*\n\n"
                    f"👤 {user['first_name']} (`{uid}`)\n"
                    f"💰 Amount: {fmt(amount)}\n"
                    f"💎 Method: Crypto (BEP20/USDT)\n"
                    f"📬 Wallet: `{addr}`\n"
                    f"≈ ${usdt_amt:.3f} USDT",
                    parse_mode="Markdown",
                )
            except Exception:
                pass


# ─────────────────────────────────────────────
# STATE-BASED MESSAGE HANDLER
# ─────────────────────────────────────────────
def _md(text: str) -> str:
    """Escape Markdown v1 special characters in user-supplied strings."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:          # callback queries reach here — ignore them
        return
    user  = ensure_user(update)
    uid   = update.effective_user.id
    state = user.get("state")

    # ── Admin number-pool states ──────────────────────────────────
    if state == STATE_ADMIN_WAITING_ADD_NUMBER:
        text = update.message.text.strip() if update.message.text else ""
        if not text:
            await update.message.reply_text("Please send the number(s) as text.", reply_markup=CANCEL_KB)
            return
        tokens = [t.strip() for line in text.splitlines() for t in line.split() if t.strip()]
        existing = set(NUMBER_POOL)
        added, dupes = [], []
        for n in tokens:
            if n in existing:
                dupes.append(n)
            else:
                added.append(n)
                existing.add(n)
        if added:
            save_numbers(list(existing))
            reload_pool()
        set_state(uid, None)
        parts = []
        if added:
            parts.append(f"✅ Added *{len(added)}* number(s):\n" + "\n".join(f"  `{n}`" for n in added))
        if dupes:
            parts.append(f"⚠️ Already in pool: *{len(dupes)}*")
        parts.append(f"📊 Pool total: *{len(NUMBER_POOL)}* numbers")
        await update.message.reply_text("\n".join(parts), parse_mode="Markdown", reply_markup=ADMIN_MENU)
        return

    if state == STATE_ADMIN_WAITING_DEL_NUMBER:
        text = update.message.text.strip() if update.message.text else ""
        if not text:
            await update.message.reply_text("Please send the number(s) as text.", reply_markup=CANCEL_KB)
            return
        tokens = [t.strip() for line in text.splitlines() for t in line.split() if t.strip()]
        pool_set = set(NUMBER_POOL)
        removed, not_found = [], []
        for n in tokens:
            if n in pool_set:
                pool_set.discard(n)
                removed.append(n)
            else:
                not_found.append(n)
        if removed:
            save_numbers(list(pool_set))
            reload_pool()
        set_state(uid, None)
        parts = []
        if removed:
            parts.append(f"🗑️ Removed *{len(removed)}* number(s):\n" + "\n".join(f"  `{n}`" for n in removed))
        if not_found:
            parts.append(f"❌ Not found: *{len(not_found)}*")
        parts.append(f"📊 Pool total: *{len(NUMBER_POOL)}* numbers")
        await update.message.reply_text("\n".join(parts), parse_mode="Markdown", reply_markup=ADMIN_MENU)
        return

    if state == STATE_ADMIN_WAITING_UPLOAD:
        doc = update.message.document
        if not doc:
            await update.message.reply_text(
                "📎 Please send a `.txt` file now.", parse_mode="Markdown", reply_markup=CANCEL_KB
            )
            return
        file = await ctx.bot.get_file(doc.file_id)
        raw = await file.download_as_bytearray()
        lines = raw.decode("utf-8", errors="ignore").splitlines()
        new_nums = [ln.strip() for ln in lines if ln.strip()]
        existing = set(NUMBER_POOL)
        added = [n for n in new_nums if n not in existing]
        dupes = len(new_nums) - len(added)
        existing.update(added)
        save_numbers(list(existing))
        reload_pool()
        set_state(uid, None)
        await update.message.reply_text(
            f"✅ *Bulk upload complete!*\n\n"
            f"➕ Added: *{len(added)}*\n"
            f"⚠️ Skipped (duplicates): *{dupes}*\n"
            f"📊 Pool total: *{len(NUMBER_POOL)}* numbers",
            parse_mode="Markdown",
            reply_markup=ADMIN_MENU,
        )
        return
    # ── End admin states ──────────────────────────────────────────

    if state == STATE_WAITING_SCREENSHOT:
        if not update.message.photo:
            await update.message.reply_text(
                "📸 Please send a *screenshot* (photo) of your sent WhatsApp message.",
                parse_mode="Markdown",
                reply_markup=CANCEL_KB,
            )
            return

        task_id = user.get("current_task_id")
        if not task_id:
            set_state(uid, None)
            await update.message.reply_text("⚠️ No active task found.", reply_markup=MAIN_MENU)
            return

        task = get_task(task_id)
        if not task or task["status"] != "waiting_screenshot":
            set_state(uid, None)
            set_current_task(uid, None)
            await update.message.reply_text("⚠️ Task not found or already processed.", reply_markup=MAIN_MENU)
            return

        file_id  = update.message.photo[-1].file_id
        selected = json.loads(task["selected"])

        conn = get_db()
        conn.execute(
            "UPDATE tasks SET status='pending_review', screenshot_file_id=? WHERE id=?",
            (file_id, task_id),
        )
        conn.commit()
        conn.close()
        set_state(uid, None)

        await update.message.reply_text(
            f"✅ Screenshot received!\n"
            f"📋 Task #{task_id} is under review — you'll be notified once approved.",
            reply_markup=MAIN_MENU,
        )

        # Forward screenshot to admin with approve/reject buttons
        all_numbers  = json.loads(task["numbers"])
        claimed_nums = [all_numbers[i - 1] for i in selected if 0 < i <= len(all_numbers)]
        num_lines    = "\n".join(f"  {i}. `+{n}`" for i, n in enumerate(claimed_nums, 1))
        earn_amt     = len(selected) * EARN_PER_NUMBER / 100

        caption = (
            f"📋 *New Task Review — \#{task_id}*\n\n"
            f"👤 {_md(user['first_name'])} \(`{uid}`\)\n"
            f"🔖 Type: {task['task_type']}\n\n"
            f"📞 *Numbers claimed \({len(selected)}/{NUMBERS_PER_TASK}\):*\n{num_lines}\n\n"
            f"💰 If approved: {fmt(len(selected) * EARN_PER_NUMBER)}"
        )
        try:
            await ctx.bot.send_photo(
                ADMIN_ID,
                photo=file_id,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=build_admin_keyboard(task_id),
            )
        except Exception as e:
            logger.error(f"Failed to send screenshot to admin: {e}")
            plain_nums = "\n".join(f"  {i}. +{n}" for i, n in enumerate(claimed_nums, 1))
            await ctx.bot.send_photo(
                ADMIN_ID,
                photo=file_id,
                caption=(
                    f"📋 New Task Review — #{task_id}\n\n"
                    f"👤 {user['first_name']} ({uid})\n"
                    f"🔖 Type: {task['task_type']}\n\n"
                    f"📞 Numbers claimed ({len(selected)}/{NUMBERS_PER_TASK}):\n{plain_nums}\n\n"
                    f"💰 If approved: {fmt(len(selected) * EARN_PER_NUMBER)}"
                ),
                reply_markup=build_admin_keyboard(task_id),
            )
        return

    if state == STATE_WAITING_UPI:
        upi = update.message.text.strip() if update.message.text else ""
        if not upi or " " in upi or "@" not in upi:
            await update.message.reply_text(
                "❌ Invalid UPI ID. Please enter a valid UPI ID (e.g. `name@upi`):",
                parse_mode="Markdown",
                reply_markup=CANCEL_KB,
            )
            return

        set_state(uid, None, upi=upi)
        amount = user["balance"]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data="wd_confirm_upi"),
            InlineKeyboardButton("✏️ Change UPI", callback_data="wd_change_upi"),
        ]])
        await update.message.reply_text(
            f"💰 *Withdrawal Confirmation*\n\n"
            f"💰 Amount: *{fmt(amount)}*\n"
            f"🏦 Method: UPI\n"
            f"🆔 UPI ID: `{upi}`\n\n"
            f"Confirm?",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    if state == STATE_WAITING_UPI_SETUP:
        upi = update.message.text.strip() if update.message.text else ""
        if not upi or " " in upi or "@" not in upi:
            await update.message.reply_text(
                "❌ Invalid UPI ID. Please enter a valid UPI ID (e.g. `name@upi`):",
                parse_mode="Markdown",
                reply_markup=CANCEL_KB,
            )
            return

        conn = get_db()
        conn.execute("UPDATE users SET upi_id=? WHERE telegram_id=?", (upi, uid))
        conn.commit()
        conn.close()
        set_state(uid, STATE_WAITING_UPI_QR)

        skip_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏭ Skip QR", callback_data="upi_skip_qr"),
        ]])
        await update.message.reply_text(
            f"✅ UPI ID saved: `{upi}`\n\n"
            f"📷 Now send a photo of your *UPI QR code*, or tap Skip:",
            parse_mode="Markdown",
            reply_markup=skip_kb,
        )
        return

    if state == STATE_WAITING_CRYPTO:
        addr = update.message.text.strip() if update.message.text else ""
        # Accept standard BEP20/EVM address (0x + 40 hex chars) OR Binance Pay/UID (numeric)
        import re as _re
        is_bep20   = bool(_re.match(r"^0x[0-9a-fA-F]{40}$", addr))
        is_numeric = addr.isdigit() and len(addr) >= 6
        if not (is_bep20 or is_numeric):
            await update.message.reply_text(
                "❌ Invalid wallet address.\n\n"
                "Please enter a valid:\n"
                "• *BEP20 address* — starts with `0x`, 42 characters\n"
                "  e.g. `0xAbC123...`\n"
                "• *Binance UID* — numeric ID from your Binance app",
                parse_mode="Markdown",
                reply_markup=CANCEL_KB,
            )
            return

        set_state(uid, None, crypto=addr)
        amount   = user["balance"]
        usdt_amt = to_usdt(amount)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data="wd_confirm_crypto"),
            InlineKeyboardButton("✏️ Change Wallet", callback_data="wd_change_crypto"),
        ]])
        await update.message.reply_text(
            f"💰 *Withdrawal Confirmation*\n\n"
            f"💰 Amount: *{fmt(amount)}*\n"
            f"💎 Method: Crypto (BEP20/USDT)\n"
            f"📬 Wallet: `{addr}`\n"
            f"≈ ${usdt_amt:.3f} USDT\n\n"
            f"⚠️ Ensure this is a *BEP20 / Binance Smart Chain* address.\n\n"
            f"Confirm?",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    if state == STATE_WAITING_REVERIFY_NUMBER:
        raw = update.message.text.strip() if update.message.text else ""
        import re as _re
        parts = _re.split(r"[\n,]+", raw)
        valid_numbers = []
        for p in parts:
            n = p.strip().lstrip("+").replace(" ", "").replace("-", "")
            if n.isdigit() and len(n) >= 7:
                valid_numbers.append(n)

        if not valid_numbers:
            await update.message.reply_text(
                "❌ No valid numbers found. Please send the numbers one per line or comma-separated.",
                parse_mode="Markdown",
                reply_markup=CANCEL_KB,
            )
            return

        numbers_str = ",".join(valid_numbers)
        conn = get_db()
        conn.execute("UPDATE users SET reverify_number=? WHERE telegram_id=?", (numbers_str, uid))
        conn.commit()
        conn.close()
        set_state(uid, STATE_WAITING_REVERIFY_SCREENSHOT)

        num_list = "\n".join(f"• `+{n}`" for n in valid_numbers)
        earn_est = len(valid_numbers) * EARN_PER_NUMBER
        await update.message.reply_text(
            f"✅ *{len(valid_numbers)} number(s) saved:*\n{num_list}\n\n"
            f"💰 Potential earn if approved: *{fmt(earn_est)}*\n\n"
            f"📸 Now send *one screenshot* showing the WhatsApp messages you sent to these numbers:",
            parse_mode="Markdown",
            reply_markup=CANCEL_KB,
        )
        return

    if state == STATE_WAITING_REVERIFY_SCREENSHOT:
        if not update.message.photo:
            await update.message.reply_text(
                "📸 Please send a *screenshot* (photo) showing the sent WhatsApp message:",
                parse_mode="Markdown",
                reply_markup=CANCEL_KB,
            )
            return

        fresh_user   = get_user(uid)
        numbers_str  = fresh_user.get("reverify_number", "") if fresh_user else ""
        number_list  = [n for n in numbers_str.split(",") if n]
        file_id      = update.message.photo[-1].file_id
        total_earn   = len(number_list) * EARN_PER_NUMBER

        conn = get_db()
        cur = conn.execute(
            "INSERT INTO reverifications (user_id, number, screenshot_file_id) VALUES (?,?,?)",
            (uid, numbers_str, file_id),
        )
        rv_id = cur.lastrowid
        conn.execute("UPDATE users SET reverify_number=NULL WHERE telegram_id=?", (uid,))
        conn.commit()
        conn.close()
        set_state(uid, None)

        await update.message.reply_text(
            f"✅ *Re-verification submitted!*\n\n"
            f"📞 {len(number_list)} number(s) sent for review.\n"
            f"💰 You'll earn *{fmt(total_earn)}* if approved.",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )

        num_lines = "\n".join(f"  {i}. `+{n}`" for i, n in enumerate(number_list, 1))
        rv_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Approve {fmt(total_earn)}", callback_data=f"rvapprove_{rv_id}"),
            InlineKeyboardButton("❌ Reject",                     callback_data=f"rvreject_{rv_id}"),
        ]])
        try:
            await ctx.bot.send_photo(
                ADMIN_ID,
                photo=file_id,
                caption=(
                    f"🔄 *Re-verification Request* #{rv_id}\n\n"
                    f"👤 {update.effective_user.first_name} (`{uid}`)\n"
                    f"📞 *{len(number_list)} number(s):*\n{num_lines}\n\n"
                    f"Approve to credit {fmt(total_earn)} ({len(number_list)} × {fmt(EARN_PER_NUMBER)})"
                ),
                parse_mode="Markdown",
                reply_markup=rv_kb,
            )
        except Exception:
            pass
        return

    if state == STATE_WAITING_UPI_QR:
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            conn = get_db()
            conn.execute("UPDATE users SET upi_qr_file_id=? WHERE telegram_id=?", (file_id, uid))
            conn.commit()
            conn.close()
            set_state(uid, None)
            await update.message.reply_text(
                "✅ QR code saved! Your UPI details are now set.\n\n"
                "They'll be shown in your 👤 Account and used for withdrawals.",
                reply_markup=MAIN_MENU,
            )
        else:
            skip_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Skip QR", callback_data="upi_skip_qr"),
            ]])
            await update.message.reply_text(
                "📷 Please send a *photo* of your UPI QR code, or tap Skip:",
                parse_mode="Markdown",
                reply_markup=skip_kb,
            )
        return

    if state == STATE_WAITING_SUBSCRIBE_SCREENSHOT:
        if not update.message.photo:
            await update.message.reply_text(
                "📸 Please send a *screenshot* showing you are subscribed to the channel.",
                parse_mode="Markdown",
                reply_markup=CANCEL_KB,
            )
            return

        file_id = update.message.photo[-1].file_id

        conn = get_db()
        existing = conn.execute(
            "SELECT id, status FROM subscriptions WHERE user_id=?", (uid,)
        ).fetchone()
        if existing and existing["status"] == "pending":
            conn.execute(
                "UPDATE subscriptions SET screenshot_file_id=?, created_at=CURRENT_TIMESTAMP WHERE user_id=?",
                (file_id, uid),
            )
            sub_id = existing["id"]
        else:
            cur = conn.execute(
                "INSERT OR REPLACE INTO subscriptions (user_id, screenshot_file_id, status) VALUES (?,?,?)",
                (uid, file_id, "pending"),
            )
            sub_id = cur.lastrowid
        conn.commit()
        conn.close()
        set_state(uid, None)

        await update.message.reply_text(
            f"✅ Screenshot received!\n"
            f"⏳ Under review — you'll earn *{fmt(SUBSCRIBE_EARN)}* once approved.",
            parse_mode="Markdown",
            reply_markup=MAIN_MENU,
        )

        sub_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"✅ Approve {fmt(SUBSCRIBE_EARN)}", callback_data=f"subapprove_{sub_id}"),
            InlineKeyboardButton("❌ Reject",                          callback_data=f"subreject_{sub_id}"),
        ]])
        try:
            await ctx.bot.send_photo(
                ADMIN_ID,
                photo=file_id,
                caption=(
                    f"📺 *Subscribe & Earn Request* #{sub_id}\n\n"
                    f"👤 {update.effective_user.first_name} (`{uid}`)\n"
                    f"💰 Approve to credit {fmt(SUBSCRIBE_EARN)}"
                ),
                parse_mode="Markdown",
                reply_markup=sub_kb,
            )
        except Exception:
            pass
        return

    await update.message.reply_text("Please use the menu below. 👇", reply_markup=get_menu(uid))


# ─────────────────────────────────────────────
# COMMAND REGISTRATION
# ─────────────────────────────────────────────
USER_COMMANDS = [
    BotCommand("start",   "🚀 Start / restart the bot"),
    BotCommand("ping",    "🏓 Check if the bot is alive"),
]

ADMIN_COMMANDS = [
    BotCommand("start",         "🚀 Start / restart the bot"),
    BotCommand("ping",          "🏓 Check if the bot is alive"),
    BotCommand("addbal",        "➕ Add balance — /addbal <user> <₹amount>"),
    BotCommand("deductbal",     "➖ Deduct balance — /deductbal <user> <₹amount>"),
    BotCommand("checkbal",      "🔍 Check user balance — /checkbal <user>"),
    BotCommand("setbal",        "✏️ Set user balance — /setbal <user> <₹amount>"),
    BotCommand("broadcast",     "📢 Broadcast message — /broadcast <text>"),
    BotCommand("stats",         "📊 Bot-wide statistics"),
    BotCommand("pending",       "⏳ List pending task reviews"),
    BotCommand("pool",          "🔢 Show number pool status"),
    BotCommand("user",          "👤 Full user details — /user <user>"),
    BotCommand("listusers",     "📋 List all registered users"),
    BotCommand("addnumber",     "➕ Add number(s) to pool — /addnumber <num> [num2 ...]"),
    BotCommand("delnumber",     "🗑️ Remove number(s) from pool — /delnumber <num> [num2 ...]"),
    BotCommand("uploadnumbers", "📎 Bulk-add numbers by uploading a .txt file"),
    BotCommand("listnumbers",   "📋 List all pool numbers with status flags"),
]


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    await application.bot.set_my_commands(
        ADMIN_COMMANDS,
        scope=BotCommandScopeChat(chat_id=ADMIN_ID),
    )
    logger.info("✅ Bot commands registered")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main() -> None:
    global NUMBER_POOL
    init_db()
    NUMBER_POOL = load_numbers()
    print(f"✅ Loaded {len(NUMBER_POOL)} numbers")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("ping",      cmd_ping))
    app.add_handler(CommandHandler("addbal",    cmd_addbal))
    app.add_handler(CommandHandler("deductbal", cmd_deductbal))
    app.add_handler(CommandHandler("checkbal",  cmd_checkbal))
    app.add_handler(CommandHandler("setbal",    cmd_setbal))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats",     cmd_stats))
    app.add_handler(CommandHandler("pending",   cmd_pending))
    app.add_handler(CommandHandler("pool",          cmd_pool))
    app.add_handler(CommandHandler("user",          cmd_user))
    app.add_handler(CommandHandler("listusers",     cmd_listusers))
    app.add_handler(CommandHandler("listnumbers",   cmd_listnumbers))
    app.add_handler(CommandHandler("addnumber",     cmd_addnumber))
    app.add_handler(CommandHandler("delnumber",     cmd_delnumber))
    app.add_handler(MessageHandler(filters.Document.FileExtension("txt") & filters.COMMAND, cmd_uploadnumbers))
    app.add_handler(CommandHandler("uploadnumbers", cmd_uploadnumbers))

    # Menu buttons
    app.add_handler(MessageHandler(filters.Regex(r"^📤 Send Text & Earn$"), handle_send_earn))
    app.add_handler(MessageHandler(filters.Regex(r"^📲 Click & Send$"),     handle_click_send))
    app.add_handler(MessageHandler(filters.Regex(r"^👥 Refer & Earn$"),     handle_refer))
    app.add_handler(MessageHandler(filters.Regex(r"^💰 Withdraw$"),         handle_withdraw))
    app.add_handler(MessageHandler(filters.Regex(r"^👤 Account$"),          handle_account))
    app.add_handler(MessageHandler(filters.Regex(r"^🔄 Re-verify$"),        handle_reverify))
    app.add_handler(MessageHandler(filters.Regex(r"^❌ Cancel$"),           handle_cancel))
    app.add_handler(MessageHandler(filters.Regex(r"^📺 Subscribe & Earn$"), handle_subscribe))

    # Admin-only menu buttons
    app.add_handler(MessageHandler(filters.Regex(r"^➕ Add Number$"),      handle_admin_add_number))
    app.add_handler(MessageHandler(filters.Regex(r"^🗑️ Delete Number$"),  handle_admin_del_number))
    app.add_handler(MessageHandler(filters.Regex(r"^📎 Upload Numbers$"),  handle_admin_upload_numbers))
    app.add_handler(MessageHandler(filters.Regex(r"^🔢 Pool Status$"),     handle_admin_pool_status))

    # Inline callbacks — order matters
    app.add_handler(CallbackQueryHandler(handle_task_callback,              pattern=r"^(sel|tdone|tcancel)_"))
    app.add_handler(CallbackQueryHandler(handle_admin_callback,             pattern=r"^(tapprove|treject)_"))
    app.add_handler(CallbackQueryHandler(handle_reverify_admin_callback,    pattern=r"^(rvapprove|rvreject)_"))
    app.add_handler(CallbackQueryHandler(handle_subscribe_admin_callback,   pattern=r"^(subapprove|subreject)_"))
    app.add_handler(CallbackQueryHandler(handle_upi_setup_callback,  pattern=r"^(upi_setup_start|upi_skip_qr)$"))
    app.add_handler(CallbackQueryHandler(handle_wd_method,          pattern=r"^wd_method_(upi|crypto)$"))
    app.add_handler(CallbackQueryHandler(handle_wd_confirm,         pattern=r"^wd_(confirm_upi|confirm_crypto|change_upi|change_crypto)$"))

    # State-based text/photo handler
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    print("🤖 Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
