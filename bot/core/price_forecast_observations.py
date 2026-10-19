"""Private, immutable price observations collected from source ingestion.

`source_observed_at` is the scraper's existing last_seen/last_seen_at, never
the time this file was copied. Features become known only at captured_at;
date is the UTC capture day, so no current attribute is backfilled into the
past. These are asking prices, not sale prices or proof of legal availability.

Conservative purge groups can join suspected duplicates without merging any
database identity. Group evidence headers contain only hashed group tokens.
Unchanged source timestamps are not additional independent observations.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA_VERSION = 1
MAX_SOURCE_AGE = timedelta(hours=48)
HEX_EDGE_M = 100


def utc_timestamp(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def token(kind: str, value: str) -> str:
    return kind + ":" + hashlib.sha256((kind + "|" + str(value)).encode()).hexdigest()


class PurgeGroups:
    """Union of precautionary links; this does not assert physical identity."""
    def __init__(self):
        self.parents: dict[str, str] = {}

    def find(self, value: str) -> str:
        parent = self.parents.setdefault(value, value)
        while parent != self.parents[parent]:
            self.parents[parent] = self.parents[self.parents[parent]]
            parent = self.parents[parent]
        self.parents[value] = parent
        return parent

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            a, b = sorted((a, b))
            self.parents[b] = a


def build_groups(nodes: list[dict], edges: list[dict], previous: dict[str, str] | None = None):
    graph = PurgeGroups()
    entities: set[str] = set()
    for row in nodes:
        entity = str(row["listing_id"])
        entities.add(entity)
        key = token("entity", entity)
        graph.find(key)
        for kind in ("property_key", "trait_key", "photo_key"):
            if row.get(kind):
                graph.union(key, token(kind, str(row[kind])))
    for row in edges:
        left, right = str(row["left_id"]), str(row["right_id"])
        entities.update((left, right))
        graph.union(token("entity", left), token("entity", right))
    # Keep older group aliases connected after a new precautionary merge.
    # Old observations remain immutable; readers also consume group_links.
    for entity, old_group in (previous or {}).items():
        if entity in entities:
            graph.union(token("entity", entity), old_group)
    bindings = {entity: graph.find(token("entity", entity)) for entity in sorted(entities)}
    links = sorted({(bindings[entity], old) for entity, old in (previous or {}).items()
                    if entity in bindings and bindings[entity] != old})
    return bindings, links


def _number(value, low: float, high: float, *, integer=False):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or not low <= value <= high or (integer and not value.is_integer()):
        return None
    return int(value) if integer else value


def captured_location(row: dict) -> dict:
    """Current coarse scanner location, known at capture; no raw coordinates."""
    from bot.core.hexgrid import hex_id

    result = {"hex_id": None, "hex_edge_m": HEX_EDGE_M,
              "geo_quality": "unknown", "geo_source": "unknown"}
    lat = _number(row.get("lat"), 50.70, 51.55)
    lon = _number(row.get("lon"), 70.80, 72.10)
    if lat is None or lon is None:
        return result
    if row.get("entity_kind") == "listing":
        source, quality = "krisha_listing", "approximate_listing_point"
    else:
        source = row.get("coords_source")
        quality = {"listings": "listing_centroid", "homsters": "source_project_point",
                   "korter": "source_project_point", "developer": "source_project_point",
                   "nominatim": "geocoded_project_point"}.get(source)
        if quality is None:
            return result
    return {"hex_id": hex_id(lat, lon, HEX_EDGE_M), "hex_edge_m": HEX_EDGE_M,
            "geo_quality": quality, "geo_source": source}


def observation(row: dict, *, captured_at: datetime, group_id: str) -> dict | None:
    now = utc_timestamp(captured_at)
    observed = utc_timestamp(row.get("source_observed_at"))
    if now is None or observed is None or observed > now or now - observed > MAX_SOURCE_AGE:
        return None
    if row.get("entity_kind") not in {"listing", "unit"}:
        return None
    if row.get("market_type") not in {"primary", "secondary"}:
        return None
    if row.get("source_status") != "active" or row.get("source_price_confirmed") is not True:
        return None
    if row.get("source_provenance") not in {"sale_search_ingestion", "developer_inventory_ingestion"}:
        return None
    if row.get("is_duplicate") is True or row.get("dup_needs_review") is True:
        return None
    listing_id = str(row.get("listing_id", ""))
    source = str(row.get("source", ""))
    if not re.fullmatch(r"(?:\d+|newbuild:\d+)", listing_id) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", source):
        return None
    price = _number(row.get("price"), 1_000_000, 10_000_000_000, integer=True)
    area = _number(row.get("area"), 10, 1000)
    rooms = _number(row.get("rooms"), 1, 12, integer=True)
    if price is None or area is None or rooms is None:
        return None
    floor = _number(row.get("floor"), 1, 120, integer=True) if row.get("floor") is not None else None
    floors = _number(row.get("floors_total"), 1, 120, integer=True) if row.get("floors_total") is not None else None
    if (row.get("floor") is not None and floor is None) or (row.get("floors_total") is not None and floors is None):
        return None
    if floor is not None and floors is not None and floor > floors:
        return None
    if not group_id:
        return None
    reactivated = utc_timestamp(row.get("latest_reactivated_at"))
    if row.get("latest_reactivated_at") is not None and (
            reactivated is None or reactivated > now or observed < reactivated):
        return None
    result = {
        "schema_version": SCHEMA_VERSION, "record_type": "price_observation",
        "listing_id": listing_id, "entity_kind": row["entity_kind"], "source": source,
        "date": now.date().isoformat(), "capture_day": now.date().isoformat(),
        "captured_at": now.isoformat(), "copied_at": now.isoformat(),
        "attributes_known_at": now.isoformat(), "known_asof": now.isoformat(),
        "attribute_provenance": "versioned_source_observation",
        "source_observed_at": observed.isoformat(), "source_status": "active",
        "source_price_confirmed": True, "source_provenance": row.get("source_provenance"),
        "price": price, "area": area, "rooms": rooms, "floor": floor,
        "floors_total": floors, "market_type": row["market_type"],
        "complex_id": _number(row.get("complex_id"), 1, 2_147_483_647, integer=True),
        "group_id": group_id, "purge_keys": sorted({group_id, token("entity", listing_id)}),
        "identity_grouping_verified": True,
        "grouping_provenance": "conservative_duplicate_purge_v1",
        "property_identity_status": row.get("property_identity_status", "unknown"),
        "database_is_active": True, "has_prior_reactivation": bool(row.get("has_prior_reactivation")),
        "legal_availability_verified": False,
        "latest_reactivated_at": reactivated.isoformat() if reactivated else None,
    }
    result.update(captured_location(row))
    return result


@contextmanager
def collection_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = (directory / ".collector.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()


def _atomic_private(path: Path, payload: str, *, immutable=False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            os.link(temporary, path)  # fails if a published capture already exists
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_state(directory: Path) -> dict:
    state_path = directory / ".index.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported observation index")
    except (FileNotFoundError, ValueError, TypeError):
        state = {"schema_version": SCHEMA_VERSION, "latest": {}, "groups": {}, "indexed_files": []}
    indexed = set(state["indexed_files"])
    # Recover a capture published before a crash prevented updating the index.
    for file in sorted(directory.glob("????-??-??/*.jsonl")):
        relative = file.relative_to(directory).as_posix()
        if relative in indexed:
            continue
        with file.open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row.get("record_type") == "capture_meta":
                    for entity, group_id in row.get("group_bindings", {}).items():
                        state["groups"][entity] = group_id
                    continue
                observed = utc_timestamp(row.get("source_observed_at"))
                if row.get("record_type") != "price_observation" or observed is None:
                    continue
                entity = row["listing_id"]
                prior = utc_timestamp(state["latest"].get(entity, {}).get("source_observed_at"))
                if prior is None or observed > prior:
                    state["latest"][entity] = {"source_observed_at": observed.isoformat(), "price": row["price"]}
        indexed.add(relative)
    state["indexed_files"] = sorted(indexed)
    return state


def publish_capture(rows: list[dict], nodes: list[dict], edges: list[dict], directory: Path,
                    *, captured_at: datetime) -> dict:
    """Publish an immutable capture. Database content is never modified."""
    now = utc_timestamp(captured_at)
    if now is None:
        raise ValueError("Capture time must have an explicit timezone")
    with collection_lock(directory):
        state = _load_state(directory)
        bindings, links = build_groups(nodes, edges, state["groups"])
        accepted, rejected, unchanged, conflicting = [], 0, 0, 0
        pending_latest = {}
        for row in rows:
            item = observation(row, captured_at=now, group_id=bindings.get(str(row.get("listing_id")), ""))
            if item is None:
                rejected += 1
                continue
            previous = pending_latest.get(item["listing_id"]) or state["latest"].get(item["listing_id"])
            previous_seen = utc_timestamp(previous.get("source_observed_at")) if previous else None
            observed = utc_timestamp(item["source_observed_at"])
            if previous_seen is not None and observed <= previous_seen:
                if observed == previous_seen and previous.get("price") != item["price"]:
                    conflicting += 1
                    rejected += 1
                    continue
                unchanged += 1
                continue
            accepted.append(item)
            pending_latest[item["listing_id"]] = {"source_observed_at": item["source_observed_at"], "price": item["price"]}
        path = None
        if accepted or links:
            path = directory / now.date().isoformat() / (now.strftime("%H%M%S%f") + "-" + uuid.uuid4().hex[:12] + ".jsonl")
            changed_bindings = {entity: group_id for entity, group_id in bindings.items()
                                if state["groups"].get(entity) != group_id}
            header = {"schema_version": SCHEMA_VERSION, "record_type": "capture_meta",
                      "captured_at": now.isoformat(), "group_links": links,
                      "group_bindings": changed_bindings,
                      "source_history_verified": True, "price_type": "asking",
                      "grouping_is_physical_identity": False}
            payload = "\n".join(json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                                for item in [header, *accepted]) + "\n"
            _atomic_private(path, payload, immutable=True)
            state["indexed_files"].append(path.relative_to(directory).as_posix())
            for item in accepted:
                state["latest"][item["listing_id"]] = {"source_observed_at": item["source_observed_at"], "price": item["price"]}
        state["groups"] = bindings
        _atomic_private(directory / ".index.json", json.dumps(state, ensure_ascii=False, separators=(",", ":")))
        return {"captured_at": now.isoformat(), "new_observations": len(accepted),
                "unchanged_source_observations": unchanged, "rejected": rejected,
                "conflicting_cached_quotes": conflicting,
                "conservative_groups": len(set(bindings.values())), "group_links": len(links),
                "capture_file": str(path) if path else None}
