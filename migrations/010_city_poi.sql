-- 010: справочник POI города (школы, садики, вузы) из OSM
CREATE TABLE IF NOT EXISTS city_poi (
    id         SERIAL PRIMARY KEY,
    kind       TEXT NOT NULL,          -- school | kindergarten | university
    name       TEXT,
    lat        DOUBLE PRECISION NOT NULL,
    lon        DOUBLE PRECISION NOT NULL,
    address    TEXT,
    source     TEXT DEFAULT 'osm',
    extra      JSONB,
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (kind, lat, lon)
);
CREATE INDEX IF NOT EXISTS idx_city_poi_kind ON city_poi (kind);
CREATE INDEX IF NOT EXISTS idx_city_poi_geo ON city_poi (lat, lon);
