# agy-on-telegram

A lightweight, robust, and feature-rich Telegram Bot gateway to interact directly with Google's **Antigravity CLI (`agy`)** agent from your phone, desktop, or tablet. Built with native cross-platform support for **Linux** and **macOS** (Apple Silicon & Intel).

Whether running on a personal Mac, Linux workstation, home lab, VPS, or NAS, `agy-on-telegram` allows you to trigger coding sessions, run shell tasks, inspect system status, and collaborate with your agent on the go.

---

## ✨ Features

- 🧠 **Dynamic Model Selection & Quota Switcher**:
  - `/model` or `/models`: Interactively choose which AI model to use via Telegram inline buttons (or direct command `/model <name>`).
  - **Persistent across sessions**: The selected model is remembered for both new chats and continuations.
  - **Token exhaustion recovery**: If tokens or quota run out on the current model, the bot notifies you with a one-tap button to switch models and resume with `/c <prompt>`.
- 💬 **Clean New Chat by Default**:
  - Regular text messages and image attachments automatically start a **fresh session** by default.
  - `/c <prompt>` or `/continue <prompt>`: Explicitly continues the previous conversation session.
  - `/new <prompt>`: Explicitly start a fresh session.
- 🎙️ **Voice & Audio Notes (Whisper Integration)**:
  - Send voice notes or audio messages directly to Telegram.
  - Transcribes audio via a local Whisper HTTP endpoint.
  - **Voice intent detection**: Say *"New chat: ..."* or *"Continue with: ..."* in your voice note for hands-free routing, or let the bot present interactive inline buttons (*"Continue session"* vs. *"New session"*).
- 🖼️ **Vision & Multimodal Support**:
  - Send photos, screenshots, or image files directly.
  - Automatically downloads them to a temporary workspace directory and instructs `agy` to inspect and analyze them using file-reading tools. Use `/c` in the caption to continue the prior conversation.
- 🔄 **Fault-Tolerant Auto-Retries**:
  - Automatically retries up to **3 times** with exponential backoff (`2s`, `4s`, `6s`) if a backend error, transient model issue, or server restart occurs.
  - In-place message status updates keeping you informed about progress and reattempts.
- 🛡️ **Access Control & Security**:
  - Restricts execution strictly to authorized Telegram User IDs (`ALLOWED_USER_IDS`).
  - No personal secrets or tokens committed.
- 📊 **Host Status Diagnostics**:
  - Built-in `/status` command reporting active model, host uptime, CPU load average, RAM utilization, and active configuration paths.

---

## 📋 Requirements

1. **Python 3.8+**
2. **Google Antigravity CLI (`agy`)**:
   - Installed and properly authenticated on the host machine.
   - Accessible via PATH or specified through the `AGY_BIN` environment variable.
3. **Telegram Bot Token**:
   - Create a bot and obtain a token via [@BotFather](https://t.me/BotFather).
4. *(Optional)* **Whisper HTTP Service**:
   - If using voice memo transcription, a running Whisper HTTP server (such as `faster-whisper-server` or equivalent) accepting raw audio bytes at a `/transcribe` endpoint.

---

## 🚀 Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/javiergarciagonzalez/agy-on-telegram.git
cd agy-on-telegram
```

### 2. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy the example configuration file:

```bash
cp .env.example .env
```

Open `.env` and fill in your details:

```env
# Your bot token from @BotFather
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ

# Comma-separated list of Telegram User IDs allowed to interact with the bot
ALLOWED_USER_IDS=123456789

# Optional: Adjust AGY path or workspace directory if not standard
# AGY_BIN=/home/user/.local/bin/agy
# WORKSPACE_DIR=/home/user
# WHISPER_ENDPOINT=http://127.0.0.1:8765/transcribe
```

> **Tip:** You can find your Telegram User ID using bots like [@userinfobot](https://t.me/userinfobot).

### 4. Run the bot

```bash
python3 bot.py
```

Send `/start` to your bot on Telegram to verify the connection!

---

## 🛠️ Running in Background

### On Linux (Systemd Service)

To keep the bot running continuously in the background and ensure it automatically restarts on system reboot, configure it as a **systemd user service**:

1. Copy the provided unit file:
   ```bash
   mkdir -p ~/.config/systemd/user
   cp systemd/agy-telegram-bot.service ~/.config/systemd/user/
   ```

2. Adjust paths in `~/.config/systemd/user/agy-telegram-bot.service` if your installation directory differs from `~/agy-on-telegram`.

3. Reload and enable the service:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now agy-telegram-bot
   ```

4. Check the logs:
   ```bash
   journalctl --user -u agy-telegram-bot -f
   ```

### On macOS (LaunchAgent)

To run the bot as a background service on macOS (Apple Silicon or Intel) managed by `launchd`:

1. Copy the provided LaunchAgent plist:
   ```bash
   mkdir -p ~/Library/LaunchAgents
   cp launchd/com.user.agy-telegram-bot.plist ~/Library/LaunchAgents/
   ```

2. Edit `~/Library/LaunchAgents/com.user.agy-telegram-bot.plist` and update `YOUR_USERNAME` and your Python path (e.g. `/opt/homebrew/bin/python3` or virtual environment Python).

3. Load and start the agent:
   ```bash
   launchctl load ~/Library/LaunchAgents/com.user.agy-telegram-bot.plist
   ```

4. View logs:
   ```bash
   tail -f /tmp/agy-telegram-bot.log
   ```

To stop or unload:
```bash
launchctl unload ~/Library/LaunchAgents/com.user.agy-telegram-bot.plist
```

---

## 💬 Command Reference

| Command | Description |
| :--- | :--- |
| `/start`, `/help` | Displays the help message, active model, and usage instructions. |
| `/model`, `/models` | Shows an interactive menu to view and select the active AI model (persisted for new chats and continuations). Can also be run as `/model <name>`. |
| `/c <prompt>` | Continues the previous session with the given instruction. |
| `<plain text>` | Direct prompt execution (always starts a **fresh session** unless `/c` is specified). |
| `/new <prompt>` | Explicitly runs a prompt starting in a fresh session. |
| `/status` | Shows active AI model, server uptime, CPU load, memory usage, and service configuration. |
| *Send Photo / Document* | Analyzes the attached image. Caption starting with `/c` continues previous session; otherwise starts fresh. |
| *Send Voice Note* | Transcribes audio with Whisper and routes to AGY. |

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
