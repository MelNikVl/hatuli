"""Practical comparison subtopics retain the frozen evidence and archive gates."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from bot.core import analytics_articles as core
from tests.test_analytics_articles import NOW, brief_digest, brief_draft, observed_edition
from tests.test_analytics_articles_local_public_page import public_client, Markup


@pytest.fixture(autouse=True)
def deny_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_editorial_comparison_test")

    async def reject(*args, **kwargs):
        raise AssertionError("Editorial comparisons cannot connect to PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr("bot.db.pg.init_pool", reject)


def bank_edition():
    digest = brief_digest()
    keys = []
    for bank, host, rate in (("freedom", "bankffin.kz", 16.87), ("forte", "bank.forte.kz", 16.0)):
        key = "bank." + bank
        keys.append(key)
        digest["sources"][key] = {
            "kind": "official_terms", "label": f"Официальные условия банка {bank}",
            "url": f"https://{host}/deposit", "reuse_url": f"https://{host}/deposit",
            "as_of": (NOW - timedelta(minutes=5)).isoformat(), "published_at": None,
            "retrieved_at": NOW.isoformat(), "rights_status": "factual_terms_only",
        }
        digest["facts"][key] = {
            "label": "ГЭСВ проверенного годового продукта", "value": rate, "unit": "%", "n": None,
            "as_of": digest["sources"][key]["as_of"], "source_id": key,
            "comparison": {
                "topic": "banks", "entity_id": key, "entity_name": bank.title(),
                "profile_id": "kzt.twelve_month.no_additions", "profile_label": "Тенге, полный год, без дополнительных взносов",
                "metric": "twelve_month_effective_rate", "basis": "official_terms",
                "identity_verified": True, "cost_scope": "contract_rate", "horizon_months": 12,
            },
        }
    draft = brief_draft()
    draft["articles"][0]["source_ids"].extend(keys)
    draft["articles"][0]["comparisons"] = [{
        "topic": "banks", "title": "Вклады в тенге на полный год", "fact_ids": keys,
        "conclusion": "Для одинакового полного срока ставки составляют {fact:bank.freedom} и {fact:bank.forte}; это условия продуктов, а не рейтинг надёжности банков.",
        "tradeoff": "Пополнение, снятие и досрочный возврат сравнивают вместе со ставкой; условия гарантирования указаны отдельно в расчёте.",
    }]
    return core.build_edition(draft, digest, now=NOW)


def local_comparison(topic="complexes"):
    value = observed_edition()
    source = value["digest"]["sources"]["bns.housing"]
    source["as_of"] = source["published_at"] = source["retrieved_at"] = NOW.isoformat()
    for fact in value["digest"]["facts"].values():
        fact["as_of"] = NOW.isoformat()
    for index in (1, 2):
        fact = value["digest"]["facts"][f"sales.rooms_{index}"]
        fact["comparison"] = {
            "topic": topic, "entity_id": f"complex.test_{index}", "entity_name": f"ЖК Тест {index}",
            "profile_id": "secondary.two_rooms.ready", "profile_label": "Вторичная продажа: одинаковая комнатность, площадь и состояние",
            "metric": "asking_price", "basis": "observed_asking", "identity_verified": True, "cost_scope": "asking_only",
        }
    value["articles"][0]["comparisons"] = [{
        "topic": topic, "title": "Сопоставимые предложения в выбранных домах",
        "fact_ids": ["sales.rooms_1", "sales.rooms_2"],
        "conclusion": "Для первого дома наблюдаем {fact:sales.rooms_1}, для второго — {fact:sales.rooms_2}; это цены предложений сопоставимого профиля.",
        "tradeoff": "Сравнение описывает подтверждённые квартиры в выборке; более низкая цена не доказывает более высокое качество дома.",
    }]
    return value


def test_bank_terms_have_no_fabricated_release_date_sample_or_capital():
    value = core.normalize_edition(bank_edition(), now=NOW)[0]
    assert value["topics"] == [{"id": "banks", "label": "Банки"}]
    block = value["comparisons"][0]
    assert block["capital_label"] is None
    assert block["rows"][0]["value_label"] == "16,87%"
    assert all(row["n"] is None for row in block["rows"])
    assert "Расчётный сценарий" not in [row["basis_label"] for row in block["rows"]]
    assert all("Опубликовано" not in source["as_of_label"] for source in value["sources"][-2:])


@pytest.mark.parametrize("field,changed", [
    ("profile_id", "different.rooms"), ("profile_label", "Иное состояние и другая площадь"),
    ("entity_id", "complex.test_1"), ("identity_verified", False), ("cost_scope", "all_in"),
    ("metric", "monthly_rent"),
])
def test_incompatible_or_ambiguous_entities_do_not_create_public_topics(field, changed):
    value = local_comparison()
    value["digest"]["facts"]["sales.rooms_2"]["comparison"][field] = changed
    with pytest.raises(ValueError):
        core.normalize_edition(value, now=NOW)


@pytest.mark.parametrize("topic,n", [("places", 19), ("complexes", 9)])
def test_local_comparison_requires_each_independent_cohort_threshold(topic, n):
    value = local_comparison(topic)
    value["digest"]["facts"]["sales.rooms_2"]["n"] = n
    with pytest.raises(ValueError):
        core.normalize_edition(value, now=NOW)


def test_local_fact_date_and_identity_are_independent_of_publication_date():
    value = local_comparison()
    value["digest"]["sources"]["bns.housing"]["as_of"] = (NOW - timedelta(days=4)).isoformat()
    for fact in value["digest"]["facts"].values():
        fact["as_of"] = value["digest"]["sources"]["bns.housing"]["as_of"]
    with pytest.raises(ValueError, match="fresh independent"):
        core.normalize_edition(value, now=NOW)


@pytest.mark.parametrize("mutation", ["unknown_source", "uncited", "wrong_bank_host", "old_terms", "fake_n", "six_month", "nominal", "nightly_yield", "empty", "free_topics"])
def test_invalid_comparisons_fail_closed(mutation):
    value = bank_edition()
    fact = value["digest"]["facts"]["bank.freedom"]
    source = value["digest"]["sources"]["bank.freedom"]
    block = value["articles"][0]["comparisons"][0]
    if mutation == "unknown_source":
        fact["source_id"] = "bank.unknown"
    elif mutation == "uncited":
        value["articles"][0]["source_ids"].remove("bank.freedom")
    elif mutation == "wrong_bank_host":
        source["url"] = "https://bankffin.kz.example.test/official"
    elif mutation == "old_terms":
        fact["as_of"] = source["as_of"] = (NOW - timedelta(hours=25)).isoformat()
    elif mutation == "fake_n":
        fact["n"] = 30
    elif mutation == "six_month":
        fact["comparison"]["horizon_months"] = 6
    elif mutation == "nominal":
        fact["comparison"]["metric"] = "nominal_rate"
    elif mutation == "nightly_yield":
        block["topic"] = "strategies"
        fact["comparison"].update(metric="nightly_asking_rate", basis="observed_asking", cost_scope="asking_only")
        fact["unit"] = "₸/сутки"
    elif mutation == "empty":
        value["articles"][0]["comparisons"] = []
    else:
        value["articles"][0]["topics"] = ["banks"]
    with pytest.raises(ValueError):
        core.normalize_edition(value, now=NOW)


def test_bns_release_order_and_source_rights_remain_required():
    value = bank_edition()
    source = next(source for source in value["digest"]["sources"].values() if source["kind"] == "official_publication")
    source["published_at"] = None
    with pytest.raises(ValueError):
        core.normalize_edition(value, now=NOW)
    value = bank_edition()
    value["digest"]["sources"]["bank.freedom"]["rights_status"] = "official_reuse_permitted"
    with pytest.raises(ValueError):
        core.normalize_edition(value, now=NOW)


def scenario_edition():
    value = bank_edition()
    source_id = "scenario.shared"
    value["digest"]["sources"][source_id] = {
        "kind": "calculation_scenario", "label": "Явный расчётный сценарий для одинакового капитала",
        "url": "https://hatuli.ai-groundtruth.com/compare", "reuse_url": "https://hatuli.ai-groundtruth.com/how-it-works",
        "as_of": NOW.isoformat(), "published_at": NOW.isoformat(), "retrieved_at": NOW.isoformat(),
        "rights_status": "operator_authorized",
    }
    facts = value["digest"]["facts"]
    for key, amount, unit in (("scenario.capital", 40_000_000, "₸"), ("scenario.costs", 500_000, "₸"),
                              ("scenario.occupancy", 70, "%"), ("scenario.monthly_income", 250_000, "₸/мес.")):
        facts[key] = {"label": "Допущение явно указанного сценария", "value": amount, "unit": unit, "n": None,
                      "source_id": source_id, "as_of": NOW.isoformat()}
    for key, name, amount, inputs in (
        ("scenario.bank", "Вклад", 16.87, ["bank.freedom", "scenario.costs"]),
        ("scenario.monthly", "Месячная аренда", 6, ["scenario.monthly_income", "scenario.costs"]),
    ):
        facts[key] = {
            "label": "Чистый результат явно заданного сценария", "value": amount, "unit": "%", "n": None,
            "source_id": source_id, "as_of": NOW.isoformat(),
            "comparison": {"topic": "strategies", "entity_id": key, "entity_name": name,
                           "profile_id": "same.capital.twelve_month", "profile_label": "Одинаковый капитал, полный год, учтённые расходы",
                           "metric": "annual_net_yield", "basis": "scenario", "identity_verified": True,
                           "cost_scope": "net_after_costs", "horizon_months": 12,
                           "capital_fact_id": "scenario.capital", "input_fact_ids": inputs},
        }
    article = value["articles"][0]
    article["source_ids"].append(source_id)
    article["comparisons"] = [{
        "topic": "strategies", "title": "Два расчётных сценария для одинакового бюджета",
        "fact_ids": ["scenario.bank", "scenario.monthly"],
        "conclusion": "При одинаковом капитале сравнивается чистый результат заданных сценариев; это не обещание будущей доходности квартиры.",
        "tradeoff": "Расходы и занятость заданы явно, рост стоимости не включён; результаты относятся только к этим допущениям.",
    }]
    return value


def test_strategy_comparison_requires_calculated_net_result_same_capital_and_frozen_inputs():
    value = core.normalize_edition(scenario_edition(), now=NOW)[0]
    block = value["comparisons"][0]
    assert block["capital_label"] == "40 000 000 ₸"
    assert all(row["basis_label"] == "Расчётный сценарий" for row in block["rows"])
    assert value["topics"] == [{"id": "strategies", "label": "Куда вложить деньги"}]


@pytest.mark.parametrize("mutation", ["other_capital", "missing_input", "uncited_input", "actual_income", "contract_rate", "fake_scenario_n"])
def test_strategy_inputs_cannot_hide_cost_basis_or_become_observed_income(mutation):
    value = scenario_edition()
    fact = value["digest"]["facts"]["scenario.monthly"]
    if mutation == "other_capital":
        fact["comparison"]["capital_fact_id"] = "scenario.costs"
    elif mutation == "missing_input":
        fact["comparison"]["input_fact_ids"] = ["scenario.costs", "unknown.fact"]
    elif mutation == "uncited_input":
        value["articles"][0]["source_ids"].remove("bank.freedom")
    elif mutation == "actual_income":
        fact["comparison"]["basis"] = "observed_asking"
    elif mutation == "contract_rate":
        fact["comparison"].update(metric="twelve_month_effective_rate", basis="official_terms", cost_scope="contract_rate")
    else:
        fact["n"] = 30
    with pytest.raises(ValueError):
        core.normalize_edition(value, now=NOW)


def test_new_bank_checks_in_revision_use_modification_time_and_preserve_publication(tmp_path):
    import hashlib
    core.publish_edition(core.build_edition(brief_draft(), brief_digest(), now=NOW), root=tmp_path, now=NOW)
    base = core.archive_directory(tmp_path) / "2026-10-16.json"
    before = base.read_bytes()
    value = bank_edition()
    modified = NOW + timedelta(hours=2)
    value["digest"]["generated_at"] = modified.isoformat()
    for key in ("bank.freedom", "bank.forte"):
        value["digest"]["facts"][key]["as_of"] = modified.isoformat()
        value["digest"]["sources"][key]["as_of"] = modified.isoformat()
        value["digest"]["sources"][key]["retrieved_at"] = modified.isoformat()
    revision = core.build_revision({"edition_date": "2026-10-16", "articles": value["articles"]}, value["digest"],
        root=tmp_path, base_sha256=hashlib.sha256(before).hexdigest(), reason="Добавлено проверенное сопоставление годовых банковских условий.", now=modified)
    core.publish_revision(revision, root=tmp_path, now=modified)
    public = core.load_article("2026-10-16-market", root=tmp_path, now=modified)
    assert public["published_at"] == NOW.isoformat() and public["date_modified"] == modified.isoformat()
    assert public["comparisons"][0]["rows"][0]["checked_label"].endswith("UTC+5")
    assert base.read_bytes() == before


@pytest.mark.asyncio
async def test_native_bank_pair_archive_topics_and_table_preserve_original_bytes(monkeypatch, tmp_path):
    value = bank_edition()
    core.publish_edition(value, root=tmp_path, now=NOW)
    path = core.archive_directory(tmp_path) / "2026-10-16.json"
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    assert len(core.list_articles(root=tmp_path, now=NOW, topic="banks")) == 1
    assert core.list_articles(root=tmp_path, now=NOW, topic="developers") == []
    async with public_client(monkeypatch, tmp_path) as client:
        archive = await client.get("/analytics?topic=banks")
        assert archive.status_code == 200 and Markup(archive.text).cards == 1
        assert 'aria-label="Подтемы аналитики"' in archive.text
        assert '/analytics?topic=complexes' not in archive.text
        response = await client.get("/analytics/2026-10-16-market")
        assert response.status_code == 200
        assert 'data-editorial-comparison="banks"' in response.text
        assert "16,87%" in response.text and "Freedom" in response.text
        assert "None" not in response.text and "{fact:" not in response.text
        assert "банк" in response.text.lower() and 'href="/compare#strategies"' in response.text
        assert response.text.count("ha-comparison-checked") == 2
        assert len(Markup(response.text).ids) == len(set(Markup(response.text).ids))
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.asyncio
async def test_empty_subtopics_are_not_badged_and_query_is_bounded(monkeypatch, tmp_path):
    core.publish_edition(core.build_edition(brief_draft(), brief_digest(), now=NOW), root=tmp_path, now=NOW)
    async with public_client(monkeypatch, tmp_path) as client:
        response = await client.get("/analytics")
        assert 'aria-label="Подтемы аналитики"' not in response.text
        assert 'class="ha-story-topics"' not in response.text
        assert (await client.get("/analytics?topic=made-up-winner")).status_code == 422
