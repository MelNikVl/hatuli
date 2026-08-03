#!/usr/bin/env python3
"""Щадящий парсер krisha-комплексов: «Количество квартир» → housing_class_test.
Лимиты из parse_settings: krisha_delay_sec (пауза между ЖК), krisha_batch (макс за проход).
Лог каждого прохода — в krisha_parse_log. Не грузим Крышу: по умолчанию 10 ЖК / 20 мин."""
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


def get_setting(key: str, default: str) -> str:
    v = psql(f"SELECT value FROM parse_settings WHERE key='{key}'")
    return v or default


def fetch(url: str) -> str:
    return urlopen(Request(url, headers={"User-Agent": UA}), timeout=30).read().decode("utf-8", "ignore")


def extract_count(html: str):
    m = re.search(r"Количество квартир</dt>\s*<dd[^>]*>\s*([\d\s]+)", html)
    if m:
        return int(re.sub(r"\s", "", m.group(1)))
    return None


def main() -> int:
    if get_setting("krisha_enabled", "1") == "0":
        print("парсер выключен (krisha_enabled=0)")
        return 0
    delay = float(get_setting("krisha_delay_sec", "120"))
    batch = int(get_setting("krisha_batch", "10"))

    # очередь: сначала ЖК без данных, потом с данными из других источников
    rows = [r.split("|") for r in psql(f"""
        SELECT c.id, c.name, c.krisha_url FROM complexes c
        LEFT JOIN housing_class_test hct ON hct.complex_id = c.id
        WHERE c.krisha_url IS NOT NULL
          AND (hct.apartment_count IS NULL
               OR hct.apartment_count_source IS DISTINCT FROM 'krisha')
        ORDER BY (hct.apartment_count IS NULL) DESC, c.id
        LIMIT {batch}""").splitlines() if r]
    if not rows:
        print("очередь пуста — всё спарсено")
        return 0

    done, errors = 0, 0
    for cid, name, url in rows:
        try:
            html = fetch(url)
            cnt = extract_count(html)
            if cnt is None:
                raise ValueError("параметр «Количество квартир» не найден")
            psql(f"""INSERT INTO housing_class_test (complex_id, apartment_count, apartment_count_source, apartment_count_parsed_at, updated_at)
                     VALUES ({cid}, {cnt}, 'krisha', now(), now())
                     ON CONFLICT (complex_id) DO UPDATE
                     SET apartment_count = EXCLUDED.apartment_count,
                         apartment_count_source = 'krisha',
                         apartment_count_parsed_at = now(), updated_at = now()""")
            psql(f"INSERT INTO krisha_parse_log (complex_id, complex_name, apartment_count, status) "
                 f"VALUES ({cid}, '{name.replace(chr(39), chr(39)*2)}', {cnt}, 'ok')")
            done += 1
            print(f"✅ {cid} {name}: {cnt} кв")
        except Exception as e:
            psql(f"INSERT INTO krisha_parse_log (complex_id, complex_name, status, detail) "
                 f"VALUES ({cid}, '{name.replace(chr(39), chr(39)*2)}', 'error', '{str(e)[:150].replace(chr(39), chr(39)*2)}')")
            errors += 1
            print(f"❌ {cid} {name}: {e}")
        time.sleep(delay)  # щадим Крышу

    print(f"итог: {done} ok, {errors} ошибок, пауза {delay:.0f}с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
