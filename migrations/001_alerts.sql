-- ============================================================
--  Миграция 001: история цен + индексы для платных алертов
--  Применить: psql -U krisha -d krisha_bot -h localhost -f migrations/001_alerts.sql
--  Идемпотентна — можно запускать повторно.
-- ============================================================

-- История изменения цены по каждому объявлению.
-- Снижение цены = сильнейший сигнал для торга и главная киллер-фича алертов.
CREATE TABLE IF NOT EXISTS price_history (
    id          SERIAL PRIMARY KEY,
    listing_id  TEXT NOT NULL,
    old_price   BIGINT NOT NULL,
    new_price   BIGINT NOT NULL,
    changed_at  TIMESTAMPTZ DEFAULT NOW(),
    alerted     BOOLEAN DEFAULT FALSE          -- разослан ли алерт об этом изменении
);

CREATE INDEX IF NOT EXISTS idx_price_history_listing ON price_history (listing_id);
CREATE INDEX IF NOT EXISTS idx_price_history_alerted ON price_history (alerted) WHERE alerted = FALSE;

-- Быстрый выбор неразосланных топ-объектов
CREATE INDEX IF NOT EXISTS idx_apartment_notified
    ON apartment_listings (notified) WHERE notified = FALSE;

-- Быстрый выбор активных подписчиков
CREATE INDEX IF NOT EXISTS idx_users_subscription
    ON users (subscription_end) WHERE subscription_end IS NOT NULL;
