"""Source traversal, persistence and deadline regressions without a real DB."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.core import apartment_parser as parser
from bot.core.sale_sweep import (SaleSweep, STATE_KEY, COMPLETED_KEY, plan_batch, completed_evidence,
                                 acknowledged_prefix, recent_unsaved_source_ids)
from service_apartments import _collect_sale_catalog, _commit_sale_catalog

NOW = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_shared_cooldown(monkeypatch, tmp_path):
    monkeypatch.setenv('HATULI_DETAIL_COOLDOWN_PATH', str(tmp_path / 'source-cooldown.json'))


def receipt(page, *, count=20, total=60, terminal=False, error=None, blocked=False):
    ids = [str(100000 + (page - 1) * 20 + i) for i in range(count)]
    return {"page": page, "ids": ids, "accepted_ids": ids,
            "checked_at": NOW.isoformat(), "total_count": total,
            "expected_last_page": (total + 19) // 20,
            "terminal_reason": "observed_catalog_count" if terminal else None,
            "error": error, "blocked": blocked}


def completed_journal(*, pages=3, total=60, per_page=20, started=None, completed=None):
    state = SaleSweep.new(started or NOW - timedelta(hours=20))
    reports = [receipt(page, count=per_page, total=total, terminal=page == pages) for page in range(1, pages + 1)]
    state.account(reports, [i for r in reports for i in r["accepted_ids"]], now=completed or NOW)
    return state


def test_partial_batch_keeps_failed_page_for_retry_and_restarts_durably():
    state = SaleSweep.new(NOW)
    reports = [receipt(1), receipt(2), receipt(3, error="timeout")]
    state.account(reports, [i for r in reports for i in r["accepted_ids"]], now=NOW)
    restored = SaleSweep.loads(state.dumps())
    assert restored.next_page == 3
    assert len(restored.unique_ids) == 40
    assert restored.failures["3"]["attempts"] == 1
    assert not restored.closed and restored.completed_at is None
    retry = receipt(3, terminal=True)
    restored.account([retry], retry["accepted_ids"], now=NOW + timedelta(hours=1))
    assert restored.closed and restored.completed_at
    assert len(restored.unique_ids) == 60 and not restored.failures
    assert restored.failed_attempts == 1  # historical errors remain visible
    assert restored.pages["3"]["retry_attempts"] == 1


def test_database_partial_save_cannot_advance_or_close_circle():
    state = SaleSweep.new(NOW)
    reports = [receipt(1), receipt(2), receipt(3, terminal=True)]
    missing = reports[1]["accepted_ids"][0]
    saved = {i for r in reports for i in r["accepted_ids"]} - {missing}
    state.account(reports, saved, now=NOW)
    assert state.next_page == 2 and not state.closed
    assert state.failures["2"]["last_error"] == "incomplete_database_save"
    assert state.failures["2"]["missing_saved_ids"] == [missing]
    assert len(state.unique_ids) == 40
    state.account([receipt(2)], receipt(2)["accepted_ids"], now=NOW + timedelta(minutes=5))
    assert state.closed and len(state.unique_ids) == 60


def test_unique_raw_ids_include_unpriced_cards_but_not_duplicate_apartments_claim():
    state = SaleSweep.new(NOW)
    report = receipt(1, count=20, total=20, terminal=True)
    report["accepted_ids"] = report["ids"][:-1]
    report["rejected"] = {"missing_price": 1}
    state.account([report], report["accepted_ids"], now=NOW)
    assert len(state.unique_ids) == 20 and len(state.saved_ids) == 19
    assert state.pages["1"]["rejected"] == {"missing_price": 1}
    assert state.coverage_pct == 100
    assert recent_unsaved_source_ids({COMPLETED_KEY: state.dumps()}, cutoff=NOW - timedelta(days=7), now=NOW) == [report["ids"][-1]]


def test_old_or_future_unpriced_observation_does_not_protect_an_archive_candidate():
    state = SaleSweep.new(NOW - timedelta(days=8))
    report = receipt(1)
    report["accepted_ids"] = []
    state.account([report], [], now=NOW)
    state.pages["1"]["checked_at"] = (NOW - timedelta(days=8)).isoformat()
    state.recent_unsaved = {}
    assert recent_unsaved_source_ids({STATE_KEY: state.dumps()}, cutoff=NOW - timedelta(days=7), now=NOW) == []
    state.pages["1"]["checked_at"] = (NOW + timedelta(days=1)).isoformat()
    assert recent_unsaved_source_ids({STATE_KEY: state.dumps()}, cutoff=NOW - timedelta(days=7), now=NOW) == []


@pytest.mark.parametrize('failure', [False, True])
def test_head_refresh_preserves_recent_unpriced_or_failed_save_ids_without_rewriting_traversal(failure):
    state = SaleSweep.new(NOW - timedelta(hours=1))
    first = receipt(1, count=1)
    state.account([first], first['accepted_ids'], now=NOW)
    observed = {**receipt(1, count=1), 'ids': ['999999'], 'accepted_ids': ['999999'] if failure else []}
    state.account([observed], [], now=NOW)
    restored = SaleSweep.loads(state.dumps())
    assert restored.unique_ids == set(first['ids'])
    assert recent_unsaved_source_ids({STATE_KEY: restored.dumps()}, cutoff=NOW - timedelta(days=7), now=NOW) == ['999999']
    assert restored.recent_unsaved['999999'] == NOW.isoformat()
    later = {**observed, 'ids': ['888888'], 'accepted_ids': ['888888'] if failure else []}
    restored.account([later], [], now=NOW)
    assert recent_unsaved_source_ids({STATE_KEY: restored.dumps()}, cutoff=NOW - timedelta(days=7), now=NOW) == ['888888', '999999']


def test_retry_with_changed_source_ids_retains_recent_presence_until_ttl_expires():
    state = SaleSweep.new(NOW)
    failed = {**receipt(2, count=1), 'ids': ['999999'], 'accepted_ids': ['999999']}
    state.account([failed], [], now=NOW)
    replacement = {**receipt(2, count=1), 'ids': ['888888'], 'accepted_ids': ['888888']}
    state.account([replacement], ['888888'], now=NOW)
    assert recent_unsaved_source_ids({STATE_KEY: state.dumps()}, cutoff=NOW - timedelta(days=7), now=NOW) == ['999999']
    state.account([], [], now=NOW + timedelta(days=8))
    assert recent_unsaved_source_ids({STATE_KEY: state.dumps()}, cutoff=NOW + timedelta(days=1), now=NOW + timedelta(days=8)) == []


def test_repeat_without_terminal_evidence_is_not_another_page():
    state = SaleSweep.new(NOW)
    first = receipt(1)
    duplicate = {**first, "page": 2}
    state.account([first, duplicate], first["accepted_ids"], now=NOW)
    assert state.next_page == 2 and "2" in state.failures
    assert len(state.unique_ids) == 20


def test_current_source_counter_growth_is_not_hidden_by_original_snapshot():
    state = SaleSweep.new(NOW)
    first = receipt(1, total=40)
    state.account([first], first["accepted_ids"], now=NOW)
    second = receipt(2, total=60)
    state.account([second], second["accepted_ids"], now=NOW)
    assert state.expected_last_page == 3 and state.total_start == 40 and state.total_latest == 60
    assert state.coverage_pct == 66.67 and not state.closed


def test_shrinking_catalog_does_not_erase_original_denominator():
    state = SaleSweep.new(NOW)
    first = receipt(1, total=100)
    second = receipt(2, total=40, terminal=True)
    state.account([first, second], first["accepted_ids"] + second["accepted_ids"], now=NOW)
    assert state.closed and state.denominator == 100 and state.coverage_pct == 40


def test_new_head_growth_invalidates_earlier_terminal_proof_while_retry_pending():
    state = SaleSweep.new(NOW)
    reports = [receipt(1, total=40, error="timeout"), receipt(2, total=40, terminal=True)]
    state.account(reports, reports[1]["accepted_ids"], now=NOW)
    assert state.terminal_page == 2 and not state.closed
    head = receipt(1, total=60)
    state.account([head], head["accepted_ids"], now=NOW + timedelta(minutes=1))
    assert state.terminal_page is None and not state.closed
    assert state.next_page == 3


def test_head_refresh_cannot_truncate_existing_sweep_or_replace_earlier_evidence():
    state = SaleSweep.new(NOW)
    first = receipt(1)
    state.account([first], first["accepted_ids"], now=NOW)
    empty_head = {**receipt(1, count=0, total=0, terminal=True), "terminal_reason": "explicit_empty"}
    state.account([empty_head], [], now=NOW + timedelta(minutes=10))
    assert not state.closed and state.next_page == 2
    assert len(state.unique_ids) == 20 and state.denominator == 60


@pytest.mark.parametrize("raw", ["{}", "not json", '{"version":2}', '{"version":1,"pages":{}}'])
def test_unknown_or_corrupt_legacy_state_is_not_evidence(raw):
    assert SaleSweep.loads(raw) is None


def test_corrupt_completed_journal_cannot_approve_coverage():
    state = completed_journal()
    data = json.loads(state.dumps())
    del data["pages"]["2"]
    assert completed_evidence({COMPLETED_KEY: json.dumps(data)}) is None
    data = json.loads(state.dumps())
    data["pages"]["1"]["ids"] = ["999999"]
    assert SaleSweep.loads(json.dumps(data)) is None


def sale_html(page, *, total=60, unpriced=False):
    ids = [100000 + (page - 1) * 20 + i for i in range(20)]
    cards = "".join(f'<div class="a-card" data-id="{i}"><a class="a-card__title" href="/a/show/{i}">'
                    'Квартира 2-комнатная, 60 м²</a>'
                    + ('' if unpriced else '<div class="a-card__price">30 000 000 ₸</div>')
                    + '</div>' for i in ids)
    return f'<h1>Найдено {total} объявлений</h1><div class="a-list a-search-list">{cards}</div>'


def mock_search(monkeypatch, serve):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(parser, "datetime", Clock)
    original = httpx.AsyncClient
    monkeypatch.setattr(parser.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(serve), **kw))
    monkeypatch.setattr(parser.asyncio, "sleep", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("status,html,blocked", [
    (468, "blocked", True), (429, "rate limited", True), (403, "denied", True),
    (200, "<h1>Подтвердите, что вы не робот</h1>", True),
    (200, '<div class="a-list"></div>', False), (500, "failure", False),
])
async def test_source_failure_stops_at_first_page_without_false_eof(monkeypatch, status, html, blocked):
    requested = []
    def serve(request):
        requested.append(request)
        return httpx.Response(status, text=html)
    mock_search(monkeypatch, serve)
    stats = {}
    assert await parser.parse_apartments_for_sale(max_pages=160, stats=stats) == []
    assert len(requested) == 1 and stats["pages_failed"] == 1
    assert not stats["reached_end"] and stats["blocked"] is blocked
    assert stats["page_reports"][0]["error"]


@pytest.mark.asyncio
async def test_parser_returns_successful_prefix_and_retry_receipt(monkeypatch):
    requested = []
    def serve(request):
        page = int(request.url.params.get("page", 1))
        requested.append(page)
        if page == 2:
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(200, text=sale_html(page))
    mock_search(monkeypatch, serve)
    stats = {}
    started_requests = parser.REQUEST_COUNTS["search"]
    rows = await parser.parse_apartments_for_sale(max_pages=160, stats=stats)
    state = SaleSweep.new(NOW)
    state.account(stats["page_reports"], [r["id"] for r in rows], now=NOW)
    assert requested == [1, 2] and len(rows) == 20
    assert state.next_page == 2 and not state.closed
    assert stats["pages_ok"] == 1 and stats["pages_failed"] == 1
    assert parser.REQUEST_COUNTS["search"] == started_requests + 2  # includes timeout


@pytest.mark.asyncio
async def test_valid_counter_and_cards_prove_last_page_even_when_every_card_is_unpriced(monkeypatch):
    requested = []
    def serve(request):
        requested.append(request)
        return httpx.Response(200, text=sale_html(1, total=20, unpriced=True))
    mock_search(monkeypatch, serve)
    stats = {}
    rows = await parser.parse_apartments_for_sale(max_pages=10, stats=stats)
    state = SaleSweep.new(NOW)
    state.account(stats["page_reports"], [], now=NOW)
    assert rows == [] and len(requested) == 1 and stats["reached_end"]
    assert state.closed and state.coverage_pct == 100 and not state.saved_ids
    assert state.pages["1"]["rejected"] == {"missing_price": 20}


@pytest.mark.asyncio
async def test_pagination_saying_more_overrides_rounded_catalog_count(monkeypatch):
    html = sale_html(1, total=20) + '<nav class="paginator"><a href="?page=2">2</a></nav>'
    mock_search(monkeypatch, lambda request: httpx.Response(200, text=html))
    stats = {}
    await parser.parse_apartments_for_sale(max_pages=1, stats=stats)
    assert not stats["reached_end"]
    assert stats["page_reports"][0]["expected_last_page"] == 2


@pytest.mark.asyncio
async def test_nested_recommendations_are_not_main_catalog_cards(monkeypatch):
    html = '<div class="a-list a-search-list"></div>' + sale_html(1)
    # The first real search container is empty and no explicit empty proof
    # exists; recommendations elsewhere cannot turn it into a success.
    mock_search(monkeypatch, lambda request: httpx.Response(200, text=html))
    stats = {}
    assert await parser.parse_apartments_for_sale(max_pages=1, stats=stats) == []
    assert stats["pages_failed"] == 1 and not stats["reached_end"]


class Settings:
    def __init__(self, values=None):
        self.values = {"PARSER_MAX_PAGES": "1", "DEEP_SWEEP_BATCH": "2", "DEEP_SWEEP_BATCH_MAX": "160",
                       "DEEP_SWEEP_CIRCLE_TARGET_HOURS": "24", **(values or {})}
    def get(self, key, default=None):
        return self.values.get(key, default)
    def get_int(self, key, default=0):
        return int(float(self.get(key, default)))
    def get_float(self, key, default=0):
        return float(self.get(key, default))
    async def set(self, key, value):
        self.values[key] = str(value)


@pytest.mark.asyncio
async def test_real_collect_commit_retries_missing_page_and_refuses_legacy_completion(monkeypatch):
    settings = Settings({"DEEP_SWEEP_PAGE": "294", "DEEP_SWEEP_CIRCLE_MAX_PAGE": "1906",
                         "DEEP_SWEEP_CIRCLE_COMPLETED_PAGES": "1919"})
    requests = []
    failed = False
    def serve(request):
        nonlocal failed
        page = int(request.url.params.get("page", 1))
        requests.append(page)
        if page == 2 and not failed:
            failed = True
            return httpx.Response(500, text="temporary")
        return httpx.Response(200, text=sale_html(page))
    mock_search(monkeypatch, serve)
    rows, state, reports, stats, plan = await _collect_sale_catalog(
        settings, parser.parse_apartments_for_sale, now=NOW)
    assert requests == [1, 2] and state.next_page == 1  # not committed yet
    assert COMPLETED_KEY not in settings.values
    await _commit_sale_catalog(settings, state, reports, [r["id"] for r in rows], stats, plan, now=NOW + timedelta(minutes=1))
    assert settings.values["DEEP_SWEEP_PAGE"] == "2"
    assert SaleSweep.loads(settings.values[STATE_KEY]).failures["2"]["last_error"] == "http_500"
    rows, state, reports, stats, plan = await _collect_sale_catalog(
        settings, parser.parse_apartments_for_sale, now=NOW + timedelta(hours=1))
    assert requests == [1, 2, 1, 2, 3]
    await _commit_sale_catalog(settings, state, reports, [r["id"] for r in rows], stats, plan, now=NOW + timedelta(hours=1, minutes=1))
    proof = completed_evidence(settings.values)
    assert proof.closed and len(proof.unique_ids) == 60
    assert settings.values["DEEP_SWEEP_CIRCLE_COMPLETED_PAGES"] == "3"


@pytest.mark.asyncio
async def test_block_cooldown_prevents_all_next_cycle_source_requests(monkeypatch):
    settings = Settings()
    mock_search(monkeypatch, lambda request: httpx.Response(468, text="blocked"))
    rows, state, reports, stats, plan = await _collect_sale_catalog(settings, parser.parse_apartments_for_sale, now=NOW)
    assert stats["blocked"]
    await _commit_sale_catalog(settings, state, reports, [], stats, plan, now=NOW)
    analyze = AsyncMock(side_effect=AssertionError("must not request during cooldown"))
    result = await _collect_sale_catalog(settings, analyze, now=NOW + timedelta(minutes=29))
    assert result[3]["paused"] and not analyze.called
    assert SaleSweep.loads(settings.values[STATE_KEY]).next_page == 1


@pytest.mark.asyncio
async def test_smaller_live_catalog_probes_its_real_last_page_instead_of_skipping_or_looping_past_end(monkeypatch):
    state = SaleSweep.new(NOW - timedelta(hours=2))
    prior = [receipt(1), receipt(2)]
    for report in prior:
        report['checked_at'] = (NOW - timedelta(hours=1)).isoformat()
    state.account(prior, [i for p in prior for i in p['accepted_ids']], now=NOW - timedelta(hours=1))
    settings = Settings({STATE_KEY: state.dumps()})
    requested = []
    def serve(request):
        page = int(request.url.params.get('page', 1))
        requested.append(page)
        assert page <= 2, 'must not request a page beyond the observed smaller catalog'
        return httpx.Response(200, text=sale_html(page, total=40))
    mock_search(monkeypatch, serve)
    rows, state, reports, stats, plan = await _collect_sale_catalog(settings, parser.parse_apartments_for_sale, now=NOW)
    assert requested == [1, 2]
    await _commit_sale_catalog(settings, state, reports, [r['id'] for r in rows], stats, plan, now=NOW + timedelta(minutes=1))
    proof = completed_evidence(settings.values)
    assert proof.closed and proof.terminal_receipt['page'] == 2
    assert proof.coverage_pct == 66.67  # retain the initial larger denominator


@pytest.mark.asyncio
async def test_empty_head_without_zero_counter_is_not_terminal_proof(monkeypatch):
    mock_search(monkeypatch, lambda request: httpx.Response(200, text='<div class="a-list-empty">Ничего не найдено</div>'))
    stats = {}
    assert await parser.parse_apartments_for_sale(max_pages=10, stats=stats) == []
    assert stats['pages_failed'] == 1 and not stats['reached_end']
    assert stats['page_reports'][0]['error'] == 'unconfirmed_empty_head'


@pytest.mark.asyncio
async def test_sparse_database_retry_does_not_refetch_already_saved_pages(monkeypatch):
    state = SaleSweep.new(NOW - timedelta(hours=2))
    reports = [receipt(1), receipt(2), receipt(3, terminal=True)]
    for report in reports:
        report['checked_at'] = (NOW - timedelta(hours=1)).isoformat()
    saved = {i for r in reports for i in r['accepted_ids']} - {reports[1]['accepted_ids'][0]}
    state.account(reports, saved, now=NOW - timedelta(hours=1))
    settings = Settings({STATE_KEY: state.dumps()})
    requested = []
    def serve(request):
        page = int(request.url.params.get('page', 1))
        requested.append(page)
        return httpx.Response(200, text=sale_html(page))
    mock_search(monkeypatch, serve)
    rows, state, receipts, stats, plan = await _collect_sale_catalog(settings, parser.parse_apartments_for_sale, now=NOW)
    assert requested == [1, 2]
    await _commit_sale_catalog(settings, state, receipts, [r['id'] for r in rows], stats, plan, now=NOW + timedelta(minutes=1))
    assert completed_evidence(settings.values).closed


@pytest.mark.asyncio
async def test_shared_durable_source_block_prevents_sale_head_request_after_restart():
    from bot.core.detail_cooldown import record_block
    record_block(now=NOW)
    analyze = AsyncMock(side_effect=AssertionError('must respect the shared source block'))
    result = await _collect_sale_catalog(Settings(), analyze, now=NOW + timedelta(minutes=1))
    assert result[3]['paused'] and not analyze.called


@pytest.mark.asyncio
async def test_new_round_carries_recent_unsaved_presence_from_last_completed_journal():
    proof = completed_journal()
    proof.recent_unsaved = {'999999': NOW.isoformat()}
    settings = Settings({STATE_KEY: proof.dumps(), COMPLETED_KEY: proof.dumps()})
    result = await _collect_sale_catalog(settings, AsyncMock(return_value=[]), now=NOW + timedelta(hours=1))
    assert result[1].recent_unsaved == {'999999': NOW.isoformat()}


@pytest.mark.asyncio
async def test_corrupt_shared_source_block_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / 'bad-cooldown.json'
    path.write_text('not json', encoding='utf-8')
    monkeypatch.setenv('HATULI_DETAIL_COOLDOWN_PATH', str(path))
    analyze = AsyncMock(side_effect=AssertionError('must not ignore a broken shared cooldown'))
    result = await _collect_sale_catalog(Settings(), analyze, now=NOW)
    assert result[3]['paused'] and not analyze.called


@pytest.mark.asyncio
async def test_retry_after_longer_than_local_cooldown_is_honored(monkeypatch):
    settings = Settings()
    mock_search(monkeypatch, lambda request: httpx.Response(429, headers={"Retry-After": "7200"}, text="limited"))
    rows, state, reports, stats, plan = await _collect_sale_catalog(settings, parser.parse_apartments_for_sale, now=NOW)
    await _commit_sale_catalog(settings, state, reports, [], stats, plan, now=NOW)
    assert SaleSweep.loads(settings.values[STATE_KEY]).blocked_until == (NOW + timedelta(hours=2)).isoformat()


@pytest.mark.asyncio
async def test_partial_card_parse_is_a_retry_not_a_successful_empty_batch(monkeypatch):
    html = '<div class="a-card" data-id="100000"><a href="/a/show/100000">Changed title class</a></div>'
    mock_search(monkeypatch, lambda request: httpx.Response(200, text=html))
    stats = {}
    assert await parser.parse_apartments_for_sale(max_pages=5, stats=stats) == []
    assert stats["pages_failed"] == 1 and not stats["reached_end"]
    assert stats["page_reports"][0]["error"] == "incomplete_card_parse"


@pytest.mark.asyncio
async def test_redirect_to_another_city_is_not_astana_coverage(monkeypatch):
    def serve(request):
        if request.url.path.endswith("/astana/"):
            return httpx.Response(302, headers={"Location": "/prodazha/kvartiry/almaty/"})
        return httpx.Response(200, text=sale_html(1, total=20))
    mock_search(monkeypatch, serve)
    stats = {}
    assert await parser.parse_apartments_for_sale(max_pages=10, stats=stats) == []
    assert stats["pages_failed"] == 1 and not stats["reached_end"]
    assert stats["page_reports"][0]["error"] == "redirected_search_scope_or_page"


@pytest.mark.parametrize("failed_stage", ["source", "database"])
def test_manual_sweep_keeps_first_unacknowledged_page(failed_stage):
    reports = [receipt(6), receipt(7, error="timeout" if failed_stage == "source" else None)]
    saved = reports[0]["accepted_ids"]
    assert acknowledged_prefix(reports, saved, start_page=6) == (7, False)


def test_manual_sweep_cannot_close_after_failed_persistence_of_last_page():
    report = receipt(3, terminal=True)
    assert acknowledged_prefix([report], [], start_page=3) == (3, False)
    assert acknowledged_prefix([report], report["accepted_ids"], start_page=3) == (4, True)


def test_completed_timestamp_cannot_precede_page_observations():
    proof = completed_journal()
    data = json.loads(proof.dumps())
    data["completed_at"] = (NOW - timedelta(hours=1)).isoformat()
    assert SaleSweep.loads(json.dumps(data)) is None


def test_24_hour_plan_uses_existing_pacing_and_prices_extra_page_time():
    plan = plan_batch(base_batch=40, max_batch=160, pages_left=1906,
                      elapsed_min=0, period_min=89.6, reference_batch=40,
                      page_seconds=5, target_hours=24)
    assert 147 <= plan["batch"] <= 160
    assert plan["estimated_total_hours"] <= 24 and plan["deadline_feasible"]
    assert plan["page_seconds"] == 5
    assert plan["pacing"] == "sequential_2_to_5_second_delay_plus_response"


def test_impossible_deadline_is_reported_not_promised_or_faster_requests():
    plan = plan_batch(base_batch=40, max_batch=160, pages_left=1906,
                      elapsed_min=0, period_min=180, reference_batch=40, target_hours=24)
    assert plan["batch"] == 160
    assert not plan["deadline_feasible"] and plan["estimated_total_hours"] > 24
    assert plan["page_seconds"] == 5


@pytest.mark.parametrize("value", ["bad", float("nan"), float("inf"), -1e30])
def test_deadline_plan_bounds_untrusted_settings(value):
    plan = plan_batch(base_batch=40, max_batch=value, pages_left=1906,
                      elapsed_min=value, period_min=value, page_seconds=value, target_hours=value)
    assert 40 <= plan["batch"] <= 160
    assert 2 <= plan["page_seconds"] <= 60
