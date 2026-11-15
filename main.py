import asyncio
import html
import logging
import os
import platform
import random
import re
import shutil
import socket
import sys
import time
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from math import ceil
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import aiosqlite
import orjson
import aiogram
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)
from dotenv import load_dotenv
from loguru import logger

try:
    import uvloop
except ImportError:  # например, Windows
    uvloop = None

load_dotenv()
os.umask(0o077)  # новые файлы (БД, -wal, -shm) доступны только владельцу

BOT_TOKEN = os.getenv("CARD_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Укажите CARD_BOT_TOKEN в .env или переменных окружения.")

# SQLite-база. Если в CARD_BOT_DATA_PATH до сих пор указан старый .json-файл,
# он считается источником для одноразовой миграции, а база создаётся рядом (.db).
_configured_data_path = Path(os.getenv("CARD_BOT_DATA_PATH", "data/card_bot.db"))
if _configured_data_path.suffix.lower() == ".json":
    LEGACY_JSON_PATH = _configured_data_path
    DB_PATH = _configured_data_path.with_suffix(".db")
else:
    DB_PATH = _configured_data_path
    LEGACY_JSON_PATH = Path(os.getenv("CARD_BOT_LEGACY_JSON_PATH", "data/card_bot.json"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

CASE_COOLDOWN_SECONDS = 3 * 60 * 60
# Set to None to resume random card drops after the temporary guarantee ends.
GUARANTEED_CARD_ID: str | None = None
COOLDOWN_IMAGE_URL = "https://i.ibb.co/fYvgVyv6/96-20261102223410.png"
COOLDOWN_RANDOM_IMAGE_URLS = (
    "https://i.ibb.co/ZRnmHqjm/96-20261102223256.png",
    "https://i.ibb.co/Kj1NbcY9/96-20261102223306.png",
    "https://i.ibb.co/1f7QnZhw/96-20261102223313.png",
    "https://i.ibb.co/yct386nQ/96-20261102223528.png",
    "https://i.ibb.co/SXp84Qpk/96-20261102223535.png",
    "https://i.ibb.co/RT6PSxf1/96-20261102223617.png",
)
START_IMAGE_URL = "https://i.ibb.co/JRDgvgxC/96-20261102230850.png"
TOP_IMAGE_URL = "https://i.ibb.co/xqN4ct1S/96-20261102231521.png"
HELP_DISCLAIMER_IMAGE_URL = "https://i.ibb.co/r2szFTGf/96-20261102230301.png"
START_CHAT_URL = "https://t.me/podval_server"
TATARE_IMAGE_URL = "https://i.ibb.co/C3rwn011/136-20261113071946.png"
COOLDOWN_EMOJI_ID = "5134153895039207789"
TOP_PAGE_SIZE = 15

# Глобальный лок защищает последовательности «прочитал -> изменил -> записал»
# на единственном соединении SQLite.
DATA_LOCK = asyncio.Lock()
# Отдельный лок на каждый URL: разные картинки качаются параллельно,
# а одна и та же — только один раз. Ключи берутся только из статического каталога.
CARD_IMAGE_DOWNLOAD_LOCKS: dict[str, asyncio.Lock] = {}
# LRU-кэш картинок с лимитом по суммарному размеру в байтах.
CARD_IMAGE_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
CARD_IMAGE_CACHE_BYTES = 0
IMAGE_CACHE_MAX_BYTES = int(os.getenv("CARD_BOT_IMAGE_CACHE_MB", "128")) * 1024 * 1024
IMAGE_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(8)  # одновременных загрузок

IMAGE_MAX_BYTES = 10_000_000
IMAGE_MAX_REDIRECTS = 3
IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 60
IMAGE_MAGIC_BYTES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")
# Разрешённые хосты для загрузки картинок. Если добавите карты с нового хоста,
# пропишите его в CARD_BOT_IMAGE_HOSTS (через запятую).
ALLOWED_IMAGE_HOSTS = frozenset(
    host.strip().lower()
    for host in os.getenv("CARD_BOT_IMAGE_HOSTS", "i.ibb.co,imglink.cc").split(",")
    if host.strip()
)
LEGACY_JSON_MAX_BYTES = 50 * 1024 * 1024
MAX_MATERIALS_PER_TIER = 200
NAME_MAX_LEN = 64
# Кнопки «Улучшить», которые обрабатываются прямо сейчас: (chat_id, message_id).
EVOLUTIONS_IN_PROGRESS: set[tuple[int, int]] = set()

DB: aiosqlite.Connection | None = None
BOT_START_TIME = time.monotonic()

RARITY_ID_PREFIXES = {
    "common": "c",
    "rare": "r",
    "universal": "u",
    "epic": "e",
    "mythic": "m",
    "legendary": "l",
    "royal": "k",
    "xeno": "x",
}
USER_RARITY_ID_PREFIXES = {
    "common": "A",
    "rare": "B",
    "universal": "C",
    "epic": "D",
    "mythic": "E",
    "legendary": "F",
    "royal": "K",
    "xeno": "X",
}

CARD_AUTHOR_EMOJI_ID = "5136436369509320534"
CARD_DECORATION_EMOJI_ID = "5467512381628231347"
CARD_NUMBER_EMOJI_ID = "5136522238790469903"
CARD_RARITY_EMOJI_ID = "5136607618445347119"
NEW_CARD_AURA_EMOJI_ID = "5136906732852742506"
TOP_AURA_EMOJI_ID = "5136834787855566612"
TOP_MEDAL_EMOJI_IDS = (
    "5440539497383087970",
    "5447203607294265305",
    "5453902265922376865",
)
TOP_RANK_DIGIT_EMOJI_IDS = {
    "0": "5339232999965734062",
    "1": "5339240103841644669",
    "2": "5339516819994589314",
    "3": "5341372430550054918",
    "4": "5339556926399200576",
    "5": "5339168742960020390",
    "6": "5339518731255034738",
    "7": "5341541033786225389",
    "8": "5339441464793384228",
    "9": "5341350259928872526",
}
TOP_RANK_DIGIT_EMOJI_FALLBACKS = {
    "0": "0️⃣",
    "1": "1️⃣",
    "2": "2️⃣",
    "3": "3️⃣",
    "4": "4️⃣",
    "5": "5️⃣",
    "6": "6️⃣",
    "7": "7️⃣",
    "8": "8️⃣",
    "9": "9️⃣",
}
START_WELCOME_EMOJI_ID = "5134187640597252613"
START_HELP_EMOJI_ID = "5258152182150077732"
START_LUCK_EMOJI_ID = "5143328228651303849"
HELP_TITLE_EMOJI_ID = "5257965174979042426"
HELP_COMMAND_EMOJI_ID = "5260221883940347555"
EVOLUTION_TITLE_EMOJI_ID = "5161157196242749683"
EVOLUTION_TEXT_EMOJI_ID = "5136422685743516465"
NEW_CARD_EMOJI_IDS = (
    "5136881130552690765",
    "5136390297395137418",
    "5134553090774534683",
    "5134329877029194039",
    "5136585791421548766",
)
REPEAT_CARD_EMOJI_IDS = (
    "5134648911494907642",
    "5134596418404616314",
    "5136910151646710021",
    "5136754656650725335",
    "5134333248578522012",
)
BRONZE_EVOLUTION_EMOJI_IDS = (
    "5136872768251365288",
    "5134288237321259490",
    "5134569639283526043",
    "5134180090044745983",
    "5136894341872093394",
)
SILVER_EVOLUTION_EMOJI_IDS = (
    "5134330701662915782",
    "5134287833594333980",
    "5134465520686336292",
    "5134589061125638503",
    "5134522811255097973",
)
GOLD_EVOLUTION_EMOJI_IDS = (
    "5134532182873737261",
    "5134501422317963601",
    "5134631100265531766",
    "5134375141689526561",
    "5134218186404662105",
)

RARITIES = {
    "common": {
        "name": "Обычная", "emoji": "⚪", "custom_emoji_id": "5134437633463683070",
        "aura": 10, "weight": 55,
    },
    "rare": {
        "name": "Редкая", "emoji": "🔵", "custom_emoji_id": "5134510506173794463",
        "aura": 25, "weight": 25,
    },
    "universal": {
        "name": "Универсал", "emoji": "🟢", "custom_emoji_id": "5134130916964174379",
        "aura": 60, "weight": 12,
    },
    "epic": {
        "name": "Эпическая", "emoji": "🟣", "custom_emoji_id": "5134169017619057183",
        "aura": 150, "weight": 5,
    },
    "mythic": {
        "name": "Мифическая", "emoji": "🔴", "custom_emoji_id": "5134146718148855986",
        "aura": 350, "weight": 2,
    },
    "legendary": {
        "name": "Легендарная", "emoji": "🟠", "custom_emoji_id": "5134569403060324388",
        "aura": 800, "weight": 0.70,
    },
    "royal": {
        "name": "Царская", "emoji": "👑", "custom_emoji_id": "5136807347309512910",
        "aura": 2000, "weight": 0.25,
    },
    "xeno": {
        "name": "Ксено", "emoji": "🌌", "custom_emoji_id": "5134146765393496176",
        "aura": 5000, "weight": 0.05,
    },
}

# Новые карты добавляйте в конец списка: ID состоит из префикса редкости и номера.
CARDS_BY_RARITY = {
    "common": [
        {"name": "Пиксельный", "image_url": "https://i.ibb.co/TMjXZmzB/60-20261102134736.png"},
        {"name": "Классический хог", "image_url": "https://i.ibb.co/MqHvg3T/2134-2.png"},
        {"name": "Голограмма", "image_url": "https://i.ibb.co/x485ThJ/2134-20260718100556.png"},
        {"name": "Лысяш", "image_url": "https://i.ibb.co/YTp4YP7h/2134-20260718102805.png"},
        {"name": "Циклоп", "image_url": "https://i.ibb.co/3Y93jXWX/2134-20260718104241.png"},
        {"name": "Hogness combat", "image_url": "https://i.ibb.co/YBvmvjJM/2134-20260718105031.png"},
        {"name": "Новиоп", "image_url": "https://i.ibb.co/hzLQ9cG/2134-20260718112325.png"},
        {"name": "Аллюминий", "image_url": "https://i.ibb.co/KjX48bk7/2134-20260718112958.png"},
        {"name": "Изображение заблокировано", "image_url": "https://i.ibb.co/9kLCJ8cM/2134-20260718123918.png"},
        {"name": "Пчела", "image_url": "https://i.ibb.co/qMQVvmmr/2134-20260718125706.png"},
        {"name": "Санта клаус", "image_url": "https://i.ibb.co/990Q49NL/2134-20260718135300.png"},
        {"name": "КарточкEA", "image_url": "https://i.ibb.co/mCVs105z/2145-20260718165049.png"},
        {"name": "Ботан", "image_url": "https://i.ibb.co/zVZWxBNP/2146-20260729235952.png"},
        {"name": "Тограш", "image_url": "https://i.ibb.co/mrFtN3QC/2146-20260730001045.png"},
        {"name": "Вишня", "image_url": "https://i.ibb.co/jkydjrFp/2146-20260730002125.png"},
        {"name": "Кабан на скакалке", "image_url": "https://i.ibb.co/d0n0k9Xk/2146-20260730145604.png"},
        {"name": "Под прицелом", "image_url": "https://i.ibb.co/LDQ5Y80W/2146-20260730004108.png"},
        {"name": "Пубертатник", "image_url": "https://i.ibb.co/Y4Lk4Bhk/2134-20260730232436.png"},
        {"name": "Беззубый", "image_url": "https://i.ibb.co/v40QsM0r/2145-20260725222901.png"},
        {"name": "Злюка", "image_url": "https://i.ibb.co/TDTsc5s6/2146-20260728164919.png"},
        {"name": "Пузырь", "image_url": "https://i.ibb.co/G3CjvdWY/2145-20260802125506.png"},
        {"name": "Гладкий", "image_url": "https://i.ibb.co/BHKNN974/2145-20260802140804.png"},
        {"name": "Анонимус", "image_url": "https://i.ibb.co/DHY0kcGD/2145-20260802143505.png"},
        {"name": "Гуль", "image_url": "https://i.ibb.co/zhbW397Y/2145-20260802144333.png"},
        {"name": "Гидроцефал", "image_url": "https://i.ibb.co/wFPc5QXz/2145-20260802151029.png"},
        {"name": "Рентген", "image_url": "https://i.ibb.co/NdCb0S9P/2145-20260802155914.png"},
        {"name": "Пельмень", "image_url": "https://i.ibb.co/vC0zLKbk/2145-20260802162001.png"},
        {"name": "Ретроградъ", "image_url": "https://i.ibb.co/nqxYvZVt/2145-20260802135236.png"},
    ],
    "rare": [
        {"name": "Каб и Бан", "image_url": "https://i.ibb.co/Txv3sL1Q/55-20261103153911.png"},
        {"name": "Мрак", "image_url": "https://i.ibb.co/RkgQc16c/55-20261103155449.png"},
        {"name": "Иноагент", "image_url": "https://i.ibb.co/67rCkc6z/55-20261103153639.png"},
        {"name": "Boy next door", "image_url": "https://i.ibb.co/KxyzDcjy/61-20261103152206.png"},
        {"name": "Строитель", "image_url": "https://i.ibb.co/gMGsZx8y/61-20261103233139.png"},
        {"name": "ТЦКшник", "image_url": "https://i.ibb.co/7JsGvC83/61-20261104102154.png"},
        {"name": "Раковый больной", "image_url": "https://i.ibb.co/YFtxNbVR/61-20261103002850.png"},
        {"name": "Angry hog", "image_url": "https://i.ibb.co/XxbKJQHM/61-20261102073129.png"},
        {"name": "Мистер фриман", "image_url": "https://i.ibb.co/1fgmsPwQ/61-20261101181509.png"},
        {"name": "Кабан из ноября 2026", "image_url": "https://i.ibb.co/jktwJQvN/61-20261101134154.png"},
        {"name": "Глаукома", "image_url": "https://i.ibb.co/Rp4spWKf/61-20261101125557.png"},
        {"name": "Иллюзия", "image_url": "https://i.ibb.co/C5p4rGyP/61-20261101125948.png"},
        {"name": "Негатив", "image_url": "https://i.ibb.co/k2DPDg1z/61-20261101125128.png"},
        {"name": "Балатро", "image_url": "https://i.ibb.co/ch4C9j04/61-20261101124812.png"},
        {"name": "Невидимка", "image_url": "https://i.ibb.co/60CpwHhr/61-20261101124345.png"},
        {"name": "Перс", "image_url": "https://i.ibb.co/6c7spxtN/61-20261101123430.png"},
        {"name": "Рот занят", "image_url": "https://i.ibb.co/Qy0ptM1/55-20261101120554.png"},
        {"name": "Алексей Щевцов", "image_url": "https://i.ibb.co/39B3HBc4/61-20261101110608.png"},
        {"name": "Кабанотазойды", "image_url": "https://i.ibb.co/VWcZmpzV/55-20261101010049.png"},
        {"name": "Жиденко", "image_url": "https://i.ibb.co/8nwWh8TZ/2135-20260712150559.png"},
    ],
    "universal": [
        {"name": "Загрузка", "image_url": "https://i.ibb.co/8nrdyVDX/57-20261104133455.png"},
        {"name": "Флешбеки", "image_url": "https://i.ibb.co/vthdZTC/57-20261104234537.png"},
        {"name": "Lesbian Hog Princess", "image_url": "https://i.ibb.co/RGCDhBtb/57-20261103185427.png"},
    ],
    "epic": [
        {"name": "Картелец", "image_url": "https://imglink.cc/cdn/-T7Xgyrr2L.png"},
        {"name": "Самый мирный хог", "image_url": "https://imglink.cc/cdn/9dRVBvnjRL.png"},
        {"name": "Хогджак", "image_url": "https://i.ibb.co/chG19TLF/56-20261107173806.png"},
        {"name": "Раста", "image_url": "https://i.ibb.co/Y4nth8z6/56-20261107180802.png"},
        {"name": "Анимешник", "image_url": "https://i.ibb.co/bDXcDmn/63-20261108005807.png"},
        {"name": "Нефор", "image_url": "https://i.ibb.co/7dNTXj1H/56-20261029124606.png"},
        {"name": "ПризраKKK", "image_url": "https://i.ibb.co/2YMMQsVT/2142-20260711121040.png"},
        {"name": "Чувак", "image_url": "https://i.ibb.co/yMN5q67/62-20261108191754.png"},
    ],
    "mythic": [
        {"name": "Вооружён", "image_url": "https://i.ibb.co/v495pLFQ/54-20261029084844.png"},
        {"name": "Абортыш", "image_url": "https://i.ibb.co/qFJyGshc/2146-20260711113726.png"},
        {"name": "Мистер Пиклз", "image_url": "https://i.ibb.co/WpBmHXyj/141-20261114203624.png"},
    ],
    "legendary": [
        {"name": "Соседи", "image_url": "https://i.ibb.co/r2NqWt4j/55-20261107164636.png"},
        {"name": "Мунинайт", "image_url": "https://i.ibb.co/kV7wYMrn/61-20261101135113.png"},
        {"name": "Дылда", "image_url": "https://i.ibb.co/b9Pp3M4/55-20261108210933.png"},
        {"name": "Вайомингский житель", "image_url": "https://i.ibb.co/r2m4h4Qy/125-20261109161057.png"},
        {"name": "Генеральные котлы", "image_url": "https://i.ibb.co/6JBzyCDG/53-20261109073844.png"},
        {"name": "Металлист", "image_url": "https://i.ibb.co/jZrq7B9C/55-20261109133133.png"},
        {"name": "Полурак", "image_url": "https://i.ibb.co/BHFRcsrQ/IMG-20261114-130626-337.png"},
    ],
    "royal": [
        {"name": "South Hog", "image_url": "https://i.ibb.co/hNCCVFG/63-20261105104730.png"},
        {"name": "Ариец", "image_url": "https://i.ibb.co/9mWVsnCx/63-20261108234922.png"},
        {"name": "Малой", "image_url": "https://i.ibb.co/XZQjyFYF/63-20261108231553.png"},
        {"name": "hog cards rule34", "image_url": "https://imglink.cc/cdn/O1A0Sh5pAx.png"},
    ],
    "xeno": [
        {"name": "Зеркальный гектор", "image_url": "https://i.ibb.co/39MnTW5S/59-2.png"},
        {"name": "Г3ктор порко", "image_url": "https://i.ibb.co/F4nXnQtx/59-20261101112911.png"},
    ],
}

for rarity, cards in CARDS_BY_RARITY.items():
    prefix = RARITY_ID_PREFIXES[rarity]
    for number, card in enumerate(cards, start=1):
        card["id"] = f"{prefix}{number}"

CARDS_BY_ID = {
    card["id"]: (rarity, card)
    for rarity, cards in CARDS_BY_RARITY.items()
    for card in cards
}
EVOLUTION_TIER_ORDER = ("bronze", "silver", "gold")
EVOLUTION_TIER_LABELS = {
    "bronze": "бронзовая",
    "silver": "серебряная",
    "gold": "золотая",
}
EVOLUTION_TIER_ID_SUFFIXES = {
    "bronze": "B",
    "silver": "C",
    "gold": "G",
}
CARD_EVOLUTIONS = {
    "hog cards rule34": {
        "bronze": {
            "multiplier": 2,
            "image_url": "https://imglink.cc/cdn/Z6U3kGx99g.png",
            "reveal_image_url": "https://i.ibb.co/gbd4rxbY/86-20261101174701.png",
        },
        "silver": {
            "multiplier": 3,
            "image_url": "https://imglink.cc/cdn/QXvllqca0V.png",
            "reveal_image_url": "https://i.ibb.co/W49JmZsC/86-20261101174646.png",
        },
        "gold": {
            "multiplier": 4,
            "image_url": "https://imglink.cc/cdn/ILZR9ntccN.png",
            "reveal_image_url": "https://i.ibb.co/yc7g7M7J/86-20261101174624.png",
        },
    },
}


# --------------------------------------------------------------------------- #
# Логирование (loguru, enqueue=True — запись логов вне event loop)
# --------------------------------------------------------------------------- #

class InterceptHandler(logging.Handler):
    """Перенаправляет стандартные logging-записи aiogram в loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss,SSS} {level} {name}: {message}",
    enqueue=True,
    backtrace=False,
    diagnose=False,  # не печатать значения локальных переменных (токен, ПДн)
)
logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
del BOT_TOKEN  # токен больше не живёт в глобалах модуля
dp = Dispatcher()


# --------------------------------------------------------------------------- #
# Хранилище (aiosqlite)
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    first_name TEXT,
    username TEXT,
    total_cards INTEGER NOT NULL DEFAULT 0,
    total_aura INTEGER NOT NULL DEFAULT 0,
    last_card_id TEXT,
    last_case_at REAL NOT NULL DEFAULT 0,
    cooldown_message_count INTEGER NOT NULL DEFAULT 0,
    evolution_material_messages TEXT NOT NULL DEFAULT '{}',
    evolution_combo_messages TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS collection (
    user_id INTEGER NOT NULL,
    card_id TEXT NOT NULL,
    card_name TEXT NOT NULL,
    rarity TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    evo_bronze INTEGER NOT NULL DEFAULT 0,
    evo_silver INTEGER NOT NULL DEFAULT 0,
    evo_gold INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, card_id)
);

CREATE TABLE IF NOT EXISTS image_file_ids (
    image_url TEXT PRIMARY KEY,
    file_id TEXT NOT NULL
);
"""

USER_COLUMNS = (
    "user_id, first_name, username, total_cards, total_aura, "
    "last_card_id, last_case_at, cooldown_message_count"
)
COLLECTION_COLUMNS = (
    "card_id, card_name, rarity, count, evo_bronze, evo_silver, evo_gold"
)
# Статические запросы: никакой конкатенации SQL.
SQL_COLLECTION_ALL = (
    f"SELECT {COLLECTION_COLUMNS} FROM collection WHERE user_id = ? ORDER BY rowid"
)
SQL_COLLECTION_BY_RARITY = (
    f"SELECT {COLLECTION_COLUMNS} FROM collection "
    "WHERE user_id = ? AND rarity = ? ORDER BY rowid"
)


def db() -> aiosqlite.Connection:
    """Возвращает открытое соединение с базой."""
    if DB is None:
        raise RuntimeError("База данных не инициализирована.")
    return DB


def dumps_json(value: object) -> str:
    """Сериализует значение в JSON-строку для хранения в TEXT-колонке."""
    return orjson.dumps(value).decode()


# --- валидация и очистка входных данных ---

_BIDI_RE = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_display_name(value: str | None, limit: int = NAME_MAX_LEN) -> str | None:
    """Убирает BiDi- и управляющие символы, обрезает длину."""
    if value is None:
        return None
    value = _CTRL_RE.sub("", _BIDI_RE.sub("", value)).strip()
    return value[:limit]


def loads_json_dict(raw: str | bytes | None) -> dict:
    """Безопасно читает JSON-объект; при мусоре возвращает {}."""
    if not raw:
        return {}
    try:
        value = orjson.loads(raw)
    except orjson.JSONDecodeError:
        logger.warning("Повреждённый JSON в колонке БД, использую {}")
        return {}
    return value if isinstance(value, dict) else {}


def normalize_materials(raw: dict) -> dict[str, list[dict]]:
    """Оставляет только корректные записи материалов эво."""
    result: dict[str, list[dict]] = {}
    for tier, items in raw.items():
        if not isinstance(tier, str) or not isinstance(items, list):
            continue
        result[tier] = [
            item
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("chat_id"), int)
            and isinstance(item.get("card_message_id"), int)
        ]
    return result


def as_int(value: object, default: int = 0, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        number = int(value)
    except (ValueError, OverflowError):
        return default
    return min(max(number, minimum), 2**62)


def as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    return number if number == number and abs(number) != float("inf") else default


def as_text(value: object, limit: int = 256) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def harden_storage_permissions() -> None:
    """0700 на каталог и 0600 на БД, WAL и SHM (best effort; на Windows не действует)."""
    with suppress(OSError):
        os.chmod(DB_PATH.parent, 0o700)
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{DB_PATH}{suffix}")
        if path.exists():
            with suppress(OSError):
                os.chmod(path, 0o600)


@asynccontextmanager
async def write_transaction():
    """Лок + одна транзакция BEGIN IMMEDIATE: commit при успехе, rollback при ошибке."""
    async with DATA_LOCK:
        conn = db()
        if not conn.in_transaction:
            await conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            await conn.rollback()
            raise
        else:
            await conn.commit()


async def migrate_legacy_json() -> None:
    """Однократно переносит данные из старого JSON-хранилища в пустую базу."""
    async with db().execute("SELECT 1 FROM users LIMIT 1") as cursor:
        if await cursor.fetchone() is not None:
            return
    if not LEGACY_JSON_PATH.exists():
        return
    if LEGACY_JSON_PATH.stat().st_size > LEGACY_JSON_MAX_BYTES:
        raise ValueError(f"Legacy JSON слишком большой: {LEGACY_JSON_PATH}")

    raw = await asyncio.to_thread(LEGACY_JSON_PATH.read_bytes)
    data = orjson.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(
            f"Корень JSON-хранилища должен быть объектом: {LEGACY_JSON_PATH}"
        )

    def as_dict(value: object) -> dict:
        return value if isinstance(value, dict) else {}

    users_rows = []
    for user_id, user in as_dict(data.get("users")).items():
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            logger.warning("Пропускаю пользователя с некорректным ID {!r}", user_id)
            continue
        if not isinstance(user, dict):
            continue
        users_rows.append(
            (
                uid,
                clean_display_name(as_text(user.get("first_name"))),
                clean_display_name(as_text(user.get("username"))),
                as_int(user.get("total_cards")),
                as_int(user.get("total_aura")),
                as_text(user.get("last_card_id"), 32),
                as_float(user.get("last_case_at")),
                as_int(user.get("cooldown_message_count")),
                dumps_json(as_dict(user.get("evolution_material_messages"))),
                dumps_json(as_dict(user.get("evolution_combo_messages"))),
            )
        )

    collection_rows = []
    for user_id, cards in as_dict(data.get("collection")).items():
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            continue
        for card_id, card in as_dict(cards).items():
            if not isinstance(card_id, str) or not card_id or len(card_id) > 32:
                continue
            if not isinstance(card, dict):
                continue
            evolutions = as_dict(card.get("evolutions"))
            collection_rows.append(
                (
                    uid,
                    card_id,
                    as_text(card.get("card_name"), 128) or "Неизвестная карта",
                    as_text(card.get("rarity"), 32) or "",
                    as_int(card.get("count")),
                    as_int(evolutions.get("bronze")),
                    as_int(evolutions.get("silver")),
                    as_int(evolutions.get("gold")),
                )
            )

    file_id_rows = [
        (url, file_id)
        for url, file_id in as_dict(data.get("image_file_ids")).items()
        if isinstance(url, str) and isinstance(file_id, str)
    ]

    await db().executemany(
        "INSERT OR IGNORE INTO users (user_id, first_name, username, total_cards, "
        "total_aura, last_card_id, last_case_at, cooldown_message_count, "
        "evolution_material_messages, evolution_combo_messages) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        users_rows,
    )
    await db().executemany(
        "INSERT OR IGNORE INTO collection (user_id, card_id, card_name, rarity, "
        "count, evo_bronze, evo_silver, evo_gold) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        collection_rows,
    )
    await db().executemany(
        "INSERT OR IGNORE INTO image_file_ids (image_url, file_id) VALUES (?, ?)",
        file_id_rows,
    )
    await db().commit()
    logger.info(
        "Migrated legacy JSON storage {}: {} users, {} collection rows, {} file_ids",
        LEGACY_JSON_PATH,
        len(users_rows),
        len(collection_rows),
        len(file_id_rows),
    )


async def migrate_sqlite_schema() -> None:
    """Добавляет недостающие поля в существующие таблицы, не удаляя данные."""
    required_columns = {
        "users": {
            "first_name": "TEXT",
            "username": "TEXT",
            "total_cards": "INTEGER NOT NULL DEFAULT 0",
            "total_aura": "INTEGER NOT NULL DEFAULT 0",
            "last_card_id": "TEXT",
            "last_case_at": "REAL NOT NULL DEFAULT 0",
            "cooldown_message_count": "INTEGER NOT NULL DEFAULT 0",
            "evolution_material_messages": "TEXT NOT NULL DEFAULT '{}'",
            "evolution_combo_messages": "TEXT NOT NULL DEFAULT '{}'",
        },
        "collection": {
            "card_name": "TEXT NOT NULL DEFAULT 'Неизвестная карта'",
            "rarity": "TEXT NOT NULL DEFAULT ''",
            "count": "INTEGER NOT NULL DEFAULT 0",
            "evo_bronze": "INTEGER NOT NULL DEFAULT 0",
            "evo_silver": "INTEGER NOT NULL DEFAULT 0",
            "evo_gold": "INTEGER NOT NULL DEFAULT 0",
        },
    }

    for table, columns in required_columns.items():
        async with db().execute(f"PRAGMA table_info({table})") as cursor:
            existing_columns = {row["name"] for row in await cursor.fetchall()}
        key_columns = {"users": {"user_id"}, "collection": {"user_id", "card_id"}}
        missing_keys = key_columns[table] - existing_columns
        if missing_keys:
            raise RuntimeError(
                f"Таблица {table!r} имеет несовместимую схему; "
                f"отсутствуют ключевые столбцы: {', '.join(sorted(missing_keys))}."
            )
        for column, definition in columns.items():
            if column not in existing_columns:
                await db().execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )
                logger.info("Added missing SQLite column {}.{}", table, column)
    await db().commit()


async def init_db() -> None:
    """Открывает SQLite, включает WAL, создаёт таблицы и мигрирует старый JSON."""
    global DB
    DB = await aiosqlite.connect(DB_PATH)
    DB.row_factory = aiosqlite.Row
    async with DB.execute("PRAGMA journal_mode=WAL") as cursor:
        await cursor.fetchone()
    await DB.execute("PRAGMA synchronous=NORMAL")
    await DB.execute("PRAGMA busy_timeout=5000")
    await DB.executescript(SCHEMA)
    await migrate_sqlite_schema()
    await DB.commit()
    await migrate_legacy_json()
    harden_storage_permissions()


async def close_db() -> None:
    """Закрывает соединение с базой."""
    global DB
    if DB is not None:
        await DB.close()
        DB = None


# --- users ---

async def get_user(user_id: int) -> dict | None:
    """Возвращает строку пользователя или None."""
    async with db().execute(
        f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def save_user(user: User) -> dict:
    """Создаёт или обновляет запись пользователя и возвращает её."""
    first_name = clean_display_name(user.first_name)
    username = clean_display_name(user.username)
    await db().execute(
        "INSERT INTO users (user_id, first_name, username) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "first_name = excluded.first_name, username = excluded.username "
        "WHERE users.first_name IS NOT excluded.first_name "
        "OR users.username IS NOT excluded.username",
        (user.id, first_name, username),
    )
    user_data = await get_user(user.id)
    if user_data is None:
        raise RuntimeError(f"Не удалось создать пользователя {user.id}.")
    return user_data


async def set_cooldown_message_count(user_id: int, count: int) -> None:
    """Обновляет счётчик сообщений во время КД."""
    await db().execute(
        "UPDATE users SET cooldown_message_count = ? WHERE user_id = ?",
        (count, user_id),
    )


async def record_case(
    user_id: int, aura_reward: int, card_id: str, opened_at: float
) -> None:
    """Фиксирует открытую карту: счётчики, аура, последняя карта и время."""
    await db().execute(
        "UPDATE users SET total_cards = total_cards + 1, "
        "total_aura = total_aura + ?, last_card_id = ?, last_case_at = ?, "
        "cooldown_message_count = 0 WHERE user_id = ?",
        (aura_reward, card_id, opened_at, user_id),
    )


async def get_user_materials(user_id: int) -> dict | None:
    """Загружает учёт сообщений-материалов эво (или None, если нет игрока)."""
    async with db().execute(
        "SELECT evolution_material_messages, evolution_combo_messages "
        "FROM users WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    combos = loads_json_dict(row["evolution_combo_messages"])
    return {
        "evolution_material_messages": normalize_materials(
            loads_json_dict(row["evolution_material_messages"])
        ),
        "evolution_combo_messages": {
            key: value
            for key, value in combos.items()
            if isinstance(key, str) and isinstance(value, int)
        },
    }


async def save_user_materials(user_id: int, user_data: dict) -> None:
    """Сохраняет учёт сообщений-материалов эво."""
    await db().execute(
        "UPDATE users SET evolution_material_messages = ?, "
        "evolution_combo_messages = ? WHERE user_id = ?",
        (
            dumps_json(user_data.get("evolution_material_messages", {})),
            dumps_json(user_data.get("evolution_combo_messages", {})),
            user_id,
        ),
    )


async def append_evolution_material(
    owner_id: int, material_tier: str, entry: dict
) -> None:
    """Добавляет сообщение карты в список материалов указанного уровня."""
    async with write_transaction():
        user_data = await get_user_materials(owner_id)
        if user_data is None:
            raise ValueError(f"Missing user data for evolution owner {owner_id}.")
        materials = user_data.setdefault("evolution_material_messages", {})
        tier_list = materials.setdefault(material_tier, [])
        tier_list.append(entry)
        del tier_list[:-MAX_MATERIALS_PER_TIER]  # храним только последние N
        await save_user_materials(owner_id, user_data)


# --- collection ---

def collection_row_to_card(row: aiosqlite.Row) -> dict:
    """Преобразует строку коллекции в привычный словарь карты."""
    return {
        "card_name": row["card_name"],
        "rarity": row["rarity"],
        "count": row["count"],
        "evolutions": {
            "bronze": row["evo_bronze"],
            "silver": row["evo_silver"],
            "gold": row["evo_gold"],
        },
    }


async def get_collection(user_id: int, rarity: str | None = None) -> dict[str, dict]:
    """Возвращает коллекцию игрока (опционально — только одной редкости)."""
    if rarity is None:
        sql, params = SQL_COLLECTION_ALL, (user_id,)
    else:
        if rarity not in RARITIES:
            raise ValueError(f"Неизвестная редкость: {rarity!r}")
        sql, params = SQL_COLLECTION_BY_RARITY, (user_id, rarity)
    async with db().execute(sql, params) as cursor:
        rows = await cursor.fetchall()
    return {row["card_id"]: collection_row_to_card(row) for row in rows}


async def get_collection_card(user_id: int, card_id: str) -> dict | None:
    """Возвращает одну карту из коллекции игрока или None."""
    async with db().execute(
        f"SELECT {COLLECTION_COLUMNS} FROM collection "
        "WHERE user_id = ? AND card_id = ?",
        (user_id, card_id),
    ) as cursor:
        row = await cursor.fetchone()
    return collection_row_to_card(row) if row is not None else None


async def insert_collection_card(
    user_id: int, card_id: str, card_name: str, rarity: str
) -> None:
    """Добавляет новую карту в коллекцию с количеством 1."""
    await db().execute(
        "INSERT INTO collection (user_id, card_id, card_name, rarity, count) "
        "VALUES (?, ?, ?, ?, 1)",
        (user_id, card_id, card_name, rarity),
    )


async def increment_card_count(user_id: int, card_id: str) -> None:
    """Увеличивает количество повторок карты на 1."""
    await db().execute(
        "UPDATE collection SET count = count + 1 WHERE user_id = ? AND card_id = ?",
        (user_id, card_id),
    )


async def update_collection_card(user_id: int, card_id: str, card: dict) -> None:
    """Точечно обновляет количество и счётчики эво одной карты."""
    evolutions = card.get("evolutions", {})
    values = (
        card.get("count", 0),
        evolutions.get("bronze", 0),
        evolutions.get("silver", 0),
        evolutions.get("gold", 0),
    )
    if min(values) < 0:  # страховка от двойного списания материалов
        raise ValueError(f"Отрицательный счётчик у карты {card_id} игрока {user_id}")
    await db().execute(
        "UPDATE collection SET count = ?, evo_bronze = ?, evo_silver = ?, "
        "evo_gold = ? WHERE user_id = ? AND card_id = ?",
        (*values, user_id, card_id),
    )


# --- image_file_ids ---

async def fetch_file_id(image_url: str) -> str | None:
    """Читает закэшированный Telegram file_id (без собственного лока)."""
    async with db().execute(
        "SELECT file_id FROM image_file_ids WHERE image_url = ?", (image_url,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["file_id"] if row is not None else None


async def store_file_id(image_url: str, file_id: str) -> None:
    """Сохраняет file_id картинки."""
    async with write_transaction():
        await db().execute(
            "INSERT INTO image_file_ids (image_url, file_id) VALUES (?, ?) "
            "ON CONFLICT(image_url) DO UPDATE SET file_id = excluded.file_id",
            (image_url, file_id),
        )


async def drop_file_id(image_url: str) -> None:
    """Удаляет отвергнутый Telegram file_id картинки."""
    async with write_transaction():
        await db().execute(
            "DELETE FROM image_file_ids WHERE image_url = ?", (image_url,)
        )


# --------------------------------------------------------------------------- #
# Бизнес-логика
# --------------------------------------------------------------------------- #

def choose_card() -> tuple[str, dict[str, str]]:
    """Выбирает гарантированную карту или случайную карту по редкости."""
    if GUARANTEED_CARD_ID is not None:
        guaranteed_card = CARDS_BY_ID.get(GUARANTEED_CARD_ID)
        if guaranteed_card is None:
            raise RuntimeError(
                f"Guaranteed card {GUARANTEED_CARD_ID!r} is missing from CARDS_BY_ID."
            )
        return guaranteed_card

    available = [
        (rarity, card)
        for rarity, cards in CARDS_BY_RARITY.items()
        for card in cards
    ]
    if not available:
        raise RuntimeError("В CARDS_BY_RARITY не добавлено ни одной карты.")

    available_rarities = {rarity for rarity, _ in available}
    rarity = random.choices(
        list(available_rarities),
        weights=[RARITIES[key]["weight"] for key in available_rarities],
        k=1,
    )[0]
    return rarity, random.choice(CARDS_BY_RARITY[rarity])


def display_card_id(card_id: str, rarity: str) -> str:
    """Преобразует внутренний ID карты в пользовательский формат."""
    internal_prefix = RARITY_ID_PREFIXES.get(rarity)
    display_prefix = USER_RARITY_ID_PREFIXES.get(rarity)
    if internal_prefix and display_prefix and card_id.startswith(internal_prefix):
        number = card_id[len(internal_prefix):]
        if number.isdigit():
            return f"{display_prefix}{number}"
    logger.warning(
        "Cannot format display ID for card {!r} with rarity {!r}", card_id, rarity
    )
    return card_id


def custom_emoji(emoji_id: str, fallback: str) -> str:
    """Формирует HTML-разметку Telegram для пользовательского эмодзи."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


NEW_CARD_EMOJI_COMBO = "".join(
    custom_emoji(emoji_id, "✨") for emoji_id in NEW_CARD_EMOJI_IDS
)
REPEAT_CARD_EMOJI_COMBO = "".join(
    custom_emoji(emoji_id, "✨") for emoji_id in REPEAT_CARD_EMOJI_IDS
)
BRONZE_EVOLUTION_EMOJI_COMBO = "".join(
    custom_emoji(emoji_id, "✨") for emoji_id in BRONZE_EVOLUTION_EMOJI_IDS
)
SILVER_EVOLUTION_EMOJI_COMBO = "".join(
    custom_emoji(emoji_id, "✨") for emoji_id in SILVER_EVOLUTION_EMOJI_IDS
)
GOLD_EVOLUTION_EMOJI_COMBO = "".join(
    custom_emoji(emoji_id, "✨") for emoji_id in GOLD_EVOLUTION_EMOJI_IDS
)
EVOLUTION_EMOJI_COMBOS = {
    "bronze": BRONZE_EVOLUTION_EMOJI_COMBO,
    "silver": SILVER_EVOLUTION_EMOJI_COMBO,
    "gold": GOLD_EVOLUTION_EMOJI_COMBO,
}


def rarity_emoji(rarity_key: str) -> str:
    """Возвращает custom emoji редкости или её стандартный символ."""
    rarity = RARITIES.get(rarity_key)
    if rarity is None:
        return ""
    emoji_id = rarity.get("custom_emoji_id")
    if not emoji_id:
        return rarity["emoji"]
    return custom_emoji(emoji_id, rarity["emoji"])


def evolution_counts(card_data: dict) -> dict[str, int]:
    """Добавляет недостающие счётчики уровней эво и возвращает их."""
    counts = card_data.setdefault("evolutions", {})
    for tier in EVOLUTION_TIER_ORDER:
        counts.setdefault(tier, 0)
    return counts


def next_evolution_tier(card_data: dict, evolution_config: dict) -> str | None:
    """Определяет доступный уровень эво по имеющимся материалам."""
    counts = evolution_counts(card_data)
    if counts["silver"] >= 2 and "gold" in evolution_config:
        return "gold"
    if counts["bronze"] >= 2 and "silver" in evolution_config:
        return "silver"
    if card_data.get("count", 0) >= 2 and "bronze" in evolution_config:
        return "bronze"
    return None


def highest_evolution_tier(card_data: dict, evolution_config: dict) -> str | None:
    """Находит самый высокий уровень эво, имеющийся у карты."""
    counts = evolution_counts(card_data)
    for tier in reversed(EVOLUTION_TIER_ORDER):
        if counts[tier] > 0 and tier in evolution_config:
            return tier
    return None


def evolution_keyboard(owner_id: int, card_id: str) -> InlineKeyboardMarkup:
    """Создаёт кнопку улучшения карты с подходящей подписью и emoji."""
    card_entry = CARDS_BY_ID.get(card_id)
    is_rule34 = (
        card_entry is not None
        and card_entry[1]["name"].casefold() == "hog cards rule34"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Улучшить ;3" if is_rule34 else "Улучшить",
                    callback_data=f"evo_create:{owner_id}:{card_id}",
                    icon_custom_emoji_id=(
                        "5325604415900504150"
                        if is_rule34
                        else "5136422685743516465"
                    ),
                )
            ]
        ]
    )


def build_card_caption(
    user: User, card_id: str, rarity_key: str, aura_reward: int
) -> str:
    """Формирует подпись обычной или повторно выпавшей карты."""
    rarity = RARITIES[rarity_key]
    decoration = "".join(
        custom_emoji(CARD_DECORATION_EMOJI_ID, "✨") for _ in range(7)
    )
    if user.username:
        display_user = f"@{html.escape(user.username, quote=False)}"
    else:
        safe_name = html.escape(clean_display_name(user.full_name), quote=False)
        display_user = (
            f'<a href="tg://user?id={user.id}">\u2068{safe_name}\u2069</a>'
        )
    aura_emoji = custom_emoji(NEW_CARD_AURA_EMOJI_ID, "✨")
    return (
        f"{custom_emoji(CARD_AUTHOR_EMOJI_ID, '👤')} • {display_user}\n\n"
        f"{decoration}\n"
        f"<b>{custom_emoji(CARD_NUMBER_EMOJI_ID, '🆔')} • Номер карты: "
        f"<code>{display_card_id(card_id, rarity_key)}</code>\n"
        f"{custom_emoji(CARD_RARITY_EMOJI_ID, '✨')} • Редкость: "
        f"{rarity['name']} {rarity_emoji(rarity_key)}\n"
        f"{aura_emoji} • Рост ауры: {aura_reward}</b>"
    )


def build_evolution_caption(
    card_id: str,
    rarity_key: str,
    tier: str,
    multiplier: float,
    aura_amount: int,
) -> str:
    """Собирает подпись эво с её ID, редкостью и множителем ауры."""
    evolution_suffix = EVOLUTION_TIER_ID_SUFFIXES[tier]
    return (
        f"{custom_emoji('5161157196242749683', '✨')} /// "
        "<b>КАРТА УЛУЧШЕНА!</b>\n\n"
        f"<b>{custom_emoji('5136522238790469903', '🆔')} • Номер карты: "
        f"<code>{display_card_id(card_id, rarity_key)}E{evolution_suffix}</code>\n"
        f"{custom_emoji('5136607618445347119', '✨')} • Редкость: "
        f"{RARITIES[rarity_key]['name']} {rarity_emoji(rarity_key)}\n"
        f"{custom_emoji('5136834787855566612', '✨')} • Аура умножена "
        f"в {multiplier:g} раза ({aura_amount})</b>"
    )


def format_cooldown(seconds: int) -> str:
    """Показывает оставшееся время КД в часах и минутах."""
    total_minutes = (seconds + 59) // 60
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} ч. {minutes} мин."


def format_cooldown_clock(seconds: int) -> str:
    """Преобразует секунды в формат таймера ЧЧ:ММ:СС."""
    hours, remainder = divmod(max(seconds, 0), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


# --------------------------------------------------------------------------- #
# Загрузка изображений (curl, allowlist, LRU-кэш, семафор)
# --------------------------------------------------------------------------- #

class ImageDownloadError(RuntimeError):
    pass


def validate_image_url(url: str) -> None:
    """Только https, порт 443, хост из allowlist, без userinfo."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise ImageDownloadError("Некорректный URL изображения.") from error
    if parts.scheme != "https" or port not in (None, 443):
        raise ImageDownloadError("Разрешены только https-URL на порту 443.")
    if parts.username or parts.password:
        raise ImageDownloadError("URL с учётными данными запрещён.")
    if (parts.hostname or "").lower() not in ALLOWED_IMAGE_HOSTS:
        raise ImageDownloadError(f"Хост не в allowlist: {parts.hostname!r}")


def cache_get(url: str) -> bytes | None:
    data = CARD_IMAGE_CACHE.get(url)
    if data is not None:
        CARD_IMAGE_CACHE.move_to_end(url)
    return data


def cache_put(url: str, data: bytes) -> None:
    """LRU-кэш с лимитом по суммарному размеру в байтах."""
    global CARD_IMAGE_CACHE_BYTES
    if len(data) > IMAGE_CACHE_MAX_BYTES:
        return
    old = CARD_IMAGE_CACHE.pop(url, None)
    if old is not None:
        CARD_IMAGE_CACHE_BYTES -= len(old)
    CARD_IMAGE_CACHE[url] = data
    CARD_IMAGE_CACHE_BYTES += len(data)
    while CARD_IMAGE_CACHE_BYTES > IMAGE_CACHE_MAX_BYTES:
        _, evicted = CARD_IMAGE_CACHE.popitem(last=False)
        CARD_IMAGE_CACHE_BYTES -= len(evicted)


async def _fetch_image(url: str) -> bytes:
    """Скачивает изображение через curl, ограничивая время, размер и редиректы."""
    validate_image_url(url)
    executable = shutil.which("curl.exe" if os.name == "nt" else "curl")
    if executable is None:
        raise ImageDownloadError("Команда curl не найдена в PATH.")

    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "--fail",
            "--location",
            "--max-redirs",
            str(IMAGE_MAX_REDIRECTS),
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--silent",
            "--show-error",
            "--max-time",
            str(IMAGE_DOWNLOAD_TIMEOUT_SECONDS),
            "--max-filesize",
            str(IMAGE_MAX_BYTES),
            "--output",
            "-",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        image_data, error_output = await process.communicate()
    except OSError as error:
        raise ImageDownloadError(f"Не удалось запустить curl: {error}") from error

    if process.returncode != 0:
        details = error_output.decode("utf-8", errors="replace").strip()
        raise ImageDownloadError(
            details or f"curl завершился с кодом {process.returncode}."
        )
    if len(image_data) > IMAGE_MAX_BYTES:
        raise ImageDownloadError("Изображение превышает лимит в 10 МБ.")
    return image_data


async def download_card_image(url: str) -> bytes:
    """Скачивает через curl: allowlist, LRU-кэш, семафор и проверка формата."""
    cached = cache_get(url)
    if cached is not None:
        return cached

    lock = CARD_IMAGE_DOWNLOAD_LOCKS.setdefault(url, asyncio.Lock())
    async with lock:
        cached = cache_get(url)
        if cached is not None:
            return cached

        async with IMAGE_DOWNLOAD_SEMAPHORE:
            image_data = await _fetch_image(url)

        if not image_data.startswith(IMAGE_MAGIC_BYTES):
            raise ImageDownloadError("Ссылка не вернула PNG- или JPEG-изображение.")

        cache_put(url, image_data)
        return image_data


def cooldown_keyboard(
    seconds: int, user_id: int, ready: bool = False
) -> InlineKeyboardMarkup:
    """Создаёт кнопку с таймером КД или статусом «Готово»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        "Готово"
                        if ready
                        else f"КД {format_cooldown_clock(seconds)}"
                    ),
                    callback_data=f"case_cd:{user_id}",
                    icon_custom_emoji_id=COOLDOWN_EMOJI_ID,
                )
            ]
        ]
    )


async def send_photo_with_retry(
    message: Message,
    photo: str | BufferedInputFile,
    caption: str | None,
    reply_markup: InlineKeyboardMarkup | None = None,
    has_spoiler: bool = False,
    reply_to_message: bool = False,
) -> Message:
    """Отправляет фото и повторяет запрос при временной ошибке Telegram."""
    for attempt in range(3):
        try:
            send_photo = (
                message.reply_photo if reply_to_message else message.answer_photo
            )
            return await send_photo(
                photo,
                caption=caption,
                reply_markup=reply_markup,
                has_spoiler=has_spoiler,
            )
        except TelegramBadRequest as error:
            if attempt == 2:
                raise
            delay = 2 ** (attempt + 1)
            logger.warning(
                "Photo send attempt {} failed; retrying in {} seconds: {}",
                attempt + 1,
                delay,
                error,
            )
            await asyncio.sleep(delay)


# --------------------------------------------------------------------------- #
# Хендлеры
# --------------------------------------------------------------------------- #

@dp.message(Command("start"))
async def start_command(message: Message) -> None:
    """Отправляет приветствие, изображение и кнопки старта."""
    async with write_transaction():
        if message.from_user is not None:
            await save_user(message.from_user)
        cached_file_id = await fetch_file_id(START_IMAGE_URL)

    bot_info = await bot.get_me()
    if not bot_info.username:
        raise RuntimeError("Telegram не вернул username бота.")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Добавить к себе",
                    url=f"https://t.me/{bot_info.username}?startgroup=true",
                    icon_custom_emoji_id="5258362837411045098",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Вступить в наш чат",
                    url=START_CHAT_URL,
                    icon_custom_emoji_id="5258260149037965799",
                )
            ],
        ]
    )
    rarity_lines = "\n".join(
        f"{rarity_emoji(rarity_key)} {label} ({chance})"
        for rarity_key, label, chance in (
            ("common", "Обычные", "55%"),
            ("rare", "Редкие", "25%"),
            ("universal", "Универсал", "12%"),
            ("epic", "Эпические", "5%"),
            ("mythic", "Мифические", "2%"),
            ("legendary", "Легендарные", "0,70%"),
            ("royal", "Царские", "0,25%"),
            ("xeno", "Ксенокарты", "0,05%"),
        )
    )
    caption = (
        f"{custom_emoji(START_WELCOME_EMOJI_ID, '👋')} • "
        '<b>Добро пожаловать в "Hog Cards: Rebirth"</b>\n\n'
        f"{custom_emoji(START_HELP_EMOJI_ID, '❓')} • "
        "<i>Чтобы узнать, какие команды есть в боте, откройте команду</i> /help\n\n"
        "<b>Редкости, которые бывают у карт:</b>\n"
        f"<blockquote expandable><i>{rarity_lines}</i></blockquote>\n\n"
        f"<b>Желаем вам удачи!</b> {custom_emoji(START_LUCK_EMOJI_ID, '🍀')}"
    )

    if cached_file_id:
        try:
            await send_photo_with_retry(
                message,
                cached_file_id,
                caption,
                reply_markup=keyboard,
                reply_to_message=True,
            )
        except TelegramBadRequest as error:
            logger.warning(
                "Cached Telegram welcome-image file_id was rejected: {}", error
            )
            await drop_file_id(START_IMAGE_URL)
        else:
            return

    try:
        image_data = await download_card_image(START_IMAGE_URL)
    except ImageDownloadError as error:
        logger.error(
            "Could not download welcome image {}: {}", START_IMAGE_URL, error
        )
        await message.reply(
            f"{caption}\n\n"
            "Не удалось загрузить изображение приветствия.",
            reply_markup=keyboard,
        )
        return

    filename = START_IMAGE_URL.split("?", 1)[0].rsplit("/", 1)[-1] or "welcome.png"
    try:
        sent_message = await send_photo_with_retry(
            message,
            BufferedInputFile(image_data, filename=filename),
            caption,
            reply_markup=keyboard,
            reply_to_message=True,
        )
    except TelegramBadRequest:
        logger.exception(
            "Telegram could not upload welcome image {}", START_IMAGE_URL
        )
        await message.reply(
            f"{caption}\n\n"
            "Не удалось отправить изображение приветствия.",
            reply_markup=keyboard,
        )
        return

    if not sent_message.photo:
        logger.error(
            "Telegram response contained no photo for welcome image {}",
            START_IMAGE_URL,
        )
        await message.reply(
            f"{caption}\n\n"
            "Telegram не вернул отправленное изображение.",
            reply_markup=keyboard,
        )
        return

    await store_file_id(START_IMAGE_URL, sent_message.photo[-1].file_id)


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    """Показывает список команд и краткое описание эволюций."""
    await message.reply(
        f"{custom_emoji(HELP_TITLE_EMOJI_ID, '📖')} • <b>Доступные команды</b>\n\n"
        f"{custom_emoji(HELP_COMMAND_EMOJI_ID, '▫️')} /pig — Получить карту (кд. 3 ч.)\n"
        f"{custom_emoji(HELP_COMMAND_EMOJI_ID, '▫️')} /cards — Посмотреть коллекцию\n"
        f"{custom_emoji(HELP_COMMAND_EMOJI_ID, '▫️')} /card <code>[айди]</code> — Ваша коллекция\n"
        f"{custom_emoji(HELP_COMMAND_EMOJI_ID, '▫️')} /profile — Профиль игрока\n"
        f"{custom_emoji(HELP_COMMAND_EMOJI_ID, '▫️')} /ping — Проверка работы\n"
        f"{custom_emoji(HELP_COMMAND_EMOJI_ID, '▫️')} /top — Список лучших игроков\n\n"
        f"{custom_emoji(EVOLUTION_TITLE_EMOJI_ID, '✨')} <b>Эволюционирование</b>\n\n"
        f"{custom_emoji(EVOLUTION_TEXT_EMOJI_ID, '✨')} "
        "<i>Эволюции делятся на три фазы:</i>\n"
        "<u>Бронзовая, серебряная и золотая.</u>\n\n"
        "Прокачка возможна при первых трёх повторках по желанию игрока.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Дисклеймер",
                        callback_data="help_disclaimer",
                        icon_custom_emoji_id="5275969776668134187",
                    )
                ]
            ]
        ),
    )


@dp.callback_query(F.data == "help_disclaimer")
async def help_disclaimer_callback(callback: CallbackQuery) -> None:
    """Отправляет изображение дисклеймера в ответ на кнопку из /help."""
    message = callback.message
    if message is None:
        await callback.answer("Не удалось открыть дисклеймер.", show_alert=True)
        return

    sent_message: Message | None = None
    cached_file_id = await fetch_file_id(HELP_DISCLAIMER_IMAGE_URL)
    if cached_file_id:
        try:
            sent_message = await send_photo_with_retry(
                message,
                cached_file_id,
                None,
                reply_to_message=True,
            )
        except TelegramBadRequest as error:
            logger.warning("Cached disclaimer image file_id was rejected: {}", error)
            await drop_file_id(HELP_DISCLAIMER_IMAGE_URL)

    if sent_message is None:
        try:
            image_data = await download_card_image(HELP_DISCLAIMER_IMAGE_URL)
        except ImageDownloadError as error:
            logger.error(
                "Could not download disclaimer image {}: {}",
                HELP_DISCLAIMER_IMAGE_URL,
                error,
            )
            await callback.answer("Не удалось загрузить дисклеймер.", show_alert=True)
            return

        filename = HELP_DISCLAIMER_IMAGE_URL.rsplit("/", 1)[-1] or "disclaimer.png"
        try:
            sent_message = await send_photo_with_retry(
                message,
                BufferedInputFile(image_data, filename=filename),
                None,
                reply_to_message=True,
            )
        except TelegramBadRequest:
            logger.exception(
                "Telegram could not upload disclaimer image {}",
                HELP_DISCLAIMER_IMAGE_URL,
            )
            await callback.answer("Не удалось отправить дисклеймер.", show_alert=True)
            return

    if not sent_message.photo:
        logger.error(
            "Telegram response contained no disclaimer image for {}",
            HELP_DISCLAIMER_IMAGE_URL,
        )
        await callback.answer("Telegram не вернул изображение.", show_alert=True)
        return
    await store_file_id(
        HELP_DISCLAIMER_IMAGE_URL,
        sent_message.photo[-1].file_id,
    )
    await callback.answer()
    await asyncio.sleep(10)
    try:
        await sent_message.delete()
    except TelegramBadRequest as error:
        logger.warning("Could not delete disclaimer image after 10 seconds: {}", error)


@dp.message(Command("tatare"))
async def tatare_command(message: Message) -> None:
    """Загружает и отправляет изображение шуточной команды."""
    try:
        image_data = await download_card_image(TATARE_IMAGE_URL)
    except ImageDownloadError as error:
        logger.error("Could not download /tatare image {}: {}", TATARE_IMAGE_URL, error)
        await message.reply("Не удалось загрузить картинку для команды /tatare.")
        return

    filename = TATARE_IMAGE_URL.rsplit("/", 1)[-1] or "tatare.png"
    try:
        await message.reply_photo(
            BufferedInputFile(image_data, filename=filename)
        )
    except TelegramBadRequest:
        logger.exception("Telegram could not send /tatare image {}", TATARE_IMAGE_URL)
        await message.reply("Не удалось отправить картинку для команды /tatare.")


@dp.message(Command("aura"))
async def aura_command(message: Message) -> None:
    """Показывает пользователю его ауру и количество открытых карт."""
    user = message.from_user
    if user is None:
        return

    async with write_transaction():
        user_data = await save_user(user)

    await message.reply(
        f"✨ Аура: <b>{user_data['total_aura']}</b>\n"
        f"🃏 Открыто карт: <b>{user_data['total_cards']}</b>"
    )


def cards_rarity_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Создаёт кнопки выбора категории карт по редкости."""
    labels = {
        "common": "Обычные",
        "rare": "Редкие",
        "universal": "Универсал",
        "epic": "Эпические",
        "mythic": "Мифические",
        "legendary": "Легендарные",
        "royal": "Царские",
        "xeno": "Ксенокарты",
    }
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=labels[rarity_key],
                    callback_data=f"cards_rarity:{rarity_key}:{user_id}",
                    icon_custom_emoji_id=rarity["custom_emoji_id"],
                )
            ]
            for rarity_key, rarity in RARITIES.items()
        ]
    )


def cards_menu_text(collection: dict, user_data: dict) -> str:
    """Формирует сводку коллекции, количества карт и ауры."""
    total_cards = 0
    for card in collection.values():
        total_cards += as_int(card.get("count", 0))
        evolutions = card.get("evolutions", {})
        if isinstance(evolutions, dict):
            total_cards += sum(as_int(v) for v in evolutions.values())
    return (
        f"{custom_emoji('5159263416312924571', '📦')} • "
        "<b>Ваша коллекция</b>\n\n"
        f"{custom_emoji('5136436369509320534', '👤')} • "
        "<i>Выберите редкость</i>\n\n"
        f"{custom_emoji('5280962371207077415', '🗂')} "
        f"Уникальных карт: {len(collection)}\n"
        f"{custom_emoji('5260379144167890225', '🃏')} "
        f"Всего карт: {total_cards}\n"
        f"{custom_emoji('5136834787855566612', '✨')} "
        f"Аура: {user_data.get('total_aura', 0)}"
    )


@dp.message(Command("cards"))
async def cards_command(message: Message) -> None:
    """Открывает пользователю меню его коллекции карт."""
    user = message.from_user
    if user is None:
        return

    async with write_transaction():
        user_data = await save_user(user)
        collection = await get_collection(user.id)

    if not collection:
        await message.reply("Коллекция пока пуста. Открыть карту: /case")
        return

    await message.reply(
        cards_menu_text(collection, user_data),
        reply_markup=cards_rarity_keyboard(user.id),
    )


@dp.callback_query(F.data.startswith("cards_rarity:"))
async def show_cards_by_rarity(callback: CallbackQuery) -> None:
    """Показывает карты выбранной редкости владельца коллекции."""
    if callback.data is None:
        return

    try:
        _, rarity_key, owner_id_text = callback.data.split(":", 2)
        owner_id = int(owner_id_text)
    except ValueError:
        await callback.answer("Не удалось открыть эту категорию.", show_alert=True)
        return

    if callback.from_user.id != owner_id:
        await callback.answer("Эта коллекция предназначена для другого игрока.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Не удалось открыть коллекцию.", show_alert=True)
        return
    rarity = RARITIES.get(rarity_key)
    if rarity is None:
        await callback.answer("Неизвестная редкость карты.", show_alert=True)
        return

    async with DATA_LOCK:
        collection = await get_collection(owner_id, rarity_key)
    rarity_cards = [
        (card_id, card)
        for card_id, card in collection.items()
        if card.get("rarity") == rarity_key
    ]

    if not rarity_cards:
        await callback.answer(
            f"У вас пока нет карт редкости «{rarity['name']}».",
            show_alert=True,
        )
        return

    rarity_cards.sort(key=lambda item: item[1].get("count", 0), reverse=True)
    lines = [
        f"{rarity_emoji(rarity_key)} <b>{rarity['name']} карты</b>\n"
    ]
    for card_id, card in rarity_cards[:30]:
        display_id = display_card_id(card_id, rarity_key)
        card_name = html.escape(str(card.get("card_name", "Неизвестная карта")), quote=False)
        lines.append(
            f"{rarity_emoji(rarity_key)} <b>{card_name}</b> · "
            f"<code>{display_id}</code> ×{card.get('count', 0)}"
        )
        owned_evolutions = [
            f"{EVOLUTION_TIER_LABELS[tier]} ×{count}"
            for tier, count in card.get("evolutions", {}).items()
            if tier in EVOLUTION_TIER_LABELS and count > 0
        ]
        if owned_evolutions:
            lines.append(f"    Эво: {', '.join(owned_evolutions)}")
    if len(rarity_cards) > 30:
        lines.append(f"\n<i>... и ещё {len(rarity_cards) - 30} карт</i>")
    total_count = sum(
        card.get("count", 0) + sum(card.get("evolutions", {}).values())
        for _, card in rarity_cards
    )
    lines.append(f"\nВсего карт этой редкости: <b>{total_count}</b>")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Выбрать другую редкость",
                    callback_data=f"cards_back:{owner_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Закрыть",
                    callback_data=f"cards_close:{owner_id}",
                )
            ],
        ]
    )
    await callback.message.edit_text("\n".join(lines), reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("cards_back:"))
async def back_to_cards_menu(callback: CallbackQuery) -> None:
    """Возвращает владельца категории к главному меню коллекции."""
    if callback.data is None:
        return

    try:
        owner_id = int(callback.data.partition(":")[2])
    except ValueError:
        await callback.answer("Не удалось открыть коллекцию.", show_alert=True)
        return
    if callback.from_user.id != owner_id:
        await callback.answer("Эта коллекция предназначена для другого игрока.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Не удалось открыть коллекцию.", show_alert=True)
        return

    async with DATA_LOCK:
        collection = await get_collection(owner_id)
        user_data = await get_user(owner_id)
    if user_data is None:
        await callback.answer("Не удалось загрузить данные игрока.", show_alert=True)
        return

    await callback.message.edit_text(
        cards_menu_text(collection, user_data),
        reply_markup=cards_rarity_keyboard(owner_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("cards_close:"))
async def close_cards_menu(callback: CallbackQuery) -> None:
    """Удаляет сообщение меню коллекции после проверки владельца."""
    if callback.data is None:
        return

    try:
        owner_id = int(callback.data.partition(":")[2])
    except ValueError:
        await callback.answer("Не удалось закрыть коллекцию.", show_alert=True)
        return
    if callback.from_user.id != owner_id:
        await callback.answer("Эта коллекция предназначена для другого игрока.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Не удалось закрыть коллекцию.", show_alert=True)
        return

    await callback.message.delete()
    await callback.answer()


@dp.message(Command("case", "pig"))
async def case_command(message: Message) -> None:
    """Выдаёт карту, начисляет ауру и обрабатывает время КД."""
    user = message.from_user
    if user is None:
        return

    rarity_key, card = choose_card()
    rarity = RARITIES[rarity_key]
    remaining = 0
    cooldown_message_count = 0
    evolution_config = CARD_EVOLUTIONS.get(card["name"].casefold())
    evolution_action_tier = None
    async with write_transaction():
        now = time.time()  # берём время уже под локом, чтобы оно не устарело в очереди
        user_data = await save_user(user)
        cached_file_id = await fetch_file_id(card["image_url"])
        remaining = CASE_COOLDOWN_SECONDS - (now - user_data["last_case_at"])
        if remaining > 0:
            cooldown_message_count = user_data.get("cooldown_message_count", 0) + 1
            await set_cooldown_message_count(user.id, cooldown_message_count)
        else:
            collection_card = await get_collection_card(user.id, card["id"])
            duplicate_count = collection_card["count"] if collection_card else 0
            aura_reward = rarity["aura"]
            if duplicate_count:
                multiplier = 1
                if evolution_config is not None:
                    owned_tier = highest_evolution_tier(
                        collection_card, evolution_config
                    )
                    if owned_tier is not None:
                        multiplier = evolution_config[owned_tier]["multiplier"]
                aura_reward = ceil(aura_reward * multiplier * 0.5)
            if collection_card:
                collection_card["count"] += 1
                await increment_card_count(user.id, card["id"])
            else:
                collection_card = {
                    "card_name": card["name"],
                    "rarity": rarity_key,
                    "count": 1,
                    "evolutions": {tier: 0 for tier in EVOLUTION_TIER_ORDER},
                }
                await insert_collection_card(
                    user.id, card["id"], card["name"], rarity_key
                )
            if evolution_config is not None:
                evolution_action_tier = next_evolution_tier(
                    collection_card, evolution_config
                )
            await record_case(user.id, aura_reward, card["id"], now)

    if remaining > 0:
        cooldown_image_url = COOLDOWN_IMAGE_URL
        if cooldown_message_count >= 3:
            cooldown_image_url = random.choice(COOLDOWN_RANDOM_IMAGE_URLS)
        seconds_left = int(remaining + 0.999)
        keyboard = cooldown_keyboard(seconds_left, user.id)
        try:
            image_data = await download_card_image(cooldown_image_url)
        except ImageDownloadError as error:
            logger.error(
                "Could not download cooldown image {}: {}", cooldown_image_url, error
            )
            await message.reply(
                f"⏳ До открытия следующего кейса: {format_cooldown(seconds_left)}.",
                reply_markup=keyboard,
            )
            return

        filename = cooldown_image_url.split("?", 1)[0].rsplit("/", 1)[-1]
        try:
            await message.reply_photo(
                BufferedInputFile(image_data, filename=filename or "cooldown.png"),
                reply_markup=keyboard,
            )
        except TelegramBadRequest:
            logger.exception("Telegram could not upload cooldown image {}", cooldown_image_url)
            await message.reply(
                f"До открытия следующего кейса: {format_cooldown(seconds_left)}.",
                reply_markup=keyboard,
            )
        return

    caption = build_card_caption(user, card["id"], rarity_key, aura_reward)
    card_emojis = (
        NEW_CARD_EMOJI_COMBO
        if duplicate_count == 0
        else REPEAT_CARD_EMOJI_COMBO
    )
    card_name_key = card["name"].casefold()
    has_spoiler = "rule34" in card_name_key and card_name_key not in CARD_EVOLUTIONS
    reply_markup = (
        evolution_keyboard(user.id, card["id"])
        if evolution_action_tier is not None
        else None
    )

    if cached_file_id:
        try:
            sent_message = await send_photo_with_retry(
                message,
                cached_file_id,
                caption,
                reply_markup=reply_markup,
                has_spoiler=has_spoiler,
                reply_to_message=True,
            )
        except TelegramBadRequest as error:
            logger.warning("Cached Telegram file_id was rejected: {}", error)
            await drop_file_id(card["image_url"])
        else:
            await send_card_emojis(
                message,
                card_emojis,
                sent_message,
                user.id,
                "base" if evolution_config is not None else None,
            )
            return

    try:
        image_data = await download_card_image(card["image_url"])
    except ImageDownloadError as error:
        logger.error("Could not download card image {}: {}", card["image_url"], error)
        await message.reply(f"{caption}\n🖼 Не удалось скачать изображение карты.")
        return

    filename = card["image_url"].split("?", 1)[0].rsplit("/", 1)[-1]
    if not filename:
        filename = f"{card['id']}.png"
    try:
        sent_message = await send_photo_with_retry(
            message,
            BufferedInputFile(image_data, filename=filename),
            caption,
            reply_markup=reply_markup,
            has_spoiler=has_spoiler,
            reply_to_message=True,
        )
    except TelegramBadRequest:
        logger.exception("Telegram could not upload card image {}", card["image_url"])
        await message.reply(f"{caption}\n🖼 Не удалось отправить изображение карты.")
        return

    if not sent_message.photo:
        logger.error("Telegram response contained no photo for {}", card["image_url"])
        return
    await store_file_id(card["image_url"], sent_message.photo[-1].file_id)
    await send_card_emojis(
        message,
        card_emojis,
        sent_message,
        user.id,
        "base" if evolution_config is not None else None,
    )


async def send_card_emojis(
    message: Message,
    text: str,
    card_message: Message,
    owner_id: int,
    material_tier: str | None,
) -> None:
    """Отправляет комбо эмодзи и сохраняет связь с материалом эво."""
    emoji_message = await message.answer(text)
    if material_tier is None:
        return

    await append_evolution_material(
        owner_id,
        material_tier,
        {
            "chat_id": message.chat.id,
            "card_message_id": card_message.message_id,
            "emoji_message_id": emoji_message.message_id,
        },
    )


def take_evolution_materials(
    user_data: dict,
    material_tier: str,
    needed: int,
    chat_id: int,
    current_message_id: int,
) -> list[dict]:
    """Выбирает две карты-материала и удаляет их сообщения из учёта."""
    materials = user_data.setdefault("evolution_material_messages", {})
    tier_messages = materials.setdefault(material_tier, [])
    legacy_combos = user_data.get("evolution_combo_messages", {})
    if material_tier == "base" and legacy_combos:
        for key, emoji_message_id in list(legacy_combos.items()):
            legacy_chat_id, separator, legacy_message_id = key.partition(":")
            if not separator:
                continue
            try:
                legacy_ref = {
                    "chat_id": int(legacy_chat_id),
                    "card_message_id": int(legacy_message_id),
                    "emoji_message_id": emoji_message_id,
                }
            except ValueError:
                logger.warning("Ignoring invalid legacy evolution message key {!r}", key)
                continue
            if not any(
                item.get("chat_id") == legacy_ref["chat_id"]
                and item.get("card_message_id") == legacy_ref["card_message_id"]
                for item in tier_messages
            ):
                tier_messages.append(legacy_ref)

    current_index = next(
        (
            index
            for index, item in enumerate(tier_messages)
            if item.get("chat_id") == chat_id
            and item.get("card_message_id") == current_message_id
        ),
        None,
    )
    if current_index is None:
        current_item = {
            "chat_id": chat_id,
            "card_message_id": current_message_id,
            "emoji_message_id": None,
        }
        tier_messages.append(current_item)
        current_index = len(tier_messages) - 1

    selected_indices = [current_index]
    for index in range(len(tier_messages) - 1, -1, -1):
        if index != current_index and len(selected_indices) < needed:
            selected_indices.append(index)

    selected = [dict(tier_messages[index]) for index in selected_indices]
    for item in selected:
        key = f"{item['chat_id']}:{item['card_message_id']}"
        legacy_emoji_id = legacy_combos.pop(key, None)
        if item.get("emoji_message_id") is None:
            item["emoji_message_id"] = legacy_emoji_id

    for index in sorted(selected_indices, reverse=True):
        del tier_messages[index]
    return selected


async def _create_card_evolution(callback: CallbackQuery) -> None:
    """Проверяет материалы, создаёт эво и обновляет коллекцию."""
    if callback.data is None:
        return

    try:
        _, owner_id_text, card_id = callback.data.split(":", 2)
        owner_id = int(owner_id_text)
    except ValueError:
        await callback.answer("Не удалось создать эво.", show_alert=True)
        return

    if callback.from_user.id != owner_id:
        await callback.answer("Эта кнопка предназначена для другого игрока.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Не удалось создать эво.", show_alert=True)
        return
    card_entry = CARDS_BY_ID.get(card_id)
    if card_entry is None:
        await callback.answer("Исходная карта не найдена.", show_alert=True)
        return
    rarity_key, base_card = card_entry
    evolution_config = CARD_EVOLUTIONS.get(base_card["name"].casefold())
    if evolution_config is None:
        await callback.answer("Для этой карты эволюции недоступны.", show_alert=True)
        return

    async with DATA_LOCK:
        collection_card = await get_collection_card(owner_id, card_id)
    tier = (
        next_evolution_tier(collection_card, evolution_config)
        if collection_card is not None
        else None
    )

    if tier is None:
        await callback.answer(
            "Недостаточно карт для создания эво.", show_alert=True
        )
        return

    tier_config = evolution_config[tier]
    await callback.answer(f"Создаю {EVOLUTION_TIER_LABELS[tier]} эво...")
    try:
        reveal_image = await download_card_image(tier_config["reveal_image_url"])
        evolution_image = await download_card_image(tier_config["image_url"])
    except ImageDownloadError as error:
        logger.error(
            "Could not download {} evolution images: {}", tier, error
        )
        await callback.message.answer(
            "Не удалось загрузить изображения эво. Карты не потрачены; попробуйте позже."
        )
        return

    # Повторная проверка и списание — атомарно, под локом и BEGIN IMMEDIATE.
    async with write_transaction():
        collection_card = await get_collection_card(owner_id, card_id)
        current_tier = (
            next_evolution_tier(collection_card, evolution_config)
            if collection_card is not None
            else None
        )
        if current_tier != tier:
            collection_card = None
        else:
            user_data = await get_user_materials(owner_id)
            if user_data is None:
                raise ValueError(f"Missing user data for evolution owner {owner_id}.")
            source_tier = {"bronze": "base", "silver": "bronze", "gold": "silver"}[
                tier
            ]
            source_materials = take_evolution_materials(
                user_data,
                source_tier,
                needed=2,
                chat_id=callback.message.chat.id,
                current_message_id=callback.message.message_id,
            )
            counts = evolution_counts(collection_card)
            if tier == "bronze":
                collection_card["count"] -= 2
            elif tier == "silver":
                counts["bronze"] -= 2
            else:
                counts["silver"] -= 2
            counts[tier] += 1
            next_tier = next_evolution_tier(collection_card, evolution_config)
            await update_collection_card(owner_id, card_id, collection_card)
            await save_user_materials(owner_id, user_data)

    if collection_card is None:
        await callback.message.answer(
            "Коллекция изменилась во время создания. Нажмите кнопку ещё раз."
        )
        return

    warnings = []
    clicked_material = next(
        (
            material
            for material in source_materials
            if material.get("chat_id") == callback.message.chat.id
            and material.get("card_message_id") == callback.message.message_id
        ),
        None,
    )
    if clicked_material is not None:
        emoji_message_id = clicked_material.get("emoji_message_id")
        if emoji_message_id is not None:
            try:
                await bot.delete_message(
                    chat_id=callback.message.chat.id,
                    message_id=emoji_message_id,
                )
            except TelegramBadRequest:
                logger.exception("Telegram could not delete the evolution emoji combo")
                warnings.append("Не удалось удалить комбо эмодзи карты.")

    try:
        await callback.message.delete()
    except TelegramBadRequest:
        logger.exception("Telegram could not delete the card before evolution")
        warnings.append("Не удалось удалить сообщение с картой.")

    reveal_filename = (
        tier_config["reveal_image_url"].rsplit("/", 1)[-1] or "evolution.png"
    )
    reveal_message = None
    try:
        reveal_message = await send_photo_with_retry(
            callback.message,
            BufferedInputFile(reveal_image, filename=reveal_filename),
            None,
        )
    except TelegramBadRequest:
        logger.exception("Telegram could not send {} evolution reveal", tier)
        warnings.append("Промежуточная картинка эво не отправилась.")

    await asyncio.sleep(5)
    if reveal_message is not None:
        try:
            await reveal_message.delete()
        except TelegramBadRequest:
            logger.exception("Telegram could not delete {} evolution reveal", tier)
            warnings.append("Не удалось удалить промежуточную картинку эво.")

    evolution_filename = (
        tier_config["image_url"].rsplit("/", 1)[-1] or f"{card_id}_{tier}.png"
    )
    evolved_aura = ceil(
        RARITIES[rarity_key]["aura"] * tier_config["multiplier"]
    )
    caption = build_evolution_caption(
        card_id, rarity_key, tier, tier_config["multiplier"], evolved_aura
    )
    reply_markup = (
        evolution_keyboard(owner_id, card_id)
        if next_tier is not None
        else None
    )
    evolution_message = None
    try:
        evolution_message = await send_photo_with_retry(
            callback.message,
            BufferedInputFile(evolution_image, filename=evolution_filename),
            caption,
            reply_markup=reply_markup,
            has_spoiler=True,
        )
    except TelegramBadRequest:
        logger.exception("Telegram could not send {} evolution card", tier)
        evolution_message = await callback.message.answer(
            caption, reply_markup=reply_markup
        )

    try:
        evolution_emoji_message = await callback.message.answer(
            EVOLUTION_EMOJI_COMBOS[tier]
        )
    except TelegramBadRequest:
        logger.exception("Telegram rejected the {} evolution emoji combo", tier)
        evolution_emoji_message = await callback.message.answer("✨")

    await append_evolution_material(
        owner_id,
        tier,
        {
            "chat_id": callback.message.chat.id,
            "card_message_id": evolution_message.message_id,
            "emoji_message_id": evolution_emoji_message.message_id,
        },
    )

    if warnings:
        await callback.message.answer("\n".join(warnings))


@dp.callback_query(F.data.startswith("evo_create:"))
async def create_card_evolution(callback: CallbackQuery) -> None:
    """Не даёт обработать одну и ту же кнопку «Улучшить» параллельно."""
    message = callback.message
    if message is None:
        await callback.answer("Не удалось создать эво.", show_alert=True)
        return
    key = (message.chat.id, message.message_id)
    if key in EVOLUTIONS_IN_PROGRESS:
        await callback.answer("Эво уже создаётся…")
        return
    EVOLUTIONS_IN_PROGRESS.add(key)
    try:
        await _create_card_evolution(callback)
    finally:
        EVOLUTIONS_IN_PROGRESS.discard(key)


@dp.callback_query(F.data.startswith("case_cd:"))
async def refresh_case_cooldown(callback: CallbackQuery) -> None:
    """Обновляет таймер КД или отмечает завершение ожидания."""
    if callback.data is None:
        return

    try:
        owner_id = int(callback.data.partition(":")[2])
    except ValueError:
        logger.warning("Invalid cooldown callback data: {!r}", callback.data)
        await callback.answer("Не удалось обновить таймер.", show_alert=True)
        return

    if callback.from_user.id != owner_id:
        await callback.answer("Эта кнопка предназначена для другого игрока.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Не удалось обновить таймер.", show_alert=True)
        return

    async with DATA_LOCK:
        user_data = await get_user(owner_id)
    remaining = (
        CASE_COOLDOWN_SECONDS - (time.time() - user_data["last_case_at"])
        if user_data
        else 0
    )

    if remaining <= 0:
        keyboard = cooldown_keyboard(0, owner_id, ready=True)
        current_keyboard = callback.message.reply_markup
        current_text = (
            current_keyboard.inline_keyboard[0][0].text
            if current_keyboard and current_keyboard.inline_keyboard
            else None
        )
        if current_text != "Готово":
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        await callback.answer("КД закончился. Можно открыть карту.")
        return

    seconds_left = int(remaining + 0.999)
    keyboard = cooldown_keyboard(seconds_left, owner_id)
    current_keyboard = callback.message.reply_markup
    current_text = (
        current_keyboard.inline_keyboard[0][0].text
        if current_keyboard and current_keyboard.inline_keyboard
        else None
    )
    updated_text = keyboard.inline_keyboard[0][0].text
    if current_text != updated_text:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


# --------------------------------------------------------------------------- #
# /ping, /card, /profile
# --------------------------------------------------------------------------- #

# /card доступна только для карт, которыми владеет пользователь.
CARD_COMMAND_REQUIRES_OWNERSHIP = True

DISPLAY_PREFIX_TO_RARITY = {
    prefix: rarity for rarity, prefix in USER_RARITY_ID_PREFIXES.items()
}
EVOLUTION_SUFFIX_TO_TIER = {
    suffix: tier for tier, suffix in EVOLUTION_TIER_ID_SUFFIXES.items()
}
CARD_QUERY_RE = re.compile(r"^([A-Z])(\d{1,3})(?:E([BCG]))?$")
# Кириллические двойники латинских букв (А1, К4 и т.п.)
CYRILLIC_LOOKALIKES = str.maketrans("АВСЕКХ", "ABCEKX")


# --- /ping ---

@dp.message(Command("ping"))
async def ping_command(message: Message) -> None:
    """Проверяет задержки Telegram API и SQLite, сервер и аптайм бота."""
    user = message.from_user
    if user is None:
        return

    uptime = max(0, int(time.monotonic() - BOT_START_TIME))
    uptime_days, uptime = divmod(uptime, 86400)
    uptime_hours, uptime = divmod(uptime, 3600)
    uptime_minutes, uptime_seconds = divmod(uptime, 60)
    uptime_parts = []
    if uptime_days:
        uptime_parts.append(f"{uptime_days}д")
    if uptime_hours or uptime_days:
        uptime_parts.append(f"{uptime_hours}ч")
    if uptime_minutes or uptime_hours or uptime_days:
        uptime_parts.append(f"{uptime_minutes}м")
    uptime_parts.append(f"{uptime_seconds}с")

    storage_start = time.monotonic()
    async with DATA_LOCK:
        await get_user(user.id)
    storage_latency = (time.monotonic() - storage_start) * 1000

    api_start = time.monotonic()
    test_message = await message.reply("🏓")
    send_latency = (time.monotonic() - api_start) * 1000

    edit_start = time.monotonic()
    await test_message.edit_text("🏓 Проверка соединения…")
    edit_latency = (time.monotonic() - edit_start) * 1000

    delete_start = time.monotonic()
    await test_message.delete()
    delete_latency = (time.monotonic() - delete_start) * 1000

    bot_info_start = time.monotonic()
    bot_info = await bot.get_me()
    bot_info_latency = (time.monotonic() - bot_info_start) * 1000
    bot_name = html.escape(bot_info.full_name, quote=False)
    if bot_info.username:
        bot_name += f" (@{html.escape(bot_info.username, quote=False)})"
    server_name = html.escape(socket.gethostname(), quote=False)
    server_platform = html.escape(platform.system(), quote=False)

    api_average = (
        send_latency + edit_latency + delete_latency + bot_info_latency
    ) / 4
    if api_average < 100:
        status = "🟢 Отличное"
    elif api_average < 300:
        status = "🟡 Хорошее"
    elif api_average < 600:
        status = "🟠 Среднее"
    else:
        status = "🔴 Медленное"

    await message.reply(
        "🏓 <b>Понг! Диагностика соединения</b>\n\n"
        f"📶 <b>Статус:</b> {status}\n\n"
        "📡 <b>Задержки Telegram API:</b>\n"
        f"├ 📤 Отправка сообщения: <code>{send_latency:.1f} мс</code>\n"
        f"├ ✏️ Редактирование: <code>{edit_latency:.1f} мс</code>\n"
        f"├ 🗑️ Удаление: <code>{delete_latency:.1f} мс</code>\n"
        f"├ 🤖 Информация о боте: <code>{bot_info_latency:.1f} мс</code>\n"
        f"└ 📊 Средняя задержка: <code>{api_average:.1f} мс</code>\n\n"
        "💾 <b>Хранилище:</b>\n"
        f"└ 📂 Чтение данных: <code>{storage_latency:.1f} мс</code>\n\n"
        "🤖 <b>Бот и сервер:</b>\n"
        f"├ 🏷️ Бот: <b>{bot_name}</b>\n"
        f"├ 🖥️ Сервер: <code>{server_name}</code> ({server_platform})\n"
        f"├ ⏱️ Аптайм: <code>{' '.join(uptime_parts)}</code>\n"
        f"└ 📦 aiogram: <code>{html.escape(aiogram.__version__, quote=False)}</code>",
        disable_web_page_preview=True,
    )


# --- /card ---

def parse_card_query(text: str) -> tuple[str, str | None] | None:
    """'A1' -> ('c1', None); 'K4EB' -> ('k4', 'bronze'); неизвестный ID -> None."""
    normalized = text.strip().upper().translate(CYRILLIC_LOOKALIKES)
    match = CARD_QUERY_RE.match(normalized)
    if match is None:
        return None
    prefix, number, suffix = match.groups()
    rarity = DISPLAY_PREFIX_TO_RARITY.get(prefix)
    if rarity is None:
        return None
    card_id = f"{RARITY_ID_PREFIXES[rarity]}{int(number)}"
    if card_id not in CARDS_BY_ID:
        return None
    return card_id, (EVOLUTION_SUFFIX_TO_TIER[suffix] if suffix else None)


def card_close_keyboard(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Закрыть", callback_data=f"card_close:{owner_id}"
                )
            ]
        ]
    )


async def send_card_image(
    message: Message,
    image_url: str,
    reply_markup: InlineKeyboardMarkup,
    has_spoiler: bool,
) -> bool:
    """Отправляет только фото (без подписи): сначала по file_id, затем загрузкой."""
    async with DATA_LOCK:
        cached_file_id = await fetch_file_id(image_url)

    if cached_file_id:
        try:
            await send_photo_with_retry(
                message, cached_file_id, None,
                reply_markup=reply_markup, has_spoiler=has_spoiler,
                reply_to_message=True,
            )
            return True
        except TelegramBadRequest as error:
            logger.warning("Cached file_id was rejected in /card: {}", error)
            await drop_file_id(image_url)

    try:
        image_data = await download_card_image(image_url)
    except ImageDownloadError as error:
        logger.error("Could not download card image {}: {}", image_url, error)
        return False

    filename = image_url.split("?", 1)[0].rsplit("/", 1)[-1] or "card.png"
    try:
        sent = await send_photo_with_retry(
            message,
            BufferedInputFile(image_data, filename=filename),
            None,
            reply_markup=reply_markup,
            has_spoiler=has_spoiler,
            reply_to_message=True,
        )
    except TelegramBadRequest:
        logger.exception("Telegram could not upload card image {}", image_url)
        return False

    if sent.photo:
        await store_file_id(image_url, sent.photo[-1].file_id)
    return True


@dp.message(Command("card"))
async def card_command(message: Message, command: CommandObject) -> None:
    """Показывает только фото карты по её ID и кнопку «Закрыть»."""
    user = message.from_user
    if user is None:
        return

    if not command.args:
        await message.reply("Укажите номер карты, например: <code>/card A1</code>")
        return

    parsed = parse_card_query(command.args.split()[0])
    if parsed is None:
        await message.reply(
            "Карта с таким номером не найдена. Пример: <code>/card A1</code>"
        )
        return
    card_id, evo_tier = parsed
    _, card = CARDS_BY_ID[card_id]
    name_key = card["name"].casefold()
    evolution_config = CARD_EVOLUTIONS.get(name_key)

    if evo_tier is None:
        image_url = card["image_url"]
        has_spoiler = "rule34" in name_key and name_key not in CARD_EVOLUTIONS
    else:
        if evolution_config is None or evo_tier not in evolution_config:
            await message.reply("У этой карты нет такой эволюции.")
            return
        image_url = evolution_config[evo_tier]["image_url"]
        has_spoiler = True

    if CARD_COMMAND_REQUIRES_OWNERSHIP:
        async with DATA_LOCK:
            owned = await get_collection_card(user.id, card_id)
        if owned is None or (
            evo_tier is not None and evolution_counts(owned)[evo_tier] <= 0
        ):
            await message.reply(
                f"{custom_emoji('5161591657954544631', '▫️')} • "
                "<b>У вас нет этой карточки.</b>"
            )
            return

    ok = await send_card_image(
        message, image_url, card_close_keyboard(user.id), has_spoiler
    )
    if not ok:
        await message.reply("Не удалось отправить изображение карты.")


@dp.callback_query(F.data.startswith("card_close:"))
async def close_card_view(callback: CallbackQuery) -> None:
    """Удаляет сообщение с картой (только для того, кто вызвал команду)."""
    if callback.data is None:
        return
    try:
        owner_id = int(callback.data.partition(":")[2])
    except ValueError:
        await callback.answer("Не удалось закрыть сообщение.", show_alert=True)
        return
    if callback.from_user.id != owner_id:
        await callback.answer(
            "Эта кнопка предназначена для другого игрока.", show_alert=True
        )
        return
    if callback.message is None:
        await callback.answer("Не удалось закрыть сообщение.", show_alert=True)
        return

    with suppress(TelegramBadRequest):
        await callback.message.delete()
    await callback.answer()


# --- /profile ---

async def get_aura_rank(total_aura: int) -> tuple[int, int]:
    """Место по ауре среди игроков, открывших хотя бы одну карту, и их число."""
    async with db().execute(
        "SELECT (SELECT COUNT(*) FROM users "
        "        WHERE total_cards > 0 AND total_aura > ?) + 1 AS place, "
        "       (SELECT COUNT(*) FROM users WHERE total_cards > 0) AS players",
        (total_aura,),
    ) as cursor:
        row = await cursor.fetchone()
    return row["place"], row["players"]


async def get_aura_top_page(page: int) -> tuple[list[dict], int, int]:
    """Возвращает до 15 игроков с наибольшей аурой для указанной страницы."""
    async with db().execute(
        "SELECT COUNT(*) AS player_count FROM users WHERE total_cards > 0"
    ) as cursor:
        row = await cursor.fetchone()
    player_count = row["player_count"]
    page_count = ceil(player_count / TOP_PAGE_SIZE)
    page = min(max(page, 0), max(page_count - 1, 0))
    if not player_count:
        return [], page, page_count

    async with db().execute(
        "SELECT user_id, first_name, username, total_aura FROM users "
        "WHERE total_cards > 0 "
        "ORDER BY total_aura DESC, user_id ASC LIMIT ? OFFSET ?",
        (TOP_PAGE_SIZE, page * TOP_PAGE_SIZE),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows], page, page_count


def aura_top_caption(rows: list[dict], page: int) -> str:
    """Формирует список игроков с медалями и аурой без заголовка и карт."""
    lines = []
    for rank, player in enumerate(rows, start=page * TOP_PAGE_SIZE + 1):
        name = (
            clean_display_name(player.get("first_name"))
            or clean_display_name(player.get("username"))
            or f"ID {player['user_id']}"
        )
        medal = (
            custom_emoji(
                TOP_MEDAL_EMOJI_IDS[rank - 1],
                ("🥇", "🥈", "🥉")[rank - 1],
            )
            if rank <= 3
            else "".join(
                custom_emoji(
                    TOP_RANK_DIGIT_EMOJI_IDS[digit],
                    TOP_RANK_DIGIT_EMOJI_FALLBACKS[digit],
                )
                if digit in TOP_RANK_DIGIT_EMOJI_IDS
                else digit
                for digit in str(rank)
            )
            + "."
        )
        username = clean_display_name(player.get("username"))
        if username and username.startswith("@"):
            username = username[1:]
        profile_url = (
            f"https://t.me/{html.escape(username, quote=True)}"
            if username and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username)
            else f"tg://user?id={player['user_id']}"
        )
        name_link = (
            f'<a href="{profile_url}">'
            f'{html.escape(name[:24], quote=False)}</a>'
        )
        lines.append(
            f"{medal} {name_link}\n"
            f"    {custom_emoji(TOP_AURA_EMOJI_ID, '✨')} "
            f"<b>{as_int(player.get('total_aura'))}</b> ауры"
        )
    return "\n\n".join(lines) or "Пока нет игроков с открытыми картами."


def aura_top_keyboard(page: int, page_count: int) -> InlineKeyboardMarkup | None:
    """Показывает кнопки перехода только на существующие страницы топа."""
    buttons = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton(
                text="← Назад",
                callback_data=f"top_page:{page - 1}",
            )
        )
    if page + 1 < page_count:
        buttons.append(
            InlineKeyboardButton(
                text="Вперёд →",
                callback_data=f"top_page:{page + 1}",
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None


@dp.message(Command("top"))
async def top_command(message: Message) -> None:
    """Показывает страницы рейтинга игроков по общей ауре."""
    sender = message.from_user
    if sender is None:
        return

    async with write_transaction():
        await save_user(sender)
        rows, page, page_count = await get_aura_top_page(0)
    caption = aura_top_caption(rows, page)
    keyboard = aura_top_keyboard(page, page_count)

    cached_file_id = await fetch_file_id(TOP_IMAGE_URL)
    if cached_file_id:
        try:
            await send_photo_with_retry(
                message,
                cached_file_id,
                caption,
                reply_markup=keyboard,
                reply_to_message=True,
            )
            return
        except TelegramBadRequest as error:
            logger.warning("Cached /top image file_id was rejected: {}", error)
            await drop_file_id(TOP_IMAGE_URL)

    try:
        image_data = await download_card_image(TOP_IMAGE_URL)
    except ImageDownloadError as error:
        logger.error("Could not download /top image {}: {}", TOP_IMAGE_URL, error)
        await message.reply(
            caption,
            reply_markup=keyboard,
        )
        return

    filename = TOP_IMAGE_URL.rsplit("/", 1)[-1] or "top.png"
    try:
        sent_message = await send_photo_with_retry(
            message,
            BufferedInputFile(image_data, filename=filename),
            caption,
            reply_markup=keyboard,
            reply_to_message=True,
        )
    except TelegramBadRequest:
        logger.exception("Telegram could not upload /top image {}", TOP_IMAGE_URL)
        await message.reply(
            caption,
            reply_markup=keyboard,
        )
        return
    if sent_message.photo:
        await store_file_id(TOP_IMAGE_URL, sent_message.photo[-1].file_id)


@dp.callback_query(F.data.startswith("top_page:"))
async def change_aura_top_page(callback: CallbackQuery) -> None:
    """Переключает рейтинг на выбранные 15 игроков."""
    if callback.data is None:
        return
    try:
        requested_page = int(callback.data.partition(":")[2])
    except ValueError:
        await callback.answer("Не удалось открыть эту страницу.", show_alert=True)
        return
    if requested_page < 0:
        await callback.answer("Не удалось открыть эту страницу.", show_alert=True)
        return
    if callback.message is None:
        await callback.answer("Не удалось открыть топ.", show_alert=True)
        return

    async with DATA_LOCK:
        rows, page, page_count = await get_aura_top_page(requested_page)
    await callback.message.edit_caption(
        caption=aura_top_caption(rows, page),
        reply_markup=aura_top_keyboard(page, page_count),
    )
    await callback.answer()


@dp.message(Command("profile"))
async def profile_command(message: Message) -> None:
    """Профиль игрока: свой или того, на чьё сообщение ответили командой."""
    sender = message.from_user
    if sender is None:
        return

    target = sender
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and not reply.from_user.is_bot:
        target = reply.from_user

    if target.id == sender.id:
        async with write_transaction():
            await save_user(sender)

    async with DATA_LOCK:
        user_data = await get_user(target.id)
        if user_data is None:
            collection: dict[str, dict] = {}
            place, players = 0, 0
        else:
            collection = await get_collection(target.id)
            place, players = await get_aura_rank(user_data["total_aura"])

    if user_data is None:
        await message.reply("Этот игрок ещё не пользовался ботом.")
        return

    # --- имя ---
    name = html.escape(clean_display_name(target.full_name) or "Без имени", quote=False)
    username_line = (
        f"Username: @{html.escape(target.username, quote=False)}\n"
        if target.username
        else ""
    )

    # --- коллекция ---
    total_cards = 0
    owned_by_rarity: dict[str, int] = {}
    for card in collection.values():
        total_cards += as_int(card.get("count", 0))
        total_cards += sum(as_int(v) for v in card.get("evolutions", {}).values())
        owned_by_rarity[card["rarity"]] = owned_by_rarity.get(card["rarity"], 0) + 1

    rarity_lines = "\n".join(
        f"{rarity_emoji(key)} {rarity['name']}: "
        f"{owned_by_rarity.get(key, 0)}/{len(CARDS_BY_RARITY[key])}"
        for key, rarity in RARITIES.items()
    )

    # --- последняя карта ---
    last_entry = CARDS_BY_ID.get(user_data.get("last_card_id") or "")
    if last_entry is not None:
        last_rarity, last_card = last_entry
        last_card_text = (
            f"{html.escape(last_card['name'], quote=False)} "
            f"(<code>{display_card_id(last_card['id'], last_rarity)}</code>)"
        )
    else:
        last_card_text = "—"

    # --- кд ---
    remaining = CASE_COOLDOWN_SECONDS - (time.time() - user_data["last_case_at"])
    cooldown_text = (
        "можно открывать"
        if remaining <= 0
        else format_cooldown_clock(int(remaining + 0.999))
    )
    place_text = f"#{place} из {players}" if user_data["total_cards"] > 0 else "—"

    await message.reply(
        f"{custom_emoji('5136436369509320534', '👤')} • <b>Профиль игрока</b>\n\n"
        f"Имя: <b>{name}</b>\n"
        f"{username_line}"
        f"ID: <code>{target.id}</code>\n\n"
        f"{custom_emoji('5136834787855566612', '✨')} Аура: "
        f"<b>{user_data['total_aura']}</b>\n"
        f"{custom_emoji('5260379144167890225', '🃏')} Открыто карт: "
        f"<b>{user_data['total_cards']}</b>\n"
        f"{custom_emoji('5280962371207077415', '📦')} Уникальных: "
        f"<b>{len(collection)}/{len(CARDS_BY_ID)}</b>\n"
        f"Карт в коллекции: <b>{total_cards}</b>\n"
        f"Место по ауре: <b>{place_text}</b>\n\n"
        f"Последняя карта: {last_card_text}\n"
        f"Следующая карта: <b>{cooldown_text}</b>\n\n"
        f"<b>По редкостям:</b>\n"
        f"<blockquote expandable>{rarity_lines}</blockquote>"
    )


# --------------------------------------------------------------------------- #
# Троттлинг и запуск
# --------------------------------------------------------------------------- #

class ThrottleMiddleware(BaseMiddleware):
    """Не чаще одного действия от пользователя за `interval` секунд."""

    def __init__(self, interval: float = 0.7, max_entries: int = 10_000) -> None:
        self.interval = interval
        self.max_entries = max_entries
        self._last: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        last = self._last.get(user.id)
        if last is not None and now - last < self.interval:
            if isinstance(event, CallbackQuery):
                with suppress(TelegramBadRequest):
                    await event.answer()  # убрать «часики» на кнопке
            return None

        self._last[user.id] = now
        if len(self._last) > self.max_entries:
            cutoff = now - self.interval
            self._last = {uid: ts for uid, ts in self._last.items() if ts > cutoff}
        return await handler(event, data)


async def main() -> None:
    """Инициализирует хранилище и запускает polling бота."""
    await init_db()
    dp.message.outer_middleware(ThrottleMiddleware(0.7))
    dp.callback_query.outer_middleware(ThrottleMiddleware(0.7))
    try:
        await dp.start_polling(bot)
    finally:
        await close_db()
        await logger.complete()


def run() -> None:
    """Запускает main() под uvloop (если он доступен на платформе)."""
    if uvloop is None:
        asyncio.run(main())
    elif sys.version_info >= (3, 12):
        asyncio.run(main(), loop_factory=uvloop.new_event_loop)
    else:
        uvloop.install()
        asyncio.run(main())


if __name__ == "__main__":
    run()
