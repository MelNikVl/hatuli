"""Public monthly rental selection from source-checked, read-only snapshots.

Asking rent comparisons are current estimates, never future prices or leases.
The request path reads one validated file; source and DB I/O are offline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
from statistics import median
import tempfile

from bot.core.daily_picks import LOCAL_TIMEZONE, sanitized_listing_address, _valid_source
from bot.core.comparison_offer_details import price_history

MAX_RENT = 500_000
MIN_PICKS = 10
MAX_PICKS = 30
MIN_DISCOUNT = 5.0
MAX_DISCOUNT = 30.0
MAX_SOURCE_AGE = timedelta(hours=72)
CHECK_AGE = timedelta(hours=2)
FIELDS = ('id','rank','source_url','title','price','currency','price_per_m2','area','rooms',
          'floor','floors_total','address','district','complex_name','photo_urls','reasons',
          'observed_at','source_checked_at','price_comparison','price_history','selection_facts',
          'rent_period','rental_episode_started_at','rental_episode_last_seen_at')
FIELDS += ('complex_id','hex_id','hex_edge_m')
METHODOLOGY = {
    'summary': 'Помесячная аренда до 500 000 ₸. Ставка за м² минимум на 5% ниже сопоставимых предложений. У каждой квартиры четыре подтверждённых основания выбора.',
    'details': ['Сравниваем ту же комнатность и площадь ±15%; для проверенного ЖК — от пяти объявлений, для района — от десяти. Из выборки исключаем саму квартиру и известные дубли.',
                'Период аренды подтверждён месячным каталогом. Источник выбранной квартиры проверен обычным браузером перед публикацией.',
                'Хозяин и мебель учитываются только при явном подтверждении в исходном объявлении. Привязку к ЖК по названию или близости на карте не придумываем.'],
    'caveat': 'Сравнение цен предложений, а не заключённых договоров. Подборка не подтверждает право сдачи, отсутствие комиссии или итоговую сумму договора. Снятие объявления не означает сдачу квартиры.'}


def snapshot_path():
    return Path(os.environ.get('HATULI_RENTAL_PICKS_PATH') or Path(__file__).resolve().parents[2]/'var/rental-picks.json')


def utc(value):
    if isinstance(value,str):
        value=datetime.fromisoformat(value.replace('Z','+00:00'))
    if not isinstance(value,datetime) or value.tzinfo is None:
        raise ValueError('Timezone-aware observation required')
    return value.astimezone(timezone.utc)


def number(value):
    if isinstance(value,bool):
        return None
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError,OverflowError):
        return None


def _eligible(row,now):
    from bot.core.comparison_rental_estimate import _NONMONTHLY
    try:
        price,area,rooms=[number(row.get(key)) for key in ('price','area','rooms')]
        return (re.fullmatch(r'\d{5,20}',str(row.get('id',''))) is not None and _valid_source(row)
            and row.get('city')=='astana' and row.get('prop_type')=='apartment'
            and row.get('rent_period')=='monthly' and row.get('rent_period_conflict') is not True
            and not _NONMONTHLY.search(row.get('title') or '')
            and row.get('is_active') is True and row.get('archived_at') is None
            and row.get('is_duplicate') is not True and row.get('dup_needs_review') is not True
            and not row.get('duplicate_of') and None not in (price,area,rooms)
            and 50_000<=price<=2_000_000 and 20<=area<=300 and rooms.is_integer() and 1<=rooms<=6
            and 12<=area/rooms<=100 and 1000<=price/area<=50000
            and now-MAX_SOURCE_AGE<=utc(row['last_seen'])<=now
            and (row.get('complex_id') is None or row.get('complex_binding_verified') is True))
    except (ValueError,TypeError,KeyError):
        return False


def _photos(row):
    from bot.core.rental_parser import _rental_card_photo_url
    raw=row.get('photos')
    if isinstance(raw,str):
        try: raw=json.loads(raw)
        except ValueError: raw=[]
    raw=(raw if isinstance(raw,list) else [])+[row.get('photo_url')]
    return list(dict.fromkeys(safe for value in raw if (safe:=_rental_card_photo_url(value))))[:3]


def _facts(check):
    raw=check.get('facts',{})
    if not isinstance(raw,dict) or any(key not in ('is_owner','furniture_present','washer_and_fridge') or type(value) is not bool for key,value in raw.items()):
        raise ValueError('Invalid checked rental facts')
    return {key:value for key,value in raw.items() if value is True}


def _history(row,now):
    # Legacy rental_price_history lacks a proven period at each old event.
    # Only same-episode monthly source observations can form the public graph.
    if not row.get('rental_episode_started_at') or not row.get('rental_episode_last_seen_at'):
        return None
    observations=row.get('observations') or []
    if isinstance(observations,str):
        observations=json.loads(observations)
    episode=utc(row['rental_episode_started_at'])
    latest=utc(row['rental_episode_last_seen_at'])
    values={}
    for point in observations:
        try:
            at=utc(point['observed_at'])
            value=point['price']
            if (point.get('period_source_checked') is True and point.get('rent_period')=='monthly'
                and utc(point['episode_started_at'])==episode and episode<=at<=latest<=now
                and type(value) is int and 50_000<=value<=2_000_000):
                values[at]=value
        except (KeyError,ValueError,TypeError):
            continue
    values[latest]=int(row['price'])
    events=[]
    previous=None
    for at,value in sorted(values.items()):
        if previous is not None and previous!=value:
            events.append({'old_price':previous,'new_price':value,'at':at.isoformat()})
        previous=value
    return price_history(events,int(row['price']),now=now)


def rental_factors(item):
    c=item['price_comparison']
    reasons=[f"Ставка за м² на {c['discount_pct']:g}% ниже похожих квартир {c['scope']}. Для этой площади разница — около {round(c['reference_price_per_m2']*item['area']-item['price']):,} ₸/мес.".replace(',', ' ')]
    facts=item.get('selection_facts') or {}
    if facts.get('is_owner') is True:
        reasons.append('В источнике арендодатель указан как хозяин.')
    if facts.get('furniture_present') is True:
        reasons.append('В объявлении указана мебель — часть обустройства уже есть.')
    if facts.get('washer_and_fridge') is True:
        reasons.append('Холодильник и стиральная машина указаны в объявлении.')
    if item['area']>=20*item['rooms']+20:
        reasons.append(f"Просторная квартира: {item['area']:g} м² на {item['rooms']} {'комнату' if item['rooms']==1 else 'комнаты' if item['rooms']<5 else 'комнат'}.")
    if 1<item['floor']<item['floors_total']:
        reasons.append(f"{item['floor']} этаж из {item['floors_total']}.")
    history=item.get('price_history') or {}
    if history.get('status')=='consistent':
        events=history.get('events') or []
        if events and all(event.get('old_price') and event['old_price']>=event['new_price'] for event in events):
            cut=events[0]['old_price']-item['price']
            if cut>=10000 and cut/events[0]['old_price']>=.03:
                reasons.append(f"Арендную ставку снизили на {cut:,} ₸/мес.".replace(',',' '))
    return reasons[:4]


def source_check_matches(row,check,now):
    try:
        address,note=sanitized_listing_address(row.get('address'),row['price'],now)
        checked_address,checked_note=sanitized_listing_address(check.get('address'),row['price'],now) if isinstance(check,dict) else (None,None)
        return (isinstance(check,dict) and check.get('status')=='active'
            and str(check.get('id'))==str(row['id']) and check.get('source_url')==row['url']
            and check.get('rent_astana') is True and check.get('rent_period')=='monthly'
            and address is not None and checked_address is not None
            and note==checked_note=='Адрес из объявления.'
            and address.casefold().replace('ё','е')==checked_address.casefold().replace('ё','е')
            and now-CHECK_AGE<=utc(check['checked_at'])<=now
            and all(type(check.get(key)) in (int,float) and not isinstance(check.get(key),bool)
                and number(check[key])==number(row[key]) for key in ('price','rooms','floor','floors_total'))
            and type(check.get('area')) in (int,float) and number(check['area']) is not None
            and abs(number(check['area'])-number(row['area']))<=.0500001)
    except (KeyError,TypeError,ValueError):
        return False


def build_rental_picks(rows,*,now=None,checks=None,limit=MAX_PICKS,require_checks=True):
    now=utc(now or datetime.now(timezone.utc))
    rows=[dict(row) for row in rows if _eligible(row,now)]
    unique={str(row['id']):row for row in rows}
    rows=list(unique.values())
    candidates=[]
    for row in rows:
        if row['price']>MAX_RENT:
            continue
        check=(checks or {}).get(str(row['id']),{})
        if require_checks and not source_check_matches(row,check,now):
            continue
        area=float(row['area'])
        peers=[p for p in rows if p['id']!=row['id'] and p['rooms']==row['rooms']
               and .85*area<=float(p['area'])<=1.15*area]
        complex_id=row.get('index_complex_id') if row.get('complex_binding_verified') is True else None
        local=[p for p in peers if complex_id and p.get('complex_binding_verified') is True and p.get('index_complex_id')==complex_id]
        scope='в этом ЖК'
        if len(local)<5:
            local=[p for p in peers if row.get('district') and p.get('district')==row['district']]
            scope='в этом районе'
            if len(local)<10:
                continue
        rates=sorted(float(p['price'])/float(p['area']) for p in local)
        reference=median(rates)
        if (rates[3*len(rates)//4]-rates[len(rates)//4])/reference>.5:
            continue
        discount=round(100*(1-float(row['price'])/area/reference),1)
        if not MIN_DISCOUNT<=discount<=MAX_DISCOUNT:
            continue
        floor,total=number(row.get('floor')),number(row.get('floors_total'))
        if floor is None or total is None or not floor.is_integer() or not total.is_integer() or not 1<=floor<=total<=60:
            continue
        address,note=sanitized_listing_address(row.get('address'),row['price'],now)
        if not address or note!='Адрес из объявления.':
            continue
        if 'photo_urls' in check:
            photos=_photos({'photos':check['photo_urls']})
            if not photos or photos!=check['photo_urls']:
                raise ValueError('Invalid source-checked rental photos')
        else:
            photos=_photos(row)
        if not photos:
            continue
        item={'id':str(row['id']),'rank':0,'source_url':row['url'],
            'title':f"{row['rooms']}-комнатная квартира · {area:g} м²",'price':int(row['price']),
            'currency':'KZT','rent_period':'monthly','price_per_m2':round(row['price']/area),
            'area':round(area,2),'rooms':int(row['rooms']),'floor':int(floor),'floors_total':int(total),
            'address':address,'district':row.get('district') or '',
            'complex_name':(row.get('index_complex_name') or '') if complex_id else '',
            'photo_urls':photos,'observed_at':utc(row['last_seen']).isoformat(),
            'source_checked_at':check.get('checked_at'),'price_comparison':{
                'discount_pct':discount,'reference_price_per_m2':reference,'comparable_count':len(local),'scope':scope},
            'selection_facts':_facts(check),'price_history':_history(row,now),
            'rental_episode_started_at':utc(row['rental_episode_started_at']).isoformat() if row.get('rental_episode_started_at') else None,
            'rental_episode_last_seen_at':utc(row['rental_episode_last_seen_at']).isoformat() if row.get('rental_episode_last_seen_at') else None,
            'complex_id':complex_id,'hex_id':row.get('hex_id'),'hex_edge_m':row.get('hex_edge_m')}
        item['reasons']=rental_factors(item)
        if require_checks and len(item['reasons'])!=4:
            continue
        candidates.append(item)
    candidates.sort(key=lambda item:(-item['price_comparison']['discount_pct'],item['price'],item['id']))
    items=candidates[:max(0,min(MAX_PICKS,limit))]
    for rank,item in enumerate(items,1): item['rank']=rank
    return {'schema_version':1,'selection_date':now.astimezone(LOCAL_TIMEZONE).date().isoformat(),
            'generated_at':now.isoformat(),'source_observed_at':max((x['observed_at'] for x in items),default=now.isoformat()),
            'total_candidates':len(candidates),'items':items,'methodology':METHODOLOGY}


def validate_snapshot(snapshot):
    if (not isinstance(snapshot,dict) or snapshot.get('schema_version')!=1 or not isinstance(snapshot.get('items'),list)
        or set(snapshot)-{'schema_version','selection_date','generated_at','source_observed_at','total_candidates','items','methodology'}):
        raise ValueError('Invalid rental snapshot')
    generated=utc(snapshot['generated_at'])
    if snapshot.get('selection_date')!=generated.astimezone(LOCAL_TIMEZONE).date().isoformat():
        raise ValueError('Invalid rental selection date')
    if not MIN_PICKS<=len(snapshot['items'])<=MAX_PICKS:
        raise ValueError('At least ten independently checked rentals required')
    seen=set()
    for item in snapshot['items']:
        if not isinstance(item,dict) or set(item)-set(FIELDS) or item.get('id') in seen:
            raise ValueError('Invalid rental item shape')
        seen.add(item['id'])
        row={**item,'url':item.get('source_url'),'city':'astana','prop_type':'apartment','is_active':True,
             'last_seen':item.get('observed_at'),'complex_binding_verified':item.get('complex_id') is not None}
        if not _eligible(row,generated) or item.get('price',MAX_RENT+1)>MAX_RENT:
            raise ValueError('Invalid public rental')
        if not generated-CHECK_AGE<=utc(item['source_checked_at'])<=generated:
            raise ValueError('Fresh browser confirmation required')
        address,note=sanitized_listing_address(item.get('address'),item['price'],generated)
        if address!=item.get('address') or note!='Адрес из объявления.':
            raise ValueError('Exact sanitized source address required')
        if _facts({'facts':item.get('selection_facts')})!=item.get('selection_facts'):
            raise ValueError('Invalid source facts')
        if (not isinstance(item.get('photo_urls'),list) or not 1<=len(item['photo_urls'])<=3
            or item.get('photo_urls')!=_photos({'photos':item.get('photo_urls')})):
            raise ValueError('Invalid rental photos')
        floor,total=number(item.get('floor')),number(item.get('floors_total'))
        if floor is None or total is None or not floor.is_integer() or not total.is_integer() or not 1<=floor<=total<=60:
            raise ValueError('Invalid rental floor')
        comparison=item.get('price_comparison')
        if not isinstance(comparison,dict) or comparison.get('scope') not in ('в этом ЖК','в этом районе'):
            raise ValueError('Invalid rental comparison')
        minimum=5 if comparison['scope']=='в этом ЖК' else 10
        reference=number(comparison.get('reference_price_per_m2'))
        if (type(comparison.get('comparable_count')) is not int or comparison['comparable_count']<minimum
            or reference is None or reference<=0 or type(comparison.get('discount_pct')) not in (int,float)
            or not MIN_DISCOUNT<=comparison['discount_pct']<=MAX_DISCOUNT
            or abs(comparison['discount_pct']-round(100*(1-item['price']/item['area']/reference),1))>.100001):
            raise ValueError('Rental discount contradicts evidence')
        if abs(item.get('price_per_m2',0)-item['price']/item['area'])>.51:
            raise ValueError('Invalid rental price per sqm')
        if item.get('price_history') is not None:
            history=item['price_history']
            item['price_history']=price_history(history.get('events'),item['price'],now=generated)
        if len(rental_factors(item))!=4:
            raise ValueError('Four positive rental factors required')
        item['reasons']=rental_factors(item)
    return {**snapshot,'methodology':METHODOLOGY}


def load_rental_picks(path=None):
    try:
        path=path or snapshot_path()
        if path.stat().st_size>5_000_000:
            return None
        return validate_snapshot(json.loads(path.read_text(encoding='utf-8')))
    except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError):
        return None


def publish_snapshot(snapshot,path=None):
    safe=validate_snapshot(snapshot)
    path=path or snapshot_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as out:
            temporary=Path(out.name)
            json.dump(safe,out,ensure_ascii=False,allow_nan=False)
            out.flush(); os.fsync(out.fileno())
        os.replace(temporary,path)
    finally:
        if temporary and temporary.exists(): temporary.unlink()


def prepare_rental_picks(snapshot,now=None):
    if snapshot is None:
        return None
    now=utc(now or datetime.now(timezone.utc))
    generated=utc(snapshot['generated_at'])
    # Old inventory must not masquerade as currently available rental stock.
    if not timedelta(0)<=now-generated<=timedelta(hours=24):
        return None
    from bot.core.rental_forecast_runtime import rental_forecast_for_offer
    items=[]
    label=lambda value:utc(value).astimezone(LOCAL_TIMEZONE).strftime('%d.%m.%Y %H:%M')
    for original in snapshot['items']:
        item={**original,'source_checked_at_label':label(original['source_checked_at']),
              'observed_at_label':label(original['observed_at'])}
        forecast_input={**item,'city':'astana','prop_type':'apartment','is_active':True,
            'last_seen':original['observed_at'],'is_duplicate':False,'dup_needs_review':False,
            'rent_period_conflict':False,'archived_at':None,'duplicate_of':None}
        item['rental_forecast']=rental_forecast_for_offer(forecast_input,now=now)
        items.append(item)
    return {**snapshot,'items':items,'date_label':generated.astimezone(LOCAL_TIMEZONE).strftime('%d.%m.%Y'),
            'generated_at_label':label(snapshot['generated_at']),'as_of_label':label(snapshot['source_observed_at']),
            'is_stale':False,'is_today':generated.astimezone(LOCAL_TIMEZONE).date()==now.astimezone(LOCAL_TIMEZONE).date(),
            'status_message':'В каждой карточке указано время проверки исходного объявления.'}
