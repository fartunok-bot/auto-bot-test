# ===== AUTO CATALOG v2 STEP2 (POSTER FINAL) =====
import os
import re
import asyncio
import logging
import hashlib
from datetime import datetime
from typing import Optional, Tuple, List

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiohttp import web

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MODE = os.getenv("MODE", "CATALOG").upper()  # CATALOG or POSTER
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = "db.sqlite3"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN required")
if MODE == "POSTER" and not TARGET_CHAT_ID:
    raise RuntimeError("TARGET_CHAT_ID required in POSTER mode")

# ---------------- LOG ----------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("auto_catalog")

# ---------------- PARSING ----------------
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
PRICE_RE = re.compile(r"\b\d{6,}\b|\b\d{1,3}(?:[\s.\u00A0]\d{3})+\b")

def normalize(text: str) -> str:
    return (text or "").replace("\u00A0", " ").strip()

def parse(text: str) -> Tuple[bool, Optional[int], Optional[int]]:
    y = YEAR_RE.search(text)
    p = PRICE_RE.search(text)
    if not y or not p:
        return False, None, None
    year = int(y.group())
    price = int(re.sub(r"\D", "", p.group()))
    return True, year, price

def h(text: str) -> str:
    return hashlib.md5(text.lower().encode("utf-8")).hexdigest()

def link(chat_id: int, msg_id: int) -> str:
    # Works for private supergroups/channels: t.me/c/<internal_id>/<msg_id>
    return f"https://t.me/c/{str(chat_id).replace('-100','')}/{msg_id}"

# ---------------- DB ----------------
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_chat INTEGER,
    src_msg INTEGER,
    text TEXT,
    hash TEXT,
    year INTEGER,
    price INTEGER,
    sold INTEGER DEFAULT 0,
    created TEXT,
    cat_msg INTEGER
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def exists(hash_: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM listings WHERE hash=? LIMIT 1", (hash_,))
        return await cur.fetchone() is not None

async def add_listing(src_chat: int, src_msg: int, text: str, year: int, price: int, cat_msg: Optional[int]) -> int:
    """Insert and return listing id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO listings (src_chat, src_msg, text, hash, year, price, sold, created, cat_msg) "
            "VALUES (?,?,?,?,?,?,0,?,?)",
            (src_chat, src_msg, text, h(text), year, price, datetime.utcnow().isoformat(), cat_msg),
        )
        await db.commit()
        return cur.lastrowid

async def mark_sold_db(lid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE listings SET sold=1 WHERE id=?", (lid,))
        await db.commit()

async def search_db(q: str, limit: int = 5):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, text, year, price, sold FROM listings "
            "WHERE sold=0 AND LOWER(text) LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{q.lower()}%", limit),
        )
        return await cur.fetchall()

async def last_db(limit: int = 5):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, text, year, price, sold FROM listings "
            "WHERE sold=0 ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return await cur.fetchall()

# ---------------- BOT ----------------
dp = Dispatcher()

@dp.message(F.text | F.caption)
async def catch_group(msg: Message):
    # We only index from groups/supergroups (as you intended)
    if msg.chat.type not in ("group", "supergroup"):
        return

    text = normalize(msg.text or msg.caption or "")
    if not text:
        return

    ok, year, price = parse(text)
    if not ok:
        return

    hash_ = h(text)
    if await exists(hash_):
        return

    cat_msg_id: Optional[int] = None
    listing_id: Optional[int] = None

    # First, create the DB record (we want id for SOLD callback)
    listing_id = await add_listing(msg.chat.id, msg.message_id, text, year, price, None)

    if MODE == "POSTER":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📍 Источник", url=link(msg.chat.id, msg.message_id)),
            InlineKeyboardButton(text="✅ SOLD", callback_data=f"sold:{listing_id}"),
        ]])

        caption = f"{year} | {price}\n{text}"

        if msg.photo:
            sent = await msg.bot.send_photo(
                TARGET_CHAT_ID,
                msg.photo[-1].file_id,
                caption=caption,
                reply_markup=kb,
            )
        elif msg.video:
            sent = await msg.bot.send_video(
                TARGET_CHAT_ID,
                msg.video.file_id,
                caption=caption,
                reply_markup=kb,
            )
        else:
            sent = await msg.bot.send_message(
                TARGET_CHAT_ID,
                caption,
                reply_markup=kb,
            )

        cat_msg_id = sent.message_id

        # Update cat_msg in DB for this listing
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE listings SET cat_msg=? WHERE id=?", (cat_msg_id, listing_id))
            await db.commit()

    log.info("Indexed listing id=%s from chat=%s msg=%s", listing_id, msg.chat.id, msg.message_id)

@dp.callback_query(F.data.startswith("sold:"))
async def sold_cb(call: CallbackQuery):
    try:
        lid = int(call.data.split(":", 1)[1])
    except Exception:
        await call.answer("Ошибка SOLD", show_alert=True)
        return

    await mark_sold_db(lid)

    # Replace keyboard with only the source button + SOLD label
    # (we can't always recover source url from message, so just remove buttons)
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Помечено как SOLD ✅")

@dp.message(Command("start"))
async def start(msg: Message):
    if msg.chat.type != "private":
        return
    await msg.answer("Пиши запрос: bmw / camry / 2019 / 2350000\n/last — последние")

@dp.message(Command("last"))
async def last_cmd(msg: Message):
    if msg.chat.type != "private":
        return
    rows = await last_db(5)
    if not rows:
        await msg.answer("Пока пусто 😕")
        return
    for _, text, year, price, _ in rows:
        await msg.answer(f"{year} | {price}\n{text}")

@dp.message(F.text)
async def search(msg: Message):
    if msg.chat.type != "private":
        return
    q = msg.text.strip()
    if not q:
        return
    rows = await search_db(q, limit=5)
    if not rows:
        await msg.answer("Ничего не нашёл 😕")
        return
    for _, text, year, price, _ in rows:
        await msg.answer(f"{year} | {price}\n{text}")

# ---------------- HEALTH ----------------
async def health():
    app = web.Application()
    app.router.add_get("/", lambda r: web.json_response({"ok": True}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

async def main():
    await init_db()
    asyncio.create_task(health())
    bot = Bot(BOT_TOKEN)
    # allowed_updates makes polling more reliable for different update types
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
