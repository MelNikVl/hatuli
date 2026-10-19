import json
from datetime import timedelta

import pytest

from bot.core.deposit_sources import DOCUMENT_SOURCES, HOME_PLUS
from scripts.update_deposit_catalog import build_catalog, validate_home_plus_documents
from tests.test_deposit_sources import NOW, home_html


def source_html():
    names = ("Договор банковского обслуживания", "Типовые тарифы", "Уведомление КФГД")
    urls = [url for url, product in DOCUMENT_SOURCES.items() if product == HOME_PLUS]
    values = []
    for name, url in zip(names, urls):
        offset = len(values)
        values.extend([{"name": offset + 1, "url": offset + 2, "extension": offset + 3},
                       name, url.replace("https://home.kz/strapi", ""), "PDF"])
    return home_html().replace("</html>", '<script id="__NUXT_DATA__" type="application/json">'
                               + json.dumps(values, ensure_ascii=False) + "</script></html>")


def fetched(html=None):
    return {HOME_PLUS: {"text": html or source_html()},
            **{url: {"content": b"%PDF-reviewed"} for url in DOCUMENT_SOURCES}}


def test_home_document_links_decode_json_without_executing_scripts():
    validate_home_plus_documents(source_html())


@pytest.mark.parametrize("changed", [
    lambda html: html.replace("__NUXT_DATA__", "other-data"),
    lambda html: html.replace("Dogovor_bankovskogo", "NEW_Dogovor_bankovskogo"),
    lambda html: html.replace("Svod_Tipovyh", "NEW_Svod_Tipovyh"),
    lambda html: html.replace("Uvedomlenie_KFGD", "NEW_Uvedomlenie_KFGD"),
])
def test_replaced_link_cannot_keep_verifying_an_old_reachable_pdf(changed):
    baseline = build_catalog({}, fetched(), NOW)
    updated = build_catalog(baseline, fetched(changed(source_html())), NOW + timedelta(hours=6))
    assert all(item["status"] == "unavailable" for item in updated["offers"])
    assert all(item["checked_at"] == NOW.isoformat() for item in updated["offers"])


@pytest.mark.parametrize("url", [url for url, product in DOCUMENT_SOURCES.items() if product == HOME_PLUS])
def test_changed_home_contract_tariff_or_guarantee_excludes_program_until_review(url):
    baseline = build_catalog({}, fetched(), NOW)
    changed = fetched()
    changed[url] = {"content": b"%PDF-new-conditions"}
    first = build_catalog(baseline, changed, NOW + timedelta(hours=6))
    again = build_catalog(first, changed, NOW + timedelta(hours=12))
    assert all(item["status"] == "needs_review" for item in first["offers"])
    assert all(item["status"] == "needs_review" for item in again["offers"])
    assert all(item["checked_at"] == NOW.isoformat() for item in again["offers"])
    assert len([item for item in again["changes"] if item["kind"] == "document_changed"]) == 1
