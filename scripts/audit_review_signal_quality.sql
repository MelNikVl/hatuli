-- Read-only audit, Stage 1.1 ("Property Identity — review calibration",
-- 2026-08-18): для каждого сигнала/комбинации сигналов среди РУЧНЫХ
-- решений (status='accepted' OR (status='rejected' AND reviewed_by IS
-- NOT NULL)) — сколько решений покрывает, сколько 'одна квартира', доля
-- подтверждений. Ничего не пишет, ничего не решает автоматически.

\echo '=== 0. Базовые числа (пересчёт после деплоя) ==='
SELECT
    count(*) AS total,
    count(*) FILTER (WHERE status='pending') AS pending,
    count(*) FILTER (WHERE status='accepted') AS accepted,
    count(*) FILTER (WHERE status='rejected' AND reviewed_by IS NULL) AS auto_rejected,
    count(*) FILTER (WHERE status='rejected' AND reviewed_by IS NOT NULL) AS manual_rejected
FROM property_match_candidates;

\echo '=== 1. Manual decisions base table (материализуется временно) ==='
DROP TABLE IF EXISTS _manual_decisions;
CREATE TEMP TABLE _manual_decisions AS
SELECT
    pmc.candidate_id,
    pmc.match_method,
    pmc.relationship_type,
    pmc.match_score,
    (pmc.status = 'accepted') AS is_accepted,
    pmc.evidence,
    pmc.conflict_reasons,
    COALESCE(jsonb_array_length(pmc.evidence->'corroborating_methods'), 1) AS n_corroborating,
    pcpe.exact_shared_count, pcpe.perceptual_shared_count, pcpe.ai_similar_count,
    pcpe.shared_unit_specific_count, pcpe.processing_status AS photo_status,
    (pmc.evidence->>'rooms_a')::int AS rooms_a, (pmc.evidence->>'rooms_b')::int AS rooms_b,
    pmc.evidence->>'house_number_a' AS house_number_a, pmc.evidence->>'house_number_b' AS house_number_b,
    (pmc.evidence->>'price_a')::numeric AS price_a, (pmc.evidence->>'price_b')::numeric AS price_b,
    (pmc.evidence->>'seller_equal')::boolean AS seller_equal,
    (pmc.evidence->>'simultaneously_active')::boolean AS simultaneously_active
FROM property_match_candidates pmc
LEFT JOIN property_candidate_photo_evidence pcpe ON pcpe.candidate_id = pmc.candidate_id
WHERE pmc.status = 'accepted' OR (pmc.status = 'rejected' AND pmc.reviewed_by IS NOT NULL);

SELECT count(*) AS manual_decisions_total, count(*) FILTER (WHERE is_accepted) AS accepted_count
FROM _manual_decisions;

\echo '=== 2. По основному match_method (кандидат мог быть найден несколькими методами -> corroborating_methods ниже отдельно) ==='
SELECT match_method,
       count(*) AS n,
       count(*) FILTER (WHERE is_accepted) AS accepted,
       round(100.0 * count(*) FILTER (WHERE is_accepted) / count(*), 1) AS pct_accepted
FROM _manual_decisions GROUP BY match_method ORDER BY n DESC;

\echo '=== 3. exact_hash участвует (основной ИЛИ corroborating) ==='
SELECT
    count(*) FILTER (WHERE match_method='exact_hash' OR evidence->'corroborating_methods' ? 'exact_hash') AS n,
    count(*) FILTER (WHERE (match_method='exact_hash' OR evidence->'corroborating_methods' ? 'exact_hash') AND is_accepted) AS accepted
FROM _manual_decisions;

\echo '=== 4. dedup_listings участвует ==='
SELECT
    count(*) FILTER (WHERE match_method='dedup_listings' OR evidence->'corroborating_methods' ? 'dedup_listings') AS n,
    count(*) FILTER (WHERE (match_method='dedup_listings' OR evidence->'corroborating_methods' ? 'dedup_listings') AND is_accepted) AS accepted
FROM _manual_decisions;

\echo '=== 5. fuzzy участвует ==='
SELECT
    count(*) FILTER (WHERE match_method='fuzzy' OR evidence->'corroborating_methods' ? 'fuzzy') AS n,
    count(*) FILTER (WHERE (match_method='fuzzy' OR evidence->'corroborating_methods' ? 'fuzzy') AND is_accepted) AS accepted
FROM _manual_decisions;

\echo '=== 6. SHA256 (exact photo) совпадение > 0 ==='
SELECT
    count(*) FILTER (WHERE exact_shared_count > 0) AS n,
    count(*) FILTER (WHERE exact_shared_count > 0 AND is_accepted) AS accepted
FROM _manual_decisions;

\echo '=== 7. perceptual hash совпадение > 0 (независимо от exact) ==='
SELECT
    count(*) FILTER (WHERE perceptual_shared_count > 0) AS n,
    count(*) FILTER (WHERE perceptual_shared_count > 0 AND is_accepted) AS accepted
FROM _manual_decisions;

\echo '=== 8. SigLIP (ai_similar_count > 0) ==='
SELECT
    count(*) FILTER (WHERE ai_similar_count > 0) AS n,
    count(*) FILTER (WHERE ai_similar_count > 0 AND is_accepted) AS accepted
FROM _manual_decisions;

\echo '=== 8b. Есть ЛЮБОЕ photo evidence вообще (processing_status=ok) — sample size caveat ==='
SELECT count(*) FILTER (WHERE photo_status = 'ok') AS n_with_any_photo_evidence FROM _manual_decisions;

\echo '=== 9. rooms match (rooms_a = rooms_b, оба не null) ==='
SELECT
    count(*) FILTER (WHERE rooms_a IS NOT NULL AND rooms_b IS NOT NULL AND rooms_a = rooms_b) AS n,
    count(*) FILTER (WHERE rooms_a IS NOT NULL AND rooms_b IS NOT NULL AND rooms_a = rooms_b AND is_accepted) AS accepted,
    count(*) FILTER (WHERE rooms_a IS NOT NULL AND rooms_b IS NOT NULL AND rooms_a != rooms_b) AS n_mismatch,
    count(*) FILTER (WHERE rooms_a IS NOT NULL AND rooms_b IS NOT NULL AND rooms_a != rooms_b AND is_accepted) AS mismatch_accepted
FROM _manual_decisions;

\echo '=== 10. house_number match ==='
SELECT
    count(*) FILTER (WHERE house_number_a IS NOT NULL AND house_number_b IS NOT NULL AND house_number_a = house_number_b) AS n_match,
    count(*) FILTER (WHERE house_number_a IS NOT NULL AND house_number_b IS NOT NULL AND house_number_a = house_number_b AND is_accepted) AS match_accepted,
    count(*) FILTER (WHERE house_number_a IS NOT NULL AND house_number_b IS NOT NULL AND house_number_a != house_number_b) AS n_mismatch,
    count(*) FILTER (WHERE house_number_a IS NOT NULL AND house_number_b IS NOT NULL AND house_number_a != house_number_b AND is_accepted) AS mismatch_accepted
FROM _manual_decisions;

\echo '=== 11. price diff buckets (|a-b|/greatest(a,b)) ==='
SELECT
    CASE
        WHEN price_a IS NULL OR price_b IS NULL OR price_a=0 OR price_b=0 THEN 'unknown'
        WHEN abs(price_a-price_b)/GREATEST(price_a,price_b) < 0.05 THEN '<5%'
        WHEN abs(price_a-price_b)/GREATEST(price_a,price_b) < 0.15 THEN '5-15%'
        WHEN abs(price_a-price_b)/GREATEST(price_a,price_b) < 0.30 THEN '15-30%'
        ELSE '>30%'
    END AS price_diff_bucket,
    count(*) AS n,
    count(*) FILTER (WHERE is_accepted) AS accepted
FROM _manual_decisions GROUP BY 1 ORDER BY 1;

\echo '=== 12. simultaneously_active ==='
SELECT
    count(*) FILTER (WHERE simultaneously_active IS TRUE) AS n_true,
    count(*) FILTER (WHERE simultaneously_active IS TRUE AND is_accepted) AS true_accepted,
    count(*) FILTER (WHERE simultaneously_active IS FALSE) AS n_false,
    count(*) FILTER (WHERE simultaneously_active IS FALSE AND is_accepted) AS false_accepted,
    count(*) FILTER (WHERE simultaneously_active IS NULL) AS n_unknown
FROM _manual_decisions;

\echo '=== 13. seller_equal ==='
SELECT
    count(*) FILTER (WHERE seller_equal IS TRUE) AS n_true,
    count(*) FILTER (WHERE seller_equal IS TRUE AND is_accepted) AS true_accepted,
    count(*) FILTER (WHERE seller_equal IS FALSE) AS n_false,
    count(*) FILTER (WHERE seller_equal IS FALSE AND is_accepted) AS false_accepted,
    count(*) FILTER (WHERE seller_equal IS NULL) AS n_unknown
FROM _manual_decisions;

\echo '=== 14. n_corroborating (1 vs 2 vs 3+) ==='
SELECT
    CASE WHEN n_corroborating <= 1 THEN '1' WHEN n_corroborating = 2 THEN '2' ELSE '3+' END AS bucket,
    count(*) AS n,
    count(*) FILTER (WHERE is_accepted) AS accepted
FROM _manual_decisions GROUP BY 1 ORDER BY 1;

\echo '=== 15. relationship_type ==='
SELECT relationship_type, count(*) AS n, count(*) FILTER (WHERE is_accepted) AS accepted
FROM _manual_decisions GROUP BY 1 ORDER BY n DESC;

\echo '=== 16. any conflict_reasons present ==='
SELECT
    count(*) FILTER (WHERE conflict_reasons IS NOT NULL) AS n_with_conflict,
    count(*) FILTER (WHERE conflict_reasons IS NOT NULL AND is_accepted) AS conflict_accepted,
    count(*) FILTER (WHERE conflict_reasons IS NULL) AS n_no_conflict,
    count(*) FILTER (WHERE conflict_reasons IS NULL AND is_accepted) AS no_conflict_accepted
FROM _manual_decisions;
