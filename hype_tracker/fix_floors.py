#!/usr/bin/env python3
"""Исправление этажности ЖК (>30) по данным страниц Крыши.
Парсит «Этажность N[-M] этажей», пишет в complex_tech_specs.floors_total,
чистит мусорные значения в apartment_listings, помечает пропущенный мусор."""
import re
import subprocess
import sys
import time
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36"


def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot", "-t", "-A", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()


def fetch(url: str) -> str:
    return urlopen(Request(url, headers={"User-Agent": UA}), timeout=30).read().decode("utf-8", "ignore")


def extract_floors(html: str):
    # чистим теги — «Этажность 12-18 этажей» лежит в dt/dd
    t = re.sub(r"<[^>]+>", " ", html)
    t = re.sub(r"\s+", " ", t)
    m = re.search(r"Этажность\s+(\d+)\s*(?:-|—|–)\s*(\d+)?\s*этаж", t, re.I)
    if m:
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        return max(a, b), (min(a, b), max(a, b))
    m = re.search(r"Этажность\s+(\d+)\s*этаж", t, re.I)
    if m:
        v = int(m.group(1))
        return v, (v, v)
    # фолбэк: объявления на странице «N/M этаж» — макс знаменатель = этажность дома
    ms = re.findall(r"(\d+)/(\d+)\s*этаж", html)
    if ms:
        mx = max(int(b) for _, b in ms)
        return mx, (mx, mx)
    return None, None


def main() -> int:
    # 1) цель: известные ЖК с мусорной этажностью (по id, т.к. первая итерация уже сняла значения)
    rows = [r.split("|", 2) for r in psql("""
        SELECT c.id, c.name, c.krisha_url FROM complexes c
        WHERE c.id IN (222, 234, 389, 444, 1729, 1751, 2058, 1742, 2722)
        ORDER BY c.id""").splitlines() if r]
    print(f"ЖК для проверки: {len(rows)}")

    for cid, name, url in rows:
        try:
            html = fetch(url)
        except Exception as e:
            print(f"❌ {name}: {e}")
            continue
        floors_max, rng = extract_floors(html)
        esc = lambda s: s.replace(chr(39), chr(39) * 2)
        if floors_max:
            psql(f"""INSERT INTO complex_tech_specs (complex_id, floors_total, notes, updated_at)
                     VALUES ({cid}, {floors_max}, 'этажность с Крыши: {esc(str(rng[0]) + '-' + str(rng[1]))} этажей', now())
                     ON CONFLICT (complex_id) DO UPDATE SET
                     floors_total = COALESCE(complex_tech_specs.floors_total, EXCLUDED.floors_total),
                     notes = CASE WHEN complex_tech_specs.floors_total IS NULL THEN EXCLUDED.notes ELSE complex_tech_specs.notes END,
                     updated_at = now()""")
            # чистим мусорные значения в объявлениях (по LIKE — имена бывают с мусорными хвостами)
            psql(f"""UPDATE apartment_listings SET floors_total = {floors_max}
                     WHERE lower(complex_name) LIKE '%' || lower('{esc(name)}') || '%' AND floors_total > 30""")
            print(f"✅ {name}: {rng[0]}-{rng[1]} эт. (макс {floors_max}), объявления исправлены")
        else:
            psql(f"""UPDATE apartment_listings SET floors_total = NULL
                     WHERE lower(complex_name) LIKE '%' || lower('{esc(name)}') || '%' AND floors_total > 30""")
            print(f"⚠️ {name}: этажность на Крыше не найдена, мусор снят (NULL)")
        time.sleep(2)

    # 2) пропущенный мусор
    n = psql("""UPDATE complexes SET is_garbage = TRUE
                WHERE is_garbage IS NOT TRUE AND name ~ '💥|🔥|✨|📐' RETURNING id""")
    print(f"мусорных допомечено: {len(n.splitlines()) if n else 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
