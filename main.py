# ===== AUTO CATALOG v2 (STEP1 + PLUS) =====
import os
import re
import asyncio
import logging
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Tuple

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiohttp import web

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = "db.sqlite3"

ANTI_DUP_MINUTES = 30  # антидубликаты

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN required")

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("auto_catalog")

# ---------------- PARSING ----------------
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
PRICE_RE = re.compile(r"\b\d{1,3}(?:[\s.\u00A0]\d{3})+\b|\b\d{6,}\b")

def normalize_text(text: str) -> str:
    return text.replace("\u00A0", " ").strip()

def parse_listing(text: str) -> Tuple[bool, Optional[int], Optional[int]]:
    year = None
    price = None

    y = YEAR_RE.search(text)
    if y:
        year = int(y.group(1))

    p = PRICE_RE.search(text)
    if p:
        price = int(re.sub(r"\D", "", p.group(0)))

    return (year is not None and price is not None), year, price

def format_price(price: Optional[int]) -> str:
    if not price:
        return "—"
    return f"{price:,}".replace(",", " ")

def text_hash(text: str) -> str:
    return hashlib.md5(text.lower().encode()).hexdigest()

# ---------------- DB ----------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    msg_id INTEGER,
    text TEXT,
    text_hash TEXT,
    year INTEGER,
    price INTEGER,
    sold INTEGER DEFAULT 0,
    created_at TEXT
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # базовая таблица (если БД новая)
        await db.execute(CREATE_SQL)

        # миграция: если таблица старая — добавляем недостающие колонки
        cur = await db.execute("PRAGMA table_info(listings)")
        cols = {row[1] for row in await cur.fetchall()}

        if "text_hash" not in cols:
            await db.execute("ALTER TABLE listings ADD COLUMN text_hash TEXT")
        if "sold" not in cols:
            await db.execute("ALTER TABLE listings ADD COLUMN sold INTEGER DEFAULT 0")
        if "created_at" not in cols:
            await db.execute("ALTER TABLE listings ADD COLUMN created_at TEXT")

        await db.commit()

        # (не обязательно, но полезно) заполнить text_hash для старых записей
        cur2 = await db.execute("SELECT id, text FROM listings WHERE text_hash IS NULL OR text_hash=''")
        rows = await cur2.fetchall()
        for lid, text in rows:
            th = text_hash(text or "")
            await db.execute("UPDATE listings SET text_hash=? WHERE id=?", (th, lid))
        await db.commit()


async def is_duplicate(th: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM listings WHERE text_hash=? LIMIT 1",
            (th,)
        )
        return await cur.fetchone() is not None


async def add_listing(chat_id, msg_id, text, year, price):
    th = text_hash(text)
    if await is_duplicate(th):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO listings (chat_id,msg_id,text,text_hash,year,price,sold,created_at) "
            "VALUES (?,?,?,?,?,?,0,?)",
            (chat_id, msg_id, text, th, year, price, datetime.utcnow().isoformat())
        )
        await db.commit()

async def search_db(q: str, limit=5):
    q = q.lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,chat_id,msg_id,text,year,price,sold FROM listings "
            "WHERE sold=0 AND LOWER(text) LIKE ? "
            "ORDER BY id DESC LIMIT ?",
            (f"%{q}%", limit)
        )
        return await cur.fetchall()

async def last_db(limit=5):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,chat_id,msg_id,text,year,price,sold FROM listings "
            "WHERE sold=0 ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        return await cur.fetchall()

async def mark_sold(lid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE listings SET sold=1 WHERE id=?", (lid,))
        await db.commit()

# ---------------- BOT ----------------
dp = Dispatcher()

async def send_results(message: Message, rows, title: Optional[str] = None):
    if not rows:
        await message.answer("Ничего не нашёл 😕")
        return

    if title:
        await message.answer(title)

    for lid, chat_id, msg_id, text, year, price, sold in rows:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📍 Открыть объявление",
                        url=f"https://t.me/c/{str(chat_id).replace('-100','')}/{msg_id}"
                    ),
                    InlineKeyboardButton(
                        text="✅ Продано",
                        callback_data=f"sold:{lid}"
                    )
                ]
            ]
        )

        await message.answer(
            f"🟢 | {year} | {format_price(price)}\n{text}",
            reply_markup=kb
        )

@dp.message(Command("start"))
async def start_cmd(message: Message):
    if message.chat.type != "private":
        return
    await message.answer(
        "AUTO CATALOG ✅\n\n"
        "Просто напиши запрос:\n"
        "bmw / camry / 2019 / 2350000\n\n"
        "/last — последние объявления"
    )

@dp.message(Command("last"))
async def last_cmd(message: Message):
    if message.chat.type != "private":
        return
    rows = await last_db()
    await send_results(message, rows, "Последние объявления:")

@dp.message(F.text)
async def smart_search(message: Message):
    if message.chat.type != "private":
        return
    q = message.text.strip()
    if not q or q.startswith("/"):
        return
    rows = await search_db(q)
    await send_results(message, rows, f"Поиск: {q}")

@dp.callback_query(F.data.startswith("sold:"))
async def sold_cb(call: CallbackQuery):
    lid = int(call.data.split(":")[1])
    await mark_sold(lid)
    await call.answer("Пометил как SOLD ✅")
    await call.message.edit_reply_markup()

@dp.message(F.text | F.caption)
async def catch_group(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return

    text = normalize_text(message.text or message.caption or "")
    ok, year, price = parse_listing(text)
    if not ok:
        return

    await add_listing(message.chat.id, message.message_id, text, year, price)

# ---------------- HEALTH ----------------
async def health_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.json_response({"ok": True}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

async def main():
    await init_db()
    asyncio.create_task(health_server())

    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
