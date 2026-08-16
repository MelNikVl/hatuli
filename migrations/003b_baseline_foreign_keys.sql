-- Ретроспективный baseline (задача 2026-08-16, "P0 — Integrity"), продолжение
-- migrations/000_baseline_*.sql — 11 внешних ключей, снятых с живой БД тем
-- же pg_dump'ом, что и сами таблицы. Вынесены в ОТДЕЛЬНЫЙ файл, а не
-- вписаны inline в CREATE TABLE (как остальные PK/UNIQUE в 000_baseline_*),
-- по одной причине: zone_favorites_zone_id_fkey ссылается на priority_zones,
-- которую создаёт migrations/003_active_zones_complexes.sql — файл "000_"
-- применяется РАНЬШЕ 003, priority_zones там ещё не существует. Остальные
-- 10 FK физически могли бы жить inline (их таблицы-цели — complexes/
-- developers из 000_core_tables.sql либо users/banks из этого же baseline),
-- но ради единообразия и одного места, где отслеживаются все FK
-- ретроспективного baseline, оставлены здесь тоже.
--
-- Имя файла "003b_..." — сортируется строго после "003_active_zones_
-- complexes.sql" и строго до "004_...": сравнение по символу №4 ('b' vs
-- '_' одинаковое "003"-начало, но "003" < "004" уже решает дело для ЛЮБОГО
-- суффикса после "003") — bot/db/pg.py::_apply_migrations() применяет файлы
-- строго по сортировке имени.
--
-- DROP CONSTRAINT IF EXISTS перед ADD CONSTRAINT — идемпотентность без
-- DO-блока: в Postgres нет ADD CONSTRAINT IF NOT EXISTS, тот же приём уже
-- использован в migrations/059_unit_duplicate_candidates_unique.sql. На
-- проде (констрейнт уже стоит из исходного "ручного" создания таблицы) —
-- DROP+ADD пересоздаёт тот же констрейнт с тем же именем, без потери
-- целостности (внутри одной транзакции миграции). На пустой БД — просто
-- создаёт.
--
-- 3 из исходных 11 FK (complex_materials/developer_programs/
-- mortgage_programs) сюда НЕ включены: эти три таблицы на проде
-- принадлежат роли postgres, не krisha (как и listing_floorplans/
-- backup_history выше по той же причине) — DROP/ADD CONSTRAINT без
-- владения падает InsufficientPrivilegeError, проверено эмпирически на
-- копии прода. Все три FK уже стоят на проде под этими именами (сняты
-- вместе с остальной схемой при генерации baseline). На пустой БД (CI)
-- эти 3 констрейнта не появятся — таблицы и колонки будут корректны и
-- рабочи, просто без FK-проверки на уровне БД для этих трёх связей.
ALTER TABLE complex_concrete_rebar DROP CONSTRAINT IF EXISTS complex_concrete_rebar_complex_id_fkey;
ALTER TABLE complex_concrete_rebar ADD CONSTRAINT complex_concrete_rebar_complex_id_fkey FOREIGN KEY (complex_id) REFERENCES complexes(id) ON DELETE CASCADE;
ALTER TABLE complex_doors DROP CONSTRAINT IF EXISTS complex_doors_complex_id_fkey;
ALTER TABLE complex_doors ADD CONSTRAINT complex_doors_complex_id_fkey FOREIGN KEY (complex_id) REFERENCES complexes(id) ON DELETE CASCADE;
ALTER TABLE complex_reviews DROP CONSTRAINT IF EXISTS complex_reviews_complex_id_fkey;
ALTER TABLE complex_reviews ADD CONSTRAINT complex_reviews_complex_id_fkey FOREIGN KEY (complex_id) REFERENCES complexes(id) ON DELETE CASCADE;
ALTER TABLE complex_reviews DROP CONSTRAINT IF EXISTS complex_reviews_user_id_fkey;
ALTER TABLE complex_reviews ADD CONSTRAINT complex_reviews_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(user_id);
ALTER TABLE complex_tech_specs DROP CONSTRAINT IF EXISTS complex_tech_specs_complex_id_fkey;
ALTER TABLE complex_tech_specs ADD CONSTRAINT complex_tech_specs_complex_id_fkey FOREIGN KEY (complex_id) REFERENCES complexes(id) ON DELETE CASCADE;
ALTER TABLE complex_walls DROP CONSTRAINT IF EXISTS complex_walls_complex_id_fkey;
ALTER TABLE complex_walls ADD CONSTRAINT complex_walls_complex_id_fkey FOREIGN KEY (complex_id) REFERENCES complexes(id) ON DELETE CASCADE;
ALTER TABLE complex_windows DROP CONSTRAINT IF EXISTS complex_windows_complex_id_fkey;
ALTER TABLE complex_windows ADD CONSTRAINT complex_windows_complex_id_fkey FOREIGN KEY (complex_id) REFERENCES complexes(id) ON DELETE CASCADE;
ALTER TABLE zone_favorites DROP CONSTRAINT IF EXISTS zone_favorites_zone_id_fkey;
ALTER TABLE zone_favorites ADD CONSTRAINT zone_favorites_zone_id_fkey FOREIGN KEY (zone_id) REFERENCES priority_zones(id) ON DELETE CASCADE;
