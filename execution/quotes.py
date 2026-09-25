"""Quote timestamp/spread validation, sharing the existing market-data cache.

The quote has its OWN timestamps: refreshing it must not freshen cached SMAs.
IEX is the subscribed real-time feed. No delayed SIP or last-trade fallback.
"""
import datetime as dt
from .ledger import dec,SafetyStop
import requests


def quote_price(quote,now,require_fresh):
    bid,ask=dec(quote.get('bp',0)),dec(quote.get('ap',0))
    if bid<=0 or ask<bid: raise SafetyStop('Missing or crossed bid/ask')
    mid=(bid+ask)/2
    if require_fresh:
        stamp=dt.datetime.fromisoformat(quote['t'].replace('Z','+00:00'))
        age=(now-stamp).total_seconds()
        if age < -5 or age>300: raise SafetyStop(f'Quote stale ({age:.0f}s)')
        if (ask-bid)/mid>dec('.009'): raise SafetyStop('Quote spread exceeds 0.9%')
    return mid


def get_price(bot,api,symbol,env,require_fresh=False):
    now=dt.datetime.now(dt.timezone.utc)
    # Read the canonical cache. A missing/old general cache is not usable for
    # alerts, but the independently stamped quote can still be fresh.
    data=bot.get_all_market_data(symbol,env) or {}
    quote=data.get('execution_quote')
    if quote:
        stamp=dt.datetime.fromisoformat(quote['t'].replace('Z','+00:00'))
        if 0<=(now-stamp).total_seconds()<30:
            return quote_price(quote,now,require_fresh)
    response=requests.get(f'https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest',
                          headers=bot.get_auth_headers(api),params={'feed':'iex'},timeout=(5,20))
    response.raise_for_status();quote=response.json()['quote']
    try: price=quote_price(quote,now,require_fresh)
    except SafetyStop as exc: raise SafetyStop(f'{symbol}: {exc}') from exc
    bot.get_firestore_client().collection(f'market-data-{env}').document(bot.normalize_symbol(symbol)).set(
        {'execution_quote':quote,'execution_quote_downloaded_at':now},merge=True)
    return price
