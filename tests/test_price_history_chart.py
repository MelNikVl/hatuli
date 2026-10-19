"""Linear asking-price charts only use actual dated NEW observations."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
import pytest

from bot.core.comparison_offer_details import price_history
from bot.core.price_history_chart import price_history_chart


NOW = datetime(2026, 10, 13, 20, 0, tzinfo=timezone.utc)


def event(day, old, new):
    return {"at": (NOW-timedelta(days=10-day)).isoformat(), "old_price": old, "new_price": new}


def test_dates_use_elapsed_time_and_undated_old_price_is_not_plotted():
    history = price_history([event(0, 36_000_000, 35_000_000), event(1, 35_000_000, 34_000_000),
                             event(10, 34_000_000, 33_000_000)], 33_000_000, now=NOW)
    graph = price_history_chart(history)
    assert [point["x"] for point in graph["points"]] == [64, 92, 344]
    assert [point["price"] for point in graph["points"]] == [35_000_000, 34_000_000, 33_000_000]
    assert len(graph["segments"]) == 1
    assert len(graph["segments"][0]["points"].split()) == 3
    assert graph["points"][0]["old_price"] == 36_000_000  # tooltip/table evidence only
    assert graph["points"][0]["undated_baseline"]
    assert graph["net_change"] == -3_000_000
    assert graph["net_pct"] == -8.33


@pytest.mark.parametrize("prices,directions,slope", [
    ([35_000_000, 34_000_000, 33_000_000], ["down", "down", "down"], "down"),
    ([35_000_000, 36_000_000, 37_000_000], ["down", "up", "up"], "up"),
    ([35_000_000, 35_000_000, 35_000_000], ["down", "same", "same"], "flat"),
])
def test_down_up_and_flat_series_are_linear_dated_observations(prices, directions, slope):
    events = [event(0, 36_000_000, prices[0]), event(4, prices[0], prices[1]), event(10, prices[1], prices[2])]
    graph = price_history_chart(price_history(events, prices[-1], now=NOW))
    assert [point["direction"] for point in graph["points"]] == directions
    assert len(graph["segments"]) == 1
    coordinates = [tuple(map(float, item.split(","))) for item in graph["segments"][0]["points"].split()]
    assert [point[0] for point in coordinates] == [64, 176, 344]
    if slope == "flat":
        assert len({point[1] for point in coordinates}) == 1
    elif slope == "down":
        assert coordinates[0][1] < coordinates[1][1] < coordinates[2][1]
    else:
        assert coordinates[0][1] > coordinates[1][1] > coordinates[2][1]


def test_increases_and_zero_net_change_preserve_both_recorded_values():
    history = price_history([event(0, 35_000_000, 36_000_000), event(10, 36_000_000, 35_000_000)],
                            35_000_000, now=NOW)
    graph = price_history_chart(history)
    assert [row["direction"] for row in graph["points"]] == ["up", "down"]
    assert graph["net_change"] == 0
    assert len(graph["points"]) == 2 and len(graph["segments"]) == 1


def test_gap_is_not_connected_and_has_no_total_change_claim():
    history = price_history([event(0, 36_000_000, 35_000_000), event(10, 38_000_000, 34_000_000)],
                            34_000_000, now=NOW)
    graph = price_history_chart(history)
    assert graph["has_gap"] is True
    assert graph["segments"] == []
    assert len(graph["points"]) == 2
    assert graph["net_change"] is None and graph["net_pct"] is None


def test_only_continuous_segments_are_joined_on_either_side_of_gap():
    history = price_history([event(0, 36_000_000, 35_000_000), event(1, 35_000_000, 34_000_000),
                             event(8, 38_000_000, 37_000_000), event(10, 37_000_000, 36_000_000)],
                            36_000_000, now=NOW)
    graph = price_history_chart(history)
    assert len(graph["segments"]) == 2
    assert [len(segment["points"].split()) for segment in graph["segments"]] == [2, 2]
    assert len(graph["points"]) == 4 and graph["net_change"] is None
    assert graph["segments"][0]["points"].endswith(f'{graph["points"][1]["x"]},{graph["points"][1]["y"]}')
    assert graph["segments"][1]["points"].startswith(f'{graph["points"][2]["x"]},{graph["points"][2]["y"]}')


def test_single_change_keeps_one_real_point_and_verified_total_without_fake_line():
    history = price_history([event(0, 36_000_000, 35_000_000)], 35_000_000, now=NOW)
    graph = price_history_chart(history)
    assert graph["segments"] == []
    assert len(graph["points"]) == 1 and graph["points"][0]["price"] == 35_000_000
    assert graph["points"][0]["old_price"] == 36_000_000
    assert graph["net_change"] == -1_000_000
    assert graph["date_ticks"] == [{"x": 204, "label": "04.10.2026", "anchor": "middle"}]


def test_single_developer_observation_has_one_point_and_no_invented_baseline():
    history = price_history([event(0, None, 35_000_000)], 35_000_000, now=NOW, baseline_unknown=True)
    graph = price_history_chart(history)
    assert graph["segments"] == []
    assert len(graph["points"]) == 1 and graph["points"][0]["old_price"] is None
    assert graph["net_change"] is None


def test_unknown_first_baseline_does_not_prevent_line_between_known_new_prices():
    history = price_history([event(0, None, 35_000_000), event(10, 35_000_000, 34_000_000)],
                            34_000_000, now=NOW, baseline_unknown=True)
    graph = price_history_chart(history)
    assert len(graph["points"]) == 2 and len(graph["segments"]) == 1
    assert graph["net_change"] == -1_000_000


def test_same_instant_multiple_values_have_markers_without_vertical_line():
    history = price_history([event(0, 36_000_000, 35_000_000), event(0, 35_000_000, 34_000_000)],
                            34_000_000, now=NOW)
    graph = price_history_chart(history)
    assert {point["x"] for point in graph["points"]} == {204}
    assert graph["segments"] == []
    assert len(graph["date_ticks"]) == 1
    assert graph["net_change"] == -2_000_000


def test_conflicting_same_instant_prices_do_not_connect_surrounding_observations():
    history = price_history([event(0, 40_000_000, 39_000_000), event(2, 39_000_000, 38_000_000),
                             event(2, 38_000_000, 37_000_000), event(5, 37_000_000, 36_000_000),
                             event(10, 36_000_000, 35_000_000)], 35_000_000, now=NOW)
    graph = price_history_chart(history)
    assert len(graph["segments"]) == 1  # last two unique observations only
    assert len(graph["segments"][0]["points"].split()) == 2
    assert graph["segments"][0]["points"].startswith(f'{graph["points"][-2]["x"]},')


def test_same_calendar_day_uses_actual_different_times_not_even_spacing():
    first = NOW-timedelta(hours=15)
    events = [{"at": (first+timedelta(hours=hour)).isoformat(), "old_price": old, "new_price": new}
              for hour, old, new in [(0, 36_000_000, 35_000_000), (1, 35_000_000, 34_000_000), (10, 34_000_000, 33_000_000)]]
    graph = price_history_chart(price_history(events, 33_000_000, now=NOW))
    assert {point["date_label"].split(",")[0] for point in graph["points"]} == {"13.10.2026"}
    assert [point["x"] for point in graph["points"]] == [64, 92, 344]
    assert len(graph["segments"]) == 1


def test_cache_disagreement_suppresses_total_but_preserves_dated_record():
    history = price_history([event(0, 36_000_000, 35_000_000)], 34_000_000, now=NOW)
    graph = price_history_chart(history)
    assert graph["net_change"] is None
    assert {point["price"] for point in graph["points"]} == {35_000_000}
    assert 34_000_000 not in {point["price"] for point in graph["points"]}


def test_small_real_observed_changes_have_distinct_axis_labels():
    history = price_history([event(0, 36_000_000, 35_000_000), event(10, 35_000_000, 34_999_000)],
                            34_999_000, now=NOW)
    graph = price_history_chart(history)
    assert {axis["label"] for axis in graph["axis"]} == {"35 млн", "34,999 млн"}


@pytest.mark.parametrize("history", [None, {}, {"events": []}, {"events": {}},
    {"events": [{"at": "2026-10-13", "old_price": 1, "new_price": 2}]},
    {"events": [event(0, 35_000_000, float("nan"))]},
    {"events": [event(0, 35_000_000, True)]},
    {"events": [event(10, 36_000_000, 35_000_000), event(0, 35_000_000, 34_000_000)]},
    {"events": [event(0, 36_000_000, 35_000_000), event(10, None, 34_000_000)]}])
def test_invalid_or_unavailable_data_does_not_create_svg_geometry(history):
    assert price_history_chart(history) is None


def test_source_text_and_forged_coordinates_are_not_copied_to_chart():
    graph = price_history_chart({"status": "consistent", "events": [{**event(0, 36_000_000, 35_000_000),
        "date_label": '<script>CHART-XSS</script>', "x": '" onload="CHART-XSS', "points": "PRIVATE"}]})
    assert "CHART-XSS" not in repr(graph) and "PRIVATE" not in repr(graph)
    assert graph["points"][0]["x"] == 204


@pytest.fixture
def render_history():
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "bot/templates"),
                      autoescape=select_autoescape(["html"]))
    env.globals["price_history_chart"] = price_history_chart
    env.filters["fmt_num"] = lambda number, digits=0: f"{number:,.{digits}f}".replace(",", " ").replace(".", ",")
    return env.get_template("_price_history_chart.html").module.render_price_history


def test_macro_renders_polyline_and_real_points_without_step_or_vertical_events(render_history):
    history = price_history([event(0, 36_000_000, 35_000_000), event(10, 35_000_000, 34_000_000)],
                            34_000_000, now=NOW)
    html = str(render_history(history))
    assert html.count('<polyline class="hatuli-price-chart-line"') == 1
    assert html.count('<circle class="hatuli-price-chart-point"') == 2
    assert 'hatuli-price-chart-event' not in html and 'hatuli-price-chart-connector' not in html
    assert 'Линии между зафиксированными значениями' in html
    assert 'дата её фиксации неизвестна' in html
    assert '36 000 000' in html and '<table>' in html  # undated first old price retained


def test_macro_single_change_stays_visible_with_point_table_and_total(render_history):
    history = price_history([event(0, 36_000_000, 35_000_000)], 35_000_000, now=NOW)
    html = str(render_history(history))
    assert 'data-price-history-chart' in html
    assert '<polyline' not in html and html.count('<circle') == 1
    assert '−1 000 000 ₸' in html and '36 000 000' in html and '<table>' in html
