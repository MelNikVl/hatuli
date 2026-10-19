"""Pure, cash-only comparison of explicit property scenarios and verified deposits.

Every strategy starts with the same budget and a twelve-month horizon. Unspent
cash earns no interest. Property exit values, occupancy and rates are user
scenarios, never predictions or observations. No database or source requests.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from bot.core.deposit_comparison import GUARANTEE_SOURCE, compare_investment

HORIZON_MONTHS = 12
MODEL_DAYS = 365
MAX_MONEY = 1_000_000_000
_LABELS = {"monthly": "Помесячная аренда", "daily": "Посуточная аренда",
           "construction": "Вложение в стройку"}
_PURCHASE = {"purchase_price": (1, MAX_MONEY), "repair_cost": (0, MAX_MONEY),
             "buy_cost": (0, MAX_MONEY), "annual_owner_cost": (0, MAX_MONEY),
             "wait_months": (0, 60), "exit_value": (0, MAX_MONEY),
             "sell_cost_pct": (0, 100)}
_FIELDS = {
    "monthly": {**_PURCHASE, "monthly_rent": (0, 10_000_000), "vacancy_months": (0, 12)},
    "daily": {**_PURCHASE, "adr": (0, 1_000_000), "occupancy_pct": (0, 100),
              "platform_fee_pct": (0, 100), "management_fee_pct": (0, 100),
              "cleaning_cost_per_stay": (0, 1_000_000), "stays_per_month": (0, 31)},
    "construction": {"entry_price": (1, MAX_MONEY), "finishing_cost": (0, MAX_MONEY),
                     "buy_cost": (0, MAX_MONEY), "annual_owner_cost": (0, MAX_MONEY),
                     "wait_months": (0, 60), "exit_value": (0, MAX_MONEY),
                     "sell_cost_pct": (0, 100)},
}


def _number(value: Any, field: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{field}: требуется число")
    if isinstance(value, str) and (not value.strip() or len(value) > 80):
        raise ValueError(f"{field}: требуется число")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{field}: требуется число") from None
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{field}: допустимый диапазон {low:g}–{high:g}")
    return result


def _money(value: float) -> float:
    return round(value, 2)


def _funded_occupancy_floor(values: dict, cash_remainder: float) -> float:
    """Lowest occupancy that funds every interim month from this budget."""
    gross = fixed = floor = 0.0
    fee_multiplier = 1 - (values['platform_fee_pct'] + values['management_fee_pct']) / 100
    for month in range(1, HORIZON_MONTHS + 1):
        available = min(1.0, max(0.0, month - values['wait_months']))
        gross += values['adr'] * MODEL_DAYS / HORIZON_MONTHS * available * fee_multiplier
        fixed += values['annual_owner_cost'] / HORIZON_MONTHS
        fixed += values['cleaning_cost_per_stay'] * values['stays_per_month'] * available
        needed = fixed - cash_remainder
        if needed > 0:
            if gross <= 0:
                return math.inf
            floor = max(floor, needed / gross * 100)
    return floor


def _occupancy_threshold(needed: float, after_fees: float, stays_per_month: float,
                         funding_floor: float = 0.0) -> float | None:
    if after_fees <= 0:
        return None
    result = max(stays_per_month * HORIZON_MONTHS / MODEL_DAYS * 100,
                 needed / after_fees * 100, funding_floor, 0)
    # Round up: a displayed funding threshold must pass when entered again.
    scaled = result * 10_000
    return math.ceil(scaled) / 10_000 if math.isfinite(scaled) else None


def _threshold(key: str, metric: str, value: float | None, benchmark_key: str, basis: str) -> dict:
    attainable = value is not None and (metric != "occupancy_pct" or value <= 100)
    status = "not_calculable" if value is None else "attainable" if attainable else "unattainable"
    message = ("При введённых параметрах порог не рассчитывается" if value is None else
               "Порог выше 100% загрузки и недостижим" if not attainable else
               "Порог рассчитан только для введённых параметров сценария")
    return {"key": key, "metric": metric, "value": value, "status": status,
            "benchmark_key": benchmark_key, "basis": basis, "message": message}


def _diagnostic(key: str, status: str, fields: list[str], message: str) -> dict:
    return {"key": key, "strategy": key, "status": status, "fields": fields, "message": message}


def _parse(key: str, supplied: Any) -> tuple[dict | None, dict | None]:
    if not isinstance(supplied, dict):
        return None, _diagnostic(key, "invalid", [], "Требуется объект параметров стратегии")
    missing = [name for name in _FIELDS[key] if supplied.get(name) is None or supplied.get(name) == ""]
    if missing:
        return None, _diagnostic(key, "incomplete", missing, "Заполните все параметры выбранного сценария")
    values = {}
    for field, (low, high) in _FIELDS[key].items():
        try:
            values[field] = _number(supplied[field], field, low, high)
        except ValueError as exc:
            return None, _diagnostic(key, "invalid", [field], str(exc))
    if key == "daily":
        if values["platform_fee_pct"] + values["management_fee_pct"] > 100:
            return None, _diagnostic(key, "invalid", ["platform_fee_pct", "management_fee_pct"],
                                     "Сумма комиссий не должна превышать 100% выручки")
        # One stay requires at least one occupied night. Stays describe an
        # operating month; waiting reduces both bookings and nights later.
        nights_per_month = MODEL_DAYS / HORIZON_MONTHS * values["occupancy_pct"] / 100
        if values["stays_per_month"] > nights_per_month + 1e-9:
            return None, _diagnostic(key, "invalid", ["stays_per_month", "occupancy_pct"],
                                     "Заездов не может быть больше занятых ночей")
    return values, None


def _timeline(values: dict, key: str) -> tuple[list[dict], float, float, float]:
    ready_months = max(0.0, HORIZON_MONTHS - values["wait_months"])
    earning_months = max(0.0, ready_months - values.get("vacancy_months", 0))
    vacancy_multiplier = earning_months / ready_months if ready_months else 0
    timeline = []
    cumulative = gross_total = expenses_total = 0.0
    for month in range(1, HORIZON_MONTHS + 1):
        available = min(1.0, max(0.0, month - values["wait_months"]))
        gross = 0.0
        expenses = values["annual_owner_cost"] / HORIZON_MONTHS
        if key == "monthly":
            gross = values["monthly_rent"] * available * vacancy_multiplier
        elif key == "daily":
            gross = values["adr"] * MODEL_DAYS / HORIZON_MONTHS * available * values["occupancy_pct"] / 100
            expenses += gross * (values["platform_fee_pct"] + values["management_fee_pct"]) / 100
            expenses += values["cleaning_cost_per_stay"] * values["stays_per_month"] * available
        net = gross - expenses
        cumulative += net
        gross_total += gross
        expenses_total += expenses
        timeline.append({"month": month, "gross_income": _money(gross),
                         "operating_expenses": _money(expenses), "net_cash": _money(net),
                         "cumulative_net_cash": _money(cumulative)})
    return timeline, gross_total, expenses_total, ready_months


def _property_strategy(key: str, values: dict, budget: float) -> tuple[dict | None, dict | None]:
    purchase = values["entry_price"] if key == "construction" else values["purchase_price"]
    improvements = values["finishing_cost"] if key == "construction" else values["repair_cost"]
    initial = purchase + improvements + values["buy_cost"]
    if initial > budget:
        diagnostic = _diagnostic(key, "unaffordable", [], "Начальные расходы превышают общий бюджет")
        diagnostic.update(required_budget=_money(initial), shortfall=_money(initial - budget))
        return None, diagnostic
    timeline, gross, expenses, ready_months = _timeline(values, key)
    operating = gross - expenses
    exit_cost = values["exit_value"] * values["sell_cost_pct"] / 100
    remainder = budget - initial
    asset_change = values["exit_value"] - purchase
    wealth = remainder + values["exit_value"] - exit_cost + operating
    profit = wealth - budget
    minimum_cash = min(remainder, *(remainder + item["cumulative_net_cash"] for item in timeline))
    funding_gap = max(0, -minimum_cash)
    if funding_gap > 0:
        diagnostic = _diagnostic(key, "insufficient_reserve", [],
                                 "Остатка общего бюджета не хватает на расходы до выхода; увеличьте бюджет или измените сценарий")
        diagnostic.update(required_budget=_money(budget + funding_gap), shortfall=_money(funding_gap))
        return None, diagnostic
    assumptions = [
        "Все параметры недвижимости введены пользователем; это сценарий, а не прогноз.",
        "Остаток бюджета хранится без процентов; ипотека и заёмные деньги не учитываются.",
        "Ремонт и расходы покупки оплачены в начале; ремонт сам по себе не увеличивает цену выхода.",
        "Чистый денежный поток — аренда минус эксплуатационные расходы; покупка и ремонт учтены отдельно в итоговом результате.",
        "Годовые расходы включают введённые налоги, содержание, страхование и обслуживание; дополнительные расходы автоматически не подставляются.",
        "Цена выхода введена для конца года; реализация по этой цене и срок продажи не гарантированы.",
        "График показывает модель денежного потока: расходы распределены равномерно, цена актива помесячно не прогнозируется.",
    ]
    if key == "monthly":
        assumptions.append("Указанный простой равномерно уменьшает доход месяцев после готовности; точные даты заселения неизвестны.")
    elif key == "daily":
        assumptions.extend([
            "Модель года — 365 дней. ADR — введённая ставка за занятую ночь, загрузка — сценарий, а не спрос по объявлениям.",
            "Комиссии площадки и управления начислены отдельно на выручку; уборка оплачивается владельцем по числу заездов.",
            "Расходы и плату за уборку, возмещаемые гостем, нельзя одновременно учитывать как расходы владельца без соответствующей выручки.",
        ])
    else:
        assumptions.extend([
            "Стройка моделируется без арендного дохода в течение года, включая время после готовности.",
            "Сумма отделки зарезервирована и оплачена в начале расчёта; график рассрочки не моделируется.",
            "Если готовность позже года, цена выхода относится к праву требования или переуступке по вашему сценарию, а не к готовой квартире.",
            "Возможность и условия переуступки, согласие застройщика и доступность покупателя этим расчётом не проверяются.",
        ])
    row = {"key": key, "label": _LABELS[key], "status": "ready", "evidence_type": "user_scenario",
           "initial_outlay": _money(initial), "cash_remainder": _money(remainder),
           "gross_income": _money(gross), "operating_expenses": _money(expenses),
           "net_operating_cash": _money(operating), "asset_value_change": _money(asset_change),
           "upfront_expenses": _money(improvements + values["buy_cost"]),
           "exit_value": _money(values["exit_value"]), "exit_cost": _money(exit_cost),
           "end_wealth": _money(wealth), "total_profit": _money(profit),
           "total_return_pct": round(profit / budget * 100, 4),
           "net_cash_yield_pct": round(operating / budget * 100, 4),
           "ready_months": round(ready_months, 4), "inputs": dict(values),
           "minimum_cash_balance": _money(minimum_cash), "additional_funding_required": 0,
           "assumptions": assumptions, "timeline": timeline}
    if key == "construction":
        row["asset_stage"] = "ready_scenario" if values["wait_months"] <= HORIZON_MONTHS else "unfinished_right_scenario"
        row["delay_sensitivity"] = [{"extra_months": delay,
                                      "ready_by_horizon": values["wait_months"] + delay <= HORIZON_MONTHS,
                                      "rental_income": 0, "exit_value_reestimated": False}
                                     for delay in (0, 3, 6)]
    return row, None


def _bank_strategies(budget: float, catalog: dict | None, now: datetime | None) -> tuple[list[dict], list[dict]]:
    # Keep one eligibility, guarantee and annual money implementation. In
    # particular, never turn the separate short-term APY benchmark into a
    # twelve-month rollover promise.
    existing = compare_investment({"price": budget, "repair_cost": 0, "buy_cost": 0,
                                   "monthly_rent": 0, "annual_owner_cost": 0,
                                   "vacancy_months": 0, "wait_months": 0,
                                   "annual_growth_pct": 0, "sell_cost_pct": 0,
                                   "mode": "rent_out"}, catalog=catalog, now=now)
    banks = []
    diagnostics = []
    for source in existing["banks"]:
        if not source["eligible"]:
            diagnostics.append(_diagnostic(f"bank:{source['key']}", "unavailable", [], source["reason"]))
            continue
        income = source["deposit_income"]
        banks.append({**source, "key": f"bank:{source['key']}", "bank_key": source["key"],
                      "label": source["bank"], "status": "ready", "evidence_type": "verified_bank_terms",
                      "initial_outlay": _money(budget), "cash_remainder": 0,
                      "gross_income": income, "operating_expenses": 0,
                      "net_operating_cash": income, "asset_value_change": 0,
                      "upfront_expenses": 0, "exit_value": _money(budget), "exit_cost": 0,
                      "end_wealth": source["deposit_end_value"], "total_profit": income,
                      "total_return_pct": source["effective_pct"], "net_cash_yield_pct": source["effective_pct"],
                      "timeline": None, "assumptions": [
                          "Проверенные условия фиксированного вклада в тенге ровно на 12 месяцев; ставка применена однократно по ГЭСВ.",
                          "Расчёт при сохранении всей суммы и процентов до окончания срока; досрочное снятие не моделируется.",
                          "Помесячный график выплат не строится без подтверждённого графика продукта.",
                      ]})
    return banks, diagnostics


def calculate_strategy_comparison(payload: dict, catalog: dict | None = None,
                                  now: datetime | None = None) -> dict:
    """Return ready rows only; a bad property scenario does not hide the rest.

    ``payload`` has a required ``budget`` and optional ``monthly``, ``daily``
    and ``construction`` objects. Every numeric field of a supplied property
    object is explicit. ``horizon_months``, if present, must equal twelve.
    Missing/ineligible rows are diagnostic data, not numeric public results.
    """
    if not isinstance(payload, dict):
        raise ValueError("Требуется объект параметров")
    budget = _number(payload.get("budget"), "budget", 1, MAX_MONEY)
    horizon = _number(payload.get("horizon_months", HORIZON_MONTHS), "horizon_months", 12, 12)
    if now is not None and (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None):
        raise ValueError("now: требуется дата с часовым поясом")
    now = now or datetime.now(timezone.utc)
    strategies = []
    diagnostics = []
    for key in _LABELS:
        if key not in payload:
            continue
        values, diagnostic = _parse(key, payload[key])
        if diagnostic is None:
            row, diagnostic = _property_strategy(key, values, budget)
            if row is not None:
                strategies.append(row)
        if diagnostic is not None:
            diagnostics.append(diagnostic)
    banks, bank_diagnostics = _bank_strategies(budget, catalog, now)
    diagnostics.extend(bank_diagnostics)
    best_bank = max(banks, key=lambda row: row["total_profit"], default=None)
    monthly = next((row for row in strategies if row["key"] == "monthly"), None)
    for row in strategies:
        row["bank_comparison"] = None
        row["thresholds"] = []
        if best_bank is not None:
            row["bank_comparison"] = {
                "key": best_bank["key"], "label": best_bank["label"], "product": best_bank["product"],
                "effective_pct": best_bank["effective_pct"],
                "total_profit_difference": _money(row["total_profit"] - best_bank["total_profit"]),
                "operating_cash_difference": _money(row["net_operating_cash"] - best_bank["net_operating_cash"]),
                "scope": "Максимальный расчётный доход доступных подтверждённых годовых программ отслеживаемых банков; не весь рынок",
            }
        if row["key"] == "daily":
            values = row["inputs"]
            ready = max(0.0, HORIZON_MONTHS - values["wait_months"])
            full_gross = values["adr"] * MODEL_DAYS * ready / HORIZON_MONTHS
            after_fees = full_gross * (1 - (values["platform_fee_pct"] + values["management_fee_pct"]) / 100)
            fixed = values["annual_owner_cost"] + values["cleaning_cost_per_stay"] * values["stays_per_month"] * ready
            funding_floor = _funded_occupancy_floor(values, row['cash_remainder'])
            row['minimum_funded_occupancy_pct'] = round(funding_floor, 4) if math.isfinite(funding_floor) else None
            row["break_even_occupancy_pct"] = None
            row["break_even_cash_occupancy_pct"] = None
            if monthly is not None:
                value = _occupancy_threshold(monthly["net_operating_cash"] + fixed, after_fees, values["stays_per_month"], funding_floor)
                row["thresholds"].append(_threshold("daily_vs_monthly_cash", "occupancy_pct", value,
                                                     "monthly", "net_operating_cash"))
            if best_bank is not None:
                interest = best_bank["total_profit"]
                needed = interest + fixed + row["upfront_expenses"] + row["exit_cost"] - row["asset_value_change"]
                row["break_even_occupancy_pct"] = _occupancy_threshold(needed, after_fees, values["stays_per_month"], funding_floor)
                row["break_even_cash_occupancy_pct"] = _occupancy_threshold(interest + fixed, after_fees, values["stays_per_month"], funding_floor)
                row["thresholds"].extend([
                    _threshold("daily_vs_bank_total", "occupancy_pct", row["break_even_occupancy_pct"],
                               best_bank["key"], "total_profit"),
                    _threshold("daily_vs_bank_cash", "occupancy_pct", row["break_even_cash_occupancy_pct"],
                               best_bank["key"], "net_operating_cash"),
                ])
                row["assumptions"].append("Порог загрузки для итогового результата зависит от вашей цены выхода; число заездов и расходы при расчёте порога фиксированы. Порог выше 100% недостижим.")
            row['assumptions'].append('Минимальная загрузка также обеспечивает расходы каждого месяца из общего бюджета: результат при этом не ниже сравниваемого, но не обязательно точно равен ему.')
        elif row["key"] == "construction":
            row["minimum_exit_value_to_match_bank"] = None
            if best_bank is not None:
                sell_multiplier = 1 - row["inputs"]["sell_cost_pct"] / 100
                if sell_multiplier > 0:
                    needed = (row["initial_outlay"] + best_bank["total_profit"] - row["net_operating_cash"]) / sell_multiplier
                    if math.isfinite(needed):
                        row["minimum_exit_value_to_match_bank"] = _money(needed)
                row["thresholds"].append(_threshold("construction_vs_bank_total", "exit_value",
                                                     row["minimum_exit_value_to_match_bank"],
                                                     best_bank["key"], "total_profit"))
                row["assumptions"].append("Порог цены выхода уравнивает итоговый капитал с лучшим доступным годовым вкладом; достижимость этой цены не подтверждена.")
    return {"schema_version": 1, "budget": _money(budget), "initial_capital": _money(budget),
            "horizon_months": int(horizon), "model_days": MODEL_DAYS,
            "strategies": strategies, "banks": banks, "diagnostics": diagnostics,
            "best_bank_key": best_bank["key"] if best_bank else None,
            "guarantee_source": GUARANTEE_SOURCE,
            "assumptions": [
                "Одинаковый начальный бюджет и горизонт 12 месяцев, без кредита и инфляционной корректировки.",
                "Показаны только рассчитанные сценарии. Недостающие факты не заменяются средними рыночными числами.",
                "Итоговый капитал = остаток денег + стоимость выхода − расходы выхода + чистый операционный денежный поток.",
                "Доходность недвижимости — условный результат введённых параметров; проверенные условия вклада также не гарантируют досрочный результат.",
            ]}
