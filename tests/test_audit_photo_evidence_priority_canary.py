"""Регрессия для scripts/audit_photo_evidence_priority_canary.py (задача
2026-08-18, follow-up "Property Identity — calibration validation", п.7:
"то, что не менялись bot/*.py, не отменяет тестирование нового
scoped-write скрипта"). Тестовые строки — '__test_...__' id, удаляются в
finally (тот же паттерн, что tests/test_photo_evidence.py, откуда
переиспользован _fake_download/synthetic-image паттерн).

Прод-таблица property_match_candidates содержит десятки тысяч реальных
pending-строк — тесты приоритета изолируются одним из двух способов:
либо большим --limit + фильтрация результата до СВОИХ candidate_id
(проверяем ОТНОСИТЕЛЬНЫЙ порядок, не "первый во всей очереди"), либо
прямым monkeypatch `_select_priority_candidates`, чтобы `run()` видел
ТОЛЬКО синтетический набор — тот же принцип, что уже применяют
test_queue_priority_* в tests/test_property_match_review.py."""
import io
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import numpy as np
import pytest
import pytest_asyncio
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

import audit_photo_evidence_priority_canary as canary  # noqa: E402


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


def _make_image_bytes(seed: int, size=(48, 48)) -> bytes:
    rng = np.random.RandomState(seed)
    arr = rng.randint(0, 255, size=(size[1], size[0], 3), dtype=np.uint8)
    img = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


async def _insert_listing(lid, address, photos=None):
    import json as _json
    from bot.db.pg import execute
    await execute(
        "INSERT INTO apartment_listings (id, url, address, floor, area, photos) "
        "VALUES ($1, $2, $3, 5, 45.0, $4::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET address=$3, photos=$4::jsonb",
        lid, f"https://krisha.kz/test/{lid}", address, _json.dumps(photos or []),
    )


async def _cleanup(*listing_ids, property_ids=()):
    from bot.db.pg import execute
    await execute("DELETE FROM property_match_review_log WHERE listing_id = ANY($1::text[])", list(listing_ids))
    await execute("DELETE FROM property_candidate_photo_evidence WHERE candidate_id IN "
                  "(SELECT candidate_id FROM property_match_candidates WHERE listing_id = ANY($1::text[]))",
                  list(listing_ids))
    await execute("DELETE FROM property_match_candidates WHERE listing_id = ANY($1::text[])", list(listing_ids))
    await execute("DELETE FROM listing_photo_fingerprints WHERE listing_id = ANY($1::text[])", list(listing_ids))
    await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", list(listing_ids))
    if property_ids:
        await execute("DELETE FROM properties WHERE property_id = ANY($1::int[])", list(property_ids))
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(listing_ids))


async def _make_candidate(listing_id, property_id, match_method="fuzzy", match_score=0.9,
                           corroborating=None):
    import json as _json
    from bot.db.pg import fetchval
    evidence = {"corroborating_methods": corroborating} if corroborating else {}
    return await fetchval(
        """
        INSERT INTO property_match_candidates
            (listing_id, candidate_property_id, match_method, match_score, matcher_version, status, evidence)
        VALUES ($1, $2, $3, $4, 'candidate_only_v2', 'pending', $5::jsonb)
        RETURNING candidate_id
        """,
        listing_id, property_id, match_method, match_score, _json.dumps(evidence),
    )


@pytest_asyncio.fixture
async def priority_set(db):
    """4 синтетических pending-кандидата, покрывающих все 4 приоритетных
    тира — общая target-property, разные listing_id с одной стороны."""
    lids = ["__test_apc_multi__", "__test_apc_exact__", "__test_apc_dedup__",
            "__test_apc_fuzzy_hi__", "__test_apc_fuzzy_lo__", "__test_apc_target__"]
    for lid in lids:
        await _insert_listing(lid, address="Приоритет, 1")
    from bot.db.pg import execute, fetchval
    prop = await fetchval(
        "INSERT INTO properties (address_hash, floor, area_sqm) VALUES "
        "('__test_apc_hash__', 5, 45.0) RETURNING property_id")
    await execute("INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
                  "VALUES ($1, '__test_apc_target__', 'bootstrap', 1.0)", prop)

    cand_multi = await _make_candidate("__test_apc_multi__", prop, "fuzzy", 0.99,
                                        corroborating=["fuzzy", "dedup_listings"])
    cand_exact = await _make_candidate("__test_apc_exact__", prop, "exact_hash", 0.99)
    cand_dedup = await _make_candidate("__test_apc_dedup__", prop, "dedup_listings", 0.99)
    cand_fuzzy_hi = await _make_candidate("__test_apc_fuzzy_hi__", prop, "fuzzy", 0.95)
    cand_fuzzy_lo = await _make_candidate("__test_apc_fuzzy_lo__", prop, "fuzzy", 0.50)

    try:
        yield {"multi": cand_multi, "exact": cand_exact, "dedup": cand_dedup,
               "fuzzy_hi": cand_fuzzy_hi, "fuzzy_lo": cand_fuzzy_lo, "property_id": prop,
               "lids": lids}
    finally:
        await _cleanup(*lids, property_ids=[prop])


@pytest_asyncio.fixture
async def one_candidate(db):
    """Один pending-кандидат с реальными (мокнутыми на скачивание)
    фотографиями — для dry-run/no-status-change/no-physical-merge/
    only-allowed-fields тестов."""
    lid_a, lid_b = "__test_apc_pair_a__", "__test_apc_pair_b__"
    url_a, url_b = "https://example.com/__test_apc_a__.jpg", "https://example.com/__test_apc_b__.jpg"
    await _insert_listing(lid_a, "Пара, 1", photos=[url_a])
    await _insert_listing(lid_b, "Пара, 1", photos=[url_b])
    from bot.db.pg import execute, fetchval
    prop = await fetchval(
        "INSERT INTO properties (address_hash, floor, area_sqm) VALUES "
        "('__test_apc_pair_hash__', 5, 45.0) RETURNING property_id")
    await execute("INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
                  "VALUES ($1, $2, 'bootstrap', 1.0)", prop, lid_b)
    cid = await _make_candidate(lid_a, prop)
    try:
        yield {"candidate_id": cid, "listing_a": lid_a, "listing_b": lid_b,
               "url_a": url_a, "url_b": url_b, "property_id": prop}
    finally:
        await _cleanup(lid_a, lid_b, property_ids=[prop])


# ── 1. Приоритет отбора: 2+ сигнала > exact_hash > dedup_listings > fuzzy(score) ──

@pytest.mark.asyncio
async def test_priority_ordering(priority_set):
    rows = await canary._select_priority_candidates(limit=200_000, only_missing=False)
    ids_in_order = [r["candidate_id"] for r in rows]

    expected_order = [priority_set["multi"], priority_set["exact"], priority_set["dedup"],
                       priority_set["fuzzy_hi"], priority_set["fuzzy_lo"]]
    positions = [ids_in_order.index(cid) for cid in expected_order]
    assert positions == sorted(positions), (
        "приоритет должен быть: >=2 corroborating -> exact_hash -> dedup_listings -> "
        "fuzzy(score desc), получено positions=%s" % positions)


# ── 2. --limit ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_limit_caps_selection(priority_set):
    """НЕ полагается на наполненность прод-БД (найдено на fresh Postgres
    15 в CI: 0 pending на чистой базе -> len(rows)==0 при limit=3, ложное
    падение) — priority_set сам гарантирует >=5 pending-строк, limit=3
    должен вернуть РОВНО 3 независимо от того, что ещё есть в БД."""
    rows = await canary._select_priority_candidates(limit=3, only_missing=False)
    assert len(rows) == 3


# ── 3. dry-run не пишет evidence ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_does_not_persist_evidence(one_candidate):
    from bot.db.pg import fetchrow

    same_bytes = _make_image_bytes(101)

    async def _fake_download(u, http_client=None):
        return same_bytes

    fake_row = {"candidate_id": one_candidate["candidate_id"], "listing_id": one_candidate["listing_a"],
                "candidate_property_id": one_candidate["property_id"], "match_method": "exact_hash",
                "match_score": 0.9}

    with patch.object(canary, "_select_priority_candidates", return_value=[fake_row]), \
         patch("bot.identity.photo_evidence.download_photo", new=_fake_download):
        stats = await canary.run(limit=1, dry_run=True, delay=0.0, only_missing=False, batch_size=1)

    assert stats["processed"] == 1
    assert stats["errors"] == 0
    row = await fetchrow("SELECT * FROM property_candidate_photo_evidence WHERE candidate_id=$1",
                          one_candidate["candidate_id"])
    assert row is None  # dry-run -> ничего не записано

    from bot.db.pg import execute
    await execute("DELETE FROM listing_photo_fingerprints WHERE listing_id = ANY($1::text[])",
                  [one_candidate["listing_a"], one_candidate["listing_b"]])


# ── 4. Идемпотентный resume: --only-missing пропускает уже 'ok' ─────────

@pytest.mark.asyncio
async def test_only_missing_skips_already_ok(priority_set):
    from bot.db.pg import execute
    # Симулируем "уже обработано в предыдущем запуске" — processing_status='ok'.
    await execute("""
        INSERT INTO property_candidate_photo_evidence (candidate_id, model_version, processing_status)
        VALUES ($1, 'test_v1', 'ok')
    """, priority_set["exact"])

    rows_only_missing = await canary._select_priority_candidates(limit=200_000, only_missing=True)
    ids = {r["candidate_id"] for r in rows_only_missing}
    assert priority_set["exact"] not in ids  # уже 'ok' -> пропущен
    assert priority_set["multi"] in ids       # ещё не обработан -> присутствует

    rows_all = await canary._select_priority_candidates(limit=200_000, only_missing=False)
    ids_all = {r["candidate_id"] for r in rows_all}
    assert priority_set["exact"] in ids_all  # без only_missing — снова виден

    await execute("DELETE FROM property_candidate_photo_evidence WHERE candidate_id=$1", priority_set["exact"])


# ── 5. Повторный запуск — согласованный результат (UPSERT, не дублирование) ──

@pytest.mark.asyncio
async def test_repeated_run_is_idempotent_no_duplicate_rows(one_candidate):
    from bot.db.pg import fetchval

    same_bytes = _make_image_bytes(102)

    async def _fake_download(u, http_client=None):
        return same_bytes

    fake_row = {"candidate_id": one_candidate["candidate_id"], "listing_id": one_candidate["listing_a"],
                "candidate_property_id": one_candidate["property_id"], "match_method": "exact_hash",
                "match_score": 0.9}

    with patch.object(canary, "_select_priority_candidates", return_value=[fake_row]), \
         patch("bot.identity.photo_evidence.download_photo", new=_fake_download):
        await canary.run(limit=1, dry_run=False, delay=0.0, only_missing=False, batch_size=1)
        await canary.run(limit=1, dry_run=False, delay=0.0, only_missing=False, batch_size=1)

    n_rows = await fetchval(
        "SELECT count(*) FROM property_candidate_photo_evidence WHERE candidate_id=$1",
        one_candidate["candidate_id"])
    assert n_rows == 1  # UPSERT — вторая строка не создаётся

    from bot.db.pg import execute
    await execute("DELETE FROM listing_photo_fingerprints WHERE listing_id = ANY($1::text[])",
                  [one_candidate["listing_a"], one_candidate["listing_b"]])


# ── 6. STOP при error rate > 5% ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_error_rate_stop_gate(db):
    # 60 фиктивных candidate_id (не обязаны существовать в БД — aggregate_
    # candidate_evidence мокнута целиком, реальных запросов не будет).
    fake_rows = [{"candidate_id": i, "listing_id": f"x{i}", "candidate_property_id": 1,
                  "match_method": "fuzzy", "match_score": 0.5} for i in range(60)]

    call_count = {"n": 0}

    async def _mock_aggregate(candidate_id, **kwargs):
        call_count["n"] += 1
        if call_count["n"] % 2 == 0:  # 50% error rate — точно выше порога 5%
            raise RuntimeError("simulated failure")
        return {"processing_status": "ok", "exact_shared_count": 0, "perceptual_shared_count": 0,
                "ai_similar_count": 0}

    with patch.object(canary, "_select_priority_candidates", return_value=fake_rows), \
         patch("bot.identity.photo_evidence.aggregate_candidate_evidence", new=_mock_aggregate):
        stats = await canary.run(limit=60, dry_run=True, delay=0.0, only_missing=False, batch_size=10)

    assert stats["stopped_early"] is True
    assert stats["stop_reason"] is not None
    assert stats["processed"] < 60  # прервано ДО конца полного набора
    assert stats["processed"] >= canary.MIN_SAMPLE_FOR_GATE  # гейт не судит по первым нескольким


# ── 7. Не меняет candidate status ────────────────────────────────────────

@pytest.mark.asyncio
async def test_does_not_change_candidate_status(one_candidate):
    from bot.db.pg import fetchval

    same_bytes = _make_image_bytes(103)

    async def _fake_download(u, http_client=None):
        return same_bytes

    fake_row = {"candidate_id": one_candidate["candidate_id"], "listing_id": one_candidate["listing_a"],
                "candidate_property_id": one_candidate["property_id"], "match_method": "exact_hash",
                "match_score": 0.9}

    status_before = await fetchval("SELECT status FROM property_match_candidates WHERE candidate_id=$1",
                                    one_candidate["candidate_id"])
    with patch.object(canary, "_select_priority_candidates", return_value=[fake_row]), \
         patch("bot.identity.photo_evidence.download_photo", new=_fake_download):
        await canary.run(limit=1, dry_run=False, delay=0.0, only_missing=False, batch_size=1)
    status_after = await fetchval("SELECT status FROM property_match_candidates WHERE candidate_id=$1",
                                   one_candidate["candidate_id"])
    assert status_before == status_after == "pending"

    from bot.db.pg import execute
    await execute("DELETE FROM listing_photo_fingerprints WHERE listing_id = ANY($1::text[])",
                  [one_candidate["listing_a"], one_candidate["listing_b"]])


# ── 8. Не выполняет physical merge ───────────────────────────────────────

@pytest.mark.asyncio
async def test_no_physical_merge(one_candidate):
    from bot.db.pg import fetchval

    same_bytes = _make_image_bytes(104)

    async def _fake_download(u, http_client=None):
        return same_bytes

    fake_row = {"candidate_id": one_candidate["candidate_id"], "listing_id": one_candidate["listing_a"],
                "candidate_property_id": one_candidate["property_id"], "match_method": "exact_hash",
                "match_score": 0.9}

    pl_before = await fetchval("SELECT count(*) FROM property_listings")
    p_before = await fetchval("SELECT count(*) FROM properties")
    with patch.object(canary, "_select_priority_candidates", return_value=[fake_row]), \
         patch("bot.identity.photo_evidence.download_photo", new=_fake_download):
        await canary.run(limit=1, dry_run=False, delay=0.0, only_missing=False, batch_size=1)
    pl_after = await fetchval("SELECT count(*) FROM property_listings")
    p_after = await fetchval("SELECT count(*) FROM properties")
    assert pl_after == pl_before
    assert p_after == p_before

    from bot.db.pg import execute
    await execute("DELETE FROM listing_photo_fingerprints WHERE listing_id = ANY($1::text[])",
                  [one_candidate["listing_a"], one_candidate["listing_b"]])


# ── 9. Пишет только разрешённые photo-evidence поля ──────────────────────

@pytest.mark.asyncio
async def test_writes_only_photo_evidence_tables(one_candidate):
    from bot.db.pg import fetchval

    same_bytes = _make_image_bytes(105)

    async def _fake_download(u, http_client=None):
        return same_bytes

    fake_row = {"candidate_id": one_candidate["candidate_id"], "listing_id": one_candidate["listing_a"],
                "candidate_property_id": one_candidate["property_id"], "match_method": "exact_hash",
                "match_score": 0.9}

    review_log_before = await fetchval("SELECT count(*) FROM property_match_review_log")
    with patch.object(canary, "_select_priority_candidates", return_value=[fake_row]), \
         patch("bot.identity.photo_evidence.download_photo", new=_fake_download):
        await canary.run(limit=1, dry_run=False, delay=0.0, only_missing=False, batch_size=1)
    review_log_after = await fetchval("SELECT count(*) FROM property_match_review_log")
    assert review_log_before == review_log_after  # журнал решений не тронут

    evidence_row = await fetchval(
        "SELECT count(*) FROM property_candidate_photo_evidence WHERE candidate_id=$1",
        one_candidate["candidate_id"])
    fingerprint_rows = await fetchval(
        "SELECT count(*) FROM listing_photo_fingerprints WHERE listing_id = ANY($1::text[])",
        [one_candidate["listing_a"], one_candidate["listing_b"]])
    assert evidence_row == 1
    assert fingerprint_rows >= 1  # ожидаемые побочные таблицы — единственное, что изменилось

    from bot.db.pg import execute
    await execute("DELETE FROM listing_photo_fingerprints WHERE listing_id = ANY($1::text[])",
                  [one_candidate["listing_a"], one_candidate["listing_b"]])


# ── 10. Продолжение после прерывания (resume picks up where it left off) ──

@pytest.mark.asyncio
async def test_resume_after_interruption_continues_correctly(priority_set):
    """Первый 'запуск' обрабатывает только 'exact' и помечает его 'ok'
    (симулируя завершённую AI-стадию) — второй запуск с only_missing
    должен подхватить ОСТАЛЬНЫЕ кандидаты, не трогая уже готовый."""
    from bot.db.pg import execute, fetchval

    await execute("""
        INSERT INTO property_candidate_photo_evidence (candidate_id, model_version, processing_status)
        VALUES ($1, 'test_v1', 'ok')
    """, priority_set["exact"])

    rows = await canary._select_priority_candidates(limit=200_000, only_missing=True)
    ids = {r["candidate_id"] for r in rows}
    assert priority_set["exact"] not in ids
    for key in ("multi", "dedup", "fuzzy_hi", "fuzzy_lo"):
        assert priority_set[key] in ids

    await execute("DELETE FROM property_candidate_photo_evidence WHERE candidate_id=$1", priority_set["exact"])
