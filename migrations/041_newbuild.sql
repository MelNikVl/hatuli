-- Новостройки (см. задачу "раздел Новостройки для Астаны") — прямой
-- парсинг шахматок у застройщиков (BI Group и далее), в отличие от
-- существующих korter_import.py/homsters_import.py, которые обогащают
-- complexes данными с агрегаторов, а не собирают отдельные варианты
-- квартир.
--
-- Дизайн: complexes/developers остаются единым источником правды и для
-- вторички, и для новостроек (is_newbuild — просто пометка "этот ЖК мы
-- парсим у застройщика напрямую"), а сами варианты квартир (шахматка)
-- живут в новой таблице newbuild_units — 1 строка = 1 юнит у застройщика.
--
-- Пропавшие из свежего снапшота источника юниты НЕ удаляются, а помечаются
-- status='sold' + sold_at — это и есть данные "что продаётся лучше", ради
-- которых весь раздел затевался.

ALTER TABLE complexes ADD COLUMN IF NOT EXISTS is_newbuild BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS newbuild_source TEXT;          -- 'bi_group' | 'sensata' | 'orda_invest' | 'bazis' | 'nak' | 'svoydom'
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS newbuild_source_id TEXT;       -- id ЖК в системе застройщика
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS completion_year INTEGER;       -- год сдачи (для фильтра по годам на карте)
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS completion_quarter SMALLINT;   -- 1..4, если застройщик даёт квартал
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS newbuild_description TEXT;     -- описание ЖК с сайта застройщика
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS newbuild_units_count INTEGER DEFAULT 0;  -- активных юнитов в наличии (кэш для карты/бэйджа)
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS newbuild_sold_count INTEGER DEFAULT 0;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS newbuild_last_scan_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_complexes_is_newbuild ON complexes(is_newbuild) WHERE is_newbuild;
CREATE INDEX IF NOT EXISTS idx_complexes_completion_year ON complexes(completion_year);
CREATE UNIQUE INDEX IF NOT EXISTS idx_complexes_newbuild_source_id
    ON complexes (newbuild_source, newbuild_source_id) WHERE newbuild_source_id IS NOT NULL;

-- ── Застройщики: контакт для кнопки "связаться с застройщиком" ──
-- Лого для карты не заводим отдельным полем — у developers уже есть
-- (незадокументированная, как и многое в 000_core_tables.sql) колонка
-- logo, её и используем (см. /admin/api/complex-summary/{id} в
-- terminal_extras.py, она её уже читает).
ALTER TABLE developers ADD COLUMN IF NOT EXISTS sales_phone TEXT;

-- ── Варианты квартир в новостройках (шахматка) ──────────────────────────
CREATE TABLE IF NOT EXISTS newbuild_units (
    id               SERIAL PRIMARY KEY,
    complex_id       INTEGER REFERENCES complexes(id),
    source           TEXT NOT NULL,           -- 'bi_group' и т.д. (дублирует complexes.newbuild_source — для дедупа без join)
    source_unit_id   TEXT NOT NULL,           -- id квартиры/лота в системе застройщика

    building         TEXT,                    -- корпус
    section          TEXT,                    -- секция/подъезд
    floor             INTEGER,
    floors_total       INTEGER,
    rooms              INTEGER,
    area                REAL,
    price               BIGINT,
    price_per_m2          NUMERIC,

    layout_photo_url        TEXT,             -- фото планировки (шахматка)
    photos                   JSONB,            -- доп. фото (рендеры ЖК и т.п., если есть)

    status                    TEXT NOT NULL DEFAULT 'available',  -- available | reserved | sold
    first_seen_at              TIMESTAMPTZ DEFAULT now(),
    last_seen_at                 TIMESTAMPTZ DEFAULT now(),
    sold_at                        TIMESTAMPTZ,

    raw_json                        JSONB,     -- сырой ответ источника (на будущее — новые поля без миграций)
    created_at                        TIMESTAMPTZ DEFAULT now(),
    updated_at                          TIMESTAMPTZ DEFAULT now(),

    UNIQUE (source, source_unit_id)
);
CREATE INDEX IF NOT EXISTS idx_newbuild_units_complex ON newbuild_units(complex_id);
CREATE INDEX IF NOT EXISTS idx_newbuild_units_status ON newbuild_units(status);
CREATE INDEX IF NOT EXISTS idx_newbuild_units_rooms ON newbuild_units(complex_id, rooms) WHERE status = 'available';

-- ── История цены юнита (застройщики поднимают цену по мере строительства) ──
CREATE TABLE IF NOT EXISTS newbuild_unit_price_history (
    id          SERIAL PRIMARY KEY,
    unit_id     INTEGER NOT NULL REFERENCES newbuild_units(id),
    price       BIGINT,
    changed_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_newbuild_price_history_unit ON newbuild_unit_price_history(unit_id);
