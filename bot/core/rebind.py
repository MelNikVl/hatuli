"""
Привязка объявлений к ЖК/адресу/координатам — общая логика.

Используется и веб-кнопкой /admin/rebind (мгновенный отклик + прогресс),
и фоновым сервисом service_geobind.py (периодический автозапуск).

Стадии (см. run_rebind), в порядке приоритета:
  A0. Адрес из заголовка, если address пуст.
  A.  Привязка по ссылке на карточку ЖК (complex_url == complexes.krisha_url) —
      это структурные данные от самой Крыши, самый надёжный сигнал.
  A1. Привязка по АДРЕСУ: если нормализованный адрес объявления совпадает
      с адресом, на котором УЖЕ надёжно (стадии A/B прошлых прогонов) сидит
      один-единственный ЖК — привязываем и его. Если по этому адресу ЖК не
      числится вовсе — не гадаем, оставляем как есть (скорее всего старый
      дом без ЖК, см. bind_by_address).
  B.  Название ЖК, найденное в заголовке (не в адресе — там улицы).
  D.  Geocode fallback: адрес есть, координат нет — Nominatim (bot.core.geo).
      Ограничено батчем за прогон (Nominatim: 1 запрос/сек, ToS).

УБРАНА стадия геопривязки «ближайший ЖК ≤350м по прямой» — она слепо
присваивала complex_name самому близкому ЖК по координатам, даже без
текстового/ссылочного подтверждения, что реально засоряло статистику ЖК
фантомными объявлениями (объявление в 300м от чужого ЖК подписывалось им).
Без надёжного способа подтвердить принадлежность — лучше оставить объявление
непривязанным (виден на /admin/unbound) и обработать вручную/другим методом,
чем привязать неверно. Стадия A1 (по адресу) — НЕ то же самое: это точное
совпадение НОРМАЛИЗОВАННОЙ СТРОКИ адреса с адресом уже подтверждённых
объявлений того же ЖК, а не близость по прямой — гораздо надёжнее и не
страдает той же проблемой (два соседних, но РАЗНЫХ ЖК с разными адресами
не перепутаются).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

ProgressCB = Callable[[str], Awaitable[None] | None]


async def _report(progress_cb: ProgressCB | None, stage: str) -> None:
    if progress_cb is None:
        return
    result = progress_cb(stage)
    if asyncio.iscoroutine(result):
        await result


async def record_unbound_snapshot() -> None:
    """Пишет строку в unbound_stats_history — источник графика на /admin/unbound."""
    from bot.db.pg import fetchval as pg_fv, execute as pg_exec

    total_active = await pg_fv(
        "SELECT COUNT(*) FROM apartment_listings "
        "WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE") or 0
    unbound = await pg_fv(
        "SELECT COUNT(*) FROM apartment_listings "
        "WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE "
        "AND (complex_name IS NULL OR btrim(complex_name) = '')") or 0
    unbound_coords = await pg_fv(
        "SELECT COUNT(*) FROM apartment_listings "
        "WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE "
        "AND (complex_name IS NULL OR btrim(complex_name) = '') AND lat IS NOT NULL") or 0
    await pg_exec(
        "INSERT INTO unbound_stats_history (total_active, unbound, unbound_coords) "
        "VALUES ($1, $2, $3)", total_active, unbound, unbound_coords)


async def bind_by_address(target_table: str) -> int:
    """Привязка по точному совпадению НОРМАЛИЗОВАННОГО адреса — общая для
    apartment_listings (стадия A1 в run_rebind) и rental_listings (см.
    service_rental.py). Правило "адрес -> ЖК" строится ВСЕГДА из
    apartment_listings (это основной, ревизируемый набор данных с
    is_garbage/is_street аудитом complexes) и применяется к unbound-строкам
    целевой таблицы (той же apartment_listings либо rental_listings —
    у обеих одинаковые колонки id/address/complex_name).

    Оставляем только адреса, которые указывают ровно на ОДИН ЖК (если один
    и тот же нормализованный адрес встречается у двух разных ЖК — это,
    скорее всего, коллизия улицы/двора, а не факт, — такие пропускаем, не
    угадываем). Адрес без номера дома тоже пропускаем — слишком общий текст
    ("улица Кенесары" без номера подошёл бы половине квартала).

    В отличие от убранной геопривязки "ближайший ЖК ≤350м" (см. докстринг
    модуля) — это точное совпадение строки адреса с адресом уже
    подтверждённых объявлений того же ЖК, а не близость по прямой: два
    соседних, но РАЗНЫХ ЖК с разными адресами не перепутаются."""
    if target_table not in ("apartment_listings", "rental_listings"):
        raise ValueError(target_table)
    from bot.db.pg import fetch as pg_fetch, execute as pg_exec

    addr_rules_rows = await pg_fetch(r"""
        SELECT naddr, (array_agg(complex_name))[1] AS complex_name
        FROM (
            SELECT DISTINCT lower(trim(regexp_replace(al.address, '\s*—.*$', ''))) AS naddr,
                   c.name AS complex_name
            FROM apartment_listings al
            JOIN complexes c ON lower(trim(c.name)) = lower(trim(al.complex_name))
            WHERE al.address IS NOT NULL AND btrim(al.address) != ''
              AND al.address ~ '[0-9]'
              AND al.complex_name IS NOT NULL AND btrim(al.complex_name) != ''
              AND COALESCE(c.is_garbage, FALSE) = FALSE
              AND COALESCE(c.is_street, FALSE) = FALSE
        ) t
        GROUP BY naddr
        HAVING COUNT(*) = 1
    """)
    addr_rules = {r["naddr"]: r["complex_name"] for r in addr_rules_rows}
    if not addr_rules:
        return 0

    unbound_addr_rows = await pg_fetch(rf"""
        SELECT id, lower(trim(regexp_replace(address, '\s*—.*$', ''))) AS naddr
        FROM {target_table}
        WHERE (complex_name IS NULL OR btrim(complex_name) = '')
          AND address IS NOT NULL AND btrim(address) != ''
    """)
    by_addr_updates = [(r["id"], addr_rules[r["naddr"]])
                        for r in unbound_addr_rows if r["naddr"] in addr_rules]
    for lid, canon in by_addr_updates:
        await pg_exec(
            f"UPDATE {target_table} SET complex_name = $2 WHERE id = $1",
            lid, canon)
    return len(by_addr_updates)


async def run_rebind(progress_cb: ProgressCB | None = None) -> dict:
    """Стадии A0/A/B/C. Идемпотентно, можно запускать повторно."""
    from bot.db.pg import fetch as pg_fetch, execute as pg_exec, fetchval as pg_fv

    await pg_exec("ALTER TABLE complexes ADD COLUMN IF NOT EXISTS krisha_url TEXT")

    # ── A0: адрес из заголовка («…2/5 этаж, Сатпаева 19 — Майлина») ──
    await _report(progress_cb, "адреса из заголовков…")
    addr_rows = await pg_fetch("""
        SELECT id, title FROM apartment_listings
        WHERE (address IS NULL OR btrim(address) = '') AND title IS NOT NULL
    """)
    _addr_re = re.compile(r",\s*([^,]{2,40}?\d[^,]{0,20}?)\s*(?=,|—|–|$)")
    _letters = re.compile(r"[А-Яа-яЁёA-Za-z]{2}")
    addr_updates = []
    for r in addr_rows:
        hit = None
        for m in _addr_re.finditer(r["title"] or ""):
            cand = m.group(1).strip()
            if _letters.search(cand) and not cand.lower().startswith(("м²", "м2")):
                hit = cand
                break
        if hit:
            addr_updates.append((r["id"], hit))
    for lid, addr in addr_updates:
        await pg_exec(
            "UPDATE apartment_listings SET address = $2 WHERE id = $1", lid, addr)
    addr_filled = len(addr_updates)

    # ── A: склейка по ссылке на карточку ЖК ────────────────────────────
    await _report(progress_cb, "по ссылке на карточку ЖК…")
    by_url = (await pg_exec("""
        UPDATE apartment_listings al SET complex_name = c.name
        FROM complexes c
        WHERE (al.complex_name IS NULL OR btrim(al.complex_name) = '')
          AND al.complex_url IS NOT NULL AND c.krisha_url IS NOT NULL
          AND al.complex_url = c.krisha_url
    """) or "").split()[-1]

    # ── A1: привязка по точному совпадению адреса ──────────────────────
    await _report(progress_cb, "по адресу подтверждённых ЖК…")
    by_addr = await bind_by_address("apartment_listings")

    # ── B: название ЖК в заголовке (НЕ в адресе — там улицы) ───────────
    await _report(progress_cb, "по названию из заголовков…")
    complexes = await pg_fetch(
        "SELECT name FROM complexes WHERE name IS NOT NULL AND btrim(name) != ''"
        " AND COALESCE(is_street, FALSE) = FALSE")

    def _norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r"^\s*(жк|кг)\.?\s+", "", s)
        s = re.sub(r"[«»\"']", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    norm_map = {}
    for c in complexes:
        n = _norm(c["name"])
        if len(n) >= 3:
            norm_map[n] = c["name"]
    norm_items = sorted(norm_map.items(), key=lambda kv: -len(kv[0]))
    norm_items = [
        (n, canon, re.compile(rf"(?<![а-яёa-z0-9]){re.escape(n)}(?![а-яёa-z0-9])"))
        for n, canon in norm_items
    ]

    rows = await pg_fetch("""
        SELECT id, title FROM apartment_listings
        WHERE complex_name IS NULL OR btrim(complex_name) = ''
    """)
    updates = []
    for i, r in enumerate(rows):
        if i % 500 == 0:
            await _report(progress_cb, f"по заголовкам… {i}/{len(rows)}")
            await asyncio.sleep(0)  # отдаём event loop
        hay = _norm(r["title"] or "")
        if not hay:
            continue
        hit = None
        for n, canon, rx in norm_items:
            if n in hay and (f"жк {n}" in hay or rx.search(hay)):
                hit = canon
                break
        if hit:
            updates.append((r["id"], hit))
    await _report(progress_cb, f"запись {len(updates)} привязок…")
    for lid, canon in updates:
        await pg_exec(
            "UPDATE apartment_listings SET complex_name = $2 WHERE id = $1",
            lid, canon)
    by_text = len(updates)

    # Стадия C (геопривязка по ближайшему ЖК ≤350м — угадывание БЕЗ
    # текстового подтверждения) убрана — см. докстринг модуля. by_geo
    # оставлен в возвращаемом словаре для совместимости с вызывающим кодом
    # (UI показывает "по гео: 0"), но всегда 0.
    by_geo = 0

    # ── E: координаты ЖК для объявлений, у которых ЖК ПОДТВЕРЖДЁН текстом/
    # ссылкой (complex_name уже проставлен — A/B выше или detail-страница),
    # но собственные координаты объявления либо отсутствуют, либо явно не
    # совпадают с ЖК (>~600м — то есть GPS с Крыши смотрит не туда, встречается
    # у объявлений с неточным пином на карте источника). Это НЕ угадывание —
    # ЖК уже надёжно определён текстом, просто чиним геопозицию под него.
    await _report(progress_cb, "координаты по подтверждённому ЖК…")
    geo_fixed = (await pg_exec("""
        UPDATE apartment_listings al
        SET lat = c.lat, lon = c.lon, geo_source = 'complex_confirmed'
        FROM complexes c
        WHERE al.complex_name = c.name
          AND c.lat IS NOT NULL AND c.lon IS NOT NULL
          AND COALESCE(c.is_street, FALSE) = FALSE
          AND (
            al.lat IS NULL
            OR ((al.lat - c.lat)^2 + (al.lon - c.lon)^2) > 4.0e-5
          )
    """) or "").split()[-1]

    left = await pg_fv("""
        SELECT COUNT(*) FROM apartment_listings
        WHERE is_active IS NOT FALSE
          AND (complex_name IS NULL OR btrim(complex_name) = '')
    """) or 0
    logger.info("rebind: by_url=%s by_addr=%d by_text=%d by_geo=%s geo_fixed=%s, осталось без ЖК %d",
                by_url, by_addr, by_text, by_geo, geo_fixed, left)
    return {
        "ok": True,
        "bound": int(by_url or 0) + by_addr + by_text + int(by_geo or 0),
        "by_url": int(by_url or 0), "by_addr": by_addr, "by_text": by_text,
        "by_geo": int(by_geo or 0), "geo_fixed": int(geo_fixed or 0), "left": left,
        "addr_filled": addr_filled,
    }


_ADDR_JUNK_TAIL = re.compile(r"\s*[—–]\s*.*$")  # "Турар Рыскулов 9 — ГОРЯЧЕЕ ПРЕДЛОЖЕНИЕ" -> "Турар Рыскулов 9"
_ADDR_DISTRICT_PREFIX = re.compile(r"^\s*[^,]*р-н[^,]*,\s*", re.IGNORECASE)  # "Есильский р-н, X" -> "X"


def _clean_address_for_geocode(address: str) -> str:
    """Адрес в БД часто содержит маркетинговый хвост после тире и/или
    дублирующийся префикс района (город и так добавляется отдельно
    через параметр city) — оба мусорят запрос к Nominatim и валят матч."""
    cleaned = _ADDR_JUNK_TAIL.sub("", address).strip()
    cleaned = _ADDR_DISTRICT_PREFIX.sub("", cleaned).strip()
    return cleaned or address


async def geocode_missing_coords(
    progress_cb: ProgressCB | None = None,
    batch_size: int = 300,
    city: str = "astana",
) -> dict:
    """
    Geocode fallback: объявления с адресом, но без координат и без ЖК
    (т.е. detail-fetch ещё не дошёл или не даст — например снят с публикации).
    Nominatim: 1 запрос/сек (см. bot.core.geo), поэтому батч ограничен —
    это фоновая подстраховка, не замена detail-fetch координатам с сайта.
    """
    from bot.core.geo import geocode
    from bot.db.pg import fetch as pg_fetch, execute as pg_exec

    await _report(progress_cb, "geocode: выборка адресов без координат…")
    rows = await pg_fetch("""
        SELECT id, address, district
        FROM apartment_listings
        WHERE is_active IS NOT FALSE
          AND COALESCE(is_duplicate, FALSE) = FALSE
          AND lat IS NULL
          AND (address IS NOT NULL AND btrim(address) != '')
        ORDER BY first_seen DESC
        LIMIT $1
    """, batch_size)

    geocoded = 0
    failed = 0
    no_house_number = 0
    for i, r in enumerate(rows):
        if i % 25 == 0:
            await _report(progress_cb, f"geocode… {i}/{len(rows)}")
        raw_query = r["address"] or r["district"]
        if not raw_query:
            continue
        query = _clean_address_for_geocode(raw_query)
        # БАГ (найден на живых данных, объявление 1013672419): улица без
        # номера дома ("Момышулы" без "12") — Nominatim в этом случае не
        # признаёт "не найдено", а подбирает ЛЮБУЮ точку с этим названием
        # (может быть другой конец улицы в несколько км, парк/сквер с тем же
        # именем и т.п.) — координата выглядит точной, а на деле произвольная.
        # Без номера дома честнее не гадать вообще: оставляем lat/lun NULL,
        # объявление корректно всплывает на /admin/unbound вместо неверного
        # пина на главной карте.
        if not re.search(r"\d", query):
            no_house_number += 1
            failed += 1
            continue
        coords = await geocode(query, city=city)
        if not coords and query != raw_query:
            coords = await geocode(raw_query, city=city)  # запасной вариант — вдруг чистка отрезала нужное
        # Sanity-проверка на пределы Астаны — без неё geocode() на мусорном
        # адресе ("А 105 23" и т.п.) иногда возвращает совпадение за сотни км
        # (Nominatim подбирает "похожий" объект где угодно в Казахстане, а
        # city=astana — это только подсказка, не жёсткое ограничение). Найдено
        # на живых данных: 2 объявления с district="Сарайшык р-н" получили
        # координаты у Актобе/Костаная (lat≈50.27, lon≈57.19) и потом
        # попадали в "похожие варианты" совсем в другом городе.
        if coords and not (50.0 < coords[0] < 53.0 and 69.0 < coords[1] < 73.0):
            logger.warning("geocode: отбросили результат вне Астаны для %s: %s", r["id"], coords)
            coords = None
        if coords:
            await pg_exec(
                "UPDATE apartment_listings SET lat = $2, lon = $3, "
                "geo_source = 'geocode' WHERE id = $1",
                r["id"], coords[0], coords[1])
            geocoded += 1
        else:
            failed += 1

    logger.info("geocode fallback: %d/%d успешно, %d не найдено (из них %d без номера дома)",
                geocoded, len(rows), failed, no_house_number)
    return {"attempted": len(rows), "geocoded": geocoded, "failed": failed,
            "no_house_number": no_house_number}
