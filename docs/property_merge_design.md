# Property Identity — physical merge: architecture & rollout plan

## Implementation status (2026-08-20, "Safe Physical Property Merge")

Реализовано: `bot/identity/property_merge.py` (engine), `migrations/092_
property_merge_log.sql` (§2 схема — без изменений), `scripts/property_
merge_plan.py`/`property_merge_apply.py`/`property_merge_rollback.py`
(CLI), `scripts/audit_property_merge_dry_run.py` (real-data dry-run,
§10 отчёт), `tests/test_property_merge.py`. **Ни один physical merge не
выполнен в production этим PR** — engine принят/протестирован, реальный
canary — отдельное решение (см. финальный отчёт PR).

Два расхождения с этим документом, разрешённые кодом (полные обоснования
— докстринг `bot/identity/property_merge.py`, не дублируются здесь):

1. **§1 canonical scoring** дополнен identity_status-тиром (confirmed →
   provisional → merged) ПЕРЕД 7-факторным score — задача 2026-08-20
   явно попросила этот приоритет, документ его не предполагал. На
   сегодняшних данных (100% properties `provisional`) результат
   идентичен старой формуле.
2. **Новый шаг, отсутствовавший здесь** — `_resolve_live_canonical()`:
   после ПЕРВОГО реального merge `property_match_candidates.candidate_
   property_id` может указывать на уже `'merged'` property (§3 этого
   документа сознательно не трогает такие строки) — граф связности
   резолвит такие id к их живому canonical ПЕРЕД построением компонент,
   иначе следующий `plan()` включил бы пустую (без листингов) `'merged'`
   property как полноценного "участника".

Также добавлена (документ её не описывал, задача 2026-08-20 потребовала
явно) **строгая pre-merge ревалидация** поверх §9: rooms mismatch/severe
address mismatch (house number БЕЗ общего `complex_id`)/severe price
conflict, пересчитанные на ЖИВЫХ данных перед КАЖДЫМ `--apply`, плюс
frozen-manifest workflow (`component_hash`) — гарантирует, что `--apply`
никогда не строит собственный "живой" список accepted-кандидатов заново
(тот же класс бага, что нашёлся и был исправлен в photo-evidence batch
pipeline).

Задача 2026-08-18, "Property Identity — review calibration", Stage 2:
**проектный документ, физический merge НЕ выполняется этим PR ни в
production, ни где-либо ещё.** Код (если/когда будет написан) — отдельная
ветка/PR, запуск — только после отдельного явного ОК.

## 0. Эмпирическая база (прямо с прод-данных, read-only, 2026-08-18)

Среди сегодняшних 101 `accepted`-решений (после Stage 0 деплоя) построен
граф связности (union-find) по рёбрам `(property_listings.property_id
кандидата-listing, property_match_candidates.candidate_property_id)`:

| | значение |
|---|---|
| accepted-рёбер | 101 |
| уникальных properties, затронутых accepted | 171 |
| итоговых групп связности (= будущих merge-групп) | 70 |
| размер самой большой группы | **15** properties |
| групп размера 5-6 | 4 |
| групп размера 3-4 | 4 |
| групп размера 2 (простая пара) | 61 |

Самая большая группа — `{25757, 25980, 33466, 34292, 40195, 42869, 43555,
43587, 43665, 43780, 43819, 44847, 44998, 47225, 52263}`. Это ФАКТ, не
гипотеза: план ниже спроектирован под цепочки **до 15+ properties**, не
только под парные merge, задача просила поддержать 3-10 — реальность уже
превышает верхнюю границу этого диапазона на одном конкретном случае.
Merge-код должен принимать группу любого размера ≥2, не быть жёстко
рассчитан на "пара" или "максимум 10".

## 1. Выбор канонического `property_id` (ПЕРЕСМОТРЕНО, follow-up 2026-08-18, п.6)

Кандидат в канонические — property, официально "выживающая" в группе;
все остальные получают `identity_status='merged'` (значение УЖЕ
зарезервировано в CHECK-constraint, migrations/088, ни разу не
использовалось — этот PR был бы первым, кто его проставляет).

### 1.0 Почему простые три правила заменены (follow-up ревью)

Первая версия этого документа предлагала: "больше активных listing'ов
сейчас -> раньше first_seen_at -> меньший id". Follow-up-ревью справедливо
указало: **простого правила недостаточно** — оно теряет информацию о
полноте атрибутов, согласованности адреса, наличии координат/ЖК/этажа,
длительности истории и наличии конфликтов. Заменено на многофакторный
**deterministic scoring** — `scripts/audit_merge_canonical_scoring_dry_run.py`
(read-only, ничего не пишет; используется и для реального merge-кода
позже, и для аудита ниже):

| Фактор | Вес | Что измеряет |
|---|---|---|
| completeness | 25% | доля заполненных `complex_id`/`floor`/`area_sqm`/`rooms` (0..1, 4 поля) |
| address_consistency | 15% | согласие ТЕКУЩИХ адресов связанных listing'ов между собой (1.0 = все совпадают) |
| coords_presence | 10% | есть ли хоть один связанный listing с `lat`/`lon` |
| history_duration | 15% | `last_seen_at − first_seen_at`, нормализовано ОТНОСИТЕЛЬНО компоненты (самая долгая история в группе = 1.0) |
| listing_count | 15% | `count(property_listings)`, нормализовано относительно компоненты |
| conflict_absence | 10% | `1/(1+n)` конфликтных candidate-строк, касающихся этой property |
| freshness | 10% | recency `last_seen_at` относительно компоненты (самый свежий = 1.0) |

Веса — **экспертные, НЕ откалиброванные** на исходе реальных merge (тот же
честный disclaimer, что `docs/location_score_calibration_audit.md` §2 —
не выдаю их за измеренную истину, это явный кандидат на калибровку тем же
методом, что Location Score, ПОСЛЕ накопления данных о качестве решений).
Стабильный tie-break — меньший `property_id`, последним ключом сортировки.

Старое правило (больше активных listing'ов сейчас -> раньше `first_seen_at`
-> меньший `property_id`) остаётся только как историческая справка выше
(§1.0) — заменено целиком многофакторным scoring, не расширено.

### 1.1 Dry-run на реальной 15-property цепочке (read-only, 2026-08-18)

`scripts/audit_merge_canonical_scoring_dry_run.py`, без единой записи в
БД. На момент follow-up: **105** accepted-рёбер, **74** компоненты, самая
длинная — те же 15 properties, что в §0 (`25757, 25980, 33466, 34292,
40195, 42869, 43555, 43587, 43665, 43780, 43819, 44847, 44998, 47225,
52263}`).

**CANONICAL: `property_id=25757`** (score=0.9030). Решающий фактор —
`completeness` (все 15 properties этой группы имеют одинаковую
completeness=1.0, address_consistency=1.0, coords_presence=1.0,
listing_count=1.0 — группа НЕОБЫЧНО однородна по этим осям, реально
решают `history_duration` и `freshness`: 25757 — самая старая
(`first_seen_at=2026-07-08`, `history_duration=1.0`) И почти самая свежая
(`freshness=0.953`) одновременно). В ЭТОМ конкретном случае старое
простое правило дало бы ТОТ ЖЕ результат (все properties имеют
одинаковый `n_listings=1`, значит "больше активных listing'ов" не
дискриминирует, следующий критерий "раньше `first_seen_at`" тоже указывает
на 25757) — **новый scoring не всегда меняет ответ, но всегда даёт
проверяемое обоснование**, а не "совпало, что первый критерий сработал".

**Находка, требующая внимания**: `conflict_absence` для ВСЕХ 15 properties
этой группы низкий (0.067–0.091, т.е. 10-14 конфликтных
`property_match_candidates` строк касаются каждой из них) — эта
конкретная (человеком подтверждённая, accepted) цепочка одновременно
участвует во МНОЖЕСТВЕ других candidate-пар с `conflict_reasons`
непустым в другом месте графа. Это НЕ повод отменять решение рецензента
(конфликты могут относиться к СОВЕРШЕННО другим парам-кандидатам этих же
properties, не к самой accepted-связи) — но это сигнал, что merge-код
ДОЛЖЕН выводить эту метрику оператору перед реальным запуском, не
скрывать её за высоким итоговым score.

**Attribute conflicts**: 0 — floor=3/area=68.0/rooms=3/complex_id=2070
одинаковы на ВСЕХ 15 properties, ни одного расхождения.

**Repoint plan** (что было бы репойнтнуто, при реальном запуске): все 14
losing properties имеют РОВНО по одному listing — `property_listings`
14 строк были бы репойнтнуты на `property_id=25757`, ни одна
`apartment_listings`/`price_history`/`listing_snapshots` строка не
меняется (§4).

**Полная таблица** (74 компоненты, canonical на каждую) — печатается
скриптом целиком, здесь только сводка: **9** компонент размера ≥3, **65**
простых пар, ни одна не выбрала canonical по чистому tie-break (везде
хватило взвешенных факторов различить победителя, кроме случаев явных
score=1.000 — компонент, где ОБЕ properties идентичны по всем 7 факторам,
там решает финальный `property_id ASC`).

Ничего из этого НЕ применено к БД — только напечатано.

## 2. Схема: `property_merge_log` (append-only)

Тот же архитектурный паттерн, что `property_match_review_log`
(migrations/088) — README migrations/086 уже резервирует это имя как
"следующий PR", это он:

```sql
CREATE TABLE property_merge_log (
    merge_id         SERIAL PRIMARY KEY,
    merge_group_key   UUID NOT NULL,        -- одна группа = один UUID, объединяет все строки одного merge-события
    canonical_property_id INTEGER NOT NULL REFERENCES properties(property_id),
    losing_property_id    INTEGER NOT NULL REFERENCES properties(property_id),
    -- listing_id'ы, перенесённые ИМЕННО в рамках losing_property_id -> canonical
    -- (JSONB массив text — снимок на момент merge, не FK: property_listings
    -- дальше живёт своей жизнью, снимок должен остаться неизменным историческим фактом)
    moved_listing_ids JSONB NOT NULL,
    decision_source    JSONB NOT NULL,      -- {"candidate_ids": [...], "review_log_ids": [...]} — из чего собрана группа
    matcher_version    TEXT NOT NULL,
    merge_tool_version TEXT NOT NULL,       -- версия САМОГО merge-кода (отдельно от matcher_version — они меняются независимо)
    dry_run            BOOLEAN NOT NULL DEFAULT FALSE,
    executed_by         TEXT NOT NULL,
    executed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    rolled_back_at       TIMESTAMPTZ,        -- NULL, пока не откачено; append-only -> откат ДОБАВЛЯЕТ значение сюда, не удаляет строку
    rollback_reason      TEXT
);
CREATE INDEX idx_pml_group ON property_merge_log (merge_group_key);
CREATE INDEX idx_pml_canonical ON property_merge_log (canonical_property_id);
CREATE INDEX idx_pml_losing ON property_merge_log (losing_property_id);
```

Один merge группы из N properties -> N-1 строк (canonical не мерджится
сам в себя). `merge_group_key` связывает их для отображения "это было
одно событие" на будущей странице аудита merge-истории.

## 3. Перенос `property_listings`

```sql
BEGIN;
  -- (a) снимок moved_listing_ids для лога — ДО UPDATE
  -- (b) сам перенос — repoint FK, НЕ delete/insert (сохраняет
  --     property_listings.id и linked_at неизменными — это тоже история)
  UPDATE property_listings
  SET property_id = :canonical_id
  WHERE property_id = :losing_id;

  -- (c) losing property помечается, НЕ удаляется
  UPDATE properties
  SET identity_status = 'merged'
  WHERE property_id = :losing_id;

  -- (d) INSERT в property_merge_log (append-only журнал)
  -- (e) property_match_candidates, ссылающиеся на losing_id как
  --     candidate_property_id, НЕ трогаем — они остаются историческим
  --     фактом "на момент решения кандидат ссылался на эту property",
  --     переписывать candidate_property_id задним числом исказило бы
  --     журнал решений (та же причина, по которой review log append-only)
COMMIT;
```

`property_listings.listing_id` — UNIQUE (migrations/084) — гарантирует,
что перенос НЕ может случайно задвоить listing на двух properties
одновременно; репоинт `property_id` для уже существующей строки FK
атомарен в одной транзакции.

## 4. Сохранение истории (аудит показал: НИЧЕГО переносить не нужно)

`price_history`, `listing_snapshots`, `listing_archive_history` — все
ключуются по `listing_id` (TEXT, `apartment_listings.id`), НЕ по
`property_id`. Merge не переименовывает и не трогает `apartment_listings`
вообще — эти три таблицы автоматически, без единой строки кода, остаются
консистентными после merge (listing_id, на который они ссылаются, не
меняется, только его "владелец" property меняется). Фотографии —
`apartment_listings.photos` (тот же listing_id) — не переносятся,
не трогаются.

Единственное, что физически меняется — `property_listings.property_id`
(repoint) и `properties.identity_status` losing-property (`'merged'`).
Ничего не удаляется нигде.

## 5. Rollback / split

Rollback одного merge-события (`merge_group_key`):

```sql
BEGIN;
  -- вернуть property_listings обратно на losing_id — ТОЛЬКО те
  -- listing_id, что реально были перенесены ИМЕННО этим merge-событием
  -- (moved_listing_ids снимок из лога, не "все listing'и canonical
  -- сейчас" — после merge canonical мог получить ЕЩЁ листинги через
  -- собственный incremental/другой более поздний merge, их откат
  -- этого события не должен трогать)
  UPDATE property_listings SET property_id = :losing_id
  WHERE listing_id = ANY(:moved_listing_ids) AND property_id = :canonical_id;

  UPDATE properties SET identity_status = 'provisional' WHERE property_id = :losing_id;

  UPDATE property_merge_log SET rolled_back_at = now(), rollback_reason = :reason
  WHERE merge_group_key = :group_key;
COMMIT;
```

Ограничение (честно, не скрыто): если после merge на canonical
"естественно" (через incremental job) прилинковался НОВЫЙ listing,
rollback НЕ пытается угадать, принадлежит ли он losing или canonical —
он просто не трогает то, что не было явно перенесено ЭТИМ событием
(`moved_listing_ids`). Это осознанно консервативно — false "верните всё"
хуже, чем "откатите ровно то, что сами внесли".

**Split** (разделить уже смерженную группу на две) — реализуется как
ЧАСТНЫЙ случай того же примитива: выбрать подмножество
`moved_listing_ids` одной группы, repoint их на НОВУЮ (или другую
существующую) property, записать это как отдельное merge-событие
(`decision_source` явно ссылается на "split of merge_group_key=X"). Не
отдельный код-путь — тот же UPDATE property_listings + INSERT
property_merge_log, только источник repoint — не "losing_id", а
конкретный список listing_id, выбранный оператором.

## 6. Транзакционность / идемпотентность

- Один merge (вся группа из N properties -> 1 canonical) — **одна
  Postgres-транзакция** (не N отдельных commit'ов) — половина группы
  не может остаться смерженной, а другая половина нет.
- Идемпотентность: перед UPDATE проверяется `identity_status != 'merged'`
  на losing_id (WHERE-условие) — повторный запуск того же merge-скрипта
  на уже смерженной группе — no-op (0 rows affected), не ошибка, не
  дублирующая запись в `property_merge_log` (INSERT INTO
  property_merge_log только если хотя бы одна строка property_listings
  реально была перенесена).
- Конкурентный incremental job (`bot/jobs/property_identity_incremental.py`)
  не должен работать с этой же property ОДНОВРЕМЕННО — `SELECT ...
  FOR UPDATE` на `properties` строках группы В НАЧАЛЕ транзакции merge
  (та же защита, что advisory lock в `bot/db/pg.py::_apply_migrations`,
  но на уровне строк, не глобальный).

## 7. `--dry-run`

Тот же паттерн, что `scripts/backfill_listing_floors.py` и
`scripts/photo_evidence_scan.py --dry-run`: считает и печатает ПОЛНЫЙ
план (`canonical_property_id`, все `losing_property_id`, все
`moved_listing_ids` на группу, оценка impact — сколько
`property_match_candidates`/`price_history` строк затронуто косвенно)
БЕЗ единого `UPDATE`/`INSERT`. Обязательный первый шаг перед реальным
прогоном на ЛЮБОЙ новой группе.

## 8. Запрет удаления исходных данных

Ни один DELETE не участвует в этом плане — единственные мутации:
`UPDATE property_listings.property_id` (repoint) и `UPDATE
properties.identity_status` (statuslabel). `apartment_listings`,
`price_history`, `listing_snapshots`, `photos`, `property_match_candidates`,
`property_match_review_log` — ни одна из них не в списке таблиц, которые
merge-код когда-либо модифицирует.

## 9. Цепочки 3-15 properties

Группы строятся union-find'ом по ВСЕМ `accepted`-рёбрам разом (не
попарно, не в порядке обработки) — ТОТ ЖЕ алгоритм, что использован для
эмпирического анализа в §0 этого документа (уже написан и проверен на
реальных 101 рёбрах — `scripts/audit_property_identity_review_calibration.py`,
часть "merge group preview", см. ниже). Merge одной группы — один проход:
выбрать canonical (§1), перенести ВСЕ N-1 losing одной транзакцией (§3).
Нет отдельного "парного" и "цепочечного" кода пути.

## 10. Relist vs concurrent-agent — раздельные метрики

Задача явно требует НЕ смешивать:

- **`relist`** — ОДИН и тот же продавец/собственник переразмещает ОДНО и
  то же объявление ПОСЛЕ того, как предыдущее исчезло (не одновременно
  активны). `property_match_candidates.relationship_type='relist'`
  (уже вычисляется `classify_relationship()`,
  `bot/identity/property_linker.py`) — merge-код читает это поле, НЕ
  пересчитывает заново.
- **`concurrent_agent_count`** / **`multi_agent_exposure`** — НОВАЯ
  метрика на properties (не на candidates): после merge группы —
  считается ПО ФАКТУ прожитой истории объединённой property, а не по
  относительному сравнению двух листингов:

  ```sql
  -- на одну canonical property, ПОСЛЕ merge:
  SELECT count(DISTINCT COALESCE(al.seller_name, al.id)) AS concurrent_agent_count
  FROM property_listings pl
  JOIN apartment_listings al ON al.id = pl.listing_id
  WHERE pl.property_id = :canonical_id
    AND al.first_seen <= :window_end AND COALESCE(al.archived_at, al.last_seen) >= :window_start;
  -- multi_agent_exposure = доля времени (дней) за всю историю property,
  -- когда concurrent_agent_count > 1 (не бинарный флаг "было хоть раз")
  ```

  Это ОТДЕЛЬНОЕ поле/представление (например, `property_concurrency_stats`
  materialized view или колонка, вычисляемая по требованию — НЕ
  добавляется в `property_match_candidates.relationship_type`, которое
  относится к ПАРЕ на момент candidate-генерации, а не к property целиком
  после merge). Задача, явно: "не смешанную с relist" — с этим
  разделением `relist`(pair-level, до merge) и `concurrent_agent_count`
  (property-level, после merge) физически не могут схлопнуться в одно
  число.

## 11. Impact на текущие 101 accepted

Прямое применение плана §0-§9 к сегодняшним 101 accepted:

- **70 merge-групп** будет создано одним прогоном (при `--dry-run` —
  один печатаемый план, без исполнения).
- Из них **61 — простая пара** (2 properties), **9 — цепочка ≥3** (одна
  из них — 15 properties).
- **171 properties** получат `identity_status` либо `'confirmed'`
  (canonical, ПОСЛЕ merge — отдельный вопрос, canonical-статус после
  merge задачей явно не описан, предлагается НЕ трогать
  `identity_status` canonical автоматически: merge подтверждает
  "перенос", не "полную идентификационную уверенность"; `'confirmed'`
  остаётся ручным флагом будущего PR) либо `'merged'` (101 losing).
- **0 rejected/pending кандидатов** этим планом не затрагиваются —
  merge читает ТОЛЬКО `status='accepted'`.
- Косвенный impact: `price_history`/`listing_snapshots` НЕ меняются
  (§4) — 0 строк.

## 12. Что явно ВНЕ этого документа

- Код merge-скрипта (`scripts/property_merge.py` или аналог) — не
  написан, будущий отдельный PR, после этого документа получит явное ОК.
- `identity_status='confirmed'` политика — отдельное решение.
- UI/страница merge-истории (просмотр `property_merge_log`) — отдельная
  задача уровня `/admin/property-match-review`.
- Автоматический merge по итогам будущей калибровки сигналов (Stage 1.1
  этой же задачи) — НЕ предлагается здесь; even после появления
  precision-оценок по сигналам, merge остаётся ручным per-decision
  действием, пока это явно не изменено отдельным решением.
