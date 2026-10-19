"""Conservative, offline identity gate for both daily cards and their peers.

The publisher supplies catalog evidence. This module makes no database calls,
does not merge properties and never promotes proximity to a building identity.
An address remains an observation from the advertisement, not a verified title.
"""
from __future__ import annotations

import re
from collections import defaultdict

from bot.core.house_resolution import _address_street_key, _extract_house_number
from bot.core.complex_ingest_identity import normalize_complex_name

_PUBLIC_FIELDS = ("checked", "status", "canonical_id", "canonical_name", "umbrella_id",
                  "umbrella_name", "house_id", "house_label", "binding_method")


def annotate_daily_pick_identity_rows(rows: list[dict], *, complexes: list[dict], aliases: list[dict],
                                      complex_reviews: list[dict], relations: list[dict],
                                      property_reviews: list[dict]) -> list[dict]:
    """Join bounded catalog/review scans once in memory, without SQL per row.

    Roots are memoized, cycles and missing roots remain invalid. Name keys
    come from the same database function as the binding resolver. No fuzzy
    name/address matching or new identity assignment happens here.
    """
    catalog = {item["id"]: item for item in complexes}
    roots: dict[int, int | None] = {}

    def root_id(source_id):
        if source_id in roots:
            return roots[source_id]
        path, seen, current = [], set(), source_id
        terminal = None
        while current in catalog and current not in seen:
            if current in roots:
                terminal = roots[current]
                break
            seen.add(current)
            path.append(current)
            target = catalog[current].get("canonical_id")
            if target is None:
                terminal = current
                break
            current = target
        for identifier in path:
            roots[identifier] = terminal
        return terminal

    for identifier in catalog:
        root_id(identifier)
    eligible = {}
    for identifier, terminal in roots.items():
        item = catalog.get(terminal)
        if item and item.get("is_garbage") is not True and item.get("is_street") is not True and item.get("canonical_reason") != "junk_unmatched":
            eligible[identifier] = item
    parents = {eligible[item["parent_complex_id"]]["id"] for item in eligible.values()
               if item.get("parent_complex_id") in eligible}
    names = defaultdict(set)
    for identifier, item in catalog.items():
        if identifier in eligible and item.get("name_key"):
            names[item["name_key"]].add(eligible[identifier]["id"])
    for alias in aliases:
        if alias.get("complex_id") in eligible and alias.get("normalized_name"):
            names[alias["normalized_name"]].add(eligible[alias["complex_id"]]["id"])
    ambiguous = {name for name, ids in names.items() if len(ids) > 1}
    # A redirect is not a reviewed spelling. Only the canonical row's own
    # name and explicitly verified aliases may confirm a source name.
    verified_names = defaultdict(set)
    for identifier, item in catalog.items():
        if identifier in eligible and eligible[identifier]["id"] == identifier and item.get("name_key"):
            verified_names[identifier].add(item["name_key"])
    for alias in aliases:
        owner = catalog.get(alias.get("complex_id"), {})
        if (alias.get("complex_id") in eligible and alias.get("normalized_name")
                and alias.get("status", "verified") == "verified"
                and not owner.get("is_garbage") and not owner.get("is_street")
                and owner.get("canonical_reason") != "junk_unmatched"):
            verified_names[eligible[alias["complex_id"]]["id"]].add(alias["normalized_name"])
    separated = {tuple(sorted((item["complex_id_a"], item["complex_id_b"]))) for item in relations}
    pending_complexes = set()
    for review in complex_reviews:
        pair = (review["complex_id_a"], review["complex_id_b"])
        if tuple(sorted(pair)) not in separated:
            pending_complexes.update(roots.get(identifier) for identifier in pair)
    pending_complexes.discard(None)
    pending_listings = {item["listing_id"] for item in property_reviews}
    pending_properties = {item.get(field) for item in property_reviews
                          for field in ("candidate_property_id", "source_property_id")}
    pending_properties.discard(None)

    def fields(prefix, source_id):
        item = eligible.get(source_id)
        parent = eligible.get(item.get("parent_complex_id")) if item else None
        return {
            prefix + "_id": item["id"] if item else None,
            prefix + "_name": item.get("name") if item else None,
            prefix + "_valid": bool(item and (item.get("parent_complex_id") is None or parent)),
            prefix + "_is_umbrella": bool(item and (item.get("is_umbrella") is True or item["id"] in parents)),
            prefix + "_parent_id": parent["id"] if parent else None,
            prefix + "_parent_name": parent.get("name") if parent else None,
            prefix + "_year_built": item.get("year_built") if item else None,
        }

    result = []
    for row in rows:
        metadata = {**fields("identity_complex", row.get("complex_id")),
                    **fields("identity_house", row.get("resolved_house_id"))}
        family = {metadata["identity_complex_id"], metadata["identity_house_id"]} - {None}
        if (row.get("house_attribution") in {"address", "token"}
                and metadata["identity_house_valid"] and not metadata["identity_house_is_umbrella"]):
            family.add(metadata["identity_house_parent_id"])
        source_keys = sorted({key for cid in family for key in verified_names[cid]
                              if names.get(key, set()) <= family})
        raw_key = row.get("identity_raw_name_key")
        metadata.update(
            identity_checked=True,
            identity_complex_review_pending=any(metadata[field] in pending_complexes for field in (
                "identity_complex_id", "identity_house_id", "identity_complex_parent_id", "identity_house_parent_id")),
            identity_property_match_pending=(row.get("id") in pending_listings or row.get("property_id") in pending_properties),
            identity_name_ambiguous=(row.get("complex_resolution") == "name" and row.get("identity_raw_name_key") in ambiguous),
            identity_source_name_keys=source_keys,
            identity_source_name_conflict=bool(family and (not raw_key or raw_key not in source_keys)),
        )
        result.append({**row, **metadata})
    return result

def _positive_id(value) -> int | None:
    return value if type(value) is int and 0 < value <= 2_147_483_647 else None


def _single_source_house(address) -> bool:
    if not isinstance(address, str) or not address.strip():
        return False
    head = re.split(r"\s+[—–-]\s+|\n", address.strip(), maxsplit=1)[0]
    head = re.split(r"(?:\s*,?\s+|,\s*)(?:кв(?:артира)?\.?|apartment|apt\.?|flat)\s*[№#:]?\s*\d+",
                    head, maxsplit=1, flags=re.I)[0].strip(" ,;")
    if re.search(r"https?://|www\.|@|телефон|whatsapp|ватсап", head, re.I):
        return False
    if any(sum(character.isdigit() for character in match[0]) >= 10
           for match in re.finditer(r"\+?\d[\d\s()\-]+\d", head)):
        return False
    house = r"\d+(?:[/\-]\d+)?[a-zа-яәғқңөұүһі]?"
    if re.search(rf"{house}(?:\s*[,;]\s*{house})+\s*$", head, re.I):
        return False
    number, street = _extract_house_number(head), _address_street_key(head)
    return bool(number and not number.startswith("уч") and street)


def normalize_daily_pick_identity(row: dict) -> dict | None:
    """Return a copied, canonical row or exclude the observation entirely.

    Exclusion applies before calculating medians as well as before ranking.
    ``properties.identity_status=provisional`` is intentionally not a gate:
    it describes a bootstrap property, not evidence of a duplicate.
    """
    if not isinstance(row, dict) or row.get("identity_checked") is not True:
        return None
    for flag in ("identity_complex_review_pending", "identity_property_match_pending", "identity_name_ambiguous",
                 "identity_source_name_conflict"):
        if row.get(flag) is not False:
            return None
    if row.get("complex_resolution") == "unbound":
        return None
    safe = dict(row)
    bound_id, house_id = row.get("complex_id"), row.get("resolved_house_id")
    if bound_id is None and house_id is None:
        # No invented catalog membership. This can participate only in the
        # explicitly approximate nearby comparison implemented by the caller.
        if row.get("complex_resolution") not in (None, "") or not _single_source_house(row.get("address")):
            return None
        safe.update(complex_id=None, canonical_complex_name="", complex_year_built=None,
                    resolved_house_id=None, house_attribution=None, identity_scope="source_address",
                    identity_note="Улица и номер дома указаны в объявлении; принадлежность к ЖК не подтверждена.")
        safe["complex_identity"] = {"checked": True, "status": "source_address_only", "canonical_id": None,
                                    "canonical_name": None, "umbrella_id": None, "umbrella_name": None,
                                    "house_id": None, "house_label": None, "binding_method": "source_address"}
        return safe

    has_bound = bound_id is not None
    prefix = "identity_complex" if has_bound else "identity_house"
    canonical_id = _positive_id(row.get(prefix + "_id"))
    name = row.get(prefix + "_name")
    if (row.get(prefix + "_valid") is not True or canonical_id is None
            or not isinstance(name, str) or not name.strip()):
        return None
    if has_bound and row.get("complex_resolution") not in {"name", "url", "house"}:
        return None
    if not has_bound and row.get("house_attribution") not in {"address", "token"}:
        return None
    if type(row.get(prefix + "_is_umbrella")) is not bool:
        return None

    exact_house_id = _positive_id(row.get("identity_house_id"))
    exact_house = (house_id is not None and row.get("identity_house_valid") is True
                   and row.get("identity_house_is_umbrella") is False
                   and row.get("house_attribution") in {"address", "token"}
                   and exact_house_id is not None)
    if has_bound and row.get("complex_resolution") == "house" and not exact_house:
        return None
    umbrella_id = umbrella_name = None
    if row.get(prefix + "_is_umbrella") is True:
        # A project name/URL/centroid never identifies one of its buildings.
        if not exact_house or _positive_id(row.get("identity_house_parent_id")) != canonical_id:
            return None
        house_name = row.get("identity_house_name")
        if not isinstance(house_name, str) or not house_name.strip():
            return None
        canonical_id, name = exact_house_id, house_name
        year = row.get("identity_house_year_built")
        umbrella_id, umbrella_name = row["identity_complex_id"], row["identity_complex_name"]
        scope = "house"
    else:
        # Conflicting or merely geographic house attribution must not become
        # a stronger identity than the authoritative catalog binding.
        if house_id is not None and (not exact_house or exact_house_id != canonical_id):
            return None
        year = row.get(prefix + "_year_built")
        scope = "house" if exact_house else "complex"
        if exact_house:
            umbrella_id, umbrella_name = row.get("identity_house_parent_id"), row.get("identity_house_parent_name")
    safe.update(complex_id=canonical_id, canonical_complex_name=name.strip()[:100],
                complex_year_built=year, identity_scope=scope,
                identity_note=("Дом определён по адресу или явному обозначению корпуса в источнике."
                               if scope == "house" else "ЖК определён по каталогу; это не подтверждение конкретного корпуса."))
    if exact_house:
        safe["resolved_house_id"] = exact_house_id
    try:
        safe["complex_identity"] = validate_public_daily_identity({
            "checked": True, "status": "house_from_source" if scope == "house" else "catalog_bound",
            "canonical_id": canonical_id, "canonical_name": name.strip()[:100],
            "umbrella_id": umbrella_id, "umbrella_name": umbrella_name.strip()[:100] if isinstance(umbrella_name, str) else None,
            "house_id": exact_house_id if exact_house else None, "house_label": name.strip()[:100] if exact_house else None,
            "binding_method": row.get("house_attribution") if exact_house else row.get("complex_resolution"),
        })
    except ValueError:
        return None
    return safe


def matching_source_complex_name(row: dict, record: dict) -> bool:
    """A fresh explicit observation, never an address/name-prefix inference."""
    if "source_complex_name" not in record:
        return False
    name = record["source_complex_name"]
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 200):
        return False
    bound = row.get("complex_id") is not None or row.get("resolved_house_id") is not None
    if not bound:
        return name is None
    keys = row.get("identity_source_name_keys")
    return bool(name is not None and isinstance(keys, list)
                and normalize_complex_name(name) in keys)


def validate_public_daily_identity(value: dict) -> dict:
    """Validate an already published projection without reading any databases.

    Missing legacy metadata must not silently reassert an old complex name.
    Extra internal evidence, addresses and property identifiers are discarded.
    """
    if not isinstance(value, dict) or value.get("checked") is not True or not all(key in value for key in _PUBLIC_FIELDS):
        raise ValueError("Public identity evidence is missing")
    safe = {key: value[key] for key in _PUBLIC_FIELDS}
    if not isinstance(safe["binding_method"], str):
        raise ValueError("Invalid public identity binding method")
    for id_field, name_field in (("canonical_id", "canonical_name"), ("umbrella_id", "umbrella_name"), ("house_id", "house_label")):
        identifier, name = safe[id_field], safe[name_field]
        if identifier is None:
            if name is not None:
                raise ValueError("Identity label without its canonical ID")
        elif (_positive_id(identifier) is None or not isinstance(name, str) or not name.strip() or len(name) > 100):
            raise ValueError("Invalid canonical identity pair")
    if safe["status"] == "source_address_only":
        if safe["binding_method"] != "source_address" or any(safe[key] is not None for key in ("canonical_id", "house_id", "umbrella_id")):
            raise ValueError("Source address does not establish catalog identity")
    elif safe["status"] == "house_from_source":
        if (safe["binding_method"] not in {"address", "token"} or safe["canonical_id"] != safe["house_id"]
                or safe["canonical_name"] != safe["house_label"] or safe["house_id"] is None
                or safe["umbrella_id"] == safe["house_id"]):
            raise ValueError("Invalid house identity evidence")
    elif safe["status"] == "catalog_bound":
        if (safe["binding_method"] not in {"name", "url"} or safe["canonical_id"] is None
                or safe["house_id"] is not None or safe["umbrella_id"] is not None):
            raise ValueError("Catalog project is not a confirmed building")
    else:
        raise ValueError("Unknown public identity status")
    return safe


def public_daily_pick_identity(row: dict) -> dict:
    """Only normalized catalog/house labels survive the public allowlist."""
    return validate_public_daily_identity(row.get("complex_identity"))
