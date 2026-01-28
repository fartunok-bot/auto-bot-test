import os
import re
import asyncio
import logging
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, Dict, Any, List

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
MODE = os.getenv("MODE", "CATALOG").upper()          # CATALOG | POSTER
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = "db.sqlite3"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN required")
if MODE == "POSTER" and TARGET_CHAT_ID == 0:
    raise RuntimeError("TARGET_CHAT_ID required in POSTER mode")

# ---------------- LOG ----------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("auto_catalog")

# ---------------- PARSING ----------------
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
PRICE_TOKEN_RE = re.compile(r"(?i)^(<=|>=|<|>|=)?\s*([\d\s.\u00A0]{6,})$")
PRICE_RANGE_RE = re.compile(r"^\s*(\d[\d\s.\u00A0]{5,})\s*-\s*(\d[\d\s.\u00A0]{5,})\s*$")

def normalize(text: str) -> str:
    return (text or "").replace("\u00A0", " ").strip()

def to_int_price(s: str) -> int:
    return int(re.sub(r"\D", "", s))

def parse_basic(text: str) -> Tuple[bool, Optional[int], Optional[int]]:
    """For group indexing: requires both year and price somewhere in text."""
    y = YEAR_RE.search(text)
    # price: first big number or 'x xxx xxx'
    m = re.search(r"\b\d{6,}\b|\b\d{1,3}(?:[\s.\u00A0]\d{3})+\b", text)
    if not y or not m:
        return False, None, None
    year = int(y.group())
    price = to_int_price(m.group())
    return True, year, price

def h(text: str) -> str:
    return hashlib.md5(text.lower().encode("utf-8")).hexdigest()

def tg_link(chat_id: int, msg_id: int) -> str:
    return f"https://t.me/c/{str(chat_id).replace('-100','')}/{msg_id}"

def build_filters(raw: str) -> Dict[str, Any]:
    """
    Supports:
      - year: 2018
      - price comparators: <2500000, <= 2 500 000, >=1900000, =2350000
      - price range: 2000000-2600000
      - free text terms: audi camry fixok
    """
    raw = normalize(raw)
    parts = [p for p in re.split(r"\s+", raw) if p]
    year: Optional[int] = None
    price_op: Optional[str] = None
    price_val: Optional[int] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    terms: List[str] = []

    for p in parts:
        # year
        if re.fullmatch(r"(19\d{2}|20\d{2})", p):
            year = int(p)
            continue

        # range: 2000000-2600000
        rr = PRICE_RANGE_RE.match(p)
        if rr:
            a = to_int_price(rr.group(1))
            b = to_int_price(rr.group(2))
            price_min, price_max = (a, b) if a <= b else (b, a)
            continue

        # comparator: <2500000 etc.
        mt = PRICE_TOKEN_RE.match(p)
        if mt:
            op = (mt.group(1) or "=").strip()
            val = to_int_price(mt.group(2))
            price_op, price_val = op, val
            continue

        # free term
        terms.append(p)

    return {
        "year": year,
        "price_op": price_op,
        "price_val": price_val,
        "price_min": price_min,
        "price_max": price_max,
        "terms": terms,
        "raw": raw,
    }

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

async def exists_by_hash(hash_: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM listings WHERE hash=? LIMIT 1", (hash_,))
        return await cur.fetchone() is not None

async def add_listing(src_chat: int, src_msg: int, text: str, year: int, price: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO listings (src_chat, src_msg, text, hash, year, price, sold, created, cat_msg) "
            "VALUES (?,?,?,?,?,?,0,?,NULL)",
            (src_chat, src_msg, text, h(text), year, price, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return cur.lastrowid

async def set_cat_msg(lid: int, cat_msg: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE listings SET cat_msg=? WHERE id=?", (cat_msg, lid))
        await db.commit()

async def mark_sold(lid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE listings SET sold=1 WHERE id=?", (lid,))
        await db.commit()

async def get_listing(lid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, src_chat, src_msg, text, year, price, sold, created FROM listings WHERE id=?",
            (lid,),
        )
        return await cur.fetchone()

async def search_db(filters: Dict[str, Any], limit: int = 10):
    where = ["1=1"]
    params: List[Any] = []

    where.append("sold=0")

    if filters.get("year"):
        where.append("year=?")
        params.append(filters["year"])

    # price range
    if filters.get("price_min") is not None and filters.get("price_max") is not None:
        where.append("price BETWEEN ? AND ?")
        params.extend([filters["price_min"], filters["price_max"]])
    elif filters.get("price_op") and filters.get("price_val") is not None:
        op = filters["price_op"]
        if op not in ("<", ">", "<=", ">=", "="):
            op = "="
        where.append(f"price {op} ?")
        params.append(filters["price_val"])

    # terms
    for t in filters.get("terms", []):
        where.append("LOWER(text) LIKE ?")
        params.append(f"%{t.lower()}%")

    sql = (
        "SELECT id, src_chat, src_msg, text, year, price, sold "
        "FROM listings WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT ?"
    )
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, tuple(params))
        return await cur.fetchall()

async def last_db(limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, src_chat, src_msg, text, year, price, sold "
            "FROM listings WHERE sold=0 ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return await cur.fetchall()

async def stats_db():
    async with aiosqlite.connect(DB_PATH) as db:
        cur1 = await db.execute("SELECT COUNT(*) FROM listings")
        total = (await cur1.fetchone())[0]
        cur2 = await db.execute("SELECT COUNT(*) FROM listings WHERE sold=0")
        active = (await cur2.fetchone())[0]
        cur3 = await db.execute("SELECT COUNT(*) FROM listings WHERE sold=1")
        sold = (await cur3.fetchone())[0]

        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cur4 = await db.execute("SELECT COUNT(*) FROM listings WHERE created >= ?", (since,))
        last24 = (await cur4.fetchone())[0]

    return {"total": total, "active": active, "sold": sold, "last24": last24}

# ---------------- UI helpers ----------------
def listing_kb(src_chat: int, src_msg: int, lid: int, sold: int) -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(text="📍 Источник", url=tg_link(src_chat, src_msg))]
    if not sold:
        buttons.append(InlineKeyboardButton(text="✅ SOLD", callback_data=f"sold:{lid}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])

def format_listing(lid: int, year: int, price: int, text: str, sold: int) -> str:
    head = f"{year} | {price}"
    if sold:
        head = f"❌ SOLD — {head}"
    return f"#{lid}\n{head}\n{text}"

async def safe_edit_message(call: CallbackQuery, new_text: str, kb: InlineKeyboardMarkup):
    """
    For text messages -> edit_text
    For media (photo/video) -> edit_caption
    """
    msg = call.message
    try:
        if msg.photo or msg.video or msg.document or msg.animation:
            await msg.edit_caption(caption=new_text, reply_markup=kb)
        else:
            await msg.edit_text(new_text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        log.warning("Edit failed: %s", e)

# ---------------- BOT ----------------
dp = Dispatcher()

# ГРУППЫ: ловим объявления (НЕ отвечаем)
@dp.message((F.text | F.caption) & F.chat.type.in_({"group", "supergroup"}))
async def catch_group(msg: Message):
    text = normalize(msg.text or msg.caption or "")
    if not text:
        return

    ok, year, price = parse_basic(text)
    if not ok:
        return

    hash_ = h(text)
    if await exists_by_hash(hash_):
        return

    lid = await add_listing(msg.chat.id, msg.message_id, text, year, price)
    log.info("Indexed listing id=%s from chat=%s msg=%s", lid, msg.chat.id, msg.message_id)

    # POSTER mode: repost to target chat with buttons
    if MODE != "POSTER":
        return

    kb = listing_kb(msg.chat.id, msg.message_id, lid, sold=0)
    caption = format_listing(lid, year, price, text, sold=0)

    if msg.photo:
        sent = await msg.bot.send_photo(TARGET_CHAT_ID, msg.photo[-1].file_id, caption=caption, reply_markup=kb)
    elif msg.video:
        sent = await msg.bot.send_video(TARGET_CHAT_ID, msg.video.file_id, caption=caption, reply_markup=kb)
    else:
        sent = await msg.bot.send_message(TARGET_CHAT_ID, caption, reply_markup=kb, disable_web_page_preview=True)

    await set_cat_msg(lid, sent.message_id)

# SOLD callback
@dp.callback_query(F.data.startswith("sold:"))
async def sold_cb(call: CallbackQuery):
    try:
        lid = int(call.data.split(":", 1)[1])
    except Exception:
        await call.answer("Ошибка SOLD", show_alert=True)
        return

    row = await get_listing(lid)
    if not row:
        await call.answer("Лот не найден", show_alert=True)
        return

    _, src_chat, src_msg, text, year, price, sold, _created = row
    if sold:
        await call.answer("Уже SOLD", show_alert=False)
        # обновим клаву (уберём SOLD)
        kb = listing_kb(src_chat, src_msg, lid, sold=1)
        await safe_edit_message(call, format_listing(lid, year, price, text, sold=1), kb)
        return

    await mark_sold(lid)
    kb = listing_kb(src_chat, src_msg, lid, sold=1)
    await safe_edit_message(call, format_listing(lid, year, price, text, sold=1), kb)
    await call.answer("Помечено как SOLD ✅")

# ЛИЧКА: /start
@dp.message(Command("start"))
async def start(msg: Message):
    if msg.chat.type != "private":
        return
    await msg.answer(
        "Команды:\n"
        "/search <запрос> — поиск (поддерживает фильтры)\n"
        "/last — последние\n"
        "/id <номер> — открыть лот\n"
        "/stats — статистика\n\n"
        "Примеры:\n"
        "camry 2019 <2500000\n"
        "audi 2018 1800000-2200000\n"
    )

# ЛИЧКА: /search
@dp.message(Command("search"))
async def search_cmd(msg: Message):
    if msg.chat.type != "private":
        return
    q = normalize(msg.text.replace("/search", "", 1))
    if not q:
        await msg.answer("Напиши так: /search camry 2019 <2500000")
        return

    filters = build_filters(q)
    rows = await search_db(filters, limit=10)
    if not rows:
        await msg.answer("Ничего не нашёл 😕")
        return

    await msg.answer(f"Нашёл: {len(rows)} (показываю до 10). Запрос: {filters['raw']}")
    for lid, src_chat, src_msg, text, year, price, sold in rows:
        kb = listing_kb(src_chat, src_msg, lid, sold)
        await msg.answer(format_listing(lid, year, price, text, sold), reply_markup=kb, disable_web_page_preview=True)

# ЛИЧКА: /last
@dp.message(Command("last"))
async def last_cmd(msg: Message):
    if msg.chat.type != "private":
        return
    rows = await last_db(10)
    if not rows:
        await msg.answer("Пока пусто 😕")
        return
    for lid, src_chat, src_msg, text, year, price, sold in rows:
        kb = listing_kb(src_chat, src_msg, lid, sold)
        await msg.answer(format_listing(lid, year, price, text, sold), reply_markup=kb, disable_web_page_preview=True)

# ЛИЧКА: /id
@dp.message(Command("id"))
async def id_cmd(msg: Message):
    if msg.chat.type != "private":
        return
    parts = normalize(msg.text).split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.answer("Формат: /id 123")
        return
    lid = int(parts[1])
    row = await get_listing(lid)
    if not row:
        await msg.answer("Лот не найден 😕")
        return
    _, src_chat, src_msg, text, year, price, sold, _created = row
    kb = listing_kb(src_chat, src_msg, lid, sold)
    await msg.answer(format_listing(lid, year, price, text, sold), reply_markup=kb, disable_web_page_preview=True)

# ЛИЧКА: /stats
@dp.message(Command("stats"))
async def stats_cmd(msg: Message):
    if msg.chat.type != "private":
        return
    s = await stats_db()
    await msg.answer(
        "📊 Статистика\n"
        f"Всего: {s['total']}\n"
        f"Активных: {s['active']}\n"
        f"SOLD: {s['sold']}\n"
        f"За 24ч добавлено: {s['last24']}"
    )

# ЛИЧКА: простой поиск (если просто написал текст, не команду)
@dp.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def search_plain(msg: Message):
    q = normalize(msg.text)
    if not q:
        return
    filters = build_filters(q)
    rows = await search_db(filters, limit=10)
    if not rows:
        await msg.answer("Ничего не нашёл 😕")
        return
    for lid, src_chat, src_msg, text, year, price, sold in rows:
        kb = listing_kb(src_chat, src_msg, lid, sold)
        await msg.answer(format_listing(lid, year, price, text, sold), reply_markup=kb, disable_web_page_preview=True)

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

    bot = Bot(BOT_TOKEN)

    # важно: убрать webhook, чтобы не было TelegramConflictError
    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
