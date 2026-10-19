"""Complete eligible catalog projection for the budget selector; file-only reads.

This is an inventory of Hatuli's stored projects, not a certificate of city-wide
coverage or a verified building identity. Existing redirects are followed; names
never create a new merge. Houses remain distinct children of their umbrella.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urlsplit

from bot.core.complex_canonical import norm_name

SCHEMA_VERSION = 1
MAX_ROWS = 20_000
MAX_FILE_BYTES = 15_000_000
MAX_CATALOG_AGE = timedelta(days=7)
MAX_EXPORT_AGE = timedelta(hours=1)
_LOCAL_CITIES = {"astana", "nursultan", "астана", "нурсултан"}
_FOREIGN_CITIES = {"almaty", "almatyi", "алматы", "shymkent", "шымкент", "karaganda", "караганда",
                   "aktau", "актау", "atyrau", "атырау", "aktobe", "актобе", "kokshetau", "кокшетау",
                   "kostanay", "костанай", "pavlodar", "павлодар", "petropavlovsk", "петропавловск",
                   "taraz", "тараз", "turkestan", "туркестан", "uralsk", "уральск", "kaskelen", "каскелен",
                   "talgar", "талгар", "ustkamenogorsk", "устькаменогорск", "semey", "семей"}
_LIMITATIONS = [
    "Список включает подходящие записи каталога Hatuli, а не только объекты с текущими предложениями.",
    "Полнота относительно всех ЖК Астаны не подтверждена внешним городским реестром.",
    "Часть городских привязок основана на каталожной координате в районе Астаны, а не на проверенном адресе.",
    "Спорные привязки, неизвестный город и карантин исключены; выбор ЖК не подтверждает точность его домов.",
]
_ITEM_KEYS = {"id", "name", "label", "aliases", "parent_id", "parent_name", "is_umbrella", "child_ids", "address", "city_evidence"}
_EVIDENCE = {"verified_address_city", "source_city", "catalog_address_city", "catalog_coordinate_area", "parent_catalog_city"}


def complex_catalog_path() -> Path:
    return Path(os.environ.get("HATULI_COMPARISON_COMPLEXES_PATH") or
                Path(__file__).resolve().parents[2] / "var" / "comparison-complexes.json")


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Catalog date requires timezone")
    return value.astimezone(timezone.utc)


def _id(value):
    return value if type(value) is int and 0 < value < 2**63 else None


def _text(value, limit=250):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value or len(value) > limit or any(ord(c) < 32 for c in value):
        return None
    return value


def _address(value):
    if not isinstance(value, str):
        return None
    value = value.replace("–", "—").split("—", 1)[0].splitlines()[0] if value else ""
    value = re.split(r"(?:,\s*|\s+)(?:кв(?:артира)?\.?|апартаменты)\s*\d", value, maxsplit=1, flags=re.I)[0]
    if re.search(r"https?://|@|(?:\+?7|8)[ ()-]*\d{3}[ ()-]*\d{3}[ ()-]*\d{2}[ ()-]*\d{2}", value, re.I):
        return None
    return _text(value)


def _city_key(value):
    return re.sub(r"[\s.-]+", "", (value or "").casefold())


def _url_cities(value):
    """A structural Krisha city is authoritative even outside our city list."""
    if not isinstance(value, str):
        return set()
    try:
        url = urlsplit(value)
    except ValueError:
        return set()
    host = (url.hostname or "").lower().removeprefix("www.")
    path = unquote(url.path).lower().strip("/")
    if host == "krisha.kz":
        parts = path.split("/")
        if len(parts) >= 4 and parts[:2] == ["complex", "show"]:
            city = _city_key(parts[2])
            return {city} if city else {"unknown_explicit_city"}
    if host == "korter.kz":
        for city in _LOCAL_CITIES | _FOREIGN_CITIES:
            # Recognise city suffixes, retaining hyphens inside nur-sultan.
            if any(path.endswith("-" + suffix) for suffix in (city, "nur-sultan" if city == "nursultan" else city,
                                                               "нур-султан" if city == "нурсултан" else city)):
                return {city}
    return set()


def _address_cities(value):
    if not isinstance(value, str):
        return set()
    city = re.match(r"^\s*(?:рк\s*[,;]?\s*)?(?:г(?:ород)?\.?\s*)?([\w-]+)", value, re.I)
    key = _city_key(city.group(1)) if city else ""
    return {key} if key in _LOCAL_CITIES | _FOREIGN_CITIES else set()


def _geo_state(row):
    lat, lon = row.get("lat"), row.get("lon")
    if lat is None and lon is None:
        return "unknown"
    try:
        if isinstance(lat, bool) or isinstance(lon, bool):
            return "conflict"
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError, OverflowError):
        return "conflict"
    # Same broad study area as market_comparison, not an administrative border.
    return "inside" if math.isfinite(lat) and math.isfinite(lon) and 50.70 <= lat <= 51.55 and 70.80 <= lon <= 72.10 else "conflict"


def build_comparison_complexes(complexes, *, aliases=(), addresses=(), links=(), reviews=(),
                               relations=(), conflicts=(), now=None) -> dict:
    """Project all eligible stored canonical records, independently of offers."""
    now = _utc(now or datetime.now(timezone.utc))
    if not isinstance(complexes, list) or not 0 < len(complexes) <= MAX_ROWS:
        raise ValueError("Incomplete complex catalog")
    catalog = {}
    for row in complexes:
        if not isinstance(row, dict) or not _id(row.get("id")) or row["id"] in catalog:
            raise ValueError("Invalid or duplicate complex ID")
        catalog[row["id"]] = row
    roots = {}
    for identifier in catalog:
        path, seen, current = [], set(), identifier
        while current in catalog and current not in seen and current not in roots:
            seen.add(current)
            path.append(current)
            target = catalog[current].get("canonical_id")
            if target is None:
                roots[current] = current
                break
            current = target
        terminal = roots.get(current)
        for member in path:
            roots[member] = terminal
    basic = {cid: row for cid, row in catalog.items() if roots[cid] == cid and _text(row.get("name"))
             and not row.get("is_garbage") and not row.get("is_street") and row.get("canonical_reason") != "junk_unmatched"}
    separated = {tuple(sorted((x["complex_id_a"], x["complex_id_b"]))) for x in relations
                 if x.get("relation_type") in {"sibling_phase", "same_umbrella_project", "separate_neighbor_complex"}}
    pending = set()
    for row in reviews:
        a, b = row.get("complex_id_a"), row.get("complex_id_b")
        ra, rb = roots.get(a), roots.get(b)
        if ra is not None and rb is not None and ra != rb and tuple(sorted((a, b))) not in separated:
            pending.update((ra, rb))
    source_conflicts = {roots.get(row.get(key)) for row in conflicts for key in ("complex_id", "conflict_with_complex_id")}
    source_conflicts.discard(None)
    urls, verified_cities = defaultdict(list), defaultdict(set)
    # Legacy links on redirected rows still belong to the canonical family.
    # A foreign-city link there cannot be laundered through the root's coords.
    for member_id, member in catalog.items():
        root = roots.get(member_id)
        if root in basic:
            urls[root].extend(member.get(field) for field in
                              ("krisha_url", "korter_url", "homsters_url", "source_korter_url"))
    for row in links:
        if roots.get(row.get("complex_id")) in basic:
            urls[roots[row["complex_id"]]].append(row.get("url"))
    for row in addresses:
        if row.get("status") == "verified" and roots.get(row.get("complex_id")) in basic:
            key = _city_key(row.get("city"))
            if key:
                verified_cities[roots[row["complex_id"]]].add(key)
    city_positive, city_conflict, evidence = set(), set(), {}
    for cid, row in basic.items():
        own_cities = _address_cities(row.get("address"))
        source_cities = set().union(*(_url_cities(url) for url in urls[cid] +
            [row.get("krisha_url"), row.get("korter_url"), row.get("homsters_url"), row.get("source_korter_url")]))
        cities = own_cities | source_cities | verified_cities[cid]
        geo = _geo_state(row)
        if cities - _LOCAL_CITIES or geo == "conflict":
            city_conflict.add(cid)
            continue
        if verified_cities[cid] & _LOCAL_CITIES:
            evidence[cid] = "verified_address_city"
        elif source_cities & _LOCAL_CITIES:
            evidence[cid] = "source_city"
        elif own_cities & _LOCAL_CITIES:
            evidence[cid] = "catalog_address_city"
        elif geo == "inside":
            evidence[cid] = "catalog_coordinate_area"
        if cid in evidence:
            city_positive.add(cid)
    # Resolve city inheritance and parent membership without turning a child
    # into a duplicate or silently detaching it from an excluded parent.
    quarantined_roots = {roots.get(cid) for cid, row in catalog.items() if row.get("geo_quarantined_at")}
    held = pending | source_conflicts | quarantined_roots | city_conflict
    eligible, resolving = set(), set()

    def include(cid):
        if cid in eligible:
            return True
        if cid not in basic or cid in held or cid in resolving:
            return False
        resolving.add(cid)
        parent = basic[cid].get("parent_complex_id")
        parent_root = roots.get(parent) if parent is not None else None
        allowed = parent is None or (parent_root != cid and include(parent_root))
        if allowed and (cid in city_positive or parent is not None):
            eligible.add(cid)
            evidence.setdefault(cid, "parent_catalog_city")
        resolving.remove(cid)
        return cid in eligible

    for cid in basic:
        include(cid)
    names = defaultdict(set)
    for cid, row in basic.items():
        names[norm_name(row["name"])].add(cid)
    accepted_aliases = defaultdict(set)
    for row in aliases:
        owner = catalog.get(row.get("complex_id"), {})
        cid = roots.get(row.get("complex_id"))
        name = _text(row.get("name"))
        if row.get("status") == "verified" and cid in basic and name and not owner.get("is_garbage") and not owner.get("is_street") and owner.get("canonical_reason") != "junk_unmatched":
            names[norm_name(name)].add(cid)
            accepted_aliases[cid].add(name)
    children = defaultdict(list)
    for cid in eligible:
        parent = roots.get(basic[cid].get("parent_complex_id"))
        if parent is not None:
            children[parent].append(cid)
    items = []
    for cid in sorted(eligible, key=lambda x: (basic[x]["name"].casefold(), x)):
        row = basic[cid]
        name = _text(row["name"])
        parent_id = roots.get(row.get("parent_complex_id"))
        parent_name = _text(basic[parent_id]["name"]) if parent_id is not None else None
        address = _address(row.get("address"))
        label = name if parent_name is None else f"{name} · {parent_name}"
        if len(names[norm_name(name)]) > 1:
            label += f" · #{cid}"
        items.append({"id": cid, "name": name, "label": label, "address": address,
                      "aliases": sorted((a for a in accepted_aliases[cid] if names[norm_name(a)] == {cid} and a != name), key=str.casefold),
                      "parent_id": parent_id, "parent_name": parent_name,
                      "is_umbrella": bool(row.get("is_umbrella") or children[cid]),
                      "child_ids": sorted(children[cid]), "city_evidence": evidence[cid]})
    coverage = {"raw_rows": len(catalog), "canonical_rows": sum(roots[cid] == cid for cid in catalog),
                "redirect_rows": sum(row.get("canonical_id") is not None for row in catalog.values()),
                "invalid_redirect_rows": sum(value is None for value in roots.values()),
                "garbage_rows": sum(bool(x.get("is_garbage")) for x in catalog.values()),
                "street_rows": sum(bool(x.get("is_street")) for x in catalog.values()),
                "junk_rows": sum(x.get("canonical_reason") == "junk_unmatched" for x in catalog.values()),
                "basic_eligible_roots": len(basic), "city_conflict_roots": len(city_conflict),
                "unknown_city_roots": len(set(basic) - city_positive - city_conflict),
                "pending_duplicate_roots": len(pending & set(basic)),
                "source_conflict_roots": len(source_conflicts & set(basic)),
                "geo_quarantined_rows": sum(bool(x.get("geo_quarantined_at")) for x in catalog.values()),
                "verified_alias_rows": sum(x.get("status") == "verified" for x in aliases),
                "selectable_rows": len(items), "umbrella_rows": sum(x["is_umbrella"] for x in items),
                "house_rows": sum(x["parent_id"] is not None for x in items),
                "city_evidence_counts": dict(Counter(x["city_evidence"] for x in items)),
                "all_catalog_rows_read": True, "city_registry_verified": False, "limitations": _LIMITATIONS.copy()}
    return validate_comparison_complexes({"schema_version": SCHEMA_VERSION, "generated_at": now.isoformat(),
                                        "scope": "astana", "count": len(items), "coverage": coverage, "items": items}, now=now)


def validate_comparison_complexes(data, *, now=None, check_age=True):
    now = _utc(now or datetime.now(timezone.utc))
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION or data.get("scope") != "astana":
        raise ValueError("Invalid complex catalog schema")
    at = _utc(data.get("generated_at"))
    if at > now + timedelta(minutes=5) or (check_age and now - at > MAX_CATALOG_AGE):
        raise ValueError("Stale or future complex catalog")
    items, coverage = data.get("items"), data.get("coverage")
    if not isinstance(items, list) or not 0 < len(items) <= MAX_ROWS or data.get("count") != len(items):
        raise ValueError("Empty or incomplete complex catalog")
    if not isinstance(coverage, dict) or coverage.get("all_catalog_rows_read") is not True or coverage.get("city_registry_verified") is not False or coverage.get("selectable_rows") != len(items):
        raise ValueError("Invalid coverage receipt")
    safe, by_id = [], {}
    for row in items:
        if not isinstance(row, dict) or set(row) != _ITEM_KEYS or not _id(row.get("id")) or row["id"] in by_id:
            raise ValueError("Invalid catalog item")
        if (not isinstance(row.get("name"), str) or not _text(row["name"])
                or _text(row["name"]) != row["name"]
                or not isinstance(row.get("label"), str) or _text(row["label"], 550) != row["label"]
                or row.get("city_evidence") not in _EVIDENCE):
            raise ValueError("Invalid catalog label")
        if row.get("address") is not None and _address(row["address"]) != row["address"]:
            raise ValueError("Invalid catalog address")
        if type(row.get("is_umbrella")) is not bool or not isinstance(row.get("aliases"), list) or len(row["aliases"]) > 100:
            raise ValueError("Invalid aliases or umbrella")
        if any(not isinstance(a, str) or not _text(a) or _text(a) != a for a in row["aliases"]) or len(set(row["aliases"])) != len(row["aliases"]):
            raise ValueError("Invalid catalog alias")
        if not isinstance(row.get("child_ids"), list) or any(not _id(x) for x in row["child_ids"]) or row["child_ids"] != sorted(set(row["child_ids"])):
            raise ValueError("Invalid child IDs")
        if row.get("parent_id") is not None and (not _id(row["parent_id"]) or not _text(row.get("parent_name"))):
            raise ValueError("Invalid parent")
        if row.get("parent_id") is None and row.get("parent_name") is not None:
            raise ValueError("Parent name without ID")
        by_id[row["id"]] = row
        safe.append({**row, "aliases": row["aliases"].copy(), "child_ids": row["child_ids"].copy()})
    for row in safe:
        seen, current = set(), row
        while current["parent_id"] is not None:
            if current["id"] in seen or current["parent_id"] not in by_id:
                raise ValueError("Invalid parent graph")
            seen.add(current["id"])
            parent = by_id[current["parent_id"]]
            if current["parent_name"] != parent["name"] or current["id"] not in parent["child_ids"] or not parent["is_umbrella"]:
                raise ValueError("Inconsistent parent graph")
            current = parent
        if any(x not in by_id or by_id[x]["parent_id"] != row["id"] for x in row["child_ids"]):
            raise ValueError("Inconsistent children")
    return {"schema_version": SCHEMA_VERSION, "generated_at": at.isoformat(), "scope": "astana",
            "count": len(safe), "coverage": dict(coverage), "items": safe}


def load_comparison_complexes(path=None, *, now=None):
    """No pool, app initialization, network or database access on public reads."""
    try:
        path = Path(path) if path is not None else complex_catalog_path()
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return validate_comparison_complexes(json.loads(path.read_text(encoding="utf-8")), now=now)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def publish_comparison_complexes(data, path=None, *, now=None):
    """Fail closed on stale, empty, shrinking or older exports; atomic replace."""
    now = _utc(now or datetime.now(timezone.utc))
    data = validate_comparison_complexes(data, now=now)
    if now - _utc(data["generated_at"]) > MAX_EXPORT_AGE:
        raise ValueError("Export is too old for publication")
    path = Path(path) if path is not None else complex_catalog_path()
    if path.exists():
        previous = validate_comparison_complexes(json.loads(path.read_text(encoding="utf-8")), now=now, check_age=False)
        if _utc(data["generated_at"]) < _utc(previous["generated_at"]):
            raise ValueError("Older export cannot replace publication")
        if data["count"] < previous["count"] * .90:
            raise ValueError("Unexpected catalog contraction; previous publication retained")
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError("Complex catalog exceeds size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    return data
