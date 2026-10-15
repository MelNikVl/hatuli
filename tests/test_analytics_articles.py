"""Offline article contract: frozen facts, immutable pairs, honest source dates."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
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
