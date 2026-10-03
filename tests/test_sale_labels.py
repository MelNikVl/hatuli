"""Разметка исходов (задача 2026-10-03): эвристика «та же квартира», константы."""
from pathlib import Path

from bot.core import sale_labels as sl

BASE = {'id': 'a', 'rooms': 2, 'area': 55.0, 'floor': 5, 'resolved_house_id': 10,
        'lat': 51.12, 'lon': 71.43, 'seller_norm': 'асель'}


def _n(**kw):
    return {**BASE, 'id': 'b', **kw}


def test_same_house_same_floor_is_relist():
    assert sl.is_same_flat(BASE, _n())


def test_same_id_is_not_relist():
    assert not sl.is_same_flat(BASE, {**BASE})


def test_different_floor_and_seller_in_big_complex_is_not_relist():
    """Типовые одинаковые квартиры в одном ЖК — не перевыставление."""
    assert not sl.is_same_flat(BASE, _n(floor=7, seller_norm='другой'))


def test_area_tolerance():
    assert sl.is_same_flat(BASE, _n(area=55.9))
    assert not sl.is_same_flat(BASE, _n(area=56.5))


def test_generic_seller_does_not_link():
    old = {**BASE, 'seller_norm': 'хозяин', 'floor': None}
    assert not sl.is_same_flat(old, _n(seller_norm='хозяин', floor=None))


def test_same_seller_without_coords():
    old = {**BASE, 'lat': None, 'lon': None, 'resolved_house_id': None, 'floor': None}
    assert sl.is_same_flat(old, _n(lat=None, lon=None, resolved_house_id=None, floor=None))


def test_near_by_coords_same_floor():
    old = {**BASE, 'resolved_house_id': None}
    assert sl.is_same_flat(old, _n(resolved_house_id=None, lat=51.1205))   # ~55 м
    assert not sl.is_same_flat(old, _n(resolved_house_id=None, lat=51.13, seller_norm='x'))


def test_stable_order_key_deterministic():
    assert sl.stable_order_key('123') == sl.stable_order_key('123') != sl.stable_order_key('124')


def test_labels_match_migration_check():
    sql = (Path(__file__).resolve().parents[1] / 'migrations' / '098_sweep_stale_archive_and_sale_labels.sql').read_text()
    for l in sl.LABELS:
        assert f"'{l}'" in sql
    assert sl.MIN_LABELS_FOR_MODEL >= 300
