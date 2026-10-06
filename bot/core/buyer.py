"""Small, explainable buyer decision layer over Clearly's existing analytics.

Scores remain Deal Score (0..100), never a new ML model. Alternative ranking
uses only pairwise known values, reports tradeoffs, and requires a material
price/area/Deal Score gain. Missing values contribute zero, not a penalty.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import urlsplit

from bot.ai_tools.core import resolve_listing, _extract_listing_id_from_input
from bot.analytics.dom_scenario import compute_dom_scenario_cached
from bot.core.listing_detail import build_listing_detail, build_price_history, ListingNotFound
from bot.core.listing_intel import detect_finish_level
from bot.core.geo import haversine_km, in_astana_bbox
from bot.core.hexgrid import hex_id
from bot.core.buyer_store import get_profile
from bot.db import pg

log = logging.getLogger(__name__)
_FETCH_LOCK = asyncio.Lock()
_next_fetch = 0.0
_SEVERITY = {'unknown': 0, 'info': 0, 'low': 1, 'medium': 2, 'high': 3, 'critical': 4}
VERDICTS = {
    'fast': '🔥 ЗВОНИТЬ / СМОТРЕТЬ БЫСТРО',
    'view': '🟢 СТОИТ ПОСМОТРЕТЬ',
    'negotiate': '🟡 ТОЛЬКО ЕСЛИ СТОРГУЕТЕСЬ',
    'skip': '🔴 МОЖНО ПРОПУСТИТЬ',
    'unknown': '⚪ НЕДОСТАТОЧНО ДАННЫХ ДЛЯ ВЕРДИКТА',
}


def extract_krisha_url(text: str) -> str | None:
    """Validate host/path before using the existing ID parser. Strip trackers."""
    for token in re.findall(r'(?:https?://|www\.)[^\s<>"«»]+', text or '', re.I):
        token = token.rstrip('.,;!?)]}')
        try:
            parsed = urlsplit(token if '://' in token else 'https://' + token)
        except ValueError:
            continue
        if (parsed.hostname or '').lower() not in ('krisha.kz', 'www.krisha.kz'):
            continue
        if parsed.username or parsed.password or parsed.netloc.lower() not in ('krisha.kz', 'www.krisha.kz'):
            continue
        if not re.fullmatch(r'/a/show/\d{5,20}/?', parsed.path):
            continue
        lid = _extract_listing_id_from_input(parsed.path)
        return f'https://krisha.kz/a/show/{lid}'
    return None


def money(value: float | None) -> str:
    return f'{float(value)/1e6:.1f} млн ₸' if value is not None else 'не указана'


async def import_listing(lid: str) -> bool:
    """One bounded fetch via existing parser, never a city crawl or retry loop.

    Fail closed on unknown scope/price, archive or layout advertisements. The
    normal collector later enriches/scorers this minimal row. ON CONFLICT
    cannot overwrite a concurrently refreshed row or its price history.
    """
    global _next_fetch
    from bot.core.apartment_details import fetch_apartment_details
    if _FETCH_LOCK.locked() or time.monotonic() < _next_fetch:
        return False
    async with _FETCH_LOCK:
        _next_fetch = time.monotonic() + 15
        url = f'https://krisha.kz/a/show/{lid}'
        try:
            d = await asyncio.wait_for(fetch_apartment_details(url), timeout=38)
        except Exception as exc:
            log.warning('Single listing import failed: %s', type(exc).__name__)
            return False
        identity = d.get('listing_identity') or {}
        price, rooms = identity.get('price'), identity.get('rooms')
        area = identity.get('area') or d.get('area_total')
        if (not identity.get('sale_astana') or not price or price <= 0 or not rooms
                or not area or d.get('is_archived') or d.get('is_flat_layout') or d.get('is_price_from')):
            return False
        await pg.execute('''INSERT INTO apartment_listings
            (id,url,title,price,rooms,area,address,description,lat,lon,floor,floors_total,
             ceiling_height,renovation,year_built,complex_name,is_owner,details_fetched,is_active)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,TRUE,TRUE)
            ON CONFLICT (id) DO NOTHING''', lid, url, d.get('title_full'), price, rooms, area,
            d.get('address_full'), d.get('description'), d.get('lat'), d.get('lon'),
            d.get('floor'), d.get('floors_total'), d.get('ceiling_height'), d.get('renovation'),
            d.get('year_built'), d.get('complex_name'), d.get('is_owner'))
        return True


def profile_mismatches(d: dict, profile: dict) -> list[str]:
    reasons = []
    if profile.get('budget_max') and d.get('price') and d['price'] > profile['budget_max']:
        reasons.append(f"Выше вашего бюджета на {money(d['price']-profile['budget_max'])}")
    rooms = profile.get('rooms') or []
    n = d.get('rooms')
    if rooms and n and n not in rooms and not (4 in rooms and n >= 4):
        reasons.append(f'{n}-комнатная квартира — не подходит под выбранную комнатность')
    if profile.get('area_min') and d.get('area') and d['area'] < profile['area_min']:
        reasons.append(f"Площадь меньше выбранных {profile['area_min']:g} м²")
    kind = {'new': 'primary', 'secondary': 'secondary'}.get(profile.get('property_type'))
    if kind and d.get('market') and d['market'] != kind:
        reasons.append('Не соответствует выбранному типу рынка')
    if has_location(profile) and d.get('lat') is not None and d.get('lon') is not None:
        area = profile.get('buyer_area') or {}
        if area.get('hex_ids'):
            if hex_id(d['lat'], d['lon'], area['edge_m']) not in area['hex_ids']:
                reasons.append('Квартира вне выбранных на карте участков')
        else:
            distance = haversine_km(profile['location_lat'], profile['location_lon'], d['lat'], d['lon'])
            if distance > profile['radius_km']:
                reasons.append(f"От выбранной точки {distance:.1f} км — дальше вашего радиуса {profile['radius_km']} км")
    return reasons


def has_location(profile: dict) -> bool:
    return bool((profile.get('buyer_area') or {}).get('hex_ids')) or (profile.get('location_lat') is not None and profile.get('location_lon') is not None
            and bool(profile.get('radius_km')))


def risk_text(item: dict) -> str:
    if item.get('code') != 'LONGER_THAN_EXPECTED':
        return item['title']
    evidence = item.get('exposure') or {}
    days, low, high = (evidence.get(k) for k in ('observed_days', 'expected_days_low', 'expected_days_high'))
    if days is None or high is None:
        # Older cached risk records still carry the numbers in the description.
        return item.get('description') or 'Срок наблюдения требует уточнения'
    expected = f'{low:g}–{high:g}' if low is not None else f'до {high:g}'
    text = f'Наблюдаем {days:g} дн.; аналоги ~{expected} дн. — дольше верхнего ориентира на {days-high:g} дн.'
    if evidence.get('confidence') == 'low':
        text += ' Оценка аналогов ненадёжна.'
    return text


def summarize_price_history(history: dict) -> dict:
    events = [e for e in history.get('events', [])
              if e.get('old_price') is not None and e.get('new_price') is not None
              and e['old_price'] != e['new_price']]
    return {'available': history.get('available', True), 'events': events, 'changes': len(events),
            'net_change': events[-1]['new_price'] - events[0]['old_price'] if events else None}


def price_summary(d: dict) -> dict:
    b = d.get('bargain') or {}
    reliable = bool(b.get('median_price') and (b.get('comparables_cnt') or 0) >= 5
                    and b.get('method') not in ('city', 'city_fallback', 'city_segment', 'district_fallback'))
    return {'asking': d.get('price'), 'fair': b.get('median_price') if reliable else None,
            'offer': b.get('target_price') if reliable else None,
            'sample_size': b.get('comparables_cnt') or 0, 'reliable': reliable}


def urgency(d: dict, price: dict) -> dict:
    dom = d.get('dom_scenario') or {}
    current = dom.get('current') or {}
    reliable = dom.get('available') and not dom.get('insufficient_data') and dom.get('confidence') in ('medium', 'high')
    discounted = price['fair'] and price['asking'] and price['asking'] < price['fair'] * .95
    if reliable and discounted and 0 < (current.get('days_high') or 9999) <= 30:
        return {'level': 'high', 'text': 'Лучше не затягивать: цена ниже аналогов, сегмент быстро уходит с публикации.'}
    if reliable and (current.get('days_low') or 0) >= 60:
        return {'level': 'low', 'text': 'Можно не спешить: у сегмента длительная экспозиция.'}
    return {'level': 'normal' if reliable else 'unknown',
            'text': 'Обычная срочность.' if reliable else 'Срочность неизвестна: мало надёжных данных.'}


def _finish(d: dict) -> tuple[int | None, str | None]:
    ranks = {'rough': 0, 'none': 0, 'needs_repair': 0, 'prefinish': 1, 'finish': 2,
             'finished': 2, 'cosmetic': 2, 'good': 3, 'fresh': 3, 'euro': 3,
             'renovated': 3, 'designer': 4, 'furnished': 3}
    code = d.get('finish_level') or d.get('renovation')
    if not code:
        code, _, _ = detect_finish_level(d.get('description'))
    return ranks.get(code), code


def _floor(d: dict) -> int | None:
    f, total = d.get('floor'), d.get('floors_total')
    if f == 1:
        return 0
    if f and total and 1 < f <= total:
        return 1 if f == total else 2
    return None


def _deal(d: dict) -> float | None:
    score = d.get('deal_score') or {}
    return score.get('deal') if (score.get('confidence') or 0) >= 50 else None


def _location(d: dict) -> float | None:
    # Use the same measured location snapshot as the existing location API.
    # Deal Score's location component partly reflects price, not amenity quality.
    score = d.get('location_score') or {}
    return score.get('score') if (score.get('confidence') or 0) >= 50 else None


def summarize(d: dict, profile: dict | None = None) -> dict:
    profile = profile or {}
    price = price_summary(d)
    plus, minus, warnings = [], [], []
    score = _deal(d)
    if has_location(profile) and (d.get('lat') is None or d.get('lon') is None):
        warnings.append('Нет координат квартиры — соответствие вашей локации не проверено.')
    if price['fair'] and price['asking']:
        ratio = price['asking'] / price['fair']
        if ratio <= .95:
            plus.append(f'Цена примерно на {(1-ratio)*100:.0f}% ниже аналогов')
        elif ratio > 1.07:
            minus.append(f'Цена примерно на {(ratio-1)*100:.0f}% выше аналогов')
        else:
            plus.append('Цена близка к медиане аналогов')
    else:
        warnings.append('Недостаточно хороших аналогов, оценка цены ненадёжна.')
    loc = _location(d)
    if loc is not None and loc >= 75:
        plus.append('Высокая оценка локации ЖК')
    elif loc is not None and loc < 35:
        minus.append('Низкая оценка локации ЖК')
    floor = _floor(d)
    if floor == 2:
        plus.append(f"Средний этаж: {d['floor']}/{d['floors_total']}")
    elif floor == 0:
        minus.append('Первый этаж — проверьте приватность и сырость')
    elif floor == 1:
        minus.append('Последний этаж — проверьте крышу и техэтаж')
    if d.get('ceiling_height') and d['ceiling_height'] >= 3:
        plus.append(f"Потолки {d['ceiling_height']:g} м")
    finish, _ = _finish(d)
    if finish is not None:
        (plus if finish >= 2 else minus).append('Заявлена готовая отделка' if finish >= 2 else 'Потребуются расходы на ремонт')
    risk = d.get('risk_analysis') or {}
    items = risk.get('items') or []
    significant = [r for r in items if _SEVERITY.get(r.get('severity'), 0) >= 2]
    mismatch = profile_mismatches(d, profile)
    minus = mismatch + [risk_text(r) for r in significant] + minus
    if d.get('is_active') is False:
        minus.insert(0, 'Объявление снято с публикации')
    if risk.get('overall_level') in (None, 'unknown'):
        warnings.append('Риски пока не рассчитаны.')
    if score is None:
        warnings.append('Deal Score пока недостаточно надёжен.')
    urgent = urgency(d, price)
    severe = _SEVERITY.get(risk.get('overall_level'), 0) >= 3
    if d.get('is_active') is False or severe or mismatch:
        code = 'skip'
    elif price['fair'] and price['asking'] and price['asking'] > price['fair'] * 1.07:
        code = 'negotiate'
    elif score is not None and score < 40:
        code = 'skip'
        minus.append('Низкий Deal Score по имеющимся рыночным данным')
    elif not price['reliable'] and score is None:
        code = 'unknown'
    elif urgent['level'] == 'high' and score is not None and score >= 75 and not significant:
        code = 'fast'
    else:
        code = 'view'
    checks = [r['recommendation'] for r in items if r.get('recommendation')]
    if floor == 0:
        checks.append('Проверьте сырость, запах из подвала и приватность окон первого этажа.')
    elif floor == 1:
        checks.append('Осмотрите потолок на протечки и уточните состояние крыши/техэтажа.')
    if finish is not None and finish < 2:
        checks.append('Оцените объём и стоимость ремонта на месте.')
    # listing_intel's broad baseline checklist is deliberately not copied.
    checks = list(dict.fromkeys(checks))[:5]
    reasons = list(dict.fromkeys((minus if code in ('skip', 'negotiate') else plus) + warnings))[:3]
    facts = [f"Площадь {d['area']:g} м²" if d.get('area') else None,
             f"Цена продавца {money(d['price'])}" if d.get('price') else None]
    for fact in facts:
        if len(reasons) < 2 and fact:
            reasons.append(fact)
    return {'listing': d, 'verdict': {'code': code, 'label': VERDICTS[code], 'reasons': reasons},
            'score': score, 'positives': list(dict.fromkeys(plus))[:3],
            'negatives': list(dict.fromkeys(minus))[:3], 'price_summary': price,
            'price_history': summarize_price_history(d.get('price_history') or {}),
            'location_unverified': has_location(profile) and (d.get('lat') is None or d.get('lon') is None),
            'urgency': urgent, 'risks': checks, 'better_nearby': [],
            'confidence': 'sufficient' if price['reliable'] and score is not None else 'limited',
            'warnings': warnings}


def compare_alternative(base: dict, candidate: dict, profile: dict | None = None) -> dict | None:
    """Pairwise benefit points: 1 per price %, 0.7 per area %, and capped
    secondary signals. >=3 points + material gain; no presumed benefit for
    unknowns. This is a transparent prioritisation, not a probability.
    """
    if candidate.get('is_active') is not True or candidate.get('is_duplicate'):
        return None
    if str(candidate.get('id')) == str(base.get('id')):
        return None
    if base.get('property_id') and base['property_id'] == candidate.get('property_id'):
        return None
    if any(d.get(k) is None for d in (base, candidate) for k in ('lat', 'lon', 'price', 'area')):
        return None
    if min(base['price'], candidate['price'], base['area'], candidate['area']) <= 0:
        return None
    distance = haversine_km(base['lat'], base['lon'], candidate['lat'], candidate['lon']) * 1000
    if distance > 500 or profile_mismatches(candidate, profile or {}):
        return None
    if base.get('market') and candidate.get('market') and base['market'] != candidate['market']:
        return None
    if base.get('rooms') and candidate.get('rooms') and base['rooms'] != candidate['rooms'] and not (profile or {}).get('rooms'):
        return None
    risk = candidate.get('risk_analysis') or {}
    if _SEVERITY.get(risk.get('overall_level'), 0) >= 3:
        return None
    dp = (base['price'] - candidate['price']) / base['price'] * 100
    da = (candidate['area'] - base['area']) / base['area'] * 100
    # Keep the generic alternative financially comparable.
    if dp < -5 or da < -10:
        return None
    value = max(-10, min(10, dp)) + .7 * max(-10, min(10, da))
    advantages, tradeoffs = [], []
    if abs(dp) >= .5:
        (advantages if dp > 0 else tradeoffs).append(
            f"{'Дешевле' if dp > 0 else 'Дороже'} на {money(abs(base['price']-candidate['price']))}")
    if abs(candidate['area'] - base['area']) >= 1:
        (advantages if da > 0 else tradeoffs).append(
            f"{'Больше' if da > 0 else 'Меньше'} на {abs(candidate['area']-base['area']):g} м²")
    material = dp >= 2 or (candidate['area'] - base['area'] >= 2 and dp >= -1)
    pairs = [(_floor(base), _floor(candidate), 2, 'Этаж удобнее', 'Этаж менее удобный'),
             (_finish(base)[0], _finish(candidate)[0], 1.5, 'Лучше заявленная отделка', 'Отделка слабее'),
             (base.get('ceiling_height'), candidate.get('ceiling_height'), 3, 'Потолки выше', 'Потолки ниже'),
             (_deal(base), _deal(candidate), .12, 'Выше Deal Score', 'Ниже Deal Score'),
             (_location(base), _location(candidate), .08, 'Выше оценка локации', 'Ниже оценка локации')]
    for a, b, weight, good, bad in pairs:
        if a is None or b is None or abs(b-a) < .01:
            continue
        delta = max(-3, min(3, (b-a)*weight))
        value += delta
        (advantages if delta > 0 else tradeoffs).append(good if delta > 0 else bad)
    a, b = _deal(base), _deal(candidate)
    material = material or (a is not None and b is not None and b-a >= 8 and dp >= -1)
    p1, p2 = price_summary(base), price_summary(candidate)
    if p1['fair'] and p2['fair']:
        delta = (base['price']/p1['fair'] - candidate['price']/p2['fair']) * 30
        value += max(-3, min(3, delta))
        if abs(delta) >= 1:
            (advantages if delta > 0 else tradeoffs).append('Выгоднее относительно аналогов' if delta > 0 else 'Менее выгодна относительно аналогов')
    r1, r2 = (base.get('risk_analysis') or {}).get('overall_level'), risk.get('overall_level')
    if r1 in _SEVERITY and r2 in _SEVERITY and 'unknown' not in (r1, r2):
        delta = _SEVERITY[r1] - _SEVERITY[r2]
        value += 2 * delta
        if delta:
            (advantages if delta > 0 else tradeoffs).append('Ниже выявленные риски' if delta > 0 else 'Выше выявленные риски')
    if not material or value < 3 or not advantages:
        return None
    unknown = any(a is None or b is None for a, b, *_ in pairs) or r2 in (None, 'unknown')
    return {'listing': candidate, 'distance_m': int(round(distance / 10) * 10),
            'advantages': advantages, 'tradeoffs': tradeoffs,
            'warnings': ['Сравнение неполное: часть характеристик неизвестна.'] if unknown else [],
            'rank_points': round(value, 2), 'method': 'buyer_pairwise_v1',
            'urgency': urgency(candidate, p2)}


def rank_alternatives(base: dict, candidates: list[dict], profile: dict | None = None) -> list[dict]:
    ranked = [r for c in candidates if (r := compare_alternative(base, c, profile))]
    ranked.sort(key=lambda r: (-r['rank_points'], r['urgency']['level'] != 'high',
                               r['distance_m'], str(r['listing']['id'])))
    result, seen = [], set()
    for r in ranked:
        d = r['listing']
        key = ('property', d['property_id']) if d.get('property_id') else ('listing', d['id'])
        if key not in seen:
            result.append(r)
            seen.add(key)
        if len(result) == 3:
            break
    return result


async def _detail(lid: str, *, similar_limit: int = 0) -> dict:
    detail = await build_listing_detail(lid, tier='admin', similar_limit=similar_limit)
    raw = await pg.fetchrow('''SELECT a.is_active,a.is_duplicate,a.market_type,a.renovation,
        a.finish_level,COALESCE(cx.canonical_id,cx.id) AS complex_id,pl.property_id
        FROM apartment_listings a LEFT JOIN property_listings pl ON pl.listing_id=a.id
        LEFT JOIN complexes cx ON cx.id=a.complex_id WHERE a.id=$1''', lid)
    if raw:
        detail.update(dict(raw))
        detail['market'] = raw['market_type']  # do not turn unknown into secondary
    if detail.get('complex_id'):
        from bot.core.complex_location_detail import _build_score
        try:
            detail['location_score'] = await _build_score(detail['complex_id'])
        except Exception:
            detail['location_score'] = None
    try:
        detail['price_history'] = await build_price_history(lid)
    except Exception:
        log.warning('Price history unavailable for %s', lid, exc_info=True)
        detail['price_history'] = {'available': False}
    return detail


async def analyze_for_buyer(listing_url_or_id: str, user_id: int | None = None) -> dict:
    value = str(listing_url_or_id).strip()
    url = extract_krisha_url(value)
    if not url and not re.fullmatch(r'\d{5,20}', value):
        return {'found': False, 'message': 'Пришлите ссылку вида https://krisha.kz/a/show/…'}
    resolved = await resolve_listing(url or value)
    lid = resolved.data.get('listing_id')
    if not resolved.data.get('found'):
        if not lid or not await import_listing(lid):
            return {'found': False, 'message': 'Объявления нет в базе, а загрузить его не удалось. Возможно, оно снято или Krisha временно недоступна. Попробуйте позже. Работаем с продажей квартир в Астане.'}
    try:
        d = await _detail(lid, similar_limit=40)
    except ListingNotFound:
        return {'found': False, 'message': 'Объявление больше не доступно в базе.'}
    if d.get('lat') is not None and d.get('lon') is not None and not in_astana_bbox(d['lat'], d['lon']):
        return {'found': False, 'message': 'Пока анализируем только покупку квартир в Астане.'}
    try:
        d['dom_scenario'] = await asyncio.wait_for(compute_dom_scenario_cached(lid), timeout=5)
    except Exception:
        d['dom_scenario'] = {'available': False}
    profile = await get_profile(user_id) if user_id is not None else {}
    result = summarize(d, profile)
    result['found'] = True
    # Same candidate search as listing detail; cheap pairwise screening before
    # costly risk/comparables analytics. Bounded work for a public bot.
    candidates = d.get('similar') or []
    candidates.sort(key=lambda c: ((c.get('price') or float('inf')) / max(c.get('area') or 1, 1)))
    sem = asyncio.Semaphore(3)
    async def load(c):
        async with sem:
            try:
                candidate = await asyncio.wait_for(_detail(str(c['id'])), timeout=8)
                try:
                    candidate['dom_scenario'] = await asyncio.wait_for(
                        compute_dom_scenario_cached(str(c['id'])), timeout=2)
                except Exception:
                    candidate['dom_scenario'] = {'available': False}
                return candidate
            except Exception:
                return None
    loaded = await asyncio.gather(*(load(c) for c in candidates[:9]))
    result['better_nearby'] = rank_alternatives(d, [c for c in loaded if c], profile)
    if candidates:
        result['warnings'].append('Сравнение по доступной выборке рядом; актуальность проверяйте на Krisha.')
    if any(c is None for c in loaded):
        result['warnings'].append('Часть соседних объявлений не удалось проверить.')
    return result
