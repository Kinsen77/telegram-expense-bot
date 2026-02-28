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
    raise RuntimeError("Missing BOT_TOKEN. Set it in Railway Variables.")

DB_PATH = os.getenv("DB_PATH", "data.db")

TH_TZ = timezone(timedelta(hours=7))

# ตัดยอดวันที่ 5 => วันที่ 6 เริ่มรอบใหม่
CUTOFF_DAY = 6  # start of new cycle

# จับข้อความรูปแบบ:
# - "กาแฟ 50" (ค่าใช้จ่าย)
# - "+ โอนคืน 200" (รายรับ)
# - "- ข้าว 120" (ค่าใช้จ่าย)
TX_PATTERN = re.compile(r"^\s*([+-])?\s*(.*?)\s*([0-9][0-9,]*)\s*$")

RESET_CONFIRM_TEXT = "RESET"
RESET_EXPIRE_SECONDS = 60


# =========================
# Time helpers (Thai time)
# =========================
def now_dt() -> datetime:
    return datetime.now(TH_TZ)

def fmt(n: int) -> str:
    return f"{n:,}"

def cycle_key_from_date(d: date) -> str:
    """
    cycle_key = 'YYYY-MM' โดยนิยามว่า:
    - วันที่ 6..สิ้นเดือน => อยู่ในรอบของเดือนนั้น
    - วันที่ 1..5 => ยังนับเป็นรอบของเดือนก่อนหน้า
    """
    y, m = d.year, d.month
    if d.day >= CUTOFF_DAY:
        return f"{y:04d}-{m:02d}"
    # ต้นเดือน => ย้อนไปเดือนก่อน
    if m == 1:
        return f"{y-1:04d}-12"
    return f"{y:04d}-{m-1:02d}"

def cycle_range_from_key(key: str) -> Tuple[date, date]:
    """
    key=YYYY-MM => รอบ: start = วันที่ 6 ของเดือนนั้น
                   end   = วันที่ 5 ของเดือนถัดไป (inclusive)
    """
    y, m = map(int, key.split("-"))
    start = date(y, m, CUTOFF_DAY)
    if m == 12:
        ny, nm = y + 1, 1
    else:
        ny, nm = y, m + 1
    end = date(ny, nm, CUTOFF_DAY) - timedelta(days=1)  # = วันที่ 5
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
            ts TEXT NOT NULL,          -- ISO datetime (+07:00)
            day_key TEXT NOT NULL,     -- YYYY-MM-DD (ไทย)
            cycle_key TEXT NOT NULL,   -- YYYY-MM (รอบตัดวันที่ 5)
            sign TEXT NOT NULL,        -- '+' income, '-' expense
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
# Reset confirmation state (in-memory)
# =========================
@dataclass
class ResetPending:
    chat_id: int
    user_id: int
    expires_at: datetime

PENDING_RESETS: Dict[Tuple[int, int], ResetPending] = {}  # (chat_id,user_id) -> pending


# =========================
# Texts
# =========================
HELP_TEXT = (
    "📌 วิธีใช้บอทกองกลาง (รวมทั้งกลุ่ม)\n\n"
    "✅ บันทึกรายการ (พิมพ์ในกลุ่ม)\n"
    "• ค่าใช้จ่าย: พิมพ์ “รายการ จำนวน”\n"
    "  ตัวอย่าง: กาแฟ 50\n"
    "• รายรับ: ใส่ + นำหน้า\n"
    "  ตัวอย่าง: + โอนคืน 200\n\n"
    "📊 คำสั่งสรุป\n"
    "• /today  สรุปวันนี้\n"
    "• /month  สรุปรอบเดือนปัจจุบัน (ตัดยอดวันที่ 5 / วันที่ 6 เริ่มรอบใหม่)\n"
    "• /month -1  ย้อน 1 รอบ, /month -2 ย้อน 2 รอบ\n"
    "• /month 2026-02  ดูรอบเดือนที่ระบุ (ตามกติกาตัดยอดวันที่ 5)\n\n"
    "🧹 รีเซ็ตยอดรอบปัจจุบัน\n"
    "• /reset  แล้วพิมพ์ RESET ภายใน 60 วินาทีเพื่อยืนยัน\n"
    "• /cancel ยกเลิกการรีเซ็ต\n"
)

START_TEXT = (
    "สวัสดีครับ 👋 บอทรายรับรายจ่าย (กองกลาง) ออนไลน์แล้ว ✅\n\n"
    "พิมพ์ /help เพื่อดูวิธีใช้งาน"
)

NOT_GROUP_TEXT = "บอทนี้ใช้งานใน “กลุ่ม” เท่านั้นครับ ✅"


# =========================
# Commands
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

def ensure_group(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_group(update):
        await update.message.reply_text(NOT_GROUP_TEXT)
        return

    chat_id = update.effective_chat.id
    now = now_dt()
    day_key = now.date().isoformat()

    with db() as conn:
        income, expense = sum_today(conn, chat_id, day_key)
        items = list_today(conn, chat_id, day_key)

    net = income - expense
    net_str = f"+{fmt(net)}" if net >= 0 else f"-{fmt(abs(net))}"

    lines = [
        f"📅 สรุปรายวัน ({now.strftime('%d/%m/%Y')})",
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
            sign = r["sign"]
            amt = int(r["amount"])
            detail = r["detail"] or "-"
            lines.append(f"{sign} {fmt(amt)} {detail}")

    await update.message.reply_text("\n".join(lines))

def parse_month_arg(arg: Optional[str], current_cycle_key: str) -> str:
    """
    รองรับ:
    - ไม่มี arg => รอบปัจจุบัน
    - arg เป็นเลข เช่น -1, -2, 0 => ย้อนตามจำนวนรอบ
    - arg เป็น YYYY-MM => รอบตามที่ระบุ
    """
    if not arg:
        return current_cycle_key

    a = arg.strip()
    if re.match(r"^-?\d+$", a):
        return shift_cycle_key(current_cycle_key, int(a))

    if re.match(r"^\d{4}-\d{2}$", a):
        return a

    raise ValueError("bad month arg")

async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_group(update):
        await update.message.reply_text(NOT_GROUP_TEXT)
        return

    chat_id = update.effective_chat.id
    now = now_dt()
    current_key = cycle_key_from_date(now.date())

    arg = None
    if context.args:
        arg = context.args[0]

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
    if not ensure_group(update):
        await update.message.reply_text(NOT_GROUP_TEXT)
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    now = now_dt()
    key = cycle_key_from_date(now.date())
    start_d, end_d = cycle_range_from_key(key)

    expires = now + timedelta(seconds=RESET_EXPIRE_SECONDS)
    PENDING_RESETS[(chat_id, user_id)] = ResetPending(chat_id=chat_id, user_id=user_id, expires_at=expires)

    await update.message.reply_text(
        "⚠️ ต้องการรีเซ็ตยอด “เฉพาะรอบปัจจุบัน” ใช่ไหม?\n\n"
        f"รอบนี้คือ ({start_d.strftime('%d/%m/%Y')} - {end_d.strftime('%d/%m/%Y')})\n\n"
        f"เพื่อยืนยัน ให้พิมพ์คำว่า {RESET_CONFIRM_TEXT} ภายใน {RESET_EXPIRE_SECONDS} วินาที\n"
        "ยกเลิกได้ด้วย /cancel"
    )

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_group(update):
        await update.message.reply_text(NOT_GROUP_TEXT)
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0

    if (chat_id, user_id) in PENDING_RESETS:
        PENDING_RESETS.pop((chat_id, user_id), None)
        await update.message.reply_text("ยกเลิกการรีเซ็ตแล้ว ✅")
    else:
        await update.message.reply_text("ไม่มีรายการรีเซ็ตที่รอการยืนยัน")

async def confirm_reset_if_needed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    ถ้าข้อความคือ RESET และมี pending => ทำการ reset
    คืนค่า True ถ้าจัดการไปแล้ว
    """
    if not ensure_group(update):
        return False

    if not update.message or not update.message.text:
        return False

    text = update.message.text.strip()
    if text != RESET_CONFIRM_TEXT:
        return False

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    key = (chat_id, user_id)

    pending = PENDING_RESETS.get(key)
    if not pending:
        return False

    now = now_dt()
    if now > pending.expires_at:
        PENDING_RESETS.pop(key, None)
        await update.message.reply_text("หมดเวลายืนยันแล้ว ⏳ พิมพ์ /reset ใหม่อีกครั้ง")
        return True

    # ทำ reset: บันทึก reset_ts ลง DB
    cycle_key = cycle_key_from_date(now.date())
    with db() as conn:
        conn.execute(
            "INSERT INTO resets (chat_id, cycle_key, reset_ts) VALUES (?, ?, ?)",
            (chat_id, cycle_key, now.isoformat()),
        )
        conn.commit()

    PENDING_RESETS.pop(key, None)

    start_d, end_d = cycle_range_from_key(cycle_key)
    await update.message.reply_text(
        "รีเซ็ตยอดเรียบร้อย ✅\n"
        f"รอบปัจจุบัน ({start_d.strftime('%d/%m/%Y')} - {end_d.strftime('%d/%m/%Y')}) ถูกนับใหม่จาก 0 แล้ว"
    )
    return True


# =========================
# Message handler: record transactions
# =========================
async def record_tx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_group(update):
        return
    if not update.message or not update.message.text:
        return

    # 1) ถ้าเป็นการยืนยัน reset ให้จัดการก่อน
    handled = await confirm_reset_if_needed(update, context)
    if handled:
        return

    text = update.message.text.strip()

    # ไม่ยุ่งกับคำสั่ง
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

    # ตีความ sign:
    # - ไม่มี sign => expense
    # - '-' => expense
    # - '+' => income
    sign = "+" if sign_raw == "+" else "-"

    if not detail:
        detail = "-"

    t = now_dt()
    chat_id = update.effective_chat.id
    day_key = t.date().isoformat()
    cycle_key = cycle_key_from_date(t.date())

    user_id = update.effective_user.id if update.effective_user else None
    user_name = update.effective_user.full_name if update.effective_user else None

    with db() as conn:
        conn.execute(
            """
            INSERT INTO transactions (chat_id, ts, day_key, cycle_key, sign, amount, detail, user_id, user_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, t.isoformat(), day_key, cycle_key, sign, amount, detail, user_id, user_name),
        )
        conn.commit()

        # สรุปยอดรอบเดือนปัจจุบัน (หลัง reset ถ้ามี)
        last_reset = get_last_reset_ts(conn, chat_id, cycle_key)
        income, expense = sum_cycle(conn, chat_id, cycle_key, last_reset)
        bal = income - expense

    # ตอบสั้นๆ
    if sign == "+":
        await update.message.reply_text(f"บันทึกรายรับแล้ว ✅ (+{fmt(amount)}) | คงเหลือรอบนี้ {fmt(bal)}")
    else:
        await update.message.reply_text(f"บันทึกรายจ่ายแล้ว ✅ (-{fmt(amount)}) | คงเหลือรอบนี้ {fmt(bal)}")


# =========================
# Main
# =========================
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cmd", help_cmd))

    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("month", month_cmd))

    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, record_tx))

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
