-- Когда AI-слой (bot/core/ai_text_analysis.py) разобрал описание — для
-- будущего графика покрытия во времени на /admin/analytics/ai-analysis
-- (по тому же принципу, что floor_stats_history/ceiling_stats_history).
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS ai_analyzed_at TIMESTAMPTZ;
