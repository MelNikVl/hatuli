"""Read only validated, personal monthly-rental projections; never fit on requests.

There is deliberately no trained rental model shipped with this adapter. An
asking-rent estimate is conditional on the advertisement remaining listed. A
source-confirmed removal is not evidence of a tenancy or achieved contract rent.
Missing/invalid projections return an empty mapping: hide the entire UI block.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

METHOD_VERSION = 'monthly-rental-forecast-v1'
PRICE_TARGET = 'future_asking_rent_conditional_on_remaining_listed'
REMOVAL_TARGET = 'remaining_days_to_source_confirmed_removal_not_letting'
DEFAULT_PATH = Path(__file__).resolve().parents[2] / 'var/rental-forecasts.json'
MAX_AGE = timedelta(hours=48)
LOCAL_TZ = timezone(timedelta(hours=5))
MIN_TEST_PROPERTIES = 200


def _utc(value):
    if isinstance(value,str):
        try:
            value=datetime.fromisoformat(value.replace('Z','+00:00'))
        except (ValueError,OverflowError):
            return None
    return value.astimezone(timezone.utc) if isinstance(value,datetime) and value.tzinfo else None


def _number(value):
    return type(value) in (int,float) and math.isfinite(value)


def _count(value, minimum=0):
    return type(value) is int and value>=minimum


def offer_signature(listing: dict) -> str | None:
    """An ID/price match alone cannot reuse a prediction after a new episode."""
    if not isinstance(listing,dict):
        return None
    episode=_utc(listing.get('rental_episode_started_at'))
    identifier=listing.get('id')
    price,rooms,area=(listing.get(k) for k in ('price','rooms','area'))
    if (not isinstance(identifier,str) or not identifier or len(identifier)>80
            or listing.get('city')!='astana' or listing.get('rent_period')!='monthly'
            or not episode or type(price) is not int or not 50000<=price<=2000000
            or type(rooms) is not int or not 1<=rooms<=6
            or not _number(area) or not 10<=area<=500):
        return None
    fields={key:listing.get(key) for key in ('id','city','district','complex_id','hex_id','hex_edge_m')}
    fields.update(price=price,rooms=rooms,area=float(area),rent_period='monthly',
                  rental_episode_started_at=episode.isoformat())
    try:
        raw=json.dumps(fields,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    except (ValueError,TypeError):
        return None
    return hashlib.sha256(raw.encode()).hexdigest()


def _folds_valid(folds,generated):
    if not isinstance(folds,list) or len(folds)<2:
        return False
    previous_test_end=None
    for row in folds:
        if not isinstance(row,dict):
            return False
        values=[_utc(row.get(key)) for key in (
            'train_end','calibration_start','calibration_end','test_start','test_end')]
        if any(value is None for value in values):
            return False
        train_end,cal_start,cal_end,test_start,test_end=values
        if not (train_end<cal_start<=cal_end<test_start<test_end<=generated):
            return False
        if previous_test_end is not None and test_start<previous_test_end:
            return False
        if (row.get('disjoint_property_groups') is not True
                or row.get('future_labels_purged') is not True
                or row.get('features_known_at_anchor') is not True):
            return False
        previous_test_end=test_end
    return True


def report_ready(report,generated):
    """Predetermined held-out gates; no accepting a naked `status=ready` flag."""
    if (not isinstance(report,dict) or report.get('status')!='ready'
            or report.get('target') not in (PRICE_TARGET,REMOVAL_TARGET)
            or not isinstance(report.get('model_version'),str) or not 1<=len(report['model_version'])<=100
            or not _folds_valid(report.get('folds'),generated)):
        return False
    metrics=report.get('validation')
    if not isinstance(metrics,dict):
        return False
    independent=metrics.get('independent_properties')
    sample=metrics.get('sample_size')
    coverage=metrics.get('coverage_pct')
    ci=metrics.get('improvement_ci95')
    if (not _count(independent,MIN_TEST_PROPERTIES) or not _count(sample,independent)
            or not _number(coverage) or not 75<=coverage<=95
            or metrics.get('interval_nominal_coverage_pct')!=80
            or not isinstance(ci,list) or len(ci)!=2
            or not all(_number(v) for v in ci) or not 0<ci[0]<=ci[1]):
        return False
    if report['target']==PRICE_TARGET:
        changed=metrics.get('changed_properties')
        improvement=metrics.get('rms_log_improvement_pct')
        width=metrics.get('median_interval_width_pct')
        rms,baseline=(metrics.get(k) for k in ('rms_log_error','baseline_rms_log_error'))
        mape,baseline_mape=(metrics.get(k) for k in ('mape_pct','baseline_mape_pct'))
        return (type(report.get('horizon_days')) is int and report['horizon_days'] in (7,14,30)
                and report.get('baseline')=='unchanged_asking_rent'
                and _count(changed,50) and changed<=independent
                and all(_number(v) for v in (improvement,width,rms,baseline,mape,baseline_mape))
                and 0<=rms<baseline and 5<=improvement<=100
                and abs(improvement-(1-rms/baseline)*100)<=.1
                and 0<=mape<=baseline_mape*1.05 and 0<width<=40)
    brier,baseline=(metrics.get(k) for k in ('integrated_brier_score','baseline_integrated_brier_score'))
    events=metrics.get('confirmed_removal_properties')
    return (report.get('baseline')=='kaplan_meier_segment'
            and report.get('right_censoring_accounted') is True
            and report.get('complete_at_risk_population') is True
            and report.get('interval_censoring_accounted') is True
            and report.get('prediction_time_origin')=='remaining_from_anchor'
            and _count(events,100) and events<=independent
            and all(_number(v) for v in (brier,baseline))
            and 0<=brier<baseline<=1 and brier<=baseline*.95)


@lru_cache(maxsize=2)
def _read(path,mtime,size):
    try:
        if size>20_000_000:
            return None
        data=json.loads(Path(path).read_text(encoding='utf-8'))
        if (not isinstance(data,dict) or data.get('schema_version')!=1
                or data.get('method_version')!=METHOD_VERSION
                or data.get('source_history_verified') is not True
                or data.get('independent_property_groups_verified') is not True
                or not isinstance(data.get('reports'),list)
                or not isinstance(data.get('offers'),dict)):
            return None
        return data
    except (OSError,ValueError,TypeError):
        return None


def rental_forecast_for_offer(listing: dict, *, now=None, path=None) -> dict:
    """Only ready `asking_price` / `removal` keys; no public refusal messages.

Call after the normal source/access guards. No sale-model fallback, city mean,
database reads, network requests, training, or inferred achieved rent is used.
"""
    now=_utc(now) if now is not None else datetime.now(timezone.utc)
    if not now or not isinstance(listing,dict):
        return {}
    observed=_utc(listing.get('last_seen'))
    if (listing.get('is_active') is not True or listing.get('archived_at') is not None
            or listing.get('is_duplicate') is True or listing.get('dup_needs_review') is True
            or listing.get('duplicate_of') or listing.get('rent_period_conflict') is True
            or not observed or not now-timedelta(hours=72)<=observed<=now):
        return {}
    signature=offer_signature(listing)
    if not signature:
        return {}
    path=Path(path) if path is not None else DEFAULT_PATH
    try:
        info=path.stat()
        data=_read(str(path),info.st_mtime_ns,info.st_size)
    except (OSError,ValueError,TypeError):
        return {}
    generated=_utc(data.get('generated_at')) if data else None
    if not generated or not now-MAX_AGE<=generated<=now:
        return {}
    stored=data['offers'].get(listing['id'])
    if not isinstance(stored,dict) or stored.get('signature')!=signature:
        return {}
    as_of=_utc(stored.get('as_of'))
    episode=_utc(listing.get('rental_episode_started_at'))
    if not as_of or not episode<=as_of<=generated or not now-MAX_AGE<=as_of<=now:
        return {}
    result={}
    label=as_of.astimezone(LOCAL_TZ).strftime('%d.%m.%Y')
    candidates=stored.get('forecasts')
    if not isinstance(candidates,list):
        return {}
    for candidate in candidates:
        if not isinstance(candidate,dict) or candidate.get('status')!='ready':
            continue
        target=candidate.get('target')
        reports=[r for r in data['reports'] if isinstance(r,dict)
                 and r.get('target')==target and r.get('model_version')==candidate.get('model_version')
                 and r.get('horizon_days')==candidate.get('horizon_days')]
        if len(reports)!=1 or not report_ready(reports[0],generated):
            continue
        if target==PRICE_TARGET:
            horizon=candidate.get('horizon_days')
            values=[candidate.get(k) for k in ('low','estimate','high')]
            target_date=as_of.date()+timedelta(days=horizon)
            if (all(type(v) is int for v in values)
                    and 50000<=values[0]<=values[1]<=values[2]<=2000000
                    and .5<=values[1]/listing['price']<=1.5
                    and candidate.get('target_date')==target_date.isoformat() and target_date>now.date()
                    and _count(candidate.get('historical_analog_count'),20)):
                value={key:candidate[key] for key in ('horizon_days','estimate','low','high')}
                value.update(as_of_label=label,target_date_label=target_date.strftime('%d.%m.%Y'))
                if 'asking_price' not in result or horizon<result['asking_price']['horizon_days']:
                    result['asking_price']=value
        elif target==REMOVAL_TARGET:
            values=[candidate.get(k) for k in ('low_days','median_days','high_days')]
            elapsed=(now-as_of).total_seconds()/86400
            if (all(_number(v) for v in values) and 0<=values[0]<=values[1]<=values[2]<=365
                    and values[1]>elapsed and values[2]>elapsed):
                result['removal']={key:round(max(0,candidate[key]-elapsed),1)
                                   for key in ('low_days','median_days','high_days')}
                result['removal']['as_of_label']=label
    return result
