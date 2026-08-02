-- Размер (грузоподъёмность) лифтов — второй параметр наряду с их
-- количеством для скора housing_class_test (см. bot/core/housing_class_score.py).
ALTER TABLE housing_class_test ADD COLUMN IF NOT EXISTS elevator_capacity_kg INTEGER;
