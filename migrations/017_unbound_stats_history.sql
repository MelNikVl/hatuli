-- История количества непривязанных к ЖК объявлений — для графика на /admin/unbound,
-- чтобы видеть, что число непривязанных со временем уменьшается.
CREATE TABLE IF NOT EXISTS unbound_stats_history (
    id              SERIAL PRIMARY KEY,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_active    INT NOT NULL,
    unbound         INT NOT NULL,
    unbound_coords  INT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unbound_stats_history_at ON unbound_stats_history (at);

-- Стартовая точка на графике — снимок текущего состояния на момент миграции.
INSERT INTO unbound_stats_history (total_active, unbound, unbound_coords)
SELECT
    (SELECT COUNT(*) FROM apartment_listings
     WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE),
    (SELECT COUNT(*) FROM apartment_listings
     WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
       AND (complex_name IS NULL OR btrim(complex_name) = '')),
    (SELECT COUNT(*) FROM apartment_listings
     WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
       AND (complex_name IS NULL OR btrim(complex_name) = '') AND lat IS NOT NULL);
