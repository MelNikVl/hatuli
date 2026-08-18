import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import photo_evidence_ai_scan as ai_scan
import photo_evidence_scan as evidence_scan


def test_load_listing_ids_accepts_list_and_deduplicates(tmp_path):
    path = tmp_path / "listing_ids.json"
    path.write_text(json.dumps(["a", "b", "a"]), encoding="utf-8")

    assert ai_scan.load_listing_ids(str(path)) == ["a", "b"]


def test_load_listing_ids_accepts_named_manifest_field(tmp_path):
    path = tmp_path / "listing_ids.json"
    path.write_text(json.dumps({"listing_ids": ["a", "b"]}), encoding="utf-8")

    assert ai_scan.load_listing_ids(str(path)) == ["a", "b"]


def test_load_listing_ids_rejects_malformed_manifest(tmp_path):
    path = tmp_path / "listing_ids.json"
    path.write_text(json.dumps({"listing_ids": ["a", 1]}), encoding="utf-8")

    with pytest.raises(ValueError, match="JSON list"):
        ai_scan.load_listing_ids(str(path))


def test_load_candidate_ids_accepts_manifest_and_deduplicates(tmp_path):
    path = tmp_path / "candidate_ids.json"
    path.write_text(json.dumps({"candidate_ids": [7, "8", 7]}), encoding="utf-8")

    assert evidence_scan.load_candidate_ids(str(path)) == [7, 8]


@pytest.mark.parametrize("payload", [[], {"candidate_ids": [0]}, {"candidate_ids": [True]}])
def test_load_candidate_ids_rejects_empty_or_invalid_manifest(tmp_path, payload):
    path = tmp_path / "candidate_ids.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        evidence_scan.load_candidate_ids(str(path))
