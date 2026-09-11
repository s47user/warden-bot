import os
import html
import random
import logging
import asyncio
import datetime
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
BAIL_THRESHOLD = int(os.getenv("BAIL_THRESHOLD", "3").strip() or 3)
CAPTCHA_TIMEOUT = int(os.getenv("CAPTCHA_TIMEOUT", "90").strip() or 90)
DB_FILE = os.getenv("DB_FILE", "group_moderator.db").strip()

logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("GroupModeratorBot")

# In-memory store for active CAPTCHA challenges: {user_id: {"target": emoji, "chat_id": int, "message_id": int}}
active_captchas: dict[int, dict] = {}

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

# ---------------------------------------------------------------------------
# Database Initialization
# ---------------------------------------------------------------------------

async def init_db():
    """Initializes the SQLite database with required tables and indexes."""
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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_jail_chat_status ON jail (chat_id, status)")
        await db.commit()
    logger.info("Database initialized successfully at '%s'", DB_FILE)

# ---------------------------------------------------------------------------
# Safe Helpers & Permissions
# ---------------------------------------------------------------------------

def user_mention(user_id: int, first_name: Optional[str], username: Optional[str] = None) -> str:
    """Returns safe HTML formatted user mention, escaping special HTML characters."""
    if username:
        return f"@{html.escape(username)}"
    safe_name = html.escape(first_name or "Member")
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'

def get_mute_permissions() -> ChatPermissions:
    """Returns ChatPermissions object restricting all forms of messaging."""
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
        logger.info("Restored default permissions for user %s in chat %s", user_id, chat_id)
    except Exception as e:
        logger.error("Failed to restore permissions for user %s in chat %s: %s", user_id, chat_id, e)

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Checks if a given user is an administrator or creator in the chat."""
    if not update.effective_chat:
        return False
    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        return any(admin.user.id == user_id for admin in admins)
    except Exception as e:
        logger.error("Error checking admin status for user %s: %s", user_id, e)
        return False

async def delete_message_delayed(context: ContextTypes.DEFAULT_TYPE):
    """Job callback to delete a message after a delay."""
    data = context.job.data
    try:
        await context.bot.delete_message(chat_id=data["chat_id"], message_id=data["message_id"])
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Gatekeeper Emoji CAPTCHA Verification
# ---------------------------------------------------------------------------

async def on_user_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles new member joins with a randomized Emoji CAPTCHA challenge."""
    result = update.chat_member
    new_member = result.new_chat_member

    if new_member.status == ChatMemberStatus.MEMBER and result.old_chat_member.status != ChatMemberStatus.MEMBER:
        user = new_member.user
        chat_id = update.effective_chat.id

        if user.is_bot:
            return

        # Restrict member immediately
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user.id,
                permissions=get_mute_permissions()
            )
        except Exception as e:
            logger.error("Failed to restrict new join user %s: %s", user.id, e)
            return

        # Select 1 target and 3 decoys
        target_pair = random.choice(CAPTCHA_CHALLENGES)
        target_emoji, target_label = target_pair

        decoys = [p for p in CAPTCHA_CHALLENGES if p[0] != target_emoji]
        selected_decoys = random.sample(decoys, 3)

        choices = [target_pair] + selected_decoys
        random.shuffle(choices)

        # Build inline keyboard
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

        # Record active challenge
        active_captchas[user.id] = {
            "target": target_emoji,
            "chat_id": chat_id,
            "message_id": sent_msg.message_id,
        }

        # Schedule timeout job with unique name for targeted cancellation
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

    # Prevent other users from answering
    if query.from_user.id != target_user_id:
        await query.answer("⚠️ This verification prompt is not for you.", show_alert=True)
        return

    chat_id = update.effective_chat.id
    challenge = active_captchas.get(target_user_id)

    if not challenge:
        await query.answer("Verification session expired.", show_alert=True)
        return

    # Cancel scheduled timeout job immediately
    scheduled_jobs = context.job_queue.get_jobs_by_name(f"captcha_{target_user_id}")
    for job in scheduled_jobs:
        job.schedule_removal()

    target_emoji = challenge["target"]
    message_id = challenge["message_id"]
    active_captchas.pop(target_user_id, None)

    if selected_emoji == target_emoji:
        # Correct challenge response
        await restore_user_permissions(context.bot, chat_id, target_user_id)
        mention = user_mention(target_user_id, query.from_user.first_name)
        await query.message.edit_text(
            f"✅ <b>Verified!</b> Welcome to the community, {mention}.",
            parse_mode=ParseMode.HTML
        )
        # Auto-delete message after 5 seconds to keep chat clean
        context.job_queue.run_once(
            delete_message_delayed,
            when=5,
            data={"chat_id": chat_id, "message_id": message_id}
        )
    else:
        # Incorrect challenge response -> soft kick
        await query.answer("❌ Incorrect selection. You have been removed.", show_alert=True)
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_user_id)
            await context.bot.unban_chat_member(chat_id=chat_id, user_id=target_user_id)
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
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
            logger.info("Kicked user %s for CAPTCHA timeout in chat %s", user_id, chat_id)
    except Exception as e:
        logger.debug("Captcha timeout check exception for user %s: %s", user_id, e)

# ---------------------------------------------------------------------------
# Moderation Engine (Jail, Warn, Bail, Pardon, Status)
# ---------------------------------------------------------------------------

async def jail_user_logic(user, chat_id: int, context: ContextTypes.DEFAULT_TYPE, reason: str = "manual"):
    """Applies chat mute and records jail state in database."""
    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user.id,
        permissions=get_mute_permissions()
    )

    async with aiosqlite.connect(DB_FILE) as db:
        # Clear previous bails for this user in this chat
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
    logger.info("User %s jailed in chat %s (Reason: %s)", user.id, chat_id, reason)

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

    await jail_user_logic(target_user, chat_id, context, reason="admin jail")
    mention = user_mention(target_user.id, target_user.first_name, target_user.username)

    text = (
        f"🚨 <b>PRISON SENTENCE!</b>\n\n"
        f"{mention} has been thrown into jail until tonight's 23:59 UTC prison break!\n"
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
        # Reset warnings and jail
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("UPDATE warnings SET warn_count = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?", (target_user.id,))
            await db.commit()

        await jail_user_logic(target_user, chat_id, context, reason="3 warnings accumulated")
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
        text = f"⚠️ <b>Warning Issued!</b> {mention} has received warning <b>({count}/3)</b>."
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

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

    # Inmates cannot bail themselves
    if voucher.id == target_user_id:
        await query.answer("⚖️ You cannot post bail for yourself!", show_alert=True)
        return

    async with aiosqlite.connect(DB_FILE) as db:
        # Verify prisoner is still jailed
        async with db.execute("SELECT status, first_name, username FROM jail WHERE user_id = ?", (target_user_id,)) as cur:
            jail_entry = await cur.fetchone()
            if not jail_entry or jail_entry[0] != "jailed":
                await query.answer("This user is no longer in jail.", show_alert=True)
                return
            prisoner_name, prisoner_username = jail_entry[1], jail_entry[2]

        # Check if voucher is currently jailed
        async with db.execute("SELECT status FROM jail WHERE user_id = ? AND status = 'jailed'", (voucher.id,)) as cur:
            if await cur.fetchone():
                await query.answer("🚫 Inmates currently in jail cannot post bail for others!", show_alert=True)
                return

        # Attempt to insert vouch
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

        # Count total vouches
        async with db.execute("SELECT COUNT(*) FROM bail WHERE prisoner_id = ? AND chat_id = ?", (target_user_id, chat_id)) as cur:
            row = await cur.fetchone()
            vouch_count = row[0] if row else 0

    if vouch_count >= BAIL_THRESHOLD:
        # Threshold reached: Grant bail release
        await restore_user_permissions(context.bot, chat_id, target_user_id)

        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("UPDATE jail SET status = 'bailed' WHERE user_id = ?", (target_user_id,))
            await db.commit()

        mention = user_mention(target_user_id, prisoner_name, prisoner_username)
        release_text = (
            f"🔓 <b>COMMUNITY BAILOUT!</b>\n\n"
            f"{mention} received <b>{vouch_count}/{BAIL_THRESHOLD} vouches</b> and has been released from jail early!\n"
            f"Speak responsibly and thank your benefactors."
        )
        await query.message.edit_text(release_text, parse_mode=ParseMode.HTML)
    else:
        # Update button counter
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
        f"🕊️ <b>Official Pardon:</b> {mention} has been pardoned by an administrator and restored to full privileges.",
        parse_mode=ParseMode.HTML
    )

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

# ---------------------------------------------------------------------------
# Spam Link Filter
# ---------------------------------------------------------------------------

SPAM_PATTERNS = ["t.me/", "telegram.me/", "bit.ly/", "tinyurl.com/", "is.gd/"]

async def spam_link_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detects unauthorized links and shorteners, deleting them instantly."""
    if not update.message or not update.message.text:
        return

    # Never filter group administrators
    if await is_admin(update, context, update.effective_user.id):
        return

    text = update.message.text.lower()
    if any(pattern in text for pattern in SPAM_PATTERNS):
        try:
            await update.message.delete()
            mention = user_mention(update.effective_user.id, update.effective_user.first_name)
            warning_msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"🚫 Unauthorized link removed, {mention}!",
                parse_mode=ParseMode.HTML
            )
            # Delete warning after 5 seconds
            context.job_queue.run_once(
                delete_message_delayed,
                when=5,
                data={"chat_id": update.effective_chat.id, "message_id": warning_msg.message_id}
            )
        except Exception as e:
            logger.error("Failed to handle spam link: %s", e)

# ---------------------------------------------------------------------------
# Nightly Scheduled Batch Unfreeze & Manual Unfreeze All
# ---------------------------------------------------------------------------

async def execute_batch_unfreeze(bot, chat_id: int):
    """Unfreezes all jailed inmates in a specific chat with rate limiting."""
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT user_id, first_name, username FROM jail WHERE chat_id = ? AND status = 'jailed'",
            (chat_id,)
        ) as cursor:
            prisoners = await cursor.fetchall()

    if not prisoners:
        logger.info("No prisoners found to unfreeze in chat %s", chat_id)
        return

    released_mentions = []
    for user_id, first_name, username in prisoners:
        try:
            await restore_user_permissions(bot, chat_id, user_id)
            mention = user_mention(user_id, first_name, username)
            released_mentions.append(mention)
            # Sleep 150ms between actions to comply with Telegram rate limits
            await asyncio.sleep(0.15)
        except Exception as e:
            logger.error("Failed to unfreeze prisoner %s: %s", user_id, e)
            continue

    # Mark all as released in SQLite
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE jail SET status = 'released' WHERE chat_id = ? AND status = 'jailed'", (chat_id,))
        await db.commit()

    if not released_mentions:
        return

    quote = random.choice(FUNNY_QUOTES)

    # Chunk mentions in groups of 5 to avoid notification suppression
    chunk_size = 5
    for i in range(0, len(released_mentions), chunk_size):
        chunk = released_mentions[i:i + chunk_size]
        tag_line = ", ".join(chunk)

        if i == 0:
            announcement = (
                f"🔓 <b>PRISON BREAK! (Midnight Ritual)</b>\n\n"
                f"<i>\"{quote}\"</i>\n\n"
                f"You are all free to speak now:\n{tag_line}"
            )
        else:
            announcement = f"Also released:\n{tag_line}"

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=announcement,
                parse_mode=ParseMode.HTML
            )
            await asyncio.sleep(0.3)
        except Exception as e:
            logger.error("Failed to send prison break announcement: %s", e)

async def scheduled_midnight_job(context: ContextTypes.DEFAULT_TYPE):
    """Cron callback executed at 23:59 UTC daily."""
    logger.info("Triggering scheduled 23:59 UTC midnight prison break...")
    if TARGET_GROUP_ID != 0:
        await execute_batch_unfreeze(context.bot, TARGET_GROUP_ID)
    else:
        # Fallback: check all chats that have active prisoners
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
# Application Entry Point
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable is not set! Please check your .env file.")
        return

    asyncio.run(init_db())

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Chat Member Handler (New joins / CAPTCHA)
    app.add_handler(ChatMemberHandler(on_user_join, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(handle_captcha_button, pattern="^captcha:"))
    app.add_handler(CallbackQueryHandler(handle_bail_button, pattern="^bail:"))

    # Moderation Commands
    app.add_handler(CommandHandler("jail", jail_command))
    app.add_handler(CommandHandler("warn", warn_command))
    app.add_handler(CommandHandler("pardon", pardon_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("clearwarns", clear_warns_command))
    app.add_handler(CommandHandler("unfreezeall", unfreeze_all_command))
    app.add_handler(CommandHandler("unmuteall", unfreeze_all_command))

    # Spam Auto-Filter
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), spam_link_filter))

    # Daily Nightly Cron at 23:59 UTC
    midnight_time = datetime.time(hour=23, minute=59, second=0, tzinfo=datetime.timezone.utc)
    app.job_queue.run_daily(scheduled_midnight_job, time=midnight_time)
    logger.info("Midnight unfreeze scheduled daily at 23:59 UTC.")

    logger.info("Bot started and listening for events...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
