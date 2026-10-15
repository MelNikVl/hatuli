"""Public snapshot renders without consulting private listings or exposing IDs."""
import copy
import json

import httpx
import pytest

from bot.admin_web import create_admin_app
from bot.core.public_buyer_example import SNAPSHOT_PATH, build_public_example


class PrivateDBMustNotBeRead:
    def __getattr__(self, name):
        raise AssertionError(f"Public example attempted private DB access: {name}")


@pytest.mark.asyncio
async def test_anonymous_marketing_pages_are_self_contained_and_work_with_forged_cookie():
    app = create_admin_app(PrivateDBMustNotBeRead(), "isolated-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", cookies={"admin_auth": "1"}) as client:
        for path in ("/", "/example", "/how-it-works", "/request-analysis"):
            page = await client.get(path)
            assert page.status_code == 200, path
            assert "Hatuli" in page.text
            assert "Clearly" not in page.text
            assert "mc.yandex.ru" not in page.text
            assert "admin/analysis-requests" not in page.text
            assert 'href="None"' not in page.text
            assert 'href="/map"' in page.text
        report = (await client.get("/example")).text
        assert "Нужно уточнить" in report
        assert "33,25 млн" in report and "всем 30 объявлениям" in report
        assert "4 октября 2026 года" in report
        assert "Снятие объявления не означает продажу" in report
        assert 'stroke-dasharray="5 5"' in report
        assert report.count("Аналог ") == 5
        assert "/a/show/" not in report and "property_id" not in report
        form = await client.get("/request-analysis")
        assert form.headers["cache-control"] == "no-store"
        assert "HttpOnly" in form.headers["set-cookie"]


def test_example_projection_omits_extra_private_fields_and_uses_actual_dates():
    raw = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    raw["property"].update({"id": "PRIVATE-ID", "seller_phone": "+77001234567", "address": "PRIVATE-ADDRESS"})
    raw["comparables"][0]["property_id"] = "PRIVATE-PROPERTY"
    public = build_public_example(raw)
    encoded = json.dumps(public, ensure_ascii=False)
    assert "PRIVATE" not in encoded and "+77001234567" not in encoded
    assert public["property"]["floor"] is None
    assert [p["price"] for p in public["price_history"]] == [34_000_000, 33_000_000, 34_000_000]
    points = public["chart"]["points"]
    assert points[1]["x"] - points[0]["x"] > points[2]["x"] - points[1]["x"]
    assert public["comparables"][0]["price_per_m2"] == pytest.approx(36_000_000 / 47)


@pytest.mark.parametrize("change", ["external_photo", "invalid_price", "future_observation", "wrong_complex", "wrong_area"])
def test_unreviewed_or_inconsistent_snapshot_fails_closed(change):
    raw = copy.deepcopy(json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")))
    if change == "external_photo":
        raw["property"]["photo_url"] = "https://seller.example/contact.jpg"
    elif change == "invalid_price":
        raw["property"]["price"] = float("nan")
    elif change == "future_observation":
        raw["price_history"][-1]["date"] = "2027-01-01"
    elif change == "wrong_complex":
        raw["comparables"][0]["complex_name"] = "Another physical complex"
    else:
        raw["comparables"][0]["area"] = 100
    with pytest.raises(ValueError):
        build_public_example(raw)
