import base64
import json
import logging
import os
from collections import defaultdict
from logging.handlers import RotatingFileHandler
from tempfile import NamedTemporaryFile
from typing import Dict, List, Union

import psycopg2
import redis
from dotenv import load_dotenv
from openai import DefaultHttpxClient, OpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import InvalidToken
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты полезный русскоязычный ассистент. Отвечай понятно и по делу.",
)
MAX_HISTORY_MESSAGES = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
HISTORY_BACKEND = os.getenv("HISTORY_BACKEND", "memory").lower()
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
POSTGRES_DSN = os.getenv(
    "POSTGRES_DSN",
    "dbname=telegram_bot user=telegram_bot password=telegram_bot host=127.0.0.1 port=5432",
)
ALLOWED_TELEGRAM_USER_IDS = {
    int(user_id.strip())
    for user_id in os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").split(",")
    if user_id.strip()
}
TELEGRAM_MODE = os.getenv("TELEGRAM_MODE", "polling").lower()
TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "")
WEBHOOK_LISTEN = os.getenv("WEBHOOK_LISTEN", "127.0.0.1")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram/webhook")
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "")
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()
OPENAI_PROXY = os.getenv("OPENAI_PROXY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
# Для OpenAI можно использовать тот же прокси, но в ollama-режиме локальный
# endpoint не должен идти через SOCKS/HTTP proxy.
if LLM_PROVIDER == "openai" and not OPENAI_PROXY and TELEGRAM_PROXY:
    OPENAI_PROXY = TELEGRAM_PROXY
LOG_FILE = os.getenv("LOG_FILE", "logs/bot.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))


def configure_logging() -> None:
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)

    # Не логируем сырые HTTP URL (в них может быть bot token).
    logging.getLogger("httpx").setLevel(logging.WARNING)

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Переменная TELEGRAM_BOT_TOKEN не задана")
if TELEGRAM_MODE == "webhook" and not TELEGRAM_WEBHOOK_URL:
    raise RuntimeError("Для режима webhook нужно задать TELEGRAM_WEBHOOK_URL")
if LLM_PROVIDER not in {"openai", "ollama"}:
    raise RuntimeError("LLM_PROVIDER должен быть openai или ollama")
if LLM_PROVIDER == "openai" and not OPENAI_API_KEY:
    raise RuntimeError("Для LLM_PROVIDER=openai нужно задать OPENAI_API_KEY")

openai_client_kwargs = {
    "api_key": OPENAI_API_KEY if LLM_PROVIDER == "openai" else "ollama",
}
if OPENAI_PROXY:
    openai_client_kwargs["http_client"] = DefaultHttpxClient(proxy=OPENAI_PROXY)
    logger.info("OpenAI proxy enabled")
if OPENAI_BASE_URL:
    openai_client_kwargs["base_url"] = OPENAI_BASE_URL
    logger.info("OpenAI base URL overridden")
if LLM_PROVIDER == "ollama" and not OPENAI_BASE_URL:
    openai_client_kwargs["base_url"] = OLLAMA_BASE_URL
    logger.info("Using Ollama at %s", OLLAMA_BASE_URL)

client = OpenAI(**openai_client_kwargs)


class HistoryStore:
    def get_history(self, chat_id: int) -> List[dict]:
        raise NotImplementedError

    def append(self, chat_id: int, role: str, content: str) -> None:
        raise NotImplementedError

    def clear(self, chat_id: int) -> None:
        raise NotImplementedError


class InMemoryHistoryStore(HistoryStore):
    def __init__(self) -> None:
        self.chat_history: Dict[int, List[dict]] = defaultdict(list)

    def get_history(self, chat_id: int) -> List[dict]:
        return self.chat_history[chat_id][-MAX_HISTORY_MESSAGES:]

    def append(self, chat_id: int, role: str, content: str) -> None:
        self.chat_history[chat_id].append({"role": role, "content": content})
        self.chat_history[chat_id] = self.chat_history[chat_id][-MAX_HISTORY_MESSAGES:]

    def clear(self, chat_id: int) -> None:
        self.chat_history.pop(chat_id, None)


class RedisHistoryStore(HistoryStore):
    def __init__(self, redis_url: str) -> None:
        self.redis = redis.from_url(redis_url, decode_responses=True)

    def _key(self, chat_id: int) -> str:
        return f"chat:{chat_id}:history"

    def get_history(self, chat_id: int) -> List[dict]:
        items = self.redis.lrange(self._key(chat_id), -MAX_HISTORY_MESSAGES, -1)
        return [json.loads(item) for item in items]

    def append(self, chat_id: int, role: str, content: str) -> None:
        key = self._key(chat_id)
        self.redis.rpush(key, json.dumps({"role": role, "content": content}, ensure_ascii=False))
        self.redis.ltrim(key, -MAX_HISTORY_MESSAGES, -1)

    def clear(self, chat_id: int) -> None:
        self.redis.delete(self._key(chat_id))


class PostgresHistoryStore(HistoryStore):
    def __init__(self, dsn: str) -> None:
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = True
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_history (
                    id BIGSERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_chat_history_chat_id_id
                ON chat_history(chat_id, id);
                """
            )

    def get_history(self, chat_id: int) -> List[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT role, content
                FROM (
                    SELECT role, content, id
                    FROM chat_history
                    WHERE chat_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                ) t
                ORDER BY id ASC
                """,
                (chat_id, MAX_HISTORY_MESSAGES),
            )
            rows = cur.fetchall()
        return [{"role": role, "content": content} for role, content in rows]

    def append(self, chat_id: int, role: str, content: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_history(chat_id, role, content) VALUES (%s, %s, %s)",
                (chat_id, role, content),
            )

    def clear(self, chat_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM chat_history WHERE chat_id = %s", (chat_id,))


def create_history_store() -> HistoryStore:
    if HISTORY_BACKEND == "memory":
        return InMemoryHistoryStore()
    if HISTORY_BACKEND == "redis":
        return RedisHistoryStore(REDIS_URL)
    if HISTORY_BACKEND == "postgres":
        return PostgresHistoryStore(POSTGRES_DSN)
    raise RuntimeError("HISTORY_BACKEND должен быть: memory, redis или postgres")


history_store = create_history_store()


def is_user_allowed(update: Update) -> bool:
    if not ALLOWED_TELEGRAM_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_TELEGRAM_USER_IDS)


def build_messages(chat_id: int, user_content: Union[str, List[dict]]) -> List[dict]:
    history = history_store.get_history(chat_id)
    return [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": user_content}]


def request_llm_answer(messages: List[dict]) -> str:
    response = client.chat.completions.create(
        model=OPENAI_MODEL if LLM_PROVIDER == "openai" else OLLAMA_MODEL,
        messages=messages,
        temperature=0.7,
    )
    content = response.choices[0].message.content
    return content.strip() if content else "Не удалось получить ответ от модели."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_user_allowed(update):
        await update.message.reply_text("Доступ запрещен.")
        return

    await update.message.reply_text(
        "Привет! Я проксирую твои сообщения в ИИ и возвращаю ответ.\n"
        "Поддерживаю текст, голос и фото."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_user_allowed(update):
        await update.message.reply_text("Доступ запрещен.")
        return

    chat_id = update.effective_chat.id
    history_store.clear(chat_id)
    await update.message.reply_text("Контекст чата сброшен.")


async def process_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_content: Union[str, List[dict]],
    history_user_text: str,
) -> None:
    if not update.message:
        return

    chat_id = update.effective_chat.id

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        messages = build_messages(chat_id, user_content)
        answer = request_llm_answer(messages)

        history_store.append(chat_id, "user", history_user_text)
        history_store.append(chat_id, "assistant", answer)

        await update.message.reply_text(answer)
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM request failed: %s", exc)
        await update.message.reply_text("Ошибка при запросе к ИИ. Попробуй ещё раз позже.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not is_user_allowed(update):
        await update.message.reply_text("Доступ запрещен.")
        return

    user_text = update.message.text.strip()
    if not user_text:
        return

    logger.info("Text message received from user_id=%s", update.effective_user.id if update.effective_user else "unknown")
    await process_prompt(update, context, user_text, user_text)


def transcribe_voice_file(path: str) -> str:
    if not OPENAI_API_KEY:
        return ""
    with open(path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model=OPENAI_TRANSCRIBE_MODEL,
            file=audio_file,
        )
    text = getattr(transcript, "text", None)
    return text.strip() if text else ""


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.voice:
        return
    if not is_user_allowed(update):
        await update.message.reply_text("Доступ запрещен.")
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        logger.info("Voice message received from user_id=%s", update.effective_user.id if update.effective_user else "unknown")
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        with NamedTemporaryFile(suffix=".ogg", delete=True) as tmp:
            await voice_file.download_to_drive(custom_path=tmp.name)
            transcribed = transcribe_voice_file(tmp.name)

        if not transcribed:
            await update.message.reply_text("Не удалось распознать голосовое сообщение.")
            return

        await update.message.reply_text(f"Распознал: {transcribed}")
        await process_prompt(update, context, transcribed, f"[VOICE] {transcribed}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Voice handling failed: %s", exc)
        await update.message.reply_text("Ошибка при обработке голосового сообщения.")


def build_vision_user_content(caption: str, photo_bytes: bytes) -> List[dict]:
    encoded = base64.b64encode(photo_bytes).decode("utf-8")
    prompt_text = caption or "Опиши, что на изображении, и ответь полезно."
    return [
        {"type": "text", "text": prompt_text},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
    ]


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return
    if not is_user_allowed(update):
        await update.message.reply_text("Доступ запрещен.")
        return

    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        logger.info("Photo message received from user_id=%s", update.effective_user.id if update.effective_user else "unknown")
        largest_photo = update.message.photo[-1]
        file = await context.bot.get_file(largest_photo.file_id)
        photo_bytes = await file.download_as_bytearray()
        caption = (update.message.caption or "").strip()
        user_content = build_vision_user_content(caption, bytes(photo_bytes))
        history_note = f"[PHOTO] caption={caption}" if caption else "[PHOTO]"
        await process_prompt(update, context, user_content, history_note)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Photo handling failed: %s", exc)
        await update.message.reply_text("Ошибка при обработке фото.")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled telegram handler error: %s", context.error)
    if isinstance(update, Update) and update.message:
        try:
            await update.message.reply_text("Внутренняя ошибка обработчика. Попробуй ещё раз.")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send fallback error message")


def main() -> None:
    configure_logging()
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    if TELEGRAM_PROXY:
        builder = builder.proxy(TELEGRAM_PROXY).get_updates_proxy(TELEGRAM_PROXY)
        logger.info("Telegram proxy enabled")

    application = builder.build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_error_handler(on_error)

    logger.info("Bot started in %s mode", TELEGRAM_MODE)
    try:
        if TELEGRAM_MODE == "webhook":
            application.run_webhook(
                listen=WEBHOOK_LISTEN,
                port=WEBHOOK_PORT,
                url_path=WEBHOOK_PATH,
                webhook_url=f"{TELEGRAM_WEBHOOK_URL}{WEBHOOK_PATH}",
                secret_token=WEBHOOK_SECRET_TOKEN or None,
                drop_pending_updates=True,
            )
        else:
            application.run_polling(drop_pending_updates=True)
    except InvalidToken:
        logger.error("Telegram token is invalid. Update TELEGRAM_BOT_TOKEN in .env and restart.")


if __name__ == "__main__":
    main()
