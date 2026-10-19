"""Missing description DOM is different from an observed empty description."""
from unittest.mock import AsyncMock
import httpx
import pytest
from bot.core import apartment_details


@pytest.mark.asyncio
@pytest.mark.parametrize("description,present", [
    ("",False), ('<div class="offer__description"><div class="js-description"></div></div>',True),
])
async def test_description_presence_marker_has_no_inferred_empty_observation(monkeypatch,tmp_path,description,present):
    monkeypatch.setenv("HATULI_DETAIL_COOLDOWN_PATH",str(tmp_path/"cooldown.json"))
    monkeypatch.setattr(apartment_details,"_BLOCKED_UNTIL",0.)
    monkeypatch.setattr(apartment_details,"_BLOCKED_STREAK",0)
    monkeypatch.setattr(apartment_details.asyncio,"sleep",AsyncMock())
    constructor = httpx.AsyncClient
    html = '<h1>Квартира</h1><div class="offer__price">200000 ₸</div>'+description
    monkeypatch.setattr(apartment_details.httpx,"AsyncClient",lambda **kwargs:constructor(
        transport=httpx.MockTransport(lambda request:httpx.Response(200,text=html)),**kwargs))
    data = await apartment_details.fetch_apartment_details("https://krisha.kz/a/show/123456",raise_on_error=True)
    assert data["source_detail_valid"] is True
    assert data["description"] == "" and data["description_observed"] is present
