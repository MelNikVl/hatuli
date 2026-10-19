#!/usr/bin/env python3
"""One-shot, private full rental editorial export. No collection or DB writes."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bot.core.rental_editorial_slice import EVIDENCE_READS, MAX_SCAN, build_slice, rental_rows_sql

SETTINGS_SQL = """SELECT key,value FROM app_settings WHERE
 key LIKE 'RENTAL_CRAWL_V1_APARTMENT%' OR key LIKE 'RENTAL_METRICS_V1_%'
 OR key IN ('RENTAL_SERVICE_CYCLE_LAST','RENTAL_SERVICE_REVISION','RENTAL_SERVICE_STARTED_AT',
 'RENTAL_FURNISHING_INDEX_ENABLED','RENTAL_FURNISHING_VALIDATION_RECEIPT','RENTAL_FURNISHING_RULES_JSON',
 'RENTAL_MAINTENANCE_BUDGET_SEC','RENTAL_BACKFILL_BATCH','RENTAL_ARCHIVE_CHECK_BATCH',
 'RENTAL_DB_MAINTENANCE_EVERY','RENTAL_ALLOW_GEO_COMPLEX_GUESS','RENTAL_INDEX_REFRESH_SEC') ORDER BY key"""


async def export_slice(dsn, *, max_scan=MAX_SCAN, connect=None):
    if type(max_scan) is not int or not 1 <= max_scan <= MAX_SCAN:
        raise ValueError("Invalid complete scan bound")
    if connect is None:
        import asyncpg
        connect = asyncpg.connect
    conn = await connect(dsn,command_timeout=20,server_settings={
        "default_transaction_read_only":"on","statement_timeout":"20000",
        "lock_timeout":"2000","application_name":"hatuli_rental_editorial_slice_readonly"})
    try:
        async with conn.transaction(readonly=True,isolation="repeatable_read"):
            if await conn.fetchval("SHOW transaction_read_only") != "on":
                raise RuntimeError("Read-only transaction required")
            if await conn.fetchval("SHOW transaction_isolation") != "repeatable read":
                raise RuntimeError("Repeatable-read transaction required")
            now = await conn.fetchval("SELECT now()")
            expected = await conn.fetchval("SELECT count(*) FROM rental_listings WHERE city='astana' AND prop_type='apartment' AND is_active IS TRUE")
            if type(expected) is not int or expected > max_scan:
                raise ValueError("Incomplete bounded rental population; export refused")
            settings = {r["key"]:r["value"] for r in await conn.fetch(SETTINGS_SQL)}
            evidence = {}
            for name,query,maximum in EVIDENCE_READS:
                values = [dict(r) for r in await conn.fetch(query,maximum+1)]
                if len(values)>maximum:
                    raise ValueError(f"Incomplete bounded identity evidence: {name}")
                evidence[name]=values
            rows = [dict(r) for r in await conn.fetch(rental_rows_sql(),max_scan+1)]
            report = build_slice(rows,evidence,settings,now=now,expected_count=expected,max_scan=max_scan)
            # Expose only operational settings projected by the established audit.
            from scripts.audit_rental_coverage import setting_projection
            from bot.core.rental_service_metrics import RentalCyclePlan
            from dataclasses import asdict
            report["collector_settings"]=setting_projection([{"key":k,"value":v} for k,v in settings.items()])
            report["effective_maintenance_config"]=asdict(RentalCyclePlan.from_settings(settings.get))
            report["transaction_read_only"]=True
            report["transaction_isolation"]="repeatable read"
            report["identity_evidence_counts"]={key:len(value) for key,value in evidence.items()}
            return report
    finally:
        await conn.close()


def write_private(path, report):
    """Atomic private artifact; never overwrite an earlier captured slice."""
    path = Path(path)
    if path.exists(): raise ValueError("Existing slice preserved; choose a new private output path")
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    data=(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n").encode()
    name=None
    try:
        with tempfile.NamedTemporaryFile("wb",dir=path.parent,prefix=".rental-editorial-",delete=False) as f:
            name=f.name
            os.chmod(name,0o600)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # Atomic no-clobber installation, including concurrent invocations.
        os.link(name,path)
    finally:
        if name is not None: os.unlink(name)
    return {"sha256":hashlib.sha256(data).hexdigest(),"size":len(data)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,default=ROOT)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--max-scan",type=int,default=MAX_SCAN)
    args=parser.parse_args()
    if args.output.exists(): parser.error("Existing private output is preserved")
    from dotenv import dotenv_values
    dsn=dotenv_values(args.root/".env",interpolate=False).get("DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn: parser.error("DATABASE_URL required")
    report=asyncio.run(export_slice(dsn,max_scan=args.max_scan))
    metadata=write_private(args.output,report)
    print(json.dumps({**metadata,"generated_at":report["generated_at"],
        "complete_database_scan":True,"scanned":report["active_astana_apartment_rows_scanned"],
        "eligible_source_ids":report["n_unique_source_after_conservative_dedup"],
        "descriptive_ready_cohorts":report["ready_descriptive_cohorts"],
        "n_physical":None,"ranking_ready":False,"database_writes":0}))


if __name__ == "__main__": main()
