#!/usr/bin/env python3
"""
Сервис парсинга КВАРТИР НА ПРОДАЖУ.
Каждые 10-20 минут:
  1. Парсит krisha.kz/prodazha/kvartiry/astana/
  2. Считает скор (yield из rental_index, bargain из аналогов)
  3. Сохраняет/обновляет apartment_listings в PostgreSQL
  4. Синкает в Google Sheets (вкладка Квартиры)

Запуск:  python service_apartments.py
Логи:    apartments.log
"""
import asyncio
import json
from datetime import datetime, timezone
import logging
import os
import random

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("apartments.log", encoding="utf-8", errors="replace"),
    ],
)
log = logging.getLogger("apartment_service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def run_cycle():
    from bot.core.apartment_parser import analyze_apartments
    from bot.core.sheets_sync import sync_apartments_to_sheets_pg
    from bot.db.pg import execute as pg_exec, fetchrow as pg_get
    from bot.db import settings as app_settings

    log.info("=== Apartment cycle start ===")

    # Подтягиваем настройки, выставленные через веб-терминал (/admin/settings)
    await app_settings.load()
    max_pages = app_settings.get_int("PARSER_MAX_PAGES", 5)

    # ── Свежие объявления: первые страницы выдачи (как раньше) ────────────
    results = await analyze_apartments("astana", max_pages=max_pages)
    log.info("Parsed %d fresh listings (pages 1-%d)", len(results), max_pages)

    # ── ГЛУБОКИЙ ОБХОД: идём до конца выдачи, запоминая позицию ───────────
    # Крыша по Астане ≤80млн — это ~100-200 страниц. Свежий парс покрывает
    # только первые 5, дальше живут объявления, которые никто не «поднимает»
    # — они никогда не попадут в базу без сквозного обхода. Каждый цикл
    # дочитываем DEEP_SWEEP_BATCH страниц с сохранённой позиции; дойдя до
    # конца выдачи (пустая страница) — начинаем заново с 6-й. Полный круг
    # при 5 стр/цикл и ~70-100 циклах в сутки занимает меньше суток,
    # при этом нагрузка на Крышу не растёт скачком.
    # Реальный размер выдачи с Крыши -> детерминированный конец круга.
    # (Прежний детектор "в батче нет новых id" после первого круга ломался:
    # известные страницы попадаются уже в начале, курсор вечно сбрасывался
    # и глубокие страницы не перечитывались.)
    from bot.core import apartment_parser as _ap
    if _ap.LAST_TOTAL_FOUND:
        await app_settings.set("KRISHA_TOTAL_FOUND", str(_ap.LAST_TOTAL_FOUND))
    krisha_total = app_settings.get_int("KRISHA_TOTAL_FOUND", 0)
    max_deep_page = (krisha_total // 20 + 2) if krisha_total else 0

    deep_batch = app_settings.get_int("DEEP_SWEEP_BATCH", 5)
    if deep_batch > 0:
        cursor = app_settings.get_int("DEEP_SWEEP_PAGE", max_pages + 1)
        if cursor <= max_pages:
            cursor = max_pages + 1
        try:
            deep_results = await analyze_apartments(
                "astana", max_pages=deep_batch, start_page=cursor)
            # Крыша на несуществующие страницы отдаёт последнюю (НЕ пустую!),
            # поэтому "пустая страница" как признак конца не работает — курсор
            # улетал на страницу 900+. Новый детект: если в батче нет ни
            # одного объявления, которого ещё нет в БД, — выдача исчерпана.
            new_ids = 0
            if deep_results:
                from bot.db.pg import fetchval as _pg_fv
                for _r in deep_results:
                    known = await _pg_fv(
                        "SELECT 1 FROM apartment_listings WHERE id=$1", _r["id"])
                    if not known:
                        new_ids += 1
            past_end = max_deep_page and cursor > max_deep_page
            if deep_results and (new_ids > 0 or not past_end) and not past_end:
                results.extend(deep_results)
                next_cursor = cursor + deep_batch
                log.info("Deep sweep: pages %d-%d → %d listings (%d новых), cursor → %d",
                         cursor, cursor + deep_batch - 1, len(deep_results),
                         new_ids, next_cursor)
            else:
                results.extend(deep_results or [])
                next_cursor = max_pages + 1
                log.info("Deep sweep: страница %d (последняя ~%d по счётчику "
                         "Крыши) — круг завершён, cursor → %d", cursor,
                         max_deep_page, next_cursor)
            await app_settings.set("DEEP_SWEEP_PAGE", str(next_cursor))
            await app_settings.set("DEEP_SWEEP_LAST_AT",
                                   datetime.now(timezone.utc).isoformat())
        except Exception as e:
            log.warning("Deep sweep failed (продолжаем со свежими): %s", e)

    log.info("Parsed %d listings total", len(results))

    # ── Отсев «ЖК-улиц»: названия, помеченные аудитом как улицы, не принимаем ──
    try:
        from bot.core.complex_audit import street_names as _street_names
        import re as _re_s
        _streets = await _street_names()
        from bot.core.complex_audit import _JUNK_NAMES
        _streets |= set(_JUNK_NAMES)
        if _streets:
            def _norm_s(s):
                s = (s or "").lower()
                s = _re_s.sub(r"^\s*(жк|кг)\.?\s+", "", s)
                s = _re_s.sub(r"[«»\"'()]", " ", s)
                return _re_s.sub(r"\s+", " ", s).strip()
            cleared = 0
            for r in results:
                if r.get("complex_name"):
                    _n = _norm_s(r["complex_name"])
                    if _n in _streets or len(_n) < 4:
                        r["complex_name"] = None
                        r["complex_url"] = None
                        cleared += 1
            if cleared:
                log.info("street filter: убрано %d привязок к ЖК-улицам", cleared)
    except Exception as e:
        log.warning("street filter failed: %s", e)

    new_cnt = upd_cnt = 0

    for r in results:
        sd = r.get("score_data", {})
        bd = sd.get("breakdown", {})
        bargain = sd.get("bargain", {})
        reasons_json = json.dumps(sd.get("reasons", []), ensure_ascii=False)

        exists = await pg_get("SELECT id, price FROM apartment_listings WHERE id=$1", r["id"])

        # История изменения цены (для статистики "цена выросла/упала")
        if exists and r.get("price") and exists["price"] and exists["price"] != r["price"]:
            try:
                await pg_exec(
                    "INSERT INTO price_history (listing_id, old_price, new_price) VALUES ($1,$2,$3)",
                    r["id"], exists["price"], r["price"])
            except Exception as e:
                log.warning("price_history failed %s: %s", r["id"], e)

        try:
            if not exists:
                await pg_exec("""
                    INSERT INTO apartment_listings
                        (id, url, title, price, area, rooms, address, district, complex_name,
                         est_rent, yield_pct, payback_years,
                         score_total, score_yield, score_price_market, score_location,
                         score_apt_type, score_floor, score_complex, score_supply,
                         reasons, description, floor, floors_total,
                         year_built, building_type, renovation, furniture,
                         is_new_build, developer_name, seller_type, is_owner,
                         rent_source, bargain_target, bargain_discount_pct, bargain_rec,
                         details_fetched, first_seen, last_seen, notified)
                    VALUES
                        ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
                         $13,$14,$15,$16,$17,$18,$19,$20,
                         $21,$22,$23,$24,$25,$26,$27,$28,$29,$30,
                         $31,$32,$33,$34,$35,$36,$37,NOW(),NOW(),FALSE)
                    ON CONFLICT (id) DO NOTHING
                """,
                    r["id"], r.get("url"), r.get("title"), r.get("price"), r.get("area"),
                    r.get("rooms"), r.get("address"), r.get("district"), r.get("complex_name"),
                    r.get("est_rent", 0), r.get("yield_pct", 0), r.get("payback_years"),
                    sd.get("total_score", 0), bd.get("yield", 0), bd.get("price_market", 0),
                    bd.get("location", 0), bd.get("apt_type", 0), bd.get("floor", 0),
                    bd.get("complex", 0), bd.get("supply", 0),
                    reasons_json, r.get("description", ""),
                    r.get("floor"), r.get("floors_total"),
                    r.get("year_built"), r.get("building_type"),
                    r.get("renovation"), r.get("furniture"),
                    r.get("is_new_build", False), r.get("developer_name"),
                    r.get("seller_type"), r.get("is_owner"),
                    r.get("rent_source"), bargain.get("target_price"),
                    bargain.get("discount_pct"), bargain.get("recommendation"),
                    r.get("details_fetched", False),
                )
                new_cnt += 1
            else:
                # История цен: фиксируем изменение — сигнал для алертов о торге
                old_price = exists["price"]
                new_price = r.get("price")
                if new_price and old_price and new_price != old_price:
                    try:
                        await pg_exec(
                            "INSERT INTO price_history (listing_id, old_price, new_price) "
                            "VALUES ($1, $2, $3)",
                            r["id"], old_price, new_price,
                        )
                        log.info("price change %s: %s -> %s", r["id"], old_price, new_price)
                    except Exception as e:
                        log.warning("price_history insert failed %s: %s", r["id"], e)
                await pg_exec("""
                    UPDATE apartment_listings SET
                        price=$2, est_rent=$3, yield_pct=$4, payback_years=$5,
                        score_total=$6, score_yield=$7, score_price_market=$8,
                        score_location=$9, score_apt_type=$10, score_floor=$11,
                        score_complex=$12, score_supply=$13, reasons=$14,
                        floor=$15, floors_total=$16, year_built=$17,
                        complex_name=$18, seller_type=$19, is_owner=$20,
                        rent_source=$21, bargain_target=$22,
                        bargain_discount_pct=$23, bargain_rec=$24,
                        details_fetched=$25, last_seen=NOW()
                    WHERE id=$1
                """,
                    r["id"], r.get("price"), r.get("est_rent", 0),
                    r.get("yield_pct", 0), r.get("payback_years"),
                    sd.get("total_score", 0), bd.get("yield", 0), bd.get("price_market", 0),
                    bd.get("location", 0), bd.get("apt_type", 0), bd.get("floor", 0),
                    bd.get("complex", 0), bd.get("supply", 0), reasons_json,
                    r.get("floor"), r.get("floors_total"), r.get("year_built"),
                    r.get("complex_name"), r.get("seller_type"), r.get("is_owner"),
                    r.get("rent_source"), bargain.get("target_price"),
                    bargain.get("discount_pct"), bargain.get("recommendation"),
                    r.get("details_fetched", False),
                )
                upd_cnt += 1
        except Exception as e:
            log.warning("DB error %s: %s", r["id"], e)

    log.info("DB: +%d new, ~%d updated", new_cnt, upd_cnt)

    # ── Отделка: определяем по тексту, правим скор ────────────────────────
    from bot.core.listing_intel import detect_finish_level
    for r in results:
        code, adj, label = detect_finish_level(r.get("title"), r.get("description"))
        if code:
            try:
                await pg_exec(
                    "UPDATE apartment_listings SET finish_level=$2, "
                    "score_total = LEAST(100, GREATEST(0, COALESCE(score_total,0) + $3)) "
                    "WHERE id=$1 AND (finish_level IS DISTINCT FROM $2)",
                    r["id"], code, adj,
                )
            except Exception as e:
                log.warning("finish update failed %s: %s", r["id"], e)

    # ── Координаты + свежепойманная архивность из детального парсера ──────
    try:
        await pg_exec("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS complex_url TEXT")
    except Exception:
        pass
    for r in results:
        try:
            if r.get("lat") and r.get("lon"):
                await pg_exec(
                    "UPDATE apartment_listings SET lat=$2, lon=$3, geo_source='krisha' WHERE id=$1",
                    r["id"], r["lat"], r["lon"],
                )
            if r.get("complex_url"):
                await pg_exec(
                    "UPDATE apartment_listings SET complex_url=$2 WHERE id=$1",
                    r["id"], r["complex_url"],
                )
            if r.get("photos"):
                await pg_exec(
                    "UPDATE apartment_listings SET photos=$2::jsonb WHERE id=$1",
                    r["id"], json.dumps(r["photos"]),
                )
            if r.get("is_archived"):
                await pg_exec(
                    "UPDATE apartment_listings SET is_active=FALSE, archived_at=now() WHERE id=$1",
                    r["id"],
                )
        except Exception as e:
            log.warning("coords/archive update failed %s: %s", r["id"], e)

    # ── БЭКФИЛЛ координат по ВСЕЙ базе (не только текущий цикл) ────────────
    # Баг, который это чинит: раньше детали докачивались только для
    # объявлений, увиденных в ЭТОМ цикле парсинга страниц. Объявление,
    # вставленное неделю назад и не попавшее тогда в случайную выборку на
    # докачку, больше никогда не получало координат — отсюда целые районы
    # без единой точки на карте, сколько бы времени ни прошло. Этот блок
    # работает НАПРЯМУЮ с БД: берёт любые активные объявления без lat/lon,
    # независимо от того, встретились ли они в сегодняшних страницах.
    try:
        from bot.db import settings as _app_settings2
        backfill_batch = _app_settings2.get_int("COORD_BACKFILL_BATCH", 15)
        if backfill_batch > 0:
            from bot.db.pg import fetch as _pg_fetch_bf
            from bot.core.apartment_details import fetch_apartment_details as _fetch_details_bf

            # Колонки для жёсткой привязки к ЖК: ссылка на карточку ЖК с Крыши
            await pg_exec("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS complex_url TEXT")
            await pg_exec("ALTER TABLE complexes ADD COLUMN IF NOT EXISTS krisha_url TEXT")

            # Берём объявления, у которых нет координат ИЛИ нет привязки к ЖК —
            # с детальной страницы достаём и то, и другое (там официальный
            # блок «Жилой комплекс» со ссылкой + JS-блоб с lat/lon).
            missing = await _pg_fetch_bf("""
                SELECT id, url FROM apartment_listings
                WHERE (lat IS NULL OR complex_name IS NULL OR btrim(complex_name) = '')
                  AND is_active IS NOT FALSE AND url IS NOT NULL
                  AND (coord_fetch_attempted_at IS NULL
                       OR coord_fetch_attempted_at < now() - interval '3 days')
                ORDER BY coord_fetch_attempted_at ASC NULLS FIRST, first_seen ASC
                LIMIT $1
            """, backfill_batch)

            got = got_cx = 0
            for m in missing:
                await asyncio.sleep(random.uniform(8.0, 15.0))
                try:
                    details = await _fetch_details_bf(m["url"])
                except Exception as e:
                    log.warning("backfill fetch failed %s: %s", m["id"], e)
                    details = None
                if details and (details.get("lat") or details.get("complex_name")):
                    if details.get("lat") and details.get("lon"):
                        await pg_exec(
                            "UPDATE apartment_listings SET lat=$2, lon=$3, geo_source='krisha', "
                            "coord_fetch_attempted_at=now() WHERE id=$1",
                            m["id"], details["lat"], details["lon"],
                        )
                        got += 1
                    else:
                        await pg_exec(
                            "UPDATE apartment_listings SET coord_fetch_attempted_at=now() WHERE id=$1",
                            m["id"],
                        )
                    if details.get("complex_name"):
                        # не принимаем названия, помеченные аудитом как улицы
                        try:
                            from bot.core.complex_audit import street_names as _sn2
                            import re as _re_b
                            _st2 = await _sn2()
                            _n = (details["complex_name"] or "").lower()
                            _n = _re_b.sub(r"^\s*(жк|кг)\.?\s+", "", _n)
                            _n = _re_b.sub(r"[«»\"'()]", " ", _n)
                            _n = _re_b.sub(r"\s+", " ", _n).strip()
                            from bot.core.complex_audit import _JUNK_NAMES
                            if _n in _st2 or _n in _JUNK_NAMES or len(_n) < 4:
                                details.pop("complex_name", None)
                                details.pop("complex_url", None)
                        except Exception:
                            pass
                    if details.get("complex_name"):
                        await pg_exec(
                            "UPDATE apartment_listings SET complex_name=$2, "
                            "complex_url=COALESCE($3, complex_url) WHERE id=$1",
                            m["id"], details["complex_name"],
                            details.get("complex_url"))
                        got_cx += 1
                        # Канонический ЖК в справочнике: ссылка на карточку
                        # Крыши — надёжный ключ для склейки названий
                        if details.get("complex_url"):
                            await pg_exec("""
                                INSERT INTO complexes (name, krisha_url)
                                SELECT $1, $2
                                WHERE NOT EXISTS (
                                    SELECT 1 FROM complexes
                                    WHERE lower(name) = lower($1) OR krisha_url = $2)
                            """, details["complex_name"], details["complex_url"])
                    if details.get("photos"):
                        await pg_exec(
                            "UPDATE apartment_listings SET photos=$2::jsonb WHERE id=$1",
                            m["id"], json.dumps(details["photos"]))
                else:
                    # Не нашли ни координат, ни ЖК — отметим попытку, чтобы
                    # не долбить это же объявление каждый цикл; повторим через 3 дня.
                    await pg_exec(
                        "UPDATE apartment_listings SET coord_fetch_attempted_at=now() WHERE id=$1",
                        m["id"],
                    )
            if missing:
                log.info("coord/complex backfill: координаты %d/%d, ЖК %d (батч %d)",
                         got, len(missing), got_cx, backfill_batch)
    except Exception as e:
        log.warning("coord backfill failed: %s", e)

    # ── Гео по адресу: объявления без координат ставим на карту по адресу ──
    # Если у объявления нет координат с Крыши, но такой же адрес встречается
    # у других объявлений С координатами — берём центроид этих координат.
    # Помечаем geo_source='address' (приблизительная привязка, видно в попапе).
    # В качестве источника используем только "настоящие" координаты
    # (geo_source != 'address'), чтобы не было самоподкрепления.
    try:
        await pg_exec("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS geo_source TEXT")
        geo_cnt = await pg_exec("""
            UPDATE apartment_listings t
            SET lat = s.lat, lon = s.lon, geo_source = 'address'
            FROM (
                SELECT lower(btrim(address)) AS addr, AVG(lat) AS lat, AVG(lon) AS lon
                FROM apartment_listings
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  AND address IS NOT NULL AND btrim(address) <> ''
                  AND COALESCE(geo_source, '') <> 'address'
                GROUP BY 1
            ) s
            WHERE t.lat IS NULL
              AND t.address IS NOT NULL
              AND lower(btrim(t.address)) = s.addr
        """)
        try:
            n = int(str(geo_cnt).split()[-1])
        except (ValueError, IndexError):
            n = 0
        if n:
            log.info("geo by address: привязано %d объявлений по адресу", n)
    except Exception as e:
        log.warning("geo-by-address failed: %s", e)

    # ── Зоны приоритета: пересчёт бонусов для всех объявлений с координатами ──
    try:
        from bot.core.zones import load_zones, zone_bonus_for
        zones = await load_zones()
        if zones:
            from bot.db.pg import fetch as pg_fetch
            coords = await pg_fetch(
                "SELECT id, lat, lon, COALESCE(zone_bonus,0) AS zb FROM apartment_listings "
                "WHERE lat IS NOT NULL AND lon IS NOT NULL"
            )
            zcnt = 0
            for c in coords:
                bonus, zname = zone_bonus_for(c["lat"], c["lon"], zones)
                if zname is None:
                    # Вне всех зон: штраф -20 (фокус стратегии — зонные локации).
                    # Применяется только когда зоны нарисованы; без координат
                    # объявление не наказываем (данных нет — не вина объекта).
                    bonus = -20
                if bonus != c["zb"]:
                    await pg_exec(
                        "UPDATE apartment_listings SET zone_bonus=$2, zone_name=$3 WHERE id=$1",
                        c["id"], bonus, zname,
                    )
                    zcnt += 1
            if zcnt:
                log.info("zones: updated bonus for %d listings", zcnt)
    except Exception as e:
        log.warning("zone recompute failed: %s", e)

    # ── Первичка/вторичка ─────────────────────────────────────────────────
    # Правило: ВСЁ, что в стройке — первичка. Признаки:
    #   • год постройки >= текущего (дом ещё не сдан / сдаётся)
    #   • "от застройщика", "сдача в <будущий год/квартал>"
    #   • явные маркеры стадии: "на этапе строительства", "котлован", "строится"
    import re as _re_mt
    from datetime import date as _date_mt
    _cur_year = _date_mt.today().year

    def _detect_primary(title: str, desc: str, year) -> bool:
        blob = f"{title} {desc}".lower()
        if year and year >= _cur_year:
            return True
        if "от застройщика" in blob:
            return True
        # "сдача в 2026", "сдача в 4 кв. 2027", "срок сдачи 2026"
        if _re_mt.search(r"(?:срок\s+)?сдач[аи]\s+(?:в\s+)?(?:\d\s*кв\.?\s*)?20(?:2[6-9]|3\d)", blob):
            return True
        if _re_mt.search(r"на\s+этапе\s+строительств|стади[яи]\s+строительств|котлован|дом\s+строится", blob):
            return True
        return False

    for r in results:
        is_primary = _detect_primary(r.get("title", "") or "", r.get("description", "") or "",
                                     r.get("year_built"))
        try:
            # NULL → выставляем всегда; secondary → повышаем до primary, если
            # появились явные признаки стройки (обратно primary→secondary не
            # понижаем автоматически — сданный дом уточняется вручную/годом)
            await pg_exec(
                "UPDATE apartment_listings SET market_type=$2 WHERE id=$1 "
                "AND (market_type IS NULL OR (market_type='secondary' AND $2='primary'))",
                r["id"], "primary" if is_primary else "secondary",
            )
        except Exception as e:
            log.warning("market_type failed %s: %s", r["id"], e)

    # ── Отдельная модель скоринга ПЕРВИЧКИ ─────────────────────────────────
    # (developer + стадия + дисконт к вторичке + локация — вместо базовой
    # модели, где 20% веса это фиктивный для первички yield)
    try:
        from bot.core.primary_score import compute_primary_score
        from bot.db.pg import fetch as pg_fetch3

        primary_rows = await pg_fetch3("""
            SELECT id, title, description, year_built, price, area, district,
                   complex_name
            FROM apartment_listings
            WHERE market_type = 'primary' AND is_active IS NOT FALSE
              AND (primary_score_total IS NULL
                   OR last_seen > now() - interval '1 day')
            ORDER BY last_seen DESC NULLS LAST
            LIMIT 30
        """)
        for r in primary_rows:
            r = dict(r)
            developer = None
            cx = await pg_fetch3(
                "SELECT source_info FROM complexes WHERE lower(trim(name)) = lower(trim($1)) LIMIT 1",
                r.get("complex_name") or "",
            ) if r.get("complex_name") else []
            if cx and cx[0]["source_info"]:
                si = cx[0]["source_info"]
                if isinstance(si, str):
                    si = json.loads(si)
                developer = (si.get("korter") or si.get("homsters") or {}).get("developer")

            secondary_median = None
            if r.get("district") and r.get("area"):
                med_row = await pg_fetch3("""
                    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY price/NULLIF(area,0)) AS m
                    FROM apartment_listings
                    WHERE market_type = 'secondary' AND district = $1
                      AND area BETWEEN $2 * 0.7 AND $2 * 1.3
                      AND COALESCE(is_duplicate, FALSE) = FALSE AND price > 500000
                """, r["district"], r["area"])
                secondary_median = med_row[0]["m"] if med_row else None

            score, details = compute_primary_score(r, developer, secondary_median)
            await pg_exec(
                "UPDATE apartment_listings SET primary_score_total=$2, "
                "primary_score_details=$3::jsonb, score_total=$2 WHERE id=$1",
                r["id"], score, json.dumps(details, ensure_ascii=False),
            )
        if primary_rows:
            log.info("primary score: computed for %d listings", len(primary_rows))
    except Exception as e:
        log.warning("primary score failed: %s", e)

    # ── Слои скоринга (шум, школы — OSM с кешем) ──────────────────────────
    if app_settings.get_bool("OSM_LAYERS", True):
        try:
            from bot.score_layers import compute_all_layers, details_to_json
            from bot.db.pg import fetch as pg_fetch2
            candidates = await pg_fetch2("""
                SELECT id, lat, lon FROM apartment_listings
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  AND is_active IS NOT FALSE
                  AND (layers_computed_at IS NULL
                       OR layers_computed_at < now() - interval '30 days')
                ORDER BY score_total DESC NULLS LAST
                LIMIT 10
            """)
            for c in candidates:
                adj, details = await compute_all_layers(dict(c))
                await pg_exec(
                    "UPDATE apartment_listings SET layer_bonus=$2, "
                    "layer_details=$3::jsonb, layers_computed_at=now() WHERE id=$1",
                    c["id"], adj, details_to_json(details),
                )
                await asyncio.sleep(1.5)  # вежливость к Overpass
            if candidates:
                log.info("layers: computed for %d listings", len(candidates))
        except Exception as e:
            log.warning("layers failed: %s", e)

    # ── AI-анализ текста (DeepSeek, включается настройкой) ────────────────
    if app_settings.get_bool("AI_TEXT_ANALYSIS", False):
        try:
            from bot.core.ai_text_analysis import analyze_top_listings
            await analyze_top_listings(limit=10)
        except Exception as e:
            log.warning("ai text analysis failed: %s", e)

    # ── Живая статистика ЖК: активные в продаже / продано / аренда ────────
    # (раньше listings_count был заморожен со времён миграции — отсюда
    # "рейтинг 1" у большинства ЖК; теперь пересчитывается каждый цикл)
    try:
        await pg_exec("""
            UPDATE complexes c SET
                listings_count = s.active_cnt,
                sold_count     = s.sold_cnt,
                avg_price_m2   = s.avg_m2
            FROM (
                SELECT complex_name,
                       COUNT(*) FILTER (WHERE is_active IS NOT FALSE)             AS active_cnt,
                       COUNT(*) FILTER (WHERE is_active IS FALSE)                 AS sold_cnt,
                       AVG(price / NULLIF(area,0)) FILTER (WHERE is_active IS NOT FALSE) AS avg_m2
                FROM apartment_listings
                WHERE complex_name IS NOT NULL AND complex_name != ''
                  AND COALESCE(is_duplicate, FALSE) = FALSE
                GROUP BY complex_name
            ) s
            WHERE lower(c.name) = lower(s.complex_name)
        """)
        await pg_exec("""
            UPDATE complexes c SET rental_listings_count = s.cnt
            FROM (
                SELECT complex_name, COUNT(*) AS cnt FROM rental_listings
                WHERE complex_name IS NOT NULL AND complex_name != ''
                  AND last_seen > now() - interval '14 days'
                GROUP BY complex_name
            ) s
            WHERE lower(c.name) = lower(s.complex_name)
        """)
        # Координаты ЖК из центроидов объявлений (только пустые)
        await pg_exec("""
            UPDATE complexes c SET lat = s.lat, lon = s.lon, coords_source = 'listings'
            FROM (
                SELECT lower(trim(regexp_replace(complex_name, '^\\s*(жк|кг)\\.?\\s+', '', 'i'))) AS n,
                       AVG(lat) AS lat, AVG(lon) AS lon
                FROM apartment_listings
                WHERE lat IS NOT NULL AND complex_name IS NOT NULL AND complex_name != ''
                GROUP BY 1
            ) s
            WHERE c.lat IS NULL
              AND lower(trim(regexp_replace(c.name, '^\\s*(жк|кг)\\.?\\s+', '', 'i'))) = s.n
        """)

        # Фото ЖК = первое фото любого его объявления
        await pg_exec("""
            UPDATE complexes c SET photo_url = s.photo
            FROM (
                SELECT DISTINCT ON (lower(trim(complex_name)))
                       lower(trim(complex_name)) AS cname,
                       photos->>0 AS photo
                FROM apartment_listings
                WHERE photos IS NOT NULL AND complex_name IS NOT NULL
            ) s
            WHERE lower(trim(c.name)) = s.cname AND c.photo_url IS NULL
        """)
        # Новые ЖК, которых ещё нет в справочнике — создаём
        await pg_exec("""
            INSERT INTO complexes (name, district, listings_count)
            SELECT al.complex_name, MAX(al.district),
                   COUNT(*) FILTER (WHERE al.is_active IS NOT FALSE)
            FROM apartment_listings al
            WHERE al.complex_name IS NOT NULL AND al.complex_name != ''
              AND NOT EXISTS (SELECT 1 FROM complexes c WHERE lower(c.name) = lower(al.complex_name))
            GROUP BY al.complex_name
        """)
    except Exception as e:
        log.warning("complex stats refresh failed: %s", e)

    # ── Проверка архивности топовых объявлений (максимум 15 за цикл) ──────
    try:
        from bot.core.archive_check import check_archived
        res = await check_archived(limit=15)
        log.info("archive check: %s", res)
    except Exception as e:
        log.warning("archive check failed: %s", e)

    # Google Sheets sync
    try:
        # === Гексагональный анализ цены (микролокальный Deal Index) ===
        try:
            from bot.core.deal_score import apply_deal_scores
            await apply_deal_scores()
        except Exception as e:
            log.warning("hex price failed: %s", e)

        # === Дедупликация (приоритет объявлений от хозяина) ===
        try:
            from bot.core.dedup_listings import deduplicate_apartment_listings
            dup_count = await deduplicate_apartment_listings()
            if dup_count:
                log.info("Deduplicated %d apartment listings", dup_count)
        except Exception as e:
            log.warning("Apartment deduplication failed: %s", e)

        await sync_apartments_to_sheets_pg()
        log.info("Google Sheets: Квартиры synced")
        from datetime import datetime, timezone
        await app_settings.set("SHEETS_APARTMENTS_SYNCED_AT",
                               datetime.now(timezone.utc).isoformat())
    except Exception as e:
        log.warning("Sheets sync failed: %s", e)

    log.info("=== Apartment cycle done ===\n")


async def main():
    from bot.db.pg import init_pool
    await init_pool(DATABASE_URL)
    log.info("=== Apartment service started ===")

    # Первый цикл сразу
    try:
        await run_cycle()
    except Exception as e:
        log.error("First cycle error: %s", e, exc_info=True)

    while True:
        sleep_min = random.uniform(50, 80)  # ~раз в час, рандом
        log.info("Next cycle in %.0f min...", sleep_min)
        await asyncio.sleep(sleep_min * 60)
        try:
            await run_cycle()
        except Exception as e:
            log.error("Cycle error: %s", e, exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
