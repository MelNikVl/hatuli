-- Ключевые слова, которые админ может добавлять/удалять руками через
-- /admin/analytics/ai-analysis — используются как доп. сигнал для
-- отделки (finish_classify.py) и как живой счётчик упоминаний для
-- квартирных/ЖК-признаков (см. bot/admin_web.py ai_analysis_status_page).
CREATE TABLE IF NOT EXISTS ai_keywords (
    id SERIAL PRIMARY KEY,
    category TEXT NOT NULL,
    word TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(category, word)
);
