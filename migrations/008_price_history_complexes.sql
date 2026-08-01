-- 008: история изменений цен + живая статистика ЖК

CREATE TABLE IF NOT EXISTS price_history (
    listing_id TEXT NOT NULL,
    old_price  BIGINT,
    new_price  BIGINT,
    changed_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_price_history_at ON price_history (changed_at);
CREATE INDEX IF NOT EXISTS idx_price_history_listing ON price_history (listing_id);

-- Продано за историю мониторинга = ушло в архив
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS sold_count INTEGER DEFAULT 0;
