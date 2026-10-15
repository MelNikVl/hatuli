"""SQL filter gates for controlled rental scenario snapshots, never legacy yields."""
from dataclasses import asdict
import json
import math
import re

from bot.core.rental_yield import NetYieldParams


def qualified_snapshot_sql(alias, params_placeholder):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", alias) or not re.fullmatch(r"\$[1-9]\d*", params_placeholder):
        raise ValueError("Trusted SQL identifiers required")
    root = f"{alias}.rental_economics"
    def num(path):
        expr = root+"".join(f"->'{part}'" for part in path.split("."))
        return f"(CASE WHEN jsonb_typeof({expr})='number' THEN ({expr}#>>'{{}}')::numeric END)"
    source = root+"->'rent_source'"
    return f"""({root}->>'schema_version'='1' AND {root}->>'city_scope'='astana'
      AND {root}->>'asking_prices'='true' AND {root}->>'scenario'='true'
      AND {root}->'params_used'={params_placeholder}::jsonb
      AND {source}->>'policy_version'='3' AND {source}->>'window_days'='30'
      AND {num('rent_source.oldest_epoch')} BETWEEN extract(epoch FROM now())-259200 AND {num('rent_source.newest_epoch')}
      AND {num('rent_source.newest_epoch')} BETWEEN {num('rent_source.oldest_epoch')} AND {num('rent_source.updated_epoch')}
      AND {num('rent_source.updated_epoch')} <= {num('computed_epoch')}
      AND {num('computed_epoch')} BETWEEN extract(epoch FROM now())-93600 AND extract(epoch FROM now())
      AND {num('net_yield_pct')} BETWEEN -100 AND 100
      AND {root}->'listing'->>'id'={alias}.id
      AND {num('listing.price')}={alias}.price
      AND abs({num('listing.area')}-{alias}.area::numeric)<0.0001
      AND {num('listing.rooms')}={alias}.rooms
      AND ({root}->'listing'->>'district') IS NOT DISTINCT FROM {alias}.district
      AND ({root}->'listing'->>'market_type') IS NOT DISTINCT FROM {alias}.market_type
      AND ({root}->'listing'->>'complex_id') IS NOT DISTINCT FROM {alias}.complex_id::text
      AND ({root}->'listing'->>'complex_name') IS NOT DISTINCT FROM {alias}.complex_name
      AND ({root}->'listing'->>'complex_resolution') IS NOT DISTINCT FROM {alias}.complex_resolution
      AND ({root}->'listing'->>'resolved_house_id') IS NOT DISTINCT FROM {alias}.resolved_house_id::text
      AND ({root}->'listing'->>'house_attribution') IS NOT DISTINCT FROM {alias}.house_attribution
      AND {alias}.is_active IS TRUE AND {alias}.archived_at IS NULL
      AND {alias}.market_type IS DISTINCT FROM 'primary'
      AND NOT coalesce({alias}.is_duplicate,false) AND NOT coalesce({alias}.dup_needs_review,false)
      AND nullif(btrim({alias}.duplicate_of),'') IS NULL
      AND {alias}.last_seen BETWEEN now()-interval '72 hours' AND now())"""


def append_rental_filters(conditions, parameters, *, net_yield_min=0, rent_confidence="", sort="", high_yield=False, alias="a"):
    from bot.db import settings
    if rent_confidence not in ("", "low", "medium", "high") or sort not in ("", "score", "net_yield_pct"):
        raise ValueError("Неверный фильтр доходности")
    if isinstance(net_yield_min, bool) or not math.isfinite(net_yield_min) or not 0 <= net_yield_min <= 100:
        raise ValueError("Доходность должна быть от 0 до 100%")
    active = net_yield_min > 0 or rent_confidence or sort == "net_yield_pct" or high_yield
    if not active:
        return None
    params = NetYieldParams.from_settings(settings.get)
    parameters.append(json.dumps(asdict(params)))
    placeholder = f"${len(parameters)}"
    conditions.append("AND "+qualified_snapshot_sql(alias, placeholder))
    conf = rent_confidence or "medium"
    levels = {"low": "('low','medium','high')", "medium": "('medium','high')", "high": "('high')"}[conf]
    conditions.append(f"AND {alias}.rental_economics->>'confidence' IN {levels}")
    minimum_complex = max(5, settings.get_int("RENT_MIN_SAMPLE_COMPLEX", 5))
    minimum_area = max(10, settings.get_int("RENT_MIN_SAMPLE_AREA", 10))
    source = f"{alias}.rental_economics->'rent_source'"
    n = f"(CASE WHEN jsonb_typeof({source}->'n')='number' THEN ({source}->>'n')::numeric END)"
    net = f"(CASE WHEN jsonb_typeof({alias}.rental_economics->'net_yield_pct')='number' THEN ({alias}.rental_economics->>'net_yield_pct')::numeric END)"
    conditions.append(f"AND (({alias}.rental_economics->>'confidence'='high' AND {source}->>'level'='ЖК' AND {n}>={minimum_complex}) OR ({alias}.rental_economics->>'confidence'='medium' AND {source}->>'level'='район' AND {n}>={minimum_area}) OR ({alias}.rental_economics->>'confidence'='low' AND {source}->>'level' IN ('город','город (все комн.)','район (все комн.)') AND {n}>={minimum_area}))")
    parameters.append(net_yield_min)
    conditions.append(f"AND {net}>=${len(parameters)}")
    if high_yield:
        conditions.append(f"""AND {alias}.rental_economics->>'confidence' IN ('medium','high')
            AND {net} >= (
              SELECT b.p90_net FROM rental_yield_benchmarks b WHERE b.rooms={alias}.rooms AND b.n>=10
                AND b.policy_version='3' AND b.params_used={placeholder}::jsonb
                AND b.computed_at BETWEEN now()-interval '26 hours' AND now()
                AND b.source_oldest_at BETWEEN now()-interval '72 hours' AND b.source_newest_at
                AND b.source_newest_at<=b.computed_at
                AND ((b.scope='district' AND b.scope_value={alias}.district) OR (b.scope='city' AND b.scope_value='astana')
                  OR b.scope IN ('class','district_class') AND EXISTS (
                    SELECT 1 FROM complexes hc WHERE hc.id=coalesce({alias}.resolved_house_id,{alias}.complex_id)
                      AND {alias}.complex_resolution IS DISTINCT FROM 'geo' AND {alias}.complex_resolution IS DISTINCT FROM 'unbound'
                      AND {alias}.house_attribution IS DISTINCT FROM 'geo' AND {alias}.house_attribution IS DISTINCT FROM 'address_geo'
                      AND NOT coalesce(hc.is_garbage,false) AND NOT coalesce(hc.is_street,false)
                      AND NOT coalesce(hc.is_umbrella,false) AND hc.canonical_reason IS DISTINCT FROM 'junk_unmatched'
                      AND b.scope_value=CASE WHEN b.scope='class' THEN hc.housing_class ELSE {alias}.district||' · '||hc.housing_class END))
              ORDER BY CASE b.scope WHEN 'district_class' THEN 0 WHEN 'district' THEN 1 WHEN 'class' THEN 2 ELSE 3 END LIMIT 1)""")
    return f"{net} DESC NULLS LAST,{alias}.id" if sort == "net_yield_pct" else None
