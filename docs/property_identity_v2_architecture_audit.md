# Property Identity v2 — архитектурный аудит (read-only)

Задача 2026-08-16, "Property Identity v2: read-only аудит сильных
сигналов и существующего unit/entity-resolution слоя". Ничего не
меняет: ни схему, ни `property_linker.py`, ни production-данные.
Компаньон-документы: `docs/entity_resolution_plan.md` (Фазы 1/2, ЖК и
юниты застройщика), `docs/property_match_candidates_proposal.md`
(предложение из задачи "безопасный exact-only property linker", НЕ
применено).

## 1. Карта существующих identity/dedup-механизмов

Проект уже содержит **семь независимых** механизмов, решающих родственные,
но не идентичные задачи "это тот же объект?" на разных уровнях
сущностей. Ни один код-путь их не объединяет.

| # | Механизм | Файл(ы) | Что с чем сопоставляет | Статус |
|---|---|---|---|---|
| A | **dedup_listings** (живой, прод) | `bot/core/dedup_listings.py`, вызывается из `service_apartments.py:898`/`service_rental.py:92` на КАЖДОМ цикле парсинга | `apartment_listings` ↔ `apartment_listings` (та же таблица, ОДНОВРЕМЕННО активные дубли — «от хозяина» и «от риелтора» одного жилья) | **Живой**, пишет `is_duplicate`/`duplicate_of`/`dup_match`/`dup_needs_review` прямо в `apartment_listings` |
| B | **outcome_labels relist heuristic** | `outcome_labels_recompute.py` (`relist_candidates`/`relist_match` CTE, строки 118-206) | архивный listing ↔ новый listing в окне 60 дней (ЖК+комнаты+этаж+площадь±5%+цена±15%, либо ослабленно площадь±10%) | **Живой** (таймер `krisha-outcome-labels`), пишет `outcome_labels.relisted_within_60d`/`possibly_relisted` |
| C | **bot/core/dedup.py + find_similar_photo_hash** | `bot/core/dedup.py`, `bot/db/queries.py:418-431` | listing ↔ listing в **отдельной legacy sqlite-таблице `listings`** (не `apartment_listings`!), ≥3 полей ИЛИ phash-дистанция <10 | **Мёртвый** — `krisha-bot.service` (systemctl) `inactive (dead)`, `listings.photo_hash` никогда не заполняется ни одним writer'ом, `find_similar_photo_hash`/`deduplicate()` не вызываются ниоткуда, кроме тестов |
| D | **entity_resolution (Фаза 1, ЖК)** | `bot/core/entity_resolution.py`, `complex_source_links`/`_candidates`/`_rejections` (миграция 043/044), + семейство скриптов `sweep_translit_dups.py`/`merge_translit_dups.py`/`orphan_match*.py`/`strict_match.py`/`match_homeportal_531.py`/`kzk_registry_match.py`/`split_detect.py` | `complexes` ↔ `complexes` (разные источники одного ЖК), либо listing↔complex атрибуция | **Живой**, реализовано и работает в проде |
| E | **phase2_unit_match (Фаза 2, юниты новостроек)** | `phase2_unit_match.py`, `unit_duplicate_candidates`/`unit_source_links`/`unit_match_gold_labels` (миграции 049/050/051) | `newbuild_units` (реестр застройщика) ↔ `apartment_listings` (Крыша, `is_new_build=TRUE`) | **Живой**, гейт-режим (`--limit`) |
| F | **house-resolution** | `bot/core/house_resolution.py` (см. миграцию 058), `apartment_listings.resolved_house_id` | listing ↔ конкретный ДОМ внутри зонтичного ЖК (`parent_complex_id`) | **Живой** |
| G | **property_linker (Property Identity v1)** | `bot/identity/property_linker.py`, `properties`/`property_listings` (миграции 083/084) | `apartment_listings` ↔ `apartment_listings` (тот же адрес+этаж+площадь, ЛЮБОЙ момент времени, включая relist через месяцы) | Написан, **backfill на проде НЕ запускался** |

### Ответы на прямые вопросы задачи

**Не дублирует ли `properties` уже существующую сущность `unit`
(newbuild_units)?** Нет, по населению данных: `newbuild_units` —
ТОЛЬКО застройщик-предоставленные юниты новостроек (5 источников: BI
Group/Sensata/Bazis/NAK/Orda Invest/Свой дом), `apartment_listings` с
`is_new_build=TRUE` — лишь часть от 50352 (вторичка не имеет
`newbuild_units`-аналога вовсе). `properties` покрывает ВСЕ 50352,
включая вторичный рынок. Пересечение по населению — подмножество, не
дубль: там, где Krisha-листинг УЖЕ подтверждённо смэтчен с
`newbuild_units` через `unit_source_links`, `unit_id` — более точный
и надёжный идентификатор физической квартиры (реестр застройщика
знает номер квартиры/корпус/секцию), чем `properties.address_hash`
(не знает ничего из этого, см. §2). Рекомендация в §5.

**Какие данные УЖЕ используются для поиска дублей?** См. таблицу
выше — адрес(норм.)+площадь+комнаты (A, addr_area), адрес+цена+этаж
(A, addr_price), координаты+комнаты+этаж+площадь+цена (A, geo), фото
UUID (A, photo — **0 реальных совпадений на 50352 строк**, см. §2),
ЖК+комнаты+этаж+площадь±5%+цена±15% с временным окном (B), номер
квартиры/этаж/площадь/цена/дата (E, только newbuild).

**Можно ли связать/объединить unit и property?** Да — предложение в
§5: там, где есть подтверждённый `unit_source_links`, приоритет
физической идентичности должен быть за `unit_id`, НЕ за
`address_hash`. `properties`/`property_listings` НЕ заменяются, но
получают опциональную ссылку `properties.newbuild_unit_id` (только
предложение схемы, не применено).

**Какой слой должен быть каноническим?** Ни один существующий слой не
покрывает ВСЮ задачу целиком (relist на вторичке через месяцы, с
именно этой физической квартирой, без временного окна). `properties`
— единственный кандидат на эту роль СТРУКТУРНО (охват 100% населения),
но его конкретная реализация (address_hash) содержит риски (§2, §6,
§7) и НЕ консультируется с A/B/E, хотя должен бы (см. §5 —
рекомендация сверять с `is_duplicate`/`duplicate_of` как с независимым
подтверждающим сигналом).

**Какие таблицы сейчас источник истины?** `apartment_listings` — сырые
факты о каждом объявлении (единственный источник правды по самим
объявлениям). `complexes` — источник истины по ЖК (после Фазы 1).
`newbuild_units` — источник истины по юнитам новостроек СО СТОРОНЫ
застройщика. `properties` — ЗАДУМАН как источник истины по физической
квартире, но пока НЕ backfilled и содержит признанные архитектурные
риски.

## 2. Инвентаризация сигналов (реальное покрытие, 50352 apartment_listings)

| Сигнал | Покрытие | Стабильность | Положительное доказательство? | Отрицательное доказательство? | Риск коллизий |
|---|---|---|---|---|---|
| normalized address (без номера дома) | 100% (пустая строка — 0 наблюдалось) | Высокая, но текст «— ориентир» варьируется (см. `dedup_listings._norm_address` — уже чистит этот хвост, `property_linker.normalize_address` — нет) | Слабое само по себе (см. house number ниже) | Да, при разном адресе | Высокий: 42891/50283 уникальных hash из <complex_id-масштаба района> |
| house number (внутри адреса, НЕ отдельное поле) | входит в address_hash неявно | Средняя — формат "10", "10/2", "10 стр 1" не унифицирован | Да, в паре с адресом | Да | Средний — опечатки/сокращения |
| complex_id (через complex_name) | 45065/50352 (89.5%) | lower(trim()) лукап, дубли имён возможны | Да (якорь для fuzzy) | Слабое (разные ЖК рядом) | Низкий |
| rooms | 50352/50352 (**100%**) | Высокая | Да | Да, ЕСЛИ отличается у пары с иначе совпадающими сигналами | Низкий (само по себе) |
| floor | 48752/50352 (96.8%) | Высокая | Да | Да | Низкий |
| area | 50352/50352 (**100%**) | Высокая, но округление/грешность парсинга ±0.1-0.5м² бывает | Да (в допуске) | Да (вне допуска) | Средний (см. §6, transitive chains) |
| seller identity (имя, НЕ телефон — `seller_phone` НЕ СУЩЕСТВУЕТ, Крыша прячет за JS) | зависит от заполненности `seller_name` (не проверял отдельно в этой задаче — уже посчитано в seller_profiles) | Низкая (см. `seller_profile_snapshot.py` — коллизии частых имён: "Асель" 172 объявления) | Слабое (совпадение) | Слабое-среднее (несовпадение — НЕ доказывает разные квартиры, могло смениться агентство) | Высокий (частые имена) |
| first_seen/last_seen/archived_at | 100% (NOT NULL default) | Высокая | Да (непересекающиеся интервалы = типичный relist-паттерн) | Да (пересечение = вероятно РАЗНЫЕ квартиры) | Низкий |
| одновременная активность (интервалы) | вычисляется из вышеуказанных | Высокая | — | **Сильный** — 94-97% exact/fuzzy кластеров имеют пересечение (см. предыдущие аудиты) | — |
| price + price_history | 50352/50352 price; price_history по каждому listing_id отдельно | Средняя — цена сама по себе слабый сигнал (сходная цена — совпадение рынка) | Слабое (похожая цена) | Среднее (сильно разная цена — по духу `outcome_labels_recompute.py`'s ±15%/`dedup_listings`'s ±250к) | Средний |
| description | 41506/50352 (82.4%) | Низкая — свободный текст, шаблонные фразы у риелторов дают ложное сходство | Слабое (высокое текстовое сходство — не идентификация) | Слабое | Средний (шаблонность) |
| photo URLs/hashes | `photos` JSONB — 44120/50352 (87.6%); **перцептивный хэш НЕ существует на apartment_listings вообще** | URL содержит per-listing UUID (не привязан к физическому файлу изображения content-addressably на стороне Крыши по наблюдаемым данным) | **0 реальных совпадений** на всей базе через `dedup_listings.py`'s photo-UUID правило (`dup_match='photo'`: 0 строк из 14867 dup) — см. §2.1 | Не проверял (симметрично — раз 0 совпадений, сигнал не работает ни в какую сторону) | Не оценить — сигнал фактически не функционирует |
| coordinates | 50128/50352 (99.5%) | Высокая, geocoding-погрешность ~десятки метров | Да (в допуске ~50-60м, тот же допуск, что `dedup_listings.py` rule geo) | Слабое (разные координаты — geocoding мог ошибиться) | Низкий |
| apartment_number | НЕТ отдельного поля; извлекается regex из description/title (`phase2_unit_number_coverage.py::unit_number_listing`) | **9.6%** (4851/50352) реальное покрытие на полной популяции (было известно только 4% на подвыборке is_new_build) | **Сильное** — если извлечён с обеих сторон и совпадает | **Сильное** — если извлечён с обеих сторон и НЕ совпадает (тот же принцип, что `phase2_unit_match.py::decide_pair`: "есть и НЕ равны -> reject, перебивает остальные сигналы") | Низкий там, где извлечён |
| section/entrance (подъезд) | НЕТ вообще ни в одном поле `apartment_listings` (есть только в `newbuild_units.section`, только застройщик-сторона) | — | Недоступно | Недоступно | — |
| layout/plan identifiers | НЕТ на `apartment_listings` (есть `newbuild_units.layout_photo_url`, только новостройки-застройщик) | — | Недоступно | Недоступно | — |
| source listing identifiers | Единственный источник — `id` = "id объявления с Крыши" (см. `000_core_tables.sql`). Других скрейперов, пишущих в `apartment_listings`, НЕТ (korter/homsters/bazis/nak/orda/bi_group/sensata пишут в `complexes`/`newbuild_units`, не сюда) | — | Н/п — нет кросс-source дублирования на этом уровне | — | — |
| `is_duplicate`/`duplicate_of` (существующий, независимый сигнал) | 14867/50352 (**29.5%**) уже помечены `is_duplicate=TRUE` живым `dedup_listings.py` | Средняя-высокая (production-калиброванный, но допуски area±3м²/цена±250к отличаются от property_linker'а) | **Сильное** — независимое подтверждение "то же жильё" другим механизмом | Слабое (не помечено ≠ точно разные — dedup_listings матчит только СРЕДИ ОДНОВРЕМЕННО просканированных, не через месяцы) | См. §5 |

### 2.1 Почему photo URL — мёртвый сигнал на практике

`dedup_listings.py`'s правило `'photo'` (высший приоритет по коду) даёт
**0 совпадений** из 14867 реальных dup-пар на всей базе (проверено:
`SELECT count(*) FILTER (WHERE dup_match='photo') FROM apartment_listings`
→ 0). Реальный перцептивный хэш (`bot/core/dedup.py::compute_image_hash`,
`imagehash.phash`) существует в коде, но НЕ подключён ни к какому
production-пути и требует скачивания изображений (сетевой ввод-вывод) —
намеренно не реализовывал в read-only аудите (см. §9, "минимальный
безопасный вариант backfill").

## 3. Read-only pair audit — см. `scripts/audit_property_match_signals.py`

Реализация и цифры — в финальном отчёте задачи (не здесь, чтобы не
дублировать вывод скрипта).

## 4. Candidate tiers

`rejected` / `weak_candidate` / `strong_candidate` / `review_required`
— правила и распределение см. в докстринге
`scripts/audit_property_match_signals.py::classify_tier()` и в
финальном отчёте. `confirmed` сознательно НЕ используется нигде в
коде до появления номера квартиры/ручной проверки/`unit_source_links`
(задача, п.4).

## 5. Unit layer (`unit_duplicate_candidates`) — детальный разбор

**Как создаются кандидаты**: `phase2_unit_match.py::decide_pair()` —
блокировка по `(complex_id, rooms)`, дальше: номер квартиры на ОБЕИХ
сторонах и РАВЕН → `auto` (в `unit_source_links`, confidence=1.0);
номер на обеих сторонах и НЕ равен → `skip`/reject (прямое
противоречие, не кандидат вовсе); иначе этаж точный + площадь±3м² +
(цена±5% ИЛИ пересечение дат) → `auto` (confidence=0.85); "зеркальный
кап" — если планировка (этаж+похожая площадь) повторяется 2+ раз на
ЛЮБОЙ стороне блока → всегда `review`, даже при подтверждении
цена/дата (несколько идентичных юнитов в разных секциях одного дома
не различить); иначе → `review` (`unit_duplicate_candidates`).

**Какие evidence хранятся**: JSONB `{floor_match, area_delta,
price_delta_pct, date_overlap, unit_number_nb, unit_number_al,
mirror_count_nb, mirror_count_al}` — прямо предвосхищает то, что
задача просит для `property_match_candidates`.

**Решает ли он уже ту же задачу?** Частично и для ДРУГОЙ пары сущностей
— см. таблицу §1. Он решает "листинг Крыши ↔ юнит застройщика", не
"листинг Крыши ↔ листинг Крыши другого месяца". Архитектурный ПАТТЕРН
(candidates review-queue → source_links spine → gold_labels
append-only журнал решений) — ПОЛНОСТЬЮ переиспользуем, и уже был взят
за образец в `docs/property_match_candidates_proposal.md`
(предыдущая задача) независимо, теперь подтверждено осознанно, не
случайно.

**Можно ли property_id строить поверх подтверждённого unit_id?** Да,
для подмножества, где `unit_source_links` уже существует
(`is_new_build=TRUE` + застройщик даёт feed) — `unit_id` там строже
(знает planировку/секцию), чем `address_hash`. Для остальной (большей)
части базы (вторичка, нет `newbuild_units`) — `unit_id` в принципе
недоступен, `properties` остаётся единственным кандидатом.

**Почему был создан отдельный properties layer, если unit уже
существует?** `newbuild_units` физически не существует для вторичного
рынка и для НЕ покрытых feed'ом новостроек — `properties` не мог быть
построен НАД `unit`, только РЯДОМ, как более широкий (но менее точный)
слой.

**План объединения без потери данных (предложение, миграция НЕ
создана)**: `properties.newbuild_unit_id INTEGER REFERENCES
newbuild_units(id) NULL` — заполняется ТОЛЬКО когда конкретный
`property_id` уже линкует listing, у которого ЕСТЬ подтверждённый
`unit_source_links` (JOIN по listing_id). НЕ меняет `address_hash`-based
линковку для остальных 100% — чисто аддитивная колонка, обратно
совместимая. Дальнейший backfill/чтение приложений, где
`newbuild_unit_id IS NOT NULL`, могут доверять физической идентичности
СИЛЬНЕЕ, чем там, где он NULL (доверие только к address_hash).

## 6. Схема address_hash — предложение (миграция НЕ создана)

**Проблема**: `properties.address_hash` сейчас `UNIQUE` — структурно
запрещает существование ДВУХ properties с одним и тем же
адрес+этаж+площадь, даже когда это ДВЕ РЕАЛЬНЫЕ РАЗНЫЕ квартиры
(многоподъездный ЖК с повторяющейся планировкой — см. предыдущий
аудит, `scripts/audit_address_hash_exact.py`, кластер size=28 с 19
разными seller identity). UNIQUE буквально не даёт создать вторую
запись даже если бы линковщик ЗАХОТЕЛ.

**Предложение**:
```sql
-- ПРЕДЛОЖЕНИЕ, НЕ ПРИМЕНЕНО.
ALTER TABLE properties DROP CONSTRAINT properties_address_hash_key;
CREATE INDEX IF NOT EXISTS idx_properties_address_hash ON properties (address_hash);
-- address_hash теперь НЕ identity key сам по себе — только индекс
-- ускорения "кандидаты с таким же адресом+этажом+площадью".
-- НАСТОЯЩИЙ identity key остаётся property_id (SERIAL PK, без
-- семантики — тот же принцип, что entity_resolution.py про complexes.id:
-- "PK без семантики, семантика — в атрибутах").
```

**Как создавать несколько разных properties с одинаковым address_hash**:
без UNIQUE — просто два INSERT с одинаковым `address_hash`, разными
`property_id`. Линковщику нужно СМЕНИТЬ решающий вопрос с "есть ли
СТРОКА с этим хэшем" на "есть ли СТРОКА с этим хэшем, evidence которой
НЕ противоречит новому listing" (rooms/section/unit_number, если
известны) — то же самое дерево решений, что уже есть в
`phase2_unit_match.py::decide_pair()`.

**Как в будущем безопасно merge/split properties**: append-only журнал
решений (`property_merge_log`, по образцу `unit_match_gold_labels`) —
`{from_property_id, into_property_id, decided_by, decided_at,
evidence_snapshot}` для merge; split — создать новую property, явно
перенести подмножество `property_listings.property_id`, тот же журнал
с `action='split'`. НЕ удалять исходную property физически — только
переставлять `property_listings.property_id` внутри транзакции.

**Хранение matcher_version/evidence/решения reviewer** — см.
`docs/property_match_candidates_proposal.md` (уже предложено ранее):
`property_match_candidates.matcher_version`/`evidence`/`status`, плюс
предложенная `property_listings.matcher_version TEXT` (тот же
документ). В этой задаче добавляю: `property_match_candidates.tier`
(`rejected`/`weak_candidate`/`strong_candidate`/`review_required` — §4)
как отдельное поле ОТ `status` (`pending`/`accepted`/`rejected`) —
`tier` — это ЧТО посчитал матчер (объяснимо, воспроизводимо), `status`
— что РЕШИЛ человек (может не совпадать с tier, human override).

## 7. Проверка confidence — семантика перепутана

`bot/identity/property_linker.py` — **ВСЕ** места точного exact-hash
совпадения дают `confidence=1.0` (строки с `"confidence": 1.0` — уже
связанный существующий, только что созданный новый, и cache-hit в
dry-run — 4 return-точки). Формально это означает "мы на 100% уверены,
что это та же физическая квартира" — но §2/§6 выше эмпирически
показывают, что это НЕ гарантировано (204 exact-hash кластера с
доказанным rooms mismatch на реальных данных, предыдущий аудит).

**Смешаны ДВЕ разные вещи**:
1. **Confidence извлечения полей** — "мы уверены, что распарсили
   address/floor/area правильно" — здесь `1.0` оправдан: поля не
   fuzzy-угаданы, взяты как есть из `apartment_listings`.
2. **Confidence совпадения объектов** — "мы уверены, что ДВЕ строки с
   этим хэшем — одна и та же физическая квартира" — здесь `1.0` НЕ
   оправдан безусловно: зависит от того, есть ли в доме несколько
   одинаковых по площади юнитов на этаже (§2, §6).

**Предлагаемая семантика (не применено к коду в этой задаче)**:
- `property_listings.confidence` — переименовать смысл (не колонку
  физически, миграция дорога) на "match confidence" (объект = объект),
  считать НЕ константой, а функцией: 1.0, только если(a) хэш совпал И
  (b) НЕТ других properties с тем же (complex_id, floor) и площадью
  в допуске (т.е. кластер РЕАЛЬНО однозначен структурно), иначе
  <1.0 (см. `classify_tier` в §4/аудит-скрипте — та же логика, что
  уже разделяет `strong_candidate` от `weak_candidate`/`review_required`).
- `manual`/`confirmed_by_unit_id` — отдельный булев/enum статус,
  ортогональный confidence (задача, п.4: "не смешивать"). Confidence
  0.6 НЕ становится "не проверено человеком" — это два разных вопроса.

## 8. Тесты

См. `tests/test_property_match_signals.py`.

## 9. Итог — см. финальный отчёт задачи в чате (commit/PR/pytest/CI/
рекомендации/минимальный безопасный backfill).
