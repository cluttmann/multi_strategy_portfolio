"""Quote timestamp/spread validation, sharing the existing market-data cache.

The quote has its OWN timestamps: refreshing it must not freshen cached SMAs.
Execution uses IEX. No delayed SIP, SIP subscription or last-trade fallback.
"""
import datetime as dt
from .ledger import dec,SafetyStop
from .timestamps import parse_timestamp
import requests


def quote_price(quote,now,require_fresh):
    bid,ask=dec(quote.get('bp',0)),dec(quote.get('ap',0))
    if bid<=0 or ask<bid: raise SafetyStop('Missing or crossed bid/ask')
    mid=(bid+ask)/2
    if require_fresh:
        stamp=parse_timestamp(quote['t'])
        age=(now-stamp).total_seconds()
        if age < -5 or age>300: raise SafetyStop(f'Quote stale ({age:.0f}s)')
        if (ask-bid)/mid>dec('.009'): raise SafetyStop('Quote spread exceeds 0.9%')
    return mid


# Daily/upgrade plans: five-minute source timestamp limit (shared-etf-operations).
# IEX re-stamps a quote only when its top of book changes, so a quiet T-bill ETF
# (USFR/BIL) is often 1-4 minutes "old" while still current. 30 s here blocked
# the confirmed WLDU/SPXL exits on 2026-11-18..20. Monthly attempts keep 30 s.
PLANNING_QUOTE_MAX_AGE=300
# The trend sleeves' T-bill parking ETFs move ~0.01 %/day. BIL's newest IEX quote
# was 401 s old at 13:20 ET on 2026-11-23; one stale defensive leg aborts the whole
# daily plan, exits included. The spread check still applies to them.
TBILL_PLANNING_SYMBOLS=frozenset({'USFR','BIL'})
TBILL_PLANNING_QUOTE_MAX_AGE=900


def planning_max_age(symbol):
    return TBILL_PLANNING_QUOTE_MAX_AGE if symbol in TBILL_PLANNING_SYMBOLS else PLANNING_QUOTE_MAX_AGE


def get_price(bot,api,symbol,env,require_fresh=False):
    max_age=planning_max_age(symbol)
    quote=get_quote(bot,api,symbol,env,max_age=max_age)
    mid=validate_quote(quote,max_age=max_age)
    if require_fresh and (dec(quote['ap'])-dec(quote['bp']))/mid>dec('.009'):
        raise SafetyStop(f'{symbol}: quote spread exceeds 0.9%')
    return mid


EET_SPREAD_SOFT=dec('.003')
EET_SPREAD_HARD=dec('.005')
EET_FIRST_CONCESSION=dec('.001')
EET_LATER_CONCESSION=dec('.0015')
EET_BUY_PRICE_POLICY='iex-bid-cap-v1'
EET_BID_CAP_CONCESSION=dec('.001')
MIDPOINT_PRICE_POLICY='iex-midpoint-v1'
_LEGACY_EET_BUY_POLICY=object()


class QuoteDeferred(RuntimeError):
    """A valid market whose spread is temporarily outside the entry policy."""


def validate_quote(quote,now=None,max_age=30):
    now=now or dt.datetime.now(dt.timezone.utc)
    if quote.get('source')!='iex':raise SafetyStop('Execution quote source must be IEX')
    bid,ask=dec(quote.get('bp',0)),dec(quote.get('ap',0))
    if bid<=0 or ask<=0 or ask<bid or dec(quote.get('bs',0))<=0 or dec(quote.get('as',0))<=0:
        raise SafetyStop('Missing, crossed or zero-depth IEX market')
    try:age=(now-parse_timestamp(quote['t'])).total_seconds()
    except (KeyError,ValueError,TypeError) as exc:raise SafetyStop('Malformed IEX quote timestamp') from exc
    if age< -5 or age>max_age:raise SafetyStop(f'IEX quote stale ({age:.0f}s)')
    return (bid+ask)/2


def execution_price_policy(symbol,side,risk_exit=False,eet_buy_price_policy=_LEGACY_EET_BUY_POLICY):
    """Only an absent marker enables legacy buys; explicit unknowns fail closed."""
    if symbol=='EET' and side=='buy' and not risk_exit:
        if eet_buy_price_policy is _LEGACY_EET_BUY_POLICY:return MIDPOINT_PRICE_POLICY
        if eet_buy_price_policy!=EET_BUY_PRICE_POLICY:raise SafetyStop('Unknown EET buy price policy')
        return EET_BUY_PRICE_POLICY
    return MIDPOINT_PRICE_POLICY


def execution_limit(quote,symbol,side,attempt=0,now=None,risk_exit=False,eet_buy_price_policy=_LEGACY_EET_BUY_POLICY):
    from decimal import Decimal,ROUND_DOWN,ROUND_UP
    mid=validate_quote(quote,now)
    bid,ask=dec(quote['bp']),dec(quote['ap'])
    policy=execution_price_policy(symbol,side,risk_exit,eet_buy_price_policy)
    if policy==EET_BUY_PRICE_POLICY:
        # IEX is one venue, not NBBO. A wide ask cannot raise this purchase cap,
        # and retries always retain the same ten-basis-point bid concession.
        return min(ask,bid*(1+EET_BID_CAP_CONCESSION)).quantize(Decimal('.01'),rounding=ROUND_DOWN)
    spread=(ask-bid)/mid
    if symbol=='EET' and not risk_exit:
        if spread>EET_SPREAD_HARD:raise QuoteDeferred('spread_above_hard_limit')
        concession=EET_FIRST_CONCESSION if attempt==0 else EET_LATER_CONCESSION
    else:
        if spread>dec('.009'):raise QuoteDeferred('spread_above_hard_limit')
        concession=dec('.005')
    # Even below the soft EET spread threshold the concession bound applies.
    # At .003-.005 the bounded price deliberately rests inside the market.
    if side=='buy':return min(ask,mid*(1+concession)).quantize(Decimal('.01'),rounding=ROUND_DOWN)
    if side=='sell':return max(bid,mid*(1-concession)).quantize(Decimal('.01'),rounding=ROUND_UP)
    raise SafetyStop('Invalid execution side')


def get_quote(bot,api,symbol,env='live',use_cache=True,persist=True,now=None,max_age=30):
    """Read an actual fresh IEX market; persist=False is a read-only preflight.

    Quote source and source timestamp are independent of the valuation/SMA cache.
    A cached SIP quote never satisfies an IEX request. No provider fallback exists.
    A cached quote is reused only within 30 s; max_age applies to a new download.
    """
    injected_now=now
    now=now or dt.datetime.now(dt.timezone.utc)
    data=(bot.get_all_market_data(symbol,env) or {}) if use_cache else {}
    cached=data.get('execution_quote')
    if cached and cached.get('source')=='iex':
        try:validate_quote(cached,now);return cached
        except SafetyStop:pass
    response=requests.get(f'https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest',
                          headers=bot.get_auth_headers(api),params={'feed':'iex'},timeout=(5,20))
    response.raise_for_status()
    try:quote={**response.json()['quote'],'source':'iex'}
    except (KeyError,TypeError,ValueError) as exc:raise SafetyStop(f'{symbol}: malformed IEX response') from exc
    downloaded_at=injected_now or dt.datetime.now(dt.timezone.utc)
    validate_quote(quote,downloaded_at,max_age=max_age)
    if persist:
        bot.get_firestore_client().collection(f'market-data-{env}').document(bot.normalize_symbol(symbol)).set(
            {'execution_quote':quote,'execution_quote_source':'iex','execution_quote_downloaded_at':downloaded_at},merge=True)
    return quote
