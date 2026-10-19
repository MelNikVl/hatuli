"""Public seller report and guarded imports run with no real DB or source HTTP."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json

import httpx
import pytest

from bot.admin_web import create_admin_app

BASE = "https://test"
ID = "1012345678"
URL = "https://krisha.kz/a/show/" + ID
ORIGIN = {"origin": BASE, "sec-fetch-site": "same-origin"}


class NoPrivateDB:
    def __getattr__(self, name):
        raise AssertionError("Seller route touched private BotDB: " + name)


class Inputs(HTMLParser):
    def __init__(self, value):
        super().__init__()
        self.inputs = {}
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name"):
            self.inputs[values["name"]] = values


@pytest.fixture(autouse=True)
def isolated_sources(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Seller review must not reach real PostgreSQL or HTTP")
    monkeypatch.setattr("asyncpg.connect", forbidden)
    monkeypatch.setattr("asyncpg.create_pool", forbidden)
    monkeypatch.setattr("bot.db.pg.fetchrow", forbidden)
    monkeypatch.setattr("bot.db.pg.fetch", forbidden)
    monkeypatch.setattr("bot.db.pg.execute", forbidden)
    monkeypatch.setattr("bot.web.seller_review.load_daily_picks", lambda: None)
    # The middleware still verifies opaque sessions through a synthetic stub.
    async def no_admin(token):
        return None
    monkeypatch.setattr("bot.core.admin_sessions.load_admin_session", no_admin)


@pytest.fixture
def app():
    return create_admin_app(NoPrivateDB(), "isolated-seller-secret", "test")


def snapshot(at, *, identifier=ID):
    return {"items": [{"id": identifier, "rooms": 2, "area": 60, "price": 32_000_000,
        "floor": 5, "floors_total": 12, "complex_name": "Светлый дом",
        "source_checked_at": at.isoformat(), "source_status": "active",
        "selection_facts": {"ceiling_height": 2.9, "kitchen_area": 12, "finish_level": "renovated", "is_owner": True},
        "phone": "PRIVATE-PHONE", "raw_description": "PRIVATE-DESCRIPTION", "address": "PRIVATE-ADDRESS"}]}


async def post(app, data, *, headers=None, cookies=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, cookies=cookies) as client:
        return await client.post("/improve-listing", data=data, headers=ORIGIN if headers is None else headers)


@pytest.mark.asyncio
async def test_page_public_with_no_db_or_account_and_native_form(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        response = await client.get("/improve-listing?phone=PRIVATE-QUERY")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "PRIVATE" not in response.text
    assert 'method="post" action="/improve-listing#seller-result"' in response.text
    assert 'href="/improve-listing" aria-current="page"' in response.text
    assert 'href="/admin-panel"' not in response.text
    assert Inputs(response.text).inputs["rooms"]["min"] == "0"


@pytest.mark.asyncio
async def test_manual_text_report_works_without_js_db_url_or_llm(app):
    response = await post(app, {"description": "Продаю 2-комнатную квартиру, 65 м², 4 этаж из 9. Кухня 12 м².", "photo_kitchen": "missing", "photos_count": "8"})
    assert response.status_code == 200
    assert "Что улучшить в объявлении" in response.text
    assert "Вы отметили как отсутствующие: кухня." in response.text
    assert "Площадь кухни: 12 м²." in response.text
    assert "Этаж: 4 из 9." in response.text
    assert "Содержимое фотографий пока не анализируем" in response.text
    assert 'name="description"' in response.text


@pytest.mark.asyncio
async def test_empty_form_explains_what_to_supply(app):
    response = await post(app, {"description": "", "url": ""})
    assert response.status_code == 422
    assert "Вставьте текст объявления" in response.text


@pytest.mark.asyncio
async def test_unknown_url_returns_manual_guidance_without_any_fetch(app):
    response = await post(app, {"url": URL + "?utm_source=telegram"})
    assert response.status_code == 200
    assert "нет доступной свежей версии" in response.text
    assert "Вставьте описание" in response.text
    assert "PRIVATE" not in response.text
    assert 'id="seller-result"' not in response.text
    assert 'id="seller-draft-text"' not in response.text
    assert "В переданных данных не указаны" not in response.text


@pytest.mark.asyncio
async def test_fresh_public_top30_imports_allowlisted_numeric_facts_only(app, monkeypatch):
    monkeypatch.setattr("bot.web.seller_review.load_daily_picks", lambda: snapshot(datetime.now(timezone.utc)))
    response = await post(app, {"url": URL})
    assert response.status_code == 200
    assert "Подставили известные параметры" in response.text
    assert "PRIVATE" not in response.text
    fields = Inputs(response.text).inputs
    assert fields["price"]["value"] == "32000000"
    assert fields["rooms"]["value"] == "2"
    assert fields["kitchen_area"]["value"] == "12"
    assert "Светлый дом" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [-2, 73])
async def test_old_or_future_source_observation_never_imported(app, monkeypatch, hours):
    at = datetime.now(timezone.utc) - timedelta(hours=hours)
    monkeypatch.setattr("bot.web.seller_review.load_daily_picks", lambda: snapshot(at))
    response = await post(app, {"url": URL})
    assert response.status_code == 200
    assert "нет доступной свежей версии" in response.text
    assert Inputs(response.text).inputs["price"]["value"] == ""


@pytest.mark.asyncio
async def test_other_public_listing_not_substituted_for_user_url(app, monkeypatch):
    monkeypatch.setattr("bot.web.seller_review.load_daily_picks", lambda: snapshot(datetime.now(timezone.utc), identifier="1099999999"))
    response = await post(app, {"url": URL})
    assert response.status_code == 200
    assert "нет доступной свежей версии" in response.text
    assert "32 000 000 ₸" not in response.text


@pytest.mark.asyncio
async def test_manual_fields_and_pasted_text_override_cached_source(app, monkeypatch):
    monkeypatch.setattr("bot.web.seller_review.load_daily_picks", lambda: snapshot(datetime.now(timezone.utc)))
    response = await post(app, {"url": URL, "price": "31000000", "description": "Свежий текст продавца.", "photo_kitchen": "missing"})
    assert response.status_code == 200
    assert Inputs(response.text).inputs["price"]["value"] == "31000000"
    assert "31 000 000 ₸" in response.text and "32 000 000 ₸" not in response.text
    assert "Свежий текст продавца." in response.text
    assert "Вы отметили как отсутствующие: кухня." in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["admin", "subscriber"])
async def test_verified_tier_import_projection_uses_bound_numeric_id_and_freshness(app, monkeypatch, tier):
    async def verified(request):
        return tier
    monkeypatch.setattr("bot.web.seller_review.get_user_tier", verified)
    calls = []
    async def fixture_fetchrow(sql, *args):
        calls.append((sql, args))
        return {"title": "2-комнатная квартира", "description": "Текст продавца с разрешённым доступом.", "rooms": 2, "area": 60,
            "price": 32_000_000, "floor": 5, "floors_total": 12, "address": "улица Сарайшык, 12/1", "finish_level": "prefinished",
            "phone": "PRIVATE-PHONE", "admin_notes": "PRIVATE-NOTE"}
    monkeypatch.setattr("bot.db.pg.fetchrow", fixture_fetchrow)
    response = await post(app, {"url": URL})
    assert response.status_code == 200
    assert len(calls) == 1 and calls[0][1] == (ID,)
    query = calls[0][0].lower()
    assert "where id=$1" in query and "is_active is true" in query and "72 hours" in query
    assert "select *" not in query and "phone" not in query
    assert "Текст продавца с разрешённым доступом." in response.text
    assert "предчистовая отделка" in response.text
    assert "PRIVATE" not in response.text


@pytest.mark.asyncio
async def test_fake_admin_cookies_never_grant_private_import(app):
    response = await post(app, {"url": URL}, cookies={"admin_auth": "1", "admin_user": "admin", "admin_session": "forged"})
    assert response.status_code == 200
    assert "нет доступной свежей версии" in response.text
    assert 'href="/admin-panel"' not in response.text


@pytest.mark.asyncio
async def test_actual_admin_tier_comes_from_verified_middleware_session(app, monkeypatch):
    token = "A" * 43
    async def verified_session(candidate):
        return {"id": 1, "username": "admin", "csrf_token": "synthetic"} if candidate == token else None
    monkeypatch.setattr("bot.core.admin_sessions.load_admin_session", verified_session)
    async def listing(sql, *args):
        assert args == (ID,) and "apartment_listings" in sql
        return {"description": "ADMIN-SOURCE-DESCRIPTION", "rooms": 2, "area": 60, "price": 32_000_000}
    monkeypatch.setattr("bot.db.pg.fetchrow", listing)
    response = await post(app, {"url": URL}, cookies={"admin_session": token})
    assert response.status_code == 200
    assert "ADMIN-SOURCE-DESCRIPTION" in response.text
    assert 'href="/admin-panel"' in response.text


@pytest.mark.asyncio
async def test_actual_subscriber_tier_requires_resolved_full_access_session(app, monkeypatch):
    auth_calls = []
    async def resolve(sql, *args):
        auth_calls.append((sql, args))
        return {"user_id": 7, "full_access": True, "is_blocked": 0}
    monkeypatch.setattr("bot.core.site_auth.fetchrow", resolve)
    async def listing(sql, *args):
        assert args == (ID,) and "apartment_listings" in sql
        return {"description": "SUBSCRIBER-SOURCE-DESCRIPTION", "rooms": 2, "area": 60}
    monkeypatch.setattr("bot.db.pg.fetchrow", listing)
    response = await post(app, {"url": URL}, cookies={"site_session": "opaque-test-session"})
    assert response.status_code == 200
    assert "SUBSCRIBER-SOURCE-DESCRIPTION" in response.text
    assert len(auth_calls) == 1 and auth_calls[0][1] == ("opaque-test-session",)
    assert "COALESCE(u.is_blocked,0)=0" in auth_calls[0][0]
    assert 'href="/admin-panel"' not in response.text


@pytest.mark.asyncio
async def test_blocked_or_expired_session_resolution_is_fail_closed(app, monkeypatch):
    async def unavailable(sql, *args):
        assert "COALESCE(u.is_blocked,0)=0" in sql and "180 days" in sql
        assert args == ("blocked-session",)
        return None
    monkeypatch.setattr("bot.core.site_auth.fetchrow", unavailable)
    response = await post(app, {"url": URL}, cookies={"site_session": "blocked-session"})
    assert response.status_code == 200 and "нет доступной свежей версии" in response.text
    assert 'id="seller-result"' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [None, {"full_access": False}, {"full_access": 0, "is_blocked": 1}])
async def test_untrusted_or_restricted_site_session_cannot_import_private_listing(app, monkeypatch, user):
    async def resolve(session):
        assert session == "opaque-test-session"
        return user
    monkeypatch.setattr("bot.core.site_auth.get_user_by_session", resolve)
    response = await post(app, {"url": URL}, cookies={"site_session": "opaque-test-session"})
    assert response.status_code == 200
    assert "нет доступной свежей версии" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {}, {"origin": "https://evil.example"}, {"origin": "https://test.evil.example"},
    {"origin": "http://test"}, {"origin": "https://test:8787"},
    {"origin": BASE, "sec-fetch-site": "cross-site"}, {"referer": "https://evil.example/improve-listing"},
])
async def test_foreign_or_missing_origin_rejected_before_import(app, headers):
    response = await post(app, {"url": URL}, headers=headers)
    assert response.status_code == 403
    assert "Обновите страницу" in response.text


@pytest.mark.asyncio
async def test_same_origin_referer_native_form_fallback_supported(app):
    response = await post(app, {"rooms": "0", "area": "35.5"}, headers={"referer": BASE + "/improve-listing", "sec-fetch-site": "same-origin"})
    assert response.status_code == 200 and "Студия, 35,5 м²" in response.text


@pytest.mark.asyncio
async def test_form_duplicates_rejected(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        response = await client.post("/improve-listing", content="rooms=2&rooms=3", headers={**ORIGIN, "content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 422
    assert "Не удалось прочитать форму" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ['{"rooms":2,"rooms":3}', '[' * 1100 + '0' + ']' * 1100])
async def test_duplicate_json_keys_and_deep_json_are_friendly_errors(app, body):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        response = await client.post("/improve-listing", content=body, headers={**ORIGIN, "content-type": "application/json"})
    assert response.status_code == 422
    assert "Не удалось прочитать форму" in response.text


@pytest.mark.asyncio
async def test_public_selection_fact_projection_is_explicit_allowlist(app, monkeypatch):
    value = snapshot(datetime.now(timezone.utc))
    value["items"][0]["selection_facts"].update({"description": "PRIVATE-DESCRIPTION", "phone": "PRIVATE-PHONE", "legal_status": "no_mortgage"})
    monkeypatch.setattr("bot.web.seller_review.load_daily_picks", lambda: value)
    response = await post(app, {"url": URL})
    assert response.status_code == 200
    assert "PRIVATE" not in response.text
    assert "Продавец указывает, что квартира не в ипотеке" not in response.text


@pytest.mark.asyncio
async def test_unknown_url_with_own_text_or_photo_checks_can_generate_report(app):
    own_text = await post(app, {"url": URL, "description": "Продаю 2-комнатную квартиру, 65 м²."})
    photo_checks = await post(app, {"url": URL, "photo_kitchen": "missing"})
    assert own_text.status_code == photo_checks.status_code == 200
    assert 'id="seller-result"' in own_text.text and 'id="seller-result"' in photo_checks.text
    assert "Вы отметили как отсутствующие: кухня." in photo_checks.text


@pytest.mark.asyncio
@pytest.mark.parametrize("body,content_type", [
    ("not-json", "application/json"), ("[]", "application/json"),
    ('{"description":{}}', "application/json"), ('{"description":[]}', "application/json"),
    ("description=text", "text/plain"), ("&".join(f"field{i}=x" for i in range(41)), "application/x-www-form-urlencoded"),
])
async def test_malformed_unexpected_or_excessive_fields_friendly_error(app, body, content_type):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        response = await client.post("/improve-listing", content=body, headers={**ORIGIN, "content-type": content_type})
    assert response.status_code == 422
    assert "Не удалось прочитать форму" in response.text


@pytest.mark.asyncio
async def test_json_native_equivalent_and_checkbox_states_work(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        response = await client.post("/improve-listing", json={"rooms": 2, "area": 60, "is_owner": True, "photo_kitchen": "missing"}, headers=ORIGIN)
    assert response.status_code == 200
    assert "Вы отметили как отсутствующие: кухня." in response.text


@pytest.mark.asyncio
async def test_oversized_body_and_long_description_are_bounded(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        body = await client.post("/improve-listing", content="description=" + "x" * (128 * 1024), headers={**ORIGIN, "content-type": "application/x-www-form-urlencoded"})
        description = await client.post("/improve-listing", data={"description": "x" * 12001}, headers=ORIGIN)
    assert body.status_code == description.status_code == 422
    assert "слишком большое" in body.text
    assert "максимум 12000 символов" in description.text


@pytest.mark.asyncio
async def test_arbitrary_url_invalid_and_no_sql_or_network_called(app):
    response = await post(app, {"url": "http://127.0.0.1:8082/admin"})
    assert response.status_code == 422
    assert "Вставьте ссылку вида" in response.text


@pytest.mark.asyncio
async def test_raw_input_and_freeform_facts_escaped_in_form_and_draft(app):
    response = await post(app, {"description": "</textarea><script>INPUT_XSS</script>", "address": '<img src=x onerror="ADDRESS_XSS">улица Сарайшык, 12',
                               "included_items": '<svg onload="FACT_XSS">холодильник</svg>', "rooms": "2"})
    assert response.status_code == 200
    assert '<script>INPUT_XSS</script>' not in response.text
    assert '&lt;script&gt;INPUT_XSS&lt;/script&gt;' in response.text
    assert '<img src=x onerror="ADDRESS_XSS">' not in response.text
    assert '<svg onload="FACT_XSS">' not in response.text
    assert '&lt;svg onload=' in response.text
    assert "В цену входит: холодильник." in response.text
    assert response.headers["cache-control"] == "no-store"
