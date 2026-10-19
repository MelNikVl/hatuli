"""Run the shipped browser code in a deterministic Node DOM/fetch harness."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_deferred_responses_furniture_budget_percentages_and_empty_sections():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the deferred browser-state regression")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root/"tests/js/sublease_ui_harness.cjs"),
                             str(root/"static/rental-scanner.js")], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sublease UI regressions passed" in result.stdout
