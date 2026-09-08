"""tests/test_ai_tools_envelope.py — Этап 2 задачи "Hatuli API v1 для AI
tools": контракт envelope сам по себе, без БД (bot.ai_tools.core уже
возвращает готовый ToolResult, здесь проверяется только сборка в финальный
JSON-контракт, build_envelope())."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.ai_tools.envelope import ToolResult, build_envelope


def test_envelope_has_exact_contract_keys():
    result = ToolResult(
        data={"foo": "bar"}, as_of="2026-09-08T00:00:00Z", confidence=0.75,
        evidence=[{"source": "x"}], warnings=["w1"], methodology_version="v1",
    )
    envelope = build_envelope("listing", "123", result)
    assert set(envelope.keys()) == {
        "entity_type", "entity_id", "as_of", "data", "confidence",
        "evidence", "warnings", "methodology_version",
    }
    assert envelope["entity_type"] == "listing"
    assert envelope["entity_id"] == "123"
    assert envelope["as_of"] == "2026-09-08T00:00:00Z"
    assert envelope["data"] == {"foo": "bar"}
    assert envelope["confidence"] == 0.75
    assert envelope["evidence"] == [{"source": "x"}]
    assert envelope["warnings"] == ["w1"]
    assert envelope["methodology_version"] == "v1"


def test_envelope_defaults_are_none_or_empty_not_invented():
    """Задача, явно: "если поле реально нет — не выдумывать". Дефолты
    ToolResult должны остаться None/[] — не превращаться в 0/"" молча."""
    result = ToolResult(data={"a": 1})
    envelope = build_envelope("complex", 42, result)
    assert envelope["as_of"] is None
    assert envelope["confidence"] is None
    assert envelope["evidence"] == []
    assert envelope["warnings"] == []
    assert envelope["methodology_version"] is None
    assert envelope["data"] == {"a": 1}


def test_envelope_preserves_nested_insufficient_data_and_confidence_inside_data():
    """envelope не должен ТЕРЯТЬ insufficient_data/confidence, если они
    посчитаны внутри data (напр. per-section у complex_market_profile) —
    build_envelope не трогает data вообще, поэтому это по построению
    гарантия, но тест фиксирует контракт явно (регрессия недопустима)."""
    nested = {
        "price": {"sample_size": 3, "insufficient_data": True, "median_price_m2": None},
        "liquidity": {"sample_size_properties": 40, "insufficient_data": False},
    }
    result = ToolResult(data=nested, confidence=None, evidence=[{"source": "x"}])
    envelope = build_envelope("complex", 1, result)
    assert envelope["data"]["price"]["insufficient_data"] is True
    assert envelope["data"]["liquidity"]["insufficient_data"] is False
    assert envelope["evidence"] == [{"source": "x"}]
