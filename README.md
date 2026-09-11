# 🛡️ Telegram Supergroup Moderation & Warden Bot

A battle-tested Telegram Supergroup moderation bot featuring **Emoji CAPTCHA** gatekeeping, **Anti-Raid Lockdown**, an **Anonymous Channel Shield**, a community-driven **Bailout (`/bail`)** mechanic, **Timed Mutes**, **Admin Caching**, SQLite persistence, and a daily **23:59 UTC "Midnight Prison Break & Crime Report"**.

---

## ✨ Features & Capabilities

- **🧩 Dynamic Emoji CAPTCHA:**
  - Mutes new members immediately and challenges them to find a randomized target emoji (e.g. *"Click the 🐶 Dog to unlock chat access"*).
  - Outsmarts headless browser click-bots that target generic "I am Human" buttons.
  - Automatically cancels the kick timer on success and removes verification prompts after 5 seconds.
  - Soft-kicks users who fail or timeout after 90 seconds.

- **🚨 Anti-Raid Panic Mode:**
  - Detects join velocity surges (e.g., >5 joins in 10 seconds).
  - Automatically locks down the gates for 5 minutes, auto-kicking raid accounts without sending CAPTCHAs into the chat.

- **🎭 Anonymous Channel Impersonation Shield:**
  - Prevents scammers from sending messages on behalf of public Telegram channels (often used to evade user bans and promote crypto drainers).
  - Automatically deletes posts from external channels while whitelisting the group itself and official linked channels.

- **🤝 Community Bailout System (`/bail`):**
  - Incarcerated users receive an interactive `[🤝 Post Bail (0/3)]` button.
  - Active group members can vouch for them. Once 3 vouches are collected, the prisoner is released early!
  - Inmates cannot bail themselves or other inmates.

- **⏳ Timed Mutes (`/mute` & `/unmute`):**
  - Allows quick, temporary timeouts (e.g. `/mute 15m`, `/mute 2h`, `/mute 1d`) without full midnight sentencing.
  - Automatically restores permissions when the timer expires.

- **⚡ In-Memory Admin Caching (429 Rate-Limit Prevention):**
  - Caches administrator IDs with a 5-minute TTL to prevent Telegram API flood limits during high chat activity.

- **🛡️ Unicode & Zero-Width Anti-Spam Filter:**
  - Normalizes homoglyphs (NFKD) and strips hidden zero-width spaces (`\u200b`, `\ufeff`) before inspecting links (`t.me/`, `telegram.me/`, `bit.ly/`, `tinyurl.com/`, etc.).

- **📊 Warden's Daily Crime Report (23:59 UTC):**
  - Releases all remaining prisoners at midnight with humorous quotes and broadcasts daily moderation statistics:
    - *Inmates Pardoned*
    - *Spam Links Intercepted*
    - *Bails Granted*
    - *Raid Incursions Deflected*

- **📋 Dedicated Admin Audit Log Channel (Optional):**
  - Automatically dispatches moderation audit logs (warnings, jailing, pardons, raid triggers, link blocks) to a private channel.

---

## 📋 Prerequisites & Telegram Setup

### 1. Create the Bot with @BotFather
1. Open Telegram and search for [`@BotFather`](https://t.me/BotFather).
2. Send `/newbot`, choose a display name and a username ending in `bot`.
3. Copy your **HTTP API Token**.
4. Send `/setprivacy`, select your bot, and choose **Disable**. *(Allows the bot to monitor chat messages for link spam).*

### 2. Group Configuration
1. Ensure your group is a **Supergroup** (groups with chat history visible to new members or with >200 members are automatically Supergroups).
2. Add the bot to the group and promote it to **Administrator** with:
   - ✅ *Delete messages*
   - ✅ *Ban users* (Restrict members)
   - ✅ *Invite users via link*

### 3. Retrieve Your Numeric Group ID
- Forward any message from your group to [`@JsonDumpBot`](https://t.me/JsonDumpBot) or [`@userinfobot`](https://t.me/userinfobot).
- Look for `chat.id` (starts with `-100`, e.g. `-1001234567890`).

---

## 🚀 Quickstart & Installation

### Local / VPS Deployment

1. **Clone the repository:**
   ```bash
   git clone <repo-url>
   cd charming-lovelace
   ```

2. **Create a virtual environment & install dependencies:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure environment:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env`:
   ```env
   TELEGRAM_BOT_TOKEN=1234567890:ABCDefGhIJkLmNoPQRstUVwxyZ
   TARGET_GROUP_ID=-1001234567890
   LOG_CHANNEL_ID=-1009876543210
   BAIL_THRESHOLD=3
   CAPTCHA_TIMEOUT=90
   RAID_THRESHOLD=5
   RAID_WINDOW=10
   LOCKDOWN_DURATION=300
   ```

4. **Run the bot:**
   ```bash
   python main.py
   ```

---

## 📖 Command Reference

| Command | Target | Permission | Description |
| :--- | :--- | :--- | :--- |
| `/warn [reason]` | Reply | Admin | Issues a warning strike. Jails on 3 strikes. |
| `/jail [reason]` | Reply | Admin | Jails user until 23:59 UTC or community bail. |
| `/mute [15m/2h/1d]`| Reply | Admin | Applies a temporary timed mute. |
| `/unmute` | Reply / ID | Admin | Lifts a mute immediately. |
| `/pardon` | Reply / ID | Admin | Pardons and releases an incarcerated inmate. |
| `/clearwarns` | Reply / ID | Admin | Resets strike count back to 0/3. |
| `/unfreezeall` | None | Admin | Triggers an immediate mass prison break. |
| `/status` | Reply / ID / Self | Everyone | Checks warnings and incarceration status. |
| `/rules` | None | Everyone | Displays group safety rules. |
| `/help` | None | Everyone | Displays command guide. |

---

## 🗄️ Database Schema

SQLite database: `group_moderator.db`

- **`jail`**: Incarcerated members, status (`jailed`, `released`, `bailed`, `pardoned`), reason, timestamp.
- **`warnings`**: Tracks warning counters per user.
- **`bail`**: Tracks community vouches with unique `(prisoner_id, voucher_id)` constraints.
- **`daily_stats`**: Tracks daily metrics (`links_blocked`, `users_jailed`, `bails_granted`, `raids_blocked`).
