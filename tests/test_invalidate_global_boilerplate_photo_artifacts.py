import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import invalidate_global_boilerplate_photo_artifacts as cleanup


def test_load_candidate_ids_reads_and_deduplicates_audit_manifest(tmp_path):
    path = tmp_path / "audit.json"
    path.write_text(json.dumps({"affected_candidate_ids": [5, "7", 5]}), encoding="utf-8")

    assert cleanup.load_candidate_ids(str(path)) == [5, 7]


@pytest.mark.parametrize("payload", [[], {"affected_candidate_ids": [0]}, {"wrong": [1]}])
def test_load_candidate_ids_rejects_non_frozen_or_invalid_manifest(tmp_path, payload):
    path = tmp_path / "audit.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError):
        cleanup.load_candidate_ids(str(path))
