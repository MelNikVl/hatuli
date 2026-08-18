"""bot/identity/photo_evidence.py — задача 2026-08-17, "Property Identity —
photo evidence + admin review queue", часть B: fingerprints фотографий +
per-candidate evidence. Property Identity сейчас в match_mode=
"candidate_only" (bot/identity/property_linker.py) — НИКАКОГО автоматического
merge здесь тоже нет, эта evidence только копится для ручной проверки
(/admin/property-match-review, bot/admin_web.py).

## A. Аудит существующего image-кода (задача, п.A — сделан ПЕРЕД тем, как
писать этот файл, не предположение)

floorplan_scan.py — единственный существующий image-пайплайн в проекте.
Проверено:
  - embeddings: ДА, доступны. `_model.encode_image(image)` (open_clip
    ViT-B-16-SigLIP, pretrained="webli") — уже вызывается для zero-shot
    классификации план/интерьер (косинус к текстовым промптам), но сам
    image-эмбеддинг — обычный нормализованный вектор, ничто не мешает
    сравнивать ДВА эмбеддинга друг с другом (косинус), не только с
    текстом. Тот же backbone здесь переиспользован (scripts/photo_
    evidence_ai_scan.py) — НЕ новая модель.
  - применимость к "то же фото после screenshot/crop/watermark/resize":
    SigLIP — семантический embedding, обучен на естественных
    вариациях кадрирования/масштаба/лёгкой цветокоррекции (та же причина,
    по которой он вообще различает план/интерьер устойчиво к разным
    ракурсам съёмки одной и той же планировки) — качественно подходит
    ЛУЧШЕ, чем pixel-hash, для этого класса трансформаций. НЕ откалибровано
    на размеченном наборе "тот же кадр после screenshot/watermark" в этом
    PR — порог _AI_SIMILARITY_THRESHOLD ниже сознательно консервативен и
    помечен как требующий калибровки на canary (см. отчёт задачи).
  - классификатор «план/не план» САМ ПО СЕБЕ НЕ считается similarity-
    моделью (задача, явно предупреждает не путать) — здесь он не
    переиспользован как есть: 4-промптный zero-shot (план/не план)
    расширен до 6 категорий (см. _PHOTO_TYPE_PROMPTS в scripts/photo_
    evidence_ai_scan.py), а для similarity используется РАЗНАЯ операция
    (image-image cosine), не image-text softmax floorplan_scan.py.
  - URL фотографий хранятся в apartment_listings.photos (JSONB-массив
    строк, CDN URL) — тот же источник, тем же кодом читается
    (bot.core.apartment_details.fetch_apartment_details()["photos"]).
  - скачиваются ОРИГИНАЛЫ по URL из galleries (не отдельные "thumbnail"-
    эндпоинты) — тот же URL, что показывается пользователю; CDN отдаёт
    несколько size-вариантов на фото, apartment_details.py уже выбирает
    "лучший" вариант (-full. > resize, см. _variant_rank) при парсинге,
    здесь используется РОВНО то, что уже сохранено в photos.
  - кэш на диске: floorplan_scan.py уже кэширует скачанные файлы в
    static/cache/photos/<sha256(url)>.jpg — та же директория и ФОРМУЛА
    ИМЕНИ переиспользованы здесь (cache_path() ниже, буквально та же
    формула) — фото, уже скачанное для floorplan-детекции, НЕ качается
    повторно этим пайплайном.

Переиспользовано технически обоснованно: URL-based кэш-путь, HTTP User-
Agent, backbone SigLIP (embeddings), open_clip preprocessing — ВСЁ из
floorplan_scan.py, без копирования его классификационной логики план/не
план как если бы она была similarity-моделью.

## B. Fingerprints + evidence (эта часть — main venv, БЕЗ torch)

sha256/phash считаются здесь (bot.core.dedup.compute_image_hash —
переиспользован, НЕ переизобретён). embedding/photo_type — ТОЛЬКО
scripts/photo_evidence_ai_scan.py (/home/nik/floorplan-clip/venv, там
torch/open_clip) — main venv их не может (см. requirements.txt, torch
отсутствует НАМЕРЕННО, floorplan_scan.py по той же причине — отдельный
процесс/venv). Разделение стадий закодировано в схеме (migrations/088:
computed_at vs ai_computed_at).

## Область сравнения (задача, явно)

"Сравнивать все фотографии ТОЛЬКО внутри уже найденных пар-кандидатов" —
aggregate_candidate_evidence() всегда берёт ОДНУ property_match_candidates
строку, никогда не ищет новые пары по фото. Это дёшево по конструкции:
максимум 15×15 попарных сравнений на кандидата (лимит фото на объявление,
см. bot/core/apartment_details.py), не 50k×50k.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import numpy as np

from bot.core.dedup import compute_image_hash
# _hash_distance — "приватная" (по соглашению об именовании), но это ровно
# та же формула hamming distance, что уже используется для дублей фото
# (bot/core/dedup.py::_are_duplicates, порог <10) — переиспользуем ЕЁ,
# не пишем вторую копию того же 3-строчного сравнения.
from bot.core.dedup import _hash_distance as _phash_hamming_distance

logger = logging.getLogger(__name__)

# Та же директория/формула имени, что floorplan_scan.py::PHOTO_CACHE/
# cache_path() — намеренно совпадает, чтобы делить уже скачанные файлы
# между двумя пайплайнами (floorplan-детекция уже качала часть тех же
# фото для тех же объявлений).
BASE_DIR = Path(__file__).resolve().parents[2]
PHOTO_CACHE = BASE_DIR / "static" / "cache" / "photos"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

_EVIDENCE_VERSION = "photo_evidence_v1"          # версия ЭТОГО модуля (агрегация/пороги)
_EMBEDDING_MODEL_VERSION = "siglip_vit_b16_webli_v1"  # версия AI-стадии (scripts/photo_evidence_ai_scan.py)

# Пороги (задача, "гейты": "модель и пороги обязательно версионировать" —
# версионированы через _EVIDENCE_VERSION/_EMBEDDING_MODEL_VERSION в имени
# model_version строки, см. save_candidate_evidence). НЕ откалиброваны на
# размеченном наборе в этом PR — canary (scripts/photo_evidence_scan.py
# --canary) существует именно чтобы это проверить перед полным прогоном.
PHASH_HAMMING_THRESHOLD = 10       # тот же порог, что bot/core/dedup.py (дубли объявлений)
AI_SIMILARITY_THRESHOLD = 0.90     # cosine, консервативно высокий — уменьшить только по данным canary

_UNIT_SPECIFIC_TYPES = frozenset({"interior", "view"})
_COMMON_TYPES = frozenset({"floorplan", "building_common", "render", "other"})
_ALL_PHOTO_TYPES = _UNIT_SPECIFIC_TYPES | _COMMON_TYPES

# Рекламные/заглушечные изображения, подтверждённые аудитом 2026-08-18.
# CDN перекодирует байты, поэтому проверяем и sha256, и pHash; URL берём
# динамически из blocked_photo_urls. Это hard exclusion на всех стадиях.
BLOCKED_PHOTO_SHA256 = frozenset({
    "db30b8758249cf797d8df5afe308ef91b8dae2c5f863d486dc6b6b4c3a280862",
    "76d0d8ef35582c03ec57fc74a4fcfd6ca942093d94b9cfb344639ba955fc6bfa",
})
BLOCKED_PHOTO_PHASH = frozenset({"f8f4cf81dc17200f", "e0ce2517dbe40ae9"})

_MAX_MATCHED_PHOTOS_STORED = 25  # matched_photos JSONB — не хранить неограниченно


def cache_path(url: str) -> Path:
    """Буквально та же формула, что floorplan_scan.py::cache_path — файлы
    делятся между пайплайнами, см. докстринг модуля, часть A."""
    return PHOTO_CACHE / (hashlib.sha256(url.encode()).hexdigest() + ".jpg")


def is_blocked_photo_fingerprint(fingerprint: dict, *, blocked_urls: frozenset[str] = frozenset()) -> bool:
    """True для рекламного фото по URL или известному fingerprint."""
    return (
        fingerprint.get("photo_url") in blocked_urls
        or fingerprint.get("sha256") in BLOCKED_PHOTO_SHA256
        or fingerprint.get("phash") in BLOCKED_PHOTO_PHASH
    )


async def _blocked_photo_urls() -> frozenset[str]:
    from bot.db.pg import fetch
    return frozenset(row["url"] for row in await fetch("SELECT url FROM blocked_photo_urls"))


def pack_embedding(vec: "np.ndarray") -> bytes:
    """float32[dim], НОРМАЛИЗОВАННЫЙ (единичная норма) — cosine между двумя
    packed-эмбеддингами тогда просто dot product (см. cosine_similarity)."""
    v = np.asarray(vec, dtype=np.float32)
    norm = np.linalg.norm(v)
    if norm > 0:
        v = v / norm
    return v.tobytes()


def unpack_embedding(data: bytes | None) -> "np.ndarray | None":
    if not data:
        return None
    return np.frombuffer(data, dtype=np.float32)


def cosine_similarity(a: bytes | None, b: bytes | None) -> float | None:
    va, vb = unpack_embedding(a), unpack_embedding(b)
    if va is None or vb is None or va.shape != vb.shape:
        return None
    return float(np.dot(va, vb))  # оба уже единичной нормы (pack_embedding) -> dot = cosine


async def download_photo(url: str, *, http_client=None, timeout: float = 20.0) -> bytes:
    """Качает (или берёт из кэша) байты фото. http_client — опциональный
    переданный httpx.AsyncClient (caller держит один клиент на весь батч,
    не пересоздаёт на каждое фото)."""
    p = cache_path(url)
    if p.exists() and p.stat().st_size > 0:
        return p.read_bytes()

    import httpx
    close_after = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await client.get(url, headers={"User-Agent": _UA})
        resp.raise_for_status()
        data = resp.content
        if not data:
            raise ValueError("empty response body")
        PHOTO_CACHE.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return data
    finally:
        if close_after:
            await client.aclose()


async def _listing_photos(listing_id: str) -> list[str]:
    from bot.db.pg import fetchval
    raw = await fetchval("SELECT photos::text FROM apartment_listings WHERE id = $1", listing_id)
    if not raw:
        return []
    try:
        urls = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [u for u in urls if isinstance(u, str) and u.startswith("http")]


async def fingerprint_listing_photos(listing_id: str, *, http_client=None, delay: float = 0.0) -> dict:
    """Скачивает+sha256+phash все фото listing'а, апсертит в listing_photo_
    fingerprints. Идемпотентно: строки с fetch_status='ok' И computed_at
    НЕ NULL — пропускаются (уже посчитаны), не перекачиваются. embedding/
    photo_type НЕ трогает (см. докстринг модуля, стадии отдельные).

    delay — пауза ПЕРЕД каждой РЕАЛЬНОЙ сетевой закачкой (та же идея, что
    floorplan_scan.py::--delay, дефолт 1.0с там) — НЕ применяется к уже
    закэшированным файлам (нет сетевого запроса, нечего придерживать).
    Дефолт 0.0 — тесты/пары с уже тёплым кэшем не должны искусственно
    тормозиться; вызывающий CLI (scripts/photo_evidence_scan.py) передаёт
    реальное значение."""
    import asyncio as _asyncio
    import random as _random

    from bot.db.pg import execute, fetch

    urls = await _listing_photos(listing_id)
    if not urls:
        return {"listing_id": listing_id, "photo_count": 0, "fetched": 0, "failed": 0}

    blocked_urls = await _blocked_photo_urls()
    urls = [url for url in urls if not is_blocked_photo_fingerprint(
        {"photo_url": url}, blocked_urls=blocked_urls)]
    if not urls:
        return {"listing_id": listing_id, "photo_count": 0, "fetched": 0, "failed": 0,
                "skipped_blocked": True}

    already = await fetch(
        "SELECT photo_url FROM listing_photo_fingerprints "
        "WHERE listing_id = $1 AND fetch_status = 'ok' AND computed_at IS NOT NULL",
        listing_id,
    )
    done_urls = {r["photo_url"] for r in already}

    fetched = failed = 0
    for url in urls:
        if url in done_urls:
            continue
        try:
            if delay and not cache_path(url).exists():
                await _asyncio.sleep(_random.uniform(delay * 0.5, delay * 1.5))
            data = await download_photo(url, http_client=http_client)
            sha256 = hashlib.sha256(data).hexdigest()
            phash = compute_image_hash(data)
            if is_blocked_photo_fingerprint(
                {"photo_url": url, "sha256": sha256, "phash": phash}, blocked_urls=blocked_urls):
                logger.info("fingerprint_listing_photos: skipped blocked photo %s", url)
                continue
            await execute(
                """
                INSERT INTO listing_photo_fingerprints
                    (listing_id, photo_url, sha256, phash, fetch_status, computed_at)
                VALUES ($1, $2, $3, $4, 'ok', now())
                ON CONFLICT (listing_id, photo_url) DO UPDATE SET
                    sha256 = EXCLUDED.sha256, phash = EXCLUDED.phash,
                    fetch_status = 'ok', fetch_error = NULL, computed_at = now()
                """,
                listing_id, url, sha256, phash,
            )
            fetched += 1
        except Exception as exc:  # noqa: BLE001 — сеть/декод, любая ошибка -> download_failed, не падаем на батче
            logger.warning("fingerprint_listing_photos: %s (%s) failed: %s", listing_id, url, exc)
            await execute(
                """
                INSERT INTO listing_photo_fingerprints (listing_id, photo_url, fetch_status, fetch_error)
                VALUES ($1, $2, 'download_failed', $3)
                ON CONFLICT (listing_id, photo_url) DO UPDATE SET
                    fetch_status = 'download_failed', fetch_error = $3
                """,
                listing_id, url, str(exc)[:500],
            )
            failed += 1
    return {"listing_id": listing_id, "photo_count": len(urls), "fetched": fetched, "failed": failed}


async def _fingerprints_for(listing_id: str) -> list[dict]:
    from bot.db.pg import fetch
    rows = await fetch(
        "SELECT photo_url, sha256, phash, embedding, photo_type, fetch_status "
        "FROM listing_photo_fingerprints WHERE listing_id = $1",
        listing_id,
    )
    blocked_urls = await _blocked_photo_urls()
    return [
        fp for fp in (dict(r) for r in rows)
        if not is_blocked_photo_fingerprint(fp, blocked_urls=blocked_urls)
    ]


def _photo_tier(a: dict, b: dict) -> str | None:
    """exact_hash > sha256 совпадает; perceptual > phash hamming <
    PHASH_HAMMING_THRESHOLD; ai > cosine >= AI_SIMILARITY_THRESHOLD; None —
    не совпадают ни на одном уровне. Уровни ИСКЛЮЧАЮЩИЕ (задача просит три
    ОТДЕЛЬНЫХ счётчика — exact_shared/perceptual_shared/ai_similar_count —
    не перекрывающихся, иначе одна и та же пара тройной раз считалась бы
    "похожей")."""
    if a.get("sha256") and a.get("sha256") == b.get("sha256"):
        return "exact"
    dist = _phash_hamming_distance(a.get("phash"), b.get("phash"))
    if dist is not None and dist < PHASH_HAMMING_THRESHOLD:
        return "perceptual"
    sim = cosine_similarity(a.get("embedding"), b.get("embedding"))
    if sim is not None and sim >= AI_SIMILARITY_THRESHOLD:
        return "ai"
    return None


def _pair_similarity(a: dict, b: dict) -> float | None:
    return cosine_similarity(a.get("embedding"), b.get("embedding"))


def compare_fingerprints(fps_a: list[dict], fps_b: list[dict]) -> dict:
    """ЧИСТАЯ функция (без БД/сети) — берёт списки fingerprint-словарей
    ОБЕИХ сторон candidate-пары, возвращает агрегированную evidence. Каждое
    фото стороны A сопоставляется максимум ОДНОМУ фото стороны B (жадно,
    по убыванию силы уровня exact->perceptual->ai) — не считаем одно и то
    же фото совпавшим с несколькими сразу, иначе счётчики раздуваются на
    объявлениях с почти-дублирующимися фото в галерее."""
    used_b: set[int] = set()
    matches: list[dict] = []
    counts = {"exact": 0, "perceptual": 0, "ai": 0}
    max_similarity = 0.0

    for a in fps_a:
        best_j, best_tier, best_sim = None, None, None
        for j, b in enumerate(fps_b):
            if j in used_b:
                continue
            tier = _photo_tier(a, b)
            if tier is None:
                continue
            # приоритет уровня: exact > perceptual > ai; при равном уровне —
            # первый найденный (порядок фото в галерее детерминирован).
            tier_rank = {"exact": 3, "perceptual": 2, "ai": 1}[tier]
            if best_tier is None or tier_rank > {"exact": 3, "perceptual": 2, "ai": 1}[best_tier]:
                best_j, best_tier, best_sim = j, tier, _pair_similarity(a, b)
        if best_j is not None:
            used_b.add(best_j)
            counts[best_tier] += 1
            b = fps_b[best_j]
            if best_sim is not None:
                max_similarity = max(max_similarity, best_sim)
            matches.append({
                "a_url": a["photo_url"], "b_url": b["photo_url"], "method": best_tier,
                "similarity": best_sim, "type_a": a.get("photo_type"), "type_b": b.get("photo_type"),
            })

    unit_specific = sum(
        1 for m in matches
        if m["type_a"] in _UNIT_SPECIFIC_TYPES and m["type_b"] in _UNIT_SPECIFIC_TYPES
    )
    # "common" — консервативный дефолт: и явно распознанные план/фасад/
    # рендер/другое, И ещё НЕ классифицированные (AI-стадия не отработала)
    # — задача: одинаковый общий рендер/план САМ ПО СЕБЕ не подтверждает
    # квартиру, неизвестный тип тем более не должен давать сильный сигнал.
    common = len(matches) - unit_specific

    all_types_known = all(
        (fp.get("photo_type") is not None) for fp in fps_a + fps_b if fp.get("fetch_status") == "ok"
    )
    any_fetch_failed = any(fp.get("fetch_status") != "ok" for fp in fps_a + fps_b)
    if any_fetch_failed:
        status = "partial"
    elif not all_types_known:
        status = "partial"  # sha256/phash полны, AI-классификация ещё не прошла
    else:
        status = "ok"

    return {
        "exact_shared_count": counts["exact"],
        "perceptual_shared_count": counts["perceptual"],
        "ai_similar_count": counts["ai"],
        "shared_unit_specific_count": unit_specific,
        "shared_common_count": common,
        "max_similarity": round(max_similarity, 4) if matches else None,
        "matched_photos": matches[:_MAX_MATCHED_PHOTOS_STORED],
        "processing_status": status,
    }


async def _resolve_pair_listings(candidate: dict) -> tuple[str, str] | None:
    """(listing_id_a, listing_id_b) стороны кандидата. B — listing,
    ЖИВОЙ СЕЙЧАС привязанный к candidate_property_id через property_
    listings. На проде на 2026-08-17 КАЖДАЯ property имеет РОВНО один
    property_listings (match_mode=candidate_only никогда не делает
    auto-merge — проверено прямым запросом, см. отчёт задачи), но код
    защищается от будущего N>1 (бы) выбором самого свежего last_seen,
    а не падает."""
    from bot.db.pg import fetch
    rows = await fetch(
        """
        SELECT al.id FROM property_listings pl
        JOIN apartment_listings al ON al.id = pl.listing_id
        WHERE pl.property_id = $1
        ORDER BY al.last_seen DESC NULLS LAST, al.id
        """,
        candidate["candidate_property_id"],
    )
    if not rows:
        return None
    return candidate["listing_id"], rows[0]["id"]


async def save_candidate_evidence(candidate_id: int, photo_count_a: int, photo_count_b: int,
                                   evidence: dict, *, error: str | None = None) -> None:
    """Идемпотентный UPSERT — повторный вызов на тот же candidate_id
    ПЕРЕЗАПИСЫВАЕТ строку (не копит историю), задача: "пересчёт должен
    быть идемпотентным и версионированным" (версия — model_version)."""
    from bot.db.pg import execute
    status = "error" if error else evidence.get("processing_status", "ok")
    await execute(
        """
        INSERT INTO property_candidate_photo_evidence
            (candidate_id, photo_count_a, photo_count_b, exact_shared_count, perceptual_shared_count,
             ai_similar_count, shared_unit_specific_count, shared_common_count, max_similarity,
             matched_photos, model_version, processing_status, processing_error, computed_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, $13, now())
        ON CONFLICT (candidate_id) DO UPDATE SET
            photo_count_a = EXCLUDED.photo_count_a, photo_count_b = EXCLUDED.photo_count_b,
            exact_shared_count = EXCLUDED.exact_shared_count,
            perceptual_shared_count = EXCLUDED.perceptual_shared_count,
            ai_similar_count = EXCLUDED.ai_similar_count,
            shared_unit_specific_count = EXCLUDED.shared_unit_specific_count,
            shared_common_count = EXCLUDED.shared_common_count,
            max_similarity = EXCLUDED.max_similarity, matched_photos = EXCLUDED.matched_photos,
            model_version = EXCLUDED.model_version, processing_status = EXCLUDED.processing_status,
            processing_error = EXCLUDED.processing_error, computed_at = now()
        """,
        candidate_id, photo_count_a, photo_count_b,
        evidence.get("exact_shared_count", 0), evidence.get("perceptual_shared_count", 0),
        evidence.get("ai_similar_count", 0), evidence.get("shared_unit_specific_count", 0),
        evidence.get("shared_common_count", 0), evidence.get("max_similarity"),
        json.dumps(evidence.get("matched_photos", []), ensure_ascii=False, default=str),
        f"{_EVIDENCE_VERSION}+{_EMBEDDING_MODEL_VERSION}", status, error,
    )


async def aggregate_candidate_evidence(candidate_id: int, *, http_client=None, delay: float = 0.0,
                                        dry_run: bool = False,
                                        reuse_existing_fingerprints: bool = False) -> dict:
    """Точка входа для scripts/photo_evidence_scan.py: fingerprint ОБЕИХ
    сторон candidate-пары (если ещё не закэшировано, см. idempotency в
    fingerprint_listing_photos) + сравнение + сохранение evidence. НЕ
    вызывает AI-стадию (embedding'ов может ещё не быть — evidence тогда
    'partial', см. compare_fingerprints, и это ОЖИДАЕМО до scripts/
    photo_evidence_ai_scan.py).

    reuse_existing_fingerprints — НЕ скачивает и НЕ fingerprint'ит фото: пересчитывает
    evidence только из уже сохранённых строк. Нужен после AI-стадии и для
    безопасного resume, когда повторная CDN-закачка не нужна.

    dry_run — считает evidence (если reuse_existing_fingerprints не задан, реальные
    сетевые закачки всё равно происходят    происходят, иначе не из чего считать — dry-run здесь про запись в БД,
    не про сеть, тот же смысл, что --dry-run у scripts/backfill_listing_
    floors.py: "не пиши решение", не "не делай работу"), но НЕ вызывает
    save_candidate_evidence — для canary/оценки стоимости без изменения
    property_candidate_photo_evidence."""
    from bot.db.pg import fetchrow

    candidate = await fetchrow(
        "SELECT candidate_id, listing_id, candidate_property_id FROM property_match_candidates "
        "WHERE candidate_id = $1", candidate_id,
    )
    if candidate is None:
        raise ValueError(f"candidate_id {candidate_id} не найден")
    candidate = dict(candidate)

    pair = await _resolve_pair_listings(candidate)
    if pair is None:
        evidence = {"processing_status": "error"}
        if not dry_run:
            await save_candidate_evidence(candidate_id, 0, 0, evidence,
                                           error="candidate_property_id не имеет ни одного property_listings")
        return evidence

    lid_a, lid_b = pair
    if not reuse_existing_fingerprints:
        await fingerprint_listing_photos(lid_a, http_client=http_client, delay=delay)
        await fingerprint_listing_photos(lid_b, http_client=http_client, delay=delay)
    fps_a = await _fingerprints_for(lid_a)
    fps_b = await _fingerprints_for(lid_b)
    ok_a = [f for f in fps_a if f["fetch_status"] == "ok"]
    ok_b = [f for f in fps_b if f["fetch_status"] == "ok"]

    evidence = compare_fingerprints(ok_a, ok_b)
    if not dry_run:
        await save_candidate_evidence(candidate_id, len(fps_a), len(fps_b), evidence)
    return evidence
