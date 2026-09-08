"""bot/ai_tools/tool_registry.py — Этап 4 задачи "Hatuli API v1 для AI
tools": декларативный, read-only реестр capabilities для БУДУЩЕГО
orchestrator-агента. Никакого OpenAI/Claude SDK здесь — это просто
данные (name/description/input schema/output schema/read_only), которые
такой агент (или просто HTTP-клиент) может прочитать, чтобы узнать, что
доступно и как это вызвать. Ничего в этом файле не исполняется само по
себе — `handler` ниже указывает на реальную функцию в bot.ai_tools.core /
bot.ai_tools.data_quality, вызывающий код сам решает, звать ли её
напрямую (in-process) или через HTTP (bot.ai_tools.router)."""
from __future__ import annotations

# Общая форма ответа всех entity-based тулов (bot/ai_tools/envelope.py) —
# Этап 2 задачи. data_quality_snapshot — единственное исключение (у него
# своя, отдельно заданная задачей форма {status, findings}, см. ниже).
ENVELOPE_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "entity_type": {"type": "string"},
        "entity_id": {"type": ["string", "integer", "null"]},
        "as_of": {"type": ["string", "null"], "format": "date-time"},
        "data": {"type": "object"},
        "confidence": {"type": ["number", "string", "null"]},
        "evidence": {"type": "array"},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "methodology_version": {"type": ["string", "null"]},
    },
    "required": ["entity_type", "entity_id", "as_of", "data", "confidence",
                 "evidence", "warnings", "methodology_version"],
}

DATA_QUALITY_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "warning", "critical"]},
        "generated_at": {"type": "string", "format": "date-time"},
        "collectors_checked": {"type": "integer"},
        "coverage_checks_run": {"type": "integer"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "component": {"type": "string"},
                    "severity": {"type": "string", "enum": ["warning", "critical"]},
                    "fact": {"type": "string"},
                    "evidence": {"type": "object"},
                    "recommended_action": {"type": "string"},
                },
                "required": ["component", "severity", "fact", "evidence", "recommended_action"],
            },
        },
    },
    "required": ["status", "findings"],
}

TOOLS: list[dict] = [
    {
        "name": "resolve_listing",
        "description": "Resolve a Krisha listing URL or bare listing_id into its "
                        "identity (listing_id, property_id, complex_id) and basic "
                        "attributes. Read-only, does not recompute Property/Complex Identity.",
        "input_schema": {
            "type": "object",
            "properties": {"url_or_id": {"type": "string",
                                          "description": "Krisha URL (.../a/show/<id>) or bare numeric listing_id"}},
            "required": ["url_or_id"],
        },
        "output_schema": {**ENVELOPE_OUTPUT_SCHEMA,
                           "description": "data: {listing_id, found, property_id, complex_id, "
                                          "complex_id_resolution, address, district, complex_name, market_type, ...}"},
        "read_only": True,
        "handler": "bot.ai_tools.core.resolve_listing",
    },
    {
        "name": "property_history",
        "description": "Full event timeline for one physical property (Property "
                        "Timeline, Phase 1): relist history, price changes, archive/"
                        "reactivation events, identity-link evidence. Pure passthrough "
                        "of bot.core.property_timeline.build_property_timeline().",
        "input_schema": {
            "type": "object",
            "properties": {"property_id": {"type": "integer"}},
            "required": ["property_id"],
        },
        "output_schema": {**ENVELOPE_OUTPUT_SCHEMA,
                           "description": "data: {property_id, found, identity_status, identity, "
                                          "metrics, listings[], events[]}"},
        "read_only": True,
        "handler": "bot.ai_tools.core.get_property_history",
    },
    {
        "name": "listing_analysis",
        "description": "Current price, fair/target price (if comparables exist), "
                        "yield, deal-score, and days-on-market scenario forecast for "
                        "one listing. Wraps bot.core.listing_detail.build_listing_detail "
                        "+ bot.analytics.dom_scenario.compute_dom_scenario_cached.",
        "input_schema": {
            "type": "object",
            "properties": {"listing_id": {"type": "string"}},
            "required": ["listing_id"],
        },
        "output_schema": {**ENVELOPE_OUTPUT_SCHEMA,
                           "description": "data: {listing_id, found, price, market, fair_price{}, "
                                          "deal_score, yield_pct, net_yield_pct, dom_scenario{}, "
                                          "similar_listings[], score_breakdown{}, sample_size, insufficient_data}"},
        "read_only": True,
        "handler": "bot.ai_tools.core.get_listing_analysis",
    },
    {
        "name": "listing_risks",
        "description": "Normalized risk passport for one listing (valuation, floor, "
                        "building, seller, KZK/developer-protection, location, "
                        "liquidity signals). Pure passthrough of the risk_analysis "
                        "field already computed by bot.core.listing_detail.build_listing_detail "
                        "(bot.core.listing_risks.compute_listing_risks_safe).",
        "input_schema": {
            "type": "object",
            "properties": {"listing_id": {"type": "string"}},
            "required": ["listing_id"],
        },
        "output_schema": {**ENVELOPE_OUTPUT_SCHEMA,
                           "description": "data: {listing_id, found, risk_analysis: "
                                          "{overall_level, summary, items[], protective[], unknowns[], "
                                          "calculated_at, version}}"},
        "read_only": True,
        "handler": "bot.ai_tools.core.get_listing_risks",
    },
    {
        "name": "complex_market_profile",
        "description": "Market profile for one ЖК (complex): supply, price, "
                        "liquidity (true relist rate, observed DOM, stale inventory), "
                        "demand (views), data quality — each section carries its own "
                        "sample_size/insufficient_data. Pure passthrough of "
                        "bot.core.complex_market_profile.get_complex_market_profile.",
        "input_schema": {
            "type": "object",
            "properties": {"complex_id": {"type": "integer"}},
            "required": ["complex_id"],
        },
        "output_schema": {**ENVELOPE_OUTPUT_SCHEMA,
                           "description": "data: {complex_id, found, as_of, identity{}, physical{}, "
                                          "supply{}, price{}, liquidity{}, demand{}, data_quality{}}"},
        "read_only": True,
        "handler": "bot.ai_tools.core.get_complex_market_profile",
    },
    {
        "name": "location_analysis",
        "description": "Location detail for one ЖК (complex): location score "
                        "breakdown (transport/infra/noise/green/risk), hex density, "
                        "nearby demolition houses, POI/schools, walkability, price-drop "
                        "trend. Pure passthrough of "
                        "bot.core.complex_location_detail.build_complex_location_detail.",
        "input_schema": {
            "type": "object",
            "properties": {"complex_id": {"type": "integer"}},
            "required": ["complex_id"],
        },
        "output_schema": {**ENVELOPE_OUTPUT_SCHEMA,
                           "description": "data: {complex_id, found, has_coords, has_score, score, "
                                          "density[], demolition[], poi{}, price_drop_trend[], walkability{}}"},
        "read_only": True,
        "handler": "bot.ai_tools.core.get_location_analysis",
    },
    {
        "name": "data_quality_snapshot",
        "description": "Read-only collector health snapshot: freshness/gap, sudden "
                        "volume drops, and NULL-coverage degradation across key "
                        "temporal tables. Foundation for a future Data Quality Agent — "
                        "diagnoses only, fixes nothing.",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": DATA_QUALITY_OUTPUT_SCHEMA,
        "read_only": True,
        "handler": "bot.ai_tools.data_quality.build_data_quality_snapshot",
    },
]

TOOLS_BY_NAME: dict[str, dict] = {t["name"]: t for t in TOOLS}


def get_tool(name: str) -> dict | None:
    return TOOLS_BY_NAME.get(name)
