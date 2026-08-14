#!/usr/bin/env python3
"""Парсер homeportal.kz: официальные данные по ЖК (долевое строительство КЖК).
Список: POST /api/v1/getobjects (публичный) → детали: GET /api/v1/objects-detail/{id}.
Щадящий: пауза 1с между деталками. Заполняет homeportal_objects + маппинг на complexes.

Маппинг на complexes (задача 2026-08-12, см. README): кандидата находим
локально (дёшево, без похода в БД на каждый из ~600 объектов) через
норм./fuzzy/first_word индексы по именам, а вот итоговые confidence и
match_method считает bot.core.entity_resolution.score_match() по реальным
сигналам (имя через pg_trgm + гео + застройщик БИН + адрес) — и пишет
через record_source_link() в spine (complex_source_links) при auto,
иначе в очередь на проверку (учитывает уже отклонённые руками пары и не
перезаписывает молча конфликт с другим ЖК). Раньше кандидат писался в
homeportal_objects.matched_complex_id напрямую, мимо spine — 585 связей
скопились вне единой истины между источниками, гасили backfill'ом
разово; теперь копится через record_source_link() каждый прогон."""
import asyncio
import difflib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv()
import os
from complex_completion_year_backfill import _parse_commissioning_date

FUZZY_THRESHOLD = 0.75
MAX_AGE_DAYS = 7  # инкремент: деталки только для новых ЖК и старше 7 дней

API = "https://api.homeportal.kz/api/v1"
UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36"
DELAY = 90.0  # щадящий темп: 1 ЖК / 90 сек (по требованию пользователя)
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot", "-t", "-A", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()


def req(url: str, post=None) -> dict:
    data = json.dumps(post).encode() if post is not None else None
    r = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Accept": "application/json",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


ESC = lambda s: str(s).replace(chr(39), chr(39) * 2) if s is not None else ""


def norm_name(n: str) -> str:
    """Нормализация названия ЖК для маппинга: нижний регистр, убираем мусор маркетплейсов."""
    if not n:
        return ""
    n = n.lower()
    n = re.sub(r"жк[\s\-]*", "", n)          # «жк »
    n = re.sub(r"\([^)]*\)", " ", n)          # «(Комфорт+ Класс)»
    n = re.sub(r"—.*$", "", n)                # « — Формат...»
    n = re.sub(r"\|.*$", "", n)               # « | Комфорт+»
    n = re.sub(r"год постройки.*$", "", n)
    n = re.sub(r"[^a-zа-яё0-9 ]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


async def main() -> int:
    from bot.db.pg import init_pool, close_pool, fetch, fetchrow, fetchval
    from bot.core.entity_resolution import score_match, record_source_link
    from bot.core.geo import in_astana_bbox
    await init_pool(DATABASE_URL)

    # индексы complexes для маппинга (загружаем один раз)
    global COMPLEX_INDEX, NORM_INDEX
    cx_rows = [r.split("|", 1) for r in psql("SELECT id, name FROM complexes WHERE is_garbage IS NOT TRUE").splitlines() if r]
    COMPLEX_INDEX = {name.strip().lower(): int(cid) for cid, name in cx_rows}
    NORM_INDEX = {}
    for cid, name in cx_rows:
        nm = norm_name(name)
        if nm and nm not in NORM_INDEX:
            NORM_INDEX[nm] = int(cid)
    print(f"complexes проиндексировано: {len(COMPLEX_INDEX)}")
    link_stats: dict[str, int] = {}

    # Уже подтверждённые связи этого источника в spine — если source_id
    # там уже есть, это settled fact (сам object_id стабилен между
    # прогонами), пере-скорить и заново класть в очередь на review при
    # каждом перезапуске таймера (раз в 45 мин) незачем: geo у homeportal
    # (точные координаты объекта) и у complexes (обычно Nominatim по
    # улице) может расходиться на 100-300+ м — этого одного достаточно,
    # чтобы уже подтверждённая пара не набрала 0.8 заново и заспамила
    # очередь тем же кандидатом на каждый прогон.
    hp_links = {r["source_id"]: r["complex_id"] for r in
                await fetch("SELECT source_id, complex_id FROM complex_source_links WHERE source = 'homeportal'")}
    print(f"уже в spine (homeportal): {len(hp_links)}")

    # 1) список всех объектов
    d = req(f"{API}/getobjects", {})
    rows = d["data"]["objects"]["data"]
    astana = [r for r in rows if (r.get("region") or {}).get("ru") == "г. Астана"]
    print(f"всего: {len(rows)}, Астана: {len(astana)}")

    # Инкрементальный проход: реестр конечный (620 ЖК), раньше каждый
    # таймерный запуск (~45 мин) переписывал ВСЕ 620 заново ≈ 15.5 ч.
    # Теперь деталки тянем только для новых ЖК и тех, чьи данные старше
    # MAX_AGE_DAYS дней (свежесть по fetched_at, считаем в SQL).
    fresh_ids = set()
    for r in psql(f"SELECT object_id FROM homeportal_objects "
                  f"WHERE fetched_at > now() - interval '{MAX_AGE_DAYS} days'").splitlines():
        if r:
            fresh_ids.add(int(r))
    todo = [o for o in astana if o["id"] not in fresh_ids]
    skipped = len(astana) - len(todo)
    print(f"свежих (< {MAX_AGE_DAYS} дн): {skipped}, к обновлению: {len(todo)}")

    done, errors = 0, 0
    for o in todo:
        oid = o["id"]
        try:
            det = req(f"{API}/objects-detail/{oid}")
            data = det.get("data") or {}
            basic = data.get("basicData") or {}
            dev = basic.get("developerData") or {}
            apt = data.get("apartmentData") or []
            objd = data.get("objectData") or {}
            loc = data.get("locationData") or {}
            comp = data.get("companyData") or {}
            auth = comp.get("authorizedData") or {}
            sup = comp.get("supervisingData") or {}
            tech = comp.get("technicalSupervisingData") or {}

            # bbox-валидация координат ДО записи (задача 2026-08-12,
            # карантин — geo_quarantine.py постфактум нашёл 2 значения в
            # сотнях км от Астаны, отданных этим же API; отсекаем на входе,
            # чтобы такое больше не копилось). hp_lat/hp_lon — вниз по
            # коду для score_match(), тот же провалидированный источник,
            # не парсим loc.get(...) дважды.
            try:
                hp_lat = float(loc.get("latitude")) if loc.get("latitude") not in (None, "") else None
                hp_lon = float(loc.get("longitude")) if loc.get("longitude") not in (None, "") else None
            except (TypeError, ValueError):
                hp_lat = hp_lon = None
            if hp_lat is not None and not in_astana_bbox(hp_lat, hp_lon):
                print(f"  ⚠️ {oid}: координаты вне bbox Астаны ({hp_lat},{hp_lon}) от API — не пишу")
                hp_lat = hp_lon = None

            # сумма квартир и проданных по очередям
            apt_total = sum((a.get("no_of_apartments") or 0) for a in apt)
            apt_sold = sum((a.get("no_of_apartments_sold") or 0) for a in apt)
            r1 = sum((a.get("amount_of_1_room_ap") or 0) for a in apt)
            r2 = sum((a.get("amount_of_2_room_ap") or 0) for a in apt)
            r3 = sum((a.get("amount_of_3_room_ap") or 0) for a in apt)
            r4 = sum((a.get("amount_of_4_room_ap") or 0) for a in apt)

            psql(f"""INSERT INTO homeportal_objects (
                object_id, name, slug, authority, warranty_number, issue_date, start_date,
                commissioning_date, address, region, latitude, longitude, cadastral_number,
                developer_bin, developer_name, developer_phone, developer_email,
                authorized_bin, authorized_name, supervising_bin, supervising_name,
                tech_bin, tech_name, no_of_houses, no_of_floors, ceiling_height,
                building_type, wall_filling, facade_finishing, comfort_class,
                no_of_entrances, passenger_elevators, freight_elevators, parking_places,
                playgrounds, sports_fields, is_orda_plus, orda_plus_percent, program, program_link,
                apartments_total, apartments_sold, rooms_1, rooms_2, rooms_3, rooms_4,
                apartment_data, images, fetched_at)
                VALUES ({oid}, '{ESC(basic.get("name"))}', '{ESC(o.get("slug"))}',
                '{ESC((basic.get("authority") or {}).get("name"))}', '{ESC(basic.get("warranty_number"))}',
                '{ESC(basic.get("issue_date"))}', '{ESC(basic.get("start_date"))}',
                '{ESC(basic.get("commissioning_date"))}', '{ESC(basic.get("address"))}',
                '{ESC((basic.get("region") or {}).get("name_ru"))}', '{ESC(hp_lat)}',
                '{ESC(hp_lon)}', '{ESC(loc.get("cadastral_number"))}',
                '{ESC(dev.get("bin"))}', '{ESC(dev.get("name"))}', '{ESC(dev.get("phone"))}', '{ESC(dev.get("email"))}',
                '{ESC(auth.get("bin"))}', '{ESC(auth.get("name"))}',
                '{ESC(sup.get("bin"))}', '{ESC(sup.get("name"))}',
                '{ESC(tech.get("bin"))}', '{ESC(tech.get("name"))}',
                '{ESC(objd.get("no_of_houses"))}', '{ESC(objd.get("no_of_floors"))}',
                '{ESC(objd.get("ceiling_height"))}', '{ESC(objd.get("building_type"))}',
                '{ESC(objd.get("wall_filling"))}', '{ESC(objd.get("facade_finishing"))}',
                '{ESC(objd.get("comfort_class"))}', '{ESC(objd.get("number_of_entrances"))}',
                '{ESC(objd.get("passenger_elevators_in_the_entrance"))}', '{ESC(objd.get("freight_elevators_at_the_entrance"))}',
                '{ESC(objd.get("amount_of_parking_places"))}', '{ESC(objd.get("playgrounds"))}',
                '{ESC(objd.get("sports_fields"))}', '{ESC(objd.get("is_orda_plus"))}',
                '{ESC(objd.get("orda_plus_percent"))}', '{ESC(basic.get("program"))}', '{ESC(basic.get("program_link"))}',
                {apt_total}, {apt_sold}, {r1}, {r2}, {r3}, {r4},
                '{json.dumps(apt, ensure_ascii=False).replace(chr(39), chr(39)*2)}'::jsonb,
                '{json.dumps(o.get("images") or [], ensure_ascii=False).replace(chr(39), chr(39)*2)}'::jsonb, now())
                ON CONFLICT (object_id) DO UPDATE SET
                name = EXCLUDED.name, authority = EXCLUDED.authority, warranty_number = EXCLUDED.warranty_number,
                issue_date = EXCLUDED.issue_date, start_date = EXCLUDED.start_date,
                commissioning_date = EXCLUDED.commissioning_date, address = EXCLUDED.address,
                latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude, cadastral_number = EXCLUDED.cadastral_number,
                developer_bin = EXCLUDED.developer_bin, developer_name = EXCLUDED.developer_name,
                no_of_floors = EXCLUDED.no_of_floors, ceiling_height = EXCLUDED.ceiling_height,
                building_type = EXCLUDED.building_type, wall_filling = EXCLUDED.wall_filling,
                facade_finishing = EXCLUDED.facade_finishing, comfort_class = EXCLUDED.comfort_class,
                no_of_entrances = EXCLUDED.no_of_entrances, passenger_elevators = EXCLUDED.passenger_elevators,
                freight_elevators = EXCLUDED.freight_elevators, parking_places = EXCLUDED.parking_places,
                apartments_total = EXCLUDED.apartments_total, apartments_sold = EXCLUDED.apartments_sold,
                rooms_1 = EXCLUDED.rooms_1, rooms_2 = EXCLUDED.rooms_2, rooms_3 = EXCLUDED.rooms_3,
                rooms_4 = EXCLUDED.rooms_4, apartment_data = EXCLUDED.apartment_data,
                images = EXCLUDED.images, fetched_at = now()""")
            # Кандидата на complexes ищем локально (дёшево): точное →
            # нормализованное → fuzzy (difflib, порог FUZZY_THRESHOLD) →
            # первое слово (самый слабый fallback) — по индексам выше.
            # Но confidence/match_method, которые реально пишутся, считает
            # score_match() по нормализованным именам (сырые "ЖК \"Х\"
            # (очередь N)" против "Х" у trigram-сравнения почти не похожи —
            # без norm_name() имя-сигнал гас бы на большинстве настоящих
            # совпадений) + гео/застройщик/адрес, реальные сигналы, которых
            # у локального индекса не было вовсе.
            obj_name = o.get("name") or ""
            cid, method, link_result = None, None, None
            if str(oid) in hp_links:
                # уже подтверждённая связь этого источника — используем как
                # есть, без пере-скоринга и без похода в очередь (см. коммент
                # у hp_links выше).
                cid, method, link_result = hp_links[str(oid)], "already_in_spine", "auto"
                link_stats[link_result] = link_stats.get(link_result, 0) + 1
            else:
                find_tier = None
                cid = COMPLEX_INDEX.get(obj_name.strip().lower())
                if cid:
                    find_tier = "exact"
                nm = norm_name(obj_name)
                if not cid and nm:
                    cid = NORM_INDEX.get(nm)
                    if cid:
                        find_tier = "normalized"
                if not cid and nm:
                    best_ratio, best_cid = 0.0, None
                    for cand_norm, cand_cid in NORM_INDEX.items():
                        ratio = difflib.SequenceMatcher(None, nm, cand_norm).ratio()
                        if ratio > best_ratio:
                            best_ratio, best_cid = ratio, cand_cid
                    if best_ratio >= FUZZY_THRESHOLD:
                        cid, find_tier = best_cid, f"fuzzy:{best_ratio:.2f}"
                if not cid and nm:
                    parts = nm.split()
                    if len(parts) >= 2:
                        cid = NORM_INDEX.get(parts[0])
                        if cid:
                            find_tier = "first_word"

                if cid:
                    cand = await fetchrow("SELECT name, lat, lon, address FROM complexes WHERE id = $1", cid)
                    dev_bin = await fetchval("SELECT developer_bin FROM complex_tech_specs WHERE complex_id = $1", cid)
                    # hp_lat/hp_lon уже провалидированы bbox-проверкой выше
                    # (до записи в homeportal_objects) — переиспользуем.
                    conf, method = await score_match(
                        nm or obj_name, norm_name(cand["name"]) if cand else (nm or obj_name),
                        existing_lat=cand["lat"] if cand else None,
                        existing_lon=cand["lon"] if cand else None,
                        candidate_lat=hp_lat, candidate_lon=hp_lon,
                        developer_match=bool(dev_bin) and dev_bin == dev.get("bin"),
                        existing_address=cand["address"] if cand else None,
                        candidate_address=basic.get("address"),
                        # СЫРЫЕ имена отдельно — токен очереди/фазы («2
                        # очередь», хвостовой номер) часто сидит внутри
                        # скобок, которые norm_name() уже вырезал из nm/
                        # cand["name"] выше (см. докстринг _phase_token).
                        name_a_full=obj_name, name_b_full=cand["name"] if cand else obj_name,
                    )
                    link_result = await record_source_link(
                        cid, "homeportal", str(oid), confidence=conf, method=method, matched_by="auto")
                    link_stats[link_result] = link_stats.get(link_result, 0) + 1
                    print(f"  {oid} {obj_name[:40]!r}: кандидат #{cid} ({find_tier}) -> "
                          f"{link_result} ({method}, {conf:.2f})")

            # Дальше (matched_complex_id для UI, housing_class_test,
            # description, developer_bin) пишем только для auto — эти
            # производные не должны показывать несогласованный/спорный
            # матч как подтверждённый. review/conflict ждут в очереди
            # (см. approve_candidate()/reject_candidate()).
            if cid and link_result == "auto":
                psql(f"UPDATE homeportal_objects SET matched_complex_id = {cid}, matched_at = now(), "
                     f"match_method = '{ESC(method)}' WHERE object_id = {oid}")
                psql(f"""INSERT INTO housing_class_test (complex_id, apartment_count, rooms_1, rooms_2, rooms_3, rooms_4, elevator_count, apartment_count_source, updated_at)
                         VALUES ({cid}, {apt_total}, {r1}, {r2}, {r3}, {r4},
                         {(objd.get('freight_elevators_at_the_entrance') or 0) + (objd.get('passenger_elevators_in_the_entrance') or 0)}, 'homeportal', now())
                         ON CONFLICT (complex_id) DO UPDATE SET
                         apartment_count = COALESCE(housing_class_test.apartment_count, EXCLUDED.apartment_count),
                         rooms_1 = COALESCE(housing_class_test.rooms_1, EXCLUDED.rooms_1),
                         rooms_2 = COALESCE(housing_class_test.rooms_2, EXCLUDED.rooms_2),
                         rooms_3 = COALESCE(housing_class_test.rooms_3, EXCLUDED.rooms_3),
                         rooms_4 = COALESCE(housing_class_test.rooms_4, EXCLUDED.rooms_4),
                         elevator_count = COALESCE(housing_class_test.elevator_count, EXCLUDED.elevator_count),
                         apartment_count_source = CASE WHEN housing_class_test.apartment_count IS NULL THEN 'homeportal' ELSE housing_class_test.apartment_count_source END,
                         updated_at = now()""")
                psql(f"""UPDATE complexes SET description = COALESCE(description, '🏛 {ESC(o.get('name'))}: официальные данные КЖК (долевое строительство)') WHERE id = {cid} AND description IS NULL""")
                # completion_year/quarter (задача 2026-08-14, "Часть 0 —
                # быстрые победы": срок сдачи был известен только у 48%
                # is_newbuild ЖК, единственный источник из трёх проверенных
                # {homeportal, developer-direct, korter}, реально отдающий
                # эти данные — см. complex_completion_year_backfill.py,
                # тот же парсер даты, чтобы не дублировать формат DD.MM.YYYY
                # -> год+квартал в двух местах). COALESCE-гейт — не
                # перезаписывает уже заполненное (в т.ч. руками).
                _commissioning = basic.get("commissioning_date")
                if _commissioning:
                    _parsed = _parse_commissioning_date(_commissioning)
                    if _parsed:
                        _year, _quarter = _parsed
                        psql(f"""UPDATE complexes SET
                                 completion_year = COALESCE(completion_year, {_year}),
                                 completion_quarter = COALESCE(completion_quarter, {_quarter})
                                 WHERE id = {cid}""")
                # БИН застройщика → complex_tech_specs (если пусто)
                if dev.get("bin"):
                    psql(f"""INSERT INTO complex_tech_specs (complex_id, developer_bin, updated_at)
                             VALUES ({cid}, '{ESC(dev.get("bin"))}', now())
                             ON CONFLICT (complex_id) DO UPDATE SET
                             developer_bin = COALESCE(complex_tech_specs.developer_bin, EXCLUDED.developer_bin),
                             updated_at = now()""")
            done += 1
        except Exception as e:
            errors += 1
            psql(f"INSERT INTO homeportal_parse_log (object_id, name, status, detail) "
                 f"VALUES ({oid}, '{ESC(o.get('name'))}', 'error', '{ESC(str(e)[:150])}')")
            print(f"❌ {oid}: {e}")
        await asyncio.sleep(DELAY)

    print(f"итог: {done} ok, {errors} ошибок (пропущено свежих: {skipped})")
    print(f"spine: {link_stats}")
    await close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
