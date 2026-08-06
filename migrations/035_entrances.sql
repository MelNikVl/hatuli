-- Подъезды (entrances) в ЖК — ручное поле для оценки населения (см. задачу
-- "внести подъезды в /admin/analytics/complexes и учитывать при оценке
-- населения"). Как и elevator_count/apartment_count — нигде не парсится
-- автоматически, вводится вручную на вкладке "Класс жилья".
ALTER TABLE housing_class_test ADD COLUMN IF NOT EXISTS entrances INTEGER;
