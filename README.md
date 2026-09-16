# 🛡️ Warden Bot

<p align="center">
  <strong>High-Performance Telegram Supergroup Moderation & Protection Engine</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python Version" />
  <img src="https://img.shields.io/badge/Telegram%20Bot%20API-v21+-0088cc?style=for-the-badge&logo=telegram&logoColor=white" alt="Telegram Bot API" />
  <img src="https://img.shields.io/badge/Docker-Ready-2496ed?style=for-the-badge&logo=docker&logoColor=white" alt="Docker Ready" />
  <img src="https://img.shields.io/badge/Database-SQLite%20WAL-003B57?style=for-the-badge&logo=sqlite&logoColor=white" alt="SQLite WAL" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License MIT" />
</p>

---

## 🌟 Overview

**Warden Bot** is a production-grade, multi-tenant moderation bot engineered for Telegram Supergroups. Designed to combat sophisticated spam waves, crypto link injectors, and automated bot raids, Warden equips group admins with granular security controls configured directly within Telegram.

Featuring **dynamic Emoji CAPTCHAs**, **velocity-based Anti-Raid lockdowns**, **Anonymous Channel Shielding**, community-driven **Bailout mechanics (`/bail`)**, **process-surviving timed mutes**, and daily **Midnight Crime Reports (23:59 UTC)**, Warden keeps your community clean, engaging, and resilient.

---

## ✨ Core Features

### 🧩 1. Dynamic Emoji CAPTCHA Gatekeeper
- Automatically mutes newly joined members upon entering the chat.
- Generates a randomized target emoji challenge with decoy buttons (e.g., *"Click the 🐶 Dog to verify"*).
- Defeats headless browser click-bots that blindly target generic "I am Human" webhooks.
- Configurable timeout (30s, 60s, 90s, 120s) with soft-kick on failure or timeout.

### 🚨 2. Anti-Raid Velocity Lockdown
- Tracks join bursts in real-time within a sliding observation window (e.g., >5 joins in 10s).
- Triggers an automated lockdown during raids, kicking incoming bot accounts silently.
- Adjustable sensitivity (`High: 3j/10s`, `Normal: 5j/10s`, `Relaxed: 10j/10s`) and customizable lockdown duration (1m to 15m).

### 🎭 3. Anonymous Channel Impersonator Shield
- Scammers frequently post on behalf of public channels to bypass user bans and push drainers.
- Automatically removes unauthorized channel posts while whitelisting the supergroup itself and any official linked broadcast channels.

### 🤝 4. Community Bailout Mechanic (`/bail`)
- Incarcerated members receive an interactive inline `[🤝 Post Bail (0/3)]` card.
- Established members can vouch for jailed users. Once the configurable threshold is met, the user is automatically unmuted before midnight.
- Prevents prisoner self-bailing or inmate-to-inmate collusion.

### ⏳ 5. Timed Mutes (`/mute` & `/unmute`)
- Apply temporary silences with natural duration syntax (`/mute 15m`, `/mute 2h`, `/mute 1d`).
- **Reboot & Crash Resilient:** Active timed mutes are saved to SQLite and automatically restored on bot restart.

### 🛡️ 6. Unicode & Zero-Width Anti-Spam Filter
- Sanitizes zero-width spaces (`\u200b`, `\ufeff`) and homoglyphs using Unicode NFKD normalization.
- Scans message text, media captions, and embedded rich hyperlinks (`MessageEntity.TEXT_LINK` and `URL`).

### ⚙️ 7. In-Chat Interactive `/settings` Dashboard
- Admins configure all group policies right inside the chat via an interactive inline menu.
- Changes apply instantly with zero bot restarts or server access required.

### 📊 8. Midnight Prison Break & Crime Report (23:59 UTC)
- Executes a daily mass unfreeze of remaining jailed members with witty parole quotes.
- Broadcasts aggregated daily statistics:
  - *Inmates Pardoned*
  - *Spam Links Intercepted*
  - *Community Bails Granted*
  - *Raid Incursions Deflected*

### 📋 9. Dedicated Audit Log Channel (`/setlog`)
- Route all moderation actions, kicks, warnings, and anti-spam detections to a private staff channel.

---

## 🚀 Quickstart Guide

### Option A: Docker Compose (Recommended)

1. **Clone the repository:**
   ```bash
   git clone https://github.com/s47user/warden-bot.git
   cd warden-bot
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   nano .env  # Add your TELEGRAM_BOT_TOKEN from @BotFather
   ```

3. **Start the container:**
   ```bash
   docker compose up -d --build
   ```

4. **Monitor logs:**
   ```bash
   docker compose logs -f warden-bot
   ```

Data is stored persistently in the named volume `bot_data` at `/app/data/group_moderator.db`.

---

### Option B: Local / VPS Installation

#### Prerequisites
- Python 3.11 or 3.12
- Git

1. **Clone & setup virtual environment:**
   ```bash
   git clone https://github.com/s47user/warden-bot.git
   cd warden-bot
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. **Configure environment variables:**
   ```bash
   cp .env.example .env
   nano .env
   ```

4. **Launch the bot:**
   ```bash
   python main.py
   ```

---

## ⚙️ Configuration Reference

All settings can be specified in `.env` as global fallbacks or customized per-group in Telegram via `/settings`:

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | *Required* | API token generated via [@BotFather](https://t.me/BotFather). |
| `TARGET_GROUP_ID` | `0` | Default group ID (`0` enables multi-tenant support for all groups). |
| `LOG_CHANNEL_ID` | `0` | Fallback audit log channel ID (`0` to disable by default). |
| `BAIL_THRESHOLD` | `3` | Default community vouches required to release a prisoner. |
| `CAPTCHA_TIMEOUT` | `90` | Time window (in seconds) for new members to solve the CAPTCHA. |
| `RAID_THRESHOLD` | `5` | Maximum joins permitted inside the observation window. |
| `RAID_WINDOW` | `10` | Observation window (in seconds) for join surge detection. |
| `LOCKDOWN_DURATION`| `300` | Duration (in seconds) of panic auto-kick mode during a raid. |
| `DB_FILE` | `group_moderator.db` | Path to the SQLite database file. |

---

## 📋 Command Reference

### Admin Commands

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `/settings` / `/setup` | None | Opens the interactive moderation settings dashboard. |
| `/setlog` | `<channel_id> \| off` | Links or disconnects an audit log channel for this group. |
| `/warn` | `[reason]` *(on reply)* | Issues a warning strike. User is jailed upon reaching 3 strikes. |
| `/clearwarns` | *(on reply / ID)* | Resets warning strikes back to `0/3`. |
| `/jail` | `[reason]` *(on reply)* | Incarcerates member until midnight UTC or community bailout. |
| `/mute` | `[15m/2h/1d]` *(on reply)* | Applies a temporary timed mute (persists across restarts). |
| `/unmute` | *(on reply / ID)* | Lifts an active mute immediately. |
| `/pardon` | *(on reply / ID)* | Immediately releases and pardons an incarcerated member. |
| `/unfreezeall` | None | Emergency command to unfreeze all current inmates. |

### Member Commands

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `/status` | *(reply / ID / self)* | Inspects warnings and incarceration history. |
| `/rules` | None | Shows group safety rules (dynamically reflects chat settings). |
| `/help` | None | Displays the bot user and admin guide. |

---

## 🛡️ Supergroup Permissions Checklist

To enable Warden Bot to protect your group, add the bot to your Telegram Supergroup and grant the following **Administrator Rights**:

- ✅ **Delete Messages** (required for spam removal & channel shield)
- ✅ **Ban Users** / **Restrict Members** (required for CAPTCHA mutes, timed timeouts & jail sentences)

---

## 🏗️ Architecture & Production Hardening

- **SQLite WAL & Concurrency Hardening:**
  Initializes with `PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;`, and a 5-second busy timeout to avoid write contention.
- **Sliding-Window Telegram Rate Limiter:**
  Custom `ProductionRateLimiter` subclassing `BaseRateLimiter`. Enforces global limits (25 req/s) and group-specific rates (20 req/60s), handling Telegram `RetryAfter` (429) backoffs cleanly.
- **Process-Surviving Timed Mutes:**
  Mute expiry timestamps are stored in SQLite and recovered upon restart through a `post_init` hook.
- **Admin Status TTL Caching:**
  Caches admin lists with an in-memory TTL and LRU-style size bounds to prevent Telegram 429 errors on high-traffic groups.
- **Non-Root Container Security:**
  Runs as an unprivileged user (`appuser`, UID 1000) inside Docker with json-file log rotation.

---

## 🤝 Contributing

Contributions are welcome! If you have suggestions or find issues:
1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'feat: add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📄 License

Distributed under the **MIT License**. See [`LICENSE`](LICENSE) for more information.
