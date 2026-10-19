"""Read-only canonical rental identity and observed-coordinate hex projection."""
from __future__ import annotations

import math
import re
from urllib.parse import urlsplit


class RentalStage0MigrationRequired(RuntimeError):
    def __init__(self):
        super().__init__("Rental stage 0 requires migration 109_rental_period_complex_hex.sql; no rental observation was written")


def source_complex_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or parsed.hostname != "krisha.kz"
                or parsed.username or parsed.password or parsed.port is not None or parsed.query or parsed.fragment
                or not re.fullmatch(r"/complex/show/(?:\d+|astana/[A-Za-z0-9_%\-]+)/?", parsed.path)):
            return None
    except ValueError:
        return None
    return "https://krisha.kz"+parsed.path.rstrip("/")


def observed_hex(lat, lon, edge_m):
    from bot.core.hexgrid import hex_id
    if any(isinstance(value, bool) for value in (lat, lon, edge_m)):
        return None
    try:
        lat, lon, edge = float(lat), float(lon), float(edge_m)
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) for value in (lat, lon, edge)) or not (50.70 <= lat <= 51.55 and 70.80 <= lon <= 72.10 and 25 <= edge <= 500):
        return None
    return {"lat": lat, "lon": lon, "hex_id": hex_id(lat, lon, edge), "hex_edge_m": edge}


async def resolve_rental_complex(*, complex_url, complex_name, address=None, title=None, return_decision=False):
    """A structural source URL and a known source name must agree.

    The shared canonical/name and house resolvers are reused. An umbrella
    remains an umbrella unless its own child matches an exact address/token.
    Optional decisions let a successful fresh observation invalidate an old
    FK without confusing uncertainty/conflict with a failed network request.
    No raw observations, aliases or catalog bindings are changed here.
    """
    from bot.db import pg
    from bot.core.complex_ingest_identity import canonical_complex_id, resolve_complex_name, InvalidComplexIdentity, AmbiguousComplexIdentity
    from bot.core.house_resolution import get_umbrella_children, resolve_house, conflicting_house_addresses
    source_id = named_id = None
    def answer(complex_id, status, reason):
        if return_decision:
            return {"complex_id": complex_id, "status": status, "reason": reason,
                    "source_canonical_id": source_id, "name_canonical_id": named_id}
        return complex_id
    url = source_complex_url(complex_url)
    if not url:
        return answer(None, "unresolved", "source_url_missing_or_invalid")
    if not isinstance(complex_name, str) or not complex_name.strip():
        return answer(None, "unresolved", "source_name_missing")
    rows = await pg.fetch("""
        SELECT id FROM complexes WHERE rtrim(split_part(split_part(krisha_url,'?',1),'#',1),'/')=$1
        UNION
        SELECT complex_id AS id FROM complex_source_links
        WHERE source='krisha' AND rtrim(split_part(split_part(url,'?',1),'#',1),'/')=$1
          AND (match_method='manual' OR confidence>=0.85)
    """, url)
    try:
        ids = {await canonical_complex_id(row["id"]) for row in rows}
        if len(ids) != 1:
            return answer(None, "unresolved", "source_url_ambiguous" if ids else "source_url_unverified")
        cid = next(iter(ids))
        source_id = cid
        named = await resolve_complex_name(complex_name, strict=True, canonical_names_only=True)
        named_id = named
        if named is None:
            return answer(None, "unresolved", "source_name_unverified")
        catalog = await pg.fetchrow("SELECT id,name,address,parent_complex_id FROM complexes WHERE id=$1", cid)
        if catalog is None:
            return answer(None, "unresolved", "source_catalog_missing")
        named_is_child = False
        if named != cid:
            named_row = await pg.fetchrow("SELECT parent_complex_id FROM complexes WHERE id=$1", named)
            named_is_child = bool(named_row and named_row["parent_complex_id"] == cid)
            if not (named_is_child or catalog["parent_complex_id"] == named):
                return answer(None, "conflict", "source_name_conflict")
        children = await get_umbrella_children(cid)
        if not children:
            if conflicting_house_addresses(address, catalog.get("address")):
                return answer(None, "conflict", "source_address_conflict")
            return answer(cid, "resolved", "source_url_and_name_agree")
        result = await resolve_house(umbrella_id=cid, umbrella_name=catalog["name"],
            listing_address=address, listing_title=title, listing_description=None,
            listing_lat=None, listing_lon=None, children=children)
        if not result or result["method"] not in ("address", "token"):
            return answer(cid, "resolved", "umbrella_house_unresolved")
        child_id = await canonical_complex_id(result["house_id"])
        child = await pg.fetchrow("SELECT parent_complex_id FROM complexes WHERE id=$1", child_id)
        if child and child["parent_complex_id"] == cid:
            if named_is_child and child_id != named:
                return answer(None, "conflict", "source_name_house_conflict")
            return answer(child_id, "resolved", "source_house_" + result["method"])
        return answer(None, "unresolved", "source_house_parent_unverified")
    except AmbiguousComplexIdentity:
        return answer(None, "unresolved", "source_name_ambiguous")
    except InvalidComplexIdentity:
        return answer(None, "unresolved", "source_catalog_invalid")
