# Property Identity — physical merge: architecture & rollout plan

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

## 1. Выбор канонического `property_id`

Кандидат в канонические — property, официально "выживающая" в группе;
все остальные получают `identity_status='merged'` (значение УЖЕ
зарезервировано в CHECK-constraint, migrations/088, ни разу не
использовалось — этот PR был бы первым, кто его проставляет).

Правило (детерминированное, без ручного выбора на каждую группу):

1. **Больше активных (`is_active=TRUE`) listing'ов сейчас** — property,
   вокруг которой сегодня больше живых объявлений, вероятнее "настоящий"
   якорь текущего состояния рынка.
2. При равенстве — **раньше `first_seen_at`** (самая старая property в
   группе) — совпадает с интуицией "первое найденное = историческая
   точка отсчёта", тот же принцип, что уже используется для primary
   listing в `bot/core/dedup.py`.
3. При полном равенстве (одинаковый `first_seen_at`, что при `now()`-
   default статистически маловероятно, но не невозможно) — **меньший
   `property_id`** (чисто детерминированный tie-break, не оценочный).

Правило реализуется ОДНИМ SQL-запросом на группу (не веткой Python-кода
на каждый случай) — легко unit-тестируется на синтетических группах.

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
