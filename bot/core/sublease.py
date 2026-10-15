"""Manual sublease scenarios. Asking prices and occupancy are not outcomes.

No database, network, tax advice, or inferred occupancy belongs in this module.
Rates are fractions (0.10 means 10%); money is KZT. A month is 30.4 nights.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re

DAYS = Decimal("30.4")
SCENARIOS = (0.45, 0.60, 0.75)
FURNISHING = ("none", "partial", "full", "unknown")


class SubleaseError(ValueError):
    def __init__(self, field: str, message: str):
        self.errors = {field: message}
        super().__init__(message)


def number(value, field, low, high, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise SubleaseError(field, "Введите число.")
    try:
        if len(str(value)) > 48:
            raise InvalidOperation()
        result = Decimal(str(value).strip())
        if not result.is_finite() or not Decimal(str(low)) <= result <= Decimal(str(high)):
            raise InvalidOperation()
        if integer and result != result.to_integral_value():
            raise InvalidOperation()
    except (InvalidOperation, ValueError, OverflowError):
        raise SubleaseError(field, f"Допустимо от {low} до {high}.") from None
    return result


def rounded(value: Decimal, places=0):
    result = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return int(result) if places == 0 else float(result)


@dataclass(frozen=True)
class SubleaseParams:
    occupancy: float
    avg_stay_nights: float
    cleaning_per_turn: int
    consumables_per_night: int
    utilities_month: int
    platform_fee_pct: float
    tax_pct: float
    furnish_capex: int
    capex_months: int
    deposit_months: float

    def __post_init__(self):
        bounds = {
            "occupancy": (0, 1, False), "avg_stay_nights": (.25, 365, False),
            "cleaning_per_turn": (0, 2_000_000, True),
            "consumables_per_night": (0, 2_000_000, True),
            "utilities_month": (0, 20_000_000, True),
            "platform_fee_pct": (0, 1, False), "tax_pct": (0, 1, False),
            "furnish_capex": (0, 500_000_000, True), "capex_months": (1, 240, True),
            "deposit_months": (0, 24, False),
        }
        for field, (low, high, integer) in bounds.items():
            value = number(getattr(self, field), field, low, high, integer=integer)
            object.__setattr__(self, field, int(value) if integer else float(value))
        if Decimal(str(self.platform_fee_pct)) + Decimal(str(self.tax_pct)) > 1:
            raise SubleaseError("tax_pct", "Сумма налога и комиссии не может превышать 100%.")


def _inputs(rent_month, price, furnishing, p):
    rent = number(rent_month, "rent_month", 1, 20_000_000, integer=True)
    price = number(price, "nightly", 1, 20_000_000, integer=True)
    if furnishing not in FURNISHING:
        raise SubleaseError("furnishing", "Выберите состояние мебели.")
    if not isinstance(p, SubleaseParams):
        raise SubleaseError("params", "Нужны параметры расчёта.")
    values = {key: Decimal(str(value)) for key, value in asdict(p).items()}
    # Full furnishing never consumes a hypothetical refurbishment budget.
    values["furnish_capex"] = Decimal(0) if furnishing == "full" else values["furnish_capex"]
    return rent, price, values


def _result(rent, furnishing, p, values, revenue, variable, fixed, contribution, *, strategy):
    margin = revenue - variable - fixed
    breakeven = fixed / contribution if contribution > 0 else None
    startup = rent * (1 + values["deposit_months"]) + values["furnish_capex"]
    used = asdict(p)
    used["furnish_capex"] = int(values["furnish_capex"])
    return {
        "strategy": strategy, "days_per_month": 30.4, "revenue": rounded(revenue),
        "costs": rounded(variable + fixed), "margin": rounded(margin),
        "margin_pct": rounded(margin / revenue * 100, 1) if revenue else None,
        "breakeven_occupancy": rounded(breakeven, 6) if breakeven is not None else None,
        "breakeven_occupancy_exact": str(breakeven) if breakeven is not None else None,
        "breakeven_possible": breakeven is not None and breakeven <= 1,
        "startup_capital": rounded(startup),
        "refundable_deposit": rounded(rent * values["deposit_months"]),
        "deposit_is_expense": False, "furnishing": furnishing,
        "payback_months": rounded(startup / margin, 1) if margin > 0 else None,
        "params_used": used, "index_source": {"kind": "manual"},
        "scenario_only": True,
    }


def sublease_economics(rent_month: int, nightly: int, furnishing: str, p: SubleaseParams) -> dict:
    rent, nightly, v = _inputs(rent_month, nightly, furnishing, p)
    nights = DAYS * v["occupancy"]
    revenue = nightly * nights
    turns = nights / v["avg_stay_nights"]
    variable = (turns * v["cleaning_per_turn"] + nights * v["consumables_per_night"]
                + revenue * (v["platform_fee_pct"] + v["tax_pct"]))
    fixed = rent + v["utilities_month"] + v["furnish_capex"] / v["capex_months"]
    per_night = (nightly * (1 - v["platform_fee_pct"] - v["tax_pct"])
                 - v["consumables_per_night"] - v["cleaning_per_turn"] / v["avg_stay_nights"])
    result = _result(rent, furnishing, p, v, revenue, variable, fixed, per_night * DAYS, strategy="daily")
    result.update(nights=rounded(nights, 3), turns=rounded(turns, 3),
                  nightly=int(nightly), rent_month=int(rent))
    return result


def scenario_economics(rent_month, nightly, furnishing, params, *, scenarios=SCENARIOS):
    if not isinstance(scenarios, (tuple, list)) or not 1 <= len(scenarios) <= 5:
        raise SubleaseError("scenarios", "Нужно от одного до пяти сценариев.")
    occupancies = [float(number(value, "occupancy", 0, 1)) for value in scenarios]
    if len(set(occupancies)) != len(occupancies):
        raise SubleaseError("scenarios", "Сценарии не должны повторяться.")
    return [sublease_economics(rent_month, nightly, furnishing, replace(params, occupancy=value))
            for value in occupancies]


def occupancy_scenarios(settings):
    raw = settings.get("SUBLEASE_OCC_SCENARIOS", "0.45,0.60,0.75")
    if not isinstance(raw, str) or len(raw) > 120:
        raise SubleaseError("scenarios", "Проверьте настройки сценариев загрузки.")
    values = tuple(float(number(value.strip(), "scenarios", 0, 1)) for value in raw.split(","))
    if not 1 <= len(values) <= 5 or len(set(values)) != len(values):
        raise SubleaseError("scenarios", "Проверьте настройки сценариев загрузки.")
    return values


def mid_term_economics(rent_month, furnished_month, furnishing, p, *, vacancy_fraction, turnovers_per_year):
    """Explicit vacancy/turnovers only; source removals cannot estimate them."""
    rent, target, v = _inputs(rent_month, furnished_month, furnishing, p)
    vacancy = number(vacancy_fraction, "vacancy_fraction", 0, 1)
    turnovers = number(turnovers_per_year, "turnovers_per_year", 0, 365)
    occupied = 1 - vacancy
    revenue = target * occupied
    variable = (revenue * (v["platform_fee_pct"] + v["tax_pct"])
                + DAYS * occupied * v["consumables_per_night"])
    fixed = (rent + v["utilities_month"] + v["furnish_capex"] / v["capex_months"]
             + v["cleaning_per_turn"] * turnovers / 12)
    contribution = target * (1 - v["platform_fee_pct"] - v["tax_pct"]) - DAYS * v["consumables_per_night"]
    result = _result(rent, furnishing, p, v, revenue, variable, fixed, contribution, strategy="mid_term")
    result["params_used"].update(occupancy=float(occupied), vacancy_fraction=float(vacancy),
                                 turnovers_per_year=float(turnovers))
    result["params_used"].pop("avg_stay_nights")
    result.update(rent_month=int(rent), furnished_month=int(target))
    return result


SETTING_FIELDS = {
    "avg_stay_nights": "SUBLEASE_AVG_STAY", "consumables_per_night": "SUBLEASE_CONSUMABLES_PER_NIGHT",
    "platform_fee_pct": "SUBLEASE_PLATFORM_FEE", "capex_months": "SUBLEASE_FURNISH_CAPEX_MONTHS",
    "deposit_months": "SUBLEASE_DEPOSIT_MONTHS",
}
ROOM_SETTINGS = {"cleaning_per_turn": "SUBLEASE_CLEANING_BY_ROOMS",
                 "utilities_month": "SUBLEASE_UTILITIES_BY_ROOMS", "furnish_capex": "SUBLEASE_FURNISH_CAPEX_BY_ROOMS"}


def params_from_inputs(fields, settings, *, rooms=None, furnishing="unknown", now=None):
    """No missing tax or cost is silently converted to zero."""
    now = now or datetime.now(timezone.utc)
    values, sources = {}, {}
    for field in SubleaseParams.__dataclass_fields__:
        if field in fields and fields[field] not in (None, ""):
            values[field], sources[field] = fields[field], "user"
            continue
        if field == "furnish_capex" and furnishing == "full":
            values[field], sources[field] = 0, "full_furnishing"
            continue
        if field == "occupancy":
            values[field], sources[field] = .60, "scenario"
            continue
        setting = SETTING_FIELDS.get(field)
        value = settings.get(setting, "") if setting else ""
        if field in ROOM_SETTINGS:
            try:
                mapping = json.loads(settings.get(ROOM_SETTINGS[field], ""))
                value = mapping.get(str(rooms), "") if isinstance(mapping, dict) else ""
            except (ValueError, TypeError):
                value = ""
        if field == "tax_pct":
            checked = settings.get("SUBLEASE_TAX_CHECKED_AT", "")
            digest = settings.get("SUBLEASE_TAX_REFERENCE_HASH", "")
            try:
                date = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
                dated = date.tzinfo is not None and date <= now
            except (ValueError, TypeError):
                dated = False
            operator = (settings.get("SUBLEASE_TAX_VERIFIED", "") == "1" and dated
                        and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest))
            value = settings.get("SUBLEASE_TAX_PCT", "") if operator else ""
        if value in (None, ""):
            raise SubleaseError(field, "Укажите значение: подходящая настройка не задана.")
        values[field], sources[field] = value, "operator_setting"
    params = SubleaseParams(**values)
    return params, sources


def evaluate_sublease_alert(candidate, params):
    """Stage-1 queue handoff only. Does not deliver or mutate subscriptions."""
    try:
        threshold = number(params.get("breakeven_occupancy_max"), "threshold", 0, 1)
        economics = candidate.get("sublease_economics") or {}
        source = economics.get("index_source") or {}
        observed = candidate.get("last_seen")
        if isinstance(observed, str):
            observed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if (candidate.get("rent_period") != "monthly" or candidate.get("rent_period_conflict") is not False
                or candidate.get("is_active") is not True or candidate.get("is_duplicate") is not False
                or candidate.get("dup_needs_review") is not False
                or not isinstance(observed, datetime) or observed.tzinfo is None
                or not 0 <= (now - observed).total_seconds() <= 72 * 3600
                or economics.get("strategy") != "daily" or source.get("confidence") not in ("high", "medium")
                or source.get("rent_period") != "daily" or economics.get("breakeven_possible") is not True):
            return None
        if type(candidate.get("property_id")) is not int or candidate["property_id"] <= 0:
            return None
        minimum = 5 if source.get("level") == "complex" else 10
        if (source.get("level") not in ("complex", "district") or type(source.get("n")) is not int
                or source["n"] < minimum or source.get("policy_version") != "daily_v1"
                or source.get("window_days") not in (30, 90)
                or type(source.get("active_supply")) is not int or source["active_supply"] <= 0):
            return None
        dates = [datetime.fromisoformat(str(source.get(key)).replace("Z", "+00:00"))
                 for key in ("source_oldest_at", "source_newest_at", "updated_at")]
        if any(date.tzinfo is None for date in dates):
            return None
        oldest, newest, updated = dates
        if not (0 <= (now-updated).total_seconds() <= 72*3600
                and 0 <= (now-newest).total_seconds() <= 72*3600
                and oldest <= newest <= updated and 0 <= (now-oldest).total_seconds() <= source["window_days"]*86400):
            return None
        breakeven = number(economics.get("breakeven_occupancy_exact"), "breakeven", 0, 1)
        if breakeven > threshold:
            return None
        if params.get("districts") and candidate.get("district") not in params["districts"]:
            return None
        if params.get("rooms") is not None and candidate.get("rooms") != params["rooms"]:
            return None
        rent = number(candidate.get("price"), "price", 1, 20_000_000, integer=True)
        if params.get("max_rent") is not None and rent > number(params["max_rent"], "max_rent", 1, 20_000_000):
            return None
        identifier = str(candidate.get("id") or "")
        if not re.fullmatch(r"[0-9]{5,20}", identifier):
            return None
        return {"listing_id": identifier, "url": "/rental/scanner?listing_id=" + identifier,
                "rent_month": int(rent), "breakeven_occupancy": float(breakeven),
                "margin": int(number(economics.get("margin"), "margin", -100_000_000, 100_000_000, integer=True)),
                "scenario_only": True, "occupancy_assumed": economics.get("params_used", {}).get("occupancy")}
    except (SubleaseError, ValueError, TypeError, AttributeError):
        return None
