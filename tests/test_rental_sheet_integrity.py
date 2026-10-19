from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
from bot.core import sheets_sync_rental as sheets


def test_unknown_price_and_studio_are_preserved_without_claiming_unknown_monthly_units():
    rows = sheets.rental_sheet_rows([{'id':'123456','rooms':0,'price':None,'prop_type':'apartment'},
        {'id':'123457','price':10000,'prop_type':'commercial','last_seen':datetime(2026,10,13,tzinfo=timezone.utc)}])
    assert rows[1][2]==0 and rows[1][6]==''
    assert rows[2][6]==10000 and rows[2][7]=='не определена'
    assert '₸/мес' not in str(rows[0])


@pytest.mark.asyncio
async def test_failed_export_reports_failure_and_does_not_contact_google(monkeypatch):
    monkeypatch.setattr('bot.db.pg.fetch',AsyncMock(side_effect=RuntimeError('isolated DB failure')))
    google = AsyncMock()
    monkeypatch.setattr(sheets.gspread,'service_account',google)
    assert await sheets.sync_rental_to_sheets() is False
    google.assert_not_called()
