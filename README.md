# 🚀 Telegram Services — CRM System & TG Combiner

> **Версия:** 2026-05-03 | **Деплой:** Docker Compose / Systemd

----------


🔹 CRM System:

Полная архитектура (Business API → Webhook → Dispatcher → AI/Мастер-группа → WebApp)
AI-рекрутер на DeepSeek-V3: debounce, имитация живого человека, эскалация, [SILENCE], [LEAD_READY]
Human Override логика (ручной ответ → AI на паузе)
Тестовый AI-чат
Все 25+ API эндпоинтов
🔹 TG Combiner:

12 режимов рассылки + spintax
Anti-Ban система (лимиты, задержки, FloodWait)
4 режима парсинга + Smart AI-Parser на Claude Opus 4.5
Курьерский словарь (50+ терминов с весами)
Device Spoofing (30+ реальных устройств)
WebApp с WebSocket real-time + Gemini AI генерация ответов
Двухуровневая аутентификация (initData + auth_token)
🔹 Деплой:

Docker Compose (WARP + CRM + Combiner)
Systemd конфигурация
Nginx + SSL (с WebSocket proxy)
Troubleshooting таблица

----------

## 📋 Обзор

Два независимых Telegram-сервиса, работающих на одном VPS:

| Сервис | Назначение | Стек | Порт | Домен |
|--------|-----------|------|------|-------|
| **CRM System** | AI-рекрутер с Business API | Aiogram 3.15 / FastAPI / SQLite / DeepSeek-V3 | `8000` | `crmsystem.example.com` |
| **TG Combiner** | Парсер + Рассыльщик + Пульт | Pyrogram (MTProto) / FastAPI / WebSocket / Gemini | `8080` | `combiner.example.com` |

---

# 📦 Сервис 1: CRM System

## Что это

CRM-система для управления лидами через Telegram Business API. Подключается к рабочим аккаунтам Telegram как «бизнес-бот» и перехватывает все входящие/исходящие сообщения. Имеет встроенного **AI-рекрутера** на DeepSeek-V3, который автоматически отвечает лидам, собирает данные и эскалирует сложные вопросы оператору.

## Архитектура

```
Лид в Telegram ──► Telegram Business API ──► Webhook /tg-webhook ──► Aiogram Dispatcher
                                                                          │
                                              ┌───────────────────────────┤
                                              │                           │
                                         AI Handler               Мастер-группа
                                        (DeepSeek-V3)             (Форум с топиками)
                                              │                           │
                                              ▼                           ▼
                                     Ответ лиду через              Оператор отвечает
                                     Business API                  из топика форума
                                              │
                                              ▼
                                     SQLite (crm_data.db)
                                              │
                                              ▼
                                     FastAPI REST API
                                              │
                                              ▼
                                     Telegram Mini App (WebApp)
```

## Ключевые функции

### 1. Бизнес-подключение (`@dp.business_connection`)
- Автоматически обнаруживает подключение/отключение рабочих аккаунтов
- Мигрирует чаты при смене `business_connection_id`
- Сохраняет `ai_enabled` статус при переподключении

### 2. Перехват сообщений (`@dp.business_message`)
- Ловит **все** сообщения (входящие и исходящие) через Business API
- Скачивает медиа (фото, видео, голос, документы) на сервер
- Кэширует аватарки пользователей
- Создаёт топик в мастер-группе для каждого нового лида
- Пересылает сообщения лидов в соответствующие топики

### 3. Ответы из мастер-группы (`@dp.message` в группе)
- Оператор пишет в топик → бот пересылает ответ лиду через Business API
- Поддержка любых типов контента (текст, фото, видео и т.д.)

### 4. WebApp (Telegram Mini App)
- **Glassmorphism UI** — полноценная CRM-панель внутри Telegram
- Список чатов с лидами, непрочитанные счётчики, приоритеты
- Полная история сообщений с медиа
- Отправка текста и файлов лидам прямо из WebApp
- Управление AI-настройками
- Тестовый чат с AI (без отправки в Telegram)

## 🤖 AI-Ассистент (DeepSeek-V3)

### Как работает

**Провайдер:** OnlySQ API (`api.onlysq.ru/ai/openai/`) — OpenAI-совместимый прокси для DeepSeek-V3.

**Включение:** В WebApp переключатель "AI" на карточке рабочего аккаунта. AI включается per-account, не глобально.

### Механика ответа

1. **Debounce** — при получении сообщения от лида запускается адаптивный таймер:
   - Короткое сообщение (<30 симв.): ждёт 7-9 сек (лид может дописать мысль)
   - Среднее (30-80 симв.): 5-6.5 сек
   - Длинное (>80 симв.): 3.5-5 сек
   - Если лид пишет ещё одно сообщение — таймер сбрасывается (debounce)

2. **Имитация живого человека:**
   - `read_delay` — задержка перед "прочтением" (бот читает сообщение через Telegram API)
   - Статус "typing" отправляется пока AI генерирует ответ
   - `typing_delay` — имитация набора текста пропорционально длине ответа (100 симв. = 10 сек)
   - Ответ разбивается на части по `|||` или двойному переносу — каждая часть отправляется отдельным сообщением с паузой

3. **Контекст:**
   - Загружает последние 100 сообщений чата из SQLite
   - System prompt + база знаний + имя лида
   - Лимит параллельных запросов к API: 3 (семафор)
   - 3 попытки при rate limit (пауза 20 сек между попытками)

### Специальные теги AI

| Тег | Действие |
|-----|----------|
| `<ESCALATE>причина</ESCALATE>` | AI не знает ответа → ставит чат на паузу, шлёт алерт операторам |
| `[SILENCE]` | AI решил не отвечать (лид написал "ок", "спс") |
| `[LEAD_READY]{json}` | Лид готов → извлекаются данные (телефон, ФИО, ДР) → алерт операторам |

### Human Override
- Если оператор вручную ответил в чат → AI ставится на паузу (`ai_paused=1`)
- Возобновить AI можно через WebApp (кнопка "Возобновить AI")
- Force AI — принудительный вызов ответа AI через WebApp

### Настройки AI (через WebApp)
- **API Key** — ключ OnlySQ (проверяется через `/api/ai_settings/verify`)
- **System Prompt** — инструкция для AI-рекрутера
- **Knowledge Base** — база знаний (условия работы, FAQ)
- **Read Delay** — задержка перед прочтением (сек)
- **Typing Delay** — задержка перед началом печати (сек)

### Тестовый чат AI
- Создание тест-чатов через WebApp без реальной отправки в Telegram
- AI может начать первым (как рабочий аккаунт пишет лиду)
- Отображает специальные теги (ESCALATE, LEAD_READY, SILENCE)

## API Endpoints (CRM)

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/` | Отдаёт WebApp (index.html) |
| POST | `/tg-webhook` | Webhook для Telegram |
| GET | `/api/accounts` | Список подключённых бизнес-аккаунтов |
| GET | `/api/chats` | Список чатов с лидами |
| GET | `/api/messages/{chat_id}` | История сообщений чата |
| POST | `/api/send/{chat_id}` | Отправить текст лиду |
| POST | `/api/upload/{chat_id}` | Отправить файл лиду |
| POST | `/api/chats/{chat_id}/read` | Пометить прочитанным |
| POST | `/api/chats/{chat_id}/priority` | Переключить приоритет |
| POST | `/api/accounts/{id}/toggle_ai` | Вкл/выкл AI для аккаунта |
| POST | `/api/chats/{id}/toggle_ai` | Вкл/выкл AI для чата |
| POST | `/api/chats/{id}/force_ai` | Принудительный вызов AI |
| POST | `/api/chats/{id}/generate_ai` | Сгенерировать текст (без отправки) |
| GET/POST | `/api/ai_settings` | Получить/обновить настройки AI |
| POST | `/api/ai_settings/verify` | Проверить API-ключ |
| GET/POST/DELETE | `/api/operators` | CRUD операторов |
| GET/POST/DELETE | `/api/test_ai/*` | Тестовые AI-чаты |

**Аутентификация:** HMAC-SHA256 валидация Telegram initData (заголовок `Authorization: tma <initData>`).

---

# 📦 Сервис 2: TG Combiner

## Что это

Многофункциональный инструмент для работы с Telegram через MTProto (Pyrogram). Объединяет: управление несколькими аккаунтами, массовую рассылку (12 режимов), парсинг пользователей (включая AI-парсер на Claude), и glassmorphism WebApp для управления чатами.

## Архитектура

```
Telegram MTProto ◄──► Pyrogram User Clients (sessions/*.session)
                           │
                     ┌─────┴─────┐
                     │           │
              Bot Client    User Clients (1..N)
              (управление)   (рассылка, парсинг, чаты)
                     │           │
                     ▼           ▼
              bot_interface.py   modules/sender.py
              (Inline-меню)      modules/parser.py
                                 modules/direct_sender.py
                     │
                     ▼
              FastAPI WebApp (webapp/main.py)
                     │
              ┌──────┼──────┐
              │      │      │
           REST   WebSocket  Static
           API    (real-time) (SPA)
```

## Модули

### 1. Управление сессиями (Аккаунтами)
- Авторизация Telegram-аккаунтов прямо из бота: номер → код → 2FA
- Каждая сессия — `.session` файл Pyrogram в папке `sessions/`
- **Device Spoofing** — каждый клиент получает случайный fingerprint из пула 30+ реальных устройств (Samsung, Xiaomi, Pixel, OnePlus и др.)
- Автозапуск всех сессий при старте бота

### 2. Рассылка (12 режимов)

| Режим | Описание |
|-------|----------|
| `text` | Обычный текст |
| `media` | Фото с caption |
| `repost` | Пересылка поста из канала |
| `hidden_repost` | Копирование поста (без "Forwarded from") |
| `video_note` | Кружочек (видеосообщение) |
| `voice` | Голосовое сообщение |
| `postbot` | Через @postbot |
| `secret_chat` | Секретный чат |

**Spintax:** Рандомизация текста — `{Привет|Здравствуйте}, {как|какие} дела?`

**Anti-Ban система (`antiban.py`):**
- `GLOBAL_LIMIT` — максимум сообщений за сессию рассылки (по умолчанию 500)
- `ACCOUNT_LIMIT` — лимит на один аккаунт (20)
- `MIN_DELAY` / `MAX_DELAY` — случайная задержка между отправками (3-10 сек)
- FloodWait обработка: ожидание до 120 сек, затем пропуск аккаунта
- Round-robin распределение по аккаунтам

**Мультиаккаунтность:** Рассылка автоматически распределяется между всеми выбранными аккаунтами.

### 3. Парсер пользователей (4 + 1 режимов)

| Режим | Источник | Метод |
|-------|----------|-------|
| **Участники групп** | Группа/чат | `get_chat_members` с алфавитным перебором (A-Z + А-Я + 0-9) |
| **Комментаторы** | Канал + пост | `get_discussion_replies` |
| **Писавшие в чат** | Группа/чат | Сканирование истории сообщений |
| **Smart AI-Parser** | Группа/чат | AI-анализ сообщений |

**Фильтры парсинга:**
- Пол (эвристика по имени: окончания -а, -я = женский)
- Активность: был сегодня / на неделе / в месяце
- Наличие аватарки
- Наличие Premium
- Глубина по дням

**Экспорт:** Excel (`.xlsx`) через pandas

### 4. 🧠 Smart AI-Parser (Claude через OnlySQ)

Продвинутый парсер, который использует **Claude Opus 4.5** для анализа сообщений пользователей и определения подходящих кандидатов на позицию курьера.

**Как работает:**
1. Сканирует историю чата за N дней
2. Собирает все сообщения каждого уникального пользователя (до 30 штук)
3. Обогащает данные: bio, статус, premium
4. Формирует батчи по 150 пользователей
5. Отправляет в Claude с промптом HR-аналитика
6. Claude возвращает JSON с `is_target`, `confidence`, `reason`, `inferred_age`, `inferred_city`

**Настраиваемые параметры:**
- Возрастной диапазон (по умолчанию 16-25)
- Город (строгий/мягкий режим)
- Требование опыта курьера
- Строгость отбора (0-100)
- Порог курьерского сленга
- Кастомный промпт

**Курьерский словарь:** 50+ терминов с весами — "батч" (вес 2), "доставка" (вес 1) — для предварительной фильтрации перед отправкой в AI.

### 5. WebApp (Telegram Mini App)

**Glassmorphism SPA** — полнофункциональная панель управления.

**Функции WebApp:**
- Просмотр чатов всех подключённых аккаунтов
- История сообщений с lazy-loading
- Отправка сообщений от имени аккаунта
- **AI-генерация ответов** (Gemini 1.5 Flash) — кнопка "🤖" в чате
- Прямая рассылка (Direct Send) с прогрессом через WebSocket
- Запуск Smart AI-Parser из WebApp
- Аватарки с кэшированием на сервере
- Real-time уведомления о новых сообщениях через WebSocket

**Аутентификация WebApp (двухуровневая):**
1. `initData` — стандартная валидация Telegram Mini App (HMAC-SHA256)
2. `auth_token` — кастомный HMAC-токен для запуска через Menu Button (т.к. Menu Button не передаёт initData)

### 6. 🤖 AI в Combiner (Gemini 1.5 Flash)

**Файл:** `webapp/llm.py`

Генерирует контекстные ответы для переписок. Принимает историю чата и возвращает короткий, естественный ответ.

**Требует:** `GEMINI_API_KEY` в `.env`

---

# 🐳 Развёртывание

## Docker Compose (рекомендуется)

### Структура

```
VPS_Backup/
├── docker-compose.yml          # Оркестрация: WARP + CRM + Combiner
├── crm_bot/
│   ├── .env                    # ⚡ BOT_TOKEN, MASTER_GROUP_ID
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                 # Точка входа
│   ├── ai_handler.py           # DeepSeek AI логика
│   ├── crm_data.db             # 💾 SQLite база
│   └── frontend/               # WebApp assets
└── tg_combiner/
    ├── .env                    # ⚡ API_ID, API_HASH, BOT_TOKEN, ADMIN_ID
    ├── Dockerfile
    ├── requirements.txt
    ├── config.py               # Центральная конфигурация
    ├── main.py                 # Точка входа
    ├── bot_interface.py        # Inline-меню бота
    ├── antiban.py              # Антибан-система
    ├── device_spoof.py         # Подмена устройств
    ├── sessions/               # 💾 КРИТИЧНО: .session файлы
    ├── admins.json             # 💾 Список админов
    ├── contacts.json           # 💾 Спарсенные контакты
    ├── modules/
    │   ├── sender.py           # 12 режимов рассылки
    │   ├── parser.py           # Парсер + Smart AI Parser
    │   ├── direct_sender.py    # Прямая отправка из WebApp
    │   └── smart_parser_menu.py
    └── webapp/
        ├── main.py             # FastAPI + WebSocket
        ├── auth.py             # Двухуровневая аутентификация
        ├── llm.py              # Gemini AI
        └── static/index.html   # SPA интерфейс
```

### Шаг 1: `.env` файлы

**`crm_bot/.env`:**
```bash
BOT_TOKEN=<от @BotFather>
MASTER_GROUP_ID=<ID группы с форумом>
# PROXY_URL задаётся в docker-compose.yml автоматически
```

**`tg_combiner/.env`:**
```bash
API_ID=<от https://my.telegram.org>
API_HASH=<от https://my.telegram.org>
BOT_TOKEN=<от @BotFather>
ADMIN_ID=<ваш Telegram user ID>
GEMINI_API_KEY=<опционально, для AI в WebApp>
# SOCKS5_HOST/PORT задаются в docker-compose.yml
```

### Шаг 2: Подготовка данных

```bash
touch crm_bot/crm_data.db
mkdir -p crm_bot/frontend/{avatars,media} tg_combiner/sessions
[ -f tg_combiner/admins.json ] || echo '{"admins": []}' > tg_combiner/admins.json
[ -f tg_combiner/contacts.json ] || echo '[]' > tg_combiner/contacts.json
```

> ⚠️ **`tg_combiner/sessions/`** — содержит авторизованные Telegram-аккаунты. Потеря = повторная авторизация.

### Шаг 3: Запуск

```bash
docker compose up -d
# Порядок: warp (healthcheck) → crm-bot + tg-combiner
```

### Шаг 4: Проверка

```bash
docker compose ps
docker compose logs warp        # "Connected"
docker compose logs crm-bot     # "Webhook установлен"
docker compose logs tg-combiner # "Admin Bot started"
```

## Systemd (текущая конфигурация на VPS)

### WARP Proxy

```bash
# Установка
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | sudo gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflare-client.list
sudo apt update && sudo apt install -y cloudflare-warp

# Настройка
sudo warp-cli --accept-tos registration new
sudo warp-cli --accept-tos mode proxy
sudo warp-cli --accept-tos proxy port 40000
sudo warp-cli --accept-tos connect
```

### Systemd-сервисы

```ini
# /etc/systemd/system/crm-bot.service
[Unit]
Description=CRM Telegram Bot
After=network.target warp-svc.service
[Service]
Type=simple
User=user1
WorkingDirectory=/home/user1/crm_bot
ExecStart=/home/user1/crm_bot/venv/bin/python main.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/tg_combiner.service
[Unit]
Description=TG Combiner Bot
After=network.target warp-svc.service
[Service]
Type=simple
User=user1
WorkingDirectory=/home/user1/tg_combiner
ExecStart=/home/user1/tg_combiner/venv/bin/python main.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
```

## Nginx + SSL

### DNS A-записи

| Host | Тип | IP |
|------|-----|-----|
| `crmsystem` | A | `<IP сервера>` |
| `combiner` | A | `<IP сервера>` |

### Nginx для CRM

```nginx
server {
    listen 80;
    server_name crmsystem.yourdomain.com;
    location / {
        client_max_body_size 50m;
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Nginx для Combiner (+ WebSocket!)

```nginx
server {
    listen 80;
    server_name combiner.yourdomain.com;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location /ws {
        proxy_pass http://127.0.0.1:8080/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }
}
```

### SSL

```bash
sudo ln -sf /etc/nginx/sites-available/{crmsystem,combiner}.conf /etc/nginx/sites-enabled/
sudo certbot --nginx -d crmsystem.yourdomain.com -d combiner.yourdomain.com --non-interactive
```

## Хардкоженные URL (обновить при смене домена!)

| Файл | Строка | Что менять |
|------|--------|-----------|
| `crm_bot/main.py` | ~330 | `WebAppInfo(url="https://crmsystem.ДОМЕН/")` |
| `crm_bot/main.py` | ~560 | `WEBHOOK_URL = f"https://crmsystem.ДОМЕН{WEBHOOK_PATH}"` |
| `tg_combiner/bot_interface.py` | ~50 | `_APP_URL_BASE = "https://combiner.ДОМЕН/app"` |

---

# ⚠️ КРИТИЧНО: Telegram-блокировка

Российские VPS **блокируют** Telegram API. Без WARP-прокси сервисы не работают.

**Как настроен прокси в коде:**
- CRM: env var `PROXY_URL` (default: `socks5://127.0.0.1:40000`)
- Combiner: env vars `SOCKS5_HOST` / `SOCKS5_PORT` → `config.PYROGRAM_PROXY`
- Docker автоматически переопределяет через `environment:` в `docker-compose.yml`

---

# 🐛 Troubleshooting

| Проблема | Причина | Решение |
|----------|---------|---------|
| Бот не реагирует | Нет связи с Telegram | `warp-cli status` / `docker logs warp` |
| WebApp спиннер | DNS не дошёл до Telegram CDN | `dig +short domain @8.8.8.8`, подождать 5-15 мин |
| `ModuleNotFoundError: aiohttp_socks` | Не установлен | `pip install aiohttp-socks` |
| `Socket error: 0x04` | WARP не подключён | Проверить `SOCKS5_HOST`/`PORT` |
| Чаты не грузятся в WebApp | Nginx не проксирует WS | Добавить `location /ws` с `Upgrade` |
| AI не отвечает в CRM | `ai_enabled=0` или `ai_paused=1` | Включить в WebApp |
| Menu Button не работает | `set_chat_menu_button` не вызван | Перезапустить бота |

---

# 📞 Текущий деплой

| Параметр | Значение |
|----------|----------|
| VPS | `123.45.67.89` |
| SSH | `ssh user@123.45.67.89` |
| CRM | `https://crmsystem.example.com` |
| Combiner | `https://combiner.example.com/app` |
| CRM бот | через Business API |
| Combiner бот | `@tgcombinebot` |
| WARP | `127.0.0.1:40000` (systemd) |
| SSL | Let's Encrypt (auto-renew) |
