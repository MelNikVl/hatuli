"""The anonymous developer route uses its aggregate without any DB connection."""
import httpx
import pytest

from bot.admin_web import create_admin_app
from bot.core.developer_detail import DEVELOPER_COMPLEXES_SQL


class NoDatabase:
    def __getattr__(self, key):
        raise AssertionError(f"Developer page must use the isolated fetch fixture: {key}")


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_test")

    async def deny(*args, **kwargs):
        raise AssertionError("Production connections and init_pool are prohibited")

    monkeypatch.setattr("asyncpg.connect", deny)
    monkeypatch.setattr("asyncpg.create_pool", deny)
    monkeypatch.setattr("bot.db.pg.init_pool", deny)
    calls = []

    async def fetchrow(sql, *args):
        calls.append((sql, args))
        assert sql == "SELECT * FROM developers WHERE id = $1"
        return None if args == (999,) else {"id": 7, "name": "Fixture developer", "score_total": None}

    async def fetch(sql, *args):
        calls.append((sql, args))
        if sql == DEVELOPER_COMPLEXES_SQL:
            return [{"id": 18, "name": "Fixture project", "district": "Fixture district",
                     "year_built": 2020, "housing_class": None, "photo_url": None,
                     "lat": None, "lon": None, "active_cnt": 12, "sold_cnt": 5,
                     "avg_price_m2": None, "avg_days_to_sell": None, "address": "Street 20/1"}]
        if "FROM homeportal_objects" in sql or "FROM developer_programs" in sql:
            return []
        raise AssertionError("Unexpected developer query: " + sql)

    monkeypatch.setattr("bot.db.pg.fetchrow", fetchrow)
    monkeypatch.setattr("bot.db.pg.fetch", fetch)
    return create_admin_app(NoDatabase(), "isolated-developer-secret", "test"), calls


@pytest.mark.asyncio
async def test_anonymous_developer_uses_one_bulk_query_and_preserves_public_project_fields(isolated):
    application, calls = isolated
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get("/developer/7")
    assert response.status_code == 200
    assert "Fixture developer" in response.text and "Fixture project" in response.text
    assert "Street 20/1" in response.text and 'href="/complex/18"' in response.text
    assert "в продаже 12" in response.text
    assert [sql for sql, _ in calls].count(DEVELOPER_COMPLEXES_SQL) == 1
    assert len(calls) == 4
    assert all(arguments == (7,) for _, arguments in calls)


@pytest.mark.asyncio
async def test_missing_developer_does_not_run_membership_or_other_queries(isolated):
    application, calls = isolated
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get("/developer/999")
    assert response.status_code == 404
    assert "Застройщик не найден" in response.text
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_legacy_admin_alias_retains_same_public_data_and_no_admin_controls(isolated):
    application, calls = isolated
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as client:
        response = await client.get("/admin/developer/7", follow_redirects=False)
    # This existing alias is intentionally the same public page, not a new
    # privileged endpoint. The performance fix must not change its policy.
    assert response.status_code == 200
    assert "Fixture developer" in response.text
    assert 'href="/admin-panel"' not in response.text
    assert len(calls) == 4
