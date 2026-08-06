"""
Отделка, аргументы для торга и вопросы продавцу.

1. detect_finish_level(text) — определяет отделку по ключевым словам
   в заголовке/описании: черновая / предчистовая / чистовая / ремонт / мебель.
   Возвращает (level_code, score_adjustment).

2. build_negotiation_points(listing, bargain) — конкретные зацепки для торга
   по данным этого объявления.

3. build_seller_questions(listing) — недостающая информация + вопросы,
   реально влияющие на жизнь и комфорт (шум, соседи, ОСИ, парковка...).
"""
from __future__ import annotations

from datetime import datetime, timezone

# (ключевые слова, код, поправка к скору, человекочитаемо)
_FINISH_RULES = [
    (("черновая", "без отделки", "черновой"), "rough", -5, "черновая"),
    (("предчистовая", "улучшенная черновая", "white box"), "prefinish", -2, "предчистовая"),
    (("дизайнерский ремонт", "дизайнерская", "премиум ремонт"), "designer", +6, "дизайнерский ремонт"),
    (("с мебелью", "мебель остается", "мебель остаётся", "полностью укомплект"), "furnished", +5, "с мебелью"),
    (("евроремонт", "свежий ремонт", "после ремонта", "новый ремонт"), "renovated", +4, "свежий ремонт"),
    (("чистовая", "с отделкой", "готова к заселению", "заезжай и живи"), "finished", +3, "чистовая"),
    (("требует ремонта", "под ремонт", "состояние среднее"), "needs_repair", -4, "требует ремонта"),
]


def detect_finish_level(*texts: str | None) -> tuple[str | None, int, str | None]:
    """Возвращает (code, score_adj, label) или (None, 0, None)."""
    blob = " ".join(t.lower() for t in texts if t)
    if not blob:
        return None, 0, None
    for keywords, code, adj, label in _FINISH_RULES:
        if any(k in blob for k in keywords):
            return code, adj, label
    return None, 0, None


def build_negotiation_points(listing: dict, bargain: dict | None,
                             comps_count: int = 0) -> list[str]:
    """Конкретные аргументы для торга по этому объявлению."""
    points: list[str] = []
    price = listing.get("price") or 0

    # 1. Время на рынке
    first_seen = listing.get("first_seen")
    if first_seen:
        if first_seen.tzinfo is None:
            first_seen = first_seen.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - first_seen).days
        if days >= 60:
            points.append(f"Объявление висит уже {days} дней — продавец скорее всего устал ждать. Смело торгуйся от 7–10%.")
        elif days >= 30:
            points.append(f"На рынке {days} дней — дольше среднего. Уместен торг 5–7%.")

    # 2. Цена против аналогов
    if bargain and bargain.get("median_price") and price:
        median = bargain["median_price"]
        diff_pct = (price - median) / median * 100
        if diff_pct > 7:
            points.append(f"Цена на {diff_pct:.0f}% выше медианы аналогов ({median:,.0f} ₸ по {comps_count} похожим) — покажи продавцу выборку с Крыши и торгуйся к медиане.".replace(",", " "))
        elif diff_pct < -7:
            points.append(f"Цена уже на {abs(diff_pct):.0f}% ниже медианы аналогов — сильно продавить не выйдет, но 2–3% «за быструю сделку» попросить можно.")

    # 3. Температура рынка
    temp = (bargain or {}).get("market_temp")
    if temp == "cold":
        points.append("Рынок в этом сегменте холодный (много старых объявлений) — время на стороне покупателя.")
    elif temp == "hot":
        points.append("Сегмент горячий, свежие объявления уходят быстро — сильно торговаться рискованно, можно упустить.")

    # 4. Продавец
    if listing.get("is_owner"):
        points.append("Продаёт собственник — нет комиссии риелтора, и решение он принимает сам: аргументы «живыми деньгами, быстрый выход на сделку» работают лучше всего.")
    else:
        points.append("Продаёт риелтор — торгуйся и по цене, и по комиссии; уточни, кто её платит.")

    # 5. Отделка
    fl = listing.get("finish_level")
    if fl == "rough":
        points.append("Черновая отделка: посчитай ремонт (~80–150к ₸/м²) и вычитай из цены при торге — это сильный аргумент.")
    elif fl == "needs_repair":
        points.append("Требует ремонта — смета ремонта это готовый аргумент для скидки.")

    # 6. Новостройка не сдана
    year = listing.get("year_built")
    if year and year >= 2026:
        points.append(f"Дом {year} года — если ещё не сдан, торгуйся за риск переноса сроков и уточни неустойки по договору.")

    # 7. Этаж
    floor, floors_total = listing.get("floor"), listing.get("floors_total")
    if floor is not None and floors_total:
        if floor == 1:
            points.append("Первый этаж традиционно дисконтируется на 3–7% — если цена как у средних этажей, это аргумент.")
        elif floor == floors_total:
            points.append("Последний этаж: спроси про состояние крыши/техэтаж — любой нюанс это скидка.")

    # 8. Наличка на руках — универсальный аргумент
    points.append("Если у вас есть наличные на руках — можно попросить скидку ~10% от цены за скорость и удобство сделки для продавца.")

    if not points:
        points.append("Явных зацепок в данных нет — торгуйся стандартно: осмотр, мелкие недостатки, готовность к быстрой сделке (2–4%).")
    return points


# Базовые вопросы, влияющие на жизнь (не зависят от данных)
_BASE_QUESTIONS = [
    "Насколько шумно? Какие окна выходят на дорогу, что слышно вечером и утром?",
    "Какая звукоизоляция между квартирами — слышно ли соседей?",
    "Кто соседи сверху/снизу/по площадке? Есть ли проблемные?",
    "Сколько квартир на этаже и сколько лифтов? Часто ли ломаются?",
    "Взносы ОСИ/КСК: сколько в месяц, что входит, есть ли целевые сборы?",
    "Отопление зимой: тепло ли, сколько выходит коммуналка в январе?",
    "Напор воды и электрика: выбивает ли пробки, хватает ли мощности?",
    "Парковка: есть ли место, сколько стоит купить/арендовать в этом доме?",
    "Почему продаёте? Как срочно нужны деньги?",
    "Документы: кто собственник, есть ли обременения, несовершеннолетние в долях, узаконена ли перепланировка?",
]


def build_seller_questions(listing: dict) -> list[str]:
    """Недостающая информация в объявлении + базовые вопросы."""
    missing: list[str] = []
    if not listing.get("year_built"):
        missing.append("Год постройки дома не указан — уточни (и заодно материал стен).")
    if not listing.get("floor"):
        missing.append("Этаж не указан — уточни этаж и этажность.")
    if not listing.get("finish_level"):
        missing.append("Состояние/отделка не указаны — черновая, чистовая, с ремонтом?")
    if not listing.get("complex_name"):
        missing.append("ЖК не указан — уточни название комплекса и управляющую компанию.")
    if not listing.get("is_owner"):
        missing.append("Продавец не подтверждён как собственник — спроси, кто продаёт и какая комиссия.")
    if not listing.get("floorplan_url"):
        missing.append("В объявлении нет плана квартиры — запроси у продавца.")
    return missing + _BASE_QUESTIONS


async def compute_similar_listings(listing: dict, listing_id: str, limit: int = 10) -> list[dict]:
    """10 (или limit) похожих вариантов — приоритет: тот же ЖК -> тот же/
    соседний гексагон (~300м) -> просто похожая цена по городу. Общая логика
    для страницы объявления (/admin/analytics/{id}) и большого попапа на
    карте (/admin/api/listing/{id}) — раньше жила только в admin_web.py,
    вынесена сюда, чтобы не дублировать между двумя местами."""
    import json as _json

    from bot.db.pg import fetch as pg_fetch

    similar_listings: list[dict] = []
    if not (listing.get("rooms") and listing.get("price")):
        return similar_listings

    sim_rows = []
    if listing.get("complex_name"):
        sim_rows = list(await pg_fetch("""
            SELECT id, url, price, area, floor, floors_total, district,
                   complex_name, photos, lat, lon
            FROM apartment_listings
            WHERE rooms = $1 AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
              AND id != $2 AND lower(trim(complex_name)) = lower(trim($3))
            ORDER BY ABS(price - $4) ASC
            LIMIT $5
        """, listing["rooms"], listing_id, listing["complex_name"], listing["price"], limit))

    if listing.get("lat") and listing.get("lon"):
        from bot.core.hexgrid import hex_id as _hex_id, neighbors as _hex_nb
        HEX_EDGE = 300.0
        my_hid = _hex_id(float(listing["lat"]), float(listing["lon"]), HEX_EDGE)
        ring1 = set(_hex_nb(my_hid))
        ring2 = set()
        for h in ring1:
            ring2.update(_hex_nb(h))
        wanted_hids = {my_hid} | ring1 | ring2
        exclude_ids = [listing_id] + [r["id"] for r in sim_rows]
        candidates = await pg_fetch("""
            SELECT id, url, price, area, floor, floors_total, district,
                   complex_name, photos, lat, lon
            FROM apartment_listings
            WHERE rooms = $1 AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
              AND lat IS NOT NULL AND NOT (id = ANY($2::text[]))
            ORDER BY ABS(price - $3) ASC
            LIMIT 1500
        """, listing["rooms"], exclude_ids, listing["price"])
        for c in candidates:
            if len(sim_rows) >= limit:
                break
            if _hex_id(float(c["lat"]), float(c["lon"]), HEX_EDGE) in wanted_hids:
                sim_rows.append(c)

    for r in sim_rows[:limit]:
        sp = r["photos"]
        if isinstance(sp, str):
            try:
                sp = _json.loads(sp)
            except ValueError:
                sp = []
        similar_listings.append({
            "id": r["id"], "url": r["url"], "price": r["price"],
            "area": float(r["area"]) if r["area"] else None,
            "floor": r["floor"], "floors_total": r["floors_total"],
            "district": r["district"], "complex_name": r["complex_name"],
            "photo": (sp or [None])[0],
            "lat": float(r["lat"]) if r["lat"] else None,
            "lon": float(r["lon"]) if r["lon"] else None,
        })
    return similar_listings
