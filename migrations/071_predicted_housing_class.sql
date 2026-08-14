-- Фаза B, п.3 вердикт-стратегии (задача 2026-08-14, docs/verdict_strategy.md)
-- — класс-модель ЖК (bot/core/housing_class_model.py): обучена на
-- ручных лейблах complexes.housing_class (273 из 2097 живых, не-garbage
-- комплексов на дату задачи), применена ко ВСЕМ комплексам с известными
-- признаками (avg_price_m2, year_built).
--
-- **НЕ путать с `housing_class_estimate`** (Часть 2 п.11,
-- `housing_class_estimate_computed_at`) — та колонка: разовый
-- heuristic-бэкфил (медианная цена/м² + высота потолков, порог правило,
-- заморожен с 2026-08-01) от отдельной задачи, не модель. Эта колонка —
-- Naive Bayes классификатор (log(avg_price_m2) + year_built),
-- обучаемый/переобучаемый заново по мере роста разметки, с честной
-- вероятностью предсказания, не эвристическое правило. Обе колонки
-- сосуществуют осознанно (тот же принцип, что "четыре понятия
-- confidence", scoring_audit.md §5.0 — не объединяем то, что у него
-- разная природа).
--
-- **Назначение** (задача 2026-08-14, честно ограничено в доке) —
-- подготовка данных для будущей стратификации аналогов и ML-моделей
-- Фазы C, НЕ попытка поднять price_score AUC прямо сейчас (тот вопрос
-- уже проверен честным backtest Фазы B п.2 — упирается не в качество
-- пула аналогов).
--
-- predicted_housing_class_source: 'manual' (housing_class уже заполнен
-- руками — предсказание не считается, поле пусто, ручная разметка
-- приоритетнее) | 'predicted' (модель дала предсказание) | NULL
-- (признаков недостаточно — avg_price_m2 или year_built неизвестны,
-- Unknown ≠ average, не гадаем).
-- predicted_housing_class_computed_at — по temporal_policy.md правилу 2
-- (обязателен на любую НОВУЮ вычисляемую/пересчитываемую колонку с
-- самого начала, урок Г3/housing_class_estimate).
ALTER TABLE complexes
    ADD COLUMN IF NOT EXISTS predicted_housing_class TEXT,
    ADD COLUMN IF NOT EXISTS predicted_housing_class_probability REAL,
    ADD COLUMN IF NOT EXISTS predicted_housing_class_source TEXT,
    ADD COLUMN IF NOT EXISTS predicted_housing_class_computed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_complexes_predicted_housing_class ON complexes (predicted_housing_class);
