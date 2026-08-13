-- Чинит владельца таблиц Фазы 2 (юниты) — задача 2026-08-13, по следам
-- живого инцидента при 051 (см. docs/entity_resolution_plan.md,
-- "2026-08-13: review-UX для unit_duplicate_candidates + gold labels"):
-- 049/050 были применены вручную через `sudo -u postgres psql`, а не
-- через штатный `bot/db/pg.py::_apply_migrations()` (тот работает под
-- пользователем `krisha` при каждом `init_pool()`) — обе таблицы (и их
-- SERIAL-последовательности) остались с владельцем `postgres`, а не
-- `krisha`. Пока это никого не задевало (GRANT SELECT/INSERT/UPDATE/
-- DELETE уже был выдан 049/050 корректно — DML работает), но ЛЮБАЯ
-- будущая миграция, ссылающаяся на них через FK (как 051 сегодня) —
-- падает `permission denied for table ...` при попытке `krisha`
-- применить её автоматически (REFERENCES не входит в базовый GRANT
-- набор и не наследуется не-владельцем). Раз столкнулись — чиним ВСЕ
-- таблицы Фазы 2 (юниты) разом, не только ту, что зацепило.
--
-- ALTER TABLE/SEQUENCE ... OWNER TO — идемпотентно (повторный прогон
-- с тем же новым владельцем не ошибка), и после первого прогона (эта
-- миграция применяется вручную через postgres ОДИН раз, см. ниже)
-- становится безопасным для штатного автопримения под `krisha` —
-- владелец, меняющий OWNER на самого себя, не требует суперюзера,
-- только членства в новой роли (тривиально верно для самой себя).
ALTER TABLE IF EXISTS unit_duplicate_candidates OWNER TO krisha;
ALTER SEQUENCE IF EXISTS unit_duplicate_candidates_id_seq OWNER TO krisha;
ALTER TABLE IF EXISTS unit_source_links OWNER TO krisha;
ALTER SEQUENCE IF EXISTS unit_source_links_id_seq OWNER TO krisha;
ALTER TABLE IF EXISTS unit_match_gold_labels OWNER TO krisha;
ALTER SEQUENCE IF EXISTS unit_match_gold_labels_id_seq OWNER TO krisha;

-- Явные GRANT'ы — избыточно (владелец и так имеет все права), но
-- держим явными по тому же принципу, что 049/050/051: права роли
-- krisha читаются из файла миграции, а не подразумеваются владением.
GRANT SELECT, INSERT, UPDATE, DELETE ON unit_duplicate_candidates TO krisha;
GRANT USAGE, SELECT ON SEQUENCE unit_duplicate_candidates_id_seq TO krisha;
GRANT SELECT, INSERT, UPDATE, DELETE ON unit_source_links TO krisha;
GRANT USAGE, SELECT ON SEQUENCE unit_source_links_id_seq TO krisha;
GRANT SELECT, INSERT, UPDATE, DELETE ON unit_match_gold_labels TO krisha;
GRANT USAGE, SELECT ON SEQUENCE unit_match_gold_labels_id_seq TO krisha;
