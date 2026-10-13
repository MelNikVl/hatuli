"""Reviewed complex redirects with read-only previews and durable provenance.

This replaces the old destructive duplicate merge: source spellings, source
links, property/unit identities and favorites are retained. Every approval is
bound to a fresh preview and records the before/after listing transitions.
No migration or pool initialization is performed by this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
from urllib.parse import urlsplit
import uuid

import asyncpg

from bot.core.complex_binding import build_preview_sql
from bot.core.complex_canonical import compute_canonical
from bot.core.entity_resolution import _haversine_m, _phase_token, _PRODUCT_TOKEN_RE
from bot.core.complex_metrics import CURRENT_METRICS_SQL

VERSION = "complex_identity_review_v1"
_LOCK = 728143
_SEPARATE = {"sibling_phase", "same_umbrella_project", "separate_neighbor_complex"}


class ComplexIdentityReviewError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _decode(value):
    if isinstance(value, str):
        return json.loads(value) if value else {}
    return value or {}


def _key(value):
    return " ".join(str(value or "").lower().split())


def _root(cid, rows):
    seen = set()
    while cid in rows and cid not in seen:
        seen.add(cid)
        target = rows[cid].get("canonical_id")
        if target is None:
            return cid
        cid = target
    raise ComplexIdentityReviewError("invalid_identity", "Некорректная цепочка идентичности ЖК")


def build_decision(candidate, complexes, counts, relations, overrides):
    """Pure conservative decision; fuzzy names and cached URLs never prove it."""
    by_id = {row["id"]: row for row in complexes}
    blockers = []
    if candidate.get("status") != "review":
        blockers.append("Кандидат уже рассмотрен; повторное слияние запрещено")
    if candidate["complex_id_a"] not in by_id or candidate["complex_id_b"] not in by_id:
        raise ComplexIdentityReviewError("not_found", "Одна из записей ЖК отсутствует")
    a = _root(candidate["complex_id_a"], by_id)
    b = _root(candidate["complex_id_b"], by_id)
    if a == b:
        blockers.append("Обе записи уже ведут на один ЖК")
    for relation in relations:
        if relation["relation_type"] in _SEPARATE:
            separated = {_root(relation["complex_id_a"], by_id), _root(relation["complex_id_b"], by_id)}
            if separated == {a, b}:
                blockers.append("Решение противоречит проверенному отношению между частями ЖК")
    heads = [by_id[a], by_id[b]]
    if any(row.get("is_garbage") or row.get("is_street") or
           row.get("canonical_reason") == "junk_unmatched" for row in heads):
        blockers.append("Мусорная или неподтверждённая запись требует отдельного разбора")
    if heads[0].get("parent_complex_id") != heads[1].get("parent_complex_id"):
        blockers.append("Разные родительские проекты: дом и зонтик нельзя объединить")
    phase_a, _ = _phase_token(heads[0]["name"])
    phase_b, _ = _phase_token(heads[1]["name"])
    if phase_a != phase_b and (phase_a is not None or phase_b is not None):
        blockers.append("Различаются номера очереди или корпуса")
    products = [{m.group(0).lower() for m in _PRODUCT_TOKEN_RE.finditer(row["name"] or "")} for row in heads]
    if products[0] != products[1]:
        blockers.append("Различаются названия продукта или класса проекта")
    da, db = (row.get("developer_id") for row in heads)
    if da is not None and db is not None and da != db:
        blockers.append("Разные застройщики: требуется отдельная проверка источников")
    geo = None
    coordinates = [row.get(key) for row in heads for key in ("lat", "lon")]
    if all(isinstance(value, (int, float)) and isfinite(value) for value in coordinates):
        geo = round(_haversine_m(*coordinates), 1)
        if geo > 300:
            blockers.append("Координаты расходятся более чем на 300 м")
    signals = {
        "same_developer": da is not None and da == db,
        "same_address": bool(_key(heads[0].get("address"))) and
                        _key(heads[0].get("address")) == _key(heads[1].get("address")),
        "near_coordinates": geo is not None and geo <= 150,
    }
    if sum(signals.values()) < 2:
        blockers.append("Недостаточно независимых подтверждений: нужно уточнить адрес и источник")
    pinned = {row["complex_id"] for row in overrides}
    # Existing reviewed roots stay stable; otherwise choose the better populated
    # current root. The fingerprint prevents a later score change selecting anew.
    canon, dup = sorted((a, b), key=lambda cid: (
        cid not in pinned, -counts.get(cid, 0), cid
    ))
    if dup in pinned:
        blockers.append("Запись закреплена ручным решением; автоматическое изменение запрещено")
    component = {cid for cid in by_id if _root(cid, by_id) in {a, b}}
    desired_overrides = list(overrides)
    desired_overrides.append({"complex_id": dup, "canonical_id": canon})
    eligible = [row for row in complexes if not row.get("is_garbage") and not row.get("is_street")]
    try:
        proposed = compute_canonical(eligible, counts, relations, desired_overrides)
    except ValueError as error:
        proposed = {row["id"]: (row.get("canonical_id"), row.get("canonical_reason")) for row in eligible}
        blockers.append("Решение противоречит проверенному отношению: " + str(error))
    changes = []
    for row in eligible:
        target, reason = proposed[row["id"]]
        if (target, reason) != (row.get("canonical_id"), row.get("canonical_reason")):
            changes.append({
                "complex_id": row["id"], "previous_canonical_id": row.get("canonical_id"),
                "previous_reason": row.get("canonical_reason"), "canonical_id": target, "reason": reason,
            })
    if any(row["complex_id"] not in component for row in changes):
        blockers.append("Пересчёт затрагивает другие ЖК: сначала нужен отдельный аудит")
    return {
        "canon_id": canon, "dup_id": dup, "component_ids": sorted(component),
        "blockers": list(dict.fromkeys(blockers)), "canonical_changes": changes,
        "signals": signals, "geo_m": geo,
    }


def build_shadow_preview_sql():
    """Use the exact production resolver against a SELECT-only shadow map."""
    sql = build_preview_sql().replace("FROM complexes", "FROM reviewed_complexes")
    sql = sql.replace("{{scope}}", "AND a.id=ANY($1::text[])")
    # build_preview_sql already expands its empty scope; add scope to states and
    # key candidate scans through a scoped listing CTE instead.
    sql = sql.replace("FROM apartment_listings", "FROM reviewed_listings")
    return """
WITH reviewed_mapping AS (
    SELECT * FROM jsonb_to_recordset($2::jsonb)
        AS mapping(complex_id int, canonical_id int, reason text)
), reviewed_complexes AS (
    SELECT c.id,c.name,c.lat,c.lon,c.krisha_url,c.is_garbage,c.is_street,
           CASE WHEN m.complex_id IS NULL THEN c.canonical_id ELSE m.canonical_id END AS canonical_id,
           CASE WHEN m.complex_id IS NULL THEN c.canonical_reason ELSE m.reason END AS canonical_reason
    FROM complexes c LEFT JOIN reviewed_mapping m ON m.complex_id=c.id
), reviewed_listings AS (
    SELECT * FROM apartment_listings WHERE id=ANY($1::text[])
), """ + sql.lstrip().removeprefix("WITH ")


async def _snapshot(conn, candidate_id):
    candidate_row = await conn.fetchrow(
        "SELECT * FROM complex_duplicate_candidates WHERE id=$1", candidate_id)
    if candidate_row is None:
        raise ComplexIdentityReviewError("not_found", "Кандидат не найден")
    candidate = dict(candidate_row)
    complexes = [dict(row) for row in await conn.fetch("""
        SELECT id,name,address,lat,lon,developer_id,parent_complex_id,is_umbrella,
               housing_class,is_newbuild,krisha_url,canonical_id,canonical_reason,
               COALESCE(is_garbage,false) AS is_garbage,COALESCE(is_street,false) AS is_street
        FROM complexes ORDER BY id
    """)]
    counts = {row["complex_id"]: row["n"] for row in await conn.fetch(
        "SELECT complex_id,count(*) AS n FROM apartment_listings WHERE complex_id IS NOT NULL GROUP BY 1")}
    relations = [dict(row) for row in await conn.fetch(
        "SELECT complex_id_a,complex_id_b,relation_type FROM complex_relations ORDER BY 1,2")]
    overrides = [dict(row) for row in await conn.fetch(
        "SELECT complex_id,canonical_id FROM complex_canonical_overrides ORDER BY 1")]
    decision = build_decision(candidate, complexes, counts, relations, overrides)
    ids = decision["component_ids"]
    affected = [dict(row) for row in await conn.fetch("""
        SELECT a.id,a.complex_id,a.complex_resolution,a.complex_name,a.complex_url,a.resolved_house_id
        FROM apartment_listings a
        WHERE a.complex_id=ANY($1::int[]) OR a.resolved_house_id=ANY($1::int[])
           OR EXISTS(SELECT 1 FROM complexes c WHERE c.id=ANY($1::int[])
                     AND complex_name_key(c.name)=complex_name_key(a.complex_name))
           OR EXISTS(SELECT 1 FROM complex_aliases ca WHERE ca.complex_id=ANY($1::int[])
                     AND ca.status='verified' AND ca.normalized_name=complex_name_key(a.complex_name))
           OR EXISTS(SELECT 1 FROM complexes c WHERE c.id=ANY($1::int[])
                     AND nullif(c.krisha_url,'') IS NOT NULL
                     AND lower(substring(c.krisha_url from '/complex/show/[^/]+/([^/?#]+)')) =
                         lower(substring(a.complex_url from '/complex/show/[^/]+/([^/?#]+)')))
        ORDER BY a.id
    """, ids)]
    changes = []
    if not decision["blockers"]:
        changes = [dict(row) for row in await conn.fetch(
            build_shadow_preview_sql(), [row["id"] for row in affected],
            _json(decision["canonical_changes"]))]
        # Never detach or move other families unnoticed. Such a conflict must be
        # resolved in the richer batch workflow, rather than approving a name.
        if any(row["complex_id"] is None or row["complex_id"] not in ids for row in changes):
            decision["blockers"].append("Есть конфликт привязки объявлений; нужен отдельный план исправления")
    pair = [row for row in complexes if row["id"] in {
        candidate["complex_id_a"], candidate["complex_id_b"]
    }]
    preserved = dict(await conn.fetchrow("""
        SELECT (SELECT count(*) FROM complex_source_links WHERE complex_id=ANY($1::int[])) AS source_links,
               (SELECT count(*) FROM newbuild_units WHERE complex_id=ANY($1::int[])) AS newbuild_units,
               (SELECT count(*) FROM properties WHERE complex_id=ANY($1::int[])) AS properties,
               (SELECT count(*) FROM complex_favorites WHERE complex_id=ANY($1::int[])) AS favorites
    """, ids))
    fingerprint = hashlib.sha256(_json({
        "candidate": candidate, "complexes": complexes, "counts": counts,
        "relations": relations, "overrides": overrides, "affected": affected,
        "canonical_changes": decision["canonical_changes"], "listing_changes": changes,
        "preserved": preserved,
    }).encode()).hexdigest()
    summary = {
        "count": len(changes),
        "rebound": sum(row["previous_complex_id"] != row["complex_id"] and row["complex_id"] is not None for row in changes),
        "detached": sum(row["previous_complex_id"] is not None and row["complex_id"] is None for row in changes),
        "method_only": sum(row["previous_complex_id"] == row["complex_id"] for row in changes),
        "samples": changes[:10],
    }
    public = {
        "candidate_id": candidate_id, "status": candidate["status"],
        "canon_id": decision["canon_id"], "dup_id": decision["dup_id"],
        "can_apply": not decision["blockers"], "blockers": decision["blockers"],
        "fingerprint": fingerprint, "complexes": pair,
        "canonical_changes": decision["canonical_changes"],
        "listing_changes": summary, "preserved": preserved,
        "signals": decision["signals"], "geo_m": decision["geo_m"],
        "methodology_version": VERSION,
    }
    return public, changes


async def preview_duplicate_candidate(candidate_id: int, *, connection=None):
    if connection is not None:
        return (await _snapshot(connection, candidate_id))[0]
    from bot.db.pg import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            return (await _snapshot(conn, candidate_id))[0]


def validate_review(expected_fingerprint, review_note, source_urls, reviewed_by):
    if not isinstance(expected_fingerprint, str) or len(expected_fingerprint) != 64:
        raise ComplexIdentityReviewError("preview_required", "Сначала откройте свежий предпросмотр")
    if not isinstance(review_note, str) or not 15 <= len(review_note.strip()) <= 2000:
        raise ComplexIdentityReviewError("evidence_required", "Кратко опишите доказательство одного физического ЖК")
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise ComplexIdentityReviewError("evidence_required", "Не указан проверяющий")
    if not isinstance(source_urls, list) or not 1 <= len(source_urls) <= 5:
        raise ComplexIdentityReviewError("evidence_required", "Добавьте ссылку на подтверждающий источник")
    for value in source_urls:
        if not isinstance(value, str) or len(value) > 2048:
            raise ComplexIdentityReviewError("evidence_required", "Некорректная ссылка источника")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ComplexIdentityReviewError("evidence_required", "Нужна HTTPS-ссылка источника без авторизации")


async def _apply(conn, candidate_id, expected_fingerprint, review_note, source_urls, reviewed_by):
    await conn.fetchval("SELECT pg_advisory_xact_lock($1)", _LOCK)
    # Ordinary ingestion is briefly held while the reviewed before/after state
    # is checked. Concurrent source changes cannot invalidate the chosen rows.
    await conn.execute("""LOCK TABLE complexes,apartment_listings,complex_canonical_overrides,
        complex_relations,complex_aliases,complex_duplicate_candidates
        IN SHARE ROW EXCLUSIVE MODE""")
    preview, changes = await _snapshot(conn, candidate_id)
    if preview["fingerprint"] != expected_fingerprint:
        raise ComplexIdentityReviewError("stale_preview", "Данные изменились; обновите предпросмотр")
    if not preview["can_apply"]:
        raise ComplexIdentityReviewError("identity_conflict", "; ".join(preview["blockers"]))
    canon, dup = preview["canon_id"], preview["dup_id"]
    review_id = str(uuid.uuid4())
    a, b = sorted((canon, dup))
    existing_relation = await conn.fetchrow(
        "SELECT relation_id FROM complex_relations WHERE complex_id_a=$1 AND complex_id_b=$2", a, b)
    evidence = {
        "review_id": review_id, "review_note": review_note.strip(),
        "source_urls": source_urls, "candidate_id": candidate_id,
        "preview_fingerprint": expected_fingerprint, "resolver_version": VERSION,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "canonical_changes": preview["canonical_changes"], "listing_changes": changes,
        "preserved": preview["preserved"],
        "source_values_retained": True,
        "created_relation": existing_relation is None,
        "relation_pair": [a, b],
        "reviewed_by": reviewed_by,
    }
    await conn.execute("""
        INSERT INTO complex_relations(complex_id_a,complex_id_b,relation_type,confidence,
            evidence,reviewed_by,reviewed_at,methodology_version)
        VALUES($1,$2,'duplicate_same_complex',1,$3::jsonb,$4,now(),$5)
        ON CONFLICT(complex_id_a,complex_id_b) DO NOTHING
    """, a, b, _json(evidence), reviewed_by, VERSION)
    await conn.execute("""
        INSERT INTO complex_canonical_overrides(complex_id,canonical_id,evidence,reviewed_by)
        VALUES($1,$2,$3::jsonb,$4)
    """, dup, canon, _json(evidence), reviewed_by)
    await conn.execute("""
        INSERT INTO complex_canonical_override_log(complex_id,previous_canonical_id,
            canonical_id,evidence,reviewed_by)
        VALUES($1,NULL,$2,$3::jsonb,$4)
    """, dup, canon, _json(evidence), reviewed_by)
    for change in preview["canonical_changes"]:
        await conn.execute("UPDATE complexes SET canonical_id=$2,canonical_reason=$3 WHERE id=$1",
                           change["complex_id"], change["canonical_id"], change["reason"])
    for row in preview["complexes"]:
        await conn.execute("""
            INSERT INTO complex_aliases(complex_id,name,normalized_name,source,source_id,
                evidence,status,reviewed_by,reviewed_at)
            VALUES($1,$2,complex_name_key($2),'identity_review',$3,$4::jsonb,'verified',$5,now())
            ON CONFLICT(complex_id,source,source_id,name) DO NOTHING
        """, canon, row["name"], review_id, _json({"review_id": review_id, "source_urls": source_urls}), reviewed_by)
    for change in changes:
        await conn.execute(
            "UPDATE apartment_listings SET complex_id=$2,complex_resolution=$3 WHERE id=$1",
            change["listing_id"], change["complex_id"], change["method"])
        await conn.execute("""
            INSERT INTO listing_complex_resolution_log(listing_id,complex_id,resolution_method,
                confidence_tier,resolved_at,evidence,resolver_version,previous_complex_id,previous_resolution_method)
            VALUES($1,$2,$3,CASE WHEN $3='geo' THEN 'B' WHEN $3='unbound' THEN 'C' ELSE 'A' END,
                now(),$4::jsonb,$5,$6,$7)
        """, change["listing_id"], change["complex_id"], change["method"] or "unbound",
            _json({"review_id": review_id, "source_evidence": _decode(change["evidence"])}),
            VERSION, change["previous_complex_id"], change["previous_complex_resolution"])
    await conn.execute(CURRENT_METRICS_SQL)
    await conn.execute("""
        UPDATE complex_duplicate_candidates SET status='merged',resolved_at=now(),resolved_by=$2 WHERE id=$1
    """, candidate_id, reviewed_by)
    return {**preview, "review_id": review_id, "applied": True,
            "listings_moved": preview["listing_changes"]["rebound"], "links_moved": 0, "favorites_moved": 0}


async def apply_duplicate_candidate(candidate_id: int, *, expected_fingerprint: str,
                                    review_note: str, source_urls: list[str],
                                    reviewed_by: str = "admin", connection=None):
    validate_review(expected_fingerprint, review_note, source_urls, reviewed_by)
    if connection is not None:
        return await _serializable_action(connection, _apply, candidate_id, expected_fingerprint,
                                          review_note, source_urls, reviewed_by)
    from bot.db.pg import get_pool
    async with get_pool().acquire() as conn:
        return await _serializable_action(conn, _apply, candidate_id, expected_fingerprint,
                                          review_note, source_urls, reviewed_by)


async def _serializable_action(conn, operation, *args):
    try:
        async with conn.transaction(isolation="serializable"):
            return await operation(conn, *args)
    except (asyncpg.SerializationError, asyncpg.DeadlockDetectedError):
        raise ComplexIdentityReviewError(
            "stale_preview", "Данные изменяются другим процессом; обновите предпросмотр") from None


async def _rollback(conn, review_id, reviewed_by):
    await conn.fetchval("SELECT pg_advisory_xact_lock($1)", _LOCK)
    await conn.execute("""LOCK TABLE complexes,apartment_listings,complex_canonical_overrides,
        complex_relations,complex_aliases,complex_duplicate_candidates
        IN SHARE ROW EXCLUSIVE MODE""")
    log = await conn.fetchrow("""
        SELECT complex_id,canonical_id,evidence FROM complex_canonical_override_log
        WHERE evidence->>'review_id'=$1 ORDER BY log_id LIMIT 1
    """, review_id)
    if log is None:
        raise ComplexIdentityReviewError("not_found", "Журнал решения не найден")
    evidence = _decode(log["evidence"])
    override = await conn.fetchrow(
        "SELECT canonical_id,evidence FROM complex_canonical_overrides WHERE complex_id=$1", log["complex_id"])
    if (override is None or override["canonical_id"] != log["canonical_id"] or
            _decode(override["evidence"]).get("review_id") != review_id):
        raise ComplexIdentityReviewError("rollback_conflict", "Ручное решение изменилось после применения")
    for change in evidence["canonical_changes"]:
        current = await conn.fetchrow(
            "SELECT canonical_id,canonical_reason FROM complexes WHERE id=$1", change["complex_id"])
        if current is None or (current["canonical_id"], current["canonical_reason"]) != (
                change["canonical_id"], change["reason"]):
            raise ComplexIdentityReviewError("rollback_conflict", "Принадлежность ЖК изменилась после применения")
    for change in evidence["listing_changes"]:
        current = await conn.fetchrow(
            "SELECT complex_id,complex_resolution FROM apartment_listings WHERE id=$1", change["listing_id"])
        if current is None or (current["complex_id"], current["complex_resolution"]) != (
                change["complex_id"], change["method"]):
            raise ComplexIdentityReviewError("rollback_conflict", "Объявление изменилось после применения")
    candidate = await conn.fetchrow(
        "SELECT status,resolved_by FROM complex_duplicate_candidates WHERE id=$1", evidence["candidate_id"])
    if candidate is None or candidate["status"] != "merged" or candidate["resolved_by"] != evidence["reviewed_by"]:
        raise ComplexIdentityReviewError("rollback_conflict", "Кандидат повторно рассмотрен после применения")
    a, b = evidence["relation_pair"]
    if evidence["created_relation"]:
        relation = await conn.fetchrow(
            "SELECT evidence FROM complex_relations WHERE complex_id_a=$1 AND complex_id_b=$2", a, b)
        if relation is None or _decode(relation["evidence"]).get("review_id") != review_id:
            raise ComplexIdentityReviewError("rollback_conflict", "Отношение ЖК изменилось после применения")
    inverse = {"rollback_of": review_id, "reviewed_by": reviewed_by, "resolver_version": VERSION}
    for change in evidence["canonical_changes"]:
        await conn.execute("UPDATE complexes SET canonical_id=$2,canonical_reason=$3 WHERE id=$1",
                           change["complex_id"], change["previous_canonical_id"], change["previous_reason"])
    for change in evidence["listing_changes"]:
        await conn.execute("UPDATE apartment_listings SET complex_id=$2,complex_resolution=$3 WHERE id=$1",
                           change["listing_id"], change["previous_complex_id"], change["previous_complex_resolution"])
        await conn.execute("""
            INSERT INTO listing_complex_resolution_log(listing_id,complex_id,resolution_method,
                confidence_tier,resolved_at,evidence,resolver_version,previous_complex_id,previous_resolution_method)
            VALUES($1,$2,$3,CASE WHEN $3='geo' THEN 'B' WHEN $3='unbound' THEN 'C' ELSE 'A' END,
                now(),$4::jsonb,$5,$6,$7)
        """, change["listing_id"], change["previous_complex_id"],
            change["previous_complex_resolution"] or "unbound", _json(inverse), VERSION + "_rollback",
            change["complex_id"], change["method"])
    await conn.execute("DELETE FROM complex_canonical_overrides WHERE complex_id=$1", log["complex_id"])
    await conn.execute("DELETE FROM complex_aliases WHERE source='identity_review' AND source_id=$1", review_id)
    if evidence["created_relation"]:
        await conn.execute("DELETE FROM complex_relations WHERE complex_id_a=$1 AND complex_id_b=$2", a, b)
    await conn.execute("""
        INSERT INTO complex_canonical_override_log(complex_id,previous_canonical_id,canonical_id,evidence,reviewed_by)
        VALUES($1,$2,NULL,$3::jsonb,$4)
    """, log["complex_id"], log["canonical_id"], _json(inverse), reviewed_by)
    await conn.execute("""
        UPDATE complex_duplicate_candidates SET status='review',resolved_at=NULL,resolved_by=NULL WHERE id=$1
    """, evidence["candidate_id"])
    await conn.execute(CURRENT_METRICS_SQL)
    return {"rolled_back": True, "review_id": review_id,
            "listings_restored": len(evidence["listing_changes"]), "journals_retained": True}


async def rollback_duplicate_review(review_id: str, *, reviewed_by="admin", connection=None):
    """Guarded inverse for an unchanged applied decision; audit events remain."""
    try:
        uuid.UUID(review_id)
    except (ValueError, TypeError, AttributeError):
        raise ComplexIdentityReviewError("invalid_review", "Некорректный ID решения") from None
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise ComplexIdentityReviewError("evidence_required", "Не указан проверяющий")
    if connection is not None:
        return await _serializable_action(connection, _rollback, review_id, reviewed_by)
    from bot.db.pg import get_pool
    async with get_pool().acquire() as conn:
        return await _serializable_action(conn, _rollback, review_id, reviewed_by)


def discover_review_pairs(complexes, counts, existing_pairs=(), limit=20):
    """Name variants become review hypotheses only, including geo conflicts."""
    from collections import defaultdict
    from itertools import combinations
    import re
    from bot.core.complex_canonical import norm_name
    from bot.core.entity_resolution import transliterate
    groups = defaultdict(list)
    for row in complexes:
        if (row.get("canonical_id") is not None or row.get("is_garbage") or row.get("is_street") or
                row.get("canonical_reason") == "junk_unmatched"):
            continue
        key = re.sub(r"[^\w]+", "", transliterate(norm_name(row.get("name"))))
        if len(key) >= 4:
            groups[key].append(row)
    excluded = {tuple(sorted(pair)) for pair in existing_pairs}
    candidates = []
    for key, members in groups.items():
        for a, b in combinations(sorted(members, key=lambda row: row["id"]), 2):
            pair = (a["id"], b["id"])
            if pair in excluded:
                continue
            coordinates = [row.get(field) for row in (a, b) for field in ("lat", "lon")]
            geo = round(_haversine_m(*coordinates), 1) if all(
                isinstance(value, (int, float)) and isfinite(value) for value in coordinates) else None
            phase_a, _ = _phase_token(a["name"])
            phase_b, _ = _phase_token(b["name"])
            evidence = {
                "hypothesis": "unconfirmed_name_variant",
                "same_developer": a.get("developer_id") is not None and a.get("developer_id") == b.get("developer_id"),
                "address_match": bool(_key(a.get("address"))) and _key(a.get("address")) == _key(b.get("address")),
                "geo_m": geo, "phase_conflict": phase_a != phase_b and (phase_a is not None or phase_b is not None),
                "source_urls": sorted({row.get(field) for row in (a, b) for field in ("krisha_url", "korter_url") if row.get(field)}),
                "source_urls_verified": False, "canonical_decision_made": False,
                "names": [a["name"], b["name"]], "addresses": [a.get("address"), b.get("address")],
                "listing_counts": [counts.get(a["id"], 0), counts.get(b["id"], 0)],
            }
            candidates.append({
                "complex_id_a": a["id"], "complex_id_b": b["id"], "translit_key": key,
                "reason": "identity_name_variant", "evidence": evidence,
                "impact_listings": sum(evidence["listing_counts"]),
                "source_verified": False,
            })
    return sorted(candidates, key=lambda row: (-row["impact_listings"], row["complex_id_a"], row["complex_id_b"]))[:limit]


async def nominate_complex_pair(complex_id_a: int, complex_id_b: int, *, review_note: str,
                                source_urls=None, nominated_by="admin", connection=None):
    """Insert a pending hypothesis only. Existing decisions are never reopened."""
    if (type(complex_id_a) is not int or type(complex_id_b) is not int or complex_id_a <= 0 or
            complex_id_b <= 0 or complex_id_a == complex_id_b):
        raise ComplexIdentityReviewError("invalid_pair", "Нужны два разных ID ЖК")
    if not isinstance(review_note, str) or not 10 <= len(review_note.strip()) <= 2000:
        raise ComplexIdentityReviewError("evidence_required", "Опишите, почему пару нужно проверить")
    if not isinstance(nominated_by, str) or not nominated_by.strip():
        raise ComplexIdentityReviewError("evidence_required", "Не указан автор кандидата")
    urls = [] if source_urls is None else source_urls
    if not isinstance(urls, list) or len(urls) > 5 or any(not isinstance(url, str) or len(url) > 2048 for url in urls):
        raise ComplexIdentityReviewError("evidence_required", "Некорректные ссылки кандидата")
    a, b = sorted((complex_id_a, complex_id_b))

    async def insert(conn):
        rows = [dict(row) for row in await conn.fetch(
            "SELECT id,name,canonical_id,is_garbage,is_street,canonical_reason FROM complexes WHERE id=ANY($1::int[])", [a,b])]
        if len(rows) != 2:
            raise ComplexIdentityReviewError("not_found", "Одна из записей ЖК отсутствует")
        if any(row["canonical_id"] is not None or row["is_garbage"] or row["is_street"] or
               row["canonical_reason"] == "junk_unmatched" for row in rows):
            raise ComplexIdentityReviewError("identity_conflict", "Проверять нужно действующие самостоятельные записи ЖК")
        evidence = {"hypothesis": "unconfirmed_manual_review", "review_note": review_note.strip(),
                    "source_urls": urls, "source_urls_verified": False,
                    "nominated_by": nominated_by, "canonical_decision_made": False}
        row = await conn.fetchrow("""
            INSERT INTO complex_duplicate_candidates(complex_id_a,complex_id_b,translit_key,
                reason,evidence,method,status)
            VALUES($1,$2,'manual_pair','identity_manual_review',$3::jsonb,$4,'review')
            ON CONFLICT(complex_id_a,complex_id_b) DO NOTHING RETURNING id,status
        """, a,b,_json(evidence), VERSION)
        if row is None:
            existing = await conn.fetchrow(
                "SELECT id,status FROM complex_duplicate_candidates WHERE complex_id_a=$1 AND complex_id_b=$2", a,b)
            return {"candidate_id": existing["id"], "status": existing["status"], "created": False}
        return {"candidate_id": row["id"], "status": row["status"], "created": True}
    if connection is not None:
        async with connection.transaction():
            return await insert(connection)
    from bot.db.pg import get_pool
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            return await insert(conn)



