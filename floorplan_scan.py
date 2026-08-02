#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Детекция планировок на фото объявлений (SigLIP, локально, без API).
Запуск (из ~/floorplan-clip/venv — там torch):
  /home/nik/floorplan-clip/venv/bin/python /home/nik/krisha_bot/floorplan_scan.py [--limit N] [--max-photos M] [--min-score 0.22]

Что делает:
  1. Берёт объявления с фото, у которых floorplan_checked_at IS NULL (пачками по 100).
  2. Качает фото с CDN (кэш в static/cache/photos/<sha256(url)>.jpg).
  3. Классифицирует каждое фото SigLIP (floorplan vs интерьер).
  4. Пишет: listing_floorplans (пофото), listings.floorplan_url (первое фото-план).
"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

BASE = Path("/home/nik/krisha_bot")
PHOTO_CACHE = BASE / "static" / "cache" / "photos"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def db():
    import psycopg2
    return psycopg2.connect(load_database_url())


def cache_path(url: str) -> Path:
    return PHOTO_CACHE / (hashlib.sha256(url.encode()).hexdigest() + ".jpg")


def download(url: str) -> Path:
    """Качает фото в кэш (thread-safe), возвращает путь."""
    p = cache_path(url)
    if p.exists() and p.stat().st_size > 0:
        return p
    req = Request(url, headers={"User-Agent": UA})
    data = urlopen(req, timeout=20).read()
    if not data:
        raise ValueError("empty")
    p.write_bytes(data)
    return p


# --- Модель SigLIP (один раз на процесс) ---
_model = None
_tokenizer = None
_preprocess = None
_text_features = None
_device = "cpu"
_TEXTS = [
    "a floor plan of an apartment",
    "architectural floor plan drawing",
    "blueprint of a flat",
    "top-down floor plan layout",
    "photo of a living room",
    "interior photo of a kitchen",
    "photo of a bedroom",
    "real photo of an apartment interior",
]


def load_model():
    global _model, _tokenizer, _preprocess, _text_features, _device
    if _model is not None:
        return
    import torch
    import open_clip
    print("Загружаю модель SigLIP ViT-B-16 (webli)...", flush=True)
    _model, _, _preprocess = open_clip.create_model_and_transforms(
        "ViT-B-16-SigLIP", pretrained="webli")
    _tokenizer = open_clip.get_tokenizer("ViT-B-16-SigLIP")
    _device = "cuda" if torch.cuda.is_available() else "cpu"
    _model = _model.to(_device)
    _model.eval()
    with torch.no_grad():
        text_tokens = _tokenizer(_TEXTS).to(_device)
        text_features = _model.encode_text(text_tokens)
        _text_features = text_features / text_features.norm(dim=-1, keepdim=True)
    print(f"Модель на: {_device}", flush=True)


def classify(path: Path, min_score: float):
    """Возвращает (is_floorplan, floorplan_score, other_score)."""
    import torch
    from PIL import Image
    load_model()
    image = _preprocess(Image.open(path).convert("RGB")).unsqueeze(0).to(_device)
    with torch.no_grad():
        image_features = _model.encode_image(image)
        image_features = image_features / image_features.norm(dim=-1, keepdim=True)
        sim = (100.0 * image_features @ _text_features.T).softmax(dim=-1)
        probs = sim[0].cpu().numpy()
    fp = max(probs[0], probs[1], probs[2], probs[3])
    other = max(probs[4], probs[5], probs[6], probs[7])
    return (fp > other and fp > min_score, float(fp), float(other))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200, help="сколько объявлений за прогон")
    ap.add_argument("--max-photos", type=int, default=12)
    ap.add_argument("--min-score", type=float, default=0.22)
    ap.add_argument("--batch", type=int, default=100)
    a = ap.parse_args()

    PHOTO_CACHE.mkdir(parents=True, exist_ok=True)
    conn = db()
    cur = conn.cursor()

    # пул загрузок
    dl = ThreadPoolExecutor(max_workers=6)
    dl_lock = threading.Lock()
    dl_count = 0

    cur.execute("""
        SELECT id, photos::text FROM apartment_listings
        WHERE floorplan_checked_at IS NULL AND photos IS NOT NULL AND photos::text != '[]'
        ORDER BY id LIMIT %s""", (a.limit,))
    rows = cur.fetchall()
    print(f"Объявлений к обработке: {len(rows)}", flush=True)

    done = 0
    found = 0
    for lid, photos_json in rows:
        try:
            urls = [u for u in json.loads(photos_json) if isinstance(u, str) and u.startswith("http")]
        except Exception:
            urls = []
        urls = urls[: a.max_photos]
        floorplan_url = None

        for url in urls:
            try:
                p = dl.submit(download, url).result(timeout=60)
                with dl_lock:
                    dl_count += 1
                is_fp, fp_s, ot_s = classify(p, a.min_score)
                cur.execute(
                    "INSERT INTO listing_floorplans (listing_id, photo_url, floorplan_score, other_score, is_floorplan) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (lid, url, float(fp_s), float(ot_s), bool(is_fp)))
                if is_fp and floorplan_url is None:
                    floorplan_url = url
            except Exception as e:
                print(f"    [ERR] listing {lid}: {type(e).__name__}: {e}", flush=True)
                continue

        cur.execute(
            "UPDATE apartment_listings SET floorplan_url = %s, floorplan_checked_at = now() WHERE id = %s",
            (floorplan_url, lid))
        done += 1
        if floorplan_url:
            found += 1

        if done % 25 == 0:
            conn.commit()
            print(f"  {done}/{len(rows)} · планов найдено: {found} · скачано фото: {dl_count}", flush=True)

    conn.commit()
    conn.close()
    print(f"Готово: обработано {done}, планов найдено: {found}", flush=True)


if __name__ == "__main__":
    main()
