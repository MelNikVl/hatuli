"""Reviewed editorial comparisons, resolved only from immutable digest facts.

This module neither collects market data nor ranks entities. Matching profile
identities and dated, cleared inputs are an upstream editorial responsibility.
"""
from __future__ import annotations

from datetime import timedelta
import re
from zoneinfo import ZoneInfo


TOPICS = {"places": "Районы и места", "complexes": "ЖК и дома", "banks": "Банки",
          "developers": "Застройщики", "strategies": "Куда вложить деньги"}
METRICS = {"asking_price": ("Цена предложения", {"₸", "₸/м²"}),
           "monthly_rent": ("Аренда за месяц", {"₸/мес."}),
           "nightly_asking_rate": ("Ставка объявления за сутки", {"₸/сутки"}),
           "all_in_cost": ("Полные вложения", {"₸"}),
           "annual_net_yield": ("Чистый результат за год к капиталу", {"%"}),
           "twelve_month_effective_rate": ("ГЭСВ вклада на год", {"%"})}
BASES = {"observed_asking": "Цены предложений", "official_terms": "Официальные условия",
         "scenario": "Расчётный сценарий"}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_.]{1,79}\Z")


def validate_comparison_fact(fact, *, plain):
    """Optional facts retain an explicit, machine-checkable comparison basis."""
    if "comparison" not in fact:
        return
    meta = fact["comparison"]
    required = {"topic", "entity_id", "entity_name", "profile_id", "profile_label",
                "metric", "basis", "identity_verified", "cost_scope"}
    optional = {"capital_fact_id", "horizon_months", "input_fact_ids"}
    if (not isinstance(meta, dict) or set(meta) - (required | optional)
            or not required <= set(meta) or meta["topic"] not in TOPICS
            or meta["metric"] not in METRICS or meta["basis"] not in BASES
            or meta["identity_verified"] is not True
            or not isinstance(meta["entity_id"], str) or not IDENTIFIER.fullmatch(meta["entity_id"])
            or not isinstance(meta["profile_id"], str) or not IDENTIFIER.fullmatch(meta["profile_id"])
            or fact["unit"] not in METRICS[meta["metric"]][1]
            or meta["cost_scope"] not in {"asking_only", "all_in", "net_after_costs", "contract_rate"}):
        raise ValueError("Invalid verified editorial comparison fact")
    plain(meta["entity_name"], 2, 120)
    if re.search(r"[{}%₸$€]|скидк|рентаб|прибыл|гарантир", meta["entity_name"], re.I):
        raise ValueError("An entity name cannot contain investment claims")
    plain(meta["profile_label"], 10, 240)
    if meta["metric"] in {"annual_net_yield", "twelve_month_effective_rate"}:
        if (meta.get("horizon_months") != 12 or type(meta.get("horizon_months")) is not int
                or meta["cost_scope"] != ("contract_rate" if meta["metric"] == "twelve_month_effective_rate" else "net_after_costs")
                or meta["basis"] == "observed_asking"):
            raise ValueError("Yield comparisons require the same twelve-month net capital basis")
        if meta["metric"] == "twelve_month_effective_rate" and meta["basis"] != "official_terms":
            raise ValueError("A bank rate requires checked official terms")
        if meta["metric"] == "annual_net_yield" or "capital_fact_id" in meta:
            if (not isinstance(meta.get("capital_fact_id"), str)
                    or not IDENTIFIER.fullmatch(meta["capital_fact_id"])):
                raise ValueError("Yield comparisons require a positive frozen capital basis")
            inputs = meta.get("input_fact_ids")
            if (not isinstance(inputs, list) or not 2 <= len(inputs) <= 16
                    or len(set(inputs)) != len(inputs)
                    or any(not isinstance(key, str) or not IDENTIFIER.fullmatch(key) for key in inputs)):
                raise ValueError("Yield comparison inputs must be frozen dated facts")
        elif "input_fact_ids" in meta:
            raise ValueError("Yield inputs require an explicit capital basis")
    elif any(key in meta for key in optional):
        raise ValueError("Capital and horizon metadata are reserved for yield comparisons")
    if meta["metric"] in {"asking_price", "monthly_rent", "nightly_asking_rate"} and (
            meta["basis"] != "observed_asking" or meta["cost_scope"] != "asking_only"):
        raise ValueError("Asking rates cannot become actual income or net returns")
    if meta["metric"] == "all_in_cost" and meta["cost_scope"] != "all_in":
        raise ValueError("A full investment comparison must include the full cost scope")
    if meta["topic"] == "banks" and meta["metric"] != "twelve_month_effective_rate":
        raise ValueError("Bank comparisons require a checked twelve-month effective rate")


def normalize_comparisons(raw, digest, *, published, resolve, plain, utc, number_label):
    if raw is None:
        return [], set()
    if not isinstance(raw, list) or not 1 <= len(raw) <= 4:
        raise ValueError("Editorial comparisons must be nonempty and bounded")
    facts, sources = digest["facts"], digest["sources"]
    result, used = [], set()
    for block in raw:
        if (not isinstance(block, dict)
                or set(block) != {"topic", "title", "fact_ids", "conclusion", "tradeoff"}
                or block["topic"] not in TOPICS or not isinstance(block["fact_ids"], list)
                or not 2 <= len(block["fact_ids"]) <= 6
                or len(set(block["fact_ids"])) != len(block["fact_ids"])
                or any(key not in facts or "comparison" not in facts[key] for key in block["fact_ids"])):
            raise ValueError("A comparison requires two to six evidence-backed entities")
        entries = [facts[key] for key in block["fact_ids"]]
        metas = [fact["comparison"] for fact in entries]
        # A common date and cohort are required even when the prose suggests
        # the candidates are similar. A bank and a scenario may share a net
        # return metric only in the explicitly annual capital comparison.
        if (len({meta["entity_id"] for meta in metas}) != len(metas)
                or len({fact["unit"] for fact in entries}) != 1
                or len({meta["profile_id"] for meta in metas}) != 1
                or len({meta["profile_label"] for meta in metas}) != 1
                or len({meta["cost_scope"] for meta in metas}) != 1):
            raise ValueError("Comparison entities must share a date, unit and matched profile")
        topic = block["topic"]
        dates = [utc(fact["as_of"]) for fact in entries]
        if topic == "banks":
            if any(not published - timedelta(hours=24) <= value <= published for value in dates):
                raise ValueError("Bank conditions must be freshly checked within one day")
        elif len(set(dates)) != 1:
            raise ValueError("Comparison entities must share an observation date")
        metrics = {meta["metric"] for meta in metas}
        if topic == "strategies":
            if metrics != {"annual_net_yield"}:
                raise ValueError("Strategy returns must be annual net returns, never nightly asking rates")
        elif (any(meta["topic"] != topic for meta in metas) or len(metrics) != 1
              or len({meta["basis"] for meta in metas}) != 1):
            raise ValueError("Entity comparisons must use one topic, metric and basis")
        referenced = set(block["fact_ids"])
        capital = None
        if topic == "strategies" or any("capital_fact_id" in meta for meta in metas):
            capitals = {meta.get("capital_fact_id") for meta in metas}
            if len(capitals) != 1:
                raise ValueError("Yield rows must use the same capital fact")
            capital_key = capitals.pop()
            if capital_key not in facts or facts[capital_key]["unit"] != "₸" or facts[capital_key]["value"] <= 0:
                raise ValueError("A positive dated capital amount is required")
            capital = number_label(facts[capital_key]["value"], "₸")
            referenced.add(capital_key)
            for meta in metas:
                if any(key not in facts for key in meta.get("input_fact_ids", [])):
                    raise ValueError("Unknown frozen yield input")
                referenced.update(meta.get("input_fact_ids", []))
        for fact, meta in zip(entries, metas):
            source = sources[fact["source_id"]]
            if meta["basis"] == "official_terms" and source["kind"] != "official_terms":
                raise ValueError("Official conditions must cite an official publication")
            if meta["basis"] == "scenario" and source["kind"] != "calculation_scenario":
                raise ValueError("Calculated returns must be explicitly identified as scenarios")
            if meta["basis"] == "observed_asking":
                minimum = 20 if topic == "places" else 10
                if (source["kind"] != "hatuli_observations" or type(fact["n"]) is not int
                        or fact["n"] < minimum or not published - timedelta(hours=72) <= utc(fact["as_of"]) <= published):
                    raise ValueError("Local comparisons require fresh independent observed cohorts")
        used.update(referenced)
        period = max(dates)
        result.append({"topic": topic, "topic_label": TOPICS[topic],
                       "title": resolve(block["title"], facts, 10, 180),
                       "profile_label": metas[0]["profile_label"],
                       "metric_label": "Чистый результат за год к капиталу" if topic == "strategies" else METRICS[metas[0]["metric"]][0],
                       "as_of": period.isoformat(), "as_of_label": period.strftime("%d.%m.%Y"),
                       "capital_label": capital,
                       "conclusion": resolve(block["conclusion"], facts, 30, 600),
                       "tradeoff": resolve(block["tradeoff"], facts, 30, 600),
                       "rows": [{"entity_name": meta["entity_name"], "value_label": number_label(fact["value"], fact["unit"]),
                                 "basis_label": BASES[meta["basis"]], "n": fact["n"],
                                 "checked_label": utc(fact["as_of"]).astimezone(ZoneInfo("Asia/Almaty")).strftime("%d.%m.%Y %H:%M UTC+5"),
                                 "source_url": sources[fact["source_id"]]["url"],
                                 "source_label": sources[fact["source_id"]]["label"]}
                                for fact, meta in zip(entries, metas)]})
    return result, used
