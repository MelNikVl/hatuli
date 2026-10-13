"""Server-browser publication boundaries; no network, browser or real DB."""
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
import asyncio
import json
import sys
import time

import pytest

from scripts import publish_daily_picks_browser as worker

NOW = datetime(2026, 10, 13, 4, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def prohibit_real_io(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Browser worker tests must never access PostgreSQL or a real browser")
    monkeypatch.setattr(worker.publisher, "read_candidates", reject)
    monkeypatch.setattr(worker, "ordinary_browser", reject)


def item(index=1, **changes):
    lid = str(1016000000 + index)
    return {"id": lid, "source_url": f"https://krisha.kz/a/show/{lid}",
            "price": 30_000_000, "rooms": 2, "area": 60.4, "floor": 5, "floors_total": 12, **changes}


def observation(candidate=None, **changes):
    candidate = candidate or item()
    return {"page_url": candidate["source_url"], "identity_urls": [candidate["source_url"]],
            "document_title": f"Продажа квартиры — объявление № {candidate['id']}",
            "main_visible": True, "title": "2-комнатная квартира, 60,4 м², 5/12 этаж",
            "price_text": "30 000 000 ₸", "main_text": "Квартира в продаже", "body_text": "Квартира в продаже",
            "captcha_visible": False, "breadcrumbs": ["/prodazha/kvartiry/astana/"], **changes}


def test_source_facts_are_observed_and_accepted_by_existing_publisher(tmp_path):
    candidate = item()
    record = worker.source_record(candidate, observation(), NOW)
    assert record == {**candidate, "status": "active", "checked_at": NOW.isoformat(), "sale_astana": True}
    path = tmp_path / "checks.json"
    worker.atomic_private_json(path, {"method": "normal_browser", "items": [record]})
    loaded = worker.publisher.load_source_checks(str(path), NOW)
    projected, gets, invalid = worker.publisher.apply_source_checks({"items": [candidate]}, loaded)
    assert len(projected["items"]) == 1 and gets == 0 and invalid == set()


@pytest.mark.parametrize("age", [timedelta(hours=2, seconds=1), -timedelta(seconds=1)])
def test_stale_or_future_observations_cannot_reach_publication(tmp_path, age):
    record = worker.source_record(item(), observation(), NOW - age)
    path = tmp_path / "checks.json"
    worker.atomic_private_json(path, {"method": "normal_browser", "items": [record]})
    with pytest.raises(ValueError):
        worker.publisher.load_source_checks(str(path), NOW)


def test_missing_and_duplicate_confirmations_fail_closed(tmp_path):
    assert worker.publisher.apply_source_checks({"items": [item()]}, {})[0]["items"] == []
    record = worker.source_record(item(), observation(), NOW)
    path = tmp_path / "checks.json"
    worker.atomic_private_json(path, {"method": "normal_browser", "items": [record, record]})
    with pytest.raises(ValueError):
        worker.publisher.load_source_checks(str(path), NOW)


@pytest.mark.parametrize("change", [
    {"page_url": "https://private.example/a/show/1016000001"},
    {"page_url": item()["source_url"] + "?anything=1"},
    {"identity_urls": []}, {"identity_urls": [item(2)["source_url"]]},
    {"identity_urls": [item()["source_url"], item(2)["source_url"]]},
    {"document_title": "Продажа квартиры"}, {"document_title": "Объявление №1016000002"},
    {"main_visible": False}, {"title": "Похожие предложения"}, {"title": "2-комнатная квартира 60,4 м²"},
    {"price_text": "от 30 000 000 ₸"}, {"price_text": "30 000 000 USD"},
    {"price_text": "30 000 000 ₸ / м²"}, {"price_text": "30 000 000 ₸ 20 000 000 ₸"},
    {"breadcrumbs": ["/arenda/kvartiry/astana/"]}, {"breadcrumbs": ["/prodazha/kvartiry/almaty/"]},
    {"breadcrumbs": ["https://private.example/prodazha/kvartiry/astana/"]},
])
def test_wrong_identity_recommendations_and_incomplete_offer_fail_closed(change):
    record = worker.source_record(item(), observation(**change), NOW)
    assert record["status"] == "unknown"
    assert "price" not in record and "rooms" not in record


@pytest.mark.parametrize("change,field,value", [
    ({"price_text": "29 000 000 ₸"}, "price", 29_000_000),
    ({"title": "3-комнатная квартира, 60,4 м², 5/12 этаж"}, "rooms", 3),
    ({"title": "2-комнатная квартира, 70 м², 5/12 этаж"}, "area", 70),
    ({"title": "2-комнатная квартира, 60,4 м², 7/12 этаж"}, "floor", 7),
    ({"title": "2-комнатная квартира, 60,4 м², 5/15 этаж"}, "floors_total", 15),
])
def test_changed_offer_records_real_facts_instead_of_seed_values(change, field, value):
    record = worker.source_record(item(), observation(**change), NOW)
    assert record["status"] == "mismatch" and record[field] == value
    assert record[field] != item()[field]


def test_archive_is_scoped_to_main_offer_not_global_navigation():
    assert worker.source_record(item(), observation(body_text="Мои объявления: В архиве"), NOW)["status"] == "active"
    assert worker.source_record(item(), observation(main_text="Объявление в архиве", price_text=""), NOW)["status"] == "archived"


@pytest.mark.parametrize("text", ["Проверка браузера — Kolesa Group", "Подтвердите, что вы не робот", "Checking your browser", "Verify you are human", "Access denied", "Too many requests"])
def test_challenge_detection(text):
    assert worker.is_challenged(observation(body_text=text))


def test_visible_captcha_is_blocking_even_without_text():
    assert worker.is_challenged(observation(captcha_visible=True))
    assert not worker.is_challenged(observation(body_text="Квартира от собственника"))


@pytest.mark.parametrize("change", [
    {"source_url": "http://krisha.kz/a/show/1016000001"},
    {"source_url": "https://127.0.0.1/a/show/1016000001"}, {"id": "../../secret"},
    {"price": True}, {"area": float("nan")}, {"floor": 0},
])
def test_candidate_validation_prevents_unsafe_navigation(change):
    with pytest.raises(ValueError):
        worker.validate_candidates({"kind": "unverified_review_candidates", "items": [item(**change)]}, 90)


def test_candidate_limit_and_unique_identity_are_strict():
    payload = {"kind": "unverified_review_candidates", "items": [item(i) for i in range(1, 91)]}
    assert len(worker.validate_candidates(payload, 1)) == 1
    with pytest.raises(ValueError):
        worker.validate_candidates({**payload, "items": [item(), item()]}, 90)
    with pytest.raises(ValueError):
        worker.validate_candidates({**payload, "items": [item(i) for i in range(1, 92)]}, 90)


class FakePage:
    def __init__(self, actions, context):
        self.actions = iter(actions)
        self.context = context
        self.main_frame = object()
        self.listeners = {}
        self.urls = []
        self.current = {}
        self.evaluate_calls = 0

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        assert self.listeners.pop(event) is callback

    async def goto(self, url, **options):
        assert options["wait_until"] == "domcontentloaded" and 0 < options["timeout"] <= 25000
        self.urls.append(url)
        self.current = next(self.actions)
        if "error" in self.current:
            raise self.current["error"]
        response = SimpleNamespace(status=self.current.get("status", 200),
            request=SimpleNamespace(is_navigation_request=lambda: True), frame=self.main_frame)
        if "response" in self.listeners:
            self.listeners["response"](response)
        if self.current.get("close"):
            self.context.emit_close()
        return response

    async def evaluate(self, code):
        assert code == worker.READ_OFFER_DOM
        self.evaluate_calls += 1
        return self.current.get("observation", observation())

    def locator(self, selector):
        assert selector == ".offer__price"
        async def wait_for(**options):
            raise TimeoutError()
        return SimpleNamespace(first=SimpleNamespace(wait_for=wait_for))


class FakeContext:
    def __init__(self, actions):
        self.page = FakePage(actions, self)
        self.closed = False
        self.close_listener = None

    async def new_page(self):
        return self.page

    def on(self, event, callback):
        assert event == "close"
        self.close_listener = callback

    def emit_close(self):
        if self.close_listener:
            self.close_listener()

    async def close(self):
        self.closed = True
        self.emit_close()


def fake_browser(monkeypatch, context, modes=None):
    @asynccontextmanager
    async def opened(profile, *, headless=True):
        assert profile.name == "profile"
        if modes is not None:
            modes.append(headless)
        try:
            yield context
        finally:
            await context.close()
    monkeypatch.setattr(worker, "ordinary_browser", opened)
    monkeypatch.setattr(worker, "VISIT_INTERVAL_SECONDS", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 468])
async def test_blocked_browser_stops_before_next_visit_and_closes(tmp_path, monkeypatch, status):
    context = FakeContext([{"status": status}, {"observation": observation(item(2))}])
    fake_browser(monkeypatch, context)
    path = tmp_path / "checks.json"
    with pytest.raises(worker.publisher.SourceBlockedError):
        await worker.check_sources([item(), item(2)], path, time.monotonic() + 60, tmp_path / "profile")
    assert context.page.urls == [item()["source_url"]] and context.closed
    evidence = json.loads(path.read_text())
    assert evidence["completed"] is False and evidence["blocked_http_status"] == status
    assert evidence["attempted_count"] == 1 and evidence["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 404, 503])
async def test_captcha_any_http_status_stops_entire_run_without_any_interaction(tmp_path, monkeypatch, status):
    context = FakeContext([{"status": status, "observation": observation(body_text="Подтвердите, что вы не робот")}, {}])
    fake_browser(monkeypatch, context)
    with pytest.raises(worker.BrowserChallengeError):
        await worker.check_sources([item(), item(2)], tmp_path / "checks.json", time.monotonic() + 60, tmp_path / "profile")
    assert context.page.urls == [item()["source_url"]] and context.closed


@pytest.mark.asyncio
async def test_plain_503_without_challenge_remains_unknown(tmp_path, monkeypatch):
    context = FakeContext([{"status": 503, "observation": observation(body_text="Сервис временно недоступен")},
                           {"observation": observation(item(2))}])
    fake_browser(monkeypatch, context)
    evidence = await worker.check_sources([item(), item(2)], tmp_path / "checks.json", time.monotonic() + 60,
                                          tmp_path / "profile")
    assert [row["status"] for row in evidence["items"]] == ["unknown", "active"]
    assert len(context.page.urls) == 2 and evidence["completed"] is True


@pytest.mark.asyncio
async def test_deleted_unknown_timeout_and_real_success_one_pass(tmp_path, monkeypatch):
    context = FakeContext([{"status": 404}, {"error": TimeoutError("navigation")},
                           {"observation": observation(item(3), price_text="29 000 000 ₸")},
                           {"observation": observation(item(4))}])
    modes = []
    fake_browser(monkeypatch, context, modes)
    records = await worker.check_sources([item(i) for i in range(1, 5)], tmp_path / "checks.json",
                                        time.monotonic() + 60, tmp_path / "profile", headed=True)
    assert [row["status"] for row in records["items"]] == ["deleted", "unknown", "mismatch", "active"]
    assert records["completed"] is True and records["attempted_count"] == 4
    assert modes == [False] and len(context.page.urls) == 4 and context.closed


@pytest.mark.asyncio
async def test_expired_budget_never_visits_source(tmp_path, monkeypatch):
    context = FakeContext([{}])
    fake_browser(monkeypatch, context)
    with pytest.raises(worker.BrowserBudgetExceeded):
        await worker.check_sources([item()], tmp_path / "checks.json", time.monotonic() - 1, tmp_path / "profile")
    assert context.page.urls == [] and context.closed


@pytest.mark.asyncio
async def test_persistent_launch_has_no_stealth_or_sandbox_override(tmp_path, monkeypatch):
    context = FakeContext([])
    calls = []
    async def launch(**options):
        calls.append(options)
        return context
    @asynccontextmanager
    async def factory():
        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    module = ModuleType("playwright.async_api")
    module.async_playwright = factory
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    # Undo only the autouse browser guard; no real browser module is invoked.
    monkeypatch.undo()
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    async with worker.ordinary_browser(tmp_path / "profile", headless=False) as opened:
        assert opened is context
    assert calls == [{"user_data_dir": str(tmp_path / "profile"), "channel": "chromium", "headless": False, "chromium_sandbox": True}]
    assert context.closed


def pipeline_args(tmp_path, **changes):
    return SimpleNamespace(budget_seconds=60, check_limit=90, profile_path=tmp_path / "profile",
                           headed=False, dry_run=False, force=True, **changes)


@pytest.mark.asyncio
async def test_pipeline_requires_evidence_and_30_then_uses_atomic_existing_publisher(tmp_path, monkeypatch):
    calls = []
    async def export(path, now):
        calls.append("prepare")
        return {"kind": "unverified_review_candidates", "input_count": 900,
                "items": [item(i) for i in range(1, 31)]}
    async def check(items, path, deadline, profile, **options):
        calls.append("check")
        evidence = {"method": "normal_browser", "items": [worker.source_record(row, observation(row), NOW) for row in items]}
        worker.atomic_private_json(path, evidence)
        return evidence
    async def generate(args, now):
        calls.append("generate")
        assert args.skip_photo_check is False and args.source_checks == str(tmp_path / "source-checks.json")
        return {"snapshot": {"items": [item(i) for i in range(1, 31)], "selection_date": "2026-10-13"}, "photo_heads": 30, "source_gets": 0}
    @contextmanager
    def lock(path):
        calls.append("publication_lock")
        yield
    monkeypatch.setattr(worker.publisher, "export_candidate_pool", export)
    monkeypatch.setattr(worker, "check_sources", check)
    monkeypatch.setattr(worker.publisher, "generate", generate)
    monkeypatch.setattr(worker.publisher, "publisher_lock", lock)
    monkeypatch.setattr(worker.publisher, "publish_snapshot", lambda snapshot, path: calls.append("publish"))
    result = await worker.pipeline(pipeline_args(tmp_path), tmp_path, {}, tmp_path / "public.json")
    assert calls == ["prepare", "check", "publication_lock", "generate", "publish"]
    assert result["status"] == "published" and result["count"] == 30 and result["source_gets"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_partial_cards_and_dry_run_never_replace_snapshot(tmp_path, monkeypatch, dry_run):
    public = tmp_path / "public.json"
    public.write_bytes(b"last verified snapshot")
    calls = []
    async def export(path, now):
        return {"kind": "unverified_review_candidates", "input_count": 900, "items": [item()]}
    async def check(*args, **kwargs):
        return {"items": [{"status": "active"}], "attempted_count": 1}
    async def generate(*args):
        calls.append("generate")
        return {"snapshot": {"items": [item(i) for i in range(1, 30)]}}
    @contextmanager
    def lock(path):
        yield
    def reject_publish(*args):
        raise AssertionError("Unverified/partial output must not be published")
    monkeypatch.setattr(worker.publisher, "export_candidate_pool", export)
    monkeypatch.setattr(worker, "check_sources", check)
    monkeypatch.setattr(worker.publisher, "generate", generate)
    monkeypatch.setattr(worker.publisher, "publisher_lock", lock)
    monkeypatch.setattr(worker.publisher, "publish_snapshot", reject_publish)
    args = pipeline_args(tmp_path)
    args.dry_run = dry_run
    if dry_run:
        assert (await worker.pipeline(args, tmp_path, {}, public))["status"] == "dry_run"
        assert calls == []
    else:
        with pytest.raises(worker.InsufficientConfirmedCardsError):
            await worker.pipeline(args, tmp_path, {}, public)
        assert calls == ["generate"]
    assert public.read_bytes() == b"last verified snapshot"


@pytest.mark.asyncio
async def test_actual_publisher_uses_browser_evidence_without_source_get_fallback(tmp_path, monkeypatch):
    now = worker.utc_now()
    rows = [dict(id=row["id"], url=row["source_url"], property_id=index, is_active=True,
                 market_type="secondary", last_seen=now, complex_id=1, complex_resolution="name",
                 price=row["price"], rooms=row["rooms"], area=row["area"], floor=row["floor"],
                 floors_total=row["floors_total"], finish_level="finished", ceiling_height=3.0,
                 approved_photo_urls=["https://krisha-photos.kcdn.online/webp/c7/c70532b0-9310-4f48-bc2d-e59a83f9f345/1-full.jpg"])
            for index, row in enumerate(item(i) for i in range(1, 31))]
    async def read(*args):
        return rows
    async def reject_get(*args):
        raise AssertionError("A browser-confirmed pipeline must never fetch sources via HTTP")
    async def photos(snapshot, deadline):
        return snapshot, len(snapshot["items"])
    path = tmp_path / "checks.json"
    worker.atomic_private_json(path, {"method": "normal_browser", "items": [
        worker.source_record(item(i), observation(item(i)), now) for i in range(1, 31)]})
    monkeypatch.setenv("DATABASE_URL", "postgresql://unit_test/unused")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args: False)
    monkeypatch.setattr(worker.publisher, "read_candidates", read)
    monkeypatch.setattr(worker.publisher, "verify_sources", reject_get)
    monkeypatch.setattr(worker.publisher, "verify_photos", photos)
    result = await worker.publisher.generate(SimpleNamespace(skip_photo_check=False, source_checks=str(path)), now)
    assert len(result["snapshot"]["items"]) == 30 and result["source_gets"] == 0


@pytest.mark.asyncio
async def test_manual_browser_stays_open_on_468_and_never_calls_publisher(tmp_path, monkeypatch):
    context = FakeContext([{"status": 468, "close": True, "observation": observation(body_text="Подтвердите, что вы не робот")}])
    modes = []
    fake_browser(monkeypatch, context, modes)
    args = SimpleNamespace(profile_path=tmp_path / "profile", source_url=item()["source_url"], budget_seconds=60)
    path = tmp_path / "manual-status.json"
    result = await worker.open_manual_browser(args, path)
    assert result["status"] == "browser_closed" and result["blocked_http_status"] == 468
    assert result["published"] is False and modes == [False]
    assert context.page.urls == [item()["source_url"]] and context.closed


def test_private_atomic_failure_retains_previous_bytes_and_removes_temporary(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_bytes(b"previous")
    with pytest.raises(ValueError):
        worker.atomic_private_json(path, {"bad": float("nan")})
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("url", ["http://krisha.kz/a/show/1016000001", "https://private.example/a/show/1016000001", "https://krisha.kz/a/show/1016000001?x=1"])
def test_manual_url_is_bounded_and_canonical(url):
    with pytest.raises(ValueError):
        worker.validated_source_url(url)


def test_profile_rejects_existing_user_profile_outside_dedicated_var(tmp_path):
    snapshot = tmp_path / "var" / "daily-picks.json"
    assert worker.dedicated_profile(None, snapshot) == (tmp_path / "var" / "browser-picks" / "profile").resolve()
    with pytest.raises(ValueError):
        worker.dedicated_profile(str(tmp_path / "Chrome"), snapshot)


def test_main_failure_retains_publication_and_records_safe_counts(tmp_path, monkeypatch, capsys):
    public = tmp_path / "daily-picks.json"
    public.write_bytes(b"last verified snapshot")
    @contextmanager
    def lock(path):
        path.mkdir(parents=True, exist_ok=True)
        yield
    async def blocked(args, directory, status, snapshot):
        worker.atomic_private_json(directory / "source-checks.json", {"items": [], "attempted_count": 1})
        raise worker.publisher.SourceBlockedError(468)
    monkeypatch.setattr(worker.publisher, "snapshot_path", lambda: public)
    monkeypatch.setattr(worker, "worker_lock", lock)
    monkeypatch.setattr(worker, "already_published", lambda *args: False)
    monkeypatch.setattr(worker, "pipeline", blocked)
    assert worker.main([]) == 1
    assert public.read_bytes() == b"last verified snapshot"
    status = json.loads((tmp_path / "daily-picks-browser" / "last-run.json").read_text())
    assert status["status"] == "failed" and status["blocked_http_status"] == 468
    assert status["attempted_count"] == 1 and status["previous_snapshot_retained"] is True
    assert "source_url" not in capsys.readouterr().err


def test_failure_report_is_written_while_outer_lock_is_owned(tmp_path, monkeypatch):
    state = {"held": False, "failure_report": False}
    original_write = worker.atomic_private_json
    @contextmanager
    def lock(path):
        path.mkdir(parents=True, exist_ok=True)
        state["held"] = True
        try:
            yield
        finally:
            state["held"] = False
    async def blocked(*args):
        raise worker.publisher.SourceBlockedError(468)
    def write(path, payload):
        if path.name == "last-run.json":
            assert state["held"], "Failure status must remain inside outer lock"
            state["failure_report"] = True
        original_write(path, payload)
    monkeypatch.setattr(worker.publisher, "snapshot_path", lambda: tmp_path / "daily-picks.json")
    monkeypatch.setattr(worker, "worker_lock", lock)
    monkeypatch.setattr(worker, "already_published", lambda *args: False)
    monkeypatch.setattr(worker, "pipeline", blocked)
    monkeypatch.setattr(worker, "atomic_private_json", write)
    assert worker.main([]) == 1
    assert state == {"held": False, "failure_report": True}


def test_real_pipeline_503_captcha_retains_previous_public_snapshot(tmp_path, monkeypatch):
    public = tmp_path / "daily-picks.json"
    public.write_bytes(b"previous verified snapshot")
    context = FakeContext([{"status": 503, "observation": observation(body_text="Подтвердите, что вы не робот")}, {}])
    fake_browser(monkeypatch, context)
    @contextmanager
    def lock(path):
        path.mkdir(parents=True, exist_ok=True)
        yield
    async def export(path, now):
        return {"kind": "unverified_review_candidates", "input_count": 900, "items": [item(), item(2)]}
    async def reject_generate(*args):
        raise AssertionError("A challenged run must never reach publication")
    monkeypatch.setattr(worker.publisher, "snapshot_path", lambda: public)
    monkeypatch.setattr(worker, "worker_lock", lock)
    monkeypatch.setattr(worker.publisher, "export_candidate_pool", export)
    monkeypatch.setattr(worker.publisher, "generate", reject_generate)
    assert worker.main(["--force"]) == 1
    assert public.read_bytes() == b"previous verified snapshot" and len(context.page.urls) == 1
    evidence_path = next((tmp_path / "daily-picks-browser").glob("*/source-checks.json"))
    assert json.loads(evidence_path.read_text())["completed"] is False


def test_diagnostics_failure_after_publication_never_claims_old_snapshot_retained(tmp_path, monkeypatch, capsys):
    public = tmp_path / "daily-picks.json"
    public.write_bytes(b"old snapshot")
    @contextmanager
    def lock(path):
        path.mkdir(parents=True, exist_ok=True)
        yield
    async def published(args, directory, status, snapshot):
        snapshot.write_bytes(b"new verified snapshot")
        status.update(published=True, count=30)
        return {**status, "status": "published"}
    def broken_diagnostics(*args):
        raise OSError("simulated diagnostics write failure")
    monkeypatch.setattr(worker.publisher, "snapshot_path", lambda: public)
    monkeypatch.setattr(worker, "worker_lock", lock)
    monkeypatch.setattr(worker, "already_published", lambda *args: False)
    monkeypatch.setattr(worker, "pipeline", published)
    monkeypatch.setattr(worker, "atomic_private_json", broken_diagnostics)
    assert worker.main([]) == 1
    assert public.read_bytes() == b"new verified snapshot"
    report = json.loads(capsys.readouterr().err)
    assert report["status"] == "published_with_diagnostics_error" and report["published"] is True
    assert report["previous_snapshot_retained"] is False and report["diagnostics_error_type"] == "OSError"


def test_main_manual_status_never_replaces_job_status(tmp_path, monkeypatch):
    root = tmp_path / "daily-picks-browser"
    root.mkdir()
    (root / "last-run.json").write_bytes(b"blocked job")
    @contextmanager
    def lock(path):
        yield
    async def manual(args, status_path):
        return {"status": "browser_closed", "mode": "manual_browser", "published": False}
    monkeypatch.setattr(worker.publisher, "snapshot_path", lambda: tmp_path / "daily-picks.json")
    monkeypatch.setattr(worker, "worker_lock", lock)
    monkeypatch.setattr(worker, "open_manual_browser", manual)
    assert worker.main(["--open-browser", "--headed", "--source-url", item()["source_url"]]) == 0
    assert (root / "last-run.json").read_bytes() == b"blocked job"


@pytest.mark.asyncio
async def test_hung_manual_launch_is_cancelled_and_does_not_replace_job_status(tmp_path, monkeypatch):
    root = tmp_path / "daily-picks-browser"
    root.mkdir()
    (root / "last-run.json").write_bytes(b"blocked job")
    public = tmp_path / "daily-picks.json"
    public.write_bytes(b"last verified snapshot")
    runtime = {"started": False, "closed": False}
    @contextmanager
    def lock(path):
        yield
    @asynccontextmanager
    async def hung_launch(profile, *, headless=True):
        assert headless is False
        runtime["started"] = True
        try:
            # Simulate a driver launch that never reaches its context return.
            await asyncio.Future()
            yield None
        finally:
            runtime["closed"] = True
    async def reject_publish(*args):
        raise AssertionError("A manual browser must never reach publication")
    monkeypatch.setattr(worker.publisher, "snapshot_path", lambda: public)
    monkeypatch.setattr(worker, "worker_lock", lock)
    monkeypatch.setattr(worker, "ordinary_browser", hung_launch)
    monkeypatch.setattr(worker.publisher, "generate", reject_publish)
    result = await asyncio.to_thread(worker.main, ["--open-browser", "--source-url", item()["source_url"],
                                                  "--budget-seconds", "1"])
    assert result == 1 and runtime == {"started": True, "closed": True}
    status = json.loads((tmp_path / "browser-picks" / "manual-status.json").read_text())
    assert status["status"] == "failed" and status["error_type"] == "TimeoutError"
    assert status["mode"] == "manual_browser" and status["published"] is False
    assert public.read_bytes() == b"last verified snapshot"
    assert (root / "last-run.json").read_bytes() == b"blocked job"


def test_valid_todays_30_skip_candidate_export_and_browser(tmp_path, monkeypatch):
    @contextmanager
    def lock(path):
        path.mkdir(parents=True, exist_ok=True)
        yield
    async def reject_pipeline(*args):
        raise AssertionError("An existing complete edition needs no source visits")
    monkeypatch.setattr(worker.publisher, "snapshot_path", lambda: tmp_path / "daily-picks.json")
    monkeypatch.setattr(worker, "worker_lock", lock)
    monkeypatch.setattr(worker, "already_published", lambda *args: True)
    monkeypatch.setattr(worker, "pipeline", reject_pipeline)
    assert worker.main([]) == 0
    status = json.loads((tmp_path / "daily-picks-browser" / "last-run.json").read_text())
    assert status["status"] == "already_published" and status["published"] is False


@pytest.mark.parametrize("argv", [["--check-limit", "1"], ["--check-limit", "91", "--dry-run"], ["--budget-seconds", "1801"], ["--open-browser"], ["--source-url", item()["source_url"]]])
def test_cli_rejects_unbounded_or_ambiguous_modes(argv):
    with pytest.raises(SystemExit) as raised:
        worker.main(argv)
    assert raised.value.code == 2
