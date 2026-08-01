"""
Гексагональный анализ цены (микролокальное сравнение вместо районного).

Модель (взвешенный индекс привлекательности цены):
  P_target   — цена/м² оцениваемой квартиры
  P0         — медиана цены/м² в её гексагоне (кольцо 0)
  P1         — медиана в 6 соседних гексагонах (кольцо 1)
  Pcity      — медиана по городу в том же сегменте
  w0=1.0, w1=0.7, w2=0.35 — веса; δ0/δ1 ∈ {0,1} — доступность данных:
    δ0=1 если в своём гексагоне ≥3 объявлений, δ1=1 если в кольце ≥5.

  P_expected = (w0·δ0·P0 + w1·δ1·P1 + w2·Pcity) / (w0·δ0 + w1·δ1 + w2)
  DealIndex  = P_expected / P_target
    DI > 1 — дешевле локального рынка, DI < 1 — дороже.

Сегментация: по комнатности (1 / 2 / 3 / 4+) — студии и 1-комнатные
системно дороже за м², чем 3-комнатные, сравнивать их вместе нельзя.
Сегментация по классу жилья добавится, когда korter-обогащение покроет
достаточно ЖК (класс есть в complexes, но не у каждого объявления).

Поправка к скору: adj = clamp(round((DI − 1) · 40), −8, +8)
  (5% дешевле локального ожидания → +2, 15% → +6, 20%+ → +8; дороже — минус)
Применяется дельтой (new_adj − old_adj), поэтому идемпотентна между циклами.
Районный компонент базового скора остаётся — он про макроуровень, гексагон
про микро; частичное пересечение осознанное.

Ребро сетки — ползунок HEX_EDGE_M (дефолт 50м). При 50м большинство
гексагонов пустые (это ~1 дом) — модель штатно опирается на кольцо/город;
при желании ребро поднимается до 100-120м без изменений кода.
"""
from __future__ import annotations

import json
import logging
from statistics import median

from bot.core.hexgrid import hex_id, neighbors

logger = logging.getLogger(__name__)

W0, W1, W2 = 1.0, 0.7, 0.35
MIN_HEX, MIN_RING = 3, 5


def _rooms_bucket(rooms) -> str:
    if not rooms or rooms <= 1:
        return "1"
    if rooms >= 4:
        return "4+"
    return str(int(rooms))


def compute_hex_prices(listings: list[dict], edge_m: float) -> dict[str, dict]:
    """
    listings: [{id, lat, lon, price, area, rooms}] — активные, без дублей.
    Возвращает {id: {di, expected_m2, adj, detail}}.
    """
    # Раскладываем по (сегмент, гекс)
    seg_hex_prices: dict[tuple[str, str], list[float]] = {}
    seg_city: dict[str, list[float]] = {}
    enriched = []
    for l in listings:
        if not l.get("lat") or not l.get("price") or not l.get("area"):
            continue
        p_m2 = float(l["price"]) / float(l["area"])
        if p_m2 <= 0:
            continue
        seg = _rooms_bucket(l.get("rooms"))
        hid = hex_id(float(l["lat"]), float(l["lon"]), edge_m)
        seg_hex_prices.setdefault((seg, hid), []).append(p_m2)
        seg_city.setdefault(seg, []).append(p_m2)
        enriched.append((l, seg, hid, p_m2))

    city_median = {seg: median(v) for seg, v in seg_city.items() if v}

    out: dict[str, dict] = {}
    for l, seg, hid, p_m2 in enriched:
        own = seg_hex_prices.get((seg, hid), [])
        # свой гекс без самой квартиры — чтобы не сравнивать сам с собой
        own_others = list(own)
        try:
            own_others.remove(p_m2)
        except ValueError:
            pass
        ring_prices: list[float] = []
        for nb in neighbors(hid):
            ring_prices.extend(seg_hex_prices.get((seg, nb), []))

        d0 = 1 if len(own_others) >= MIN_HEX else 0
        d1 = 1 if len(ring_prices) >= MIN_RING else 0
        p_city = city_median.get(seg)
        if not p_city:
            continue

        num = W2 * p_city
        den = W2
        if d0:
            num += W0 * median(own_others)
            den += W0
        if d1:
            num += W1 * median(ring_prices)
            den += W1
        expected = num / den
        di = expected / p_m2

        adj = max(-8, min(8, round((di - 1) * 40)))
        detail = {
            "di": round(di, 3),
            "expected_m2": round(expected),
            "actual_m2": round(p_m2),
            "hex_n": len(own_others),
            "ring_n": len(ring_prices),
            "segment": f"{seg}-комн",
            "edge_m": edge_m,
            "sources": ("гекс+кольцо+город" if d0 and d1 else
                        "кольцо+город" if d1 else
                        "гекс+город" if d0 else "только город"),
        }
        out[str(l["id"])] = {"di": di, "adj": adj,
                             "detail": json.dumps(detail, ensure_ascii=False)}
    return out


async def apply_hex_prices() -> int:
    """Считает Deal Index для всех активных объявлений с координатами и
    применяет дельту поправки к score_total. Возвращает число обновлённых."""
    from bot.db.pg import fetch, execute
    from bot.db import settings as app_settings

    edge = float(app_settings.get_int("HEX_EDGE_M", 50))
    rows = await fetch("""
        SELECT id, lat, lon, price, area, rooms,
               COALESCE(hex_price_adj, 0) AS old_adj
        FROM apartment_listings
        WHERE is_active IS NOT FALSE
          AND COALESCE(is_duplicate, FALSE) = FALSE
          AND lat IS NOT NULL AND price > 0 AND area > 0
    """)
    listings = [dict(r) for r in rows]
    result = compute_hex_prices(listings, edge)

    updated = 0
    for l in listings:
        res = result.get(str(l["id"]))
        if not res:
            continue
        delta = res["adj"] - l["old_adj"]
        if delta == 0 and res["adj"] == l["old_adj"]:
            # обновим только DI-детали, если поправка не изменилась — редко нужно
            continue
        await execute(
            """UPDATE apartment_listings
               SET hex_deal_index = $2, hex_price_adj = $3, hex_details = $4,
                   score_total = LEAST(100, GREATEST(0, COALESCE(score_total, 0) + $5))
               WHERE id = $1""",
            l["id"], round(res["di"], 3), res["adj"], res["detail"], delta)
        updated += 1
    logger.info("hex price: %d listings updated (edge=%dм, всего с коорд.: %d)",
                updated, int(edge), len(listings))
    return updated
