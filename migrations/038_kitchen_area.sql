-- Площадь кухни (м²) — извлекается в bot/core/apartment_details.py
-- (kitchen_match), но раньше нигде не сохранялась (см. задачу "кухня в
-- парсерах продажи"). Обычный ALTER, без CONCURRENTLY (см. gotcha в памяти
-- проекта — миграции идут в одной транзакции на каждом старте сервиса).
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS kitchen_area DOUBLE PRECISION;
