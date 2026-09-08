"""tests/test_ai_tools_tool_registry.py — Этап 4: реестр capabilities для
будущего orchestrator-агента. Проверяет форму реестра и то, что каждый
`handler` реально резолвится в существующую, вызываемую (async) функцию —
не строка "в воздухе"."""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.ai_tools.tool_registry import TOOLS, TOOLS_BY_NAME, get_tool

EXPECTED_NAMES = {
    "resolve_listing", "property_history", "listing_analysis", "listing_risks",
    "complex_market_profile", "location_analysis", "data_quality_snapshot",
}


def test_registry_has_all_seven_capabilities():
    assert {t["name"] for t in TOOLS} == EXPECTED_NAMES


def test_every_tool_declares_required_fields_and_is_read_only():
    for tool in TOOLS:
        for field in ("name", "description", "input_schema", "output_schema", "read_only", "handler"):
            assert field in tool, f"{tool.get('name')} missing '{field}'"
        assert tool["read_only"] is True
        assert isinstance(tool["input_schema"], dict)
        assert isinstance(tool["output_schema"], dict)
        assert tool["description"]  # непустое


def test_every_handler_resolves_to_a_real_async_callable():
    import inspect

    for tool in TOOLS:
        module_path, func_name = tool["handler"].rsplit(".", 1)
        module = importlib.import_module(module_path)
        func = getattr(module, func_name)
        assert inspect.iscoroutinefunction(func), f"{tool['name']} handler is not async: {tool['handler']}"


def test_get_tool_lookup():
    assert get_tool("resolve_listing") is TOOLS_BY_NAME["resolve_listing"]
    assert get_tool("does_not_exist") is None
