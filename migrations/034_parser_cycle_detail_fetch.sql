-- Эффективность оптимизации detail-fetch (см. задачу "оптимизация работы
-- парсеров" — пропуск дорогого detail-fetch для объявлений без изменений
-- цены/координат) — расширяем существующий parser_cycle_history снимками
-- total_seen/needs_detail_fetch/skipped_no_change за цикл. Показывается на
-- /admin/parsers?tab=recheck, секция "Нагрузка на Крышу".
ALTER TABLE parser_cycle_history ADD COLUMN IF NOT EXISTS total_seen INT;
ALTER TABLE parser_cycle_history ADD COLUMN IF NOT EXISTS needs_detail_fetch INT;
ALTER TABLE parser_cycle_history ADD COLUMN IF NOT EXISTS skipped_no_change INT;
