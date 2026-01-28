import os
import re
import asyncio
import logging
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiohttp import web


# -------------------- CONFIG --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MODE = os.getenv("MODE", "CATALOG").strip().upper()  # CATALOG | POSTER
TARGET_CHAT_ID_RAW = os.getenv("TARGET_CHAT_ID", "").strip()
PORT = int(os.getenv("PORT", "10000"))

DB_PATH = "db.sqlite3"

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is required")

TARGET_CHAT_ID = None
if MODE == "POSTER":
    if not TARGET_CHAT_ID_RAW:
        raise RuntimeError("MODE=POSTER requires ENV TARGET_CHAT_ID")
    try:
        TARGET_CHAT_ID = int(TARGET_CHAT_ID_RAW)
    except ValueError:
        raise RuntimeError("TARGET_CHAT_ID must be integer (e.g. -1001234567890)")


# -------------------- LOGGING --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("auto_catalog")


# -------------------- PARSING --------------------
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
# Цена: 2100000 или 2 100 000 или 2.100.000
PRICE_RE = re.compile(r"\b\d{1,3}(?:[ .]\d{3})+\b|\b\d{6,}\b")

def normalize_price(s: str) -> int | None:
    # вытащим первую подходящую "цену" и приведем к int
    m = PRICE_RE.search(s)
    if not m:
        return None
    raw = m.group(0)
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    # отсечём слишком маленькие/странные
    try:
        val = int(digits)
        if val < 50000:  # меньше 50к почти точно не цена авто
            return None
        return val
    except ValueError:
        return None

def extract_year(s: str) -> int | None:
    m = YEAR_RE.search(s)
    if not m:
        return None
    y = int(m.group(1))
    if 1970 <= y <= datetime.now().year + 1:
        return y
    return None

def looks_like_listing(text: str) -> tuple[bool, int | None, int | None]:
    """
    Возвращает (is_listing, year, price)
    Объявление = есть ГОД и ЦЕНА.
    """
    year = extract_year(text)
    price = normalize_price(text)
    return (year is not None and price is not None), year, price


# -------------------- DB --------------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_chat_id INTEGER NOT NULL,
    src_message_id INTEGER NOT NULL,
    author_id INTEGER,
    text TEXT NOT NULL,
    year INTEGER,
    price INTEGER,
    created_at TEXT NOT NULL,
    sold INTEGER NOT NULL DEFAULT 0,

    posted INTEGER NOT NULL DEFAULT 0,
    target_chat_id INTEGER,
    target_message_id INTEGER,

    UNIQUE(src_chat_id, src_message_id)
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def save_listing(src_chat_id: int, src_message_id: int, author_id: int | None,
                       text: str, year: int | None, price: int | None) -> bool:
    """
    True если вставили новое, False если уже было.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                """
                INSERT INTO listings (src_chat_id, src_message_id, author_id, text, year, price, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (src_chat_id, src_message_id, author_id, text, year, price, datetime.utcnow().isoformat()),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def mark_sold(src_chat_id: int, src_message_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE listings SET sold=1 WHERE src_chat_id=? AND src_message_id=?",
            (src_chat_id, src_message_id),
        )
        await db.commit()
        return cur.rowcount > 0

async def set_posted(src_chat_id: int, src_message_id: int, target_chat_id: int, target_message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE listings
            SET posted=1, target_chat_id=?, target_message_id=?
            WHERE src_chat_id=? AND src_message_id=?
            """,
            (target_chat_id, target_message_id, src_chat_id, src_message_id),
        )
        await db.commit()

async def search_listings(query: str, limit: int = 5):
    q = f"%{query.strip()}%"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT src_chat_id, src_message_id, text, year, price, sold, created_at
            FROM listings
            WHERE text LIKE ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (q, limit),
        )
        return await cur.fetchall()


# -------------------- BOT --------------------
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "✅ AUTO CATALOG запущен.\n\n"
        "Команды:\n"
        "/search <запрос> — последние 5 совпадений\n"
        "/sold — ответом на объявление пометить как продано\n\n"
        f"MODE: {MODE}"
    )

@dp.message(Command("search"))
async def cmd_search(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Напиши так: /search BMW")
        return

    query = parts[1].strip()
    rows = await search_listings(query, limit=5)

    if not rows:
        await message.answer("Ничего не нашёл 😕")
        return

    lines = []
    for (chat_id, msg_id, text, year, price, sold, created_at) in rows:
        sold_mark = "✅ SOLD" if sold else "🟢"
        price_str = f"{price:,}".replace(",", " ") if price else "—"
        year_str = str(year) if year else "—"
        snippet = text.strip().replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:160] + "…"
        lines.append(
            f"{sold_mark} | {year_str} | {price_str}\n"
            f"{snippet}\n"
            f"(src: {chat_id}, msg: {msg_id})"
        )

    await message.answer("\n\n".join(lines))

@dp.message(Command("sold"))
async def cmd_sold(message: Message):
    if not message.reply_to_message:
        await message.answer("Нужно ответить на объявление и написать /sold")
        return

    src_chat_id = message.chat.id
    src_message_id = message.reply_to_message.message_id

    ok = await mark_sold(src_chat_id, src_message_id)
    await message.answer("✅ Пометил как SOLD" if ok else "Не нашёл это объявление в базе 😕")

@dp.message(F.text)
async def catch_listings(message: Message, bot: Bot):
    # Сохраняем только в группах/супергруппах (не обязательно, но логично)
    if message.chat.type not in ("group", "supergroup"):
        return

    text = message.text or ""
    is_listing, year, price = looks_like_listing(text)
    if not is_listing:
        return

    inserted = await save_listing(
        src_chat_id=message.chat.id,
        src_message_id=message.message_id,
        author_id=message.from_user.id if message.from_user else None,
        text=text,
        year=year,
        price=price,
    )

    if not inserted:
        return  # уже было

    log.info(f"Saved listing: chat={message.chat.id} msg={message.message_id} year={year} price={price}")

    # POSTER: копируем сообщение в канал/чат каталога
    if MODE == "POSTER" and TARGET_CHAT_ID is not None:
        try:
            copied = await bot.copy_message(
                chat_id=TARGET_CHAT_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            await set_posted(message.chat.id, message.message_id, TARGET_CHAT_ID, copied.message_id)
            log.info(f"Posted to target: target_chat={TARGET_CHAT_ID} target_msg={copied.message_id}")
        except Exception as e:
            log.exception(f"Failed to post to TARGET_CHAT_ID: {e}")


# -------------------- HEALTH SERVER (Render-friendly) --------------------
async def health_server():
    app = web.Application()

    async def health(_):
        return web.json_response({"ok": True, "mode": MODE})

    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Health server on 0.0.0.0:{PORT}")


async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    await health_server()  # чтобы Render Web Service не ругался

    log.info("Bot starting polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

