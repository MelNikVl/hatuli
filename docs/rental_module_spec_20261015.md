# ТЗ: арендный модуль Hatuli

Oct 15, 2026 · @Nik

## Контекст и цель

Цель: превратить уже собираемую аренду в инструмент решений «купить под сдачу / взять в субаренду», не уводя продукт от покупки. Новых страниц — две (сканер и посуточный индекс), остальное встраивается в существующие.

Что уже есть в репо (на 15.10, `main`):

- Парсер аренды `bot/core/rental_parser.py` (квартиры, паркинг, коммерция) → `rental_listings`, история цен `rental_price_history`, архивация (`archive_check.check_archived_rentals`, `stale_archive.archive_stale_rentals`).
- `rebuild_rental_index()` — медиана / p25 / p75 / ₸/м² по уровням ЖК+комн → район → город за 30 дней, upsert в `rental_index`.
- `lookup_rental_estimate()` + расчёт в `apartment_parser.py`: `est_rent = price_per_sqm × area`, `yield_pct` (gross), `net_yield_pct` (×0.95 вакантность, −10% налог/ремонт, ×1.02 покупка), `payback_years`, `rent_source`.
- `deal_score.py`: блок MARKET (15%) использует gross `yield_pct / 15`.
- `listing_detail.py`: флаг `high_yield` при `yield_pct ≥ 13.9`; тепловая карта «Цены аренды» и `rental_comps` в карточке.

Чего нет: признака срока аренды (сутки / месяц), мебели, описания, `complex_id` и `hex_id` у аренды, посуточного индекса, метрики скорости сдачи.

Не делаем: CRM управления квартирами (брони, уборки, ключи), парсинг Airbnb/Booking на этом этапе, изменение весов Deal Score сверх описанного в Этапе 1.

Работа — в отдельном worktree (`docs/process.md`), ветка `feat/rental-module`, по одному PR на этап.

## Этап 0 — гигиена данных аренды (блокер)

Без этого этапа любой yield и сканер будут на грязных цифрах: сейчас в месячный индекс, скорее всего, попадают посуточные цены, а устаревшие строки индекса никогда не удаляются. Сначала замер на прод-БД, потом правки.

**0.1. Разделить сутки и месяц.** В `RENTAL_PATHS` нет фильтра периода, `_parse_card` пропускает цены от 10 000 ₸, `rebuild_rental_index` — от 15 000 ₸. Посуточная однушка за 15–30 тыс. проходит оба фильтра и занижает медиану.

1. Вручную на Крыше проверить: идут ли посуточные в `/arenda/kvartiry/astana/`, как период виден в карточке выдачи (текст цены «за сутки» / «в месяц») и есть ли GET-фильтр периода в URL. Результат — в докстринг парсера.
2. Миграция: `rental_listings.rent_period TEXT CHECK (rent_period IN ('daily','monthly','unknown'))`.
3. `_parse_card`: заполнять `rent_period` по тексту цены/заголовка; если фильтр в URL есть — завести отдельные пути в `RENTAL_PATHS` со своим курсором.
4. Бэкфил истории: эвристика по тексту + цена на м² (порог подобрать по распределению, не хардкодить); спорные → `unknown`.
5. `rebuild_rental_index` считает месячный индекс только по `monthly`; ключ `rental_index` расширить на `rent_period` (UNIQUE NULLS NOT DISTINCT, как в миграции 030).

**0.2. Протухшие строки индекса.** Upsert обновляет только группы из текущего 30-дневного окна; группа, выпавшая из окна, живёт вечно, и `lookup_rental_estimate` её берёт без проверки даты и размера выборки.

- В конце rebuild в той же транзакции: `DELETE FROM rental_index WHERE updated_at < <старт rebuild>` (для пересчитанных `prop_type`/`rent_period`).
- `lookup_rental_estimate`: принимать уровень только при `sample_count ≥ RENT_MIN_SAMPLE_COMPLEX` (ЖК, дефолт 5) / `RENT_MIN_SAMPLE_AREA` (район/город, дефолт 10), иначе падать на следующий. Сейчас достаточно n=2.
- Возвращать в результате `p25/p75` и `updated_at` — нужны для диапазона в Этапе 1.
- p25/p75 считать через `statistics.quantiles(n=4)`, а не индексами `n//4-1`.

**0.3. Привязка к ЖК по id, не по имени.** Продажа ищет аренду по строке `complex_name` (или первой части адреса), а аренда нормализует имя по-своему — совпадения случайны.

- Добавить `rental_listings.complex_id INT REFERENCES complexes(id)` и заполнять тем же резолвером, что `apartment_listings.complex_id` (миграция 100), с учётом зонтиков; бэкфил истории.
- `rental_index.complex_id` + лукап по `complex_id`; имя — только фолбэк, пока покрытие id неполное.
- Добавить `rental_listings.hex_id` (считать `hexgrid.hex_id` при получении координат в `backfill_rental_details`, ребро из `HEX_EDGE_M`).

**0.4. Порядок в коде.**

- Удалить `bot/core/rental_index.py` — нигде не импортируется, дублирует `rental_parser`.
- Проверить, не крутятся ли одновременно `_rental_loop` в `bot/main.py` и `service_rental.py` (двойной обход и rebuild); оставить один.
- Константы net yield (0.95 / 0.10 / 1.02) и порог `high_yield` 13.9 — в `app_settings` (список — в разделе настроек).

**Приёмка Этапа 0** — отчёт `docs/rental_stage0.md` до/после: доля `daily` среди активных, сколько строк индекса удалено, сдвиг медианы по району × комнатности, покрытие `complex_id` и `hex_id` у аренды, как сдвинулось распределение `yield_pct` и доля `rent_source` по уровням. После релиза — пересчёт yield и Deal Score по всей активной базе.

## Этап 1 — доходность в карточке, фильтр, алерт

Результат: у каждой квартиры на продажу — честная оценка аренды с диапазоном и доверием, сравнение с рынком, фильтр и алерт. Всё на данных Этапа 0, без нового парсинга.

**1.1. Бенчмарки доходности.** Новая таблица `rental_yield_benchmarks` (схема — в разделе БД), считается сразу после пересчёта yield: медиана и p90 `net_yield_pct` по срезам город / район / класс жилья × комнатность. Исключать дубли и однодневки — как в метриках недели 2.

**1.2. Блок «Доходность сдачи» в карточке.** `listing_detail.py` отдаёт объект `rental_economics`, шаблон карточки и buyer-bot его рисуют:

```json
{
  "est_rent": 280000,
  "rent_range": [250000, 320000],
  "rent_source": {"level": "ЖК", "n": 14, "updated_at": "2026-10-15"},
  "confidence": "high",
  "gross_yield_pct": 8.4,
  "net_yield_pct": 6.9,
  "payback_years": 11.9,
  "benchmark": {"scope": "Есиль · комфорт · 2к", "median_net": 6.1, "p90_net": 7.8, "percentile": 72},
  "rental_comps": ["…до 5 активных аренд в том же ЖК/гексе…"]
}
```

- `rent_range` = `est_rent × p25/median … est_rent × p75/median` того же уровня индекса.
- `confidence`: high — ЖК, n ≥ 5; medium — район+комнаты, n ≥ 10; low — город или «все комнаты». При low yield показывать серым с подписью «мало данных по ЖК».
- Главная цифра — net, gross — вторично и с расшифровкой допущений (вакантность, налог, затраты на покупку).
- Посуточный сценарий в карточке **не показывать до Этапа 3**. Грубые коэффициенты «долгосрок × 1.5» без данных — выдумка, а продукт продаёт точность.
- Эндпоинт для фронта и бота: `GET /api/listing/{id}/rental-economics` → объект выше.

**1.3. Фильтр и сортировка.**

- На карте и в списках продажи: фильтры `net_yield_min`, `rent_confidence` (≥ medium по умолчанию), сортировка `net_yield_pct`.
- Пресет «Высокая доходность» = net ≥ p90 своего среза из 1.1 и confidence ≥ medium. Флаг `high_yield` перевести на то же правило вместо фиксированных 13.9% gross.
- Индекс `apartment_listings (net_yield_pct DESC) WHERE is_active`.

**1.4. Telegram-алерт по доходности.** Новый тип подписки в существующем механизме подписок (уточнить, через какой бот — buyer-bot или основной): параметры `net_yield_min`, бюджет, комнаты, районы или ЖК.

- Триггеры: новое объявление или снижение цены, переведшее объект через порог.
- Только confidence ≥ medium, не дубли, дедуп по `property_id` (одна квартира — одно уведомление на все её перевыставления), лимит N в сутки на подписчика.
- Текст: цена, оценка аренды с диапазоном, net yield vs медиана среза, Deal Score, ссылка на карточку.

**1.5. Deal Score — не трогать вслепую.** Блок MARKET сейчас берёт gross `yield_pct / 15`. Вариант «net + штраф за low confidence» прогнать через `bot/jobs/deal_score_backtest.py` и переключать только если метрики бэктеста не хуже; результат — в отчёт этапа.

## Этап 2 — посуточный индекс, скорость сдачи, мебель

Порядок изменён против первоначального плана: сканер субаренды считает выручку от посуточной цены, поэтому посуточный индекс делается до него. Иначе сканер стоил бы на коэффициентах из головы.

**2.1. Посуточный индекс.**

- Источник — те же `rental_listings` с `rent_period='daily'` (сбор настроен в 0.1), с координатами, `complex_id`, `hex_id` через `backfill_rental_details`.
- `rebuild_rental_index` для `daily` пишет в тот же `rental_index` (ключ уже с `rent_period`): медиана / p25 / p75 за сутки, n, плюс `active_supply` — сколько активных посуточных в группе сейчас (конкуренция).
- Окно — 30 дней, при n < порога — 90; окно писать в строку индекса.
- Загрузку по Крыше измерить нельзя: объявление висит и тогда, когда квартира занята. Загрузка — параметр сценария, а не данные; в UI так и подписать.

**2.2. Скорость сдачи (аналог срока экспозиции для аренды).**

- Для `monthly`: медиана дней от `found_at` до `archived_at` по ушедшим за 30 дней, срезы как у индекса. Исключить дубли, однодневки (< 2 дней) и `archive_reason='sweep_stale'` — те же правила, что у продажи (неделя 2).
- Поля в `rental_index`: `median_dom_days`, `gone_30d`, `active_supply`, `months_of_supply`.
- В карточке продажи (Этап 1) — строка «похожие сдаются за \~N дней»; длинная экспозиция повышает вакантность в net yield (формула — в разделе настроек).

**2.3. Мебель и описание.**

- `backfill_rental_details` сохраняет `description` (детальная страница уже качается); расширить выборку с «нет ЖК/координат» на «нет описания», темп сохранить.
- `furnishing TEXT CHECK IN ('none','partial','full','unknown')` — классификатор в стиле `finish_classify.py` (словарь + кастомный regex из настроек), тест-выборка из 50 размеченных объявлений.
- Когда по группе хватает n — индекс с разбивкой по `furnishing`; иначе общий.

**2.4. Витрина.** Вкладка «Аренда» на существующей странице аналитики (`_analytics_tabs.html`): таблица район × комнатность — месячная медиана, посуточная медиана, их отношение, срок сдачи, предложение, n. На карте — слой «посуточная / месячная» по гексам (порог n на гекс, пустые не закрашивать).

**Приёмка:** отчёт с покрытием (сколько групп район × комнаты имеют n ≥ 10 по посуточным), точность классификатора мебели на разметке, медиана срока сдачи по районам. Если посуточных мало — сканер идёт только на уровне района, без ЖК.

## Этап 3 — Opportunity Scanner субаренды

Главная метрика сканера — **точка безубыточности по загрузке**, а не маржа: загрузку мы не знаем, а «окупается при загрузке от 42%» — честно и сравнимо между объектами. Маржа показывается для трёх сценариев загрузки.

**3.1. Расчёт.** Модуль `bot/core/sublease.py`, чистые функции без БД, все ставки — из `app_settings` через объект `SubleaseParams`.

```python
DAYS = 30.4

@dataclass
class SubleaseParams:
    occupancy: float            # 0..1, сценарий
    avg_stay_nights: float      # SUBLEASE_AVG_STAY, дефолт 2.5
    cleaning_per_turn: int      # ₸ за уборку, по комнатности
    consumables_per_night: int  # бельё, химия, вода
    utilities_month: int        # коммуналка + интернет, по комнатности
    platform_fee_pct: float     # комиссия площадки/менеджера
    tax_pct: float              # налог с выручки — настройка, не константа
    furnish_capex: int          # если furnishing != 'full'
    capex_months: int           # амортизация, дефолт 24
    deposit_months: float       # депозит собственнику (в стартовый капитал, не в расход)

def sublease_economics(rent_month: int, nightly: int, furnishing: str,
                       p: SubleaseParams) -> dict:
    nights = DAYS * p.occupancy
    revenue = nightly * nights
    turns = nights / p.avg_stay_nights
    variable = (turns * p.cleaning_per_turn
                + nights * p.consumables_per_night
                + revenue * (p.platform_fee_pct + p.tax_pct))
    capex_m = p.furnish_capex / p.capex_months if furnishing != "full" else 0
    fixed = rent_month + p.utilities_month + capex_m
    margin = revenue - variable - fixed

    # точка безубыточности: вклад одной занятой ночи после переменных
    per_night = (nightly * (1 - p.platform_fee_pct - p.tax_pct)
                 - p.consumables_per_night
                 - p.cleaning_per_turn / p.avg_stay_nights)
    breakeven_occ = fixed / (per_night * DAYS) if per_night > 0 else None

    startup = rent_month * (1 + p.deposit_months) + (p.furnish_capex if furnishing != "full" else 0)
    return {
        "revenue": round(revenue), "costs": round(variable + fixed),
        "margin": round(margin),
        "margin_pct": round(margin / revenue * 100, 1) if revenue else None,
        "breakeven_occupancy": round(breakeven_occ, 3) if breakeven_occ else None,
        "startup_capital": round(startup),
        "payback_months": round(startup / margin, 1) if margin > 0 else None,
    }
```

- Сценарии загрузки: `SUBLEASE_OCC_SCENARIOS` = 0.45 / 0.60 / 0.75 (настраиваемо, пользователь может ввести своё).
- `nightly` — медиана посуточного индекса самого узкого уровня с достаточным n (ЖК → район), с тем же `confidence`, что в Этапе 1. Консервативный сценарий берёт p25.
- Вторая стратегия — `mid_term` (взять пустую, меблировать, сдать помесячно): выручка = медиана `monthly` с `furnishing='full'` минус вакантность из срока сдачи (2.2). Включать, только если по мебели хватило n.

**3.2. Кандидаты и ранжирование.**

- Выборка: активные `monthly`, не дубли, с `complex_id` или `hex_id`, цена ≤ медиана своего уровня × `SUBLEASE_CHEAP_RATIO` (дефолт 0.95).
- Сортировка по умолчанию — `breakeven_occupancy` по возрастанию; альтернативы — маржа базового сценария, стартовый капитал.
- Риск-флаги (массив строк, не влияют на расчёт): высокая конкуренция посуточных в гексе/ЖК (`active_supply` выше p75); low confidence по посуточной цене; шум (`score_layers/noise.py`); нет фото; цена на 30%+ ниже медианы (возможен фейк); мебель `unknown`.
- Постоянная плашка на странице: субаренда и посуточная сдача — только с письменного согласия собственника; правила ЖК могут ограничивать посуточную сдачу.

**3.3. API.**

| Метод | Путь | Что делает | Доступ |
| --- | --- | --- | --- |
| GET | `/api/rental/segment?district=&complex_id=&hex_id=&rooms=` | Индексы monthly/daily, срок сдачи, предложение, экономика сегмента по 3 сценариям | бесплатно |
| GET | `/api/rental/scanner?strategy=daily\|mid_term&district=&rooms=&max_rent=&occupancy=&sort=&page=` | Список кандидатов с экономикой и флагами | платно |
| POST | `/api/rental/calc` | Расчёт по одному объявлению или ручным вводным, с переопределением любого поля `SubleaseParams` | бесплатно |
| GET | `/api/rental/scanner.csv` | Экспорт текущей выборки | платно |

Ответ каждого расчёта содержит `params_used` — все ставки, которые вошли в формулу, и `index_source` с уровнем, n и датой индекса.

**3.4. Страница `/rental/scanner`.** Слева — фильтры и параметры (свёрнутый блок «мои расходы»), сверху — карточка сегмента (медианы, срок сдачи, точка безубыточности сегмента), ниже — таблица кандидатов и карта с теми же точками. Шаблон на `base_public.html`, доступ — через существующий `site_auth` (платная часть — `access_locked.html` для бесплатных). Приём оплаты (Telegram Stars или другое) — отдельная задача, здесь только флаг доступа.

**3.5. Алерт субаренды.** На базе 1.4: новое объявление аренды с `breakeven_occupancy ≤` порога подписчика в его районах. Алерт по аренде должен уходить быстро: триггер — сразу после `save_rental_listings`, а не по дневному таймеру.

## БД, настройки, тесты, порядок

**Миграции** (последняя в репо — 102):

```sql
-- 103_rental_period_complex_hex.sql
ALTER TABLE rental_listings
  ADD COLUMN IF NOT EXISTS rent_period TEXT NOT NULL DEFAULT 'unknown'
    CHECK (rent_period IN ('daily','monthly','unknown')),
  ADD COLUMN IF NOT EXISTS complex_id INTEGER REFERENCES complexes(id),
  ADD COLUMN IF NOT EXISTS hex_id TEXT,
  ADD COLUMN IF NOT EXISTS hex_edge_m REAL;
CREATE INDEX IF NOT EXISTS idx_rental_period_active ON rental_listings (rent_period) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_rental_complex_id ON rental_listings (complex_id);
CREATE INDEX IF NOT EXISTS idx_rental_hex ON rental_listings (hex_id);

-- 104_rental_index_v2.sql
ALTER TABLE rental_index
  ADD COLUMN IF NOT EXISTS rent_period TEXT NOT NULL DEFAULT 'monthly',
  ADD COLUMN IF NOT EXISTS complex_id INTEGER,
  ADD COLUMN IF NOT EXISTS furnishing TEXT,           -- NULL = все
  ADD COLUMN IF NOT EXISTS window_days INTEGER,
  ADD COLUMN IF NOT EXISTS median_dom_days REAL,
  ADD COLUMN IF NOT EXISTS gone_30d INTEGER,
  ADD COLUMN IF NOT EXISTS active_supply INTEGER;
ALTER TABLE rental_index DROP CONSTRAINT IF EXISTS rental_index_city_district_complex_name_rooms_prop_type_key;
ALTER TABLE rental_index ADD CONSTRAINT rental_index_key_v2 UNIQUE NULLS NOT DISTINCT
  (city, district, complex_name, complex_id, rooms, prop_type, rent_period, furnishing);

-- 105_rental_yield_benchmarks.sql
CREATE TABLE IF NOT EXISTS rental_yield_benchmarks (
  scope        TEXT NOT NULL,   -- 'city' | 'district' | 'class'
  scope_value  TEXT,
  rooms        INTEGER,
  median_net   REAL, p90_net REAL, n INTEGER,
  computed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE NULLS NOT DISTINCT (scope, scope_value, rooms)
);
CREATE INDEX IF NOT EXISTS idx_apt_net_yield_active
  ON apartment_listings (net_yield_pct DESC) WHERE is_active;

-- 106_rental_description_furnishing.sql
ALTER TABLE rental_listings
  ADD COLUMN IF NOT EXISTS description TEXT,
  ADD COLUMN IF NOT EXISTS furnishing TEXT NOT NULL DEFAULT 'unknown'
    CHECK (furnishing IN ('none','partial','full','unknown'));
```

Плюс алерты (1.4, 3.5) — схема по образцу существующей таблицы подписок (`migrations/001_alerts.sql`), поля `kind`, `params JSONB`, журнал отправок для дедупа.

**Настройки `app_settings`** (дефолты — стартовые, проверить на рынке перед релизом):

| Ключ | Дефолт | Где |
| --- | --- | --- |
| `RENT_MIN_SAMPLE_COMPLEX` / `_AREA` | 5 / 10 | 0.2, лукап индекса |
| `NET_YIELD_VACANCY` | 0.95, либо из срока сдачи: 1 − dom/365 | net yield |
| `NET_YIELD_OPEX_SHARE` | 0.10 | net yield |
| `NET_YIELD_PURCHASE_COST` | 1.02 | net yield |
| `SUBLEASE_OCC_SCENARIOS` | 0.45, 0.60, 0.75 | 3.1 |
| `SUBLEASE_AVG_STAY` | 2.5 ночи | 3.1 |
| `SUBLEASE_CLEANING_BY_ROOMS` | JSON, ₸ | 3.1 |
| `SUBLEASE_UTILITIES_BY_ROOMS` | JSON, ₸/мес | 3.1 |
| `SUBLEASE_PLATFORM_FEE` | 0.0 (Krisha), выше для агрегаторов | 3.1 |
| `SUBLEASE_TAX_PCT` | заполнить по актуальному режиму в РК | 3.1 |
| `SUBLEASE_FURNISH_CAPEX_BY_ROOMS` / `_MONTHS` | JSON, ₸ / 24 | 3.1 |
| `SUBLEASE_CHEAP_RATIO` | 0.95 | 3.2 |

**Тесты** (pytest, в стиле существующих):

- [ ] `rent_period`: карточки «за сутки» / «в месяц» / без метки → daily / monthly / unknown; daily не попадает в monthly-индекс.
- [ ] rebuild удаляет группы, выпавшие из окна; лукап пропускает уровень с n ниже порога.
- [ ] лукап по `complex_id` приоритетнее имени; зонтик и дом разрешаются как у продажи.
- [ ] `sublease_economics`: ручной пример с посчитанным ответом; при `occupancy = breakeven_occupancy` маржа ≈ 0; `per_night ≤ 0` → `None`, не деление на ноль; furnishing full → без capex.
- [ ] алерт: одно уведомление на `property_id` при перевыставлении, лимит в сутки, low confidence не шлётся.
- [ ] smoke новых роутов в `test_admin_route_smoke.py`; доступ к платным эндпоинтам без прав → 403.
- [ ] полный suite зелёный, JS страниц — `node --check`.

**Порядок и оценка** (дни разработки, грубо). Этапы 1 и 2 можно вести параллельно в разных worktree; Этап 2 запускается как можно раньше — посуточным данным нужно 3–4 недели накопления.

| Этап | Зависит от | Оценка | Результат |
| --- | --- | --- | --- |
| 0. Гигиена данных | — | 3–4 | Чистый месячный индекс, complex\_id/hex\_id у аренды, отчёт до/после |
| 1. Доходность в карточке | 0 | 4–5 | Блок, фильтр, алерт, бэктест Deal Score |
| 2. Посуточный индекс | 0 | 4–5 + накопление | Daily-индекс, срок сдачи, мебель, вкладка «Аренда» |
| 3. Сканер субаренды | 2 | 5–7 | Расчёт, API, страница, алерт |

Открытые вопросы: через какой бот шлём алерты; актуальная ставка налога для сдачи в РК; какие части сканера платные на старте.