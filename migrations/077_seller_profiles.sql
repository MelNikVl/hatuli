-- Seller Profile — блок 7 docs/liquidity_model_design.md (§2.7 «Продавец»),
-- задача 2026-08-15. `seller_type`/`seller_name`/`is_owner` уже есть на
-- каждой строке apartment_listings (миграции 000/014/055), но НЕТ
-- агрегации на уровне продавца — сколько у него объявлений, как часто он
-- перевыставляет, снижает ли цену. §2.7 прямо называет это отдельной
-- сущностью («профиль продавца»), которой в проекте нет вовсе.
--
-- PK — seller_name, нормализованный (trim + lower + схлопнутые пробелы) —
-- НЕ телефон. §2.7 сам предполагал телефон как более стабильный
-- идентификатор («seller_name может быть неуникален»), но телефон
-- продавца СЕЙЧАС нигде не парсится: apartment_listings.seller_phone не
-- существует, Krisha прячет номер за JS-кнопкой «Показать номер» с
-- отдельным AJAX (не факт что безопасно от рейт-лимита — отдельная
-- задача на будущее, не эта). Подтверждено пользователем 2026-08-15:
-- работаем по имени сейчас, телефон — отдельным заходом (тогда
-- потребуется отдельная миграция на смену PK + бэкафилл).
--
-- Известное ограничение, не баг (см. seller_profile_snapshot.py про
-- стоп-лист generic-имён вроде "хозяин"/"продавец" — 1000+ разных людей
-- под одной строкой): частые казахские/русские имена (Асель, Динара...)
-- тоже коллизируют между разными реальными продавцами при поиске только
-- по имени — на дату задачи (2026-08-15) это единственный доступный
-- идентификатор, честно зафиксировано как источник шума в UI-копирайтинге
-- (см. блок «Продавец» в модалке объявления), не скрыто молча.
--
-- Снимок пересчитывается целиком раз в сутки (seller_profile_snapshot.py,
-- krisha-seller-profile.timer) — UPSERT по seller_name, а не append-only
-- история: как и outcome_labels (065), нужен только текущий профиль, не
-- архив предыдущих версий (temporal_policy.md правило 2 — computed_at =
-- когда последний раз пересчитано).
--
-- relist_count/relist_rate — через outcome_labels.relisted_within_60d
-- (069) на листингах этого продавца: TRUE = похожий листинг того же
-- продавца переоткрылся в течение 60 дней после архивации.
-- price_cut_count/price_cut_rate — количество листингов этого продавца
-- (не событий!) с хотя бы одним снижением цены в price_history (001) —
-- та же "count/total"-форма, что у relist_rate, ради прямой сравнимости.
-- avg_days_to_sell — среднее outcome_labels.time_on_market по архивным
-- листингам продавца (NULL у листингов без time_on_market не учитывается
-- — Unknown ≠ average, verdict_strategy.md §3.1).
-- median_discount_pct — медиана apartment_listings.bargain_discount_pct
-- (bot/core/bargain.py) среди текущих листингов продавца, statistics.
-- median() в Python, тот же метод, что deal_score.py уже использует для
-- медиан по рынку.
--
-- is_high_relist_rate — relist_rate > 0.3 И total_listings_count >= 3:
-- порог задачи применён не "в лоб" — 1 релист из 1 объявления даёт
-- rate=1.0, это шум одного случая, не поведенческий паттерн. Тот же
-- анти-шумовой принцип, что confidence в location_score.py (не отвечаем
-- уверенно на маленькой выборке).
-- is_motivated_seller — 2+ снижения цены (по ЛЮБЫМ его листингам вместе)
-- за последние 30 дней от computed_at.
CREATE TABLE IF NOT EXISTS seller_profiles (
    seller_name             TEXT PRIMARY KEY,   -- нормализованный: trim+lower+' '.join(split())
    seller_type               TEXT,

    active_listings_count       INTEGER NOT NULL DEFAULT 0,
    total_listings_count          INTEGER NOT NULL DEFAULT 0,

    relist_count                    INTEGER NOT NULL DEFAULT 0,
    relist_rate                       NUMERIC,
    price_cut_count                     INTEGER NOT NULL DEFAULT 0,
    price_cut_rate                        NUMERIC,

    avg_days_to_sell                        NUMERIC,
    median_discount_pct                       NUMERIC,

    is_high_relist_rate                         BOOLEAN NOT NULL DEFAULT FALSE,
    is_motivated_seller                           BOOLEAN NOT NULL DEFAULT FALSE,

    computed_at                                     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_seller_profiles_high_relist
    ON seller_profiles (seller_name) WHERE is_high_relist_rate;
CREATE INDEX IF NOT EXISTS idx_seller_profiles_motivated
    ON seller_profiles (seller_name) WHERE is_motivated_seller;
-- Бейдж "⚠️ агентство с >50 активными объявлениями" (UI) — быстрый скан
-- по убыванию без сортировки всей таблицы.
CREATE INDEX IF NOT EXISTS idx_seller_profiles_active_count
    ON seller_profiles (active_listings_count DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON seller_profiles TO krisha;
