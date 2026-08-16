-- Property Identity, слой 1 (задача 2026-08-16, "P1 — Property Identity")
-- — стабильный идентификатор ФИЗИЧЕСКОЙ квартиры, к которому привязываются
-- все listing_id, под которыми она когда-либо публиковалась. Проблема:
-- сейчас "квартира" = listing_id, а одна физическая квартира может быть
-- перевыставлена 3-5 раз с разными listing_id — нельзя отличить relist от
-- нового объекта, посчитать честный true DOM или собрать price timeline
-- на уровне реального объекта, не отдельного объявления.
--
-- address_hash — SHA1(нормализованный_адрес|этаж|площадь), считает
-- bot/identity/property_linker.py::compute_address_hash() (единственное
-- место, где формула должна жить — миграция её не дублирует). UNIQUE —
-- ключ дедупликации: та же тройка (адрес, этаж, площадь) — та же
-- физическая квартира. Все три компонента ОБЯЗАТЕЛЬНЫ у линковщика
-- (адрес/этаж/площадь неизвестны — квартиру НЕ линкуем вовсе, Unknown ≠
-- average, не гадаем на неполных данных) — но на уровне СХЕМЫ floor/
-- area_sqm/rooms оставлены nullable: сама таблица не обязана знать,
-- почему линковщик отказался бы, дисциплина полноты — на стороне кода.
--
-- complex_id — nullable: не у каждой квартиры есть распознанный ЖК
-- (старый фонд без записи в complexes, ЖК ещё не резолвится по имени).
CREATE TABLE IF NOT EXISTS properties (
    property_id   SERIAL PRIMARY KEY,
    complex_id    INTEGER REFERENCES complexes(id) ON DELETE SET NULL,
    address_hash  TEXT NOT NULL,
    floor         INTEGER,
    area_sqm      REAL,
    rooms         INTEGER,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (address_hash)
);

-- Поиск при fuzzy-match (тот же complex_id + этаж, площадь с допуском —
-- см. property_linker.py) — без индекса это полный скан properties на
-- каждый непойманный по точному хэшу listing.
CREATE INDEX IF NOT EXISTS idx_properties_complex_floor ON properties (complex_id, floor);

GRANT SELECT, INSERT, UPDATE, DELETE ON properties TO krisha;
GRANT USAGE, SELECT ON SEQUENCE properties_property_id_seq TO krisha;
