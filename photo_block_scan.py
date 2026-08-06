# -*- coding: utf-8 -*-
"""
photo_block_scan.py — поиск «мусорных» фото в объявлениях по эталонам.

Идея: у пользователя есть 2 эталонных фото, которые часто встречаются во
всех объявлениях (реклама/заглушки). Находим все URL из apartment_listings.photos,
которые ВИЗУАЛЬНО совпадают с эталонами (SigLIP-эмбеддинги, cosine),
пишем их в таблицу blocked_photo_urls и вычищаем из photos у всех объявлений.

CDN-URL стабильны (один контент = один URL), поэтому достаточно проверить
фото из локального кэша static/cache/photos/ (sha256(url).jpg) — найденные
URL удаляются у ВСЕХ объявлений, даже у тех, чьи фото не в кэше.

Запуск:
    ~/floorplan-clip/venv/bin/python photo_block_scan.py --limit 5000   # тест
    ~/floorplan-clip/venv/bin/python photo_block_scan.py                # полный
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "postgresql://krisha:***@localhost/krisha_bot")

# dotenv в floorplan-clip venv нет — читаем DATABASE_URL из .env вручную
_env_path = Path("/home/nik/krisha_bot/.env")
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line.startswith("DATABASE_URL="):
            os.environ["DATABASE_URL"] = _line.split("=", 1)[1].strip().strip('"').strip("'")

CACHE_DIR = Path("/home/nik/krisha_bot/static/cache/photos")
REFS = ["/tmp/ref1.jpg", "/tmp/ref2.jpg"]
SIM_THRESHOLD = 0.82   # косинусное сходство SigLIP: идентичные/почти ~0.95+


def load_model():
    import open_clip
    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-B-16-SigLIP", pretrained="webli")
    model.eval()
    tokenizer = open_clip.get_tokenizer("ViT-B-16-SigLIP")
    return model, preprocess


def ref_embeddings(model, preprocess, device):
    from PIL import Image
    embs = []
    for p in REFS:
        img = preprocess(Image.open(p).convert("RGB")).unsqueeze(0).to(device)
        with torch.no_grad():
            e = model.encode_image(img)
        embs.append(e)
    return torch.cat(embs)  # (2, dim)


def load_db_urls(limit: int | None):
    """Все уникальные URL фото из apartment_listings.photos."""
    import psycopg2
    url = os.getenv("DATABASE_URL", "postgresql://krisha:***@localhost/krisha_bot")
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    q = "SELECT photos FROM apartment_listings WHERE photos IS NOT NULL AND photos <> '[]'"
    if limit:
        q += f" LIMIT {limit}"
    cur.execute(q)
    urls: set[str] = set()
    for (photos,) in cur:
        if not photos:
            continue
        if isinstance(photos, str):
            try:
                photos = json.loads(photos)
            except ValueError:
                continue
        for u in photos:
            if isinstance(u, str) and u:
                urls.add(u)
    cur.close()
    conn.close()
    return urls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="ограничить число объявлений (тест)")
    ap.add_argument("--threshold", type=float, default=SIM_THRESHOLD)
    args = ap.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device={device}", flush=True)
    model, preprocess = load_model()
    model = model.to(device)
    refs = ref_embeddings(model, preprocess, device)
    refs = refs / refs.norm(dim=-1, keepdim=True)
    print(f"refs: {refs.shape}", flush=True)

    urls = load_db_urls(args.limit)
    print(f"уникальных URL: {len(urls)}", flush=True)

    # Кэш: url -> файл (по sha256)
    hits, missing = 0, 0
    batch_urls: list[str] = []
    batch_files: list[Path] = []
    blocked: dict[str, float] = {}
    t0 = time.time()

    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 50_000_000

    def flush_batch():
        nonlocal batch_urls, batch_files
        if not batch_files:
            return
        imgs = []
        ok_urls, ok_files = [], []
        for u, f in zip(batch_urls, batch_files):
            try:
                im = Image.open(f).convert("RGB")
                imgs.append(preprocess(im).unsqueeze(0))
                ok_urls.append(u)
                ok_files.append(f)
            except Exception:
                continue
        if imgs:
            x = torch.cat(imgs).to(device)
            with torch.no_grad():
                e = model.encode_image(x)
            e = e / e.norm(dim=-1, keepdim=True)
            sims = e @ refs.T  # (B, 2)
            best = sims.max(dim=1).values
            for u, b in zip(ok_urls, best.tolist()):
                if b >= args.threshold:
                    blocked.setdefault(u, max(blocked.get(u, 0.0), b))
        batch_urls, batch_files = [], []

    for i, u in enumerate(sorted(urls)):
        h = hashlib.sha256(u.encode()).hexdigest()
        f = CACHE_DIR / f"{h}.jpg"
        if not f.exists():
            missing += 1
            continue
        hits += 1
        batch_urls.append(u)
        batch_files.append(f)
        if len(batch_files) >= 64:
            flush_batch()
        if i % 5000 == 0:
            print(f"  {i}/{len(urls)} hits={hits} miss={missing} "
                  f"blocked={len(blocked)} elapsed={time.time()-t0:.0f}s", flush=True)
    flush_batch()

    print(f"\nГотово: проверено {hits} (в кэше), пропущено {missing} (нет в кэше), "
          f"совпало с эталонами: {len(blocked)}", flush=True)

    # Запись: blocked_photo_urls + вычистка из photos
    import psycopg2
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS blocked_photo_urls (
        url TEXT PRIMARY KEY, score REAL, reason TEXT, created_at TIMESTAMPTZ DEFAULT now())""")
    for u, s in blocked.items():
        cur.execute("""INSERT INTO blocked_photo_urls (url, score, reason)
                       VALUES (%s, %s, 'siglip_etalon') ON CONFLICT (url) DO UPDATE SET score=EXCLUDED.score""",
                    (u, s))
    conn.commit()

    if blocked:
        urls_list = list(blocked.keys())
        # Вычистить эти URL из photos у ВСЕХ объявлений (jsonb массив → фильтр)
        cur.execute("SELECT id, photos FROM apartment_listings WHERE photos IS NOT NULL AND photos <> '[]'")
        upd = 0
        rows = cur.fetchall()
        block_set = set(urls_list)
        for lid, photos in rows:
            if isinstance(photos, str):
                try:
                    photos = json.loads(photos)
                except ValueError:
                    continue
            newp = [u for u in photos if u not in block_set]
            if len(newp) != len(photos):
                cur.execute("UPDATE apartment_listings SET photos = %s::jsonb WHERE id = %s",
                            (json.dumps(newp, ensure_ascii=False), lid))
                upd += 1
        conn.commit()
        print(f"обновлено объявлений: {upd}", flush=True)

    cur.close()
    conn.close()

    with open("/tmp/blocked_urls.txt", "w", encoding="utf-8") as f:
        for u, s in sorted(blocked.items(), key=lambda x: -x[1]):
            f.write(f"{s:.3f}\t{u}\n")
    print("список: /tmp/blocked_urls.txt", flush=True)


if __name__ == "__main__":
    main()
