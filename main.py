import os
import re
import html
import random
import logging
import asyncio
import datetime
import unicodedata
import time
from typing import Optional

from dotenv import load_dotenv
import aiosqlite

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    ChatMemberHandler,
    MessageHandler,
    filters,
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

logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("GroupModeratorBot")

# State & In-memory caches
active_captchas: dict[int, dict] = {}
admin_cache: dict[int, dict] = {}         # {chat_id: {"admins": set[user_id], "expires": float}}
join_tracker: dict[int, list[float]] = {} # {chat_id: [timestamps]}
lockdown_state: dict[int, float] = {}     # {chat_id: lockdown_until_timestamp}

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
    """Initializes the SQLite database with required tables, stats, and indexes."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jail (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                first_name TEXT,
                username TEXT,
                jailed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'jailed',
                reason TEXT DEFAULT 'manual'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                warn_count INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prisoner_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                voucher_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(prisoner_id, voucher_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                stat_date TEXT PRIMARY KEY,
                links_blocked INTEGER DEFAULT 0,
                users_jailed INTEGER DEFAULT 0,
                bails_granted INTEGER DEFAULT 0,
                raids_blocked INTEGER DEFAULT 0
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_jail_chat_status ON jail (chat_id, status)")
        await db.commit()
    logger.info("Database initialized successfully at '%s'", DB_FILE)

async def increment_daily_stat(stat_column: str, amount: int = 1):
    """Increments a counter column for today's date in daily_stats."""
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    valid_columns = {"links_blocked", "users_jailed", "bails_granted", "raids_blocked"}
    if stat_column not in valid_columns:
        return
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(f"""
                INSERT INTO daily_stats (stat_date, {stat_column})
                VALUES (?, ?)
                ON CONFLICT(stat_date) DO UPDATE SET {stat_column} = {stat_column} + excluded.{stat_column}
            """, (today, amount))
            await db.commit()
    except Exception as e:
        logger.error("Failed to increment daily stat %s: %s", stat_column, e)

async def get_today_stats() -> dict[str, int]:
    """Retrieves today's aggregated moderation statistics."""
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    default_stats = {"links_blocked": 0, "users_jailed": 0, "bails_granted": 0, "raids_blocked": 0}
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT links_blocked, users_jailed, bails_granted, raids_blocked FROM daily_stats WHERE stat_date = ?",
                (today,)
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
        logger.error("Failed to fetch today stats: %s", e)
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
    if unit == "s":
        return max(10, val), f"{val} seconds"
    elif unit == "m":
        return val * 60, f"{val} minutes"
    elif unit == "h":
        return val * 3600, f"{val} hours"
    elif unit == "d":
        return val * 86400, f"{val} days"
    else:
        return val * 60, f"{val} minutes"

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

async def send_audit_log(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Dispatches moderation audit log to the private log channel if configured."""
    if LOG_CHANNEL_ID == 0:
        return
    try:
        await context.bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=f"📋 <b>[MOD AUDIT LOG]</b>\n{text}",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error("Failed to send audit log to %s: %s", LOG_CHANNEL_ID, e)

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
    """Handles new member joins with Anti-Raid detection and Emoji CAPTCHA."""
    result = update.chat_member
    new_member = result.new_chat_member

    if new_member.status == ChatMemberStatus.MEMBER and result.old_chat_member.status != ChatMemberStatus.MEMBER:
        user = new_member.user
        chat_id = update.effective_chat.id

        if user.is_bot:
            return

        now = time.time()

        # 1. Anti-Raid Lockdown Check
        if chat_id in lockdown_state and now < lockdown_state[chat_id]:
            # Silent immediate kick during lockdown
            try:
                await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
                await context.bot.unban_chat_member(chat_id=chat_id, user_id=user.id)
                await increment_daily_stat("raids_blocked")
                await send_audit_log(
                    context,
                    f"🚨 <b>Raid Join Intercepted:</b> User <code>{user.id}</code> was auto-kicked under active Lockdown."
                )
            except Exception as e:
                logger.error("Failed to kick user %s during raid lockdown: %s", user.id, e)
            return

        # 2. Track Join Velocity
        recent_joins = join_tracker.get(chat_id, [])
        recent_joins = [t for t in recent_joins if now - t <= RAID_WINDOW]
        recent_joins.append(now)
        join_tracker[chat_id] = recent_joins

        if len(recent_joins) >= RAID_THRESHOLD:
            # Trigger Panic Lockdown
            lockdown_state[chat_id] = now + LOCKDOWN_DURATION
            logger.warning("Raid detected in chat %s! Enabling lockdown for %s seconds.", chat_id, LOCKDOWN_DURATION)
            try:
                await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
                await context.bot.unban_chat_member(chat_id=chat_id, user_id=user.id)
                await increment_daily_stat("raids_blocked")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🚨 <b>ANTI-RAID LOCKDOWN ACTIVATED!</b>\n\n"
                        f"Detected abnormal join surge (&gt;{RAID_THRESHOLD} joins in {RAID_WINDOW}s).\n"
                        f"Strict protection enabled: new joins will be auto-kicked for the next <b>{LOCKDOWN_DURATION // 60} minutes</b>."
                    ),
                    parse_mode=ParseMode.HTML
                )
                await send_audit_log(
                    context,
                    f"🚨 <b>LOCKDOWN ACTIVATED</b> in chat <code>{chat_id}</code>. Velocity exceeded {RAID_THRESHOLD} joins/{RAID_WINDOW}s."
                )
            except Exception as e:
                logger.error("Failed to execute raid trigger actions: %s", e)
            return

        # 3. Restrict member immediately
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user.id,
                permissions=get_mute_permissions()
            )
        except Exception as e:
            logger.error("Failed to restrict new join user %s: %s", user.id, e)
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
                    callback_data=f"captcha:{user.id}:{emoji}"
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
            f"<b>{CAPTCHA_TIMEOUT} seconds</b> to unlock chat access."
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

        active_captchas[user.id] = {
            "target": target_emoji,
            "chat_id": chat_id,
            "message_id": sent_msg.message_id,
        }

        context.job_queue.run_once(
            check_captcha_timeout,
            when=CAPTCHA_TIMEOUT,
            data={"user_id": user.id, "chat_id": chat_id, "message_id": sent_msg.message_id},
            name=f"captcha_{user.id}"
        )

async def handle_captcha_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles emoji clicks for verification."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3:
        return

    _, target_user_id_str, selected_emoji = parts
    target_user_id = int(target_user_id_str)

    if query.from_user.id != target_user_id:
        await query.answer("⚠️ This verification prompt is not for you.", show_alert=True)
        return

    chat_id = update.effective_chat.id
    challenge = active_captchas.get(target_user_id)

    if not challenge:
        await query.answer("Verification session expired.", show_alert=True)
        return

    scheduled_jobs = context.job_queue.get_jobs_by_name(f"captcha_{target_user_id}")
    for job in scheduled_jobs:
        job.schedule_removal()

    target_emoji = challenge["target"]
    message_id = challenge["message_id"]
    active_captchas.pop(target_user_id, None)

    if selected_emoji == target_emoji:
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
                f"🚫 <b>CAPTCHA Failed:</b> User <code>{target_user_id}</code> picked wrong emoji and was kicked."
            )
        except Exception as e:
            logger.error("Failed to soft kick user %s on wrong captcha: %s", target_user_id, e)

async def check_captcha_timeout(context: ContextTypes.DEFAULT_TYPE):
    """Kicks users who fail to complete the CAPTCHA before the timer expires."""
    job_data = context.job.data
    user_id = job_data["user_id"]
    chat_id = job_data["chat_id"]
    message_id = job_data["message_id"]

    active_captchas.pop(user_id, None)

    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        if member.status == ChatMemberStatus.RESTRICTED and not member.can_send_messages:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id)
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            await send_audit_log(
                context,
                f"⏱️ <b>CAPTCHA Timeout:</b> User <code>{user_id}</code> failed to verify within {CAPTCHA_TIMEOUT}s and was removed."
            )
    except Exception as e:
        logger.debug("Captcha timeout check exception for user %s: %s", user_id, e)

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

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM bail WHERE prisoner_id = ? AND chat_id = ?", (user.id, chat_id))
        await db.execute(
            """
            INSERT INTO jail (user_id, chat_id, first_name, username, status, reason, jailed_at)
            VALUES (?, ?, ?, ?, 'jailed', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                first_name=excluded.first_name,
                username=excluded.username,
                status='jailed',
                reason=excluded.reason,
                jailed_at=CURRENT_TIMESTAMP
            """,
            (user.id, chat_id, user.first_name, user.username, reason)
        )
        await db.commit()

    await increment_daily_stat("users_jailed")
    await send_audit_log(
        context,
        f"🚨 <b>User Incarcerated:</b> {user_mention(user.id, user.first_name, user.username)} (ID: <code>{user.id}</code>)\n"
        f"<b>Reason:</b> {html.escape(reason)}"
    )

def build_bail_keyboard(user_id: int, current_vouches: int = 0) -> InlineKeyboardMarkup:
    """Builds the inline keyboard with the current bail vouch counter."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🤝 Post Bail ({current_vouches}/{BAIL_THRESHOLD})", callback_data=f"bail:{user_id}")]
    ])

async def jail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to immediately jail a member until midnight or bail."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("ℹ️ Reply to a user's message to jail them.")
        return

    target_user = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    if await is_admin(update, context, target_user.id):
        await update.message.reply_text("❌ Administrators cannot be jailed.")
        return

    reason = " ".join(context.args) if context.args else "Admin order"
    await jail_user_logic(target_user, chat_id, context, reason=reason)
    mention = user_mention(target_user.id, target_user.first_name, target_user.username)

    text = (
        f"🚨 <b>PRISON SENTENCE!</b>\n\n"
        f"{mention} has been thrown into jail until tonight's 23:59 UTC prison break!\n"
        f"<b>Reason:</b> {html.escape(reason)}\n\n"
        f"<i>Community members can vouch below to bail them out early ({BAIL_THRESHOLD} vouches required).</i>"
    )
    await update.message.reply_text(
        text,
        reply_markup=build_bail_keyboard(target_user.id, 0),
        parse_mode=ParseMode.HTML
    )

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to issue a warning, jailing upon 3 warnings."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("ℹ️ Reply to a user's message to warn them.")
        return

    target_user = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    if await is_admin(update, context, target_user.id):
        await update.message.reply_text("❌ Administrators cannot be warned.")
        return

    reason = " ".join(context.args) if context.args else "Rule violation"

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT warn_count FROM warnings WHERE user_id = ?", (target_user.id,)) as cursor:
            row = await cursor.fetchone()
            count = (row[0] if row else 0) + 1

        await db.execute(
            """
            INSERT INTO warnings (user_id, chat_id, warn_count, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET warn_count = ?, updated_at = CURRENT_TIMESTAMP
            """,
            (target_user.id, chat_id, count, count)
        )
        await db.commit()

    mention = user_mention(target_user.id, target_user.first_name, target_user.username)

    if count >= 3:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("UPDATE warnings SET warn_count = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?", (target_user.id,))
            await db.commit()

        await jail_user_logic(target_user, chat_id, context, reason=f"Accumulated 3 strikes ({reason})")
        text = (
            f"⚠️ <b>STRIKE THREE!</b>\n\n"
            f"{mention} reached <b>3 warnings</b> and was sent to jail until midnight!\n"
            f"<i>Group members may vouch below to bail them out ({BAIL_THRESHOLD} vouches required).</i>"
        )
        await update.message.reply_text(
            text,
            reply_markup=build_bail_keyboard(target_user.id, 0),
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
            f"<b>Reason:</b> {html.escape(reason)}"
        )

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command for timed mute: /mute 15m, /mute 2h, /mute 1d (on reply)."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("ℹ️ Reply to a user's message to mute them: <code>/mute 30m</code>", parse_mode=ParseMode.HTML)
        return

    target_user = update.message.reply_to_message.from_user
    chat_id = update.effective_chat.id

    if await is_admin(update, context, target_user.id):
        await update.message.reply_text("❌ Administrators cannot be muted.")
        return

    duration_arg = context.args[0] if context.args else "1h"
    seconds, label = parse_duration(duration_arg)

    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=target_user.id,
        permissions=get_mute_permissions()
    )

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
        f"⏳ <b>Timed Mute:</b> {mention} muted for {label} by admin <code>{update.effective_user.id}</code>."
    )

async def timed_unmute_callback(context: ContextTypes.DEFAULT_TYPE):
    """Job callback to automatically unmute user after timed mute expires."""
    data = context.job.data
    chat_id = data["chat_id"]
    user_id = data["user_id"]
    await restore_user_permissions(context.bot, chat_id, user_id)
    await send_audit_log(context, f"🔓 <b>Timed Mute Expired:</b> User <code>{user_id}</code> unmuted.")

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manually unmute a member."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    elif context.args:
        try:
            user_id = int(context.args[0])
            member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
            target_user = member.user
        except Exception:
            await update.message.reply_text("❌ User not found.")
            return

    if not target_user:
        await update.message.reply_text("ℹ️ Reply to a user or pass their user ID: <code>/unmute 123456</code>", parse_mode=ParseMode.HTML)
        return

    chat_id = update.effective_chat.id
    # Cancel pending timed mute job
    for job in context.job_queue.get_jobs_by_name(f"timed_mute_{chat_id}_{target_user.id}"):
        job.schedule_removal()

    await restore_user_permissions(context.bot, chat_id, target_user.id)
    mention = user_mention(target_user.id, target_user.first_name, target_user.username)
    await update.message.reply_text(f"🔊 {mention} has been unmuted.", parse_mode=ParseMode.HTML)

async def handle_bail_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles community bail vouches."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 2:
        return

    target_user_id = int(parts[1])
    voucher = query.from_user
    chat_id = update.effective_chat.id

    if voucher.id == target_user_id:
        await query.answer("⚖️ You cannot post bail for yourself!", show_alert=True)
        return

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT status, first_name, username FROM jail WHERE user_id = ?", (target_user_id,)) as cur:
            jail_entry = await cur.fetchone()
            if not jail_entry or jail_entry[0] != "jailed":
                await query.answer("This user is no longer in jail.", show_alert=True)
                return
            prisoner_name, prisoner_username = jail_entry[1], jail_entry[2]

        async with db.execute("SELECT status FROM jail WHERE user_id = ? AND status = 'jailed'", (voucher.id,)) as cur:
            if await cur.fetchone():
                await query.answer("🚫 Inmates currently in jail cannot post bail for others!", show_alert=True)
                return

        try:
            cursor = await db.execute(
                "INSERT INTO bail (prisoner_id, chat_id, voucher_id) VALUES (?, ?, ?)",
                (target_user_id, chat_id, voucher.id)
            )
            await db.commit()
            if cursor.rowcount == 0:
                await query.answer("You have already vouched for this member.", show_alert=True)
                return
        except aiosqlite.IntegrityError:
            await query.answer("You have already vouched for this member.", show_alert=True)
            return

        async with db.execute("SELECT COUNT(*) FROM bail WHERE prisoner_id = ? AND chat_id = ?", (target_user_id, chat_id)) as cur:
            row = await cur.fetchone()
            vouch_count = row[0] if row else 0

    if vouch_count >= BAIL_THRESHOLD:
        await restore_user_permissions(context.bot, chat_id, target_user_id)

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("UPDATE jail SET status = 'bailed' WHERE user_id = ?", (target_user_id,))
            await db.commit()

        await increment_daily_stat("bails_granted")
        mention = user_mention(target_user_id, prisoner_name, prisoner_username)
        release_text = (
            f"🔓 <b>COMMUNITY BAILOUT!</b>\n\n"
            f"{mention} received <b>{vouch_count}/{BAIL_THRESHOLD} vouches</b> and has been released from jail early!\n"
            f"Speak responsibly and thank your benefactors."
        )
        await query.message.edit_text(release_text, parse_mode=ParseMode.HTML)
        await send_audit_log(context, f"🤝 <b>Bailout Completed:</b> {mention} released with {vouch_count} vouches.")
    else:
        await query.answer(f"🤝 Vouch recorded ({vouch_count}/{BAIL_THRESHOLD})!")
        await query.message.edit_reply_markup(
            reply_markup=build_bail_keyboard(target_user_id, vouch_count)
        )

async def pardon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to pardon and unjail an individual user immediately."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    elif context.args:
        try:
            user_id = int(context.args[0])
            chat_member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
            target_user = chat_member.user
        except Exception:
            await update.message.reply_text("❌ Invalid user ID or user not found.")
            return

    if not target_user:
        await update.message.reply_text("ℹ️ Reply to a user or provide their numeric ID: <code>/pardon 12345678</code>", parse_mode=ParseMode.HTML)
        return

    chat_id = update.effective_chat.id
    await restore_user_permissions(context.bot, chat_id, target_user.id)

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE jail SET status = 'pardoned' WHERE user_id = ?", (target_user.id,))
        await db.commit()

    mention = user_mention(target_user.id, target_user.first_name, target_user.username)
    await update.message.reply_text(
        f"🕊️ <b>Official Pardon:</b> {mention} has been pardoned by an administrator.",
        parse_mode=ParseMode.HTML
    )
    await send_audit_log(context, f"🕊️ <b>Pardon Issued:</b> {mention} pardoned by <code>{update.effective_user.id}</code>.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays a user's moderation record and warning count."""
    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    elif context.args:
        try:
            user_id = int(context.args[0])
            chat_member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
            target_user = chat_member.user
        except Exception:
            await update.message.reply_text("❌ User not found.")
            return
    else:
        target_user = update.effective_user

    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT warn_count FROM warnings WHERE user_id = ?", (target_user.id,)) as cur:
            warn_row = await cur.fetchone()
            warn_count = warn_row[0] if warn_row else 0

        async with db.execute("SELECT status, jailed_at, reason FROM jail WHERE user_id = ?", (target_user.id,)) as cur:
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

    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    elif context.args:
        try:
            user_id = int(context.args[0])
            chat_member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
            target_user = chat_member.user
        except Exception:
            await update.message.reply_text("❌ User not found.")
            return

    if not target_user:
        await update.message.reply_text("ℹ️ Reply to a user or specify user ID to clear warnings.")
        return

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE warnings SET warn_count = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?", (target_user.id,))
        await db.commit()

    mention = user_mention(target_user.id, target_user.first_name, target_user.username)
    await update.message.reply_text(f"🧹 Warnings cleared for {mention}. (0/3)", parse_mode=ParseMode.HTML)
    await send_audit_log(context, f"🧹 <b>Warnings Cleared:</b> {mention} reset by <code>{update.effective_user.id}</code>.")

# ---------------------------------------------------------------------------
# Anti-Spam Link & Channel Impersonator Shield
# ---------------------------------------------------------------------------

async def spam_and_channel_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Filters spam links and prevents anonymous channel impersonation."""
    if not update.message:
        return

    chat = update.effective_chat

    # 1. Anonymous Channel Impersonator Shield
    if update.message.sender_chat:
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
                    f"🎭 <b>Channel Post Blocked:</b> Channel <code>{sender_chat.title}</code> (ID: <code>{sender_chat.id}</code>) tried posting in chat."
                )
            except Exception as e:
                logger.error("Failed to block channel impersonator: %s", e)
            return

    if not update.message.text:
        return

    # Skip administrators from link spam filtering
    if await is_admin(update, context, update.effective_user.id):
        return

    # 2. Unicode Normalized Link Filter
    normalized_text = sanitize_text(update.message.text.lower())
    if any(pattern in normalized_text for pattern in SPAM_PATTERNS):
        try:
            await update.message.delete()
            await increment_daily_stat("links_blocked")
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
                f"<code>{html.escape(update.message.text[:120])}</code>"
            )
        except Exception as e:
            logger.error("Failed to handle spam link: %s", e)

# ---------------------------------------------------------------------------
# Nightly Scheduled Batch Unfreeze & Warden's Daily Crime Report
# ---------------------------------------------------------------------------

async def execute_batch_unfreeze(bot, chat_id: int):
    """Unfreezes all jailed inmates with rate limiting and broadcasts the Warden's Daily Crime Report."""
    async with aiosqlite.connect(DB_FILE) as db:
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
            logger.error("Failed to unfreeze prisoner %s: %s", user_id, e)
            continue

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE jail SET status = 'released' WHERE chat_id = ? AND status = 'jailed'", (chat_id,))
        await db.commit()

    stats = await get_today_stats()
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
        logger.error("Failed to send prison break report: %s", e)

async def scheduled_midnight_job(context: ContextTypes.DEFAULT_TYPE):
    """Cron callback executed at 23:59 UTC daily."""
    logger.info("Triggering scheduled 23:59 UTC midnight prison break...")
    if TARGET_GROUP_ID != 0:
        await execute_batch_unfreeze(context.bot, TARGET_GROUP_ID)
    else:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT DISTINCT chat_id FROM jail WHERE status = 'jailed'") as cur:
                rows = await cur.fetchall()
                for row in rows:
                    await execute_batch_unfreeze(context.bot, row[0])

async def unfreeze_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to trigger immediate prison break for the current chat."""
    if not await is_admin(update, context, update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    status_msg = await update.message.reply_text("⏳ Initiating emergency prison release...")
    await execute_batch_unfreeze(context.bot, chat_id)
    try:
        await status_msg.delete()
    except Exception:
        pass

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
        "• <b>Emoji CAPTCHA:</b> New joins must click the designated emoji within 90s.\n"
        "• <b>Community Bailout:</b> Click <code>Post Bail</code> on a jailed user to vouch for their early release!\n\n"
        "<b>Admin Commands:</b>\n"
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
    """Displays standard group safety and conduct rules."""
    text = (
        "📜 <b>Group Rules & Conduct Code</b>\n\n"
        "1. <b>No Spam or Invite Links:</b> Unauthorized promotional links and shorteners are deleted automatically.\n"
        "2. <b>Pass Verification:</b> All new members must solve the Emoji CAPTCHA within 90 seconds or face removal.\n"
        "3. <b>Three Strikes Rule:</b> 3 warnings will result in incarceration until the 23:59 UTC Prison Break.\n"
        "4. <b>Bail System:</b> Inmates can be bailed out early if 3 community members vouch for them.\n"
        "5. <b>Respect All Members:</b> Maintain constructive discussions and avoid harassment."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ---------------------------------------------------------------------------
# Application Entry Point
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable is not set! Please check your .env file.")
        return

    asyncio.run(init_db())

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Chat Member Handler (New joins / CAPTCHA / Anti-Raid)
    app.add_handler(ChatMemberHandler(on_user_join, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(handle_captcha_button, pattern="^captcha:"))
    app.add_handler(CallbackQueryHandler(handle_bail_button, pattern="^bail:"))

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

    logger.info("Bot started and listening for events...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
