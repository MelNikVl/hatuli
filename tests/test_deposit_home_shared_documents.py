"""Common Home documents govern all retail products, using mocked sources only."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json

import pytest

from bot.core import deposit_sources as source
from bot.core.deposit_comparison import public_deposit_catalog
from bot.public_comparison_routes import _deposit_changes
from scripts.update_deposit_catalog import build_catalog, validate_home_documents
from tests.test_deposit_bereke_review import current as bereke_html
from tests.test_deposit_home_review import home_plus_html, home_simple_html
from tests.test_deposit_sources import NOW, forte_html, freedom_html, table


HOME_DOCUMENTS = tuple(url for url in source.DOCUMENT_SOURCES
                       if source.DOCUMENT_SOURCES[url] == source.HOME_PLUS)
HOME_IDS = {"home-simple-12", "home-simple-13", "home-savings-6", "home-savings-12",
            "home-savings-24", "home-savings-plus-3", "home-savings-plus-6", "home-savings-plus-12"}
DOCUMENT_NAMES = ("Договор банковского обслуживания", "Типовые тарифы", "Уведомление КФГД")


def with_documents(html):
    values = []
    for name, url in zip(DOCUMENT_NAMES, HOME_DOCUMENTS):
        index = len(values)
        values.extend([{"name": index + 1, "url": index + 2, "extension": index + 3},
                       name, url.replace("https://home.kz/strapi", ""), "PDF"])
    return html.replace("</html>", '<script id="__NUXT_DATA__" type="application/json">'
                        + json.dumps(values, ensure_ascii=False) + "</script></html>")


def home_savings_html():
    html = table([
        ["Срок депозита", "6 месяцев", "12 месяцев", "24 месяца"],
        ["Номинальная ставка", "16,55%", "13,98%", "9,53%"],
        ["Годовая эффективная ставка", "18,00%", "15,00%", "10,00%"],
        ["Валюта депозита", "теңге"], ["Минимальная сумма", "100 000 ₸"],
        ["Капитализация", "ежедневная"],
    ])
    return html.replace("</table>", '<tr><td>Максимальная сумма депозита</td>'
                        '<td colspan="2">без ограничений</td><td>100 000 000 ₸</td></tr></table>')


def fetched():
    data = {source.FREEDOM: {"text": freedom_html()}, source.FORTE: {"text": forte_html()},
            source.BEREKE: {"text": bereke_html()},
            source.HOME_SIMPLE: {"text": with_documents(home_simple_html())},
            source.HOME_SAVINGS: {"text": with_documents(home_savings_html())},
            source.HOME_PLUS: {"text": with_documents(home_plus_html())}}
    data.update({url: {"content": b"%PDF-previously-reviewed-fixture"} for url in source.DOCUMENT_SOURCES})
    return data


def home_offers(catalog):
    return [item for item in catalog["offers"] if item["bank_key"] == "home_credit"]


def foreign_offers(catalog):
    return [item for item in catalog["offers"] if item["bank_key"] != "home_credit"]


def test_dependency_helper_preserves_single_bank_documents_and_expands_common_home_documents():
    for url, primary in source.DOCUMENT_SOURCES.items():
        expected = source.HOME_PRODUCTS if primary == source.HOME_PLUS else (primary,)
        assert source.document_products(url) == expected
    assert source.document_products("https://unknown.test/conditions.pdf") == ()


@pytest.mark.parametrize("document_url", HOME_DOCUMENTS)
def test_changed_common_document_excludes_all_eight_home_programs_without_affecting_other_banks(document_url):
    data = fetched()
    baseline = build_catalog({}, data, NOW)
    assert {item["id"] for item in home_offers(baseline)} == HOME_IDS
    assert all(item["status"] == "verified" for item in baseline["offers"])
    data[document_url] = {"content": b"%PDF-real-changed-contract-fixture"}
    checked = NOW + timedelta(hours=6)
    updated = build_catalog(baseline, data, checked)
    control = build_catalog(baseline, fetched(), checked)
    assert {item["id"] for item in home_offers(updated)} == HOME_IDS
    assert all(item["status"] == "needs_review" and item["checked_at"] == NOW.isoformat()
               for item in home_offers(updated))
    assert foreign_offers(updated) == foreign_offers(control)
    assert [item["kind"] for item in updated["changes"]] == ["document_changed"]
    assert updated["changes"][0]["url"] == document_url
    public = public_deposit_catalog(updated, checked)
    assert [item["eligible"] for item in public["banks"]] == [True, True, True, False]
    assert public["benchmark"]["verified_bank_count"] == 3


@pytest.mark.parametrize("document_url", HOME_DOCUMENTS)
def test_pending_shared_review_persists_after_retry_and_document_fetch_failure(document_url):
    data = fetched()
    baseline = build_catalog({}, data, NOW)
    accepted_source = next(item for item in baseline["sources"] if item["url"] == document_url)
    changed_bytes = b"%PDF-changed-shared-terms-fixture"
    data[document_url] = {"content": changed_bytes}
    first = build_catalog(baseline, data, NOW + timedelta(hours=6))
    retry = build_catalog(first, data, NOW + timedelta(hours=12))
    assert retry["changes"] == first["changes"]
    failed_data = deepcopy(data)
    failed_data[document_url] = {"error": "HTTP 503"}
    failed = build_catalog(retry, failed_data, NOW + timedelta(hours=18))
    recovered = build_catalog(failed, data, NOW + timedelta(hours=24))
    assert recovered["changes"] == first["changes"]
    for catalog in (first, retry, failed, recovered):
        assert all(item["status"] == "needs_review" for item in home_offers(catalog))
        document = next(item for item in catalog["sources"] if item["url"] == document_url)
        assert document["source_hash"] == accepted_source["source_hash"]
        assert document["checked_at"] == accepted_source["checked_at"]
        assert document["pending_source_hash"] == hashlib.sha256(changed_bytes).hexdigest()
        assert document["review_required"] is True


@pytest.mark.parametrize("product_url", source.HOME_PRODUCTS)
def test_failed_product_page_cannot_leave_its_old_offers_verified_when_shared_document_changed(product_url):
    data = fetched()
    baseline = build_catalog({}, data, NOW)
    data[HOME_DOCUMENTS[0]] = {"content": b"%PDF-real-changed-contract-fixture"}
    data[product_url] = {"error": "HTTP 503"}
    updated = build_catalog(baseline, data, NOW + timedelta(hours=6))
    assert all(item["status"] != "verified" and item["checked_at"] == NOW.isoformat()
               for item in home_offers(updated))
    assert all(item["status"] == "unavailable" for item in home_offers(updated)
               if item["source_url"] == product_url)
    assert all(item["status"] == "needs_review" for item in home_offers(updated)
               if item["source_url"] != product_url)
    assert not public_deposit_catalog(updated, NOW + timedelta(hours=6))["banks"][3]["eligible"]
    data[product_url] = fetched()[product_url]
    recovered = build_catalog(updated, data, NOW + timedelta(hours=12))
    assert all(item["status"] == "needs_review" for item in home_offers(recovered))
    assert recovered["changes"] == updated["changes"]


@pytest.mark.parametrize("product_url", source.HOME_PRODUCTS)
@pytest.mark.parametrize("document_url", HOME_DOCUMENTS)
def test_each_home_product_checks_current_document_links(product_url, document_url):
    data = fetched()
    baseline = build_catalog({}, data, NOW)
    html = data[product_url]["text"]
    validate_home_documents(product_url, html)
    data[product_url]["text"] = html.replace(document_url.replace("https://home.kz/strapi", ""),
                                             "/uploads/new-unknown-document.pdf")
    with pytest.raises(ValueError, match="Изменились ссылки"):
        validate_home_documents(product_url, data[product_url]["text"])
    updated = build_catalog(baseline, data, NOW + timedelta(hours=6))
    assert all(item["status"] == "unavailable" for item in home_offers(updated)
               if item["source_url"] == product_url)
    assert all(item["status"] == "verified" for item in home_offers(updated)
               if item["source_url"] != product_url)


def test_a_new_pending_document_hash_creates_one_new_event_without_accepting_either_version():
    baseline = build_catalog({}, fetched(), NOW)
    data = fetched()
    data[HOME_DOCUMENTS[0]] = {"content": b"%PDF-new-version-one"}
    first = build_catalog(baseline, data, NOW + timedelta(hours=6))
    data[HOME_DOCUMENTS[0]] = {"content": b"%PDF-new-version-two"}
    second = build_catalog(first, data, NOW + timedelta(hours=12))
    retry = build_catalog(second, data, NOW + timedelta(hours=18))
    assert len(first["changes"]) == 1 and len(second["changes"]) == 2
    assert retry["changes"] == second["changes"]
    assert all(item["status"] == "needs_review" for item in home_offers(retry))


def test_accepting_only_a_document_hash_cannot_clear_pending_product_reviews():
    baseline = build_catalog({}, fetched(), NOW)
    data = fetched()
    data[HOME_DOCUMENTS[0]] = {"content": b"%PDF-changed-contract-fixture"}
    pending = build_catalog(baseline, data, NOW + timedelta(hours=6))
    hash_only = deepcopy(pending)
    document = next(item for item in hash_only["sources"] if item["url"] == HOME_DOCUMENTS[0])
    document.update(status="verified", source_hash=document.pop("pending_source_hash"), review_required=False)
    refreshed = build_catalog(hash_only, data, NOW + timedelta(hours=12))
    assert all(item["status"] == "needs_review" for item in home_offers(refreshed))
    assert not public_deposit_catalog(refreshed, NOW + timedelta(hours=12))["banks"][3]["eligible"]


def test_catalog_recording_completed_field_and_document_review_can_resume_normal_monitoring():
    baseline = build_catalog({}, fetched(), NOW)
    data = fetched()
    data[HOME_DOCUMENTS[0]] = {"content": b"%PDF-fully-reviewed-new-contract-fixture"}
    pending = build_catalog(baseline, data, NOW + timedelta(hours=6))
    reviewed_at = NOW + timedelta(hours=7)
    # Represents an external, completed manual review and atomic publication,
    # not an automatic approval mechanism. Fixture rates/terms are checked too.
    reviewed_values = build_catalog({}, data, reviewed_at)
    assert {item["id"] for item in home_offers(reviewed_values)} == HOME_IDS
    for item in home_offers(reviewed_values):
        assert item["currency"] == "KZT" and item["rate_type"] == "fixed"
        assert item["capitalization"] == "ежедневная"
        if item["term_months"] == 12:
            assert item["nominal_pct"] == 13.98 and item["effective_pct"] == 15
        if item["source_url"] == source.HOME_SIMPLE:
            assert item["can_top_up"] and item["can_withdraw"]
            assert item["min_amount"] == 1000 and item["max_amount"] is None
            assert item["deposit_type"] == "other" and item["guarantee_limit"] == 10_000_000
        else:
            assert not item["can_withdraw"] and item["deposit_type"] == "savings"
            assert item["guarantee_limit"] == 20_000_000
            assert item["can_top_up"] == (item["source_url"] == source.HOME_PLUS)
        assert item["early_termination"]
    reviewed = deepcopy(pending)
    reviewed["offers"] = foreign_offers(pending) + home_offers(reviewed_values)
    reviewed_document = next(item for item in reviewed_values["sources"] if item["url"] == HOME_DOCUMENTS[0])
    reviewed["sources"] = [item for item in pending["sources"] if item["url"] != HOME_DOCUMENTS[0]] + [reviewed_document]
    assert reviewed["changes"] == pending["changes"]
    assert "проверено после" in _deposit_changes(reviewed)[0]["label"]
    refreshed = build_catalog(reviewed, data, NOW + timedelta(hours=12))
    assert all(item["status"] == "verified" for item in home_offers(refreshed))
    assert refreshed["changes"] == pending["changes"]
    home = public_deposit_catalog(refreshed, NOW + timedelta(hours=12))["banks"][3]
    assert home["eligible"] and home["term_months"] == 12 and home["effective_pct"] == 15
    assert all("vip" not in item["id"].lower() for item in home_offers(refreshed))
