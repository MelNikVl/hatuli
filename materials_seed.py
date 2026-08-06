#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""complex_materials: материалы ЖК из открытых источников (сайты застройщиков, PDF, обзоры).
Заполнение по данным из «Песочницы информации по ЖК» (Notion, 06.08.2026)."""
import subprocess, json

def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot", "-t", "-A", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()

DDL = """
CREATE TABLE IF NOT EXISTS complex_materials (
  id SERIAL PRIMARY KEY,
  complex_id INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
  facade TEXT,
  walls TEXT,
  windows TEXT,
  elevators TEXT,
  heating TEXT,
  doors TEXT,
  notes TEXT,
  source_name TEXT,
  source_url TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (complex_id, source_name)
);
GRANT SELECT, INSERT, UPDATE ON complex_materials TO krisha;
GRANT USAGE, SELECT ON SEQUENCE complex_materials_id_seq TO krisha;
"""
psql(DDL)

# (название ЖК для матча, данные)
MAT = [
    ("Parkside", "Бесшумные скоростные лифты; входная группа с витражным остеклением; колясочные; стабилизированные растения", None, None, "Бесшумные скоростные лифты", None, None, "6 секций, 9–20 этажей", "bi.group", "https://bi.group/ru/landing/parkside"),
    ("Vivaldi", "Фасад — алюминиевые панели + лаймстоун; монолитный каркас; 7–8 этажей; потолки 3,6 м", "Фасад: алюминиевые панели, лаймстоун", "Монолитный", None, "Центральное", None, "Потолки 3,6 м; грузопассажирский лифт; отделка предчистовая; застройщик BI Development Astana / Grand Арнау", "vivaldi.bi.group", "https://vivaldi.bi.group/"),
    ("La Vie", "Панорамные окна; премиальные материалы; монолит; потолки 3,15–3,6 м; 42 квартиры", None, "Панорамные окна", None, None, None, "Система очистки воздуха на крыше с подачей в квартиры + кондиционирование; застройщик Orda Invest", "lavie.orda-invest.kz", "https://lavie.orda-invest.kz/"),
    ("Akbulak Riviera", "Материалы премиум-класса; архитектор Level 80", None, None, None, None, None, "PDF-презентация с материалами на сайте; дизайнерская отделка — кварц-винил, МДФ", "akbulak-riviera.bi.group", "https://akbulak-riviera.bi.group/"),
    ("Garden View", "Фасад — HPL панели; бизнес+ класс; 12/20 этажей; потолки 3,3 м", "Фасад: HPL панели", None, None, None, None, "Отделка предчистовая", "bi.group", "https://bi.group/ru/landing/garden-view"),
    ("Aisar", "Вентилируемый фасад из композитных алюминиевых панелей (1–2 этажи — облицовочный керамический кирпич); монолитный железобетонный каркас", "Фасад: композитные алюминиевые панели + керамический кирпич", "3-е остекление, 5-камерный металлопластиковый профиль", "Бесшумные лифты", "Модульная газовая котельная (~190 ₸/м² зимой)", None, "Межквартирные стены — Acoustic Pro (2 оч.) / керамический кирпич (1 оч.); межкомнатные — газоблок/ГКЛ; Aisar 4: электрозамки, IP-домофония", "PDF-брошюра BI Group", "https://s3.bi.group/biclick/content-manager/Aisar_fc3325979d.pdf"),
    ("AruPark", "Фасад — вентилируемые навесные панели; каркас — керамзитобетонные модули; эко-линейка материалов", "Каркас: керамзитобетонные модули", None, None, None, None, "9 этажей; потолки 2,65 м; окна 1,7 м; отделка черновая", "bi.group / krisha", "https://bi.group/ru/landing/arupark"),
    ("GreenLine", "Фасад — композитные алюминиевые панели (Vita) / клинкерная плитка + композит (Flora); стены — керамзитобетонные модули", "Стены: керамзитобетонные модули", "Металлопластиковые", None, None, None, "Vita: 9/12 эт., потолки 3 м; Headliner Exclusive: витражное остекление входной группы", "krisha / kapster", "https://m.krisha.kz/complex/show/nur-sultan/greenlinevita/"),
    ("Capital Park", "Вентилируемый фасад (Emotions); стены — газоблок; монолитный ЖБ каркас", "Стены: газоблок", "Европейские металлопластиковые, тройное остекление, пятикамерные", None, None, None, "Art: потолки 3–3,3 м, окна 2,2–2,5 м; Flowers: фасад первых этажей камень; Emotions: витраж", "krisha", "https://krisha.kz/complex/show/astana/capitalparkemotions/"),
    ("Auez", "Фасад — клинкерный кирпич + керамогранит / алюминиевые композитные панели + клинкерная плитка; монолитный ЖБ каркас", "Фасад: клинкер + керамогранит", None, None, None, None, "9/16 этажей; потолки 3 м; окна до 2,7 м; 259 квартир", "krisha / kn.kz", "https://krisha.kz/complex/show/astana/auez/"),
    ("MOD Urban", "Фасад — клинкерный кирпич + навесной вентилируемый; бизнес-класс", "Фасад: клинкер + вентфасад", None, None, None, None, "9/12 этажей; потолки 3 м; 417 квартир; отделка предчистовая", "krisha / kapster", "https://m.krisha.kz/complex/show/astana/modurban/"),
    ("UIA.BIRLIK", "Фасады — HPL панели / алюминиевые вентилируемые; наружные стены — газоблок; монолитно-каркасная технология", "Стены: газоблок", "Металлопластиковые, тройное остекление", None, None, None, "Район Нура, пр. Улы Дала 15", "kn.kz / homsters", "https://www.kn.kz/zhilye-kompleksy/astana/uia-birlik"),
    ("Park City Forum", "Фасад — фиброцементные панели; монолитная технология; 12–18 этажей", "Фасад: фиброцемент", None, None, None, None, "Комфорт-класс; потолки 3 м; застройщик SAT-NS", "krisha / sat-ns.kz", "https://krisha.kz/complex/show/astana/parkcityforum/"),
    ("Koktobe City", "Фасадная плитка LAMINAM (уникальные фасады); монолитный ЖБ каркас с диафрагмами жесткости; монолитные перекрытия", "Фасад: LAMINAM", None, None, None, None, "Застройщик Kusto Home (4-я очередь)", "kustohome.kz", "https://kustohome.kz/projects/koktobe-city-4-ya-ochered/"),
    ("The One", "Панорамные окна до 2,7×2,9 м; металлопластиковый профиль, тройные энергосберегающие стеклопакеты; высокие потолки", None, "Панорамные, металлопластик, тройные энергосберегающие стеклопакеты", "Скоростные бесшумные с персональным доступом на этаж", None, None, "Застройщик BAZIS-А", "the-one.bazis.kz", "https://the-one.bazis.kz/"),
    ("Dara Residence", "Фасад — комбинация высококлассных материалов + витражное остекление; утепление эко-теплоизолятором", "Фасад: комбинация материалов", None, None, None, None, "14/22 этажа; потолки 3 м; отделка предчистовая", "sensata.kz", "https://sensata.kz/project/zk-dara-residence"),
    ("Europe City", "Фасад — фиброцементная панель + клинкерный кирпич; стены ЖБ; монолитно-каркасная; утепление минвата", "Стены: железобетон; утепление минвата", "Металлопластиковые, энергосберегающие", "Грузовой, пассажирский", "Центральное", None, "Элит; 6 домов 18–25 эт.; 520 квартир 66–234 м²; застройщик ТОО «Байсанат» / Orda Invest", "korter / europecity.orda-invest.kz", "https://europecity.orda-invest.kz/"),
    ("Swiss Collection", "Фасад — натуральный камень + панорамные окна; премиум-класс", "Фасад: натуральный камень", "Панорамные", None, None, None, "50–210 м²; потолки до 3,75 м; Swissôtel; застройщик Raaf Group", "swisscollection.kz", "https://swisscollection.kz/"),
    ("GRAND MONACO", "Фасады — натуральный гранит + травертин / керамические панели + натуральный камень; классический стиль; 8/12/14 этажей", "Фасад: гранит, травертин", None, "Грузовой, пассажирский", "Автономное", None, "Застройщик BAZIS-А Corp.", "kn.kz / krisha", "https://www.kn.kz/zhilye-kompleksy/astana/grand-monaco"),
    ("Highvill", "Фасад — натуральные материалы и гранит (Gold Ishim); премиум (Gold) / комфорт (Ishim)", "Фасад: натуральные материалы, гранит", None, "Грузовой, пассажирский", None, None, "Ishim: 28 эт., потолки 3 м, 342 квартиры, надземный паркинг; Gold: двухуровневый паркинг", "krisha / youtube", "https://m.krisha.kz/complex/show/highvill-ishim/"),
    ("London", "Фасад в классическом британском стиле; лифтовые холлы облицованы дорогими материалами", None, None, "Скоростные бесшумные", None, None, "10 одноподъездных домов, ул. И. Панфилова 6–13; застройщик BAZIS-А", "krisha / korter", "https://krisha.kz/complex/show/astana/london/"),
    ("Landmark Gold", "Фасад — алюминиевые композитные панели / клинкерный кирпич + керамическая плитка + вентилируемый фасад; монолит", "Фасад: алюмокомпозит / клинкер", None, "Лифты фирмы Silver", "Автономное", None, "18/21 этажей; 403 квартиры; потолки 3 м; приточные клапаны AERONIX", "metry.kz / youtube", "http://metry.kz/zhilye-kompleksy/zhk-landmark-gold/"),
    ("SALZBURG", "Фасад — навесной вентилируемый из гранита и травертина; каркас сборно-монолитный; стены — газобетонные блоки", "Стены: газобетон; каркас сборно-монолитный", None, None, None, None, "4 дома, 8 этажей, бизнес-класс; потолки 3 м; отделка черновая", "krisha / kapster", "https://krisha.kz/complex/show/astana/salzburg/"),
    ("LEVEL", "Фасад — клинкерный кирпич + натуральный камень + вентилируемый фасад", "Фасад: клинкер, натуральный камень", None, "Пассажирский", None, None, "167 квартир; застройщик BAZIS-А", "krisha", "https://krisha.kz/complex/show/astana/level/"),
    ("Imran", "Наружные стены — газобетонные блоки + утепление минватой; фасад — фиброцементные панели", "Стены: газобетон + минвата", None, None, None, None, "Комфорт", "korter", "https://korter.kz/жк-imran"),
    ("Salman City", "Стены — газобетон с утеплением минераловатными плитами; фасады — фиброцементные панели (композитные по kn.kz); 9–16 этажей, 6 блоков", "Стены: газобетон + минплита", None, None, None, None, "Комфорт+; площади 36,7–113,5 м²; застройщик Ulytau Group / Мой Дом MWC", "ulytau.group / krisha", "https://ulytau.group/ru/projects/salman-city/"),
    ("Altyn Säulet", "Стены — газоблок; каркас — монолитный ЖБ; фасад — фиброцементные панели + гранит", "Стены: газоблок; каркас монолитный ЖБ", "На сайте altyn-saulet.kz (полный список)", None, None, None, "465 квартир; высокая сейсмостойкость; многоуровневая подсветка фасадов; застройщик Ulytau Group", "altyn-saulet.kz", "https://altyn-saulet.kz/"),
    ("Aviator 2", None, None, None, None, None, None, "Эконом-класс, 1 дом 7–8 эт.; застройщик Aiz Qurylys; сдан I кв. 2020", "korter", "https://korter.kz/жк-aviator-2"),
    ("Altyn City", None, None, "Панорамные энергосберегающие", None, "Автономное", None, "Свободная планировка; стильный фасад", "instagram", None),
    ("Time City", None, None, None, None, None, None, "Сайт застройщика: timecity.kz", "timecity.kz", "https://timecity.kz/"),
    ("Manar", "Навесной вентилируемый фасад из гранита + фиброцементных панелей; кирпичный комплекс", "Фасад: гранит + фиброцемент", None, None, None, None, "Комфорт, 9–16 эт., Есильский район; корзины под кондиционеры; застройщик Sensata Group", "sensata.kz", "https://sensata.kz/project/manar"),
    ("W TOWERS", "Фасад — клинкер бренда Feldhaus + фиброцементные панели Equitone / алюминий + стекло; монолит", "Фасад: Feldhaus, Equitone", "Угловое остекление, витражные окна", "Пассажирский", None, None, "Бизнес; 19–22 эт.; потолки 2,95 м; подземный паркинг", "sensata.kz / wtowers.kz", "https://sensata.kz/project/w-towers"),
    ("UIA.TARIH", "Фасад — алюминиевые композитные панели + клинкерная плитка + Limestone; наружные стены — кирпич; монолитно-каркасная", "Наружные стены: кирпич", None, None, None, None, "Застройщик BI Group", "krisha / kn.kz", "https://krisha.kz/complex/show/astana/uiatarih/"),
    ("Turan Palace", "Фасады — утепление эко-теплоизолятором + фиброцементные панели; подсветка", "Фасад: фиброцемент", None, None, None, None, "Бизнес (II класс); 9–16 эт.; 289 объектов; сдан III кв. 2025", "korter / homsters", "https://korter.kz/жк-turan-palace"),
    ("Galaxy Star", None, None, None, None, None, None, "Застройщик Galamat; левый берег", "galamat.kz", "https://galamat.kz/project/galaxy-star"),
    ("Мирадж", "Фасад — гранит + декоративная штукатурка с утеплением", "Фасад: гранит, декоративная штукатурка", None, None, None, None, "12/15 эт.; потолки 2,8 м; ул. Кайым Мухамедханов 25, Нура; сдача IV кв. 2027; застройщик Дилан-Куш", "zhkmiradge.kz", "https://zhkmiradge.kz/"),
    ("Столичный 2", None, None, None, None, None, None, "Застройщик Meridian Stroy LTD; Сарыарка, пр. Абая 10; сдан II кв. 2024; от 420 тыс ₸/м²", "krisha", "https://krisha.kz/complex/show/nur-sultan/stolichniy2/"),
    ("Evolution", "Фасад — фиброцементные панели + клинкерный кирпич на нижних этажах; теплоизоляция под столичный климат", "Фасад: фиброцемент + клинкер", "Энергосберегающее стекло", "Пассажирский", "Центральное", None, "Бизнес; 80 квартир; ввод 2025; 133 762 м²; застройщик BAZIS-А", "evolution.bazis.kz / krisha", "https://evolution.bazis.kz/"),
    ("Фирдаус", "Фасад — облицовочный кирпич / фиброцементные панели; кирпичное здание 12 эт., 8 секций", "Фасад: облицовочный кирпич / фиброцемент", "Немецкий профиль, тройное остекление", None, None, None, "660 квартир; видеонаблюдение; застройщик Beles", "krisha / kn.kz", "https://krisha.kz/complex/show/nur-sultan/firdaus/"),
    ("Rauda", None, None, None, None, None, None, "Застройщик Isma Invest", "korter", "https://korter.kz/жк-rauda"),
]

# матчим по имени
rows = [r.split("	", 1) for r in psql("SELECT id || chr(9) || name FROM complexes WHERE is_garbage IS NOT TRUE").splitlines() if r]
by_name = {}
for cid, name in rows:
    n = name.strip().lower()
    by_name.setdefault(n, []).append(cid)

def norm(s):
    import re
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^a-zа-я0-9]", "", s)
    return s

matched = 0
unmatched = []
for name, facade, walls, windows, elev, heat, doors, notes, src, url in MAT:
    cand = None
    key = norm(name)
    # точное или содержит
    for n, ids in by_name.items():
        if norm(n) == key or key in norm(n) or norm(n) in key:
            cand = ids[0]
            break
    if cand is None and name == "Landmark Gold":
        # попробуем Landmark
        for n, ids in by_name.items():
            if "landmark" in norm(n):
                cand = ids[0]
                break
    if cand is None and name == "Highvill":
        for n, ids in by_name.items():
            if "highvill" in norm(n):
                cand = ids[0]
                break
    if cand is None and name == "GreenLine":
        for n, ids in by_name.items():
            if "greenline" in norm(n) or "green line" in norm(n):
                cand = ids[0]
                break
    if cand is None and name == "Capital Park":
        for n, ids in by_name.items():
            if "capital park" in norm(n):
                cand = ids[0]
                break
    if cand is None and name == "Altyn Säulet":
        for n, ids in by_name.items():
            if "алтын саулет" in norm(n):
                cand = ids[0]
                break
    if cand is None:
        unmatched.append(name)
        continue
    f = f"'{facade.replace(chr(39), chr(39)*2)}'" if facade else "NULL"
    w = f"'{walls.replace(chr(39), chr(39)*2)}'" if walls else "NULL"
    wi = f"'{windows.replace(chr(39), chr(39)*2)}'" if windows else "NULL"
    e = f"'{elev.replace(chr(39), chr(39)*2)}'" if elev else "NULL"
    h = f"'{heat.replace(chr(39), chr(39)*2)}'" if heat else "NULL"
    d = f"'{doors.replace(chr(39), chr(39)*2)}'" if doors else "NULL"
    n = f"'{notes.replace(chr(39), chr(39)*2)}'" if notes else "NULL"
    s = f"'{src.replace(chr(39), chr(39)*2)}'" if src else "NULL"
    u = f"'{url.replace(chr(39), chr(39)*2)}'" if url else "NULL"
    psql(f"""INSERT INTO complex_materials (complex_id, facade, walls, windows, elevators, heating, doors, notes, source_name, source_url)
             VALUES ({cand}, {f}, {w}, {wi}, {e}, {h}, {d}, {n}, {s}, {u})
             ON CONFLICT (complex_id, source_name) DO UPDATE SET facade=EXCLUDED.facade, walls=EXCLUDED.walls,
             windows=EXCLUDED.windows, elevators=EXCLUDED.elevators, heating=EXCLUDED.heating, doors=EXCLUDED.doors,
             notes=EXCLUDED.notes, source_url=EXCLUDED.source_url""")
    matched += 1
    print(f"✓ {name} -> complex #{cand}")

print(f"\nСопоставлено: {matched}, не найдено: {unmatched}")
print(psql("SELECT COUNT(*) FROM complex_materials"))
