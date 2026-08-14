"""Сборка данных для блока «🗺 Локация» на странице ЖК (Фаза L2
продуктового трека «Локация», docs/location_product_design.md, задача
2026-08-14) — /admin/api/complex/{id}/location-detail.

Дополняет существующую живую карточку «Что рядом»
(/admin/api/complex/{id}/location-score, bot/core/location_score.py —
НЕ меняется, РЕШЕНИЕ 2 плана L1) данными из Фазы L1:
  - последний снимок `complex_location_scores` (группированный скор);
  - соседние гексагоны `hex_market_stats` (плотность/перенасыщение);
  - `demolition_houses` в радиусе;
  - точки POI/школ — читаются через уже кэш-aware fetch_poi()/
    fetch_schools_poi() (bot/score_layers/{poi,schools}.py) — HIT кэша
    в большинстве случаев (та же ячейка, что уже дёргалась при расчёте
    location-score), без нового живого Overpass-запроса specifically
    для карты;
  - тренд `complex_stats_history.price_drop_share_30d` по дням.

**Явно НЕ включает цену/DOM отдельными временными рядами** — те уже
показаны на странице (price-dynamics/turnover-dynamics графики),
дублировать нельзя (план L2, п.3).

Unknown ≠ average (docs/verdict_strategy.md §3.1): нет строки в
`complex_location_scores` (backfill ещё не дошёл до этого ЖК) ->
`has_score=False`, не подделываем и не ждём."""
from __future__ import annotations

import json


class ComplexNotFound(Exception):
    """complex_id не найден в complexes."""


DEMOLITION_RADIUS_KM = 1.0


async def build_complex_location_detail(complex_id: int) -> dict:
    from bot.db.pg import fetchrow, fetch
    from bot.core.house_resolution import resolve_complex_geo_centroid
    from bot.core.hexgrid import hex_id as compute_hex_id, neighbors as hex_neighbors, hex_corners
    from bot.core.geo import haversine_km
    from bot.db import settings as app_settings

    cx = await fetchrow("SELECT name FROM complexes WHERE id = $1", complex_id)
    if not cx:
        raise ComplexNotFound(complex_id)

    centroid = await resolve_complex_geo_centroid(complex_id, cx["name"])
    if centroid is None:
        return {
            "has_coords": False, "has_score": False, "score": None,
            "density": [], "demolition": [], "poi": {}, "price_drop_trend": [],
        }
    lat, lon = centroid

    score = await _build_score(complex_id)
    density = await _build_density(lat, lon, fetch, app_settings, compute_hex_id, hex_neighbors, hex_corners)
    demolition = await _build_demolition(lat, lon, fetch, haversine_km)
    poi = await _build_poi(lat, lon)
    price_drop_trend = await _build_price_drop_trend(complex_id, fetch)

    return {
        "has_coords": True,
        "has_score": score is not None,
        "score": score,
        "density": density,
        "demolition": demolition,
        "poi": poi,
        "price_drop_trend": price_drop_trend,
    }


async def _build_score(complex_id: int) -> dict | None:
    from bot.db.pg import fetchrow
    row = await fetchrow("""
        SELECT score, confidence, transport_score, infra_score, noise_score,
               green_score, risk_score, breakdown, computed_at
        FROM complex_location_scores WHERE complex_id=$1
        ORDER BY computed_at DESC LIMIT 1
    """, complex_id)
    if not row:
        return None
    breakdown = row["breakdown"]
    if isinstance(breakdown, str):
        breakdown = json.loads(breakdown)
    return {
        "score": row["score"], "confidence": row["confidence"],
        "transport_score": row["transport_score"], "infra_score": row["infra_score"],
        "noise_score": row["noise_score"], "green_score": row["green_score"],
        "risk_score": row["risk_score"], "breakdown": breakdown,
        "computed_at": row["computed_at"].strftime("%d.%m.%Y") if row["computed_at"] else None,
    }


async def _build_density(lat, lon, fetch, app_settings, compute_hex_id, hex_neighbors, hex_corners) -> list[dict]:
    """Свой гексагон + кольцо соседей (7 ячеек) — та же гранулярность,
    что уже используется everywhere в скоринге (deal_score.py/
    bargain.py: own_hex + ring)."""
    edge_m = float(app_settings.get_int("HEX_EDGE_M", 50))
    center_hex = compute_hex_id(lat, lon, edge_m)
    hex_ids = [center_hex] + hex_neighbors(center_hex)

    rows = await fetch("""
        SELECT DISTINCT ON (hex_id) hex_id, listings_count, avg_price_m2, date
        FROM hex_market_stats WHERE hex_id = ANY($1::text[])
        ORDER BY hex_id, date DESC
    """, hex_ids)
    by_hex = {r["hex_id"]: r for r in rows}

    out = []
    for hid in hex_ids:
        r = by_hex.get(hid)
        corners = hex_corners(hid, edge_m)
        out.append({
            "hex_id": hid,
            "is_center": hid == center_hex,
            "corners": [[round(c[0], 6), round(c[1], 6)] for c in corners],
            "listings_count": r["listings_count"] if r else None,
            "avg_price_m2": float(r["avg_price_m2"]) if r and r["avg_price_m2"] is not None else None,
        })
    return out


async def _build_demolition(lat, lon, fetch, haversine_km) -> list[dict]:
    rows = await fetch(
        "SELECT address, demolish_year, lat, lon FROM demolition_houses WHERE lat IS NOT NULL AND lon IS NOT NULL")
    out = []
    for r in rows:
        dist_km = haversine_km(lat, lon, float(r["lat"]), float(r["lon"]))
        if dist_km <= DEMOLITION_RADIUS_KM:
            out.append({
                "address": r["address"], "demolish_year": r["demolish_year"],
                "dist_m": round(dist_km * 1000), "lat": float(r["lat"]), "lon": float(r["lon"]),
            })
    out.sort(key=lambda d: d["dist_m"])
    return out


async def _build_poi(lat, lon) -> dict:
    """Читает через уже кэш-aware fetch_poi()/fetch_schools_poi() — не
    новый прямой Overpass-запрос специально для карты, HIT в
    большинстве случаев (та же ячейка, что уже посчитана location-score)."""
    from bot.score_layers.poi import fetch_poi
    from bot.score_layers.schools import fetch_schools_poi

    poi_points = await fetch_poi(lat, lon) or []
    school_points = await fetch_schools_poi(lat, lon) or []

    out: dict[str, list] = {"bus_stop": [], "shop": [], "health": [], "food": [],
                             "service": [], "park": [], "school": []}
    for p in poi_points:
        out.setdefault(p["kind"], []).append({"lat": p["lat"], "lon": p["lon"]})
    for s in school_points:
        out["school"].append({"lat": s["lat"], "lon": s["lon"], "kind": s["kind"]})
    return out


async def _build_price_drop_trend(complex_id: int, fetch) -> list[dict]:
    rows = await fetch("""
        SELECT date, price_drop_share_30d FROM complex_stats_history
        WHERE complex_id=$1 AND price_drop_share_30d IS NOT NULL
        ORDER BY date ASC
    """, complex_id)
    return [{"date": r["date"].strftime("%Y-%m-%d"), "share": float(r["price_drop_share_30d"])} for r in rows]
