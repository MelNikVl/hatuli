-- Net-доходность (см. Notion "Расчет доходности"): раньше в БД хранился
-- только gross yield_pct (rent*12/price*100) — без вакантности/налога/
-- расходов на покупку. Добавляем net_yield_pct, gross_yield_pct остаётся
-- в старой колонке yield_pct для обратной совместимости (карта/скоринг,
-- которые уже читают yield_pct, продолжат работать без изменений).
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS net_yield_pct FLOAT;
