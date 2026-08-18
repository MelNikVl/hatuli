import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import build_photo_evidence_batch_manifest as batch_manifest


def test_build_manifest_preserves_candidate_order_and_deduplicates_listing_ids():
    manifest = batch_manifest.build_manifest([
        {"candidate_id": 10, "listing_id_a": "a", "listing_id_b": "b"},
        {"candidate_id": 11, "listing_id_a": "b", "listing_id_b": "c"},
    ])

    assert manifest["candidate_ids"] == [10, 11]
    assert manifest["listing_ids"] == ["a", "b", "c"]
    assert manifest["rows"][0]["candidate_id"] == 10
