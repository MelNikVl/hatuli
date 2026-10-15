"""HTTP-смоук на /complex/{id} — "модель зонтик/дом" (задача 2026-08-13):
дом ссылается на зонтик, зонтик перечисляет дома, кнопка расшивки
меняет ярлык, если на ЖК уже есть неразрешённая пометка. Тот же паттерн,
что tests/test_split_flag_route.py (реальный ASGI-запрос, не вызов
функции напрямую — ловит и разметку, и то, что роут реально отдаёт 200
на новых полях контекста, которые могли не пробрасываться)."""
import os
import re
import sys
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from tests.admin_auth_helpers import admin_cookies
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
DB_PATH = os.getenv("DB_PATH", "bot.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")


@pytest_asyncio.fixture
async def client():
    import httpx
    from bot.db.pg import init_pool, close_pool
    from bot.db.compat import BotDB
    from bot.admin_web import create_admin_app

    await init_pool(DATABASE_URL)
    db = BotDB(DB_PATH)
    await db.init()
    app = create_admin_app(db, ADMIN_PASSWORD, "test", DB_PATH)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 cookies=await admin_cookies()) as c:
        yield c
    await close_pool()


@pytest_asyncio.fixture
async def umbrella_and_house(client):
    from bot.db.pg import fetchval, execute
    umbrella_id = await fetchval(
        "INSERT INTO complexes (name, lat, lon) VALUES ('__test_umbrella_page__', 51.1, 71.4) RETURNING id")
    house_id = await fetchval("""
        INSERT INTO complexes (name, lat, lon, parent_complex_id)
        VALUES ('__test_house_page__ A', 51.1, 71.4, $1) RETURNING id
    """, umbrella_id)
    try:
        yield umbrella_id, house_id
    finally:
        await execute("DELETE FROM split_candidates WHERE complex_id IN ($1, $2)", umbrella_id, house_id)
        await execute("DELETE FROM complexes WHERE id IN ($1, $2)", umbrella_id, house_id)


@pytest.mark.asyncio
async def test_house_page_links_to_parent(client, umbrella_and_house):
    umbrella_id, house_id = umbrella_and_house
    r = await client.get(f"/complex/{house_id}")
    assert r.status_code == 200
    assert "Часть комплекса" in r.text
    assert f"/complex/{umbrella_id}" in r.text


@pytest.mark.asyncio
async def test_umbrella_page_lists_houses(client, umbrella_and_house):
    umbrella_id, house_id = umbrella_and_house
    r = await client.get(f"/complex/{umbrella_id}")
    assert r.status_code == 200
    assert "Дома в составе комплекса" in r.text
    assert "__test_house_page__ A" in r.text
    assert f"/complex/{house_id}" in r.text


@pytest.mark.asyncio
async def test_plain_complex_shows_note_button_without_pending_marker(client, umbrella_and_house):
    """Задача 2026-08-13 ("убрать якоря/редиректы"): кнопка теперь одна
    и та же всегда — "📋 Пометки на расшивку" — открывает модалку (не
    ссылка на другую страницу). Без неразрешённой пометки — без
    маркера "(есть неразрешённая)"."""
    umbrella_id, _ = umbrella_and_house
    r = await client.get(f"/complex/{umbrella_id}")
    assert "Пометки на расшивку" in r.text
    assert "(есть неразрешённая)" not in r.text
    assert "/admin/entity-ids#" not in r.text  # якорей на другую страницу больше нет


@pytest.mark.asyncio
async def test_complex_with_pending_candidate_shows_marker_and_modal_data(client, umbrella_and_house):
    """С неразрешённой пометкой — та же кнопка получает маркер, модалка
    (eidOpenNoteModal, из _entity_modals.html) есть на странице —
    ничего не редиректит на /admin/entity-ids."""
    from bot.db.pg import fetchval
    umbrella_id, _ = umbrella_and_house
    await fetchval("""
        INSERT INTO split_candidates (complex_id, reason, comment, evidence, matched_by)
        VALUES ($1, 'manual', 'заметка', '{}'::jsonb, 'pytest') RETURNING id
    """, umbrella_id)

    r = await client.get(f"/complex/{umbrella_id}")
    assert "Пометки на расшивку (есть неразрешённая)" in r.text
    assert "eidOpenNoteModal" in r.text
    assert "/admin/entity-ids#" not in r.text


@pytest.mark.asyncio
async def test_homeportal_photos_removed_from_official_data_block(client, umbrella_and_house):
    """Задача 2026-08-13: убрать фото из блока "Официальные данные
    (homeportal.kz)" — cxOpenLightboxByUrl (только там использовался)
    больше не должен встречаться в разметке."""
    umbrella_id, _ = umbrella_and_house
    r = await client.get(f"/complex/{umbrella_id}")
    assert "cxOpenLightboxByUrl" not in r.text


@pytest_asyncio.fixture
async def canonical_identity(client):
    """Separate display aliases from real complexes and bound listing IDs."""
    from bot.db.pg import execute, fetchval

    prefix = "__test_identity_page_" + uuid4().hex + "__"
    canonical_id = await fetchval(
        "INSERT INTO complexes (name, lat, lon) VALUES ($1, 51.1, 71.4) RETURNING id",
        prefix + "canonical")
    alias_id = await fetchval("""
        INSERT INTO complexes (name, canonical_id, canonical_reason, lat, lon)
        VALUES ($1, $2, 'krisha_slug', 51.1, 71.4) RETURNING id
    """, prefix + "alias", canonical_id)
    junk_id = await fetchval("""
        INSERT INTO complexes (name, canonical_reason, lat, lon)
        VALUES ($1, 'junk_unmatched', 51.1, 71.4) RETURNING id
    """, prefix + "junk")
    other_id = await fetchval(
        "INSERT INTO complexes (name) VALUES ($1) RETURNING id", prefix + "other")
    listings = [
        (prefix + "id_only", prefix + "unrelated_text", canonical_id, "Тестовый адрес по ID"),
        (prefix + "alias_bound", prefix + "another_text", alias_id, "Тестовый адрес по ID"),
        (prefix + "legacy_alias", prefix + "alias", None, "Тестовый адрес по ID"),
        # Stale display text must not steal a listing from another bound ID.
        (prefix + "conflicting", prefix + "canonical", other_id, "Чужой адрес"),
    ]
    try:
        for listing_id, name, bound_id, address in listings:
            await execute("""
                INSERT INTO apartment_listings
                    (id, url, complex_name, complex_id, address, price, area,
                     rooms, is_active, first_seen, last_seen)
                VALUES ($1, $1, $2, $3, $4, 25000000, 50, 2, TRUE, now(), now())
            """, listing_id, name, bound_id, address)
        yield {
            "prefix": prefix, "canonical_id": canonical_id, "alias_id": alias_id,
            "junk_id": junk_id, "included": [row[0] for row in listings[:3]],
            "excluded": listings[3][0],
        }
    finally:
        await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])",
                      [row[0] for row in listings])
        await execute("DELETE FROM complexes WHERE id = ANY($1::int[])",
                      [alias_id, canonical_id, junk_id, other_id])


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/admin"])
async def test_alias_page_redirects_to_canonical_and_preserves_filters(client, canonical_identity, prefix):
    identity = canonical_identity
    query = "nb_rooms=2&nb_all=1&source=old%20link"
    response = await client.get(f"{prefix}/complex/{identity['alias_id']}?{query}")
    assert response.status_code == 302
    assert response.headers["location"] == f"{prefix}/complex/{identity['canonical_id']}?{query}"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/complexes", "/admin/complexes"])
async def test_catalog_hides_aliases_and_unmatched_fragments(client, canonical_identity, path):
    identity = canonical_identity
    response = await client.get(path, params={"search": identity["prefix"]})
    assert response.status_code == 200
    assert f"/complex/{identity['canonical_id']}" in response.text
    assert identity["prefix"] + "canonical" in response.text
    assert identity["prefix"] + "alias" not in response.text
    assert identity["prefix"] + "junk" not in response.text


@pytest.mark.asyncio
async def test_canonical_page_counts_id_and_alias_membership_without_text_conflict(client, canonical_identity):
    identity = canonical_identity
    response = await client.get(f"/complex/{identity['canonical_id']}")
    assert response.status_code == 200
    assert re.search(r"В продаже сейчас.*?<b[^>]*>\s*3\s*</b>", response.text, re.DOTALL)
    for listing_id in identity["included"]:
        assert f"/listing/{listing_id}" in response.text
    assert f"/listing/{identity['excluded']}" not in response.text
    assert "Тестовый адрес по ID" in response.text
    assert "Чужой адрес" not in response.text


@pytest.mark.asyncio
async def test_unmatched_fragment_has_no_public_detail_page(client, canonical_identity):
    response = await client.get(f"/complex/{canonical_identity['junk_id']}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_umbrella_aggregates_bound_child_but_house_excludes_unknown_parent(client, umbrella_and_house):
    from bot.db.pg import execute

    umbrella_id, house_id = umbrella_and_house
    prefix = "__test_umbrella_id_" + uuid4().hex + "__"
    child_listing = prefix + "child"
    parent_listing = prefix + "unknown_parent"
    try:
        for listing_id, bound_id in [(child_listing, house_id), (parent_listing, umbrella_id)]:
            await execute("""
                INSERT INTO apartment_listings
                    (id, url, complex_name, complex_id, price, area, rooms,
                     is_active, first_seen, last_seen)
                VALUES ($1, $1, $2, $3, 25000000, 50, 2, TRUE, now(), now())
            """, listing_id, prefix + "unrelated_name", bound_id)

        umbrella = await client.get(f"/complex/{umbrella_id}")
        assert umbrella.status_code == 200
        assert re.search(r"В продаже сейчас.*?<b[^>]*>\s*2\s*</b>", umbrella.text, re.DOTALL)
        assert f"/listing/{child_listing}" in umbrella.text
        assert f"/listing/{parent_listing}" in umbrella.text
        assert "дом неизвестен: 1" in umbrella.text
        assert "1 объявл." in umbrella.text

        house = await client.get(f"/complex/{house_id}")
        assert house.status_code == 200
        assert re.search(r"В продаже сейчас.*?<b[^>]*>\s*1\s*</b>", house.text, re.DOTALL)
        assert f"/listing/{child_listing}" in house.text
        assert f"/listing/{parent_listing}" not in house.text
        for target_id, expected in [(umbrella_id, 2), (house_id, 1)]:
            graph = await client.get(f"/admin/api/complex/{target_id}/price-dynamics",
                                     params={"rooms": "2"})
            assert graph.status_code == 200
            assert sum(point["n"] for point in graph.json()["data"]["2"]) == expected
    finally:
        await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])",
                      [child_listing, parent_listing])


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/complexes", "/admin/complexes"])
async def test_catalog_search_by_alias_returns_canonical(client, canonical_identity, path):
    identity = canonical_identity
    response = await client.get(path, params={"search": identity["prefix"] + "alias"})
    assert response.status_code == 200
    assert f"/complex/{identity['canonical_id']}" in response.text
    assert identity["prefix"] + "canonical" in response.text
    assert f"/complex/{identity['alias_id']}" not in response.text


@pytest.mark.asyncio
async def test_catalog_map_hides_aliases_and_unmatched_fragments(client, canonical_identity):
    identity = canonical_identity
    response = await client.get("/admin/api/complexes-map")
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["complexes"]}
    assert identity["canonical_id"] in ids
    assert identity["alias_id"] not in ids
    assert identity["junk_id"] not in ids


@pytest.mark.asyncio
@pytest.mark.parametrize("id_key", ["canonical_id", "alias_id"])
@pytest.mark.parametrize("endpoint", ["price-dynamics", "turnover-dynamics"])
async def test_sale_graph_uses_canonical_membership_and_preserves_room_filter(
        client, canonical_identity, id_key, endpoint):
    from bot.db.pg import execute

    identity = canonical_identity
    await execute("""
        UPDATE apartment_listings
        SET first_seen = now() - interval '10 days',
            archived_at = now() - interval '1 day', is_active = FALSE,
            price = CASE WHEN id = $2 THEN 99000000 ELSE 25000000 END
        WHERE id = ANY($1::text[])
    """, identity["included"] + [identity["excluded"]], identity["excluded"])
    path = f"/admin/api/complex/{identity[id_key]}/{endpoint}"
    response = await client.get(path, params={"kind": "sale", "rooms": "2", "days": 90})
    assert response.status_code == 200
    points = response.json()["data"]["2"]
    assert sum(point["n"] for point in points) == 3
    if endpoint == "price-dynamics":
        assert all(point["median_price"] == 25000000 for point in points)
    else:
        assert all(point["avg_days"] == 9.0 for point in points)
    filtered = await client.get(path, params={"kind": "sale", "rooms": "1", "days": 90})
    assert filtered.status_code == 200
    assert filtered.json()["data"] == {}
