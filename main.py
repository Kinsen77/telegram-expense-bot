import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing BOT_TOKEN. Set it in Railway Variables.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "สวัสดีครับ 👋\n"
        "บอทออนไลน์แล้ว ✅\n\n"
        "พิมพ์ /start เพื่อทดสอบ"
    )


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
