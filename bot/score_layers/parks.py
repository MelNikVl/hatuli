"""
Слой ПАРКИ: зелёные зоны в пешей доступности (OSM), 0..+2.
  парк <400м → +2
  парк <700м → +1

Детализация (задача 2026-08-15, "детализация parks.py"): reason теперь
не просто "парк рядом", а расстояние до БЛИЖАЙШЕГО + его площадь в га,
если удалось её посчитать. adj/пороги (400/700м) НЕ менялись — это
только про текст reason, площадь никак не влияет на adj (парк в 3 га и
парк в 300 га в 350м одинаково дают +2 — размер сейчас чисто
информационный, не калиброван как отдельный сигнал).

Площадь считается ТОЛЬКО для БЛИЖАЙШЕГО парка, отдельным точечным
Overpass-запросом по его OSM id (не для всех парков в радиусе 700 м —
общий поисковый запрос в poi.py достаточен для поиска/расстояния и не
раздувается геометрией ради данных, нужных только этому слою). Площадь
доступна не всегда:
  - ближайший парк размечен ТОЧКОЙ (node), не полигоном (way) — у точки
    площади в принципе нет, это честно, не ошибка;
  - Overpass недоступен именно для этого дозапроса;
  - геометрия пришла вырожденной (< 3 узлов).
Во всех этих случаях reason остаётся "парк в Nм" без указания площади —
Unknown ≠ average, не подставляем нулевую/угаданную площадь.
"""
from __future__ import annotations

from bot.score_layers.poi import fetch_poi

# Проекция градусы->метры вокруг Астаны (51°с.ш., cos(51°)≈0.63) — тот же
# коэффициент, что уже используется по всему проекту для дешёвой
# аппроксимации расстояний без точного haversine на каждый узел полигона
# (см. bot/core/location_score.py::_schools_factor и соседние SQL-запросы).
_M_PER_DEG_LAT = 111_000.0
_LON_SCALE = 0.63


def _polygon_area_m2(nodes: list[dict]) -> float | None:
    """Площадь простого многоугольника по формуле шнурков (shoelace) —
    nodes: [{lat, lon}, ...] в порядке обхода (как отдаёт Overpass
    `out geom`). None, если узлов меньше 3 (не многоугольник вовсе —
    вырожденная/неполная геометрия)."""
    pts = [(n["lat"], n["lon"]) for n in nodes if n and n.get("lat") is not None and n.get("lon") is not None]
    if len(pts) < 3:
        return None
    # Локальная плоская проекция от первого узла — числа меньше, точность
    # float не страдает от больших абсолютных координат (тот же приём,
    # что обычно применяется перед формулой шнурков на geo-данных).
    lat0, lon0 = pts[0]
    xy = [((lon - lon0) * _M_PER_DEG_LAT * _LON_SCALE, (lat - lat0) * _M_PER_DEG_LAT)
          for lat, lon in pts]
    area2 = 0.0
    n = len(xy)
    for i in range(n):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % n]
        area2 += x1 * y2 - x2 * y1
    return abs(area2) / 2.0


async def _nearest_park_area_ha(park: dict) -> float | None:
    """Площадь ближайшего парка в га.

    Задача 2026-08-17 ("Parks — исправить потерю площади после перехода
    на локальные точки"): основной путь теперь — area_ha, уже посчитанный
    ОДИН РАЗ при еженедельной sync_city_poi.py (см. scripts/sync_city_poi
    .py::_fetch_park_points_with_area, bot/score_layers/poi.py::fetch_poi)
    и пришедший прямо в `park` из city_poi — БЕЗ сети. Раньше (до этой
    задачи) единственный путь был точечный live-Overpass-дозапрос
    геометрии по OSM id на КАЖДОМ расчёте локации — с переходом на
    city_poi local-записи получали id=None/type=None, поэтому guard
    "type != way" всегда срабатывал и площадь МОЛЧА переставала
    находиться почти для всех ЖК (баг, найденный этой же задачей).

    Live-дозапрос ОСТАВЛЕН как fallback ТОЛЬКО для редкого случая, когда
    fetch_poi() целиком пошёл по live-Overpass пути (park кроме area_ha
    несёт live id/type, area_ha никогда не приходит с этой ветки) — не
    вводим сюда сетевой запрос заново для обычного, local-пути (Overpass
    в критический путь скоринга не возвращается, задача явно)."""
    if park.get("area_ha") is not None:
        return park["area_ha"]
    if park.get("type") != "way" or not park.get("id"):
        return None
    from bot.score_layers.osm import overpass_cached

    query = "[out:json][timeout:15];way(%d);out geom;" % park["id"]
    data = await overpass_cached(park["lat"], park["lon"], "park_geom", query)
    if not data:
        return None
    elements = data.get("elements") or []
    if not elements:
        return None
    geometry = elements[0].get("geometry") or []
    area_m2 = _polygon_area_m2(geometry)
    if area_m2 is None:
        return None
    return area_m2 / 10_000.0


async def compute(listing: dict) -> tuple[int, str]:
    lat, lon = listing.get("lat"), listing.get("lon")
    if not lat or not lon:
        return 0, "нет координат"
    poi = await fetch_poi(lat, lon)
    if poi is None:
        return 0, "OSM недоступен"
    parks = [p for p in poi if p["kind"] == "park"]
    if not parks:
        return 0, "парков в 700м нет"
    nearest = min(parks, key=lambda p: p["dist_m"])
    d = nearest["dist_m"]
    adj = 2 if d <= 400 else 1

    area_ha = await _nearest_park_area_ha(nearest)
    if area_ha is not None:
        return adj, f"парк в {d:.0f}м, площадь ~{area_ha:.1f} га"
    return adj, f"парк в {d:.0f}м"
