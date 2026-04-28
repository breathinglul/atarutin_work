# Telegram бот-прокси к ИИ

Бот принимает сообщения в Telegram, отправляет их в OpenAI API и возвращает ответ в чат.

Поддерживает:
- текстовые сообщения
- голосовые сообщения (распознавание + ответ)
- фото (vision-анализ + ответ)
- ограничение доступа по `user_id` (whitelist)
- хранение истории в `memory`, `Redis` или `PostgreSQL`
- запуск в `polling` и `webhook` режимах (для прод-сервера)

## 1) Создай бота в Telegram

1. Открой [@BotFather](https://t.me/BotFather)
2. Выполни `/newbot`
3. Сохрани токен (`TELEGRAM_BOT_TOKEN`)

## 2) Настрой окружение

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Заполни `.env`:

- `TELEGRAM_BOT_TOKEN` — токен от BotFather
- `LLM_PROVIDER` — `openai` или `ollama`
- `OPENAI_API_KEY` — ключ OpenAI
- `OPENAI_MODEL` — модель (по умолчанию `gpt-4o-mini`)
- `SYSTEM_PROMPT` — системная инструкция для бота

Для бесплатного локального режима (Ollama):
- `LLM_PROVIDER=ollama`
- `OLLAMA_BASE_URL=http://127.0.0.1:11434/v1`
- `OLLAMA_MODEL=qwen2.5:7b`

## 3) Настрой `.env`

Основные параметры:

- `ALLOWED_TELEGRAM_USER_IDS` — через запятую, например `12345678,9876543`
- `HISTORY_BACKEND` — `memory`, `redis` или `postgres`
- `TELEGRAM_MODE` — `polling` (локально) или `webhook` (прод)

Для `redis` укажи `REDIS_URL`.
Для `postgres` укажи `POSTGRES_DSN`.
Если есть блокировка доступа к Telegram API, укажи `TELEGRAM_PROXY`.
Если блокируется OpenAI API, укажи `OPENAI_PROXY` (и при необходимости `OPENAI_BASE_URL`).
Лог в файл включен по умолчанию: `logs/bot.log` (с ротацией).

## 4) Локальный запуск (polling)

```bash
python bot.py
```

## 5) Продакшен: webhook + systemd + nginx

### 5.1 Подготовка сервера

Пример пути деплоя:

- проект: `/opt/telegram-ai-bot`
- venv: `/opt/telegram-ai-bot/.venv`

В `.env`:

- `TELEGRAM_MODE=webhook`
- `TELEGRAM_WEBHOOK_URL=https://your-domain.com`
- `WEBHOOK_PATH=/telegram/webhook`
- `WEBHOOK_PORT=8080`
- `WEBHOOK_SECRET_TOKEN=<случайная длинная строка>`

### 5.2 Systemd

В репозитории есть шаблон:

- `deploy/systemd/telegram-ai-bot.service`

Установи его:

```bash
sudo cp deploy/systemd/telegram-ai-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable telegram-ai-bot
sudo systemctl start telegram-ai-bot
sudo systemctl status telegram-ai-bot
```

### 5.3 Nginx

Шаблон конфига:

- `deploy/nginx/telegram-ai-bot.conf`

Установи и включи:

```bash
sudo cp deploy/nginx/telegram-ai-bot.conf /etc/nginx/sites-available/telegram-ai-bot.conf
sudo ln -s /etc/nginx/sites-available/telegram-ai-bot.conf /etc/nginx/sites-enabled/telegram-ai-bot.conf
sudo nginx -t
sudo systemctl reload nginx
```

Добавь TLS (например через certbot), чтобы webhook работал по HTTPS.

## Команды

- `/start` — приветствие
- `/reset` — сброс контекста текущего чата

## Логи

- realtime лог в терминале: при запуске `python bot.py`
- файл лога: `logs/bot.log`
- смотреть хвост:

```bash
tail -f logs/bot.log
```
