"""Offline article contract: frozen facts, immutable pairs, honest source dates."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bot.core import analytics_articles as articles
from scripts import publish_analytics_articles as publisher

NOW = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc)
AS_OF = datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc)


def digest():
    sources = {"bns.housing": {
        "kind": "official_publication", "label": "Бюро национальной статистики: цены на жильё",
        "url": "https://stat.gov.kz/ru/industries/business-statistics/stat-price/",
        "as_of": AS_OF.isoformat(), "published_at": "2026-10-09T05:00:00+00:00",
        "retrieved_at": NOW.isoformat(), "reuse_url": "https://stat.gov.kz/ru/description/",
        "rights_status": "official_reuse_permitted",
    }}
    facts = {}
    for prefix, unit, base in (("sales", "₸/м²", 500_000), ("rent_monthly", "₸/мес.", 150_000)):
        for room in range(1, 5):
            facts[f"{prefix}.rooms_{room}"] = {
                "label": f"{room} комнаты", "value": base + 10_000 * room, "unit": unit,
                "n": None, "as_of": AS_OF.isoformat(), "source_id": "bns.housing",
            }
    return {"schema_version": 1, "generated_at": NOW.isoformat(),
            "scope": "Официальные месячные показатели Астаны; период наблюдения указан отдельно от даты публикации.",
            "sources": sources, "facts": facts}


PARAGRAPH = (
    "Сравнивать жильё полезно внутри понятного сегмента: одинаковая комнатность, близкая площадь, "
    "сопоставимое состояние и знакомый район помогают увидеть реальные различия между предложениями. "
    "Общая величина описывает группу объектов и не заменяет оценку конкретной квартиры. Покупателю "
    "важно сначала определить посильный бюджет, затем проверить состав расходов и только после этого "
    "переходить к просмотрам. Разница между районами может отражать особенности домов и планировок, "
    "а сама по себе не доказывает лучшее качество или гарантированную выгодность выбранного варианта."
)


def draft():
    result = []
    for kind, prefix in (("market", "sales"), ("rent", "rent_monthly")):
        result.append({
            "kind": kind, "title": "Как читать официальный срез жилой недвижимости Астаны",
            "summary": "Разбираем свежую официальную публикацию: сравниваем сегменты жилья и объясняем, как использовать показатели при выборе квартиры.",
            "lead": "Официальная статистика помогает задать ориентир, если читать её вместе с периодом наблюдения и особенностями исходной выборки. Показатель не является обещанием цены конкретной сделки.",
            "key_points": [
                f"Первый показатель в опубликованной таблице составляет {{fact:{prefix}.rooms_1}}.",
                f"Для другой группы официальный ориентир составляет {{fact:{prefix}.rooms_2}}.",
                "Средние показатели описывают сегмент, а документы и состояние выбранной квартиры проверяют отдельно.",
            ],
            "sections": [{"heading": "Что видно в официальном срезе", "paragraphs": [PARAGRAPH], "chart_id": "first-chart"},
                         {"heading": "Как сопоставлять разные группы", "paragraphs": [PARAGRAPH], "chart_id": "second-chart"},
                         {"heading": "Почему важен состав предложения", "paragraphs": [PARAGRAPH, PARAGRAPH]},
                         {"heading": "Как применить результат к поиску", "paragraphs": [PARAGRAPH, PARAGRAPH]}],
            "charts": [{"id": "first-chart", "title": "Официальные показатели по группам",
                        "caption": "Период и источник указаны под статьёй; значения относятся к разным группам объектов.",
                        "axis_label": "Официальный показатель", "fact_ids": [f"{prefix}.rooms_1", f"{prefix}.rooms_2"]},
                       {"id": "second-chart", "title": "Ещё одна часть официального среза",
                        "caption": "Значения относятся к соответствующим группам и не являются оценкой конкретной квартиры.",
                        "axis_label": "Официальный показатель", "fact_ids": [f"{prefix}.rooms_3", f"{prefix}.rooms_4"]}],
            "source_ids": ["bns.housing"],
            "methodology": ["Использована официальная месячная публикация; период наблюдения и дата получения различаются.",
                            "Размер исходной выборки не раскрыт в использованной таблице и не подставляется условно."],
        })
    return {"edition_date": "2026-10-16", "articles": result}


def edition():
    return articles.build_edition(draft(), digest(), now=NOW)


def observed_edition():
    value = edition()
    source = value["digest"]["sources"]["bns.housing"]
    source.update(kind="hatuli_observations", rights_status="operator_authorized",
                  url="https://hatuli.ai-groundtruth.com/how-it-works",
                  reuse_url="https://hatuli.ai-groundtruth.com/how-it-works")
    for fact in value["digest"]["facts"].values():
        fact["n"] = 30
    return value


def test_local_research_is_separate_immutable_pair_with_original_bytes_preserved(tmp_path):
    articles.publish_edition(edition(), root=tmp_path, now=NOW)
    original = articles.archive_directory(tmp_path) / "2026-10-16.json"
    original_bytes, original_mtime = original.read_bytes(), original.stat().st_mtime_ns
    value = observed_edition()
    assert articles.publish_edition(value, root=tmp_path, now=NOW, local=True)["status"] == "published"
    local = articles.archive_directory(tmp_path, local=True) / "2026-10-16.json"
    before, mtime = local.read_bytes(), local.stat().st_mtime_ns
    assert articles.publish_edition(value, root=tmp_path, now=NOW, local=True)["status"] == "already_published"
    assert (local.read_bytes(), local.stat().st_mtime_ns) == (before, mtime)
    assert (original.read_bytes(), original.stat().st_mtime_ns) == (original_bytes, original_mtime)
    items = articles.list_articles(root=tmp_path, now=NOW)
    assert len(items) == 4
    assert items[0]["slug"].endswith("-local")
    assert len(articles.list_articles(root=tmp_path, category="rent", now=NOW)) == 2
    assert articles.load_article("2026-10-16-market-local", root=tmp_path, now=NOW)["url"].endswith("-market-local")
    assert articles.load_article("2026-10-16-market", root=tmp_path, now=NOW)["url"].endswith("-market")


def test_local_research_requires_approved_observed_source_and_retains_fail_closed_reader(tmp_path):
    with pytest.raises(ValueError, match="Hatuli observations"):
        articles.publish_edition(edition(), root=tmp_path, now=NOW, local=True)
    value = observed_edition()
    value["digest"]["sources"]["bns.housing"]["rights_status"] = "not_cleared"
    with pytest.raises(ValueError, match="reuse"):
        articles.publish_edition(value, root=tmp_path, now=NOW, local=True)
    directory = articles.archive_directory(tmp_path, local=True)
    directory.mkdir(parents=True)
    (directory / "2026-10-16.json").write_text(json.dumps(edition()), encoding="utf-8")
    assert articles.load_article("2026-10-16-market-local", root=tmp_path, now=NOW) is None
    assert articles.list_articles(root=tmp_path, now=NOW) == []


def test_unused_observation_source_cannot_relabel_official_pair_as_local():
    value = edition()
    value["digest"]["sources"]["hatuli.unused"] = deepcopy(observed_edition()["digest"]["sources"]["bns.housing"])
    with pytest.raises(ValueError, match="used numeric Hatuli observations"):
        articles.normalize_local_edition(value, now=NOW)


@pytest.mark.parametrize("label", ["Скидка 90% и доходность 50%", "ЖК Скидка 90%", "ЖК {fact:sales.rooms_2}", "ЖК Цена 5 млн"])
def test_bound_place_labels_cannot_hide_claims_or_nested_tokens(label):
    value = observed_edition()
    value["digest"]["facts"]["sales.rooms_1"]["label"] = label
    value["articles"][0]["lead"] += " Рассматриваем {label:sales.rooms_1}."
    with pytest.raises(ValueError, match="place name"):
        articles.normalize_local_edition(value, now=NOW)


def test_bound_place_labels_allow_digit_in_complex_name_but_not_unbound_numeric_claims():
    value = observed_edition()
    value["digest"]["facts"]["sales.rooms_1"]["label"] = "ЖК Geneva 4"
    value["articles"][0]["lead"] += " В сравнении рассматривается {label:sales.rooms_1}, а цена показана отдельно."
    normalized = articles.normalize_local_edition(value, now=NOW)
    assert "ЖК Geneva 4" in normalized[0]["lead"]
    value["articles"][0]["lead"] += " Скидка 20 процентов."
    with pytest.raises(ValueError, match="fact tokens"):
        articles.normalize_local_edition(value, now=NOW)


def test_place_labels_support_kazakh_catalog_names():
    value = observed_edition()
    value["digest"]["facts"]["sales.rooms_1"]["label"] = "ЖК Арғымақ"
    value["articles"][0]["lead"] += " В сравнении есть {label:sales.rooms_1}."
    assert "ЖК Арғымақ" in articles.normalize_local_edition(value, now=NOW)[0]["lead"]


def test_place_label_must_exist_and_its_fact_source_must_be_cited():
    value = observed_edition()
    value["articles"][0]["lead"] += " Сравниваем {label:sales.unknown_place}."
    with pytest.raises(ValueError, match="Unknown label"):
        articles.normalize_local_edition(value, now=NOW)
    value = observed_edition()
    value["digest"]["sources"]["hatuli.extra"] = deepcopy(value["digest"]["sources"]["bns.housing"])
    value["digest"]["facts"]["sales.extra_place"] = {
        **value["digest"]["facts"]["sales.rooms_1"], "source_id": "hatuli.extra", "label": "ЖК Geneva 4"}
    value["articles"][0]["lead"] += " Сравниваем {label:sales.extra_place}."
    with pytest.raises(ValueError, match="cite every"):
        articles.normalize_local_edition(value, now=NOW)


def test_normalizes_two_ready_articles_without_fake_sample_size():
    values = articles.normalize_edition(edition(), now=NOW)
    assert {item["kind"] for item in values} == {"market", "rent"}
    market = values[0]
    assert market["slug"] == "2026-10-16-market"
    assert market["url"] == "/analytics/2026-10-16-market"
    assert "510 000 ₸/м²" in market["key_points"][0]
    assert market["charts"][0]["rows"][0]["n"] is None
    assert market["charts"][0]["rows"][1]["bar_pct"] == 100
    assert "30 сентября 2026" in market["sources"][0]["as_of_label"]
    assert "9 октября 2026" in market["sources"][0]["as_of_label"]
    assert "16 октября 2026" in market["sources"][0]["as_of_label"]
    assert 2 <= market["reading_minutes"] <= 5
    assert "facts" not in market and "source_ids" not in market


def test_current_publication_is_atomic_immutable_and_archive_never_expires(tmp_path):
    result = articles.publish_edition(edition(), root=tmp_path, now=NOW)
    path = tmp_path / "var/analytics-articles/2026-10-16.json"
    before = path.read_bytes()
    assert result["status"] == "published"
    changed = edition()
    changed["articles"][0]["title"] = "Изменённый заголовок не должен переписать опубликованный архив"
    assert articles.publish_edition(changed, root=tmp_path, now=NOW)["status"] == "already_published"
    assert path.read_bytes() == before
    assert len(articles.list_articles(root=tmp_path, now=NOW + timedelta(days=900))) == 2
    assert articles.load_article("2026-10-16-rent", root=tmp_path, now=NOW + timedelta(days=900))
    assert not list(path.parent.glob(".article-*"))


def test_invalid_pair_never_replaces_previous_or_creates_partial_day(tmp_path):
    broken = edition()
    broken["articles"].pop()
    with pytest.raises(ValueError, match="Exactly two"):
        articles.publish_edition(broken, root=tmp_path, now=NOW)
    assert articles.list_articles(root=tmp_path, now=NOW) == []
    assert not (tmp_path / "var/analytics-articles/2026-10-16.json").exists()


def test_invalid_existing_archive_is_preserved_for_manual_review(tmp_path):
    directory = articles.archive_directory(tmp_path)
    directory.mkdir(parents=True)
    target = directory / "2026-10-16.json"
    target.write_text('{"broken":true}', encoding="utf-8")
    with pytest.raises(ValueError):
        articles.publish_edition(edition(), root=tmp_path, now=NOW)
    assert target.read_text() == '{"broken":true}'
    assert articles.list_articles(root=tmp_path, now=NOW) == []


@pytest.mark.parametrize("content", ["null", "[]", "42", '"text"'])
def test_non_object_json_archive_never_crashes_public_reader(content, tmp_path):
    directory = articles.archive_directory(tmp_path)
    directory.mkdir(parents=True)
    (directory / "2026-10-16.json").write_text(content, encoding="utf-8")
    assert articles.list_articles(root=tmp_path, now=NOW) == []
    assert articles.load_article("2026-10-16-market", root=tmp_path, now=NOW) is None


@pytest.mark.parametrize("slug", ["../../private", "2026-10-16-market/../secret", "2026-02-30-market",
                                 "2026-10-16-rent?x=1", "2026-10-16-admin", None])
def test_slug_is_strict_and_cannot_read_arbitrary_files(slug, tmp_path):
    assert articles.load_article(slug, root=tmp_path, now=NOW) is None


def test_filtered_archive_omits_future_and_mismatched_files(tmp_path):
    articles.publish_edition(edition(), root=tmp_path, now=NOW)
    assert len(articles.list_articles(root=tmp_path, category="rent", now=NOW)) == 1
    assert articles.list_articles(root=tmp_path, category="admin", now=NOW) == []
    assert articles.list_articles(root=tmp_path, now=NOW - timedelta(days=1)) == []
    path = articles.archive_directory(tmp_path) / "2026-10-16.json"
    wrong = json.loads(path.read_text(encoding="utf-8"))
    wrong["edition_date"] = "2026-10-15"
    path.write_text(json.dumps(wrong), encoding="utf-8")
    assert articles.load_article("2026-10-16-market", root=tmp_path, now=NOW) is None


@pytest.mark.parametrize("bad", ["2026-10-15", "2026-10-17", "2026-2-1", "../../secret"])
def test_cannot_backfill_or_publish_future(bad):
    value = draft()
    value["edition_date"] = bad
    with pytest.raises(ValueError):
        articles.build_edition(value, digest(), now=NOW)


@pytest.mark.parametrize("mutation", ["stale_digest", "future_digest", "old_retrieval", "fact_date", "uncleared", "fake_sample", "unknown_unit"])
def test_source_dates_rights_and_samples_are_enforced(mutation):
    value = digest()
    if mutation == "stale_digest": value["generated_at"] = (NOW - timedelta(hours=25)).isoformat()
    if mutation == "future_digest": value["generated_at"] = (NOW + timedelta(seconds=1)).isoformat()
    if mutation == "old_retrieval": value["sources"]["bns.housing"]["retrieved_at"] = (NOW - timedelta(hours=25)).isoformat()
    if mutation == "fact_date": value["facts"]["sales.rooms_1"]["as_of"] = NOW.isoformat()
    if mutation == "uncleared": value["sources"]["bns.housing"]["rights_status"] = "not_cleared"
    if mutation == "fake_sample": value["facts"]["sales.rooms_1"]["n"] = True
    if mutation == "unknown_unit": value["facts"]["sales.rooms_1"]["unit"] = "bitcoin"
    with pytest.raises(ValueError):
        articles.build_edition(draft(), value, now=NOW)


@pytest.mark.parametrize("url", ["http://stat.gov.kz/", "https://stat.gov.kz.evil.test/file", "javascript:alert(1)",
                                "https://user:pass@stat.gov.kz/", "https://stat.gov.kz:443/", "https://krisha.kz/a/show/12345"])
def test_external_source_https_allowlist(url):
    value = digest()
    value["sources"]["bns.housing"]["url"] = url
    with pytest.raises(ValueError):
        articles.build_edition(draft(), value, now=NOW)


@pytest.mark.parametrize("bad_text", ["<script>unsafe</script>", "Неподтверждённая цена 250000 тенге для выбранной квартиры.",
                                     "Связаться с автором можно через test@example.com для обсуждения.",
                                     "Ссылка https://evil.test/ здесь не должна оказаться в тексте.",
                                     "Неизвестный показатель {fact:secret.fact} нельзя показывать в статье."])
def test_editorial_prose_cannot_invent_numbers_or_embed_contacts_html(bad_text):
    value = draft()
    value["articles"][0]["key_points"][0] = bad_text
    with pytest.raises(ValueError):
        articles.build_edition(value, digest(), now=NOW)


def test_all_chart_values_come_from_declared_facts_and_cited_sources():
    value = draft()
    value["articles"][0]["charts"][0]["rows"] = [{"label": "Выдумано", "value": 123}]
    with pytest.raises(ValueError):
        articles.build_edition(value, digest(), now=NOW)
    value = draft()
    value["articles"][0]["charts"][0]["fact_ids"] = ["sales.rooms_1", "missing.fact"]
    with pytest.raises(ValueError):
        articles.build_edition(value, digest(), now=NOW)


def test_signed_percentage_and_explicit_timeline_preserve_dates_and_scale():
    value = digest()
    second = deepcopy(value["sources"]["bns.housing"])
    second["as_of"] = "2026-08-31T18:00:00+00:00"
    value["sources"]["bns.august"] = second
    a, b = value["facts"]["sales.rooms_1"], value["facts"]["sales.rooms_2"]
    a.update(value=-2.5, unit="%", as_of=second["as_of"], source_id="bns.august")
    b.update(value=5, unit="%")
    body = draft()
    body["articles"][0]["source_ids"].append("bns.august")
    with pytest.raises(ValueError, match="Comparison rows"):
        articles.build_edition(body, value, now=NOW)
    body["articles"][0]["charts"][0]["mode"] = "timeline"
    normalized = articles.normalize_edition(articles.build_edition(body, value, now=NOW), now=NOW)[0]
    assert normalized["charts"][0]["rows"][0]["value_label"] == "-2,5%"
    assert normalized["charts"][0]["rows"][0]["bar_pct"] == 50
    body["articles"][0]["charts"][0]["fact_ids"].reverse()
    with pytest.raises(ValueError, match="chronological"):
        articles.build_edition(body, value, now=NOW)


def test_requires_two_distinct_kinds_four_sections_two_charts_and_medium_length():
    value = draft()
    value["articles"][1]["kind"] = "market"
    with pytest.raises(ValueError, match="one market"):
        articles.build_edition(value, digest(), now=NOW)
    value = draft()
    value["articles"][0]["sections"].pop()
    with pytest.raises(ValueError, match="four to seven"):
        articles.build_edition(value, digest(), now=NOW)
    value = draft()
    for section in value["articles"][0]["sections"]:
        section["paragraphs"] = ["Краткий абзац не должен подменять содержательный разбор официальной статистики, который нужен читателю для выбора квартиры."]
    with pytest.raises(ValueError, match="350–850"):
        articles.build_edition(value, digest(), now=NOW)
    value = draft()
    value["articles"][0]["sections"][0].pop("chart_id")
    with pytest.raises(ValueError, match="exactly one"):
        articles.build_edition(value, digest(), now=NOW)


def sale(identifier, **changes):
    return {"id": identifier, "url": f"https://krisha.kz/a/show/{identifier}", "price": 25_000_000,
            "area": 40, "rooms": 1, "district": "Есильский р-н", "market_type": "secondary",
            "last_seen": NOW, "is_active": True, "is_duplicate": False, "dup_needs_review": False,
            "duplicate_of": None, "property_review_pending": False, "archived_at": None, **changes}


def rental(identifier, **changes):
    return {**sale(identifier), "city": "astana", "prop_type": "apartment", "rent_period": "monthly",
            "rent_period_conflict": False, "price": 200_000, "title": "Квартира помесячно",
            "rental_episode_started_at": NOW - timedelta(days=1), "rental_episode_last_seen_at": NOW,
            **changes}


@pytest.mark.parametrize("label,key", [("Алматинский район", "almaty"), ("Сарыаркинский район", "saryarka"),
                                      ("Сарайшық", "saraishyk"), ("Байқоңыр ауданы", "baikonur")])
def test_known_district_aliases_are_not_lost_from_observation_counts(label, key):
    assert articles.district_key(label)[0] == key


def test_observation_digest_uses_clean_unique_fresh_separate_periods_without_public_ids():
    sales = [sale(str(10000 + index)) for index in range(12)]
    sales += [sale("20001", property_id=1), sale("20002", property_id=1),
              sale("20003", is_duplicate=True), sale("20004", property_review_pending=True),
              sale("20005", last_seen=NOW - timedelta(hours=73)), sale("20006", district="Другой город"),
              sale("20007", last_seen=NOW + timedelta(seconds=1))]
    rentals = [rental(str(30000 + index)) for index in range(10)]
    rentals += [rental(str(40000 + index), rent_period="daily", title="Посуточно", price=15_000) for index in range(10)]
    rentals += [rental("50001", rent_period_conflict=True), rental("50002", duplicate_of="30000"),
                rental("50003", title="Посуточно"), rental("50004", rent_period="unknown"),
                rental("50005", rent_period="daily", title="Посуточно", rental_episode_last_seen_at=None)]
    result = articles.build_digest(sales, rentals, now=NOW)
    assert result["facts"]["sales.count"]["value"] == 13
    assert result["facts"]["rent_monthly.count"]["value"] == 10
    assert result["facts"]["rent_daily.count"]["value"] == 10
    assert result["facts"]["rent_monthly.price_median"]["value"] == 200_000
    assert result["facts"]["rent_daily.price_median"]["value"] == 15_000
    assert result["sources"]["hatuli.observed"]["rights_status"] == "not_cleared"
    encoded = json.dumps(result, ensure_ascii=False)
    assert "20001" not in encoded and "30000" not in encoded and "krisha.kz/a/show" not in encoded
    with pytest.raises(ValueError, match="reuse"):
        articles.build_edition(draft(), result, now=NOW)


class ReadonlyTransaction:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False


class ReadonlyConnection:
    def __init__(self):
        self.close = AsyncMock()
        self.fetch = AsyncMock(return_value=[])
        self.fetchval = AsyncMock(side_effect=["on", "repeatable read"])
    def transaction(self, **kwargs):
        assert kwargs == {"readonly": True, "isolation": "repeatable_read"}
        return ReadonlyTransaction()


@pytest.mark.asyncio
async def test_export_uses_only_bounded_readonly_repeatable_transaction(monkeypatch):
    import asyncpg
    connection = ReadonlyConnection()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(publisher, "build_digest", lambda sales, rentals, now: {"approved": True})
    assert await publisher.read_digest("postgresql://unused:unused@127.0.0.1:1/test", NOW) == {"approved": True}
    kwargs = connect.call_args.kwargs
    assert kwargs["server_settings"]["default_transaction_read_only"] == "on"
    assert connection.fetch.await_count == 2
    for call in connection.fetch.call_args_list:
        assert call.args[-1] == articles.MAX_SCAN + 1
        assert "LIMIT $2" in call.args[0]
        assert not any(word in call.args[0].upper().split() for word in ("UPDATE", "INSERT", "DELETE", "ALTER"))
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_readonly_assertion_closes_without_query(monkeypatch):
    import asyncpg
    connection = ReadonlyConnection()
    connection.fetchval = AsyncMock(return_value="off")
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))
    with pytest.raises(RuntimeError, match="read-only"):
        await publisher.read_digest("postgresql://unused:unused@127.0.0.1:1/test", NOW)
    connection.fetch.assert_not_awaited()
    connection.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_truncated_population_is_not_published(monkeypatch):
    import asyncpg
    connection = ReadonlyConnection()
    connection.fetch = AsyncMock(return_value=[{}] * (articles.MAX_SCAN + 1))
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=connection))
    with pytest.raises(ValueError, match="incomplete"):
        await publisher.read_digest("postgresql://unused:unused@127.0.0.1:1/test", NOW)
    connection.close.assert_awaited_once()


def test_official_publish_branch_never_connects_or_loads_environment(monkeypatch, tmp_path):
    import dotenv
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None): return NOW
    monkeypatch.setattr(publisher, "datetime", FixedDateTime)
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *a, **kw: pytest.fail("Publication must not load DB credentials"))
    monkeypatch.setattr(publisher, "read_digest", lambda *a, **kw: pytest.fail("Publication must not query DB"))
    draft_path, digest_path = tmp_path / "draft.json", tmp_path / "digest.json"
    draft_path.write_text(json.dumps(draft(), ensure_ascii=False), encoding="utf-8")
    digest_path.write_text(json.dumps(digest(), ensure_ascii=False), encoding="utf-8")
    argv = ["--root", str(tmp_path), "--articles", str(draft_path), "--digest", str(digest_path)]
    assert publisher.main([*argv, "--check"]) == 0
    assert not articles.archive_directory(tmp_path).exists()
    assert publisher.main(argv) == 0
    draft_path.unlink()
    digest_path.unlink()
    assert publisher.main(argv) == 0
    assert len(articles.list_articles(root=tmp_path, now=NOW)) == 2


def brief_digest(now=NOW):
    value = digest()
    value["generated_at"] = now.isoformat()
    value["sources"]["bns.housing"]["retrieved_at"] = now.isoformat()
    august = deepcopy(value["sources"]["bns.housing"])
    august["as_of"] = "2026-08-31T18:00:00+00:00"
    value["sources"]["bns.august"] = august
    for prefix in ("sales", "rent_monthly"):
        for room in range(1, 5):
            value["facts"][f"{prefix}.rooms_{room}"].update(
                label="Август" if room % 2 else "Сентябрь",
                series_id=f"{prefix}.{'first' if room <= 2 else 'second'}",
                as_of=august["as_of"] if room % 2 else AS_OF.isoformat(),
                source_id="bns.august" if room % 2 else "bns.housing")
    return value


def brief_draft():
    value = draft()
    for article in value["articles"]:
        article["editorial_format"] = "brief"
        article["sections"] = article["sections"][:2]
        article["source_ids"].append("bns.august")
        for chart in article["charts"]:
            chart["mode"] = "timeline"
    return value


def base_archive(tmp_path):
    articles.publish_edition(edition(), root=tmp_path, now=NOW)
    path = articles.archive_directory(tmp_path) / "2026-10-16.json"
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def revised(tmp_path, at=None):
    at = at or NOW + timedelta(hours=3)
    path, sha = base_archive(tmp_path)
    revision = articles.build_revision(brief_draft(), brief_digest(at), root=tmp_path,
        base_sha256=sha, reason="Сокращён текст и добавлены проверенные временные ряды.", now=at)
    return path, revision, at


def test_explicit_brief_and_legacy_metadata_are_backwards_compatible():
    old = articles.normalize_edition(edition(), now=NOW)[0]
    assert old["editorial_format"] == "standard" and 350 <= old["word_count"] <= 850
    assert old["published_at"] == old["date_modified"] == NOW.isoformat()
    assert old["is_revised"] is False and old["modified_label"] is None
    values = articles.normalize_edition(articles.build_edition(brief_draft(), brief_digest(), now=NOW), now=NOW)
    assert all(v["editorial_format"] == "brief" and 150 <= v["word_count"] <= 250 for v in values)
    assert all(len(v["sections"]) == 2 and len(v["charts"]) == 2 for v in values)
    chart = values[0]["charts"][0]
    assert chart["mode"] == "timeline"
    assert [r["as_of"] for r in chart["rows"]] == ["2026-08-31T18:00:00+00:00", AS_OF.isoformat()]
    assert [r["value"] for r in chart["rows"]] == [510000, 520000]


@pytest.mark.parametrize("mutation",["missing_series","mixed_series","same_calendar_month","comparison","no_mode","extra_chart","one_section","five_sections","too_short","too_long"])
def test_brief_cannot_connect_categories_or_ignore_its_short_contract(mutation):
    value, body = brief_digest(), brief_draft()
    if mutation == "missing_series": value["facts"]["sales.rooms_1"].pop("series_id")
    if mutation == "mixed_series": value["facts"]["sales.rooms_2"]["series_id"] = "sales.other_category"
    if mutation == "same_calendar_month":
        value["sources"]["bns.august"]["as_of"] = "2026-09-15T18:00:00+00:00"
        for fact in value["facts"].values():
            if fact["source_id"] == "bns.august": fact["as_of"] = "2026-09-15T18:00:00+00:00"
    if mutation == "comparison": body["articles"][0]["charts"][0]["mode"] = "comparison"
    if mutation == "no_mode": body["articles"][0]["charts"][0].pop("mode")
    if mutation == "extra_chart": body["articles"][0]["charts"].append(deepcopy(body["articles"][0]["charts"][0]))
    if mutation == "one_section": body["articles"][0]["sections"].pop()
    if mutation == "five_sections": body["articles"][0]["sections"] *= 3
    if mutation == "too_short":
        for section in body["articles"][0]["sections"]:
            section["paragraphs"] = ["Проверяем период официальных данных перед выбором жилья."]
    if mutation == "too_long":
        for section in body["articles"][0]["sections"]:
            section["paragraphs"] = [PARAGRAPH, PARAGRAPH * 2]
    with pytest.raises(ValueError): articles.build_edition(body, value, now=NOW)


def test_brief_timeline_can_have_missing_month_without_inventing_an_intermediate_fact():
    value = brief_digest()
    source = value["sources"]["bns.august"]
    source["as_of"] = "2026-07-31T18:00:00+00:00"
    for fact in value["facts"].values():
        if fact["source_id"] == "bns.august": fact["as_of"] = source["as_of"]
    values = articles.normalize_edition(articles.build_edition(brief_draft(), value, now=NOW), now=NOW)
    assert len(values[0]["charts"][0]["rows"]) == 2
    assert values[0]["charts"][0]["rows"][0]["as_of"].startswith("2026-07-31")


def test_revision_newly_checked_sources_validate_at_real_modified_time_not_original_publication(tmp_path):
    path, revision, at = revised(tmp_path)
    assert articles.utc(revision["digest"]["generated_at"]) > articles.utc(edition()["published_at"])
    with pytest.raises(ValueError, match="aggregate digest"):
        articles.normalize_edition({**edition(),"digest":revision["digest"],"articles":revision["articles"]},now=at)
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    result = articles.publish_revision(revision,root=tmp_path,now=at)
    assert result["status"] == "revised"
    assert (path.read_bytes(),path.stat().st_mtime_ns) == (before,mtime)
    values = articles.list_articles(root=tmp_path,now=at)
    assert len(values) == 2
    for value in values:
        assert value["published_at"] == NOW.isoformat()
        assert value["date_modified"] == at.isoformat() and value["is_revised"] is True
        assert value["edition_date"] == "2026-10-16" and value["editorial_format"] == "brief"
        assert value["modified_label"] and value["revision_reason"] == revision["reason"]
        assert value["slug"] in {"2026-10-16-market","2026-10-16-rent"}
        assert articles.load_article(value["slug"],root=tmp_path,now=at+timedelta(days=900))["date_modified"] == at.isoformat()


def test_revision_history_is_immutable_idempotent_and_latest_full_pair_wins(tmp_path):
    path, revision, at = revised(tmp_path)
    articles.publish_revision(revision,root=tmp_path,now=at)
    directory=articles.revision_directory("2026-10-16",tmp_path)
    first=next(directory.glob("*.json"));before=(first.read_bytes(),first.stat().st_mtime_ns)
    same=articles.build_revision(brief_draft(),revision["digest"],root=tmp_path,
        base_sha256=revision["base_sha256"],reason=revision["reason"],now=at+timedelta(minutes=1))
    assert articles.publish_revision(same,root=tmp_path,now=at+timedelta(minutes=1))["status"] == "already_revised"
    assert len(list(directory.glob("*.json"))) == 1
    second=deepcopy(same);second["articles"][0]["title"]="Новый короткий разбор официальной динамики цен Астаны"
    articles.publish_revision(second,root=tmp_path,now=at+timedelta(minutes=1))
    assert len(list(directory.glob("*.json"))) == 2
    assert (first.read_bytes(),first.stat().st_mtime_ns) == before
    pair=articles.list_articles(root=tmp_path,now=at+timedelta(minutes=1))
    assert len(pair) == 2 and pair[0]["date_modified"] == pair[1]["date_modified"]
    assert next(v for v in pair if v["kind"]=="market")["title"] == second["articles"][0]["title"]
    assert not list(directory.glob(".revision-*"))


def test_invalid_future_mismatched_or_partial_overlay_falls_back_as_a_whole_pair(tmp_path):
    _, revision, at = revised(tmp_path)
    directory=articles.revision_directory("2026-10-16",tmp_path);directory.mkdir(parents=True)
    for mutation in ("partial","base_sha","day","future","filename"):
        bad=deepcopy(revision)
        if mutation=="partial": bad["articles"].pop()
        if mutation=="base_sha": bad["base_sha256"]="0"*64
        if mutation=="day": bad["edition_date"]="2026-10-15"
        if mutation=="future": bad["modified_at"]=(at+timedelta(hours=1)).isoformat()
        stamp=articles._revision_stamp(bad["modified_at"] if mutation!="filename" else at+timedelta(seconds=1))
        overlay=directory/f"{stamp}.json";overlay.write_text(json.dumps(bad),encoding="utf-8")
        assert all(v["is_revised"] is False for v in articles.list_articles(root=tmp_path,now=at))
        overlay.unlink()
    articles.publish_revision(revision,root=tmp_path,now=at)
    corrupt=directory/f"{articles._revision_stamp(at+timedelta(minutes=1))}.json"
    corrupt.write_text("null",encoding="utf-8")
    assert all(v["is_revised"] for v in articles.list_articles(root=tmp_path,now=at+timedelta(minutes=1)))


def test_changed_original_bytes_disable_old_overlays_and_block_stale_authorization(tmp_path):
    path, revision, at = revised(tmp_path)
    articles.publish_revision(revision,root=tmp_path,now=at)
    path.write_bytes(path.read_bytes()+b"\n")
    assert all(v["is_revised"] is False for v in articles.list_articles(root=tmp_path,now=at))
    with pytest.raises(ValueError,match="SHA256"):
        articles.build_revision(brief_draft(),brief_digest(at),root=tmp_path,
            base_sha256=revision["base_sha256"],reason=revision["reason"],now=at)
    with pytest.raises(ValueError,match="SHA256"):
        articles.publish_revision(revision,root=tmp_path,now=at)


@pytest.mark.parametrize("reason",["<b>Новый формат</b>","Читайте https://example.test/revision", "Контакт test@example.com для правки"])
def test_revision_reason_must_be_plain_without_html_links_or_contacts(reason,tmp_path):
    _, revision, at = revised(tmp_path)
    revision["reason"] = reason
    with pytest.raises(ValueError,match="Plain editorial"):
        articles.publish_revision(revision,root=tmp_path,now=at)
    assert not articles.revision_directory("2026-10-16",tmp_path).exists()


def test_revision_cannot_forge_modification_timestamp_or_create_absent_original(tmp_path):
    _, revision, at = revised(tmp_path)
    with pytest.raises(ValueError,match="actual revision publication"):
        articles.publish_revision(revision,root=tmp_path,now=at+timedelta(seconds=1))
    with pytest.raises(ValueError,match="Invalid archive"):
        articles.build_revision(brief_draft(),brief_digest(at),root=tmp_path/"missing",
            base_sha256=revision["base_sha256"],reason=revision["reason"],now=at)


def test_bounded_revision_overflow_keeps_original_and_refuses_more_history(monkeypatch,tmp_path):
    _, revision, at = revised(tmp_path)
    monkeypatch.setattr(articles,"MAX_REVISIONS",2)
    directory=articles.revision_directory("2026-10-16",tmp_path);directory.mkdir(parents=True)
    for i in range(2): (directory/f"invalid{i}.json").write_text("null",encoding="utf-8")
    with pytest.raises(ValueError,match="Maximum immutable"):
        articles.publish_revision(revision,root=tmp_path,now=at)
    (directory/"another.json").write_text("null",encoding="utf-8")
    with pytest.raises(ValueError,match="bounded limit"):
        articles.publish_revision(revision,root=tmp_path,now=at)
    assert len(articles.list_articles(root=tmp_path,now=at))==2
    assert articles.load_article("2026-10-16-rent",root=tmp_path,now=at)["is_revised"] is False


def test_revision_check_only_and_publish_never_touch_database_or_environment(monkeypatch,tmp_path,capsys):
    import dotenv
    _, revision, at = revised(tmp_path)
    class FixedDateTime(datetime):
        @classmethod
        def now(cls,tz=None): return at
    monkeypatch.setattr(publisher,"datetime",FixedDateTime)
    monkeypatch.setattr(dotenv,"dotenv_values",lambda *a,**k:pytest.fail("No DB environment in revision"))
    monkeypatch.setattr(publisher,"read_digest",lambda *a,**k:pytest.fail("No DB in revision"))
    body, facts=tmp_path/"brief.json",tmp_path/"facts.json"
    body.write_text(json.dumps(brief_draft()),encoding="utf-8")
    facts.write_text(json.dumps(revision["digest"]),encoding="utf-8")
    args=["--root",str(tmp_path),"--articles",str(body),"--digest",str(facts),"--revise",
          "--base-sha256",revision["base_sha256"],"--reason",revision["reason"]]
    assert publisher.main([*args,"--check-only"]) == 0
    assert json.loads(capsys.readouterr().out)["file_writes"]==0
    assert not articles.revision_directory("2026-10-16",tmp_path).exists()
    assert publisher.main(args)==0
    assert json.loads(capsys.readouterr().out)["status"]=="revised"
    assert publisher.main(args)==0
    assert json.loads(capsys.readouterr().out)["status"]=="already_revised"


@pytest.mark.parametrize("extra",[["--revise"],["--base-sha256","0"*64],["--revise","--local","--base-sha256","0"*64,"--reason","Нельзя менять отдельный архив."]])
def test_revision_cli_requires_explicit_bound_original_and_reason(extra):
    with pytest.raises(SystemExit): publisher.main(["--articles","missing.json","--digest","missing.json",*extra])
