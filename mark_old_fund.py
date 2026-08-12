#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пометить пятиэтажки (floors_total=5, avg price/m2 <= 700k) как старый фонд
в house_years: is_old_fund=true, year_built=1970 (только где нет года)."""
import subprocess

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return r.stdout.strip()

# 1) колонка is_old_fund в house_years, если нет
cols = psql("SELECT column_name FROM information_schema.columns WHERE table_name='house_years' AND column_name='is_old_fund'")
if not cols:
    psql("ALTER TABLE house_years ADD COLUMN is_old_fund BOOLEAN NOT NULL DEFAULT FALSE")
    print("колонка is_old_fund добавлена")

# 2) адреса пятиэтажек со средней ценой/м² <= 700к
adr = psql("""
    SELECT DISTINCT lower(trim(regexp_replace(address, '\\s*—.*$', '')))
    FROM apartment_listings
    WHERE floors_total = 5 AND price > 0 AND area > 0
      AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
      AND address IS NOT NULL AND address != ''
""").splitlines()
print(f"адресов пятиэтажек: {len(adr)}")

# средняя цена/м² по адресам
avg_rows = psql("""
    SELECT addr || chr(9) || AVG(pm2)::text FROM (
        SELECT lower(trim(regexp_replace(address, '\\s*—.*$', ''))) AS addr,
               price/NULLIF(area,0) AS pm2
        FROM apartment_listings
        WHERE floors_total = 5 AND price > 0 AND area > 0
          AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
          AND address IS NOT NULL AND address != ''
    ) t GROUP BY addr
""").splitlines()
avg_map = {}
for l in avg_rows:
    if not l:
        continue
    p = l.split('\t')
    try:
        avg_map[p[0].strip().lower()] = float(p[1])
    except Exception:
        pass

targets = [a.strip().lower() for a in adr if a.strip().lower() in avg_map and avg_map[a.strip().lower()] <= 700000]
print(f"из них со средней ценой/м² <= 700k: {len(targets)}")

# 3) UPDATE батчами: is_old_fund=true, year_built=1970 где нет
batch = []
upd = 0
for addr in targets:
    safe = addr.replace("'", "''")
    batch.append(f"('{safe}')")
    if len(batch) >= 200:
        vals = ','.join(batch)
        psql(f"""UPDATE house_years SET is_old_fund = TRUE,
                 year_built = COALESCE(year_built, 1970)
                 WHERE address IN (SELECT * FROM (VALUES {vals}) AS v(a))""")
        upd += len(batch)
        batch = []
if batch:
    vals = ','.join(batch)
    psql(f"""UPDATE house_years SET is_old_fund = TRUE,
             year_built = COALESCE(year_built, 1970)
             WHERE address IN (SELECT * FROM (VALUES {vals}) AS v(a))""")
    upd += len(batch)

print(f"обновлено записей house_years: {upd}")

# 4) сводка
print("")
print(psql("""
    SELECT COUNT(*) AS vsego, COUNT(*) FILTER (WHERE is_old_fund) AS star_fond,
           COUNT(*) FILTER (WHERE is_old_fund AND year_built = 1970) AS god_1970
    FROM house_years
"""))
