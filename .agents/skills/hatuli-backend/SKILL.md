---
name: hatuli-backend
description: Implement and validate Hatuli Python backend, data quality and property identity changes using existing PostgreSQL conventions.
---

Read docs/process.md and relevant tests. PostgreSQL is the canonical market store; the rental pilot has a separate private SQLite queue. Reuse bot/core/complex_membership.py and existing canonical identity writers rather than restoring name-only membership. Keep complex, building and physical property distinct. Preserve fractional house/plot numbers, letters, source provenance and ambiguity.
For forecasts, follow docs/temporal_policy.md and current readiness gates. Use capture-time features, independent temporal validation and right censoring; do not relabel disappearance as a verified sale or expose unsupported numerical predictions.
Use an isolated branch/worktree based on the actual deployed revision. Inspect dirty files before deployment and preserve unrelated work. Run pure tests with a dummy DSN and connection-denying fixtures where available; DB-writing tests need an ephemeral test database. A read-only audit is not a test database. Apply migrations only when the change needs them. Restart affected long-running services after Python changes and verify live behavior and service health. Log exact revision, checks, rollback path and unresolved limits.
