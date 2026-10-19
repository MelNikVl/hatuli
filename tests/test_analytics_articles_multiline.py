"""One panel compares fact-backed monthly series on common honest axes."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import math

import pytest

from bot.core import analytics_articles as core
from bot.web import analytics_articles as web
from tests.test_analytics_articles import NOW, brief_digest, brief_draft, edition
from tests.test_analytics_articles_public_page import page
from tests.test_analytics_articles_line_charts import Lines


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_multi_chart_test")
    async def denied(*args, **kwargs):
        raise AssertionError("Chart tests must not access PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", denied)
    monkeypatch.setattr("asyncpg.create_pool", denied)
    monkeypatch.setattr("bot.db.pg.init_pool", denied)


def multi_draft():
    data = brief_draft()
    for article in data["articles"]:
        first, second = article["charts"]
        combined = {key: value for key, value in first.items() if key != "fact_ids"}
        combined["series"] = [{"id": "primary", "label": "Квартиры от застройщика", "fact_ids": first["fact_ids"]},
                              {"id": "secondary", "label": "Квартиры на вторичном рынке", "fact_ids": second["fact_ids"]}]
        article["charts"] = [combined]
        for section in article["sections"]:
            if section.get("chart_id") == second["id"]:
                section.pop("chart_id")
    return data


def normalized():
    return core.normalize_edition(core.build_edition(multi_draft(), brief_digest(), now=NOW), now=NOW)


def test_one_panel_normalizes_distinct_fact_series_without_literal_values():
    digest, draft = brief_digest(), multi_draft()
    before = json.dumps({"draft": draft, "digest": digest}, sort_keys=True)
    articles = core.normalize_edition(core.build_edition(draft, digest, now=NOW), now=NOW)
    assert len(articles[0]["charts"]) == 1
    chart = articles[0]["charts"][0]
    assert len(chart["rows"]) == 4 and len(chart["series"]) == 2
    assert [line["label"] for line in chart["series"]] == ["Квартиры от застройщика", "Квартиры на вторичном рынке"]
    assert [row["value"] for row in chart["series"][0]["rows"]] == [510000, 520000]
    assert [row["value"] for row in chart["series"][1]["rows"]] == [530000, 540000]
    assert json.dumps({"draft": draft, "digest": digest}, sort_keys=True) == before


@pytest.mark.parametrize("change", ["mixed_units", "missing_series_id", "mixed_series_id", "duplicate_factual_series",
                                    "same_month", "unordered", "literal_values", "both_contracts", "unreferenced_source",
                                    "duplicate_id", "comparison", "seven_lines"])
def test_multiline_contract_rejects_unrelated_or_unvalidated_numbers(change):
    digest, draft = brief_digest(), multi_draft()
    chart = draft["articles"][0]["charts"][0]
    line = chart["series"][0]
    if change == "mixed_units":
        digest["facts"]["sales.rooms_3"]["unit"] = "%"
        digest["facts"]["sales.rooms_3"]["value"] = 5
    elif change == "missing_series_id": digest["facts"]["sales.rooms_1"].pop("series_id")
    elif change == "mixed_series_id": digest["facts"]["sales.rooms_2"]["series_id"] = "sales.different"
    elif change == "duplicate_factual_series":
        for key in chart["series"][1]["fact_ids"]:
            digest["facts"][key]["series_id"] = digest["facts"]["sales.rooms_1"]["series_id"]
    elif change == "same_month":
        digest["facts"]["sales.rooms_1"].update(source_id="bns.housing", as_of=digest["sources"]["bns.housing"]["as_of"])
    elif change == "unordered": line["fact_ids"].reverse()
    elif change == "literal_values": line["values"] = [1, 2]
    elif change == "both_contracts": chart["fact_ids"] = line["fact_ids"]
    elif change == "unreferenced_source": draft["articles"][0]["source_ids"].remove("bns.august")
    elif change == "duplicate_id": chart["series"][1]["id"] = line["id"]
    elif change == "comparison": chart["mode"] = "comparison"
    elif change == "seven_lines": chart["series"] *= 4
    with pytest.raises(ValueError):
        core.build_edition(draft, digest, now=NOW)


def test_shared_vertical_scale_and_calendar_are_identical_for_equal_values():
    article = normalized()[0]
    chart = article["charts"][0]
    chart["series"][0]["rows"][1]["value"] = 530000
    before = json.dumps(article, sort_keys=True)
    display = web._display_article(article)["charts"][0]
    first, second = display["plot"]["series"]
    assert first["points"][1]["y"] == second["points"][0]["y"]
    assert first["points"][0]["x"] == second["points"][0]["x"]
    assert first["points"][1]["x"] == second["points"][1]["x"]
    assert first["ticks"] == second["ticks"]
    assert len(display["plot"]["table_rows"]) == 2
    assert json.dumps(article, sort_keys=True) == before


def test_gaps_are_per_series_and_missing_values_are_not_zero_or_interpolated():
    article = normalized()[0]
    chart = article["charts"][0]
    first, second = chart["series"]
    first["rows"][0]["as_of"] = "2026-07-31T00:00:00+00:00"
    second["rows"][0]["as_of"] = "2026-08-31T00:00:00+00:00"
    plot = web._display_article(article)["charts"][0]["plot"]
    assert plot["has_gaps"] is True
    assert plot["series"][0]["segments"] == []
    assert len(plot["series"][1]["segments"]) == 1
    assert plot["series"][1]["points"][0]["x"] == pytest.approx((8+992)/2)
    assert plot["table_rows"][0]["cells"][1] is None
    assert plot["table_rows"][1]["cells"][0] is None
    assert len(plot["series"][0]["points"]) == 2


@pytest.mark.parametrize("values", [[0, 0, 0, 0], [-2, 0, 0, 3], [600000, 600000, 600000, 600000]])
def test_constant_signed_and_zero_display_values_have_finite_shared_geometry(values):
    article = normalized()[0]
    for row, value in zip((row for line in article["charts"][0]["series"] for row in line["rows"]), values):
        row["value"] = value
    plot = web._display_article(article)["charts"][0]["plot"]
    assert all(math.isfinite(point["y"]) and 0 <= point["y"] <= 240 for line in plot["series"] for point in line["points"])
    if min(values) <= 0 <= max(values): assert plot["zero_y"] is not None


@pytest.mark.asyncio
async def test_one_public_panel_has_two_named_lines_and_one_accessible_table(monkeypatch):
    article = normalized()[0]
    response = await page(monkeypatch, [article], "/analytics/2026-10-16-market")
    assert response.status_code == 200
    html = response.text
    assert html.count("data-analytics-chart") == 1
    assert len(Lines(html).segments) == 2 and len(Lines(html).points) == 4
    assert html.count("<table>") == 1
    for label in ("Квартиры от застройщика", "Квартиры на вторичном рынке"):
        assert f'<th scope="col">{label}</th>' in html
        assert label in html
    assert html.index('class="ha-takeaways"') < html.index('class="ha-brief-charts ha-brief-charts-single"')
    assert 'class="ha-time-legend"' in html and 'data-series-index="1"' in html
    assert "Шкала:" in html and "₸/м²" in html
    assert "None" not in html


@pytest.mark.asyncio
async def test_multi_series_labels_and_values_are_escaped_without_losing_old_archives(monkeypatch):
    article = normalized()[0]
    article["charts"][0]["series"][0]["label"] = '</title><script>alert("XSS")</script>'
    response = await page(monkeypatch, [article], "/analytics/2026-10-16-market")
    assert '</title><script>' not in response.text
    assert '&lt;script&gt;' in response.text
    legacy = core.normalize_edition(edition(), now=NOW)[0]
    assert "series" not in legacy["charts"][0]
    assert len(legacy["charts"]) == 2


def test_revision_preserves_original_bytes_and_reads_multi_panel_from_actual_new_revision(tmp_path):
    core.publish_edition(edition(), root=tmp_path, now=NOW)
    path = core.archive_directory(tmp_path) / "2026-10-16.json"
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    modified = NOW+timedelta(hours=3)
    revision = core.build_revision(multi_draft(), brief_digest(modified), root=tmp_path,
        base_sha256=hashlib.sha256(before).hexdigest(), reason="Объединены линии на общей проверенной шкале.", now=modified)
    assert core.publish_revision(revision, root=tmp_path, now=modified)["status"] == "revised"
    restored = core.load_article("2026-10-16-market", root=tmp_path, now=modified)
    assert len(restored["charts"]) == 1 and len(restored["charts"][0]["series"]) == 2
    assert restored["published_at"] == NOW.isoformat()
    assert restored["date_modified"] == modified.isoformat()
    assert (path.read_bytes(), path.stat().st_mtime_ns) == (before, mtime)
