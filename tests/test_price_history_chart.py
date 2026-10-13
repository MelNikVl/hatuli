"""Recorded changes retain dates, gaps and unknown initial prices in charts."""
from datetime import datetime, timedelta, timezone

import pytest

from bot.core.comparison_offer_details import price_history
from bot.core.price_history_chart import price_history_chart


NOW = datetime(2026, 10, 13, 20, 0, tzinfo=timezone.utc)


def event(day, old, new):
    return {"at": (NOW-timedelta(days=10-day)).isoformat(), "old_price": old, "new_price": new}


def test_dates_use_elapsed_time_and_first_old_price_is_never_backdated():
    history = price_history([event(0, 36_000_000, 35_000_000), event(1, 35_000_000, 34_000_000),
                             event(10, 34_000_000, 33_000_000)], 33_000_000, now=NOW)
    graph = price_history_chart(history)
    assert [row["x"] for row in graph["changes"]] == [64, 92, 344]
    assert graph["points"][0]["x"] == graph["points"][1]["x"] == 64
    assert graph["points"][0]["price"] == 36_000_000
    assert graph["connectors"][0]["x1"] == 64  # no interval before the first real event
    assert graph["net_change"] == -3_000_000
    assert graph["net_pct"] == -8.33


def test_increases_are_drawn_and_zero_net_change_keeps_both_changes():
    history = price_history([event(0, 35_000_000, 36_000_000), event(10, 36_000_000, 35_000_000)],
                            35_000_000, now=NOW)
    graph = price_history_chart(history)
    assert [row["direction"] for row in graph["changes"]] == ["up", "down"]
    assert graph["net_change"] == 0
    assert len(graph["points"]) == 4


def test_gap_is_not_connected_and_has_no_total_change_claim():
    history = price_history([event(0, 36_000_000, 35_000_000), event(10, 38_000_000, 34_000_000)],
                            34_000_000, now=NOW)
    graph = price_history_chart(history)
    assert graph["has_gap"] is True
    assert graph["connectors"] == []
    assert len(graph["changes"]) == 2
    assert graph["net_change"] is None and graph["net_pct"] is None


def test_single_developer_observation_has_one_point_and_no_invented_old_price():
    history = price_history([event(0, None, 35_000_000)], 35_000_000, now=NOW, baseline_unknown=True)
    graph = price_history_chart(history)
    assert graph["changes"] == graph["connectors"] == []
    assert len(graph["points"]) == 1
    assert graph["points"][0]["kind"] == "after"
    assert graph["net_change"] is None
    assert graph["date_ticks"] == [{"x": 204, "label": "04.10.2026", "anchor": "middle"}]


def test_same_instant_multiple_events_have_no_fake_horizontal_duration():
    history = price_history([event(0, 36_000_000, 35_000_000), event(0, 35_000_000, 34_000_000)],
                            34_000_000, now=NOW)
    graph = price_history_chart(history)
    assert {point["x"] for point in graph["points"]} == {204}
    assert all(line["x1"] == line["x2"] for line in graph["connectors"])
    assert len(graph["date_ticks"]) == 1


def test_cache_disagreement_suppresses_total_but_preserves_recorded_points():
    history = price_history([event(0, 36_000_000, 35_000_000)], 34_000_000, now=NOW)
    graph = price_history_chart(history)
    assert graph["net_change"] is None
    assert {point["price"] for point in graph["points"]} == {35_000_000, 36_000_000}
    assert 34_000_000 not in {point["price"] for point in graph["points"]}


def test_small_real_changes_have_distinct_axis_labels():
    history = price_history([event(0, 35_000_000, 34_999_000)], 34_999_000, now=NOW)
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
