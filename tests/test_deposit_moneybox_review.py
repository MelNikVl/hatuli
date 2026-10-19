from datetime import datetime, timezone

import pytest

from bot.core.deposit_sources import FREEDOM, parse_source
from tests.test_deposit_sources import freedom_html


@pytest.mark.parametrize("prefix", ["до ", "от ", "рекламная ставка "])
@pytest.mark.parametrize("rate", ["14.90%", "15.95%"])
def test_moneybox_ambiguous_rate_text_is_excluded(prefix, rate):
    # A matched numeric fragment must not turn an advertised range into an
    # exact rate usable by the apartment/deposit calculation.
    html = freedom_html().replace(rate, prefix + rate, 1)
    with pytest.raises(ValueError, match="Неоднозначные ставки Копилки"):
        parse_source(FREEDOM, html, datetime(2026, 10, 13, tzinfo=timezone.utc))


def test_moneybox_extra_term_does_not_shift_monthly_and_daily_annual_rates():
    html = freedom_html().replace("14.90% 12 мес.", "13.80% 6 мес. 14.90% 12 мес.")
    html = html.replace("15.95% 12 мес.", "14.75% 6 мес. 15.95% 12 мес.", 1)
    annual = [item for item in parse_source(FREEDOM, html, datetime(2026, 10, 13, tzinfo=timezone.utc))
              if item["term_months"] == 12]
    assert [(item["capitalization"], item["nominal_pct"], item["effective_pct"]) for item in annual] == [
        ("ежемесячная", 14.9, 15.95), ("ежедневная", 14.8, 15.95)]
