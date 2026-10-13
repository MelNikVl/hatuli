from bs4 import BeautifulSoup
import pytest
from bot.core.rental_parser import _extract_area, _extract_rooms, _parse_card


@pytest.mark.parametrize('text,area', [('Офис, 2 500 м²',2500),('1 234,5 м2',1234.5),
    ('Квартира 60,5 м²',60.5),('Квартира 1250м²',1250),('Офис 2\u00a0500 м²',2500)])
def test_area_keeps_thousands_and_decimal_part(text,area):
    assert _extract_area(text)==area


def test_multi_digit_rooms_are_not_a_studio():
    assert _extract_rooms('10-комнатная квартира')==10
    assert _extract_rooms('студия')==0


def test_floor_is_read_from_source_title_when_description_does_not_repeat_it():
    html = '<div class="a-card" data-id="12345678"><a class="a-card__title" href="/a/show/12345678">Офис, 2 500 м², 3/7 этаж</a><div class="a-card__price">5 000 000 ₸</div></div>'
    listing = _parse_card(BeautifulSoup(html,'html.parser').div,'commercial')
    assert (listing.area,listing.floor,listing.floors_total,listing.price)==(2500,3,7,5000000)
