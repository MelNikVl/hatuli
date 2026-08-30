"""bot/identity/property_merge_provenance.py — durable auditability/
provenance layer поверх bot/identity/property_merge.py (задача: "Перед
следующими physical merges закрыть auditability/provenance gap").

Контекст: read-only provenance audit (2026-08-30) существующих 22 production
merge (batch20 + size3-canary, все выполнены ДО этого модуля) нашёл clean
state, но НЕполный процесс-provenance — ни одного frozen manifest-файла на
диске, ни одной строки execution-лога, ни одного зафиксированного вызова
timeline-валидации между операциями. Этот модуль закрывает именно этот
разрыв ДЛЯ БУДУЩИХ merge, не переписывает и не "дорисовывает" прошлое —
см. property_merge_provenance_note (migrations/093) для честной
reconstructed-пометки прошлого, отдельно от originally-persisted записей,
которые этот модуль производит начиная с этого PR.

Три новых append-only таблицы (миграция 093), одна цепочка гарантий:
`persist_manifest()` (ДО apply, ВСЕГДА, независимо от исхода apply) ->
`apply_property_merge_durable()` (оборачивает существующий, НЕ изменённый
bot.identity.property_merge.apply_property_merge — engine-логика та же)
-> `validate_property_merge()` (read-only, ПОСЛЕ успешного apply, НИКОГДА
не делает rollback сама — задача, явно).

Git-provenance проверяется ЧЕРЕЗ инжектируемый провайдер (`git_provenance`
параметр == dict, не обязательно результат реального `git`-вызова) —
задача явно требует unit-тестируемость guard'а через mock, без реального
git-репозитория/subprocess в тестах."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ── git provenance (реальный источник — subprocess; guard — чистая функция) ─

def get_git_provenance(cwd: str | Path | None = None) -> dict:
    """Реальная git-интроспекция (rev-parse HEAD, branch --show-current,
    status --porcelain -> dirty). Единственная функция здесь, которая
    шеллится наружу — все остальные принимают уже готовый dict, поэтому
    guard/persist-код тестируется без git вообще (задача, явно: "unit-
    тестируемо через injected provenance provider/mock"). Любая ошибка
    subprocess (например, репозиторий недоступен) -> все поля None,
    dirty=None — вызывающий guard решает, что делать с неизвестным
    состоянием (см. check_git_provenance: неизвестное dirty НЕ считается
    автоматически безопасным)."""
    cwd = str(cwd) if cwd is not None else str(_REPO_ROOT)

    def _run(args: list[str]) -> str | None:
        try:
            out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
        except Exception:
            return None
        if out.returncode != 0:
            return None
        return out.stdout.strip()

    sha = _run(["rev-parse", "HEAD"])
    branch = _run(["rev-parse", "--abbrev-ref", "HEAD"])
    status = _run(["status", "--porcelain"])
    dirty = None if status is None else bool(status)
    return {"git_sha": sha, "git_branch": branch, "git_dirty": dirty}


def check_git_provenance(provenance: dict, *, allow_non_master: bool = False) -> list[str]:
    """Чистая функция, без I/O — принимает уже готовый provenance dict
    (реальный из get_git_provenance() ИЛИ инжектированный мок в тестах).
    Возвращает список нарушений (пусто -> apply разрешён). Dirty working
    tree — БЕЗ override вообще (задача: "Production apply запрещён, если
    working tree dirty" — не 'запрещён по умолчанию', запрещён без
    исключений, поэтому здесь нет allow_dirty параметра — единственный
    способ обойти dirty-guard — не вызывать этот guard, что явно и
    осознанно, а не тихий флаг). Неизвестное состояние (dirty is None —
    git недоступен/ошибка) ТРЕБУЕТ явного allow_non_master=True тоже не
    поможет — неизвестный dirty всегда блокирует, задача просит fail-
    closed, не fail-open, при недоступности git-интроспекции."""
    violations: list[str] = []
    dirty = provenance.get("git_dirty")
    if dirty is None:
        violations.append("git provenance unavailable — cannot confirm working tree is clean (fail-closed)")
    elif dirty:
        violations.append("working tree is dirty — production apply refused")

    branch = provenance.get("git_branch")
    if branch != "master" and not allow_non_master:
        violations.append(f"not on master (on {branch!r}) — explicit override required")

    return violations


# ── manifest persistence (ДО apply, всегда, append-only) ────────────────────

async def persist_manifest(manifest: dict, *, actor: str, git_provenance: dict | None = None) -> int:
    """INSERT-only снимок frozen manifest'а. Вызывается ПЕРЕД любой
    попыткой apply (успешной или нет) — задача, явно: "сохранять JSONB
    manifest полностью... обязательно... не делать destructive updates
    manifest snapshot". manifest уже должен быть validate_manifest_shape()
    -валидным (apply_property_merge_durable гарантирует это, вызывая её
    первой — см. ниже)."""
    from bot.db.pg import fetchval

    evidence = manifest.get("evidence_snapshot") or {}
    losing_ids = [pid for pid in manifest["property_ids"] if pid != manifest["canonical_property_id"]]
    expected_listing_ids = evidence.get("moved_listing_ids") or {}
    warnings = evidence.get("warnings") or []
    gp = git_provenance or {}

    manifest_created_at = manifest.get("created_at")
    if isinstance(manifest_created_at, str):
        manifest_created_at = datetime.fromisoformat(manifest_created_at)
    elif manifest_created_at is None:
        manifest_created_at = datetime.now(timezone.utc)

    return await fetchval(
        """
        INSERT INTO property_merge_manifest_log
            (component_hash, candidate_ids, property_ids, canonical_property_id, losing_property_ids,
             expected_listing_ids, warnings, evidence_snapshot, manifest, manifest_created_at,
             tool_version, actor, git_sha, git_branch, git_dirty)
        VALUES ($1, $2::jsonb, $3::jsonb, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9::jsonb, $10,
                $11, $12, $13, $14, $15)
        RETURNING manifest_id
        """,
        manifest["component_hash"],
        json.dumps(manifest["candidate_ids"]),
        json.dumps(manifest["property_ids"]),
        manifest["canonical_property_id"],
        json.dumps(losing_ids),
        json.dumps(expected_listing_ids),
        json.dumps(warnings, default=str, ensure_ascii=False),
        json.dumps(evidence, default=str, ensure_ascii=False),
        json.dumps(manifest, default=str, ensure_ascii=False),
        manifest_created_at,
        manifest.get("merge_tool_version") or "unknown",
        actor,
        gp.get("git_sha"), gp.get("git_branch"), gp.get("git_dirty"),
    )


async def _property_statuses(property_ids: list[int]) -> dict:
    from bot.db.pg import fetch
    rows = await fetch(
        "SELECT property_id, identity_status FROM properties WHERE property_id = ANY($1::int[])",
        property_ids,
    )
    return {str(r["property_id"]): r["identity_status"] for r in rows}


def _rows_repointed_from_result(result: dict) -> list[dict] | None:
    if result.get("status") != "merged":
        return None
    canonical_id = result["canonical_property_id"]
    out = []
    for row in result.get("log_rows", []):
        for lid in row.get("moved_listing_ids", []):
            out.append({
                "listing_id": lid, "from_property_id": row["losing_property_id"],
                "to_property_id": canonical_id,
            })
    return out


async def apply_property_merge_durable(
    manifest: dict, *, actor: str, dry_run: bool = True,
    git_provenance: dict | None = None, allow_non_master: bool = False,
    override_reason: str | None = None,
) -> dict:
    """Единственная рекомендуемая точка входа для production apply начиная
    с этого PR — persist_manifest() (ВСЕГДА, до guard) -> git provenance
    guard (только для реального, не dry_run, apply) -> существующий,
    НЕИЗМЕНЁННЫЙ bot.identity.property_merge.apply_property_merge() (та же
    engine-логика: idempotence/component_hash/revalidation) -> ОДНА
    append-only execution_log строка, что бы ни произошло (успех, blocked,
    exception — задача, явно: "Для failed/blocked apply тоже должен
    оставаться audit result").

    git_provenance=None -> реальный get_git_provenance() репозитория; explicit
    dict (в частности из тестов) -> используется как есть, ни один git-
    subprocess не запускается — это и есть unit-тестируемость guard'а."""
    from bot.identity.property_merge import apply_property_merge, validate_manifest_shape

    validate_manifest_shape(manifest)
    manifest_id = await persist_manifest(manifest, actor=actor, git_provenance=git_provenance)

    property_ids = [int(x) for x in manifest["property_ids"]]
    gp = git_provenance if git_provenance is not None else get_git_provenance()
    started_at = datetime.now(timezone.utc)
    branch_needs_override = gp.get("git_branch") not in (None, "master")
    override_used = (not dry_run) and allow_non_master and branch_needs_override

    if not dry_run:
        violations = check_git_provenance(gp, allow_non_master=allow_non_master)
        if violations:
            finished_at = datetime.now(timezone.utc)
            result_detail = {"status": "blocked_provenance", "violations": violations}
            execution_id = await _persist_execution(
                manifest_id=manifest_id, merge_group_key=None, status="blocked_provenance",
                dry_run=dry_run, started_at=started_at, finished_at=finished_at,
                manifest_hash=manifest["component_hash"], rows_repointed=None,
                property_statuses_before=None, property_statuses_after=None,
                error="; ".join(violations), result_detail=result_detail, actor=actor, gp=gp,
                override_used=False, override_reason=override_reason,
            )
            return {**result_detail, "manifest_id": manifest_id, "execution_id": execution_id}

    statuses_before = await _property_statuses(property_ids)

    error: str | None = None
    try:
        result = await apply_property_merge(manifest, actor=actor, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 — намеренно широкий: любая
        # ошибка тоже должна оставить audit-строку (задача, явно), не
        # уйти в вызывающий код неаудированной.
        error = f"{type(exc).__name__}: {exc}"
        result = {"status": "error", "dry_run": dry_run, "error": error}

    finished_at = datetime.now(timezone.utc)
    statuses_after = await _property_statuses(property_ids)
    merge_group_key = result.get("merge_group_key")
    rows_repointed = _rows_repointed_from_result(result)

    execution_id = await _persist_execution(
        manifest_id=manifest_id, merge_group_key=merge_group_key, status=result["status"],
        dry_run=dry_run, started_at=started_at, finished_at=finished_at,
        manifest_hash=manifest["component_hash"], rows_repointed=rows_repointed,
        property_statuses_before=statuses_before, property_statuses_after=statuses_after,
        error=error, result_detail=result, actor=actor, gp=gp,
        override_used=override_used, override_reason=override_reason,
    )
    return {**result, "manifest_id": manifest_id, "execution_id": execution_id}


async def _persist_execution(
    *, manifest_id: int, merge_group_key, status: str, dry_run: bool,
    started_at: datetime, finished_at: datetime, manifest_hash: str,
    rows_repointed: list[dict] | None, property_statuses_before: dict | None,
    property_statuses_after: dict | None, error: str | None, result_detail: dict,
    actor: str, gp: dict, override_used: bool, override_reason: str | None,
) -> int:
    from bot.db.pg import fetchval

    return await fetchval(
        """
        INSERT INTO property_merge_execution_log
            (manifest_id, merge_group_key, status, dry_run, started_at, finished_at, manifest_hash,
             rows_repointed, property_statuses_before, property_statuses_after, error, result_detail,
             actor, git_sha, git_branch, git_dirty, provenance_override, provenance_override_reason)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb, $10::jsonb, $11, $12::jsonb,
                $13, $14, $15, $16, $17, $18)
        RETURNING execution_id
        """,
        manifest_id, merge_group_key, status, dry_run, started_at, finished_at, manifest_hash,
        json.dumps(rows_repointed, default=str) if rows_repointed is not None else None,
        json.dumps(property_statuses_before, default=str) if property_statuses_before is not None else None,
        json.dumps(property_statuses_after, default=str) if property_statuses_after is not None else None,
        error,
        json.dumps(result_detail, default=str, ensure_ascii=False),
        actor, gp.get("git_sha"), gp.get("git_branch"), gp.get("git_dirty"),
        override_used, override_reason,
    )


# ── timeline validation (read-only, persist результата, НИКОГДА rollback) ──

def _events_signature(events: list[dict]) -> str:
    return json.dumps(events, sort_keys=True, default=str, ensure_ascii=False)


async def _run_validation_checks(*, canonical_property_id: int, losing_property_ids: list[int],
                                  expected_listing_ids: dict, candidate_ids: list[int]) -> list[dict]:
    """Чистая (кроме чтения БД) проверка — используется И persisting
    validate_property_merge() (execution_id обязателен), И read-only demo-
    скриптом на legacy-данных (scripts/audit_property_merge_provenance_
    dry_run.py), который результат НЕ персистит (см. модульный докстринг —
    'не пытаться дорисовать'). Одна реализация, два вызывающих контекста —
    не дублируем логику checks между 'настоящей' и 'демо' валидацией."""
    from bot.core.property_timeline import build_property_timeline
    from bot.db.pg import fetch

    checks: list[dict] = []

    canonical_tl = await build_property_timeline(canonical_property_id)
    checks.append({
        "name": "canonical_exists",
        "passed": canonical_tl is not None,
        "detail": f"property_id={canonical_property_id}",
    })
    if canonical_tl is None:
        return checks  # остальные checks бессмысленны без timeline

    all_expected = sorted({lid for lids in expected_listing_ids.values() for lid in lids})
    canonical_listing_ids = {l["listing_id"] for l in canonical_tl["listings"]}
    missing = [lid for lid in all_expected if lid not in canonical_listing_ids]
    checks.append({
        "name": "expected_listing_ids_present_on_canonical",
        "passed": not missing,
        "detail": f"missing={missing}" if missing else f"all {len(all_expected)} expected listing(s) present",
    })

    checks.append({
        "name": "canonical_listing_count_covers_moved",
        "passed": canonical_tl["metrics"]["listing_count"] >= len(all_expected),
        "detail": f"listing_count={canonical_tl['metrics']['listing_count']} expected_moved={len(all_expected)}",
    })

    for pid in losing_property_ids:
        losing_tl = await build_property_timeline(pid)
        ok = losing_tl is not None and losing_tl["metrics"]["listing_count"] == 0 \
            and losing_tl["identity_status"] == "merged"
        checks.append({
            "name": f"losing_property_{pid}_empty_and_merged",
            "passed": ok,
            "detail": (f"listing_count={losing_tl['metrics']['listing_count']} "
                       f"identity_status={losing_tl['identity_status']}") if losing_tl is not None
                      else "property row missing",
        })

    canonical_tl_2 = await build_property_timeline(canonical_property_id)
    deterministic = _events_signature(canonical_tl["events"]) == _events_signature(canonical_tl_2["events"])
    checks.append({
        "name": "timeline_events_deterministic",
        "passed": deterministic,
        "detail": "two consecutive build_property_timeline() calls produced identical events"
                  if deterministic else "events differed between two consecutive calls",
    })

    days = canonical_tl["metrics"]["observed_market_days"]
    sane = days is None or (isinstance(days, (int, float)) and 0 <= days <= 3650)
    checks.append({
        "name": "observed_market_days_sane",
        "passed": sane,
        "detail": f"observed_market_days={days}",
    })

    if candidate_ids:
        rows = await fetch(
            "SELECT candidate_id, status FROM property_match_candidates WHERE candidate_id = ANY($1::int[])",
            candidate_ids,
        )
        by_id = {r["candidate_id"]: r["status"] for r in rows}
        not_accepted = {cid: by_id.get(cid) for cid in candidate_ids if by_id.get(cid) != "accepted"}
        checks.append({
            "name": "candidate_statuses_untouched",
            "passed": not not_accepted,
            "detail": f"still accepted: {len(candidate_ids) - len(not_accepted)}/{len(candidate_ids)}"
                      + (f", changed={not_accepted}" if not_accepted else ""),
        })

    return checks


async def validate_property_merge(execution_id: int) -> dict:
    """Read-only post-apply validation. Требует РЕАЛЬНУЮ execution_log
    строку со status IN ('merged', 'already_merged') — валидировать
    blocked/error попытку бессмысленно (ничего не изменилось). Персистит
    результат в property_merge_validation_log (execution_id NOT NULL —
    без реального execution нет валидации, задача §3). НИКОГДА не
    откатывает сама при failed — задача, явно."""
    from bot.db.pg import fetchrow, fetchval

    execution = await fetchrow(
        "SELECT execution_id, manifest_id, status FROM property_merge_execution_log WHERE execution_id = $1",
        execution_id,
    )
    if execution is None:
        raise ValueError(f"no execution_log row for execution_id={execution_id}")
    if execution["status"] not in ("merged", "already_merged"):
        raise ValueError(
            f"execution_id={execution_id} has status={execution['status']!r} — "
            "nothing to validate (validation only meaningful after a successful apply)"
        )

    manifest_row = await fetchrow(
        "SELECT canonical_property_id, losing_property_ids, expected_listing_ids, candidate_ids "
        "FROM property_merge_manifest_log WHERE manifest_id = $1",
        execution["manifest_id"],
    )
    canonical_id = manifest_row["canonical_property_id"]
    losing_ids = json.loads(manifest_row["losing_property_ids"]) if isinstance(manifest_row["losing_property_ids"], str) else manifest_row["losing_property_ids"]
    expected_listing_ids = json.loads(manifest_row["expected_listing_ids"]) if isinstance(manifest_row["expected_listing_ids"], str) else manifest_row["expected_listing_ids"]
    candidate_ids = json.loads(manifest_row["candidate_ids"]) if isinstance(manifest_row["candidate_ids"], str) else manifest_row["candidate_ids"]

    checks = await _run_validation_checks(
        canonical_property_id=canonical_id, losing_property_ids=losing_ids,
        expected_listing_ids=expected_listing_ids, candidate_ids=candidate_ids,
    )
    passed = all(c["passed"] for c in checks)

    validation_id = await fetchval(
        """
        INSERT INTO property_merge_validation_log (execution_id, canonical_property_id, passed, checks)
        VALUES ($1, $2, $3, $4::jsonb)
        RETURNING validation_id
        """,
        execution_id, canonical_id, passed,
        json.dumps(checks, default=str, ensure_ascii=False),
    )
    return {"validation_id": validation_id, "execution_id": execution_id,
            "canonical_property_id": canonical_id, "passed": passed, "checks": checks}
