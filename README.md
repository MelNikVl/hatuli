# Hatuli — Аналитическая платформа инвестиций в недвижимость Астаны

Система автоматически парсит объявления о продаже и аренде квартир с Krisha.kz, рассчитывает инвестиционный скор каждого объекта и помогает принять решение о покупке.

## Что умеет

- **Парсинг продаж** — квартиры, паркинги, кладовки, коммерция с krisha.kz/prodazha/
- **Парсинг аренды** — строит rental_index: реальные медианные ставки по ЖК, району, комнатности
- **Скоринг квартир** — 7 критериев, 100 баллов (yield, цена vs рынок, локация, этаж, ЖК, тип, supply)
- **Анализ торга** — сравнивает с реальными аналогами из БД, определяет температуру рынка, рекомендует цену
- **Название ЖК** — автоматически парсится со страницы каждого объявления
- **Google Sheets sync** — три вкладки: Квартиры, Аренда, Инвест-объекты
- **Веб-терминал** — дашборд, аналитика квартир с breakdown скора, скоринговая модель, логи

## Архитектура

```
krisha_bot/
├── bot/
│   ├── core/
│   │   ├── apartment_parser.py     # Парсер квартир на продажу + полный pipeline
│   │   ├── apartment_score_v2.py   # Скоринговая модель v2
│   │   ├── apartment_details.py    # Детальный парсер страницы объявления
│   │   ├── rental_parser.py        # Парсер аренды + rental_index
│   │   ├── investment_score.py     # Скоринг паркингов/кладовок
│   │   ├── parser.py               # Базовый парсер инвест-объектов
│   │   ├── bargain.py              # Анализ торга по аналогам
│   │   ├── dedup_listings.py       # Дедупликация объявлений
│   │   ├── sheets_sync.py          # Google Sheets синхронизация
│   │   └── sheets_sync_rental.py   # Синхронизация вкладки Аренда
│   ├── db/
│   │   ├── pg.py                   # PostgreSQL connection pool (asyncpg)
│   │   ├── models.py               # DDL / инициализация схемы
│   │   ├── schema.sql              # SQL схема всех таблиц
│   │   └── compat.py               # SQLite compat layer (legacy)
│   ├── admin_web.py                # FastAPI веб-терминал
│   └── templates/                  # Jinja2 шаблоны
│       ├── dashboard.html          # Дашборд мониторинга
│       ├── analytics.html          # Таблица квартир со скорами
│       ├── analytics_detail.html   # Детальная карточка объекта
│       ├── scoring.html            # Документация скоринговой модели
│       └── logs_page.html          # Просмотр логов
├── service_rental.py               # Сервис парсинга аренды (loop)
├── service_apartments.py           # Сервис парсинга продаж (loop)
├── service_web.py                  # Веб-терминал (FastAPI)
└── migrate_sqlite_to_pg.py         # Миграция SQLite → PostgreSQL
```

## Базы данных (PostgreSQL)

| Таблица | Описание |
|---------|----------|
| `rental_listings` | Сырые объявления аренды (1к+ записей) |
| `rental_index` | Агрегированные ставки аренды по ЖК/район/комнаты |
| `apartment_listings` | Квартиры на продажу со скорами |
| `investment_listings` | Паркинги, кладовки, коммерция |
| `listings` | Общая таблица объявлений |

## Скоринговая модель (100 баллов)

| Критерий | Баллы | Источник данных |
|----------|-------|-----------------|
| Yield (доходность) | 20 | rental_index — реальные ставки аренды |
| Цена vs рынок | 15 | Медиана цен по району из текущего цикла |
| Локация | 20 | Район + POI (ТРЦ, метро, LRT) |
| Тип квартиры | 15 | Комнатность + штраф за площадь 70м²+ |
| Этаж | 8 | Штраф за 1й и последний этаж |
| Скор ЖК | 15 | Год постройки |
| Supply | 7 | Кол-во аналогов в ЖК |

**Анализ торга** — скрипт берёт до 30 аналогов из БД (тот же район, комнаты, площадь ±15%), определяет температуру рынка по доле "старых" объявлений (30+ дней), рекомендует дисконт 2–10%.

## Сервисы (systemd)

```bash
# Парсер аренды — каждые 5-30 минут, страница за раз
krisha-rental-parser.service + .timer

# Парсер продаж — каждые 30-120 минут, полный цикл
krisha-apartment-parser.service + .timer

# Веб-терминал — постоянно
krisha-web.service
```

## Установка

```bash
# 1. Клонировать
git clone https://github.com/MelNikVl/hatuli.git
cd hatuli

# 2. Виртуальное окружение
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. PostgreSQL
sudo apt install postgresql
sudo -u postgres createuser -P krisha
sudo -u postgres createdb -O krisha krisha_bot
psql -U krisha -d krisha_bot -h localhost -f bot/db/schema.sql

# 4. Конфигурация
cp .env.example .env
# Заполнить .env: DATABASE_URL, BOT_TOKEN, ADMIN_PASSWORD

# 5. Добавить google_creds.json (сервисный аккаунт Google Cloud)
# Открыть таблицу → Поделиться → добавить email сервисного аккаунта

# 6. Запустить сервисы
sudo cp *.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now krisha-web krisha-rental-parser.timer krisha-apartment-parser.timer
```

## Веб-терминал

```
http://localhost:8082/admin/           — дашборд мониторинга
http://localhost:8082/admin/analytics  — аналитика квартир
http://localhost:8082/admin/scoring    — документация скоринга
http://localhost:8082/admin/logs/page  — логи сервисов
```

## Переменные окружения

```env
DATABASE_URL=postgresql://krisha:password@localhost/krisha_bot
BOT_TOKEN=                    # Telegram Bot Token (опционально)
ADMIN_PASSWORD=               # Пароль веб-терминала
DB_PATH=bot.db                # SQLite (legacy)
PARSER_ENABLED=0              # 0 = парсер продаж выключен
```

## Планы

- Скор по ЖК (застройщик, КСК, отзывы, тайный покупатель)
- AI-комментарии в Google Sheets
- Анализ фото (состояние ремонта)
- Данные новостроек и застройщиков
- Публичный рейтинг ЖК
