"""An existing analytics tab; aggregate data only, no source contacts or IDs."""
from __future__ import annotations

from datetime import datetime,timezone

import asyncpg
from fastapi import APIRouter,Query,Request
from fastapi.responses import JSONResponse

from bot.core.rental_stage2_index import index_ready,DAILY_POLICY,HEX_MONTHLY_POLICY

HEADERS={'Cache-Control':'no-store','X-Robots-Tag':'noindex, nofollow'}


def _date(value):
    return value.strftime('%d.%m.%Y %H:%M UTC') if isinstance(value,datetime) else ''


def segment_table(rows,now,minimum=10):
    grouped={}
    for raw in rows:
        row=dict(raw)
        period=row.get('rent_period')
        policy=DAILY_POLICY if period=='daily' else '3'
        if (row.get('district') is None or row.get('complex_id') is not None or row.get('furnishing') is not None
                or not index_ready(row,now,minimum,period,policy=policy)):
            continue
        key=(row['district'],row['rooms'])
        row['updated_at_label']=_date(row['updated_at'])
        row['source_newest_label']=_date(row['source_newest_at'])
        row['source_range_label']=row['source_oldest_at'].strftime('%d.%m')+'–'+row['source_newest_at'].strftime('%d.%m')
        grouped.setdefault(key,{'district':key[0],'rooms':key[1]})[period]=row
    result=[]
    for key,item in sorted(grouped.items()):
        monthly,daily=item.get('monthly'),item.get('daily')
        item['price_ratio_pct']=round(100*daily['median_price']/monthly['median_price'],1) if monthly and daily else None
        if monthly and type(monthly.get('gone_30d')) is int and monthly['gone_30d']>=minimum:
            item['removal_days']=monthly.get('median_dom_days')
            item['gone_30d']=monthly['gone_30d']
        result.append(item)
    return result


def hex_feature_collection(rows,now,*,period,rooms,edge_m,minimum=5):
    from bot.core.hexgrid import hex_corners
    features=[]
    policy=DAILY_POLICY if period=='daily' else HEX_MONTHLY_POLICY
    for raw in rows:
        row=dict(raw)
        if (row.get('rent_period')!=period or row.get('rooms')!=rooms or row.get('hex_edge_m')!=edge_m
                or row.get('furnishing') is not None or not index_ready(row,now,minimum,period,policy=policy)):
            continue
        try:
            corners=[[lon,lat] for lat,lon in hex_corners(row['hex_id'],edge_m)]
        except (ValueError,TypeError,KeyError,OverflowError):
            continue
        properties={key:row[key] for key in ('hex_id','hex_edge_m','rooms','rent_period','median_price',
                    'p25_price','p75_price','sample_count','active_supply','window_days')}
        properties.update(source_newest_at=_date(row['source_newest_at']),source_oldest_at=_date(row['source_oldest_at']),updated_at=_date(row['updated_at']))
        features.append({'type':'Feature','geometry':{'type':'Polygon','coordinates':[corners+[corners[0]]]},'properties':properties})
    return {'type':'FeatureCollection','features':features}


async def rental_analytics_data(rooms=2):
    from bot.db.pg import fetch
    from bot.db import settings
    from bot.core.rental_parser import _rental_index_thresholds
    now=datetime.now(timezone.utc)
    try:
        edge=float(settings.get('HEX_EDGE_M','50'))
        minimum_hex=max(5,int(settings.get('RENT_MIN_SAMPLE_HEX','5')))
    except (ValueError,TypeError,OverflowError):
        edge,minimum_hex=50.,5
    _,minimum_area=_rental_index_thresholds()
    try:
        rows=await fetch("""SELECT i.* FROM rental_index i WHERE i.city='astana' AND i.district IS NOT NULL
          AND i.complex_id IS NULL AND i.complex_name IS NULL AND i.rooms BETWEEN 1 AND 6
          AND i.prop_type='apartment' AND i.rent_period IN ('monthly','daily') AND i.furnishing IS NULL""")
        hex_rows=await fetch("SELECT h.* FROM rental_hex_index h WHERE h.city='astana' AND h.rooms=$1 "
                             "AND h.hex_edge_m=$2 AND h.furnishing IS NULL",rooms,edge)
    except (asyncpg.UndefinedTableError,asyncpg.UndefinedColumnError):
        return {'segments':[],'hex_layers':{},'rooms':rooms,'minimum_area':minimum_area,'minimum_hex':minimum_hex,
                'migration_ready':False,'edge_m':edge}
    segments=segment_table(rows,now,minimum_area)
    layers={period:hex_feature_collection(hex_rows,now,period=period,rooms=rooms,edge_m=edge,minimum=minimum_hex)
            for period in ('monthly','daily')}
    layers={key:value for key,value in layers.items() if value['features']}
    return {'segments':segments,'hex_layers':layers,'rooms':rooms,'minimum_area':minimum_area,
            'minimum_hex':minimum_hex,'migration_ready':True,'edge_m':edge,
            'has_monthly':any('monthly' in r for r in segments),'has_daily':any('daily' in r for r in segments),
            'has_ratio':any(r['price_ratio_pct'] is not None for r in segments),
            'has_removal':any(r.get('removal_days') is not None for r in segments)}


async def render_rental_analytics(request,templates,rooms):
    try:
        selected=int(rooms or 2)
    except (ValueError,TypeError):
        selected=2
    selected=selected if 1<=selected<=6 else 2
    context=await rental_analytics_data(selected)
    return templates.TemplateResponse('rental_analytics.html',{'request':request,'atab':'rental_market',**context},headers=HEADERS)


def make_rental_analytics_router(is_authed):
    router=APIRouter()
    @router.get('/admin/api/rental-market')
    async def rental_market(request:Request,rooms:int=Query(2,ge=1,le=6)):
        if not is_authed(request):
            return JSONResponse({'error':'Требуется вход администратора'},status_code=401,headers=HEADERS)
        data=await rental_analytics_data(rooms)
        # Datetimes remain private inside the projection, not JSON records.
        return JSONResponse({'hex_layers':data['hex_layers'],'rooms':rooms,'minimum_hex':data['minimum_hex'],
                             'migration_ready':data['migration_ready']},headers=HEADERS)
    return router
