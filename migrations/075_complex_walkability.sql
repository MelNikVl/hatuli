-- Фаза L3 продуктового трека «Локация» (docs/location_product_design.md
-- §3/§4 «Ходьбо-доступность (walkability)», задача 2026-08-15) —
-- реальные пешеходные маршруты вместо прямой линии (haversine), роутинг
-- через self-hosted OSRM (foot-профиль, OSM-экстракт Казахстана,
-- /home/nik/osrm, контейнер osrm-foot на 127.0.0.1:5000).
--
-- complex_walkability — append-only снимок пешеходной доступности ЖК:
-- для каждого complex_id × destination_type одна строка на прогон.
-- Писатель — complex_walkability_snapshot.py (ежемесячно, 1 число,
-- krisha-complex-walkability.timer), читатели —
-- bot/core/location_score.py::_schools_factor()/_kindergartens_factor()
-- (walking distance вместо SQL-евклидовой аппроксимации, фолбэк на
-- хаверсин если строки нет/устарела) и complex_location_detail.py
-- (риск-бейдж «Пешеходная изоляция»).
--
-- walking_distance_m/walking_duration_s — NULL, если OSRM не построил
-- маршрут (точка не снапнулась к foot-графу: новостройка, забор ЖК,
-- дыра в OSM — см. no_route_reason). NULL — валидная строка, не повод
-- не писать: «попытались, вот что реально знаем» (Unknown ≠ average,
-- docs/verdict_strategy.md §3.1), тот же принцип, что низкий confidence
-- в complex_location_scores (миграция 072).
--
-- haversine_distance_m NOT NULL — всегда известно из координат, это
-- база для ratio и фолбэк для скоринга.
--
-- ratio = walking/haversine; barrier = ratio > 1.5 — маркер физического
-- барьера (река/трасса/забор), считается писателем на дату снимка, не
-- переинтерпретируется при чтении (принцип temporal_policy.md, тот же
-- что lat/lon снимка в complex_location_scores).
--
-- dest_* — POI, оказавшийся ближайшим ПО ФАКТУ маршрута (не обязательно
-- ближайший по прямой — в этом и смысл таблицы). complex_lat/lon —
-- координаты, от которых реально считали (резолв центроида на момент
-- computed_at).
--
-- engine_version — 'osrm-foot-v1@<дата OSM-экстракта>': позволяет
-- отличить строки до/после пересборки графа, тот же смысл, что
-- score_version в complex_location_scores. git_commit —
-- bot/git_info.git_hash().
--
-- PRIMARY KEY (complex_id, destination_type, computed_at) — append-only
-- история, «текущее» = ORDER BY computed_at DESC LIMIT 1 (паттерн
-- complex_location_scores/061 complex_stats_history).
CREATE TABLE IF NOT EXISTS complex_walkability (
    complex_id           INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    destination_type     TEXT NOT NULL,        -- 'school'|'kindergarten'|'transit'|'shop'|'park'
    computed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    walking_distance_m   REAL,
    walking_duration_s   REAL,
    haversine_distance_m REAL NOT NULL,
    ratio                REAL,
    barrier              BOOLEAN,
    dest_name            TEXT,
    dest_lat             REAL,
    dest_lon             REAL,
    complex_lat          REAL,
    complex_lon          REAL,
    no_route_reason      TEXT,                 -- 'no_snap'|'no_route'|'osrm_unavailable'|NULL
    engine_version       TEXT NOT NULL,
    git_commit           TEXT,
    PRIMARY KEY (complex_id, destination_type, computed_at)
);
CREATE INDEX IF NOT EXISTS idx_complex_walkability_latest
    ON complex_walkability (complex_id, destination_type, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_complex_walkability_barrier
    ON complex_walkability (barrier) WHERE barrier;

GRANT SELECT, INSERT, UPDATE, DELETE ON complex_walkability TO krisha;
