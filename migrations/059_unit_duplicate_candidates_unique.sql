-- Задача 2026-08-14: "Проверить истинные дубли: одинаковые (listing_id,
-- unit_id) в unit_duplicate_candidates — если есть, удалить и добавить
-- UNIQUE-констрейнт." Живая проверка на момент задачи (psql, ручной
-- GROUP BY ... HAVING COUNT(*) > 1) нашла 0 строк — констрейнт UNIQUE
-- (unit_id, listing_id) стоял с самого создания таблицы (см. CREATE
-- TABLE в migrations/049_unit_duplicate_candidates.sql), дублей
-- физически не могло появиться после первой строки на пару. DELETE
-- ниже — не "исправление найденной проблемы", а идемпотентная защита
-- на случай расхождения (констрейнт мог быть снят руками в обход
-- миграций где-то между 049 и этим файлом) — тот же приём (ROW_NUMBER
-- + ctid), что migrations/030_rental_index_dedup.sql; keep = наименьший
-- id (первая когда-либо заведённая пара на unit+listing).
WITH ranked AS (
  SELECT ctid, ROW_NUMBER() OVER (
    PARTITION BY unit_id, listing_id ORDER BY id
  ) AS rn
  FROM unit_duplicate_candidates
)
DELETE FROM unit_duplicate_candidates WHERE ctid IN (SELECT ctid FROM ranked WHERE rn > 1);

ALTER TABLE unit_duplicate_candidates DROP CONSTRAINT IF EXISTS unit_duplicate_candidates_unit_id_listing_id_key;
ALTER TABLE unit_duplicate_candidates ADD CONSTRAINT unit_duplicate_candidates_unit_id_listing_id_key
    UNIQUE (unit_id, listing_id);

-- Новый операционный статус 'superseded' (задача 2026-08-14, "Approve
-- одного кандидата → остальные кандидаты этого listing автоматически
-- status='superseded'", см. bot/core/entity_resolution.py:approve_unit_
-- candidate) — то же свободное TEXT-поле, без CHECK, что и review/
-- merged/rejected с самого начала (049) — миграция тут не нужна
-- технически, COMMENT — для читаемости схемы будущим читателем \d.
COMMENT ON COLUMN unit_duplicate_candidates.status IS
    'review | merged | rejected | superseded (auto-closed: another candidate for the same listing_id was approved)';
