#!/usr/bin/env python3
"""Compare manually observed rental catalog IDs with an aggregate-only DB audit.

The JSON inputs come from an ordinary browser. This script makes no Krisha
requests, initialises no application pool and applies no migrations. A few
observed pages are a convenience sample of a changing catalog, not proof of
100% market coverage. Output contains no individual IDs, addresses or contacts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SAMPLES = 100
MAX_IDS_PER_SAMPLE = 10_000
MAX_UNIQUE_IDS = 20_000
PATH_TYPES = {
    "/arenda/kvartiry/astana/": "apartment",
    "/arenda/garazhi/astana/": "parking",
    "/arenda/kommercheskaya-nedvizhimost/astana/": "commercial",
}
COUNT_FIELDS = (
    "sample_size", "db_present", "correct_scope", "active_clean", "fresh_72h",
    "fresh_1h", "with_stored_price", "with_stored_cover",
)

SAMPLE_SQL = """
WITH sampled AS (SELECT DISTINCT unnest($1::text[]) AS id), matched AS (
    SELECT sampled.id, r.id IS NOT NULL AS present,
      r.city = 'astana' AND r.prop_type IN ('apartment','parking','commercial')
        AND ($2::text IS NULL OR r.prop_type = $2) AS correct_scope,
      r.is_active IS TRUE AND r.archived_at IS NULL
        AND NOT coalesce(r.is_duplicate, FALSE) AND NOT coalesce(r.dup_needs_review, FALSE)
        AND nullif(btrim(r.duplicate_of), '') IS NULL AS clean,
      r.last_seen, r.price,
      nullif(btrim(r.photo_url), '') IS NOT NULL OR
        CASE WHEN jsonb_typeof(r.photos) = 'array' THEN jsonb_array_length(r.photos) > 0
             ELSE FALSE END AS stored_cover
    FROM sampled LEFT JOIN rental_listings r ON r.id = sampled.id
)
SELECT count(*) AS sample_size,
  count(*) FILTER (WHERE present) AS db_present,
  count(*) FILTER (WHERE present AND correct_scope) AS correct_scope,
  count(*) FILTER (WHERE present AND correct_scope AND clean) AS active_clean,
  count(*) FILTER (WHERE present AND correct_scope AND clean
    AND last_seen BETWEEN $3::timestamptz - interval '72 hours' AND $3) AS fresh_72h,
  count(*) FILTER (WHERE present AND correct_scope AND clean
    AND last_seen BETWEEN $3::timestamptz - interval '1 hour' AND $3) AS fresh_1h,
  count(*) FILTER (WHERE present AND correct_scope AND clean AND price > 0) AS with_stored_price,
  count(*) FILTER (WHERE present AND correct_scope AND clean AND stored_cover) AS with_stored_cover
FROM matched
"""


@dataclass(frozen=True)
class SourceSample:
    source_url: str
    prop_type: str
    page: int
    checked_at: datetime
    ids: tuple[str, ...]


def utc_timestamp(value: object) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Invalid source observation timestamp") from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("A timezone-aware observation timestamp is required")
    return value.astimezone(timezone.utc)


def parse_sample(payload: object) -> SourceSample:
    if not isinstance(payload, dict):
        raise ValueError("Source sample must be an object")
    supplied_url = payload.get("url") or payload.get("observedUrl")
    derived_url = supplied_url is None
    value = supplied_url or payload.get("lastPageHref")
    if not isinstance(value, str) or len(value) > 600:
        raise ValueError("A rental catalog URL is required")
    if derived_url and value.startswith("/") and not value.startswith("//"):
        value = "https://krisha.kz" + value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.hostname != "krisha.kz" or parsed.username
                or parsed.password or parsed.port is not None or parsed.fragment
                or parsed.path not in PATH_TYPES):
            raise ValueError("Unsupported rental catalog URL")
    except ValueError:
        raise ValueError("Unsupported rental catalog URL") from None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {"page", "rent-period-switch"} or any(len(values) != 1 for values in query.values()):
        raise ValueError("Unsupported catalog filters")
    period = query.get("rent-period-switch", [None])[0]
    if period is not None and (parsed.path != "/arenda/kvartiry/astana/" or period != "/arenda/kvartiry"):
        raise ValueError("Unsupported rental-period filter")
    # The first browser artifact has no observed URL, but does have its
    # catalog's last-page link and an explicit observed page. Never confuse
    # the linked last page with the page actually inspected in the browser.
    if derived_url:
        page = payload.get("page")
        if type(page) is not int:
            raise ValueError("Derived catalog URL requires an explicit observed page")
    else:
        page_text = query.get("page", ["1"])[0]
        if not re.fullmatch(r"[1-9][0-9]{0,4}", page_text):
            raise ValueError("Invalid catalog page")
        page = int(page_text)
        if "page" in payload and payload["page"] != page:
            raise ValueError("Conflicting observed page")
    if type(page) is not int or not 1 <= page <= 10000:
        raise ValueError("Invalid catalog page")
    kind = PATH_TYPES[parsed.path]
    if "prop_type" in payload and payload["prop_type"] != kind:
        raise ValueError("Conflicting source category")
    query.pop("page", None)
    if page > 1:
        query["page"] = [str(page)]
    source_url = urlunsplit(("https", "krisha.kz", parsed.path, urlencode(query, doseq=True), ""))
    checked_at = utc_timestamp(payload.get("sourceCheckedAt") or payload.get("sourcecheckedAt")
                               or payload.get("checkedAt"))
    ids = payload.get("regularIds") if "regularIds" in payload else payload.get("ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_IDS_PER_SAMPLE:
        raise ValueError("A bounded nonempty catalog ID list is required")
    if any(not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", value) for value in ids):
        raise ValueError("Source IDs must be decimal strings")
    return SourceSample(source_url, kind, page, checked_at, tuple(sorted(set(ids))))


def load_samples(paths: list[Path]) -> list[SourceSample]:
    if not 1 <= len(paths) <= MAX_SAMPLES:
        raise ValueError("A bounded source sample list is required")
    groups: dict[tuple, SourceSample] = {}
    for path in paths:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Source sample file is too large")
        sample = parse_sample(json.loads(path.read_text(encoding="utf-8")))
        key = (sample.source_url, sample.page, sample.prop_type, sample.checked_at)
        if key in groups:
            previous = groups[key]
            sample = SourceSample(sample.source_url, sample.prop_type, sample.page, sample.checked_at,
                                  tuple(sorted(set(previous.ids) | set(sample.ids))))
        groups[key] = sample
    samples = list(groups.values())
    if len({listing_id for sample in samples for listing_id in sample.ids}) > MAX_UNIQUE_IDS:
        raise ValueError("Too many unique source IDs")
    return samples


def count_projection(row: object, sample_size: int) -> dict:
    result = {}
    for name in COUNT_FIELDS:
        number = row[name]
        if type(number) is not int or not 0 <= number <= sample_size:
            raise RuntimeError("Invalid aggregate rental sample counts")
        result[name] = number
    if result["sample_size"] != sample_size:
        raise RuntimeError("Source sample size does not match aggregate")
    if not (result["fresh_1h"] <= result["fresh_72h"] <= result["active_clean"]
            <= result["correct_scope"] <= result["db_present"] <= sample_size):
        raise RuntimeError("Inconsistent aggregate rental sample counts")
    if max(result["with_stored_price"], result["with_stored_cover"]) > result["active_clean"]:
        raise RuntimeError("Inconsistent aggregate rental field counts")
    result["missing_from_database"] = sample_size - result["db_present"]
    result["presence_percent"] = round(100 * result["db_present"] / sample_size, 2)
    result["fresh_72h_percent"] = round(100 * result["fresh_72h"] / sample_size, 2)
    return result


async def audit(dsn: str, samples: list[SourceSample]) -> dict:
    import asyncpg
    if not samples:
        raise ValueError("A nonempty source sample is required")
    connection = await asyncpg.connect(dsn, command_timeout=60, server_settings={
        "default_transaction_read_only": "on", "statement_timeout": "60000",
        "application_name": "hatuli_rental_source_sample_readonly"})
    try:
        async with connection.transaction(readonly=True, isolation="repeatable_read"):
            if await connection.fetchval("SHOW transaction_read_only") != "on":
                raise RuntimeError("Rental source sample audit requires a read-only transaction")
            now = utc_timestamp(await connection.fetchval("SELECT now()"))
            groups = []
            for sample in samples:
                row = await connection.fetchrow(SAMPLE_SQL, list(sample.ids), sample.prop_type, now)
                groups.append({"source_url": sample.source_url, "page": sample.page,
                               "prop_type": sample.prop_type, "source_checked_at": sample.checked_at.isoformat(),
                               "source_observation_age_seconds": round((now - sample.checked_at).total_seconds()),
                               **count_projection(row, len(sample.ids))})
            ids = sorted({listing_id for sample in samples for listing_id in sample.ids})
            row = await connection.fetchrow(SAMPLE_SQL, ids, None, now)
            return {
                "database_time": now.isoformat(), "database_writes": 0, "transaction_read_only": "on",
                "market_completeness": "unknown_convenience_sample_only",
                "scope": "Astana monitored Krisha rental categories; manually observed catalog pages",
                "note": "This is a convenience sample, not proof of 100% market coverage. Catalog membership and prices can change between source and database observations.",
                "price_match_check": "not_performed_only_stored_price_presence_counted",
                "sample_groups": groups,
                "combined": {"source_samples": len(samples),
                             "id_observations": sum(len(sample.ids) for sample in samples),
                             "unique_source_ids": len(ids),
                             **count_projection(row, len(ids))},
            }
    finally:
        await connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--sample", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        samples = load_samples(args.sample)
        from dotenv import load_dotenv
        load_dotenv(args.root / ".env")
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is required")
        report = asyncio.run(audit(dsn, samples))
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    except Exception as exc:
        # Raw asyncpg exceptions can contain connection credentials.
        print(f"Rental source sample audit failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
