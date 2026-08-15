-- Реестр КЖК (задача 2026-08-15) — блок 7 docs/liquidity_model_design.md
-- (юридические риски первички: БВУ/КЖК/МИО) и механизм §3.3 docs/
-- strategic_independence.md (данные без Крыши).
--
-- Источник — https://developers.kz/market/proverit-zastroyshika, ЕДИНСТВЕННАЯ
-- нужная страница: весь реестр (313 записей на дату разведки) отдаётся
-- одним GET-запросом, встроен в HTML как JSON (<script id="regBase">),
-- не требует пагинации/Playwright/повторных запросов на проект.
--
-- **КРИТИЧНО: схема НА УРОВНЕ ЗАСТРОЙЩИКА, не проекта.** Первая версия
-- задачи предполагала project_address/warranty_number/apartments_total/
-- apartments_sold/status по КАЖДОМУ ЖК — таких полей на сайте физически
-- нет (проверено вручную перед миграцией, не догадка). Источник даёт:
-- БИН+юрлицо+бренд застройщика, схему гарантии (БВУ/МИО/КЖК) НА ВСЕГО
-- застройщика разом, счётчики объектов по городам, статус чёрного
-- списка, и ИНОГДА (142 записи из 313) — голый список названий ЖК без
-- какой-либо дополнительной детализации. Заводить колонки под
-- недоступные поля значило бы держать вечный NULL, выдавая его за
-- "просто ещё не собрали" — Unknown ≠ average, не гадаем.
--
-- is_blacklisted = сырое поле "flagged" с источника КАК ЕСТЬ, не AND с
-- in_registry (проверено на разведке: 139 записей "не в реестре +
-- flagged" — основной чёрный список developers.kz; отдельно есть 3
-- записи "в реестре, но тоже flagged" — пограничный случай, ОБЕ
-- колонки хранятся раздельно, не схлопнуты в одну — is_blacklisted=true
-- у всех 142 flagged-записей разом, in_registry разводит "чисто чёрный
-- список" от "формально в реестре, но с красным флагом" — схлопывание
-- в AND потеряло бы этот пограничный случай вовсе, показывая его как
-- обычную нормальную запись).
--
-- developer_id/developer_match_method — результат matching'а уровня 1
-- (по БИН — надёжно; по fuzzy-имени — не надёжно, помечено методом).
-- zhk_matches — результат matching'а уровня 2 (по редким zhk_names),
-- JSONB-массив [{"name","complex_id","method","confidence"}] — не
-- отдельная таблица: поле само по себе редкое (142/313 застройщиков),
-- как правило 1 элемент, join-таблица ради этого избыточна.
--
-- source_snapshot_date — дата "Данные обновлены ..." С САМОГО САЙТА
-- (не когда МЫ сходили) — сайт обновляется нечасто (в разведке снапшот
-- был датирован на 2.5 недели раньше даты разведки), эта дата — то,
-- НА КОГДА реально актуальны данные, fetched_at — когда МЫ их забрали
-- (тот же принцип разделения "факт vs момент наблюдения", что уже
-- применён в deal_score_snapshots.observed_at/created_at).
CREATE TABLE IF NOT EXISTS kzk_registry (
    id                    SERIAL PRIMARY KEY,
    bin                   TEXT NOT NULL,
    developer_legal       TEXT NOT NULL,
    developer_brand       TEXT,
    cities                JSONB,
    objects_count         INT,
    zhk_count             INT,
    by_city               JSONB,
    warranty_scheme       TEXT,
    is_blacklisted        BOOLEAN NOT NULL DEFAULT FALSE,
    in_registry           BOOLEAN NOT NULL,
    zhk_names             JSONB,
    phone                 TEXT,
    developer_id          INT REFERENCES developers(id) ON DELETE SET NULL,
    developer_match_method TEXT,
    zhk_matches           JSONB,
    source_snapshot_date  DATE,
    fetched_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bin)
);
CREATE INDEX IF NOT EXISTS idx_kzk_registry_brand ON kzk_registry (developer_brand);
CREATE INDEX IF NOT EXISTS idx_kzk_registry_blacklisted ON kzk_registry (is_blacklisted) WHERE is_blacklisted;
CREATE INDEX IF NOT EXISTS idx_kzk_registry_developer_id ON kzk_registry (developer_id) WHERE developer_id IS NOT NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON kzk_registry TO krisha;
GRANT USAGE, SELECT ON SEQUENCE kzk_registry_id_seq TO krisha;

-- developers.bin — для надёжного matching'а уровня 1 (БИН, не только
-- fuzzy по name/aliases, которые уже были единственным способом связать
-- developers с чем-либо внешним). НЕ заполняется этой миграцией — сама
-- задача разведки не давала БИН по существующим developers, только по
-- kzk_registry; заполнение — со стороны match_kzk_to_complexes() при
-- первом успешном BIN-совпадении по имени.
ALTER TABLE developers ADD COLUMN IF NOT EXISTS bin TEXT;
CREATE INDEX IF NOT EXISTS idx_developers_bin ON developers (bin) WHERE bin IS NOT NULL;
