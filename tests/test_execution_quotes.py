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


class _QuoteBot:
    """Real get_price/get_quote path; only the HTTP response and cache are fakes."""
    def __init__(self,cached=None):self.cached=cached;self.writes=[]
    def get_all_market_data(self,symbol,env):return {'execution_quote':self.cached} if self.cached else {}
    def get_auth_headers(self,api):return {}
    def normalize_symbol(self,symbol):return symbol
    def get_firestore_client(self):
        bot=self
        class Db:
            def collection(self,name):return self
            def document(self,symbol):return self
            def set(self,data,merge=False):bot.writes.append(data)
        return Db()


def _serve_iex_quote(monkeypatch,age_seconds,calls=None):
    import requests
    def latest(url,**kwargs):
        if calls is not None:calls.append(url)
        stamp=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age_seconds)).isoformat()
        class Response:
            def raise_for_status(self):pass
            def json(self):return {'quote':{'bp':'50.51','ap':'50.52','bs':100,'as':100,'t':stamp}}
        return Response()
    monkeypatch.setattr(requests,'get',latest)


@pytest.mark.parametrize('age',[34,137,239])
def test_daily_planning_price_accepts_quiet_etf_within_five_minutes(monkeypatch,age):
    """The 2026-11-18..20 BIL/USFR ages that blocked the daily risk exits."""
    from execution.quotes import get_price
    _serve_iex_quote(monkeypatch,age)
    assert str(get_price(_QuoteBot(),{},'USFR','live',require_fresh=True))=='50.515'


def test_daily_planning_price_still_rejects_quote_older_than_five_minutes(monkeypatch):
    from execution.quotes import get_price
    _serve_iex_quote(monkeypatch,301)
    with pytest.raises(SafetyStop,match='IEX quote stale'):
        get_price(_QuoteBot(),{},'USFR','live',require_fresh=True)


def test_daily_planning_refetches_instead_of_reusing_cache_older_than_30s(monkeypatch):
    from execution.quotes import get_price
    calls=[]
    _serve_iex_quote(monkeypatch,0,calls)
    old=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=120)).isoformat()
    cached={'bp':'1','ap':'1.001','bs':1,'as':1,'t':old,'source':'iex'}
    assert str(get_price(_QuoteBot(cached),{},'USFR','live',require_fresh=True))=='50.515'
    assert len(calls)==1


def test_monthly_attempt_quote_contract_stays_thirty_seconds(monkeypatch):
    from execution.quotes import get_quote
    _serve_iex_quote(monkeypatch,31)
    with pytest.raises(SafetyStop,match='IEX quote stale'):
        get_quote(_QuoteBot(),{},'USFR','live')


@pytest.mark.parametrize('stamp',['2026-09-25T19:49:55.978124906Z','2026-09-25T15:49:55.9-04:00'])
def test_alpaca_fractional_timestamp_precision_on_cloud_python(stamp):
    assert str(quote_price({'bp':111.75,'ap':112.26,'t':stamp},NOW,True))=='112.005'
