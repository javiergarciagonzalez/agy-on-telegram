#!/usr/bin/env python3
# ==============================================================================
# agy-on-telegram - Lightweight Telegram Gateway for AGY (Antigravity CLI)
# ==============================================================================
import os
import sys
import re
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


def handle_signal(sig, frame):
    global RUNNING
    logger.info(f"Received signal {sig}, shutting down...")
    RUNNING = False


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def split_text(text: str, max_len: int = 3900) -> List[str]:
    """Split text into chunks that fit within Telegram limits."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    lines = text.splitlines(keepends=True)
    current_chunk = []
    current_len = 0

    for line in lines:
        if current_len + len(line) > max_len:
            if current_chunk:
                chunks.append("".join(current_chunk))
                current_chunk = []
                current_len = 0
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
        current_chunk.append(line)
        current_len += len(line)

    if current_chunk:
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
    """Send message with markdown, falling back to plain text if parsing fails."""
    chunks = split_text(text)
    sent_ids = []

    for idx, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown",
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
            res2 = tg_request("sendMessage", payload)
            if res2 and res2.get("ok"):
                sent_ids.append(res2["result"]["message_id"])

    return sent_ids


def edit_message(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[dict] = None,
) -> bool:
    """Edit an existing message, supporting optional inline keyboards."""
    chunks = split_text(text)
    first_chunk = chunks[0]

    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": first_chunk,
        "parse_mode": "Markdown",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    res = tg_request("editMessageText", payload)
    if not (res and res.get("ok")):
        payload.pop("parse_mode", None)
        res2 = tg_request("editMessageText", payload)
        success = bool(res2 and res2.get("ok"))
    else:
        success = True

    if len(chunks) > 1:
        for chunk in chunks[1:]:
            send_message(chat_id, chunk)

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


def run_agy_subprocess(
    prompt: str,
    continue_session: bool,
    chat_id: int,
    status_msg_id: Optional[int] = None,
    max_retries: int = 3,
) -> str:
    """Run AGY in non-interactive print mode with periodic typing indicators and automatic retries."""
    cmd = [AGY_BIN]
    if continue_session:
        cmd.append("-c")
    cmd.extend(["-p", prompt, "--dangerously-skip-permissions"])

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
                    output = stdout.strip()
                    return output if output else "✅ Tarea completada sin salida de texto."

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
                            f"⚠️ *Fallo temporal en AGY (intento {attempt}/{total_attempts})*.\n"
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
                            f"🧠 *AGY ejecutando tarea (reintento {attempt}/{max_retries})...*",
                        )
                    except Exception as me:
                        logger.warning(f"Failed to edit status message on resume: {me}")

        return f"❌ *Error de ejecución de AGY tras {max_retries} reintentos (código {last_return_code})*:\n```\n{last_error}\n```"
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


def get_system_status() -> str:
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

        return (
            f"📊 *System Status ({os_name})*\n"
            f"• *Uptime:* {uptime_str}\n"
            f"• *CPU Load:* {load_str}\n"
            f"• *RAM:* {ram_str}\n"
            f"• *AGY Binary:* `{AGY_BIN}`\n"
            f"• *Whisper API:* `{WHISPER_ENDPOINT}`\n"
            f"• *Workspace:* `{WORKSPACE_DIR}`"
        )
    except Exception as e:
        return f"Error obtaining status: {e}"


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
        edit_message(chat_id, message_id, "❌ *Tarea cancelada.*")
        return

    if data.startswith("cont:") or data.startswith("new:"):
        action, task_id = data.split(":", 1)
        task = pending_audio_tasks.pop(task_id, None)
        if not task:
            edit_message(chat_id, message_id, "⚠️ *Esta tarea ha expirado o ya fue procesada.*")
            return

        continue_session = (action == "cont")
        prompt = task["text"]
        action_label = "Continuar sesión 💬" if continue_session else "Nueva sesión 🆕"

        edit_message(
            chat_id,
            message_id,
            f"🧠 *AGY ejecutando tarea ({action_label})*...\n\n📝 _{prompt}_",
        )

        def worker():
            response = run_agy_subprocess(prompt, continue_session, chat_id, status_msg_id=message_id)
            edit_message(chat_id, message_id, response)

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

    status_msg = tg_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "🎧 *Transcribiendo nota de voz con Whisper...*",
            "parse_mode": "Markdown",
            "reply_to_message_id": msg_id,
        },
    )
    status_id = status_msg["result"]["message_id"] if (status_msg and status_msg.get("ok")) else None

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

            if intent is not None:
                # Explicit intent spoken by user -> Direct execution
                action_label = "Continuar sesión 💬" if intent else "Nueva sesión limpia 🆕"
                logger.info(f"Explicit intent detected: {'CONTINUE' if intent else 'NEW'}. Clean prompt: {clean_prompt}")

                initial_status = (
                    f"🎙️ *Nota de voz recibida ({action_label})*\n"
                    f"📝 _{clean_prompt}_\n\n"
                    f"🧠 *AGY ejecutando tarea...*"
                )
                if status_id:
                    edit_message(chat_id, status_id, initial_status)
                else:
                    sent = send_message(chat_id, initial_status, reply_to_id=msg_id)
                    status_id = sent[0] if sent else None

                response = run_agy_subprocess(clean_prompt, intent, chat_id, status_msg_id=status_id)
                if status_id:
                    edit_message(chat_id, status_id, response)
                else:
                    send_message(chat_id, response, reply_to_id=msg_id)

            else:
                # No intent specified -> Offer interactive buttons
                task_id = str(uuid.uuid4())[:8]
                pending_audio_tasks[task_id] = {
                    "text": clean_prompt,
                    "chat_id": chat_id,
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
                    f"🎙️ *Transcripción:*\n"
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
    continue_session = True
    user_prompt = caption

    if user_prompt.startswith("/new"):
        continue_session = False
        user_prompt = user_prompt[4:].strip()
    elif user_prompt.startswith("/c ") or user_prompt.startswith("/continue "):
        continue_session = True
        user_prompt = user_prompt.split(" ", 1)[1].strip()

    status_msg = tg_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "🖼️ *Descargando imagen...*",
            "parse_mode": "Markdown",
            "reply_to_message_id": msg_id,
        },
    )
    status_id = status_msg["result"]["message_id"] if (status_msg and status_msg.get("ok")) else None

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

        action_label = "Continuar sesión 💬" if continue_session else "Nueva sesión limpia 🆕"
        initial_status = f"🖼️ *Imagen recibida ({action_label})*\n🧠 *AGY analizando imagen...*"
        if user_prompt:
            initial_status = f"🖼️ *Imagen recibida ({action_label})*\n📝 _{user_prompt}_\n\n🧠 *AGY analizando imagen...*"

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

        response = run_agy_subprocess(prompt, continue_session, chat_id, status_msg_id=status_id)
        if status_id:
            edit_message(chat_id, status_id, response)
        else:
            send_message(chat_id, response, reply_to_id=msg_id)

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
        welcome = (
            "🤖 *AGY Telegram Assistant*\n\n"
            "Gateway connected to your Google Antigravity CLI (AGY) agent.\n\n"
            "*Usage instructions:*\n"
            "• Send any text message to interact (persists conversation context).\n"
            "• Send a **photo or screenshot** (with or without caption) for multimodal inspection.\n"
            "• Send a **voice note** or audio:\n"
            "   - Say *'New chat: ...'* or *'Continue: ...'* for direct execution.\n"
            "   - Or leave it unstated to receive interactive buttons.\n"
            "• `/new <prompt>` : Run a task starting from a fresh session (clears previous context).\n"
            "• `/c <prompt>` or `/continue <prompt>` : Explicitly continue previous session.\n"
            "• `/status` : View host system load and service status.\n"
            "• `/help` : Show this help message."
        )
        send_message(chat_id, welcome, reply_to_id=msg.get("message_id"))
        return

    if text == "/status":
        status_text = get_system_status()
        send_message(chat_id, status_text, reply_to_id=msg.get("message_id"))
        return

    continue_session = True
    prompt = text

    if text.startswith("/new "):
        continue_session = False
        prompt = text[5:].strip()
    elif text.startswith("/c ") or text.startswith("/continue "):
        continue_session = True
        prompt = text.split(" ", 1)[1].strip()

    if not prompt:
        send_message(chat_id, "⚠️ Por favor, escribe un prompt para ejecutar.", reply_to_id=msg.get("message_id"))
        return

    status_msg = tg_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "🧠 *AGY ejecutando tarea...*",
            "parse_mode": "Markdown",
            "reply_to_message_id": msg.get("message_id"),
        },
    )
    status_msg_id = status_msg["result"]["message_id"] if (status_msg and status_msg.get("ok")) else None

    def worker():
        response = run_agy_subprocess(prompt, continue_session, chat_id, status_msg_id=status_msg_id)
        if status_msg_id:
            edit_message(chat_id, status_msg_id, response)
        else:
            send_message(chat_id, response, reply_to_id=msg.get("message_id"))

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
