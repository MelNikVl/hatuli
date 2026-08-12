#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пометить ЖК Svoy Dom (Астана) is_newbuild + developer_id=72."""
import subprocess

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return r.stdout.strip()

# CSV-имена -> поиск в БД по lower(name)
pairs = [
    ('Shalqar', 'shalqar'), ('Altyn Emel', 'altyn emel'), ('Aqterek', 'aqterek'),
    ('Aqterek 2', 'aqterek 2'), ('Araily', 'araily'), ('Baiqadam', 'baiqadam'),
    ('Baisal', 'baisal'), ('Elaman', 'elaman'), ('Umit', 'umit'),
    ('Qadam', 'qadam'), ('Gauhartas 2', 'gauhartas 2'), ('Gauhartas', 'gauhartas'),
    ('Asyl Meken', 'asyl meken'), ('Jana Qala', 'jana qala'), ('Arman Meken', 'arman meken'),
]
for csv_name, pat in pairs:
    rows = psql(f"""
        SELECT id || chr(9) || name || chr(9) || COALESCE(developer_id::text,'') || chr(9) || COALESCE(is_newbuild::text,'') || chr(9) || 'X'
        FROM complexes WHERE lower(name) LIKE '%{pat}%' AND is_garbage IS NOT TRUE AND is_street IS NOT TRUE
        ORDER BY (krisha_url IS NOT NULL) DESC, id LIMIT 3
    """)
    print(f'--- {csv_name} ({pat}) ---')
    for l in rows.splitlines():
        if not l:
            continue
        p = l.split('\t')
        print(f'  id={p[0]} | {p[1][:40]} | dev={p[2]} | nb={p[3]}')
