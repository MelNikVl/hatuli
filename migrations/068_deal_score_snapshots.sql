-- Фаза A.5, п.2 вердикт-стратегии (docs/verdict_strategy.md, задача
-- 2026-08-14) — исторический журнал Deal Score НА МОМЕНТ наблюдения, не
-- только текущий срез в apartment_listings.score_total/hex_details.
-- Причина: baseline_measure.py (Фаза A, п.3) вынужденно мерил AUC против
-- ТЕКУЩЕГО score_total на объявлениях, которые могли архивироваться
-- ДАВНО — то есть сравнивал сегодняшний скор (посчитанный по сегодняшней
-- формуле) с исходом, случившимся неделями раньше, при другой формуле и
-- других вводных. deal_score_snapshots делает эту связь честной:
-- каждая строка — что РЕАЛЬНО было посчитано и КОГДА, с версией формулы
-- и git-коммитом, чтобы будущий baseline мог отобрать только снапшоты,
-- сделанные ДО начала outcome-окна (temporally_safe, см. baseline_
-- measure.py).
--
-- Append-only (НЕ upsert по listing_id+date) — сознательно: observed_at
-- (когда наблюдали состояние) и created_at (когда физически вставлена
-- строка) разведены на случай будущего ручного бэкфила/исправления, где
-- они могут не совпадать; ежедневный таймер и так производит по одной
-- строке на объявление в сутки самой своей частотой, идемпотентность
-- через UNIQUE не требуется (тот же аргумент, что у views_history —
-- "событие = сам факт наблюдения").
--
-- score_version/git_commit — версия формулы (bot/core/deal_score.py
-- compute_deal_scores() уже возвращает "version": 4 в каждой записи) и
-- короткий git-хэш (bot/git_info.git_hash(), тот же механизм, что
-- service_apartments.py/service_web.py используют в логах при старте,
-- docs/process.md "Deploy-чеклист") — без этой пары нельзя отличить
-- "формула не менялась, вводные другие" от "формула изменилась" при
-- сравнении двух снапшотов одного объявления.
--
-- risk_flags — JSONB-копия hex_details.flags (Фаза A п.6) на момент
-- снимка, не только агрегированный risk_score.
--
-- inputs_hash — md5 от price/area/rooms/floor/floors_total/complex_name/
-- resolved_house_id/finish_level/is_active НА МОМЕНТ снимка — позволяет
-- отличить "скор пересчитан, потому что вводные объявления изменились"
-- от "скор устарел, вводные те же, просто давно не пересчитывался" при
-- последующем анализе истории снимков одного listing_id.
--
-- bargain_target_price/discount_pct/analogs_count/method — снимок с
-- apartment_listings.bargain_target/bargain_discount_pct/comparables_cnt/
-- bargain_method (последние два — задача Фазы A.5 п.2, "живой код,
-- мёртвый эффект", см. соседний коммит) — торг тоже часть вердикта, не
-- только Deal Score.
CREATE TABLE IF NOT EXISTS deal_score_snapshots (
    id                    SERIAL PRIMARY KEY,
    listing_id            TEXT NOT NULL REFERENCES apartment_listings(id) ON DELETE CASCADE,
    observed_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    score_version         INT,
    git_commit            TEXT,
    score_total           INT,
    price_score           INT,
    quality_score         INT,
    market_score          INT,
    risk_score            INT,
    risk_flags            JSONB,
    deal_confidence       INT,
    data_completeness     INT,
    bargain_target_price  INT,
    bargain_discount_pct  REAL,
    bargain_analogs_count INT,
    bargain_method        TEXT,
    inputs_hash           TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_deal_score_snapshots_listing ON deal_score_snapshots (listing_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_deal_score_snapshots_observed_at ON deal_score_snapshots (observed_at);
CREATE INDEX IF NOT EXISTS idx_deal_score_snapshots_version ON deal_score_snapshots (score_version);

GRANT SELECT, INSERT, UPDATE, DELETE ON deal_score_snapshots TO krisha;
GRANT USAGE, SELECT ON SEQUENCE deal_score_snapshots_id_seq TO krisha;
