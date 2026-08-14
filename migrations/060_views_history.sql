-- Г2 (задача 2026-08-14, docs/data_collection_audit.md) — apartment_
-- listings.views_count/.views_count_updated_at хранят только ТЕКУЩИЙ
-- снимок (когда в последний раз проверяли, сколько было) — динамику
-- просмотров (график "просмотры по дням") восстановить нельзя. append-
-- only журнал по образцу price_history/rental_price_history/
-- newbuild_unit_price_history — тот же принцип "текущее значение в
-- колонке + полная история в отдельной таблице", просто для просмотров,
-- не для цены. Пишется service_viewcount.py на КАЖДЫЙ успешный прогон
-- (не только на изменение — в отличие от price_history, где событие =
-- смена цены, здесь "событие" = сам факт наблюдения, ноль-разностные
-- точки тоже нужны для честного графика "не изменилось между X и Y").
--
-- Гейт (решение заказчика 2026-08-14): через 7 дней — отчёт "сколько
-- строк в истории, покрытие по объявлениям".
CREATE TABLE IF NOT EXISTS views_history (
    id          SERIAL PRIMARY KEY,
    listing_id  TEXT NOT NULL REFERENCES apartment_listings(id) ON DELETE CASCADE,
    views_count INT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_views_history_listing ON views_history (listing_id, observed_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON views_history TO krisha;
GRANT USAGE, SELECT ON SEQUENCE views_history_id_seq TO krisha;
