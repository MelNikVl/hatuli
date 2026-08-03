#!/usr/bin/env python3
"""Парсер homeportal.kz: официальные данные по ЖК (долевое строительство КЖК).
Список: POST /api/v1/getobjects (публичный) → детали: GET /api/v1/objects-detail/{id}.
Щадящий: пауза 1с между деталками. Заполняет homeportal_objects + маппинг на complexes."""
import json
import re
import subprocess
import sys
import time
import urllib.request

API = "https://api.homeportal.kz/api/v1"
UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36"
DELAY = 90.0  # щадящий темп: 1 ЖК / 90 сек (по требованию пользователя)


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


def main() -> int:
    # индексы complexes для маппинга (загружаем один раз)
    global COMPLEX_INDEX, NORM_INDEX
    cx_rows = [r.split("|", 1) for r in psql("SELECT id, name FROM complexes").splitlines() if r]
    COMPLEX_INDEX = {name.strip().lower(): int(cid) for cid, name in cx_rows}
    NORM_INDEX = {}
    for cid, name in cx_rows:
        nm = norm_name(name)
        if nm and nm not in NORM_INDEX:
            NORM_INDEX[nm] = int(cid)
    print(f"complexes проиндексировано: {len(COMPLEX_INDEX)}")

    # 1) список всех объектов
    d = req(f"{API}/getobjects", {})
    rows = d["data"]["objects"]["data"]
    astana = [r for r in rows if (r.get("region") or {}).get("ru") == "г. Астана"]
    print(f"всего: {len(rows)}, Астана: {len(astana)}")

    done, errors = 0, 0
    for o in astana:
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
                '{ESC((basic.get("region") or {}).get("name_ru"))}', '{ESC(loc.get("latitude"))}',
                '{ESC(loc.get("longitude"))}', '{ESC(loc.get("cadastral_number"))}',
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
            # маппинг на complexes: точное → нормализованное → LIKE; помечаем matched
            obj_name = o.get("name") or ""
            cid = COMPLEX_INDEX.get(obj_name.strip().lower())
            if not cid:
                nm = norm_name(obj_name)
                cid = NORM_INDEX.get(nm)
            if not cid and nm:
                parts = nm.split()
                if len(parts) >= 2:
                    cid = NORM_INDEX.get(parts[0])
            if cid:
                psql(f"UPDATE homeportal_objects SET matched_complex_id = {cid}, matched_at = now() WHERE object_id = {oid}")
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
        time.sleep(DELAY)

    print(f"итог: {done} ok, {errors} ошибок")
    return 0


if __name__ == "__main__":
    sys.exit(main())
