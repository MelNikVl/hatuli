-- Расшивка blob-комплексов (задача 2026-08-12, Gate 2, см.
-- docs/entity_resolution_plan.md) — старый (до фикса токена фазы)
-- матчинг слил разные буквенные/номерные блоки одного застройщика в
-- один complex_id (Family Nest A/B/F, UIA.BIRLIK A-E, ...). Плейбук:
-- кластеризация spine-связей по (блок-токен, адрес), каждый кластер ->
-- отдельный complex_id, паспорт (исходный complex_id) остаётся у
-- крупнейшего кластера/безномерной базы.
--
-- provenance — на НОВЫХ (отпочковавшихся) строках complexes, чтобы
-- отличать "отпочковался от расшивки" от органически заведённого ЖК:
-- {"split_from": <id паспорта>, "split_at": "...", "method": "unravel_2026-08-12"}.
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS provenance JSONB;

-- evidence — на ПЕРЕНЕСЁННЫХ complex_source_links (кластер, по которому
-- решили, куда переносить): {"cluster_token": "block:f", "sample_address": "...",
-- "unravel_from": <id паспорта>}. match_method/matched_by уже есть в
-- 043_entity_resolution.sql — тут используем их со значениями
-- 'manual'/'unravel', evidence — новое, детальнее одной строки метода.
ALTER TABLE complex_source_links ADD COLUMN IF NOT EXISTS evidence JSONB;
