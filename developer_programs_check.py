#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Еженедельная проверка программ покупки у застройщиков (developer_programs).

Для каждого застройщика из реестра заново тянет страницу(ы) программ с его
сайта, извлекает карточки программ сайт-специфичным экстрактором и UPSERT-ит
в developer_programs. Вывод в stdout = отчёт (systemd journal; Hermes-крон
может забирать и доставлять в чат).

Правило щадящего парсинга: пауза >= 1 c между сайтами, по одному запросу
на страницу.
"""
import datetime
import html as html_mod
import json
import re
import subprocess
import sys
import time
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36"
BASE = "https://sat-ns.kz"


def fetch(url: str, timeout: int = 40) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "ru-RU,ru;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot",
                        "-t", "-A", "-c", sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()


def esc(s) -> str:
    return str(s).replace("'", "''") if s is not None else ""


# ── Экстракторы ──────────────────────────────────────────────────────────────

def extract_satns(html: str) -> list[dict]:
    """SAT-NS /ru/mortgage: карточки .mortgage-program (программы) и
    .bank-card (банки внутри программ)."""
    progs = []
    # 1) программы
    for m in re.finditer(r'<div\s+class="mortgage-program"[^>]*data-program="([^"]+)"', html):
        data_prog = m.group(1)
        block_end = html.find('class="mortgage-program"', m.end())
        block = html[m.start():block_end if block_end != -1 else m.start() + 6000]
        def grab(cls):
            mm = re.search(r'class="' + cls + r'"[^>]*>(.*?)</div>', block, re.S)
            return re.sub(r"<[^>]+>", "", mm.group(1)).strip() if mm else ""
        rate = re.sub(r"\s+", " ", grab(r"mortgage-program-card__rate"))
        term = re.sub(r"\s+", " ", grab(r"mortgage-program-card__term"))
        tags = re.findall(r'class="mortgage-tag">([^<]+)</span>', block)
        title = tags[0] if tags else data_prog
        other = ", ".join(tags[1:])
        desc_bits = []
        if rate:
            desc_bits.append("ставка " + rate)
        if term:
            desc_bits.append("срок " + term)
        if other:
            desc_bits.append(other)
        url = f"{BASE}/ru/mortgage?program={data_prog}" if data_prog != "standard" else f"{BASE}/ru/mortgage"
        progs.append({"title": html_mod.unescape(title),
                      "description": "; ".join(desc_bits) or None,
                      "url": url})
    # 2) банки (карточки .bank-card)
    for m in re.finditer(r'<div\s+class="bank-card"', html):
        block_end = html.find('class="bank-card"', m.end())
        block = html[m.start():block_end if block_end != -1 else m.start() + 4000]
        name_m = re.search(r'bank-card__name">([^<]+)</div>', block)
        badge_m = re.search(r'bank-card__badge">([^<]+)</div>', block)
        rate_m = re.search(r'bank-card__item rate">.*?bank-card__value">\s*([^<]+?)\s*</span>', block, re.S)
        cost_m = re.search(r'bank-card__label">([^<]+)</span>\s*<span class="bank-card__value">([^<]+)</span>', block)
        details = re.findall(r'bank-card__label">([^<]+)</span>\s*<span class="bank-card__value">([^<]+)</span>', block)
        if not name_m:
            continue
        name = name_m.group(1).strip()
        badge = badge_m.group(1).strip() if badge_m else ""
        desc_bits = []
        if badge:
            desc_bits.append("программа " + badge)
        if rate_m:
            desc_bits.append("ставка " + re.sub(r"\s+", " ", rate_m.group(1)).strip())
        for lbl, val in details:
            clean_val = re.sub(r"\s+", " ", val).strip()
            desc_bits.append(f"{lbl.lower()} {clean_val}")
        progs.append({"title": "Ипотека — " + html_mod.unescape(name),
                      "description": "; ".join(desc_bits) or None,
                      "url": f"{BASE}/ru/mortgage"})
    return progs


# ── Реестр: developer_id → (источник, URL, экстрактор, статичные программы) ─
REGISTRY = [
    # (developer_id, source, [urls], extractor, [static (title, desc, url)])
    (7, "sat-ns.kz/ru/mortgage", [f"{BASE}/ru/mortgage"], extract_satns,
     [("Рассрочка", "Рассрочка на определённые жилые комплексы — условия в отделе продаж.",
       f"{BASE}/ru")]),
    # остальные застройщики добавляются по мере готовности экстракторов:
    # (1, "bi.group", [...], extract_bi, []), (16, "sensata.kz", [...], ...)
]


def main() -> int:
    changed = 0
    report = []
    for dev_id, source, urls, extractor, statics in REGISTRY:
        try:
            all_progs: dict = {}
            for u in urls:
                h = fetch(u)
                for p in extractor(h):
                    all_progs.setdefault(p["title"], p)
                time.sleep(1.2)  # щадящий режим
            for title, desc, url in statics:
                all_progs.setdefault(title, {"title": title, "description": desc, "url": url})
            if not all_progs:
                report.append(f"⚠️ dev {dev_id}: страница загрузилась, но программ не найдено")
                continue
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            for title, p in all_progs.items():
                r = psql(
                    "SELECT title FROM developer_programs WHERE developer_id = %d AND title = '%s'"
                    % (dev_id, esc(title)))
                if not r:
                    psql(
                        "INSERT INTO developer_programs (developer_id, title, description, url, source) "
                        "VALUES (%d, '%s', '%s', '%s', '%s')"
                        % (dev_id, esc(title), esc(p.get("description")), esc(p.get("url")), esc(source)))
                    report.append(f"➕ {dev_id}: «{title}»")
                    changed += 1
                else:
                    cur = psql(
                        "SELECT COALESCE(description,'') || '|' || COALESCE(url,'') FROM developer_programs "
                        "WHERE developer_id = %d AND title = '%s'" % (dev_id, esc(title)))
                    new_desc = p.get("description") or ""
                    new_url = p.get("url") or ""
                    if cur != f"{new_desc}|{new_url}":
                        psql(
                            "UPDATE developer_programs SET description = '%s', url = '%s', "
                            "source = '%s', updated_at = now() WHERE developer_id = %d AND title = '%s'"
                            % (esc(new_desc), esc(new_url), esc(source), dev_id, esc(title)))
                        report.append(f"🔄 {dev_id}: «{title}» обновлена")
                        changed += 1
        except Exception as e:
            report.append(f"❌ dev {dev_id}: {str(e)[:200]}")

    # 2) общая проверка доступности ВСЕХ известных URL программ (для сайтов
    #    без полных экстракторов — bi.group, sensata, svoydom и т.д.): мёртвые
    #    ссылки попадут в отчёт.
    dead = 0
    for row in psql("SELECT id, developer_id, title, COALESCE(url,'') FROM developer_programs "
                    "WHERE url != '' ORDER BY developer_id").splitlines():
        if not row:
            continue
        pid, dev_id, title, url = row.split("|", 3)
        code = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
            with urllib.request.urlopen(req, timeout=25) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            code = None
        # 401/403 = защита от ботов (страница жива) — не считаем мёртвой
        if code is None or (code >= 400 and code not in (401, 403)):
            report.append(f"💀 dev {dev_id}: «{title}» — недоступна (http {code}) {url[:80]}")
            dead += 1
        time.sleep(0.8)

    print("── developer_programs_check " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M") + " ──")
    if report:
        print("\n".join(report))
    else:
        print("изменений нет")
    print(f"итого изменений: {changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
