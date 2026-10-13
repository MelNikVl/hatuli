"""The readiness audit cannot accidentally open a writable DB or export cards."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from scripts.audit_sale_forecast_readiness import audit, public_artifact_audit


def test_artifact_audit_excludes_offer_and_training_feature_values(tmp_path):
    (tmp_path / "var").mkdir()
    (tmp_path / "models/liquidity").mkdir(parents=True)
    (tmp_path / "var/price-forecast-model.json").write_text(json.dumps({
        "target": "future_asking_price_conditional_on_remaining_listed",
        "audit": {"input_listings": 2, "seller_phone": "PRIVATE-PHONE"},
        "horizons": [{"horizon_days": 7, "status": "insufficient_history", "folds": []}],
    }), encoding="utf-8")
    (tmp_path / "var/price-forecasts.json").write_text(json.dumps({
        "offers": {"123": {"address": "PRIVATE-ADDRESS", "seller_phone": "PRIVATE-PHONE"}},
    }), encoding="utf-8")
    (tmp_path / "models/liquidity/exit14_active.json").write_text(json.dumps({"version": "20261012T021526"}), encoding="utf-8")
    (tmp_path / "models/liquidity/exit14_20261012T021526.json").write_text(json.dumps({
        "metrics": {"auc_cv": .67, "n": 200},
        "categories": {"district": ["PRIVATE-ADDRESS"]},
        "training_examples": [{"seller_phone": "PRIVATE-PHONE"}],
    }), encoding="utf-8")
    result = public_artifact_audit(tmp_path)
    assert result["asking_price_forecast"]["published_offers"] == 1
    assert result["liquidity_models"]["exit14"]["metrics"]["auc_cv"] == .67
    assert "PRIVATE" not in json.dumps(result)
    assert "offers" not in result["asking_price_forecast"]


@pytest.mark.parametrize("version", ["../../private", "../20261012T021526", "12345678901234/"])
def test_model_pointer_cannot_read_outside_model_metadata(tmp_path, version):
    (tmp_path / "models/liquidity").mkdir(parents=True)
    (tmp_path / "models/liquidity/exit14_active.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    assert public_artifact_audit(tmp_path)["liquidity_models"] == {}


class FakeTransaction:
    async def __aenter__(self):
        pass

    async def __aexit__(self, *args):
        pass


class FakeConnection:
    def __init__(self, readonly):
        self.readonly = readonly
        self.closed = False
        self.transaction_options = None
        self.fetched = []

    def transaction(self, **kwargs):
        self.transaction_options = kwargs
        return FakeTransaction()

    async def fetchval(self, query):
        return self.readonly if query.startswith("SHOW") else "2026-10-13T10:45:00Z"

    async def fetch(self, query):
        self.fetched.append(query)
        return []

    async def fetchrow(self, query, identifiers):
        self.fetched.append(query)
        assert identifiers == []
        return {"matched_listings": 0, "fresh_listings": 0}

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("readonly", ["on", "off"])
async def test_raw_connection_and_transaction_must_both_be_read_only(monkeypatch, tmp_path, readonly):
    connection = FakeConnection(readonly)
    seen = {}

    async def connect(dsn, **kwargs):
        seen.update(kwargs)
        return connection

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
    if readonly == "off":
        with pytest.raises(RuntimeError, match="read-only"):
            await audit("dummy-dsn", tmp_path)
        assert connection.fetched == []
    else:
        report = await audit("dummy-dsn", tmp_path)
        assert report["database_writes"] == 0
        assert report["manual_outcome_progress"]["sold"] == 0
        assert report["manual_outcome_progress"]["closing_price_fields_found"] == []
        assert report["manual_outcome_progress"]["closing_date_fields_found"] == []
        assert connection.fetched and all(query.strip().startswith(("SELECT", "WITH")) for query in connection.fetched)
    assert seen["server_settings"]["default_transaction_read_only"] == "on"
    assert connection.transaction_options == {"readonly": True, "isolation": "repeatable_read"}
    assert connection.closed is True
