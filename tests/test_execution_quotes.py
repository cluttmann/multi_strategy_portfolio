import datetime as dt
import pytest
from execution.quotes import quote_price
from execution.ledger import SafetyStop

NOW=dt.datetime(2026,9,25,19,50,tzinfo=dt.timezone.utc)

def test_stale_quote_is_rejected_even_if_recently_downloaded():
    with pytest.raises(SafetyStop): quote_price({'bp':100,'ap':101,'t':'2026-09-24T19:50:00Z'},NOW,True)

def test_crossed_wide_or_missing_market_is_rejected():
    for bid,ask in [(100,99),(100,110),(0,100)]:
        with pytest.raises(SafetyStop): quote_price({'bp':bid,'ap':ask,'t':'2026-09-25T19:50:00Z'},NOW,True)

def test_current_quote_midpoint_preserves_decimal_precision():
    assert str(quote_price({'bp':111.75,'ap':112.26,'t':'2026-09-25T19:49:55Z'},NOW,True))=='112.005'


def test_quote_uses_existing_five_minute_cache_window_for_quiet_etfs():
    assert quote_price({'bp':100,'ap':100.1,'t':'2026-09-25T19:46:00Z'},NOW,True)>100
    with pytest.raises(SafetyStop): quote_price({'bp':100,'ap':100.1,'t':'2026-09-25T19:44:59Z'},NOW,True)
