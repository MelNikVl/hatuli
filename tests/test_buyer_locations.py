from unittest.mock import AsyncMock, patch

import pytest
from bot.core import buyer_locations as locations


@pytest.mark.asyncio
async def test_address_reuses_geocoder_with_astana_scope():
    with patch.object(locations, 'geocode', AsyncMock(return_value=(51.13, 71.43))) as geocode:
        result = await locations.search_locations('Кабанбай батыра 58', 'address')
    geocode.assert_awaited_once_with('Кабанбай батыра 58', city='astana')
    assert result == [{'label': 'Кабанбай батыра 58', 'lat': 51.13, 'lon': 71.43}]


@pytest.mark.asyncio
async def test_geocoder_result_outside_astana_is_rejected():
    with patch.object(locations, 'geocode', AsyncMock(return_value=(43.2, 76.9))):
        assert await locations.search_locations('Абая 1', 'address') == []


@pytest.mark.asyncio
async def test_complex_uses_canonical_id_and_existing_centroid():
    with patch.object(locations, 'search_complexes_for_parent', AsyncMock(return_value=[{'id': 12}, {'id': 13}])), \
         patch.object(locations.pg, 'fetch', AsyncMock(return_value=[{'id': 9, 'name': 'Highvill', 'address': 'Тест 1'}])) as fetch, \
         patch.object(locations, 'resolve_complex_geo_centroid', AsyncMock(return_value=(51.13, 71.43))) as centroid:
        result = await locations.search_locations('Highvill', 'complex')
    assert len(result) == 1
    assert 'COALESCE(alias.canonical_id,alias.id)' in fetch.await_args.args[0]
    centroid.assert_awaited_once_with(9, 'Highvill')
