"""The acceleration must not remove neighbours at spatial cell boundaries."""
import math

import pytest

from bot.core import daily_picks
from bot.core.daily_picks import _nearby, _nearby_cell
from tests.test_daily_picks import NOW, row


def destination(centre, metres, bearing):
    """Independent spherical destination, with distance specified in metres."""
    lat, lon = math.radians(centre["lat"]), math.radians(centre["lon"])
    angle, azimuth = metres/6_371_000, math.radians(bearing)
    target_lat = math.asin(math.sin(lat)*math.cos(angle)+math.cos(lat)*math.sin(angle)*math.cos(azimuth))
    target_lon = lon+math.atan2(math.sin(azimuth)*math.sin(angle)*math.cos(lat),
                               math.cos(angle)-math.sin(lat)*math.sin(target_lat))
    return {"lat": math.degrees(target_lat), "lon": math.degrees(target_lon)}


@pytest.mark.parametrize("lat,lon", [(50.86, 71.115), (51.0, 71.4), (51.39, 71.835)])
def test_every_point_within_radius_is_in_one_of_nine_cells(lat, lon):
    centre = {"lat": lat - 1e-7, "lon": lon - 1e-7}
    cell = _nearby_cell(centre)
    for bearing in range(0, 360, 5):
        for metres in (1, 400, 799.9, 800):
            angle = math.radians(bearing)
            peer = {"lat": centre["lat"] + metres * math.cos(angle) / 111_195,
                    "lon": centre["lon"] + metres * math.sin(angle) / (111_195 * math.cos(math.radians(lat)))}
            if _nearby(centre, peer):
                other = _nearby_cell(peer)
                assert other is not None
                assert abs(cell[0] - other[0]) <= 1 and abs(cell[1] - other[1]) <= 1


@pytest.mark.parametrize("point", [{}, {"lat": True, "lon": 71.4},
    {"lat": float("nan"), "lon": 71.4}, {"lat": 52, "lon": 71.4}])
def test_invalid_points_have_no_spatial_cell(point):
    assert _nearby_cell(point) is None


@pytest.mark.parametrize("bearing", [0, 45, 90, 135, 180, 225, 270, 315])
def test_exact_radius_does_not_expand_to_the_surrounding_grid_cells(bearing):
    centre = {"lat": 51.105, "lon": 71.4075}
    inside, outside = destination(centre, 799.9, bearing), destination(centre, 800.1, bearing)
    cell, other = _nearby_cell(centre), _nearby_cell(outside)
    assert abs(cell[0]-other[0]) <= 1 and abs(cell[1]-other[1]) <= 1
    assert _nearby(centre, inside)
    assert not _nearby(centre, outside)


def test_builder_spatial_index_keeps_full_scan_result_and_excludes_801m_price_outlier(monkeypatch):
    centre = {"lat": 51.105, "lon": 71.4075}
    candidate = row(0, price=24_000_000, complex_id=None, complex_resolution=None, **centre)
    peers = [row(index, price=price, complex_id=None, complex_resolution=None,
                 approved_photo_urls=[], **destination(centre, 799.9, 72*index))
             for index, price in enumerate((30_000_000, 30_000_000, 30_000_000, 36_000_000, 36_000_000), 1)]
    outsider = row(10, price=90_000_000, complex_id=None, complex_resolution=None,
                   approved_photo_urls=[], **destination(centre, 801, 90))
    rows = [candidate, *peers, outsider]
    indexed = daily_picks.build_daily_picks(rows, NOW)
    assert len(indexed["items"]) == 1
    comparison = indexed["items"][0]["price_comparison"]
    assert comparison["comparable_count"] == 5
    assert comparison["reference_price_per_m2"] == 500_000
    assert comparison["scope"] == "в радиусе 800 м по приблизительным координатам"
    # Putting every point in one cell creates the old full-scan candidate
    # population. The final exact distance check must give the same edition.
    monkeypatch.setattr(daily_picks, "_nearby_cell", lambda point: (0, 0))
    assert daily_picks.build_daily_picks(rows, NOW) == indexed
