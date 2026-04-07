import os
import re
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Tuple, Dict

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# Config
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("expensebot")

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN. Set it in Render Environment Variables.")

DB_PATH = os.getenv("DB_PATH", "data.db")

TH_TZ = timezone(timedelta(hours=7))
CUTOFF_DAY = 6  # วันที่ 6 เริ่มรอบใหม่
TX_PATTERN = re.compile(r"^\s*([+-])?\s*(.*?)\s*([0-9][0-9,]*)\s*$")
RESET_CONFIRM_TEXT = "RESET"
RESET_EXPIRE_SECONDS = 60

# =========================
# Time helpers
# =========================
def now_dt() -> datetime:
    return datetime.now(TH_TZ)

def fmt(n: int) -> str:
    return f"{n:,}"

def cycle_key_from_date(d: date) -> str:
    y, m = d.year, d.month
    if d.day >= CUTOFF_DAY:
        return f"{y:04d}-{m:02d}"
    if m == 1:
        return f"{y-1:04d}-12"
    return f"{y:04d}-{m-1:02d}"

def cycle_range_from_key(key: str) -> Tuple[date, date]:
    y, m = map(int, key.split("-"))
    start = date(y, m, CUTOFF_DAY)
    if m == 12:
        ny, nm = y + 1, 1
    else:
        ny, nm = y, m + 1
    end = date(ny, nm, CUTOFF_DAY) - timedelta(days=1)
    return start, end

def shift_cycle_key(key: str, offset_months: int) -> str:
    y, m = map(int, key.split("-"))
    total = (y * 12 + (m - 1)) + offset_months
    ny = total // 12
    nm = (total % 12) + 1
    return f"{ny:04d}-{nm:02d}"

# =========================
# DB
# =========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            day_key TEXT NOT NULL,
            cycle_key TEXT NOT NULL,
            sign TEXT NOT NULL,
            amount INTEGER NOT NULL,
            detail TEXT NOT NULL,
            user_id INTEGER,
            user_name TEXT
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_chat_day ON transactions(chat_id, day_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tx_chat_cycle ON transactions(chat_id, cycle_key)")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            cycle_key TEXT NOT NULL,
            reset_ts TEXT NOT NULL
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reset_chat_cycle ON resets(chat_id, cycle_key)")
        conn.commit()

def get_last_reset_ts(conn: sqlite3.Connection, chat_id: int, cycle_key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT reset_ts FROM resets WHERE chat_id=? AND cycle_key=? ORDER BY id DESC LIMIT 1",
        (chat_id, cycle_key),
    ).fetchone()
    return row["reset_ts"] if row else None

def sum_cycle(conn: sqlite3.Connection, chat_id: int, cycle_key: str, after_ts: Optional[str]) -> Tuple[int, int]:
    q = """
    SELECT
      COALESCE(SUM(CASE WHEN sign='+' THEN amount ELSE 0 END), 0) AS income,
      COALESCE(SUM(CASE WHEN sign='-' THEN amount ELSE 0 END), 0) AS expense
    FROM transactions
    WHERE chat_id=? AND cycle_key=?
    """
    params = [chat_id, cycle_key]
    if after_ts:
        q += " AND ts > ?"
        params.append(after_ts)
    row = conn.execute(q, params).fetchone()
    return int(row["income"]), int(row["expense"])

def list_today(conn: sqlite3.Connection, chat_id: int, day_key: str):
    return conn.execute(
        """
        SELECT sign, amount, detail
        FROM transactions
        WHERE chat_id=? AND day_key=?
        ORDER BY id ASC
        """,
        (chat_id, day_key),
    ).fetchall()

def sum_today(conn: sqlite3.Connection, chat_id: int, day_key: str) -> Tuple[int, int]:
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN sign='+' THEN amount ELSE 0 END), 0) AS income,
          COALESCE(SUM(CASE WHEN sign='-' THEN amount ELSE 0 END), 0) AS expense
        FROM transactions
        WHERE chat_id=? AND day_key=?
        """,
        (chat_id, day_key),
    ).fetchone()
    return int(row["income"]), int(row["expense"])

# =========================
# Reset state
# =========================
@dataclass
class ResetPending:
    chat_id: int
    user_id: int
    expires_at: datetime

PENDING_RESETS: Dict[Tuple[int, int], ResetPending] = {}

# =========================
# Texts
# =========================
HELP_TEXT = (
    "📌 วิธีใช้บอทกองกลาง\n\n"
    "✅ บันทึกรายการในกลุ่ม\n"
    "• ค่าใช้จ่าย: รายการ จำนวน\n"
    "  ตัวอย่าง: ข้าว 50\n"
    "• รายรับ: ใส่ + นำหน้า\n"
    "  ตัวอย่าง: + โอนคืน 200\n\n"
    "📊 คำสั่ง\n"
    "• /today = สรุปวันนี้\n"
    "• /month = สรุปรอบปัจจุบัน\n"
    "• /month -1 = ย้อน 1 รอบ\n"
    "• /month 2026-02 = ดูรอบที่ระบุ\n"
    "• /reset = รีเซ็ตรอบปัจจุบัน\n"
    "• /cancel = ยกเลิกการรีเซ็ต\n"
)

START_TEXT = (
    "สวัสดีครับ 👋\n"
    "บอทรายรับรายจ่ายออนไลน์แล้ว ✅\n\n"
    "ใช้ /help เพื่อดูวิธีใช้งาน"
)

NOT_GROUP_TEXT = "คำสั่งนี้ใช้ในกลุ่มเท่านั้นครับ ✅"

# =========================
# Helpers
# =========================
def is_group(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))

def parse_month_arg(arg: Optional[str], current_cycle_key: str) -> str:
    if not arg:
        return current_cycle_key
    a = arg.strip()
    if re.match(r"^-?\d+$", a):
        return shift_cycle_key(current_cycle_key, int(a))
    if re.match(r"^\d{4}-\d{2}$", a):
        return a
    raise ValueError("bad month arg")

# =========================
# Commands
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Received /start from chat_id=%s type=%s", update.effective_chat.id if update.effective_chat else None, update.effective_chat.type if update.effective_chat else None)
    await update.message.reply_text(START_TEXT)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.message.reply_text(NOT_GROUP_TEXT)
        return

    chat_id = update.effective_chat.id
    day_key = now_dt().date().isoformat()

    with db() as conn:
        income, expense = sum_today(conn, chat_id, day_key)
        items = list_today(conn, chat_id, day_key)

    net = income - expense
    net_str = f"+{fmt(net)}" if net >= 0 else f"-{fmt(abs(net))}"

    lines = [
        f"📅 สรุปรายวัน ({now_dt().strftime('%d/%m/%Y')})",
        "",
        f"รายรับ: {fmt(income)} บาท",
        f"รายจ่าย: {fmt(expense)} บาท",
        f"คงเหลือสุทธิวันนี้: {net_str} บาท",
        "",
        "🧾 รายการวันนี้:",
    ]

    if not items:
        lines.append("- ยังไม่มีรายการวันนี้")
    else:
        for r in items:
            lines.append(f"{r['sign']} {fmt(int(r['amount']))} {r['detail']}")

    await update.message.reply_text("\n".join(lines))

async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.message.reply_text(NOT_GROUP_TEXT)
        return

    chat_id = update.effective_chat.id
    current_key = cycle_key_from_date(now_dt().date())
    arg = context.args[0] if context.args else None

    try:
        key = parse_month_arg(arg, current_key)
    except ValueError:
        await update.message.reply_text("ใช้แบบนี้: /month หรือ /month -1 หรือ /month 2026-02")
        return

    start_d, end_d = cycle_range_from_key(key)

    with db() as conn:
        last_reset = get_last_reset_ts(conn, chat_id, key)
        income, expense = sum_cycle(conn, chat_id, key, last_reset)

    bal = income - expense
    await update.message.reply_text(
        f"📆 สรุปรอบเดือน {key}\n"
        f"({start_d.strftime('%d/%m/%Y')} - {end_d.strftime('%d/%m/%Y')})\n\n"
        f"รายรับ: {fmt(income)} บาท\n"
        f"รายจ่าย: {fmt(expense)} บาท\n"
        f"คงเหลือสุทธิรอบนี้: {fmt(bal)} บาท"
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.message.reply_text(NOT_GROUP_TEXT)
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    cycle_key = cycle_key_from_date(now_dt().date())
    start_d, end_d = cycle_range_from_key(cycle_key)

    PENDING_RESETS[(chat_id, user_id)] = ResetPending(
        chat_id=chat_id,
        user_id=user_id,
        expires_at=now_dt() + timedelta(seconds=RESET_EXPIRE_SECONDS),
    )

    await update.message.reply_text(
        "⚠️ ต้องการรีเซ็ตยอดรอบปัจจุบันใช่ไหม?\n\n"
        f"รอบนี้คือ ({start_d.strftime('%d/%m/%Y')} - {end_d.strftime('%d/%m/%Y')})\n\n"
        f"ให้พิมพ์ {RESET_CONFIRM_TEXT} ภายใน {RESET_EXPIRE_SECONDS} วินาที\n"
        "ยกเลิกได้ด้วย /cancel"
    )

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.message.reply_text(NOT_GROUP_TEXT)
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0

    if (chat_id, user_id) in PENDING_RESETS:
        PENDING_RESETS.pop((chat_id, user_id), None)
        await update.message.reply_text("ยกเลิกการรีเซ็ตแล้ว ✅")
    else:
        await update.message.reply_text("ไม่มีรายการรีเซ็ตที่รอการยืนยัน")

async def confirm_reset_if_needed(update: Update) -> bool:
    if not is_group(update):
        return False
    if not update.message or not update.message.text:
        return False

    text = update.message.text.strip()
    if text != RESET_CONFIRM_TEXT:
        return False

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    pending = PENDING_RESETS.get((chat_id, user_id))

    if not pending:
        return False

    if now_dt() > pending.expires_at:
        PENDING_RESETS.pop((chat_id, user_id), None)
        await update.message.reply_text("หมดเวลายืนยันแล้ว ⏳ พิมพ์ /reset ใหม่อีกครั้ง")
        return True

    cycle_key = cycle_key_from_date(now_dt().date())
    with db() as conn:
        conn.execute(
            "INSERT INTO resets (chat_id, cycle_key, reset_ts) VALUES (?, ?, ?)",
            (chat_id, cycle_key, now_dt().isoformat()),
        )
        conn.commit()

    PENDING_RESETS.pop((chat_id, user_id), None)
    start_d, end_d = cycle_range_from_key(cycle_key)

    await update.message.reply_text(
        "รีเซ็ตยอดเรียบร้อย ✅\n"
        f"รอบปัจจุบัน ({start_d.strftime('%d/%m/%Y')} - {end_d.strftime('%d/%m/%Y')}) เริ่มนับใหม่จาก 0 แล้ว"
    )
    return True

# =========================
# Message handler
# =========================
async def record_tx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    if not update.message or not update.message.text:
        return

    if await confirm_reset_if_needed(update):
        return

    text = update.message.text.strip()

    if text.startswith("/"):
        return

    m = TX_PATTERN.match(text)
    if not m:
        return

    sign_raw = (m.group(1) or "").strip()
    detail = (m.group(2) or "").strip()
    amt_s = m.group(3).replace(",", "")

    try:
        amount = int(amt_s)
    except ValueError:
        return

    sign = "+" if sign_raw == "+" else "-"
    if not detail:
        detail = "-"

    t = now_dt()
    chat_id = update.effective_chat.id
    day_key = t.date().isoformat()
    cycle_key = cycle_key_from_date(t.date())
    user_id = update.effective_user.id if update.effective_user else None
    user_name = update.effective_user.full_name if update.effective_user else None

    logger.info("Record tx chat_id=%s sign=%s amount=%s detail=%s", chat_id, sign, amount, detail)

    with db() as conn:
        conn.execute(
            """
            INSERT INTO transactions (chat_id, ts, day_key, cycle_key, sign, amount, detail, user_id, user_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, t.isoformat(), day_key, cycle_key, sign, amount, detail, user_id, user_name),
        )
        conn.commit()

        last_reset = get_last_reset_ts(conn, chat_id, cycle_key)
        income, expense = sum_cycle(conn, chat_id, cycle_key, last_reset)

    bal = income - expense

    if sign == "+":
        await update.message.reply_text(f"บันทึกรายรับแล้ว ✅ (+{fmt(amount)}) | คงเหลือรอบนี้ {fmt(bal)}")
    else:
        await update.message.reply_text(f"บันทึกรายจ่ายแล้ว ✅ (-{fmt(amount)}) | คงเหลือรอบนี้ {fmt(bal)}")

# =========================
# Error handler
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Exception while handling an update:", exc_info=context.error)

# =========================
# Main
# =========================
def main():
    init_db()
    logger.info("BOT STARTING...")
    logger.info("TOKEN loaded: %s", "YES" if TOKEN else "NO")
    logger.info("DB_PATH: %s", DB_PATH)

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cmd", help_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("month", month_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, record_tx))
    app.add_error_handler(error_handler)

    logger.info("BOT STARTED SUCCESSFULLY")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
