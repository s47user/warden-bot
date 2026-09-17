import os
import re
import html
import random
import logging
import asyncio
import datetime
import unicodedata
import time
import math
from collections import deque
from typing import Optional, Any

from dotenv import load_dotenv
import aiosqlite

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.error import RetryAfter
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ChatMemberHandler,
    MessageHandler,
    filters,
    BaseRateLimiter,
)

# ---------------------------------------------------------------------------
# Configuration & Logging
# ---------------------------------------------------------------------------

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID", "0").strip() or 0)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0").strip() or 0)
BAIL_THRESHOLD = int(os.getenv("BAIL_THRESHOLD", "3").strip() or 3)
CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT", "90").strip() or 90)
RAID_THRESHOLD = int(os.getenv("RAID_THRESHOLD", "5").strip() or 5)
RAID_WINDOW = int(os.getenv("RAID_WINDOW", "10").strip() or 10)
LOCKDOWN_DURATION = int(os.getenv("LOCKDOWN_DURATION", "300").strip() or 300)
DB_FILE = os.getenv("DB_FILE", "group_moderator.db").strip()

def db_connect():
    """Returns an aiosqlite connection with 5-second busy timeout enabled for concurrency."""
    return aiosqlite.connect(DB_FILE, timeout=5.0)

logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("GroupModeratorBot")

# ---------------------------------------------------------------------------
# Telegram API Flood Control & Production Rate Limiter
# ---------------------------------------------------------------------------

class SlidingWindowLimiter:
    """Thread-safe sliding window rate limiter using monotonic timestamps."""
    def __init__(self, max_rate: float, time_period: float):
        self.max_rate = max_rate
        self.time_period = time_period
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        if self.max_rate <= 0 or self.time_period <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self.time_period
                while self._timestamps and self._timestamps[0] <= cutoff:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.max_rate:
                    self._timestamps.append(time.monotonic())
                    return

                sleep_time = (self._timestamps[0] + self.time_period) - now

            # Release lock before sleeping so other coroutines can check/acquire
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)


class ProductionRateLimiter(BaseRateLimiter):
    """
    Zero-dependency production rate limiter subclassing PTB's BaseRateLimiter.
    Throttles global bot throughput (default: 25 req/s) and group-specific requests (default: 20 req/60s).
    Catches Telegram RetryAfter flood exceptions and pauses outgoing traffic with exponential backoff.
    """
    def __init__(
        self,
        overall_max_rate: float = 25.0,
        overall_time_period: float = 1.0,
        group_max_rate: float = 20.0,
        group_time_period: float = 60.0,
        max_retries: int = 3,
    ):
        super().__init__()
        self.overall_limiter = SlidingWindowLimiter(overall_max_rate, overall_time_period)
        self.group_max_rate = group_max_rate
        self.group_time_period = group_time_period
        self.group_limiters: dict[Any, SlidingWindowLimiter] = {}
        self.max_retries = max_retries
        self._retry_after_event = asyncio.Event()
        self._retry_after_event.set()
        self._dict_lock = asyncio.Lock()

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def _get_group_limiter(self, group_id: Any) -> SlidingWindowLimiter:
        async with self._dict_lock:
            if len(self.group_limiters) > 1024:
                now = time.monotonic()
                cutoff = now - self.group_time_period
                keys_to_remove = [
                    k for k, v in self.group_limiters.items()
                    if not v._timestamps or v._timestamps[-1] < cutoff
                ]
                for k in keys_to_remove:
                    self.group_limiters.pop(k, None)

            if group_id not in self.group_limiters:
                self.group_limiters[group_id] = SlidingWindowLimiter(
                    self.group_max_rate, self.group_time_period
                )
            return self.group_limiters[group_id]

    async def process_request(
        self,
        callback,
        args,
        kwargs,
        endpoint: str,
        data: dict[str, Any],
        rate_limit_args: Optional[int] = None,
    ):
        max_retries = rate_limit_args if rate_limit_args is not None else self.max_retries
        chat_id = data.get("chat_id")
        group_id = None
        if chat_id is not None:
            try:
                chat_id_int = int(chat_id)
                if chat_id_int < 0:
                    group_id = chat_id_int
            except (ValueError, TypeError):
                if isinstance(chat_id, str):
                    group_id = chat_id

        for attempt in range(max_retries + 1):
            await self._retry_after_event.wait()
            # Enforce group rate limit first (longer window)
            if group_id is not None and self.group_max_rate > 0:
                limiter = await self._get_group_limiter(group_id)
                await limiter.acquire()
            # Enforce global bot throttle immediately before dispatching
            await self.overall_limiter.acquire()

            try:
                return await callback(*args, **kwargs)
            except RetryAfter as exc:
                if attempt >= max_retries:
                    logger.error("Rate limit retry budget exceeded (%d) for endpoint '%s': %s", max_retries, endpoint, exc)
                    raise
                sleep_dur = float(exc.retry_after) + 0.1
                logger.warning("Flood control hit on '%s'. Retrying after %.2f seconds (attempt %d/%d)...", endpoint, sleep_dur, attempt + 1, max_retries)
                self._retry_after_event.clear()
                try:
                    await asyncio.sleep(sleep_dur)
                finally:
                    self._retry_after_event.set()


# State & In-memory caches
active_captchas: dict = {}                        # {(chat_id, user_id): dict}
admin_cache: dict[int, dict] = {}                 # {chat_id: {"admins": set[user_id], "expires": float}}
join_tracker: dict[int, list[float]] = {}         # {chat_id: [timestamps]}
lockdown_state: dict[int, float] = {}             # {chat_id: lockdown_until_timestamp}
chat_settings_cache: dict[int, tuple] = {}        # {chat_id: (settings_dict, expiry_monotonic)}
SETTINGS_CACHE_TTL = 300.0                        # Seconds before a cached settings entry expires

DEFAULT_SETTINGS = {
    "captcha_enabled": 1,
    "captcha_timeout": CAPTCHA_TIMEOUT if CAPTCHA_TIMEOUT > 0 else 90,
    "anti_raid_enabled": 1,
    "raid_threshold": RAID_THRESHOLD if RAID_THRESHOLD > 0 else 5,
    "raid_window": RAID_WINDOW if RAID_WINDOW > 0 else 10,
    "lockdown_duration": LOCKDOWN_DURATION if LOCKDOWN_DURATION > 0 else 300,
    "channel_shield_enabled": 1,
    "spam_filter_enabled": 1,
    "bail_threshold": BAIL_THRESHOLD if BAIL_THRESHOLD > 0 else 3,
    "log_channel_id": LOG_CHANNEL_ID,
    "midnight_report_enabled": 1,
}

# Recognizable challenge pairs: (Emoji, English Label)
CAPTCHA_CHALLENGES = [
    ("🐶", "Dog"),
    ("🐱", "Cat"),
    ("🚗", "Car"),
    ("🍎", "Apple"),
    ("⚽", "Soccer Ball"),
    ("🍕", "Pizza"),
    ("⭐", "Star"),
    ("🚀", "Rocket"),
    ("✈️", "Airplane"),
    ("☕", "Coffee Cup"),
    ("🎸", "Guitar"),
    ("🌻", "Sunflower"),
    ("🍩", "Donut"),
    ("🦁", "Lion"),
    ("🚲", "Bicycle"),
]

FUNNY_QUOTES = [
    "The warden fell asleep and dropped the master keys. Run for it!",
    "Your sentence has been commuted by executive decree. Go forth and talk responsibly.",
    "Bail has been posted! The silence ends today.",
    "Freedom is yours again! Don't make us build higher prison walls tomorrow.",
    "You survived the silence chamber. Welcome back to the realm of the living!",
    "A tunnel was discovered behind the poster in cell block C. Everyone out!",
    "The parole board voted unanimously to let you loose. Don't make them regret it.",
]

SPAM_PATTERNS = [
    "t.me/", "telegram.me/", "bit.ly/", "tinyurl.com/",
    "is.gd/", "cutt.ly/", "linktr.ee/", "t.co/"
]

# ---------------------------------------------------------------------------
# Database Initialization & Daily Stats Tracking
# ---------------------------------------------------------------------------

async def init_db():
    """Initializes the SQLite database with multi-tenant tables, settings, indexes, and WAL concurrency mode."""
    db_dir = os.path.dirname(DB_FILE)
    if db_dir:
        try:
            os.makedirs(db_dir, exist_ok=True)
        except Exception as err:
            logger.error("Failed to create database directory '%s': %s", db_dir, err)

    logger.info("Connecting to database at path: '%s' (absolute: '%s')", DB_FILE, os.path.abspath(DB_FILE))

    async with db_connect() as db:
        # SQLite Concurrency & Durability Hardening
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=5000;")
        await db.execute("PRAGMA synchronous=NORMAL;")

        # 1. Chat settings table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                chat_title TEXT DEFAULT '',
                captcha_enabled INTEGER DEFAULT 1,
                captcha_timeout INTEGER DEFAULT 90,
                anti_raid_enabled INTEGER DEFAULT 1,
                raid_threshold INTEGER DEFAULT 5,
                raid_window INTEGER DEFAULT 10,
                lockdown_duration INTEGER DEFAULT 300,
                channel_shield_enabled INTEGER DEFAULT 1,
                spam_filter_enabled INTEGER DEFAULT 1,
                bail_threshold INTEGER DEFAULT 3,
                log_channel_id INTEGER DEFAULT 0,
                midnight_report_enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Check and migrate jail table if needed
        async with db.execute("PRAGMA table_info(jail)") as cur:
            jail_info = await cur.fetchall()
            pk_cols = [col[1] for col in jail_info if col[5] > 0]
            if jail_info and ("chat_id" not in pk_cols or len(pk_cols) < 2):
                logger.info("Migrating jail table to composite primary key (chat_id, user_id)...")
                await db.execute("ALTER TABLE jail RENAME TO jail_old")
                await db.execute("""
                    CREATE TABLE jail (
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        first_name TEXT,
                        username TEXT,
                        jailed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        status TEXT DEFAULT 'jailed',
                        reason TEXT DEFAULT 'manual',
                        PRIMARY KEY (chat_id, user_id)
                    )
                """)
                await db.execute("""
                    INSERT OR IGNORE INTO jail (chat_id, user_id, first_name, username, jailed_at, status, reason)
                    SELECT chat_id, user_id, first_name, username, jailed_at, status, reason FROM jail_old
                """)
                await db.execute("DROP TABLE jail_old")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS jail (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_name TEXT,
                username TEXT,
                jailed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'jailed',
                reason TEXT DEFAULT 'manual',
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        # 3. Check and migrate warnings table if needed
        async with db.execute("PRAGMA table_info(warnings)") as cur:
            warn_info = await cur.fetchall()
            pk_cols = [col[1] for col in warn_info if col[5] > 0]
            if warn_info and ("chat_id" not in pk_cols or len(pk_cols) < 2):
                logger.info("Migrating warnings table to composite primary key (chat_id, user_id)...")
                await db.execute("ALTER TABLE warnings RENAME TO warnings_old")
                await db.execute("""
                    CREATE TABLE warnings (
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        warn_count INTEGER DEFAULT 0,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (chat_id, user_id)
                    )
                """)
                await db.execute("""
                    INSERT OR IGNORE INTO warnings (chat_id, user_id, warn_count, updated_at)
                    SELECT chat_id, user_id, warn_count, updated_at FROM warnings_old
                """)
                await db.execute("DROP TABLE warnings_old")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                warn_count INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        # 4. Bail table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                prisoner_id INTEGER NOT NULL,
                voucher_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, prisoner_id, voucher_id)
            )
        """)

        # 5. Check and migrate daily_stats table if needed
        async with db.execute("PRAGMA table_info(daily_stats)") as cur:
            stats_info = await cur.fetchall()
            cols = [col[1] for col in stats_info]
            if stats_info and "chat_id" not in cols:
                logger.info("Migrating daily_stats table to include chat_id composite key...")
                await db.execute("ALTER TABLE daily_stats RENAME TO daily_stats_old")
                await db.execute("""
                    CREATE TABLE daily_stats (
                        stat_date TEXT NOT NULL,
                        chat_id INTEGER NOT NULL DEFAULT 0,
                        links_blocked INTEGER DEFAULT 0,
                        users_jailed INTEGER DEFAULT 0,
                        bails_granted INTEGER DEFAULT 0,
                        raids_blocked INTEGER DEFAULT 0,
                        PRIMARY KEY (stat_date, chat_id)
                    )
                """)
                await db.execute("""
                    INSERT OR IGNORE INTO daily_stats (stat_date, chat_id, links_blocked, users_jailed, bails_granted, raids_blocked)
                    SELECT stat_date, 0, links_blocked, users_jailed, bails_granted, raids_blocked FROM daily_stats_old
                """)
                await db.execute("DROP TABLE daily_stats_old")

        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                stat_date TEXT NOT NULL,
                chat_id INTEGER NOT NULL DEFAULT 0,
                links_blocked INTEGER DEFAULT 0,
                users_jailed INTEGER DEFAULT 0,
                bails_granted INTEGER DEFAULT 0,
                raids_blocked INTEGER DEFAULT 0,
                PRIMARY KEY (stat_date, chat_id)
            )
        """)

        # 6. Timed mutes table for process restart resilience
        await db.execute("""
            CREATE TABLE IF NOT EXISTS timed_mutes (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                unmute_at REAL NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_jail_chat_status ON jail (chat_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_warnings_chat_user ON warnings (chat_id, user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bail_chat_prisoner ON bail (chat_id, prisoner_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_stats_chat_date ON daily_stats (chat_id, stat_date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_timed_mutes_unmute ON timed_mutes (unmute_at)")
        await db.commit()
    logger.info("Database initialized successfully at '%s' (WAL mode enabled)", DB_FILE)

async def get_chat_settings(chat_id: int) -> dict:
    """Retrieves chat settings with TTL-based in-memory caching (5 min) and automatic default provisioning."""
    now = time.monotonic()
    cached = chat_settings_cache.get(chat_id)
    if cached and now < cached[1]:
        return cached[0]

    try:
        async with db_connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)) as cur:
                row = await cur.fetchone()
                if row:
                    settings = dict(row)
                    chat_settings_cache[chat_id] = (settings, now + SETTINGS_CACHE_TTL)
                    return settings

            # Provision default row for new chat
            await db.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,))
            await db.commit()
            async with db.execute("SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)) as cur:
                row = await cur.fetchone()
                settings = dict(row) if row else dict(DEFAULT_SETTINGS, chat_id=chat_id)
                chat_settings_cache[chat_id] = (settings, now + SETTINGS_CACHE_TTL)
                return settings
    except Exception as e:
        logger.error("Error retrieving settings for chat %s: %s", chat_id, e)
        return dict(DEFAULT_SETTINGS, chat_id=chat_id)

async def update_chat_setting(chat_id: int, key: str, value) -> dict:
    """Updates a chat setting in SQLite and invalidates the in-memory cache."""
    valid_keys = set(DEFAULT_SETTINGS.keys()) | {"chat_title"}
    if key not in valid_keys:
        raise ValueError(f"Invalid setting key: {key}")

    async with db_connect() as db:
        await db.execute(f"""
            INSERT INTO chat_settings (chat_id, {key}, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET {key} = excluded.{key}, updated_at = CURRENT_TIMESTAMP
        """, (chat_id, value))
        await db.commit()

    chat_settings_cache.pop(chat_id, None)
    return await get_chat_settings(chat_id)

async def increment_daily_stat(stat_column: str, amount: int = 1, chat_id: int = 0):
    """Increments a counter column for today's date in daily_stats scoped to chat_id."""
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    valid_columns = {"links_blocked", "users_jailed", "bails_granted", "raids_blocked"}
    if stat_column not in valid_columns:
        return
    try:
        async with db_connect() as db:
            await db.execute(f"""
                INSERT INTO daily_stats (stat_date, chat_id, {stat_column})
                VALUES (?, ?, ?)
                ON CONFLICT(stat_date, chat_id) DO UPDATE SET {stat_column} = {stat_column} + excluded.{stat_column}
            """, (today, chat_id, amount))
            await db.commit()
    except Exception as e:
        logger.error("Failed to increment daily stat %s for chat %s: %s", stat_column, chat_id, e)

async def get_today_stats(chat_id: int = 0) -> dict[str, int]:
    """Retrieves today's aggregated moderation statistics for a given chat."""
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    default_stats = {"links_blocked": 0, "users_jailed": 0, "bails_granted": 0, "raids_blocked": 0}
    try:
        async with db_connect() as db:
            async with db.execute(
                "SELECT links_blocked, users_jailed, bails_granted, raids_blocked FROM daily_stats WHERE stat_date = ? AND chat_id = ?",
                (today, chat_id)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return {
                        "links_blocked": row[0],
                        "users_jailed": row[1],
                        "bails_granted": row[2],
                        "raids_blocked": row[3],
                    }
    except Exception as e:
        logger.error("Failed to fetch today stats for chat %s: %s", chat_id, e)
    return default_stats

# ---------------------------------------------------------------------------
# Safe Helpers, Normalization, & Permissions
# ---------------------------------------------------------------------------

def user_mention(user_id: int, first_name: Optional[str], username: Optional[str] = None) -> str:
    """Returns safe HTML formatted user mention, escaping special HTML characters."""
    if username:
        return f"@{html.escape(username)}"
    safe_name = html.escape(first_name or "Member")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'

def sanitize_text(text: str) -> str:
    """Strips zero-width characters and normalizes Unicode homoglyphs."""
    cleaned = re.sub(r'[\u200b-\u200f\u2028-\u202f\ufeff]', '', text)
    return unicodedata.normalize('NFKD', cleaned)

def parse_duration(arg: str) -> tuple[int, str]:
    """Parses a time string like '15m', '2h', '1d' into seconds and a human label."""
    if not arg:
        return 3600, "1 hour"
    arg = arg.strip().lower()
    match = re.match(r"^(\d+)([smhd]?)$", arg)
    if not match:
        return 3600, "1 hour"
    val, unit = int(match.group(1)), match.group(2)
    if val <= 0:
        return 3600, "1 hour"

    max_seconds = 86400 * 365
    if unit == "s":
        secs = min(max(10, val), max_seconds)
        return secs, f"{secs} seconds"
    elif unit == "m":
        secs = min(val * 60, max_seconds)
        return secs, f"{val} minutes"
    elif unit == "h":
        secs = min(val * 3600, max_seconds)
        return secs, f"{val} hours"
    elif unit == "d":
        secs = min(val * 86400, max_seconds)
        return secs, f"{val} days"
    else:
        secs = min(val * 60, max_seconds)
        return secs, f"{val} minutes"

def get_mute_permissions() -> ChatPermissions:
    """Returns ChatPermissions restricting all forms of messaging."""
    return ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )

async def restore_user_permissions(bot, chat_id: int, user_id: int):
    """Restores user permissions using the group's default permissions."""
    try:
        chat = await bot.get_chat(chat_id)
        permissions = chat.permissions or ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_invite_users=True,
        )
        await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=permissions)
        logger.info("Restored permissions for user %s in chat %s", user_id, chat_id)
    except Exception as e:
        logger.error("Failed to restore permissions for user %s in chat %s: %s", user_id, chat_id, e)

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Checks admin status with in-memory TTL caching (5 minutes) to avoid 429 rate limits."""
    if not update.effective_chat:
        return False
    chat_id = update.effective_chat.id
    now = time.time()

    if chat_id in admin_cache and now < admin_cache[chat_id]["expires"]:
        return user_id in admin_cache[chat_id]["admins"]

    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        admin_ids = {admin.user.id for admin in admins}
        # Evict oldest expired entry when cache exceeds 512 chats
        if len(admin_cache) >= 512:
            oldest_key = min(admin_cache, key=lambda k: admin_cache[k]["expires"])
            admin_cache.pop(oldest_key, None)
        admin_cache[chat_id] = {
            "admins": admin_ids,
            "expires": now + 300.0  # 5-minute cache TTL
        }
        return user_id in admin_ids
    except Exception as e:
        logger.error("Error fetching chat admins for chat %s: %s", chat_id, e)
        return False

def invalidate_admin_cache(chat_id: int):
    """Flushes admin cache for a chat."""
    admin_cache.pop(chat_id, None)

async def send_audit_log(context: ContextTypes.DEFAULT_TYPE, text: str, chat_id: Optional[int] = None):
    """Dispatches moderation audit log to the chat's configured log channel or fallback LOG_CHANNEL_ID."""
    target_channel = 0
    if chat_id:
        settings = await get_chat_settings(chat_id)
        target_channel = settings.get("log_channel_id", 0)
    if not target_channel:
        target_channel = LOG_CHANNEL_ID
    if not target_channel:
        return
    try:
        await context.bot.send_message(
            chat_id=target_channel,
            text=f"📋 <b>[MOD AUDIT LOG]</b>\n{text}",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error("Failed to send audit log to %s: %s", target_channel, e)

async def check_bot_rights(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> tuple[bool, bool]:
    """Checks if the bot has required admin rights: (can_delete_messages, can_restrict_members)."""
    try:
        bot_member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if bot_member.status == ChatMemberStatus.ADMINISTRATOR:
            return (
                getattr(bot_member, "can_delete_messages", False),
                getattr(bot_member, "can_restrict_members", False),
            )
        return False, False
    except Exception as e:
        logger.debug("Failed to check bot rights in chat %s: %s", chat_id, e)
        return False, False

async def delete_message_delayed(context: ContextTypes.DEFAULT_TYPE):
    """Job callback to delete a message after a delay."""
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Gatekeeper Emoji CAPTCHA & Anti-Raid Panic Mode
# ---------------------------------------------------------------------------

async def on_user_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles new member joins with Anti-Raid detection and Emoji CAPTCHA based on chat settings."""
    result = update.chat_member
    new_member = result.new_chat_member

    old_status = result.old_chat_member.status
    new_status = new_member.status

    # True member join event: user was outside the group (LEFT or BANNED) and transitioned in.
    # Prevents triggering CAPTCHA on unmuting or resolving verification for existing members.
    is_join = (
        old_status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]
        and new_status in [ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED]
    )

    if is_join:
        user = new_member.user
        chat_id = update.effective_chat.id

        if user.is_bot:
            return

        settings = await get_chat_settings(chat_id)
        now = time.time()

        # 1. Anti-Raid Lockdown Check
        if settings.get("anti_raid_enabled", 1):
            if chat_id in lockdown_state and now < lockdown_state[chat_id]:
                # Silent immediate kick during lockdown
                try:
                    await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
                    await context.bot.unban_chat_member(chat_id=chat_id, user_id=user.id)
                    await increment_daily_stat("raids_blocked", chat_id=chat_id)
                    await send_audit_log(
                        context,
                        f"🚨 <b>Raid Join Intercepted:</b> User <code>{user.id}</code> was auto-kicked under active Lockdown.",
                        chat_id=chat_id
                    )
                except Exception as e:
                    logger.error("Failed to kick user %s during raid lockdown in chat %s: %s", user.id, chat_id, e)
                return

            # 2. Track Join Velocity
            raid_window = settings.get("raid_window", 10)
            raid_threshold = settings.get("raid_threshold", 5)
            lockdown_duration = settings.get("lockdown_duration", 300)

            recent_joins = join_tracker.get(chat_id, [])
            recent_joins = [t for t in recent_joins if now - t <= raid_window]
            recent_joins.append(now)
            join_tracker[chat_id] = recent_joins

            if len(recent_joins) >= raid_threshold:
                lockdown_state[chat_id] = now + lockdown_duration
                logger.warning("Raid detected in chat %s! Enabling lockdown for %s seconds.", chat_id, lockdown_duration)
                try:
                    await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
                    await context.bot.unban_chat_member(chat_id=chat_id, user_id=user.id)
                    await increment_daily_stat("raids_blocked", chat_id=chat_id)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "🚨 <b>ANTI-RAID LOCKDOWN ACTIVATED!</b>\n\n"
                            f"Detected abnormal join surge (&gt;{raid_threshold} joins in {raid_window}s).\n"
                            f"Strict protection enabled: new joins will be auto-kicked for the next <b>{lockdown_duration // 60} minutes</b>."
                        ),
                        parse_mode=ParseMode.HTML
                    )
                    await send_audit_log(
                        context,
                        f"🚨 <b>LOCKDOWN ACTIVATED</b> in chat <code>{chat_id}</code>. Velocity exceeded {raid_threshold} joins/{raid_window}s.",
                        chat_id=chat_id
                    )
                except Exception as e:
                    logger.error("Failed to execute raid trigger actions in chat %s: %s", chat_id, e)
                return

        # 3. Check CAPTCHA Enabled
        if not settings.get("captcha_enabled", 1):
            return

        captcha_timeout = settings.get("captcha_timeout", 90)

        # Restrict member immediately
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user.id,
                permissions=get_mute_permissions()
            )
        except Exception as e:
            logger.error("Failed to restrict new join user %s in chat %s: %s", user.id, chat_id, e)
            return

        # 4. Generate Random Emoji Challenge
        target_pair = random.choice(CAPTCHA_CHALLENGES)
        target_emoji, target_label = target_pair

        decoys = [p for p in CAPTCHA_CHALLENGES if p[0] != target_emoji]
        selected_decoys = random.sample(decoys, 3)

        choices = [target_pair] + selected_decoys
        random.shuffle(choices)

        keyboard = [
            [
                InlineKeyboardButton(
                    emoji,
                    callback_data=f"captcha:{chat_id}:{user.id}:{emoji}"
                )
                for emoji, _ in choices
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        mention = user_mention(user.id, user.first_name)
        text = (
            f"👋 Welcome, {mention}!\n\n"
            f"🛡️ <b>Anti-Bot Verification</b>\n"
            f"Please click the <b>{target_label}</b> ({target_emoji}) button below within "
            f"<b>{captcha_timeout} seconds</b> to unlock chat access."
        )

        try:
            sent_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error("Failed to send CAPTCHA message for user %s: %s", user.id, e)
            return

        captcha_data = {
            "target": target_emoji,
            "chat_id": chat_id,
            "message_id": sent_msg.message_id,
        }
        # Cancel any previous captcha timeout job for this user in this chat
        for job in context.job_queue.get_jobs_by_name(f"captcha_{chat_id}_{user.id}"):
            job.schedule_removal()

        active_captchas[(chat_id, user.id)] = captcha_data

        context.job_queue.run_once(
            check_captcha_timeout,
            when=captcha_timeout,
            data={"user_id": user.id, "chat_id": chat_id, "message_id": sent_msg.message_id, "timeout": captcha_timeout},
            name=f"captcha_{chat_id}_{user.id}"
        )

async def handle_captcha_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles emoji clicks for verification."""
    query = update.callback_query

    parts = query.data.split(":")
    if len(parts) == 4:
        _, chat_id_str, target_user_id_str, selected_emoji = parts
        chat_id = int(chat_id_str)
        target_user_id = int(target_user_id_str)
    elif len(parts) == 3:
        _, target_user_id_str, selected_emoji = parts
        chat_id = update.effective_chat.id
        target_user_id = int(target_user_id_str)
    else:
        await query.answer()
        return

    if query.from_user.id != target_user_id:
        await query.answer("⚠️ This verification prompt is not for you.", show_alert=True)
        return

    challenge = active_captchas.get((chat_id, target_user_id))
    if not challenge:
        await query.answer("Verification session expired.", show_alert=True)
        return

    for job in context.job_queue.get_jobs_by_name(f"captcha_{chat_id}_{target_user_id}"):
        job.schedule_removal()

    target_emoji = challenge["target"]
    message_id = challenge["message_id"]
    active_captchas.pop((chat_id, target_user_id), None)

    if selected_emoji == target_emoji:
        await query.answer("✅ Verification successful!")
        await restore_user_permissions(context.bot, chat_id, target_user_id)
        mention = user_mention(target_user_id, query.from_user.first_name)
        await query.message.edit_text(
            f"✅ <b>Verified!</b> Welcome to the community, {mention}.",
            parse_mode=ParseMode.HTML
        )
        context.job_queue.run_once(
            delete_message_delayed,
            when=5,
            data={"chat_id": chat_id, "message_id": message_id}
        )
    else:
        await query.answer("❌ Incorrect selection. You have been removed.", show_alert=True)
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_user_id)
            await context.bot.unban_chat_member(chat_id=chat_id, user_id=target_user_id)
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            await send_audit_log(
                context,
                f"🚫 <b>CAPTCHA Failed:</b> User <code>{target_user_id}</code> picked wrong emoji and was kicked.",
                chat_id=chat_id
            )
        except Exception as e:
            logger.error("Failed to soft kick user %s on wrong captcha: %s", target_user_id, e)

async def check_captcha_timeout(context: ContextTypes.DEFAULT_TYPE):
    """Kicks users who fail to complete the CAPTCHA before the timer expires."""
    job_data = context.job.data
    user_id = job_data["user_id"]
    chat_id = job_data["chat_id"]
    message_id = job_data["message_id"]
    timeout = job_data.get("timeout", 90)

    active_captchas.pop((chat_id, user_id), None)

    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status == ChatMemberStatus.RESTRICTED and not member.can_send_messages:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            await send_audit_log(
                context,
                f"⏱️ <b>CAPTCHA Timeout:</b> User <code>{user_id}</code> failed to verify within {timeout}s and was removed.",
                chat_id=chat_id
            )
    except Exception as e:
        logger.debug("Captcha timeout check exception for user %s in chat %s: %s", user_id, chat_id, e)

# ---------------------------------------------------------------------------
# Moderation Engine (Jail, Warn, Bail, Mute, Pardon, Status)
# ---------------------------------------------------------------------------

async def jail_user_logic(user, chat_id: int, context: ContextTypes.DEFAULT_TYPE, reason: str = "manual"):
    """Applies chat mute and records jail state in database."""
    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user.id,
        permissions=get_mute_permissions()
    )

    async with db_connect() as db:
        await db.execute("DELETE FROM bail WHERE prisoner_id = ? AND chat_id = ?", (user.id, chat_id))
        await db.execute(
            """
            INSERT INTO jail (chat_id, user_id, first_name, username, status, reason, jailed_at)
            VALUES (?, ?, ?, ?, 'jailed', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                first_name=excluded.first_name,
                username=excluded.username,
                status='jailed',
                reason=excluded.reason,
                jailed_at=CURRENT_TIMESTAMP
            """,
            (chat_id, user.id, user.first_name, user.username, reason)
        )
        await db.commit()

    await increment_daily_stat("users_jailed", chat_id=chat_id)
    await send_audit_log(
        context,
        f"🚨 <b>User Incarcerated:</b> {user_mention(user.id, user.first_name, user.username)} (ID: <code>{user.id}</code>)\n"
        f"<b>Reason:</b> {html.escape(reason)}",
        chat_id=chat_id
    )

def build_bail_keyboard(user_id: int, current_vouches: int = 0, threshold: int = 3, chat_id: int = 0) -> InlineKeyboardMarkup:
    """Builds the inline keyboard with the current bail vouch counter."""
    cb_data = f"bail:{chat_id}:{user_id}" if chat_id else f"bail:{user_id}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🤝 Post Bail ({current_vouches}/{threshold})", callback_data=cb_data)]
    ])

async def jail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to immediately jail a member until midnight or bail."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    can_del, can_restrict = await check_bot_rights(context, chat_id)
    if not (can_del and can_restrict):
        await update.message.reply_text(
            "⚠️ <b>Administrator Rights Required</b>\n\n"
            "The bot requires Administrator rights (Delete Messages & Ban/Restrict Members) to enforce jail sentences.\n"
            "Please promote the bot to Admin in group settings.",
            parse_mode=ParseMode.HTML
        )
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("ℹ️ Reply to a user's message to jail them.")
        return

    target_user = update.message.reply_to_message.from_user

    if await is_admin(update, context, target_user.id):
        await update.message.reply_text("❌ Administrators cannot be jailed.")
        return

    settings = await get_chat_settings(chat_id)
    bail_threshold = settings.get("bail_threshold", 3)

    reason = " ".join(context.args) if context.args else "Admin order"
    await jail_user_logic(target_user, chat_id, context, reason=reason)
    mention = user_mention(target_user.id, target_user.first_name, target_user.username)

    text = (
        f"🚨 <b>PRISON SENTENCE!</b>\n\n"
        f"{mention} has been thrown into jail until tonight's 23:59 UTC prison break!\n"
        f"<b>Reason:</b> {html.escape(reason)}\n\n"
        f"<i>Community members can vouch below to bail them out early ({bail_threshold} vouches required).</i>"
    )
    await update.message.reply_text(
        text,
        reply_markup=build_bail_keyboard(target_user.id, 0, bail_threshold, chat_id),
        parse_mode=ParseMode.HTML
    )

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to issue a warning, jailing upon 3 warnings."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    can_del, can_restrict = await check_bot_rights(context, chat_id)
    if not (can_del and can_restrict):
        await update.message.reply_text(
            "⚠️ <b>Administrator Rights Required</b>\n\n"
            "The bot requires Administrator rights (Delete Messages & Ban/Restrict Members) to issue strikes and jail users.\n"
            "Please promote the bot to Admin in group settings.",
            parse_mode=ParseMode.HTML
        )
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("ℹ️ Reply to a user's message to warn them.")
        return

    target_user = update.message.reply_to_message.from_user

    if await is_admin(update, context, target_user.id):
        await update.message.reply_text("❌ Administrators cannot be warned.")
        return

    reason = " ".join(context.args) if context.args else "Rule violation"
    settings = await get_chat_settings(chat_id)
    bail_threshold = settings.get("bail_threshold", 3)

    async with db_connect() as db:
        async with db.execute("SELECT warn_count FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, target_user.id)) as cursor:
            row = await cursor.fetchone()
            count = (row[0] if row else 0) + 1

        await db.execute(
            """
            INSERT INTO warnings (chat_id, user_id, warn_count, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET warn_count = ?, updated_at = CURRENT_TIMESTAMP
            """,
            (chat_id, target_user.id, count, count)
        )
        await db.commit()

    mention = user_mention(target_user.id, target_user.first_name, target_user.username)

    if count >= 3:
        async with db_connect() as db:
            await db.execute("UPDATE warnings SET warn_count = 0, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ? AND user_id = ?", (chat_id, target_user.id))
            await db.commit()

        await jail_user_logic(target_user, chat_id, context, reason=f"Accumulated 3 strikes ({reason})")
        text = (
            f"⚠️ <b>STRIKE THREE!</b>\n\n"
            f"{mention} reached <b>3 warnings</b> and was sent to jail until midnight!\n"
            f"<i>Group members may vouch below to bail them out ({bail_threshold} vouches required).</i>"
        )
        await update.message.reply_text(
            text,
            reply_markup=build_bail_keyboard(target_user.id, 0, bail_threshold, chat_id),
            parse_mode=ParseMode.HTML
        )
    else:
        text = (
            f"⚠️ <b>Warning Issued!</b> {mention} has received warning <b>({count}/3)</b>.\n"
            f"<b>Reason:</b> {html.escape(reason)}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        await send_audit_log(
            context,
            f"⚠️ <b>Warning ({count}/3):</b> {mention} (ID: <code>{target_user.id}</code>)\n"
            f"<b>Reason:</b> {html.escape(reason)}",
            chat_id=chat_id
        )

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command for timed mute: /mute 15m, /mute 2h, /mute 1d (on reply)."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    can_del, can_restrict = await check_bot_rights(context, chat_id)
    if not (can_del and can_restrict):
        await update.message.reply_text(
            "⚠️ <b>Administrator Rights Required</b>\n\n"
            "The bot requires Administrator rights (Delete Messages & Ban/Restrict Members) to mute members.\n"
            "Please promote the bot to Admin in group settings.",
            parse_mode=ParseMode.HTML
        )
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("ℹ️ Reply to a user's message to mute them: <code>/mute 30m</code>", parse_mode=ParseMode.HTML)
        return

    target_user = update.message.reply_to_message.from_user

    if await is_admin(update, context, target_user.id):
        await update.message.reply_text("❌ Administrators cannot be muted.")
        return

    duration_arg = context.args[0] if context.args else "1h"
    seconds, label = parse_duration(duration_arg)
    unmute_at = time.time() + seconds

    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=target_user.id,
        permissions=get_mute_permissions()
    )

    # Persist timed mute to SQLite for restart survival
    try:
        async with db_connect() as db:
            await db.execute(
                "INSERT OR REPLACE INTO timed_mutes (chat_id, user_id, unmute_at) VALUES (?, ?, ?)",
                (chat_id, target_user.id, unmute_at)
            )
            await db.commit()
    except Exception as e:
        logger.error("Failed to persist timed mute for user %s in chat %s: %s", target_user.id, chat_id, e)

    # Cancel previous mute jobs if any
    job_name = f"timed_mute_{chat_id}_{target_user.id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    context.job_queue.run_once(
        timed_unmute_callback,
        when=seconds,
        data={"chat_id": chat_id, "user_id": target_user.id},
        name=job_name
    )

    mention = user_mention(target_user.id, target_user.first_name, target_user.username)
    await update.message.reply_text(
        f"⏳ <b>Timed Mute:</b> {mention} has been silenced for <b>{label}</b>.",
        parse_mode=ParseMode.HTML
    )
    await send_audit_log(
        context,
        f"⏳ <b>Timed Mute:</b> {mention} muted for {label} by admin <code>{update.effective_user.id}</code>.",
        chat_id=chat_id
    )

async def timed_unmute_callback(context: ContextTypes.DEFAULT_TYPE):
    """Job callback to automatically unmute user after timed mute expires."""
    data = context.job.data
    chat_id = data["chat_id"]
    user_id = data["user_id"]
    try:
        await restore_user_permissions(context.bot, chat_id, user_id)
        async with db_connect() as db:
            await db.execute("DELETE FROM timed_mutes WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
            await db.commit()
        await send_audit_log(context, f"🔓 <b>Timed Mute Expired:</b> User <code>{user_id}</code> unmuted.", chat_id=chat_id)
    except Exception as e:
        logger.error("Error executing timed_unmute_callback for user %s in chat %s: %s", user_id, chat_id, e)

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manually unmute one or multiple members."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    can_del, can_restrict = await check_bot_rights(context, chat_id)
    if not (can_del and can_restrict):
        await update.message.reply_text(
            "⚠️ <b>Administrator Rights Required</b>\n\n"
            "The bot requires Administrator rights to manage chat restrictions.",
            parse_mode=ParseMode.HTML
        )
        return

    targets = []
    if update.message.reply_to_message:
        targets.append(update.message.reply_to_message.from_user)
    elif context.args:
        for arg in context.args:
            clean_arg = arg.strip().lstrip("@")
            try:
                uid = int(clean_arg)
                member = await context.bot.get_chat_member(chat_id, uid)
                targets.append(member.user)
            except Exception:
                pass
    else:
        await update.message.reply_text(
            "ℹ️ <b>How to unmute members:</b>\n\n"
            "• Reply to any message from the user with <code>/unmute</code>\n"
            "• Or pass one or more Telegram numeric IDs:\n"
            "  <code>/unmute 123456789</code>\n"
            "  <code>/unmute 123456 987654 112233</code>",
            parse_mode=ParseMode.HTML
        )
        return

    if not targets:
        await update.message.reply_text("❌ No valid members found. Please check the user IDs.")
        return

    unmuted_mentions = []
    for target_user in targets:
        # Cancel pending timed mute job
        for job in context.job_queue.get_jobs_by_name(f"timed_mute_{chat_id}_{target_user.id}"):
            job.schedule_removal()

        # Clear from SQLite persistence
        try:
            async with db_connect() as db:
                await db.execute("DELETE FROM timed_mutes WHERE chat_id = ? AND user_id = ?", (chat_id, target_user.id))
                await db.execute("UPDATE jail SET status = 'released' WHERE chat_id = ? AND user_id = ? AND status = 'jailed'", (chat_id, target_user.id))
                await db.commit()
        except Exception as e:
            logger.error("Failed to remove timed mute record for %s: %s", target_user.id, e)

        await restore_user_permissions(context.bot, chat_id, target_user.id)
        unmuted_mentions.append(user_mention(target_user.id, target_user.first_name, target_user.username))

    if len(unmuted_mentions) == 1:
        await update.message.reply_text(f"🔊 {unmuted_mentions[0]} has been unmuted.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(
            f"🔊 <b>Unmuted {len(unmuted_mentions)} members:</b>\n" + ", ".join(unmuted_mentions),
            parse_mode=ParseMode.HTML
        )
    await send_audit_log(
        context,
        f"🔊 <b>Unmuted ({len(unmuted_mentions)} user(s)):</b> {', '.join(unmuted_mentions)} by admin <code>{update.effective_user.id}</code>.",
        chat_id=chat_id
    )

async def restore_pending_timed_mutes(application):
    """Restores pending timed mutes from SQLite on application startup."""
    try:
        now = time.time()
        async with db_connect() as db:
            async with db.execute("SELECT chat_id, user_id, unmute_at FROM timed_mutes") as cur:
                rows = await cur.fetchall()

        if not rows:
            logger.info("Mute persistence: No pending timed mutes to restore.")
            return

        restored = 0
        expired = 0
        for chat_id, user_id, unmute_at in rows:
            remaining = unmute_at - now
            if remaining <= 0:
                # Expired while bot was offline; unmute immediately
                try:
                    await restore_user_permissions(application.bot, chat_id, user_id)
                    async with db_connect() as db:
                        await db.execute("DELETE FROM timed_mutes WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
                        await db.commit()
                    expired += 1
                except Exception as e:
                    logger.warning("Failed to restore permissions for expired mute user %s in chat %s: %s", user_id, chat_id, e)
            else:
                # Still remaining; schedule job
                job_name = f"timed_mute_{chat_id}_{user_id}"
                for j in application.job_queue.get_jobs_by_name(job_name):
                    j.schedule_removal()
                application.job_queue.run_once(
                    timed_unmute_callback,
                    when=max(1.0, remaining),
                    data={"chat_id": chat_id, "user_id": user_id},
                    name=job_name
                )
                restored += 1

        logger.info("Mute persistence: Restored %d active timed mutes, resolved %d expired mutes.", restored, expired)
    except Exception as e:
        logger.error("Error restoring pending timed mutes: %s", e)

async def handle_bail_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles community bail vouches."""
    query = update.callback_query

    parts = query.data.split(":")
    if len(parts) == 3:
        _, chat_id_str, target_user_id_str = parts
        chat_id = int(chat_id_str)
        target_user_id = int(target_user_id_str)
    elif len(parts) == 2:
        target_user_id = int(parts[1])
        chat_id = update.effective_chat.id
    else:
        await query.answer()
        return

    voucher = query.from_user

    if voucher.id == target_user_id:
        await query.answer("⚖️ You cannot post bail for yourself!", show_alert=True)
        return

    settings = await get_chat_settings(chat_id)
    bail_threshold = settings.get("bail_threshold", 3)

    async with db_connect() as db:
        async with db.execute("SELECT status, first_name, username FROM jail WHERE chat_id = ? AND user_id = ?", (chat_id, target_user_id)) as cur:
            jail_entry = await cur.fetchone()
            if not jail_entry or jail_entry[0] != "jailed":
                await query.answer("This user is no longer in jail.", show_alert=True)
                return
            prisoner_name, prisoner_username = jail_entry[1], jail_entry[2]

        async with db.execute("SELECT status FROM jail WHERE chat_id = ? AND user_id = ? AND status = 'jailed'", (chat_id, voucher.id)) as cur:
            if await cur.fetchone():
                await query.answer("🚫 Inmates currently in jail cannot post bail for others!", show_alert=True)
                return

        try:
            cursor = await db.execute(
                "INSERT INTO bail (chat_id, prisoner_id, voucher_id) VALUES (?, ?, ?)",
                (chat_id, target_user_id, voucher.id)
            )
            await db.commit()
            if cursor.rowcount == 0:
                await query.answer("You have already vouched for this member.", show_alert=True)
                return
        except aiosqlite.IntegrityError:
            await query.answer("You have already vouched for this member.", show_alert=True)
            return

        async with db.execute("SELECT COUNT(*) FROM bail WHERE chat_id = ? AND prisoner_id = ?", (chat_id, target_user_id)) as cur:
            row = await cur.fetchone()
            vouch_count = row[0] if row else 0

    if vouch_count >= bail_threshold:
        await query.answer("🔓 Bail threshold reached! Member released!")
        await restore_user_permissions(context.bot, chat_id, target_user_id)

        async with db_connect() as db:
            await db.execute("UPDATE jail SET status = 'bailed' WHERE chat_id = ? AND user_id = ?", (chat_id, target_user_id))
            await db.commit()

        await increment_daily_stat("bails_granted", chat_id=chat_id)
        mention = user_mention(target_user_id, prisoner_name, prisoner_username)
        release_text = (
            f"🔓 <b>COMMUNITY BAILOUT!</b>\n\n"
            f"{mention} received <b>{vouch_count}/{bail_threshold} vouches</b> and has been released from jail early!\n"
            f"Speak responsibly and thank your benefactors."
        )
        await query.message.edit_text(release_text, parse_mode=ParseMode.HTML)
        await send_audit_log(context, f"🤝 <b>Bailout Completed:</b> {mention} released with {vouch_count} vouches.", chat_id=chat_id)
    else:
        await query.answer(f"🤝 Vouch recorded ({vouch_count}/{bail_threshold})!")
        await query.message.edit_reply_markup(
            reply_markup=build_bail_keyboard(target_user_id, vouch_count, bail_threshold, chat_id)
        )

async def pardon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to pardon and unjail an individual user immediately."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    can_del, can_restrict = await check_bot_rights(context, chat_id)
    if not (can_del and can_restrict):
        await update.message.reply_text(
            "⚠️ <b>Administrator Rights Required</b>\n\n"
            "The bot requires Administrator rights (Delete Messages & Ban/Restrict Members) to manage chat restrictions.",
            parse_mode=ParseMode.HTML
        )
        return

    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    elif context.args:
        try:
            user_id = int(context.args[0])
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            target_user = chat_member.user
        except Exception:
            await update.message.reply_text("❌ Invalid user ID or user not found.")
            return

    if not target_user:
        await update.message.reply_text("ℹ️ Reply to a user or provide their numeric ID: <code>/pardon 12345678</code>", parse_mode=ParseMode.HTML)
        return

    # Cancel any active timed mute job
    for job in context.job_queue.get_jobs_by_name(f"timed_mute_{chat_id}_{target_user.id}"):
        job.schedule_removal()

    await restore_user_permissions(context.bot, chat_id, target_user.id)

    async with db_connect() as db:
        await db.execute("DELETE FROM timed_mutes WHERE chat_id = ? AND user_id = ?", (chat_id, target_user.id))
        await db.execute("UPDATE jail SET status = 'pardoned' WHERE chat_id = ? AND user_id = ?", (chat_id, target_user.id))
        await db.commit()

    mention = user_mention(target_user.id, target_user.first_name, target_user.username)
    await update.message.reply_text(
        f"🕊️ <b>Official Pardon:</b> {mention} has been pardoned by an administrator.",
        parse_mode=ParseMode.HTML
    )
    await send_audit_log(context, f"🕊️ <b>Pardon Issued:</b> {mention} pardoned by <code>{update.effective_user.id}</code>.", chat_id=chat_id)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays a user's moderation record and warning count."""
    chat_id = update.effective_chat.id
    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    elif context.args:
        try:
            user_id = int(context.args[0])
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            target_user = chat_member.user
        except Exception:
            await update.message.reply_text("❌ User not found.")
            return
    else:
        target_user = update.effective_user

    async with db_connect() as db:
        async with db.execute("SELECT warn_count FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, target_user.id)) as cur:
            warn_row = await cur.fetchone()
            warn_count = warn_row[0] if warn_row else 0

        async with db.execute("SELECT status, jailed_at, reason FROM jail WHERE chat_id = ? AND user_id = ?", (chat_id, target_user.id)) as cur:
            jail_row = await cur.fetchone()

    mention = user_mention(target_user.id, target_user.first_name, target_user.username)

    if jail_row and jail_row[0] == "jailed":
        jail_status = f"🔴 Incarcerated (Since: {jail_row[1]} UTC)\n<b>Reason:</b> {html.escape(jail_row[2] or 'N/A')}"
    elif jail_row:
        jail_status = f"🟢 Free (Last Status: {html.escape(jail_row[0])})"
    else:
        jail_status = "🟢 Clean Record"

    text = (
        f"📋 <b>Record for {mention}</b>:\n\n"
        f"• <b>User ID:</b> <code>{target_user.id}</code>\n"
        f"• <b>Warnings:</b> {warn_count}/3\n"
        f"• <b>Prison Status:</b> {jail_status}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def clear_warns_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to clear warnings for a user."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    elif context.args:
        try:
            user_id = int(context.args[0])
            chat_member = await context.bot.get_chat_member(chat_id, user_id)
            target_user = chat_member.user
        except Exception:
            await update.message.reply_text("❌ User not found.")
            return

    if not target_user:
        await update.message.reply_text("ℹ️ Reply to a user or specify user ID to clear warnings.")
        return

    async with db_connect() as db:
        await db.execute("UPDATE warnings SET warn_count = 0, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ? AND user_id = ?", (chat_id, target_user.id))
        await db.commit()

    mention = user_mention(target_user.id, target_user.first_name, target_user.username)
    await update.message.reply_text(f"🧹 Warnings cleared for {mention}. (0/3)", parse_mode=ParseMode.HTML)
    await send_audit_log(context, f"🧹 <b>Warnings Cleared:</b> {mention} reset by <code>{update.effective_user.id}</code>.", chat_id=chat_id)

# ---------------------------------------------------------------------------
# Anti-Spam Link & Channel Impersonator Shield
# ---------------------------------------------------------------------------

async def spam_and_channel_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Filters spam links (in text and captions) and prevents anonymous channel impersonation based on chat settings."""
    if not update.message:
        return

    chat = update.effective_chat
    if not chat or chat.type in ["private"]:
        return

    settings = await get_chat_settings(chat.id)

    # 1. Anonymous Channel Impersonator Shield
    if settings.get("channel_shield_enabled", 1) and update.message.sender_chat:
        sender_chat = update.message.sender_chat
        # Allow group itself or the official linked channel
        is_group_itself = sender_chat.id == chat.id
        is_linked_channel = (getattr(chat, "linked_chat_id", None) == sender_chat.id)

        if not (is_group_itself or is_linked_channel):
            try:
                await update.message.delete()
                warn_msg = await context.bot.send_message(
                    chat_id=chat.id,
                    text="🚫 <b>Posting on behalf of an external channel is not permitted.</b>",
                    parse_mode=ParseMode.HTML
                )
                context.job_queue.run_once(
                    delete_message_delayed,
                    when=5,
                    data={"chat_id": chat.id, "message_id": warn_msg.message_id}
                )
                await send_audit_log(
                    context,
                    f"🎭 <b>Channel Post Blocked:</b> Channel <code>{sender_chat.title}</code> (ID: <code>{sender_chat.id}</code>) tried posting in chat.",
                    chat_id=chat.id
                )
            except Exception as e:
                logger.error("Failed to block channel impersonator in chat %s: %s", chat.id, e)
            return

    # Check if spam filter is enabled
    if not settings.get("spam_filter_enabled", 1):
        return

    # Ensure message has a sender user (non-channel / non-system message)
    if not update.effective_user:
        return

    # Skip administrators from link spam filtering
    if await is_admin(update, context, update.effective_user.id):
        return

    # 2. Unicode Normalized Link & Rich Entity Filter
    raw_text = update.message.text or update.message.caption or ""
    entities = list(update.message.entities or []) + list(update.message.caption_entities or [])

    normalized_text = sanitize_text(raw_text.lower())
    has_spam_url = any(pattern in normalized_text for pattern in SPAM_PATTERNS)

    if not has_spam_url:
        for ent in entities:
            if ent.type == "text_link" and ent.url:
                url_lower = ent.url.lower()
                if any(p in url_lower for p in SPAM_PATTERNS):
                    has_spam_url = True
                    break
            elif ent.type == "url":
                offset, length = ent.offset, ent.length
                extracted_url = raw_text[offset:offset+length].lower()
                if any(p in extracted_url for p in SPAM_PATTERNS):
                    has_spam_url = True
                    break

    if has_spam_url:
        try:
            await update.message.delete()
            await increment_daily_stat("links_blocked", chat_id=chat.id)
            mention = user_mention(update.effective_user.id, update.effective_user.first_name)
            warning_msg = await context.bot.send_message(
                chat_id=chat.id,
                text=f"🚫 Unauthorized link removed, {mention}!",
                parse_mode=ParseMode.HTML
            )
            context.job_queue.run_once(
                delete_message_delayed,
                when=5,
                data={"chat_id": chat.id, "message_id": warning_msg.message_id}
            )
            await send_audit_log(
                context,
                f"🛡️ <b>Link Intercepted:</b> From {mention} (ID: <code>{update.effective_user.id}</code>):\n"
                f"<code>{html.escape(raw_text[:120])}</code>",
                chat_id=chat.id
            )
        except Exception as e:
            logger.error("Failed to handle spam link in chat %s: %s", chat.id, e)

# ---------------------------------------------------------------------------
# Nightly Scheduled Batch Unfreeze & Warden's Daily Crime Report
# ---------------------------------------------------------------------------

async def execute_batch_unfreeze(bot, chat_id: int):
    """Unfreezes all jailed inmates with rate limiting and broadcasts the Warden's Daily Crime Report."""
    async with db_connect() as db:
        async with db.execute(
            "SELECT user_id, first_name, username FROM jail WHERE chat_id = ? AND status = 'jailed'",
            (chat_id,)
        ) as cursor:
            prisoners = await cursor.fetchall()

    released_mentions = []
    for user_id, first_name, username in prisoners:
        try:
            await restore_user_permissions(bot, chat_id, user_id)
            mention = user_mention(user_id, first_name, username)
            released_mentions.append(mention)
            await asyncio.sleep(0.15)  # 150ms throttle against Telegram 429
        except Exception as e:
            logger.error("Failed to unfreeze prisoner %s in chat %s: %s", user_id, chat_id, e)
            continue

    async with db_connect() as db:
        await db.execute("UPDATE jail SET status = 'released' WHERE chat_id = ? AND status = 'jailed'", (chat_id,))
        # Only remove timed mute records for users who were jailed and are now released;
        # preserve legitimate short-duration /mute entries for non-jailed members.
        if prisoners:
            prisoner_ids = [row[0] for row in prisoners]
            placeholders = ",".join("?" * len(prisoner_ids))
            await db.execute(
                f"DELETE FROM timed_mutes WHERE chat_id = ? AND user_id IN ({placeholders})",
                [chat_id] + prisoner_ids
            )
        await db.commit()

    settings = await get_chat_settings(chat_id)
    if not settings.get("midnight_report_enabled", 1):
        return

    stats = await get_today_stats(chat_id)
    quote = random.choice(FUNNY_QUOTES)

    # Chunk mentions in groups of 5
    chunk_size = 5
    tag_lines = []
    if released_mentions:
        for i in range(0, len(released_mentions), chunk_size):
            chunk = released_mentions[i:i + chunk_size]
            tag_lines.append(", ".join(chunk))
    else:
        tag_lines.append("<i>No inmates were in prison tonight! A quiet day in the city.</i>")

    report_header = (
        f"🔓 <b>PRISON BREAK! (Midnight Ritual)</b>\n\n"
        f"<i>\"{quote}\"</i>\n\n"
        f"📊 <b>Warden's Daily Crime Report:</b>\n"
        f"• Inmates Pardoned: <b>{len(released_mentions)}</b>\n"
        f"• Spam Links Intercepted: <b>{stats['links_blocked']}</b>\n"
        f"• Community Bails Granted: <b>{stats['bails_granted']}</b>\n"
        f"• Raid Incursions Deflected: <b>{stats['raids_blocked']}</b>\n\n"
        f"<b>Free to speak again:</b>\n{tag_lines[0]}"
    )

    try:
        await bot.send_message(chat_id=chat_id, text=report_header, parse_mode=ParseMode.HTML)
        for subsequent in tag_lines[1:]:
            await asyncio.sleep(0.3)
            await bot.send_message(chat_id=chat_id, text=f"Also released:\n{subsequent}", parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error("Failed to send prison break report to chat %s: %s", chat_id, e)

async def scheduled_midnight_job(context: ContextTypes.DEFAULT_TYPE):
    """Cron callback executed at 23:59 UTC daily across all active groups."""
    logger.info("Triggering scheduled 23:59 UTC midnight prison break...")
    chats_to_process = set()
    async with db_connect() as db:
        async with db.execute("SELECT DISTINCT chat_id FROM jail WHERE status = 'jailed'") as cur:
            rows = await cur.fetchall()
            for row in rows:
                chats_to_process.add(row[0])

        async with db.execute("SELECT chat_id FROM chat_settings WHERE midnight_report_enabled = 1") as cur:
            rows = await cur.fetchall()
            for row in rows:
                chats_to_process.add(row[0])

    if TARGET_GROUP_ID != 0:
        chats_to_process.add(TARGET_GROUP_ID)

    for chat_id in chats_to_process:
        try:
            await execute_batch_unfreeze(context.bot, chat_id)
        except Exception as e:
            logger.error("Failed midnight unfreeze for chat %s: %s", chat_id, e)

async def unfreeze_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to trigger immediate prison break for the current chat."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    can_del, can_restrict = await check_bot_rights(context, chat_id)
    if not can_restrict:
        await update.message.reply_text(
            "⚠️ <b>Administrator Rights Required</b>\n\n"
            "The bot requires Administrator rights (Ban/Restrict Members) to release prisoners.",
            parse_mode=ParseMode.HTML
        )
        return

    status_msg = await update.message.reply_text("⏳ Initiating emergency prison release...")
    await execute_batch_unfreeze(context.bot, chat_id)
    try:
        await status_msg.delete()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Bot Onboarding, Permissions Verification, & Interactive Settings UI
# ---------------------------------------------------------------------------

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles bot membership updates (added to group or promoted to admin)."""
    result = update.my_chat_member
    if not result:
        return
    chat = update.effective_chat
    if not chat or chat.type in ["private"]:
        return

    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status

    if new_status == ChatMemberStatus.ADMINISTRATOR:
        bot_member = result.new_chat_member
        can_delete = getattr(bot_member, "can_delete_messages", False)
        can_restrict = getattr(bot_member, "can_restrict_members", False)

        # Record chat title in database
        await update_chat_setting(chat.id, "chat_title", chat.title or "")

        if can_delete and can_restrict:
            text = (
                f"🛡️ <b>Warden Bot Activated!</b>\n\n"
                f"I have been granted Administrator permissions in <b>{html.escape(chat.title or 'this group')}</b>.\n\n"
                f"<b>Moderation Privileges:</b>\n"
                f"• Delete Messages: ✅\n"
                f"• Restrict / Ban Members: ✅\n\n"
                f"👉 <b>Group Admins:</b> Run <code>/settings</code> right here to customize your Emoji CAPTCHA, "
                f"Anti-Raid panic velocity, Channel Impersonator Shield, Community Bailouts, and Dedicated Log Channels.\n\n"
                f"Type <code>/help</code> to see the full list of commands."
            )
        else:
            text = (
                f"⚠️ <b>Warden Bot — Missing Permissions</b>\n\n"
                f"I was promoted to Administrator in <b>{html.escape(chat.title or 'this group')}</b>, but I still require essential privileges:\n\n"
                f"• Delete Messages: {'✅' if can_delete else '❌ (Required)'}\n"
                f"• Restrict / Ban Members: {'✅' if can_restrict else '❌ (Required)'}\n\n"
                f"Please update my admin rights in group settings so moderation works."
            )
        try:
            await context.bot.send_message(chat_id=chat.id, text=text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error("Failed to send admin welcome in chat %s: %s", chat.id, e)

    elif new_status == ChatMemberStatus.MEMBER and old_status != ChatMemberStatus.MEMBER:
        await update_chat_setting(chat.id, "chat_title", chat.title or "")
        text = (
            f"👋 <b>Hello {html.escape(chat.title or 'everyone')}!</b>\n\n"
            f"I am the <b>Warden Moderation Bot</b>.\n\n"
            f"To activate automated protections (Emoji CAPTCHA gatekeeper, Anti-Raid Panic Mode, and Community Bailout), "
            f"please promote me to <b>Administrator</b> with permissions to <b>Delete Messages</b> and <b>Ban Users</b>.\n\n"
            f"Once promoted, administrators can customize all settings with <code>/settings</code>."
        )
        try:
            await context.bot.send_message(chat_id=chat.id, text=text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error("Failed to send member welcome in chat %s: %s", chat.id, e)

def format_settings_text(chat_title: str, settings: dict) -> str:
    """Formats the main settings overview text."""
    captcha_status = "🟢 Enabled" if settings.get("captcha_enabled", 1) else "🔴 Disabled"
    raid_status = "🟢 Enabled" if settings.get("anti_raid_enabled", 1) else "🔴 Disabled"
    shield_status = "🟢 Enabled" if settings.get("channel_shield_enabled", 1) else "🔴 Disabled"
    spam_status = "🟢 Enabled" if settings.get("spam_filter_enabled", 1) else "🔴 Disabled"
    report_status = "🟢 Enabled" if settings.get("midnight_report_enabled", 1) else "🔴 Disabled"

    log_channel = settings.get("log_channel_id", 0)
    log_text = f"<code>{log_channel}</code>" if log_channel else "<i>None (use /setlog)</i>"

    return (
        f"⚙️ <b>Warden Bot Settings — {html.escape(chat_title or 'Supergroup')}</b>\n\n"
        f"• <b>Emoji CAPTCHA:</b> {captcha_status} ({settings.get('captcha_timeout', 90)}s)\n"
        f"• <b>Anti-Raid Panic Mode:</b> {raid_status} (&gt;{settings.get('raid_threshold', 5)}j/{settings.get('raid_window', 10)}s)\n"
        f"• <b>Channel Impersonator Shield:</b> {shield_status}\n"
        f"• <b>Link Anti-Spam:</b> {spam_status}\n"
        f"• <b>Community Bailout:</b> {settings.get('bail_threshold', 3)} vouches required\n"
        f"• <b>Midnight Crime Report:</b> {report_status} (23:59 UTC)\n"
        f"• <b>Audit Log Channel:</b> {log_text}\n\n"
        f"<i>Tap the buttons below to toggle modules or customize parameters:</i>"
    )

def build_settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Builds the main interactive settings inline keyboard."""
    captcha_btn = "🧩 CAPTCHA: 🟢" if settings.get("captcha_enabled", 1) else "🧩 CAPTCHA: 🔴"
    raid_btn = "🚨 Anti-Raid: 🟢" if settings.get("anti_raid_enabled", 1) else "🚨 Anti-Raid: 🔴"
    shield_btn = "🎭 Channel Shield: 🟢" if settings.get("channel_shield_enabled", 1) else "🎭 Channel Shield: 🔴"
    spam_btn = "🛡️ Link Filter: 🟢" if settings.get("spam_filter_enabled", 1) else "🛡️ Link Filter: 🔴"
    report_btn = "📊 Midnight: 🟢" if settings.get("midnight_report_enabled", 1) else "📊 Midnight: 🔴"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(captcha_btn, callback_data="cfg:toggle:captcha_enabled"),
            InlineKeyboardButton(f"⏱️ {settings.get('captcha_timeout', 90)}s", callback_data="cfg:menu:timeout"),
        ],
        [
            InlineKeyboardButton(raid_btn, callback_data="cfg:toggle:anti_raid_enabled"),
            InlineKeyboardButton(f"⚡ {settings.get('raid_threshold', 5)}j/{settings.get('raid_window', 10)}s", callback_data="cfg:menu:raid"),
        ],
        [
            InlineKeyboardButton(shield_btn, callback_data="cfg:toggle:channel_shield_enabled"),
            InlineKeyboardButton(spam_btn, callback_data="cfg:toggle:spam_filter_enabled"),
        ],
        [
            InlineKeyboardButton(f"🤝 Bail: {settings.get('bail_threshold', 3)} vouches", callback_data="cfg:menu:bail"),
            InlineKeyboardButton(report_btn, callback_data="cfg:toggle:midnight_report_enabled"),
        ],
        [
            InlineKeyboardButton("📋 Log Channel", callback_data="cfg:menu:logchannel"),
            InlineKeyboardButton("🔄 Refresh", callback_data="cfg:menu:main"),
        ],
        [
            InlineKeyboardButton("❌ Close Settings", callback_data="cfg:action:close"),
        ]
    ])

def build_timeout_menu(current: int) -> tuple[str, InlineKeyboardMarkup]:
    """Builds the CAPTCHA timeout selection submenu."""
    text = (
        f"⏱️ <b>Emoji CAPTCHA Timeout</b>\n\n"
        f"Current: <b>{current} seconds</b>\n\n"
        f"Choose how many seconds a newly joined user has to solve the emoji challenge before being soft-kicked:"
    )
    buttons = [
        [
            InlineKeyboardButton(f"{'✅ ' if current == 30 else ''}30s", callback_data="cfg:set:captcha_timeout:30"),
            InlineKeyboardButton(f"{'✅ ' if current == 60 else ''}60s", callback_data="cfg:set:captcha_timeout:60"),
        ],
        [
            InlineKeyboardButton(f"{'✅ ' if current == 90 else ''}90s", callback_data="cfg:set:captcha_timeout:90"),
            InlineKeyboardButton(f"{'✅ ' if current == 120 else ''}120s", callback_data="cfg:set:captcha_timeout:120"),
        ],
        [InlineKeyboardButton("⬅️ Back to Main Settings", callback_data="cfg:menu:main")]
    ]
    return text, InlineKeyboardMarkup(buttons)

def build_raid_menu(threshold: int, window: int, duration: int) -> tuple[str, InlineKeyboardMarkup]:
    """Builds the Anti-Raid sensitivity selection submenu."""
    text = (
        f"🚨 <b>Anti-Raid Panic Mode Configuration</b>\n\n"
        f"• Trigger Velocity: <b>&gt;{threshold} joins in {window} seconds</b>\n"
        f"• Panic Lockdown: <b>{duration // 60} minutes</b>\n\n"
        f"Select velocity trigger sensitivity:"
    )
    buttons = [
        [
            InlineKeyboardButton(f"{'✅ ' if threshold == 3 else ''}High (3j/10s)", callback_data="cfg:set:raid_threshold:3"),
            InlineKeyboardButton(f"{'✅ ' if threshold == 5 else ''}Normal (5j/10s)", callback_data="cfg:set:raid_threshold:5"),
            InlineKeyboardButton(f"{'✅ ' if threshold == 10 else ''}Relaxed (10j/10s)", callback_data="cfg:set:raid_threshold:10"),
        ],
        [
            InlineKeyboardButton(f"{'✅ ' if duration == 60 else ''}Lockdown 1m", callback_data="cfg:set:lockdown_duration:60"),
            InlineKeyboardButton(f"{'✅ ' if duration == 300 else ''}Lockdown 5m", callback_data="cfg:set:lockdown_duration:300"),
            InlineKeyboardButton(f"{'✅ ' if duration == 900 else ''}Lockdown 15m", callback_data="cfg:set:lockdown_duration:900"),
        ],
        [InlineKeyboardButton("⬅️ Back to Main Settings", callback_data="cfg:menu:main")]
    ]
    return text, InlineKeyboardMarkup(buttons)

def build_bail_menu(current: int) -> tuple[str, InlineKeyboardMarkup]:
    """Builds the Community Bailout threshold submenu."""
    text = (
        f"🤝 <b>Community Bailout Vouches</b>\n\n"
        f"Current: <b>{current} vouches required</b>\n\n"
        f"Number of distinct non-inmate group members who must vouch for a prisoner to grant early release:"
    )
    buttons = [
        [
            InlineKeyboardButton(f"{'✅ ' if current == 1 else ''}1 Vouch", callback_data="cfg:set:bail_threshold:1"),
            InlineKeyboardButton(f"{'✅ ' if current == 2 else ''}2 Vouches", callback_data="cfg:set:bail_threshold:2"),
        ],
        [
            InlineKeyboardButton(f"{'✅ ' if current == 3 else ''}3 Vouches", callback_data="cfg:set:bail_threshold:3"),
            InlineKeyboardButton(f"{'✅ ' if current == 5 else ''}5 Vouches", callback_data="cfg:set:bail_threshold:5"),
        ],
        [InlineKeyboardButton("⬅️ Back to Main Settings", callback_data="cfg:menu:main")]
    ]
    return text, InlineKeyboardMarkup(buttons)

def build_log_channel_menu(current_channel: int) -> tuple[str, InlineKeyboardMarkup]:
    """Builds the audit log channel submenu."""
    status_str = f"<code>{current_channel}</code>" if current_channel else "<i>Not Configured</i>"
    text = (
        f"📋 <b>Dedicated Audit Log Channel</b>\n\n"
        f"Current Log Channel: {status_str}\n\n"
        f"<b>To connect your private audit channel:</b>\n"
        f"1. Add Warden Bot to your private log channel as an <b>Administrator</b> (grant 'Post Messages' right).\n"
        f"2. In this group, run:\n"
        f"<code>/setlog &lt;channel_id&gt;</code> (e.g. <code>/setlog -1001234567890</code>)\n\n"
        f"Or click below to disconnect:"
    )
    buttons = [
        [InlineKeyboardButton("🚫 Disconnect Log Channel", callback_data="cfg:set:log_channel_id:0")],
        [InlineKeyboardButton("⬅️ Back to Main Settings", callback_data="cfg:menu:main")]
    ]
    return text, InlineKeyboardMarkup(buttons)

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to display and configure group settings."""
    chat = update.effective_chat
    user = update.effective_user

    if not chat or chat.type in ["private"]:
        await update.message.reply_text(
            "ℹ️ <b>Group Configuration</b>\n\n"
            "To configure moderation settings, add this bot to your Supergroup as an Administrator and run <code>/settings</code> inside that group.",
            parse_mode=ParseMode.HTML
        )
        return

    if not await is_admin(update, context, user.id):
        await update.message.reply_text("⚠️ Only group administrators can access and configure Warden settings.")
        return

    # Update chat title if changed
    if chat.title:
        await update_chat_setting(chat.id, "chat_title", chat.title)

    settings = await get_chat_settings(chat.id)
    text = format_settings_text(chat.title or "Supergroup", settings)
    keyboard = build_settings_keyboard(settings)

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles interactive setting toggles, submenus, and adjustments."""
    query = update.callback_query

    user = query.from_user
    chat = update.effective_chat

    if not await is_admin(update, context, user.id):
        await query.answer("⚠️ Only administrators can change group settings.", show_alert=True)
        return

    data = query.data
    parts = data.split(":")
    if len(parts) < 2:
        await query.answer()
        return

    action_type = parts[1]

    if action_type == "action" and len(parts) >= 3 and parts[2] == "close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    settings = await get_chat_settings(chat.id)
    chat_title = chat.title or "Supergroup"

    if action_type == "toggle" and len(parts) >= 3:
        key = parts[2]
        current_val = settings.get(key, 0)
        new_val = 0 if current_val else 1
        settings = await update_chat_setting(chat.id, key, new_val)
        await query.answer(f"{'🟢 Enabled' if new_val else '🔴 Disabled'}: {key.replace('_', ' ').title()}")
        text = format_settings_text(chat_title, settings)
        keyboard = build_settings_keyboard(settings)
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return

    elif action_type == "menu" and len(parts) >= 3:
        await query.answer()
        menu_name = parts[2]
        if menu_name == "main":
            text = format_settings_text(chat_title, settings)
            keyboard = build_settings_keyboard(settings)
            await query.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        elif menu_name == "timeout":
            text, keyboard = build_timeout_menu(settings.get("captcha_timeout", 90))
            await query.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        elif menu_name == "raid":
            text, keyboard = build_raid_menu(
                settings.get("raid_threshold", 5),
                settings.get("raid_window", 10),
                settings.get("lockdown_duration", 300)
            )
            await query.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        elif menu_name == "bail":
            text, keyboard = build_bail_menu(settings.get("bail_threshold", 3))
            await query.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        elif menu_name == "logchannel":
            text, keyboard = build_log_channel_menu(settings.get("log_channel_id", 0))
            await query.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return

    elif action_type == "set" and len(parts) >= 4:
        key = parts[2]
        val_str = parts[3]
        try:
            val = int(val_str)
        except ValueError:
            await query.answer("⚠️ Invalid value.", show_alert=True)
            return
        settings = await update_chat_setting(chat.id, key, val)
        await query.answer("✅ Setting updated!")
        text = format_settings_text(chat_title, settings)
        keyboard = build_settings_keyboard(settings)
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return

async def setlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to configure or disconnect audit log channel: /setlog -100xxxx or /setlog off"""
    chat = update.effective_chat
    if not chat or chat.type in ["private"]:
        await update.message.reply_text("ℹ️ Run this command inside your supergroup.")
        return

    if not await is_admin(update, context, update.effective_user.id):
        return

    if not context.args:
        settings = await get_chat_settings(chat.id)
        current_log = settings.get("log_channel_id", 0)
        current_str = f"<code>{current_log}</code>" if current_log else "<i>Not configured</i>"
        await update.message.reply_text(
            f"📋 <b>Audit Log Channel Setup</b>\n\n"
            f"Current Log Channel: {current_str}\n\n"
            f"<b>Usage:</b>\n"
            f"• <code>/setlog -1001234567890</code> — Connect a channel\n"
            f"• <code>/setlog off</code> — Disconnect log channel\n\n"
            f"<i>Make sure Warden Bot is added to the channel as an Administrator with 'Post Messages' permission before running!</i>",
            parse_mode=ParseMode.HTML
        )
        return

    arg = context.args[0].strip().lower()
    if arg in ["off", "disable", "none", "0"]:
        await update_chat_setting(chat.id, "log_channel_id", 0)
        await update.message.reply_text("✅ Audit log channel disconnected.", parse_mode=ParseMode.HTML)
        return

    try:
        channel_id = int(arg)
    except ValueError:
        await update.message.reply_text("❌ Invalid channel ID. Please provide a numeric ID (e.g. <code>/setlog -1001234567890</code>).", parse_mode=ParseMode.HTML)
        return

    # Verify bot can post to that channel
    try:
        await context.bot.send_message(
            chat_id=channel_id,
            text=(
                f"📋 <b>Audit Log Connected</b>\n\n"
                f"• <b>Group:</b> {html.escape(chat.title or 'Supergroup')} (ID: <code>{chat.id}</code>)\n"
                f"• <b>Connected By:</b> {user_mention(update.effective_user.id, update.effective_user.first_name)}"
            ),
            parse_mode=ParseMode.HTML
        )
        await update_chat_setting(chat.id, "log_channel_id", channel_id)
        await update.message.reply_text(
            f"✅ <b>Audit Log Connected!</b> Dispatched test message to channel <code>{channel_id}</code>.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error("Failed to connect log channel %s for chat %s: %s", channel_id, chat.id, e)
        await update.message.reply_text(
            f"❌ <b>Connection Failed:</b> Could not post test message to <code>{channel_id}</code>.\n\n"
            f"<b>Checklist:</b>\n"
            f"1. Is the bot added to the channel?\n"
            f"2. Is the bot promoted to <b>Administrator</b> with 'Post Messages' right?\n"
            f"3. Is the channel ID correct?\n\n"
            f"<i>Error detail: {html.escape(str(e))}</i>",
            parse_mode=ParseMode.HTML
        )

# ---------------------------------------------------------------------------
# Informational Commands: /help & /rules
# ---------------------------------------------------------------------------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Provides usage guidance for both regular members and administrators."""
    text = (
        "🤖 <b>Group Warden Bot - Guide</b>\n\n"
        "<b>Member Commands:</b>\n"
        "• <code>/status</code>: Check your warnings & incarceration status.\n"
        "• <code>/rules</code>: Read the group rules & safety guidelines.\n"
        "• <code>/help</code>: Display this command list.\n\n"
        "<b>Interactive Features:</b>\n"
        "• <b>Emoji CAPTCHA:</b> New joins must click the designated emoji within the time limit.\n"
        "• <b>Community Bailout:</b> Click <code>Post Bail</code> on a jailed user to vouch for their early release!\n\n"
        "<b>Admin Configuration:</b>\n"
        "• <code>/settings</code>: Open interactive settings dashboard to toggle CAPTCHA, Anti-Raid, Channel Shield, Bailout thresholds, etc.\n"
        "• <code>/setlog &lt;id&gt;</code>: Connect or disconnect an audit log channel.\n\n"
        "<b>Admin Moderation:</b>\n"
        "• <code>/warn [reason]</code>: Issue a strike (3 strikes = automatic jail).\n"
        "• <code>/jail [reason]</code>: Immediately jail a user until midnight or bail.\n"
        "• <code>/mute [15m/2h/1d]</code>: Timed silence with automatic expiration.\n"
        "• <code>/unmute</code>: Lift a timed mute immediately.\n"
        "• <code>/pardon</code>: Pardon and release an individual inmate.\n"
        "• <code>/clearwarns</code>: Reset warnings for a user.\n"
        "• <code>/unfreezeall</code>: Trigger emergency prison break for all inmates."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays group safety and conduct rules dynamically based on chat settings."""
    chat = update.effective_chat
    bail_vouches = 3
    captcha_time = 90
    if chat and chat.type not in ["private"]:
        settings = await get_chat_settings(chat.id)
        bail_vouches = settings.get("bail_threshold", 3)
        captcha_time = settings.get("captcha_timeout", 90)

    text = (
        f"📜 <b>Group Rules & Conduct Code</b>\n\n"
        f"1. <b>No Spam or Unauthorized Links:</b> Promotional links and URL shorteners are deleted automatically.\n"
        f"2. <b>Pass Verification:</b> All new members must solve the Emoji CAPTCHA within {captcha_time} seconds or face removal.\n"
        f"3. <b>Three Strikes Rule:</b> 3 warnings will result in incarceration until the 23:59 UTC Prison Break.\n"
        f"4. <b>Bail System:</b> Inmates can be bailed out early if {bail_vouches} community members vouch for them.\n"
        f"5. <b>Respect All Members:</b> Maintain constructive discussions and avoid harassment."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ---------------------------------------------------------------------------
# Global Error Handling & Application Entry Point
# ---------------------------------------------------------------------------

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global unhandled exception handler that logs safely without terminating the bot."""
    if isinstance(context.error, RetryAfter):
        logger.warning("Uncaught flood wait: Telegram RetryAfter %s seconds", context.error.retry_after)
        return
    logger.error("Unhandled exception processing update '%s': %s", update, context.error, exc_info=context.error)

async def post_init_hook(application) -> None:
    """Startup hook executed inside the application event loop before polling starts."""
    logger.info("Running post_init startup sequence...")
    await init_db()
    await restore_pending_timed_mutes(application)

def main():
    if not BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable is not set! Please check your .env file.")
        return

    rate_limiter = ProductionRateLimiter(
        overall_max_rate=25.0,
        overall_time_period=1.0,
        group_max_rate=20.0,
        group_time_period=60.0,
        max_retries=3,
    )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(rate_limiter)
        .post_init(post_init_hook)
        .build()
    )

    # Attach global error handler
    app.add_error_handler(global_error_handler)

    # Bot Membership & Rights Change Handler
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Chat Member Handler (New joins / CAPTCHA / Anti-Raid)
    app.add_handler(ChatMemberHandler(on_user_join, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(handle_captcha_button, pattern="^captcha:"))
    app.add_handler(CallbackQueryHandler(handle_bail_button, pattern="^bail:"))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern="^cfg:"))

    # Admin Settings Commands
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("setup", settings_command))
    app.add_handler(CommandHandler("setlog", setlog_command))

    # Moderation Commands
    app.add_handler(CommandHandler("jail", jail_command))
    app.add_handler(CommandHandler("warn", warn_command))
    app.add_handler(CommandHandler("mute", mute_command))
    app.add_handler(CommandHandler("unmute", unmute_command))
    app.add_handler(CommandHandler("pardon", pardon_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("clearwarns", clear_warns_command))
    app.add_handler(CommandHandler("unfreezeall", unfreeze_all_command))
    app.add_handler(CommandHandler("unmuteall", unfreeze_all_command))

    # General Information Commands
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("rules", rules_command))

    # Spam Auto-Filter & Anonymous Channel Impersonator Shield
    app.add_handler(MessageHandler(filters.ALL & (~filters.COMMAND), spam_and_channel_filter))

    # Daily Nightly Cron at 23:59 UTC
    midnight_time = datetime.time(hour=23, minute=59, second=0, tzinfo=datetime.timezone.utc)
    app.job_queue.run_daily(scheduled_midnight_job, time=midnight_time)
    logger.info("Midnight unfreeze scheduled daily at 23:59 UTC.")

    logger.info("Bot initialized with production rate limiting, WAL mode, and persistent timed mutes.")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
