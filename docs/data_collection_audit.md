# Аудит сбора данных: инвентарь пайплайнов и темпоральная целостность

Дата: 2026-08-14. Исследовательская задача — код не менялся, только чтение
кода/схемы/`systemctl`. Независима от блока по скорингу
([scoring_roadmap.md](scoring_roadmap.md)), но гэпы ниже стоит заносить в
тот же `decisions`-лог (п.15 того блока) — единая дисциплина для решений
по данным, не только по скору.

Итог одной строкой: коллекторов **более 25**, планировщик — почти
исключительно `systemd` timers/daemons (не cron, не APScheduler), политес
(паузы между запросами) соблюдается почти everywhere явным `sleep`/rate-
limit в коде. Темпорально проект — **гибрид**: у денег (цена продажи и
аренды) образцовая append-only история; у почти всего остального
(просмотры, агрегаты ЖК, статусы, описания, фото, официальные данные,
программы застройщиков, ссылки между сущностями) — overwrite-in-place без
истории. `raw`-хранилище — исключение (только прямые импорты
застройщиков), не правило.

---

## Методология

- Инвентарь systemd-юнитов — `systemctl list-timers`/`list-units` +
  `systemctl cat` (ExecStart/OnCalendar/OnUnitActiveSec) — не полагался на
  докстринги скриптов, они иногда расходятся с реальным расписанием
  (живой пример ниже — `krisha-complex-scan` был написан, но не стоял на
  таймере до отдельной жалобы, см. его же докстринг).
- Схема — `\d <table>` на живой БД (не только `migrations/*.sql`: часть
  таблиц создана вручную/через `CREATE TABLE IF NOT EXISTS` внутри самих
  скриптов сбора, а не в `migrations/`, см. гэп Г11 ниже).
- «Append-only» здесь = отдельная таблица-журнал (или `INSERT`, где
  естественный ключ гарантирует уникальность момента, напр.
  `UNIQUE(station, ts)`), где старое значение физически не удаляется и не
  правится. «Overwrite» = `UPDATE`/`UPSERT` одной строки на сущность —
  прошлое значение теряется безвозвратно в момент записи нового.

---

## §1. Инвентарь пайплайнов сбора

| Источник | Скрипт | Триггер / частота | Вежливость | Пишет в | raw+normalized? |
|---|---|---|---|---|---|
| **Krisha — объявления (поиск+деталка)** | [`service_apartments.py`](../service_apartments.py) → `bot/core/apartment_parser.py`+`apartment_details.py` | `krisha-apartments.service`, вечный цикл, случайные 50-80 мин между циклами (джиттер) | Пагинация поиска без явной паузы (httpx, последовательно); деталки — по мере надобности, не на каждый листинг | `apartment_listings` (десятки полей), `price_history` (событие), `newbuild_units`(косвенно нет) | Только normalized — HTML не сохраняется |
| **Krisha — просмотры объявлений** | [`service_viewcount.py`](../service_viewcount.py) (Playwright, headless Chromium) | `krisha-viewcount.service`, вечный цикл, малые батчи, специально медленно (докстринг: «более заметный паттерн трафика») | Явно щадящий батч-режим, headless-браузер сам по себе дороже httpx | `apartment_listings.views_count`, `.views_count_updated_at` | Только normalized, только ТЕКУЩЕЕ число — истории нет (см. Г2) |
| **Krisha — аренда** | [`service_rental.py`](../service_rental.py) → `bot/core/rental_parser.py` | `krisha-rental.service`, вечный цикл, 5-15 мин, 1 страница/цикл (`MAX_PAGES_PER_TYPE=5`) | Постранично, по одной странице за проход — сознательно медленно | `rental_listings`, `rental_price_history` (событие), `rental_index` (агрегат) | Только normalized |
| **Krisha — комплексы (кол-во квартир+описание+фото)** | [`hype_tracker/krisha_complex_scan.py`](../hype_tracker/krisha_complex_scan.py) | `krisha-complex-scan.timer`, каждые 20 мин (было написано, но **не стояло на таймере** до жалобы — см. докстринг скрипта, живой пример гэпа расписания, не темпоральной модели) | `parse_settings`-лимит (по умолчанию 10 ЖК/20 мин) | `housing_class_test.apartment_count`, `complexes.description` (только если пусто), `complexes.photos`/`photos_source` (только если источник не приоритетнее) | Только normalized |
| **Krisha — планировки (детекция на фото)** | [`floorplan_scan.py`](../floorplan_scan.py) (OpenCV+SigLIP, локально — не сеть к Крыше, сеть только для скачивания уже известных фото) | `krisha-floorplan.timer`, каждые 20 мин, `--limit 200` | Не применимо (свои же URL фото, не новый контент с Крыши) | `listing_floorplans.checked_at`, флаг плана на фото | Кэш фото на диске (`static/cache/photos`) — единственный «raw»-кэш вне newbuild-импортёров |
| **Homeportal.kz (официальные данные КЖК)** | [`hype_tracker/homeportal_scan.py`](../hype_tracker/homeportal_scan.py) | `krisha-homeportal.timer`, ежедневно 07:00 | Пауза 1с между деталками (~600 объектов) | `homeportal_objects` (~35 полей, все `text`), `complex_source_links`/`complex_source_link_candidates` (через `record_source_link()`) | Только normalized |
| **Korter.kz (класс жилья, застройщик)** | [`service_korter.py`](../service_korter.py) | `krisha-korter.service`, вечный цикл, раз/сутки ±2ч джиттер | ~9 запросов/прогон, паузы, весь обход <1 мин | `complexes.housing_class`/`.source_info->korter`, `source_runs`+`source_changes` (событие!) | Только normalized |
| **Homsters.kz (застройщик, цена, площадь)** | [`service_homsters.py`](../service_homsters.py) | `krisha-homsters.service`, вечный цикл, раз/сутки ±2ч джиттер | 15 стр. ЖК + 253 карточки застройщиков, паузы 3-5с, ~45-50 мин полный обход | `complexes.source_info->homsters`, `developers`, `source_runs`+`source_changes` (событие) | Только normalized |
| **Застройщики напрямую (шахматки)** | [`newbuild_weekly.py`](../newbuild_weekly.py) → `bi_group_import.py`/`sensata_import.py`/`orda_invest_import.py`/`bazis_import.py`/`nak_import.py` (через `newbuild_common.py`) | `krisha-newbuild.timer`, еженедельно пн 06:00 (+30 мин джиттер) | По одному сайту за раз, весь обход раз/неделю — сама редкость и есть вежливость | `newbuild_units` (+`newbuild_unit_price_history` — событие!), `complexes` (is_newbuild, completion_year/quarter) | **Единственный пайплайн с raw**: `newbuild_units.raw_json` хранит сырой ответ источника |
| **Программы покупки застройщиков** | [`developer_programs_check.py`](../developer_programs_check.py) | `krisha-programs.timer`, еженедельно вт 07:30 | Пауза ≥1с между сайтами | `developer_programs` (UPSERT по `developer_id+title`) | Только normalized |
| **OSM (шум/школы/транспорт/магазины/парки)** | [`bot/score_layers/`](../bot/score_layers/) (`osm.py`/`poi.py` — вызывается по требованию, не отдельный демон) + разовые импорты `city_poi`/`city_roads` | По требованию: при парсинге объявления (`compute_all_layers`, раз/30 дней на объявление, см. `service_apartments.py`) и при live-запросе локационного скора ЖК; справочники (`city_poi`) — разовый импорт, без расписания | Overpass — несколько зеркал (докстринг: «реально жив только 1 из 4»), `osm_cache` — кэш по сетке ~110м | `osm_cache`, `city_poi`, `city_roads`, `apartment_listings.layer_bonus/.layer_details/.layers_computed_at` | Кэш (`osm_cache.payload` JSONB) — по сути raw ответ Overpass, с TTL по `fetched_at` |
| **transport_hexes** | [`hype_tracker/transport_hexes.py`](../hype_tracker/transport_hexes.py) | **Без таймера** — ручной запуск (не найден в `systemctl list-timers`) | N/A (batch по гексагонам, локально считает из уже собранного) | `transport_hexes` (per-hex, перезаписывается целиком при перезапуске — не найдено признаков инкрементальности) | Только normalized |
| **demolition_houses (реестр под снос)** | `demolition_seed.py`/`demolition_geocode*.py` | **Без таймера** — разовый ручной сид + геокодинг | N/A (Nominatim — 1 запрос/сек, если используется) | `demolition_houses` | Только normalized |
| **Новости (RSS) + хайп-анализ (LLM)** | [`news_collect.py`](../news_collect.py) (RSS) + [`hype_tracker/news_analyze.py`](../hype_tracker/news_analyze.py) (DeepSeek) | `krisha-hype-news.timer`, ежедневно 06:30 | RSS — без явного троттлинга (немного источников); DeepSeek — по 1 запросу/статья, 10 последних дней непройденных | `news` (krisha_bot, INSERT ONLY — UNIQUE(url)), `hype_tracker.processed_articles`, `hype_tracker.hype_locations`+`hype_location_history` (СОБЫТИЕ) | Только normalized (LLM-вывод, не сырой текст статьи сверх `summary`) |
| **Хайп — ручная аннотация** | [`hype_tracker/hype_annotate.py`](../hype_tracker/hype_annotate.py) + `location_upsert.py` | По требованию (CLI, человек читает снимок и вбивает рейтинги) | N/A | `hype_tracker.hype_locations`+`hype_location_history` (тот же апсерт, что автомат) | N/A (ручной ввод) |
| **Преступность (КПСиСУ)** | [`crime_collect.py`](../crime_collect.py) | `krisha-crime.timer`, ежедневно 06:30 | Пауза 1.2с/запрос, инкремент по дате (или `--full`/`--since`) | `crime_incidents` (INSERT, `UNIQUE(objectid)`+`UNIQUE(lat,lon,date,title)` — де-факто append-only, записи неизменны по природе источника) | Только normalized |
| **Качество воздуха — станции ПНЗ (почасовые)** | [`pnz_collect.py`](../pnz_collect.py) (Playwright, ecodata.kz) | `krisha-air-stations.timer`, ежечасно | N/A (1 страница/запуск) | `air_stations` (`UNIQUE(station_name, ts)` — append-only по конструкции) | Только normalized |
| **Качество воздуха — официальные месячные (Казгидромет)** | [`air_collect.py`](../air_collect.py) | `krisha-air.timer`, еженедельно ср 07:30 | Явное правило «≥1с между запросами» в докстринге | `air_quality_astana` (`UNIQUE(version, pollutant)` — append-only по версии сводки) | Только normalized |
| **Качество воздуха — сетка CAMS** | [`air_grid_collect.py`](../air_grid_collect.py) | `krisha-airgrid.timer` — **disabled/inactive** (по факту НЕ собирается) | 1 запрос/прогон (мульти-точка) | `air_grid` (**без UNIQUE-ограничения** — см. Г9) | Только normalized |
| **Макро-данные рынка (НБРК/KDIF/Отбасы/stat.gov.kz)** | [`service_market_data.py`](../service_market_data.py) → `bot/core/market_data.py` | `krisha-market.service`, вечный цикл (интервал не проверялся отдельно) | По источнику, независимые try/except | `app_settings` (скаляры! см. Г5), `source_runs` | Частично — `KDIF_RATES_RAW`/`STAT_HOUSING_ASTANA_RAW` хранят сырой список найденных строк текстом |
| **Entity resolution — ЖК↔источник** | `record_source_link()` внутри каждого из сборщиков выше (`bot/core/entity_resolution.py`) | По требованию, при каждом сборе с любого источника | N/A | `complex_source_links` (UPSERT по `source+source_id` — см. Г7), `complex_source_link_candidates`, `complex_source_link_rejections` | N/A |
| **Entity resolution — дубли/расшивка/юниты (свипы)** | `merge_translit_dups.py`, `backfill_entity_resolution.py`, `rescore_review_queue.py`, `phase2_unit_match.py`, `split_detect.py` | **Без таймера, кроме `rescore_review_queue.py`** (`krisha-review-rescore.timer`, ежедневно 05:30) — остальные ручные/разовые | N/A (локальные пересчёты по уже собранным данным, кроме homeportal/korter повторных запросов внутри rescore) | `complex_duplicate_candidates`, `unit_duplicate_candidates`, `split_candidates`, `unit_match_gold_labels` | N/A |
| **House-resolution (объявление → дом зонтика)** | `bot/core/house_resolution.py`, встроен в цикл `service_apartments.py` | Каждый цикл парсера квартир (см. выше) | N/A (только чтение уже собранного + гео-расчёт) | `apartment_listings.resolved_house_id`/`.house_attribution`/`.house_attribution_detail` | N/A |
| **Гео-привязка (rebind/coords/geocode)** | [`service_geobind.py`](../service_geobind.py) (rebind → complex_audit → complex_coords → krisha_complex_import → geocode) | Вечный цикл (интервал не проверялся отдельно) | Внутри — `krisha_complex_import.py` явно щадящий (4-8с/запрос), Nominatim — 1 запрос/сек | `apartment_listings.complex_name/.lat/.lon`, `complexes.lat/.lon/.developer/.address/.year_built` | Только normalized |
| **Дедупликация объявлений** | `bot/core/dedup.py`/`dedup_listings.py`, встроен в циклы парсеров | Каждый цикл (продажа/аренда) | N/A (локальный анализ уже собранного) | `apartment_listings.is_duplicate/.duplicate_of`, `dedup_scan_log` (лог прогона — событие) | N/A |
| **Собственные пользователи сайта (избранное/визиты)** | `bot/core/site_auth.py`, `service_site_bot.py` | По действию пользователя (не расписание) | N/A (внутренний трафик) | `listing_views` (СОБЫТИЕ: user_id+listing_id+action+ts), `users`, `login_tokens` | N/A |
| **Гараж/кладовка/коммерция (investment)** | `bot/jobs/scheduler.py` `check_investment_objects()` | Часть цикла бота (частота не проверялась отдельно) | Догрузка описания только для pre-score≥50 | **SQLite** `investment_listings` (`bot.db`) — см. Г12: в Postgres тоже есть одноимённая таблица, но она мертва с 2026-06-05 | Только normalized |

---

## §2. Темпоральная целостность по сущностям

Легенда: 🟢 append-only (событие/история сохраняется) · 🟡 частично
(таймстамп «последнего изменения» есть, но не полная история) ·
🔴 overwrite без таймстампа вовсе (прошлое не восстановить и не видно,
когда поменялось).

| Сущность | Модель | Таймстампы | Комментарий |
|---|---|---|---|
| Цена продажи (`apartment_listings.price`) | 🟢 событие | `price_history(changed_at)` | Образцово: колонка = текущее значение, `price_history` = полная история изменений |
| Цена аренды (`rental_listings.price`) | 🟢 событие | `rental_price_history(changed_at)` | Та же образцовая пара, независимо реализована для аренды |
| Цена юнита застройщика (`newbuild_units.price`) | 🟢 событие | `newbuild_unit_price_history(changed_at)` | Третья независимая, но идентичная по духу реализация той же пары |
| Статус объявления (активно/архив) | 🟡 частично | `archived_at`, `archive_checked_at`, `is_active` (bool) | Есть МОМЕНТ ухода в архив, но не полная история переходов (перевыставили → снова активно → это НЕ видно, `archived_at` просто обнулится/перезапишется — см. Г1) |
| **Просмотры объявления (`views_count`)** | 🔴 overwrite | `views_count_updated_at` (когда проверяли, НЕ ряд значений) | **Гэп Г2**: динамика просмотров недоступна вообще — только «сейчас N, обновлено тогда-то». Нет `views_history` (не путать с `listing_views` — та о своих пользователях, не о счётчике Крыши) |
| **Агрегаты ЖК (`complexes.avg_price_m2/.avg_yield/.listings_count`)** | 🔴 overwrite | только `updated_at` на ВСЮ строку `complexes` (много других полей могли обновиться, не отличить, что именно) | **Гэп Г3**: «росла ли медианная цена ЖК за полгода» нельзя восстановить из `complexes` вообще — только реконструкцией из `apartment_listings.first_seen` задним числом, и то не то же самое (объявления архивируются) |
| Описание ЖК (`complexes.notes/.residents_notes/.description`) | 🔴 overwrite | нет своего таймстампа (общий `complexes.updated_at`, который не гейтится конкретно этим полем) | **Гэп Г4**: правка/затирание текста невидимо-молчаливо; `krisha_complex_scan.py` осознанно пишет `description` ТОЛЬКО если было пусто (защита от перезаписи) — хорошая практика на уровне ОДНОГО скрипта, но не общая политика (админ-правка через `/admin/complexes` всё ещё просто `UPDATE`, без версии) |
| Фото ЖК (`complexes.photos`) | 🔴 overwrite | `photos_source` (текущий источник, не история источников) | Приоритетная политика (developer>homeportal>krisha>korter>homsters) защищает от понижения качества, но не хранит, какие фото были ДО замены — если новый набор фото хуже (битые ссылки, не тот ЖК), старый набор не восстановить |
| Официальные данные КЖК (`homeportal_objects.*`) | 🔴 overwrite | `fetched_at` (последний скан), `matched_at` (для привязки к `complexes`) | ~35 текстовых полей перезаписываются целиком на каждый скан; если застройщик ИСПРАВИЛ, скажем, `commissioning_date` — старое значение теряется, при этом это официальный юридический факт, где история изменений могла бы быть значимой |
| Программы застройщика (`developer_programs`) | 🔴 overwrite | `created_at`/`updated_at` на строку | UPSERT по `(developer_id,title)`; при изменении текста/срока — старая версия теряется; при исчезновении программы с сайта — строка НЕ удаляется и не помечается (см. Г6) — нельзя отличить «ещё действует» от «давно снята, просто не почистили» |
| Связь ЖК↔источник (`complex_source_links`) | 🟡 частично | `matched_at`, `matched_by` | **Гэп Г7**: `UNIQUE(source,source_id)` + запись через `record_source_link()`/`rescore_review_queue.py` — при повторном подтверждении/rescore той же пары `matched_at`/`confidence`/`evidence` перезаписываются, старое evidence (почему матч посчитали таким в первый раз) теряется |
| ER review-очереди (`complex_source_link_candidates`, `complex_duplicate_candidates`, `unit_duplicate_candidates`, `split_candidates`) | 🟢 событие (де-факто) | `created_at`+`resolved_at`, статус меняется, строка не удаляется | Хороший паттерн — решения оператора не теряются, полная история кто/когда/почему решил |
| Gold-labels юнит-мэтчинга (`unit_match_gold_labels`) | 🟢 событие | `decided_at` | Append-only по конструкции (уже отмечено в scoring-аудите) |
| Хайп/новостной рейтинг локации (`hype_locations`+`hype_location_history`) | 🟢 событие | `first_seen`/`last_seen` (текущее состояние) + `ts` (каждое упоминание в истории) | Образцовая пара текущее-состояние/история — тот же паттерн, что цены, просто для другого домена |
| Enrichment-изменения ЖК (Korter/Homsters конкретных полей) | 🟢 событие | `source_changes.ts` | Отдельный лог `(source, complex_id, field, old_value, new_value, ts)` — фиксирует именно то, чего не хватает `complexes.updated_at`: ЧТО изменилось. Не используется для `krisha_complex_scan.py`/`homeportal_scan.py` — только Korter/Homsters (см. Г8) |
| Дедуп-статус объявления (`is_duplicate`) | 🔴 overwrite | `dup_marked_at` (момент последней разметки) | Нет истории «объявление A считалось дублем B, потом — дублем C» |
| Материалы ЖК (`complex_materials`) | 🟡 частично | `created_at`, нет `updated_at` | UPSERT по `(complex_id, source_name)` — правка теряет старый текст той же строки, но НОВЫЙ источник добавляет новую строку (не перезаписывает чужой) — частично хорошо |
| Тех.характеристики ЖК (`complex_tech_specs`) | 🔴 overwrite | `updated_at` | Одна строка на ЖК, полностью перезаписываемая |
| Housing-class эксперимент (`housing_class_test`) | 🔴 overwrite | `updated_at`, `apartment_count_parsed_at` (отдельно!) | Хорошая деталь — `apartment_count` версионируется своим таймстампом отдельно от остальной строки, но остальные поля (лифты и т.п., ручной ввод) — нет |
| `complexes.score_total/.score_location/.score_infrastructure/.score_developer/.score_quality` | — | — | **Не про темпоральность — про существование**: 0 живых писателей во всём репозитории (проверено `grep`), значит эти 5 колонок мертвы, не только не версионированы. Отдельная от scoring-аудита находка (там жила похожая проблема с `apartment_score.py`), тот же класс проблемы на уровне ЖК |
| Юниты застройщика (`newbuild_units`) | 🟢 событие (частично) | `first_seen_at`/`last_seen_at`/`sold_at`/`created_at`/`updated_at` | Лучшая по покрытию таймстампов таблица в проекте; статус `available→sold` фиксируется `sold_at`, но НЕ полная машина состояний (нет `reserved_at`, если юнит уходит available→reserved→available снова — не видно) |
| Краш-инциденты (`crime_incidents`) | 🟢 событие | `fetched_at`, натурально неизменны (дата преступления не меняется задним числом) | Один из лучших примеров — уникальность по `objectid` + натуральному ключу, `ON CONFLICT` не перезаписывает |
| Воздух (станции/официальный/сетка) | 🟢 / 🟢 / 🔴 | `ts`+`fetched_at` (станции), `version`+`fetched_at` (офиц.), только `fetched_at` без уникальности (сетка) | Сетка (`air_grid`) — единственная из трёх воздух-таблиц БЕЗ `UNIQUE`, см. Г9 |

---

## §3. Специально запрошенные проверки

- **История цены — событие, не перезапись.** ✅ Подтверждено для продажи
  И аренды (`price_history`/`rental_price_history`) И юнитов
  застройщика (`newbuild_unit_price_history`) — все три независимо
  реализованы, но по одному и тому же корректному паттерну
  (`(listing_id, old_price, new_price, changed_at)`). Симметрично,
  надёжно, это ядро временной модели проекта сделано правильно.
- **Динамика просмотров.** ❌ Не сохраняется. `apartment_listings.
  views_count` — текущее число + `views_count_updated_at` (когда в
  последний раз проверяли). Нельзя построить график «просмотры по дням»
  для конкретного объявления, только видеть текущий снимок. Не путать с
  `listing_views` (см. ниже) — та о поведении СВОИХ пользователей на
  сайте, не о счётчике Крыши.
- **Агрегаты `complexes` (avg_price_m2/avg_yield).** ❌ Не сохраняется.
  Прямая перезапись каждый цикл, без даже намёка на снимок «на дату».
  Ближайший обходной путь для ретроспективы — реконструкция из
  `apartment_listings` по `first_seen`/`archived_at`, но это не то же
  самое (не snapshot агрегата на дату, а пересчёт задним числом по
  выжившим/известным сейчас записям — которые могли отличаться от того,
  что реально было видно системе в тот момент, если что-то было удалено
  или дедуплицировано позже).
- **Статусы active/archived.** 🟡 Момент ПЕРВОГО ухода в архив виден
  (`archived_at`), но не полный журнал переходов. У `apartment_listings`
  нет отдельной `status_history`. Частично компенсируется тем, что
  архивация в этой системе почти всегда терминальна (Krisha убирает
  проданное/снятое, не переиспользует объявление) — риск ниже, чем для
  сущностей, которые реально мигрируют туда-сюда (см. `newbuild_units.
  status`, где `available↔reserved` возможен и НЕ версионируется).
- **Версии описаний/фото.** ❌ Не сохраняются нигде (ни объявления, ни
  ЖК). Единственное исключение — фото ЖК имеют `photos_source`
  (текущий источник) с приоритетной защитой от понижения качества, но
  это не версия/история, а гейт на запись.
- **Provenance ссылок и решений.** 🟡 Смешанная картина: ER
  review-таблицы (`*_candidates`, `unit_match_gold_labels`) —
  образцовый append-only с полным `created_at`/`resolved_at`/decision;
  сам «боевой» спайн (`complex_source_links`, `unit_source_links`) —
  overwrite при повторном подтверждении/rescore, provenance ДО
  последнего решения теряется. `source_changes` — отдельный частичный
  ответ на этот же вопрос, но охватывает только Korter/Homsters, не
  Homeportal/Krisha-скан/ручные правки в админке.

---

## §4. Гэп-репорт (риск + предложение)

Приоритет — по риску потери НЕВОССТАНОВИМОГО ретро-ряда, не по объёму
работы на фикс. Статус — по итогам решения заказчика 2026-08-14
(приоритет 1 — сделано в тот же день, приоритет 2 — следующий заход).

| # | Гэп | Риск / какой ретро-ряд невозможен | Предложение | Статус |
|---|---|---|---|---|
| **Г1** | Статус объявления — только `archived_at`, нет журнала переходов | Нельзя отличить «ушло в архив один раз» от «перевыставлялось несколько раз» | `status_history`-таблица по аналогии с `price_history`; либо задокументировать терминальность архивации на Krisha как факт | ⏳ вне приоритета 1/2 |
| **Г2** | `views_count` — снимок, не ряд | Нельзя построить «просмотры по дням»; популярность-сигнал, отмеченный неиспользуемым в `scoring_audit.md` §5.4 | `views_history(listing_id, views_count, observed_at)` | ✅ **сделано 2026-08-14** — таблица + `service_viewcount.py`, коммит `Г2:`. Гейт: отчёт через 7 дней (покрытие) |
| **Г3** | Агрегаты `complexes` (avg_price_m2/avg_yield/listings_count) — overwrite | Нет ответа на «как менялась медианная цена/м² по ЖК за N месяцев» | Ежедневный снимок `complex_stats_history(complex_id, date, avg_price_m2, avg_yield, listings_count)` | ✅ **сделано 2026-08-14** — таблица + `complex_stats_snapshot.py` + `krisha-complex-stats.timer` (08:15 ежедневно), коммит `Г3:`. Живой баг найден и починен в процессе: наивный join `resolved_house_id OR имя` считал объявление дважды (дом + зонтик) — переписано через CTE с приоритетом. Гейт: график через 30 дней |
| **Г4** | Описание ЖК — overwrite без своего таймстампа | Правка/порча текста невидима | `updated_at` на поле либо append-only `complex_notes_history` при ручных правках | ⏳ приоритет 2 (вместе с Г8) |
| **Г5** | Макро-данные рынка (`app_settings`) — скаляр, перезаписывается | Нельзя увидеть динамику ставки НБРК/KDIF | `market_data_history(key, value, observed_at)` | ⏳ вне приоритета 1/2 |
| **Г6** | Программы застройщика — пропавшие со страницы не помечаются | Нельзя отличить «действует» от «давно снята» | `is_active`+`removed_at` вместо молчаливого «оставить как есть» | ⏳ **приоритет 2**, следующий заход |
| **Г7** | `complex_source_links`/`unit_source_links` перезаписываются при rescore | Теряется evidence первого матча | `complex_source_links_history` либо не трогать `matched_at` при подтверждении той же связи | ⏳ вне приоритета 1/2 |
| **Г8** | `source_changes` есть только для Korter/Homsters | Тот же класс проблемы (Г4/Г7) уже решён для двух источников, не распространён | Распространить `source_changes`-запись на `homeportal_scan.py`/`krisha_complex_scan.py`/админ-формы | ⏳ **приоритет 2**, следующий заход |
| **Г9** | `air_grid` — без `UNIQUE`-ограничения | Дубли строк при ретрае таймера; коллектор и так `disabled` | `UNIQUE(lat, lon, fetched_at)` + решить, включать ли таймер обратно | ⏳ **приоритет 2**, следующий заход |
| **Г10** | `homeportal_objects` — ~35 полей перезаписываются целиком | Смена официальных юридических данных невидима | `source_changes`-паттерн (см. Г8) для реально волатильных полей | ⏳ приоритет 2 (вместе с Г8) |
| **Г11** | Часть таблиц создаётся `CREATE TABLE IF NOT EXISTS` в скриптах, не в `migrations/` | Схему нельзя воспроизвести по одному `migrations/` | Свести схему сбора данных в `migrations/` | ⏳ вне приоритета 1/2 |
| **Г12** | Postgres `investment_listings` — мёртвый снимок с 2026-06-05, живой писатель — SQLite | Дашборд/sheets-экспорт молча показывали 2-месячную статистику как текущую | Переключить читателей на SQLite | ✅ **сделано 2026-08-14** — `bot/admin_web.py` читает `bot/db/investment_queries.get_healthcheck_stats()` (SQLite). Уточнение по ходу: `bot/core/sheets_sync.py`, тоже заподозренный в аудите, при проверке оказался уже корректен (SQLite) — его инвестиционный экспорт просто нигде не вызывается (мёртвый код, не баг с БД). Коммит `Г12:` |

### Единая темпоральная политика

Вынесена в отдельный документ — [`temporal_policy.md`](temporal_policy.md)
(правило на НОВЫЕ таблицы/поля, не только на закрытие гэпов выше).
Коротко: append-only для полей, где реально спрашивают «как менялось»;
`updated_at`/`computed_at` на всё мутируемое; `observed_at` отдельно от
обоих на каждое собранное (не вычисленное) значение; `raw` рядом с
`normalized` — по TTL, для дорогих/нестабильных источников, не по
умолчанию everywhere.

---

## Как это соотносится со scoring-блоком

Часть находок здесь **усиливает** уже принятые решения из
[`scoring_roadmap.md`](scoring_roadmap.md):
- Г3 (агрегаты ЖК без истории) — прямая причина, почему «пересчёт раз в
  месяц» для `complex_location_scores` (Часть 3, п.9 того документа)
  должен явно проектироваться С `computed_at`, не как ещё один overwrite
  — иначе тот же класс гэпа появится на новой таблице в день её
  создания.
- Г2 (просмотры без истории) — тот же `views_count`, что аудит скоринга
  уже пометил как «собран, но не используется в скоре» — здесь
  подтверждено, что даже ЕСЛИ его когда-нибудь включат в формулу,
  сначала нужна история (Г2), иначе включать нечего, кроме мгновенного
  снимка.
- Г12 (investment_listings раздвоение) — независимая, но такая же по
  духу находка, как «`hype_tracker` без схемы в `migrations/`» (Г11) —
  оба про источник истины, размазанный между процессами/базами без
  единого документированного владельца.

Фиксы из §4 предлагаются как записи в будущую таблицу `decisions`
(scoring_roadmap.md, Часть 5, п.15) со `status='proposed'` — до её
появления фиксируются здесь текстом, ничего не реализовано кодом.
