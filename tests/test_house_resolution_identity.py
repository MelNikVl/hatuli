"""House identity: address context, ambiguous text, and nearby competing houses."""
import pytest

from bot.core.house_resolution import resolve_house


LAT = 51.12
LON = 71.40


def child(id_, *, address=None, north_m=None, name=None):
    return {
        "id": id_, "name": name or f"Demo {id_}", "address": address,
        "lat": LAT + north_m / 111_195 if north_m is not None else None,
        "lon": LON if north_m is not None else None,
    }


async def resolve(children, *, address=None, title=None, description=None, geo=False):
    return await resolve_house(
        umbrella_id=100, umbrella_name="Demo", children=children,
        listing_address=address, listing_title=title,
        listing_description=description,
        listing_lat=LAT if geo else None, listing_lon=LON if geo else None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("number", ["4", "14/2", "20Б"])
async def test_same_number_on_different_street_is_not_address_match(number):
    result = await resolve(
        [child(1, address=f"Улица Анет баба, {number}")],
        address=f"Улица Туркестан, {number}",
    )
    assert result is None


@pytest.mark.asyncio
async def test_address_keeps_street_and_house_number():
    result = await resolve(
        [child(1, address="Астана, Есильский р-н, ул. Анет баба, 4"),
         child(2, address="Улица Туркестан, 4")],
        address="Анет баба, 4 — рядом с парком",
    )
    assert result["house_id"] == 1
    assert result["method"] == "address"


@pytest.mark.asyncio
async def test_bare_house_number_is_not_enough():
    assert await resolve([child(1, address="Анет баба, 4")], address="4") is None


@pytest.mark.asyncio
async def test_unmatched_address_can_fall_back_to_explicit_block_token():
    result = await resolve(
        [child(1, address="Анет баба, 4"), child(2, address="Туркестан, 6")],
        address="Туркестан, 4", description="Продажа в блоке, корпус 2",
    )
    assert result["house_id"] == 2
    assert result["method"] == "token"


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_multiple_text_tokens_do_not_choose_first_house(reverse):
    children = [child(1, north_m=0), child(2, north_m=80)]
    if reverse:
        children.reverse()
    assert await resolve(children, title="Блок 1, блок 2", geo=True) is None


@pytest.mark.asyncio
async def test_single_explicit_text_token_still_resolves():
    result = await resolve([child(1), child(2)], description="Квартира в корпусе: корпус 2")
    assert result["house_id"] == 2
    assert result["method"] == "token"


@pytest.mark.asyncio
async def test_shared_plot_is_resolved_by_clear_geo_tiebreak():
    result = await resolve(
        [child(1, address="уч. 6", north_m=10),
         child(2, address="уч. 6", north_m=60)],
        address="уч. 6", geo=True,
    )
    assert result["house_id"] == 1
    assert result["method"] == "address_geo"


@pytest.mark.asyncio
async def test_shared_plot_with_close_geo_candidates_stays_unresolved():
    assert await resolve(
        [child(1, address="уч. 6", north_m=10),
         child(2, address="уч. 6", north_m=15)],
        address="уч. 6", geo=True,
    ) is None


@pytest.mark.asyncio
async def test_shared_plot_does_not_escape_to_unrelated_geo_candidate():
    assert await resolve(
        [child(1, address="уч. 6", north_m=200),
         child(2, address="уч. 6", north_m=250),
         child(3, address="уч. 7", north_m=0)],
        address="уч. 6", geo=True,
    ) is None


@pytest.mark.asyncio
async def test_shared_plot_can_use_text_when_geo_is_missing():
    result = await resolve(
        [child(1, address="уч. 6"), child(2, address="уч. 6")],
        address="уч. 6", title="Корпус 2",
    )
    assert result["house_id"] == 2
    assert result["method"] == "token"


@pytest.mark.asyncio
async def test_plot_number_with_different_known_streets_is_not_address_match():
    assert await resolve(
        [child(1, address="ул. Анет баба, уч. 6")],
        address="ул. Туркестан, уч. 6",
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("distances", [(10, 20), (10, 10), (149, 151)])
async def test_nearby_geo_competitor_prevents_attribution(distances):
    children = [child(1, north_m=distances[0]), child(2, north_m=distances[1])]
    assert await resolve(children, geo=True) is None
    assert await resolve(list(reversed(children)), geo=True) is None


@pytest.mark.asyncio
async def test_geo_still_resolves_with_clear_distance_margin():
    result = await resolve([child(1, north_m=10), child(2, north_m=40)], geo=True)
    assert result["house_id"] == 1
    assert result["method"] == "geo"


@pytest.mark.asyncio
async def test_only_house_outside_geo_radius_is_not_resolved():
    assert await resolve([child(1, north_m=151)], geo=True) is None
