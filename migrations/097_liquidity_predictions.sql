-- Прогнозы модели ликвидности (Фаза D вердикт-стратегии, задача 2026-10-02).
-- Append-only журнал: одна строка = прогноз для объявления на дату as_of
-- конкретной версией модели. История нужна для будущей честной проверки
-- («что модель говорила тогда» vs что случилось) — тот же принцип, что
-- deal_score_snapshots (Фаза A.5). score_total не меняется (freeze §6).
CREATE TABLE IF NOT EXISTS liquidity_predictions (
    listing_id      TEXT        NOT NULL,
    as_of           DATE        NOT NULL,
    p_exit14        REAL,                 -- P(пропадёт из выдачи за 14 дней) — НЕ P(продажи)
    exit14_pct      REAL,                 -- перцентиль p_exit14 среди живых объявлений на as_of, 0..100
    p_cut30         REAL,                 -- P(снижение цены за 30 дней)
    cut30_pct       REAL,
    reasons         JSONB,                -- {"exit14": [...], "cut30": [...]} топ-вклады признаков
    model_versions  JSONB       NOT NULL, -- {"exit14": "<version>", "cut30": "<version>"}
    git_commit      TEXT,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (listing_id, as_of)
);
CREATE INDEX IF NOT EXISTS idx_liquidity_predictions_as_of ON liquidity_predictions (as_of);
