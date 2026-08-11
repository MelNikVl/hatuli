#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Объединение дублей застройщиков v2: + перенос developer_id в apartment_listings."""
import subprocess

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

PAIRS = [
    (2, 71), (306, 71), (3, 16), (33, 87), (37, 163), (13, 148), (321, 96),
    (112, 6), (25, 295), (56, 72), (365, 152), (350, 259), (360, 164),
    (328, 229), (309, 300), (326, 305), (377, 261),
]

for dup_id, canon_id in PAIRS:
    exists = psql(f"SELECT COUNT(*) FROM developers WHERE id={dup_id}")
    if exists == "0":
        print(f"  {dup_id} уже объединён (удалён ранее)")
        continue
    canon = psql(f"SELECT name FROM developers WHERE id={canon_id}").splitlines()[0]
    dup = psql(f"SELECT name FROM developers WHERE id={dup_id}").splitlines()[0]
    n1 = psql(f"SELECT COUNT(*) FROM complexes WHERE developer_id={dup_id} AND is_garbage IS NOT TRUE")
    n2 = psql(f"SELECT COUNT(*) FROM apartment_listings WHERE developer_id={dup_id}")
    psql(f"UPDATE complexes SET developer_id={canon_id} WHERE developer_id={dup_id}")
    psql(f"UPDATE apartment_listings SET developer_id={canon_id} WHERE developer_id={dup_id}")
    # перенос данных если у канона пусто (homsters_slug — уникален, пропустить при конфликте)
    for col in ("homsters_slug", "website", "founded_year", "description", "projects_total",
                "projects_delivered", "projects_active", "score_total"):
        v = psql(f"SELECT COALESCE({col}::text,'') FROM developers WHERE id={dup_id}")
        if v:
            cur = psql(f"SELECT COALESCE({col}::text,'') FROM developers WHERE id={canon_id}")
            if not cur:
                try:
                    psql(f"UPDATE developers SET {col} = (SELECT {col} FROM developers WHERE id={dup_id}) WHERE id={canon_id}")
                except RuntimeError as e:
                    if "duplicate key" in str(e) or "unique" in str(e):
                        print(f"    (slug {col} конфликт — пропущен)")
                    else:
                        raise
    psql(f"DELETE FROM developers WHERE id={dup_id}")
    print(f"  {dup_id} {dup[:32]:32} -> {canon_id} {canon[:32]:32} (ЖК={n1}, объявл.={n2})")

print("\n=== ПРОВЕРКА ===")
print(psql("SELECT COUNT(*) || ' застройщиков' FROM developers"))
print(psql("SELECT d.id, d.name, COUNT(c.id) AS zhk FROM developers d LEFT JOIN complexes c ON c.developer_id=d.id AND c.is_garbage IS NOT TRUE WHERE d.name ILIKE '%bazis%' OR d.name ILIKE '%sensata%' GROUP BY 1,2 ORDER BY d.name"))
