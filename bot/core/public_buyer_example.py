"""Explicit public projection of a manually checked, anonymized snapshot.

The public pages never query or serialize a private listing. Provenance with
source IDs stays outside this repository; the reviewed photo is a local asset.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path

SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "data" / "public_buyer_example.json"
_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря")
_PROPERTY_FIELDS = ("rooms", "area", "floor", "floors_total", "district", "complex_name", "renovation", "price", "currency", "photo_url", "photo_alt", "floor_note")
_COMPARABLE_FIELDS = ("label", "rooms", "area", "floor", "floors_total", "complex_name", "district", "price", "renovation", "observed_at")


def _positive(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Public example requires finite positive prices and areas")
    return float(value)


def _date_label(value: str) -> str:
    day = date.fromisoformat(value[:10])
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year} года"


def build_public_example(snapshot: dict) -> dict:
    """Allowlist fields; fail rather than manufacture missing observations."""
    if snapshot.get("schema_version") != 1 or snapshot.get("snapshot") is not True:
        raise ValueError("A reviewed snapshot is required")
    reviewed = datetime.fromisoformat(snapshot["reviewed_at"])
    observed = datetime.fromisoformat(snapshot["observed_at"])
    if reviewed.tzinfo is None or observed.tzinfo is None or observed > reviewed:
        raise ValueError("Invalid public snapshot dates")
    prop = {key: snapshot["property"].get(key) for key in _PROPERTY_FIELDS}
    if prop["currency"] != "KZT" or prop["photo_url"] != "/static/examples/astana-apartment-2026-10-09.jpg":
        raise ValueError("Only the reviewed local photo and KZT prices are public")
    price_per_m2 = _positive(prop["price"]) / _positive(prop["area"])
    history = []
    for item in snapshot["price_history"]:
        day = date.fromisoformat(item["date"])
        if day > reviewed.date():
            raise ValueError("Price observation is after the snapshot")
        history.append({"date": day.isoformat(), "price": _positive(item["price"]), "date_label": _date_label(item["date"])})
    if not history or [p["date"] for p in history] != sorted({p["date"] for p in history}):
        raise ValueError("Unique chronological price observations are required")
    selection = snapshot["selection"]
    comparables = []
    for item in snapshot["comparables"]:
        comp = {key: item.get(key) for key in _COMPARABLE_FIELDS}
        comp["price_per_m2"] = _positive(comp["price"]) / _positive(comp["area"])
        if comp["rooms"] != prop["rooms"] or comp["complex_name"] != prop["complex_name"] or not prop["area"] * .85 <= comp["area"] <= prop["area"] * 1.15:
            raise ValueError("Comparable contradicts the documented selection")
        if datetime.fromisoformat(comp["observed_at"]) > reviewed:
            raise ValueError("Comparable observed after snapshot")
        comp["observed_label"] = _date_label(comp["observed_at"])
        comparables.append(comp)
    if not 3 <= len(comparables) <= 5 or selection["shown_count"] != len(comparables) or selection["sample_count"] < len(comparables) or selection["price_basis"] != "asking":
        raise ValueError("Invalid public comparable selection")
    # Real dates determine x positions; dashed connections are not daily prices.
    days = [date.fromisoformat(p["date"]).toordinal() for p in history]
    prices = [p["price"] for p in history]
    padding = max((max(prices) - min(prices)) * .2, 100_000)
    low, high = min(prices) - padding, max(prices) + padding
    points = [{**p, "x": round(72 + 548 * (day - days[0]) / max(days[-1] - days[0], 1), 2), "y": round(40 + 110 * (high - p["price"]) / (high - low), 2)} for p, day in zip(history, days)]
    chart = {
        "points": points,
        "path": " ".join(f"{'M' if i == 0 else 'L'} {p['x']} {p['y']}" for i, p in enumerate(points)),
        "ticks": [{"y": round(40 + 110 * (high - price) / (high - low), 2), "label": f"{price / 1e6:g}".replace(".", ",")} for price in sorted(set(prices))],
        "first_label": history[0]["date_label"].replace(" 2026 года", ""),
        "last_label": history[-1]["date_label"].replace(" 2026 года", ""),
        "accessible_label": "Наблюдения заявленной цены: " + "; ".join(f"{p['date_label']}: {p['price'] / 1e6:g} млн тенге" for p in history),
    }
    relist = snapshot["relist"]
    return {
        "property": prop, "price_per_m2": price_per_m2,
        "reviewed_label": _date_label(snapshot["reviewed_at"]), "observed_label": _date_label(snapshot["observed_at"]),
        "price_history": history, "chart": chart, "comparables": comparables,
        "selection": {"description": selection["description"], "limitations": list(selection["limitations"]), "sample_count": selection["sample_count"]},
        "relist": {"status": relist["status"], "title": relist["title"], "reasoning": list(relist["reasoning"])},
        "conclusions": list(snapshot["conclusions"]),
        "preview_note": "Цена снижалась до 33 млн ₸ и вернулась к 34 млн ₸. Этаж нужно уточнить.",
        "questions": [
            "Какой этаж фактически? Карточка и описание объявления расходятся.",
            "Почему объявление снимали и возвращали 4 октября? Снятие само по себе не подтверждает продажу.",
            "Какие работы выполнены при ремонте и что из мебели входит в цену? В объявлении это заявления продавца.",
            "Совпадают ли фактическая площадь и планировка с документами на квартиру?",
        ],
        "sources": [{"name": s["name"], "url": s["url"] if s.get("url") == "https://krisha.kz/" else None} for s in snapshot["sources"]],
    }


def load_public_example() -> dict:
    return build_public_example(json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")))
