# 🛡️ Telegram Supergroup Moderation & Warden Bot

A production-ready Telegram Supergroup moderation bot featuring **Emoji CAPTCHA** gatekeeping, a community-driven **Bailout (`/bail`)** mechanic, anti-spam link filtering, SQLite persistence, and a daily **23:59 UTC "Midnight Prison Break"** unfreeze ritual.

---

## ✨ Features

- **🧩 Dynamic Emoji CAPTCHA:**
  - Upon joining, new members are restricted and presented with 4 randomized emoji buttons (e.g. *"Click the 🐶 Dog to prove you are human"*).
  - Protects against automated headless click-bots that easily bypass static "I am Human" buttons.
  - Automatically cancels the kick timer upon success and cleans up verification messages after 5 seconds.
  - Soft-kicks (ban + unban) users who fail or timeout after 90 seconds.

- **🤝 Community Bailout System (`/bail`):**
  - Jailed users receive an interactive `[🤝 Post Bail (0/3)]` button.
  - Active group members can vouch for the prisoner. Once the threshold is met, the inmate is released early!
  - Inmates cannot bail themselves or other inmates.

- **🛡️ HTML Injection & Character Escaping:**
  - Full HTML escaping prevents crashes from markdown injection or special characters in user names (`_`, `*`, `[`, `]`).

- **🔒 Group-Respecting Permissions:**
  - When unmuting or bailing out members, the bot inherits the group's default `chat.permissions` rather than applying arbitrary elevated privileges.

- **🧹 Anti-Spam Link Interceptor:**
  - Automatically deletes unauthorized invite links (`t.me/`, `telegram.me/`) and shorteners (`bit.ly/`, `tinyurl.com/`, `is.gd/`) from non-admin members.
  - Leaves a temporary 5-second auto-deleting warning.

- **🔓 The Midnight Prison Break Ritual (23:59 UTC):**
  - Nightly automated cron job pardons all inmates with humorous warden quotes.
  - Mentions are dispatched in chunks with rate-limit throttles (150ms) to prevent Telegram API 429 errors.

- **⚡ Complete Admin Quality-of-Life Suite:**
  - `/warn` (reply): Issues warning; automatically jails on 3 strikes.
  - `/jail` (reply): Immediately jails a user until midnight or community bail.
  - `/pardon` (reply or `ID`): Manually releases an inmate immediately.
  - `/status` (reply, `ID`, or self): Checks warning count and prison status.
  - `/clearwarns` (reply or `ID`): Resets warnings to 0.
  - `/unfreezeall` / `/unmuteall`: Emergency manual release of all inmates.

---

## 📋 Prerequisites & Telegram Setup

### 1. Create the Bot with @BotFather
1. Open Telegram and search for [`@BotFather`](https://t.me/BotFather).
2. Send `/newbot`, name your bot, and pick a username ending in `bot`.
3. Save the **HTTP API Token**.
4. Send `/setprivacy` to `@BotFather`, select your bot, and set it to **Disable**. *(This is required for the bot to monitor chat messages for link spam).*

### 2. Prepare Your Telegram Group
1. Convert your group to a **Supergroup** (groups with chat history visible to new members or with >200 members are automatically Supergroups).
2. Add your bot to the group.
3. Promote the bot to **Administrator** with the following permissions:
   - ✅ *Delete messages*
   - ✅ *Ban users* (Restrict members)
   - ✅ *Invite users via link*

### 3. Retrieve Your Numeric Group ID
- Forward any message from your Supergroup to [`@JsonDumpBot`](https://t.me/JsonDumpBot) or [`@userinfobot`](https://t.me/userinfobot).
- The ID begins with `-100` (e.g. `-1001234567890`).

---

## 🚀 Quickstart & Installation

### Local / VPS Deployment

1. **Clone the repository:**
   ```bash
   git clone <repo-url>
   cd charming-lovelace
   ```

2. **Create a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and configure your credentials:
   ```env
   TELEGRAM_BOT_TOKEN=1234567890:ABCDefGhIJkLmNoPQRstUVwxyZ
   TARGET_GROUP_ID=-1001234567890
   BAIL_THRESHOLD=3
   CAPTCHA_TIMEOUT=90
   ```

5. **Start the bot:**
   ```bash
   python main.py
   ```

---

## 🌐 24/7 Free Cloud Hosting (Render / Koyeb)

1. Push your repository to GitHub (ensure `.env` and `*.db` are ignored).
2. On [Render](https://render.com) or [Koyeb](https://koyeb.com), create a **Background Worker** (or **Web Service**).
3. Set **Build Command**:
   ```bash
   pip install -r requirements.txt
   ```
4. Set **Start Command**:
   ```bash
   python main.py
   ```
5. In the **Environment Variables** dashboard, add:
   - `TELEGRAM_BOT_TOKEN`: *your bot token*
   - `TARGET_GROUP_ID`: *your numeric supergroup ID*
   - `BAIL_THRESHOLD`: `3`

> [!TIP]
> Render free containers use ephemeral filesystems. For zero-maintenance persistence on cloud containers, attach a persistent disk volume or configure an external database like **Turso** (`libsql`).

---

## 📖 Command Reference

| Command | Target | Permission | Description |
| :--- | :--- | :--- | :--- |
| `/warn` | Reply | Admin | Issues a warning. Strikes 3/3 triggers jail. |
| `/jail` | Reply | Admin | Jails user until midnight or bail. |
| `/pardon` | Reply / ID | Admin | Immediately releases a jailed inmate. |
| `/clearwarns` | Reply / ID | Admin | Clears warning counter back to 0/3. |
| `/status` | Reply / ID / Self | Everyone | Checks criminal record and warnings. |
| `/unfreezeall` | None | Admin | Triggers an immediate mass release. |

---

## 🗄️ Database Architecture

SQLite file: `group_moderator.db`

- **`jail`**: Records incarcerated users (`user_id`, `chat_id`, `first_name`, `username`, `status`, `reason`, `jailed_at`).
- **`warnings`**: Tracks warnings per user (`user_id`, `chat_id`, `warn_count`, `updated_at`).
- **`bail`**: Tracks community vouches (`prisoner_id`, `chat_id`, `voucher_id`, `created_at`) with unique constraints preventing duplicate votes.
