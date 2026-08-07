-- Тепловая карта преступности (см. задачу "тепловая карта преступности") —
-- источник krisha.kz/ms/geodata/crime (тот же API, что у кнопки
-- "Преступность" на их /map/). Только локация + тип, без адресов/деталей
-- (не нужны для тепловой карты). Уникальность — по естественному ключу
-- (координата+дата+тип), т.к. у API нет собственного id записи.
CREATE TABLE IF NOT EXISTS crime_incidents (
    id SERIAL PRIMARY KEY,
    lat DOUBLE PRECISION NOT NULL,
    lon DOUBLE PRECISION NOT NULL,
    crime_title TEXT,
    hard_code SMALLINT,
    date_excitation DATE,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS crime_incidents_natural_key
    ON crime_incidents (lat, lon, date_excitation, crime_title);
CREATE INDEX IF NOT EXISTS crime_incidents_date_idx ON crime_incidents (date_excitation);
