#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/photo_evidence_ai_scan.py — задача 2026-08-17, "Property Identity
— photo evidence", часть B, ФАЗА 2 (AI): embedding + 6-way классификация
типа фото для строк listing_photo_fingerprints, которые ФАЗА 1
(scripts/photo_evidence_scan.py, main venv) уже скачала и захэшировала
(sha256/phash), но НЕ смогла классифицировать (нет torch в main venv).

Запуск — ТОЛЬКО из /home/nik/floorplan-clip/venv (там torch/open_clip),
тот же venv, что floorplan_scan.py уже использует для детекции планировок:
    /home/nik/floorplan-clip/venv/bin/python scripts/photo_evidence_ai_scan.py [--limit N]

## Почему self-contained (не импортирует bot.* пакет)

floorplan-clip venv НАМЕРЕННО изолирован от основного (там torch/
open_clip, НЕТ asyncpg/fastapi/bs4 — проверено перед написанием: `import
asyncpg`/`import fastapi` там падают ModuleNotFoundError). bot/identity/
photo_evidence.py импортирует bot.db.pg (asyncpg) — даже ленивый импорт
внутри функций всё равно упал бы при ПЕРВОМ вызове любой БД-функции этого
модуля. floorplan_scan.py УЖЕ решает эту задачу тем же способом — psycopg2
(sync) напрямую, без импорта bot.* пакета вообще. Этот скрипт — тот же
архитектурный выбор, не "новый параллельный парсер" (задача про image-
пайплайн просит не писать НОВЫЙ классификатор план/не план — здесь
переиспользован ИМЕННО backbone/preprocessing/инфраструктура загрузки
floorplan_scan.py, см. bot/identity/photo_evidence.py докстринг, часть A).

cache_path() — та же формула (sha256(url)+".jpg" в static/cache/photos/),
что floorplan_scan.py И bot/identity/photo_evidence.py — ОДНА строка,
продублирована трижды по необходимости изоляции процессов/venv'ов, НЕ
дрейфует произвольно (комментарий в каждой копии указывает на две другие).

## 6-way классификация (задача B, явно — расширение 4-промптного план/
не план zero-shot floorplan_scan.py, ТА ЖЕ модель/preprocessing/подход,
другой набор промптов и другое число категорий)

interior/view/floorplan/building_common/render/other — ЗНАЧЕНИЯ должны
буква-в-букву совпадать с CHECK-constraint migrations/088
(lpf_photo_type_check).

## Embedding

L2-нормализованный float32 вектор (encode_image, тот же вызов, что
floorplan_scan.py::siglip_probs использует ВНУТРИ, просто здесь берём сам
вектор, не софтмакс к тексту) — packed в BYTEA той же формулой, что
bot/identity/photo_evidence.py::pack_embedding (float32 tobytes) — cosine
между двумя такими эмбеддингами = dot product (см. cosine_similarity
там же, main venv её и вызывает при пересчёте evidence).

## После этого скрипта

property_candidate_photo_evidence НЕ обновляется отсюда (этот процесс не
может писать через bot.db.pg/asyncpg-путь, и агрегация — логика main
venv). Повторный запуск scripts/photo_evidence_scan.py --only-missing
(main venv) подхватит новые embedding/photo_type — предыдущие evidence-
строки со processing_status='partial' пересчитаются в 'ok' (или
останутся 'partial', если фото всё ещё не докачаны/не classified — сам
recompute идемпотентен, не требует отдельного флага).
"""
import argparse
import hashlib
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parents[1]
PHOTO_CACHE = BASE / "static" / "cache" / "photos"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

_EMBEDDING_MODEL_VERSION = "siglip_vit_b16_webli_v1"  # ДОЛЖНО совпадать с bot/identity/photo_evidence.py

# 6 категорий — задача B: "interior/unit-specific; view-from-window;
# floorplan; building/common-area; developer render; logo/banner/other".
# logo/banner объединены в "other" (структурно тот же "не про квартиру"
# бакет) — те же ЗНАЧЕНИЯ, что CHECK constraint migrations/088.
_PHOTO_TYPE_PROMPTS = {
    "interior": "a photograph of a furnished apartment interior room, kitchen or bedroom",
    "view": "a photograph of a view from an apartment window or balcony, cityscape or landscape",
    "floorplan": "an architectural floor plan blueprint diagram with room layout",
    "building_common": "a photograph of a building facade, entrance, staircase, hallway or courtyard",
    "render": "a 3d computer generated architectural render or visualization of a building",
    "other": "a logo, banner, advertisement graphic or watermark image",
}
_PHOTO_TYPES = list(_PHOTO_TYPE_PROMPTS.keys())


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def db():
    import psycopg2
    return psycopg2.connect(load_database_url())


def cache_path(url: str) -> Path:
    """Буквально та же формула, что floorplan_scan.py::cache_path и
    bot/identity/photo_evidence.py::cache_path — см. докстринг модуля."""
    return PHOTO_CACHE / (hashlib.sha256(url.encode()).hexdigest() + ".jpg")


def ensure_downloaded(url: str, timeout: float = 20.0) -> Path:
    """Fallback: файл ДОЛЖЕН уже быть в кэше (ФАЗА 1 его туда положила,
    fetch_status='ok' в listing_photo_fingerprints это гарантирует) — но
    кэш на диске мог быть вычищен независимо от БД-состояния, поэтому
    защищаемся, а не падаем."""
    p = cache_path(url)
    if p.exists() and p.stat().st_size > 0:
        return p
    data = urlopen(Request(url, headers={"User-Agent": UA}), timeout=timeout).read()
    if not data:
        raise ValueError("empty response body")
    PHOTO_CACHE.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# --- SigLIP (тот же backbone, что floorplan_scan.py) ---
_model = None
_preprocess = None
_text_features = None
_device = "cpu"


def load_model() -> None:
    global _model, _preprocess, _text_features, _device
    if _model is not None:
        return
    import open_clip
    import torch

    print("Загружаю SigLIP (siglip_vit_b16_webli)...", flush=True)
    _model, _, _preprocess = open_clip.create_model_and_transforms("ViT-B-16-SigLIP", pretrained="webli")
    tokenizer = open_clip.get_tokenizer("ViT-B-16-SigLIP")
    _device = "cuda" if torch.cuda.is_available() else "cpu"
    _model = _model.to(_device)
    _model.eval()
    with torch.no_grad():
        prompts = [_PHOTO_TYPE_PROMPTS[t] for t in _PHOTO_TYPES]
        tt = tokenizer(prompts).to(_device)
        tf = _model.encode_text(tt)
        _text_features = tf / tf.norm(dim=-1, keepdim=True)
    print(f"SigLIP на: {_device}", flush=True)


def classify_and_embed(path: Path) -> tuple:
    """(embedding: np.ndarray[float32] нормализован, photo_type: str,
    type_scores: dict[str, float]) — ЧИСТАЯ (кроме глобального _model)
    функция от файла на диске, отдельно тестируемая мокой _model."""
    import numpy as np
    import torch
    from PIL import Image

    load_model()
    image = _preprocess(Image.open(path).convert("RGB")).unsqueeze(0).to(_device)
    with torch.no_grad():
        im = _model.encode_image(image)
        im = im / im.norm(dim=-1, keepdim=True)
        sim = (100.0 * im @ _text_features.T).softmax(dim=-1)
        scores = sim[0].cpu().numpy()
        embedding = im[0].cpu().numpy().astype(np.float32)
    type_scores = {t: float(scores[i]) for i, t in enumerate(_PHOTO_TYPES)}
    photo_type = max(type_scores, key=type_scores.get)
    return embedding, photo_type, type_scores


def pack_embedding(vec) -> bytes:
    """Та же формула, что bot/identity/photo_evidence.py::pack_embedding
    (float32 tobytes, единичная норма — здесь уже нормализован encode_
    image'ом выше, дополнительная нормализация не нужна, но не вредит)."""
    import numpy as np
    v = np.asarray(vec, dtype=np.float32)
    norm = np.linalg.norm(v)
    if norm > 0:
        v = v / norm
    return v.tobytes()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--batch-size", type=int, default=20, help="commit каждые N фото")
    ap.add_argument("--dry-run", action="store_true", help="не писать embedding/photo_type в БД")
    args = ap.parse_args()

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT lpf.id, lpf.listing_id, lpf.photo_url FROM listing_photo_fingerprints lpf
        LEFT JOIN blocked_photo_urls bpu ON bpu.url = lpf.photo_url
        WHERE lpf.fetch_status = 'ok' AND lpf.embedding IS NULL
          AND bpu.url IS NULL
          AND NOT (lpf.sha256 = ANY(%s))
          AND NOT (lpf.phash = ANY(%s))
        ORDER BY lpf.id LIMIT %s
    """, (list(BLOCKED_PHOTO_SHA256), list(BLOCKED_PHOTO_PHASH), args.limit))
    rows = cur.fetchall()
    print(f"Фото к AI-классификации: {len(rows)}", flush=True)

    t0 = time.monotonic()
    done = errors = 0
    type_counts: dict = {t: 0 for t in _PHOTO_TYPES}
    for i, (row_id, listing_id, url) in enumerate(rows):
        try:
            path = ensure_downloaded(url)
            embedding, photo_type, _scores = classify_and_embed(path)
            type_counts[photo_type] += 1
            if not args.dry_run:
                cur.execute(
                    "UPDATE listing_photo_fingerprints SET embedding=%s, embedding_model=%s, "
                    "photo_type=%s, ai_computed_at=now() WHERE id=%s",
                    (psycopg2_binary(pack_embedding(embedding)), _EMBEDDING_MODEL_VERSION, photo_type, row_id),
                )
            done += 1
        except Exception as e:
            print(f"    [ERR] fingerprint_id={row_id} listing={listing_id}: {type(e).__name__}: {e}", flush=True)
            if not args.dry_run:
                cur.execute(
                    "UPDATE listing_photo_fingerprints SET fetch_error=%s WHERE id=%s",
                    (f"ai_stage: {type(e).__name__}: {e}"[:500], row_id),
                )
            errors += 1

        if (i + 1) % max(args.batch_size, 1) == 0:
            conn.commit()
            print(f"  {i + 1}/{len(rows)} · ok={done} errors={errors} · {type_counts}", flush=True)

    conn.commit()
    conn.close()
    elapsed = round(time.monotonic() - t0, 1)
    print(f"Готово ({elapsed}с): {done} обработано, {errors} ошибок, распределение типов: {type_counts}")


def psycopg2_binary(data: bytes):
    import psycopg2
    return psycopg2.Binary(data)


if __name__ == "__main__":
    main()
