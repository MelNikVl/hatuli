-- Фаза A, п.7 вердикт-стратегии (docs/verdict_strategy.md §5, "гигиена:
-- ALTER TABLE вынести из apply_deal_scores в миграции") — единственный
-- ALTER TABLE, который раньше гонялся на КАЖДЫЙ вызов apply_deal_scores()
-- (bot/core/deal_score.py, каждый цикл парсера) вместо того, чтобы жить
-- одноразовой миграцией — колонка существует и активно используется уже
-- давно (deal_confidence — "полнота данных" в UI, см. Фаза A п.5), ALTER
-- ... IF NOT EXISTS был безвредным, но лишним no-op на живой таблице
-- на каждом цикле.
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS deal_confidence INT;
