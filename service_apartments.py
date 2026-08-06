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
                # Метрика "за сколько мы обходим всю Крышу": засекаем момент
                # завершения полного круга глубокого обхода и считаем дельту
                # с предыдущим завершением — это и есть время полного обхода.
                now_iso = datetime.now(timezone.utc).isoformat()
                prev_completed = app_settings.get("DEEP_SWEEP_CIRCLE_COMPLETED_AT")
                if prev_completed:
                    try:
                        prev_dt = datetime.fromisoformat(prev_completed)
                        duration_sec = (datetime.now(timezone.utc) - prev_dt).total_seconds()
                        await app_settings.set("DEEP_SWEEP_CIRCLE_DURATION_SEC", str(int(duration_sec)))
                    except Exception as e:
                        log.warning("circle duration calc failed: %s", e)
                await app_settings.set("DEEP_SWEEP_CIRCLE_COMPLETED_AT", now_iso)
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

        try:
            if not exists:
                await pg_exec("""
                    INSERT INTO apartment_listings
                        (id, url, title, price, area, rooms, address, district, complex_name,
                         est_rent, yield_pct, net_yield_pct, payback_years,
                         score_total, score_yield, score_price_market, score_location,
                         score_apt_type, score_floor, score_complex, score_supply,
                         reasons, description, floor, floors_total,
                         year_built, building_type, renovation, furniture,
                         is_new_build, developer_name, seller_type, is_owner,
                         rent_source, bargain_target, bargain_discount_pct, bargain_rec,
                         details_fetched, ceiling_height, kitchen_area, first_seen, last_seen, notified)
                    VALUES
                        ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,
                         $14,$15,$16,$17,$18,$19,$20,$21,
                         $22,$23,$24,$25,$26,$27,$28,$29,$30,$31,
                         $32,$33,$34,$35,$36,$37,$38,$39,$40,NOW(),NOW(),FALSE)
                    ON CONFLICT (id) DO NOTHING
                """,
                    r["id"], r.get("url"), r.get("title"), r.get("price"), r.get("area"),
                    r.get("rooms"), r.get("address"), r.get("district"), r.get("complex_name"),
                    r.get("est_rent", 0), r.get("yield_pct", 0), r.get("net_yield_pct", 0), r.get("payback_years"),
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
                    r.get("details_fetched", False), r.get("ceiling_height"),
                    r.get("kitchen_area"),
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
                    if new_price < old_price:
                        # Мотивированный продавец: 1-е снижение без бонуса,
                        # 2-е +5, 3-е +10 и т.д. — прибавляем к скору.
                        try:
                            from bot.db.pg import fetchval as _pg_fv2
                            drops = await _pg_fv2(
                                "SELECT COUNT(*) FROM price_history "
                                "WHERE listing_id=$1 AND new_price < old_price",
                                r["id"])
                            bonus = max(0, (int(drops or 0) - 1) * 5)
                            await pg_exec(
                                "UPDATE apartment_listings SET price_drop_bonus=$2 WHERE id=$1",
                                r["id"], bonus)
                        except Exception as e:
                            log.warning("price_drop_bonus update failed %s: %s", r["id"], e)
                # score_total и breakdown НЕ трогаем здесь — реальный скор
                # (Deal Score v3) считается отдельно в deal_score.apply_deal_scores()
                # для всей базы; перезапись их плейсхолдером 0 на каждый re-parse
                # обнуляла бы уже посчитанный скор до следующего прохода v3.
                # ВАЖНО: title/rooms/area/address/district тоже обновляем —
                # продавец может отредактировать объявление (сменить площадь,
                # число комнат и т.п.), оставив тот же URL/ID. Раньше эти поля
                # писались только при первой вставке и потом никогда не
                # синхронизировались, из-за чего карточка навсегда застревала
                # на комнатности/площади с момента первого скана (баг с
                # расхождением комнатности между Крышей и нашей аналитикой).
                await pg_exec("""
                    UPDATE apartment_listings SET
                        price=$2, est_rent=$3, yield_pct=$4, payback_years=$5,
                        reasons=$6,
                        floor=$7, floors_total=$8, year_built=$9,
                        complex_name=$10, seller_type=$11, is_owner=$12,
                        rent_source=$13, bargain_target=$14,
                        bargain_discount_pct=$15, bargain_rec=$16,
                        details_fetched=$17, ceiling_height=COALESCE($18, ceiling_height),
                        title=$19, rooms=$20, area=$21, address=$22, district=$23,
                        net_yield_pct=$24, kitchen_area=COALESCE($25, kitchen_area),
                        last_seen=NOW()
                    WHERE id=$1
                """,
                    r["id"], r.get("price"), r.get("est_rent", 0),
                    r.get("yield_pct", 0), r.get("payback_years"),
                    reasons_json,
                    r.get("floor"), r.get("floors_total"), r.get("year_built"),
                    r.get("complex_name"), r.get("seller_type"), r.get("is_owner"),
                    r.get("rent_source"), bargain.get("target_price"),
                    bargain.get("discount_pct"), bargain.get("recommendation"),
                    r.get("details_fetched", False), r.get("ceiling_height"),
                    r.get("title"), r.get("rooms"), r.get("area"),
                    r.get("address"), r.get("district"),
                    r.get("net_yield_pct", 0), r.get("kitchen_area"),
                )
                upd_cnt += 1

            if r.get("views_count"):
                # COALESCE — не затираем уже сохранённое число NULL'ом на
                # циклах без свежего detail-fetch (views_count там просто нет).
                await pg_exec(
                    "UPDATE apartment_listings SET views_count=$2 WHERE id=$1",
                    r["id"], r["views_count"])
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
                # Прогреваем кэш первого фото сразу при парсинге, а не по
                # факту первого открытия попапа пользователем — раньше это
                # давало 2-3с задержку на каждое ранее непросмотренное фото
                # (см. img-proxy/prewarm_photo_cache в terminal_extras.py).
                try:
                    from terminal_extras import prewarm_photo_cache
                    asyncio.create_task(prewarm_photo_cache(r["photos"][0]))
                except Exception:
                    pass
            if r.get("seller_name"):
                await pg_exec(
                    "UPDATE apartment_listings SET seller_name=$2 WHERE id=$1",
                    r["id"], r["seller_name"],
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
        backfill_batch = _app_settings2.get_int("COORD_BACKFILL_BATCH", 80)
        if backfill_batch > 0:
            from bot.core.coord_backfill import backfill_coords_and_complex
            res = await backfill_coords_and_complex(backfill_batch)
            if res["attempted"]:
                log.info("coord/complex backfill: координаты %d/%d, ЖК %d (батч %d)",
                         res["got_coords"], res["attempted"], res["got_complex"], backfill_batch)
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
                  -- Sanity-пределы Астаны: без этого один "отравленный" геокод
                  -- (координаты за сотни км от города — см. фикс в rebind.py)
                  -- усреднялся сюда и разъезжался по ВСЕМ объявлениям с тем
                  -- же текстом адреса, превращая один плохой геокод в десятки.
                  AND lat BETWEEN 50.0 AND 53.0 AND lon BETWEEN 69.0 AND 73.0
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
    # ВАЖНО: раньше это блок пропускался целиком, если zones пуст (все зоны
    # удалены) — из-за этого старые zone_bonus от УЖЕ УДАЛЁННЫХ зон навсегда
    # застревали на объявлениях и продолжали влиять на скор. Теперь строки
    # без активных зон явно обнуляются (bonus=0, zone_name=None) вместо
    # того чтобы просто не трогаться.
    try:
        from bot.core.zones import load_zones, zone_bonus_for
        zones = await load_zones()
        from bot.db.pg import fetch as pg_fetch
        coords = await pg_fetch(
            "SELECT id, lat, lon, COALESCE(zone_bonus,0) AS zb FROM apartment_listings "
            "WHERE lat IS NOT NULL AND lon IS NOT NULL"
        )
        zcnt = 0
        for c in coords:
            if zones:
                # Зоны только ПОВЫШАЮТ скор (или не делают ничего, если зона
                # сама отрицательная/анти-зона) — вне любых зон бонус 0,
                # никакого штрафа за то, что объявление просто не попало
                # в нарисованную область.
                bonus, zname = zone_bonus_for(c["lat"], c["lon"], zones)
            else:
                # Зоны не нарисованы / все удалены — бонус не применяется.
                bonus, zname = 0, None
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
            # LIMIT 10 (найдено при расследовании "у объявления в разборе
            # локации есть только распашонка, остальных слоёв нет"): при
            # десятках тысяч активных объявлений и цикле раз в 50-80 мин это
            # ~220/день — реально почти никто не успевает получить шум/школы/
            # транспорт/поблизости/парки/банки, layer_details застревает с
            # одной "layout" записью (её пишет отдельный, более быстрый AI-
            # проход). Клетки сетки Overpass кешируются на 60 дней
            # (osm_cache, ~110м клетка) — в плотной застройке Астаны большая
            # часть объявлений внутри уже прогретой клетки не бьёт по
            # внешнему API вообще, sleep(1.5) ниже — доминирующая стоимость
            # прохода, а не сам запрос. Подняли лимит вчетверо; в худшем
            # случае (все клетки холодные) это +60с к циклу — не критично на
            # фоне 50-80 минут между циклами.
            candidates = await pg_fetch2("""
                SELECT id, lat, lon FROM apartment_listings
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  AND is_active IS NOT FALSE
                  AND (layers_computed_at IS NULL
                       OR layers_computed_at < now() - interval '30 days')
                ORDER BY score_total DESC NULLS LAST
                LIMIT 40
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
    # Не трогает Крышу вообще (только DeepSeek, ~$0.0001/объявление) — лимит
    # держим щедрым, чтобы разобрать весь бэклог за разумное число дней, а
    # не годы (было 10/цикл ~= 240/день на 23к+ объявлений).
    if app_settings.get_bool("AI_TEXT_ANALYSIS", False):
        try:
            from bot.core.ai_text_analysis import analyze_top_listings
            await analyze_top_listings(limit=150)
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

    # ── Проверка архивности объявлений ─────────────────────────────────────
    # Было limit=15/цикл — при ~22k+ активных объявлений и цикле ~60-90 мин
    # это давало охват ~15-20/час, то есть полный проход по базе занимал бы
    # больше месяца. Подняли дефолт на порядок; настраивается в /admin/settings.
    try:
        from bot.core.archive_check import check_archived
        archive_batch = app_settings.get_int("ARCHIVE_CHECK_BATCH", 150)
        res = await check_archived(limit=archive_batch)
        log.info("archive check: %s", res)
    except Exception as e:
        log.warning("archive check failed: %s", e)

    # ── Снимок для графика "объявления без фото во времени" (/admin/analytics) ──
    try:
        from bot.db.pg import execute as _pg_exec3, fetchval as _pg_fv3
        _total_active = await _pg_fv3(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0
        _no_photo = await _pg_fv3(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND (photos IS NULL OR photos::text IN ('[]', 'null'))") or 0
        await _pg_exec3(
            "INSERT INTO no_photo_stats_history (total_active, no_photo) VALUES ($1, $2)",
            _total_active, _no_photo)
    except Exception as e:
        log.warning("no_photo snapshot failed: %s", e)

    try:
        from bot.db.pg import execute as _pg_exec4, fetchval as _pg_fv4
        _total_active2 = await _pg_fv4(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0
        _with_floor = await _pg_fv4(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND floor IS NOT NULL") or 0
        await _pg_exec4(
            "INSERT INTO floor_stats_history (total_active, with_floor) VALUES ($1, $2)",
            _total_active2, _with_floor)
    except Exception as e:
        log.warning("floor snapshot failed: %s", e)

    # Снимок для графика "покрытие данными о высоте потолка во времени"
    # (/admin/analytics/ceiling) — тот же принцип, что и floor_stats_history:
    # потолок тоже приходит только с детальной страницы объявления.
    try:
        from bot.db.pg import execute as _pg_exec_ceil, fetchval as _pg_fv_ceil
        _total_active3 = await _pg_fv_ceil(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0
        _with_ceiling = await _pg_fv_ceil(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND ceiling_height IS NOT NULL") or 0
        await _pg_exec_ceil(
            "INSERT INTO ceiling_stats_history (total_active, with_ceiling) VALUES ($1, $2)",
            _total_active3, _with_ceiling)
    except Exception as e:
        log.warning("ceiling snapshot failed: %s", e)

    # Снимок для графика "покрытие данными о годе постройки во времени"
    # (/admin/analytics/year) — год берём с самого объявления, а если там
    # пусто — с его ЖК (a.year_built чаще пусто, чем у самого ЖК, который
    # заполняется вручную/через Korter/Homsters).
    try:
        from bot.db.pg import execute as _pg_exec_year, fetchval as _pg_fv_year
        _total_active_y = await _pg_fv_year(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0
        _with_year = await _pg_fv_year("""
            SELECT COUNT(*) FROM apartment_listings a
            LEFT JOIN complexes c ON lower(trim(c.name)) = lower(trim(a.complex_name))
            WHERE a.is_active IS NOT FALSE
              AND COALESCE(a.year_built, c.year_built) IS NOT NULL
        """) or 0
        await _pg_exec_year(
            "INSERT INTO year_stats_history (total_active, with_year) VALUES ($1, $2)",
            _total_active_y, _with_year)
    except Exception as e:
        log.warning("year snapshot failed: %s", e)

    # Снимок просмотров по комнатности во времени (/admin/analytics/views) —
    # views_count накопительный с даты публикации, поэтому график по факту
    # показывает суммарный накопленный интерес по типам квартир на срезах
    # раз в цикл, а не прирост за конкретный день.
    try:
        from bot.db.pg import execute as _pg_exec5, fetchval as _pg_fv5
        _v1 = await _pg_fv5(
            "SELECT COALESCE(SUM(views_count),0) FROM apartment_listings "
            "WHERE is_active IS NOT FALSE AND rooms = 1") or 0
        _v2 = await _pg_fv5(
            "SELECT COALESCE(SUM(views_count),0) FROM apartment_listings "
            "WHERE is_active IS NOT FALSE AND rooms = 2") or 0
        _v3 = await _pg_fv5(
            "SELECT COALESCE(SUM(views_count),0) FROM apartment_listings "
            "WHERE is_active IS NOT FALSE AND rooms = 3") or 0
        _v4p = await _pg_fv5(
            "SELECT COALESCE(SUM(views_count),0) FROM apartment_listings "
            "WHERE is_active IS NOT FALSE AND rooms >= 4") or 0
        await _pg_exec5(
            "INSERT INTO views_stats_history (views_1, views_2, views_3, views_4p) VALUES ($1, $2, $3, $4)",
            _v1, _v2, _v3, _v4p)
    except Exception as e:
        log.warning("views snapshot failed: %s", e)

    # Снимок покрытия данными о просмотрах во времени (/admin/analytics/views)
    # — сколько активных объявлений вообще имеют известный views_count,
    # тот же принцип, что и ceiling_stats_history выше. views_count качает
    # отдельный медленный микросервис krisha-viewcount (Playwright).
    try:
        from bot.db.pg import execute as _pg_exec_vc, fetchval as _pg_fv_vc
        _total_active_vc = await _pg_fv_vc(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE") or 0
        _with_views = await _pg_fv_vc(
            "SELECT COUNT(*) FROM apartment_listings WHERE is_active IS NOT FALSE "
            "AND views_count IS NOT NULL") or 0
        await _pg_exec_vc(
            "INSERT INTO views_coverage_history (total_active, with_views) VALUES ($1, $2)",
            _total_active_vc, _with_views)
    except Exception as e:
        log.warning("views coverage snapshot failed: %s", e)

    # === Мониторинг сервера/проекта (CPU/память/диск/размер) — снимок раз
    # в цикл, см. /admin/settings ===
    try:
        from bot.core.system_stats import snapshot_system_stats
        await snapshot_system_stats()
    except Exception as e:
        log.warning("system stats snapshot failed: %s", e)

    # Google Sheets sync
    try:
        # === Гексагональный анализ цены (микролокальный Deal Index) ===
        try:
            from bot.core.deal_score import apply_deal_scores
            await apply_deal_scores()
            # Топ-10 по скору пересчитывается каждый цикл парсера квартир
            # (см. app_settings.PARSE_INTERVAL_*/random.uniform(50,80) мин) —
            # штамп нужен только чтобы показать "когда в последний раз" в аналитике.
            await app_settings.set("DEAL_SCORE_LAST_RUN_AT",
                                    datetime.now(timezone.utc).isoformat())
        except Exception as e:
            log.warning("hex price failed: %s", e)

        # === Отделка (черновая/с отделкой/с мебелью) — бесплатная эвристика
        # по тексту, см. bot/core/finish_classify.py ===
        if app_settings.get_bool("AI_FINISH_CLASSIFY", True):
            try:
                from bot.core.finish_classify import apply_finish_classification
                await apply_finish_classification()
            except Exception as e:
                log.warning("finish classification failed: %s", e)

        # === Дедупликация (приоритет объявлений от хозяина) ===
        try:
            from bot.core.dedup_listings import deduplicate_apartment_listings
            dup_count = await deduplicate_apartment_listings()
            if dup_count:
                log.info("Deduplicated %d apartment listings", dup_count)
        except Exception as e:
            log.warning("Apartment deduplication failed: %s", e)

        # Раньше синкали КАЖДЫЙ цикл парсера (~50-80 мин, т.е. десятки раз в
        # сутки) — Sheets не нужен настолько часто, а лишняя нагрузка на API
        # Google (и на сам цикл) того не стоит. Раз в сутки достаточно.
        # (datetime/timezone уже импортированы на уровне модуля — этот
        # повторный локальный импорт делал имя "datetime" локальным для ВСЕЙ
        # функции run_cycle, включая код глубокого обхода ВЫШЕ по тексту,
        # который выполняется раньше этой строки: там падало
        # UnboundLocalError на каждом завершении круга обхода, из-за чего
        # DEEP_SWEEP_PAGE никогда не сохранялся и курсор навсегда застревал
        # на последней странице вместо сброса на страницу 9 — обход
        # фактически не работал.)
        last_sync = app_settings.get("SHEETS_APARTMENTS_SYNCED_AT", "")
        needs_sync = True
        if last_sync:
            try:
                last_dt = datetime.fromisoformat(last_sync)
                needs_sync = (datetime.now(timezone.utc) - last_dt).total_seconds() > 20 * 3600
            except ValueError:
                needs_sync = True
        if needs_sync:
            await sync_apartments_to_sheets_pg()
            log.info("Google Sheets: Квартиры synced")
            await app_settings.set("SHEETS_APARTMENTS_SYNCED_AT",
                                   datetime.now(timezone.utc).isoformat())
    except Exception as e:
        log.warning("Sheets sync failed: %s", e)

    log.info("=== Apartment cycle done ===\n")


async def _run_cycle_timed():
    """run_cycle() обёрнутый таймингом + счётчиком реальных HTTP-запросов
    к Крыше за цикл — снимок в parser_cycle_history для графиков
    "сколько идёт парсинг" и "нагрузка на Крышу" на /admin/parser.
    Плюс (см. задачу "оптимизация работы парсеров" — пропуск detail-fetch
    для объявлений без изменений) — эффективность этой оптимизации за
    цикл: total_seen/needs_detail_fetch/skipped_no_change, см.
    apartment_parser.DETAIL_FETCH_STATS. Показывается на
    /admin/parsers?tab=recheck, секция "Нагрузка на Крышу"."""
    import time as _time
    from bot.core.apartment_parser import (
        REQUEST_COUNTS as _search_counts, DETAIL_FETCH_STATS as _df_stats)
    from bot.core.apartment_details import REQUEST_COUNTS as _detail_counts
    _search_counts["search"] = 0
    _detail_counts["detail"] = 0
    _df_stats["total_seen"] = 0
    _df_stats["needs_fetch"] = 0
    _df_stats["skipped"] = 0
    started = _time.monotonic()
    try:
        await run_cycle()
    finally:
        duration_sec = _time.monotonic() - started
        try:
            from bot.db.pg import execute as _pg_exec_cycle
            await _pg_exec_cycle(
                "INSERT INTO parser_cycle_history "
                "(duration_sec, search_requests, detail_requests, "
                " total_seen, needs_detail_fetch, skipped_no_change) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                round(duration_sec), _search_counts["search"], _detail_counts["detail"],
                _df_stats["total_seen"], _df_stats["needs_fetch"], _df_stats["skipped"])
        except Exception as e:
            log.warning("parser_cycle_history snapshot failed: %s", e)


async def main():
    from bot.db.pg import init_pool, execute as _pg_exec_init
    await init_pool(DATABASE_URL)
    await _pg_exec_init("""
        CREATE TABLE IF NOT EXISTS parser_cycle_history (
            id SERIAL PRIMARY KEY,
            at TIMESTAMPTZ DEFAULT now(),
            duration_sec INT,
            search_requests INT,
            detail_requests INT,
            total_seen INT,
            needs_detail_fetch INT,
            skipped_no_change INT
        )
    """)
    # Бэкфилл колонок для инсталляций, где таблица уже существовала до
    # оптимизации detail-fetch (см. migrations/034_parser_cycle_detail_fetch.sql).
    for _col in ("total_seen", "needs_detail_fetch", "skipped_no_change"):
        try:
            await _pg_exec_init(f"ALTER TABLE parser_cycle_history ADD COLUMN IF NOT EXISTS {_col} INT")
        except Exception as e:
            log.warning("parser_cycle_history ALTER (%s) failed: %s", _col, e)
    log.info("=== Apartment service started ===")

    # Первый цикл сразу
    try:
        await _run_cycle_timed()
    except Exception as e:
        log.error("First cycle error: %s", e, exc_info=True)

    while True:
        sleep_min = random.uniform(50, 80)  # ~раз в час, рандом
        log.info("Next cycle in %.0f min...", sleep_min)
        await asyncio.sleep(sleep_min * 60)
        try:
            await _run_cycle_timed()
        except Exception as e:
            log.error("Cycle error: %s", e, exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
