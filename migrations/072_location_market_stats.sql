-- Фаза L1 продуктового трека «Локация» (docs/location_product_design.md
-- §7, задача 2026-08-14) — данные и инфраструктура, БЕЗ UI (Фаза L2).
-- Закрывает п.9 «Часть 3. Локация как продуктовая ось» из
-- scoring_roadmap.md.
--
-- Три независимых прироста схемы одной миграцией (связаны одной фазой,
-- не одной таблицей — раздельные CREATE/ALTER ниже):
--
-- 1) complex_location_scores — append-only снимок локационного скора
--    ЖК/дома. НЕ дублирует bot/core/location_score.py::
--    compute_complex_location_score() и не меняет живой эндпойнт
--    /admin/api/complex/{id}/location-score (тот отдаёт сырой
--    total/factors/confidence, единственный консьюмер — complex_detail.
--    html:645, менять его формат — регрессия). Эта таблица — ОТДЕЛЬНЫЙ
--    нормализованный 0-100 срез, который пишет
--    complex_location_score_snapshot.py (Фаза L1, п.5).
--
--    score — линейный кламп total (raw Σadj факторов) в 0-100 по
--    теоретическому диапазону _TOTAL_MIN=-8/_TOTAL_MAX=24
--    (bot/core/location_score.py) — диапазон вычислен из сумм
--    минимумов/максимумов каждого фактора в score_layers/*.py на дату
--    задачи; если диапазон отдельного слоя изменится, константы надо
--    пересчитать (то же обязательство, что уже несёт _CLASS_SCORE в
--    hedonic_constants.py — комментарий-маячок в коде, не здесь).
--
--    transport_score/infra_score/noise_score/green_score/risk_score —
--    RAW Σadj по группе (см. РЕШЕНИЕ 4 плана L1: transport =
--    transit_stops+lrt_access+road_access+route_connectivity, infra =
--    schools+amenities, noise = noise, green = parks, risk =
--    demolition+building_age; bank — informational, живёт только внутри
--    breakdown, не в отдельной колонке, вне групп — он всегда adj=0).
--    Не 0-100 КАЖДАЯ — величина сигнала (не только знак) важна как
--    будущая ML-фича для Фазы C (verdict_strategy.md §6 "Связь с
--    будущей Фазой C" в location_product_design.md), нормализация в
--    группах добавила бы шум без пользы на этом этапе.
--
--    lat/lon — координаты, ПО КОТОРЫМ реально считали (снимок на
--    момент computed_at, не re-resolve центроида при каждом чтении
--    задним числом — тот же принцип temporal_policy.md, что уже
--    применён в deal_score_snapshots.inputs_hash).
--
--    confidence — то же поле, что уже возвращает
--    compute_complex_location_score() (доля факторов, реально
--    посчитанных, не по дефолту/ошибке). НИЗКИЙ confidence — валидная
--    строка, не повод не писать её вовсе: полный отказ Overpass при
--    пустом osm_cache всё равно даёт transport_hexes/demolition_houses/
--    building_age/bank факторы (они не зависят от Overpass), скрипт
--    обязан зафиксировать "попытались, вот что реально знаем" (Unknown
--    ≠ average, docs/verdict_strategy.md §3.1), а не тихо пропустить ЖК
--    как будто его не существует.
--
--    score_version/git_commit — та же пара, что уже есть в
--    deal_score_snapshots (bot/git_info.git_hash()), позволяет отличить
--    "формула не менялась" от "формула изменилась" между снимками.
--
--    PRIMARY KEY (complex_id, computed_at) — append-only история по
--    прямому требованию задачи (не overwrite), "текущий" скор ЖК =
--    ORDER BY computed_at DESC LIMIT 1.
CREATE TABLE IF NOT EXISTS complex_location_scores (
    complex_id      INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    score           INT NOT NULL,
    confidence      INT NOT NULL,
    transport_score INT,
    infra_score     INT,
    noise_score     INT,
    green_score     INT,
    risk_score      INT,
    lat             REAL,
    lon             REAL,
    breakdown       JSONB NOT NULL,
    score_version   TEXT NOT NULL,
    git_commit      TEXT,
    PRIMARY KEY (complex_id, computed_at)
);
CREATE INDEX IF NOT EXISTS idx_complex_location_scores_complex ON complex_location_scores (complex_id, computed_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON complex_location_scores TO krisha;

-- 2) complex_stats_history расширяется DOM/долей снижений цены —
--    пишет тот же ежедневный complex_stats_snapshot.py (Г3,
--    migrations/061), тот же INSERT...SELECT с приоритетом
--    resolved_house_id (listing_complex CTE), не второй писатель.
--    Тренд НЕ хранится отдельной колонкой — это уже посчитанный
--    ежедневный ряд, дельта к периоду N дней назад читается на запросе
--    (сегодняшняя строка минус строка за N дней), хранить готовую
--    дельту значило бы дублировать выводимое из уже сохранённых данных
--    (тот же принцип, что уже применён в этом файле выше к score в
--    complex_location_scores против пересчёта его на каждое чтение).
ALTER TABLE complex_stats_history
    ADD COLUMN IF NOT EXISTS avg_dom_days          NUMERIC,
    ADD COLUMN IF NOT EXISTS price_drop_share_30d   REAL,
    ADD COLUMN IF NOT EXISTS price_drop_share_60d   REAL;

-- 3) hex_market_stats — новая таблица, hex-уровень (не complex_id):
--    "перенасыщение предложением" — свойство района/гексагона, не
--    одного ЖК (см. Задача 3 плана L1). Гексагон не выразим в чистом
--    SQL (та же математика bot/core/hexgrid.py::hex_id(), что и
--    deal_score.py/bargain.py) — пишет Python-скрипт
--    hex_market_stats_snapshot.py, группируя apartment_listings по
--    hex_id(lat, lon, edge_m) в Python, не GROUP BY в SQL.
--
--    edge_m зафиксирован НА ДАТУ СНИМКА (не читается заново из
--    app_settings.HEX_EDGE_M при последующем анализе) — если ребро
--    сетки сменится, старые снимки не станут молча несопоставимы с
--    новыми под одним и тем же hex_id (тот же id при разном ребре —
--    разные физические ячейки).
--
--    resolved_house_id НЕ участвует здесь намеренно (в отличие от
--    complex_id-агрегатов выше) — это агрегация по координате самого
--    объявления, не по имени ЖК; зонтик/дом ни при чём, у объявления
--    одна пара lat/lon независимо от того, к какому ЖК резолвится его
--    complex_name.
CREATE TABLE IF NOT EXISTS hex_market_stats (
    hex_id         TEXT NOT NULL,
    date           DATE NOT NULL,
    edge_m         REAL NOT NULL,
    listings_count INT NOT NULL,
    avg_price_m2   NUMERIC,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (hex_id, date)
);
CREATE INDEX IF NOT EXISTS idx_hex_market_stats_date ON hex_market_stats (date);

GRANT SELECT, INSERT, UPDATE, DELETE ON hex_market_stats TO krisha;
