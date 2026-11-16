#!/usr/bin/env python3
# ==============================================================================
# agy-on-telegram - Lightweight Telegram Gateway for AGY (Antigravity CLI)
# ==============================================================================
import os
import sys
import re
import html
import time
import json
import uuid
import signal
import shutil
import logging
import getpass
import tempfile
import subprocess
import threading
from typing import List, Optional, Tuple
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("agy-bot")

# --- Configuration ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED_USERS = [u.strip() for u in ALLOWED_USERS_RAW.split(",") if u.strip()]

WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", os.path.expanduser("~")).strip()

# Path resolution with cross-platform support (Linux + macOS / Homebrew)
def find_agy_binary() -> str:
    env_bin = os.getenv("AGY_BIN", "").strip()
    if env_bin:
        return env_bin
    which_bin = shutil.which("agy")
    if which_bin:
        return which_bin
    candidates = [
        os.path.expanduser("~/.local/bin/agy"),
        "/opt/homebrew/bin/agy",  # macOS Apple Silicon
        "/usr/local/bin/agy",     # macOS Intel / Linux
    ]
    for c in candidates:
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return os.path.expanduser("~/.local/bin/agy")

AGY_BIN = find_agy_binary()
WHISPER_ENDPOINT = os.getenv("WHISPER_ENDPOINT", "http://127.0.0.1:8765/transcribe").strip()

DEFAULT_IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
IMAGE_DIR = os.getenv("IMAGE_DIR", DEFAULT_IMAGE_DIR).strip()
os.makedirs(IMAGE_DIR, exist_ok=True)

if not TOKEN:
    logger.error("TELEGRAM_BOT_TOKEN is not set. Please configure it in your environment or .env file.")
    sys.exit(1)

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
FILE_BASE_URL = f"https://api.telegram.org/file/bot{TOKEN}"
RUNNING = True

# In-memory storage for pending audio task confirmations
# {task_id: {"text": str, "chat_id": int, "created_at": float}}
pending_audio_tasks = {}

# --- Persistent Model Preferences & Model Registry ---
PREFERENCES_FILE = os.getenv(
    "PREFERENCES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "preferences.json"),
).strip()

# --- Persistent Message -> AGY Conversation Mapping ---
CONVERSATIONS_FILE = os.getenv(
    "CONVERSATIONS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "conversations.json"),
).strip()
MAX_CONVERSATION_ENTRIES = 2000
_conversations_lock = threading.Lock()


def _load_conversations_unlocked() -> dict:
    try:
        if os.path.exists(CONVERSATIONS_FILE) and os.path.getsize(CONVERSATIONS_FILE) > 0:
            with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logger.warning(f"Error loading conversations from {CONVERSATIONS_FILE}: {e}")
    return {}


def save_message_conversations(chat_id: int, message_ids: List[Optional[int]], conversation_id: Optional[str]):
    """Persistently map one or more Telegram message IDs to an AGY conversation ID."""
    if not conversation_id or not message_ids:
        return
    valid_ids = [mid for mid in message_ids if mid is not None]
    if not valid_ids:
        return

    with _conversations_lock:
        data = _load_conversations_unlocked()
        now = time.time()
        for mid in valid_ids:
            data[f"{chat_id}:{mid}"] = {
                "conversation_id": conversation_id,
                "updated_at": now,
            }

        if len(data) > MAX_CONVERSATION_ENTRIES:
            sorted_items = sorted(
                data.items(),
                key=lambda item: item[1].get("updated_at", 0) if isinstance(item[1], dict) else 0,
                reverse=True,
            )
            data = dict(sorted_items[:MAX_CONVERSATION_ENTRIES])

        try:
            tmp_path = f"{CONVERSATIONS_FILE}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, CONVERSATIONS_FILE)
        except Exception as e:
            logger.warning(f"Error saving conversations to {CONVERSATIONS_FILE}: {e}")


def get_conversation_for_message(chat_id: int, message_id: Optional[int]) -> Optional[str]:
    """Look up the AGY conversation ID associated with a given Telegram message ID."""
    if message_id is None:
        return None
    with _conversations_lock:
        data = _load_conversations_unlocked()
        entry = data.get(f"{chat_id}:{message_id}")
        if isinstance(entry, dict):
            return entry.get("conversation_id")
        if isinstance(entry, str):
            return entry
    return None


def get_replied_conversation_id(msg: dict) -> Optional[str]:
    """If msg is a Telegram reply to a tracked message, return its AGY conversation ID."""
    reply_to = msg.get("reply_to_message")
    if not isinstance(reply_to, dict):
        return None
    chat_id = msg.get("chat", {}).get("id")
    replied_msg_id = reply_to.get("message_id")
    if chat_id is None or replied_msg_id is None:
        return None
    return get_conversation_for_message(chat_id, replied_msg_id)

DEFAULT_MODELS = [
    ("gemini-4-argon-medium", "Gemini 4 Argon (Medium)"),
    ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
    ("gemini-4-argon-low", "Gemini 4 Argon (Low)"),
    ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
    ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
    ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
    ("claude-opus-5-5-low", "Claude Opus 5.5 (Low)"),
    ("claude-opus-5-5-medium", "Claude Opus 5.5 (Medium)"),
    ("claude-opus-5-5-high", "Claude Opus 5.5 (High)"),
    ("claude-sonnet-5-5-low", "Claude Sonnet 5.5 (Low)"),
    ("claude-sonnet-5-5-medium", "Claude Sonnet 5.5 (Medium)"),
    ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)"),
    ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
]

MODELS_CACHE = {"models": [], "timestamp": 0.0}


def load_preferences() -> dict:
    try:
        if os.path.exists(PREFERENCES_FILE) and os.path.getsize(PREFERENCES_FILE) > 0:
            with open(PREFERENCES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Error loading preferences from {PREFERENCES_FILE}: {e}")
    return {}


def save_preferences(prefs: dict):
    try:
        tmp_path = f"{PREFERENCES_FILE}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, PREFERENCES_FILE)
    except Exception as e:
        logger.error(f"Error saving preferences to {PREFERENCES_FILE}: {e}")


def get_available_models() -> List[Tuple[str, str]]:
    """Fetch available models from AGY CLI, with caching and fallback."""
    now = time.time()
    if MODELS_CACHE["models"] and (now - MODELS_CACHE["timestamp"] < 3600):
        return MODELS_CACHE["models"]

    try:
        res = subprocess.run(
            [AGY_BIN, "models"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0 and res.stdout.strip():
            models = []
            for line in res.stdout.strip().splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    models.append((parts[0].strip(), parts[1].strip()))
                elif len(parts) == 1 and parts[0].strip():
                    models.append((parts[0].strip(), parts[0].strip()))
            if models:
                MODELS_CACHE["models"] = models
                MODELS_CACHE["timestamp"] = now
                return models
    except Exception as e:
        logger.warning(f"Failed to fetch models from AGY CLI: {e}")

    if not MODELS_CACHE["models"]:
        MODELS_CACHE["models"] = DEFAULT_MODELS
        MODELS_CACHE["timestamp"] = now
    return MODELS_CACHE["models"]


def get_current_model(chat_id: Optional[int] = None) -> str:
    """Retrieve persisted model for this chat or global default."""
    prefs = load_preferences()
    if chat_id is not None:
        chat_model = prefs.get("chat_models", {}).get(str(chat_id))
        if chat_model:
            return chat_model
    default_model = prefs.get("default_model")
    if default_model:
        return default_model
    return "gemini-3.8-flash-medium"


def set_current_model(model_id: str, chat_id: Optional[int] = None):
    """Persist chosen model for chat and global default."""
    prefs = load_preferences()
    prefs["default_model"] = model_id
    if chat_id is not None:
        if "chat_models" not in prefs:
            prefs["chat_models"] = {}
        prefs["chat_models"][str(chat_id)] = model_id
    save_preferences(prefs)
    logger.info(f"Model updated to '{model_id}' (chat_id={chat_id})")


def get_model_label(model_id: str) -> str:
    """Return friendly label for a model ID."""
    for m_id, label in get_available_models():
        if m_id == model_id:
            return label
    return model_id


def build_model_selection_message(chat_id: int) -> Tuple[str, dict]:
    """Build message and inline keyboard for model selection."""
    current_model = get_current_model(chat_id)
    models = get_available_models()
    current_label = get_model_label(current_model)

    text = (
        f"🤖 **Selección de Modelo de AGY**\n\n"
        f"• **Modelo actual:** `{current_label}` (`{current_model}`)\n\n"
        f"Toca un modelo de la lista para activarlo. Esta selección se recordará tanto para **nuevos chats** como para **continuaciones** (`/c`):\n"
    )

    keyboard_buttons = []
    for m_id, label in models:
        is_active = (m_id == current_model)
        btn_text = f"✅ {label}" if is_active else label
        keyboard_buttons.append([{"text": btn_text, "callback_data": f"set_model:{m_id}"}])

    keyboard = {"inline_keyboard": keyboard_buttons}
    return text, keyboard


def handle_signal(sig, frame):
    global RUNNING
    logger.info(f"Received signal {sig}, shutting down...")
    RUNNING = False


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def _clean_table_cell(cell: str) -> str:
    """Strip inline markdown syntax inside table cells so ASCII column alignment stays accurate."""
    c = cell.strip()
    c = re.sub(r"\[`?([^`\]]+)`?\]\([^)\s]+\)", r"\1", c)
    c = re.sub(r"\*\*(.+?)\*\*", r"\1", c)
    c = re.sub(r"`([^`]+)`", r"\1", c)
    return c


def _format_markdown_table(table_lines: List[str]) -> str:
    """Convert a Markdown pipe table into an aligned monospace <pre> block for Telegram."""
    rows = []
    for line in table_lines:
        stripped = line.strip()
        if re.match(r"^\|[\s:|-]+\|$", stripped):
            continue
        cells = [_clean_table_cell(c) for c in stripped.strip("|").split("|")]
        rows.append(cells)

    if not rows:
        return html.escape("\n".join(table_lines))

    num_cols = max(len(r) for r in rows)
    col_widths = [0] * num_cols
    for r in rows:
        for i, val in enumerate(r):
            col_widths[i] = max(col_widths[i], len(val))

    formatted_lines = []
    for r_idx, r in enumerate(rows):
        padded = [
            (r[i] if i < len(r) else "").ljust(col_widths[i])
            for i in range(num_cols)
        ]
        formatted_lines.append(" │ ".join(padded).rstrip())
        if r_idx == 0 and len(rows) > 1:
            sep = "─┼─".join("─" * w for w in col_widths)
            formatted_lines.append(sep)

    return f"<pre>{html.escape(chr(10).join(formatted_lines))}</pre>"


def markdown_to_telegram_html(text: str) -> str:
    """
    Convert GitHub-Flavored Markdown (as emitted by AGY) into clean Telegram HTML.
    - Strips unsupported file:// links into monospace <code> tags while preserving http(s):// links.
    - Converts headings (#, ##, ###), bold (**), italic (* / _), strikethrough (~~),
      fenced code blocks (```), inline code (`), blockquotes (>), task lists, bullets, and tables.
    """
    if not text:
        return ""

    placeholders: List[str] = []

    def add_placeholder(html_fragment: str) -> str:
        idx = len(placeholders)
        placeholders.append(html_fragment)
        return f"\x00TGPH{idx}END\x00"

    # 1. Extract fenced code blocks: ```lang\n...\n```
    def replace_fenced_code(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        code = m.group(2)
        if code.startswith("\n"):
            code = code[1:]
        code = code.rstrip("\n")
        escaped_code = html.escape(code)
        if lang:
            return add_placeholder(
                f'<pre><code class="language-{html.escape(lang, quote=True)}">{escaped_code}</code></pre>'
            )
        return add_placeholder(f"<pre><code>{escaped_code}</code></pre>")

    text = re.sub(r"```([a-zA-Z0-9_+-]*)[ \t]*\n?(.*?)```", replace_fenced_code, text, flags=re.DOTALL)

    # 2. Extract Markdown pipe tables into aligned <pre> blocks
    lines = text.splitlines()
    out_lines: List[str] = []
    i = 0
    while i < len(lines):
        if (
            lines[i].strip().startswith("|")
            and lines[i].strip().endswith("|")
            and i + 1 < len(lines)
            and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1])
        ):
            table_buf = [lines[i], lines[i + 1]]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                table_buf.append(lines[i])
                i += 1
            out_lines.append(add_placeholder(_format_markdown_table(table_buf)))
        else:
            out_lines.append(lines[i])
            i += 1
    text = "\n".join(out_lines)

    # 3. Extract links wrapping inline code: [`symbol`](url)
    def replace_code_link(m: re.Match) -> str:
        label = m.group(1).strip()
        url = m.group(2).strip()
        if url.startswith(("http://", "https://", "tg://")):
            return add_placeholder(
                f'<a href="{html.escape(url, quote=True)}"><code>{html.escape(label)}</code></a>'
            )
        return add_placeholder(f"<code>{html.escape(label)}</code>")

    text = re.sub(r"\[`([^`\n]+)`\]\(([^)\s]+)\)", replace_code_link, text)

    # 4. Extract inline code spans: `code`
    def replace_inline_code(m: re.Match) -> str:
        return add_placeholder(f"<code>{html.escape(m.group(1))}</code>")

    text = re.sub(r"`([^`\n]+)`", replace_inline_code, text)

    # 5. Extract standard Markdown links: [label](url)
    def replace_standard_link(m: re.Match) -> str:
        label = m.group(1).strip().strip("`")
        url = m.group(2).strip()
        if url.startswith(("http://", "https://", "tg://")):
            return add_placeholder(
                f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
            )
        return add_placeholder(f"<code>{html.escape(label)}</code>")

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", replace_standard_link, text)

    # 6. Escape remaining raw HTML characters
    text = html.escape(text, quote=False)

    # 7. Process line-level elements (blockquotes, headings, lists, horizontal rules)
    raw_lines = text.splitlines()
    processed_lines: List[str] = []
    quote_buffer: List[str] = []

    alert_map = {
        "[!NOTE]": "ℹ️ <b>Nota</b>",
        "[!TIP]": "💡 <b>Consejo</b>",
        "[!IMPORTANT]": "📌 <b>Importante</b>",
        "[!WARNING]": "⚠️ <b>Aviso</b>",
        "[!CAUTION]": "🚨 <b>Precaución</b>",
    }

    def flush_quote():
        if quote_buffer:
            content = "\n".join(quote_buffer).strip()
            processed_lines.append(f"<blockquote>{content}</blockquote>")
            quote_buffer.clear()

    for line in raw_lines:
        q_match = re.match(r"^\s*&gt;\s?(.*)$", line)
        if q_match:
            q_content = q_match.group(1)
            for tag, replacement in alert_map.items():
                if q_content.strip().startswith(tag):
                    rest = q_content.strip()[len(tag):].strip()
                    q_content = f"{replacement}\n{rest}" if rest else replacement
                    break
            quote_buffer.append(q_content)
            continue
        else:
            flush_quote()

        # Horizontal rules
        if re.match(r"^\s*(?:-\s*){3,}$|^\s*(?:\*\s*){3,}$|^\s*(?:_\s*){3,}$", line):
            processed_lines.append("────────────────────")
            continue

        # Headings
        h1 = re.match(r"^#\s+(.+)$", line)
        if h1:
            processed_lines.append(f"\n📌 <b>{h1.group(1).strip('* ')}</b>")
            continue
        h2 = re.match(r"^##\s+(.+)$", line)
        if h2:
            processed_lines.append(f"\n🔹 <b>{h2.group(1).strip('* ')}</b>")
            continue
        h3 = re.match(r"^###\s+(.+)$", line)
        if h3:
            processed_lines.append(f"\n▸ <b>{h3.group(1).strip('* ')}</b>")
            continue
        h4 = re.match(r"^#{4,6}\s+(.+)$", line)
        if h4:
            processed_lines.append(f"• <b>{h4.group(1).strip('* ')}</b>")
            continue

        # Task lists
        task_done = re.match(r"^(\s*)[-*]\s+\[[xX]\]\s+(.+)$", line)
        if task_done:
            indent = "  " * (len(task_done.group(1)) // 2)
            processed_lines.append(f"{indent}✅ {task_done.group(2)}")
            continue
        task_todo = re.match(r"^(\s*)[-*]\s+\[\s\]\s+(.+)$", line)
        if task_todo:
            indent = "  " * (len(task_todo.group(1)) // 2)
            processed_lines.append(f"{indent}⬜ {task_todo.group(2)}")
            continue

        # Unordered bullet lists
        bullet = re.match(r"^(\s*)[-*]\s+(.+)$", line)
        if bullet:
            indent = "  " * (len(bullet.group(1)) // 2)
            processed_lines.append(f"{indent}• {bullet.group(2)}")
            continue

        processed_lines.append(line)

    flush_quote()
    text = "\n".join(processed_lines)

    # 8. Inline formatting: bold, italic, strikethrough
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w\\])_([^_\n]+?)_(?![\w])", r"<i>\1</i>", text)

    # 9. Restore placeholders
    for idx, fragment in enumerate(placeholders):
        text = text.replace(f"\x00TGPH{idx}END\x00", fragment)

    # Clean excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def split_text(text: str, max_len: int = 3500) -> List[str]:
    """
    Split Markdown text into chunks that fit within Telegram limits after HTML conversion,
    automatically closing and reopening fenced code blocks across chunk boundaries.
    """
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    lines = text.splitlines(keepends=True)
    current_chunk: List[str] = []
    current_len = 0
    open_fence: Optional[str] = None

    for line in lines:
        if current_len + len(line) > max_len:
            if current_chunk:
                if open_fence is not None:
                    current_chunk.append("\n```")
                chunks.append("".join(current_chunk))
                current_chunk = []
                current_len = 0
                if open_fence is not None:
                    reopen = f"{open_fence}\n"
                    current_chunk.append(reopen)
                    current_len += len(reopen)

            while len(line) > max_len:
                part = line[:max_len]
                if open_fence is not None:
                    chunks.append(f"{open_fence}\n{part}\n```")
                else:
                    chunks.append(part)
                line = line[max_len:]

        fence_match = re.match(r"^\s*(```[a-zA-Z0-9_+-]*)\s*$", line)
        if fence_match:
            if open_fence is None:
                open_fence = fence_match.group(1)
            else:
                open_fence = None

        current_chunk.append(line)
        current_len += len(line)

    if current_chunk:
        if open_fence is not None:
            current_chunk.append("\n```")
        chunks.append("".join(current_chunk))

    return chunks


def tg_request(method: str, data: dict, timeout: int = 35) -> Optional[dict]:
    url = f"{BASE_URL}/{method}"
    try:
        resp = requests.post(url, json=data, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        logger.warning(f"Telegram API {method} returned {resp.status_code}: {resp.text}")
        return resp.json()
    except Exception as e:
        logger.error(f"Error calling {method}: {e}")
        return None


def send_chat_action(chat_id: int, action: str = "typing"):
    tg_request("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=10)


def send_message(
    chat_id: int,
    text: str,
    reply_to_id: Optional[int] = None,
    reply_markup: Optional[dict] = None,
) -> List[int]:
    """Send message formatted as Telegram HTML, falling back to plain text if parsing fails."""
    chunks = split_text(text)
    sent_ids = []

    for idx, chunk in enumerate(chunks):
        html_chunk = markdown_to_telegram_html(chunk)
        payload = {
            "chat_id": chat_id,
            "text": html_chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_to_id and not sent_ids:
            payload["reply_to_message_id"] = reply_to_id
        if reply_markup and idx == len(chunks) - 1:
            payload["reply_markup"] = reply_markup

        res = tg_request("sendMessage", payload)
        if res and res.get("ok"):
            sent_ids.append(res["result"]["message_id"])
        else:
            payload.pop("parse_mode", None)
            payload["text"] = chunk
            res2 = tg_request("sendMessage", payload)
            if res2 and res2.get("ok"):
                sent_ids.append(res2["result"]["message_id"])

    return sent_ids


def edit_message(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[dict] = None,
    out_message_ids: Optional[List[int]] = None,
) -> bool:
    """Edit an existing message using Telegram HTML formatting, supporting optional inline keyboards."""
    if out_message_ids is not None:
        out_message_ids.append(message_id)

    chunks = split_text(text)
    first_chunk = chunks[0]
    html_first = markdown_to_telegram_html(first_chunk)

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": html_first,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    res = tg_request("editMessageText", payload)
    if not (res and res.get("ok")):
        payload.pop("parse_mode", None)
        payload["text"] = first_chunk
        res2 = tg_request("editMessageText", payload)
        success = bool(res2 and res2.get("ok"))
    else:
        success = True

    if len(chunks) > 1:
        for chunk in chunks[1:]:
            extra_ids = send_message(chat_id, chunk)
            if out_message_ids is not None and extra_ids:
                out_message_ids.extend(extra_ids)

    return success


def answer_callback_query(callback_query_id: str, text: Optional[str] = None):
    data = {"callback_query_id": callback_query_id}
    if text:
        data["text"] = text
    tg_request("answerCallbackQuery", data, timeout=5)


def download_telegram_file(file_id: str, dest_path: str) -> bool:
    """Download file from Telegram servers."""
    res = tg_request("getFile", {"file_id": file_id})
    if not (res and res.get("ok")):
        logger.error(f"Failed to getFile info for {file_id}: {res}")
        return False
    file_path = res["result"].get("file_path")
    if not file_path:
        return False

    download_url = f"{FILE_BASE_URL}/{file_path}"
    try:
        with requests.get(download_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"Failed to download file {file_path}: {e}")
        return False


def transcribe_audio_bytes(audio_bytes: bytes) -> Optional[dict]:
    """Send audio bytes to local Whisper HTTP endpoint."""
    try:
        resp = requests.post(
            WHISPER_ENDPOINT,
            data=audio_bytes,
            headers={"Content-Type": "application/octet-stream"},
            timeout=120,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.error(f"Whisper endpoint returned {resp.status_code}: {resp.text}")
        return None
    except Exception as e:
        logger.error(f"Error calling Whisper endpoint {WHISPER_ENDPOINT}: {e}")
        return None


def detect_session_intent(raw_text: str) -> Tuple[Optional[bool], str]:
    """
    Detects if user explicitly specified a NEW session or CONTINUATION in the audio.
    Returns:
        - (False, clean_prompt): Explicitly NEW session requested
        - (True, clean_prompt): Explicitly CONTINUE session requested
        - (None, raw_prompt): Unspecified, prompt user with buttons
    """
    text = raw_text.strip()
    words = text.split()
    first_chunk = " ".join(words[:8]).lower()

    new_kw = [
        "nuevo chat", "nueva conversación", "nueva conversacion",
        "nueva sesión", "nueva sesion", "nueva tarea", "nuevo hilo",
        "sesión limpia", "sesion limpia", "de cero", "desde cero",
        "empezar de cero", "empezando de cero", "new chat", "new session",
        "start from scratch", "fresh session"
    ]

    cont_kw = [
        "continúa con lo anterior", "continua con lo anterior",
        "continúa con", "continua con", "continúa", "continua", "continuar",
        "en el mismo chat", "en la misma sesión", "en la misma sesion",
        "en la misma conversación", "en la misma conversacion",
        "siguiendo con lo anterior", "siguiendo con", "sigue con", "sigue",
        "continue with", "continue session", "keep going"
    ]

    for kw in new_kw:
        if kw in first_chunk:
            pattern = rf"^(.*?){re.escape(kw)}[\s,.:;—-]*(?:y\s+|que\s+|por\s+favor\s*,?\s*|and\s+|please\s*,?\s*)?"
            cleaned = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE).strip()
            if cleaned:
                cleaned = cleaned[0].upper() + cleaned[1:]
            return False, cleaned if cleaned else text

    for kw in cont_kw:
        if kw in first_chunk:
            pattern = rf"^(.*?){re.escape(kw)}[\s,.:;—-]*(?:con\s+lo\s+anterior\s*,?\s*|de\s+antes\s*,?\s*|y\s+|que\s+|por\s+favor\s*,?\s*|with\s+|please\s*,?\s*)?"
            cleaned = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE).strip()
            if cleaned:
                cleaned = cleaned[0].upper() + cleaned[1:]
            return True, cleaned if cleaned else text

    return None, text


def _parse_agy_json_output(raw_stdout: str) -> Tuple[str, Optional[str]]:
    """Parse AGY JSON output into (response_text, conversation_id)."""
    text = raw_stdout.strip()
    if not text:
        return "✅ Tarea completada sin salida de texto.", None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            conv_id = data.get("conversation_id")
            resp = str(data.get("response") or "").strip()
            return (resp if resp else "✅ Tarea completada sin salida de texto."), conv_id
    except json.JSONDecodeError:
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        conv_id = data.get("conversation_id")
                        resp = str(data.get("response") or "").strip()
                        return (resp if resp else "✅ Tarea completada sin salida de texto."), conv_id
                except json.JSONDecodeError:
                    pass
    return text, None


def run_agy_subprocess(
    prompt: str,
    continue_session: bool,
    chat_id: int,
    status_msg_id: Optional[int] = None,
    max_retries: int = 3,
    model: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Run AGY in non-interactive print mode with periodic typing indicators and automatic retries."""
    if model is None:
        model = get_current_model(chat_id)
    model_label = get_model_label(model)
    if conversation_id:
        action_label = "Continuar hilo 💬"
    elif continue_session:
        action_label = "Continuar sesión 💬"
    else:
        action_label = "Nuevo chat 🆕"

    cmd = [AGY_BIN]
    if conversation_id:
        cmd.extend(["--conversation", conversation_id])
    elif continue_session:
        cmd.append("-c")
    if model:
        cmd.extend(["--model", model])
    cmd.extend(["-p", prompt, "--output-format", "json", "--dangerously-skip-permissions"])

    logger.info(f"Running AGY command: {' '.join(cmd)}")

    env = os.environ.copy()
    env["HOME"] = WORKSPACE_DIR
    env["USER"] = os.environ.get("USER", os.environ.get("USERNAME", getpass.getuser()))
    user_local_bin = os.path.join(env["HOME"], ".local", "bin")
    env["PATH"] = f"{user_local_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")

    stop_heartbeat = threading.Event()

    def heartbeat():
        while not stop_heartbeat.is_set():
            send_chat_action(chat_id, "typing")
            stop_heartbeat.wait(4.0)

    t = threading.Thread(target=heartbeat, daemon=True)
    t.start()

    last_return_code = None
    last_error = ""
    total_attempts = max_retries + 1

    try:
        for attempt in range(1, total_attempts + 1):
            logger.info(f"AGY execution attempt {attempt}/{total_attempts}: {' '.join(cmd)}")
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=WORKSPACE_DIR,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                stdout, stderr = proc.communicate()
                last_return_code = proc.returncode

                if last_return_code == 0:
                    parsed_text, parsed_conv_id = _parse_agy_json_output(stdout)
                    return parsed_text, (parsed_conv_id or conversation_id)

                last_error = stderr.strip() or stdout.strip()
                logger.warning(
                    f"AGY attempt {attempt}/{total_attempts} failed with code {last_return_code}: {last_error[:200]}"
                )
            except Exception as e:
                last_error = str(e)
                last_return_code = -1
                logger.error(
                    f"AGY attempt {attempt}/{total_attempts} exception: {last_error}", exc_info=True
                )

            # If there are attempts left, wait and retry
            if attempt < total_attempts:
                delay = 2 * attempt  # Backoff: 2s, 4s, 6s...
                logger.info(f"Retrying AGY execution in {delay}s (retry {attempt}/{max_retries})...")
                if status_msg_id:
                    err_preview = (last_error[:100] + "...") if len(last_error) > 100 else (last_error or "Fallo no especificado")
                    try:
                        edit_message(
                            chat_id,
                            status_msg_id,
                            f"⚠️ **Fallo temporal en AGY (intento {attempt}/{total_attempts})** [Modelo: `{model_label}`].\n"
                            f"Reintentando en {delay}s ({attempt} de {max_retries} reintentos)...\n\n"
                            f"_Error: {err_preview}_",
                        )
                    except Exception as me:
                        logger.warning(f"Failed to edit status message on retry: {me}")

                time.sleep(delay)

                if status_msg_id:
                    try:
                        edit_message(
                            chat_id,
                            status_msg_id,
                            f"🧠 **AGY ejecutando ({action_label} • {model_label} • reintento {attempt}/{max_retries})...**",
                        )
                    except Exception as me:
                        logger.warning(f"Failed to edit status message on resume: {me}")

        quota_keywords = [
            "RESOURCE_EXHAUSTED", "quota", "rate limit", "token limit",
            "429", "exhausted", "too many requests", "tokens"
        ]
        is_quota_issue = any(k.lower() in last_error.lower() for k in quota_keywords)

        err_msg = f"❌ **Error de ejecución de AGY tras {max_retries} reintentos (código {last_return_code})**:\n```\n{last_error}\n```"
        if is_quota_issue:
            err_msg += (
                f"\n\n🚨 **Parece que se ha agotado la cuota o los tokens del modelo `{model_label}`.**\n"
                f"💡 Usa `/model` para cambiar a otro modelo disponible (como Claude o GPT) y vuelve a intentar con `/c <prompt>`."
            )
        return err_msg, conversation_id
    finally:
        stop_heartbeat.set()
        t.join(timeout=1.0)


def get_uptime_seconds() -> Optional[float]:
    """Retrieve host uptime in seconds across Linux and macOS."""
    if os.path.exists("/proc/uptime"):
        try:
            with open("/proc/uptime") as f:
                return float(f.readline().split()[0])
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            res = subprocess.check_output(["sysctl", "-n", "kern.boottime"], text=True)
            match = re.search(r"sec\s*=\s*(\d+)", res)
            if match:
                boot_sec = int(match.group(1))
                return max(0.0, time.time() - boot_sec)
        except Exception:
            pass
    return None


def get_memory_info() -> Tuple[Optional[float], Optional[float]]:
    """Retrieve memory usage (used_gb, total_gb) across Linux and macOS."""
    if os.path.exists("/proc/meminfo"):
        try:
            mem_info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        mem_info[parts[0].strip()] = parts[1].strip()
            mem_total_kb = int(mem_info.get("MemTotal", "0 kB").split()[0])
            mem_avail_kb = int(mem_info.get("MemAvailable", "0 kB").split()[0])
            return (mem_total_kb - mem_avail_kb) / (1024 * 1024), mem_total_kb / (1024 * 1024)
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            total_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
            total_gb = total_bytes / (1024 ** 3)
            vm = subprocess.check_output(["vm_stat"], text=True)
            page_size = 4096
            m_page = re.search(r"page size of (\d+) bytes", vm)
            if m_page:
                page_size = int(m_page.group(1))
            free_pages = 0
            m_free = re.search(r"Pages free:\s+(\d+)", vm)
            m_spec = re.search(r"Pages speculative:\s+(\d+)", vm)
            if m_free:
                free_pages += int(m_free.group(1))
            if m_spec:
                free_pages += int(m_spec.group(1))
            avail_bytes = free_pages * page_size
            used_gb = max(0.0, (total_bytes - avail_bytes) / (1024 ** 3))
            return used_gb, total_gb
        except Exception:
            pass
    return None, None


def get_system_status(chat_id: Optional[int] = None) -> str:
    """Returns system status overview across Linux and macOS."""
    try:
        try:
            load1, load5, load15 = os.getloadavg()
            load_str = f"{load1:.2f}, {load5:.2f}, {load15:.2f}"
        except (AttributeError, OSError):
            load_str = "N/A"

        uptime_sec = get_uptime_seconds()
        if uptime_sec is not None:
            days = int(uptime_sec // 86400)
            hours = int((uptime_sec % 86400) // 3600)
            mins = int((uptime_sec % 3600) // 60)
            uptime_str = f"{days}d {hours}h {mins}m"
        else:
            uptime_str = "N/A"

        used_gb, total_gb = get_memory_info()
        if used_gb is not None and total_gb is not None:
            ram_str = f"{used_gb:.1f} GB / {total_gb:.1f} GB used"
        else:
            ram_str = "N/A"

        os_name = "macOS" if sys.platform == "darwin" else ("Linux" if sys.platform.startswith("linux") else sys.platform)
        current_model = get_current_model(chat_id)
        current_label = get_model_label(current_model)

        return (
            f"📊 **System Status ({os_name})**\n"
            f"• **Modelo activo:** `{current_label}` (`{current_model}`)\n"
            f"• **Uptime:** {uptime_str}\n"
            f"• **CPU Load:** {load_str}\n"
            f"• **RAM:** {ram_str}\n"
            f"• **AGY Binary:** `{AGY_BIN}`\n"
            f"• **Whisper API:** `{WHISPER_ENDPOINT}`\n"
            f"• **Workspace:** `{WORKSPACE_DIR}`"
        )
    except Exception as e:
        return f"Error obtaining status: {e}"


def deliver_agy_response(
    chat_id: int,
    response: str,
    conversation_id: Optional[str],
    status_msg_id: Optional[int] = None,
    reply_to_id: Optional[int] = None,
    user_msg_id: Optional[int] = None,
):
    """Send or edit the AGY response in Telegram and persist message->conversation mappings."""
    quota_markup = None
    if "agotado la cuota o los tokens" in response or "RESOURCE_EXHAUSTED" in response:
        quota_markup = {
            "inline_keyboard": [
                [{"text": "🔄 Cambiar modelo de IA", "callback_data": "open_model_menu"}]
            ]
        }

    tracked_ids: List[Optional[int]] = []
    if user_msg_id is not None:
        tracked_ids.append(user_msg_id)

    if status_msg_id:
        out_ids: List[int] = []
        edit_message(chat_id, status_msg_id, response, reply_markup=quota_markup, out_message_ids=out_ids)
        tracked_ids.extend(out_ids)
    else:
        sent_ids = send_message(chat_id, response, reply_to_id=reply_to_id, reply_markup=quota_markup)
        tracked_ids.extend(sent_ids)

    if conversation_id:
        save_message_conversations(chat_id, tracked_ids, conversation_id)


def handle_callback_query(cb: dict):
    cb_id = cb.get("id")
    from_user = cb.get("from", {})
    user_id = str(from_user.get("id"))
    username = from_user.get("username", "unknown")
    data = cb.get("data", "")
    message = cb.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized callback from user_id={user_id} (@{username})")
        answer_callback_query(cb_id, "Acceso no autorizado.")
        return

    answer_callback_query(cb_id)

    if data.startswith("cancel:"):
        task_id = data.split(":", 1)[1]
        pending_audio_tasks.pop(task_id, None)
        edit_message(chat_id, message_id, "❌ **Tarea cancelada.**")
        return

    if data == "open_model_menu":
        msg_text, markup = build_model_selection_message(chat_id)
        send_message(chat_id, msg_text, reply_markup=markup)
        return

    if data.startswith("set_model:"):
        model_id = data.split(":", 1)[1]
        current_model = get_current_model(chat_id)
        label = get_model_label(model_id)

        if model_id == current_model:
            answer_callback_query(cb_id, f"El modelo activo ya es: {label}")
            return

        set_current_model(model_id, chat_id)
        answer_callback_query(cb_id, f"Modelo cambiado a: {label}")
        msg_text, markup = build_model_selection_message(chat_id)
        edit_message(chat_id, message_id, msg_text, reply_markup=markup)
        return

    if data.startswith("cont:") or data.startswith("new:"):
        action, task_id = data.split(":", 1)
        task = pending_audio_tasks.pop(task_id, None)
        if not task:
            edit_message(chat_id, message_id, "⚠️ **Esta tarea ha expirado o ya fue procesada.**")
            return

        continue_session = (action == "cont")
        prompt = task["text"]
        user_msg_id = task.get("user_msg_id")
        model = get_current_model(chat_id)
        model_label = get_model_label(model)
        action_label = "Continuar sesión 💬" if continue_session else "Nueva sesión 🆕"

        edit_message(
            chat_id,
            message_id,
            f"🧠 **AGY ejecutando ({action_label} • {model_label})**...\n\n📝 _{prompt}_",
        )

        def worker():
            response, conv_id = run_agy_subprocess(prompt, continue_session, chat_id, status_msg_id=message_id, model=model)
            deliver_agy_response(
                chat_id,
                response,
                conv_id,
                status_msg_id=message_id,
                user_msg_id=user_msg_id,
            )

        threading.Thread(target=worker, daemon=True).start()


def process_audio_message(msg: dict):
    from_user = msg.get("from", {})
    user_id = str(from_user.get("id"))
    username = from_user.get("username", "unknown")
    chat_id = msg["chat"]["id"]
    msg_id = msg.get("message_id")

    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized audio from user_id={user_id} (@{username})")
        return

    file_id = None
    if "voice" in msg:
        file_id = msg["voice"].get("file_id")
    elif "audio" in msg:
        file_id = msg["audio"].get("file_id")
    elif "video_note" in msg:
        file_id = msg["video_note"].get("file_id")
    elif "document" in msg:
        doc = msg["document"]
        mime = doc.get("mime_type", "")
        if mime.startswith("audio/") or mime in ("video/ogg", "application/ogg"):
            file_id = doc.get("file_id")

    if not file_id:
        return

    # Clean old tasks (> 1 hour)
    now = time.time()
    for tid in list(pending_audio_tasks.keys()):
        if now - pending_audio_tasks[tid].get("created_at", 0) > 3600:
            pending_audio_tasks.pop(tid, None)

    sent_status = send_message(chat_id, "🎧 **Transcribiendo nota de voz con Whisper...**", reply_to_id=msg_id)
    status_id = sent_status[0] if sent_status else None

    def worker():
        nonlocal status_id
        with tempfile.NamedTemporaryFile(suffix=".tmp", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            send_chat_action(chat_id, "record_voice")
            if not download_telegram_file(file_id, tmp_path):
                if status_id:
                    edit_message(chat_id, status_id, "❌ Error al descargar el archivo de audio de Telegram.")
                return

            with open(tmp_path, "rb") as f:
                audio_bytes = f.read()

            send_chat_action(chat_id, "typing")
            trans_result = transcribe_audio_bytes(audio_bytes)

            if not trans_result or "text" not in trans_result:
                err_msg = trans_result.get("error") if trans_result else "Servicio Whisper no disponible"
                if status_id:
                    edit_message(chat_id, status_id, f"❌ Error en transcripción Whisper: `{err_msg}`")
                return

            raw_text = trans_result["text"].strip()
            duration = trans_result.get("duration", 0)

            if not raw_text:
                if status_id:
                    edit_message(chat_id, status_id, "🔇 No se ha detectado voz inteligible en el audio.")
                return

            intent, clean_prompt = detect_session_intent(raw_text)
            replied_conv_id = get_replied_conversation_id(msg)

            target_conv_id = None
            if intent is False:
                # Explicit "new session" spoken by user overrides reply context
                target_conv_id = None
            elif replied_conv_id:
                # Replying to a tracked message automatically continues that conversation
                intent = True
                target_conv_id = replied_conv_id

            if intent is not None:
                # Explicit intent or reply to tracked conversation -> Direct execution
                model = get_current_model(chat_id)
                model_label = get_model_label(model)
                if target_conv_id:
                    action_label = "Continuar hilo 💬"
                elif intent:
                    action_label = "Continuar sesión 💬"
                else:
                    action_label = "Nueva sesión limpia 🆕"
                logger.info(
                    f"Direct audio execution: {'CONTINUE' if intent else 'NEW'} "
                    f"(conv_id={target_conv_id}). Clean prompt: {clean_prompt}"
                )

                initial_status = (
                    f"🎙️ **Nota de voz recibida ({action_label} • {model_label})**\n"
                    f"📝 _{clean_prompt}_\n\n"
                    f"🧠 **AGY ejecutando tarea...**"
                )
                if status_id:
                    edit_message(chat_id, status_id, initial_status)
                else:
                    sent = send_message(chat_id, initial_status, reply_to_id=msg_id)
                    status_id = sent[0] if sent else None

                response, conv_id = run_agy_subprocess(
                    clean_prompt,
                    intent,
                    chat_id,
                    status_msg_id=status_id,
                    model=model,
                    conversation_id=target_conv_id,
                )
                deliver_agy_response(
                    chat_id,
                    response,
                    conv_id,
                    status_msg_id=status_id,
                    reply_to_id=msg_id,
                    user_msg_id=msg_id,
                )

            else:
                # No intent specified -> Offer interactive buttons
                task_id = str(uuid.uuid4())[:8]
                pending_audio_tasks[task_id] = {
                    "text": clean_prompt,
                    "chat_id": chat_id,
                    "user_msg_id": msg_id,
                    "created_at": time.time(),
                }

                keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": "💬 Continuar sesión", "callback_data": f"cont:{task_id}"},
                            {"text": "🆕 Nueva sesión", "callback_data": f"new:{task_id}"},
                        ],
                        [
                            {"text": "❌ Cancelar", "callback_data": f"cancel:{task_id}"}
                        ]
                    ]
                }

                prompt_text = (
                    f"🎙️ **Transcripción:**\n"
                    f"«{clean_prompt}»\n\n"
                    f"_⏱️ Transcrito en {duration:.1f}s_\n"
                    f"¿Cómo quieres ejecutar esta tarea?"
                )

                if status_id:
                    edit_message(chat_id, status_id, prompt_text, reply_markup=keyboard)
                else:
                    send_message(chat_id, prompt_text, reply_to_id=msg_id, reply_markup=keyboard)

        except Exception as e:
            logger.error(f"Error processing audio: {e}", exc_info=True)
            if status_id:
                edit_message(chat_id, status_id, f"⚠️ Error inesperado procesando el audio: `{e}`")
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    threading.Thread(target=worker, daemon=True).start()


def cleanup_old_images(directory: str, max_age_days: int = 3):
    """Remove image files older than max_age_days to conserve disk space."""
    try:
        now = time.time()
        for fname in os.listdir(directory):
            fpath = os.path.join(directory, fname)
            if os.path.isfile(fpath) and (now - os.path.getmtime(fpath) > max_age_days * 86400):
                try:
                    os.remove(fpath)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Error during image cleanup: {e}")


def process_image_message(msg: dict):
    from_user = msg.get("from", {})
    user_id = str(from_user.get("id"))
    username = from_user.get("username", "unknown")
    chat_id = msg["chat"]["id"]
    msg_id = msg.get("message_id")

    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized image from user_id={user_id} (@{username})")
        return

    file_id = None
    ext = ".jpg"

    if "photo" in msg:
        file_id = msg["photo"][-1].get("file_id")
        ext = ".jpg"
    elif "document" in msg:
        doc = msg["document"]
        mime = doc.get("mime_type", "")
        if mime.startswith("image/"):
            file_id = doc.get("file_id")
            original_name = doc.get("file_name", "")
            orig_ext = os.path.splitext(original_name)[1].lower()
            if orig_ext in [".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"]:
                ext = orig_ext
            elif "png" in mime:
                ext = ".png"
            elif "webp" in mime:
                ext = ".webp"

    if not file_id:
        return

    caption = msg.get("caption", "").strip()
    replied_conv_id = get_replied_conversation_id(msg)
    continue_session = False
    target_conv_id = None
    user_prompt = caption

    if caption in ["/c", "/continue"]:
        continue_session = True
        target_conv_id = replied_conv_id
        user_prompt = ""
    elif caption.startswith("/c ") or caption.startswith("/continue "):
        continue_session = True
        target_conv_id = replied_conv_id
        user_prompt = caption.split(" ", 1)[1].strip()
    elif caption.startswith("/c\n") or caption.startswith("/continue\n"):
        continue_session = True
        target_conv_id = replied_conv_id
        user_prompt = caption.split("\n", 1)[1].strip()
    elif caption == "/new":
        continue_session = False
        target_conv_id = None
        user_prompt = ""
    elif caption.startswith("/new ") or caption.startswith("/new\n"):
        continue_session = False
        target_conv_id = None
        user_prompt = caption[4:].strip()
    elif replied_conv_id:
        continue_session = True
        target_conv_id = replied_conv_id
        user_prompt = caption
    else:
        continue_session = False
        target_conv_id = None
        user_prompt = caption

    model = get_current_model(chat_id)
    model_label = get_model_label(model)
    if target_conv_id:
        action_label = "Continuar hilo 💬"
    elif continue_session:
        action_label = "Continuar sesión 💬"
    else:
        action_label = "Nuevo chat 🆕"

    sent_status = send_message(chat_id, f"🖼️ **Descargando imagen ({action_label} • {model_label})...**", reply_to_id=msg_id)
    status_id = sent_status[0] if sent_status else None

    def worker():
        cleanup_old_images(IMAGE_DIR, max_age_days=3)

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        img_filename = f"img_{timestamp}_{uuid.uuid4().hex[:6]}{ext}"
        dest_path = os.path.join(IMAGE_DIR, img_filename)

        send_chat_action(chat_id, "upload_photo")
        if not download_telegram_file(file_id, dest_path):
            if status_id:
                edit_message(chat_id, status_id, "❌ Error al descargar la imagen de Telegram.")
            return

        initial_status = f"🖼️ **Imagen recibida ({action_label} • {model_label})**\n🧠 **AGY analizando imagen...**"
        if user_prompt:
            initial_status = f"🖼️ **Imagen recibida ({action_label} • {model_label})**\n📝 _{user_prompt}_\n\n🧠 **AGY analizando imagen...**"

        if status_id:
            edit_message(chat_id, status_id, initial_status)

        if user_prompt:
            prompt = (
                f"El usuario ha compartido una imagen adjunta guardada localmente en la ruta: '{dest_path}'.\n"
                f"Por favor, examínala usando tu herramienta `read_file` y atiende a su petición:\n\n{user_prompt}"
            )
        else:
            prompt = (
                f"El usuario ha compartido una imagen adjunta guardada localmente en la ruta: '{dest_path}'.\n"
                f"Por favor, examínala detalladamente usando tu herramienta `read_file` y describe su contenido, detecta cualquier texto o datos relevantes y ofrece tu asistencia."
            )

        response, conv_id = run_agy_subprocess(
            prompt,
            continue_session,
            chat_id,
            status_msg_id=status_id,
            model=model,
            conversation_id=target_conv_id,
        )
        deliver_agy_response(
            chat_id,
            response,
            conv_id,
            status_msg_id=status_id,
            reply_to_id=msg_id,
            user_msg_id=msg_id,
        )

    threading.Thread(target=worker, daemon=True).start()


def handle_message(msg: dict):
    from_user = msg.get("from", {})
    user_id = str(from_user.get("id"))
    username = from_user.get("username", "unknown")
    chat_id = msg["chat"]["id"]

    # Authorization Check
    if ALLOWED_USERS and user_id not in ALLOWED_USERS:
        logger.warning(f"Unauthorized access attempt from user_id={user_id} (@{username})")
        return

    # Check for photo or image document first
    if "photo" in msg:
        process_image_message(msg)
        return

    if "document" in msg:
        doc = msg.get("document", {})
        mime = doc.get("mime_type", "")
        if mime.startswith("image/"):
            process_image_message(msg)
            return

    # Check for audio/voice messages
    if any(k in msg for k in ("voice", "audio", "video_note", "document")):
        doc = msg.get("document", {})
        mime = doc.get("mime_type", "")
        if "voice" in msg or "audio" in msg or "video_note" in msg or mime.startswith("audio/") or mime in ("video/ogg", "application/ogg"):
            process_audio_message(msg)
            return

    text = msg.get("text", "").strip()
    if not text:
        return

    logger.info(f"Message from @{username} ({user_id}): {text[:60]}")

    # Built-in commands
    if text in ["/start", "/help"]:
        current_model = get_current_model(chat_id)
        current_label = get_model_label(current_model)
        welcome = (
            "🤖 **AGY Telegram Assistant**\n\n"
            "Conectado a tu agente Google Antigravity CLI (AGY).\n\n"
            f"• **Modelo actual:** `{current_label}` (`{current_model}`)\n\n"
            "**Instrucciones de uso:**\n"
            "• **Nuevo chat:** Envía cualquier mensaje de texto o imagen para iniciar una conversación nueva (por defecto siempre es un nuevo chat).\n"
            "• **Responder a un mensaje:** Haz *Responder (Reply)* sobre cualquier respuesta del bot para continuar automáticamente esa conversación concreta sin necesidad de `/c` (puedes llevar varias sesiones independientes en paralelo).\n"
            "• **Continuar última sesión:** Usa `/c <prompt>` o `/continue <prompt>` para continuar la sesión más reciente.\n"
            "• **Elegir modelo:** Usa `/model` o `/models` para cambiar el modelo de IA (se recuerda para nuevos chats y continuaciones, ideal si se agotan los tokens).\n"
            "• **Imágenes:** Envía capturas o fotos (con o sin texto). Usa `/c` en el pie si quieres continuar la conversación previa.\n"
            "• **Notas de voz:** Envía audio o notas de voz (di *'Nuevo chat'* o *'Continúa'*, o usa los botones interactivos).\n"
            "• `/status` : Ver estado del host y modelo activo.\n"
            "• `/help` : Ver este mensaje de ayuda."
        )
        send_message(chat_id, welcome, reply_to_id=msg.get("message_id"))
        return

    if text == "/status":
        status_text = get_system_status(chat_id)
        send_message(chat_id, status_text, reply_to_id=msg.get("message_id"))
        return

    if text in ["/model", "/models"]:
        msg_text, markup = build_model_selection_message(chat_id)
        send_message(chat_id, msg_text, reply_to_id=msg.get("message_id"), reply_markup=markup)
        return

    if text.startswith("/model ") or text.startswith("/models "):
        target = text.split(" ", 1)[1].strip().lower()
        models = get_available_models()
        matched = None
        for m_id, label in models:
            if target == m_id.lower() or target == label.lower():
                matched = (m_id, label)
                break
        if not matched:
            for m_id, label in models:
                if target in m_id.lower() or target in label.lower():
                    matched = (m_id, label)
                    break
        if matched:
            set_current_model(matched[0], chat_id)
            send_message(
                chat_id,
                f"✅ **Modelo actualizado a:** `{matched[1]}` (`{matched[0]}`)\n\n"
                f"Se utilizará para todos tus **nuevos chats** y **continuaciones**.",
                reply_to_id=msg.get("message_id"),
            )
        else:
            send_message(
                chat_id,
                f"⚠️ Modelo '{target}' no reconocido.\nUsa `/model` para ver la lista interactiva de modelos disponibles.",
                reply_to_id=msg.get("message_id"),
            )
        return

    msg_id = msg.get("message_id")
    replied_conv_id = get_replied_conversation_id(msg)
    continue_session = False
    target_conv_id = None
    prompt = text

    # Handle /c or /continue with no argument
    if text in ["/c", "/continue"]:
        send_message(
            chat_id,
            "⚠️ **Continuar sesión:** Por favor escribe tu mensaje después del comando.\n"
            "Ejemplo: `/c explica esto con más detalle`\n\n"
            "💡 _Nota: También puedes hacer Responder (Reply) directamente a cualquier mensaje del bot sin usar `/c`._",
            reply_to_id=msg_id,
        )
        return

    if text.startswith("/c ") or text.startswith("/continue "):
        continue_session = True
        target_conv_id = replied_conv_id
        prompt = text.split(" ", 1)[1].strip()
    elif text.startswith("/c\n") or text.startswith("/continue\n"):
        continue_session = True
        target_conv_id = replied_conv_id
        prompt = text.split("\n", 1)[1].strip()
    elif text == "/new":
        send_message(
            chat_id,
            "⚠️ Por favor escribe tu prompt tras `/new` (aunque ahora cualquier mensaje sin `/c` ni respuesta a un hilo ya inicia un chat nuevo de forma automática).",
            reply_to_id=msg_id,
        )
        return
    elif text.startswith("/new ") or text.startswith("/new\n"):
        continue_session = False
        target_conv_id = None
        prompt = text[4:].strip()
    elif replied_conv_id:
        # Replying to a tracked bot/user message automatically continues that conversation!
        continue_session = True
        target_conv_id = replied_conv_id
        prompt = text
    else:
        # Default is ALWAYS a new chat if /c is not specified and not replying to a tracked thread!
        continue_session = False
        target_conv_id = None
        prompt = text

    if not prompt:
        send_message(chat_id, "⚠️ Por favor, escribe un prompt para ejecutar.", reply_to_id=msg_id)
        return

    model = get_current_model(chat_id)
    model_label = get_model_label(model)
    if target_conv_id:
        action_label = "Continuar hilo 💬"
    elif continue_session:
        action_label = "Continuar sesión 💬"
    else:
        action_label = "Nuevo chat 🆕"

    sent_status = send_message(
        chat_id,
        f"🧠 **AGY ejecutando ({action_label} • {model_label})...**",
        reply_to_id=msg_id,
    )
    status_msg_id = sent_status[0] if sent_status else None

    def worker():
        response, conv_id = run_agy_subprocess(
            prompt,
            continue_session,
            chat_id,
            status_msg_id=status_msg_id,
            model=model,
            conversation_id=target_conv_id,
        )
        deliver_agy_response(
            chat_id,
            response,
            conv_id,
            status_msg_id=status_msg_id,
            reply_to_id=msg_id,
            user_msg_id=msg_id,
        )

    threading.Thread(target=worker, daemon=True).start()


def main():
    logger.info("Starting agy-telegram-bot gateway service...")
    logger.info(f"Allowed user IDs: {ALLOWED_USERS if ALLOWED_USERS else 'ALL (Warning: open access!)'}")
    logger.info(f"AGY binary: {AGY_BIN}")
    logger.info(f"Whisper endpoint: {WHISPER_ENDPOINT}")
    logger.info(f"Workspace directory: {WORKSPACE_DIR}")

    offset = None

    while RUNNING:
        try:
            params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset

            resp = requests.get(f"{BASE_URL}/getUpdates", params=params, timeout=35)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        if "message" in update:
                            handle_message(update["message"])
                        elif "callback_query" in update:
                            handle_callback_query(update["callback_query"])
            elif resp.status_code == 409:
                logger.error("409 Conflict: Another instance is polling getUpdates. Retrying in 5s...")
                time.sleep(5)
            else:
                logger.warning(f"getUpdates returned HTTP {resp.status_code}: {resp.text}")
                time.sleep(3)

        except requests.exceptions.RequestException as e:
            logger.error(f"Network error in polling loop: {e}")
            time.sleep(3)
        except Exception as e:
            logger.error(f"Unexpected error in polling loop: {e}", exc_info=True)
            time.sleep(3)

    logger.info("Bot service stopped cleanly.")


if __name__ == "__main__":
    main()
