"""HTTP regressions for the actual extras router, with all data access mocked."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio

from bot.core import complex_identity_review as review


@pytest_asyncio.fixture
async def client(monkeypatch):
    from bot.core import site_auth
    from terminal_extras import make_extras_router

    async def user_tier(request):
        return getattr(request.state, "test_role", "public")
    monkeypatch.setattr(site_auth, "get_user_tier", user_tier)
    app = FastAPI()

    @app.middleware("http")
    async def test_auth(request, call_next):
        role = request.headers.get("x-test-role", "public")
        request.state.test_role = role
        request.state.admin_user = {"id": 42, "username": "qa_operator"} if role == "admin" else None
        request.state.admin_csrf = "test-csrf" if role == "admin" else ""
        return await call_next(request)

    app.include_router(make_extras_router(SimpleNamespace(env=SimpleNamespace(globals={}))))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://test") as conn:
        yield conn


def headers(role="admin"):
    return {"x-test-role": role, "x-csrf-token": "test-csrf"}


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["public", "subscriber"])
@pytest.mark.parametrize("method,path", [
    ("get", "/admin/api/entity-ids/duplicates/7/preview"),
    ("post", "/admin/api/entity-ids/duplicates/7/approve"),
    ("post", "/admin/api/entity-ids/duplicates/bulk-approve"),
])
async def test_operator_endpoints_deny_non_admin_before_core_access(client, monkeypatch, role, method, path):
    async def forbidden(*args, **kwargs):
        pytest.fail("Unauthorized request reached identity data")
    monkeypatch.setattr(review, "preview_duplicate_candidate", forbidden)
    monkeypatch.setattr(review, "apply_duplicate_candidate", forbidden)
    result = await getattr(client, method)(path, headers=headers(role))
    assert result.status_code == 401


@pytest.mark.asyncio
async def test_preview_handles_json_database_values_and_cannot_be_cached(client, monkeypatch):
    async def preview(candidate_id):
        assert candidate_id == 7
        return {"can_apply": True, "geo_m": Decimal("12.7"),
                "observed_at": datetime(2026,10,13,tzinfo=timezone.utc)}
    monkeypatch.setattr(review, "preview_duplicate_candidate", preview)
    response = await client.get("/admin/api/entity-ids/duplicates/7/preview", headers=headers())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["geo_m"] == 12.7


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [("not_found",404),("identity_conflict",409)])
async def test_preview_conflicts_have_clear_http_status(client, monkeypatch, code, status):
    async def preview(*args):
        raise review.ComplexIdentityReviewError(code, "Предпросмотр недоступен")
    monkeypatch.setattr(review, "preview_duplicate_candidate", preview)
    response = await client.get("/admin/api/entity-ids/duplicates/7/preview", headers=headers())
    assert response.status_code == status
    assert response.json()["error"] == code


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["[1,2]", "null", "123", '"text"', '{"broken":'])
async def test_approval_rejects_malformed_or_non_object_json(client, monkeypatch, content):
    async def forbidden(*args, **kwargs):
        pytest.fail("Malformed body reached approval")
    monkeypatch.setattr(review, "apply_duplicate_candidate", forbidden)
    response = await client.post("/admin/api/entity-ids/duplicates/7/approve", content=content,
                                 headers={**headers(),"Content-Type":"application/json"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_approval_body_limit_is_checked_before_core(client, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Oversized body reached approval")
    monkeypatch.setattr(review, "apply_duplicate_candidate", forbidden)
    response = await client.post("/admin/api/entity-ids/duplicates/7/approve",
                                 json={"review_note":"x"*12001},headers=headers())
    assert response.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("body,code,status", [
    ({}, "preview_required",409),
    ({"fingerprint":"a"*64}, "evidence_required",422),
    ({"fingerprint":"a"*64,"review_note":"Подтверждён один физический объект","source_urls":[]},
     "evidence_required",422),
])
async def test_approval_real_core_validation_runs_before_database(client, body, code, status):
    response = await client.post("/admin/api/entity-ids/duplicates/7/approve", json=body,headers=headers())
    assert response.status_code == status
    assert response.json()["error"] == code


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [("stale_preview",409),("identity_conflict",409),("not_found",404)])
async def test_approval_reports_stale_or_conflicting_decisions(client, monkeypatch, code, status):
    async def apply(*args, **kwargs):
        raise review.ComplexIdentityReviewError(code, "Обновите предпросмотр")
    monkeypatch.setattr(review, "apply_duplicate_candidate", apply)
    response = await client.post("/admin/api/entity-ids/duplicates/7/approve", json={},headers=headers())
    assert response.status_code == status
    assert response.json()["error"] == code


@pytest.mark.asyncio
async def test_approval_forwards_review_proof_and_actual_operator(client, monkeypatch):
    seen = {}
    async def apply(candidate_id, **kwargs):
        seen.update(kwargs)
        seen["candidate_id"] = candidate_id
        return {"applied":True, "review_id":"test-review"}
    monkeypatch.setattr(review, "apply_duplicate_candidate", apply)
    body = {"fingerprint":"a"*64,"review_note":"Одинаковый объект подтверждён источником",
            "source_urls":["https://korter.kz/project"]}
    response = await client.post("/admin/api/entity-ids/duplicates/7/approve",json=body,headers=headers())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["ok"] is True
    assert seen["candidate_id"] == 7
    assert seen["expected_fingerprint"] == body["fingerprint"]
    assert seen["review_note"] == body["review_note"]
    assert seen["source_urls"] == body["source_urls"]
    assert seen["reviewed_by"] == "qa_operator"


@pytest.mark.asyncio
async def test_bulk_approval_requires_individual_review_and_cannot_write(client, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Bulk request attempted an identity change")
    monkeypatch.setattr(review, "apply_duplicate_candidate", forbidden)
    response = await client.post("/admin/api/entity-ids/duplicates/bulk-approve",
                                 json={"ids":[1,2,3]},headers=headers())
    assert response.status_code == 409
    assert response.json()["error"] == "individual_review_required"


@pytest.mark.asyncio
async def test_buyer_journey_is_private_before_analysis(client, monkeypatch):
    from bot.core import buyer_journey
    async def forbidden(*args, **kwargs):
        pytest.fail("Public request reached private apartment analysis")
    monkeypatch.setattr(buyer_journey, "build_buyer_journey", forbidden)
    response = await client.get("/admin/api/buyer-journey/1012345678")
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["bad-id","1234","9"*21])
async def test_buyer_journey_rejects_non_source_id_before_analysis(client, monkeypatch, identifier):
    from bot.core import buyer_journey
    async def forbidden(*args, **kwargs):
        pytest.fail("Invalid ID reached analysis")
    monkeypatch.setattr(buyer_journey, "build_buyer_journey", forbidden)
    response = await client.get("/admin/api/buyer-journey/"+identifier,headers=headers("subscriber"))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_buyer_journey_subscriber_success_is_not_cached(client, monkeypatch):
    from bot.core import buyer_journey
    async def build(identifier):
        assert identifier == "1012345678"
        return {"found":True,"alternatives":[],"as_of":datetime(2026,10,13,tzinfo=timezone.utc)}
    monkeypatch.setattr(buyer_journey, "build_buyer_journey", build)
    response = await client.get("/admin/api/buyer-journey/1012345678",headers=headers("subscriber"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["found"] is True


@pytest.mark.asyncio
async def test_buyer_journey_timeout_returns_retryable_message(client, monkeypatch):
    from bot.core import buyer_journey
    async def build(identifier):
        raise asyncio.TimeoutError
    monkeypatch.setattr(buyer_journey, "build_buyer_journey", build)
    response = await client.get("/admin/api/buyer-journey/1012345678",headers=headers("subscriber"))
    assert response.status_code == 503
    assert response.json()["found"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["text/plain","application/x-www-form-urlencoded"])
async def test_review_mutation_requires_json_content_type(client, monkeypatch, content_type):
    async def forbidden(*args, **kwargs):
        pytest.fail("Form-compatible request reached identity mutation")
    monkeypatch.setattr(review, "apply_duplicate_candidate", forbidden)
    monkeypatch.setattr(review, "rollback_duplicate_review", forbidden)
    for path in ("/admin/api/entity-ids/duplicates/7/approve",
                 "/admin/api/entity-ids/duplicate-reviews/test-review/rollback"):
        response = await client.post(path,content="{}",headers={**headers(),"Content-Type":content_type})
        assert response.status_code == 415


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["public","subscriber"])
async def test_rollback_is_operator_only(client, monkeypatch, role):
    async def forbidden(*args, **kwargs):
        pytest.fail("Unauthorized rollback reached core")
    monkeypatch.setattr(review, "rollback_duplicate_review", forbidden)
    response = await client.post("/admin/api/entity-ids/duplicate-reviews/test-review/rollback",
                                 json={},headers=headers(role))
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [("not_found",404),("rollback_conflict",409),("stale_preview",409)])
async def test_rollback_reports_conflicts_without_silent_repair(client, monkeypatch, code, status):
    async def rollback(*args, **kwargs):
        raise review.ComplexIdentityReviewError(code, "Откат заблокирован")
    monkeypatch.setattr(review, "rollback_duplicate_review", rollback)
    response = await client.post("/admin/api/entity-ids/duplicate-reviews/test-review/rollback",
                                 json={},headers=headers())
    assert response.status_code == status
    assert response.json()["error"] == code


@pytest.mark.asyncio
async def test_rollback_records_operator_and_returns_private_non_cached_result(client, monkeypatch):
    async def rollback(review_id, *, reviewed_by):
        assert review_id == "test-review"
        assert reviewed_by == "qa_operator"
        return {"rolled_back":True,"journals_retained":True}
    monkeypatch.setattr(review, "rollback_duplicate_review", rollback)
    response = await client.post("/admin/api/entity-ids/duplicate-reviews/test-review/rollback",
                                 json={},headers=headers())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["journals_retained"] is True


