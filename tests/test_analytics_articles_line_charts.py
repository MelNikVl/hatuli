"""Monthly lines preserve observed order and gaps; categories never form a line."""
from html.parser import HTMLParser
import json
import math

import pytest

from bot.web import analytics_articles as web
from tests.test_analytics_articles_public_page import Parts, page, prepared


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_line_chart_test")
    async def reject(*args, **kwargs):
        raise AssertionError("Timeline display tests must not access PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr("bot.db.pg.init_pool", reject)


def timeline(months=(4, 5, 6, 7, 8, 9), values=None):
    article = prepared("rent")
    article["editorial_format"] = "brief"
    chart = article["charts"][0]
    chart.update(mode="timeline", unit="₸/м²", title="Цена по месяцам", axis_label="Апрель–сентябрь")
    values = values or [600_000 + index * 10_000 for index in range(len(months))]
    labels = {4: "Апрель", 5: "Май", 6: "Июнь", 7: "Июль", 8: "Август", 9: "Сентябрь"}
    chart["rows"] = [{"label": labels[month], "value": value, "value_label": f"{value} ₸/м²",
                      "n": None, "bar_pct": 100 * abs(value) / max(abs(x) for x in values),
                      "as_of": f"2026-{month:02}-01T00:00:00+00:00"}
                     for month, value in zip(months, values)]
    return article


class Lines(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.segments, self.points, self.svg_roles = [], [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "polyline":
            self.segments.append([tuple(float(v) for v in pair.split(",")) for pair in attrs["points"].split()])
        if "ha-time-point" in attrs.get("class", "").split():
            self.points.append(attrs)
        if tag == "svg" and attrs.get("class") == "ha-time-svg":
            self.svg_roles.append(attrs.get("role"))


@pytest.mark.asyncio
async def test_brief_timeline_has_all_actual_months_and_values_with_accessible_table(monkeypatch):
    article = timeline()
    response = await page(monkeypatch, [article], "/analytics/2026-10-16-rent")
    assert response.status_code == 200
    chart = Lines(response.text)
    assert len(chart.segments) == 1 and len(chart.segments[0]) == 6
    assert len(chart.points) == 6 and chart.svg_roles == ["img"]
    assert all(x[0] < y[0] and x[1] > y[1] for x, y in zip(chart.segments[0], chart.segments[0][1:]))
    assert '<th scope="col">Период</th>' in response.text
    for row in article["charts"][0]["rows"]:
        assert f'<th scope="row">{row["label"]}</th><td>{row["value_label"]}</td>' in response.text
    assert "2026" in response.text and "Шкала:" in response.text
    assert '<th scope="col">Наблюдений</th>' not in response.text
    assert "None" not in response.text
    assert "ha-extended" not in response.text


@pytest.mark.asyncio
async def test_monthly_gap_is_real_horizontal_space_and_two_unconnected_segments(monkeypatch):
    response = await page(monkeypatch, [timeline((4, 5, 7, 8))], "/analytics/2026-10-16-rent")
    lines = Lines(response.text)
    assert len(lines.segments) == 2
    assert [len(segment) for segment in lines.segments] == [2, 2]
    first, second = lines.segments
    assert second[0][0] - first[1][0] == pytest.approx(2 * (first[1][0] - first[0][0]), abs=.002)
    assert "Пропущенные месяцы не соединены" in response.text


@pytest.mark.asyncio
async def test_isolated_months_remain_points_without_invented_continuity(monkeypatch):
    response = await page(monkeypatch, [timeline((4, 6, 8))], "/analytics/2026-10-16-rent")
    lines = Lines(response.text)
    assert not lines.segments and len(lines.points) == 3
    assert response.text.count('<th scope="row">') == 3


@pytest.mark.asyncio
async def test_categories_are_unconnected_even_when_dates_exist(monkeypatch):
    article = timeline()
    article["charts"][0]["mode"] = "comparison"
    response = await page(monkeypatch, [article], "/analytics/2026-10-16-rent")
    assert not Lines(response.text).segments
    assert "data-analytics-timeline" not in response.text
    assert "ha-chart-dot" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing_date", "out_of_order", "same_month"])
async def test_invalid_month_sequence_never_becomes_a_connected_line(monkeypatch, change):
    article = timeline()
    rows = article["charts"][0]["rows"]
    if change == "missing_date":
        rows[0].pop("as_of")
    elif change == "out_of_order":
        rows[0], rows[1] = rows[1], rows[0]
    else:
        rows[1]["as_of"] = "2026-04-20T00:00:00+00:00"
    response = await page(monkeypatch, [article], "/analytics/2026-10-16-rent")
    assert not Lines(response.text).segments
    assert "data-analytics-timeline" not in response.text
    assert response.text.count('<th scope="row">') == 6


@pytest.mark.parametrize("values", [[625_000] * 6, [-2, -1, 0, 1, 2, 3]])
def test_constant_and_signed_series_have_finite_honest_scales_without_changing_input(values):
    article = timeline(values=values)
    original = json.dumps(article, sort_keys=True)
    plot = web._display_article(article)["charts"][0]["plot"]
    assert all(math.isfinite(p["y"]) and 0 <= p["y"] <= 240 for p in plot["points"])
    if min(values) < 0:
        assert plot["zero_y"] is not None
    assert json.dumps(article, sort_keys=True) == original


@pytest.mark.asyncio
async def test_archive_preview_has_whole_timeline_and_unique_accessible_svg_ids(monkeypatch):
    first, second = timeline(), timeline()
    second.update(slug="2026-10-17-rent", url="/analytics/2026-10-17-rent")
    response = await page(monkeypatch, [first, second])
    assert len(Lines(response.text).points) == 12
    ids = Parts(response.text).ids
    assert len(ids) == len(set(ids))
    assert "Сентябрь" in response.text


@pytest.mark.asyncio
async def test_revision_metadata_retains_original_publication_and_real_modification(monkeypatch):
    article = timeline()
    article.update(published_at="2026-10-15T21:31:13+00:00", date_modified="2026-10-19T10:00:00+00:00",
                   modified_label="19 октября 2026, 15:00 UTC+5", revision_reason="Короткая редакция", is_revised=True)
    response = await page(monkeypatch, [article], "/analytics/2026-10-16-rent")
    schema = json.loads("".join(Parts(response.text).json_ld))
    assert schema["datePublished"] == article["published_at"]
    assert schema["dateModified"] == article["date_modified"]
    assert article["published_label"] in response.text
    assert "Обновлено 19 октября 2026, 15:00 UTC+5" in response.text


@pytest.mark.asyncio
async def test_legacy_full_text_is_retained_with_charts_before_disclosure(monkeypatch):
    article = prepared()
    response = await page(monkeypatch, [article], "/analytics/2026-10-16-market")
    assert '<details class="ha-extended">' in response.text
    assert response.text.index("data-analytics-chart") < response.text.index('<details class="ha-extended">')
    assert all(paragraph in response.text for section in article["sections"] for paragraph in section["paragraphs"])


@pytest.mark.asyncio
async def test_timeline_labels_are_escaped_in_svg_description_and_titles(monkeypatch):
    article = timeline()
    attack = '</desc><script>alert("unsafe")</script>'
    article["charts"][0]["rows"][0]["label"] = attack
    response = await page(monkeypatch, [article], "/analytics/2026-10-16-rent")
    assert attack not in response.text
    assert "&lt;/desc&gt;" in response.text
