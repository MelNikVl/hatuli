import pytest

from bot.public_comparison_routes import _deposit_changes


@pytest.mark.parametrize("status,checked,pending,resolved", [
    ("verified", "2026-10-13T13:00:00+00:00", False, True),
    ("needs_review", "2026-10-13T13:00:00+00:00", False, False),
    ("unavailable", "2026-10-13T13:00:00+00:00", False, False),
    ("verified", "2026-10-13T08:00:00+00:00", False, False),
    ("verified", "2026-10-13T13:00:00", False, False),
    ("verified", "2026-10-13T13:00:00+00:00", True, False),
])
def test_reviewed_event_does_not_keep_showing_an_unresolved_warning(status, checked, pending, resolved):
    offer = {"id": "ordinary-12", "name": "Обычный вклад", "status": status, "checked_at": checked}
    if pending:
        offer["pending_terms_hash"] = "still-unreviewed"
    events = _deposit_changes({"offers": [offer], "changes": [
        {"kind": "conditions_changed", "id": offer["id"], "at": "2026-10-13T09:00:00+00:00"}]})
    assert ("проверено после" in events[0]["label"]) is resolved
    assert ("требует повторной проверки" in events[0]["label"]) is not resolved


def test_reviewed_document_event_uses_current_document_status():
    catalog = {"sources": [{"url": "https://home.kz/conditions.pdf", "status": "verified",
                           "checked_at": "2026-10-13T13:00:00+00:00"}], "changes": [
        {"kind": "document_changed", "url": "https://home.kz/conditions.pdf", "at": "2026-10-13T09:00:00+00:00"}]}
    assert "проверено после" in _deposit_changes(catalog)[0]["label"]
    catalog["sources"][0]["review_required"] = True
    assert "проверено после" not in _deposit_changes(catalog)[0]["label"]
