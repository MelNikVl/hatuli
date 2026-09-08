"""bot/ai_tools/core.py — 6 read-only capabilities, Этап 1 задачи "Hatuli
API v1 для AI tools" (фундамент под будущий agentic-layer / HackAlem).

Каждая функция ниже — ТОНКАЯ обёртка над уже существующей core-функцией.
Докстринг каждой явно называет, что именно переиспользуется, чтобы не
дублировать расчёты (условие задачи):

  resolve_listing            -> прямые SELECT'ы по apartment_listings /
                                 property_listings / properties / complexes
                                 (тот же join, что уже использует bot/core/
                                 property_timeline.py и listing_detail.py).
  get_property_history       -> bot.core.property_timeline.build_property_timeline
  get_listing_analysis       -> bot.core.listing_detail.build_listing_detail
                                 + bot.analytics.dom_scenario.compute_dom_scenario_cached
  get_listing_risks          -> bot.core.listing_detail.build_listing_detail
                                 (поле risk_analysis, откуда — см. его
                                 докстринг: bot.core.listing_risks)
  get_complex_market_profile -> bot.core.complex_market_profile.get_complex_market_profile
  get_location_analysis      -> bot.core.complex_location_detail.build_complex_location_detail

Никаких INSERT/UPDATE/DELETE — этот модуль импортирует ТОЛЬКО read-функции
(fetch/fetchrow чужих core-модулей и bot.db.pg.fetchrow напрямую в
resolve_listing). tests/test_ai_tools_read_only.py проверяет это явно.

get_location_analysis — единственное исключение, требовавшее отдельного
внимания (найдено в review PR #51): build_complex_location_detail() при
дефолтных настройках может на cache-miss сходить в живой Overpass И
записать результат в osm_cache (bot/score_layers/osm.py::overpass_cached).
Задача "fix/ai-tools-strict-read-only" добавила параметр
allow_live_fetch=False именно для AI-tools пути (см. докстринг
get_location_analysis и build_complex_location_detail) — обычный UI и
любой другой потребитель по умолчанию (allow_live_fetch=True) продолжают
работать как раньше, ни строкой иначе.

Каждая функция возвращает `ToolResult` (bot/ai_tools/envelope.py) — router.py
и/или тесты дальше заворачивают его в общий envelope через build_envelope().

## methodology_version — версия ЭТОГО адаптера, не гарантия апстрима

RESOLVE_LISTING_VERSION/PROPERTY_HISTORY_VERSION/LISTING_ANALYSIS_VERSION/
COMPLEX_MARKET_PROFILE_VERSION/LOCATION_ANALYSIS_VERSION ниже — версии
API/adaptor-контракта bot/ai_tools/ (эта обёртка: какие поля она отдаёт и
как их извлекает), НЕ версии внутренних формул апстрим-модулей, которые
она вызывает. У большинства из них (bot.core.bargain, bot.analytics.
dom_scenario, bot.core.complex_market_profile, bot.core.
complex_location_detail) СВОЕГО version-маркера нет вообще — если их
формула/методология изменится, эти константы НЕ изменятся автоматически
(задача явно требует это не путать, review PR #51 п.6). Единственное
исключение — get_listing_risks: там methodology_version берётся из
РЕАЛЬНОГО bot.core.listing_risks.VERSION апстрим-модуля (см. её докстринг
ниже), LISTING_RISKS_VERSION_FALLBACK используется только если апстрим
почему-то не вернул risk_analysis вовсе."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from bot.ai_tools.envelope import ToolResult

RESOLVE_LISTING_VERSION = "ai_api.resolve_listing.v1"
PROPERTY_HISTORY_VERSION = "ai_api.property_history.v1"
LISTING_ANALYSIS_VERSION = "ai_api.listing_analysis.v1"
LISTING_RISKS_VERSION_FALLBACK = "ai_api.listing_risks.v1"
COMPLEX_MARKET_PROFILE_VERSION = "ai_api.complex_market_profile.v1"
LOCATION_ANALYSIS_VERSION = "ai_api.location_analysis.v1"

# Тот же паттерн, что bot/core/parser.py::_extract_listing_id использует
# для карточек с Крыши — численный id объявления, последний числовой
# сегмент URL вида .../a/show/<id> (5+ цифр, тот же порог, что и в
# parser.py — короче не встречается). Не изобретаем новый парсинг URL,
# та же семантика id, что уже хранится как apartment_listings.id (text).
_ID_IN_URL_RE = re.compile(r"/(\d{5,})")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_listing_id_from_input(url_or_id: str) -> str | None:
    s = (url_or_id or "").strip()
    if not s:
        return None
    if s.isdigit():
        return s
    match = _ID_IN_URL_RE.search(s)
    return match.group(1) if match else None


# ── 1. resolve_listing ──────────────────────────────────────────────────

async def resolve_listing(url_or_id: str) -> ToolResult:
    """Принимает Krisha URL (.../a/show/<id>) или голый listing_id,
    возвращает listing_id/property_id/complex_id + базовую identity-
    информацию. Три read-only SELECT'а, без пересчёта чего-либо:

      1. apartment_listings по id — сама карточка (адрес/район/ЖК/маркет).
      2. property_listings по listing_id — канонический путь к Property
         Identity (bot/core/property_timeline.py докстринг: "property_
         identity.linked_property_id В СХЕМЕ НЕ СУЩЕСТВУЕТ", единственный
         реальный путь — через property_listings).
      3. properties по property_id -> complex_id, ЕСЛИ (2) нашла связь.

    complex_id имеет ЕЩЁ один, менее надёжный источник — apartment_listings.
    complex_name, сматченный текстом по complexes.name (тот же приём, что
    bot/core/listing_detail.py::build_listing_detail использует для
    complex_photos/kzk_badge). Используется ТОЛЬКО как fallback, когда
    Property Identity ещё не связала объявление, и явно помечается через
    `complex_id_resolution` + warning — не выдаётся молча за тот же по
    надёжности факт, что and property_identity-путь."""
    from bot.db.pg import fetchrow

    as_of = _now_iso()
    listing_id = _extract_listing_id_from_input(url_or_id)
    if listing_id is None:
        return ToolResult(
            data={"input": url_or_id, "listing_id": None, "found": False},
            as_of=as_of,
            warnings=["could not extract a numeric listing_id from input "
                      "(expected a Krisha /a/show/<id> URL or a bare numeric id)"],
            methodology_version=RESOLVE_LISTING_VERSION,
        )

    row = await fetchrow(
        "SELECT id, url, address, district, complex_name, market_type, is_active, first_seen "
        "FROM apartment_listings WHERE id = $1",
        listing_id,
    )
    if row is None:
        return ToolResult(
            data={"input": url_or_id, "listing_id": listing_id, "found": False},
            as_of=as_of,
            warnings=[f"listing_id {listing_id} not found in apartment_listings"],
            methodology_version=RESOLVE_LISTING_VERSION,
        )
    l = dict(row)

    link_row = await fetchrow(
        "SELECT property_id, link_method, confidence, linked_at "
        "FROM property_listings WHERE listing_id = $1",
        listing_id,
    )

    complex_id = None
    complex_id_resolution = None
    warnings: list[str] = []

    if link_row and link_row["property_id"] is not None:
        prop_row = await fetchrow(
            "SELECT complex_id FROM properties WHERE property_id = $1", link_row["property_id"])
        if prop_row and prop_row["complex_id"] is not None:
            complex_id = prop_row["complex_id"]
            complex_id_resolution = "property_identity"

    if complex_id is None and l.get("complex_name"):
        cx_row = await fetchrow(
            "SELECT id FROM complexes WHERE lower(trim(name)) = lower(trim($1)) LIMIT 1",
            l["complex_name"],
        )
        if cx_row:
            complex_id = cx_row["id"]
            complex_id_resolution = "complex_name_match"
            warnings.append(
                "complex_id resolved via complex_name text match, not Property Identity "
                "(listing not linked, or linked property has no complex_id)")

    if link_row is None:
        warnings.append("listing not yet linked to Property Identity "
                         "(no row in property_listings for this listing_id)")

    link_confidence = (float(link_row["confidence"])
                       if link_row and link_row.get("confidence") is not None else None)

    data = {
        "input": url_or_id,
        "listing_id": listing_id,
        "found": True,
        "url": l.get("url"),
        "address": l.get("address"),
        "district": l.get("district"),
        "complex_name": l.get("complex_name"),
        "market_type": l.get("market_type"),
        "is_active": l.get("is_active"),
        "first_seen": l["first_seen"].isoformat() if l.get("first_seen") else None,
        "property_id": link_row["property_id"] if link_row else None,
        "property_link_method": link_row["link_method"] if link_row else None,
        "property_link_confidence": link_confidence,
        "complex_id": complex_id,
        "complex_id_resolution": complex_id_resolution,
    }

    evidence = [{"source": "apartment_listings", "key": "id"}]
    if link_row:
        evidence.append({"source": "property_listings", "key": "listing_id"})
    if complex_id_resolution == "property_identity":
        evidence.append({"source": "properties", "key": "property_id"})
    elif complex_id_resolution == "complex_name_match":
        evidence.append({"source": "complexes", "key": "name (text match, fallback)"})

    return ToolResult(
        data=data, as_of=as_of, confidence=link_confidence, evidence=evidence,
        warnings=warnings, methodology_version=RESOLVE_LISTING_VERSION,
    )


# ── 2. get_property_history ─────────────────────────────────────────────

async def get_property_history(property_id: int) -> ToolResult:
    """Переиспользует bot.core.property_timeline.build_property_timeline()
    1:1 — БЕЗ новой логики (задача, явно). Эта функция только достаёт
    as_of/confidence/evidence/warnings из уже посчитанного результата."""
    from bot.core.property_timeline import build_property_timeline

    as_of = _now_iso()
    timeline = await build_property_timeline(property_id)
    if timeline is None:
        return ToolResult(
            data={"property_id": property_id, "found": False},
            as_of=as_of,
            warnings=[f"property_id {property_id} not found in properties"],
            methodology_version=PROPERTY_HISTORY_VERSION,
        )

    identity = timeline.get("identity") or {}
    warnings: list[str] = []
    if identity.get("status") == "provisional":
        warnings.append("identity_status is 'provisional' (not confirmed) — "
                         "this timeline may still change as more evidence accumulates")
    if identity.get("linked_listing_count") == 0:
        warnings.append("no listings linked to this property_id yet")

    data = {"found": True, **timeline}
    return ToolResult(
        data=data,
        as_of=as_of,
        confidence=identity.get("confidence"),
        evidence=[
            {"source": "property_listings"}, {"source": "apartment_listings"},
            {"source": "price_history"}, {"source": "listing_archive_history"},
            {"source": "property_match_candidates"}, {"source": "property_candidate_photo_evidence"},
        ],
        warnings=warnings,
        methodology_version=PROPERTY_HISTORY_VERSION,
    )


# ── 3. get_listing_analysis ─────────────────────────────────────────────

async def get_listing_analysis(listing_id: str) -> ToolResult:
    """Переиспользует bot.core.listing_detail.build_listing_detail()
    (tier='admin' — этот internal AI-tool не подчиняется 3-уровневому
    доступу ПУБЛИЧНОГО САЙТА: bot/core/site_auth.py докстринг явно говорит,
    что тиры (public/subscriber/admin) регулируют, что видит анонимный
    посетитель сайта, это не имеет отношения к доверенному server-side
    вызову аналитики) для цены/торга/доходности, и bot.analytics.
    dom_scenario.compute_dom_scenario_cached() для прогноза срока
    экспозиции — те же два вызова, что уже делают существующие роуты
    /admin/api/listing/{id} и /admin/api/listing/{id}/dom-scenario.
    НИКАКИХ новых расчётов, только выборка нужных полей из их результатов."""
    from bot.core.listing_detail import build_listing_detail, ListingNotFound

    as_of = _now_iso()
    try:
        detail = await build_listing_detail(listing_id, tier="admin")
    except ListingNotFound:
        return ToolResult(
            data={"listing_id": listing_id, "found": False},
            as_of=as_of,
            warnings=[f"listing_id {listing_id} not found in apartment_listings"],
            methodology_version=LISTING_ANALYSIS_VERSION,
        )

    try:
        from bot.analytics.dom_scenario import compute_dom_scenario_cached
        dom_scenario = await compute_dom_scenario_cached(listing_id)
    except Exception:
        # Тот же принцип, что и существующий роут /admin/api/listing/{id}/
        # dom-scenario (terminal_extras.py): ошибка прогноза не должна
        # ронять весь ответ.
        dom_scenario = {"available": False, "reason": "error"}

    bargain = detail.get("bargain") or {}
    comparables_cnt = bargain.get("comparables_cnt") or 0

    warnings: list[str] = []
    if not comparables_cnt:
        warnings.append("no comparables found — fair/target price is not available")
    if dom_scenario.get("available") is False:
        warnings.append(f"dom_scenario unavailable: {dom_scenario.get('reason', 'unknown')}")
    elif dom_scenario.get("insufficient_data"):
        warnings.append("dom_scenario: insufficient comparable sample for a reliable exposure-time forecast")

    data = {
        "listing_id": listing_id,
        "found": True,
        "price": detail.get("price"),
        "market": detail.get("market"),
        "fair_price": {
            "target_price": bargain.get("target_price"),
            "median_price": bargain.get("median_price"),
            "comparables_cnt": comparables_cnt,
            "recommendation": bargain.get("recommendation"),
            "method": bargain.get("method"),
            "class_note": bargain.get("class_note"),
        },
        "deal_score": detail.get("deal_score"),
        "underpriced_pct": detail.get("underpriced_pct"),
        "yield_pct": detail.get("yield_pct"),
        "net_yield_pct": detail.get("net_yield_pct"),
        "payback_years": detail.get("payback_years"),
        "high_yield": detail.get("high_yield"),
        "dom_scenario": dom_scenario,
        "comparables_count": comparables_cnt,
        "similar_listings": detail.get("similar") or [],
        "score_breakdown": detail.get("score_breakdown"),
        "sample_size": comparables_cnt,
        "insufficient_data": comparables_cnt == 0,
    }

    confidence = (detail.get("deal_score") or {}).get("confidence")
    evidence = [
        {"source": "apartment_listings"},
        {"source": "bargain.get_comparables (hex+ring, rooms, area ±15%, housing class)"},
        {"source": "hex_details (Deal Score, bot/core/deal_score.py, precomputed batch)"},
    ]
    if dom_scenario.get("available") is not False:
        evidence.append({"source": "dom_scenario (Kaplan-Meier + PAVA, bot/analytics/dom_scenario.py)"})

    return ToolResult(
        data=data, as_of=as_of, confidence=confidence, evidence=evidence,
        warnings=warnings, methodology_version=LISTING_ANALYSIS_VERSION,
    )


# ── 4. get_listing_risks ────────────────────────────────────────────────

async def get_listing_risks(listing_id: str) -> ToolResult:
    """Переиспользует bot.core.listing_detail.build_listing_detail() ->
    поле `risk_analysis`, посчитанное bot.core.listing_risks.
    compute_listing_risks_safe() — докстринг того модуля явно называет его
    "единственным источником истины для UI". Намеренно НЕ вызывает
    compute_listing_risks*() напрямую: build_listing_detail уже собирает
    kzk_badge/seller_profile/layers/ai_analysis/complex_housing_class,
    нужные risk-расчёту на входе — пересобирать их здесь второй раз means
    a second source of truth for the same inputs, exactly what that
    module's docstring warns against."""
    from bot.core.listing_detail import build_listing_detail, ListingNotFound

    as_of = _now_iso()
    try:
        detail = await build_listing_detail(listing_id, tier="admin")
    except ListingNotFound:
        return ToolResult(
            data={"listing_id": listing_id, "found": False},
            as_of=as_of,
            warnings=[f"listing_id {listing_id} not found in apartment_listings"],
            methodology_version=LISTING_RISKS_VERSION_FALLBACK,
        )

    risk_analysis = detail.get("risk_analysis")
    warnings: list[str] = []
    if not risk_analysis:
        warnings.append("risk_analysis missing from listing_detail (unexpected)")
        risk_analysis = {}
    elif risk_analysis.get("overall_level") == "unknown":
        warnings.append(
            "risk calculation failed internally — risk_analysis is a safe fallback, "
            "not a real assessment (see bot.core.listing_risks.compute_listing_risks_safe)")

    data = {"listing_id": listing_id, "found": True, "risk_analysis": risk_analysis}

    return ToolResult(
        data=data,
        as_of=risk_analysis.get("calculated_at") or as_of,
        # risk_analysis категоричен (overall_level: critical/high/medium/low/
        # info/unknown) — числового confidence не считается, не выдумываем.
        confidence=None,
        evidence=[{
            "source": "bot.core.listing_risks.compute_listing_risks_safe",
            "note": "full per-signal evidence is inside data.risk_analysis.items[]/.protective[]/.unknowns[]",
        }],
        warnings=warnings,
        methodology_version=risk_analysis.get("version") or LISTING_RISKS_VERSION_FALLBACK,
    )


# ── 5. get_complex_market_profile ───────────────────────────────────────

async def get_complex_market_profile(complex_id: int) -> ToolResult:
    """Переиспользует bot.core.complex_market_profile.
    get_complex_market_profile() 1:1 (as_of=None -> считает на "сейчас",
    тот же default, что и у оригинала). Вся методология и её ограничения
    задокументированы в докстринге того модуля — здесь не повторяются и
    не пересчитываются."""
    from bot.core.complex_market_profile import get_complex_market_profile as _get_profile

    as_of = _now_iso()
    profile = await _get_profile(complex_id)
    if profile is None:
        return ToolResult(
            data={"complex_id": complex_id, "found": False},
            as_of=as_of,
            warnings=[f"complex_id {complex_id} not found in complexes"],
            methodology_version=COMPLEX_MARKET_PROFILE_VERSION,
        )

    warnings: list[str] = []
    dq = profile.get("data_quality") or {}
    if dq.get("complex_marked_garbage"):
        warnings.append("complex is marked is_garbage — profile is likely unreliable")
    if (profile.get("price") or {}).get("insufficient_data"):
        warnings.append("price section: insufficient_data (sample below internal minimum)")
    if (profile.get("liquidity") or {}).get("insufficient_data"):
        warnings.append("liquidity section: insufficient_data (sample below internal minimum)")
    # Тот же принцип, что и price/liquidity выше (review PR #51 п.5) —
    # demand использует ДРУГОЕ имя флага (insufficient_history, не
    # insufficient_data — см. bot/core/complex_market_profile.py::
    # _build_demand докстринг: "нет истории вообще" это отдельный случай
    # от "истории мало"), но сигнал того же рода и заслуживает того же
    # warning, а не молчаливого пропуска.
    if (profile.get("demand") or {}).get("insufficient_history"):
        warnings.append("demand section: insufficient_history (no/too little views_history for this complex)")

    data = {"found": True, **profile}
    return ToolResult(
        data=data,
        as_of=profile.get("as_of") or as_of,
        # Модуль не считает единый числовой confidence — только per-section
        # sample_size/insufficient_data (data.price / data.liquidity /
        # data.demand) — не сворачиваем их в одно выдуманное число.
        confidence=None,
        evidence=[
            {"source": "properties"}, {"source": "property_listings"}, {"source": "apartment_listings"},
            {"source": "price_history"}, {"source": "views_history"}, {"source": "listing_archive_history"},
        ],
        warnings=warnings,
        methodology_version=COMPLEX_MARKET_PROFILE_VERSION,
    )


# ── 6. get_location_analysis ────────────────────────────────────────────

async def get_location_analysis(complex_id: int) -> ToolResult:
    """Переиспользует bot.core.complex_location_detail.
    build_complex_location_detail() 1:1 (блок «Локация» на странице ЖК,
    Фаза L2) — без нового геопространственного расчёта.

    Вызывает с allow_live_fetch=False (задача "fix/ai-tools-strict-read-
    only", review PR #51 п.1: этот AI-tool не должен делать живые внешние
    HTTP-запросы к Overpass и не должен писать в osm_cache — обычный
    /admin/api/complex/{id}/location-detail для человеческого UI и любой
    другой потребитель продолжают работать как раньше, allow_live_fetch
    по умолчанию True именно для них, см. докстринг
    build_complex_location_detail). POI/школы при этом читаются ТОЛЬКО из
    city_poi/уже существующего кэша — если ни того, ни другого для этой
    точки нет, честный insufficient_data-warning ниже, НЕ молчаливый
    fallback и НЕ ноль/среднее вместо ответа."""
    from bot.core.complex_location_detail import build_complex_location_detail, ComplexNotFound

    as_of = _now_iso()
    try:
        detail = await build_complex_location_detail(complex_id, allow_live_fetch=False)
    except ComplexNotFound:
        return ToolResult(
            data={"complex_id": complex_id, "found": False},
            as_of=as_of,
            warnings=[f"complex_id {complex_id} not found in complexes"],
            methodology_version=LOCATION_ANALYSIS_VERSION,
        )

    warnings: list[str] = []
    if not detail.get("has_coords"):
        warnings.append("no resolved geo centroid for this complex — location analysis unavailable")
    elif not detail.get("has_score"):
        warnings.append("no complex_location_scores snapshot yet for this complex_id (score is None)")

    poi_source = detail.get("poi_source") or {}
    if detail.get("has_coords") and poi_source.get("available") is False:
        warnings.append(
            "POI data unavailable in local/cache sources; live external fetch disabled "
            "for AI read-only API")

    data = {"complex_id": complex_id, "found": True, **detail}
    score = detail.get("score") or {}
    return ToolResult(
        data=data,
        as_of=as_of,
        confidence=score.get("confidence"),
        evidence=[
            {"source": "complex_location_scores"}, {"source": "hex_market_stats"},
            {"source": "demolition_houses"},
            {"source": "poi/schools layers (bot/score_layers/) — local city_poi sync + "
                       "existing osm_cache rows only, live Overpass fetch disabled for this API"},
            {"source": "complex_walkability"}, {"source": "complex_stats_history"},
        ],
        warnings=warnings,
        methodology_version=LOCATION_ANALYSIS_VERSION,
    )
