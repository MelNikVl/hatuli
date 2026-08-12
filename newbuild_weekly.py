#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Еженедельный полный обход застройщиков (раздел «Новостройки»).

Запускает все *_import.py по очереди (каждый парсит сайт своего застройщика
и пишет в newbuild_units/complexes через newbuild_common.py). После обхода
печатает сводку по застройщикам. Не включает Svoy Dom — его пайплайн ручной
(svoydom_scrape3.py → svoydom_matched.json → load_svoydom.py).

Таймер: krisha-newbuild.timer (пн 06:00 + RandomizedDelaySec=30m)."""
import subprocess
import sys
import time

VENV = "/home/nik/krisha_bot/venv/bin/python"
WORKDIR = "/home/nik/krisha_bot"

IMPORTERS = [
    "bi_group_import.py",     # BI Group (apigw.bi.group, шахматки)
    "sensata_import.py",      # Sensata Group
    "orda_invest_import.py",  # ORDA INVEST
    "bazis_import.py",        # BAZIS-А
    "nak_import.py",          # NAK
]


def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot",
                        "-t", "-A", "-c", sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()


def main() -> int:
    rc = 0
    t0 = time.time()
    for imp in IMPORTERS:
        print(f"── {imp} ──", flush=True)
        try:
            r = subprocess.run([VENV, imp], cwd=WORKDIR, timeout=3600)
            if r.returncode != 0:
                rc = 1
                print(f"!! {imp}: rc={r.returncode}", flush=True)
        except subprocess.TimeoutExpired:
            rc = 1
            print(f"!! {imp}: таймаут 60 мин", flush=True)
        except Exception as e:
            rc = 1
            print(f"!! {imp}: {e}", flush=True)

    # Сводка по застройщикам
    print("── сводка ──", flush=True)
    try:
        print(psql("""SELECT d.name || ': ' || count(u.id) || ' юнитов ('
                      || count(*) FILTER (WHERE u.status='available') || ' в наличии)'
                      FROM developers d
                      JOIN complexes c ON c.developer_id = d.id AND c.is_newbuild
                      LEFT JOIN newbuild_units u ON u.complex_id = c.id
                      GROUP BY d.name ORDER BY 1"""))
    except Exception as e:
        print(f"сводка не удалась: {e}", flush=True)

    print(f"готово за {time.time() - t0:.0f} с, rc={rc}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
