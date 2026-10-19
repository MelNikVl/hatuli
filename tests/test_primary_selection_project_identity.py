"""Unknown building identities cannot create a persuasive price advantage."""
from datetime import datetime, timezone

from bot.public_comparison_routes import _primary_selection_offer


def offer(identifier, complex_id, price_per_m2):
    return {"id": "newbuild:" + str(identifier), "market_type": "primary", "complex_id": complex_id,
            "complex_name": "Проект", "rooms": 2, "area": 60, "price": int(price_per_m2 * 60),
            "price_per_m2": price_per_m2, "observed_at": datetime.now(timezone.utc).isoformat()}


def market(rows):
    return {"offers": rows, "generated_at": datetime.now(timezone.utc).isoformat()}


def test_unknown_project_cannot_compare_unrelated_unknown_buildings():
    selected = offer(1, None, 500_000)
    peers = [offer(index, None, 800_000) for index in range(2, 8)]
    result = _primary_selection_offer(selected, market(peers))
    assert not any("ниже" in reason for reason in result["reasons"])
    selected.pop("complex_id")
    assert not any("ниже" in reason for reason in _primary_selection_offer(selected, market(peers))["reasons"])


def test_known_project_uses_its_own_peers_and_skips_unknown_project_rows():
    selected = offer(1, 17, 500_000)
    peers = [offer(index, 17, 750_000) for index in range(2, 8)]
    others = [offer(index, None, 2_000_000) for index in range(10, 16)]
    others[0].pop("complex_id")
    result = _primary_selection_offer(selected, market(peers + others))
    assert any("33,3%" in reason for reason in result["reasons"])
