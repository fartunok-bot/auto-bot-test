# ===== AUTO CATALOG v2 (STEP 1) =====
import os
import re
import asyncio
import logging
from datetime import datetime
from typing import Optional, Tuple, List

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
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

async def add_listing(chat_id, msg_id, text, year, price):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO listings VALUES (NULL,?,?,?,?,0,?)",
            (chat_id, msg_id, text, year, price, datetime.utcnow().isoformat())
        )
        await db.commit()

async def search_db(q: str, limit=5):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,text,year,price,sold FROM listings "
            "WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{q}%", limit)
        )
        return await cur.fetchall()

async def mark_sold(listing_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE listings SET sold=1 WHERE id=?",
            (listing_id,)
        )
        await db.commit()

# ---------------- BOT ----------------
dp = Dispatcher()

@dp.message(Command("start"))
async def start(message: Message):
    if message.chat.type != "private":
        return
    await message.answer(
        "✅ AUTO CATALOG v2\n\n"
        "Просто напиши запрос:\n"
        "BMW / 2015 / 2 100 000\n\n"
        "Команды:\n"
        "/last — последние объявления"
    )

@dp.message(Command("last"))
async def last_cmd(message: Message):
    rows = await search_db("", limit=5)
    await send_results(message, rows)

@dp.message(Command("search"))
async def search_cmd(message: Message):
    q = message.text.replace("/search", "").strip()
    rows = await search_db(q)
    await send_results(message, rows)

@dp.message(F.text)
async def smart_search(message: Message):
    if message.chat.type != "private":
        return
    rows = await search_db(message.text.strip())
    if rows:
        await send_results(message, rows)

async def send_results(message: Message, rows):
    if not rows:
        await message.answer("Ничего не нашёл 😕")
        return

    for row in rows:
        lid, text, year, price, sold = row

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Продано" if not sold else "☑️ Уже SOLD",
                        callback_data=f"sold:{lid}"
                    )
                ]
            ]
        )

        price_str = f"{int(price):,}".replace(",", " ") if price else "—"
        year_str = str(year) if year else "—"

        await message.answer(
            f"{'✅ SOLD' if sold else '🟢'} | {year_str} | {price_str}\n{text}",
            reply_markup=kb
        )

        await message.answer(
            f"{'✅ SOLD' if sold else '🟢'} | {year} | {price:,}\n{text}",
            reply_markup=kb
        )

@dp.callback_query(F.data.startswith("sold:"))
async def sold_cb(call: CallbackQuery):
    lid = int(call.data.split(":")[1])
    await mark_sold(lid)
    await call.answer("Пометил как SOLD")
    await call.message.edit_reply_markup()

@dp.message(F.text | F.caption)
async def catch_group(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return

    text = message.text or message.caption or ""
    ok, year, price = parse_listing(text)
    if not ok:
        return

    await add_listing(
        message.chat.id,
        message.message_id,
        text,
        year,
        price
    )

# ---------------- HEALTH ----------------
async def health_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.json_response({"ok": True}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.include_router(router)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
