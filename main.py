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
    # Set 2026-09-23. Base AAA/World-Trend/Mix8 = 1:1:2 from a 66-mix grid on
    # 1994-2026 and 2000-2026 after German tax, each rotator on the exact live
    # sizing logic (25/25/50: 15.3 / 14.6 % CAGR pre-tax, Sharpe 0.63 / 0.62,
    # worst max DD -23.8 %). Then 15 % S&P-Trend 3x on top, pro rata from the
    # three, so the portfolio keeps up in long bull runs: 15.8 / 14.8 % CAGR,
    # Sharpe 0.62 / 0.62, max DD -25.1 %, behind the MSCI World in 12 % of
    # 5-year windows instead of 23 %.
    #
    # Retired: Dual Momentum 2026-09-23, HFEA + SPXL SMA 2026-09-22 (SPXL SMA
    # returns as S&P-Trend under a different objective), Regime SSO + 9-Sig
    # 2026-09-21.
    #
    # These govern the MONTHLY CONTRIBUTION SPLIT; calculate_monthly_investments()
    # invests ALL account cash on each monthly run.
    "aaa_allo":         0.2125,   # 7-Asset Rotator
    "world_trend_allo": 0.2125,   # World-Trend (WLDU / UGLD, 150-day SMA)
    "mix8_allo":        0.425,    # Mix8 Top-2
    "spx_trend_allo":   0.15,     # S&P-Trend 3x (SPXL / BIL, 200-day SMA)
}


# Strategy Ticker Ownership
# Each strategy has clear ticker ownership for simplified margin calculations and position tracking:
# - 7-Asset Rotator (AAA family): NTSD, SAA, EET, UBT, UST, UGL, DBC (top-3 selected monthly), SHV (defensive)
# - World-Trend: WLDU, UGLD, USFR (defensive)
# - Mix8 Top-2: SSO, QLD, EFO, EEM, GLD, IEF, TLT, KMLM (top-2 monthly), SGOV (defensive)
# - S&P-Trend 3x: SPXL, BIL (defensive)

# Strategy ticker ownership mapping for cost basis recalculation
STRATEGY_SYMBOLS = {
    "aaa": ["NTSD", "SAA", "EET", "UBT", "UST", "UGL", "DBC", "SHV"],
    "world_trend": ["WLDU", "UGLD", "USFR"],
    "mix8": ["SSO", "QLD", "EFO", "EEM", "GLD", "IEF", "TLT", "KMLM", "SGOV"],
    "spx_trend": ["SPXL", "BIL"],
}

# Sleeve-Register: Firestore-Schluessel -> (Gewichts-Schluessel, Anzeigename).
# Die EINE Stelle, an der Reporting, Rebalancing, Waechter und Orchestrator
# ihre Sleeves herbekommen. Handgepflegte Kopien davon waren die Ursache des
# KeyError-Risikos in audit_monthly_run beim HFEA-Retirement.
SLEEVES = {
    "aaa":         ("aaa_allo", "7-Asset Rotator"),
    "world_trend": ("world_trend_allo", "World-Trend"),
    "mix8":        ("mix8_allo", "Mix8 Top-2"),
    "spx_trend":   ("spx_trend_allo", "S&P-Trend 3x"),
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
    "lookbacks": [126],                   # 6-month momentum on signal symbols
    "lookback_weights": [1.0],
    "score_label": "6m",
    "top_n": 3,                           # Hold top-3 by momentum
    "min_score": 0.0,                     # Only include picks with positive momentum
    "vol_window": 60,                     # 60-day trailing realized vol for inverse-vol + target
    "target_vol": 0.25,                   # 25% annualized portfolio vol target
    "dd_threshold": 0.30,                 # 30% trailing-peak NAV stop → all to defensive
    "tolerance_amount": 5.0,              # Skip trades < $5
}

# Mix8 Top-2 — live since 2026-09-23. Same engine as AAA (make_monthly_buys_rotator),
# broader menu, blended 3/6/12m momentum, top-2. Backtested with EXACTLY this sizing
# logic: 1994-2026 16.7 % CAGR pre-tax, Sharpe 0.61 after German tax, max DD -33.7 %.
# Managed futures via KMLM, not DBMF: the backtest's managed-futures series IS KMLM,
# and KMLM became free when HFEA was retired on 2026-09-22.
mix8_config = {
    "strategy_key": "mix8",
    "alloc_key": "mix8_allo",
    "display_name": "Mix8 Top-2",
    "candidates": [
        ("SPY", "SSO"),
        ("QQQ", "QLD"),
        ("EFA", "EFO"),
        ("EEM", "EEM"),
        ("GLD", "GLD"),
        ("IEF", "IEF"),
        ("TLT", "TLT"),
        ("KMLM", "KMLM"),
    ],
    "defensive": "SGOV",
    "lookbacks": [63, 126, 252],          # 3/6/12-month momentum, equally weighted
    "lookback_weights": [1 / 3, 1 / 3, 1 / 3],
    "score_label": "3/6/12m",
    "top_n": 2,
    "min_score": 0.0,
    "vol_window": 60,
    "target_vol": 0.25,
    "dd_threshold": 0.30,
    "tolerance_amount": 5.0,
}

# World-Trend — live since 2026-09-23. Each leg is held (50 %) while its UNLEVERAGED
# index sits above its 150-day SMA, confirmed on 3 consecutive days with a 1 % band;
# otherwise that half sits in USFR. Signals come from EODHD, not Alpaca: URTH trades
# ~5,700 shares/day on IEX, and IEX showed stale closes 7 % off consolidated on
# 2023-05-01 and 2023-05-18 plus a different band state on 9 of 1,027 days.
# EODHD adjusted_close is total return, which is what the backtest ran on.
world_trend_config = {
    "strategy_key": "world_trend",
    "alloc_key": "world_trend_allo",
    "display_name": "World-Trend",
    "emoji": "🌍",
    "legs": [
        ("URTH.US", "WLDU"),               # 2x MSCI World
        ("GLD.US", "UGLD"),                # 2x gold (UGL belongs to AAA)
    ],
    "defensive": "USFR",
    "sma_period": 150,
    "band": 0.01,
    "confirm_days": 3,
    "max_live_jump": 0.20,                 # gold fell a real 10 % on 2026-01-30; only
                                           # catch ticker changes and broken prints
    "max_quote_age_minutes": 120,
    "tolerance_amount": 5.0,
}

# S&P-Trend 3x — live since 2026-09-23, added so the portfolio keeps up in long
# equity bull runs. SPXL while SPY (EODHD, total return) sits above its 200-day
# SMA with a 1 % band, else BIL; one day beyond the band switches. It is the
# SPXL SMA sleeve retired on 2026-09-22 under a max-Sharpe objective (neutral,
# -0.002) and brought back under Carl's objective of not lagging the MSCI World:
# at 15 % it cuts the share of 5-year windows behind the MSCI World from 23 % to
# 12 % (1994-2026), lifts 2009-21 from 10.4 % to 12.4 %/yr (World 12.3 %),
# raises CAGR by 0.6 pp and leaves Sharpe unchanged. Cost: 2022 -18 % vs -14 %.
spx_trend_config = {
    "strategy_key": "spx_trend",
    "alloc_key": "spx_trend_allo",
    "display_name": "S&P-Trend 3x",
    "emoji": "📈",
    "legs": [
        ("SPY.US", "SPXL"),
    ],
    "defensive": "BIL",
    "sma_period": 200,
    "band": 0.01,
    "confirm_days": 1,
    "max_live_jump": 0.20,
    "max_quote_age_minutes": 120,
    "tolerance_amount": 5.0,
}

TREND_SLEEVES = [world_trend_config, spx_trend_config]

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
    "max_single_strategy_pct": 0.50,  # Cap per strategy = max(this, 1.5 × its target) of the monthly contribution
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


def get_alpaca_historical_bars(api, symbol, days=400, raw=False, adjustment="split"):
    """Fetch historical daily bars from Alpaca IEX feed.

    `days` is interpreted as TRADING days. We request ~1.5× as many calendar
    days from Alpaca to account for weekends + holidays, so callers can write
    `days=200` and reliably get back ≥200 bars (when the symbol has history).

    raw=False (default) → list of closing prices (floats).
    raw=True            → list of bar dicts {t, o, h, l, c, v, ...} from Alpaca.
                          Callers that need OHLC + timestamps (ADX, backfill,
                          OHLC-based signals) must request raw=True.
    adjustment="split" (default) liefert Kurse ohne Dividendenbereinigung - so
    rechnen die SMA-Gates und Alerts seit jeher. "all" bereinigt auch
    Ausschuettungen (Total Return); die Rotatoren nutzen das fuer Momentum und
    Vola, weil ihr Backtest auf Total Return steht.
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
        "adjustment": adjustment,
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


def save_balance(strategy, data, env="live", merge=False):
    """
    Save strategy balance to Firestore with environment separation.
    Handles Firestore unavailability gracefully for local testing.
    
    Args:
        strategy: Strategy name (e.g., "aaa")
        data: Either a simple float (invested amount) or dict with multiple fields
        env: Environment ("live" or "paper") - determines Firestore collection
    """
    try:
        # Use environment-specific collection to separate paper/live data
        collection_name = f"strategy-balances-{env}"
        doc_ref = get_firestore_client().collection(collection_name).document(strategy)
        
        # Handle both simple float values and complex dictionaries
        if isinstance(data, dict):
            # merge=True nur fuer Teil-Updates (World-Trend-Tagescheck): set()
            # ohne merge ueberschreibt das ganze Dokument inkl. total_invested.
            doc_ref.set(data, merge=merge)
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


def get_all_strategy_values(api):
    """
    Current market value of every sleeve from Alpaca positions, plus "total".
    Used for contribution rebalancing to see how far each sleeve is from target.
    """
    try:
        positions = {p["symbol"]: float(p["market_value"]) for p in list_positions(api)}
        values = {key: sum(positions.get(sym, 0) for sym in STRATEGY_SYMBOLS[key]) for key in SLEEVES}
        values["total"] = sum(values.values())
        return values
    except Exception as e:
        print(f"Error getting all strategy values: {e}")
        values = {key: 0 for key in SLEEVES}
        values["total"] = 0
        return values


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
    strategy_to_allo_key = {key: allo for key, (allo, _) in SLEEVES.items()}
    
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
    
    # Apply the per-sleeve cap and redistribute excess.
    # The cap needs headroom ABOVE a sleeve's own target, or the tilt can never
    # help that sleeve catch up: at cap = target, an underweight Mix8 (target
    # 50 %) was clipped back to 50 % and its excess flowed to the others. With
    # the old flat 50 % cap it was worse still. Cap = max(50 %, 1.5 x target):
    # 50 % for the 25 % sleeves, 75 % for Mix8.
    caps = {key: min(1.0, max(max_single_pct, 1.5 * strategy_allocations[key]))
            for key in adjusted_allocations_normalized}
    adjusted_allocations = adjusted_allocations_normalized.copy()
    iterations = 0
    max_iterations = 10

    while iterations < max_iterations:
        excess = 0
        strategies_at_cap = []
        strategies_below_cap = []

        for key, val in adjusted_allocations.items():
            if val > caps[key]:
                excess += val - caps[key]
                adjusted_allocations[key] = caps[key]
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
    strategy_display_names = {key: label for key, (_, label) in SLEEVES.items()}
    
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
        allo_key = f"{strategy}_allo"
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


# Non-fractionable tickers on Alpaca — must be traded in whole shares.
# These are typically newer or smaller ETFs that Alpaca hasn't added to
# its fractional list yet. Submitting a fractional order errors with
# Alpaca code 40310000. The 7-Asset Rotator checks this set when sizing
# orders and floors to integer for these tickers.
NON_FRACTIONABLE_TICKERS = {
    "NTSD",   # WisdomTree US Plus Intl — may be non-fractionable, defensive listing
    "WLDU",   # Leverage Shares 2x World — fractionable=False per Alpaca assets API (2026-09-22)
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


# ════════════════════════════════════════════════════════════════════
# AAA FREE 2× + NTSD STRATEGY — 7-asset top-3 momentum rotation
# Universe (signal → held position, all ≤2× per ticker):
#   SPY → NTSD,  IWM → SAA,  EEM → EET,  TLT → UBT,
#   IEF → UST,   GLD → UGL,  DBC → DBC.
# Monthly: 6m momentum rank → top-3 with positive scores → inverse-vol
# weight → vol-target scale → balance to SHV. DD-30 stop on trailing-peak
# NAV. Promoted to deployed 2026-05-12 from research.
# ════════════════════════════════════════════════════════════════════


class RotatorDataError(Exception):
    """Kursdaten fuer einen Rotator fehlen. Wird NIE in einen Default
    abgefangen: fielen alle Abrufe aus und wuerden fehlende Kandidaten einfach
    ausgelassen, landete der Rotator komplett im Defensiv-ETF - eine
    Liquidation wegen eines Datenlochs. Also: Abbruch, keine Trades."""


def _rotator_momentum(api, signal_symbol, lookbacks, weights):
    """Gewichtete Trailing-Rendite ueber mehrere Fenster auf dem ungehebelten
    Signal. Dividendenbereinigt (adjustment="all"): der Backtest rechnet auf
    Total Return, und auf reinen Kursen haette KMLMs Jahresausschuettung vom
    28.12.2022 (-10,5 % an einem Tag) ein Jahr lang als Verlust gezaehlt.

    None = zu wenig Historie (Kandidat wird ausgelassen, wie bisher).
    RotatorDataError = Abruf fehlgeschlagen (Lauf bricht ab)."""
    longest = max(lookbacks)
    bars = get_alpaca_historical_bars(api, signal_symbol, days=max(300, longest + 100),
                                      adjustment="all")
    if bars is None:
        raise RotatorDataError(f"keine Kursdaten fuer {signal_symbol}")
    if len(bars) < longest + 1:
        print(f"Rotator: insufficient bars for {signal_symbol} ({len(bars)} < {longest + 1})")
        return None
    price_now = bars[-1]
    score = 0.0
    for lb, w in zip(lookbacks, weights):
        price_past = bars[-(lb + 1)]
        if price_now <= 0 or price_past <= 0:
            return None
        score += w * (price_now / price_past - 1)
    return score


def _rotator_realized_vol(api, symbol, window=60):
    """60-day annualized realized vol of close-to-close simple returns,
    dividendenbereinigt - sonst blaeht ein Ausschuettungstag die Vola fuer
    60 Tage auf und das Inverse-Vola-Gewicht schrumpft grundlos."""
    bars = get_alpaca_historical_bars(api, symbol, days=max(150, window + 60),
                                      adjustment="all")
    if bars is None or len(bars) < window + 1:
        return None
    rets = [(bars[i + 1] / bars[i]) - 1 for i in range(len(bars) - window - 1, len(bars) - 1) if bars[i] > 0]
    if len(rets) < window // 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    return (var ** 0.5) * (252 ** 0.5)


def get_rotator_position_value(api, cfg):
    """Total value + per-symbol breakdown for one rotator's ticker universe."""
    symbols = STRATEGY_SYMBOLS[cfg["strategy_key"]]
    try:
        positions = list_positions(api)
        by_symbol = {sym: {"value": 0.0, "shares": 0.0} for sym in symbols}
        total_value = 0.0
        for position in positions:
            ticker = position.get("symbol")
            if ticker in symbols:
                value = float(position.get("market_value", 0))
                shares = float(position.get("qty", 0))
                by_symbol[ticker] = {"value": value, "shares": shares}
                total_value += value
        return {"total_value": total_value, "by_symbol": by_symbol}
    except Exception as e:
        print(f"Error getting {cfg['display_name']} position value: {e}")
        return {"total_value": 0.0, "by_symbol": {sym: {"value": 0.0, "shares": 0.0} for sym in symbols}}


def plan_rotator_weights(api, cfg):
    """Signal-Teil eines Monatslaufs ohne Orders: Scores, Top-N, Inverse-Vola
    auf den GEHALTENEN ETF, Vola-Ziel mit gewichteter Summe der Einzelvolas
    (konservativ - unterstellt perfekte Korrelation), Gewichte ueber die Picks
    normiert. Genau diese Logik steckt im Backtest 'Live-Logik'.

    Raises RotatorDataError, wenn ein Signalabruf fehlschlaegt."""
    scores = {}
    for signal_sym, pos_sym in cfg["candidates"]:
        sc = _rotator_momentum(api, signal_sym, cfg["lookbacks"], cfg["lookback_weights"])
        if sc is not None:
            scores[pos_sym] = sc
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    picks = [pos for pos, sc in ranked[: cfg["top_n"]] if sc > cfg["min_score"]]

    weights = {pos: 0.0 for _, pos in cfg["candidates"]}
    realized_vols = {}
    scale = 0.0
    if picks:
        invvols = {}
        for pos in picks:
            v = _rotator_realized_vol(api, pos, window=cfg["vol_window"])
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
    return {"scores": scores, "picks": picks, "weights": weights, "cash_weight": cash_weight,
            "scale": scale, "realized_vols": realized_vols}


def make_monthly_buys_rotator(api, cfg, force_execute=False, investment_calc=None,
                              margin_result=None, skip_order_wait=False, env="live"):
    """
    Monthly execution for a momentum rotator (7-Asset Rotator, Mix8). Each call:
      1. Add this month's contribution to the sleeve's pool.
      2. Compute blended momentum on the unleveraged signal symbols.
      3. Check DD-30 trailing-peak NAV stop — if breached, dump everything to defensive.
      4. Pick top-N positive-momentum signals; if none positive, go defensive.
      5. Inverse-vol weight the picks (using 60d trailing vol of the *held* positions).
      6. Apply portfolio vol-target scale = min(1, target / weighted_vol). Excess to defensive.
      7. Generate buy/sell deltas, sells-first then buys.
      8. Save state and update Firestore.
    """
    name = cfg["display_name"]
    tag = cfg["strategy_key"].upper()
    if not force_execute and not check_trading_day(mode="monthly"):
        return "Not first trading day of the month"
    if force_execute:
        print(f"{tag}: Force execution enabled — bypassing trading day check")

    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)

    symbols = STRATEGY_SYMBOLS[cfg["strategy_key"]]
    alloc_key = cfg["alloc_key"]
    investment_amount = investment_calc["strategy_amounts"].get(alloc_key, 0)
    pct_label = strategy_allocations.get(alloc_key, 0) * 100
    check_date = datetime.datetime.now().strftime("%Y-%m-%d")

    def _skip(reason):
        msg = f"🎛 {name} ({pct_label:.2f}%) — ${investment_amount:,.2f}\n⏭ {reason}"
        send_telegram_message(msg)
        print(reason)
        return reason

    # Gate checks
    gate = _contribution_gate(investment_amount, investment_calc, margin_result)
    if gate:
        return _skip(gate)

    # Load state
    balances = load_balances(env)
    state = balances.get(cfg["strategy_key"], {})
    total_invested = state.get("total_invested", 0)
    peak_nav = float(state.get("peak_nav", 0) or 0)

    # Current sleeve value (before today's contribution)
    value_data = get_rotator_position_value(api, cfg)
    current_value = value_data["total_value"]
    by_symbol = value_data["by_symbol"]
    print(f"{tag} — investment ${investment_amount:.2f}, current value ${current_value:.2f}")

    # DD-30 stop check
    new_peak_nav = max(peak_nav, current_value) if peak_nav > 0 else current_value
    dd = (current_value - new_peak_nav) / new_peak_nav if new_peak_nav > 0 else 0.0
    dd_triggered = peak_nav > 0 and dd < -cfg["dd_threshold"]
    if dd_triggered:
        print(f"  DD-stop TRIGGERED: drawdown {dd:.1%} < -{cfg['dd_threshold']:.0%} — forcing defensive")
        new_peak_nav = current_value  # reset peak

    # Signals (skipped entirely if DD-triggered)
    if dd_triggered:
        plan = {"scores": {}, "picks": [], "weights": {pos: 0.0 for _, pos in cfg["candidates"]},
                "cash_weight": 1.0, "scale": 0.0, "realized_vols": {}}
    else:
        try:
            plan = plan_rotator_weights(api, cfg)
        except RotatorDataError as e:
            send_telegram_message(f"❗ {name}: {e} — Lauf abgebrochen, keine Trades.")
            return f"❌ {name}: {e}"
        print(f"  momentum scores: {plan['scores']}")
    scores, picks, weights = plan["scores"], plan["picks"], plan["weights"]
    cash_weight, scale, realized_vols = plan["cash_weight"], plan["scale"], plan["realized_vols"]
    if picks:
        print(f"  picks: {picks}, inverse-vol scale: {scale:.3f}")

    # Compute target dollar amounts
    total_to_allocate = current_value + investment_amount
    targets = {sym: 0.0 for sym in symbols}
    for pos, w in weights.items():
        if pos in targets:
            targets[pos] = w * total_to_allocate
    targets[cfg["defensive"]] = cash_weight * total_to_allocate

    print(f"  target $: {{ {', '.join(f'{s}: ${v:,.2f}' for s, v in targets.items() if v > 0)} }}")

    # Fetch prices for any symbols we'll trade
    prices = {}
    for sym in symbols:
        if targets[sym] > 0 or by_symbol[sym]["value"] > cfg["tolerance_amount"]:
            try:
                prices[sym] = float(get_latest_trade(api, sym))
            except Exception as e:
                send_telegram_message(f"🎛 {name}\n❌ Failed to fetch price for {sym}: {e}")
                return f"{tag}: failed to fetch price for {sym}: {e}"

    current_dollars = {sym: by_symbol[sym]["value"] for sym in symbols}
    deltas = {sym: targets[sym] - current_dollars[sym] for sym in symbols}
    trades_info = []

    # Sells first (negative deltas)
    for sym in symbols:
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
            return f"{tag}: failed to sell {sym}: {e}"

    # Buys (positive deltas)
    for sym in symbols:
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
            return f"{tag}: failed to buy {sym}: {e}"

    if not trades_info:
        trades_info.append("No trades needed; targets already aligned.")

    # Persist state
    final_value_data = get_rotator_position_value(api, cfg)
    final_total = final_value_data["total_value"]
    final_by_symbol = final_value_data["by_symbol"]
    final_peak_nav = max(new_peak_nav, final_total)
    final_total_invested = total_invested + investment_amount
    strategy_return = (final_total / final_total_invested - 1) if final_total_invested > 0 else 0
    save_balance(cfg["strategy_key"], {
        "total_invested": final_total_invested,
        "current_positions": {sym: final_by_symbol[sym]["shares"] for sym in symbols},
        "current_values": {sym: final_by_symbol[sym]["value"] for sym in symbols},
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
    msg += f"{cfg['score_label']} scores: {scores_str}\n"
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

    return f"{tag} monthly complete. Picks: {picks}. Value ${final_total:,.2f}, return {strategy_return:.2%}"


def make_monthly_buys_aaa(api, force_execute=False, investment_calc=None,
                           margin_result=None, skip_order_wait=False, env="live"):
    """7-Asset Rotator (AAA family) monthly execution — see make_monthly_buys_rotator."""
    return make_monthly_buys_rotator(api, aaa_config, force_execute, investment_calc,
                                     margin_result, skip_order_wait, env)


def make_monthly_buys_mix8(api, force_execute=False, investment_calc=None,
                           margin_result=None, skip_order_wait=False, env="live"):
    """Mix8 Top-2 monthly execution — see make_monthly_buys_rotator."""
    return make_monthly_buys_rotator(api, mix8_config, force_execute, investment_calc,
                                     margin_result, skip_order_wait, env)


def _contribution_gate(investment_amount, investment_calc, margin_result):
    """Gemeinsame Monats-Gates der Sleeves. Liefert den Skip-Grund oder None."""
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    buying_power = investment_calc["total_available"] + investment_calc["margin_approved"]
    if target_margin == 0 and leverage > 1.0:
        return f"Skipped — deleveraging required ({leverage:.2f}x)"
    if buying_power < investment_amount:
        return f"Skipped — insufficient buying power (${buying_power:,.2f})"
    if investment_amount < margin_control_config["min_investment"]:
        return f"Skipped — ${investment_amount:.2f} below $1.00 minimum"
    if target_margin > 0:
        pv = metrics.get("portfolio_value", 0)
        equity = metrics.get("equity", 0)
        if pv > 0 and equity > 0:
            projected_leverage = (pv + investment_amount) / equity
            if projected_leverage >= margin_control_config["max_leverage"]:
                return f"Skipped — projected leverage {projected_leverage:.3f}x exceeds limit"
    return None


def _wt_leg_state(closes, sma_period, band, confirm):
    """Trendzustand eines Beins. Dieselbe Zustandsmaschine wie der Backtest:
    ueber dem Band zaehlt `up`, darunter `dn`, im Band werden beide
    zurueckgesetzt; `confirm` Tage in Folge schalten um. Sie laeuft ueber die
    ganze geladene Historie, damit der Zustand auch ohne gespeicherten Vortag
    stimmt - ein verpasster Lauf verfaelscht nichts."""
    if len(closes) < sma_period:
        raise EodhdDataError(f"nur {len(closes)} Kurse, SMA{sma_period} braucht {sma_period}")
    state, up, dn, sma = False, 0, 0, None
    for t in range(sma_period - 1, len(closes)):
        sma = sum(closes[t - sma_period + 1:t + 1]) / sma_period
        p = closes[t]
        if p > sma * (1 + band):
            up += 1
            dn = 0
        elif p < sma * (1 - band):
            dn += 1
            up = 0
        else:
            up = dn = 0
        if up >= confirm:
            state = True
        if dn >= confirm:
            state = False
    return {"on": state, "price": closes[-1], "sma": sma, "up": up, "dn": dn,
            "diff_pct": (closes[-1] / sma - 1) * 100}


def _wt_signal_closes(index_symbol, cfg, today_iso):
    """Schlusskurse vor heute (EODHD, Total Return) plus heutiger Live-Kurs."""
    hist = fetch_eodhd_eod_series(index_symbol, calendar_days=900)
    past = [(d, c) for d, c in hist if d < today_iso]
    if not past:
        raise EodhdDataError(f"{index_symbol}: keine Schlusskurse vor {today_iso}")
    newest_date, newest_close = past[-1]
    gap = (datetime.date.fromisoformat(today_iso)
           - datetime.date.fromisoformat(newest_date)).days
    if gap > MAX_EOD_GAP_DAYS:
        raise EodhdDataError(f"{index_symbol}: neuester Schluss ist {gap} Tage alt ({newest_date})")
    live, ts = fetch_eodhd_realtime(index_symbol)
    age_min = (datetime.datetime.utcnow() - ts).total_seconds() / 60
    if age_min > cfg["max_quote_age_minutes"]:
        raise EodhdDataError(f"{index_symbol}: Live-Kurs ist {age_min:.0f} min alt")
    if abs(live / newest_close - 1) > cfg["max_live_jump"]:
        raise EodhdDataError(
            f"{index_symbol}: Live-Kurs {live:.2f} weicht {(live / newest_close - 1) * 100:+.1f} % "
            f"vom letzten Schluss {newest_close:.2f} ({newest_date}) ab")
    return [c for _, c in past] + [live], {"last_close_date": newest_date,
                                             "live_ts": ts.isoformat() + "Z"}


def trend_signals(cfg, today_iso=None):
    """Zustand je Bein einer Trend-Sleeve, keyed by Produkt-Ticker.
    Raises EodhdDataError."""
    today_iso = today_iso or _heute_iso()
    out = {}
    for index_symbol, product in cfg["legs"]:
        closes, meta = _wt_signal_closes(index_symbol, cfg, today_iso)
        leg = _wt_leg_state(closes, cfg["sma_period"], cfg["band"], cfg["confirm_days"])
        leg.update(meta)
        leg["index"] = index_symbol
        out[product] = leg
    return out


def world_trend_target_weights(cfg, signals):
    """Jedes aktive Bein 1/n, der Rest im Defensiv-ETF."""
    n = len(cfg["legs"])
    w = {product: (1.0 / n if signals[product]["on"] else 0.0) for _, product in cfg["legs"]}
    w[cfg["defensive"]] = 1.0 - sum(w.values())
    return w


def _wt_buy(api, cfg, symbol, dollars, price, skip_order_wait, trades):
    shares, note = _size_buy_order(symbol, dollars, price)
    if shares <= 0 or shares * price < margin_control_config["min_investment"]:
        if note:
            trades.append(f"⏭ {symbol}: {note}")
        return 0.0
    order = submit_order(api, symbol, shares, "buy")
    if not skip_order_wait:
        wait_for_order_fill(api, order["id"])
    spent = shares * price
    trades.append(f"Bought {shares:.4f} {symbol} @ ${price:.2f} (${spent:,.2f})" + (f" [{note}]" if note else ""))
    return spent


def _wt_trade_to_targets(api, cfg, target_w, skip_order_wait=False):
    """Stellt die Sleeve auf `target_w` ihres EIGENEN Werts um. Verkauft zuerst;
    Kaeufe sind auf die eigenen Verkaufserloese begrenzt. Eine Trend-Sleeve
    greift nie auf Konto-Cash zu - der Fehler des alten SPXL-Tagesjobs, der ein
    SGOV-Polster automatisch zurueck in SPXL tauschte."""
    symbols = STRATEGY_SYMBOLS[cfg["strategy_key"]]
    vd = get_rotator_position_value(api, cfg)
    total, by = vd["total_value"], vd["by_symbol"]
    targets = {s: target_w.get(s, 0.0) * total for s in symbols}
    prices = {s: float(get_latest_trade(api, s)) for s in symbols
              if targets[s] > 0 or by[s]["value"] > cfg["tolerance_amount"]}
    trades, cash = [], 0.0
    for s in symbols:                                    # 1) Verkaeufe
        delta = targets[s] - by[s]["value"]
        if delta >= -cfg["tolerance_amount"] or s not in prices:
            continue
        shares = _size_sell_order(s, min(by[s]["shares"], -delta / prices[s]))
        if shares <= 0:
            continue
        order = submit_order(api, s, shares, "sell")
        filled = None if skip_order_wait else wait_for_order_fill(api, order["id"])
        proceeds = filled if filled else shares * prices[s]
        cash += proceeds
        trades.append(f"Sold {shares:.4f} {s} (${proceeds:,.2f})")
    for _, s in cfg["legs"]:                             # 2) Risiko-Beine
        delta = targets[s] - by[s]["value"]
        if delta > cfg["tolerance_amount"] and s in prices:
            cash -= _wt_buy(api, cfg, s, min(delta, cash), prices[s], skip_order_wait, trades)
    d = cfg["defensive"]                                 # 3) Rest inkl. Rundungsrest
    if cash > cfg["tolerance_amount"]:
        price = prices.get(d) or float(get_latest_trade(api, d))
        _wt_buy(api, cfg, d, cash, price, skip_order_wait, trades)
    return trades


def _wt_signal_summary(cfg, signals):
    lines = []
    for index_symbol, product in cfg["legs"]:
        sg = signals[product]
        lines.append(f"{'🟢' if sg['on'] else '🔴'} {product} ({index_symbol.split('.')[0]}): "
                     f"{sg['price']:.2f} vs SMA{cfg['sma_period']} {sg['sma']:.2f} ({sg['diff_pct']:+.1f} %)")
    return "\n".join(lines)


def daily_trend_sleeve(api, cfg, env="live", force=False, skip_order_wait=False):
    """Taeglicher Check einer Trend-Sleeve. Handelt nur, wenn ein Bein
    umschaltet oder der Bestand nicht zum Signal passt - nicht auf Drift,
    genau wie der Backtest."""
    name = cfg["display_name"]
    if not force and not check_trading_day(mode="daily"):
        print(f"{name}: market closed today")
        return "Market closed today."
    try:
        signals = trend_signals(cfg)
    except EodhdDataError as e:
        send_telegram_message(f"❗ {name}: {e} — kein Signal, keine Trades.")
        return f"❌ {name}: {e}"
    target_w = world_trend_target_weights(cfg, signals)
    states = {p: bool(signals[p]["on"]) for _, p in cfg["legs"]}
    stored = load_balances(env).get(cfg["strategy_key"], {}).get("leg_states")
    vd = get_rotator_position_value(api, cfg)
    total = vd["total_value"]
    trades, reason = [], None
    if total >= margin_control_config["min_investment"]:
        cur = {s: vd["by_symbol"][s]["value"] / total for s in STRATEGY_SYMBOLS[cfg["strategy_key"]]}
        flipped = stored is not None and any(stored.get(p) != states[p] for p in states)
        mismatch = any((target_w[p] > 0 and cur[p] < 0.05) or (target_w[p] == 0 and cur[p] > 0.05)
                       for _, p in cfg["legs"])
        if flipped or mismatch:
            reason = "Signalwechsel" if flipped else "Bestand passt nicht zum Signal"
            trades = _wt_trade_to_targets(api, cfg, target_w, skip_order_wait)
    save_balance(cfg["strategy_key"], {
        "leg_states": states,
        "last_signal": {p: {k: signals[p][k] for k in ("on", "price", "sma", "diff_pct", "up", "dn", "last_close_date")}
                        for p in states},
        "last_signal_check_date": datetime.datetime.now().strftime("%Y-%m-%d"),
    }, env, merge=True)
    if trades:
        msg = f"{cfg['emoji']} {name} — {reason}\n\n{_wt_signal_summary(cfg, signals)}\n\n" + "\n".join(trades)
        send_telegram_message(msg)
        return f"{name}: {reason}, {len(trades)} trades"
    legs_str = ", ".join(p + (" on" if v else " off") for p, v in states.items())
    return f"{name}: no change ({legs_str})"


def daily_world_trend(api, env="live", force=False, skip_order_wait=False):
    return daily_trend_sleeve(api, world_trend_config, env, force, skip_order_wait)


def daily_trend_sleeves(api, env="live", force=False, skip_order_wait=False):
    """Alle Trend-Sleeves nacheinander. Ein Datenfehler in einer blockiert die
    andere nicht; das Gesamtergebnis ist ❌, sobald eine gescheitert ist."""
    results = []
    for cfg in TREND_SLEEVES:
        try:
            results.append(daily_trend_sleeve(api, cfg, env, force, skip_order_wait))
        except Exception as e:
            send_telegram_message(f"❗ {cfg['display_name']}: {e}")
            results.append(f"❌ {cfg['display_name']}: {e}")
    failed = [r for r in results if str(r).startswith("❌")]
    return ("❌ " if failed else "") + " | ".join(str(r) for r in results)


def make_monthly_buys_trend(api, cfg, force_execute=False, investment_calc=None,
                            margin_result=None, skip_order_wait=False, env="live"):
    """Monatszufuehrung einer Trend-Sleeve: der Beitrag geht nach dem aktuellen
    Signal auf die Beine, ohne den Bestand umzuschichten (das tut nur der
    Tagesjob bei Signalwechsel)."""
    name = cfg["display_name"]
    if not force_execute and not check_trading_day(mode="monthly"):
        return "Not first trading day of the month"
    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)
    amount = investment_calc["strategy_amounts"].get(cfg["alloc_key"], 0)
    pct_label = strategy_allocations.get(cfg["alloc_key"], 0) * 100
    reason = _contribution_gate(amount, investment_calc, margin_result)
    if reason:
        send_telegram_message(f"{cfg['emoji']} {name} ({pct_label:.2f}%) — ${amount:,.2f}\n⏭ {reason}")
        return reason
    try:
        signals = trend_signals(cfg)
    except EodhdDataError as e:
        send_telegram_message(f"❗ {name}: {e} — Beitrag nicht investiert.")
        return f"❌ {name}: {e}"
    target_w = world_trend_target_weights(cfg, signals)
    trades, cash = [], amount
    for _, s in cfg["legs"]:
        if target_w[s] > 0:
            cash -= _wt_buy(api, cfg, s, target_w[s] * amount, float(get_latest_trade(api, s)),
                            skip_order_wait, trades)
    d = cfg["defensive"]
    if cash > cfg["tolerance_amount"]:
        _wt_buy(api, cfg, d, cash, float(get_latest_trade(api, d)), skip_order_wait, trades)
    state = load_balances(env).get(cfg["strategy_key"], {})
    vd = get_rotator_position_value(api, cfg)
    symbols = STRATEGY_SYMBOLS[cfg["strategy_key"]]
    total_invested = float(state.get("total_invested", 0) or 0) + amount
    save_balance(cfg["strategy_key"], {
        "total_invested": total_invested,
        "leg_states": {p: bool(signals[p]["on"]) for _, p in cfg["legs"]},
        "current_positions": {s: vd["by_symbol"][s]["shares"] for s in symbols},
        "current_values": {s: vd["by_symbol"][s]["value"] for s in symbols},
        "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
    }, env, merge=True)
    msg = (f"{cfg['emoji']} {name} ({pct_label:.2f}%) — ${amount:,.2f}\n\n{_wt_signal_summary(cfg, signals)}\n\n"
           + "\n".join(trades or ["No trades."]) + f"\n\nCurrent value: ${vd['total_value']:,.2f}")
    send_telegram_message(msg)
    return f"{name} monthly complete. Value ${vd['total_value']:,.2f}"


def make_monthly_buys_world_trend(api, force_execute=False, investment_calc=None,
                                  margin_result=None, skip_order_wait=False, env="live"):
    return make_monthly_buys_trend(api, world_trend_config, force_execute, investment_calc,
                                   margin_result, skip_order_wait, env)


def make_monthly_buys_spx_trend(api, force_execute=False, investment_calc=None,
                                margin_result=None, skip_order_wait=False, env="live"):
    return make_monthly_buys_trend(api, spx_trend_config, force_execute, investment_calc,
                                   margin_result, skip_order_wait, env)


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
    Orchestrator function that runs every active monthly investment strategy.
    Calculates budgets ONCE and distributes them to ensure exact percentage splits.
    
    This prevents the problem of each function independently calculating and over-spending.
    
    Args:
        api: Alpaca API credentials
        force_execute: Bypass trading day check for testing
    
    Returns:
        dict with results from every active strategy
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
    for _key, (_allo, _label) in SLEEVES.items():
        print(f"  {_label} ({get_pct(_allo):.1f}%): ${strategy_amounts[_allo]:.2f}")
    
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
    _labels = {allo: label for allo, label in SLEEVES.values()}
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

    _runners = {
        "aaa": make_monthly_buys_aaa,
        "world_trend": make_monthly_buys_world_trend,
        "mix8": make_monthly_buys_mix8,
        "spx_trend": make_monthly_buys_spx_trend,
    }
    for _key, (_allo, _label) in SLEEVES.items():
        _fn = _runners[_key]
        _run(_key, _label, lambda _fn=_fn: _fn(api, force_execute=force_execute, investment_calc=investment_calc,
                                              margin_result=margin_result, skip_order_wait=skip_order_wait, env=env))

    print("\n=== All Monthly Strategies Complete ===")

    # Send a summary so a missing strategy is impossible to overlook
    summary_lines = ["📋 Monthly Orchestrator Summary"]
    label_map = {key: label for key, (_, label) in SLEEVES.items()}
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


@app.route("/monthly_buy_mix8", methods=["POST"])
def monthly_buy_mix8(request):
    """Manual/debug entry point — the orchestrator runs Mix8 in-process."""
    api = set_alpaca_environment(env=alpaca_environment)
    return make_monthly_buys_mix8(api, env=alpaca_environment)


@app.route("/daily_trend_sleeves", methods=["POST"])
def daily_trend_sleeves_route(request):
    """Taeglicher Check aller Trend-Sleeves (Scheduler 15:50 ET). Datenfehler => 500."""
    api = set_alpaca_environment(env=alpaca_environment)
    result = daily_trend_sleeves(api, env=alpaca_environment)
    return result, (500 if str(result).startswith("❌") else 200)

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

    expected_symbols = {label: STRATEGY_SYMBOLS[key] for key, (_, label) in SLEEVES.items()}

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
    elif action == "monthly_buy_mix8":
        return make_monthly_buys_mix8(api, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "monthly_buy_world_trend":
        return make_monthly_buys_world_trend(api, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "monthly_buy_spx_trend":
        return make_monthly_buys_spx_trend(api, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "daily_trend_sleeves":
        return daily_trend_sleeves(api, env=env, force=force_execute)
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
            "index_alert",
            "monthly_buy_aaa",
            "monthly_buy_mix8",
            "monthly_buy_world_trend",
            "monthly_buy_spx_trend",
            "daily_trend_sleeves",
        ],
        required=True,
        help="Action to perform: 'monthly_invest_all' runs all monthly strategies with coordinated budgets (recommended)",
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
