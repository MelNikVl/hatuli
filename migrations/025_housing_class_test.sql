-- Тестовая таблица для admin-only скора "класс жилья" по ЖК.
-- Цена/м², этажность и высота потолка уже считаются на лету из
-- complexes.avg_price_m2 / apartment_listings / complex_tech_specs —
-- здесь хранится только то, чего в БД сейчас нет вообще (лифты, кол-во
-- квартир) и что вводится вручную на странице /admin/analytics/housing-class.
CREATE TABLE IF NOT EXISTS housing_class_test (
    complex_id      INTEGER PRIMARY KEY REFERENCES complexes(id) ON DELETE CASCADE,
    elevator_count  INTEGER,
    apartment_count INTEGER,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
