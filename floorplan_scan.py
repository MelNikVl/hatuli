#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Детекция планировок: гибрид — эвристика OpenCV (быстрый фильтр) + SigLIP на кандидатах.
Запуск (из ~/floorplan-clip/venv — там torch, opencv):
  /home/nik/floorplan-clip/venv/bin/python /home/nik/krisha_bot/floorplan_scan.py [--limit N] [--delay 1.0]

Решение «план» = эвристика прошла И SigLIP согласен (fp > other, fp > порог).
"""
import argparse
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

BASE = Path("/home/nik/krisha_bot")
PHOTO_CACHE = BASE / "static" / "cache" / "photos"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

# пороги эвристики (подобраны по 8 реальным планам: sat 1-33, white 0.19-0.95, gray 0.51-0.99, ink 0-0.21)
H_SAT_MAX = 30.0      # средняя насыщенность (план — почти ч/б; интерьеры ~42+)
H_WHITE_MIN = 0.18    # доля почти белых пикселей (у фото ~0.05)
H_GRAY_MIN = 0.50     # доля «серых» пикселей (max-min < 15)
H_INK_MIN = 0.002     # доля тёмных пикселей (должны быть линии чертежа)
H_INK_MAX = 0.50
# SigLIP: строже
FP_MARGIN = 0.05      # план должен быть заметно выше интерьера
FP_MIN = 0.25


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def db():
    import psycopg2
    return psycopg2.connect(load_database_url())


def is_enabled() -> bool:
    """Флаг AI_FLOORPLAN_SCAN в app_settings (по умолчанию включён — True).
    Скрипт — отдельный процесс (свой venv, torch/opencv), общего async
    app_settings-кеша (bot/db/settings.py) у него нет, поэтому читаем
    напрямую через psycopg2, как и остальную БД в этом файле."""
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT value FROM app_settings WHERE key = 'AI_FLOORPLAN_SCAN'")
        row = cur.fetchone()
        conn.close()
        if row is None:
            return True
        return str(row[0]).strip() in ("1", "true", "True", "on")
    except Exception as e:
        print(f"[WARN] не удалось прочитать AI_FLOORPLAN_SCAN, считаю включённым: {e}", flush=True)
        return True


def cache_path(url: str) -> Path:
    return PHOTO_CACHE / (hashlib.sha256(url.encode()).hexdigest() + ".jpg")


_rate_lock = threading.Lock()
_last_dl = [0.0]


def _throttle(delay: float) -> None:
    with _rate_lock:
        elapsed = time.time() - _last_dl[0]
        if elapsed < delay:
            time.sleep(delay - elapsed)
        _last_dl[0] = time.time()


def download(url: str, delay: float = 1.0) -> Path:
    _throttle(delay)
    p = cache_path(url)
    if p.exists() and p.stat().st_size > 0:
        return p
    data = urlopen(Request(url, headers={"User-Agent": UA}), timeout=20).read()
    if not data:
        raise ValueError("empty")
    p.write_bytes(data)
    return p


# --- Эвристика OpenCV ---
def heuristic_features(path: Path):
    """(sat_mean, white_ratio, gray_ratio, ink_ratio) или None."""
    import cv2
    import numpy as np
    img = cv2.imread(str(path))
    if img is None:
        return None
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    sat = float(hsv[:, :, 1].mean())
    white = float(((hsv[:, :, 1] < 20) & (hsv[:, :, 2] > 200)).mean())
    ink = float((hsv[:, :, 2] < 80).mean())
    b, g, r = img[:, :, 0].astype(int), img[:, :, 1].astype(int), img[:, :, 2].astype(int)
    gray = float((np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b) < 15).mean())
    return sat, white, gray, ink


def is_candidate(f):
    if f is None:
        return False
    sat, white, gray, ink = f
    return (sat < H_SAT_MAX and white > H_WHITE_MIN and gray > H_GRAY_MIN
            and H_INK_MIN < ink < H_INK_MAX)


# --- SigLIP (только для кандидатов) ---
_model = None
_preprocess = None
_text_features = None
_device = "cpu"
_TEXTS = [
    "apartment floor plan diagram",
    "architectural floor plan blueprint",
    "photograph of apartment interior",
    "photograph of furniture and rooms",
]


def load_model():
    global _model, _preprocess, _text_features, _device
    if _model is not None:
        return
    import torch
    import open_clip
    print("Загружаю SigLIP...", flush=True)
    _model, _, _preprocess = open_clip.create_model_and_transforms("ViT-B-16-SigLIP", pretrained="webli")
    tokenizer = open_clip.get_tokenizer("ViT-B-16-SigLIP")
    _device = "cuda" if torch.cuda.is_available() else "cpu"
    _model = _model.to(_device)
    _model.eval()
    with torch.no_grad():
        tt = tokenizer(_TEXTS).to(_device)
        tf = _model.encode_text(tt)
        _text_features = tf / tf.norm(dim=-1, keepdim=True)
    print(f"SigLIP на: {_device}", flush=True)


def siglip_probs(path: Path):
    """(floorplan_prob, interior_prob) — softmax по 2+2 промптам."""
    import torch
    from PIL import Image
    load_model()
    image = _preprocess(Image.open(path).convert("RGB")).unsqueeze(0).to(_device)
    with torch.no_grad():
        im = _model.encode_image(image)
        im = im / im.norm(dim=-1, keepdim=True)
        sim = (100.0 * im @ _text_features.T).softmax(dim=-1)
        p = sim[0].cpu().numpy()
    return float(p[0] + p[1]), float(p[2] + p[3])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--max-photos", type=int, default=12)
    ap.add_argument("--min-score", type=float, default=0.22)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()

    if not is_enabled():
        print("AI_FLOORPLAN_SCAN выключен в /admin/analytics/ai-status — выхожу без обработки.", flush=True)
        sys.exit(0)

    PHOTO_CACHE.mkdir(parents=True, exist_ok=True)
    conn = db()
    cur = conn.cursor()
    dl = ThreadPoolExecutor(max_workers=a.workers)
    dl_lock = threading.Lock()
    dl_count = 0

    cur.execute("""
        SELECT id, photos::text FROM apartment_listings
        WHERE floorplan_checked_at IS NULL
          AND photos IS NOT NULL AND photos::text != '[]'
          AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
        ORDER BY id LIMIT %s""", (a.limit,))
    rows = cur.fetchall()
    print(f"Объявлений к обработке: {len(rows)}", flush=True)

    done = found = candidates = 0
    for lid, photos_json in rows:
        try:
            urls = [u for u in json.loads(photos_json) if isinstance(u, str) and u.startswith("http")]
        except Exception:
            urls = []
        urls = urls[: a.max_photos]
        floorplan_url = None

        for url in urls:
            try:
                p = dl.submit(download, url, a.delay).result(timeout=60)
                with dl_lock:
                    dl_count += 1
                f = heuristic_features(p)
                h_pass = is_candidate(f)
                if not h_pass:
                    cur.execute(
                        "INSERT INTO listing_floorplans (listing_id, photo_url, floorplan_score, other_score, is_floorplan, h_sat, h_white, h_gray, h_ortho, h_ink) "
                        "VALUES (%s, %s, 0, 0, FALSE, %s, %s, %s, NULL, %s)",
                        (lid, url, *(f or (0, 0, 0, 0))))
                    continue
                candidates += 1
                fp_s, ot_s = siglip_probs(p)
                is_fp = (fp_s - ot_s) > FP_MARGIN and fp_s > FP_MIN
                cur.execute(
                    "INSERT INTO listing_floorplans (listing_id, photo_url, floorplan_score, other_score, is_floorplan, h_sat, h_white, h_gray, h_ortho, h_ink) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s)",
                    (lid, url, fp_s, ot_s, bool(is_fp), *(f or (0, 0, 0, 0))))
                if is_fp and floorplan_url is None:
                    floorplan_url = url
            except Exception as e:
                print(f"    [ERR] {lid}: {type(e).__name__}: {e}", flush=True)
                continue

        cur.execute(
            "UPDATE apartment_listings SET floorplan_url = %s, floorplan_checked_at = now() WHERE id = %s",
            (floorplan_url, lid))
        done += 1
        if floorplan_url:
            found += 1
        if done % 10 == 0:
            conn.commit()
            print(f"  {done}/{len(rows)} · планов: {found} · кандидатов SigLIP: {candidates} · фото: {dl_count}", flush=True)

    conn.commit()
    conn.close()
    print(f"Готово: {done} объявлений, планов {found}, кандидатов на SigLIP {candidates}", flush=True)


if __name__ == "__main__":
    main()
