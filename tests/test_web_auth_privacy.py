"""Real PostgreSQL sessions and HTTP guards: forged cookies never grant access."""
import asyncio
import hashlib
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from bot.core import admin_sessions as sessions, auth_users, site_auth
from bot.db import pg

load_dotenv()


@pytest_asyncio.fixture
async def security_data():
    dsn = os.getenv("DATABASE_URL", "")
    if "test" not in dsn.rsplit("/", 1)[-1]:
        pytest.skip("Requires an isolated test database")
    await pg.init_pool(dsn)
    prefix = "__security_" + uuid.uuid4().hex[:12]
    password_hash = auth_users.hash_password("private-test-password")
    user_ids = []
    for suffix in ("one", "two"):
        user_ids.append(await pg.fetchval(
            "INSERT INTO admin_users(username,password_hash) VALUES($1,$2) RETURNING id",
            prefix + suffix, password_hash))
    site_user_id = -int(uuid.uuid4().hex[:12], 16)
    await pg.execute("INSERT INTO users(user_id,full_access,is_blocked) VALUES($1,TRUE,0)", site_user_id)
    data = SimpleNamespace(prefix=prefix, user_ids=user_ids, password_hash=password_hash,
                           site_user_id=site_user_id)
    try:
        yield data
    finally:
        await pg.execute("DELETE FROM login_tokens WHERE telegram_id=$1 OR token LIKE $2", site_user_id, prefix + "%")
        await pg.execute("DELETE FROM favorites WHERE user_id=$1", site_user_id)
        await pg.execute("DELETE FROM site_sessions WHERE user_id=$1", site_user_id)
        await pg.execute("DELETE FROM users WHERE user_id=$1", site_user_id)
        await pg.execute("DELETE FROM admin_users WHERE id=ANY($1::int[])", user_ids)
        await pg.close_pool()


def probe_app():
    app = FastAPI()
    app.add_middleware(sessions.AdminSessionMiddleware)

    @app.get("/probe")
    async def probe(request: Request):
        return {"admin": sessions.is_admin(request), "username": sessions.admin_username(request),
                "tier": await site_auth.get_user_tier(request), "csrf": sessions.admin_csrf_token(request)}

    @app.post("/csrf")
    async def csrf(request: Request):
        valid = sessions.validate_admin_csrf(request, (await request.json()).get("token"))
        return JSONResponse({"ok": valid}, status_code=200 if valid else 403)

    return app


async def token_for(data):
    return await sessions.create_admin_session(data.user_ids[0], password_hash=data.password_hash)


@pytest.mark.asyncio
async def test_opaque_session_rejects_forgery_tampering_expiry_and_username_cookie(security_data):
    data = security_data
    async with AsyncClient(transport=ASGITransport(app=probe_app()), base_url="http://test") as client:
        client.cookies.update({"admin_auth": "1", "admin_user": "admin"})
        assert (await client.get("/probe")).json() == {"admin": False, "username": None, "tier": "public", "csrf": ""}
        token = await token_for(data)
        client.cookies.set(sessions.COOKIE_NAME, token)
        result = (await client.get("/probe")).json()
        assert result["admin"] and result["tier"] == "admin"
        assert result["username"] == data.prefix + "one"
        assert result["csrf"]
        stored = await pg.fetchval("SELECT token_hash FROM admin_sessions WHERE user_id=$1", data.user_ids[0])
        assert stored == hashlib.sha256(token.encode("ascii")).hexdigest() and stored != token
        client.cookies.set(sessions.COOKIE_NAME, token[:-1] + ("a" if token[-1] != "a" else "b"))
        assert not (await client.get("/probe")).json()["admin"]
        client.cookies.set(sessions.COOKIE_NAME, token)
        await pg.execute("UPDATE admin_sessions SET expires_at=now()-interval '1 second' WHERE user_id=$1", data.user_ids[0])
        assert not (await client.get("/probe")).json()["admin"]


@pytest.mark.asyncio
async def test_password_change_revokes_existing_and_old_verified_credentials(security_data):
    data = security_data
    token = await token_for(data)
    await auth_users.set_password(data.user_ids[0], "new-test-password")
    assert await sessions.load_admin_session(token) is None
    assert await pg.fetchval("SELECT count(*) FROM admin_sessions WHERE user_id=$1", data.user_ids[0]) == 0
    with pytest.raises(ValueError):
        await token_for(data)
    current = await pg.fetchval("SELECT password_hash FROM admin_users WHERE id=$1", data.user_ids[0])
    renewed = await sessions.create_admin_session(data.user_ids[0], password_hash=current)
    assert (await sessions.load_admin_session(renewed))["id"] == data.user_ids[0]


@pytest.mark.asyncio
async def test_logout_and_deleted_admin_revoke_sessions(security_data):
    token = await token_for(security_data)
    await sessions.destroy_admin_session(token)
    assert await sessions.load_admin_session(token) is None
    token = await token_for(security_data)
    assert await auth_users.delete_user(security_data.user_ids[0])
    assert await sessions.load_admin_session(token) is None


@pytest.mark.asyncio
async def test_csrf_requires_verified_session_and_handles_unicode(security_data):
    async with AsyncClient(transport=ASGITransport(app=probe_app()), base_url="http://test") as client:
        assert (await client.post("/csrf", json={"token": "guessed"})).status_code == 403
        client.cookies.set(sessions.COOKIE_NAME, await token_for(security_data))
        token = (await client.get("/probe")).json()["csrf"]
        for bad in (None, "", "guessed", "подделка"):
            assert (await client.post("/csrf", json={"token": bad})).status_code == 403
        assert (await client.post("/csrf", json={"token": token})).status_code == 200


def test_admin_cookie_has_expiry_and_secure_https_flags():
    request = Request({"type": "http", "scheme": "https", "server": ("test", 443),
                       "path": "/", "query_string": b"", "headers": [], "method": "GET"})
    response = JSONResponse({"ok": True})
    sessions.set_admin_session_cookie(response, request, "x" * 43)
    cookie = response.headers.getlist("set-cookie")[0]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "Max-Age=43200" in cookie and "expires=" in cookie


@pytest.mark.asyncio
async def test_blocked_site_user_loses_existing_and_cannot_create_session(security_data):
    data = security_data
    session = await site_auth.create_session(data.site_user_id)
    assert (await site_auth.get_user_by_session(session))["full_access"]
    await site_auth.set_user_blocked(data.site_user_id, True)
    assert await site_auth.get_user_by_session(session) is None
    with pytest.raises(ValueError):
        await site_auth.create_session(data.site_user_id)
    async with AsyncClient(transport=ASGITransport(app=probe_app()), base_url="http://test",
                           cookies={"site_session": session}) as client:
        assert (await client.get("/probe")).json()["tier"] == "public"


async def verified_token(data, *, old=False):
    token = data.prefix + uuid.uuid4().hex
    await pg.execute("""INSERT INTO login_tokens(token,telegram_id,status,created_at)
        VALUES($1,$2,'verified',now()-$3::int*interval '1 minute')""",
        token, data.site_user_id, 11 if old else 0)
    return token


@pytest.mark.asyncio
async def test_verified_token_expires_and_is_consumed_once_under_concurrent_polls(security_data):
    data = security_data
    old = await verified_token(data, old=True)
    assert (await site_auth.get_token_status(old))["status"] == "expired"
    assert await site_auth.consume_login_token(old) is None
    fresh = await verified_token(data)
    outcomes = await asyncio.gather(site_auth.consume_login_token(fresh), site_auth.consume_login_token(fresh))
    assert sum(bool(x) for x in outcomes) == 1
    assert await pg.fetchval("SELECT count(*) FROM site_sessions WHERE user_id=$1", data.site_user_id) == 1
    # A delayed legacy bot UPDATE cannot resurrect a consumed credential.
    await pg.execute("UPDATE login_tokens SET status='verified' WHERE token=$1", fresh)
    assert await site_auth.consume_login_token(fresh) is None
    assert (await site_auth.get_token_status(fresh))["status"] == "expired"


@pytest_asyncio.fixture
async def web_client(security_data):
    from bot.admin_web import create_admin_app
    from bot.db.compat import BotDB
    db = BotDB("/tmp/__test_web_security.db")
    await db.init()
    app = create_admin_app(db, "test-password", "test")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield client


@pytest.mark.asyncio
async def test_real_admin_login_and_logout_use_revocable_cookie(security_data, web_client):
    username = security_data.prefix + "one"
    rejected = await web_client.post("/admin/login", data={"username": username, "password": "wrong"})
    assert "admin_session" not in rejected.headers.get("set-cookie", "")
    accepted = await web_client.post("/admin/login", data={"username": username, "password": "private-test-password"})
    assert accepted.status_code in (302, 303)
    token = web_client.cookies.get(sessions.COOKIE_NAME)
    assert token and token != "1" and (await sessions.load_admin_session(token))["username"] == username
    cookie = next(c for c in accepted.headers.get_list("set-cookie") if c.startswith(sessions.COOKIE_NAME + "="))
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    logout = await web_client.get("/admin/logout")
    assert logout.status_code in (302, 303) and await sessions.load_admin_session(token) is None


@pytest.mark.asyncio
async def test_auth_poll_consumes_once_and_does_not_log_in_blocked_users(security_data, web_client):
    token = await verified_token(security_data)
    response = await web_client.get("/api/auth/poll", params={"token": token})
    assert response.json()["status"] == "verified"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    again = await web_client.get("/api/auth/poll", params={"token": token})
    assert again.json()["status"] == "expired" and "set-cookie" not in again.headers
    await site_auth.set_user_blocked(security_data.site_user_id, True)
    blocked = await web_client.get("/api/auth/poll", params={"token": await verified_token(security_data)})
    assert blocked.json()["status"] == "expired" and "set-cookie" not in blocked.headers


@pytest_asyncio.fixture
async def private_records(security_data):
    prefix = security_data.prefix
    dev = await pg.fetchval("INSERT INTO developers(name) VALUES($1) RETURNING id", prefix + "developer")
    cx = await pg.fetchval("""INSERT INTO complexes(name,developer_id,is_newbuild,lat,lon)
        VALUES($1,$2,TRUE,51.128,71.43) RETURNING id""", prefix + "complex", dev)
    lid = prefix + "listing"
    await pg.execute("""INSERT INTO apartment_listings(id,title,address,complex_name,complex_id,price,
        area,rooms,lat,lon,is_active,first_seen,photos)
        VALUES($1,$2,'known-building-address',$3,$4,30000000,60,2,51.128,71.43,TRUE,now(),'[]')""",
        lid, prefix + "private-address", prefix + "complex", cx)
    unit_ids = []
    for source in ("bazis", "person"):
        unit_ids.append(await pg.fetchval("""INSERT INTO newbuild_units(complex_id,source,source_unit_id,
            rooms,area,price,status) VALUES($1,$2,$3,2,60,31000000,'available') RETURNING id""",
            cx, source, prefix + source))
    try:
        yield SimpleNamespace(cx=cx, lid=lid, unit=unit_ids[0], private_unit=unit_ids[1], marker=prefix + "private-address")
    finally:
        await pg.execute("DELETE FROM favorites WHERE listing_id=$1", lid)
        await pg.execute("DELETE FROM apartment_listings WHERE id=$1", lid)
        await pg.execute("DELETE FROM newbuild_units WHERE id=ANY($1::int[])", unit_ids)
        await pg.execute("DELETE FROM complexes WHERE id=$1", cx)
        await pg.execute("DELETE FROM developers WHERE id=$1", dev)


@pytest.mark.asyncio
async def test_private_exports_reject_anonymous_and_forged_admin_cookie(security_data, web_client, private_records):
    record = private_records
    paths = ["/admin/api/geo-kepler.json?type=sale", "/admin/api/geo-rentals.geojson",
             "/admin/api/heat-points", "/admin/api/liquidity-points", "/admin/api/archived-sale-points", "/admin/api/archived-rental-points",
             f"/admin/api/price-history/{record.lid}", f"/admin/api/listing/{record.lid}",
             f"/admin/api/newbuild-unit/{record.private_unit}"]
    for forged in (False, True):
        if forged:
            web_client.cookies.update({"admin_auth": "1", "admin_user": "admin"})
        for path in paths:
            response = await web_client.get(path)
            assert response.status_code == 403, (path, response.status_code)
            assert record.marker not in response.text
        assert (await web_client.get(f"/admin/api/ai/v1/listing/{record.lid}/analysis")).status_code == 401
        for listing_id, private_price in ((record.lid, "30.0 млн ₸"), (f"nb-{record.private_unit}", "31.0 млн ₸")):
            locked_page = await web_client.get(f"/listing/{listing_id}")
            assert locked_page.status_code in (200, 403)
            assert private_price not in locked_page.text
    web_client.cookies.set(sessions.COOKIE_NAME, await token_for(security_data))
    for path in paths:
        assert (await web_client.get(path)).status_code == 200, path


@pytest.mark.asyncio
async def test_public_catalog_retains_developer_units_but_hides_person_lists(web_client, private_records):
    record = private_records
    units = (await web_client.get(f"/admin/api/newbuild-complex/{record.cx}/units")).json()["units"]
    assert [u["id"] for u in units] == [record.unit]
    assert (await web_client.get(f"/admin/api/newbuild-unit/{record.unit}")).status_code == 200
    assert "31.0 млн ₸" in (await web_client.get(f"/listing/nb-{record.unit}")).text
    summary = await web_client.get(f"/admin/api/complex-summary/{record.cx}")
    assert summary.status_code == 200 and summary.json()["listings"] == []
    page = await web_client.get(f"/complex/{record.cx}")
    assert page.status_code == 200 and record.lid not in page.text and record.marker not in page.text


@pytest.mark.asyncio
async def test_site_favorites_do_not_bypass_full_access(security_data, web_client, private_records):
    data, record = security_data, private_records
    session = await site_auth.create_session(data.site_user_id)
    await pg.execute("INSERT INTO favorites(user_id,listing_id) VALUES($1,$2)", data.site_user_id, record.lid)
    web_client.cookies.set("site_session", session)
    assert (await web_client.get("/api/favorites")).status_code == 200
    await site_auth.set_user_full_access(data.site_user_id, False)
    assert (await web_client.get("/api/favorites")).status_code == 403
    assert (await web_client.get("/api/favorites/ids", params={"ids": record.lid})).json()["ids"] == []
    for path in ("/favorites", "/cabinet"):
        page = await web_client.get(path)
        assert page.status_code == 200 and record.marker not in page.text and record.lid not in page.text
