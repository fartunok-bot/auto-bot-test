import os
import re
import asyncio
import logging
from datetime import datetime
from typing import Optional, Tuple, List, Any

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiohttp import web


# -------------------- CONFIG --------------------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
MODE = (os.getenv("MODE") or "CATALOG").strip().upper()  # CATALOG | POSTER
TARGET_CHAT_ID_RAW = (os.getenv("TARGET_CHAT_ID") or "").strip()
PORT = int((os.getenv("PORT") or "10000").strip())

DB_PATH = "db.sqlite3"

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is required")

if MODE not in ("CATALOG", "POSTER"):
    raise RuntimeError("MODE must be CATALOG or POSTER")

TARGET_CHAT_ID: Optional[int] = None
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


def normalize_price(text: str) -> Optional[int]:
    m = PRICE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    if not digits:
        return None
    try:
        val = int(digits)
    except ValueError:
        return None
    if val < 50_000:
        return None
    return val


def extract_year(text: str) -> Optional[int]:
    m = YEAR_RE.search(text)
    if not m:
        return None
    y = int(m.group(1))
    if 1970 <= y <= datetime.now().year + 1:
        return y
    return None


def detect_listing(text: str) -> Tuple[bool, Optional[int], Optional[int]]:
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


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()


async def save_listing_to_db(
    src_chat_id: int,
    src_message_id: int,
    author_id: Optional[int],
    text: str,
    year: Optional[int],
    price: Optional[int],
) -> bool:
    """
    True = inserted new row, False = already exists
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


async def search_db(query: str, limit: int = 5) -> List[Any]:
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


async def mark_sold_db(src_chat_id: int, src_message_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE listings SET sold=1 WHERE src_chat_id=? AND src_message_id=?",
            (src_chat_id, src_message_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def set_posted_db(src_chat_id: int, src_message_id: int, target_chat_id: int, target_message_id: int) -> None:
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


# -------------------- BOT --------------------
dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    # ВАЖНО: /start только в личке (чтобы не флудить в группе)
    if message.chat.type != "private":
        return

    await message.answer(
        "✅ AUTO CATALOG запущен.\n\n"
        "Команды:\n"
        "/search <запрос> — последние 5 совпадений\n"
        "/sold — ответом на объявление пометить SOLD\n\n"
        f"MODE: {MODE}"
    )


@dp.message(Command("search"))
async def cmd_search(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Пиши так: /search BMW")
        return

    query = parts[1].strip()
    rows = await search_db(query, limit=5)

    if not rows:
        await message.answer("Ничего не нашёл 😕")
        return

    out = []
    for (chat_id, msg_id, text, year, price, sold, created_at) in rows:
        sold_mark = "✅ SOLD" if sold else "🟢"
        price_str = f"{int(price):,}".replace(",", " ") if price else "—"
        year_str = str(year) if year else "—"
        snippet = (text or "").strip().replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:160] + "…"

        out.append(
            f"{sold_mark} | {year_str} | {price_str}\n"
            f"{snippet}\n"
            f"(src: {chat_id}, msg: {msg_id})"
        )

    await message.answer("\n\n".join(out))


@dp.message(Command("sold"))
async def cmd_sold(message: Message) -> None:
    if not message.reply_to_message:
        await message.answer("Нужно ответить на объявление и написать /sold")
        return

    src_chat_id = message.chat.id
    src_message_id = message.reply_to_message.message_id

    ok = await mark_sold_db(src_chat_id, src_message_id)
    await message.answer("✅ Пометил как SOLD" if ok else "Не нашёл это объявление в базе 😕")


# ВАЖНО: НЕ ОТВЕЧАЕТ на обычные сообщения — только сохраняет и (опционально) копирует в каталог
@dp.message(F.text)
async def catch_listings(message: Message, bot: Bot) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return

    text = message.text or ""
    ok, year, price = detect_listing(text)
    if not ok:
        return

    inserted = await save_listing_to_db(
        src_chat_id=message.chat.id,
        src_message_id=message.message_id,
        author_id=message.from_user.id if message.from_user else None,
        text=text,
        year=year,
        price=price,
    )
    if not inserted:
        return

    log.info("Saved listing: chat=%s msg=%s year=%s price=%s", message.chat.id, message.message_id, year, price)

    if MODE == "POSTER" and TARGET_CHAT_ID is not None:
        try:
            copied = await bot.copy_message(
                chat_id=TARGET_CHAT_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            await set_posted_db(message.chat.id, message.message_id, TARGET_CHAT_ID, copied.message_id)
            log.info("Posted to target: target_chat=%s target_msg=%s", TARGET_CHAT_ID, copied.message_id)
        except Exception:
            log.exception("Failed to post to TARGET_CHAT_ID")


# -------------------- HEALTH SERVER (Render-friendly) --------------------
async def health_server() -> None:
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "mode": MODE})

    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Health server on 0.0.0.0:%s", PORT)


async def main() -> None:
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    await health_server()

    log.info("Bot starting polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
