-- Часть 2, п.8 (задача 2026-08-14, "скоринг волна 2 — гигиена") —
-- effective_score = score_total + zone_bonus + layer_bonus +
-- price_drop_bonus считался заново в 4+ разных SQL-запросах
-- terminal_extras.py (docs/scoring_audit.md §2.4/§7 п.9), НЕ идентичных
-- копиях: /admin/api/map-points ("eff_score") дополнительно подставляет
-- primary_score_total вместо score_total для market_type='primary' —
-- остальные 5 копий (top10, complexes-map x2, min_score-фильтр) этого
-- не делали. Живая проверка на момент миграции: 43 активных primary-
-- объявления имеют score_total IS DISTINCT FROM primary_score_total —
-- не просто defensive-код, реальное расхождение сортировки/фильтра
-- между разными страницами продукта для одних и тех же объявлений.
--
-- GENERATED ALWAYS ... STORED — колонка пересчитывается автоматически
-- на каждый UPDATE влияющих полей (Postgres 12+), не требует отдельного
-- батч-джоба и не может разойтись с исходными полями.
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS effective_score INTEGER
    GENERATED ALWAYS AS (
        (CASE WHEN market_type = 'primary' AND primary_score_total IS NOT NULL
              THEN primary_score_total
              ELSE COALESCE(score_total, 0) END)
        + COALESCE(zone_bonus, 0) + COALESCE(layer_bonus, 0) + COALESCE(price_drop_bonus, 0)
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_apt_effective_score ON apartment_listings (effective_score DESC);
