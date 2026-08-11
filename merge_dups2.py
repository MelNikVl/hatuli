#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Объединение 16 подтверждённых дублей (канон = с krisha_url/больше объявлений)."""
import subprocess, sys
sys.path.insert(0, '/home/nik/krisha_bot')
from complexes_cleanup import norm_name

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# (дубль, канон) — канон выбран по krisha_url/объявлениям/координатам
PAIRS = [
    (3868, 2141),   # Магистральный-3 -> Магистральный 3 (url)
    (2257, 3908),   # Достар 2 (url) <- Достар-2 — ВНИМАНИЕ: канон с url, дубль без
    (3864, 2082),   # Adamant Plus — Это Строящийся -> Adamant Plus (url)
    (2295, 3902),   # 6 микрорайон (Кюйши Дины 25) <- 6 микрорайон
    (1771, 3899),   # ЖК Respublika Towers (url) <- Respublika Towers
    (1096, 3901),   # Jetisu Aqsu (url) <- Jetisu.Aqsu
    (3081, 3859),   # Sunset Avenue <- Sunset Avenue | Sensata Group
    (3401, 3860),   # Oiu-2 <- Oiu-2 — Современный Дом
    (1767, 3896),   # ЖК Park City Forum (url) <- Park City Forum
    (207, 3897),    # ЖК Mangilik Park (url) <- Mangilik Park
    (129, 3891),    # ЖК Akan City (url) <- Akan City
    (1737, 3907),   # ЖК Gala One (url) <- Gala One
    (2536, 3892),   # ЖК MoD. Style (url) <- MoD. Style
    (1739, 3890),   # ЖК Beles City. Baq Sarai (url) <- Beles City. Baq Sarai
    (1786, 3904),   # ЖК Tasty (url) <- Tasty
    (1434, 3855),   # Arai Towers (url) <- Arai Towers — Стильная
]

for dup_id, canon_id in PAIRS:
    # проверяем: кто канон? тот у кого url или больше объявлений
    r = psql(f"""SELECT c.id, c.name, COALESCE(c.krisha_url,'') || '|' ||
                 (SELECT COUNT(*) FROM apartment_listings a WHERE lower(trim(a.complex_name))=lower(trim(c.name)))
                 FROM complexes c WHERE c.id IN ({dup_id}, {canon_id}) ORDER BY c.id""")
    infos = {}
    for line in r.splitlines():
        if not line:
            continue
        p = line.split('|')
        infos[int(p[0])] = (p[1], p[2])
    # решаем канон: с url > с объявлениями > первый
    def score(i):
        name, url_cnt = infos[i]
        url, cnt = url_cnt.rsplit('|', 1) if '|' in url_cnt else (url_cnt, '0')
        return (1 if url else 0, int(cnt or 0))
    if score(dup_id) >= score(canon_id):
        canon_id, dup_id = dup_id, canon_id
    dup_name, dc = infos[dup_id]
    canon_name, cc = infos[canon_id]
    # перепривязка объявлений
    n = psql(f"SELECT COUNT(*) FROM apartment_listings WHERE lower(trim(complex_name)) = '{dup_name.lower().strip().replace(chr(39), chr(39)*2)}'")
    psql(f"UPDATE apartment_listings SET complex_name = '{canon_name.replace(chr(39), chr(39)*2)}' "
         f"WHERE lower(trim(complex_name)) = '{dup_name.lower().strip().replace(chr(39), chr(39)*2)}'")
    psql(f"UPDATE complexes SET is_garbage=TRUE WHERE id={dup_id}")
    print(f"  {dup_id} {dup_name[:35]:35} -> {canon_id} {canon_name[:35]:35} (объявл.={n})")

print(psql("SELECT COUNT(*) || ' чистых ЖК' FROM complexes WHERE is_garbage IS NOT TRUE"))
