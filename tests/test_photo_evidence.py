"""Регрессия для bot/identity/photo_evidence.py (задача 2026-08-17,
"Property Identity — photo evidence + admin review queue", часть B/F).
Тестовые строки — '__test_...__' id, удаляются в finally."""
import io
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest
import pytest_asyncio
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

from bot.identity import photo_evidence as pe


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


# ── Синтетические изображения (реальный imagehash.phash, не заглушка) ────

def _make_image_bytes(seed: int, size=(64, 64)) -> bytes:
    rng = np.random.RandomState(seed)
    arr = rng.randint(0, 255, size=(size[1], size[0], 3), dtype=np.uint8)
    img = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _resize_bytes(data: bytes, scale: float) -> bytes:
    """Симулирует screenshot/resize того же изображения — тот класс
    трансформации, который perceptual hash (imagehash.phash) должен
    пережить (задача F: 'AI/embedding должен находить screenshot, crop,
    resize... одного изображения' — здесь конкретно resize, phash-уровень)."""
    img = Image.open(io.BytesIO(data))
    w, h = img.size
    img2 = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    buf = io.BytesIO()
    img2.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _fp(url: str, data: bytes | None = None, embedding: np.ndarray | None = None,
        photo_type: str | None = None, fetch_status: str = "ok") -> dict:
    import hashlib
    d = {"photo_url": url, "fetch_status": fetch_status, "photo_type": photo_type}
    if data is not None:
        d["sha256"] = hashlib.sha256(data).hexdigest()
        d["phash"] = pe.compute_image_hash(data)
    else:
        d["sha256"] = None
        d["phash"] = None
    d["embedding"] = pe.pack_embedding(embedding) if embedding is not None else None
    return d


# ── 1. Exact photo bytes ───────────────────────────────────────────────────

def test_exact_shared_bytes_counted_as_exact():
    data = _make_image_bytes(1)
    a = [_fp("a1.jpg", data)]
    b = [_fp("b1.jpg", data)]  # ТЕ ЖЕ байты, другой URL
    result = pe.compare_fingerprints(a, b)
    assert result["exact_shared_count"] == 1
    assert result["perceptual_shared_count"] == 0
    assert result["ai_similar_count"] == 0
    assert result["matched_photos"][0]["method"] == "exact"


# ── 2. Resize/screenshot/crop -> perceptual (НЕ exact, sha256 отличается) ──

def test_resized_photo_matches_as_perceptual_not_exact():
    original = _make_image_bytes(2)
    resized = _resize_bytes(original, 0.5)  # "screenshot"-подобное уменьшение
    a = [_fp("a1.jpg", original)]
    b = [_fp("b1.jpg", resized)]
    result = pe.compare_fingerprints(a, b)
    assert result["exact_shared_count"] == 0  # разные байты после resize
    assert result["perceptual_shared_count"] == 1  # но phash достаточно близок
    assert result["matched_photos"][0]["method"] == "perceptual"


def test_completely_different_photos_do_not_match():
    a = [_fp("a1.jpg", _make_image_bytes(10))]
    b = [_fp("b1.jpg", _make_image_bytes(999))]
    result = pe.compare_fingerprints(a, b)
    assert result["exact_shared_count"] == 0
    assert result["perceptual_shared_count"] == 0
    assert result["ai_similar_count"] == 0
    assert result["matched_photos"] == []


# ── 3. Одинаковый общий рендер/план НЕ подтверждает квартиру ──────────────

def test_shared_render_counts_as_common_not_unit_specific():
    data = _make_image_bytes(3)
    a = [_fp("a1.jpg", data, photo_type="render")]
    b = [_fp("b1.jpg", data, photo_type="render")]
    result = pe.compare_fingerprints(a, b)
    assert result["exact_shared_count"] == 1
    assert result["shared_unit_specific_count"] == 0
    assert result["shared_common_count"] == 1


def test_shared_floorplan_also_counts_as_common():
    """Типовая планировка — задача, явно: слабый сигнал, НЕ unit-specific,
    даже если это тот же файл (одна и та же типовая планировка ЖК)."""
    data = _make_image_bytes(4)
    a = [_fp("a1.jpg", data, photo_type="floorplan")]
    b = [_fp("b1.jpg", data, photo_type="floorplan")]
    result = pe.compare_fingerprints(a, b)
    assert result["shared_unit_specific_count"] == 0
    assert result["shared_common_count"] == 1


# ── 4. Несколько совпавших interior-фото — сильный сигнал ─────────────────

def test_multiple_interior_matches_counted_as_unit_specific():
    imgs = [_make_image_bytes(i) for i in range(3)]
    a = [_fp(f"a{i}.jpg", d, photo_type="interior") for i, d in enumerate(imgs)]
    b = [_fp(f"b{i}.jpg", d, photo_type="interior") for i, d in enumerate(imgs)]
    result = pe.compare_fingerprints(a, b)
    assert result["shared_unit_specific_count"] == 3
    assert result["exact_shared_count"] == 3


def test_view_type_also_counts_as_unit_specific():
    data = _make_image_bytes(5)
    a = [_fp("a1.jpg", data, photo_type="view")]
    b = [_fp("b1.jpg", data, photo_type="view")]
    result = pe.compare_fingerprints(a, b)
    assert result["shared_unit_specific_count"] == 1


def test_mixed_type_match_is_not_unit_specific():
    """Одна сторона interior, другая floorplan — НЕ unit-specific (задача:
    обе стороны должны быть unit-specific типом)."""
    data = _make_image_bytes(6)
    a = [_fp("a1.jpg", data, photo_type="interior")]
    b = [_fp("b1.jpg", data, photo_type="floorplan")]
    result = pe.compare_fingerprints(a, b)
    assert result["shared_unit_specific_count"] == 0
    assert result["shared_common_count"] == 1


# ── 5. AI-уровень ловит similarity, когда hash не совпадает (watermark/
#      цветокоррекция — задача, явно: "AI должен находить... watermark и
#      небольшую цветокоррекцию") ─────────────────────────────────────────

def test_ai_tier_catches_similar_embedding_when_hashes_differ():
    emb_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    emb_b = np.array([0.97, 0.02, 0.01], dtype=np.float32)  # почти то же направление
    a = [_fp("a1.jpg", _make_image_bytes(20), embedding=emb_a)]
    b = [_fp("b1.jpg", _make_image_bytes(21), embedding=emb_b)]  # разные байты/phash
    result = pe.compare_fingerprints(a, b)
    assert result["exact_shared_count"] == 0
    assert result["perceptual_shared_count"] == 0
    assert result["ai_similar_count"] == 1
    assert result["max_similarity"] > pe.AI_SIMILARITY_THRESHOLD


def test_ai_tier_does_not_fire_below_threshold():
    emb_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    emb_b = np.array([0.0, 1.0, 0.0], dtype=np.float32)  # ортогонален
    a = [_fp("a1.jpg", _make_image_bytes(22), embedding=emb_a)]
    b = [_fp("b1.jpg", _make_image_bytes(23), embedding=emb_b)]
    result = pe.compare_fingerprints(a, b)
    assert result["ai_similar_count"] == 0


# ── 6. Приоритет уровней и greedy-безповторное сопоставление ──────────────

def test_exact_wins_over_perceptual_and_ai_for_same_pair():
    data = _make_image_bytes(30)
    emb = np.array([1.0, 0.0], dtype=np.float32)
    a = [_fp("a1.jpg", data, embedding=emb)]
    b = [_fp("b1.jpg", data, embedding=emb)]  # тот же файл -> exact ДОЛЖЕН победить
    result = pe.compare_fingerprints(a, b)
    assert result["matched_photos"][0]["method"] == "exact"
    assert result["ai_similar_count"] == 0


def test_each_photo_matched_at_most_once_no_double_counting():
    """3 фото стороны A, идентичных МЕЖДУ СОБОЙ (частый кейс — Крыша
    отдаёт несколько размеров одного кадра), 1 фото стороны B — НЕ 3
    совпадения, максимум 1 (greedy, used_b защищает от повторного
    использования той же b-фотографии)."""
    data = _make_image_bytes(40)
    a = [_fp(f"a{i}.jpg", data) for i in range(3)]
    b = [_fp("b1.jpg", data)]
    result = pe.compare_fingerprints(a, b)
    assert result["exact_shared_count"] == 1


# ── 7. Пустые/отсутствующие данные — не падает ────────────────────────────

def test_empty_sides_return_zero_counts():
    result = pe.compare_fingerprints([], [])
    assert result["exact_shared_count"] == 0
    assert result["matched_photos"] == []
    assert result["max_similarity"] is None


# ── 8. pack/unpack/cosine — round-trip ─────────────────────────────────────

def test_pack_unpack_embedding_roundtrip():
    vec = np.array([3.0, 4.0], dtype=np.float32)
    packed = pe.pack_embedding(vec)
    unpacked = pe.unpack_embedding(packed)
    assert np.allclose(unpacked, [0.6, 0.8], atol=1e-5)


def test_cosine_similarity_identical_vectors_is_one():
    vec = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    packed = pe.pack_embedding(vec)
    assert pe.cosine_similarity(packed, packed) == pytest.approx(1.0, abs=1e-5)


def test_cosine_similarity_none_when_missing():
    valid = pe.pack_embedding(np.array([1.0, 2.0], dtype=np.float32))
    assert pe.cosine_similarity(None, valid) is None
    assert pe.cosine_similarity(valid, None) is None
    assert pe.cosine_similarity(None, None) is None


def test_cache_path_deterministic_same_url():
    p1 = pe.cache_path("https://example.com/a.jpg")
    p2 = pe.cache_path("https://example.com/a.jpg")
    p3 = pe.cache_path("https://example.com/b.jpg")
    assert p1 == p2
    assert p1 != p3


# ── 9. save_candidate_evidence — идемпотентный UPSERT (задача: "пересчёт
#      должен быть идемпотентным и версионированным") ────────────────────

@pytest_asyncio.fixture
async def candidate_pair(db):
    from bot.db.pg import execute, fetchval
    cid = await fetchval(
        "INSERT INTO complexes (name) VALUES ('__test_pe_complex__') RETURNING id")
    await execute("""
        INSERT INTO apartment_listings (id, url, address, floor, area, complex_name, photos)
        VALUES ('__test_pe_a__', 'https://krisha.kz/test/a', 'Тест Адрес, 1', 5, 45.0,
                '__test_pe_complex__', '[]'::jsonb)
    """)
    await execute("""
        INSERT INTO apartment_listings (id, url, address, floor, area, complex_name, photos)
        VALUES ('__test_pe_b__', 'https://krisha.kz/test/b', 'Тест Адрес, 1', 5, 45.0,
                '__test_pe_complex__', '[]'::jsonb)
    """)
    prop_id = await fetchval(
        "INSERT INTO properties (complex_id, address_hash, floor, area_sqm) "
        "VALUES ($1, '__test_pe_hash__', 5, 45.0) RETURNING property_id", cid)
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
        "VALUES ($1, '__test_pe_b__', 'bootstrap', 1.0)", prop_id)
    candidate_id = await fetchval("""
        INSERT INTO property_match_candidates
            (listing_id, candidate_property_id, match_method, match_score, matcher_version, status)
        VALUES ('__test_pe_a__', $1, 'exact_hash', 0.9, 'candidate_only_v2', 'pending')
        RETURNING candidate_id
    """, prop_id)
    try:
        yield {"candidate_id": candidate_id, "property_id": prop_id, "listing_a": "__test_pe_a__",
               "listing_b": "__test_pe_b__"}
    finally:
        await execute("DELETE FROM property_candidate_photo_evidence WHERE candidate_id = $1", candidate_id)
        await execute("DELETE FROM property_match_review_log WHERE candidate_id = $1", candidate_id)
        await execute("DELETE FROM property_match_candidates WHERE candidate_id = $1", candidate_id)
        await execute("DELETE FROM property_listings WHERE property_id = $1", prop_id)
        await execute("DELETE FROM properties WHERE property_id = $1", prop_id)
        await execute("DELETE FROM listing_photo_fingerprints WHERE listing_id = ANY($1::text[])",
                       ["__test_pe_a__", "__test_pe_b__"])
        await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])",
                       ["__test_pe_a__", "__test_pe_b__"])
        await execute("DELETE FROM complexes WHERE id = $1", cid)


@pytest.mark.asyncio
async def test_save_candidate_evidence_upsert_is_idempotent(candidate_pair):
    from bot.db.pg import fetchrow
    cid = candidate_pair["candidate_id"]

    evidence1 = {"exact_shared_count": 2, "perceptual_shared_count": 0, "ai_similar_count": 0,
                 "shared_unit_specific_count": 1, "shared_common_count": 1, "max_similarity": 0.5,
                 "matched_photos": [], "processing_status": "ok"}
    await pe.save_candidate_evidence(cid, 5, 5, evidence1)
    row1 = await fetchrow("SELECT * FROM property_candidate_photo_evidence WHERE candidate_id=$1", cid)
    assert row1["exact_shared_count"] == 2

    # Второй вызов с ДРУГИМИ числами — ОДНА строка, значения обновлены,
    # не задвоена (candidate_id PRIMARY KEY + ON CONFLICT DO UPDATE).
    evidence2 = {**evidence1, "exact_shared_count": 7}
    await pe.save_candidate_evidence(cid, 5, 5, evidence2)
    rows = await fetchrow("SELECT count(*) AS c FROM property_candidate_photo_evidence WHERE candidate_id=$1", cid)
    assert rows["c"] == 1
    row2 = await fetchrow("SELECT * FROM property_candidate_photo_evidence WHERE candidate_id=$1", cid)
    assert row2["exact_shared_count"] == 7


# ── 10. fingerprint_listing_photos — идемпотентность (не перекачивает
#       уже готовые фото) ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fingerprint_listing_photos_skips_already_fetched(candidate_pair, tmp_path, monkeypatch):
    from bot.db.pg import execute, fetchval

    url = "https://example.com/__test_pe_photo__.jpg"
    await execute(
        "UPDATE apartment_listings SET photos = $2::jsonb WHERE id = $1",
        candidate_pair["listing_a"], f'["{url}"]',
    )

    call_count = {"n": 0}

    async def _fake_download(u, http_client=None):
        call_count["n"] += 1
        return _make_image_bytes(50)

    with patch("bot.identity.photo_evidence.download_photo", new=_fake_download):
        r1 = await pe.fingerprint_listing_photos(candidate_pair["listing_a"])
        assert r1["fetched"] == 1
        assert call_count["n"] == 1

        r2 = await pe.fingerprint_listing_photos(candidate_pair["listing_a"])
        assert r2["fetched"] == 0  # уже 'ok' — не перекачано
        assert call_count["n"] == 1  # download_photo НЕ вызван повторно

    got = await fetchval(
        "SELECT sha256 FROM listing_photo_fingerprints WHERE listing_id=$1 AND photo_url=$2",
        candidate_pair["listing_a"], url,
    )
    assert got is not None
    await execute("DELETE FROM listing_photo_fingerprints WHERE listing_id=$1", candidate_pair["listing_a"])


@pytest.mark.asyncio
async def test_recompressed_ad_phash_is_removed_before_fingerprinting(candidate_pair):
    from bot.db.pg import execute, fetchval

    url = "https://example.com/__test_recompressed_ad__.jpg"
    await execute("UPDATE apartment_listings SET photos = $2::jsonb WHERE id = $1",
                  candidate_pair["listing_a"], f'["{url}"]')
    jpeg_variant = _resize_bytes(_make_image_bytes(71), 0.5)

    async def _fake_download(u, http_client=None):
        return jpeg_variant

    with patch("bot.identity.photo_evidence.download_photo", new=_fake_download), \
         patch("bot.identity.photo_evidence.compute_image_hash", return_value="f8f4cf81dc17200f"):
        result = await pe.fingerprint_listing_photos(candidate_pair["listing_a"])

    assert result["fetched"] == 0
    assert await fetchval("SELECT count(*) FROM listing_photo_fingerprints WHERE listing_id=$1 AND photo_url=$2",
                          candidate_pair["listing_a"], url) == 0
    assert await fetchval("SELECT photos::text FROM apartment_listings WHERE id=$1",
                          candidate_pair["listing_a"]) == "[]"
    assert await fetchval("SELECT reason FROM blocked_photo_urls WHERE url=$1", url) == "known_ad_fingerprint"
    await execute("DELETE FROM blocked_photo_urls WHERE url=$1", url)


# ── 11. aggregate_candidate_evidence — end-to-end (mocked download) ───────

@pytest.mark.asyncio
async def test_aggregate_candidate_evidence_end_to_end_exact_match(candidate_pair):
    from bot.db.pg import execute, fetchrow

    shared_url_a = "https://example.com/__test_pe_shared_a__.jpg"
    shared_url_b = "https://example.com/__test_pe_shared_b__.jpg"
    same_bytes = _make_image_bytes(60)
    await execute("UPDATE apartment_listings SET photos = $2::jsonb WHERE id = $1",
                  candidate_pair["listing_a"], f'["{shared_url_a}"]')
    await execute("UPDATE apartment_listings SET photos = $2::jsonb WHERE id = $1",
                  candidate_pair["listing_b"], f'["{shared_url_b}"]')

    async def _fake_download(u, http_client=None):
        return same_bytes  # ОБЕ стороны получают одинаковые байты -> exact match

    with patch("bot.identity.photo_evidence.download_photo", new=_fake_download):
        evidence = await pe.aggregate_candidate_evidence(candidate_pair["candidate_id"])

    assert evidence["exact_shared_count"] == 1

    row = await fetchrow(
        "SELECT * FROM property_candidate_photo_evidence WHERE candidate_id=$1",
        candidate_pair["candidate_id"])
    assert row is not None
    assert row["exact_shared_count"] == 1
    assert row["photo_count_a"] == 1
    assert row["photo_count_b"] == 1

    await execute("DELETE FROM listing_photo_fingerprints WHERE listing_id = ANY($1::text[])",
                  [candidate_pair["listing_a"], candidate_pair["listing_b"]])


@pytest.mark.asyncio
async def test_aggregate_candidate_evidence_dry_run_does_not_persist(candidate_pair):
    from bot.db.pg import execute, fetchrow

    url_a = "https://example.com/__test_pe_dry_a__.jpg"
    url_b = "https://example.com/__test_pe_dry_b__.jpg"
    same_bytes = _make_image_bytes(61)
    await execute("UPDATE apartment_listings SET photos = $2::jsonb WHERE id = $1",
                  candidate_pair["listing_a"], f'["{url_a}"]')
    await execute("UPDATE apartment_listings SET photos = $2::jsonb WHERE id = $1",
                  candidate_pair["listing_b"], f'["{url_b}"]')

    async def _fake_download(u, http_client=None):
        return same_bytes

    with patch("bot.identity.photo_evidence.download_photo", new=_fake_download):
        evidence = await pe.aggregate_candidate_evidence(candidate_pair["candidate_id"], dry_run=True)

    assert evidence["exact_shared_count"] == 1
    row = await fetchrow(
        "SELECT * FROM property_candidate_photo_evidence WHERE candidate_id=$1",
        candidate_pair["candidate_id"])
    assert row is None  # dry-run — evidence НЕ записана

    await execute("DELETE FROM listing_photo_fingerprints WHERE listing_id = ANY($1::text[])",
                  [candidate_pair["listing_a"], candidate_pair["listing_b"]])


# ── 12. Confirmed advertisement fingerprints are hard exclusions ───────────

def test_confirmed_ad_fingerprints_are_blocked_by_sha_phash_and_url():
    assert pe.is_blocked_photo_fingerprint({
        "photo_url": "https://cdn.example/ad.jpg",
        "sha256": "db30b8758249cf797d8df5afe308ef91b8dae2c5f863d486dc6b6b4c3a280862",
        "phash": None,
    })
    assert pe.is_blocked_photo_fingerprint({
        "photo_url": "https://cdn.example/reencoded-ad.jpg",
        "sha256": "different-bytes",
        "phash": "e0ce2517dbe40ae9",
    })
    assert pe.is_blocked_photo_fingerprint({
        "photo_url": "https://cdn.example/recompressed-ad.jpg",
        "sha256": "36c2109ebf8a2e02ef90f9e70cc93aa391082a22782bc5045f19de7b88c54475",
        "phash": "different-phash",
    })
    assert pe.is_blocked_photo_fingerprint(
        {"photo_url": "https://cdn.example/blocked-url.jpg"},
        blocked_urls=frozenset({"https://cdn.example/blocked-url.jpg"}),
    )
    assert not pe.is_blocked_photo_fingerprint({
        "photo_url": "https://cdn.example/real.jpg", "sha256": "real", "phash": "real"
    })
