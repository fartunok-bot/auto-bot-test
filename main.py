import os
import re
import sqlite3
import datetime as dt
import asyncio
import random

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiohttp import web

TOKEN = os.getenv("BOT_TOKEN")
MODE = os.getenv("MODE", "CATALOG")  # POSTER or CATALOG
DB_PATH = "db.sqlite3"

dp = Dispatcher()

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.execute("""
    CREATE TABLE IF NOT EXISTS ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        msg_id INTEGER,
        author_id INTEGER,
        author_name TEXT,
        created_at TEXT,
        text_raw TEXT,
        status TEXT DEFAULT 'active'
    )
    """)
    c.commit()
    c.close()

def looks_like_ad(text):
    t = text.lower()
    has_price = bool(re.search(r"(\\d+[\\s\\d]{5,}|\\d+(\\.\\d+)?\\s*(м|m)|\\d+\\s*(к|k))", t))
    has_year = bool(re.search(r"\\b(20[0-2]\\d|199\\d)\\b", t))
    return has_price and has_year

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def catalog_listener(m: Message):
    if MODE != "CATALOG":
        return
    text = m.text or m.caption
    if not text or not looks_like_ad(text):
        return
    c = db()
    c.execute(
        "INSERT INTO ads(chat_id,msg_id,author_id,author_name,created_at,text_raw) VALUES(?,?,?,?,?,?)",
        (m.chat.id, m.message_id, m.from_user.id, m.from_user.full_name, dt.datetime.utcnow().isoformat(), text)
    )
    c.commit()
    c.close()

@dp.message(F.text.startswith("/search"))
async def search(m: Message):
    q = m.text.replace("/search", "").strip()
    c = db()
    rows = c.execute(
        "SELECT * FROM ads WHERE status='active' AND text_raw LIKE ? ORDER BY created_at DESC LIMIT 5",
        (f"%{q}%",)
    ).fetchall()
    c.close()
    if not rows:
        await m.reply("Ничего не найдено")
        return
    for r in rows:
        await m.answer(r["text_raw"])

@dp.message(F.text.startswith("/sold"))
async def sold(m: Message):
    if not m.reply_to_message:
        return
    c = db()
    c.execute(
        "UPDATE ads SET status='sold' WHERE chat_id=? AND msg_id=? AND author_id=?",
        (m.chat.id, m.reply_to_message.message_id, m.from_user.id)
    )
    c.commit()
    c.close()
    await m.reply("Отмечено как ПРОДАНО")

async def healthz(_):
    return web.Response(text="ok")

async def start_web():
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
    await site.start()

async def main():
    init_db()
    bot = Bot(TOKEN)
    await start_web()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
