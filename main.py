import os
from flask import Flask, jsonify
from google.cloud import secretmanager
from dotenv import load_dotenv
import requests
import json
import time
import pandas as pd
import pandas_market_calendars as mcal
import datetime
from google.cloud import firestore


app = Flask(__name__)

# Strategy allocation percentages for dynamic monthly investment calculation
# Investment amounts are calculated dynamically each month based on available cash and margin
strategy_allocations = {
    # Updated 2026-09-21: Regime SSO and 9-Sig retired, live positions (SSO,
    # TQQQ, AGG) liquidated the same day and the proceeds moved into AAA.
    #
    # The new weights are NOT a renormalization of the old ones. A faithful
    # 25-year replay of every sleeve — with each one's real rules, so DD-30
    # stops, 6m momentum, daily SMA gates and per-position leverage — put AAA
    # at Sharpe 0.87 and HFEA/SPXL at 0.45/0.44. Capital follows that ranking.
    #
    # Note these govern the MONTHLY CONTRIBUTION SPLIT, not standing weights.
    # As of 2026-09-21 the book itself sits at HFEA 38%, SPXL 35%, AAA 15%,
    # DM 12% — the 3x sleeves ran away over the years and were never trimmed.
    # Bringing the book to these targets is a separate, planned rebalancing.
    "hfea_allo":          0.125,   # 12.5% — HFEA UPRO/TMF/KMLM
    "spxl_allo":          0.125,   # 12.5% — SPXL SMA trend-gate
    "dual_momentum_allo": 0.25,    # 25%   — DM 2× best-of-3 (SPUU/QLD/EFO)
    "aaa_allo":           0.50,    # 50%   — 7-Asset Rotator
}

upro_allocation = 0.45
tmf_allocation = 0.25
kmlm_allocation = 0.3

# SPXL SMA holding fund config (for T-bills when SPY < 200-SMA)
spxl_sma_holding_fund = "SGOV"  # iShares 0-3 Month Treasury Bond ETF

# Strategy Ticker Ownership
# Each strategy has clear ticker ownership for simplified margin calculations and position tracking:
# - HFEA: UPRO, TMF, KMLM
# - SPXL SMA: SPXL, SGOV (SGOV is holding fund when bearish)
# - Dual Momentum: SPUU, QLD, EFO, BND (BND is defensive + vol-target overflow)
# - 7-Asset Rotator (AAA family): NTSD, SAA, EET, UBT, UST, UGL, DBC (top-3 selected monthly), SHV (defensive)

# Strategy ticker ownership mapping for cost basis recalculation
STRATEGY_SYMBOLS = {
    "hfea": ["UPRO", "TMF", "KMLM"],
    "spxl_sma": ["SPXL", "SGOV"],
    "dual_momentum": ["SPUU", "QLD", "EFO", "BND"],
    "aaa": ["NTSD", "SAA", "EET", "UBT", "UST", "UGL", "DBC", "SHV"],
}

alpaca_environment = "live"
margin = 0.01  # band around the 200sma to avoid too many trades

# ─── 7-Asset Rotator (AAA family) strategy configuration ───────────────
# Adaptive Asset Allocation on a 7-asset capital-efficient/2×-leveraged universe.
# Each month: rank by 6m momentum on the unleveraged signal symbol, pick top-3,
# weight inverse-vol, scale by vol-target, balance to cash. DD-30 stop forces all
# to cash on -30% trailing-peak NAV breach.
#
# Universe (signal symbol → held position):
#   SPY  → NTSD (90% US + 60% intl-futures stack)
#   IWM  → SAA  (2× Russell 2000)
#   EEM  → EET  (2× emerging markets)
#   TLT  → UBT  (2× long Treasuries)
#   IEF  → UST  (2× 7-10y Treasuries)
#   GLD  → UGL  (2× gold)
#   DBC  → DBC  (1× commodities — no clean 2× equivalent)
aaa_config = {
    "strategy_key": "aaa",                # Firestore doc id under strategy-balances-{env}
    "alloc_key": "aaa_allo",              # Key in strategy_allocations
    "display_name": "7-Asset Rotator",
    "candidates": [
        ("SPY", "NTSD"),
        ("IWM", "SAA"),
        ("EEM", "EET"),
        ("TLT", "UBT"),
        ("IEF", "UST"),
        ("GLD", "UGL"),
        ("DBC", "DBC"),
    ],
    "defensive": "SHV",                   # Held when DD-stop fires or no positive momentum
    "lookback_days": 126,                 # 6-month momentum on signal symbols
    "top_n": 3,                           # Hold top-3 by momentum
    "min_score": 0.0,                     # Only include picks with positive momentum
    "vol_window": 60,                     # 60-day trailing realized vol for inverse-vol + target
    "target_vol": 0.25,                   # 25% annualized portfolio vol target
    "dd_threshold": 0.30,                 # 30% trailing-peak NAV stop → all to defensive
    "tolerance_amount": 5.0,              # Skip trades < $5
}

# Dual Momentum Strategy configuration — best-of-3 multi-asset with DD-stop + vol-target.
# Candidates are (signal_symbol, position_symbol). Strategy picks the candidate with the
# strongest blended-momentum score each month. Position size is scaled by
# min(1, target_vol / realized_vol) and the remainder parks in defensive (BND).
# A trailing-peak-NAV DD-stop forces defensive when the strategy is dd_threshold below peak.
# Backtest winner: 17.21% CAGR / 0.65 Sharpe / -34% MaxDD over 24 years (≤2× leverage).
dual_momentum_config = {
    "candidates": [("SPY", "SPUU"), ("QQQ", "QLD"), ("EFA", "EFO")],
    "defensive": "BND",
    "lookbacks": {"6m": 126, "12m": 252},   # trading days
    "lookback_weights": {"6m": 0.5, "12m": 0.5},
    "skip_days": 21,                         # Jegadeesh-Titman skip-most-recent-month
    "min_score": 0.01,                       # winner must exceed +1% to enter risk asset
    "dd_threshold": 0.30,                    # 30% trailing-peak NAV stop
    "target_vol": 0.25,                      # 25% annualized vol target
    "vol_window": 60,                        # trading-day window for realized vol
}

# Margin control configuration for automated leverage management
# Enables up to +10% leverage only when market conditions are favorable
margin_control_config = {
    "target_margin_pct": 0.10,      # Maximum +10% leverage allowed
    "max_margin_rate": 0.08,        # 8% rate threshold (FRED + spread must be ≤ this)
    "min_buffer_pct": 0.05,         # 5% minimum buffer required
    "max_leverage": 1.14,           # Maximum 1.14x leverage allowed
    "spread_below_35k": 0.025,      # +2.5% spread for accounts <$35k
    "spread_above_35k": 0.01,       # +1.0% spread for accounts ≥$35k
    "portfolio_threshold": 35000,   # Threshold for spread calculation (in dollars)
    "min_investment": 1.00,         # Minimum investment amount (Alpaca requirement)
}

# Contribution rebalancing configuration
# Tilts monthly contributions toward underweight strategies to bring portfolio back to target
rebalance_config = {
    "aggressiveness": 2.0,          # 0.0 = disabled (use fixed %), 1.0 = proportional tilt, 2.0+ = aggressive tilt
    "max_single_strategy_pct": 0.50,  # Cap any single strategy at 50% of monthly contribution
    "min_floor_pct_of_target": 0.50,  # Each strategy receives at least this fraction of its target (e.g. 9-Sig at 7.5% gets ≥ 3.75%) so aggressive tilts can't starve a small allocation entirely
}



# regime_world_config retired 2026-05-12 — Regime World (WLDU/USFR) discontinued
# and replaced by two new sleeves: 7-Asset Rotator (AAA family) and World 40/30/30.
# The make_monthly_buys_regime() / daily_regime_check() / backfill_regime_scores()
# helpers remain in place and continue to serve regime_sso_config.

# Firestore client - initialized lazily to respect .env file
_db_client = None

def get_firestore_client():
    """
    Get or initialize Firestore client with correct project ID.
    Lazy loading ensures .env file is loaded first in local development.
    """
    global _db_client
    if _db_client is None:
        # Ensure .env is loaded for local development (override=True ensures .env takes precedence)
        if not is_running_in_cloud():
            load_dotenv(override=True)
        
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
        if not project_id:
            # Fallback to GOOGLE_CLOUD_PROJECT (used in cloud environments)
            project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
        
        _db_client = firestore.Client(project=project_id)
    
    return _db_client


# Market data cache settings - Firestore-based for cross-function sharing
CACHE_DURATION_MINUTES = 5  # Cache freshness window


def normalize_symbol(symbol):
    """Normalize a symbol for use as a Firestore document ID."""
    return symbol.replace("^", "").replace(".", "_")


def get_all_market_data(symbol, env="live"):
    """
    Get ALL market data for a symbol from Firestore cache.
    Returns None if cache is stale or missing (caller should call update_market_data).
    """
    try:
        doc_id = normalize_symbol(symbol)
        doc_ref = get_firestore_client().collection(f"market-data-{env}").document(doc_id)
        doc = doc_ref.get()

        if not doc.exists:
            return None

        data = doc.to_dict()

        timestamp = data.get("timestamp")
        if timestamp:
            # Handle timezone-aware Firestore timestamps
            if hasattr(timestamp, 'tzinfo') and timestamp.tzinfo is not None:
                timestamp = timestamp.replace(tzinfo=None)

            age_seconds = (datetime.datetime.utcnow() - timestamp).total_seconds()
            if age_seconds > (CACHE_DURATION_MINUTES * 60):
                return None

        return data

    except Exception as e:
        print(f"Warning: Could not read market data for {symbol}: {e}")
        return None


def set_cached_market_data(symbol, data_type, value, env="live"):
    """Cache a single market data field to Firestore."""
    try:
        doc_id = normalize_symbol(symbol)
        doc_ref = get_firestore_client().collection(f"market-data-{env}").document(doc_id)
        
        # Get existing data or create new
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
        else:
            data = {"symbol": symbol}  # Store original symbol for reference
        
        # Update the specific data type and timestamp
        data[data_type] = value
        data["timestamp"] = datetime.datetime.utcnow()
        
        doc_ref.set(data)
        
    except Exception as e:
        print(f"Warning: Could not cache market data for {symbol}.{data_type}: {e}")


def get_auth_headers(api):
    return {
        "APCA-API-KEY-ID": api["API_KEY"],
        "APCA-API-SECRET-KEY": api["SECRET_KEY"],
    }


def get_retry_session(max_retries=3, backoff_factor=1.0, timeout=30):
    """Create a requests session with retry logic for SSL errors and connection issues."""
    from urllib3.util.retry import Retry
    from requests.adapters import HTTPAdapter

    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST", "DELETE"],
        raise_on_status=False,
        connect=max_retries,
        read=max_retries,
    )

    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def alpaca_request_with_retry(method, url, headers, max_retries=5, timeout=60, label="request", raise_on_fail=False, **kwargs):
    """
    Make an HTTP request with SSL retry logic and exponential backoff.
    Shared by all Alpaca API calls to avoid duplicating retry boilerplate.
    """
    from requests.exceptions import SSLError, ConnectionError, RequestException
    from urllib3.exceptions import SSLError as URLLib3SSLError, MaxRetryError

    session = get_retry_session(max_retries=2, backoff_factor=1.0, timeout=timeout)

    for attempt in range(max_retries):
        try:
            response = session.request(method, url, headers=headers, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response

        except (SSLError, URLLib3SSLError, ConnectionError, MaxRetryError) as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                print(f"SSL/Connection error for {label} (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            if raise_on_fail:
                raise Exception(f"{label} failed after {max_retries} attempts: {e}")
            print(f"{label} failed after {max_retries} attempts: {e}")
            return None

        except RequestException as e:
            if 'SSL' in str(e) and attempt < max_retries - 1:
                wait_time = 2 ** attempt
                print(f"SSL-related error for {label} (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            if raise_on_fail:
                raise Exception(f"{label} failed: {e}")
            print(f"{label} failed: {e}")
            return None

        except Exception as e:
            if 'SSL' in str(e) and attempt < max_retries - 1:
                wait_time = 2 ** attempt
                print(f"SSL error (unexpected) for {label} (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            if raise_on_fail:
                raise
            print(f"Unexpected error for {label}: {e}")
            return None

    return None


def get_alpaca_historical_bars(api, symbol, days=400, raw=False):
    """Fetch historical daily bars from Alpaca IEX feed.

    `days` is interpreted as TRADING days. We request ~1.5× as many calendar
    days from Alpaca to account for weekends + holidays, so callers can write
    `days=200` and reliably get back ≥200 bars (when the symbol has history).

    raw=False (default) → list of closing prices (floats).
    raw=True            → list of bar dicts {t, o, h, l, c, v, ...} from Alpaca.
                          Callers that need OHLC + timestamps (ADX, backfill,
                          OHLC-based signals) must request raw=True.
    Returns None if the request fails or no bars are returned.
    """
    from datetime import datetime, timedelta

    # Trading-day → calendar-day buffer. ~252 trading days / 365 calendar days
    # ≈ 0.69, so calendar = trading × 1.45. Add a small floor for short windows.
    calendar_days = int(days * 1.5) + 10

    end_date = datetime.now()
    start_date = end_date - timedelta(days=calendar_days)

    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    params = {
        "start": start_date.strftime("%Y-%m-%d"),
        "end": end_date.strftime("%Y-%m-%d"),
        "timeframe": "1Day",
        "limit": 10000,
        "adjustment": "split",
        "feed": "iex",
    }

    response = alpaca_request_with_retry(
        "GET", url, headers=get_auth_headers(api),
        params=params, label=f"historical bars for {symbol}"
    )
    if response is None:
        return None

    bars = response.json().get("bars", [])
    if not bars:
        print(f"No Alpaca bars returned for {symbol}")
        return None

    print(f"Fetched {len(bars)} bars for {symbol} from Alpaca IEX feed (requested {days} trading days)")
    if raw:
        return bars
    return [bar['c'] for bar in bars]


def get_latest_trade(api, symbol):
    """Get latest trade price from Alpaca. Raises on failure."""
    symbol = symbol.upper()
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest"

    response = alpaca_request_with_retry(
        "GET", url, headers=get_auth_headers(api),
        label=f"latest trade for {symbol}", raise_on_fail=True
    )
    return response.json()["trade"]["p"]


def get_sma(api, symbol, period):
    """
    Calculate Simple Moving Average for a symbol.
    
    Args:
        api: Alpaca API credentials
        symbol: Stock symbol (e.g., "SPY")
        period: SMA period in days (e.g., 200)
    
    Returns:
        float: SMA value or None if error
    """
    try:
        # Get historical bars with extra buffer for IEX feed limitations
        bars = get_alpaca_historical_bars(api, symbol, days=period + 100)  # Extra buffer for IEX feed
        
        if bars is None or len(bars) < period:
            print(f"Insufficient data for {period}-day SMA calculation for {symbol}")
            return None
        
        # Calculate SMA using the last 'period' bars
        recent_bars = bars[-period:]
        sma = sum(recent_bars) / len(recent_bars)
        
        return sma
        
    except Exception as e:
        print(f"Error calculating {period}-day SMA for {symbol}: {e}")
        return None

def get_account_cash(api):
    url = f"{api['BASE_URL']}/v2/account"
    response = requests.get(url, headers=get_auth_headers(api))
    response.raise_for_status()
    return float(response.json()["cash"])

def list_positions(api):
    url = f"{api['BASE_URL']}/v2/positions"
    response = requests.get(url, headers=get_auth_headers(api))
    response.raise_for_status()
    return response.json()

def get_order(api, order_id):
    url = f"{api['BASE_URL']}/v2/orders/{order_id}"
    response = requests.get(url, headers=get_auth_headers(api))
    response.raise_for_status()
    return response.json()

def get_pending_orders(api, symbol=None):
    """Get pending/open orders from Alpaca, optionally filtered by symbol"""
    url = f"{api['BASE_URL']}/v2/orders"
    params = {"status": "open", "limit": 500}
    if symbol:
        params["symbols"] = symbol
    response = requests.get(url, headers=get_auth_headers(api), params=params)
    response.raise_for_status()
    return response.json()

def cancel_order(api, order_id):
    """Cancel a specific order by ID"""
    url = f"{api['BASE_URL']}/v2/orders/{order_id}/cancel"
    response = requests.delete(url, headers=get_auth_headers(api))
    response.raise_for_status()
    return response.json()

def submit_order(api, symbol, qty, side):
    url = f"{api['BASE_URL']}/v2/orders"
    data = {
        "symbol": symbol,
        "qty": round(qty, 6),
        "side": side,
        "type": "market",
        "time_in_force": "day",
    }
    response = requests.post(url, headers=get_auth_headers(api), json=data)
    
    # Enhanced error handling to show Alpaca's actual error message
    if not response.ok:
        try:
            error_detail = response.json()
            print(f"Alpaca order error for {symbol}: {error_detail}")
        except Exception:
            print(f"Alpaca order error for {symbol}: {response.text}")
    
    response.raise_for_status()
    return response.json()

def is_running_in_cloud():
    return (
        os.getenv("GAE_ENV", "").startswith("standard")
        or os.getenv("FUNCTION_NAME") is not None
        or os.getenv("K_SERVICE") is not None
        or os.getenv("GAE_INSTANCE") is not None
        or os.getenv("GOOGLE_CLOUD_PROJECT") is not None
    )


def get_secret(secret_name):
    """Get a secret from Google Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")


def get_secret_or_env(secret_name, env_var_name=None):
    """Get a value from Secret Manager (cloud) or .env file (local)."""
    if is_running_in_cloud():
        return get_secret(secret_name)
    load_dotenv(override=True)
    return os.getenv(env_var_name or secret_name)


def set_alpaca_environment(env, use_secret_manager=True):
    """Set up Alpaca API credentials for the given environment."""
    suffix = "LIVE" if env == "live" else "PAPER"
    base_url = "https://api.alpaca.markets" if env == "live" else "https://paper-api.alpaca.markets"

    if use_secret_manager and is_running_in_cloud():
        API_KEY = get_secret(f"ALPACA_API_KEY_{suffix}")
        SECRET_KEY = get_secret(f"ALPACA_SECRET_KEY_{suffix}")
    else:
        load_dotenv(override=True)
        API_KEY = os.getenv(f"ALPACA_API_KEY_{suffix}")
        SECRET_KEY = os.getenv(f"ALPACA_SECRET_KEY_{suffix}")

    return {"API_KEY": API_KEY, "SECRET_KEY": SECRET_KEY, "BASE_URL": base_url}


def get_telegram_secrets(chat_id_secret="TELEGRAM_CHAT_ID"):
    """Bot-Token plus Ziel-Chat. `chat_id_secret` waehlt den Kanal - derselbe
    Bot kann in mehrere Chats posten, solange er dort Mitglied bzw. Admin ist.
    Muster uebernommen von quant/execution/telegram.py (eigener Quant-Kanal)."""
    return (
        get_secret_or_env("TELEGRAM_KEY"),
        get_secret_or_env(chat_id_secret),
    )


# --- Federal Funds Target Rate (Upper Limit) --------------------------------
# Three INDEPENDENT reads of the same official number (the FOMC target-range
# upper bound). This is deliberately not a "fallback default": if every source
# fails, get_fred_rate() still returns None and check_margin_conditions flips
# to cash-only, exactly as before. The conservative-by-default gate documented
# in CLAUDE.md is untouched — this only stops a single dropped packet from
# standing in for a real signal. A 10s read timeout against api.stlouisfed.org
# zeroed the entire monthly budget on 2026-08-03 and again on 2026-09-01.
# 10s was too tight: monthly_invest_all runs once a month, so it ALWAYS cold-starts,
# and both observed failures (2026-08-03, 2026-09-01) were `Read timed out` — the TLS
# connection stood and FRED was merely slow, not blocking. 60s is the empirically
# proven value: the nightly quant job hits the same API from the same project with
# timeout=60 and has never failed in ~250 runs (quant/data/public_ingest.py), and its
# requests are far heavier (full history since 1999 vs our single observation).
#
# A flat 60s x 3 sources x 2 attempts would be 360s though, which crowds the 540s
# function budget. So the per-request timeout stays generous and the WHOLE chain gets
# a hard wall-clock ceiling instead — each request is additionally clamped to whatever
# budget is left, and an exhausted budget fails closed like any other total failure.
POLICY_RATE_TIMEOUT_SECONDS = 60         # per request; matches the proven quant value
POLICY_RATE_TOTAL_BUDGET_SECONDS = 240   # hard ceiling for the entire source chain
POLICY_RATE_SOURCE_ATTEMPTS = 2          # per source, before moving to the next
POLICY_RATE_MAX_STALENESS_DAYS = 10      # target range only moves at FOMC meetings
POLICY_RATE_SANITY_BOUNDS = (0.0, 0.25)  # decimal; reject anything outside 0-25%


def _policy_rate_valid(rate, obs_date, source):
    """Reject implausible or stale observations before they can be trusted.

    A spuriously LOW rate is the dangerous direction: it would wrongly pass the
    `margin_rate <= 8%` gate and switch leverage on. Too-high or stale values
    only ever gate margin off, which is the safe failure mode.
    """
    lo, hi = POLICY_RATE_SANITY_BOUNDS
    if rate is None or not (lo <= rate <= hi):
        print(f"Policy rate: {source} rejected — {rate} outside sanity bounds")
        return False
    if obs_date is not None:
        age = (datetime.date.today() - obs_date).days
        if age > POLICY_RATE_MAX_STALENESS_DAYS:
            print(f"Policy rate: {source} rejected — observation {obs_date} is {age}d stale")
            return False
    return True


def _policy_rate_fred_api(timeout):
    """FRED JSON API (DFEDTARU) — primary source. Requires FREDKEY."""
    fred_key = get_secret_or_env("FREDKEY")
    if not fred_key:
        print("FRED API key not found")
        return None

    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id=DFEDTARU&api_key={fred_key}&file_type=json&sort_order=desc&limit=1"
    )
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()

    observations = response.json().get("observations") or []
    if not observations:
        return None

    value = observations[0].get("value")
    if value is None or value == ".":  # '.' is FRED's missing-data marker
        return None

    obs_date = datetime.datetime.strptime(observations[0]["date"], "%Y-%m-%d").date()
    return float(value) / 100.0, obs_date


def _policy_rate_fred_csv(timeout):
    """FRED graph CSV — same series, no API key, different service path.

    Covers a rate-limited or revoked FREDKEY as well as an api.stlouisfed.org
    blip that leaves the graph backend healthy.
    """
    cosd = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    response = requests.get(
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFEDTARU&cosd={cosd}",
        timeout=timeout,
    )
    response.raise_for_status()

    # Header is `observation_date,DFEDTARU`; walk backwards to the newest value.
    rows = [ln.split(",") for ln in response.text.strip().splitlines()[1:] if ln.strip()]
    for row in reversed(rows):
        if len(row) < 2:
            continue
        date_str, value = row[0].strip(), row[1].strip()
        if value in ("", "."):
            continue
        return float(value) / 100.0, datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    return None


def _policy_rate_nyfed(timeout):
    """NY Fed markets API — fully independent host, and the primary publisher.

    `targetRateTo` is the FOMC target-range upper bound, i.e. exactly what
    DFEDTARU tracks. Published with a one-business-day lag, which is irrelevant
    for a number that only moves at the 8 scheduled FOMC meetings per year.
    """
    response = requests.get(
        "https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json",
        timeout=timeout,
    )
    response.raise_for_status()

    ref_rates = response.json().get("refRates") or []
    if not ref_rates:
        return None

    target_to = ref_rates[0].get("targetRateTo")
    if target_to is None:
        return None

    obs_date = datetime.datetime.strptime(ref_rates[0]["effectiveDate"], "%Y-%m-%d").date()
    return float(target_to) / 100.0, obs_date


def get_fred_rate():
    """Current Federal Funds Target Rate (Upper Limit) as a decimal, e.g. 0.0375.

    Walks the three sources in order of authority and stops at the first value
    that passes the sanity + freshness checks. Returns None only when ALL of
    them fail, which keeps check_margin_conditions fail-closed (cash-only).
    """
    sources = (
        ("FRED API", _policy_rate_fred_api),
        ("FRED CSV", _policy_rate_fred_csv),
        ("NY Fed", _policy_rate_nyfed),
    )

    deadline = time.monotonic() + POLICY_RATE_TOTAL_BUDGET_SECONDS

    for source, fetch in sources:
        for attempt in range(1, POLICY_RATE_SOURCE_ATTEMPTS + 1):
            # Clamp each request to whatever wall-clock budget is left, so a chain of
            # slow-but-not-dead sources can never eat into the function's own timeout.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(
                    f"Policy rate: {POLICY_RATE_TOTAL_BUDGET_SECONDS}s total budget "
                    f"exhausted before {source} — giving up"
                )
                break

            try:
                fetched = fetch(min(POLICY_RATE_TIMEOUT_SECONDS, remaining))
            except Exception as e:
                print(
                    f"Policy rate: {source} attempt {attempt}/"
                    f"{POLICY_RATE_SOURCE_ATTEMPTS} failed: {e}"
                )
                if attempt < POLICY_RATE_SOURCE_ATTEMPTS:
                    time.sleep(1.5 * attempt)
                continue

            # A well-formed response we cannot use (missing data, bad shape) is
            # not worth retrying — move straight on to the next source.
            if fetched is None:
                print(f"Policy rate: {source} returned no usable observation")
                break

            rate, obs_date = fetched
            if not _policy_rate_valid(rate, obs_date, source):
                break

            print(f"Policy rate: {rate * 100:.2f}% from {source} (obs {obs_date})")
            return rate

        if time.monotonic() >= deadline:
            break

    print("Error fetching FRED rate: all sources exhausted — staying cash-only")
    return None


def get_account_info(api):
    """
    Fetch full account information from Alpaca including equity, portfolio value, and margin data.
    
    Args:
        api: Alpaca API credentials dict
    
    Returns:
        dict: Account information with keys: equity, portfolio_value, maintenance_margin, cash
              Returns None on error
    """
    try:
        url = f"{api['BASE_URL']}/v2/account"
        response = requests.get(url, headers=get_auth_headers(api))
        response.raise_for_status()
        
        account_data = response.json()
        
        # Extract relevant fields for margin calculations
        return {
            "equity": float(account_data.get("equity", 0)),
            "portfolio_value": float(account_data.get("portfolio_value", 0)),
            "maintenance_margin": float(account_data.get("maintenance_margin", 0)),
            "cash": float(account_data.get("cash", 0)),
        }
    except Exception as e:
        print(f"Error fetching account info: {e}")
        return None


def check_margin_conditions(api, env="live"):
    """
    Evaluate all margin control gates to determine if leverage is allowed.
    
    All 4 gates must pass for margin to be enabled:
    1. Market Trend: SPX > 200-SMA
    2. Margin Rate: FRED rate + spread ≤ 8.0%
    3. Buffer: (equity/portfolio_value) - (maintenance_margin/portfolio_value) ≥ 5%
    4. Leverage: portfolio_value / equity < 1.14×
    
    Args:
        api: Alpaca API credentials dict
    
    Returns:
        dict: {
            "allowed": bool - True if all gates pass
            "target_margin": float - 0.10 if allowed, else 0.0
            "gate_results": dict - individual gate pass/fail status
            "metrics": dict - all calculated metrics
            "errors": list - any errors encountered
        }
    """
    result = {
        "allowed": False,
        "target_margin": 0.0,
        "gate_results": {
            "market_trend": False,
            "margin_rate": False,
            "buffer": False,
            "leverage": False,
        },
        "metrics": {},
        "errors": [],
    }
    
    try:
        # Gate 1: Market Trend (SPY > 200-SMA as S&P 500 proxy)
        try:
            # Get all SPY data at once (efficient single fetch/read)
            spy_data = get_all_market_data("SPY", env=env)
            if spy_data is None:
                spy_data = update_market_data("SPY", env=env)
            
            spy_price = spy_data["price"]
            spy_sma = spy_data["sma200"]
            result["metrics"]["spx_price"] = spy_price  # Keep key name for compatibility
            result["metrics"]["spx_sma"] = spy_sma
            # Use 1% margin band for consistent trend filtering with SPXL strategy
            result["gate_results"]["market_trend"] = spy_price > spy_sma * (1 + margin)
        except Exception as e:
            result["errors"].append(f"Market trend check failed: {e}")
            return result
        
        # Get account information for remaining gates
        account_info = get_account_info(api)
        if not account_info:
            result["errors"].append("Failed to fetch account information")
            return result
        
        equity = account_info["equity"]
        portfolio_value = account_info["portfolio_value"]
        maintenance_margin = account_info["maintenance_margin"]
        cash = account_info["cash"]
        
        result["metrics"]["equity"] = equity
        result["metrics"]["portfolio_value"] = portfolio_value
        result["metrics"]["maintenance_margin"] = maintenance_margin
        result["metrics"]["cash"] = cash
        
        # Gate 2: Margin Rate (FRED + spread ≤ 8.0%)
        try:
            fred_rate = get_fred_rate()
            if fred_rate is None:
                result["errors"].append("Failed to fetch FRED rate")
                return result
            
            # Determine spread based on equity (actual account value)
            if equity <= margin_control_config["portfolio_threshold"]:
                spread = margin_control_config["spread_below_35k"]
            else:
                spread = margin_control_config["spread_above_35k"]
            
            margin_rate = fred_rate + spread
            result["metrics"]["fred_rate"] = fred_rate
            result["metrics"]["spread"] = spread
            result["metrics"]["margin_rate"] = margin_rate
            result["gate_results"]["margin_rate"] = margin_rate <= margin_control_config["max_margin_rate"]
        except Exception as e:
            result["errors"].append(f"Margin rate check failed: {e}")
            return result
        
        # Gate 3: Buffer (≥ 5%)
        try:
            if portfolio_value > 0:
                buffer = (equity / portfolio_value) - (maintenance_margin / portfolio_value)
            else:
                buffer = 0.0
            
            result["metrics"]["buffer"] = buffer
            result["gate_results"]["buffer"] = buffer >= margin_control_config["min_buffer_pct"]
        except Exception as e:
            result["errors"].append(f"Buffer check failed: {e}")
            return result
        
        # Gate 4: Leverage (< 1.14×)
        try:
            if equity > 0:
                leverage = portfolio_value / equity
            else:
                leverage = 0.0
            
            result["metrics"]["leverage"] = leverage
            result["gate_results"]["leverage"] = leverage < margin_control_config["max_leverage"]
        except Exception as e:
            result["errors"].append(f"Leverage check failed: {e}")
            return result
        
        # All gates must pass
        result["allowed"] = all(result["gate_results"].values())
        result["target_margin"] = margin_control_config["target_margin_pct"] if result["allowed"] else 0.0
        
    except Exception as e:
        result["errors"].append(f"Unexpected error in margin check: {e}")
    
    return result


def calculate_monthly_investments(api, margin_result, env="live"):
    """
    Calculate dynamic monthly investment amounts based on available cash and margin.
    
    Steps:
    1. Get total cash from account (can be negative if margin is already in use)
    2. Calculate available margin (equity × 10%), accounting for existing margin debt
    3. If cash is negative, subtract that amount from available margin capacity
    4. Split total by strategy percentages (with optional rebalancing tilt)
    
    Note: All strategies now use actual positions (no virtual cash in Firestore),
    so we don't need to subtract reserved amounts. Each strategy's equity is tracked
    via actual Alpaca positions.
    
    When rebalance_config["aggressiveness"] > 0, contributions are tilted toward
    underweight strategies to bring the portfolio back toward target allocations.
    
    Args:
        api: Alpaca API credentials
        margin_result: Result from check_margin_conditions()
    
    Returns:
        dict: {
            "total_cash": float,           # Total cash in account (can be negative if using margin)
            "total_reserved": float,       # Always 0 (no reserved cash anymore)
            "total_available": float,      # Total cash available
            "margin_approved": float,      # Available margin amount (accounts for existing margin debt)
            "used_margin": float,          # Amount of margin already in use (0 if cash >= 0)
            "total_investing": float,      # Total available + margin
            "strategy_amounts": dict,      # Amount per strategy
            "reserved_amounts": dict,      # Always empty (no reserved cash anymore)
            "rebalance_result": dict       # Rebalancing details (if enabled)
        }
    """
    # Step 1: Get total cash from account
    metrics = margin_result.get("metrics", {})
    total_cash = metrics.get("cash", 0)
    equity = metrics.get("equity", 0)
    
    # Step 2: Calculate available cash (no reserved amounts to subtract)
    # All strategies use actual positions, so all cash is available
    available_cash = max(0, total_cash)  # Ensure non-negative
    
    # Step 3: Calculate margin if approved, accounting for existing margin usage
    # If cash is negative, that represents margin debt already in use
    target_margin = margin_result.get("target_margin", 0)
    margin_approved = 0
    used_margin = 0
    
    if target_margin > 0 and equity > 0:
        # Calculate total margin capacity based on equity
        total_margin_capacity = equity * target_margin
        
        # Calculate how much margin is already being used (if cash is negative)
        if total_cash < 0:
            used_margin = abs(total_cash)  # Convert negative cash to positive margin debt amount
            print(f"Existing margin debt detected: ${used_margin:.2f}")
        
        # Calculate remaining available margin (capacity minus what's already used)
        remaining_margin = max(0, total_margin_capacity - used_margin)
        margin_approved = remaining_margin
        
        if used_margin > 0:
            print(f"Margin capacity: ${total_margin_capacity:.2f}, Used: ${used_margin:.2f}, Available: ${remaining_margin:.2f}")
    else:
        margin_approved = 0
    
    total_investing = available_cash + margin_approved
    
    # Step 4: Calculate allocations (with optional rebalancing tilt)
    rebalance_result = None
    
    if rebalance_config["aggressiveness"] > 0:
        # Calculate rebalanced allocations that tilt toward underweight strategies
        rebalance_result = calculate_rebalanced_allocations(
            api, 
            aggressiveness=rebalance_config["aggressiveness"]
        )
        adjusted_allocations = rebalance_result["adjusted_allocations"]
        
        # Print the allocation dashboard showing current vs target vs adjusted
        print_allocation_dashboard(rebalance_result, contribution_amount=total_investing)
        
        # Use adjusted allocations for strategy amounts
        strategy_amounts = {
            key: total_investing * adjusted_allocations[key]
            for key in strategy_allocations.keys()
        }
    else:
        # Use fixed strategy allocations (original behavior)
        strategy_amounts = {
            key: total_investing * allocation 
            for key, allocation in strategy_allocations.items()
        }
    
    return {
        "total_cash": total_cash,
        "total_reserved": 0,  # No reserved cash anymore - all strategies use actual positions
        "total_available": available_cash,
        "margin_approved": margin_approved,
        "used_margin": used_margin,
        "total_investing": total_investing,
        "strategy_amounts": strategy_amounts,
        "reserved_amounts": {},  # No reserved amounts anymore
        "rebalance_result": rebalance_result  # Include rebalancing details for reference
    }


def save_balance(strategy, data, env="live"):
    """
    Save strategy balance to Firestore with environment separation.
    Handles Firestore unavailability gracefully for local testing.
    
    Args:
        strategy: Strategy name (e.g., "dual_momentum")
        data: Either a simple float (invested amount) or dict with multiple fields
        env: Environment ("live" or "paper") - determines Firestore collection
    """
    try:
        # Use environment-specific collection to separate paper/live data
        collection_name = f"strategy-balances-{env}"
        doc_ref = get_firestore_client().collection(collection_name).document(strategy)
        
        # Handle both simple float values and complex dictionaries
        if isinstance(data, dict):
            doc_ref.set(data)
        else:
            doc_ref.set({"invested": data})
            
    except Exception as e:
        print(f"Warning: Could not save balance to Firestore for {strategy} ({env}): {e}")


def load_balances(env="live"):
    """
    Load strategy balances from Firestore with environment separation.
    Returns empty dict if Firestore is unavailable (local testing without proper config).
    
    Args:
        env: Environment ("live" or "paper") - determines Firestore collection
    
    Returns:
        dict: Strategy balances from the specified environment
    """
    balances = {}
    try:
        # Use environment-specific collection to separate paper/live data
        collection_name = f"strategy-balances-{env}"
        docs = get_firestore_client().collection(collection_name).stream()
        for doc in docs:
            balances[doc.id] = doc.to_dict()
    except Exception as e:
        print(f"Warning: Could not load Firestore balances ({env}) (local testing?): {e}")
        # Return empty dict for local testing without Firestore
    return balances


def current_quarter_id(today=None):
    """Return the current quarter id like '2026-Q2'."""
    today = today or datetime.datetime.now()
    return f"{today.year}-Q{((today.month - 1) // 3) + 1}"


def current_month_id(today=None):
    """Return the current month id like '2026-05'."""
    today = today or datetime.datetime.now()
    return today.strftime("%Y-%m")


def mark_monthly_run_complete(env="live", clean=True):
    """Record that the monthly orchestrator finished, so the watchdog can verify.

    clean=False marks a run whose margin_result carried a data-fetch error
    (FRED/Alpaca/SMA) — see should_run_monthly_orchestrator, which uses this
    flag to allow a retry on the next trading day within the same month's
    1-7 window instead of silently skipping the rest of the month. A run
    that evaluated cleanly (clean=True), even to a legitimate $0 budget, is
    never retried — only a failed EVALUATION gets another chance, never a
    correctly-computed cash-only decision.
    """
    try:
        month_id = current_month_id()
        get_firestore_client().collection(f"monthly-runs-{env}").document(month_id).set(
            {
                "month_id": month_id,
                "timestamp": datetime.datetime.utcnow(),
                "clean": clean,
            }
        )
    except Exception as e:
        print(f"Warning: could not write monthly-runs marker: {e}")


def should_run_monthly_orchestrator(env="live", today=None):
    """Gate for the monthly orchestrator: today must be a NYSE trading day
    within the first 7 calendar days of the month, AND this month must not
    already have a CLEAN evaluation on record.

    Unlike the plain check_trading_day(mode="monthly") gate (true only on the
    single first trading day), this allows the scheduler's existing daily
    1-7 firing window (see cloudbuild.yaml) to retry on day 2, 3, ... if the
    prior attempt's margin_result carried a data-fetch error — e.g. the FRED
    timeout on 2026-08-03 that zeroed total_investing for the entire month
    with no chance to recover. The conservative fail-closed DECISION itself
    is never bypassed or defaulted; this only gives that same strict check
    another shot at fresh data on the next trading day. A month that already
    had a clean evaluation (clean=True, regardless of the resulting budget —
    including a legitimate $0 from a real cash-only regime) is never retried.

    today: injectable for tests; defaults to the real current time.
    """
    today = today or datetime.datetime.now()
    if today.day > 7:
        return False
    nyse = mcal.get_calendar("NYSE")
    if nyse.schedule(start_date=today.date(), end_date=today.date()).empty:
        return False
    month_id = current_month_id(today)
    try:
        doc = get_firestore_client().collection(f"monthly-runs-{env}").document(month_id).get()
    except Exception as e:
        print(f"Warning: could not read monthly-runs marker, allowing attempt: {e}")
        return True
    if not doc.exists:
        return True
    return doc.to_dict().get("clean", True) is False


def quarterly_run_complete(strategy, env="live"):
    """
    Returns True if the strategy's quarterly rebalance has already been
    marked complete for the current quarter.

    Used to make quarterly rebalance functions idempotent — the day-1-7
    cron pattern relies on the trading-day check to prevent re-runs, but
    a marker doc lets us tolerate manual re-invocations and double-fires.
    """
    try:
        doc = (
            get_firestore_client()
            .collection(f"quarterly-runs-{env}")
            .document(f"{strategy}-{current_quarter_id()}")
            .get()
        )
        return doc.exists
    except Exception as e:
        # On Firestore failure, fall through to running — better to risk a
        # duplicate trade than silently skip a real rebalance.
        print(f"Warning: quarterly_run_complete check failed for {strategy}: {e}")
        return False


def mark_quarterly_run_complete(strategy, action, env="live"):
    """Record that a strategy's quarterly run finished, for idempotency checks."""
    try:
        quarter_id = current_quarter_id()
        get_firestore_client().collection(f"quarterly-runs-{env}").document(
            f"{strategy}-{quarter_id}"
        ).set(
            {
                "strategy": strategy,
                "quarter_id": quarter_id,
                "action": action,
                "timestamp": datetime.datetime.utcnow(),
            }
        )
    except Exception as e:
        print(f"Warning: could not write quarterly-runs marker for {strategy}: {e}")




def get_strategy_positions(api, symbols, strategy_name="strategy"):
    """Get positions from Alpaca filtered to a specific strategy's symbols."""
    try:
        positions = list_positions(api)
        result = {}
        for position in positions:
            ticker = position.get("symbol")
            qty = float(position.get("qty", 0))
            if ticker in symbols and qty > 0:
                result[ticker] = qty
        print(f"Current {strategy_name} positions from Alpaca: {result}")
        return result
    except Exception as e:
        print(f"Error getting {strategy_name} positions: {e}")
        return {}


def make_monthly_buys(api, force_execute=False, investment_calc=None, margin_result=None, skip_order_wait=False, env="live"):
    """
    Make monthly HFEA purchases (UPRO/TMF/KMLM) with margin-aware logic.
    Uses All-or-Nothing approach: invest full amount or skip entirely.
    Sends exactly one Telegram message at the end summarizing the outcome.
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        print("Not first trading day of the month")
        return "Not first trading day of the month"
    
    if force_execute:
        print("HFEA: Force execution enabled - bypassing trading day check")
    
    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)
    
    investment_amount = investment_calc["strategy_amounts"]["hfea_allo"]
    
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    buying_power = investment_calc["total_available"] + investment_calc["margin_approved"]
    
    def _skip(reason):
        msg = f"📊 HFEA (17.5%) — ${investment_amount:,.2f}\n⏭ {reason}"
        send_telegram_message(msg)
        print(reason)
        return reason
    
    # Gate checks
    # When margin gates fail and leverage <= 1.0×, we still run from available cash —
    # HFEA only skips entirely when we need to deleverage.
    if target_margin == 0 and leverage > 1.0:
        return _skip(f"Skipped — deleveraging required ({leverage:.2f}x)")

    if buying_power < investment_amount:
        return _skip(f"Skipped — insufficient buying power (${buying_power:,.2f})")

    if investment_amount < margin_control_config["min_investment"]:
        return _skip(f"Skipped — ${investment_amount:.2f} below $1.00 minimum")

    # Projected leverage check (HFEA uses Portfolio Value + Cash for equity)
    if target_margin > 0:
        pv = metrics.get("portfolio_value", 0)
        current_cash = metrics.get("cash", 0)
        current_equity = pv + current_cash
        
        if pv > 0 and current_equity > 0:
            projected_pv = pv + investment_amount
            projected_cash = current_cash - investment_amount
            projected_equity = projected_pv + projected_cash
            
            if projected_equity > 0:
                projected_leverage = projected_pv / projected_equity
                print(f"Leverage: current {leverage:.3f}x → projected {projected_leverage:.3f}x")
                if projected_leverage >= margin_control_config["max_leverage"]:
                    return _skip(f"Skipped — projected leverage {projected_leverage:.3f}x exceeds {margin_control_config['max_leverage']:.2f}x limit")
    
    # Get current allocations
    (
        upro_diff, tmf_diff, kmlm_diff,
        upro_value, tmf_value, kmlm_value, total_value,
        target_upro_value, target_tmf_value, target_kmlm_value,
        current_upro_percent, current_tmf_percent, current_kmlm_percent,
    ) = get_hfea_allocations(api)

    upro_underweight = max(0, target_upro_value - upro_value)
    tmf_underweight = max(0, target_tmf_value - tmf_value)
    kmlm_underweight = max(0, target_kmlm_value - kmlm_value)
    total_underweight = upro_underweight + tmf_underweight + kmlm_underweight

    if total_underweight == 0:
        upro_amount = investment_amount * upro_allocation
        tmf_amount = investment_amount * tmf_allocation
        kmlm_amount = investment_amount * kmlm_allocation
    else:
        upro_amount = (upro_underweight / total_underweight) * investment_amount
        tmf_amount = (tmf_underweight / total_underweight) * investment_amount
        kmlm_amount = (kmlm_underweight / total_underweight) * investment_amount

    upro_price = float(get_latest_trade(api, "UPRO"))
    tmf_price = float(get_latest_trade(api, "TMF"))
    kmlm_price = float(get_latest_trade(api, "KMLM"))

    upro_shares_to_buy = upro_amount / upro_price
    tmf_shares_to_buy = tmf_amount / tmf_price
    kmlm_shares_to_buy = kmlm_amount / kmlm_price

    balances = load_balances(env)
    hfea_data = balances.get("hfea", {})
    total_invested = hfea_data.get("total_invested", 0)
    stored_positions = hfea_data.get("current_positions", {})
    
    actual_hfea_positions = get_hfea_positions(api)
    if actual_hfea_positions:
        current_positions = actual_hfea_positions
        if stored_positions != actual_hfea_positions:
            print(f"Warning: Firestore positions differ from Alpaca, using Alpaca as truth")
    else:
        current_positions = stored_positions
    
    print(f"HFEA — Investment: ${investment_amount:.2f}")
    
    trades_executed = []
    
    for symbol, qty, amount, price in [
        ("UPRO", upro_shares_to_buy, upro_amount, upro_price),
        ("TMF", tmf_shares_to_buy, tmf_amount, tmf_price),
        ("KMLM", kmlm_shares_to_buy, kmlm_amount, kmlm_price),
    ]:
        if qty > 0:
            submit_order(api, symbol, qty, "buy")
            print(f"Bought {qty:.6f} shares of {symbol}")
            trades_executed.append({"symbol": symbol, "shares": qty, "amount": amount, "price": price})
    
    new_total_invested = total_invested + investment_amount
    
    if len(trades_executed) > 0:
        print("Waiting for orders to settle before syncing positions from Alpaca...")
        time.sleep(2)
    
    actual_positions = get_hfea_positions(api)
    if actual_positions:
        new_positions = actual_positions
    else:
        new_positions = current_positions.copy()
        for symbol, qty in [("UPRO", upro_shares_to_buy), ("TMF", tmf_shares_to_buy), ("KMLM", kmlm_shares_to_buy)]:
            if qty > 0:
                new_positions[symbol] = new_positions.get(symbol, 0) + qty
    
    current_value = upro_value + tmf_value + kmlm_value + investment_amount
    strategy_return = (current_value / new_total_invested - 1) if new_total_invested > 0 else 0
    
    save_balance("hfea", {
        "total_invested": new_total_invested,
        "current_positions": new_positions,
        "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
        "last_allocation": {
            "upro_amount": upro_amount,
            "tmf_amount": tmf_amount,
            "kmlm_amount": kmlm_amount,
            "upro_price": upro_price,
            "tmf_price": tmf_price,
            "kmlm_price": kmlm_price
        },
        "trades_executed": [f"{t['symbol']}: {t['shares']:.4f} shares" for t in trades_executed]
    }, env)
    
    # Single clean Telegram message
    msg = f"📊 HFEA (17.5%) — ${investment_amount:,.2f}\n\n"
    for t in trades_executed:
        msg += f"Bought {t['shares']:.4f} {t['symbol']} @ ${t['price']:.2f} (${t['amount']:.2f})\n"
    msg += f"\nTotal invested: ${new_total_invested:,.2f}\n"
    msg += f"Current value: ${current_value:,.2f}\n"
    msg += f"Return: {strategy_return:+.1%}"
    send_telegram_message(msg)
    
    return "Monthly investment executed."


def get_hfea_positions(api):
    return get_strategy_positions(api, STRATEGY_SYMBOLS["hfea"], "HFEA")


def sync_hfea_positions_from_alpaca(api, env="live"):
    """
    Sync HFEA positions from Alpaca to Firestore.
    This ensures Firestore data matches actual positions in Alpaca.
    
    Args:
        api: Alpaca API credentials dict
        env: Environment ("live" or "paper") - determines Firestore collection
    
    Returns:
        dict: Updated positions dictionary
    """
    try:
        # Get actual positions from Alpaca
        actual_positions = get_hfea_positions(api)
        
        if not actual_positions:
            print("Warning: No HFEA positions found in Alpaca, cannot sync")
            return {}
        
        # Load existing Firestore data
        balances = load_balances(env)
        hfea_data = balances.get("hfea", {})
        
        # Update positions while preserving other data
        hfea_data["current_positions"] = actual_positions
        hfea_data["last_sync_date"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Save to Firestore
        save_balance("hfea", hfea_data, env)
        
        print(f"Synced HFEA positions from Alpaca to Firestore: {actual_positions}")
        return actual_positions
        
    except Exception as e:
        print(f"Error syncing HFEA positions from Alpaca: {e}")
        return {}


def get_hfea_status(api, env="live"):
    """
    Get current HFEA strategy status using actual Alpaca positions.
    This function always uses Alpaca as the source of truth for positions.
    
    Args:
        api: Alpaca API credentials dict
        env: Environment ("live" or "paper") - determines Firestore collection
    
    Returns:
        dict: Dictionary with current_positions, last_allocation, total_invested, etc.
    """
    try:
        # Get actual positions from Alpaca (source of truth)
        actual_positions = get_hfea_positions(api)
        
        # Load other data from Firestore
        balances = load_balances(env)
        hfea_data = balances.get("hfea", {})
        
        # Build status dictionary with actual positions
        status = {
            "current_positions": actual_positions,  # Always use Alpaca data
            "last_allocation": hfea_data.get("last_allocation", {}),
            "total_invested": hfea_data.get("total_invested", 0),
            "last_trade_date": hfea_data.get("last_trade_date", ""),
            "trades_executed": hfea_data.get("trades_executed", []),
            "last_sync_date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        return status
        
    except Exception as e:
        print(f"Error getting HFEA status: {e}")
        return {
            "current_positions": {},
            "last_allocation": {},
            "total_invested": 0,
            "last_trade_date": "",
            "trades_executed": [],
            "error": str(e)
        }


def recalculate_all_strategies_cost_basis(api, env="live", silent=False):
    """
    Recalculate cost basis for ALL strategies from Alpaca positions and update Firestore.
    Uses actual cost_basis from Alpaca as the source of truth.
    
    This ensures Firestore total_invested values stay synchronized with Alpaca's actual cost basis,
    preventing drift from manual trades, failed trades, or data resets.
    
    Args:
        api: Alpaca API credentials dict
        env: Environment ("live" or "paper")
        silent: If True, suppress detailed output (useful when called from orchestrator)
    
    Returns:
        dict: Summary of updates with old/new values and differences
    """
    try:
        if not silent:
            print("=" * 80)
            print("RECALCULATING COST BASIS FOR ALL STRATEGIES")
            print("=" * 80)
        
        # Get all positions from Alpaca
        positions = list_positions(api)
        
        results = {}
        total_old = 0
        total_new = 0
        
        # Process each strategy
        for strategy_name, symbols in STRATEGY_SYMBOLS.items():
            if not silent:
                print(f"\n📊 {strategy_name.upper().replace('_', ' ')}")
                print(f"Symbols: {', '.join(symbols)}")
            
            # Calculate total cost basis for this strategy's positions
            total_cost_basis = 0
            position_details = {}
            
            for position in positions:
                symbol = position.get("symbol")
                if symbol in symbols:
                    cost_basis = float(position.get("cost_basis", 0))
                    qty = float(position.get("qty", 0))
                    market_value = float(position.get("market_value", 0))
                    total_cost_basis += cost_basis
                    position_details[symbol] = {
                        "shares": qty,
                        "cost_basis": cost_basis,
                        "market_value": market_value
                    }
            
            # Load existing Firestore data
            balances = load_balances(env)
            strategy_data = balances.get(strategy_name, {})
            old_total_invested = strategy_data.get("total_invested", 0)
            
            # Update total_invested with actual cost basis from Alpaca
            strategy_data["total_invested"] = total_cost_basis
            strategy_data["cost_basis_recalculated_date"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            strategy_data["old_total_invested"] = old_total_invested  # Keep for reference
            strategy_data["position_cost_basis"] = position_details
            
            # Save to Firestore
            save_balance(strategy_name, strategy_data, env)
            
            difference = total_cost_basis - old_total_invested
            total_old += old_total_invested
            total_new += total_cost_basis
            
            results[strategy_name] = {
                "old_total_invested": old_total_invested,
                "new_total_invested": total_cost_basis,
                "difference": difference,
                "position_details": position_details
            }
            
            if not silent:
                print(f"Old Firestore: ${old_total_invested:10.2f}")
                print(f"New Firestore: ${total_cost_basis:10.2f}")
                print(f"Difference:    ${difference:10.2f}")
            elif difference != 0:
                # Even in silent mode, log if there was a correction
                print(f"Cost basis sync: {strategy_name} corrected by ${difference:.2f}")
        
        if not silent:
            print()
            print("=" * 80)
            print("SUMMARY:")
            print("=" * 80)
            print(f"Total old Firestore total_invested: ${total_old:.2f}")
            print(f"Total new Firestore total_invested: ${total_new:.2f}")
            print(f"Total difference:                   ${total_new - total_old:.2f}")
            print("=" * 80)
        
        return {
            "success": True,
            "total_old": total_old,
            "total_new": total_new,
            "total_difference": total_new - total_old,
            "strategies": results
        }
        
    except Exception as e:
        error_msg = f"Error recalculating all strategies cost basis: {e}"
        print(error_msg)
        return {
            "success": False,
            "error": error_msg,
            "total_old": 0,
            "total_new": 0,
            "total_difference": 0,
            "strategies": {}
        }


def get_hfea_allocations(api):
    positions = {p["symbol"]: float(p["market_value"]) for p in list_positions(api)}
    upro_value = positions.get("UPRO", 0)
    tmf_value = positions.get("TMF", 0)
    kmlm_value = positions.get("KMLM", 0)
    total_value = upro_value + tmf_value + kmlm_value
    # Calculate current and target allocations
    current_upro_percent = upro_value / total_value if total_value else 0
    current_tmf_percent = tmf_value / total_value if total_value else 0
    current_kmlm_percent = kmlm_value / total_value if total_value else 0
    target_upro_value = total_value * upro_allocation
    target_tmf_value = total_value * tmf_allocation
    target_kmlm_value = total_value * kmlm_allocation
    # Calculate deviations
    upro_diff = upro_value - target_upro_value
    tmf_diff = tmf_value - target_tmf_value
    kmlm_diff = kmlm_value - target_kmlm_value
    return (
        upro_diff,
        tmf_diff,
        kmlm_diff,
        upro_value,
        tmf_value,
        kmlm_value,
        total_value,
        target_upro_value,
        target_tmf_value,
        target_kmlm_value,
        current_upro_percent,
        current_tmf_percent,
        current_kmlm_percent,
    )


def get_holding_fund_shares(api, ticker):
    """
    Get current shares of holding fund from Alpaca.
    
    Args:
        api: Alpaca API credentials
        ticker: Ticker symbol of the holding fund
        
    Returns:
        float: Number of shares held, or 0 if not found
    """
    positions = list_positions(api)
    for position in positions:
        if position["symbol"] == ticker:
            return float(position["qty"])
    return 0.0


def get_holding_fund_value(api, ticker):
    """
    Get current market value of holding fund from Alpaca.
    
    Args:
        api: Alpaca API credentials
        ticker: Ticker symbol of the holding fund
        
    Returns:
        float: Market value of the holding fund, or 0 if not found
    """
    positions = {p["symbol"]: float(p["market_value"]) for p in list_positions(api)}
    return positions.get(ticker, 0.0)


def get_spxl_sma_positions(api):
    return get_strategy_positions(api, STRATEGY_SYMBOLS["spxl_sma"], "SPXL SMA")


def get_spxl_sma_value(api):
    """
    Get current total value of SPXL SMA strategy from Alpaca positions.
    Includes both SPXL and SGOV (holding fund) positions.
    
    Args:
        api: Alpaca API credentials
    
    Returns:
        dict: Dictionary with total_value, position_breakdown, and invested_amount
    """
    try:
        positions = {p["symbol"]: float(p["market_value"]) for p in list_positions(api)}
        spxl_value = positions.get("SPXL", 0)
        sgov_value = get_holding_fund_value(api, spxl_sma_holding_fund)
        total_value = spxl_value + sgov_value
        
        position_breakdown = {
            "SPXL": spxl_value,
            spxl_sma_holding_fund: sgov_value
        }
        
        # Get invested amount from Firestore
        balances = load_balances()
        # Use lowercase to match other strategies (nine_sig, dual_momentum, etc.)
        # Try both for backward compatibility during migration
        spxl_data = balances.get("spxl_sma", {}) or balances.get("SPXL_SMA", {})
        invested_amount = spxl_data.get("total_invested", 0)
        
        return {
            "total_value": total_value,
            "position_breakdown": position_breakdown,
            "invested_amount": invested_amount
        }
    except Exception as e:
        print(f"Error getting SPXL SMA value: {e}")
        return {
            "total_value": 0,
            "position_breakdown": {},
            "invested_amount": 0
        }


def rebalance_portfolio(api):
    if not check_trading_day(mode="quarterly"):
        print("Not first trading day of the month in this Quarter")
        return "Not first trading day of the month in this Quarter"
    if quarterly_run_complete("hfea", env=alpaca_environment):
        msg = f"HFEA quarterly rebalance already executed for {current_quarter_id()} — skipping."
        print(msg)
        return msg
    # Get UPRO, TMF, and KMLM values and deviations from target allocation
    (
        upro_diff,
        tmf_diff,
        kmlm_diff,
        upro_value,
        tmf_value,
        kmlm_value,
        total_value,
        target_upro_value,
        target_tmf_value,
        target_kmlm_value,
        current_upro_percent,
        current_tmf_percent,
        current_kmlm_percent,
    ) = get_hfea_allocations(api)

    # Apply a margin for fees (e.g., 0.5%)
    fee_margin = 0.995

    # If the total value is 0, nothing to rebalance
    if total_value == 0:
        print("No holdings to rebalance.")
        send_telegram_message("No holdings to rebalance for HFEA Strategy.")
        return "No holdings to rebalance for HFEA Strategy."

    # Define trade parameters for each ETF
    rebalance_actions = []

    # If UPRO is over-allocated, adjust TMF or KMLM if under-allocated
    if upro_diff > 0:
        if tmf_diff < 0:
            upro_shares_to_sell = min(upro_diff, abs(tmf_diff)) / float(get_latest_trade(api, "UPRO"))
            tmf_shares_to_buy = (
                upro_shares_to_sell
                * float(get_latest_trade(api, "UPRO"))
                / float(get_latest_trade(api, "TMF"))
            ) * fee_margin
            rebalance_actions.append(("UPRO", upro_shares_to_sell, "sell"))
            rebalance_actions.append(("TMF", tmf_shares_to_buy, "buy"))

        if kmlm_diff < 0:
            upro_shares_to_sell = min(upro_diff, abs(kmlm_diff)) / float(get_latest_trade(api, "UPRO"))
            kmlm_shares_to_buy = (
                upro_shares_to_sell
                * float(get_latest_trade(api, "UPRO"))
                / float(get_latest_trade(api, "KMLM"))
            ) * fee_margin
            rebalance_actions.append(("UPRO", upro_shares_to_sell, "sell"))
            rebalance_actions.append(("KMLM", kmlm_shares_to_buy, "buy"))

    # If TMF is over-allocated, adjust UPRO or KMLM if under-allocated
    if tmf_diff > 0:
        if upro_diff < 0:
            tmf_shares_to_sell = min(tmf_diff, abs(upro_diff)) / float(get_latest_trade(api, "TMF"))
            upro_shares_to_buy = (
                tmf_shares_to_sell
                * float(get_latest_trade(api, "TMF"))
                / float(get_latest_trade(api, "UPRO"))
            ) * fee_margin
            rebalance_actions.append(("TMF", tmf_shares_to_sell, "sell"))
            rebalance_actions.append(("UPRO", upro_shares_to_buy, "buy"))

        if kmlm_diff < 0:
            tmf_shares_to_sell = min(tmf_diff, abs(kmlm_diff)) / float(get_latest_trade(api, "TMF"))
            kmlm_shares_to_buy = (
                tmf_shares_to_sell
                * float(get_latest_trade(api, "TMF"))
                / float(get_latest_trade(api, "KMLM"))
            ) * fee_margin
            rebalance_actions.append(("TMF", tmf_shares_to_sell, "sell"))
            rebalance_actions.append(("KMLM", kmlm_shares_to_buy, "buy"))

    # If KMLM is over-allocated, adjust UPRO or TMF if under-allocated
    if kmlm_diff > 0:
        if upro_diff < 0:
            kmlm_shares_to_sell = min(kmlm_diff, abs(upro_diff)) / float(get_latest_trade(api, "KMLM"))
            upro_shares_to_buy = (
                kmlm_shares_to_sell
                * float(get_latest_trade(api, "KMLM"))
                / float(get_latest_trade(api, "UPRO"))
            ) * fee_margin
            rebalance_actions.append(("KMLM", kmlm_shares_to_sell, "sell"))
            rebalance_actions.append(("UPRO", upro_shares_to_buy, "buy"))

        if tmf_diff < 0:
            kmlm_shares_to_sell = min(kmlm_diff, abs(tmf_diff)) / float(get_latest_trade(api, "KMLM"))
            tmf_shares_to_buy = (
                kmlm_shares_to_sell
                * float(get_latest_trade(api, "KMLM"))
                / float(get_latest_trade(api, "TMF"))
            ) * fee_margin
            rebalance_actions.append(("KMLM", kmlm_shares_to_sell, "sell"))
            rebalance_actions.append(("TMF", tmf_shares_to_buy, "buy"))

    # Execute rebalancing actions
    for symbol, qty, action in rebalance_actions:
        if qty > 0:
            order = submit_order(api, symbol, qty, action)
            action_verb = "Bought" if action == "buy" else "Sold"
            wait_for_order_fill(api, order["id"])
            print(f"{action_verb} {qty:.6f} shares of {symbol} to rebalance.")
            send_telegram_message(
                f"{action_verb} {qty:.6f} shares of {symbol} to rebalance."
            )

    # Report completion of rebalancing check
    print("Rebalance check completed.")
    mark_quarterly_run_complete("hfea", "REBALANCED", env=alpaca_environment)
    return "Rebalance executed."


# Unified function to fetch all market data and calculate all SMAs at once
def update_market_data(symbol, env="live"):
    """
    Fetch fresh market data from Alpaca and calculate ALL metrics in one operation.
    ALWAYS calculates and saves: price, sma200, sma255, sma200_state, sma255_state.
    This ensures complete consistency across all symbols and makes the system extensible.
    
    Args:
        symbol: Stock symbol (e.g., "SPY", "URTH")
        env: Environment ("live" or "paper") - determines Firestore collection
    
    Returns:
        dict with keys: price, sma200, sma255, sma200_state, sma255_state, timestamp
    """
    print(f"Fetching fresh market data for {symbol} from Alpaca IEX feed")
    
    # Get API credentials
    api = set_alpaca_environment(env=env)
    
    # Fetch historical data (500 days covers both 200 and 255-day SMAs)
    closes = get_alpaca_historical_bars(api, symbol, days=500)
    
    if not closes or len(closes) < 255:
        raise ValueError(f"Insufficient Alpaca data for {symbol}. Got {len(closes) if closes else 0} bars, need at least 255.")
    
    # Get current price from latest trade
    current_price = get_latest_trade(api, symbol)
    
    # Calculate both SMAs from same dataset
    df = pd.DataFrame({'close': closes})
    sma_200 = df['close'].rolling(window=200).mean().iloc[-1]
    sma_255 = df['close'].rolling(window=255).mean().iloc[-1]
    
    # Calculate states for both SMA periods
    # Using 1% noise threshold (matches default in alert system)
    noise_threshold_pct = 1.0  # 1% threshold to avoid noise (as percentage)
    
    # 200-day state
    diff_200_pct = ((current_price - sma_200) / sma_200) * 100
    if diff_200_pct > noise_threshold_pct:
        sma200_state = "above"
    elif diff_200_pct < -noise_threshold_pct:
        sma200_state = "below"
    else:
        sma200_state = "neutral"
    
    # 255-day state
    diff_255_pct = ((current_price - sma_255) / sma_255) * 100
    if diff_255_pct > noise_threshold_pct:
        sma255_state = "above"
    elif diff_255_pct < -noise_threshold_pct:
        sma255_state = "below"
    else:
        sma255_state = "neutral"
    
    # Prepare complete market data
    market_data = {
        "symbol": symbol,
        "price": float(current_price),
        "sma200": float(sma_200),
        "sma255": float(sma_255),
        "sma200_state": sma200_state,
        "sma255_state": sma255_state,
        "timestamp": datetime.datetime.utcnow()
    }
    
    # Save everything to Firestore at once
    doc_id = normalize_symbol(symbol)
    doc_ref = get_firestore_client().collection(f"market-data-{env}").document(doc_id)
    
    # Get existing data (to preserve alert tracking fields)
    doc = doc_ref.get()
    if doc.exists:
        existing_data = doc.to_dict()
        # Preserve alert date fields if they exist
        for field in ['sma200_last_hour_alert_date', 'sma255_last_hour_alert_date']:
            if field in existing_data:
                market_data[field] = existing_data[field]
    
    # Write complete data
    doc_ref.set(market_data)
    
    print(f"Updated {symbol}: Price=${market_data['price']:.2f}, SMA200=${market_data['sma200']:.2f} ({sma200_state}), SMA255=${market_data['sma255']:.2f} ({sma255_state})")
    
    return market_data


def check_trading_day(mode="daily"):
    """
    Check if today is a trading day, the first trading day of the month, or the first trading day of the quarter.

    :param mode: "daily" for a regular trading day, "monthly" for the first trading day of the month,
                 "quarterly" for the first trading day of the quarter.
    :return: True if the condition is met, False otherwise.
    """
    # Get current date
    today = datetime.datetime.now()

    # Load the NYSE market calendar
    nyse = mcal.get_calendar("NYSE")

    # Check if the market is open today
    schedule = nyse.schedule(start_date=today.date(), end_date=today.date())
    if schedule.empty:
        return False  # Market is closed today (e.g., weekend or holiday)

    if mode == "daily":
        return True  # It's a trading day

    # Check if it's the first trading day of the month
    if mode == "monthly":
        first_day_of_month = today.replace(day=1)
        schedule = nyse.schedule(
            start_date=first_day_of_month,
            end_date=first_day_of_month + datetime.timedelta(days=6),
        )
        first_trading_day = schedule.index[0].date()
        return today.date() == first_trading_day

    # Check if it's the first trading day of the quarter
    if mode == "quarterly":
        first_day_of_quarter = today.replace(day=1)
        if today.month not in [1, 4, 7, 10]:
            return False  # Not the first month of a quarter
        schedule = nyse.schedule(
            start_date=first_day_of_quarter,
            end_date=first_day_of_quarter + datetime.timedelta(days=6),
        )
        first_trading_day = schedule.index[0].date()
        return today.date() == first_trading_day

    raise ValueError("Invalid mode. Use 'daily', 'monthly', or 'quarterly'.")


def monthly_buying_sma(api, symbol, force_execute=False, investment_calc=None, margin_result=None, skip_order_wait=False, env="live"):
    """
    Monthly SMA-based investment (SPXL when bullish, SGOV when bearish).
    Uses All-or-Nothing approach: invest full amount or skip entirely.
    Sends exactly one Telegram message at the end summarizing the outcome.
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        return "Not first trading day of the month"

    if force_execute:
        print(f"{symbol} SMA: Force execution enabled - bypassing trading day check")

    if symbol != "SPXL":
        return f"Unknown symbol: {symbol}"

    spy_data = get_all_market_data("SPY", env=env)
    if spy_data is None:
        spy_data = update_market_data("SPY", env=env)

    sma_200 = spy_data["sma200"]
    latest_price = spy_data["price"]

    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)
    
    investment_amount = investment_calc["strategy_amounts"]["spxl_allo"]
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    buying_power = investment_calc["total_available"] + investment_calc["margin_approved"]
    
    is_bullish = latest_price > sma_200 * (1 + margin)
    trend_label = "🟢 Bullish" if is_bullish else "🔴 Bearish"
    
    def _skip(reason):
        msg = f"📈 {symbol} SMA (17.5%) — ${investment_amount:,.2f}\n"
        msg += f"Trend: {trend_label} (SPY ${latest_price:.2f} vs SMA ${sma_200:.2f})\n"
        msg += f"⏭ {reason}"
        send_telegram_message(msg)
        print(reason)
        return reason

    # Load strategy state
    balances = load_balances(env)
    spxl_data = balances.get("spxl_sma", {}) or balances.get(f"{symbol}_SMA", {})
    total_invested = spxl_data.get("total_invested", 0)
    current_shares = spxl_data.get("current_shares", 0)
    holding_fund_position = spxl_data.get("holding_fund_position", {})
    
    sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
    sgov_value = get_holding_fund_value(api, spxl_sma_holding_fund)
    sgov_price = float(get_latest_trade(api, spxl_sma_holding_fund)) if sgov_value > 0 or investment_amount > 0 else 0
    
    print(f"{symbol}: Investment=${investment_amount:.2f}, SPY=${latest_price:.2f}, SMA=${sma_200:.2f}")
    
    # Shared gate checks for both bullish and bearish paths
    # When margin gates fail and leverage <= 1.0×, the bearish path can still buy SGOV (T-bills)
    # and the bullish path can still buy SPXL from cash. We only skip entirely when deleveraging.
    if target_margin == 0 and leverage > 1.0:
        return _skip(f"Skipped — deleveraging required ({leverage:.2f}x)")

    if buying_power < investment_amount:
        return _skip(f"Skipped — insufficient buying power (${buying_power:,.2f})")
    
    if investment_amount < margin_control_config["min_investment"]:
        return _skip(f"Skipped — ${investment_amount:.2f} below $1.00 minimum")
    
    # Projected leverage check
    if target_margin > 0:
        portfolio_value = metrics.get("portfolio_value", 0)
        current_equity = metrics.get("equity", 0)
        if portfolio_value > 0 and current_equity > 0:
            projected_leverage = (portfolio_value + investment_amount) / current_equity
            if projected_leverage >= margin_control_config["max_leverage"]:
                return _skip(f"Skipped — projected leverage {projected_leverage:.3f}x exceeds limit")
    
    trades_info = []
    
    if is_bullish:
        # Sell SGOV to switch to SPXL if needed
        if sgov_shares > 0:
            try:
                sell_order = submit_order(api, spxl_sma_holding_fund, sgov_shares, "sell")
                if not skip_order_wait:
                    wait_for_order_fill(api, sell_order["id"])
                trades_info.append(f"Sold {sgov_shares:.4f} {spxl_sma_holding_fund} (${sgov_value:.2f})")
                print(f"Sold {sgov_shares:.6f} shares of {spxl_sma_holding_fund}")
            except Exception as e:
                send_telegram_message(f"📈 {symbol} SMA (17.5%)\n❌ Error selling {spxl_sma_holding_fund}: {str(e)}")
                return f"Failed to sell {spxl_sma_holding_fund}: {str(e)}"
        
        price = get_latest_trade(api, symbol)
        shares_to_buy = investment_amount / price
        
        if shares_to_buy > 0:
            order = submit_order(api, symbol, shares_to_buy, "buy")
            if not skip_order_wait:
                wait_for_order_fill(api, order["id"])
            
            new_total_shares = current_shares + shares_to_buy
            new_total_invested = total_invested + investment_amount
            trades_info.append(f"Bought {shares_to_buy:.4f} {symbol} @ ${price:.2f} (${investment_amount:.2f})")
            
            updated_sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
            holding_fund_position[spxl_sma_holding_fund] = updated_sgov_shares
            
            save_balance("spxl_sma", {
                "total_invested": new_total_invested,
                "current_shares": new_total_shares,
                "holding_fund_position": holding_fund_position,
                "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
                "last_trade": {"action": "buy", "shares": shares_to_buy, "price": price, "amount": investment_amount},
                "trend_analysis": {"spy_price": latest_price, "spy_sma_200": sma_200, "trend_status": "bullish", "margin_band": margin}
            }, env)
            
            # Estimate current value (SPXL shares + any remaining SGOV)
            current_value = new_total_shares * price + updated_sgov_shares * sgov_price
            strategy_return = (current_value / new_total_invested - 1) if new_total_invested > 0 else 0
            
            msg = f"📈 {symbol} SMA (17.5%) — ${investment_amount:,.2f}\n"
            msg += f"Trend: {trend_label} (SPY ${latest_price:.2f} vs SMA ${sma_200:.2f})\n\n"
            for t in trades_info:
                msg += f"{t}\n"
            msg += f"\nTotal invested: ${new_total_invested:,.2f}\n"
            msg += f"Current value: ${current_value:,.2f}\n"
            msg += f"Return: {strategy_return:+.1%}"
            send_telegram_message(msg)
            return f"Bought {shares_to_buy:.6f} shares of {symbol}."
        else:
            return _skip(f"Amount too small to buy {symbol}")
    else:
        # Bearish: buy SGOV T-bills
        if sgov_price <= 0:
            send_telegram_message(f"📈 {symbol} SMA (17.5%)\n❌ Could not get {spxl_sma_holding_fund} price")
            return f"Could not get price for {spxl_sma_holding_fund}"
        
        sgov_shares_to_buy = investment_amount / sgov_price
        if sgov_shares_to_buy > 0:
            try:
                sgov_order = submit_order(api, spxl_sma_holding_fund, sgov_shares_to_buy, "buy")
                if not skip_order_wait:
                    wait_for_order_fill(api, sgov_order["id"])
                
                new_total_invested = total_invested + investment_amount
                updated_sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
                holding_fund_position[spxl_sma_holding_fund] = updated_sgov_shares
                
                save_balance("spxl_sma", {
                    "total_invested": new_total_invested,
                    "current_shares": current_shares,
                    "holding_fund_position": holding_fund_position,
                    "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
                    "last_trade": {"action": "buy_tbill", "shares": sgov_shares_to_buy, "price": sgov_price, "amount": investment_amount},
                    "trend_analysis": {"spy_price": latest_price, "spy_sma_200": sma_200, "trend_status": "bearish", "margin_band": margin}
                }, env)
                
                current_value = current_shares * float(get_latest_trade(api, symbol)) + updated_sgov_shares * sgov_price if current_shares > 0 else updated_sgov_shares * sgov_price
                strategy_return = (current_value / new_total_invested - 1) if new_total_invested > 0 else 0
                
                msg = f"📈 {symbol} SMA (17.5%) — ${investment_amount:,.2f}\n"
                msg += f"Trend: {trend_label} (SPY ${latest_price:.2f} vs SMA ${sma_200:.2f})\n\n"
                msg += f"Bought {sgov_shares_to_buy:.4f} {spxl_sma_holding_fund} @ ${sgov_price:.2f} (T-bills)\n\n"
                msg += f"Total invested: ${new_total_invested:,.2f}\n"
                msg += f"Current value: ${current_value:,.2f}\n"
                msg += f"Return: {strategy_return:+.1%}"
                send_telegram_message(msg)
                return f"Bought {sgov_shares_to_buy:.6f} shares of {spxl_sma_holding_fund}"
            except Exception as e:
                send_telegram_message(f"📈 {symbol} SMA (17.5%)\n❌ Error buying {spxl_sma_holding_fund}: {str(e)}")
                return f"Failed to buy {spxl_sma_holding_fund}: {str(e)}"
        else:
            return _skip(f"Amount too small to buy {spxl_sma_holding_fund}")


def daily_trade_sma(api, symbol, env="live"):
    if not check_trading_day(mode="daily"):
        send_telegram_message(f"Market closed today. Skipping 200SMA. for {symbol}")
        return "Market closed today."

    # Use SPY as S&P 500 proxy for SPXL trading decisions
    if symbol == "SPXL":
        # Get all SPY market data at once (efficient single fetch/read)
        spy_data = get_all_market_data("SPY", env=env)
        if spy_data is None:
            spy_data = update_market_data("SPY", env=env)
        
        sma_200 = spy_data["sma200"]
        latest_price = spy_data["price"]
    else:
        return f"Unknown symbol: {symbol}"

    if latest_price < sma_200 * (1 - margin):
        positions = list_positions(api)
        position = next((p for p in positions if p["symbol"] == symbol), None)

        if position:
            shares_to_sell = float(position["qty"])
            invested = float(position["market_value"])
            # Sell all SPXL shares
            sell_order = submit_order(api, symbol, shares_to_sell, "sell")
            send_telegram_message(
                f"Sold all {shares_to_sell:.6f} shares of {symbol} because Index is significantly below 200-SMA."
            )
            # Wait for the sell order to be filled
            wait_for_order_fill(api, sell_order["id"])
            
            # Buy SGOV T-bills with proceeds
            try:
                sgov_price = float(get_latest_trade(api, spxl_sma_holding_fund))
                if sgov_price > 0:
                    sgov_shares_to_buy = invested / sgov_price
                    if sgov_shares_to_buy > 0:
                        sgov_order = submit_order(api, spxl_sma_holding_fund, sgov_shares_to_buy, "buy")
                        wait_for_order_fill(api, sgov_order["id"])
                        send_telegram_message(
                            f"Bought {sgov_shares_to_buy:.6f} shares of {spxl_sma_holding_fund} (${invested:.2f}) with proceeds from {symbol} sale"
                        )
            except Exception as e:
                print(f"Error buying {spxl_sma_holding_fund} after selling {symbol}: {e}")
                send_telegram_message(f"Warning: Sold {symbol} but failed to buy {spxl_sma_holding_fund}: {e}")
            
            # Update Firestore with comprehensive tracking
            # Use lowercase to match other strategies, try both for backward compatibility
            existing_data = load_balances().get("spxl_sma", {}) or load_balances().get(f"{symbol}_SMA", {})
            updated_sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
            holding_fund_position = existing_data.get("holding_fund_position", {})
            holding_fund_position[spxl_sma_holding_fund] = updated_sgov_shares
            
            save_balance("spxl_sma", {
                "total_invested": existing_data.get("total_invested", invested),
                "current_shares": 0,  # Sold all shares
                "holding_fund_position": holding_fund_position,
                "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
                "last_trade": {
                    "action": "sell_to_tbill",
                    "shares": shares_to_sell,
                    "price": invested / shares_to_sell if shares_to_sell > 0 else 0,
                    "amount": invested
                },
                "trend_analysis": {
                    "spy_price": latest_price,
                    "spy_sma_200": sma_200,
                    "trend_status": "bearish",
                    "margin_band": margin
                }
            })
        else:
            send_telegram_message(
                f"Index is significantly below 200-SMA and no {symbol} position to sell."
            )
            return f"Index is significantly below 200-SMA and no {symbol} position to sell."
    elif latest_price > sma_200 * (1 + margin):
        # Check if we have SGOV to sell and convert to SPXL
        positions = list_positions(api)
        position = next((p for p in positions if p["symbol"] == symbol), None)
        sgov_position = next((p for p in positions if p["symbol"] == spxl_sma_holding_fund), None)
        
        if sgov_position and not position:
            # We have SGOV but no SPXL - sell SGOV and buy SPXL
            sgov_shares_to_sell = float(sgov_position["qty"])
            sgov_value = float(sgov_position["market_value"])
            
            try:
                # Sell SGOV
                sgov_sell_order = submit_order(api, spxl_sma_holding_fund, sgov_shares_to_sell, "sell")
                wait_for_order_fill(api, sgov_sell_order["id"])
                send_telegram_message(
                    f"Sold {sgov_shares_to_sell:.6f} shares of {spxl_sma_holding_fund} (${sgov_value:.2f}) to buy {symbol}"
                )
                
                # Buy SPXL with proceeds
                spxl_price = float(get_latest_trade(api, symbol))
                spxl_shares_to_buy = sgov_value / spxl_price
                if spxl_shares_to_buy > 0:
                    spxl_buy_order = submit_order(api, symbol, spxl_shares_to_buy, "buy")
                    wait_for_order_fill(api, spxl_buy_order["id"])
                    
                    # Get updated position
                    positions = list_positions(api)
                    position = next((p for p in positions if p["symbol"] == symbol), None)
                    invested = float(position["market_value"]) if position else sgov_value
                    current_shares = float(position["qty"]) if position else 0
                    
                    # Update Firestore
                    # Use lowercase to match other strategies, try both for backward compatibility
                    existing_data = load_balances().get("spxl_sma", {}) or load_balances().get(f"{symbol}_SMA", {})
                    updated_sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
                    holding_fund_position = existing_data.get("holding_fund_position", {})
                    holding_fund_position[spxl_sma_holding_fund] = updated_sgov_shares
                    
                    save_balance("spxl_sma", {
                        "total_invested": existing_data.get("total_invested", invested),
                        "current_shares": current_shares,
                        "holding_fund_position": holding_fund_position,
                        "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
                        "last_trade": {
                            "action": "tbill_to_spxl",
                            "shares": spxl_shares_to_buy,
                            "price": spxl_price,
                            "amount": sgov_value
                        },
                        "trend_analysis": {
                            "spy_price": latest_price,
                            "spy_sma_200": sma_200,
                            "trend_status": "bullish",
                            "margin_band": margin
                        }
                    })
                    send_telegram_message(
                        f"Bought {spxl_shares_to_buy:.6f} shares of {symbol} with proceeds from {spxl_sma_holding_fund} sale"
                    )
                    return f"Bought {spxl_shares_to_buy:.6f} shares of {symbol} with proceeds from {spxl_sma_holding_fund} sale."
            except Exception as e:
                error_msg = f"Error converting {spxl_sma_holding_fund} to {symbol}: {e}"
                print(error_msg)
                send_telegram_message(f"{symbol} SMA Error: {error_msg}")
                return error_msg
        elif position:
            # Position exists but no new shares bought - no notification needed
            # Update Firestore with current position data (preserve rich structure)
            invested = float(position["market_value"])
            current_shares = float(position["qty"])
            
            # Load existing data to preserve other fields
            # Use lowercase to match other strategies, try both for backward compatibility
            existing_data = load_balances().get("spxl_sma", {}) or load_balances().get(f"{symbol}_SMA", {})
            holding_fund_position = existing_data.get("holding_fund_position", {})
            updated_sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
            holding_fund_position[spxl_sma_holding_fund] = updated_sgov_shares
            
            save_balance("spxl_sma", {
                "total_invested": invested,
                "current_shares": current_shares,
                "holding_fund_position": holding_fund_position,
                "last_trade_date": existing_data.get("last_trade_date", datetime.datetime.now().strftime("%Y-%m-%d")),
                "last_trade": existing_data.get("last_trade", {}),
                "trend_analysis": {
                    "spy_price": latest_price,
                    "spy_sma_200": sma_200,
                    "trend_status": "bullish",
                    "margin_band": margin
                }
            })
            return f"Index is above 200-SMA. {symbol} position already exists (${invested:.2f})"
        else:
            # No SPXL and no SGOV - nothing to do
            send_telegram_message(
                f"Index is above 200-SMA but no {symbol} or {spxl_sma_holding_fund} positions to convert"
            )
            return f"Index is above 200-SMA but no positions to convert"
    else:
        positions = list_positions(api)
        position = next((p for p in positions if p["symbol"] == symbol), None)
        
        # Load existing data to preserve other fields
        # Use lowercase to match other strategies, try both for backward compatibility
        existing_data = load_balances().get("spxl_sma", {}) or load_balances().get(f"{symbol}_SMA", {})
        holding_fund_position = existing_data.get("holding_fund_position", {})
        updated_sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
        holding_fund_position[spxl_sma_holding_fund] = updated_sgov_shares
        
        if position:
            invested = float(position["market_value"])
            current_shares = float(position["qty"])
            
            save_balance("spxl_sma", {
                "total_invested": invested,
                "current_shares": current_shares,
                "holding_fund_position": holding_fund_position,
                "last_trade_date": existing_data.get("last_trade_date", datetime.datetime.now().strftime("%Y-%m-%d")),
                "last_trade": existing_data.get("last_trade", {}),
                "trend_analysis": {
                    "spy_price": latest_price,
                    "spy_sma_200": sma_200,
                    "trend_status": "neutral",
                    "margin_band": margin
                }
            })
        else:
            # Update holding fund position even if no SPXL position
            save_balance("spxl_sma", {
                "total_invested": existing_data.get("total_invested", 0),
                "current_shares": 0,
                "holding_fund_position": holding_fund_position,
                "last_trade_date": existing_data.get("last_trade_date", datetime.datetime.now().strftime("%Y-%m-%d")),
                "last_trade": existing_data.get("last_trade", {}),
                "trend_analysis": {
                    "spy_price": latest_price,
                    "spy_sma_200": sma_200,
                    "trend_status": "neutral",
                    "margin_band": margin
                }
            })
        send_telegram_message(
            f"Index is not significantly below or above 200-SMA. No {symbol} shares sold or bought"
        )
        return f"Index is not significantly below or above 200-SMA. No {symbol} shares sold or bought"

# Function to send a message via Telegram
def send_telegram_message(message, chat_id_secret="TELEGRAM_CHAT_ID"):
    """
    Send a message to Telegram. Handles network errors gracefully.
    
    Args:
        message: Message text to send
    
    Returns:
        HTTP status code if successful, None if failed
    """
    try:
        telegram_key, chat_id = get_telegram_secrets(chat_id_secret)
        url = f"https://api.telegram.org/bot{telegram_key}/sendMessage"
        data = {"chat_id": chat_id, "text": message}
        response = requests.post(url, data=data, timeout=10)
        return response.status_code
    except Exception as e:
        # Log error but don't crash - network issues shouldn't stop execution
        print(f"Warning: Failed to send Telegram message: {str(e)}")
        return None


def get_index_data(index_symbol):
    """Fetch the all-time high and current price for an index using 5 years of Alpaca data."""
    from datetime import datetime, timedelta

    api = set_alpaca_environment(env=alpaca_environment)

    end_date = datetime.now()
    start_date = end_date - timedelta(days=1825)

    url = f"https://data.alpaca.markets/v2/stocks/{index_symbol}/bars"
    params = {
        "start": start_date.strftime("%Y-%m-%d"),
        "end": end_date.strftime("%Y-%m-%d"),
        "timeframe": "1Day",
        "limit": 10000,
        "adjustment": "split",
        "feed": "iex",
    }

    response = alpaca_request_with_retry(
        "GET", url, headers=get_auth_headers(api),
        params=params, label=f"index data for {index_symbol}", raise_on_fail=True
    )

    bars = response.json().get("bars", [])
    if not bars:
        raise ValueError(f"No Alpaca data returned for {index_symbol}")

    all_time_high = max(bar['h'] for bar in bars)
    current_price = bars[-1]['c']
    return current_price, all_time_high


def get_index_sma_state(index_symbol, sma_period, env="live"):
    """
    Load the previous SMA state for an index from Firestore.
    
    Args:
        index_symbol: Market symbol (e.g., "^GSPC")
        sma_period: SMA period (e.g., 200, 255)
        env: Environment ("live" or "paper") - determines Firestore collection
    
    Returns:
        dict with keys: state, timestamp
        Returns None if no previous state exists
    """
    try:
        # Normalize symbol for Firestore document ID
        doc_id = normalize_symbol(index_symbol)
        
        doc_ref = get_firestore_client().collection(f"market-data-{env}").document(doc_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return None
        
        data = doc.to_dict()
        
        # Extract the state field for this SMA period
        state_field = f"sma{sma_period}_state"
        state = data.get(state_field)
        
        if state is None:
            return None
        
        return {
            "state": state,
            "timestamp": data.get("timestamp")
        }
        
    except Exception as e:
        print(f"Warning: Could not load SMA state for {index_symbol}: {e}")
        return None


def save_index_sma_state(index_symbol, sma_period, state, price, sma_value, env="live"):
    """
    Save the current SMA state for an index to Firestore.
    Note: update_market_data() now handles price/SMA/state updates automatically.
    This function is kept for backward compatibility with alert system.
    
    Args:
        index_symbol: Market symbol
        sma_period: SMA period
        state: Current state ("above", "below", or "neutral")
        price: Current price (ignored - preserved from update_market_data)
        sma_value: Current SMA value (ignored - preserved from update_market_data)
        env: Environment ("live" or "paper") - determines Firestore collection
    """
    try:
        # Normalize symbol for Firestore document ID
        doc_id = normalize_symbol(index_symbol)
        
        doc_ref = get_firestore_client().collection(f"market-data-{env}").document(doc_id)
        
        # Get existing data
        doc = doc_ref.get()
        if not doc.exists:
            print(f"Warning: No market data exists for {index_symbol}. Call update_market_data() first.")
            return
        
        data = doc.to_dict()
        
        # Only update the specific state field (price and SMA already set by update_market_data)
        data[f"sma{sma_period}_state"] = state
        data["timestamp"] = datetime.datetime.utcnow()
        
        doc_ref.set(data)
        
    except Exception as e:
        print(f"Warning: Could not save SMA state for {index_symbol}: {e}")


def is_last_trading_hour():
    """
    Check if current time is within the last hour of the trading day.
    
    Returns:
        bool: True if within 1 hour of market close, False otherwise
    """
    try:
        # Get current time
        now = datetime.datetime.now()
        
        # Load NYSE calendar
        nyse = mcal.get_calendar("NYSE")
        
        # Get today's schedule
        schedule = nyse.schedule(start_date=now.date(), end_date=now.date())
        
        if schedule.empty:
            # Market is closed today
            return False
        
        # Get market close time for today
        market_close = schedule.iloc[0]['market_close']
        
        # Convert to naive datetime for comparison (both in local timezone)
        if hasattr(market_close, 'tz_localize'):
            market_close_naive = market_close.tz_localize(None)
        elif hasattr(market_close, 'tz_convert'):
            market_close_naive = market_close.tz_convert(None)
        else:
            market_close_naive = market_close.replace(tzinfo=None)
        
        # Calculate time until market close
        time_until_close = market_close_naive - now
        
        # Check if within last hour (3600 seconds)
        return 0 <= time_until_close.total_seconds() <= 3600
        
    except Exception as e:
        print(f"Warning: Could not determine if last trading hour: {e}")
        return False


def was_last_hour_alert_sent_today(index_symbol, sma_period, env="live"):
    """
    Check if a last-hour confirmation alert was already sent today.
    
    Args:
        index_symbol: Market symbol
        sma_period: SMA period
        env: Environment ("live" or "paper") - determines Firestore collection
    
    Returns:
        bool: True if alert was already sent today, False otherwise
    """
    try:
        # Normalize symbol for Firestore document ID
        doc_id = normalize_symbol(index_symbol)
        
        doc_ref = get_firestore_client().collection(f"market-data-{env}").document(doc_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return False
        
        data = doc.to_dict()
        
        # Get the last hour alert date field for this SMA period
        alert_date_field = f"sma{sma_period}_last_hour_alert_date"
        last_alert_date = data.get(alert_date_field)
        
        if not last_alert_date:
            return False
        
        # Check if alert was sent today
        today = datetime.datetime.now().date()
        
        # Handle both string and datetime formats
        if isinstance(last_alert_date, str):
            last_alert_date = datetime.datetime.fromisoformat(last_alert_date).date()
        elif hasattr(last_alert_date, 'date'):
            last_alert_date = last_alert_date.date()
        
        return last_alert_date == today
        
    except Exception as e:
        print(f"Warning: Could not check last hour alert status: {e}")
        return False


def mark_last_hour_alert_sent(index_symbol, sma_period, env="live"):
    """
    Mark that a last-hour confirmation alert was sent today.
    Updates the unified market-data document with the alert date.
    
    Args:
        index_symbol: Market symbol
        sma_period: SMA period
        env: Environment ("live" or "paper") - determines Firestore collection
    """
    try:
        # Normalize symbol for Firestore document ID
        doc_id = normalize_symbol(index_symbol)
        
        doc_ref = get_firestore_client().collection(f"market-data-{env}").document(doc_id)
        
        # Get existing data or create new
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
        else:
            data = {"symbol": index_symbol}
        
        # Update the last hour alert date field for this SMA period
        alert_date_field = f"sma{sma_period}_last_hour_alert_date"
        data[alert_date_field] = datetime.datetime.now().date().isoformat()
        data["timestamp"] = datetime.datetime.utcnow()
        
        doc_ref.set(data)
        
    except Exception as e:
        print(f"Warning: Could not mark last hour alert as sent: {e}")




# ─────────────────────────────────────────────────────────────────────────────
# EODHD-Zweig des Index-Alerts (EUR-notierte XETRA-Tracker)
#
# Warum eine eigene Quelle statt Alpaca: der Alert entscheidet ueber einen Trade,
# der um 17:30 CET an der XETRA in EUR ausgefuehrt wird. Alpaca fuehrt keine
# EUR-Variante des ACWI, liefert Bars nur mit adjustment="split" (also ohne
# Dividenden, ~1,8 %/Jahr Versatz gegen eine Net-TR-Studie) und quotet diese
# Ticker im IEX-Feed unbrauchbar breit (ACWI am 2026-09-07: 156,85/166,65).
# Details: docs/superpowers/specs/2026-09-07-acwi-world-eur-sma-alert-design.md
# ─────────────────────────────────────────────────────────────────────────────

EODHD_BASE_URL = "https://eodhd.com/api"
MAX_EOD_GAP_DAYS = 10      # beobachtete Maximalluecke bei IUSQ.XETRA: 6 (Weihnachten)
MAX_LIVE_JUMP = 0.08       # groesserer Sprung => Tickerwechsel, Split oder Fehlprint


class EodhdDataError(Exception):
    """EODHD-Daten fehlen, sind veraltet oder unplausibel.

    Wird bewusst NIE in einen Default abgefangen. Ein Alert, der bei
    Datenausfall stillschweigend 'alles in Ordnung' meldet, ist schlimmer als
    keiner - dieselbe Regel wie fuer die Margin-Gates in CLAUDE.md.
    """


def _heute_iso():
    """Heutiges Datum als ISO-String. Eigene Funktion, damit Tests sie ersetzen koennen."""
    return datetime.date.today().isoformat()


def _xetra_trading_day(date_iso):
    """Handelt die XETRA an diesem Tag? Nutzt den XETR-Kalender aus
    pandas_market_calendars (schon deployt, keine neue Abhaengigkeit).

    Wird NUR benutzt, um die Schwere einer veralteten Datenlage einzuordnen -
    nie, um einen Lauf zu ueberspringen. Waere der Kalender falsch und sagte
    'geschlossen' an einem echten Handelstag, wuerde ein Skip ein echtes Signal
    verschlucken. Primaerer Waechter bleibt die Frische der Daten selbst.
    """
    try:
        return not mcal.get_calendar("XETR").schedule(
            start_date=date_iso, end_date=date_iso).empty
    except Exception as e:
        # Kalender kaputt => im Zweifel Handelstag annehmen, damit ein echtes
        # Datenproblem laut wird statt still zu verschwinden.
        print(f"Warning: XETR-Kalender nicht lesbar ({e}) - nehme Handelstag an")
        return True


def _stale_data(index_name, run_role, was_fehlt, today_iso):
    """Veraltete Datenlage einordnen: an einem Handelstag ein echter Fehler,
    an einem Feiertag oder Wochenende ein stiller, korrekter Nichtlauf."""
    if _xetra_trading_day(today_iso):
        raise EodhdDataError(
            f"{was_fehlt} - und {today_iso} ist ein XETRA-Handelstag, "
            f"also ein echtes Datenproblem")
    print(f"{index_name} ({run_role}): {was_fehlt}; {today_iso} ist kein "
          f"XETRA-Handelstag - kein Lauf, kein Alert")
    return None


def _sma_state_from_diff(diff_percent, noise_threshold):
    """Zustand aus dem prozentualen Abstand zur SMA, mit Totband."""
    if diff_percent > noise_threshold:
        return "above"
    if diff_percent < -noise_threshold:
        return "below"
    return "neutral"


def _eodhd_sma_series(hist, live_price, period, today_iso):
    """SMA-Fenster fuer advisory/decisive: (period-1) Schlusskurse + Live-Kurs.

    `hist` ist [(datum_iso, close)] aufsteigend und darf den heutigen Bar
    enthalten - der wird verworfen, damit der Tag nicht doppelt im Fenster
    steht (einmal als Schluss, einmal als Live-Kurs).
    """
    past = [(d, c) for d, c in hist if d < today_iso]
    if len(past) < period - 1:
        raise EodhdDataError(
            f"nur {len(past)} historische Schlusskurse vor {today_iso}, "
            f"benoetigt {period - 1}")

    newest_date, newest_close = past[-1]
    gap = (datetime.date.fromisoformat(today_iso)
           - datetime.date.fromisoformat(newest_date)).days
    if gap > MAX_EOD_GAP_DAYS:
        raise EodhdDataError(
            f"neuester Schlusskurs ist {gap} Kalendertage alt ({newest_date})")

    if abs(live_price / newest_close - 1) > MAX_LIVE_JUMP:
        raise EodhdDataError(
            f"Live-Kurs {live_price:.4f} weicht "
            f"{(live_price / newest_close - 1) * 100:+.2f} % vom letzten "
            f"Schluss {newest_close:.4f} ({newest_date}) ab")

    series = [c for _, c in past[-(period - 1):]] + [live_price]
    if len(series) != period:
        raise EodhdDataError(f"Fenster hat {len(series)} statt {period} Werte")
    return series


def _eodhd_close_series(hist, period, today_iso):
    """SMA-Fenster fuer reconcile: `period` echte Schlusskurse inkl. heute."""
    if not hist or hist[-1][0] != today_iso:
        neuester = hist[-1][0] if hist else "keiner"
        raise EodhdDataError(
            f"kein Schlusskurs fuer {today_iso} vorhanden (neuester: {neuester})")
    if len(hist) < period:
        raise EodhdDataError(
            f"nur {len(hist)} Schlusskurse, benoetigt {period}")
    return [c for _, c in hist[-period:]]


def _eodhd_token():
    token = get_secret_or_env("EODHD_TOKEN")
    if not token:
        raise EodhdDataError(
            "EODHD_TOKEN nicht gefunden (weder Secret Manager noch .env)")
    return token


def fetch_eodhd_eod_series(symbol, calendar_days=600):
    """Taegliche Schlusskurse von EODHD als [(datum_iso, close)] aufsteigend.

    Nutzt `adjusted_close`; die verwendeten Tracker sind thesaurierend, die
    Reihe ist also Total Return per Konstruktion.
    """
    start = (datetime.date.today()
             - datetime.timedelta(days=calendar_days)).isoformat()
    response = requests.get(
        f"{EODHD_BASE_URL}/eod/{symbol}",
        params={"api_token": _eodhd_token(), "fmt": "json",
                "period": "d", "from": start},
        timeout=30)

    if response.status_code != 200:
        raise EodhdDataError(
            f"EOD-Abruf fuer {symbol}: HTTP {response.status_code}")
    try:
        rows = response.json()
    except ValueError:
        raise EodhdDataError(f"EOD-Abruf fuer {symbol}: Antwort ist kein JSON")
    if not isinstance(rows, list) or not rows:
        raise EodhdDataError(f"EOD-Abruf fuer {symbol}: keine Bars geliefert")

    series = []
    for row in rows:
        close = row.get("adjusted_close", row.get("close"))
        if close in (None, "NA") or not row.get("date"):
            continue
        series.append((row["date"], float(close)))
    if not series:
        raise EodhdDataError(
            f"EOD-Abruf fuer {symbol}: keine verwertbaren Schlusskurse")
    return sorted(series)


def fetch_eodhd_realtime(symbol):
    """Aktueller Kurs von EODHD als (close, zeitstempel_utc)."""
    response = requests.get(
        f"{EODHD_BASE_URL}/real-time/{symbol}",
        params={"api_token": _eodhd_token(), "fmt": "json"},
        timeout=20)

    if response.status_code != 200:
        raise EodhdDataError(
            f"Live-Abruf fuer {symbol}: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        raise EodhdDataError(f"Live-Abruf fuer {symbol}: Antwort ist kein JSON")

    close, timestamp = data.get("close"), data.get("timestamp")
    if close in (None, "NA") or timestamp in (None, "NA"):
        raise EodhdDataError(
            f"Live-Abruf fuer {symbol}: EODHD meldet 'NA' - Ticker unbekannt "
            f"oder umbenannt?")
    return float(close), datetime.datetime.utcfromtimestamp(int(timestamp))


def get_eodhd_sma_state(index_symbol, sma_period, env="live"):
    """Letzter bekannter Crossing-Zustand eines EODHD-Index, oder None.

    Bewusst NICHT get_index_sma_state(): das legt den Zustand als Feld im
    Alpaca-Preis-Cache (`market-data-{env}`) ab und schreibt gar nicht, wenn
    das Dokument fehlt. Fuer EODHD-Symbole legt update_market_data() nie eines
    an - der Zustand wuerde also nie persistieren, previous_state waere immer
    None und es gaebe nie einen Crossover-Alert.
    """
    try:
        doc = (get_firestore_client()
               .collection(f"index-alert-state-{env}")
               .document(normalize_symbol(index_symbol))
               .get())
        if not doc.exists:
            return None
        return doc.to_dict().get(f"sma{sma_period}_state")
    except Exception as e:
        print(f"Warning: could not load EODHD SMA state for {index_symbol}: {e}")
        return None


def save_eodhd_sma_state(index_symbol, sma_period, state, price, sma_value,
                         env="live"):
    """Crossing-Zustand schreiben. Legt das Dokument an, falls noetig.

    Faengt bewusst keine Exception ab: schlaegt das Schreiben fehl, sieht der
    naechste Lauf noch den alten Zustand und wuerde denselben Alert erneut
    senden. Das soll auffallen, nicht in einem Log versickern.
    """
    (get_firestore_client()
     .collection(f"index-alert-state-{env}")
     .document(normalize_symbol(index_symbol))
     .set({f"sma{sma_period}_state": state,
           f"sma{sma_period}_value": sma_value,
           "price": price,
           "timestamp": datetime.datetime.utcnow()}, merge=True))


def _handle_eodhd_sma_crossing(index_symbol, index_name, sma_period,
                               noise_threshold, currency_symbol, run_role, env,
                               public_chat_secret=None):
    """SMA-Crossing-Alert auf einem EUR-notierten XETRA-Tracker.

    Drei Rollen statt der US-Handelszeiten-Logik des Alpaca-Pfads:
      advisory  - Vorwarnung 15:00-17:00, schreibt bewusst KEINEN State
      decisive  - Handelsalarm 17:20, schreibt den State
      reconcile - Abgleich 21:00 nach EODHD-Publikation auf den echten Schluss, korrigiert den State
    """
    today_iso = _heute_iso()
    hist = fetch_eodhd_eod_series(index_symbol)

    if run_role == "reconcile":
        if not hist or hist[-1][0] != today_iso:
            neuester = hist[-1][0] if hist else "keiner"
            _stale_data(index_name, run_role,
                        f"kein Schlusskurs fuer {today_iso} (neuester: "
                        f"{neuester})", today_iso)
            return jsonify({"message": f"{index_name}: kein XETRA-Handelstag",
                            "status": "no_trading_day",
                            "run_role": run_role}), 200
        series = _eodhd_close_series(hist, sma_period, today_iso)
        current_price = series[-1]
        price_source = f"XETRA-Schluss {today_iso}"
    elif run_role in ("advisory", "decisive"):
        current_price, live_ts = fetch_eodhd_realtime(index_symbol)
        if live_ts.date().isoformat() != today_iso:
            _stale_data(index_name, run_role,
                        f"Live-Kurs stammt vom {live_ts.date()}, nicht von "
                        f"heute", today_iso)
            return jsonify({"message": f"{index_name}: kein XETRA-Handelstag",
                            "status": "no_trading_day",
                            "run_role": run_role}), 200
        series = _eodhd_sma_series(hist, current_price, sma_period, today_iso)
        price_source = f"live {live_ts:%H:%M} UTC"
    else:
        raise EodhdDataError(
            f"Unbekannte run_role '{run_role}' - erlaubt: advisory, decisive, "
            f"reconcile")

    sma_value = sum(series) / len(series)
    diff_percent = (current_price / sma_value - 1) * 100
    current_state = _sma_state_from_diff(diff_percent, noise_threshold)
    previous_state = get_eodhd_sma_state(index_symbol, sma_period, env=env)
    trigger = sma_value * (1 - noise_threshold / 100)

    def body(headline):
        return (f"{headline}\n"
                f"Kurs: {current_price:.2f} {currency_symbol} ({price_source})\n"
                f"SMA{sma_period}: {sma_value:.2f} {currency_symbol} "
                f"({diff_percent:+.2f} %)\n"
                f"Ausstiegslinie (SMA -{noise_threshold:.1f} %): "
                f"{trigger:.2f} {currency_symbol} "
                f"({(trigger / current_price - 1) * 100:+.2f} % vom Kurs)")

    message, status = None, f"{current_state}_no_change"

    if run_role == "advisory":
        # Meldet nur, wenn es eng wird oder das Vorzeichen gegen den letzten
        # Tagesschluss kippt. Schreibt bewusst keinen State: ein am Band
        # zitternder Intraday-Kurs wuerde sonst Whipsaw-Alerts erzeugen, die
        # der Backtest nie hatte - der schaut nur auf Schlusskurse.
        if current_state == "neutral" or (previous_state
                                          and current_state != previous_state):
            message = body(f"⚠️ {index_name} Vorwarnung (SMA{sma_period}) - "
                           f"Handelsschluss XETRA 17:30 CET")
            status = "advisory"

    elif run_role == "decisive":
        if previous_state is None:
            message = body(f"🆕 {index_name}: Alert initialisiert "
                           f"(SMA{sma_period}) - Zustand {current_state.upper()}")
            status = "initialised"
        elif previous_state != current_state:
            emoji = {"above": "🚀", "below": "📉", "neutral": "📊"}[current_state]
            message = body(f"{emoji} {index_name}: SMA{sma_period} "
                           f"{previous_state.upper()} -> {current_state.upper()} "
                           f"- noch bis 17:30 CET handelbar")
            status = f"crossover_{current_state}"
        save_eodhd_sma_state(index_symbol, sma_period, current_state,
                             current_price, sma_value, env=env)

    else:  # reconcile
        if previous_state != current_state:
            message = body(f"🔁 {index_name}: Korrektur nach Schlusskurs "
                           f"(SMA{sma_period}) - {previous_state} -> {current_state}")
            status = "reconciled"
        save_eodhd_sma_state(index_symbol, sma_period, current_state,
                             current_price, sma_value, env=env)

    if message:
        send_telegram_message(message)

    # Zusaetzlicher oeffentlicher Kanal: bewusst NUR echte Crossings. Die
    # stuendlichen Vorwarnungen, die reconcile-Korrekturen und die
    # Datenfehler-Meldungen sind Betriebsrauschen - in einem Kanal mit
    # Aussenstehenden machen sie das Signal unlesbar und legen offen, wenn die
    # Infrastruktur klemmt. Ein Fehler beim oeffentlichen Versand darf den
    # Hauptalert und das State-Schreiben nie kippen, deshalb gekapselt; er
    # wird im privaten Kanal gemeldet, statt still zu verschwinden.
    if public_chat_secret and message and status.startswith("crossover_"):
        try:
            if not get_secret_or_env(public_chat_secret):
                raise EodhdDataError(
                    f"Secret {public_chat_secret} ist leer oder fehlt")
            send_telegram_message(message, chat_id_secret=public_chat_secret)
        except Exception as e:
            send_telegram_message(
                f"❗ {index_name}: Crossing-Alert ging NICHT in den "
                f"oeffentlichen Kanal ({public_chat_secret}): {e}\n"
                f"Der Alert oben ist gueltig, nur die Weiterleitung fehlt.")

    return jsonify({
        "message": message or f"{index_name} ist {current_state} SMA{sma_period}",
        "status": status,
        "run_role": run_role,
        "current_price": current_price,
        "sma_value": sma_value,
        "price_diff_percent": diff_percent,
        "trigger_price": trigger,
        "previous_state": previous_state,
        "current_state": current_state,
    }), 200


def check_unified_index_alert(request, env=None):
    """
    Unified index alert function that can handle multiple indices and alert types.
    
    Args:
        request: Flask request object
        env: Environment ("live" or "paper") - if None, defaults to alpaca_environment or "live"
    """
    # Handle case where Content-Type is not set to application/json (e.g., application/octet-stream)
    if request.content_type == "application/json":
        request_json = request.get_json(silent=True)
    else:
        # If the Content-Type is octet-stream or undefined, attempt to decode the body manually
        try:
            request_json = json.loads(request.data.decode("utf-8"))
        except Exception:
            return jsonify({"error": "Failed to parse request body"}), 400

    # Check if the required parameters are present
    if not request_json:
        return jsonify({"error": "No request body provided"}), 400
    
    # Extract parameters with defaults
    index_symbol = request_json.get("index_symbol")
    index_name = request_json.get("index_name", index_symbol)
    alert_type = request_json.get("alert_type", "ath_drop")  # "ath_drop", "sma_crossing"
    sma_period = request_json.get("sma_period", 200)  # Default to 200-day SMA
    threshold_percent = request_json.get("threshold_percent", 30.0)  # For ATH drops
    noise_threshold = request_json.get("noise_threshold", 1.0)  # For SMA crossings
    source = request_json.get("source", "alpaca")             # "alpaca" | "eodhd"
    currency_symbol = request_json.get("currency_symbol", "$")
    run_role = request_json.get("run_role", "decisive")        # nur bei source="eodhd"
    public_chat_secret = request_json.get("public_chat_secret")  # zweiter Kanal
    
    # Determine environment: from parameter, request JSON, or default to alpaca_environment
    if env is None:
        env = request_json.get("env", alpaca_environment if 'alpaca_environment' in globals() else "live")
    
    if not index_symbol:
        return jsonify({"error": "Missing required parameter: index_symbol"}), 400

    try:
        if alert_type == "ath_drop":
            # Handle all-time high drop alerts
            current_price, all_time_high = get_index_data(index_symbol)
            drop_percentage = ((all_time_high - current_price) / all_time_high) * 100
            
            if drop_percentage >= threshold_percent:
                message = f"Alert: {index_name} has dropped {drop_percentage:.2f}% from its ATH! Consider a loan with a duration of 6 to 8 years (50k to 100k) at around 4.5% interest max"
                send_telegram_message(message)
                return jsonify({"message": message, "status": "ath_drop_alert", "drop_percentage": drop_percentage}), 200
            else:
                return jsonify({
                    "message": f"{index_name} is within safe range ({drop_percentage:.2f}% below ATH)",
                    "status": "within_range",
                    "drop_percentage": drop_percentage
                }), 200
                
        elif alert_type == "sma_crossing":
            if source == "eodhd":
                return _handle_eodhd_sma_crossing(
                    index_symbol, index_name, sma_period, noise_threshold,
                    currency_symbol, run_role, env,
                    public_chat_secret=public_chat_secret)

            # ── Alpaca-Pfad, unveraendert ──────────────────────────────────
            # Handle SMA crossing alerts with crossover detection
            # Get all market data at once for efficiency
            market_data = get_all_market_data(index_symbol, env=env)
            if market_data is None:
                market_data = update_market_data(index_symbol, env=env)
            
            current_price = market_data["price"]
            
            # Get appropriate SMA based on period
            if sma_period == 255:
                sma_value = market_data["sma255"]
            elif sma_period == 200:
                sma_value = market_data["sma200"]
            else:
                # For any other period, calculate dynamically using Alpaca
                api = set_alpaca_environment(env=env)
                
                # Fetch enough data for custom SMA period (add 50% buffer)
                days_needed = int(sma_period * 1.5 * 1.4)  # trading days to calendar days with buffer
                closes = get_alpaca_historical_bars(api, index_symbol, days=days_needed)
                
                if closes and len(closes) >= sma_period:
                    df = pd.DataFrame({'close': closes})
                    sma_value = df['close'].rolling(window=sma_period).mean().iloc[-1]
                else:
                    raise ValueError(f"Insufficient Alpaca data for {index_symbol} {sma_period}-day SMA. Got {len(closes) if closes else 0} bars, need {sma_period}.")
            
            # Calculate percentage difference from SMA
            price_diff_percent = ((current_price - sma_value) / sma_value) * 100
            
            # Load previous state from Firestore
            previous_state_data = get_index_sma_state(index_symbol, sma_period, env=env)
            previous_state = previous_state_data.get("state") if previous_state_data else None
            
            # Determine current state based on noise threshold
            if price_diff_percent > noise_threshold:
                current_state = "above"
            elif price_diff_percent < -noise_threshold:
                current_state = "below"
            else:
                current_state = "neutral"
            
            # Check if we're in the last trading hour
            in_last_hour = is_last_trading_hour()
            already_sent_last_hour = was_last_hour_alert_sent_today(index_symbol, sma_period, env=env)
            
            # Initialize response variables
            message = None
            status = None
            alert_sent = False
            
            # Check for state change (crossover)
            if previous_state and previous_state != current_state:
                # State changed - send crossover alert
                if current_state == "above":
                    emoji = "🚀" if price_diff_percent > 2.0 else "📈"
                    urgency = " ⚡🔔 LAST HOUR" if in_last_hour else ""
                    message = f"{emoji} {index_name} Alert: Crossed ABOVE its {sma_period}-day SMA!{urgency}\nCurrent: ${current_price:.2f} (SMA: ${sma_value:.2f}, +{price_diff_percent:.2f}%)"
                    status = "crossover_above"
                    alert_sent = True
                    
                elif current_state == "below":
                    emoji = "📉" if price_diff_percent < -2.0 else "📊"
                    urgency = " ⚡🔔 LAST HOUR" if in_last_hour else ""
                    message = f"{emoji} {index_name} Alert: Crossed BELOW its {sma_period}-day SMA!{urgency}\nCurrent: ${current_price:.2f} (SMA: ${sma_value:.2f}, {price_diff_percent:.2f}%)"
                    status = "crossover_below"
                    alert_sent = True
                    
                elif current_state == "neutral":
                    # Moved into neutral zone from above or below
                    message = f"📊 {index_name}: Entered neutral zone (within {noise_threshold}% of {sma_period}-day SMA)\nCurrent: ${current_price:.2f} (SMA: ${sma_value:.2f}, {price_diff_percent:+.2f}%)"
                    status = "neutral_zone"
                    alert_sent = True
                
                # Send the crossover alert
                if message:
                    send_telegram_message(message)
                    # If sent during last hour, mark it
                    if in_last_hour:
                        mark_last_hour_alert_sent(index_symbol, sma_period, env=env)
            
            # Check for last hour confirmation (only if no crossover alert was sent)
            elif in_last_hour and not already_sent_last_hour and current_state != "neutral":
                # Send urgent confirmation alert during last trading hour
                if current_state == "above":
                    message = f"⚡🔔 {index_name} FINAL HOUR CONFIRMATION:\nStill ABOVE {sma_period}-day SMA\nCurrent: ${current_price:.2f} (SMA: ${sma_value:.2f}, +{price_diff_percent:.2f}%)\n\n✅ Signal: Buy/Hold position"
                    status = "last_hour_above"
                    alert_sent = True
                elif current_state == "below":
                    message = f"⚡🔔 {index_name} FINAL HOUR CONFIRMATION:\nStill BELOW {sma_period}-day SMA\nCurrent: ${current_price:.2f} (SMA: ${sma_value:.2f}, {price_diff_percent:.2f}%)\n\n❌ Signal: Avoid/Sell position"
                    status = "last_hour_below"
                    alert_sent = True
                
                # Send the last hour confirmation
                if message:
                    send_telegram_message(message)
                    mark_last_hour_alert_sent(index_symbol, sma_period, env=env)
            
            # Save current state to Firestore (always update)
            save_index_sma_state(index_symbol, sma_period, current_state, current_price, sma_value, env=env)
            
            # Return appropriate response
            if alert_sent:
                return jsonify({
                    "message": message,
                    "status": status,
                    "price_diff_percent": price_diff_percent,
                    "current_price": current_price,
                    "sma_value": sma_value,
                    "previous_state": previous_state,
                    "current_state": current_state
                }), 200
            else:
                # No alert sent - state unchanged
                return jsonify({
                    "message": f"{index_name} is {current_state} {sma_period}-day SMA (no state change, no alert sent)",
                    "status": f"{current_state}_no_change",
                    "price_diff_percent": price_diff_percent,
                    "current_price": current_price,
                    "sma_value": sma_value,
                    "previous_state": previous_state,
                    "current_state": current_state
                }), 200
        else:
            return jsonify({"error": f"Invalid alert_type: {alert_type}. Must be 'ath_drop' or 'sma_crossing'"}), 400
                
    except EodhdDataError as e:
        error_message = f"❗ {index_name} ({index_symbol}, {run_role}): {e}"
        print(error_message)
        send_telegram_message(error_message)
        return jsonify({"error": str(e), "status": "data_error"}), 500
    except Exception as e:
        error_message = f"Error checking {index_name} alert: {str(e)}"
        print(error_message)
        send_telegram_message(error_message)
        return jsonify({"error": error_message}), 500


def get_dual_momentum_position_value(api):
    """
    Get current value and position details for dual momentum strategy.
    
    Args:
        api: Alpaca API credentials dict
    
    Returns:
        dict: {
            "total_value": float,
            "current_position": str,
            "shares_held": float,
            "position_value": float
        }
    """
    try:
        # Get positions using the list_positions function
        positions = list_positions(api)
        dual_momentum_symbols = STRATEGY_SYMBOLS["dual_momentum"]
        defensive = dual_momentum_config["defensive"]

        total_value = 0
        by_symbol = {}
        primary_position = None
        primary_value = 0.0
        primary_shares = 0.0

        for position in positions:
            ticker = position.get("symbol")
            if ticker in dual_momentum_symbols:
                position_value = float(position.get("market_value", 0))
                qty = float(position.get("qty", 0))
                total_value += position_value
                by_symbol[ticker] = {"value": position_value, "shares": qty}
                # "Primary" is the largest non-defensive holding (the momentum winner).
                if ticker != defensive and position_value > primary_value:
                    primary_position = ticker
                    primary_value = position_value
                    primary_shares = qty

        return {
            "total_value": total_value,
            "current_position": primary_position,    # winner ETF if any, else None
            "shares_held": primary_shares,
            "position_value": total_value,
            "by_symbol": by_symbol,
        }
    except Exception as e:
        print(f"Error getting dual momentum position value: {e}")
        return {
            "total_value": 0,
            "current_position": None,
            "shares_held": 0,
            "position_value": 0
        }



def get_all_strategy_values(api):
    """
    Get current market value of all strategies from Alpaca positions.
    Aggregates values from all strategy-specific functions into a single dict.
    
    This is used for contribution rebalancing to determine how far each strategy
    is from its target allocation percentage.
    
    Args:
        api: Alpaca API credentials
    
    Returns:
        dict: {
            "hfea": float,
            "spxl_sma": float,
            "dual_momentum": float,
            "aaa": float,
            "total": float
        }
    """
    try:
        # Get all positions once to minimize API calls
        positions = {p["symbol"]: float(p["market_value"]) for p in list_positions(api)}

        # HFEA: UPRO, TMF, KMLM
        hfea_value = (
            positions.get("UPRO", 0) +
            positions.get("TMF", 0) +
            positions.get("KMLM", 0)
        )

        # SPXL SMA: SPXL, SGOV (holding fund)
        spxl_sma_value = (
            positions.get("SPXL", 0) +
            positions.get(spxl_sma_holding_fund, 0)
        )
        
        # Dual Momentum: SPUU, QLD, EFO, BND (BND shared as defensive)
        dual_momentum_value = (
            positions.get("SPUU", 0) +
            positions.get("QLD", 0) +
            positions.get("EFO", 0) +
            positions.get("BND", 0)
        )
        
        # 7-Asset Rotator: sum of all 7-asset universe + SHV defensive
        aaa_value = sum(positions.get(sym, 0) for sym in STRATEGY_SYMBOLS["aaa"])

        total_value = (
            hfea_value +
            spxl_sma_value +
            dual_momentum_value +
            aaa_value
        )

        return {
            "hfea": hfea_value,
            "spxl_sma": spxl_sma_value,
            "dual_momentum": dual_momentum_value,
            "aaa": aaa_value,
            "total": total_value
        }

    except Exception as e:
        print(f"Error getting all strategy values: {e}")
        return {
            "hfea": 0,
            "spxl_sma": 0,
            "dual_momentum": 0,
            "aaa": 0,
            "total": 0
        }


def calculate_rebalanced_allocations(api, aggressiveness=None):
    """
    Calculate contribution allocations that tilt toward underweight strategies.
    
    The algorithm:
    1. Get current portfolio value for each strategy
    2. Calculate current % vs target % for each strategy
    3. For underweight strategies, calculate how much they need to catch up
    4. Apply aggressiveness multiplier to tilt contributions toward underweight
    5. Normalize and apply max_single_strategy_pct cap
    
    Args:
        api: Alpaca API credentials
        aggressiveness: Override for rebalance_config["aggressiveness"]
                       0.0 = disabled (use fixed %), 1.0 = proportional tilt, 2.0+ = aggressive
    
    Returns:
        dict: {
            "current_values": {strategy: value},
            "current_percentages": {strategy: pct},
            "target_percentages": {strategy: pct},
            "deviations": {strategy: current - target},
            "adjusted_allocations": {strategy_allo_key: new_pct}
        }
    """
    if aggressiveness is None:
        aggressiveness = rebalance_config["aggressiveness"]
    
    max_single_pct = rebalance_config["max_single_strategy_pct"]
    
    # Map from strategy name to allocation key in strategy_allocations
    strategy_to_allo_key = {
        "hfea": "hfea_allo",
        "spxl_sma": "spxl_allo",

        "dual_momentum": "dual_momentum_allo",

        "aaa": "aaa_allo",
    }
    
    # Get target percentages from strategy_allocations
    target_percentages = {
        strategy: strategy_allocations[allo_key]
        for strategy, allo_key in strategy_to_allo_key.items()
    }
    
    # Get current values for all strategies
    strategy_values = get_all_strategy_values(api)
    total_value = strategy_values["total"]
    
    # Calculate current percentages
    current_percentages = {}
    for strategy in strategy_to_allo_key.keys():
        if total_value > 0:
            current_percentages[strategy] = strategy_values[strategy] / total_value
        else:
            current_percentages[strategy] = 0
    
    # Calculate deviations (negative = underweight, positive = overweight)
    deviations = {
        strategy: current_percentages[strategy] - target_percentages[strategy]
        for strategy in strategy_to_allo_key.keys()
    }
    
    # If aggressiveness is 0, just return fixed allocations
    if aggressiveness == 0:
        adjusted_allocations = {
            allo_key: strategy_allocations[allo_key]
            for allo_key in strategy_allocations.keys()
        }
        return {
            "current_values": {s: strategy_values[s] for s in strategy_to_allo_key.keys()},
            "current_percentages": current_percentages,
            "target_percentages": target_percentages,
            "deviations": deviations,
            "adjusted_allocations": adjusted_allocations,
            "total_portfolio_value": total_value
        }
    
    # Calculate underweight amounts (only consider underweight strategies)
    # Underweight = how much below target the strategy is
    underweight_amounts = {}
    for strategy in strategy_to_allo_key.keys():
        if deviations[strategy] < 0:
            # Strategy is underweight - needs more allocation
            underweight_amounts[strategy] = abs(deviations[strategy])
        else:
            # Strategy is at or above target - gets baseline allocation only
            underweight_amounts[strategy] = 0
    
    # Apply aggressiveness multiplier to underweight amounts
    # Higher aggressiveness = more concentration in underweight strategies
    weighted_underweight = {
        strategy: (underweight_amounts[strategy] ** aggressiveness) if underweight_amounts[strategy] > 0 else 0
        for strategy in strategy_to_allo_key.keys()
    }
    
    # Calculate adjusted allocations
    # Base allocation + proportional share of underweight adjustment
    total_weighted_underweight = sum(weighted_underweight.values())
    
    adjusted_allocations_raw = {}
    for strategy, allo_key in strategy_to_allo_key.items():
        base_allocation = target_percentages[strategy]
        
        if total_weighted_underweight > 0 and weighted_underweight[strategy] > 0:
            # Underweight strategies get extra allocation proportional to their underweight
            # The more underweight, the more extra allocation they get
            underweight_share = weighted_underweight[strategy] / total_weighted_underweight
            
            # Calculate how much to shift from overweight to underweight
            # We shift proportionally based on how much each overweight strategy exceeds target
            overweight_total = sum(max(0, dev) for dev in deviations.values())
            
            if overweight_total > 0:
                # Reduce overweight strategies and add to underweight
                extra_allocation = overweight_total * underweight_share * aggressiveness
                adjusted_allocations_raw[allo_key] = base_allocation + extra_allocation
            else:
                # No overweight strategies, just use underweight-proportional allocation
                adjusted_allocations_raw[allo_key] = underweight_share
        elif total_weighted_underweight > 0:
            # Overweight strategy - reduce allocation proportionally
            overweight_amount = max(0, deviations[strategy])
            overweight_total = sum(max(0, dev) for dev in deviations.values())
            
            if overweight_total > 0:
                reduction = (overweight_amount / overweight_total) * overweight_total * aggressiveness
                adjusted_allocations_raw[allo_key] = max(0, base_allocation - reduction)
            else:
                adjusted_allocations_raw[allo_key] = base_allocation
        else:
            # Portfolio is perfectly balanced, use target allocations
            adjusted_allocations_raw[allo_key] = base_allocation
    
    # Normalize to ensure allocations sum to 1.0
    total_raw = sum(adjusted_allocations_raw.values())
    if total_raw > 0:
        adjusted_allocations_normalized = {
            key: val / total_raw
            for key, val in adjusted_allocations_raw.items()
        }
    else:
        # Fallback to target allocations
        adjusted_allocations_normalized = {
            allo_key: strategy_allocations[allo_key]
            for allo_key in strategy_allocations.keys()
        }
    
    # Apply max_single_strategy_pct cap and redistribute excess
    adjusted_allocations = adjusted_allocations_normalized.copy()
    iterations = 0
    max_iterations = 10

    while iterations < max_iterations:
        excess = 0
        strategies_at_cap = []
        strategies_below_cap = []

        for key, val in adjusted_allocations.items():
            if val > max_single_pct:
                excess += val - max_single_pct
                adjusted_allocations[key] = max_single_pct
                strategies_at_cap.append(key)
            else:
                strategies_below_cap.append(key)

        if excess == 0:
            break

        # Redistribute excess to strategies below cap
        if strategies_below_cap:
            redistribution_per_strategy = excess / len(strategies_below_cap)
            for key in strategies_below_cap:
                adjusted_allocations[key] += redistribution_per_strategy

        iterations += 1

    # Enforce a per-strategy floor so aggressive tilting can never starve a
    # small target allocation entirely. The floor is a fraction of each
    # strategy's *target* allocation. Any shortfall is taken proportionally
    # from strategies that are above their floor.
    floor_fraction = rebalance_config.get("min_floor_pct_of_target", 0.0)
    if floor_fraction > 0:
        floors = {
            allo_key: strategy_allocations[allo_key] * floor_fraction
            for allo_key in adjusted_allocations.keys()
        }
        shortfall = 0.0
        for key, val in adjusted_allocations.items():
            if val < floors[key]:
                shortfall += floors[key] - val
                adjusted_allocations[key] = floors[key]
        if shortfall > 0:
            donors = {k: v for k, v in adjusted_allocations.items() if v > floors[k]}
            donor_excess = sum(v - floors[k] for k, v in donors.items())
            if donor_excess > 0:
                for k in donors:
                    take = (donors[k] - floors[k]) / donor_excess * shortfall
                    adjusted_allocations[k] -= take

    # Final normalization to handle any floating point drift
    total_final = sum(adjusted_allocations.values())
    if abs(total_final - 1.0) > 0.001:
        adjusted_allocations = {
            key: val / total_final
            for key, val in adjusted_allocations.items()
        }
    
    return {
        "current_values": {s: strategy_values[s] for s in strategy_to_allo_key.keys()},
        "current_percentages": current_percentages,
        "target_percentages": target_percentages,
        "deviations": deviations,
        "adjusted_allocations": adjusted_allocations,
        "total_portfolio_value": total_value
    }


def print_allocation_dashboard(rebalance_result, contribution_amount=None):
    """
    Print a dashboard showing current vs target allocations before monthly investments.
    
    Displays:
    - Current value and percentage for each strategy
    - Target percentage
    - Deviation from target
    - Adjusted allocation for this month's contribution
    - Dollar amounts if contribution_amount is provided
    
    Args:
        rebalance_result: Output from calculate_rebalanced_allocations()
        contribution_amount: Optional total contribution amount to show dollar allocations
    """
    # Strategy display names for prettier output
    strategy_display_names = {
        "hfea": "HFEA",
        "spxl_sma": "SPXL SMA",

        "dual_momentum": "Dual Momentum",

        "aaa": "7-Asset Rotator",
    }
    
    current_values = rebalance_result["current_values"]
    current_pcts = rebalance_result["current_percentages"]
    target_pcts = rebalance_result["target_percentages"]
    deviations = rebalance_result["deviations"]
    adjusted_allos = rebalance_result["adjusted_allocations"]
    total_value = rebalance_result["total_portfolio_value"]
    
    print("\n" + "=" * 80)
    print("                    PORTFOLIO ALLOCATION DASHBOARD")
    print("=" * 80)
    
    # Header row
    if contribution_amount:
        print(f"{'Strategy':<20} {'Value':>10} {'Current':>9} {'Target':>9} {'Dev':>8} {'Adj Allo':>9} {'$ Allo':>10}")
        print("-" * 80)
    else:
        print(f"{'Strategy':<20} {'Value':>10} {'Current':>9} {'Target':>9} {'Dev':>8} {'Adj Allo':>9}")
        print("-" * 75)
    
    # Sort strategies by deviation (most underweight first)
    sorted_strategies = sorted(
        strategy_display_names.keys(),
        key=lambda s: deviations.get(s, 0)
    )
    
    for strategy in sorted_strategies:
        display_name = strategy_display_names[strategy]
        value = current_values.get(strategy, 0)
        current_pct = current_pcts.get(strategy, 0)
        target_pct = target_pcts.get(strategy, 0)
        deviation = deviations.get(strategy, 0)
        
        # Find the adjusted allocation for this strategy
        allo_key = f"{strategy}_allo" if strategy != "spxl_sma" else "spxl_allo"
        adjusted_pct = adjusted_allos.get(allo_key, target_pct)
        
        # Format deviation with sign
        dev_str = f"{deviation:+.1%}"
        
        if contribution_amount:
            dollar_allo = contribution_amount * adjusted_pct
            print(f"{display_name:<20} ${value:>9,.0f} {current_pct:>8.1%} {target_pct:>8.1%} {dev_str:>8} {adjusted_pct:>8.1%} ${dollar_allo:>9,.0f}")
        else:
            print(f"{display_name:<20} ${value:>9,.0f} {current_pct:>8.1%} {target_pct:>8.1%} {dev_str:>8} {adjusted_pct:>8.1%}")
    
    # Footer
    print("-" * (80 if contribution_amount else 75))
    print(f"{'TOTAL':<20} ${total_value:>9,.0f}")
    
    if contribution_amount:
        print(f"\nMonthly Contribution: ${contribution_amount:,.2f}")
    
    # Show aggressiveness setting
    aggressiveness = rebalance_config["aggressiveness"]
    if aggressiveness == 0:
        print(f"Rebalancing: DISABLED (using fixed allocations)")
    else:
        print(f"Rebalancing: ENABLED (aggressiveness={aggressiveness})")
    
    print("=" * (80 if contribution_amount else 75) + "\n")


# ════════════════════════════════════════════════════════════════════
# DUAL MOMENTUM (SPUU/QLD/EFO + BND) — best-of-3 with DD-stop + vol-target
# Backtest: 17.2% CAGR / 0.65 Sharpe / -34% MaxDD (24y, ≤2× leverage).
# ════════════════════════════════════════════════════════════════════


# Calendar-to-trading-day ratio used to convert the backtest's calendar-day
# lookbacks to trading-day bar indices. 1.45 ≈ 365/252.
_DM_CAL_TO_TRADING = 1 / 1.45


def _dm_blended_momentum_score(api, signal_symbol, cfg):
    """
    Blended skip-1m momentum score for a signal symbol (e.g. SPY/QQQ/EFA).

    Score = Σ weight_k × (P_now / P_past_k - 1) where:
      • P_now    = close `skip_days` calendar days ago (skip-most-recent-month).
      • P_past_k = close `lookback_k` trading days ago (6m=126, 12m=252).
    Returns None if data is insufficient.
    """
    lookbacks = cfg["lookbacks"]
    weights = cfg["lookback_weights"]
    # Convert skip_days from calendar to trading days to index a trading-day bar list.
    skip_idx = max(1, int(round(cfg["skip_days"] * _DM_CAL_TO_TRADING)))
    max_lookback = max(lookbacks.values())
    needed_days = skip_idx + max_lookback + 50  # buffer for non-trading days
    try:
        bars = get_alpaca_historical_bars(api, signal_symbol, days=max(400, needed_days + 100))
    except Exception as e:
        print(f"DM: error fetching bars for {signal_symbol}: {e}")
        return None

    if len(bars) < skip_idx + max_lookback + 1:
        print(f"DM: insufficient bars for {signal_symbol} ({len(bars)} < {skip_idx + max_lookback + 1})")
        return None

    price_now = bars[-(skip_idx + 1)]
    if price_now <= 0:
        return None
    score = 0.0
    for label, lookback in lookbacks.items():
        price_past = bars[-(skip_idx + lookback + 1)]
        if price_past <= 0:
            return None
        score += weights[label] * (price_now / price_past - 1)
    return score


def _dm_realized_vol(api, symbol, window=60):
    """60-day annualized realized vol from close-to-close simple returns."""
    try:
        bars = get_alpaca_historical_bars(api, symbol, days=max(120, window + 60))
    except Exception as e:
        print(f"DM: error fetching bars for {symbol} vol: {e}")
        return None
    if len(bars) < window + 1:
        print(f"DM: insufficient bars for {symbol} vol ({len(bars)} < {window + 1})")
        return None
    rets = [(bars[i + 1] / bars[i]) - 1 for i in range(len(bars) - window - 1, len(bars) - 1) if bars[i] > 0]
    if len(rets) < window // 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return (var ** 0.5) * (252 ** 0.5)


def _dm_pick_target(api, cfg):
    """Score every candidate and pick the winner. Returns (pos|None, scores, defensive)."""
    defensive = cfg["defensive"]
    scores = {}
    for signal_sym, pos_sym in cfg["candidates"]:
        score = _dm_blended_momentum_score(api, signal_sym, cfg)
        if score is not None:
            scores[pos_sym] = score
    if not scores:
        return None, scores, defensive
    best_pos, best_score = max(scores.items(), key=lambda kv: kv[1])
    if best_score < cfg["min_score"]:
        return None, scores, defensive
    return best_pos, scores, defensive


def monthly_dual_momentum_strategy(api, force_execute=False, investment_calc=None,
                                    margin_result=None, skip_order_wait=False, env="live"):
    """
    Dual Momentum (best-of-3) — SPUU/QLD/EFO rotation with DD-stop + vol-target.

    Each month:
      1. Compute blended momentum score for SPY/QQQ/EFA (6m+12m, skip-1m).
      2. Pick the highest-scoring candidate; if score < 1%, hold defensive (BND).
      3. Apply trailing-peak-NAV DD-stop (30%): if strategy is 30% below peak,
         force defensive and reset peak.
      4. Scale the winner position by min(1, target_vol / 60d realized vol).
         Excess parks in BND. Target_vol = 25% annualized.
      5. Rebalance to (winner × scale, BND × (1-scale)).
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        print("Not first trading day of the month")
        return "Not first trading day of the month"

    if force_execute:
        print("Dual Momentum: Force execution enabled - bypassing trading day check")

    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)

    investment_amount = investment_calc["strategy_amounts"].get("dual_momentum_allo", 0.0)
    cfg = dual_momentum_config
    defensive = cfg["defensive"]
    candidate_positions = [pos for _, pos in cfg["candidates"]]
    all_symbols = candidate_positions + [defensive]
    check_date = datetime.datetime.now().strftime("%Y-%m-%d")

    balances = load_balances(env)
    state = balances.get("dual_momentum", {})
    total_invested = state.get("total_invested", 0)
    peak_nav = float(state.get("peak_nav", 0) or 0)

    # 1) Current positions and NAV
    value_data = get_dual_momentum_position_value(api)
    current_value = value_data["total_value"]
    by_symbol = value_data["by_symbol"]
    print(f"Dual Momentum — investment ${investment_amount:.2f}, current value ${current_value:.2f}")
    print(f"  by symbol: {by_symbol}")

    # 2) DD-stop check (skipped if we don't yet have a peak — first run seeds it).
    new_peak_nav = max(peak_nav, current_value) if peak_nav > 0 else current_value
    dd = (current_value - new_peak_nav) / new_peak_nav if new_peak_nav > 0 else 0.0
    dd_triggered = peak_nav > 0 and dd < -cfg["dd_threshold"]
    realized_vol = None
    if dd_triggered:
        print(f"  DD-stop TRIGGERED: drawdown {dd:.1%} < -{cfg['dd_threshold']:.0%}; forcing defensive")
        winner = None
        scores = {}
        new_peak_nav = current_value  # reset peak after stop
    else:
        winner, scores, _ = _dm_pick_target(api, cfg)
        print(f"  momentum scores: {scores}")
        print(f"  winner: {winner if winner else 'DEFENSIVE (no score > min)'}")

    # 3) Vol-target scale (only when we have a winner).
    if winner is not None:
        realized_vol = _dm_realized_vol(api, winner, window=cfg["vol_window"])
        if realized_vol is None or realized_vol <= 0:
            print(f"  realized vol unavailable for {winner}; defaulting scale=1.0")
            scale = 1.0
        else:
            scale = min(1.0, cfg["target_vol"] / realized_vol)
            print(f"  {winner} 60d vol: {realized_vol:.1%} -> scale {scale:.3f}")
    else:
        scale = 0.0

    # 4) Target dollar allocations across all 4 symbols.
    total_to_allocate = current_value + investment_amount
    targets = {sym: 0.0 for sym in all_symbols}
    if winner is None:
        targets[defensive] = total_to_allocate
    else:
        targets[winner] = scale * total_to_allocate
        targets[defensive] = (1.0 - scale) * total_to_allocate

    print(f"  target $: {{ {', '.join(f'{s}: ${v:,.2f}' for s, v in targets.items())} }}")

    # 5) Rebalance — compute deltas, sell first then buy.
    prices = {}
    for sym in all_symbols:
        try:
            prices[sym] = float(get_latest_trade(api, sym))
        except Exception as e:
            send_telegram_message(f"🔄 Dual Momentum\n❌ Failed to fetch price for {sym}: {e}")
            return f"Failed to fetch price for {sym}: {e}"

    current_dollars = {sym: by_symbol.get(sym, {}).get("value", 0.0) for sym in all_symbols}
    deltas = {sym: targets[sym] - current_dollars[sym] for sym in all_symbols}
    trades_info = []

    # Sells (negative delta) first to free cash for buys.
    for sym in all_symbols:
        if deltas[sym] >= -1.0:  # ignore < $1 deltas
            continue
        shares_have = by_symbol.get(sym, {}).get("shares", 0.0)
        sell_dollars = -deltas[sym]
        shares_to_sell = min(shares_have, sell_dollars / prices[sym])
        if shares_to_sell * prices[sym] < margin_control_config["min_investment"]:
            continue
        try:
            order = submit_order(api, sym, shares_to_sell, "sell")
            if not skip_order_wait:
                wait_for_order_fill(api, order["id"])
            trades_info.append(f"Sold {shares_to_sell:.4f} {sym} (${shares_to_sell * prices[sym]:.2f})")
            print(f"  sold {shares_to_sell:.4f} {sym} (${shares_to_sell * prices[sym]:.2f})")
        except Exception as e:
            send_telegram_message(f"🔄 Dual Momentum\n❌ Sell {sym} failed: {e}")
            return f"Failed to sell {sym}: {e}"

    # Buys (positive delta).
    for sym in all_symbols:
        if deltas[sym] <= margin_control_config["min_investment"]:
            continue
        buy_dollars = deltas[sym]
        shares_to_buy = buy_dollars / prices[sym]
        try:
            order = submit_order(api, sym, shares_to_buy, "buy")
            if not skip_order_wait:
                wait_for_order_fill(api, order["id"])
            trades_info.append(f"Bought {shares_to_buy:.4f} {sym} @ ${prices[sym]:.2f} (${buy_dollars:.2f})")
            print(f"  bought {shares_to_buy:.4f} {sym} @ ${prices[sym]:.2f} (${buy_dollars:.2f})")
        except Exception as e:
            send_telegram_message(f"🔄 Dual Momentum\n❌ Buy {sym} failed: {e}")
            return f"Failed to buy {sym}: {e}"

    if not trades_info:
        trades_info.append("No trades needed; targets already aligned.")

    # 6) Persist state.
    final_value_data = get_dual_momentum_position_value(api)
    final_by_symbol = final_value_data["by_symbol"]
    primary_position = winner if winner is not None else defensive
    primary_shares = final_by_symbol.get(primary_position, {}).get("shares", 0.0)
    defensive_shares = final_by_symbol.get(defensive, {}).get("shares", 0.0)
    final_total_invested = total_invested + investment_amount
    final_total_value = final_value_data["total_value"]
    final_peak_nav = max(new_peak_nav, final_total_value)
    strategy_return = (final_total_value / final_total_invested - 1) if final_total_invested > 0 else 0

    save_balance("dual_momentum", {
        "total_invested": final_total_invested,
        "primary_position": primary_position,
        "primary_shares": primary_shares,
        "primary_target_pct": scale if winner is not None else 0.0,
        "defensive_shares": defensive_shares,
        "defensive_target_pct": (1.0 - scale) if winner is not None else 1.0,
        "peak_nav": final_peak_nav,
        "last_momentum_check": {
            "scores": scores,
            "winner": winner,
            "dd_triggered": dd_triggered,
            "drawdown": dd,
            "realized_vol": realized_vol,
            "vol_scale": scale,
            "skip_days": cfg["skip_days"],
            "lookbacks": cfg["lookbacks"],
            "source": "monthly_dual_momentum",
        },
        "last_signal_check_date": check_date,
        "last_trade_date": check_date if any(("No trades" not in t) for t in trades_info) else state.get("last_trade_date"),
    }, env)

    # 7) Telegram summary
    scores_str = ", ".join(f"{s}: {sc:+.1%}" for s, sc in scores.items()) if scores else "n/a"
    msg = f"🔄 Dual Momentum (25.7%) — ${investment_amount:,.2f}\n\n"
    msg += f"Scores: {scores_str}\n"
    if dd_triggered:
        msg += f"⚠️ DD-stop triggered (DD {dd:.1%}) — defensive\n"
    elif winner is None:
        msg += "Signal: defensive (no candidate above +1%)\n"
    else:
        msg += f"Winner: {winner} (scale {scale:.0%}, vol {realized_vol:.0%})\n"
    msg += "\n"
    for t in trades_info:
        msg += f"{t}\n"
    msg += f"\nTotal invested: ${final_total_invested:,.2f}\n"
    msg += f"Current value: ${final_total_value:,.2f}\n"
    msg += f"Peak NAV: ${final_peak_nav:,.2f}\n"
    msg += f"Return: {strategy_return:+.1%}"
    send_telegram_message(msg)

    return f"Dual Momentum completed. Winner: {primary_position} ({scale:.0%}), return {strategy_return:.2%}"




def is_aggressive_rate_hiking_cycle():
    """Fed-policy filter: True if Fed Funds Target rose ≥50bp over last 90 days."""
    obs = get_fred_series("DFEDTARU", limit=120)
    if not obs:
        return False
    rates = []
    for o in obs:
        try:
            rates.append(float(o["value"]))
        except (ValueError, KeyError, TypeError):
            continue
    if len(rates) < 2:
        return False
    threshold_bps = regime_sso_config["fed_hike_threshold_bps"]
    older_idx = min(regime_sso_config["fed_hike_lookback_days"], len(rates) - 1)
    delta_pct = rates[0] - rates[older_idx]
    return (delta_pct * 100) >= threshold_bps






# Non-fractionable tickers on Alpaca — must be traded in whole shares.
# These are typically newer or smaller ETFs that Alpaca hasn't added to
# its fractional list yet. Submitting a fractional order errors with
# Alpaca code 40310000. The 7-Asset Rotator checks this set when sizing
# orders and floors to integer for these tickers.
NON_FRACTIONABLE_TICKERS = {
    "NTSD",   # WisdomTree US Plus Intl — may be non-fractionable, defensive listing
    # Add more here as discovered. To check: try a fractional order and watch
    # for `code: 40310000` in the error response.
}


def _size_buy_order(symbol: str, dollar_amount: float, price: float) -> tuple[float, str]:
    """Convert a dollar buy amount into shares, handling non-fractionable tickers.

    Returns (shares_to_buy, note). If the ticker is non-fractionable and the
    dollar amount is less than one share's price, returns (0, skip-reason).
    For fractionable tickers, returns the exact fractional share count.
    """
    if symbol in NON_FRACTIONABLE_TICKERS:
        whole = int(dollar_amount / price)  # floor
        if whole < 1:
            return 0.0, f"insufficient for 1 whole share (${dollar_amount:.2f} < ${price:.2f})"
        residual = dollar_amount - (whole * price)
        return float(whole), f"whole-share rounded ({whole} shares, ${residual:.2f} residual cash)"
    return dollar_amount / price, ""


def _size_sell_order(symbol: str, shares_to_sell: float) -> float:
    """Floor sells to integer shares for non-fractionable tickers."""
    if symbol in NON_FRACTIONABLE_TICKERS:
        return float(int(shares_to_sell))
    return shares_to_sell


def _retired_get_f4_position_value(api):
    """Get current F4 portfolio value and per-symbol breakdown."""
    try:
        positions = list_positions(api)
        f4_symbols = STRATEGY_SYMBOLS["f4"]
        by_symbol = {sym: {"value": 0.0, "shares": 0.0} for sym in f4_symbols}
        total_value = 0.0
        for position in positions:
            ticker = position.get("symbol")
            if ticker in f4_symbols:
                value = float(position.get("market_value", 0))
                shares = float(position.get("qty", 0))
                by_symbol[ticker] = {"value": value, "shares": shares}
                total_value += value
        return {"total_value": total_value, "by_symbol": by_symbol}
    except Exception as e:
        print(f"Error getting F4 position value: {e}")
        return {"total_value": 0.0, "by_symbol": {sym: {"value": 0.0, "shares": 0.0} for sym in STRATEGY_SYMBOLS["f4"]}}


def _retired_make_monthly_buys_f4(api, force_execute=False, investment_calc=None,
                                   margin_result=None, skip_order_wait=False, env="live"):
    """
    F4 monthly buy: deploy this month's allocation toward the most-underweight
    legs of WLDU/GOLY/TLT relative to the 40/30/30 target. Drift-correcting —
    full rebalance is handled separately by quarterly_rebalance_f4().
    """
    raise RuntimeError("F4 retired on 2026-09-09")
    if not force_execute and not check_trading_day(mode="monthly"):
        return "Not first trading day of the month"
    if force_execute:
        print("F4: Force execution enabled — bypassing trading day check")

    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)

    cfg = f4_config
    alloc_key = cfg["alloc_key"]
    name = cfg["display_name"]
    investment_amount = investment_calc["strategy_amounts"].get(alloc_key, 0)
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    buying_power = investment_calc["total_available"] + investment_calc["margin_approved"]
    pct_label = strategy_allocations.get(alloc_key, 0) * 100

    def _skip(reason):
        msg = f"🌐 {name} ({pct_label:.2f}%) — ${investment_amount:,.2f}\n⏭ {reason}"
        send_telegram_message(msg)
        print(reason)
        return reason

    if target_margin == 0 and leverage > 1.0:
        return _skip(f"Skipped — deleveraging required ({leverage:.2f}x)")
    if buying_power < investment_amount:
        return _skip(f"Skipped — insufficient buying power (${buying_power:,.2f})")
    if investment_amount < margin_control_config["min_investment"]:
        return _skip(f"Skipped — ${investment_amount:.2f} below $1.00 minimum")

    # Project leverage if the contribution adds leveraged exposure (WLDU is 2×, GOLY is 2× notional)
    if target_margin > 0:
        pv = metrics.get("portfolio_value", 0)
        equity = metrics.get("equity", 0)
        if pv > 0 and equity > 0:
            projected_leverage = (pv + investment_amount) / equity
            if projected_leverage >= margin_control_config["max_leverage"]:
                return _skip(f"Skipped — projected leverage {projected_leverage:.3f}x exceeds limit")

    value_data = get_f4_position_value(api)
    current_value = value_data["total_value"]
    by_symbol = value_data["by_symbol"]
    targets = cfg["targets"]

    # Total dollars target per leg after this contribution
    new_total = current_value + investment_amount
    target_dollars = {sym: new_total * w for sym, w in targets.items()}
    underweight = {sym: max(0.0, target_dollars[sym] - by_symbol[sym]["value"]) for sym in targets}
    total_under = sum(underweight.values())

    # If everything is at or above target (rare), fall back to proportional buys
    if total_under <= 0:
        buys = {sym: investment_amount * w for sym, w in targets.items()}
    else:
        # Tilt every dollar of contribution toward the underweight legs
        buys = {sym: investment_amount * (underweight[sym] / total_under) for sym in targets}

    # Execute buys
    trades_info = []
    prices = {}
    for sym in targets:
        if buys[sym] < cfg["tolerance_amount"]:
            continue
        try:
            prices[sym] = float(get_latest_trade(api, sym))
            shares, note = _size_buy_order(sym, buys[sym], prices[sym])
            if shares <= 0:
                trades_info.append(f"⏭ Skipped {sym}: {note}")
                print(f"  skipped {sym}: {note}")
                continue
            order = submit_order(api, sym, shares, "buy")
            if not skip_order_wait:
                wait_for_order_fill(api, order["id"])
            actual_dollars = shares * prices[sym]
            suffix = f" [{note}]" if note else ""
            trades_info.append(f"Bought {shares:.4f} {sym} @ ${prices[sym]:.2f} (${actual_dollars:.2f}){suffix}")
            print(f"  bought {shares:.4f} {sym} @ ${prices[sym]:.2f} (${actual_dollars:.2f}){suffix}")
        except Exception as e:
            send_telegram_message(f"🌐 {name}\n❌ Buy {sym} failed: {e}")
            return f"F4: failed to buy {sym}: {e}"

    if not trades_info:
        trades_info.append("No buys executed (all legs at/above target).")

    # Persist state
    balances = load_balances(env)
    state = balances.get(cfg["strategy_key"], {})
    final_value_data = get_f4_position_value(api)
    final_total = final_value_data["total_value"]
    total_invested = state.get("total_invested", 0) + investment_amount
    peak_nav = max(state.get("peak_nav", 0) or 0, final_total)
    strategy_return = (final_total / total_invested - 1) if total_invested > 0 else 0
    save_balance(cfg["strategy_key"], {
        "total_invested": total_invested,
        "current_positions": {sym: final_value_data["by_symbol"][sym]["shares"] for sym in targets},
        "current_values": {sym: final_value_data["by_symbol"][sym]["value"] for sym in targets},
        "peak_nav": peak_nav,
        "last_buy_date": datetime.datetime.now().strftime("%Y-%m-%d"),
    }, env)

    # Telegram summary
    weight_str = ", ".join(
        f"{sym}: {final_value_data['by_symbol'][sym]['value'] / max(1, final_total) * 100:.1f}%"
        for sym in targets
    )
    msg = f"🌐 {name} ({pct_label:.2f}%) — ${investment_amount:,.2f}\n\n"
    for t in trades_info:
        msg += f"{t}\n"
    msg += f"\nNew weights: {weight_str}\n"
    msg += f"Total invested: ${total_invested:,.2f}\n"
    msg += f"Current value: ${final_total:,.2f}\n"
    msg += f"Peak NAV: ${peak_nav:,.2f}\n"
    msg += f"Return: {strategy_return:+.1%}"
    send_telegram_message(msg)
    return f"F4 monthly buys complete. Value ${final_total:,.2f}, return {strategy_return:.2%}"


def _retired_quarterly_rebalance_f4(api, force_execute=False, env="live"):
    """
    F4 quarterly rebalance: bring WLDU/GOLY/TLT positions back to exact 40/30/30.
    Run idempotently on the 1st-7th trading day of each calendar quarter.
    """
    raise RuntimeError("F4 retired on 2026-09-09")
    if quarterly_run_complete(f4_config["strategy_key"], env=env) and not force_execute:
        return f"F4 quarterly rebalance already complete for {current_quarter_id()}"
    if not force_execute and not check_trading_day(mode="quarterly"):
        return "Not first 7 trading days of the quarter"

    cfg = f4_config
    name = cfg["display_name"]
    targets = cfg["targets"]
    value_data = get_f4_position_value(api)
    total_value = value_data["total_value"]
    by_symbol = value_data["by_symbol"]

    if total_value < 10.0:
        return f"F4: portfolio value ${total_value:.2f} too small to rebalance"

    target_dollars = {sym: total_value * w for sym, w in targets.items()}
    deltas = {sym: target_dollars[sym] - by_symbol[sym]["value"] for sym in targets}
    prices = {}
    trades_info = []

    # Sells first
    for sym in targets:
        if deltas[sym] >= -cfg["tolerance_amount"]:
            continue
        try:
            prices[sym] = float(get_latest_trade(api, sym))
            shares_have = by_symbol[sym]["shares"]
            shares_to_sell = min(shares_have, (-deltas[sym]) / prices[sym])
            shares_to_sell = _size_sell_order(sym, shares_to_sell)
            if shares_to_sell <= 0 or shares_to_sell * prices[sym] < margin_control_config["min_investment"]:
                continue
            order = submit_order(api, sym, shares_to_sell, "sell")
            wait_for_order_fill(api, order["id"])
            trades_info.append(f"Sold {shares_to_sell:.4f} {sym} (${shares_to_sell * prices[sym]:.2f})")
        except Exception as e:
            send_telegram_message(f"🌐 {name} (rebal)\n❌ Sell {sym} failed: {e}")
            return f"F4 rebalance: failed to sell {sym}: {e}"

    # Buys
    for sym in targets:
        if deltas[sym] <= cfg["tolerance_amount"]:
            continue
        try:
            if sym not in prices:
                prices[sym] = float(get_latest_trade(api, sym))
            shares_to_buy, note = _size_buy_order(sym, deltas[sym], prices[sym])
            if shares_to_buy <= 0:
                trades_info.append(f"⏭ Skipped {sym} rebal buy: {note}")
                continue
            order = submit_order(api, sym, shares_to_buy, "buy")
            wait_for_order_fill(api, order["id"])
            actual_dollars = shares_to_buy * prices[sym]
            suffix = f" [{note}]" if note else ""
            trades_info.append(f"Bought {shares_to_buy:.4f} {sym} (${actual_dollars:.2f}){suffix}")
        except Exception as e:
            send_telegram_message(f"🌐 {name} (rebal)\n❌ Buy {sym} failed: {e}")
            return f"F4 rebalance: failed to buy {sym}: {e}"

    # Save state + mark quarter complete
    final_value_data = get_f4_position_value(api)
    final_total = final_value_data["total_value"]
    balances = load_balances(env)
    state = balances.get(cfg["strategy_key"], {})
    peak_nav = max(state.get("peak_nav", 0) or 0, final_total)
    save_balance(cfg["strategy_key"], {
        **state,
        "current_positions": {sym: final_value_data["by_symbol"][sym]["shares"] for sym in targets},
        "current_values": {sym: final_value_data["by_symbol"][sym]["value"] for sym in targets},
        "peak_nav": peak_nav,
        "last_rebal_date": datetime.datetime.now().strftime("%Y-%m-%d"),
    }, env)
    mark_quarterly_run_complete(cfg["strategy_key"], "rebalance", env=env)

    if not trades_info:
        trades_info.append("No trades needed; weights already on target.")
    weight_str = ", ".join(
        f"{sym}: {final_value_data['by_symbol'][sym]['value'] / max(1, final_total) * 100:.1f}%"
        for sym in targets
    )
    msg = f"🌐 {name} — quarterly rebalance ({current_quarter_id()})\n\n"
    for t in trades_info:
        msg += f"{t}\n"
    msg += f"\nWeights after: {weight_str}\nTotal: ${final_total:,.2f}"
    send_telegram_message(msg)
    return f"F4 quarterly rebalance complete. Total ${final_total:,.2f}"


# ════════════════════════════════════════════════════════════════════
# AAA FREE 2× + NTSD STRATEGY — 7-asset top-3 momentum rotation
# Universe (signal → held position, all ≤2× per ticker):
#   SPY → NTSD,  IWM → SAA,  EEM → EET,  TLT → UBT,
#   IEF → UST,   GLD → UGL,  DBC → DBC.
# Monthly: 6m momentum rank → top-3 with positive scores → inverse-vol
# weight → vol-target scale → balance to SHV. DD-30 stop on trailing-peak
# NAV. Promoted to deployed 2026-05-12 from research.
# ════════════════════════════════════════════════════════════════════


def _aaa_six_month_momentum(api, signal_symbol, lookback_days):
    """Trailing-N-day price return on the unleveraged signal symbol."""
    try:
        bars = get_alpaca_historical_bars(api, signal_symbol, days=max(300, lookback_days + 100))
    except Exception as e:
        print(f"AAA: error fetching bars for {signal_symbol}: {e}")
        return None
    if len(bars) < lookback_days + 1:
        print(f"AAA: insufficient bars for {signal_symbol} ({len(bars)} < {lookback_days + 1})")
        return None
    price_now = bars[-1]
    price_past = bars[-(lookback_days + 1)]
    if price_now <= 0 or price_past <= 0:
        return None
    return price_now / price_past - 1


def _aaa_realized_vol(api, symbol, window=60):
    """60-day annualized realized vol of close-to-close simple returns."""
    try:
        bars = get_alpaca_historical_bars(api, symbol, days=max(150, window + 60))
    except Exception as e:
        print(f"AAA: error fetching bars for {symbol} vol: {e}")
        return None
    if len(bars) < window + 1:
        return None
    rets = [(bars[i + 1] / bars[i]) - 1 for i in range(len(bars) - window - 1, len(bars) - 1) if bars[i] > 0]
    if len(rets) < window // 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return (var ** 0.5) * (252 ** 0.5)


def get_aaa_position_value(api):
    """Total AAA value + per-symbol breakdown across the 7-asset universe + SHV."""
    try:
        positions = list_positions(api)
        aaa_symbols = STRATEGY_SYMBOLS["aaa"]
        by_symbol = {sym: {"value": 0.0, "shares": 0.0} for sym in aaa_symbols}
        total_value = 0.0
        for position in positions:
            ticker = position.get("symbol")
            if ticker in aaa_symbols:
                value = float(position.get("market_value", 0))
                shares = float(position.get("qty", 0))
                by_symbol[ticker] = {"value": value, "shares": shares}
                total_value += value
        return {"total_value": total_value, "by_symbol": by_symbol}
    except Exception as e:
        print(f"Error getting AAA position value: {e}")
        return {"total_value": 0.0, "by_symbol": {sym: {"value": 0.0, "shares": 0.0} for sym in STRATEGY_SYMBOLS["aaa"]}}


def make_monthly_buys_aaa(api, force_execute=False, investment_calc=None,
                           margin_result=None, skip_order_wait=False, env="live"):
    """
    7-Asset Rotator (AAA family) monthly execution. Each call:
      1. Add this month's contribution to the AAA pool.
      2. Compute 6m momentum on the 7 signal symbols (SPY/IWM/EEM/TLT/IEF/GLD/DBC).
      3. Check DD-30 trailing-peak NAV stop — if breached, dump everything to SHV.
      4. Pick top-3 positive-momentum signals; if fewer than 1 positive, go to SHV.
      5. Inverse-vol weight the top-N (using 60d trailing vol of the *held* positions).
      6. Apply portfolio vol-target scale = min(1, 25% / weighted_vol). Excess to SHV.
      7. Generate buy/sell deltas, sells-first then buys.
      8. Save state and update Firestore.
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        return "Not first trading day of the month"
    if force_execute:
        print("AAA: Force execution enabled — bypassing trading day check")

    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)

    cfg = aaa_config
    alloc_key = cfg["alloc_key"]
    name = cfg["display_name"]
    investment_amount = investment_calc["strategy_amounts"].get(alloc_key, 0)
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    buying_power = investment_calc["total_available"] + investment_calc["margin_approved"]
    pct_label = strategy_allocations.get(alloc_key, 0) * 100
    check_date = datetime.datetime.now().strftime("%Y-%m-%d")

    def _skip(reason):
        msg = f"🎛 {name} ({pct_label:.2f}%) — ${investment_amount:,.2f}\n⏭ {reason}"
        send_telegram_message(msg)
        print(reason)
        return reason

    # Gate checks
    if target_margin == 0 and leverage > 1.0:
        return _skip(f"Skipped — deleveraging required ({leverage:.2f}x)")
    if buying_power < investment_amount:
        return _skip(f"Skipped — insufficient buying power (${buying_power:,.2f})")
    if investment_amount < margin_control_config["min_investment"]:
        return _skip(f"Skipped — ${investment_amount:.2f} below $1.00 minimum")
    if target_margin > 0:
        pv = metrics.get("portfolio_value", 0)
        equity = metrics.get("equity", 0)
        if pv > 0 and equity > 0:
            projected_leverage = (pv + investment_amount) / equity
            if projected_leverage >= margin_control_config["max_leverage"]:
                return _skip(f"Skipped — projected leverage {projected_leverage:.3f}x exceeds limit")

    # Load state
    balances = load_balances(env)
    state = balances.get(cfg["strategy_key"], {})
    total_invested = state.get("total_invested", 0)
    peak_nav = float(state.get("peak_nav", 0) or 0)

    # Current AAA value (before today's contribution)
    value_data = get_aaa_position_value(api)
    current_value = value_data["total_value"]
    by_symbol = value_data["by_symbol"]
    print(f"AAA — investment ${investment_amount:.2f}, current value ${current_value:.2f}")

    # DD-30 stop check
    new_peak_nav = max(peak_nav, current_value) if peak_nav > 0 else current_value
    dd = (current_value - new_peak_nav) / new_peak_nav if new_peak_nav > 0 else 0.0
    dd_triggered = peak_nav > 0 and dd < -cfg["dd_threshold"]
    if dd_triggered:
        print(f"  DD-stop TRIGGERED: drawdown {dd:.1%} < -{cfg['dd_threshold']:.0%} — forcing defensive")
        new_peak_nav = current_value  # reset peak

    # Compute momentum scores (skip if DD-triggered)
    scores = {}
    if not dd_triggered:
        for signal_sym, pos_sym in cfg["candidates"]:
            sc = _aaa_six_month_momentum(api, signal_sym, cfg["lookback_days"])
            if sc is not None:
                scores[pos_sym] = sc
        print(f"  momentum scores: {scores}")

    # Pick top-N positive-momentum picks
    if dd_triggered:
        picks = []
    else:
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        picks = [pos for pos, sc in ranked[: cfg["top_n"]] if sc > cfg["min_score"]]

    # Compute inverse-vol weights on the picks, then portfolio vol-target scale
    weights = {pos: 0.0 for _, pos in cfg["candidates"]}
    cash_weight = 1.0
    realized_vols = {}
    scale = 0.0
    if picks:
        invvols = {}
        for pos in picks:
            v = _aaa_realized_vol(api, pos, window=cfg["vol_window"])
            if v is not None and v > 0:
                invvols[pos] = 1.0 / v
                realized_vols[pos] = v
        if not invvols:
            # Vol data unavailable — equal-weight the picks
            raw = {pos: 1.0 / len(picks) for pos in picks}
        else:
            tot = sum(invvols.values())
            raw = {pos: invvols.get(pos, 0.0) / tot for pos in picks}
        # Portfolio vol estimate (weighted sum of underlying vols — conservative)
        est_vol = sum(raw[pos] * realized_vols.get(pos, cfg["target_vol"]) for pos in picks)
        scale = min(1.0, cfg["target_vol"] / est_vol) if est_vol > 0 else 1.0
        for pos in picks:
            weights[pos] = raw[pos] * scale
        cash_weight = 1.0 - sum(weights.values())
        print(f"  picks: {picks}, inverse-vol scale: {scale:.3f}, est vol: {est_vol:.1%}")

    # Compute target dollar amounts
    total_to_allocate = current_value + investment_amount
    targets = {sym: 0.0 for sym in STRATEGY_SYMBOLS["aaa"]}
    for pos, w in weights.items():
        if pos in targets:
            targets[pos] = w * total_to_allocate
    targets[cfg["defensive"]] = cash_weight * total_to_allocate

    print(f"  target $: {{ {', '.join(f'{s}: ${v:,.2f}' for s, v in targets.items() if v > 0)} }}")

    # Fetch prices for any symbols we'll trade
    prices = {}
    for sym in STRATEGY_SYMBOLS["aaa"]:
        if targets[sym] > 0 or by_symbol[sym]["value"] > cfg["tolerance_amount"]:
            try:
                prices[sym] = float(get_latest_trade(api, sym))
            except Exception as e:
                send_telegram_message(f"🎛 {name}\n❌ Failed to fetch price for {sym}: {e}")
                return f"AAA: failed to fetch price for {sym}: {e}"

    current_dollars = {sym: by_symbol[sym]["value"] for sym in STRATEGY_SYMBOLS["aaa"]}
    deltas = {sym: targets[sym] - current_dollars[sym] for sym in STRATEGY_SYMBOLS["aaa"]}
    trades_info = []

    # Sells first (negative deltas)
    for sym in STRATEGY_SYMBOLS["aaa"]:
        if deltas[sym] >= -cfg["tolerance_amount"]:
            continue
        if sym not in prices:
            continue
        shares_have = by_symbol[sym]["shares"]
        sell_dollars = -deltas[sym]
        shares_to_sell = min(shares_have, sell_dollars / prices[sym])
        shares_to_sell = _size_sell_order(sym, shares_to_sell)
        if shares_to_sell <= 0 or shares_to_sell * prices[sym] < margin_control_config["min_investment"]:
            continue
        try:
            order = submit_order(api, sym, shares_to_sell, "sell")
            if not skip_order_wait:
                wait_for_order_fill(api, order["id"])
            trades_info.append(f"Sold {shares_to_sell:.4f} {sym} (${shares_to_sell * prices[sym]:.2f})")
        except Exception as e:
            send_telegram_message(f"🎛 {name}\n❌ Sell {sym} failed: {e}")
            return f"AAA: failed to sell {sym}: {e}"

    # Buys (positive deltas)
    for sym in STRATEGY_SYMBOLS["aaa"]:
        if deltas[sym] <= cfg["tolerance_amount"]:
            continue
        if sym not in prices:
            continue
        buy_dollars = deltas[sym]
        shares_to_buy, note = _size_buy_order(sym, buy_dollars, prices[sym])
        if shares_to_buy <= 0:
            trades_info.append(f"⏭ Skipped {sym}: {note}")
            continue
        try:
            order = submit_order(api, sym, shares_to_buy, "buy")
            if not skip_order_wait:
                wait_for_order_fill(api, order["id"])
            actual_dollars = shares_to_buy * prices[sym]
            suffix = f" [{note}]" if note else ""
            trades_info.append(f"Bought {shares_to_buy:.4f} {sym} @ ${prices[sym]:.2f} (${actual_dollars:.2f}){suffix}")
        except Exception as e:
            send_telegram_message(f"🎛 {name}\n❌ Buy {sym} failed: {e}")
            return f"AAA: failed to buy {sym}: {e}"

    if not trades_info:
        trades_info.append("No trades needed; targets already aligned.")

    # Persist state
    final_value_data = get_aaa_position_value(api)
    final_total = final_value_data["total_value"]
    final_by_symbol = final_value_data["by_symbol"]
    final_peak_nav = max(new_peak_nav, final_total)
    final_total_invested = total_invested + investment_amount
    strategy_return = (final_total / final_total_invested - 1) if final_total_invested > 0 else 0
    save_balance(cfg["strategy_key"], {
        "total_invested": final_total_invested,
        "current_positions": {sym: final_by_symbol[sym]["shares"] for sym in STRATEGY_SYMBOLS["aaa"]},
        "current_values": {sym: final_by_symbol[sym]["value"] for sym in STRATEGY_SYMBOLS["aaa"]},
        "peak_nav": final_peak_nav,
        "last_momentum_check": {
            "scores": scores,
            "picks": picks,
            "weights": weights,
            "vol_scale": scale,
            "realized_vols": realized_vols,
            "dd_triggered": dd_triggered,
            "drawdown": dd,
        },
        "last_signal_check_date": check_date,
        "last_trade_date": check_date if any(("No trades" not in t) for t in trades_info) else state.get("last_trade_date"),
    }, env)

    # Telegram summary
    scores_str = ", ".join(f"{s}: {sc:+.1%}" for s, sc in scores.items()) if scores else "n/a"
    msg = f"🎛 {name} ({pct_label:.2f}%) — ${investment_amount:,.2f}\n\n"
    msg += f"6m scores: {scores_str}\n"
    if dd_triggered:
        msg += f"⚠️ DD-stop triggered (DD {dd:.1%}) — all to {cfg['defensive']}\n"
    elif not picks:
        msg += f"Signal: defensive ({cfg['defensive']}, no positive momentum)\n"
    else:
        picks_str = ", ".join(f"{p} ({weights[p]*100:.1f}%)" for p in picks)
        msg += f"Picks: {picks_str}\n"
        msg += f"Cash ({cfg['defensive']}): {cash_weight*100:.1f}%\n"
        msg += f"Vol-scale: {scale:.2f}\n"
    msg += "\n"
    for t in trades_info[:8]:  # cap message size
        msg += f"{t}\n"
    if len(trades_info) > 8:
        msg += f"... and {len(trades_info) - 8} more trades\n"
    msg += f"\nTotal invested: ${final_total_invested:,.2f}\n"
    msg += f"Current value: ${final_total:,.2f}\n"
    msg += f"Peak NAV: ${final_peak_nav:,.2f}\n"
    msg += f"Return: {strategy_return:+.1%}"
    send_telegram_message(msg)

    return f"AAA monthly complete. Picks: {picks}. Value ${final_total:,.2f}, return {strategy_return:.2%}"


# Helper function to wait for an order to be filled
def wait_for_order_fill(api, order_id, timeout=300, poll_interval=5):
    elapsed_time = 0
    while elapsed_time < timeout:
        order = get_order(api, order_id)
        if order["status"] == "filled":
            print(f"Order {order_id} filled.")
            return float(order["filled_avg_price"]) * float(order["filled_qty"])
        elif order["status"] == "canceled":
            print(f"Order {order_id} was canceled.")
            send_telegram_message(f"Order {order_id} was canceled.")
            return
        else:
            print(f"Waiting for order {order_id} to fill... (status: {order['status']})")
            time.sleep(poll_interval)
            elapsed_time += poll_interval
    print(f"Timeout: Order {order_id} did not fill within {timeout} seconds.")
    send_telegram_message(
        f"Timeout: Order {order_id} did not fill within {timeout} seconds."
    )


def monthly_invest_all_strategies(api, force_execute=False, skip_order_wait=False, env="live"):
    """
    Orchestrator function that runs all four monthly investment strategies.
    Calculates budgets ONCE and distributes them to ensure exact percentage splits.
    
    This prevents the problem of each function independently calculating and over-spending.
    
    Args:
        api: Alpaca API credentials
        force_execute: Bypass trading day check for testing
    
    Returns:
        dict with results from all four strategies
    """
    if not force_execute and not should_run_monthly_orchestrator(env=env):
        print("Not first trading day of the month, or this month already has a clean run")
        return {"error": "Not first trading day of the month, or this month already has a clean run"}

    # Was a prior attempt this month blocked by a data-fetch error? Surfaced in
    # the Telegram message below so a retry is never confused with a fresh
    # first-of-month run.
    is_retry = False
    try:
        prior = (get_firestore_client().collection(f"monthly-runs-{env}")
                 .document(current_month_id()).get())
        is_retry = prior.exists and prior.to_dict().get("clean", True) is False
    except Exception:
        pass

    # Step 1: Sync cost basis from Alpaca to Firestore BEFORE executing trades
    # This ensures we start with accurate data
    print("=== Monthly Investment Orchestrator ===")
    print("Step 1: Syncing cost basis from Alpaca to Firestore...")
    cost_basis_result = recalculate_all_strategies_cost_basis(api, env, silent=True)
    if cost_basis_result.get("success"):
        if cost_basis_result.get("total_difference", 0) != 0:
            print(f"✅ Cost basis synced: ${cost_basis_result['total_difference']:.2f} correction applied")
        else:
            print("✅ Cost basis already in sync")
    else:
        print(f"⚠️  Warning: Cost basis sync had issues: {cost_basis_result.get('error', 'Unknown error')}")
    
    # Step 2: Calculate margin conditions and investment amounts ONCE
    print("\nStep 2: Calculating budgets for all strategies...")
    
    margin_result = check_margin_conditions(api, env=env)
    investment_calc = calculate_monthly_investments(api, margin_result, env)
    
    # Get actual allocation percentages (may differ from targets if rebalancing is enabled)
    total_investing = investment_calc['total_investing']
    strategy_amounts = investment_calc['strategy_amounts']
    
    # Calculate actual percentages being used
    def get_pct(key):
        return (strategy_amounts[key] / total_investing * 100) if total_investing > 0 else 0
    
    print(f"Total investing power: ${total_investing:.2f}")
    print(f"  HFEA ({get_pct('hfea_allo'):.1f}%): ${strategy_amounts['hfea_allo']:.2f}")
    print(f"  SPXL ({get_pct('spxl_allo'):.1f}%): ${strategy_amounts['spxl_allo']:.2f}")
    print(f"  Dual Momentum ({get_pct('dual_momentum_allo'):.1f}%): ${strategy_amounts['dual_momentum_allo']:.2f}")
    print(f"  7-Asset Rotator ({get_pct('aaa_allo'):.1f}%): ${strategy_amounts['aaa_allo']:.2f}")
    
    # Send one shared account status message to Telegram before executing strategies
    metrics = margin_result.get("metrics", {})
    gate_results = margin_result.get("gate_results", {})
    margin_errors = margin_result.get("errors", [])
    trend_emoji = "✅" if gate_results.get("market_trend", False) else "❌"

    # A gate that never RAN (check_margin_conditions returns early on a data-fetch
    # error) must not render as "0.0%" — that reads like a real measurement. On
    # 2026-09-01 a FRED timeout produced "Margin rate: 0.0% | Buffer: 0.0% |
    # Leverage: 0.00x", indistinguishable from a genuine cash-only decision.
    def _pct(key):
        return f"{metrics[key] * 100:.1f}%" if key in metrics else "n/a"

    def _usd(key):
        return f"${metrics[key]:,.2f}" if key in metrics else "n/a"

    leverage_str = f"{metrics['leverage']:.2f}x" if "leverage" in metrics else "n/a"

    if margin_errors:
        margin_decision = "⚠️ Cash-Only — data error, NOT a market signal"
    elif margin_result.get("allowed", False):
        margin_decision = "🟢 Margin ON (+10%)"
    else:
        margin_decision = "🔴 Cash-Only"

    account_msg = "📊 Monthly Investment — Account Status\n\n"
    if is_retry:
        account_msg += "🔁 Retry: prior attempt this month errored (data-fetch failure)\n\n"
    account_msg += f"SPX: {_usd('spx_price')} (SMA: {_usd('spx_sma')}) {trend_emoji}\n"
    account_msg += f"Margin rate: {_pct('margin_rate')} | Buffer: {_pct('buffer')} | Leverage: {leverage_str}\n"
    account_msg += f"Decision: {margin_decision}\n"
    if margin_errors:
        account_msg += "⚠️ " + "; ".join(margin_errors) + "\n"
        account_msg += "↳ Auto-retry on the next trading day (day 1–7 window).\n"
    account_msg += "\n"
    account_msg += f"Equity: {_usd('equity')} | Portfolio: {_usd('portfolio_value')}\n"
    account_msg += f"Investing: ${total_investing:,.2f}\n\n"
    
    # Per-strategy budget breakdown. Labels aus strategy_allocations abgeleitet,
    # damit sie bei der naechsten Gewichtsaenderung nicht wieder auseinanderlaufen.
    account_msg += "Budget per strategy:\n"
    _labels = {"hfea_allo": "HFEA", "spxl_allo": "SPXL SMA",
               "dual_momentum_allo": "Dual Momentum", "aaa_allo": "7-Asset Rotator"}
    for key, weight in strategy_allocations.items():
        label = f"{_labels.get(key, key)} {weight * 100:.1f}%"
        account_msg += f"  • {label}: ${strategy_amounts[key]:,.2f}\n"
    
    send_telegram_message(account_msg)
    
    # Run all monthly strategies with pre-calculated budgets. Each call is wrapped
    # so a single strategy raising doesn't kill the rest of the orchestrator run,
    # and the post-run summary lists exactly what each strategy did.
    results = {}

    def _run(name, label, fn):
        print(f"\n=== Executing {label} ===")
        try:
            results[name] = fn()
        except Exception as exc:
            err = f"❌ exception: {exc}"
            print(err)
            results[name] = err

    _run("hfea", "HFEA", lambda: make_monthly_buys(api, force_execute, investment_calc, margin_result, skip_order_wait, env))
    _run("spxl", "SPXL SMA", lambda: monthly_buying_sma(api, "SPXL", force_execute, investment_calc, margin_result, skip_order_wait, env))
    _run("dual_momentum", "Dual Momentum", lambda: monthly_dual_momentum_strategy(api, force_execute, investment_calc, margin_result, skip_order_wait, env))
    _run("aaa", "7-Asset Rotator", lambda: make_monthly_buys_aaa(api, force_execute=force_execute, investment_calc=investment_calc, margin_result=margin_result, skip_order_wait=skip_order_wait, env=env))

    print("\n=== All Monthly Strategies Complete ===")

    # Send a summary so a missing strategy is impossible to overlook
    summary_lines = ["📋 Monthly Orchestrator Summary"]
    label_map = {
        "hfea": "HFEA",
        "spxl": "SPXL SMA",
        "dual_momentum": "Dual Momentum",
        "aaa": "7-Asset Rotator",
    }
    for key, label in label_map.items():
        outcome = results.get(key, "(no result)")
        outcome_str = str(outcome)
        if outcome_str.startswith("❌"):
            icon = "❌"
        elif outcome_str.startswith("Skipped") or "Skipped" in outcome_str or "Not first trading day" in outcome_str:
            icon = "⏭"
        else:
            icon = "✅"
        # Trim long status lines for Telegram readability
        truncated = outcome_str if len(outcome_str) < 80 else outcome_str[:77] + "..."
        summary_lines.append(f"{icon} {label}: {truncated}")
    send_telegram_message("\n".join(summary_lines))

    mark_monthly_run_complete(env=env, clean=not margin_result.get("errors"))

    return results


def monthly_invest_all(request):
    """
    Orchestrator endpoint that runs all four monthly strategies in one coordinated execution.
    Recommended for production use to ensure exact budget splits and avoid over-spending.
    """
    api = set_alpaca_environment(env=alpaca_environment)
    results = monthly_invest_all_strategies(api)
    return jsonify(results), 200


@app.route("/monthly_buy_hfea", methods=["POST"])
def monthly_buy_hfea(request):
    api = set_alpaca_environment(
        env=alpaca_environment
    )  # or 'paper' based on your needs
    return make_monthly_buys(api, env=alpaca_environment)


@app.route("/rebalance_hfea", methods=["POST"])
def rebalance_hfea(request):
    api = set_alpaca_environment(
        env=alpaca_environment
    )  # or 'paper' based on your needs
    return rebalance_portfolio(api)


@app.route("/monthly_buy_spxl", methods=["POST"])
def monthly_buy_spxl(request):
    api = set_alpaca_environment(
        env=alpaca_environment
    )  # or 'paper' based on your needs
    result = monthly_buying_sma(api, "SPXL", env=alpaca_environment)
    print(result)
    return result, 200


@app.route("/daily_trade_spxl_200sma", methods=["POST"])
def daily_trade_spxl_200sma(request):
    api = set_alpaca_environment(
        env=alpaca_environment
    )  # or 'paper' based on your needs
    result = daily_trade_sma(api, "SPXL", env=alpaca_environment)
    print(result)
    return result, 200


@app.route("/monthly_dual_momentum", methods=["POST"])
def monthly_dual_momentum(request):
    """
    Cloud Function endpoint for Dual Momentum Strategy.
    Executes monthly dual momentum strategy with SPUU/QLD/EFO/BND best-of-3.
    """
    try:
        api = set_alpaca_environment(env=alpaca_environment)
        result = monthly_dual_momentum_strategy(api, env=alpaca_environment)
        return jsonify({"result": result}), 200
    except Exception as e:
        error_message = f"Dual Momentum Strategy error: {str(e)}"
        print(error_message)
        send_telegram_message(error_message)
        return jsonify({"error": error_message}), 500


# ─────────────────────────────────────────────────────────────────────────
# RETIRED 2026-09-21: Regime SSO und 9-Sig
#
# Beide Sleeves am 21.09.2026 aufgeloest. Grund: US-/Tech-Konzentration und
# schwache risikoadjustierte Ergebnisse. Ein originalgetreuer 25-Jahre-Replay
# aller Sleeves (mit DD-30-Stops, 6m-Momentum, taeglichen SMA-Gates und
# positionsgenauem Hebel) ergab Sharpe 0,50 fuer Regime SSO und 0,47 fuer
# 9-Sig, gegen 0,87 fuer den AAA-Rotator.
#
# Live-Positionen SSO, TQQQ und AGG wurden am selben Tag verkauft
# (Erloes 1.159,06 USD) und in AAA umgeschichtet. Die 20,74% Zielallokation
# gingen an die verbleibenden vier Sleeves, siehe strategy_allocations.
#
# Die internen Funktionen (make_monthly_buys_regime, compute_regime_score,
# execute_quarterly_nine_sig_signal und weitere) stehen noch als toter Code
# in dieser Datei und werden in einem eigenen Schritt entfernt. Sie werden
# von keinem Pfad mehr aufgerufen: weder vom Orchestrator noch ueber eine
# HTTP-Route. Ohne Route kann cloudbuild sie nicht mehr deployen.
# ─────────────────────────────────────────────────────────────────────────


@app.route("/monthly_buy_aaa", methods=["POST"])
def monthly_buy_aaa(request):
    """7-Asset Rotator (AAA family) — monthly momentum rotation + risk-controlled buys."""
    api = set_alpaca_environment(env=alpaca_environment)
    return make_monthly_buys_aaa(api, env=alpaca_environment)


@app.route("/index_alert", methods=["POST"])
def index_alert(request):
    return check_unified_index_alert(request, env=alpaca_environment)


def audit_monthly_run(api, env="live", lookback_days=14):
    """
    Verify that this month's orchestrator actually ran and that each strategy
    has produced expected activity. Sends one consolidated Telegram alert.

    Designed to be invoked daily by a Cloud Scheduler watchdog after the
    monthly buy window closes (e.g. day 8 of each month). Safe to re-run.
    """
    today = datetime.datetime.now()
    month_id = current_month_id(today)
    after = (today - datetime.timedelta(days=lookback_days)).strftime("%Y-%m-%dT00:00:00Z")

    # 1) Did the orchestrator complete this month?
    try:
        marker = (
            get_firestore_client()
            .collection(f"monthly-runs-{env}")
            .document(month_id)
            .get()
        )
        orchestrator_ok = marker.exists
    except Exception as e:
        print(f"Warning: could not read monthly-runs marker: {e}")
        orchestrator_ok = None  # unknown

    # 2) Pull recent Alpaca orders to detect strategy activity
    try:
        headers = get_auth_headers(api)
        url = f"{api['BASE_URL']}/v2/orders"
        resp = alpaca_request_with_retry(
            "GET",
            url,
            headers,
            params={"status": "closed", "after": after, "limit": 500, "direction": "asc"},
            label="audit_orders",
        )
        recent_orders = resp.json() if resp is not None else []
    except Exception as e:
        print(f"Warning: could not list recent orders: {e}")
        recent_orders = []

    expected_symbols = {
        "HFEA": STRATEGY_SYMBOLS["hfea"],
        "SPXL SMA": STRATEGY_SYMBOLS["spxl_sma"],
        "Dual Momentum": STRATEGY_SYMBOLS["dual_momentum"],
        "7-Asset Rotator": STRATEGY_SYMBOLS["aaa"],
    }

    strategy_activity = {label: [] for label in expected_symbols}
    for o in recent_orders:
        sym = o.get("symbol")
        for label, syms in expected_symbols.items():
            if sym in syms:
                strategy_activity[label].append(
                    f"{(o.get('filled_at') or o.get('created_at') or '?')[:10]} {o.get('side','?')} {sym}"
                )

    # 3) Build report
    lines = [f"🛎 Monthly Run Audit — {month_id}"]
    if orchestrator_ok is True:
        lines.append("✅ Orchestrator completed this month")
    elif orchestrator_ok is False:
        lines.append("❌ NO orchestrator completion marker for this month")
    else:
        lines.append("⚠️  Could not read orchestrator marker (Firestore error)")

    lines.append("")
    lines.append(f"Trades in last {lookback_days} days:")
    for label in expected_symbols:
        events = strategy_activity[label]
        if events:
            lines.append(f"✅ {label}: {len(events)} trade(s) — most recent {events[-1]}")
        else:
            lines.append(f"❌ {label}: NO recent trades")

    msg = "\n".join(lines)
    print(msg)
    send_telegram_message(msg)
    return msg


@app.route("/audit_monthly_run", methods=["POST"])
def audit_monthly_run_route(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return audit_monthly_run(api, env=alpaca_environment)


class _LocalRequest:
    """Minimaler Ersatz fuer ein Flask-Request-Objekt, damit run_local die
    HTTP-Handler direkt aufrufen kann."""

    content_type = "application/json"

    def __init__(self, payload):
        self._payload = payload
        self.data = json.dumps(payload).encode("utf-8")

    def get_json(self, silent=False):
        return self._payload


def validate_force_environment(env, force_execute):
    if force_execute and env != "paper":
        raise ValueError("--force is paper only and cannot be used with live trading")


def run_local(action, env="paper", request="test", force_execute=False,
              investment_amount=None, alert_payload=None):
    validate_force_environment(env, force_execute)
    api = set_alpaca_environment(env=env, use_secret_manager=False)
    if action == "monthly_invest_all":
        return monthly_invest_all_strategies(api, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "monthly_buy_hfea":
        return make_monthly_buys(api, force_execute=force_execute)
    elif action == "rebalance_hfea":
        return rebalance_portfolio(api)
    elif action == "monthly_buy_spxl":
        return monthly_buying_sma(api, "SPXL", force_execute=force_execute, env=env)
    elif action in ("sell_spxl_below_200sma", "buy_spxl_above_200sma"):
        return daily_trade_sma(api, "SPXL", env=env)
    elif action == "index_alert":
        if not alert_payload:
            return ("index_alert braucht mindestens --index_symbol. Beispiel: "
                    "--action index_alert --source eodhd "
                    "--index_symbol IUSQ.XETRA --sma_period 255")
        # jsonify() braucht einen App-Kontext. In der Cloud Function liefert
        # den der Request selbst, in der CLI gibt es keinen.
        with app.app_context():
            response, code = check_unified_index_alert(
                _LocalRequest(alert_payload), env=env)
            return {"http_status": code, **response.get_json()}
    elif action == "monthly_dual_momentum":
        return monthly_dual_momentum_strategy(api, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "monthly_buy_aaa":
        return make_monthly_buys_aaa(api, force_execute=force_execute, skip_order_wait=True, env=env)
    else:
        return "No valid action provided."


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--action",
        choices=[
            "monthly_invest_all",
            "monthly_buy_hfea",
            "rebalance_hfea",
            "monthly_buy_spxl",
            "sell_spxl_below_200sma",
            "buy_spxl_above_200sma",
            "index_alert",
            "monthly_dual_momentum",
            "monthly_buy_aaa",
        ],
        required=True,
        help="Action to perform: 'monthly_invest_all' runs all four monthly strategies with coordinated budgets (recommended)",
    )
    parser.add_argument(
        "--env",
        choices=["live", "paper"],
        default="paper",
        help="Alpaca environment: 'live' or 'paper'",
    )
    parser.add_argument(
        "--use_secret_manager",
        action="store_true",
        help="Use Google Secret Manager for API keys",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force execution even if not on the correct trading day (for testing)",
    )
    parser.add_argument(
        "--investment_amount",
        type=float,
        default=None,
    )
    # index_alert-Parameter
    parser.add_argument("--index_symbol", default=None,
                        help="index_alert: Symbol, z.B. IUSQ.XETRA oder SPY")
    parser.add_argument("--index_name", default=None,
                        help="index_alert: Klartextname fuer die Telegram-Nachricht")
    parser.add_argument("--alert_type", default="sma_crossing",
                        choices=["sma_crossing", "ath_drop"])
    parser.add_argument("--source", default="alpaca", choices=["alpaca", "eodhd"])
    parser.add_argument("--run_role", default="decisive",
                        choices=["advisory", "decisive", "reconcile"],
                        help="index_alert, nur bei --source eodhd")
    parser.add_argument("--sma_period", type=int, default=200)
    parser.add_argument("--noise_threshold", type=float, default=1.0)
    parser.add_argument("--threshold_percent", type=float, default=30.0)
    parser.add_argument("--public_chat_secret", default=None,
                        help="index_alert: Secret-Name eines zweiten Telegram-Kanals, "
                             "in den nur echte Crossings gehen")
    parser.add_argument("--currency_symbol", default=None,
                        help="Default: Euro-Zeichen bei --source eodhd, sonst $")
    args = parser.parse_args()

    alert_payload = None
    if args.index_symbol:
        alert_payload = {
            "index_symbol": args.index_symbol,
            "index_name": args.index_name or args.index_symbol,
            "alert_type": args.alert_type,
            "source": args.source,
            "run_role": args.run_role,
            "sma_period": args.sma_period,
            "noise_threshold": args.noise_threshold,
            "public_chat_secret": args.public_chat_secret,
            "threshold_percent": args.threshold_percent,
            "currency_symbol": args.currency_symbol
                               or ("\u20ac" if args.source == "eodhd" else "$"),
        }

    # Run the function locally
    result = run_local(action=args.action, env=args.env, force_execute=args.force,
                       investment_amount=args.investment_amount,
                       alert_payload=alert_payload)
    print(f"\nResult: {result}\n")
