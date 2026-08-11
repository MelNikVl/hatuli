#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Объединение дублей застройщиков: канон = тот, на кого ссылаются ЖК (developer_id)."""
import subprocess

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# (дубль, канон) — канон определён по developer_id ЖК
PAIRS = [
    (2, 71),    # Bazis-A -> BAZIS-А (8 ЖК)
    (306, 71),  # BAZIS–А -> BAZIS-А
    (3, 16),    # Sensata -> Sensata Group (14 ЖК)
    (33, 87),   # ТОО «Pangaea» -> ТОО Pangaea (1 ЖК)
    (37, 163),  # Алма-Инвест-Холдинг ТОО -> Алма-Инвест-Холдинг
    (13, 148),  # Политренд Астана ТОО -> Политренд Астана
    (321, 96),  # Boston Construction -> Boston construction
    (112, 6),   # ТОО El Invest -> El Invest
    (25, 295),  # ТОО «INTECO» -> ТОО INTECO Eurasia
    (56, 72),   # SVOYDOM -> Svoy Dom (36 ЖК)
    (365, 152), # МС-7 ТОО -> МС-7
    (350, 259), # Asyl Tas Qala -> ЖСК Asyl Tas Qala
    (360, 164), # Астана Недвижимость -> Астана-Недвижимость (8 ЖК)
    (328, 229), # Успешный Дом ЖСК -> Успешный дом ЖСК
    (309, 300), # BASTAU BUILD GROUP -> Bastau Build Group
    (326, 305), # TS Com -> ТОО TS Com
    (377, 261), # Эко Поток -> ТОО Эко Поток
]

for dup_id, canon_id in PAIRS:
    # имя канона
    canon = psql(f"SELECT name FROM developers WHERE id={canon_id}").splitlines()[0]
    dup = psql(f"SELECT name FROM developers WHERE id={dup_id}").splitlines()[0]
    # перенос ЖК
    n = psql(f"SELECT COUNT(*) FROM complexes WHERE developer_id={dup_id} AND is_garbage IS NOT TRUE")
    psql(f"UPDATE complexes SET developer_id={canon_id} WHERE developer_id={dup_id}")
    # перенос данных: homsters_slug/website если у канона пусто
    for col in ("homsters_slug", "website", "founded_year", "description", "projects_total",
                "projects_delivered", "projects_active", "score_total"):
        v = psql(f"SELECT COALESCE({col}::text,'') FROM developers WHERE id={dup_id}")
        if v:
            cur = psql(f"SELECT COALESCE({col}::text,'') FROM developers WHERE id={canon_id}")
            if not cur:
                psql(f"UPDATE developers SET {col} = (SELECT {col} FROM developers WHERE id={dup_id}) WHERE id={canon_id}")
    # дубль -> пометка (нет is_garbage в developers, удаляем дубль после переноса)
    psql(f"DELETE FROM developers WHERE id={dup_id}")
    print(f"  {dup_id} {dup[:35]:35} -> {canon_id} {canon[:35]:35} (ЖК перенесено: {n})")

print("\n=== ПРОВЕРКА ===")
print(psql("SELECT COUNT(*) || ' застройщиков осталось' FROM developers"))
print(psql("SELECT d.id, d.name, COUNT(c.id) AS zhk FROM developers d LEFT JOIN complexes c ON c.developer_id=d.id AND c.is_garbage IS NOT TRUE WHERE d.name ILIKE '%bazis%' OR d.name ILIKE '%sensata%' GROUP BY 1,2 ORDER BY d.name"))
