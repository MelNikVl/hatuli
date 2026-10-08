#!/usr/bin/env python3
"""Read-only inventory and canonical proposal; does not run schema migrations.

Run inside the server checkout:
    venv/bin/python scripts/audit_complex_identity_current.py --output /tmp/complex_identity_audit.json
Optional --binding-preview evaluates binding against the current stored map,
not against proposed canonical changes. No production assignments are written.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg
from dotenv import load_dotenv

from bot.core.complex_binding import build_preview_sql
from bot.core.complex_canonical import compute_canonical


async def audit(dsn: str, *, binding_preview: bool = False) -> dict:
    # Deliberately NOT bot.db.pg.init_pool(): that applies migrations on startup.
    conn = await asyncpg.connect(dsn, command_timeout=180)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            rows = [dict(row) for row in await conn.fetch("""
                SELECT id, name, address, lat, lon, parent_complex_id, is_umbrella,
                       housing_class, is_newbuild, krisha_url, canonical_id, canonical_reason,
                       coalesce(is_garbage, false) AS is_garbage,
                       coalesce(is_street, false) AS is_street
                  FROM complexes ORDER BY id
            """)]
            counts = {row['cid']: row['n'] for row in await conn.fetch("""
                SELECT complex_id AS cid, count(*) AS n FROM apartment_listings
                 WHERE complex_id IS NOT NULL GROUP BY 1
            """)}
            relations = [dict(row) for row in await conn.fetch("""
                SELECT complex_id_a, complex_id_b, relation_type FROM complex_relations
            """)]
            eligible = [row for row in rows if not row['is_garbage'] and not row['is_street']]
            proposed = compute_canonical(eligible, counts, relations)
            changes = []
            for row in eligible:
                target, reason = proposed[row['id']]
                if (target, reason) != (row['canonical_id'], row['canonical_reason']):
                    changes.append({
                        'id': row['id'], 'name': row['name'], 'address': row['address'],
                        'old_canonical_id': row['canonical_id'], 'old_reason': row['canonical_reason'],
                        'proposed_canonical_id': target, 'proposed_reason': reason,
                        'direct_bound_listings': counts.get(row['id'], 0),
                    })
            inventory = dict(await conn.fetchrow("""
                SELECT count(*) AS total_listings, count(complex_id) AS bound_listings,
                       count(resolved_house_id) AS house_resolved_listings,
                       count(*) FILTER (WHERE complex_id IS NULL AND nullif(btrim(complex_name), '') IS NOT NULL)
                           AS named_unbound_listings
                  FROM apartment_listings
            """))
            address_groups = [dict(row) for row in await conn.fetch("""
                SELECT coalesce(c.canonical_id, c.id) AS complex_id,
                       count(DISTINCT lower(btrim(a.address))) AS observed_addresses,
                       array_agg(DISTINCT a.address ORDER BY a.address) AS examples
                  FROM apartment_listings a JOIN complexes c ON c.id = a.complex_id
                 WHERE nullif(btrim(a.address), '') IS NOT NULL
                   AND coalesce(c.is_garbage, false) = false
                 GROUP BY 1 HAVING count(DISTINCT lower(btrim(a.address))) > 1
                 ORDER BY observed_addresses DESC, complex_id
            """)]
            for group in address_groups:
                group['examples'] = group['examples'][:10]
            report = {
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'read_only': True,
                'inventory': {**inventory, 'total_complex_rows': len(rows),
                              'eligible_complex_rows': len(eligible),
                              'reviewed_relations': len(relations)},
                'proposed_reason_counts': dict(Counter(reason or 'canonical' for _, reason in proposed.values())),
                'proposed_canonical_changes': changes,
                'complexes_with_multiple_observed_addresses': address_groups,
                'notes': [
                    'Observed address strings are candidates, not verified building addresses.',
                    'Direct bound counts exclude impact through aliases/properties; compute a full manifest before applying.',
                    'No SQL mutations or schema migrations were executed.',
                ],
            }
            if binding_preview:
                preview = [dict(row) for row in await conn.fetch(build_preview_sql())]
                report['binding_preview_for_current_canonical_map'] = {
                    'changed': len(preview),
                    'would_unbind': sum(row['complex_id'] is None for row in preview),
                    'methods': dict(Counter(row['method'] or 'unbound' for row in preview)),
                    'changes': preview,
                }
            return report
    finally:
        await conn.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--binding-preview', action='store_true')
    args = parser.parse_args()
    load_dotenv()
    dsn = os.getenv('DATABASE_URL')
    if not dsn:
        parser.error('DATABASE_URL is required; use the server environment')
    report = await audit(dsn, binding_preview=args.binding_preview)
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True)
    report['checkout_commit'] = commit.stdout.strip() if commit.returncode == 0 else None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    print(f"Read-only audit saved: {args.output}; proposed canonical changes: {len(report['proposed_canonical_changes'])}")


if __name__ == '__main__':
    asyncio.run(main())
