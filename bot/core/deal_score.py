"""
Deal Score v3 — единый многофакторный скоринг привлекательности сделки.

Синтез подходов (по материалам анализа 4 моделей):
  1. Hedonic-ядро: ожидаемая цена P_expected строится НЕ из ручных весов,
     а из локальной рыночной медианы (гексагон + кольцо + город, δ-доступность).
     Deal Index = P_expected / P_ask — цена участвует только в сравнении
     с ожиданием, а не «весом в скоре».
  2. Субиндексы 0–100 с прозрачными весами (как у ЦИАН/Zillow):
     Price 40% · Location 20% · Quality 20% · Market 15% · Risk 5%.
  3. Эмпирическая калибровка: класс жилья, год постройки, рейтинг ЖК
     (korter/homsters/krisha), yield из реальной аренды.
  4. Confidence: доля компонентов, посчитанных на реальных данных —
     низкий confidence = оценке доверяем меньше (показываем в UI).

Эмпирические эффекты исследования НБРК (левый берег +11.3%, монолит +11.8%
и т.п.) НЕ добавляются вручную: гексагональные медианы уже содержат
локальные премии/дисконты — ручное добавление дало бы двойной учёт.

Записывает: score_total (=deal), hex_deal_index, hex_details (json с
компонентами), deal_confidence, hex_price_adj=0 (старая ±8 поправка
больше не нужна — цена теперь компонента, а не дельта).
Первичку (market_type='primary') не трогаем — там своя модель.
"""
from __future__ import annotations

import json
import logging
from statistics import median

from bot.core.hexgrid import hex_id, neighbors

logger = logging.getLogger(__name__)

# ── Веса компонентов (сумма = 1.0) ────────────────────────────────────────────
W_PRICE, W_LOC, W_QUALITY, W_MARKET, W_RISK = 0.40, 0.20, 0.20, 0.15, 0.05

# δ-доступность гекс-модели
W0, W1, W2 = 1.0, 0.7, 0.35
MIN_HEX, MIN_RING = 3, 5

_CLASS_SCORE = {"элит": 100, "бизнес": 80, "комфорт": 60, "эконом": 35}
_DISTRICT_FALLBACK = {
    "есиль": 80, "алматы": 65, "сарыарка": 50, "байконур": 45, "нура": 30,
}


def _rooms_bucket(rooms) -> str:
    if not rooms or rooms <= 1:
        return "1"
    if rooms >= 4:
        return "4+"
    return str(int(rooms))


def _clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def _year_score(y):
    if not y:
        return None
    if y >= 2022:
        return 100
    if y >= 2018:
        return 80
    if y >= 2012:
        return 60
    if y >= 2000:
        return 40
    return 20


def compute_deal_scores(listings: list[dict], complexes: dict[str, dict],
                        edge_m: float) -> dict[str, dict]:
    """
    listings: [{id, lat, lon, price, area, rooms, floor, floors_total,
                year_built, complex_name, is_owner, first_seen_days, yield_pct,
                same_complex_cnt, district}]
    complexes: {lower(trim(name)): {housing_class, year_built, krisha_rating}}
    Возвращает {id: {deal, confidence, di, expected_m2, components{...}}}
    """
    # ── Гекс-агрегации (hedonic ядро) ────────────────────────────────────────
    seg_hex: dict[tuple[str, str], list[float]] = {}
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
        seg_hex.setdefault((seg, hid), []).append(p_m2)
        seg_city.setdefault(seg, []).append(p_m2)
        enriched.append((l, seg, hid, p_m2))
    city_med = {s: median(v) for s, v in seg_city.items() if v}

    out: dict[str, dict] = {}
    for l, seg, hid, p_m2 in enriched:
        own = list(seg_hex.get((seg, hid), []))
        try:
            own.remove(p_m2)
        except ValueError:
            pass
        ring: list[float] = []
        for nb in neighbors(hid):
            ring.extend(seg_hex.get((seg, nb), []))
        d0 = len(own) >= MIN_HEX
        d1 = len(ring) >= MIN_RING
        p_city = city_med.get(seg)
        if not p_city:
            continue
        num, den = W2 * p_city, W2
        if d0:
            num += W0 * median(own)
            den += W0
        if d1:
            num += W1 * median(ring)
            den += W1
        expected = num / den
        di = expected / p_m2
        sources = ("гекс+кольцо+город" if d0 and d1 else
                   "кольцо+город" if d1 else "гекс+город" if d0 else "только город")

        # ── 1. PRICE (40%): DI → 0..100 ─────────────────────────────────────
        price_score = _clamp(round(50 + (di - 1) * 200))
        pct = round((di - 1) * 100)
        price_txt = (f"на {abs(pct)}% дешевле локального ожидания" if pct > 2 else
                     f"на {abs(pct)}% дороже локального ожидания" if pct < -2 else
                     "цена на уровне локального рынка")

        # ── 2. LOCATION (20%): уровень цен гексагона vs город ───────────────
        loc_ratio = expected / p_city
        loc_score = _clamp(round(50 + (loc_ratio - 1) * 100), 5, 100)
        loc_txt = (f"локация дороже городской медианы на {round((loc_ratio-1)*100)}%"
                   if loc_ratio > 1.02 else
                   f"локация дешевле городской медианы на {round((1-loc_ratio)*100)}%"
                   if loc_ratio < 0.98 else "локация на уровне города")

        # ── 3. QUALITY (20%): класс ЖК + год + рейтинг Крыши ────────────────
        cx = complexes.get((l.get("complex_name") or "").strip().lower()) or {}
        parts, wsum = [], 0.0
        cls = (cx.get("housing_class") or "").lower()
        cls_score = next((v for k, v in _CLASS_SCORE.items() if k in cls), None)
        if cls_score is not None:
            parts.append(cls_score * 0.45)
            wsum += 0.45
        yr = l.get("year_built") or cx.get("year_built")
        ys = _year_score(yr)
        if ys is not None:
            parts.append(ys * 0.35)
            wsum += 0.35
        rating = cx.get("krisha_rating")
        if rating:
            parts.append(rating / 5 * 100 * 0.20)
            wsum += 0.20
        if wsum:
            quality_score = round(sum(parts) / wsum)
            q_bits = []
            if cls_score is not None:
                q_bits.append(f"класс «{cls}»")
            if ys is not None:
                q_bits.append(f"{yr} г.")
            if rating:
                q_bits.append(f"⭐ {rating}")
            quality_txt = ", ".join(q_bits)
        else:
            quality_score, quality_txt = 50, "нет данных о ЖК (дефолт)"

        # ── 4. MARKET (15%): yield + ликвидность ────────────────────────────
        yp = float(l.get("yield_pct") or 0)
        yield_sc = _clamp(round(yp / 15 * 100))
        supply = l.get("same_complex_cnt") or 1
        supply_sc = 90 if supply <= 3 else (60 if supply <= 8 else 30)
        age = l.get("first_seen_days")
        age_sc = (90 if age <= 7 else 60 if age <= 30 else 35) if age is not None else 50
        market_score = round(yield_sc * 0.6 + (supply_sc * 0.5 + age_sc * 0.5) * 0.4)
        market_txt = (f"yield {yp}%" if yp else "нет данных аренды") + \
                     f", в ЖК {supply} объявл."

        # ── 5. RISK (5%): штрафы ────────────────────────────────────────────
        risk, risk_bits = 100, []
        fl, flt = l.get("floor"), l.get("floors_total")
        if fl == 1:
            risk -= 40
            risk_bits.append("1й этаж")
        elif fl and flt and fl == flt:
            risk -= 25
            risk_bits.append("последний этаж")
        if l.get("is_owner") is False:
            risk -= 30
            risk_bits.append("риелтор (+комиссия)")
        risk_score = _clamp(risk)
        risk_txt = ", ".join(risk_bits) if risk_bits else "флагов нет"

        deal = round(price_score * W_PRICE + loc_score * W_LOC +
                     quality_score * W_QUALITY + market_score * W_MARKET +
                     risk_score * W_RISK)

        # ── Confidence: сколько компонентов на реальных данных ──────────────
        conf = 0
        conf += 30 if (d0 or d1) else 0           # локальная ценовая модель есть
        conf += 10 if d0 else 0                   # свой гексагон не пуст
        conf += 20 if cls_score is not None else 0
        conf += 15 if ys is not None else 0
        conf += 15 if yp else 0
        conf += 10 if rating else 0
        conf = min(conf, 100)

        out[str(l["id"])] = {
            "deal": deal,
            "confidence": conf,
            "di": round(di, 3),
            "expected_m2": round(expected),
            "actual_m2": round(p_m2),
            "hex_n": len(own), "ring_n": len(ring),
            "segment": f"{seg}-комн", "edge_m": edge_m, "sources": sources,
            "components": {
                "price": {"score": price_score, "weight": W_PRICE, "text": price_txt},
                "location": {"score": loc_score, "weight": W_LOC, "text": loc_txt},
                "quality": {"score": quality_score, "weight": W_QUALITY, "text": quality_txt},
                "market": {"score": market_score, "weight": W_MARKET, "text": market_txt},
                "risk": {"score": risk_score, "weight": W_RISK, "text": risk_txt},
            },
            "version": 3,
        }
    return out


async def apply_deal_scores() -> int:
    """Считает Deal Score v3 для всех активных вторичных объявлений и
    записывает score_total (=deal), компоненты и confidence."""
    from bot.db.pg import fetch, execute
    from bot.db import settings as app_settings

    await execute("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS deal_confidence INT")

    edge = float(app_settings.get_int("HEX_EDGE_M", 50))
    rows = await fetch("""
        SELECT id, lat, lon, price, area, rooms, floor, floors_total,
               year_built, complex_name, is_owner, district, yield_pct,
               EXTRACT(EPOCH FROM (now() - first_seen)) / 86400 AS first_seen_days,
               (SELECT COUNT(*) FROM apartment_listings s
                 WHERE lower(trim(s.complex_name)) = lower(trim(a.complex_name))
                   AND s.is_active IS NOT FALSE AND a.complex_name IS NOT NULL
                   AND btrim(a.complex_name) != '') AS same_complex_cnt
        FROM apartment_listings a
        WHERE is_active IS NOT FALSE
          AND COALESCE(is_duplicate, FALSE) = FALSE
          AND market_type IS DISTINCT FROM 'primary'
          AND price > 0 AND area > 0
    """)
    listings = [dict(r) for r in rows]
    if not listings:
        return 0

    cx_rows = await fetch(
        "SELECT name, housing_class, year_built, source_info FROM complexes")
    complexes: dict[str, dict] = {}
    for c in cx_rows:
        si = c["source_info"]
        if isinstance(si, str):
            try:
                si = json.loads(si)
            except ValueError:
                si = {}
        si = si or {}
        kr = si.get("krisha") or {}
        complexes[(c["name"] or "").strip().lower()] = {
            "housing_class": c["housing_class"],
            "year_built": c["year_built"],
            "krisha_rating": kr.get("rating"),
        }

    result = compute_deal_scores(listings, complexes, edge)

    updated = 0
    for lid, r in result.items():
        await execute("""
            UPDATE apartment_listings
            SET score_total = $2, deal_confidence = $3,
                hex_deal_index = $4, hex_details = $5::jsonb, hex_price_adj = 0
            WHERE id = $1
        """, lid, r["deal"], r["confidence"], r["di"],
            json.dumps(r, ensure_ascii=False))
        updated += 1
    logger.info("deal score v3: %d listings (edge=%dм)", updated, int(edge))
    return updated
