-- Г3 (задача 2026-08-14, docs/data_collection_audit.md) — complexes.
-- avg_price_m2/avg_yield/listings_count перезаписываются каждый цикл
-- парсера (service_apartments.py) БЕЗ истории — нельзя ответить "как
-- менялась медианная цена/м² по ЖК за N месяцев" без болезненной
-- реконструкции задним числом. Ежедневный снимок вместо этого —
-- дешёвая широкая таблица, раз в сутки (не при каждом цикле парсера).
--
-- complex_id — FK на конкретный complex_id (ЖК ИЛИ дом зонтика, снимок
-- делается по одному и тому же паттерну "имя ИЛИ resolved_house_id",
-- что и everywhere в проекте после урока волны 1 скоринга — House-
-- resolution в скоринге, см. scoring_roadmap.md).
--
-- avg_yield снимается НЕ из complexes.avg_yield — та колонка существует
-- в схеме, но не имеет ни одного живого писателя (только читается для
-- сортировки на /complexes) — снимок считает AVG(yield_pct) заново из
-- apartment_listings, как и живая страница ЖК, а не копирует вечный
-- NULL из мёртвой колонки.
--
-- UNIQUE(complex_id, date) — повторный запуск в тот же день (ручной
-- перезапуск/retry таймера) идемпотентен через ON CONFLICT DO UPDATE,
-- не плодит дубли снимков на одну дату.
--
-- Гейт (решение заказчика 2026-08-14): через 30 дней — первый график
-- динамики цены по ЖК.
CREATE TABLE IF NOT EXISTS complex_stats_history (
    id             SERIAL PRIMARY KEY,
    complex_id     INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    date           DATE NOT NULL,
    avg_price_m2   NUMERIC,
    avg_yield      NUMERIC,
    listings_count INT,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (complex_id, date)
);
CREATE INDEX IF NOT EXISTS idx_complex_stats_history_complex ON complex_stats_history (complex_id, date);

GRANT SELECT, INSERT, UPDATE, DELETE ON complex_stats_history TO krisha;
GRANT USAGE, SELECT ON SEQUENCE complex_stats_history_id_seq TO krisha;
