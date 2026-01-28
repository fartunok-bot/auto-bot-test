# ===== AUTO CATALOG v2 (STEP 1) - FIXED =====
import os
import re
import asyncio
import logging
from datetime import datetime
from typing import Optional, Tuple, List

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiohttp import web

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MODE = os.getenv("MODE", "CATALOG").strip().upper()
PORT = int(os.getenv("PORT", "10000"))

DB_PATH = "db.sqlite3"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN required")

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("auto_catalog_v2")

# ---------------- PARSING ----------------
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
PRICE_RE = re.compile(r"\b\d{1,3}(?:[ .]\d{3})+\b|\b\d{6,}\b")

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
    return f"{int(price):,}".replace(",", " ")

# ---------------- DB ----------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    msg_id INTEGER,
    text TEXT,
    year INTEGER,
    price INTEGER,
    sold INTEGER DEFAULT 0,
    created_at TEXT
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def add_listing(chat_id: int, msg_id: int, text: str, year: int, price: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # ВАЖНО: тут было неверное количество колонок
        await db.execute(
            "INSERT INTO listings (chat_id,msg_id,text,year,price,sold,created_at) VALUES (?,?,?,?,?,?,?)",
            (chat_id, msg_id, text, year, price, 0, datetime.utcnow().isoformat())
        )
        await db.commit()

async def search_db(q: str, limit: int = 5):
    q = (q or "").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,chat_id,msg_id,text,year,price,sold FROM listings "
            "WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{q}%", limit)
        )
        return await cur.fetchall()

async def last_db(limit: int = 5):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,chat_id,msg_id,text,year,price,sold FROM listings "
            "ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        return await cur.fetchall()

async def mark_sold(listing_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE listings SET sold=1 WHERE id=?", (listing_id,))
        await db.commit()

# ---------------- BOT ----------------
dp = Dispatcher()

async def send_results(message: Message, rows, title: Optional[str] = None):
    if not rows:
        await message.answer("Ничего не нашёл 😕")
        return

    if title:
        await message.answer(title)

    for row in rows:
        lid, chat_id, msg_id, text, year, price, sold = row

        sold_int = int(sold or 0)
        prefix = "✅ SOLD" if sold_int else "🟢"

        kb = None
        if not sold_int:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Продано", callback_data=f"sold:{lid}")]
                ]
            )

        await message.answer(
            f"{prefix} | {year or '—'} | {format_price(price)}\n{text}\n\nsrc: {chat_id}:{msg_id}",
            reply_markup=kb
        )

@dp.message(Command("start"))
async def start(message: Message):
    if message.chat.type != "private":
        return
    await message.answer(
        "✅ AUTO CATALOG v2\n\n"
        "Просто напиши запрос (без команд):\n"
        "bmw / camry / 2015 / 2350000\n\n"
        "Команды:\n"
        "/last — последние 5"
    )

@dp.message(Command("last"))
async def last_cmd(message: Message):
    if message.chat.type != "private":
        return
    rows = await last_db(limit=5)
    await send_results(message, rows, title="Последние объявления:")

@dp.message(Command("search"))
async def search_cmd(message: Message):
    if message.chat.type != "private":
        return
    q = message.text.replace("/search", "").strip()
    rows = await search_db(q, limit=5)
    await send_results(message, rows, title=f"Поиск: {q}")

@dp.message(F.text)
async def smart_search(message: Message):
    # Поиск без команд — только в ЛС
    if message.chat.type != "private":
        return

    q = (message.text or "").strip()
    if not q or q.startswith("/"):
        return

    rows = await search_db(q, limit=5)
    await send_results(message, rows, title=f"Поиск: {q}")

@dp.callback_query(F.data.startswith("sold:"))
async def sold_cb(call: CallbackQuery):
    try:
        lid = int(call.data.split(":")[1])
    except Exception:
        await call.answer("Ошибка", show_alert=True)
        return

    await mark_sold(lid)
    await call.answer("Пометил как SOLD ✅")
    # убираем кнопку
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

@dp.message(F.text | F.caption)
async def catch_group(message: Message):
    # В группе молчим, только сохраняем
    if message.chat.type not in ("group", "supergroup"):
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        return

    ok, year, price = parse_listing(text)
    if not ok:
        return

    try:
        await add_listing(message.chat.id, message.message_id, text, year, price)
    except Exception as e:
        log.exception("DB insert failed: %s", e)
        return

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
