-- air_stations / air_quality_astana / air_grid — воздух Астаны, три
-- источника, ни один не имел миграции (владелец в pg_tables у всех
-- троих — `postgres`, не `krisha`, как у таблиц, заведённых через
-- нормальный путь _apply_migrations() в bot/db/pg.py). Это тот же
-- исторический прецедент, что developer_reviews (миграция 078) сама
-- ссылалась как на "урок air-таблиц" — оказалось, что урок был
-- сформулирован в докстрингах/докладах (см. docs/liquidity_model_design.md,
-- docs/strategic_independence.md), но фактически не закрыт: файла
-- миграции для самих air-таблиц как не было, так и не появилось до
-- этой задачи (2026-08-15). Закрываем долг тем же способом, что 078:
-- CREATE TABLE IF NOT EXISTS ровно по живой схеме, без новых индексов
-- сверх того, что реально есть в проде.
--
-- ── air_stations — почасовые станции ПНЗ Казгидромета (ecodata.kz) ──
-- Писатель: pnz_collect.py (krisha-air-stations.timer, ежечасно).
-- Читатель: bot/core/location_score.py::_air_quality_factor()
-- (_GROUPS['risk']) — DISTINCT ON (station_name) latest + ближайшая по
-- координатам, индекс не нужен под этот паттерн при текущем объёме
-- (295 строк на 10 станций на 2026-08-15, полный скан копеечный).
-- UNIQUE(station_name, ts) — append-only по конструкции (тот же
-- источник может прислать то же измерение повторно, не дублируем).
-- index_value/index_pollutant — max(факт/ПДК) по загрязнителям и какой
-- именно загрязнитель дал максимум (см. докстринг pnz_collect.py).
CREATE TABLE IF NOT EXISTS air_stations (
    id              SERIAL PRIMARY KEY,
    station_name    TEXT NOT NULL,
    address         TEXT,
    lat             NUMERIC,
    lon             NUMERIC,
    ts              TIMESTAMPTZ,
    values_json     JSONB,
    index_value     NUMERIC,
    index_pollutant TEXT,
    fetched_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (station_name, ts)
);

-- ── air_quality_astana — ежемесячные сводки Казгидромета (data.egov.kz) ──
-- Писатель: air_collect.py. НЕ подключена как фактор location_score
-- (см. liquidity_model_design.md §2.6) — пока только отдельная сводка,
-- город × загрязнитель за версию датасета (min/max концентрация,
-- превышения ПДК/ПДК5/ПДК10). UNIQUE(version, pollutant) — одна строка
-- на загрязнитель за версию (повторный сбор той же версии не дублирует).
CREATE TABLE IF NOT EXISTS air_quality_astana (
    id           SERIAL PRIMARY KEY,
    version      INTEGER NOT NULL,
    pollutant    TEXT NOT NULL,
    min_conc     NUMERIC,
    max_conc     NUMERIC,
    excess_lc    NUMERIC,
    excess_mc    NUMERIC,
    excess_count INTEGER,
    excess5      INTEGER,
    excess10     INTEGER,
    fetched_at   TIMESTAMPTZ DEFAULT now(),
    UNIQUE (version, pollutant)
);

-- ── air_grid — модельная сетка CAMS/Open-Meteo (не Казгидромет) ──
-- Писатель: air_grid_collect.py (krisha-airgrid.timer, каждые 3ч).
-- disabled/inactive на 2026-08-15 (см. docs/location_product_design.md
-- §"Воздух / экология") — читается только тепловой картой в дашборде,
-- НЕ фактором location_score. Без UNIQUE — сетка перезаписывается целиком
-- при каждом сборе (те же lat/lon могут повторяться между прогонами),
-- поведение писателя этой миграцией не меняется, только фиксируется
-- живая схема.
CREATE TABLE IF NOT EXISTS air_grid (
    id         SERIAL PRIMARY KEY,
    lat        NUMERIC,
    lon        NUMERIC,
    aqi        NUMERIC,
    pm25       NUMERIC,
    pm10       NUMERIC,
    no2        NUMERIC,
    o3         NUMERIC,
    fetched_at TIMESTAMPTZ
);

GRANT SELECT, INSERT, UPDATE, DELETE ON air_stations TO krisha;
GRANT USAGE, SELECT ON SEQUENCE air_stations_id_seq TO krisha;
GRANT SELECT, INSERT, UPDATE, DELETE ON air_quality_astana TO krisha;
GRANT USAGE, SELECT ON SEQUENCE air_quality_astana_id_seq TO krisha;
GRANT SELECT, INSERT, UPDATE, DELETE ON air_grid TO krisha;
GRANT USAGE, SELECT ON SEQUENCE air_grid_id_seq TO krisha;
