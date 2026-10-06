"""Location anchors for buyers using existing canonical ЖК and geocoding.

A ЖК is an anchor with an explicit radius, not an implicit administrative
area or a filter for listings only inside that ЖК. No second address index.
"""
from bot.db import pg
from bot.core.entity_resolution import search_complexes_for_parent
from bot.core.house_resolution import resolve_complex_geo_centroid
from bot.core.geo import geocode, in_astana_bbox


async def search_locations(query: str, kind: str) -> list[dict]:
    query = ' '.join(query.split())
    if not 2 <= len(query) <= 150:
        return []
    if kind == 'address':
        point = await geocode(query, city='astana')
        if not point or not in_astana_bbox(*point):
            return []
        return [{'label': query, 'lat': point[0], 'lon': point[1]}]
    if kind != 'complex':
        return []
    matches = await search_complexes_for_parent(query, limit=12)
    if not matches:
        return []
    rows = await pg.fetch('''SELECT DISTINCT c.id,c.name,c.address,c.lat,c.lon
        FROM complexes alias JOIN complexes c ON c.id=COALESCE(alias.canonical_id,alias.id)
        WHERE alias.id=ANY($1::int[]) AND COALESCE(c.is_garbage,FALSE)=FALSE
        AND COALESCE(c.canonical_reason,'') != 'junk_unmatched'
        ORDER BY c.name LIMIT 6''', [m['id'] for m in matches])
    result = []
    for row in rows:
        point = ((float(row['lat']), float(row['lon']))
                 if in_astana_bbox(row.get('lat'), row.get('lon'))
                 else await resolve_complex_geo_centroid(row['id'], row['name']))
        if point and in_astana_bbox(*point):
            label = 'ЖК ' + row['name']
            if row.get('address'):
                label += ' · ' + row['address']
            result.append({'label': label, 'lat': point[0], 'lon': point[1]})
    return result
