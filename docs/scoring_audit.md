# Аудит систем оценки (scoring audit)

Дата: 2026-08-14. Исследовательская задача — код не менялся, только чтение
кода/схемы/живой БД. Все цифры покрытия — живой снапшот `krisha_bot` (и
отдельной `hype_tracker`) на момент аудита, см. §6 за точные запросы.

Итог одной строкой: скоринга в проекте **не один**, а минимум **девять**
независимых, по-разному откалиброванных механизмов на **трёх БД**
(Postgres `krisha_bot`, Postgres `hype_tracker`, SQLite `bot.db`), с
разной степенью живости — от полностью рабочих до тихо осиротевших после
рефакторингов. Ядро (`score_total` вторички) спроектировано хорошо
(единый файл, настраиваемые веса, breakdown, confidence) — проблемы
сосредоточены на периферии: старые параллельные скоринги не удалены до
конца, часть колонок пишется в никуда, часть — не пишется никем уже
месяц.

---

## 1. Инвентарь

Легенда столбца «Живой»: 🟢 пересчитывается по расписанию/на лету и
реально влияет на что-то · 🟡 считается, но эффект нейтрализован/почти
не виден · 🔴 не считается (мертвый код) или пишется в колонку, которую
больше никто не читает/не обновляет.

### A. Скоринг объявлений (apartment_listings, вторичка)

| Механизм | Живой | Хранится | Считается в | Потребители |
|---|---|---|---|---|
| **Deal Score v4** (`score_total`, 0-100) | 🟢 | `apartment_listings.score_total`, `.deal_confidence`, `.hex_deal_index`, `.hex_details` (JSONB, полный breakdown) | [`bot/core/deal_score.py`](../bot/core/deal_score.py) `apply_deal_scores()`, вызывается каждый цикл парсера ([service_apartments.py](../service_apartments.py):834) | Сортировка на карте/в списках (`ORDER BY score_total`), фильтр по мин. скору, попап объявления (breakdown), Telegram-алерты, `/admin/api/map-points` |
| **Legacy-проекция** (`score_yield/price_market/location/apt_type/floor/complex/supply`) | 🟡 | те же 7 колонок `apartment_listings` | та же функция, просто проекция компонентов v4 (`_legacy_breakdown`) | старые потребители, которые ещё читают их напрямую (sheets_sync, часть аналитики) — `score_location` **всегда 0** (см. §5) |
| **Zone bonus** (ручные приоритетные зоны на карте) | 🟢 | `apartment_listings.zone_bonus`, `.zone_name` | [`bot/core/zones.py`](../bot/core/zones.py) `zone_bonus_for()`, пересчитывается в цикле парсера | добавляется ad hoc к `score_total` в SQL на выдаче (не хранится суммарно) |
| **Layer bonus** (OSM: шум/школы/транспорт/магазины/парки/банки-заглушка) | 🟢 | `apartment_listings.layer_bonus`, `.layer_details` (JSONB), `.layers_computed_at` | [`bot/score_layers/`](../bot/score_layers/__init__.py) `compute_all_layers()`, раз в 30 дней на объявление ([service_apartments.py](../service_apartments.py):586-616) | то же — добавляется ad hoc к `score_total` на выдаче |
| **Price-drop bonus** | 🟢 | `apartment_listings.price_drop_bonus` | инлайн в [service_apartments.py](../service_apartments.py):230-247, на каждое обнаруженное снижение цены | то же — ad hoc в SQL на выдаче |
| **Finish-level adj** (отделка → прямая правка `score_total`) | 🟡 живёт, но эффект стирается | `apartment_listings.finish_level`, инкремент в `score_total` | [`bot/core/listing_intel.py`](../bot/core/listing_intel.py) `detect_finish_level()`, применяется в [service_apartments.py](../service_apartments.py):344-357 | см. §5 — правка происходит РАНЬШЕ полного пересчёта Deal Score v4 в том же цикле и стирается им |
| **Finish-type classify** (ИИ-совместимая классификация отделки) | 🟢 | `apartment_listings.finish_type` | [`bot/core/finish_classify.py`](../bot/core/finish_classify.py) `apply_finish_classification()`, тумблер `AI_FINISH_CLASSIFY` | фильтр на `/admin` по отделке; на `score_total` НЕ влияет (в отличие от `finish_level` выше — две разные колонки, два разных детектора одного явления) |
| **Trust score** (доверие к продавцу) | 🟢 | `apartment_listings.trust_score` | парсер (см. [apartment_parser.py](../bot/core/apartment_parser.py)), правило: 1.0 Крыша-агент / 0.8 собственник / 0.6 обычный риелтор | показывается на карточке, не входит в `score_total` |
| **Bargain / торг** (аналоги, целевая цена) | 🟢 сам расчёт, 🔴 персист | live-объект `{target_price, discount_pct, recommendation, market_status}` | [`bot/core/bargain.py`](../bot/core/bargain.py) `get_comparables()`+`analyze_bargain()`, считается **на лету** при открытии попапа ([admin_web.py](../bot/admin_web.py):1450, [terminal_extras.py](../terminal_extras.py):5998) | попап объявления, Telegram-алерты (частично) |
| — колонки `bargain_target/bargain_discount_pct/bargain_rec` | 🔴 | `apartment_listings.*` | писались в [service_apartments.py](../service_apartments.py) из `score_data.bargain`, который парсер больше не заполняет | **осиротело ~2026-07-25**, см. §3/§5 |
| **Relevance score** (подбор под сохранённый поиск) | 🟢, но эфемерный | не хранится, считается на лету | [`bot/core/scorer.py`](../bot/core/scorer.py) `score()` | только `reasons` (топ-3 причины) идут в текст карточки Telegram ([bot/core/cards.py](../bot/core/cards.py)); сам total нигде не показывается |
| **Insights confidence-note** (🟢/🟡/🔴 достоверность оценки) | 🟢 | не хранится | [`bot/core/insights.py`](../bot/core/insights.py) `confidence_note()` | последняя строка каждого Telegram-алерта ([service_alerts.py](../service_alerts.py)) — см. §5, это ЧЕТВЁРТОЕ по счёту понятие «confidence» в проекте |

### B. Скоринг первички (market_type='primary')

| Механизм | Живой | Хранится | Считается в | Потребители |
|---|---|---|---|---|
| **Primary Score** (застройщик 30 + стадия 20 + дисконт к вторичке 30, нормировано до 100) | 🟢, но узкое место — 30 объявлений/цикл | `apartment_listings.primary_score_total`, `.primary_score_details` (JSONB); дублируется в `score_total` | [`bot/core/primary_score.py`](../bot/core/primary_score.py) `compute_primary_score()`, вызывается из [service_apartments.py](../service_apartments.py):536-583 | попап (breakdown), сортировка тем же `score_total`/`primary_score_total` (`terminal_extras.py`:4283-4326) |

### C. Скоринг ниши (гараж/кладовка/коммерция — отдельная SQLite БД)

| Механизм | Живой | Хранится | Считается в | Потребители |
|---|---|---|---|---|
| **Investment Score** (yield 25 + location 25 + supply-demand 20 + liquidity 15 + quality 15) | 🟢 | `investment_listings.score_total` (**SQLite**, `bot.db`, НЕ Postgres) | [`bot/core/investment_score.py`](../bot/core/investment_score.py) `compute_score()`, вызывается из [`bot/jobs/scheduler.py`](../bot/jobs/scheduler.py) | Telegram-алерты по гаражам/кладовкам (`MIN_ALERT_SCORE=65`) |

### D. Скоринг ЖК/локации

| Механизм | Живой | Хранится | Считается в | Потребители |
|---|---|---|---|---|
| **Локационный скор ЖК** (~12 факторов OSM+transport_hexes+demolition+возраст+берег) | 🟢, но требует живой Overpass | НЕ хранится — считается по запросу | [`bot/core/location_score.py`](../bot/core/location_score.py) `compute_complex_location_score()`, роут `/admin/api/complex/{id}/location-score` | карточка ЖК (`complex_detail.html`, блок «Что рядом») |
| **Housing Class Estimate** (текстовый лейбл эконом/комфорт/бизнес/премиум, эвристика) | 🔴 заморожено | `complexes.housing_class_estimate` | одноразовый backfill-скрипт, **не в репозитории** (коммит `0bb2479`, "backfilled" прямо в БД) | карточка ЖК (заголовок + строка «Класс жилья», с ⓘ) |
| **Housing Class Test** (0-100, admin-эксперимент: цена/м² + потолки + кол-во квартир + этажность + лифты) | 🟢, admin-only | `housing_class_test` (частично) + расчёт на лету | [`bot/core/housing_class_score.py`](../bot/core/housing_class_score.py) `compute_housing_class_scores()` | только `/admin/analytics/housing-class`, нигде публично |
| **Price heatmap** (сырые price/м² по гекс-сетке) | 🟢 | не хранится, live-запрос | роут геоаналитики, потребляет `apartment_listings.price/area` напрямую | `_geo_body.html` (`/admin/analytics/heatmaps`) |
| **Views heatmap / популярность** | 🟢 | `apartment_listings.views_count`, `.views_count_updated_at` | сборщик `krisha-viewcount.service` / [service_viewcount.py](../service_viewcount.py) | `views_analytics.html`, heatmap на карте, поле `views` в попапе — **в скор не входит нигде** (см. §5) |
| **Hype/новостной "хайп" (buzz по локациям)** | 🟢, отдельная БД | `hype_tracker.hype_locations.rating` + `hype_location_history` (**отдельная Postgres-БД**, не `krisha_bot`) | LLM (DeepSeek) через [`hype_tracker/news_analyze.py`](../hype_tracker/news_analyze.py), ежедневно (systemd-таймер) | `hype_analytics.html`/`_hype_body.html` — heatmap; `decayed_rating`(τ=48ч)/`velocity` считаются на лету в [terminal_extras.py](../terminal_extras.py):607-684 |

### E. Entity resolution — «уверенность совпадения» (не про привлекательность, про идентичность)

| Механизм | Живой | Хранится | Считается в | Потребители |
|---|---|---|---|---|
| **ЖК ↔ источник confidence** (0-1: имя/гео/застройщик/адрес/фаза/продукт) | 🟢 | `complex_source_links.confidence`, `complex_source_link_candidates.confidence` | [`bot/core/entity_resolution.py`](../bot/core/entity_resolution.py) `score_match()` | роутинг auto-match (≥0.8) / review-очередь (0.5-0.8) / отбрасывание (<0.5); `/admin/entity-ids` |
| **ЖК-дубли (транслит)** | 🟢 | `complex_duplicate_candidates.evidence` (geo_m/same_developer/address_match) | sweep-скрипт + `score_match()` | `/admin/entity-ids`, bulk-approve по `signal_count≥2` |
| **Юнит ↔ объявление confidence** (Фаза 2) | 🟢 | `unit_source_links.confidence`, `unit_duplicate_candidates.evidence` | `phase2_unit_match.py` | `/admin/entity-ids` (аккордеон по объявлению, см. недавнюю задачу), gold-labels в `unit_match_gold_labels` — накапливаются, но пока НЕ используются для перекалибровки (TODO в коде) |
| **House-resolution attribution** (объявление → конкретный дом зонтика: адрес/адрес+гео/токен/гео) | 🟢 | `apartment_listings.resolved_house_id`, `.house_attribution`, `.house_attribution_detail` | [`bot/core/house_resolution.py`](../bot/core/house_resolution.py) | страница дома (`complex_detail.html`, список объявлений), аналитика «дом неизвестен: N» |
| **Дубли объявлений** (одна и та же квартира, разные источники/повторные публикации) | 🟢 | `apartment_listings.is_duplicate`, `.duplicate_of` | [`bot/core/dedup.py`](../bot/core/dedup.py) / [`dedup_listings.py`](../bot/core/dedup_listings.py) | фильтруется из всех агрегатов (`COALESCE(is_duplicate,FALSE)=FALSE` — это условие встречается в **десятках** SQL-запросов по всему проекту) |

### Мёртвый/осиротевший код (детали в §5)

| Файл/функция | Статус |
|---|---|
| [`bot/core/apartment_score.py`](../bot/core/apartment_score.py) `compute_apartment_score()` | 🔴 не вызывается НИГДЕ (grep по всему репо — 0 живых вызовов) |
| `bot/core/apartment_score_v2.py.clean` | 🔴 не Python-модуль (двойное расширение) — исторический слепок, физически не импортируется |
| [`bot/core/hex_price.py`](../bot/core/hex_price.py) `apply_hex_prices()` | 🔴 не вызывается НИГДЕ; логика (гекс+кольцо+город) продублирована заново внутри `deal_score.py` вместо переиспользования |

---

## 2. Глубина: Локация → ЖК → Квартира

### 2.1 Локация

Локация в проекте существует **в двух совершенно разных воплощениях**,
которые легко перепутать, потому что оба называются «локация»:

**(а) Локационный скор ЖК** — публично видимая карточка на странице ЖК,
считается по запросу (`/admin/api/complex/{id}/location-score`, таймаут
90с — «не блокирует рендер страницы, ждёт медленный Overpass»):

| Фактор | Источник | Диапазон | Гранулярность |
|---|---|---|---|
| 🔇 Шум (магистрали) | OSM Overpass, `bot/score_layers/noise.py` | −6..0 | точка (координата ЖК) |
| 🏫 Школы/сады/вузы | OSM Overpass, `.../schools.py` | 0..+5 | точка, пешая доступность |
| 🚏 Остановки транспорта | OSM Overpass, `.../transit.py` | 0..+3 | точка |
| 🛒 Магазины/сервисы | OSM Overpass, `.../amenities.py` | 0..+4 | точка |
| 🌳 Парки/зелень | OSM Overpass, `.../parks.py` | 0..+2 | точка |
| 🚈 ЛРТ рядом | `transport_hexes` (уже посчитанная таблица, hype_tracker-независимая) | 0..+4 | гекс 100м |
| 🚗 Доступность на авто | `transport_hexes` (дороги+развязки) | 0..+2 | гекс 100м |
| 🔀 Маршрутная связность | `transport_hexes` (route_count) | 0..+2 | гекс 100м |
| 🏗 Возраст дома | `complexes.year_built` | 0..+2 | ЖК |
| 🚧 Снос по соседству | `demolition_houses` (утверждённый перечень 2026-2030) | −2..0 | точка, радиус 250м |
| 🌉 Берег Ишима | эвристика по `district` | **всегда 0** | район (информационно) |
| 🏦 Банки/ипотека | — | заглушка, не реализовано | — |

Итог = **простая сумма** adj по всем факторам (не взвешенная — веса
зашиты неявно в диапазоны каждого фактора, см. таблицу). Confidence =
доля факторов, реально посчитанных не по дефолту/ошибке. Источник
координат ЖК для этого расчёта — **AVG(lat,lon) по apartment_listings с
точным совпадением имени ЖК** (не `complexes.lat/lon`! см. §3 — это
отдельная проблема после house-resolution). Свежесть: полностью
пересчитывается на каждый клик, но зависит от живости внешнего Overpass
(по коду — «реально жив только 1 из 4 зеркал»); `transport_hexes`/
`demolition_houses` — отдельные таблицы, обновляемые своими скриптами
вне этого запроса.

**(б) `location`-компонент внутри Deal Score v4** (см. 2.3) — формула
`price_loc_score*0.7 + poi_score*0.3`, где `price_loc_score` — цена
гексагона относительно медианы города (не про удобства вообще, а про
цену), а `poi_score` — только school/kindergarten/university из
`city_poi` (OSM импорт), БЕЗ шума/транспорта/магазинов/парков, которые
есть в (а). Т.е. (а) и (б) используют **пересекающиеся, но разные**
наборы сигналов, разную гранулярность (гекс+POI vs OSM Overpass
«живьём»), разные единицы (0-100 нормированный скор vs адитивные
баллы) — и НЕ являются одним и тем же числом. Хуже: **вес (б) в
итоговом Deal Score = 0** (`SCORE_W_LOCATION=0` в `app_settings`,
осознанное решение заказчика «локация как фактор — не имеет смысла») —
локация продолжает считаться и лежать в `hex_details.components.location`
(JSONB), но НЕ участвует в `score_total` и **не показывается** в legacy-
колонке `score_location` (та жёстко зафиксирована в 0 в коде, см. §5).

### 2.2 ЖК

У `complexes` **нет единого агрегированного «скора ЖК»**. Вместо этого:
- `housing_class` — ручной категориальный лейбл (эконом/комфорт/бизнес/
  премиум), заполнен только у 310 из 2387 (13%);
- `housing_class_estimate` — замороженная (см. §3) эвристика по
  медианной цене/м² и высоте потолков, у 1068 из 2387 (45%);
- `avg_price_m2`/`avg_yield`/`listings_count` — не скоры, живая
  статистика, пересчитывается каждый цикл парсера;
- Локационный скор (2.1а) — не хранится, считается по клику, per-ЖК.

Локация входит в «ЖК» ровно через это — карточка ЖК просто показывает
локационный скор рядом с остальными атрибутами, никакой формулы
«скор ЖК = f(локация, класс, …)» в коде нет.

### 2.3 Квартира — Deal Score v4

Формула (`bot/core/deal_score.py`, единственный писатель `score_total`
для вторички):

```
Hedonic-ядро (ожидаемая цена/м²):
  1) если ≥3 объявлений того же ЖК/дома и той же комнатности в площади
     ±15% — используются ТОЛЬКО они (P_expected = медиана)
  2) иначе — блендинг гекс(w=1.0,мин.3) + кольцо(w=0.7,мин.5) + город(w=0.35)
  P_expected *= class_adj(класс ЖК) * ceiling_adj(высота потолков)
  DI = P_expected / P_фактическая

score_total = round(
    price_score   * W_PRICE(40%) +   -- из DI
    location_score* W_LOC(0%)    +   -- см. 2.1б, вес обнулён
    quality_score * W_QUALITY(20%) + -- класс ЖК ИЛИ перцентиль цены как прокси + год + рейтинг Крыши
    market_score  * W_MARKET(15%) +  -- yield + ликвидность (кол-во в ЖК + возраст объявления)
    risk_score    * W_RISK(5%)       -- штрафы: 1й этаж −40, последний −25, риелтор −30
)
```

Веса `W_*` читаются из `app_settings` (`SCORE_W_PRICE/LOCATION/QUALITY/
MARKET/RISK`, редактируются на `/admin/settings`) — **единственная**
часть всей системы, где веса реально настраиваются без деплоя. Внутри
каждого компонента — россыпь **захардкоженных** констант (`_CLASS_SCORE`,
`_CLASS_PRICE_ADJ`, `_CEILING_*`, `POI_WEIGHTS`, пороги risk/floor) — они
НЕ вынесены в `app_settings`, менять их — деплой кода.

`Confidence` (0-100) — доля компонентов, посчитанных на «настоящих»
данных (свой гекс не пуст, класс ЖК известен, год известен, есть
доходность, есть рейтинг Крыши) — используется как гейт: бейджи
недооценки/переоценки в UI скрываются при `deal_confidence < 50`.

**Наследование ЖК → квартира**: `housing_class` и `year_built` читаются
из `complexes` по **точному совпадению имени** (`lower(trim(complex_name))`),
БЕЗ учёта `resolved_house_id`/`parent_complex_id` — см. §3, это
единственное реальное место, где локация/класс ЖК физически «втекают» в
квартирный скор, и оно не в курсе house-resolution.

### 2.4 Единый ли источник истины / двойной счёт

**Нет, не единый.** Пять независимых мест с захардкоженными весами
(`apartment_score.py` — мёртв, но исторически был; `investment_score.py`;
`primary_score.py`; `housing_class_score.py`; `scorer.py`) плюс
`deal_score.py`, где хотя бы верхний уровень весов вынесен в
`app_settings`. Итоговый «эффективный скор» на выдаче — ещё и не просто
`score_total`, а **`score_total + zone_bonus + layer_bonus +
price_drop_bonus`**, эта сумма **пересчитывается заново в SQL в 4+
разных запросах** ([terminal_extras.py](../terminal_extras.py):3265,
3639-3641, 3948-3950, 4225, 4288, 4328) вместо одной общей функции/view —
риск, что где-то забудут один из бонусов (уже сейчас не все 4 запроса
складывают одинаковый набор — часть не учитывает `price_drop_bonus`).

Задокументированный **живой случай двойного счёта** (в комментариях
`deal_score.py`): `bargain.py` (аналоги для бейджа «торг») и hedonic-ядро
`deal_score.py` — это ДВЕ независимо написанные реализации «найти
аналоги и сравнить цену», которые из-за рассинхрона фильтра по площади
одновременно показывали на одной странице «Недооценено на 48%» и
«переоценена на 43%» для одного и того же объявления (кейс #1014506231
Landmark). Починено синхронизацией констант (`AREA_BAND_PCT`,
`MIN_BLDG=MIN_SAME_COMPLEX=3`) вручную в комментариях — не общим кодом.
Это структурный риск: константы могут снова разойтись при следующей
правке любого из двух файлов, ничего не свяжет их формально.

---

## 3. Свежесть после большой чистки базы

| Что почистили | Что стало протухшим |
|---|---|
| **Geo-карантин** (`migrations/045`, 1 ЖК + 23 homeportal-объекта на карантине сейчас) | Локационный скор ЖК (2.1а) и `complex_detail.html` **не читают** `complexes.lat/lon` вовсе — берут live `AVG(apartment_listings.lat,lon) WHERE complex_name=точное_имя`. Карантин НЕ фильтрует этот AVG (он не трогает `apartment_listings.lat/lon`), но и не обязан — просто эти два пути (карта = `complexes.lat/lon`, локационный скор = AVG вторички) физически МОГУТ показывать разные точки для одного ЖК. |
| **Расшивки/зонтики/дома** (`parent_complex_id`, `is_umbrella`) | Локационный скор ЖК для дома использует `AVG(apartment_listings.lat,lon) WHERE lower(complex_name)=lower(имя_дома)` — точное имя, БЕЗ `resolved_house_id`. Если объявления дома всё ещё называют его именем зонтика (типично для только что расшитых домов, house-resolution их находит по адресу/токену/гео, а не по имени) — эндпоинт вернёт `{"error":"no_coords"}`, локационный скор для такого дома не посчитается вообще. |
| **House-resolution** (`resolved_house_id`) | `deal_score.py` берёт `complexes.housing_class`/`year_built` по `complex_name` **точным текстом**, не через `resolved_house_id`. Для 1275 объявлений, живущих под именем зонтика, из которых **801 (63%) уже резолвлены к конкретному дому** — `quality`-компонент Deal Score всё равно смотрит на характеристики ЗОНТИКА, а не дома, к которому объявление реально привязано. Тихая неточность, не крэш. |
| **Адресный бэкфил ЖК** (`complex_photos_address_backfill.py`) | Не влияет на скоринг напрямую (адрес не входит в формулы) — влияет только на `bargain.py`'s `district_fallback`/`address_match` сигнал в ER, косвенно. |
| **Рефакторинг Deal Score v2→v4** (сам коммит убрал параллельную систему `apartment_score_v2`) | Побочный эффект: `apartment_parser.py` перестал заполнять `score_data` (в т.ч. `score_data.bargain`) при парсинге. `bargain_target/bargain_discount_pct/bargain_rec` — **последняя строка с непустым значением датируется 2026-07-25**, у всех 32992 объявлений с `first_seen` позже — `NULL`. Живой (актуальный) торг по-прежнему считается на лету в попапе — просто эти 3 колонки больше никто не обновляет, и любой код/аналитика, читающие их напрямую из БД (а не через попап), видят замороженный срез трёхнедельной давности. |
| **Разовый backfill `housing_class_estimate`** (коммит `0bb2479`, 1 августа) | Никогда не пересчитывался — не привязан ни к одному циклу/скрипту в репозитории вообще (миграции нет, скрипта нет). Для 1068 ЖК число полностью заморожено на состояние `avg_price_m2`/потолков на 1 августа, не отражает ни один смёрдж/сплит/новые объявления с тех пор. |
| **hex_price.py → deal_score.py** (переход на v4) | `hex_price_adj` теперь **всегда 0** (deal_score v4 пишет константу, старая инкрементальная логика адаптации больше не используется) — колонка существует, заполняется, но с 2026-08-13 не несёт информации. |

**Что НЕ протухло** (пересчитывается штатно): `score_total`/`deal_confidence`/
`hex_deal_index`/`hex_details` (каждый цикл парсера, ~87% активной
вторички имеет свежий `deal_confidence`); `zone_bonus`/`layer_bonus`/
`price_drop_bonus`; `trust_score`; `views_count`; ER-спайн
(`complex_source_links`) и review-очереди; `hype_locations` (последняя
запись — сегодня).

---

## 4. Использование и прозрачность

**Публичный (анонимный) уровень доступа** — согласно
[`bot/core/site_auth.py`](../bot/core/site_auth.py) `get_user_tier()`:
видит только тепловые карты (агрегат, без breakdown по объекту) и
каталог новостроек (карточки — фото/цена/метраж, **без какого-либо
скора**). Ни один из скорингов из §1 напрямую пользователю-анониму не
показывается.

**Subscriber (Telegram-логин + ручной full_access) и Admin** — видят
одинаковый попап объявления (роуты в [`admin_web.py`](../bot/admin_web.py)
и [`terminal_extras.py`](../terminal_extras.py)), в нём:
- Deal Score v4: `hex_details.components.{price,location,quality,market,
  risk}` — у каждого числовой `score` + человекочитаемый `text`
  («на 27% дешевле локального ожидания (с поправкой: класс «элит»
  +25%)») — **хорошо объяснимо**;
- `layer_details` — по каждому OSM-слою `{adj, reason}`;
- `primary_score_details` — для новостройки: developer/stage/discount
  с `reason` у каждого;
- Bargain-анализ (аналоги, метод поиска, `market_status`) +
  `build_negotiation_points`/`build_seller_questions` (текстовые
  подсказки) — тоже с прозрачными причинами.

**Telegram-алерты** ([`service_alerts.py`](../service_alerts.py)) — другой
канал, другой набор чисел: показывает `bargain_rec` (текст) +
`build_insights_block` (ипотека/доходность/дни на рынке/красные флаги) +
**`confidence_note`** (🟢/🟡/🔴) в конце — это НЕ `deal_confidence`, а
отдельная 0-4-балльная шкала по полноте аренды/деталей/года постройки
(см. §5, четыре несовместимых понятия «confidence»).

**Карточка ЖК** (`complex_detail.html`) — публичная страница:
- Локационный скор — чипы факторов с `adj`+`reason` в `title`-тултипе —
  прозрачно, но требует живого JS-запроса (не в исходном HTML);
- `housing_class_estimate` — показан с ⓘ-тултипом, объясняющим методику
  («по медианной высоте потолков и цене/м²… не официальная
  классификация») — методика объяснена, но актуальность числа — нет
  (см. §3, заморожено);
- Динамика цены/скорость продаж («темп продаж», ближайший аналог
  «market absorption» в проекте) — считается живым SQL по клику
  (`/admin/api/complex/{id}/price-dynamics`, `/turnover-dynamics`), не
  скор, а агрегат, но по духу и месту в UI — тот же жанр «показать
  здоровье рынка».

**Admin-only, без объяснимости для не-админа**:
`/admin/analytics/housing-class` (Housing Class Test, 0-100) — есть
breakdown (`score_details` с нормированным значением+весом на метрику),
но не показывается никому кроме админа; `/admin/entity-ids` — ER
confidence с компактным `match_method` («name_fuzzy(0.82)+geo+developer»)
и явным evidence JSON (geo_m, same_developer, address_match) —
технически прозрачно, но язык рассчитан на оператора, не на
пользователя (и не должен — это внутренний QA-инструмент).

---

## 5. Качество данных

### 5.1 Покрытие (снапшот на момент аудита)

| Метрика | Значение |
|---|---|
| `apartment_listings` всего / активных | 46996 / 43737 |
| вторичка активная / первичка активная | 41980 / 1757 |
| вторичка с посчитанным `deal_confidence` | 36434 / 41980 (**87%**) |
| вторичка с `score_total > 0` | 38877 / 41980 (**93%**) |
| первичка с `primary_score_total` | 1548 / 1757 (**88%**) |
| `views_count` заполнен | 17000 / 46996 (**36%**), свежак — сегодня |
| `price_drop_bonus > 0` | 756 / 46996 (**1.6%**) |
| `bargain_target` заполнен | 14004 / 46996, но **0 из объявлений с `first_seen` после 2026-07-25** |
| `complexes` всего / с координатами | 2387 / 2305 (**97%**) |
| `complexes.housing_class` (ручной) | 310 / 2387 (**13%**) |
| `complexes.housing_class_estimate` (заморожен) | 1068 / 2387 (**45%**) |
| `complexes` в геокарантине | 1 (+ 23 homeportal-объекта) |
| ЖК-дома (`parent_complex_id`) / зонтики (`is_umbrella`) | 33 / 8 |
| ER-спайн (`complex_source_links`) | 1962 связей |
| ER review-очередь / conflicts / отклонения | 8 / 0 / 15 |
| Юнит-дубли в review | 30 (10 объявлений после группировки) |
| Объявления под именем зонтика / резолвлены к дому | 1275 / 801 (**63%**) |

### 5.2 Мёртвые/полу-мёртвые компоненты

- `bot/core/apartment_score.py` (`compute_apartment_score`) — **0 живых
  вызовов** во всём репозитории. Хардкод районов Астаны
  (Есиль/Алматы/Сарыарка/Байконур/Нура) и POI-ключевых слов — исторический
  артефакт, замещён Deal Score v4 полностью.
- `apartment_score_v2.py.clean` — файл с двойным расширением, физически
  не импортируется Python'ом; чистый архив «на всякий случай».
- `bot/core/hex_price.py.apply_hex_prices()` — определена, **никогда не
  вызывается**; сама гекс-модель (веса 1.0/0.7/0.35, MIN_HEX=3,
  MIN_RING=5) была **скопирована заново**, а не переиспользована, внутри
  `deal_score.py` — то есть логика не удалена, а продублирована в другом
  файле и один из двух экземпляров осиротел.
- **`score_location` (legacy-колонка) — всегда 0**, независимо от
  реального `loc_score`, потому что вес локации в total = 0 (осознанное
  решение) и код явно пишет константу вместо реального значения
  (`"location": 0,  # вес обнулён … не показываем`). Название колонки
  вводит в заблуждение любого, кто читает её напрямую из БД, не читая
  комментарий в коде.
- **`hex_price_adj` — всегда 0** с момента перехода на v4 (см. §3).
- **`finish_level`-инкремент score_total — живой код, мёртвый эффект.**
  `service_apartments.py` в одном и том же цикле (1) применяет
  инкремент `score_total += finish_adj` при смене `finish_level`, затем
  (2) чуть позже вызывает `apply_deal_scores()`, который делает
  `score_total = deal` (полная перезапись, БЕЗ учёта finish). Формула
  Deal Score v4 нигде не читает `finish_level`/`finish_type`. Итог:
  правка исполняется, но в подавляющем большинстве циклов немедленно
  затирается — «отделка» фактически не влияет на `score_total`, хотя
  докстринг `bot/score_layers/__init__.py` до сих пор обещает
  «finish — отделка, правит score_total напрямую −5..+6».
- **`bargain_target/discount_pct/rec` — колонки без живого писателя**
  (см. §3) — читаются `service_alerts.py` для Telegram-текста, то есть
  Telegram-алерты по объявлениям младше 3 недель **не покажут** строку
  «🤝 Торг: …» вообще, хотя тот же торг прекрасно считается и
  показывается в веб-попапе того же объявления.
- **`hype_locations`/`hype_location_history`** — не мертво (см. §1), но
  живёт в **отдельной БД** (`hype_tracker`, не `krisha_bot`), без единой
  схемы в `migrations/` вообще (создана вручную на проде, в репозитории
  нет ни одного `CREATE TABLE` для неё) — воспроизвести окружение с нуля
  по репозиторию невозможно, надо знать про эту БД отдельно.

### 5.3 Хардкод весов (нет реестра)

Единственное место, где веса скоринга — данные, а не код: 5 верхнеуровневых
весов Deal Score v4 в `app_settings` (редактируются на `/admin/settings`).
Всё остальное — константы в теле модулей: `apartment_score.py` (мёртв,
но был примером), `investment_score.py`, `primary_score.py`,
`housing_class_score.py`, `scorer.py`, плюс **вложенные** константы
внутри самого `deal_score.py` (`_CLASS_SCORE`, `_CLASS_PRICE_ADJ`,
`_CEILING_*`, `POI_WEIGHTS`, штрафы risk/floor) и `location_score.py`
(диапазоны adj по каждому фактору). Смена любого из них — деплой кода,
не клик в админке.

### 5.4 Недостающие сигналы (посчитаны, но не используются в скоре)

- **`views_count`** (популярность/просмотры Крыши) — собирается,
  показывается, но **не входит ни в один скор**. Явный кандидат: рост
  просмотров без роста цены — сигнал горячего спроса, ровно то, что Deal
  Score пытается уловить через `market`-компонент косвенно (supply/age),
  но не напрямую.
- **`hype_locations.rating`/`decayed_rating`** (новостной хайп по
  локации/ЖК) — существует, свежий, но живёт отдельным слоем карты,
  **никак не подмешивается** в `quality`/`market` компоненты Deal Score
  для объявлений в этой локации.
- **`trust_score`** — считается и хранится, но **не входит в
  `score_total`** ни одним компонентом (только показывается отдельно на
  карточке).
- **`unit_match_gold_labels`** (журнал решений оператора по юнит-дублям)
  — накапливается с явной целью «будущая перекалибровка» (докстринг
  модуля), но перекалибровки пока нет — это чистый лог, не влияющий
  сейчас ни на что автоматически.

---

## 6. Примеры из БД

### 6.1 Объявление — полный breakdown (id 1014731478, «На 188-Ой Улице»)

Сырые сигналы (из `apartment_listings`): цена 18 500 000 ₸, площадь 36 м²,
1-комн., Сарыарка р-н, `market_type='secondary'`, ЖК «На 188-Ой Улице»
(class=«элит», `year_built=2026`), в ЖК всего 44 объявления.

```
score_total       = 95      deal_confidence = 90
hex_deal_index    = 1.268   (DI: ожидаемая цена/м² на 26.8% выше фактической)
edge_m            = 100     сегмент = "1-комн"   sources = "тот же дом/ЖК"
                              (в ЖК ≥3 своих объявления той же площади →
                               используются они, а не гекс/кольцо/город)
actual_m2         = 513 889 ₸/м²    expected_m2 = 651 538 ₸/м²

components:
  price     score=100  weight=0.50 (нормировано с 40%, т.к. W_LOC=0)
            "на 27% дешевле локального ожидания
             (с поправкой: класс «элит» +25%)"
  quality   score=100  weight=0.25
            "класс «элит», 2026 г."
  market    score=74   weight=0.1875
            "yield 12.4%, в ЖК 44 объявл."
  risk      score=100  weight=0.0625
            "флагов нет"
  location  score=46   weight=0.0   ← посчитан, но вклад в total = 0
            "локация дешевле городской медианы на 15%,
             рядом 8 объектов инфраструктуры"

legacy-проекция: yield=17 price_market=15 location=0 apt_type=15
                 floor=8 complex=15 supply=2
zone_bonus=0  layer_bonus=+5  price_drop_bonus=0  →  эффективный скор
                 на выдаче = 95+0+5+0 = 100
```

Путь: сырые цена/площадь/координаты → гекс-агрегация (own_bldg, т.к.
своих объявлений ЖК хватило) → DI → price_score (40% веса после
перенормировки) + quality/market/risk (класс ЖК из `complexes` по имени,
yield из `rental_index`, штрафы этажа) → взвешенная сумма → `score_total`
→ на выдаче доп. `+layer_bonus` (OSM-слои посчитаны отдельно, по
координате, независимо от Deal Score).

### 6.2 ЖК — локационный скор (id 2361, «На 188-Ой Улице», lat 51.1716 lon 71.3838)

Живой прогон в среде аудита не смог достучаться до OSM Overpass (нет
исходящего интернета в песочнице) — ниже часть факторов, не зависящих
от Overpass, посчитана по-настоящему на живых данных `transport_hexes`/
`demolition_houses`:

```
🚈 ЛРТ рядом:            adj=0  "ЛРТ дальше 1км"
🚗 Доступность на авто:  adj=1  "рядом крупная дорога"
🔀 Маршрутная связность: adj=0  "маршрутов рядом нет"
🚧 Снос по соседству:    adj=0  "рядом нет объектов из перечня на снос"
🏗 Возраст дома:         adj=2  "новостройка 2026 г. — современные
                                  планировки/коммуникации"
🌉 Берег Ишима:          adj=0  "левый берег Ишима (р-н Есиль)"
                                  (информационно, не влияет на итог)

+ 5 факторов через OSM Overpass (шум/школы/транспорт-остановки/
  магазины/парки) — требуют живого внешнего запроса, недоступны в
  этой среде; диапазоны см. таблицу §2.1.
```

### 6.3 Пайплайн целиком (текстовая диаграмма)

```
СЫРЬЁ
 ├─ Krisha (парсер) ──────────► apartment_listings (цена/площадь/фото/…)
 ├─ rental_listings/rental_index ─► est_rent, yield_pct
 ├─ OSM Overpass (live) ──────► bot/score_layers/{noise,schools,transit,amenities,parks}
 ├─ city_poi (OSM импорт) ────► POI для Deal Score location-компонента
 ├─ transport_hexes (батч) ───► ЛРТ/дороги/маршруты по гексагону
 ├─ demolition_houses (реестр)─► снос по соседству
 ├─ Korter/Homsters/Homeportal ► complexes.source_info, housing_class, hp-данные
 ├─ Крыша view-counter ───────► views_count
 └─ RSS-новости → DeepSeek ───► hype_tracker.hype_locations.rating
                                      │
                                      ▼
СИГНАЛЫ (пер-объявление / пер-ЖК)
 ├─ finish_level/finish_type (регэксп по тексту, 2 независимых детектора)
 ├─ trust_score (правило по seller_type/is_owner)
 ├─ is_duplicate (dedup.py, похожесть объявлений)
 ├─ resolved_house_id (house_resolution.py: адрес/адрес+гео/токен/гео)
 ├─ zone_bonus (ручные зоны на карте)
 └─ complex_source_links.confidence / unit_source_links.confidence
    (score_match(): имя+гео+застройщик+адрес+фаза/продукт-токен)
                                      │
                                      ▼
СКОРЫ
 ├─ Deal Score v4 (deal_score.py)      → apartment_listings.score_total,
 │    price+location(w=0)+quality+market+risk    .deal_confidence, .hex_details
 ├─ Primary Score (primary_score.py)   → .primary_score_total (только market_type='primary')
 ├─ Investment Score (investment_score.py) → investment_listings.score_total (SQLite, гараж/кладовка)
 ├─ Локационный скор ЖК (location_score.py) → НЕ хранится, live per-request
 ├─ Housing Class Test (housing_class_score.py) → admin-only 0-100
 ├─ Bargain (bargain.py)               → НЕ хранится, live per-request (попап)
 ├─ Relevance score (scorer.py)        → эфемерный, только reasons в Telegram-карточку
 ├─ hype rating → decayed_rating/velocity (terminal_extras.py, на лету)
 └─ layer_bonus/price_drop_bonus       → отдельные колонки, складываются в SQL на выдаче
                                      │
                                      ▼
ПОТРЕБИТЕЛИ
 ├─ Ранжирование: ORDER BY score_total+zone_bonus+layer_bonus+price_drop_bonus
 │    (4+ мест в terminal_extras.py, формула не централизована)
 ├─ UI попап объявления (admin/subscriber): полный breakdown всех компонентов
 ├─ Карточка ЖК (публично): локационный скор, housing_class_estimate, темп продаж
 ├─ Тепловые карты (публично): цена/м², просмотры, хайп — без breakdown
 ├─ Telegram-алерты: bargain_rec + insights (confidence_note ≠ deal_confidence!)
 ├─ /admin/entity-ids: ER confidence → auto-match / review-queue / отбрасывание
 └─ Внутренние гейты: deal_confidence<50 скрывает бейдж недооценки;
      AUTO_MATCH_THRESHOLD=0.8 / REVIEW_QUEUE_THRESHOLD=0.5 роутят ER-кандидатов
```

---

## 7. Выводы

### Высокий приоритет

1. **`finish_level`-инкремент `score_total` не имеет эффекта** — стирается
   последующим `apply_deal_scores()` в том же цикле парсера. Либо
   встроить `finish_level` компонентом в Deal Score v4 (как `quality`
   или отдельный небольшой вес), либо убрать инкремент и обновить
   докстринг `bot/score_layers/__init__.py`, который до сих пор обещает
   обратное. Сейчас код лжёт сам себе.
2. **`bargain_target/discount_pct/rec` не пишутся с 2026-07-25** —
   `service_alerts.py` тихо теряет строку «🤝 Торг» для всех новых
   объявлений в Telegram, хотя те же данные прекрасно считаются в
   веб-попапе. Нужно либо перекинуть запись из `service_apartments.py`
   на реальный вызов `get_comparables`/`analyze_bargain`, либо убрать
   мёртвые колонки и читать бэкграунд-алертами напрямую из тех же
   функций, что и попап (единая логика вместо двух).
3. **Локационный скор ЖК/страница ЖК не в курсе house-resolution** —
   для домов под зонтиком координаты и quality-компонент считаются по
   точному текстовому имени, игнорируя `resolved_house_id`, из-за чего
   63%-покрытая house-resolution система не долетает до двух реально
   видимых пользователю мест (карточка ЖК → локационный скор; Deal
   Score → quality-компонент квартиры). Синхронизировать с уже
   существующим паттерном `_listing_id_match` (имя ИЛИ
   `resolved_house_id`), который уже используется в `/complex/{id}`.

### Средний приоритет

4. **Дублирующаяся hedonic-логика** (`bargain.py` ↔ `deal_score.py`) —
   исторически уже давала противоречивые бейджи на одной странице,
   почищено синхронизацией магических констант вручную. Стоит вынести
   общее ядро «найти аналоги по гекс+кольцо+ЖК+площадь» в одну функцию,
   которую оба потребляют — иначе следующая правка одного из файлов
   рискует снова разойтись.
5. **`hex_price.py` — мёртвый код с продублированной логикой** внутри
   `deal_score.py`. Удалить файл или явно пометить архивным (по аналогии
   с `apartment_score_v2.py.clean`), чтобы не вводить в заблуждение при
   следующем аудите/найме.
6. **`apartment_score.py` и `apartment_score_v2.py.clean`** — оба
   физически мертвы, стоит удалить или переместить в `docs/archive/` с
   пояснением, зачем оставлены (если есть причина держать историю).
7. **`housing_class_estimate` заморожен без единого писателя в
   репозитории** — либо завести регулярный пересчёт (по аналогии с
   `avg_price_m2`), либо явно пометить в UI «оценка на 2026-08-01»
   вместо текущего нейтрального ⓘ, который читается как «оценка
   актуальна».
8. **Четыре разных «confidence»** в проекте (`deal_confidence` 0-100,
   ER `score_match` 0-1, `location_score` confidence 0-100,
   `insights.confidence_note` 🟢/🟡/🔴) — не обязательно объединять (у
   них разный домен), но стоит явно задокументировать в одном месте
   (например здесь) и, возможно, переименовать один из них — сейчас
   одно и то же слово в разных частях UI/кода означает четыре разные
   вещи, что усложняет онбординг и будущий аудит.
9. **Формула «эффективного скора для сортировки»
   (`score_total+zone_bonus+layer_bonus+price_drop_bonus`) продублирована
   в 4+ SQL-запросах** вместо одного SQL-выражения/generated column/view.
   Уже сейчас не все 4 копии идентичны по набору слагаемых — риск
   тихого рассинхрона сортировки между разными страницами продукта.

### Низкий приоритет / на будущее

10. **`views_count` и `hype_locations.rating` не используются в
    скоринге** — оба сигнала живые и покрыты (просмотры — 36%, хайп —
    ежедневно свежий), но существуют только как отдельные слои карты.
    Кандидаты на интеграцию в `market`/`quality`-компонент Deal Score,
    если появится время на эксперимент + оценку эффекта.
11. **`unit_match_gold_labels` копится без потребителя** — задокументи-
    рованное намерение «перекалибровка», но пока просто растущий лог.
    Не критично, но стоит либо запланировать разбор, либо явно
    зафиксировать, что это долгосрочный архив без ближайшего плана.
12. **`hype_tracker` — отдельная БД без схемы в `migrations/`** —
    архитектурно, возможно, оправдано (изоляция экспериментального
    news-пайплайна), но воспроизвести окружение с нуля по репозиторию
    сейчас нельзя: ни одной `CREATE TABLE` для `hype_locations`/
    `hype_location_history`/`hype_resources`/`hype_snapshots` нет в
    `migrations/`. Стоит хотя бы задокументировать схему отдельным
    файлом, даже если not migrating её в общий раннер.
13. **Reasons-based интерфейсы (`scorer.py`, `apartment_score.py`,
    `investment_score.py`) не унифицированы по формату** — каждый
    возвращает свой список строк/структуру `breakdown`, что не даёт
    переиспользовать один рендерер UI для всех скоров разом (сейчас
    попап знает конкретно про `hex_details`/`primary_score_details`/
    `layer_details` — три разных JSON-формы одного и того же понятия
    «breakdown»).
