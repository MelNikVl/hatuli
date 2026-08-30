-- Fix 2026-08-30 (found while implementing the manifest-log git-provenance
-- fix, see bot/identity/property_merge_provenance.py): property_merge_
-- execution_log.status CHECK constraint (migrations/093) did not include
-- 'would_apply' — a status bot.identity.property_merge.apply_property_
-- merge() legitimately returns for a healthy component when dry_run=True.
--
-- Production impact this closes: ANY dry-run rehearsal through
-- scripts/property_merge_batch_runner.py (i.e. run WITHOUT --apply, the
-- default/safe mode) on a component with zero blocking problems would hit
-- CheckViolationError trying to persist its execution_log row — the
-- rehearsal path of the batch runner was broken from the moment
-- migrations/093 shipped. Confirmed by the first synthetic test that
-- actually exercises apply_property_merge_durable(dry_run=True) on a
-- healthy component (tests/test_property_merge_provenance.py::
-- test_manifest_and_execution_share_one_resolved_git_provenance_when_none_passed).
--
-- Widening an existing CHECK constraint — additive, no data loss, no rows
-- touched (append-only table, 0 rows persisted in production so far under
-- the old constraint that this would have rejected).
ALTER TABLE property_merge_execution_log DROP CONSTRAINT property_merge_execution_log_status_check;
ALTER TABLE property_merge_execution_log ADD CONSTRAINT property_merge_execution_log_status_check
    CHECK (status IN (
        'merged', 'already_merged', 'would_apply', 'blocked_stale', 'blocked_conflict',
        'blocked_provenance', 'error'
    ));
