"""Bounded observed facts from a valid full response; no free text/contacts."""
from datetime import datetime, timezone
import math
from bot.core.rental_stage0_identity import RentalStage0MigrationRequired


class RentalSourceDetailsMigrationRequired(RentalStage0MigrationRequired):
    def __init__(self):
        RuntimeError.__init__(self,"Rental full details require migration 114_rental_source_details.sql; no source request was started")


def _number(value, low, high, *, integer=False):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number) or not low <= number <= high or (integer and not number.is_integer()):
        return None
    return int(number) if integer else number


def rental_source_details(details, *, now=None):
    """Unknown/invalid fields are absent, never guessed from another listing."""
    if details.get("source_detail_valid") is not True or details.get("is_archived"):
        return None
    identity = details.get("listing_identity") or {}
    if not isinstance(identity, dict) or identity.get("sale_astana") is True:
        return None
    now = now or datetime.now(timezone.utc)
    snapshot = {"version":1}
    specs = {
        "rooms":(identity.get("rooms"),1,20,True),
        "area":(identity.get("area") if identity.get("area") is not None else details.get("area_total"),.1,1000000,False),
        "floor":(details.get("floor"),0,200,True),
        "floors_total":(details.get("floors_total"),1,200,True),
        "year_built":(details.get("year_built"),1800,now.year+1,True),
        "kitchen_area":(details.get("kitchen_area"),.1,1000,False),
        "ceiling_height":(details.get("ceiling_height"),2,5,False),
        "price":(identity.get("price"),1,2147483647,True),
    }
    for name,(value,low,high,integer) in specs.items():
        value = _number(value,low,high,integer=integer)
        if value is not None:
            snapshot[name] = value
    if snapshot.get("floor",0) > snapshot.get("floors_total",200):
        snapshot.pop("floor",None); snapshot.pop("floors_total",None)
    if snapshot.get("kitchen_area",0) > snapshot.get("area",1000000):
        snapshot.pop("kitchen_area",None)
    if type(details.get("description_observed")) is bool:
        snapshot["description_observed"] = details["description_observed"]
    if details.get("building_type") in ("кирпичный","панельный","монолитный","блочный"):
        snapshot["building_type"] = details["building_type"]
    # Only explicit parameter enums. Free-text descriptions, seller contacts,
    # marketing judgments and inferred motives never enter this JSON column.
    condition = {"хорошее":"good","хороший":"good","хороший ремонт":"good",
        "евроремонт":"euro","дизайнерский ремонт":"designer","дизайнерский":"designer",
        "требует ремонта":"needs_repair","без ремонта":"none","черновая":"rough",
        "черновой":"rough","косметический ремонт":"cosmetic","чистовая":"finish"}
    furniture = {"полностью":"full","полностью меблирована":"full","частично":"partial",
        "частично меблирована":"partial","без мебели":"none","не меблирована":"none"}
    for field, source, mapping in (("condition","renovation_param",condition),("furniture","furniture_param",furniture)):
        raw = details.get(source)
        value = mapping.get(raw.strip().casefold()) if isinstance(raw,str) else None
        if value:
            snapshot[field] = value
    return snapshot
