"""Public, cash-only 12-month scenarios. No DB, forecasts, or bank transactions.

Published effective annual rates are used once, without a second capitalization.
Apartment appreciation is a user scenario, not a forecast. Living and letting
have separate cash flows so avoided rent is never counted twice.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

CATALOG_PATH = Path(__file__).resolve().parents[2] / "var" / "deposit-catalog.json"
SCHEMA_VERSION = 1
MAX_RATE_AGE = timedelta(hours=48)
GUARANTEE_SOURCE = "https://new.kdif.kz/depozity-i-garantiya/garantiya/"
BANKS = (("freedom", "Freedom Bank"), ("forte", "ForteBank"),
         ("bereke", "Bereke Bank"), ("home_credit", "Home Credit Bank"))
_OFFICIAL = {"freedom": ("bankffin.kz", "https://bankffin.kz/ru/deposits/physical"),
             "forte": ("bank.forte.kz", "https://bank.forte.kz/ru/deposits"),
             "bereke": ("berekebank.kz", "https://berekebank.kz/ru/personal/deposits"),
             "home_credit": ("home.kz", "https://home.kz/deposits")}


def _source_is_official(offer: dict) -> bool:
    try:
        parsed = urlsplit(str(offer.get("source_url", "")))
        host = _OFFICIAL[offer["bank_key"]][0]
        return (parsed.scheme == "https" and parsed.hostname == host and parsed.netloc == host
                and not parsed.username and not parsed.password and not parsed.fragment)
    except (KeyError, ValueError, TypeError):
        return False


def _safe_source(offer: dict) -> str:
    return offer["source_url"] if _source_is_official(offer) else _OFFICIAL[offer["bank_key"]][1]


def _display_number(value: Any) -> int | float | None:
    try:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None
    except OverflowError:
        return None


def _number(value: Any, field: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field}: требуется число")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field}: требуется число") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{field}: допустимый диапазон {low:g}–{high:g}")
    return value


def _time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _label(value: Any) -> str:
    timestamp = _time(value)
    if timestamp is None:
        return "Нет подтверждённой проверки"
    return timestamp.astimezone(timezone(timedelta(hours=5))).strftime("%d.%m.%Y, %H:%M")


def load_deposit_catalog(path: Path | str | None = None) -> dict:
    try:
        source = Path(path or CATALOG_PATH)
        if source.stat().st_size > 2_000_000:
            raise ValueError("catalog too large")
        data = json.loads(source.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("offers"), list):
            return {"schema_version": SCHEMA_VERSION, "offers": [], "sources": []}
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        return {"schema_version": SCHEMA_VERSION, "offers": [], "sources": []}


def _eligibility(offer: dict, now: datetime, capital: float | None = None) -> str | None:
    checked = _time(offer.get("checked_at"))
    if offer.get("status") != "verified":
        return "Условия изменились или источник недоступен: нужна повторная проверка"
    if checked is None or checked > now + timedelta(minutes=5) or now - checked > MAX_RATE_AGE:
        return "Ставка не подтверждалась последние 48 часов"
    if offer.get("currency") != "KZT" or offer.get("term_months") != 12:
        return "Программа не рассчитана на полный срок 12 месяцев"
    if offer.get("rate_type") != "fixed":
        return "Плавающая ставка не даёт фиксированного дохода на год"
    if not _source_is_official(offer):
        return "Не подтверждён официальный источник банка"
    if not all(isinstance(offer.get(key), bool) for key in ("can_withdraw", "can_top_up")):
        return "Условия снятия и пополнения не подтверждены"
    if any(_display_number(offer.get(key)) is None for key in ("effective_pct", "nominal_pct", "min_amount", "guarantee_limit")):
        return "Числовые условия источника не прошли проверку"
    try:
        effective = _number(offer.get("effective_pct"), "ГЭСВ", 0, 30)
        nominal = _number(offer.get("nominal_pct"), "Номинальная ставка", 0, 30)
        if effective < nominal:
            return "ГЭСВ и номинальная ставка не согласованы"
        minimum = _number(offer.get("min_amount"), "Минимум вклада", 0, 10_000_000_000)
        maximum = offer.get("max_amount")
        if maximum is not None:
            maximum = _number(maximum, "Максимум вклада", minimum, 100_000_000_000)
        cap = _number(offer.get("guarantee_limit"), "Гарантия", 0, 20_000_000)
        if cap not in (10_000_000, 20_000_000):
            return "Тип гарантии не подтверждён"
        if offer.get("deposit_type") not in ("savings", "other") or cap != (20_000_000 if offer["deposit_type"] == "savings" else 10_000_000):
            return "Тип вклада и гарантия КФГД не согласованы"
        if capital is not None and (capital < minimum or (maximum is not None and capital > maximum)):
            return "Начальный капитал не соответствует ограничениям суммы вклада"
    except ValueError:
        return "Числовые условия источника не прошли проверку"
    return None


def _best_offers(catalog: dict, now: datetime, capital: float | None = None) -> list[dict]:
    result = []
    for key, name in BANKS:
        candidates = [dict(item) for item in catalog.get("offers", [])
                      if isinstance(item, dict) and item.get("bank_key") == key]
        available = [item for item in candidates if _eligibility(item, now, capital) is None]
        if available:
            offer = max(available, key=lambda item: (item["effective_pct"], item["can_withdraw"], item["can_top_up"]))
            reason = None
        elif candidates:
            # Show the last known comparable product for context, never calculate it.
            comparable = [item for item in candidates if item.get("term_months") == 12]
            offer = (comparable or candidates)[0]
            reason = _eligibility(offer, now, capital)
        else:
            offer = {"bank_key": key, "bank": name, "name": "Условия проверяются"}
            reason = "Нет подтверждённой программы на 12 месяцев"
        result.append({**offer, "key": key, "bank": name, "eligible": reason is None, "reason": reason})
    return result


def public_deposit_catalog(catalog: dict | None = None, now: datetime | None = None) -> dict:
    catalog = load_deposit_catalog() if catalog is None else catalog
    now = now or datetime.now(timezone.utc)
    banks = []
    for offer in _best_offers(catalog, now):
        banks.append({
            "key": offer["key"], "bank": offer["bank"], "product": offer.get("name"),
            "nominal_pct": _display_number(offer.get("nominal_pct")), "effective_pct": _display_number(offer.get("effective_pct")),
            "term_months": _display_number(offer.get("term_months")), "source_url": _safe_source(offer),
            "checked_at": offer.get("checked_at"), "checked_at_label": _label(offer.get("checked_at")),
            "status_label": "Проверено по сайту банка" if offer["eligible"] else offer["reason"],
            "eligible": offer["eligible"], "warnings": offer.get("warnings", []),
            "conditions": offer.get("conditions", []), "min_amount": _display_number(offer.get("min_amount")),
            "max_amount": _display_number(offer.get("max_amount")), "guarantee_cap": _display_number(offer.get("guarantee_limit")),
        })
    return {"updated_at_label": _label(catalog.get("checked_at")),
            "updated_at": catalog.get("checked_at"), "stale": not all(item["eligible"] for item in banks),
            "banks": banks, "guarantee_source": GUARANTEE_SOURCE,
            "monitor": catalog.get("monitor", {}), "tracked_programs": len(catalog.get("offers", []))}


def compare_investment(payload: dict, catalog: dict | None = None, now: datetime | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Требуется объект параметров")
    price = _number(payload.get("price"), "Цена квартиры", 1, 1_000_000_000)
    repair = _number(payload.get("repair_cost", 0), "Ремонт", 0, 500_000_000)
    buy_cost = _number(payload.get("buy_cost", 0), "Расходы покупки", 0, 100_000_000)
    rent = _number(payload.get("monthly_rent", 0), "Аренда в месяц", 0, 10_000_000)
    owner = _number(payload.get("annual_owner_cost", 0), "Расходы владения за год", 0, 100_000_000)
    vacancy = _number(payload.get("vacancy_months", 0), "Простой", 0, 12)
    growth = _number(payload.get("annual_growth_pct", 0), "Изменение цены за год", -90, 100)
    wait = _number(payload.get("wait_months", 0), "До готовности квартиры", 0, 60)
    sell_pct = _number(payload.get("sell_cost_pct", 0), "Расходы продажи, %", 0, 30)
    mode = payload.get("mode", "live")
    if mode not in ("live", "rent_out"):
        raise ValueError("Режим: live или rent_out")
    now = now or datetime.now(timezone.utc)
    catalog = load_deposit_catalog() if catalog is None else catalog
    capital = price + repair + buy_cost
    horizon = 12
    ready_wait = min(horizon, wait)
    rent_months = max(0, horizon - ready_wait - vacancy) if mode == "rent_out" else 0
    gross_rent = rent * rent_months
    buy_wait_rent = rent * ready_wait if mode == "live" else 0
    deposit_wait_rent = rent * horizon if mode == "live" else 0
    future_price = price * (1 + growth / 100)
    sale_cost = future_price * sell_pct / 100
    property_net_value = future_price - sale_cost
    property_cash = gross_rent - owner - buy_wait_rent
    property_wealth = property_net_value + property_cash
    future_purchase_cost = future_price + repair + buy_cost
    warnings = [
        "Изменение цены — ваш сценарий, не прогноз. Начальное значение — 0%.",
        "Расчёт без ипотеки. Ремонт и расходы покупки входят в одинаковый начальный капитал обоих вариантов.",
        "Ремонт считается расходом; автоматическое увеличение стоимости квартиры от ремонта не заложено.",
        "Доход вклада рассчитан однократно по опубликованной ГЭСВ при полном сроке и сохранении процентов.",
        "Квартира через год — оценка стоимости; продажа и срок поиска покупателя не гарантированы.",
        "Налоги, содержание и обслуживание квартиры включите в расходы владения; по умолчанию они равны 0.",
        "Лимит КФГД указан на один банк с процентами. Другие ваши счета в этом банке также занимают лимит.",
    ]
    if mode == "live":
        warnings.append("Аренда оплачивается из отдельного бюджета; досрочное снятие средств с вклада не моделируется.")
    if wait >= horizon:
        warnings.append("Квартира не будет готова в пределах года: доход от аренды и экономия на проживании не начислены.")
    if mode == "rent_out" and vacancy > horizon - ready_wait:
        warnings.append("Простой ограничен оставшимся после готовности временем; доход от аренды равен 0.")
    banks = []
    for offer in _best_offers(catalog, now, capital):
        row = {"key": offer["key"], "bank": offer["bank"], "product": offer.get("name"),
               "eligible": offer["eligible"], "reason": offer["reason"],
               "status": "eligible" if offer["eligible"] else "unavailable",
               "nominal_pct": _display_number(offer.get("nominal_pct")), "effective_pct": _display_number(offer.get("effective_pct")),
               "term_months": _display_number(offer.get("term_months")), "source_url": _safe_source(offer),
               "checked_at": offer.get("checked_at"), "checked_at_label": _label(offer.get("checked_at")),
               "conditions": offer.get("conditions", []), "warnings": list(offer.get("warnings", []))}
        if offer["eligible"]:
            income = capital * offer["effective_pct"] / 100
            deposit_end = capital + income
            after_rent = deposit_end - deposit_wait_rent
            break_even = ((after_rent - property_cash) / (price * (1 - sell_pct / 100)) - 1) * 100
            guarantee = min(deposit_end, offer["guarantee_limit"])
            row.update(deposit_income=round(income, 2), deposit_end_value=round(deposit_end, 2),
                       wait_rent_cost=round(deposit_wait_rent, 2), deposit_cash_after_rent=round(after_rent, 2),
                       property_advantage=round(property_wealth - after_rent, 2),
                       break_even_growth_pct=round(break_even, 4),
                       future_purchase_cost=round(future_purchase_cost, 2),
                       future_purchase_surplus=round(after_rent - future_purchase_cost, 2),
                       guaranteed_amount=round(guarantee, 2),
                       unguaranteed_amount=round(max(0, deposit_end - guarantee), 2),
                       guarantee_cap=offer["guarantee_limit"])
            if deposit_end > guarantee:
                row["warnings"].append("Часть начального капитала и процентов превышает гарантию КФГД в этом банке.")
        banks.append(row)
    return {"assumptions": {"horizon_months": horizon, "price": price, "repair_cost": repair,
             "buy_cost": buy_cost, "monthly_rent": rent, "annual_owner_cost": owner,
             "vacancy_months": vacancy, "annual_growth_pct": growth, "wait_months": wait,
             "sell_cost_pct": sell_pct, "mode": mode},
            "initial_capital": round(capital, 2), "future_apartment_price": round(future_price, 2),
            "property": {"end_value": round(future_price, 2), "sale_cost": round(sale_cost, 2),
              "net_value": round(property_net_value, 2), "gross_rent": round(gross_rent, 2),
              "net_rent": round(gross_rent - owner, 2), "holding_cost": owner,
              "wait_rent_cost": round(buy_wait_rent, 2), "wealth": round(property_wealth, 2),
              "rent_months": rent_months, "avoided_rent": round(deposit_wait_rent - buy_wait_rent, 2)},
            "banks": banks, "warnings": warnings, "guarantee_source": GUARANTEE_SOURCE}
