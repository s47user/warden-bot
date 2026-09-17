#!/usr/bin/env python3
"""
Warden Bot - Bulk Member Unmute Tool
Scans all restricted/muted members in a Telegram group via MTProto
and unbans/unmutes them using your bot token with live progress reporting.
"""

import os
import sys
import random
import asyncio
import httpx
from dotenv import load_dotenv

try:
    from pyrogram import Client
    from pyrogram.enums import ChatMembersFilter
except ImportError:
    print("\n❌ Pyrogram is required for this tool.")
    print("Run: pip install --user pyrogram httpx python-dotenv --break-system-packages\n")
    sys.exit(1)

load_dotenv()

SEP = "━━━━━━━━━━━━━━━━━━━━━━━"

PERMISSIONS = {
    "can_send_messages": True,
    "can_send_audios": True,
    "can_send_documents": True,
    "can_send_photos": True,
    "can_send_videos": True,
    "can_send_video_notes": True,
    "can_send_voice_notes": True,
    "can_send_polls": True,
    "can_send_other_messages": True,
    "can_add_web_page_previews": True,
    "can_invite_users": True,
    "can_pin_messages": False,
    "can_change_info": False,
}

def render_bar(done: int, total: int, width: int = 16) -> str:
    pct = int(100 * done / total) if total else 0
    filled = int(width * done / total) if total else 0
    return f"[{'█' * filled}{'░' * (width - filled)}] {pct}%"

def progress_card(done: int, total: int, success: int, failed: int, current: str = "") -> str:
    return (
        f"{SEP}\n"
        "🛡️ <b>WARDEN BULK UNMUTE ENGINE</b>\n"
        f"{SEP}\n\n"
        "⚙️ <b>Restoration in progress...</b>\n\n"
        f"<code>{render_bar(done, total)}</code>\n\n"
        f"• 👤 <b>Processing:</b> {current}\n"
        f"• ✅ <b>Freed:</b> {success}\n"
        f"• ❌ <b>Failed:</b> {failed}\n"
        f"• 📊 <b>Progress:</b> {done} / {total}\n\n"
        f"⏳ <i>Please wait while permissions are restored...</i>\n"
        f"{SEP}"
    )

def done_card(total: int, success: int, failed: int) -> str:
    return (
        f"{SEP}\n"
        "🎉 <b>WARDEN UNMUTE COMPLETE!</b>\n"
        f"{SEP}\n\n"
        f"<code>{render_bar(total, total)}</code>\n\n"
        f"• 👥 <b>Total Restricted:</b> {total}\n"
        f"• ✅ <b>Successfully Restored:</b> {success}\n"
        f"• ❌ <b>Failed / Left Chat:</b> {failed}\n\n"
        f"✨ <i>All eligible members have been granted speaking permissions!</i>\n"
        f"{SEP}"
    )

async def bot_post(http: httpx.AsyncClient, bot_api: str, method: str, payload: dict) -> dict:
    try:
        r = await http.post(f"{bot_api}/{method}", json=payload, timeout=25)
        return r.json()
    except Exception as e:
        return {"ok": False, "description": str(e)}

async def bot_send(http: httpx.AsyncClient, bot_api: str, chat_id: int, text: str) -> int | None:
    res = await bot_post(http, bot_api, "sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    if res.get("ok"):
        return res["result"]["message_id"]
    return None

async def bot_edit(http: httpx.AsyncClient, bot_api: str, chat_id: int, message_id: int, text: str):
    try:
        await bot_post(http, bot_api, "editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML"
        })
    except Exception:
        pass

async def bot_restrict(http: httpx.AsyncClient, bot_api: str, chat_id: int, user_id: int) -> bool:
    payload = {"chat_id": chat_id, "user_id": user_id, "permissions": PERMISSIONS}
    for attempt in range(5):
        d = await bot_post(http, bot_api, "restrictChatMember", payload)
        if d.get("ok"):
            return True

        desc = d.get("description", "")
        params = d.get("parameters", {})
        retry_after = params.get("retry_after")
        if retry_after:
            wait = float(retry_after) + random.uniform(0.5, 2.0)
            print(f"  [⏳ Flood Wait] Retrying in {wait:.1f}s...")
            await asyncio.sleep(wait)
        elif "Too Many Requests" in desc:
            wait = (2 ** attempt) + random.uniform(0.5, 1.5)
            print(f"  [⏳ Rate Limit] Backing off {wait:.1f}s...")
            await asyncio.sleep(wait)
        elif "not enough rights" in desc.lower() or "CHAT_ADMIN_REQUIRED" in desc:
            print(f"  [❌ Rights Error] Bot requires 'Ban / Restrict Members' admin right.")
            return False
        else:
            return False
    return False

async def main():
    print(f"\n{SEP}")
    print("🛡️  Warden Bot — Bulk Member Unmute Tool")
    print(f"{SEP}\n")

    api_id_env = os.getenv("TG_API_ID") or os.getenv("API_ID") or ""
    api_hash_env = os.getenv("TG_API_HASH") or os.getenv("API_HASH") or ""
    bot_token_env = os.getenv("TELEGRAM_BOT_TOKEN") or ""
    chat_id_env = os.getenv("TARGET_GROUP_ID") or ""

    if not api_id_env:
        api_id_env = input("Enter your Telegram API_ID (from my.telegram.org): ").strip()
    if not api_hash_env:
        api_hash_env = input("Enter your Telegram API_HASH (from my.telegram.org): ").strip()
    if not bot_token_env:
        bot_token_env = input("Enter your TELEGRAM_BOT_TOKEN (from @BotFather): ").strip()
    if not chat_id_env or chat_id_env == "0":
        chat_id_env = input("Enter your Target Group ID (e.g. -1001234567890): ").strip()

    try:
        api_id = int(api_id_env)
        api_hash = str(api_hash_env)
        bot_token = str(bot_token_env)
    except ValueError:
        print("\n❌ Invalid credentials entered. API_ID must be a number.")
        sys.exit(1)

    raw_chat = str(chat_id_env).strip()
    if raw_chat.startswith("@"):
        chat_target = raw_chat
    elif raw_chat.lstrip("-").isdigit():
        num = int(raw_chat)
        if num > 0:
            chat_target = int(f"-100{num}")
        elif not str(num).startswith("-100"):
            chat_target = int(f"-100{abs(num)}")
        else:
            chat_target = num
    else:
        chat_target = raw_chat

    bot_api = f"https://api.telegram.org/bot{bot_token}"

    print(f"\n[*] Connecting to Telegram client to scan muted members...")
    print("[*] (If this is your first time, you will be prompted for your phone & SMS code)\n")

    members = []
    async with Client("warden_unmute_session", api_id=api_id, api_hash=api_hash, sleep_threshold=60) as app:
        try:
            chat = await app.get_chat(chat_target)
            chat_id = chat.id
            print(f"[*] Successfully connected to chat: '{chat.title}' (ID: {chat_id})")
        except Exception as e:
            print(f"❌ Failed to access chat '{chat_target}': {e}")
            print("💡 Tip: Make sure your Telegram user account is a member (or admin) of the group.")
            print("💡 Tip: Supergroup IDs always start with -100 (e.g. -1001794534648).")
            sys.exit(1)

        print("[*] Querying restricted members list...")
        async for m in app.get_chat_members(chat_id, filter=ChatMembersFilter.RESTRICTED):
            name = f"{m.user.first_name or ''} {m.user.last_name or ''}".strip() or m.user.username or f"User {m.user.id}"
            members.append((m.user.id, name))

    total = len(members)
    if total == 0:
        print("\n✅ No restricted/muted members found in this group! Nothing to do.")
        return

    print(f"\n[*] Found {total} restricted member(s).")
    print("[*] Passing execution to Bot API engine with rate-limit protection...\n")

    concurrency = 5
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    done = 0
    success = 0
    failed = 0
    current_target = [""]

    async with httpx.AsyncClient() as http:
        msg_id = await bot_send(
            http,
            bot_api,
            chat_id,
            f"{SEP}\n"
            "🛡️ <b>WARDEN BULK UNMUTE ENGINE</b>\n"
            f"{SEP}\n\n"
            f"🚀 <b>Found {total} muted member(s).</b>\n"
            "⏳ <i>Starting bulk unmute operation now...</i>\n"
            f"{SEP}"
        )

        async def worker(uid: int, name: str):
            nonlocal done, success, failed
            async with sem:
                async with lock:
                    current_target[0] = name
                ok = await bot_restrict(http, bot_api, chat_id, uid)
                async with lock:
                    if ok:
                        success += 1
                    else:
                        failed += 1
                    done += 1
                tag = "✅" if ok else "❌"
                print(f"[{tag}] {done}/{total} — {name} ({uid})")
                await asyncio.sleep(0.25)

        async def progress_reporter():
            while done < total:
                await asyncio.sleep(8)
                if msg_id:
                    await bot_edit(
                        http,
                        bot_api,
                        chat_id,
                        msg_id,
                        progress_card(done, total, success, failed, current_target[0])
                    )

        updater_task = asyncio.create_task(progress_reporter())
        await asyncio.gather(*[worker(uid, name) for uid, name in members])
        updater_task.cancel()

        final_text = done_card(total, success, failed)
        if msg_id:
            await bot_edit(http, bot_api, chat_id, msg_id, final_text)
        else:
            await bot_send(http, bot_api, chat_id, final_text)

    print(f"\n{SEP}")
    print(f"🎉 Complete! Freed: {success} | Failed: {failed} | Total: {total}")
    print(f"{SEP}\n")

if __name__ == "__main__":
    asyncio.run(main())
