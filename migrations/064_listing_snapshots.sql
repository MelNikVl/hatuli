-- Фаза A, п.1 вердикт-стратегии (docs/verdict_strategy.md §5) — daily-
-- снапшот listing_id/price/views/is_active поверх существующих
-- коллекторов (service_apartments.py, service_viewcount.py).
--
-- Это НЕ дублирует price_history/views_history — те event-логи разной
-- гранулярности (price_history пишет только на ИЗМЕНЕНИЕ цены;
-- views_history — на каждую проверку, но своим отдельным потоком, живёт
-- только с 2026-08-14). listing_snapshots — денормализованный снимок
-- ОДНОГО дня по всем трём полям сразу, специально под будущий расчёт
-- outcome-меток (Фаза A п.2 и далее): survival-анализу нужна равномерная
-- дневная сетка состояния объявления, а не стыковка разреженных
-- независимых event-потоков заново на каждый расчёт.
--
-- date (грань идемпотентности) + observed_at (когда реально собрано) —
-- тот же паттерн двух полей, что уже в complex_stats_history
-- (date + computed_at) — здесь по temporal_policy.md правилу 3
-- ("observed_at — на каждое СОБРАННОЕ значение") имя другое, смысл тот
-- же: "когда систему в последний раз проверяли", не "когда изменилось".
--
-- UNIQUE(listing_id, date) — повторный запуск в тот же день (ретрай
-- таймера) идемпотентен через ON CONFLICT DO UPDATE, не плодит дубли.
--
-- БЭКФИЛ НЕВОЗМОЖЕН — снимков за прошлое не существует физически,
-- накопление строго с нуля от даты запуска таймера (2026-08-14).
CREATE TABLE IF NOT EXISTS listing_snapshots (
    id          SERIAL PRIMARY KEY,
    listing_id  TEXT NOT NULL REFERENCES apartment_listings(id) ON DELETE CASCADE,
    date        DATE NOT NULL,
    price       BIGINT,
    views       INT,
    is_active   BOOLEAN,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (listing_id, date)
);
CREATE INDEX IF NOT EXISTS idx_listing_snapshots_listing ON listing_snapshots (listing_id, date);
CREATE INDEX IF NOT EXISTS idx_listing_snapshots_date ON listing_snapshots (date);

GRANT SELECT, INSERT, UPDATE, DELETE ON listing_snapshots TO krisha;
GRANT USAGE, SELECT ON SEQUENCE listing_snapshots_id_seq TO krisha;
