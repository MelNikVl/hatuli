#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Добавить недостающие адреса старого фонда в house_years (INSERT ON CONFLICT)."""
import subprocess

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return r.stdout.strip()

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
targets = []
for l in avg_rows:
    if not l:
        continue
    p = l.split('\t')
    try:
        if float(p[1]) <= 700000:
            targets.append(p[0].strip().lower())
    except Exception:
        pass

# есть ли уже в house_years
existing = set(psql("SELECT address FROM house_years").splitlines())
todo = [a for a in targets if a not in existing]
print(f"адресов старого фонда: {len(targets)}, уже в house_years: {len(targets)-len(todo)}, добавить: {len(todo)}")

batch = []
added = 0
for addr in todo:
    safe = addr.replace("'", "''")
    batch.append(f"('{safe}', 1970, TRUE, {len(batch) + added})")
    if len(batch) >= 200:
        vals = ','.join(batch)
        psql(f"""INSERT INTO house_years (address, year_built, is_old_fund, listings_cnt)
                 VALUES {vals} ON CONFLICT (address) DO NOTHING""")
        added += len(batch)
        batch = []
if batch:
    vals = ','.join(batch)
    psql(f"""INSERT INTO house_years (address, year_built, is_old_fund, listings_cnt)
             VALUES {vals} ON CONFLICT (address) DO NOTHING""")
    added += len(batch)

print(f"добавлено: {added}")

# сводка
print("")
print(psql("""
    SELECT COUNT(*) AS vsego, COUNT(*) FILTER (WHERE is_old_fund) AS star_fond,
           COUNT(*) FILTER (WHERE is_old_fund AND year_built = 1970) AS god_1970,
           COUNT(*) FILTER (WHERE year_built IS NOT NULL) AS s_godom
    FROM house_years
"""))
