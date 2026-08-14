-- Скоринг доверия объявлений (задача 2026-08-13, "надо ввести скоринг
-- доверия для каждого объявления квартиры") — пока один параметр
-- (тип продавца): 1.0 Крыша Агент, 0.8 собственник, 0.6 обычный риелтор
-- без бейджа. См. bot/core/apartment_parser.py (детекция при парсинге,
-- .label-user-agent на карточке — тот же сигнал, что фильтр Крыши
-- "От Крыша Агентов", das[_sys.fromAgent]) и service_apartments.py
-- (запись в БД). seller_type уже существовал в схеме (000_core_tables.sql)
-- но никогда не заполнялся — trust_score новая.
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS trust_score NUMERIC;

-- Бэкфил уже лежащих в базе строк — is_krisha_agent нельзя восстановить
-- задним числом (нет сохранённого HTML карточки), поэтому только
-- is_owner-based приближение сейчас; настоящий seller_type/trust_score
-- дозаполнится сам при следующем re-parse каждого объявления (обычный
-- цикл парсера, service_apartments.py трогает UPDATE-веткой seller_type/
-- trust_score на каждый скан активных объявлений).
UPDATE apartment_listings
SET trust_score = CASE WHEN is_owner THEN 0.8 ELSE 0.6 END
WHERE trust_score IS NULL;

UPDATE apartment_listings
SET seller_type = CASE WHEN is_owner THEN 'owner' ELSE 'realtor' END
WHERE seller_type IS NULL;
