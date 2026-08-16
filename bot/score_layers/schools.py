"""
Слой ШКОЛЫ/САДИКИ/ВУЗЫ: пешая доступность по OSM (радиус 700 м).

  школа + садик в 700 м → +5
  школа или садик       → +3
  только вуз            → +2 (важно для аренды студентам)
  ничего                →  0

Рейтинг заведений — следующий шаг (отдельный источник данных),
пока считаем сам факт доступности.

**university_only** (задача 2026-08-15, "Location Reliability Phase",
коммит "двойные школы + building_age") — опциональный режим, ДЕФОЛТ
False (поведение для остальных потребителей этого модуля — per-listing
скоринг через bot/score_layers/__init__.py::compute_all_layers — не
меняется ни строкой). Единственный вызывающий с university_only=True —
bot/core/location_score.py::compute_complex_location_score(): там есть
точные факторы по школам/садикам (astana_schools/astana_kindergartens,
_schools_factor()/_kindergartens_factor()) — этот OSM-слой считал бы
ТЕ ЖЕ школы/садики ЕЩЁ РАЗ (двойное взвешивание одного и того же
сигнала с двух источников). university_only=True оставляет здесь
пользу этого слоя ТОЛЬКО там, где у astana_schools/kindergartens нет
данных вообще — вузы (их нет ни в той, ни в другой таблице)."""
from __future__ import annotations

from bot.score_layers.osm import overpass_cached

_QUERY = """
[out:json][timeout:20];
(
  node(around:700,{lat},{lon})[amenity=school];
  way(around:700,{lat},{lon})[amenity=school];
  node(around:700,{lat},{lon})[amenity=kindergarten];
  way(around:700,{lat},{lon})[amenity=kindergarten];
  node(around:700,{lat},{lon})[amenity=university];
  way(around:700,{lat},{lon})[amenity=university];
);
out tags center 50;
"""


# Задача 2026-08-16 ("Локальный OSM-слой") — kind-набор, которым
# наполняет эти три категории scripts/sync_city_poi.py (еженедельно) и
# poi_import.py (ручной разовый импорт, тот же kind).
_LOCAL_KINDS = ["school", "kindergarten", "university"]


async def _from_local_table(lat: float, lon: float, university_only: bool = False) -> tuple[int, str] | None:
    """Если city_poi наполнена ИМЕННО по school/kindergarten/university
    (scripts/sync_city_poi.py или ручной poi_import.py) — считаем по ней:
    без сетевых запросов, без лимитов Overpass. None -> фолбэк на
    overpass_cached() (bot/score_layers/osm.py::local_poi_near — bbox +
    haversine, ПРОВЕРКА ИМЕННО ПО ЭТИМ kind, не "city_poi вообще не
    пуста": после появления в city_poi десятков других категорий
    (дороги/магазины/парки и т.п., см. sync_city_poi.py) старая проверка
    "SELECT COUNT(*) FROM city_poi" стала бы ложно-уверенной — нашла бы
    непустую таблицу и ошибочно доверилась бы 0 школам, хотя школы могли
    быть вообще не синхронизированы)."""
    from bot.score_layers.osm import local_poi_near
    rows = await local_poi_near(lat, lon, _LOCAL_KINDS, 700)
    if rows is None:
        return None
    kinds = {r["kind"] for r in rows}
    if not kinds:
        return 0, ("вузов в 700м не найдено" if university_only else "школ/садиков в 700м не найдено")
    has_school, has_kg, has_uni = "school" in kinds, "kindergarten" in kinds, "university" in kinds
    if university_only:
        if has_uni:
            return 2, "вуз рядом — арендный спрос студентов"
        return 0, "вузов в 700м не найдено (школы/садики — см. точный фактор по astana_schools/kindergartens)"
    if has_school and has_kg:
        return 5, "школа и садик в 700м — семейная локация"
    if has_school or has_kg:
        return 3, f"{'школа' if has_school else 'садик'} в 700м"
    if has_uni:
        return 2, "вуз рядом — арендный спрос студентов"
    return 0, "школ/садиков в 700м не найдено"


async def fetch_schools_poi(lat: float, lon: float) -> list[dict] | None:
    """Сырые точки школ/садиков/вузов с координатами (Фаза L2, docs/
    location_product_design.md, задача 2026-08-14) — для карты со слоями
    на /complex/{id} (bot/core/complex_location_detail.py). compute()
    выше отдаёт только агрегированный adj/reason, не список точек —
    эта функция параллельна ей, тем же путём (city_poi -> Overpass-кэш),
    но возвращает список, а не решение. None, если оба источника
    недоступны (не путать с пустым списком — "искали, не нашли")."""
    from bot.score_layers.osm import overpass_cached, element_coords, local_poi_near

    # local_poi_near — [] (синхронизировано, реально пусто) отличается от
    # None (категория ещё не синхронизирована) — раньше здесь было
    # "if rows:", что не различало эти два случая и на легитимно пустом
    # районе всё равно шло в Overpass (задача 2026-08-16, "Локальный
    # OSM-слой" — тот же класс бага, что был у _from_local_table выше).
    rows = await local_poi_near(lat, lon, _LOCAL_KINDS, 700)
    if rows is not None:
        return rows

    data = await overpass_cached(lat, lon, "schools", _QUERY.format(lat=lat, lon=lon))
    if data is None:
        return None
    out = []
    for el in data.get("elements", []):
        am = (el.get("tags") or {}).get("amenity")
        if am not in ("school", "kindergarten", "university"):
            continue
        coords = element_coords(el)
        if not coords or coords[0] is None:
            continue
        out.append({"kind": am, "lat": coords[0], "lon": coords[1]})
    return out


async def compute(listing: dict, university_only: bool = False) -> tuple[int, str]:
    """university_only — см. докстринг модуля. Дефолт False сохраняет
    старое поведение для всех потребителей, кроме location_score.py."""
    lat, lon = listing.get("lat"), listing.get("lon")
    if not lat or not lon:
        return 0, "нет координат"

    local = await _from_local_table(lat, lon, university_only)
    if local is not None:
        return local

    data = await overpass_cached(lat, lon, "schools",
                                 _QUERY.format(lat=lat, lon=lon))
    if data is None:
        return 0, "OSM недоступен"

    kinds = set()
    for el in data.get("elements", []):
        am = (el.get("tags") or {}).get("amenity")
        if am:
            kinds.add(am)

    has_school = "school" in kinds
    has_kg = "kindergarten" in kinds
    has_uni = "university" in kinds

    if university_only:
        if has_uni:
            return 2, "вуз рядом — арендный спрос студентов"
        return 0, "вузов в 700м не найдено (школы/садики — см. точный фактор по astana_schools/kindergartens)"

    if has_school and has_kg:
        return 5, "школа и садик в 700м — семейная локация"
    if has_school or has_kg:
        what = "школа" if has_school else "садик"
        return 3, f"{what} в 700м"
    if has_uni:
        return 2, "вуз рядом — арендный спрос студентов"
    return 0, "школ/садиков в 700м не найдено"
