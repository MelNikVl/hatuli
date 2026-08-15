-- is_ambiguous — правка is_motivated_seller/is_high_relist_rate для
-- амбивалентных имён продавца (§2.7 liquidity_model_design.md, задача
-- 2026-08-15, находка по факту прогона seller_profile_snapshot.py на
-- живых данных: 837 из 1249 срабатываний is_motivated_seller приходились
-- на продавцов с total_listings_count>5 — то есть в основном на
-- коллизию РАЗНЫХ реальных людей под одним частым именем
-- ('Асель'/'Динара'/... — см. докстринг миграции 077), а не на реальное
-- поведение одного продавца.
--
-- Порог >15 УЖЕ использовался как мягкая UI-оговорка (bot/core/
-- listing_detail.py, "имя встречается часто, статистика может
-- относиться к нескольким людям") — здесь формализован в колонку и
-- стал ЖЁСТКИМ правилом на уровне снапшота (seller_profile_snapshot.py
-- ::_aggregate()), не только текстом в UI: для is_ambiguous=TRUE
-- is_high_relist_rate и is_motivated_seller принудительно FALSE — на
-- выборке из >15 вероятно разных людей эти два поведенческих сигнала
-- "этот ПРОДАВЕЦ так себя ведёт" не значат ничего осмысленного про
-- ОДНОГО человека. relist_rate/price_cut_rate САМИ ЧИСЛА не занулены
-- (честная агрегированная статистика по имени остаётся видна), занулены
-- только два производных "уверенных" вывода из неё.
ALTER TABLE seller_profiles ADD COLUMN IF NOT EXISTS is_ambiguous BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_seller_profiles_ambiguous
    ON seller_profiles (seller_name) WHERE is_ambiguous;
