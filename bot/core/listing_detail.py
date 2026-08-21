"""Сборка ответа для модалки объявления (/admin/api/listing/{id}) и для
истории цены (/admin/api/price-history/{id}) — вынесено из
terminal_extras.py (Фаза B, п.5, задача 2026-08-14, docs/verdict_strategy.md,
"роут не знает SQL"). Роуты остаются тонкими: разбирают request/тир,
зовут функцию отсюда, оборачивают результат/исключение в JSONResponse.
Поведение НЕ менялось — 1:1 перенос логики, только разложено по слоям.

Публичному тиру — только новостройки (market_type='primary'), вторичка
отдаётся ограниченным ответом (задача "общий доступ", 2026-08-12: это и
есть точка реальной утечки, из-за которой заход по прямой ссылке на
объявление вторички показывал всё)."""
from __future__ import annotations

import json
from datetime import datetime, timezone


class ListingNotFound(Exception):
    """listing_id не найден в apartment_listings — роут переводит в 404."""


class ListingRestricted(Exception):
    """Публичному тиру закрыт полный доступ к этому объявлению (не
    новостройка) — роут переводит в 403."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


RESTRICTED_MESSAGE = (
    "Публично открыт только раздел новостроек. Полный доступ к остальным "
    "объявлениям открывает администратор — войдите через Telegram (Личный "
    "кабинет) и запросите доступ."
)


async def build_listing_detail(listing_id: str, tier: str) -> dict:
    """Полные данные объявления для модалки (фото, адрес, торг, похожие).
    tier уже посчитан вызывающим роутом (он один знает про Request/куки) —
    сюда приходит строкой, эта функция про HTTP ничего не знает.

    Бросает ListingNotFound / ListingRestricted вместо статус-кодов —
    только роут решает, как исключение превращается в HTTP-ответ."""
    from bot.db.pg import fetchrow as pg_fetchrow
    from bot.core.bargain import get_comparables, analyze_bargain
    from bot.core.listing_intel import build_negotiation_points, build_seller_questions, compute_similar_listings

    row = await pg_fetchrow("SELECT * FROM apartment_listings WHERE id = $1", listing_id)
    if not row:
        raise ListingNotFound(listing_id)
    l = dict(row)

    if tier == "public":
        # См. bot/core/listing_detail.py докстринг — apartment_listings
        # (личные объявления) публичному тиру не показывается вовсе, даже
        # market_type='primary'. Новостройки — отдельная таблица
        # newbuild_units, свой роут (/admin/api/newbuild-unit/{id}).
        raise ListingRestricted(RESTRICTED_MESSAGE)

    photos = l.get("photos")
    if isinstance(photos, str):
        try:
            photos = json.loads(photos)
        except ValueError:
            photos = []

    comps, comps_meta = await get_comparables(
        lat=float(l["lat"]) if l.get("lat") is not None else None,
        lon=float(l["lon"]) if l.get("lon") is not None else None,
        rooms=l.get("rooms"), area=l.get("area"), current_price=l.get("price", 0),
        complex_name=l.get("complex_name"), district=l.get("district"),
        exclude_id=listing_id,
    )
    bargain = analyze_bargain(l.get("price", 0), comps, l.get("is_owner"), l.get("is_urgent") is True, comps_meta)

    negotiation_points = build_negotiation_points(l, bargain, len(comps))
    seller_questions = build_seller_questions(l)
    # Публичному тиру не показываем "похожие рядом" (карта+список) — ни в
    # попапе с карты, ни в попапе, открытом через "вставить ссылку с
    # Крыши" (задача "3 уровня доступа", 2026-08-07). Не считаем вовсе, а
    # не просто прячем в шаблоне — не тратим время на compute для тира,
    # которому это всё равно не покажется. tier здесь уже не может быть
    # "public" (см. raise ListingRestricted выше), условие оставлено 1:1
    # с исходным кодом ради минимальной дельты при переносе.
    similar_listings = [] if tier == "public" else await compute_similar_listings(l, listing_id, limit=10)

    layers = l.get("layer_details")
    if isinstance(layers, str):
        try:
            layers = json.loads(layers)
        except ValueError:
            layers = None

    ai_analysis = l.get("ai_analysis")
    if isinstance(ai_analysis, str):
        try:
            ai_analysis = json.loads(ai_analysis)
        except ValueError:
            ai_analysis = None

    # Фото ЖК — для галереи в модалке объявления (переиспользуем то же
    # поле photos, что и на карточке ЖК). Заодно резолвим id/developer_id
    # для kzk_badge ниже — тот же lookup, не дублируем запрос.
    complex_photos = []
    kzk_badge = None
    complex_housing_class = None
    if l.get("complex_name"):
        cx_row = await pg_fetchrow(
            "SELECT id, developer_id, photos, photo_url, housing_class FROM complexes "
            "WHERE lower(trim(name)) = lower(trim($1)) LIMIT 1",
            l["complex_name"])
        if cx_row:
            complex_housing_class = cx_row.get("housing_class")
            cxp = cx_row["photos"]
            if isinstance(cxp, str):
                try:
                    cxp = json.loads(cxp)
                except ValueError:
                    cxp = None
            complex_photos = cxp or ([cx_row["photo_url"]] if cx_row.get("photo_url") else [])

            # Риск-бейдж БВУ/КЖК/МИО (задача 2026-08-15) — только для
            # первички (l.market_type='primary'), см. bot/core/complex_
            # detail.py::get_kzk_info() докстринг про has_signal/резолюцию.
            if l.get("market_type") == "primary":
                from bot.core.complex_detail import get_kzk_info
                kzk_info = await get_kzk_info(cx_row["id"], cx_row.get("developer_id"))
                if kzk_info and kzk_info["has_signal"]:
                    kzk_badge = {
                        "is_blacklisted": kzk_info["is_blacklisted"],
                        "warranty_scheme": kzk_info["warranty_scheme"],
                    }

    # Профиль продавца (§2.7 liquidity_model_design.md, задача 2026-08-15,
    # миграция 077) — seller_profiles пересчитывается раз в сутки
    # (seller_profile_snapshot.py), ключ — тот же нормализатор имени
    # (trim+lower+схлопнутые пробелы), что и в снапшоте. Отсутствие строки
    # — валидный случай (generic-имя вроде "хозяин" в стоп-листе снапшота,
    # либо снапшот ещё не прогонялся после первого появления этого имени),
    # не ошибка.
    seller_profile = None
    if l.get("seller_name"):
        import re as _re
        name_norm = _re.sub(r"\s+", " ", l["seller_name"].strip()).lower()
        sp_row = await pg_fetchrow("SELECT * FROM seller_profiles WHERE seller_name = $1", name_norm)
        if sp_row:
            seller_profile = {
                "seller_type": sp_row["seller_type"],
                "active_listings_count": sp_row["active_listings_count"],
                "total_listings_count": sp_row["total_listings_count"],
                "relist_rate": float(sp_row["relist_rate"]) if sp_row.get("relist_rate") is not None else None,
                "is_high_relist_rate": sp_row["is_high_relist_rate"],
                "is_motivated_seller": sp_row["is_motivated_seller"],
                # "агентство >50 активных" — из чтения ЖИВОГО значения
                # active_listings_count здесь, не отдельного bool-поля
                # (порог 50 — свойство UI-бейджа, не самого профиля).
                "is_large_agency": sp_row["active_listings_count"] > 50,
                # is_ambiguous (миграция 079) — >15 объявлений под одним
                # словом-именем на практике почти всегда коллизия разных
                # людей, не один сверхактивный продавец (найдено на живых
                # данных при разработке §2.7 — 837/1249 срабатываний
                # is_motivated_seller были именно такими именами). Начиная
                # с миграции 079 это уже ЖЁСТКОЕ правило на уровне
                # seller_profile_snapshot.py (is_high_relist_rate/
                # is_motivated_seller принудительно FALSE для ambiguous),
                # не просто оговорка в тексте — колонка читается отсюда,
                # не пересчитывается второй раз в UI-слое.
                "is_ambiguous": sp_row["is_ambiguous"],
            }

    # Лента "рядом" — 3 ближайших активных объявления по прямому расстоянию
    nearby = []
    if l.get("lat") is not None and l.get("lon") is not None:
        from bot.db.pg import fetch as pg_fetch
        nb_rows = await pg_fetch("""
            SELECT id, url, price, rooms, area, photos
            FROM apartment_listings
            WHERE lat IS NOT NULL AND lon IS NOT NULL
              AND is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
              AND id != $3
            ORDER BY (lat - $1)^2 + (lon - $2)^2 ASC LIMIT 3
        """, float(l["lat"]), float(l["lon"]), listing_id)
        for nb in nb_rows:
            nb_photos = nb["photos"]
            if isinstance(nb_photos, str):
                try:
                    nb_photos = json.loads(nb_photos)
                except ValueError:
                    nb_photos = []
            nearby.append({
                "id": nb["id"], "url": nb.get("url") or "",
                "price": nb.get("price"), "rooms": nb.get("rooms"),
                "area": float(nb["area"]) if nb.get("area") else None,
                "photo": (nb_photos or [None])[0],
            })

    # «Паспорт рисков» (задача 2026-08-21, "Риски объекта") — единый
    # normalized risk_analysis, см. bot/core/listing_risks.py докстринг.
    # Переиспользует уже загруженные l/kzk_badge/seller_profile/layers/
    # ai_analysis/complex_housing_class — не пересчитывает их заново.
    # compute_listing_risks_safe гасит любую ошибку внутри (не роняет
    # открытие карточки), поэтому отдельного try/except здесь не нужно.
    from bot.core.listing_risks import compute_listing_risks_safe
    risk_analysis = await compute_listing_risks_safe(
        listing_id, l, kzk_badge=kzk_badge, seller_profile=seller_profile,
        layers=layers, ai_analysis=ai_analysis,
        complex_housing_class=complex_housing_class,
    )

    return {
        "id": l["id"], "url": l.get("url") or "",
        "price": l.get("price"), "rooms": l.get("rooms"),
        "market": l.get("market_type") or "secondary",
        "area": float(l["area"]) if l.get("area") else None,
        "floor": l.get("floor"), "floors_total": l.get("floors_total"),
        "address": l.get("address") or "", "district": l.get("district") or "",
        "lat": float(l["lat"]) if l.get("lat") is not None else None,
        "lon": float(l["lon"]) if l.get("lon") is not None else None,
        "complex_name": l.get("complex_name") or "",
        "complex_photos": complex_photos,
        "kzk_badge": kzk_badge,
        "geo": l.get("geo_source") or "",
        "photos": photos or [],
        "seller_name": l.get("seller_name") or "",
        "is_owner": l.get("is_owner") is True,
        "seller_type": l.get("seller_type") or ("owner" if l.get("is_owner") else "realtor"),
        "trust_score": float(l["trust_score"]) if l.get("trust_score") is not None else None,
        "seller_profile": seller_profile,
        "year_built": l.get("year_built"),
        "views_count": l.get("views_count"),
        "floorplan_url": l.get("floorplan_url") or "",
        # ceiling_height не отдавался тут вообще — d.ceiling_height в
        # модалке (dashboard.html) был мёртвым полем (всегда undefined),
        # плашка "потолок N м" никогда не показывалась. kitchen_area —
        # новое поле (см. задачу "кухня в парсерах продажи").
        "ceiling_height": float(l["ceiling_height"]) if l.get("ceiling_height") is not None else None,
        "kitchen_area": float(l["kitchen_area"]) if l.get("kitchen_area") is not None else None,
        "ai_analysis": ai_analysis,
        "similar": similar_listings,
        "tier": tier,
        "layers": layers,
        "description": l.get("description") or "",
        "first_seen": l["first_seen"].strftime("%d.%m.%Y") if l.get("first_seen") else None,
        "age": int((datetime.now(timezone.utc) - l["first_seen"]).days) if l.get("first_seen") else None,
        "bargain": {
            "discount_pct": bargain.get("discount_pct") or 0,
            "target_price": bargain.get("target_price"),
            "median_price": bargain.get("median_price"),
            "comparables_cnt": bargain.get("comparables_cnt") or 0,
            "recommendation": bargain.get("recommendation") or "",
            "method": bargain.get("method"),
            "class_note": bargain.get("class_note"),
        },
        "nearby": nearby,
        "deal_score": (lambda hd: {
            "deal": hd.get("deal"), "confidence": hd.get("confidence"),
        } if hd else None)(
            (lambda v: (json.loads(v) if isinstance(v, str) else v) if v else None)(l.get("hex_details"))
        ),
        "risk_analysis": risk_analysis,
        # То же, что показывает страница /admin/analytics/{id} — доходность
        # и разбивка скора по компонентам, теперь дублируется и в модалке
        # на карте, чтобы не заставлять переходить на отдельную страницу.
        "est_rent": l.get("est_rent"),
        "yield_pct": l.get("yield_pct"),
        # net_yield_pct — доходность с поправкой на вакантность/налог/
        # расходы на покупку (см. Notion "Расчет доходности"), gross
        # выше остаётся для обратной совместимости со старым скорингом.
        "net_yield_pct": l.get("net_yield_pct"),
        "payback_years": l.get("payback_years"),
        # ── Бейджи "недооценено"/"высокая доходность" — те же пороги
        # (топ-10%), что и в /admin/api/map-points, см. комментарий там.
        "underpriced_pct": (round((l["hex_deal_index"] - 1) * 100)
                             if l.get("hex_deal_index") and (l.get("deal_confidence") or 0) >= 50
                             and (l["hex_deal_index"] - 1) * 100 >= 28.8 else None),
        "high_yield": bool(l.get("yield_pct") and l["yield_pct"] >= 13.9),
        "rent_source": l.get("rent_source") or "",
        "zone_name": l.get("zone_name") or "",
        "zone_bonus": l.get("zone_bonus"),
        "score_breakdown": {
            "yield": l.get("score_yield") or 0,
            "price_market": l.get("score_price_market") or 0,
            "location": l.get("score_location") or 0,
            "apt_type": l.get("score_apt_type") or 0,
            "floor": l.get("score_floor") or 0,
            "complex": l.get("score_complex") or 0,
            "supply": l.get("score_supply") or 0,
        },
        "negotiation_points": negotiation_points,
        "seller_questions": seller_questions,
    }


async def build_price_history(listing_id: str) -> dict:
    """История цены для карточки/попапа на карте (/admin/api/price-history).
    Публичная (как и сама карта) — ничего чувствительного тут нет, тира
    на входе нет и не было в исходном роуте."""
    from bot.db.pg import fetch as pg_fetch, fetchrow as pg_fetchrow

    rows = await pg_fetch("""
        SELECT old_price, new_price, changed_at
        FROM price_history
        WHERE listing_id = $1
        ORDER BY changed_at ASC
    """, listing_id)
    cur = await pg_fetchrow(
        "SELECT price, first_seen FROM apartment_listings WHERE id = $1",
        listing_id)
    points = []
    # стартовая точка — цена при первом появлении в базе
    if cur and cur["first_seen"]:
        first_price = rows[0]["old_price"] if rows else cur["price"]
        points.append({"at": cur["first_seen"].strftime("%d.%m.%Y"),
                       "price": first_price})
    for r in rows:
        points.append({"at": r["changed_at"].strftime("%d.%m.%Y"),
                       "price": r["new_price"]})
    return {
        "points": points,
        "current": cur["price"] if cur else None,
        "changes": len(rows),
    }
