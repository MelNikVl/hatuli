#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Для всех ЖК с homeportal-объектами: если у homeportal есть фото — поставить их в complexes.photos
(замена; homeportal приоритетнее Крыши). Если фото нет — не трогать."""
import subprocess, json, re

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# все homeportal-объекты с привязкой к ЖК: matched_complex_id + images (jsonb text)
rows = [l.split('\t') for l in psql(
    "SELECT matched_complex_id || chr(9) || COALESCE(images::text, '') FROM homeportal_objects "
    "WHERE matched_complex_id IS NOT NULL").splitlines() if l]

# группируем фото по ЖК (до 10)
from collections import OrderedDict
by_complex = OrderedDict()
for cid, images in rows:
    try:
        imgs = json.loads(images) if images else []
    except Exception:
        imgs = []
    urls = []
    for im in imgs:
        link = im.get("image_link") or im.get("preview_link")
        if link and link not in urls:
            urls.append(link)
    if urls:
        by_complex.setdefault(int(cid), []).extend(urls)

# дедуп и лимит 10
final = {}
for cid, urls in by_complex.items():
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    final[cid] = out[:10]

print(f"ЖК с homeportal-фото: {len(final)}")

n = 0
for cid, urls in final.items():
    arr = json.dumps(urls, ensure_ascii=False).replace("'", "''")
    psql(f"UPDATE complexes SET photos = '{arr}'::jsonb WHERE id = {cid} AND is_garbage IS NOT TRUE")
    n += 1

print(f"Обновлено ЖК: {n}")
print(psql("SELECT COUNT(*) || ' ЖК теперь с фото' FROM complexes WHERE photos IS NOT NULL AND photos != '[]'::jsonb"))
