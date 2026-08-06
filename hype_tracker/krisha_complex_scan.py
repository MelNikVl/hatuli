#!/usr/bin/env python3
"""Щадящий парсер krisha-комплексов: количество квартир + описание + фото ЖК.
- apartment_count → housing_class_test
- description → complexes (ТОЛЬКО если пусто — «не менять существующие»)
- photos → complexes.photos (замена всех)
Лимиты из parse_settings. Лог — krisha_parse_log. Щадим Крышу (по умолч. 10 ЖК / 20 мин)."""
import json
import re
import subprocess
import sys
import time
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36"
MAX_PHOTOS = 10


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
    return int(re.sub(r"\s", "", m.group(1))) if m else None


def extract_build_status(html: str) -> str:
    """Статус строительства с карточки: «Строящийся» / «Сдан» / «Введен в эксплуатацию»."""
    m = re.search(r'data-name="home.state"[^>]*>\s*(?:<[^>]+>\s*)*?Статус строительства\s*(?:<[^>]+>\s*)*?([А-Яа-яЁё\s-]{3,40})', html)
    if m:
        val = re.sub(r'\s+', ' ', m.group(1)).strip()
        return val
    # fallback: ищем «Строящийся|Сдан|Введен в эксплуатацию|Возведение» рядом со «Статус строительства»
    i = html.find("Статус строительства")
    if i >= 0:
        seg = html[i:i + 1200]
        for w in ("Сдан в эксплуатацию", "Введен в эксплуатацию", "Строящийся", "Сдан", "Возведение"):
            if w.lower() in seg.lower():
                return w
    return None


def extract_deadline(html: str) -> str:
    """Срок сдачи: «Первая очередь - IV квартал 2021 г. Вторая очередь - IV квартал 2024 г.»"""
    m = re.search(r'data-name="deadline".*?Срок сдачи.*?<p[^>]*>(.*?)</p>', html, re.S)
    if m:
        txt = re.sub(r'<[^>]+>', ' ', m.group(1))
        txt = re.sub(r'\s+', ' ', txt).strip()
        if txt:
            return txt[:300]
    i = html.find("Срок сдачи")
    if i >= 0:
        seg = re.sub(r'<[^>]+>', ' ', html[i:i + 1500])
        seg = re.sub(r'\s+', ' ', seg).strip()
        seg = seg.replace("Срок сдачи", "", 1).strip()
        if seg:
            return seg[:250]
    return None


def extract_krisha_name(html: str):
    """Каноничное название ЖК из встроенного JSON страницы (объект комплекса —
    "name":"Hazar","prefix":"ЖК",... — идёт раньше названий самих объявлений
    в том же блоке, так что паттерн с prefix рядом достаточно специфичен)."""
    m = re.search(r'"name":"([^"]{2,80})","prefix":"ЖК"', html)
    if not m:
        return None
    name = m.group(1).strip()
    return name or None


def extract_description(html: str):
    """«О жилой недвижимости» — есть не на всех страницах."""
    m = re.search(r"О жилой недвижимости(.{0,2500}?)(<h[1-6]|О застройщике|</section>|</div>\s*</div>)", html, re.S)
    if not m:
        return None
    t = re.sub(r"<[^>]+>", " ", m.group(1))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:1500] if len(t) > 40 else None


def extract_photos(html: str):
    """Уникальные фото по базе URL (без размеров), максимум MAX_PHOTOS."""
    bases = {}
    for url in re.findall(r"https://krisha-photos\.kcdn\.online/[^\"\\ )]+", html):
        url = url.rstrip("/")
        if "kcdn" not in url:
            continue
        # база: убираем размерный суффикс (-120x90, -750x470, -400x300, -full...)
        base = re.sub(r"-(?:photo-)?\d+x\d+\.\w+$|-full\.\w+$", "", url)
        if base in bases:
            # предпочитаем более крупный размер
            cur = bases[base]
            if ("-full" in url) or ("750x470" in url and "750x470" not in cur):
                bases[base] = url
        else:
            bases[base] = url
    return list(bases.values())[:MAX_PHOTOS]


def esc(s):
    return s.replace(chr(39), chr(39) * 2) if s else s


def main() -> int:
    if get_setting("krisha_enabled", "1") == "0":
        print("парсер выключен (krisha_enabled=0)")
        return 0
    delay = float(get_setting("krisha_delay_sec", "120"))
    batch = int(get_setting("krisha_batch", "10"))

    # очередь: все ЖК с krisha_url, не обработанные полностью (счёт+описание+фото)
    rows = [r.split("|") for r in psql(f"""
        SELECT c.id, c.name, c.krisha_url FROM complexes c
        LEFT JOIN housing_class_test hct ON hct.complex_id = c.id
        WHERE c.krisha_url IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM krisha_parse_log l WHERE l.complex_id = c.id
                          AND l.status = 'error' AND l.ts > now() - interval '2 days')
          -- "готово" = была хотя бы одна успешная обработка за последние 30
          -- дней (потом освежаем). Раньше тут требовалось ЕЩЁ и непустое
          -- description — а «О жилой недвижимости» есть не на всех
          -- страницах Крыши, так что для ЖК без этого блока условие никогда
          -- не выполнялось, и очередь застревала на первых по id ЖК
          -- навсегда вместо продвижения по всей базе.
          AND NOT EXISTS (SELECT 1 FROM krisha_parse_log l2 WHERE l2.complex_id = c.id
                          AND l2.status = 'ok' AND l2.ts > now() - interval '30 days')
        ORDER BY c.id
        LIMIT {batch}""").splitlines() if r]
    if not rows:
        print("очередь пуста — всё обработано")
        return 0

    done, errors = 0, 0
    for cid, name, url in rows:
        try:
            html = fetch(url)
        except Exception as e404:
            # само-лечение: если страница умерла под astana, пробуем nur-sultan
            alt = url.replace("/complex/show/astana/", "/complex/show/nur-sultan/")
            if alt != url and "404" in str(e404):
                try:
                    html = fetch(alt)
                    psql(f"UPDATE complexes SET krisha_url = '{alt.replace(chr(39), chr(39) * 2)}' WHERE id = {cid}")
                    print(f"↪ {cid} {name}: переставлен на nur-sultan")
                except Exception:
                    raise e404
            else:
                raise e404
        try:
            cnt = extract_count(html)
            build_status = extract_build_status(html)
            deadline = extract_deadline(html)
            desc = extract_description(html)
            photos = extract_photos(html)
            krisha_name = extract_krisha_name(html)

            # Самовосстановление имени: держим complexes.name синхронным с
            # тем, как ЖК называется на самой Крыше (источник истины по
            # именам/фото). Обёрнуто в свой try — конфликт уникального
            # индекса (name уже занят другой строкой, обычно недобитый
            # дубль) не должен рушить учёт кв/описания/фото по этому ЖК.
            if krisha_name and krisha_name.strip().casefold() != (name or "").strip().casefold():
                try:
                    old_name = name
                    psql(f"UPDATE complexes SET name = '{esc(krisha_name)}' WHERE id = {cid}")
                    # держим apartment_listings.complex_name (свободный текст,
                    # не FK) в согласии с переименованием — иначе разъедемся
                    # заново с тем же классом бага, который чинила консолидация.
                    if old_name:
                        psql(f"UPDATE apartment_listings SET complex_name = '{esc(krisha_name)}' "
                             f"WHERE lower(btrim(complex_name)) = lower('{esc(old_name)}')")
                    print(f"✏️ {cid}: имя «{old_name}» → «{krisha_name}» (по Крыше)")
                    name = krisha_name
                except Exception as e_name:
                    print(f"⚠️ {cid} {name}: не удалось переименовать в «{krisha_name}»: {e_name}")

            if cnt is not None:
                psql(f"""INSERT INTO housing_class_test (complex_id, apartment_count, apartment_count_source, apartment_count_parsed_at, updated_at)
                         VALUES ({cid}, {cnt}, 'krisha', now(), now())
                         ON CONFLICT (complex_id) DO UPDATE
                         SET apartment_count = EXCLUDED.apartment_count,
                             apartment_count_source = 'krisha',
                             apartment_count_parsed_at = now(), updated_at = now()""")
            if build_status or deadline:
                upd = []
                if build_status:
                    upd.append(f"build_status = '{esc(build_status)}'")
                if deadline:
                    upd.append(f"deadline = '{esc(deadline)}'")
                psql(f"UPDATE complexes SET {', '.join(upd)}, updated_at = now() WHERE id = {cid}")

            # описание — ТОЛЬКО если пусто
            if desc:
                psql(f"UPDATE complexes SET description = '{esc(desc)}' WHERE id = {cid} AND (description IS NULL OR description = '')")
            # фото — замена всех
            if photos:
                psql(f"UPDATE complexes SET photos = '{json.dumps(photos, ensure_ascii=False)}'::jsonb WHERE id = {cid}")

            psql(f"INSERT INTO krisha_parse_log (complex_id, complex_name, apartment_count, status, detail) "
                 f"VALUES ({cid}, '{esc(name)}', {cnt if cnt is not None else 'NULL'}, 'ok', "
                 f"'{esc('описание: ' + ('да' if desc else 'нет') + ', фото: ' + str(len(photos)))}')")
            done += 1
            print(f"✅ {cid} {name}: кв={cnt} описание={'да' if desc else 'нет'} фото={len(photos)}")
        except Exception as e:
            psql(f"INSERT INTO krisha_parse_log (complex_id, complex_name, status, detail) "
                 f"VALUES ({cid}, '{esc(name)}', 'error', '{esc(str(e)[:150])}')")
            errors += 1
            print(f"❌ {cid} {name}: {e}")
        time.sleep(delay)

    print(f"итог: {done} ok, {errors} ошибок, пауза {delay:.0f}с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
