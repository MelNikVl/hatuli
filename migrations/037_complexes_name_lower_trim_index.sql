-- Найдено при расследовании "карта загружается медленно" (жалоба
-- пользователя + OPTIMIZATION_GUIDE.md): реальный узкий момент — НЕ
-- отсутствие индекса по lat/lon (LIMIT и так обрезает Seq Scan почти
-- мгновенно), а JOIN apartment_listings -> complexes по
-- lower(trim(cx.name)) = lower(trim(a.complex_name)) в /admin/api/map-points.
-- apartment_listings уже был проиндексирован по lower(trim(complex_name))
-- (idx_apt_complex_name_lower), но у complexes был только индекс на
-- lower(name) БЕЗ trim (idx_complexes_name_lower) — разное выражение,
-- планировщик не мог им воспользоваться и уходил в nested loop, перебирая
-- все 2781 строк complexes на каждую внешнюю строку (сотни тысяч лишних
-- сравнений). EXPLAIN ANALYZE до фикса: 18.2с на полную выдачу (LIMIT
-- 15000), 447мс на первый батч (LIMIT 300, как реально шлёт фронт). После
-- добавления этого индекса: 133мс и 31мс соответственно (~137x/14.5x).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_complexes_name_lower_trim
ON complexes (lower(trim(name)));
