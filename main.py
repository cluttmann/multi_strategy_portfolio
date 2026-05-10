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
    "hfea_allo": 0.15,             # 15% to HFEA
    "spxl_allo": 0.15,             # 15% to SPXL SMA
    "rssb_wtip_allo": 0.10,        # 10% to RSSB/WTIP (macro hedge sleeve)
    "nine_sig_allo": 0.05,         # 5% to 9-Sig (high-DD sleeve, kept small)
    "dual_momentum_allo": 0.20,    # 20% to Dual Momentum (best-of-3 SPUU/QLD/EFO)
    "regime_sso_allo": 0.15,       # 15% to SSO/USFR regime detector (US)
    "regime_world_allo": 0.20,     # 20% to WLDU/USFR regime detector (global)
}

upro_allocation = 0.45
tmf_allocation = 0.25
kmlm_allocation = 0.3

# RSSB/WTIP allocation (70/30 — moved from 80/20 after 2026-05 backtest;
# see commit message for the rigor work behind the choice)
rssb_allocation = 0.70
wtip_allocation = 0.30

# RSSB/WTIP holding fund config (for accumulating funds when WTIP can't be bought)
rssb_wtip_holding_fund = "BIL"
rssb_wtip_holding_fund_max = 70.0  # $70 maximum

# SPXL SMA holding fund config (for T-bills when SPY < 200-SMA)
spxl_sma_holding_fund = "SGOV"  # iShares 0-3 Month Treasury Bond ETF

# Strategy Ticker Ownership
# Each strategy has clear ticker ownership for simplified margin calculations and position tracking:
# - HFEA: UPRO, TMF, KMLM
# - SPXL SMA: SPXL, SGOV (SGOV is holding fund when bearish)
# - RSSB/WTIP: RSSB, WTIP, BIL (BIL is holding fund for uninvested WTIP amounts)
# - 9-Sig: TQQQ, AGG
# - Dual Momentum: SPUU, QLD, EFO, BND (BND is defensive + vol-target overflow)
# - Regime SSO: SSO (when in market), USFR (when defensive — floating-rate Treasury)
# - Regime World: WLDU (when in market), USFR (when defensive)

# Strategy ticker ownership mapping for cost basis recalculation
STRATEGY_SYMBOLS = {
    "hfea": ["UPRO", "TMF", "KMLM"],
    "spxl_sma": ["SPXL", "SGOV"],
    "rssb_wtip": ["RSSB", "WTIP", "BIL"],
    "nine_sig": ["TQQQ", "AGG"],
    "dual_momentum": ["SPUU", "QLD", "EFO", "BND"],
    "regime_sso": ["SSO", "USFR"],
    "regime_world": ["WLDU", "USFR"],
}

alpaca_environment = "live"
margin = 0.01  # band around the 200sma to avoid too many trades

# 9-sig strategy configuration following Jason Kelly's methodology
nine_sig_config = {
    "target_allocation": {"tqqq": 0.8, "agg": 0.2},  # 80/20 target allocation
    "quarterly_growth_rate": 0.09,  # 9% quarterly growth target
    "bond_rebalance_threshold": 0.30,  # Rebalance when AGG > 30%
    "tolerance_amount": 25,  # Minimum trade amount to avoid tiny trades
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

# Regime detection (SSO/SHV) configuration
# Composite score from 7 macro signals; rotates between SSO (2x S&P 500) and SHV (T-bills)
# based on Reddit /r/LETFs methodology by u/Neat_Bug1775. Each signal contributes -1/0/+1
# to the composite (range roughly -7..+7). Designed for ~1.4 executions/year — intentionally
# slow and noise-resistant.
regime_sso_config = {
    # Identity
    "strategy_key": "regime_sso",       # Firestore doc id under strategy-balances-{env}
    "alloc_key": "regime_sso_allo",     # Key in strategy_allocations
    "scores_collection": "regime-scores",  # Firestore collection for daily score history
    "display_name": "regime_sso",       # Human label
    # Universe
    "risk_asset": "SSO",                # 2x leveraged S&P 500
    "safe_asset": "USFR",               # WisdomTree Floating Rate Treasury (cash-like, no duration risk)
    "trend_symbol": "SPY",              # Signal 1 + Signal 4 base symbol
    "spy_sma_period": 200,              # Signal 1: SPY 200-SMA
    "sma_hysteresis_days": 3,           # 3-day confirmation to filter whipsaws
    "breadth_mode": "sp500",            # Signal 2: full S&P 500 constituents
    "breadth_sma_period": 50,
    "breadth_high_threshold": 0.60,     # > 60% bullish
    "breadth_low_threshold": 0.40,      # < 40% bearish
    "vix_low": 18.0,                    # Signal 3: VIX < 18 = calm
    "vix_high": 25.0,                   # VIX > 25 = stress
    "adx_period": 14,                   # Signal 4: ADX
    "adx_strong": 25.0,                 # ADX > 25 = strong trend
    "credit_sma_period": 50,            # Signal 5: HYG/LQD ratio vs its 50-SMA
    "canary_sma_period": 50,            # Signal 7: HYG/EEM/IWM vs 50-SMA
    "news_lookback_hours": 24,          # Signal 6: 24h of news
    "news_min_articles": 20,
    "news_pos_threshold": 0.10,
    "news_neg_threshold": -0.10,
    "news_tickers": None,               # None = use Alpaca news firehose (US-centric); list = filter to symbols
    "fed_hike_lookback_days": 90,       # Fed-policy filter window
    "fed_hike_threshold_bps": 50,
    # Exit thresholds
    "slow_exit_days": 15,
    "slow_exit_score": 0,
    "fast_exit_days": 3,
    "fast_exit_score": -3,
    # Re-entry thresholds (three independent paths, fastest wins)
    "credit_vix_recovery_weeks": 4,
    "credit_vix_credit_improvement": 0.005,
    "credit_vix_vix_decline": 0.05,
    "nlp_acceleration_score_days": 7,
    "nlp_acceleration_sentiment_weeks": 2,
    "nlp_confidence_threshold": 0.80,
    "standard_reentry_days": 15,
    "reentry_score": 3,
    "max_signal_failures_before_alert": 3,
}


# Regime World (WLDU/USFR) configuration — mirrors regime_sso for global markets.
# Trend signal uses URTH 255-SMA. Breadth uses a curated ex-US country/region ETF
# basket. News filters Alpaca news to global equity ETF tickers. VIX/credit/canary/
# Fed filter remain universal indicators (no point splitting them by geography).
GLOBAL_BREADTH_BASKET = [
    "EFA",  # MSCI EAFE
    "EEM",  # Emerging markets
    "VWO",  # Emerging Vanguard
    "EWG",  # Germany
    "EWU",  # UK
    "EWJ",  # Japan
    "EWQ",  # France
    "EWY",  # South Korea
    "EWA",  # Australia
    "EWC",  # Canada
    "EWZ",  # Brazil
    "INDA", # India
    "MCHI", # China
    "EWH",  # Hong Kong
    "EWT",  # Taiwan
]
GLOBAL_NEWS_TICKERS = ["URTH", "EFA", "EEM", "VWO", "VEA", "ACWI", "IEFA"]

regime_world_config = {
    # Identity
    "strategy_key": "regime_world",
    "alloc_key": "regime_world_allo",
    "scores_collection": "regime-world-scores",
    "display_name": "regime_world",
    # Universe
    "risk_asset": "WLDU",               # 2x MSCI World (Leverage Shares, live since 2026-03-12)
    "safe_asset": "USFR",               # Same defensive as regime_sso
    "trend_symbol": "URTH",             # iShares MSCI World ETF — 1× world index proxy
    "spy_sma_period": 255,              # Signal 1: URTH 255-SMA (longer window for global)
    "sma_hysteresis_days": 3,
    "breadth_mode": "basket",           # Signal 2: % of GLOBAL_BREADTH_BASKET above their 50-SMA
    "breadth_basket": GLOBAL_BREADTH_BASKET,
    "breadth_sma_period": 50,
    "breadth_high_threshold": 0.60,
    "breadth_low_threshold": 0.40,
    "vix_low": 18.0,
    "vix_high": 25.0,
    "adx_period": 14,
    "adx_strong": 25.0,
    "credit_sma_period": 50,
    "canary_sma_period": 50,
    "news_lookback_hours": 24,
    "news_min_articles": 15,            # Slightly relaxed — global news volume thinner than US
    "news_pos_threshold": 0.10,
    "news_neg_threshold": -0.10,
    "news_tickers": GLOBAL_NEWS_TICKERS, # Filter Alpaca news to these symbols
    "fed_hike_lookback_days": 90,
    "fed_hike_threshold_bps": 50,
    "slow_exit_days": 15,
    "slow_exit_score": 0,
    "fast_exit_days": 3,
    "fast_exit_score": -3,
    "credit_vix_recovery_weeks": 4,
    "credit_vix_credit_improvement": 0.005,
    "credit_vix_vix_decline": 0.05,
    "nlp_acceleration_score_days": 7,
    "nlp_acceleration_sentiment_weeks": 2,
    "nlp_confidence_threshold": 0.80,
    "standard_reentry_days": 15,
    "reentry_score": 3,
    "max_signal_failures_before_alert": 3,
}

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


def get_telegram_secrets():
    return (
        get_secret_or_env("TELEGRAM_KEY"),
        get_secret_or_env("TELEGRAM_CHAT_ID"),
    )


def get_fred_rate():
    """Fetch the current Federal Funds Target Rate (Upper Limit) from FRED API."""
    try:
        fred_key = get_secret_or_env("FREDKEY")
        if not fred_key:
            print("FRED API key not found")
            return None
        
        # Fetch DFEDTARU (Federal Funds Target Rate - Upper Limit)
        url = f"https://api.stlouisfed.org/fred/series/observations?series_id=DFEDTARU&api_key={fred_key}&file_type=json&sort_order=desc&limit=1"
        
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        if "observations" in data and len(data["observations"]) > 0:
            # Get the most recent observation value
            rate_value = data["observations"][0]["value"]
            
            # Handle '.' (missing data) or other non-numeric values
            if rate_value == "." or rate_value is None:
                print("FRED API returned missing data")
                return None
            
            # Convert to float and return as decimal (FRED returns percentage, e.g., 5.25)
            return float(rate_value) / 100.0
        else:
            print("No FRED data available")
            return None
            
    except Exception as e:
        print(f"Error fetching FRED rate: {e}")
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


def mark_monthly_run_complete(env="live"):
    """Record that the monthly orchestrator finished, so the watchdog can verify."""
    try:
        month_id = current_month_id()
        get_firestore_client().collection(f"monthly-runs-{env}").document(month_id).set(
            {
                "month_id": month_id,
                "timestamp": datetime.datetime.utcnow(),
            }
        )
    except Exception as e:
        print(f"Warning: could not write monthly-runs marker: {e}")


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


# 9-Sig Strategy Data Management Functions
def save_nine_sig_quarterly_data(quarter_id, tqqq_balance, agg_balance, signal_line, action, quarterly_contributions, env="live"):
    """
    Save quarterly data following 3Sig methodology for next quarter's calculations.
    
    Args:
        quarter_id: Quarter identifier
        tqqq_balance: TQQQ balance at quarter end
        agg_balance: AGG balance at quarter end
        signal_line: Signal line value
        action: Action taken
        quarterly_contributions: Total quarterly contributions
        env: Environment ("live" or "paper") - determines Firestore collection
    """
    doc_ref = get_firestore_client().collection(f"nine-sig-quarters-{env}").document(quarter_id)
    doc_ref.set({
        "quarter_id": quarter_id,
        "quarter_end_date": datetime.datetime.now().isoformat(),
        "previous_tqqq_balance": tqqq_balance,
        "agg_balance": agg_balance,
        "signal_line": signal_line,
        "action_taken": action,
        "quarterly_contributions": quarterly_contributions,
        "total_portfolio": tqqq_balance + agg_balance,
        "timestamp": datetime.datetime.utcnow()
    })


def get_previous_quarter_tqqq_balance(env="live"):
    """
    Get previous quarter's TQQQ ending balance for signal line calculation.
    
    Args:
        env: Environment ("live" or "paper") - determines Firestore collection
    
    Returns:
        Previous quarter's TQQQ balance or 0 if not found
    """
    docs = get_firestore_client().collection(f"nine-sig-quarters-{env}").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(1).stream()
    for doc in docs:
        data = doc.to_dict()
        return data.get("previous_tqqq_balance", 0)
    return 0


def track_nine_sig_monthly_contribution(amount, env="live"):
    """
    Track actual 9-Sig monthly contribution for quarterly signal calculation.
    Handles Firestore unavailability gracefully for local testing.
    
    Args:
        amount: Contribution amount
        env: Environment ("live" or "paper") - determines Firestore collection
    """
    try:
        current_month = datetime.datetime.now().strftime("%Y-%m")
        doc_ref = get_firestore_client().collection(f"nine-sig-monthly-contributions-{env}").document(current_month)
        doc_ref.set({
            "month": current_month,
            "amount": amount,
            "timestamp": datetime.datetime.utcnow()
        })
    except Exception as e:
        print(f"Warning: Could not track 9-Sig contribution to Firestore: {e}")


def get_quarterly_nine_sig_contributions(env="live"):
    """
    Get sum of actual 9-Sig contributions made in the current quarter.
    Returns 0 if Firestore is unavailable (local testing).
    
    Args:
        env: Environment ("live" or "paper") - determines Firestore collection
    
    Returns:
        Total quarterly contributions or 0 if unavailable
    """
    try:
        today = datetime.datetime.now()
        
        # Determine current quarter's start month
        quarter_start_month = ((today.month - 1) // 3) * 3 + 1
        quarter_start = datetime.datetime(today.year, quarter_start_month, 1)
        
        # Get all monthly contributions from this quarter
        docs = get_firestore_client().collection(f"nine-sig-monthly-contributions-{env}").where(
            "timestamp", ">=", quarter_start
        ).stream()
        
        total_contributions = sum(doc.to_dict().get("amount", 0) for doc in docs)
        return total_contributions
    except Exception as e:
        print(f"Warning: Could not load 9-Sig quarterly contributions from Firestore: {e}")
        return 0  # Return 0 for local testing without Firestore


def check_spy_30_down_rule():
    """Check if SPY has dropped 30% from its 2-year all-time high."""
    try:
        api = set_alpaca_environment(env=alpaca_environment)
        bars = get_alpaca_historical_bars(api, "SPY", days=730)

        if not bars or len(bars) < 10:
            print(f"Insufficient SPY data for 30-down rule")
            return False

        # bars are closing prices; use max as proxy for ATH
        all_time_high = max(bars)
        current_close = bars[-1]
        drop_percentage = (all_time_high - current_close) / all_time_high
        return drop_percentage >= 0.30

    except Exception as e:
        print(f"Error checking SPY 30 down rule: {e}")
        return False


def count_ignored_sell_signals(env="live"):
    """
    Count how many sell signals have been ignored in the current crash protection period.
    
    Args:
        env: Environment ("live" or "paper") - determines Firestore collection
    
    Returns:
        Number of ignored sell signals (0-4)
    """
    try:
        # Get recent quarters with ignored sell signals
        docs = get_firestore_client().collection(f"nine-sig-quarters-{env}").where("action_taken", "==", "SELL_IGNORED").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(4).stream()
        return len(list(docs))
    except Exception as e:
        print(f"Error counting ignored sell signals: {e}")
        return 0


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


def get_nine_sig_positions(api):
    return get_strategy_positions(api, STRATEGY_SYMBOLS["nine_sig"], "9-Sig")


def sync_nine_sig_positions_from_alpaca(api, env="live"):
    """
    Sync 9-Sig positions from Alpaca to Firestore.
    This ensures Firestore data matches actual positions in Alpaca.
    
    Args:
        api: Alpaca API credentials dict
        env: Environment ("live" or "paper") - determines Firestore collection
    
    Returns:
        dict: Updated positions dictionary with current_agg_shares
    """
    try:
        # Get actual positions from Alpaca
        actual_positions = get_nine_sig_positions(api)
        
        if not actual_positions:
            print("Warning: No 9-Sig positions found in Alpaca, cannot sync")
            return {}
        
        # Load existing Firestore data
        balances = load_balances(env)
        nine_sig_data = balances.get("nine_sig", {})
        
        # Update positions - AGG shares is the key field for 9-Sig
        agg_shares = actual_positions.get("AGG", 0)
        tqqq_shares = actual_positions.get("TQQQ", 0)
        
        # Update Firestore data while preserving other fields
        nine_sig_data["current_agg_shares"] = agg_shares
        nine_sig_data["current_tqqq_shares"] = tqqq_shares
        nine_sig_data["current_positions"] = actual_positions
        nine_sig_data["last_sync_date"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Save to Firestore
        save_balance("nine_sig", nine_sig_data, env)
        
        print(f"Synced 9-Sig positions from Alpaca to Firestore: AGG={agg_shares:.6f}, TQQQ={tqqq_shares:.6f}")
        return {"AGG": agg_shares, "TQQQ": tqqq_shares}
        
    except Exception as e:
        print(f"Error syncing 9-Sig positions from Alpaca: {e}")
        return {}


def make_monthly_nine_sig_contributions(api, force_execute=False, investment_calc=None, margin_result=None, skip_order_wait=False, env="live"):
    """
    Monthly contributions go ONLY to AGG (bonds) - Following 3Sig Rule.
    Includes margin-aware logic with dynamic investment amounts and All-or-Nothing approach.
    Sends exactly one Telegram message at the end summarizing the outcome.
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        print("Not first trading day of the month")
        return "Not first trading day of the month"
    
    if force_execute:
        print("9-Sig: Force execution enabled - bypassing trading day check")
    
    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)
    
    investment_amount = investment_calc["strategy_amounts"]["nine_sig_allo"]
    
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    buying_power = investment_calc["total_available"] + investment_calc["margin_approved"]
    
    # Helper to send a single skip message and return
    def _skip(reason):
        msg = f"🎯 9-Sig (5%) — ${investment_amount:,.2f}\n⏭ {reason}"
        send_telegram_message(msg)
        print(reason)
        return reason
    
    # Gate checks
    # Note: 9-Sig monthly contributions go to AGG (bonds) and don't add equity leverage,
    # so we run from cash whenever leverage is at or below 1.0× — even if margin gates fail.
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
                return _skip(f"Skipped — projected leverage {projected_leverage:.3f}x exceeds {margin_control_config['max_leverage']:.2f}x limit")
            print(f"9-Sig: Leverage check — Current {leverage:.3f}x → Projected {projected_leverage:.3f}x")
    
    # Load current state
    balances = load_balances(env)
    nine_sig_data = balances.get("nine_sig", {})
    total_invested = nine_sig_data.get("total_invested", 0)
    stored_agg_shares = nine_sig_data.get("current_agg_shares", 0)
    
    actual_positions = get_nine_sig_positions(api)
    actual_agg_shares = actual_positions.get("AGG", 0)
    
    if actual_agg_shares > 0:
        current_agg_shares = actual_agg_shares
        if abs(stored_agg_shares - actual_agg_shares) > 0.0001:
            print(f"Warning: Firestore AGG ({stored_agg_shares:.6f}) differs from Alpaca ({actual_agg_shares:.6f})")
    else:
        current_agg_shares = stored_agg_shares
    
    print(f"9-Sig Strategy — Investment: ${investment_amount:.2f}, AGG shares: {current_agg_shares:.6f}")
    
    try:
        agg_price = float(get_latest_trade(api, "AGG"))
        agg_shares_to_buy = investment_amount / agg_price
        
        if agg_shares_to_buy > 0:
            order = submit_order(api, "AGG", agg_shares_to_buy, "buy")
            if not skip_order_wait:
                wait_for_order_fill(api, order["id"])
            
            new_total_invested = total_invested + investment_amount
            
            print("Waiting for orders to settle before syncing positions from Alpaca...")
            time.sleep(2)
            
            updated_positions = get_nine_sig_positions(api)
            actual_new_agg_shares = updated_positions.get("AGG", 0)
            
            if actual_new_agg_shares > 0:
                new_total_agg_shares = actual_new_agg_shares
                print(f"Synced AGG shares from Alpaca: {new_total_agg_shares:.6f}")
            else:
                print("Warning: Could not get AGG position from Alpaca, using manual calculation")
                new_total_agg_shares = current_agg_shares + agg_shares_to_buy
            
            print(f"9-Sig: Bought {agg_shares_to_buy:.6f} shares of AGG")
            
            track_nine_sig_monthly_contribution(investment_amount, env=env)
            
            actual_tqqq_shares = updated_positions.get("TQQQ", 0) if updated_positions else 0
            
            save_balance("nine_sig", {
                "total_invested": new_total_invested,
                "current_agg_shares": new_total_agg_shares,
                "current_tqqq_shares": actual_tqqq_shares,
                "current_positions": updated_positions if updated_positions else {"AGG": new_total_agg_shares},
                "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
                "last_monthly_contribution": {
                    "amount": investment_amount,
                    "agg_shares": agg_shares_to_buy,
                    "agg_price": agg_price
                },
                "strategy_type": "monthly_contribution"
            }, env)
            
            # Calculate strategy performance
            current_value = new_total_agg_shares * agg_price + actual_tqqq_shares * float(get_latest_trade(api, "TQQQ")) if actual_tqqq_shares > 0 else new_total_agg_shares * agg_price
            strategy_return = (current_value / new_total_invested - 1) if new_total_invested > 0 else 0
            
            # Single clean Telegram message
            msg = f"🎯 9-Sig (5%) — ${investment_amount:,.2f}\n\n"
            msg += f"Bought {agg_shares_to_buy:.4f} AGG @ ${agg_price:.2f}\n\n"
            msg += f"Total invested: ${new_total_invested:,.2f}\n"
            msg += f"Current value: ${current_value:,.2f}\n"
            msg += f"Return: {strategy_return:+.1%}"
            send_telegram_message(msg)
        
        return f"9-Sig monthly contribution: ${investment_amount:.2f} invested in AGG"
    
    except Exception as e:
        error_msg = f"9-Sig monthly contribution failed: {str(e)}"
        print(error_msg)
        send_telegram_message(f"🎯 9-Sig (5%)\n❌ Error: {str(e)}")
        return error_msg


def make_monthly_buys_rssb_wtip(api, force_execute=False, investment_calc=None, margin_result=None, skip_order_wait=False, env="live"):
    """
    Make monthly RSSB/WTIP purchases with margin-aware logic and dynamic investment amounts.
    Uses All-or-Nothing approach: invest full amount or skip entirely.
    Sends exactly one Telegram message at the end summarizing the outcome.
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        print("Not first trading day of the month")
        return "Not first trading day of the month"
    
    if force_execute:
        print("RSSB/WTIP: Force execution enabled - bypassing trading day check")
    
    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)
    
    investment_amount = investment_calc["strategy_amounts"]["rssb_wtip_allo"]
    
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    
    def _skip(reason):
        msg = f"🌐 RSSB/WTIP (17.5%) — ${investment_amount:,.2f}\n⏭ {reason}"
        send_telegram_message(msg)
        print(reason)
        return reason
    
    if not target_margin and leverage > 1.0:
        return _skip("Skipped — margin disabled, still leveraged")
    
    if investment_amount < margin_control_config["min_investment"]:
        return _skip(f"Skipped — ${investment_amount:.2f} below $1.00 minimum")
    
    if target_margin > 0:
        portfolio_value = metrics.get("portfolio_value", 0)
        current_equity = metrics.get("equity", 0)
        if portfolio_value > 0 and current_equity > 0:
            projected_leverage = (portfolio_value + investment_amount) / current_equity
            if projected_leverage >= margin_control_config["max_leverage"]:
                return _skip(f"Skipped — projected leverage {projected_leverage:.3f}x exceeds limit")
            print(f"RSSB/WTIP: Leverage check — Current {leverage:.3f}x → Projected {projected_leverage:.3f}x")
    
    # Load current strategy state from Firestore (before calculations)
    balances = load_balances(env)
    rssb_wtip_data = balances.get("rssb_wtip", {})
    total_invested = rssb_wtip_data.get("total_invested", 0)
    current_positions = rssb_wtip_data.get("current_positions", {})
    holding_fund_position = rssb_wtip_data.get("holding_fund_position", {})
    
    # Get holding fund (BIL) current value and shares from Alpaca
    bil_shares = get_holding_fund_shares(api, rssb_wtip_holding_fund)
    bil_value = get_holding_fund_value(api, rssb_wtip_holding_fund)
    bil_price = float(get_latest_trade(api, rssb_wtip_holding_fund)) if bil_value > 0 or investment_amount > 0 else 0
    
    # Get current RSSB/WTIP allocations
    (
        rssb_diff,
        wtip_diff,
        rssb_value,
        wtip_value,
        total_value,
        target_rssb_value,
        target_wtip_value,
        current_rssb_percent,
        current_wtip_percent,
    ) = get_rssb_wtip_allocations(api)

    # Calculate total available: current positions + BIL + new investment
    # Strategy: MINIMIZE SELLS to avoid taxable events
    total_to_allocate = rssb_value + wtip_value + bil_value + investment_amount
    
    print(f"RSSB/WTIP Strategy - Investment: ${investment_amount:.2f}")
    print(f"Current RSSB value: ${rssb_value:.2f}")
    print(f"Current WTIP value: ${wtip_value:.2f}")
    print(f"BIL holding fund: ${bil_value:.2f}")
    print(f"Total to allocate: ${total_to_allocate:.2f}")
    
    # Calculate target allocations (70% RSSB, 30% WTIP)
    target_rssb_value_new = total_to_allocate * rssb_allocation
    target_wtip_value_new = total_to_allocate * wtip_allocation
    
    print(f"Target RSSB: ${target_rssb_value_new:.2f} (80%)")
    print(f"Target WTIP: ${target_wtip_value_new:.2f} (20%)")
    
    # Get current prices
    rssb_price = float(get_latest_trade(api, "RSSB"))
    wtip_price = float(get_latest_trade(api, "WTIP"))
    
    # Calculate how much we need to buy/sell for each
    rssb_value_delta = target_rssb_value_new - rssb_value
    wtip_value_delta = target_wtip_value_new - wtip_value
    
    # Track purchases and sells
    rssb_shares_to_buy = 0
    wtip_shares_to_buy = 0
    rssb_shares_to_sell = 0
    wtip_shares_to_sell = 0
    actual_purchases = {}
    uninvested_wtip_amount = 0
    
    # Available new funds (investment + BIL) - use these first to minimize sells
    available_new_funds = investment_amount + bil_value
    funds_used = 0
    
    # Step 1: Use new funds to buy underweight positions first (minimize sells)
    # Priority: Buy the more underweight position first
    positions_to_buy = []
    if rssb_value_delta > 0.01:
        positions_to_buy.append(("RSSB", rssb_value_delta, rssb_price, True))  # True = fractionable
    if wtip_value_delta > 0.01:
        positions_to_buy.append(("WTIP", wtip_value_delta, wtip_price, False))  # False = non-fractionable
    
    # Sort by underweight amount (largest first)
    positions_to_buy.sort(key=lambda x: x[1], reverse=True)
    
    for symbol, value_delta, price, is_fractionable in positions_to_buy:
        if funds_used >= available_new_funds:
            break  # No more funds available
            
        remaining_funds = available_new_funds - funds_used
        max_we_can_buy = min(value_delta, remaining_funds)
        
        # For fractionable assets (RSSB), we can buy with any amount > 0
        # For non-fractionable assets (WTIP), we need at least 1 share worth
        can_buy = (is_fractionable and max_we_can_buy > 0.01) or (not is_fractionable and max_we_can_buy >= price)
        
        if can_buy:
            if is_fractionable:
                # RSSB supports fractional shares - buy with all available funds
                shares_to_buy = max_we_can_buy / price
                actual_cost = shares_to_buy * price
            else:
                # WTIP doesn't support fractional - round to whole shares
                shares_to_buy = round(max_we_can_buy / price)
                if shares_to_buy >= 1:
                    actual_cost = shares_to_buy * price
                else:
                    # Can't buy even 1 share
                    if symbol == "WTIP":
                        uninvested_wtip_amount = max_we_can_buy
                    continue
            
            if shares_to_buy > 0:
                try:
                    buy_order = submit_order(api, symbol, shares_to_buy, "buy")
                    if not skip_order_wait:
                        wait_for_order_fill(api, buy_order["id"])
                    
                    shares_display = f"{shares_to_buy:.4f}" if is_fractionable else f"{shares_to_buy:.0f}"
                    print(f"Bought {shares_display} shares of {symbol} (${actual_cost:.2f})")
                    actual_purchases[symbol] = actual_cost
                    funds_used += actual_cost
                    
                    if symbol == "RSSB":
                        rssb_shares_to_buy = shares_to_buy
                    else:
                        wtip_shares_to_buy = shares_to_buy
                except Exception as e:
                    error_msg = f"RSSB/WTIP: Failed to buy {symbol}: {str(e)}"
                    print(error_msg)
                    send_telegram_message(f"🌐 RSSB/WTIP (17.5%)\n❌ Error buying {symbol}: {str(e)}")
                    return error_msg
    
    # Step 2: Only sell if still overweight after using all new funds
    # TAX-EFFICIENT: Only sell if deviation is significant (>5% of target) to minimize taxable events
    # Recalculate current values after purchases
    rssb_value_after = rssb_value + (rssb_shares_to_buy * rssb_price if rssb_shares_to_buy > 0 else 0)
    wtip_value_after = wtip_value + (wtip_shares_to_buy * wtip_price if wtip_shares_to_buy > 0 else 0)
    
    # Check if still overweight after purchases
    rssb_value_delta_after = target_rssb_value_new - rssb_value_after
    wtip_value_delta_after = target_wtip_value_new - wtip_value_after
    
    # Calculate percentage deviations from target (for tax-efficient threshold)
    rssb_overweight_pct = abs(rssb_value_delta_after) / target_rssb_value_new if target_rssb_value_new > 0 else 0
    wtip_overweight_pct = abs(wtip_value_delta_after) / target_wtip_value_new if target_wtip_value_new > 0 else 0
    
    # Tax-efficient threshold: Only sell if overweight by >5% of target allocation
    # This minimizes taxable events while still maintaining reasonable allocation
    sell_threshold_pct = 0.05  # 5% threshold
    
    # Determine which positions need selling (only if significantly overweight)
    positions_to_sell = []
    if rssb_value_delta_after < -0.01 and rssb_overweight_pct > sell_threshold_pct:
        # RSSB is overweight by more than threshold
        positions_to_sell.append(("RSSB", abs(rssb_value_delta_after), rssb_price, True, rssb_overweight_pct))
    
    if wtip_value_delta_after < -0.01 and wtip_overweight_pct > sell_threshold_pct:
        # WTIP is overweight by more than threshold
        positions_to_sell.append(("WTIP", abs(wtip_value_delta_after), wtip_price, False, wtip_overweight_pct))
    
    # Sort by overweight percentage (most overweight first) - sell the worst offender
    positions_to_sell.sort(key=lambda x: x[4], reverse=True)
    
    # Only sell if we have significant deviation AND we can't fix it through buying alone
    # Strategy: Sell only the minimum needed to get closer to target, not necessarily all the way
    for symbol, overweight_amount, price, is_fractionable, overweight_pct in positions_to_sell:
        # Calculate how much we need to sell to get within threshold
        # We don't need to sell all the way to target - just enough to get within acceptable range
        target_after_sell = target_rssb_value_new if symbol == "RSSB" else target_wtip_value_new
        current_after_buy = rssb_value_after if symbol == "RSSB" else wtip_value_after
        
        # Calculate maximum acceptable value (target + threshold)
        max_acceptable_value = target_after_sell * (1 + sell_threshold_pct)
        excess_value = current_after_buy - max_acceptable_value
        
        # Only sell if we're still significantly over the acceptable range
        if excess_value > 0.01:
            if is_fractionable:
                # RSSB: Sell the excess amount (fractional shares allowed)
                shares_to_sell = excess_value / price
                if shares_to_sell > 0.0001:  # Meaningful amount
                    try:
                        sell_order = submit_order(api, symbol, shares_to_sell, "sell")
                        if not skip_order_wait:
                            wait_for_order_fill(api, sell_order["id"])
                        rssb_shares_to_sell = shares_to_sell
                        print(f"Sold {shares_to_sell:.4f} shares of {symbol} (${excess_value:.2f}) to reduce overweight from {overweight_pct:.1%} to within {sell_threshold_pct:.1%} threshold")
                    except Exception as e:
                        print(f"RSSB/WTIP: Failed to sell {symbol}: {str(e)}")
            else:
                shares_to_sell = round(excess_value / price)
                whole_shares_to_sell = int(shares_to_sell)
                if whole_shares_to_sell > 0:
                    try:
                        sell_order = submit_order(api, symbol, whole_shares_to_sell, "sell")
                        if not skip_order_wait:
                            wait_for_order_fill(api, sell_order["id"])
                        wtip_shares_to_sell = whole_shares_to_sell
                        actual_sold_value = whole_shares_to_sell * price
                        print(f"Sold {whole_shares_to_sell:.0f} shares of {symbol} (${actual_sold_value:.2f})")
                    except Exception as e:
                        print(f"RSSB/WTIP: Failed to sell {symbol}: {str(e)}")
        else:
            # Within acceptable range after buys - no need to sell
            print(f"{symbol}: Overweight {overweight_pct:.1%} but within acceptable threshold ({sell_threshold_pct:.1%}) - skipping sell to minimize taxable event")
    
    # Step 2.5: Use proceeds from sales to buy underweight positions
    # Recalculate values after all sells to see what we still need
    rssb_value_after_all = rssb_value + (rssb_shares_to_buy * rssb_price if rssb_shares_to_buy > 0 else 0) - (rssb_shares_to_sell * rssb_price if rssb_shares_to_sell > 0 else 0)
    wtip_value_after_all = wtip_value + (wtip_shares_to_buy * wtip_price if wtip_shares_to_buy > 0 else 0) - (wtip_shares_to_sell * wtip_price if wtip_shares_to_sell > 0 else 0)
    
    # Calculate proceeds from sales
    sale_proceeds = 0
    if rssb_shares_to_sell > 0:
        sale_proceeds += rssb_shares_to_sell * rssb_price
    if wtip_shares_to_sell > 0:
        sale_proceeds += wtip_shares_to_sell * wtip_price
    
    # Use sale proceeds to buy underweight positions
    if sale_proceeds > 0:
        # Recalculate what we still need after all buys and sells
        rssb_value_delta_final = target_rssb_value_new - rssb_value_after_all
        wtip_value_delta_final = target_wtip_value_new - wtip_value_after_all
        
        # Priority: Buy the more underweight position first
        positions_to_buy_with_proceeds = []
        if rssb_value_delta_final > 0.01:
            positions_to_buy_with_proceeds.append(("RSSB", rssb_value_delta_final, rssb_price, True))  # True = fractionable
        if wtip_value_delta_final > 0.01:
            positions_to_buy_with_proceeds.append(("WTIP", wtip_value_delta_final, wtip_price, False))  # False = non-fractionable
        
        # Sort by underweight amount (largest first)
        positions_to_buy_with_proceeds.sort(key=lambda x: x[1], reverse=True)
        
        proceeds_used = 0
        for symbol, value_delta, price, is_fractionable in positions_to_buy_with_proceeds:
            if proceeds_used >= sale_proceeds:
                break  # No more proceeds available
                
            remaining_proceeds = sale_proceeds - proceeds_used
            max_we_can_buy = min(value_delta, remaining_proceeds)
            
            # For fractionable assets (RSSB), we can buy with any amount > 0
            # For non-fractionable assets (WTIP), we need at least 1 share worth
            can_buy = (is_fractionable and max_we_can_buy > 0.01) or (not is_fractionable and max_we_can_buy >= price)
            
            if can_buy:
                if is_fractionable:
                    # RSSB supports fractional shares - buy with all available proceeds
                    shares_to_buy = max_we_can_buy / price
                    actual_cost = shares_to_buy * price
                else:
                    # WTIP doesn't support fractional - round to whole shares
                    shares_to_buy = round(max_we_can_buy / price)
                    if shares_to_buy >= 1:
                        actual_cost = shares_to_buy * price
                    else:
                        # Can't buy even 1 share - will go to BIL later
                        continue
                
                if shares_to_buy > 0:
                    try:
                        buy_order = submit_order(api, symbol, shares_to_buy, "buy")
                        if not skip_order_wait:
                            wait_for_order_fill(api, buy_order["id"])
                        
                        shares_display = f"{shares_to_buy:.4f}" if is_fractionable else f"{shares_to_buy:.0f}"
                        print(f"Bought {shares_display} shares of {symbol} (${actual_cost:.2f}) using sale proceeds")
                        actual_purchases[symbol] = actual_purchases.get(symbol, 0) + actual_cost
                        proceeds_used += actual_cost
                        
                        if symbol == "RSSB":
                            rssb_shares_to_buy += shares_to_buy
                        else:
                            wtip_shares_to_buy += shares_to_buy
                    except Exception as e:
                        print(f"RSSB/WTIP: Failed to buy {symbol} with sale proceeds: {str(e)}")
        
        # Any remaining proceeds after buying should go to BIL
        remaining_proceeds_after_buys = sale_proceeds - proceeds_used
        if remaining_proceeds_after_buys > 0.01:
            # This will be handled in Step 4 (uninvested amounts)
            uninvested_wtip_amount += remaining_proceeds_after_buys
    
    # Step 3: Sell BIL to fund purchases (BIL was included in total_to_allocate)
    bil_shares_to_sell = 0
    bil_amount_to_sell = 0
    total_purchases = sum(actual_purchases.values())
    
    if total_purchases > 0 and bil_value > 0:
        # Calculate how much BIL to sell: proportional to purchases
        bil_amount_to_sell = min(bil_value, total_purchases)
        
        if bil_amount_to_sell > 0:
            bil_shares_to_sell = bil_amount_to_sell / bil_price if bil_price > 0 else 0
            
            # Get actual available BIL shares right before selling
            actual_bil_shares_available = get_holding_fund_shares(api, rssb_wtip_holding_fund)
            bil_shares_to_sell = min(bil_shares_to_sell, actual_bil_shares_available)
            
            if bil_shares_to_sell > 0.0001:  # Only sell if meaningful amount
                try:
                    sell_order = submit_order(api, rssb_wtip_holding_fund, bil_shares_to_sell, "sell")
                    if not skip_order_wait:
                        wait_for_order_fill(api, sell_order["id"])
                    
                    actual_bil_sold_value = bil_shares_to_sell * bil_price
                    bil_value -= actual_bil_sold_value
                    print(f"Sold {bil_shares_to_sell:.6f} shares of BIL (${actual_bil_sold_value:.2f}) to fund purchases")
                except Exception as e:
                    print(f"RSSB/WTIP: Failed to sell BIL: {str(e)}")
                    print("Continuing despite BIL sell failure...")
    
    # Step 4: Handle uninvested amounts - add to BIL holding fund (up to max)
    bil_leftover_after_wtip = 0
    bil_shares_to_buy = 0
    bil_amount_to_buy = 0
    
    # Handle BIL holding fund for uninvested amounts
    total_bil_to_add = uninvested_wtip_amount + bil_leftover_after_wtip
    
    if total_bil_to_add > 0:
        # Check if we can add to BIL holding fund
        # Note: bil_value was already reduced if we sold BIL
        current_bil_value_after_sale = bil_value
        bil_value_after_investment = current_bil_value_after_sale + total_bil_to_add
        
        if bil_value_after_investment <= rssb_wtip_holding_fund_max:
            # Can add all leftover/uninvested amount to BIL
            bil_amount_to_buy = total_bil_to_add
            bil_shares_to_buy = bil_amount_to_buy / bil_price if bil_price > 0 else 0
        else:
            # Can only add up to max, try to buy WTIP with excess
            bil_amount_to_buy = rssb_wtip_holding_fund_max - current_bil_value_after_sale
            if bil_amount_to_buy > 0:
                bil_shares_to_buy = bil_amount_to_buy / bil_price if bil_price > 0 else 0
            
            # Try to buy WTIP with excess
            excess_amount = total_bil_to_add - bil_amount_to_buy
            if excess_amount > 0:
                excess_wtip_shares = round(excess_amount / wtip_price)
                if excess_wtip_shares >= 1:
                    try:
                        excess_buy_order = submit_order(api, "WTIP", excess_wtip_shares, "buy")
                        if not skip_order_wait:
                            wait_for_order_fill(api, excess_buy_order["id"])
                        wtip_shares_to_buy += excess_wtip_shares
                        print(f"Using excess ${excess_amount:.2f} to buy additional {excess_wtip_shares} shares of WTIP")
                    except Exception as e:
                        print(f"Failed to buy WTIP with excess: {e}")
                else:
                    # Still can't buy WTIP, add excess to BIL if under max
                    if current_bil_value_after_sale + bil_amount_to_buy + excess_amount <= rssb_wtip_holding_fund_max:
                        bil_amount_to_buy += excess_amount
                        bil_shares_to_buy = bil_amount_to_buy / bil_price if bil_price > 0 else 0
                    else:
                        # Can't add to BIL (over max) and can't buy WTIP - this money will remain as cash
                        print(f"Warning: ${excess_amount:.2f} cannot be invested (BIL at max, WTIP too expensive)")
    
    # Execute market orders tracking
    trades_executed = []
    
    # Note: Purchases and sells were already executed above in the tax-efficient logic
    # Just track them for reporting
    if rssb_shares_to_buy > 0:
        trades_executed.append(f"Bought {rssb_shares_to_buy:.4f} shares of RSSB")
    if wtip_shares_to_buy > 0:
        trades_executed.append(f"Bought {wtip_shares_to_buy:.0f} shares of WTIP")
    if rssb_shares_to_sell > 0:
        trades_executed.append(f"Sold {rssb_shares_to_sell:.4f} shares of RSSB")
    if wtip_shares_to_sell > 0:
        trades_executed.append(f"Sold {wtip_shares_to_sell:.0f} shares of WTIP")
    
    # Buy BIL holding fund if needed (for uninvested amounts)
    if bil_shares_to_buy > 0:
        try:
            bil_order = submit_order(api, rssb_wtip_holding_fund, bil_shares_to_buy, "buy")
            if not skip_order_wait:
                wait_for_order_fill(api, bil_order["id"])
            trades_executed.append(f"Bought {bil_shares_to_buy:.6f} shares of BIL (${bil_amount_to_buy:.2f}) - holding fund")
            print(f"Bought {bil_shares_to_buy:.6f} shares of BIL (${bil_amount_to_buy:.2f}) - holding fund")
        except Exception as e:
            print(f"RSSB/WTIP: Failed to buy BIL: {e}")
            # Continue - BIL buy failure shouldn't stop the strategy
    
    # Update Firestore with new positions (even if no trades executed, update holding fund)
    if trades_executed or bil_shares_to_buy > 0:
        total_invested += investment_amount
        current_positions.update({
            "RSSB": current_positions.get("RSSB", 0) + rssb_shares_to_buy,
            "WTIP": current_positions.get("WTIP", 0) + wtip_shares_to_buy
        })
        
        # Update holding fund position (get fresh from Alpaca to be accurate)
        updated_bil_shares = get_holding_fund_shares(api, rssb_wtip_holding_fund)
        holding_fund_position[rssb_wtip_holding_fund] = updated_bil_shares
        
        # Get actual positions from Alpaca to update Firestore accurately
        positions_dict = {p["symbol"]: float(p["qty"]) for p in list_positions(api)}
        current_positions["RSSB"] = positions_dict.get("RSSB", 0)
        current_positions["WTIP"] = positions_dict.get("WTIP", 0)
        
        save_balance("rssb_wtip", {
            "total_invested": total_invested,
            "current_positions": current_positions,
            "holding_fund_position": holding_fund_position,
            "last_updated": datetime.datetime.utcnow().isoformat()
        }, env)
    
    # Calculate strategy performance
    current_value = rssb_value + wtip_value + bil_value
    if trades_executed:
        current_value = current_value + investment_amount
    strategy_return = (current_value / total_invested - 1) if total_invested > 0 else 0
    
    # Single clean Telegram message
    msg = f"🌐 RSSB/WTIP (17.5%) — ${investment_amount:,.2f}\n\n"
    if trades_executed:
        for trade in trades_executed:
            msg += f"{trade}\n"
        msg += f"\nTotal invested: ${total_invested:,.2f}\n"
        msg += f"Current value: ${current_value:,.2f}\n"
        msg += f"Return: {strategy_return:+.1%}"
    else:
        msg += "No trades executed"
    send_telegram_message(msg)
    
    return "Monthly investment executed."


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


def get_rssb_wtip_allocations(api):
    """
    Get RSSB/WTIP allocations (70/30).
    Returns current values, percentages, target values, and deviations.
    Includes BIL holding fund value in total_value calculation.
    """
    positions = {p["symbol"]: float(p["market_value"]) for p in list_positions(api)}
    rssb_value = positions.get("RSSB", 0)
    wtip_value = positions.get("WTIP", 0)
    bil_value = get_holding_fund_value(api, rssb_wtip_holding_fund)
    total_value = rssb_value + wtip_value + bil_value
    
    # Calculate current and target allocations
    current_rssb_percent = rssb_value / total_value if total_value else 0
    current_wtip_percent = wtip_value / total_value if total_value else 0
    target_rssb_value = total_value * rssb_allocation
    target_wtip_value = total_value * wtip_allocation
    
    # Calculate deviations
    rssb_diff = rssb_value - target_rssb_value
    wtip_diff = wtip_value - target_wtip_value
    
    return (
        rssb_diff,
        wtip_diff,
        rssb_value,
        wtip_value,
        total_value,
        target_rssb_value,
        target_wtip_value,
        current_rssb_percent,
        current_wtip_percent,
    )


def rebalance_rssb_wtip_portfolio(api):
    """
    Rebalance RSSB/WTIP portfolio (70/30) quarterly.
    Executes on first trading day of each quarter.
    Handles non-fractionable shares for WTIP and pending investments.
    """
    if not check_trading_day(mode="quarterly"):
        print("Not first trading day of the month in this Quarter")
        return "Not first trading day of the month in this Quarter"
    if quarterly_run_complete("rssb_wtip", env=alpaca_environment):
        msg = f"RSSB/WTIP quarterly rebalance already executed for {current_quarter_id()} — skipping."
        print(msg)
        return msg

    # Load pending investments from Firestore
    balances = load_balances()
    rssb_wtip_data = balances.get("rssb_wtip", {})
    holding_fund_position = rssb_wtip_data.get("holding_fund_position", {})
    
    # Get BIL holding fund value
    bil_value = get_holding_fund_value(api, rssb_wtip_holding_fund)
    bil_price = float(get_latest_trade(api, rssb_wtip_holding_fund)) if bil_value > 0 else 0
    
    # Get RSSB and WTIP values and deviations from target allocation
    (
        rssb_diff,
        wtip_diff,
        rssb_value,
        wtip_value,
        total_value,
        target_rssb_value,
        target_wtip_value,
        current_rssb_percent,
        current_wtip_percent,
    ) = get_rssb_wtip_allocations(api)

    # Apply a margin for fees (e.g., 0.5%)
    fee_margin = 0.995

    # If the total value is 0, nothing to rebalance
    if total_value == 0:
        print("No holdings to rebalance for RSSB/WTIP.")
        send_telegram_message("No holdings to rebalance for RSSB/WTIP Strategy.")
        return "No holdings to rebalance for RSSB/WTIP Strategy."

    # Get current prices
    rssb_price = float(get_latest_trade(api, "RSSB"))
    wtip_price = float(get_latest_trade(api, "WTIP"))

    # Define trade parameters for each ETF
    rebalance_actions = []

    # Track leftover funds that need to go into BIL
    bil_rebalance_leftover = 0
    
    # If RSSB is over-allocated, adjust WTIP if under-allocated
    if rssb_diff > 0:
        if wtip_diff < 0:
            rssb_shares_to_sell = min(rssb_diff, abs(wtip_diff)) / rssb_price
            wtip_value_to_buy = (
                rssb_shares_to_sell
                * rssb_price
                / wtip_price
            ) * fee_margin
            
            # Calculate WTIP shares to buy (round to whole shares)
            wtip_shares_to_buy = round(wtip_value_to_buy / wtip_price)
            
            # Handle non-fractionable WTIP shares
            if wtip_shares_to_buy >= 1:
                actual_wtip_cost = wtip_shares_to_buy * wtip_price
                bil_rebalance_leftover = wtip_value_to_buy - actual_wtip_cost
                rebalance_actions.append(("RSSB", rssb_shares_to_sell, "sell"))
                rebalance_actions.append(("WTIP", wtip_shares_to_buy, "buy"))
            else:
                # Can't buy any WTIP shares - put all funds into BIL
                bil_rebalance_leftover = wtip_value_to_buy
                rebalance_actions.append(("RSSB", rssb_shares_to_sell, "sell"))

    # If WTIP is over-allocated, adjust RSSB if under-allocated
    if wtip_diff > 0:
        if rssb_diff < 0:
            # Round down to whole shares when selling WTIP (non-fractionable)
            wtip_value_to_sell = min(wtip_diff, abs(rssb_diff))
            wtip_shares_to_sell = int(wtip_value_to_sell / wtip_price)  # Round down to whole shares
            
            if wtip_shares_to_sell > 0:
                actual_wtip_sale_value = wtip_shares_to_sell * wtip_price
                rssb_value_to_buy = (
                    actual_wtip_sale_value
                    / rssb_price
                ) * fee_margin
                rssb_shares_to_buy = rssb_value_to_buy / rssb_price
                
                # RSSB supports fractional shares, so no leftover here
                rebalance_actions.append(("WTIP", wtip_shares_to_sell, "sell"))
                rebalance_actions.append(("RSSB", rssb_shares_to_buy, "buy"))
                
                # If we couldn't sell all the WTIP value (due to rounding), put leftover in BIL
                wtip_leftover = wtip_value_to_sell - actual_wtip_sale_value
                if wtip_leftover > 0:
                    bil_rebalance_leftover += wtip_leftover
            else:
                print(f"Skipping WTIP sell: value ${wtip_value_to_sell:.2f} is less than 1 whole share (price: ${wtip_price:.2f})")
                # Put this small amount into BIL
                bil_rebalance_leftover += wtip_value_to_sell
    
    # Check if we can use BIL holding fund to buy WTIP if underweight
    if bil_value > 0 and wtip_diff < 0:
        # WTIP is underweight, try to use BIL funds to buy WTIP
        wtip_value_needed = abs(wtip_diff)
        wtip_shares_needed = round(wtip_value_needed / wtip_price)
        
        if wtip_shares_needed >= 1:
            # Calculate exact amount needed (with 1% buffer)
            bil_amount_needed = wtip_shares_needed * wtip_price * 1.01
            bil_value_to_use = min(bil_value, bil_amount_needed)
            # BIL may not support fractional shares - round to whole shares or use fractional if supported
            # Check current BIL shares to determine if fractional is supported
            bil_shares_to_sell = bil_value_to_use / bil_price if bil_price > 0 else 0
            
            # Try fractional first, but catch error if BIL doesn't support it
            if bil_shares_to_sell > 0:
                actual_wtip_cost = wtip_shares_needed * wtip_price
                bil_leftover = bil_value_to_use - actual_wtip_cost
                bil_rebalance_leftover += max(0, bil_leftover)
                
                # Note: BIL selling will be wrapped in try-catch in execution loop
                rebalance_actions.append((rssb_wtip_holding_fund, bil_shares_to_sell, "sell"))
                rebalance_actions.append(("WTIP", wtip_shares_needed, "buy"))
                print(f"Using ${bil_value_to_use:.2f} from BIL holding fund to buy {wtip_shares_needed} shares of WTIP")

    # Execute rebalancing actions
    for symbol, qty, action in rebalance_actions:
        if qty > 0:
            try:
                order = submit_order(api, symbol, qty, action)
                action_verb = "Bought" if action == "buy" else "Sold"
                wait_for_order_fill(api, order["id"])
                print(f"RSSB/WTIP: {action_verb} {qty:.6f} shares of {symbol} to rebalance.")
                send_telegram_message(
                    f"RSSB/WTIP: {action_verb} {qty:.6f} shares of {symbol} to rebalance."
                )
            except Exception as e:
                error_msg = f"RSSB/WTIP: Failed to {action} {symbol}: {str(e)}"
                print(error_msg)
                send_telegram_message(error_msg)
                # Continue with other trades even if one fails
                continue
    
    # Handle leftover funds from rebalancing - put into BIL if under max
    if bil_rebalance_leftover > 0:
        current_bil_value = get_holding_fund_value(api, rssb_wtip_holding_fund)
        bil_value_after_leftover = current_bil_value + bil_rebalance_leftover
        
        if bil_value_after_leftover <= rssb_wtip_holding_fund_max:
            # Can add all leftover to BIL
            bil_price_rebalance = float(get_latest_trade(api, rssb_wtip_holding_fund))
            bil_shares_to_buy_rebalance = bil_rebalance_leftover / bil_price_rebalance if bil_price_rebalance > 0 else 0
            
            if bil_shares_to_buy_rebalance > 0:
                try:
                    bil_order = submit_order(api, rssb_wtip_holding_fund, bil_shares_to_buy_rebalance, "buy")
                    wait_for_order_fill(api, bil_order["id"])
                    print(f"RSSB/WTIP: Added ${bil_rebalance_leftover:.2f} leftover from rebalancing to BIL holding fund")
                    send_telegram_message(f"RSSB/WTIP: Added ${bil_rebalance_leftover:.2f} leftover from rebalancing to BIL")
                except Exception as e:
                    print(f"RSSB/WTIP: Failed to add leftover to BIL: {e}")
        else:
            # Can only add up to max
            bil_amount_to_add = rssb_wtip_holding_fund_max - current_bil_value
            if bil_amount_to_add > 0:
                bil_price_rebalance = float(get_latest_trade(api, rssb_wtip_holding_fund))
                bil_shares_to_buy_rebalance = bil_amount_to_add / bil_price_rebalance if bil_price_rebalance > 0 else 0
                
                if bil_shares_to_buy_rebalance > 0:
                    try:
                        bil_order = submit_order(api, rssb_wtip_holding_fund, bil_shares_to_buy_rebalance, "buy")
                        wait_for_order_fill(api, bil_order["id"])
                        print(f"RSSB/WTIP: Added ${bil_amount_to_add:.2f} leftover from rebalancing to BIL (max reached)")
                    except Exception as e:
                        print(f"RSSB/WTIP: Failed to add leftover to BIL: {e}")
    
    # Update Firestore with holding fund position after rebalancing
    if rebalance_actions or bil_rebalance_leftover > 0:
        updated_bil_shares = get_holding_fund_shares(api, rssb_wtip_holding_fund)
        holding_fund_position[rssb_wtip_holding_fund] = updated_bil_shares
        rssb_wtip_data["holding_fund_position"] = holding_fund_position
        save_balance("rssb_wtip", rssb_wtip_data)
    
    # Report completion of rebalancing check
    print("RSSB/WTIP rebalance check completed.")
    mark_quarterly_run_complete("rssb_wtip", "REBALANCED", env=alpaca_environment)
    return "RSSB/WTIP rebalance executed."


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


def execute_quarterly_nine_sig_signal(api, force_execute=False, env="live"):
    """Execute quarterly 9-sig signal following Jason Kelly's exact 5-step process"""
    if not force_execute and not check_trading_day(mode="quarterly"):
        print("Not first trading day of the quarter")
        return "Not first trading day of the quarter"

    if not force_execute and quarterly_run_complete("nine_sig", env=env):
        msg = f"9-Sig quarterly signal already executed for {current_quarter_id()} — skipping."
        print(msg)
        return msg

    if force_execute:
        print("9-Sig: Force execution enabled - bypassing trading day check")
        send_telegram_message("9-Sig: Force execution enabled for testing - bypassing trading day check")
    
    try:
        # Step 1: Get current positions
        positions = {p["symbol"]: float(p["market_value"]) for p in list_positions(api)}
        current_tqqq_balance = positions.get("TQQQ", 0)
        current_agg_balance = positions.get("AGG", 0)
        total_portfolio = current_tqqq_balance + current_agg_balance
        
        print(f"\n=== 9-Sig Quarterly Rebalancing ===")
        print(f"Current TQQQ balance: ${current_tqqq_balance:.2f}")
        print(f"Current AGG balance: ${current_agg_balance:.2f}")
        print(f"Total portfolio: ${total_portfolio:.2f}")
        
        # Step 1: Determine the Quarter's Signal Line
        previous_tqqq_balance = get_previous_quarter_tqqq_balance(env=env)
        
        # Get actual contributions made during this quarter (dynamic amounts)
        quarterly_contributions = get_quarterly_nine_sig_contributions(env=env)
        half_quarterly_contributions = quarterly_contributions * 0.5
        
        print(f"Previous quarter TQQQ balance: ${previous_tqqq_balance:.2f}")
        print(f"Quarterly contributions: ${quarterly_contributions:.2f}")
        print(f"Half quarterly contributions: ${half_quarterly_contributions:.2f}")
        
        # Signal Line = Previous TQQQ Balance × 1.09 + (Half of Quarterly Contributions)
        if previous_tqqq_balance == 0 and total_portfolio > 0:
            # First quarter: Set signal line as 80% of total portfolio
            signal_line = total_portfolio * nine_sig_config["target_allocation"]["tqqq"]
            print(f"First quarter initialization - setting signal line as 80% of total portfolio")
            send_telegram_message("9-Sig: First quarter initialization - setting 80/20 target allocation")
        else:
            signal_line = (previous_tqqq_balance * (1 + nine_sig_config["quarterly_growth_rate"])) + half_quarterly_contributions
            print(f"Signal line calculation: ${previous_tqqq_balance:.2f} × 1.09 + ${half_quarterly_contributions:.2f} = ${signal_line:.2f}")
        
        print(f"Signal line: ${signal_line:.2f}")
        
        # Step 2: Determine Action (Buy, Sell, or Hold)
        difference = current_tqqq_balance - signal_line
        tolerance = nine_sig_config["tolerance_amount"]
        print(f"Difference (TQQQ - Signal Line): ${difference:.2f}")
        print(f"Tolerance: ${tolerance:.2f}")
        
        # Step 3: Execute the Trade
        if abs(difference) < tolerance:
            action = "HOLD"
            print(f"Action: HOLD - TQQQ balance within tolerance of signal line")
            send_telegram_message(f"9-Sig: HOLD - TQQQ ${current_tqqq_balance:.2f} within tolerance of signal line ${signal_line:.2f}")
            
        elif difference < 0:
            # BUY Signal: Need more TQQQ
            amount_to_buy = abs(difference)
            action = "BUY"
            print(f"Action: BUY - Need ${amount_to_buy:.2f} more TQQQ to reach signal line")
            
            # Step 4: Check for bond rebalancing threshold (30% rule from README)
            # If current AGG exceeds 30% threshold, rebalance down to 20% target during BUY signals
            current_agg_percentage = current_agg_balance / total_portfolio if total_portfolio > 0 else 0
            bond_rebalance_threshold = nine_sig_config["bond_rebalance_threshold"]
            target_agg_percentage = nine_sig_config["target_allocation"]["agg"]
            
            print(f"Current AGG percentage: {current_agg_percentage:.1%} (threshold: {bond_rebalance_threshold:.1%}, target: {target_agg_percentage:.1%})")
            
            # Calculate what portfolio would look like after signal line buy
            projected_tqqq_after_signal = current_tqqq_balance + amount_to_buy
            projected_agg_after_signal = current_agg_balance - amount_to_buy
            projected_total_after_signal = projected_tqqq_after_signal + projected_agg_after_signal
            projected_agg_percentage_after_signal = projected_agg_after_signal / projected_total_after_signal if projected_total_after_signal > 0 else 0
            
            print(f"After signal line buy: AGG would be ${projected_agg_after_signal:.2f} ({projected_agg_percentage_after_signal:.1%})")
            
            # Check if bond rebalancing is needed (either current AGG > 30% OR projected AGG > 20% target)
            # Special handling for first quarter: signal line is already set to 80% of portfolio, which naturally results in 20% AGG
            needs_rebalancing = False
            excess_agg = 0
            
            # For first quarter initialization, signal line = 80% of portfolio, so buying to signal line naturally achieves 80/20
            # Only need additional rebalancing if projected AGG would still be above target after signal buy
            is_first_quarter = (previous_tqqq_balance == 0 and total_portfolio > 0)
            
            if is_first_quarter:
                # First quarter: signal line is 80% of portfolio, so buying to signal line should naturally result in 20% AGG
                # Check if projected AGG after signal buy would be exactly 20% (as expected) or higher
                if projected_agg_percentage_after_signal > target_agg_percentage:
                    # This shouldn't happen in first quarter, but handle it if it does
                    target_agg_balance = projected_total_after_signal * target_agg_percentage
                    excess_agg = projected_agg_after_signal - target_agg_balance
                    needs_rebalancing = True
                    print(f"First quarter: Projected AGG ({projected_agg_percentage_after_signal:.1%}) would exceed {target_agg_percentage:.1%} target - adding rebalance")
                else:
                    print(f"First quarter: Signal line buy will naturally achieve {target_agg_percentage:.1%} AGG target - no additional rebalancing needed")
            elif current_agg_percentage > bond_rebalance_threshold:
                # AGG exceeds 30% threshold - rebalance down to 20% target
                target_agg_balance = projected_total_after_signal * target_agg_percentage
                excess_agg = projected_agg_after_signal - target_agg_balance
                needs_rebalancing = True
                print(f"Bond rebalancing triggered: Current AGG ({current_agg_percentage:.1%}) exceeds {bond_rebalance_threshold:.1%} threshold")
            elif projected_agg_percentage_after_signal > target_agg_percentage:
                # AGG would still be above 20% target after signal buy - rebalance to target
                target_agg_balance = projected_total_after_signal * target_agg_percentage
                excess_agg = projected_agg_after_signal - target_agg_balance
                needs_rebalancing = True
                print(f"Bond rebalancing needed: Projected AGG ({projected_agg_percentage_after_signal:.1%}) would exceed {target_agg_percentage:.1%} target")
            
            if needs_rebalancing and excess_agg > 0:
                amount_to_buy += excess_agg
                print(f"Adding ${excess_agg:.2f} to buy amount to rebalance AGG to {target_agg_percentage:.1%}")
                print(f"Total buy amount: ${amount_to_buy:.2f} (signal: ${abs(difference):.2f} + rebalance: ${excess_agg:.2f})")
                send_telegram_message(f"9-Sig: Bond rebalancing - Adding ${excess_agg:.2f} to buy amount to reach {target_agg_percentage:.1%} AGG target")
            else:
                print(f"Signal line buy will naturally rebalance AGG to target - no additional rebalancing needed")
            
            print(f"Required AGG to sell: ${amount_to_buy:.2f}, Available AGG balance: ${current_agg_balance:.2f}")
            
            # Check available shares (excluding held for orders, unsettled, etc.)
            positions = list_positions(api)
            agg_position = next((p for p in positions if p.get("symbol") == "AGG"), None)
            
            # Check for pending orders that might be holding shares
            try:
                pending_orders = get_pending_orders(api, "AGG")
                if pending_orders:
                    print(f"Found {len(pending_orders)} pending AGG orders:")
                    for order in pending_orders:
                        print(f"  Order {order.get('id')}: {order.get('side')} {order.get('qty')} shares (status: {order.get('status')})")
                    print("Note: Shares held for pending orders are not available for new trades")
            except Exception as e:
                print(f"Could not check pending orders: {e}")
            
            if agg_position:
                # Get available shares (qty_available field from Alpaca)
                available_agg_shares = float(agg_position.get("qty_available", 0))
                qty = float(agg_position.get("qty", 0))
                print(f"AGG position: {qty} total shares, {available_agg_shares} available")
            else:
                available_agg_shares = 0
                print("No AGG position found")
            
            agg_price = float(get_latest_trade(api, "AGG"))
            available_agg_value = available_agg_shares * agg_price if agg_price > 0 else 0
            
            print(f"Available AGG: {available_agg_shares:.6f} shares (value: ${available_agg_value:.2f})")
            
            # Use available shares if less than required, but only if we have at least some available
            if available_agg_value > 0 and available_agg_value < amount_to_buy:
                print(f"Warning: Only ${available_agg_value:.2f} AGG available (need ${amount_to_buy:.2f})")
                print(f"Adjusting buy amount to available funds for cold start scenario")
                amount_to_buy = available_agg_value
                print(f"Adjusted buy amount: ${amount_to_buy:.2f}")
            
            if current_agg_balance >= amount_to_buy and available_agg_value >= amount_to_buy:
                # Execute buy trade
                print("Executing BUY trade...")
                tqqq_price = float(get_latest_trade(api, "TQQQ"))
                
                agg_shares_to_sell = amount_to_buy / agg_price
                tqqq_shares_to_buy = amount_to_buy / tqqq_price
                
                # Ensure we don't try to sell more shares than available
                if agg_shares_to_sell > available_agg_shares:
                    print(f"Adjusting: Can only sell {available_agg_shares:.6f} shares (requested {agg_shares_to_sell:.6f})")
                    agg_shares_to_sell = available_agg_shares
                    amount_to_buy = agg_shares_to_sell * agg_price
                    tqqq_shares_to_buy = amount_to_buy / tqqq_price
                    print(f"Adjusted: Selling {agg_shares_to_sell:.6f} AGG shares for ${amount_to_buy:.2f}")
                
                print(f"Selling {agg_shares_to_sell:.6f} AGG shares @ ${agg_price:.2f}")
                print(f"Buying {tqqq_shares_to_buy:.6f} TQQQ shares @ ${tqqq_price:.2f}")
                
                # Sell AGG first, then buy TQQQ
                sell_order = submit_order(api, "AGG", agg_shares_to_sell, "sell")
                wait_for_order_fill(api, sell_order["id"])
                
                buy_order = submit_order(api, "TQQQ", tqqq_shares_to_buy, "buy")
                wait_for_order_fill(api, buy_order["id"])
                
                print("BUY trade executed successfully!")
                send_telegram_message(f"9-Sig: BUY signal executed - Bought ${amount_to_buy:.2f} TQQQ (sold AGG)")
            elif available_agg_value > 0:
                # Partial execution possible - use available shares
                print(f"Partial execution: Only ${available_agg_value:.2f} AGG available, executing partial buy")
                amount_to_buy = available_agg_value
                tqqq_price = float(get_latest_trade(api, "TQQQ"))
                
                agg_shares_to_sell = available_agg_shares
                tqqq_shares_to_buy = amount_to_buy / tqqq_price
                
                print(f"Executing partial BUY: Selling {agg_shares_to_sell:.6f} AGG shares for ${amount_to_buy:.2f}")
                print(f"Buying {tqqq_shares_to_buy:.6f} TQQQ shares @ ${tqqq_price:.2f}")
                
                sell_order = submit_order(api, "AGG", agg_shares_to_sell, "sell")
                wait_for_order_fill(api, sell_order["id"])
                
                buy_order = submit_order(api, "TQQQ", tqqq_shares_to_buy, "buy")
                wait_for_order_fill(api, buy_order["id"])
                
                print("Partial BUY trade executed successfully!")
                send_telegram_message(f"9-Sig: Partial BUY executed - Bought ${amount_to_buy:.2f} TQQQ (sold available AGG)")
            else:
                # Insufficient AGG funds
                print(f"Action: HOLD_INSUFFICIENT_FUNDS - Not enough AGG available to execute buy")
                print(f"  Required: ${amount_to_buy:.2f}, Available: ${available_agg_value:.2f}")
                send_telegram_message(f"9-Sig: BUY signal but insufficient AGG (${available_agg_value:.2f} available < ${amount_to_buy:.2f} needed) - HOLDING existing positions")
                action = "HOLD_INSUFFICIENT_FUNDS"
                
        else:
            # SELL Signal: Too much TQQQ
            amount_to_sell = difference
            action = "SELL"
            print(f"Action: SELL - Need to sell ${amount_to_sell:.2f} TQQQ")
            
            # Step 5: Check for "30 Down, Stick Around" rule
            spy_30_down = check_spy_30_down_rule()
            print(f"SPY 30% down rule check: {spy_30_down}")
            if spy_30_down:
                ignored_count = count_ignored_sell_signals(env=env)
                print(f"Ignored sell signals count: {ignored_count}/4")
                
                if ignored_count < 4:
                    action = "SELL_IGNORED"
                    print(f"Action: SELL_IGNORED - Ignoring sell signal due to '30 Down, Stick Around' rule")
                    send_telegram_message(f"9-Sig: SELL signal IGNORED due to '30 Down, Stick Around' rule (SPY down >30%). Ignored {ignored_count + 1}/4 signals.")
                else:
                    print("Resuming normal operation after ignoring 4 sell signals")
                    send_telegram_message("9-Sig: Resuming normal operation after ignoring 4 sell signals")
            
            if action == "SELL":
                # Execute sell trade
                print("Executing SELL trade...")
                
                # Check available TQQQ shares
                positions = list_positions(api)
                tqqq_position = next((p for p in positions if p.get("symbol") == "TQQQ"), None)
                available_tqqq_shares = float(tqqq_position.get("available", 0)) if tqqq_position else 0
                tqqq_price = float(get_latest_trade(api, "TQQQ"))
                available_tqqq_value = available_tqqq_shares * tqqq_price if tqqq_price > 0 else 0
                
                print(f"Available TQQQ shares: {available_tqqq_shares:.6f} (value: ${available_tqqq_value:.2f})")
                print(f"Required to sell: ${amount_to_sell:.2f}")
                
                # Adjust if we don't have enough available shares
                if available_tqqq_value > 0 and available_tqqq_value < amount_to_sell:
                    print(f"Warning: Only ${available_tqqq_value:.2f} TQQQ available (need ${amount_to_sell:.2f})")
                    print(f"Adjusting sell amount to available shares")
                    amount_to_sell = available_tqqq_value
                
                agg_price = float(get_latest_trade(api, "AGG"))
                tqqq_shares_to_sell = amount_to_sell / tqqq_price
                agg_shares_to_buy = amount_to_sell / agg_price
                
                # Ensure we don't try to sell more shares than available
                if tqqq_shares_to_sell > available_tqqq_shares:
                    print(f"Adjusting: Can only sell {available_tqqq_shares:.6f} shares (requested {tqqq_shares_to_sell:.6f})")
                    tqqq_shares_to_sell = available_tqqq_shares
                    amount_to_sell = tqqq_shares_to_sell * tqqq_price
                    agg_shares_to_buy = amount_to_sell / agg_price
                    print(f"Adjusted: Selling {tqqq_shares_to_sell:.6f} TQQQ shares for ${amount_to_sell:.2f}")
                
                print(f"Selling {tqqq_shares_to_sell:.6f} TQQQ shares @ ${tqqq_price:.2f}")
                print(f"Buying {agg_shares_to_buy:.6f} AGG shares @ ${agg_price:.2f}")
                
                # Sell TQQQ first, then buy AGG
                sell_order = submit_order(api, "TQQQ", tqqq_shares_to_sell, "sell")
                wait_for_order_fill(api, sell_order["id"])
                
                buy_order = submit_order(api, "AGG", agg_shares_to_buy, "buy")
                wait_for_order_fill(api, buy_order["id"])
                
                print("SELL trade executed successfully!")
                send_telegram_message(f"9-Sig: SELL signal executed - Sold ${amount_to_sell:.2f} TQQQ (bought AGG)")
        
        # Get updated positions after trades (or use current if no trades were executed)
        updated_positions = {p["symbol"]: float(p["market_value"]) for p in list_positions(api)}
        final_tqqq_balance = updated_positions.get("TQQQ", 0)
        final_agg_balance = updated_positions.get("AGG", 0)
        
        # Save quarterly data for next calculation - use POST-TRADE balances
        # This ensures the next quarter's signal line is calculated from the correct ending balance
        current_quarter = f"{datetime.datetime.now().year}-Q{((datetime.datetime.now().month-1)//3+1)}"
        save_nine_sig_quarterly_data(
            current_quarter,
            final_tqqq_balance,  # Use post-trade balance for next quarter's calculation
            final_agg_balance,   # Use post-trade balance
            signal_line,
            action,
            quarterly_contributions,
            env=env
        )
        
        # Report final allocations
        updated_total = final_tqqq_balance + final_agg_balance
        print(f"\n=== Final Results ===")
        print(f"Final TQQQ balance: ${final_tqqq_balance:.2f}")
        print(f"Final AGG balance: ${final_agg_balance:.2f}")
        print(f"Final total portfolio: ${updated_total:.2f}")
        if updated_total > 0:
            tqqq_pct = final_tqqq_balance / updated_total
            agg_pct = final_agg_balance / updated_total
            print(f"Final allocation: TQQQ {tqqq_pct:.1%}, AGG {agg_pct:.1%} (Target: 80/20)")
            send_telegram_message(f"9-Sig allocation: TQQQ {tqqq_pct:.1%}, AGG {agg_pct:.1%} (Target: 80/20)")
        print(f"Action taken: {action}")
        print("=" * 40 + "\n")

        mark_quarterly_run_complete("nine_sig", action, env=env)

        return f"9-Sig quarterly signal: {action}"

    except Exception as e:
        error_msg = f"9-Sig quarterly signal failed: {str(e)}"
        print(error_msg)
        send_telegram_message(error_msg)
        return error_msg


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
def send_telegram_message(message):
    """
    Send a message to Telegram. Handles network errors gracefully.
    
    Args:
        message: Message text to send
    
    Returns:
        HTTP status code if successful, None if failed
    """
    try:
        telegram_key, chat_id = get_telegram_secrets()
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
            "rssb_wtip": float,
            "nine_sig": float,
            "dual_momentum": float,
            "regime_sso": float,
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
        
        # RSSB/WTIP: RSSB, WTIP, BIL (holding fund)
        rssb_wtip_value = (
            positions.get("RSSB", 0) +
            positions.get("WTIP", 0) +
            positions.get(rssb_wtip_holding_fund, 0)
        )
        
        # 9-Sig: TQQQ, AGG
        nine_sig_value = (
            positions.get("TQQQ", 0) +
            positions.get("AGG", 0)
        )
        
        # Dual Momentum: SPUU, QLD, EFO, BND (BND shared as defensive)
        dual_momentum_value = (
            positions.get("SPUU", 0) +
            positions.get("QLD", 0) +
            positions.get("EFO", 0) +
            positions.get("BND", 0)
        )
        
        # Regime sleeves: tracked via per-strategy Firestore state so the
        # shared safe asset (USFR) never collides across strategies.
        def _regime_value(cfg):
            state = regime_state(cfg=cfg, env="live")
            risk_qty = state.get("risk_shares", 0) or 0
            safe_qty = state.get("safe_shares", 0) or 0
            v = 0.0
            if risk_qty > 0:
                try:
                    v += risk_qty * float(get_latest_trade(api, cfg["risk_asset"]))
                except Exception:
                    pass
            if safe_qty > 0:
                try:
                    v += safe_qty * float(get_latest_trade(api, cfg["safe_asset"]))
                except Exception:
                    pass
            return v

        regime_sso_value = _regime_value(regime_sso_config)
        regime_world_value = _regime_value(regime_world_config)

        total_value = (
            hfea_value +
            spxl_sma_value +
            rssb_wtip_value +
            nine_sig_value +
            dual_momentum_value +
            regime_sso_value +
            regime_world_value
        )

        return {
            "hfea": hfea_value,
            "spxl_sma": spxl_sma_value,
            "rssb_wtip": rssb_wtip_value,
            "nine_sig": nine_sig_value,
            "dual_momentum": dual_momentum_value,
            "regime_sso": regime_sso_value,
            "regime_world": regime_world_value,
            "total": total_value
        }

    except Exception as e:
        print(f"Error getting all strategy values: {e}")
        return {
            "hfea": 0,
            "spxl_sma": 0,
            "rssb_wtip": 0,
            "nine_sig": 0,
            "dual_momentum": 0,
            "regime_sso": 0,
            "regime_world": 0,
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
        "rssb_wtip": "rssb_wtip_allo",
        "nine_sig": "nine_sig_allo",
        "dual_momentum": "dual_momentum_allo",
        "regime_sso": "regime_sso_allo",
        "regime_world": "regime_world_allo",
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
        "rssb_wtip": "RSSB/WTIP",
        "nine_sig": "9-Sig",
        "dual_momentum": "Dual Momentum",
        "regime_sso": "Regime SSO",
        "regime_world": "Regime World",
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


# ════════════════════════════════════════════════════════════════════
# REGIME DETECTION (SSO/SHV) — full 7-signal composite system
# Methodology: r/LETFs u/Neat_Bug1775. Composite score from 7 macro
# signals; rotates SSO ↔ SHV. Designed to fire ~1.4x/year.
# ════════════════════════════════════════════════════════════════════

_finbert_pipeline = None


def _get_finbert():
    """
    Lazy-init FinBERT (ProsusAI/finbert) sentiment pipeline. Heavy import (torch +
    transformers + ~440MB of model weights at runtime) so we only load on demand.
    Cold-start cost ~30-60 s; daily check runs once per weekday so this is fine.
    """
    global _finbert_pipeline
    if _finbert_pipeline is None:
        from transformers import pipeline
        _finbert_pipeline = pipeline(
            "sentiment-analysis",
            model="ProsusAI/finbert",
            tokenizer="ProsusAI/finbert",
            truncation=True,
            max_length=512,
        )
    return _finbert_pipeline


def get_fred_series(series_id, limit=300):
    """Fetch a FRED series. Returns observations list (newest first) or None."""
    fred_key = get_secret_or_env("FREDKEY")
    if not fred_key:
        return None
    url = (f"https://api.stlouisfed.org/fred/series/observations?"
           f"series_id={series_id}&api_key={fred_key}&file_type=json"
           f"&sort_order=desc&limit={limit}")
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json().get("observations", [])
    except Exception as e:
        print(f"FRED fetch failed for {series_id}: {e}")
        return None


def get_vix_data(days=60):
    """Returns (latest_vix, list_of_recent_vix newest-first) from FRED VIXCLS."""
    obs = get_fred_series("VIXCLS", limit=days)
    if not obs:
        return None, []
    values = []
    for o in obs:
        try:
            values.append(float(o["value"]))
        except (ValueError, KeyError, TypeError):
            continue
    if not values:
        return None, []
    return values[0], values


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


def get_sp500_constituents(env="live"):
    """List of S&P 500 tickers (Wikipedia), cached daily in Firestore."""
    cache_doc_id = "sp500_constituents"
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        doc = (get_firestore_client()
               .collection(f"market-data-{env}")
               .document(cache_doc_id).get())
        if doc.exists:
            data = doc.to_dict()
            if data.get("date") == today and data.get("tickers"):
                return data["tickers"]
    except Exception:
        pass
    try:
        from bs4 import BeautifulSoup
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table", {"id": "constituents"})
        tickers = []
        for row in table.find_all("tr")[1:]:
            cell = row.find("td")
            if cell:
                t = cell.get_text(strip=True).replace(".", "-")
                tickers.append(t)
        if not tickers:
            return []
        try:
            (get_firestore_client()
             .collection(f"market-data-{env}")
             .document(cache_doc_id).set({"date": today, "tickers": tickers}))
        except Exception:
            pass
        return tickers
    except Exception as e:
        print(f"S&P 500 constituents fetch failed: {e}")
        return []


def compute_market_breadth(api, env="live", sample_size=150, cfg=None):
    """% of constituents above their N-SMA.

    cfg["breadth_mode"]:
      "sp500"  → full S&P 500 universe (sampled to sample_size for cost).
      "basket" → cfg["breadth_basket"] tickers (e.g. ex-US country ETFs).
    """
    if cfg is None:
        cfg = regime_sso_config
    mode = cfg.get("breadth_mode", "sp500")
    period = cfg["breadth_sma_period"]

    if mode == "basket":
        tickers = list(cfg.get("breadth_basket") or [])
    else:
        tickers = get_sp500_constituents(env=env)
        if not tickers:
            return None
        if sample_size and sample_size < len(tickers):
            step = max(1, len(tickers) // sample_size)
            tickers = tickers[::step][:sample_size]

    above = 0
    valid = 0
    for sym in tickers:
        try:
            closes = get_alpaca_historical_bars(api, sym, days=period + 30)
            if closes is None or len(closes) < period:
                continue
            window = closes[-period:]
            sma = sum(window) / period
            valid += 1
            if window[-1] > sma:
                above += 1
        except Exception:
            continue
    if valid == 0:
        return None
    return above / valid


def compute_adx_from_bars(bars, period=14):
    """ADX from a list of OHLC bars in Alpaca format."""
    if len(bars) < period * 2 + 1:
        return None
    highs = [float(b["h"]) for b in bars]
    lows = [float(b["l"]) for b in bars]
    closes = [float(b["c"]) for b in bars]
    tr = [max(highs[i] - lows[i],
              abs(highs[i] - closes[i-1]),
              abs(lows[i] - closes[i-1])) for i in range(1, len(bars))]
    plus_dm = []
    minus_dm = []
    for i in range(1, len(bars)):
        up = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)

    def wilder(vals, n):
        if len(vals) < n:
            return []
        first = sum(vals[:n])
        out = [first]
        for v in vals[n:]:
            out.append(out[-1] - out[-1] / n + v)
        return out

    atr = wilder(tr, period)
    plus_smoothed = wilder(plus_dm, period)
    minus_smoothed = wilder(minus_dm, period)
    if not atr or not plus_smoothed or not minus_smoothed:
        return None
    plus_di = [100 * p / a if a > 0 else 0 for p, a in zip(plus_smoothed, atr)]
    minus_di = [100 * m / a if a > 0 else 0 for m, a in zip(minus_smoothed, atr)]
    dx = [100 * abs(p - m) / (p + m) if (p + m) > 0 else 0 for p, m in zip(plus_di, minus_di)]
    if len(dx) < period:
        return None
    adx_initial = sum(dx[:period]) / period
    adx_values = [adx_initial]
    for d in dx[period:]:
        adx_values.append((adx_values[-1] * (period - 1) + d) / period)
    return adx_values[-1]


def get_alpaca_news(api, hours_back=24, limit=80, symbols=None):
    """Fetch recent macro news from Alpaca's news API.

    symbols: optional list of tickers to filter by (e.g. ["URTH","EFA","EEM"]).
    None = full firehose (US-centric macro news).
    """
    headers = get_auth_headers(api)
    after = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = "https://data.alpaca.markets/v1beta1/news"
    articles = []
    next_token = None
    while len(articles) < limit:
        params = {"start": after, "limit": min(50, limit - len(articles))}
        if symbols:
            params["symbols"] = ",".join(symbols)
        if next_token:
            params["page_token"] = next_token
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"Alpaca news fetch failed: {e}")
            break
        items = data.get("news", [])
        if not items:
            break
        articles.extend(items)
        next_token = data.get("next_page_token")
        if not next_token:
            break
    return articles


def score_news_sentiment(articles):
    """
    FinBERT-scored compound sentiment averaged over articles.
    For each article we feed `headline + ". " + summary` (truncated to 512 tokens).
    FinBERT returns label ∈ {positive, negative, neutral} with a confidence in [0, 1].
    We map: positive → +confidence, negative → −confidence, neutral → 0, then average.

    Returns (signed_avg, n_scored, avg_confidence) where:
      - signed_avg ∈ [-1, +1]: net directional sentiment (used for the −1/0/+1 signal)
      - avg_confidence ∈ [0, 1]: how decisive the model was on average
        (used for the Path B "high-confidence sentiment" re-entry check)
    """
    if not articles:
        return 0.0, 0, 0.0
    pipe = _get_finbert()
    texts = []
    for a in articles:
        text = f"{a.get('headline','') or ''}. {a.get('summary','') or ''}".strip()
        if text and text != ".":
            texts.append(text[:2000])  # outer cap, FinBERT will retokenize
    if not texts:
        return 0.0, 0, 0.0
    try:
        results = pipe(texts)
    except Exception as e:
        print(f"FinBERT inference failed: {e}")
        return 0.0, 0, 0.0
    signed = []
    confs = []
    for r in results:
        label = (r.get("label") or "").lower()
        conf = float(r.get("score") or 0.0)
        if label == "positive":
            signed.append(conf)
        elif label == "negative":
            signed.append(-conf)
        else:
            signed.append(0.0)
        confs.append(conf)
    if not signed:
        return 0.0, 0, 0.0
    return sum(signed) / len(signed), len(signed), sum(confs) / len(confs)


# --- The 7 signals (each returns -1, 0, or +1) ---


def signal_price_trend(api, cfg=None, env="live"):
    """
    Signal 1: trend_symbol vs N-SMA with strict 3-day temporal hysteresis.
    The signal only flips after 3 consecutive trading days agree on the new direction.
    Otherwise we hold the prior persisted signal.

    Returns (signal, raw_today, raw_yesterday, raw_2days_ago).
    """
    if cfg is None:
        cfg = regime_sso_config
    symbol = cfg["trend_symbol"]
    period = cfg["spy_sma_period"]
    closes = get_alpaca_historical_bars(api, symbol, days=period + 15)
    if not closes or len(closes) < period:
        return 0, 0, 0, 0

    def _raw(idx):
        if idx < period - 1 or idx >= len(closes):
            return 0
        sma = sum(closes[idx - period + 1:idx + 1]) / period
        if closes[idx] > sma:
            return 1
        if closes[idx] < sma:
            return -1
        return 0

    last3 = [_raw(len(closes) - 1), _raw(len(closes) - 2), _raw(len(closes) - 3)]

    # If the most recent 3 trading days all agree, that's the confirmed signal.
    if last3[0] != 0 and all(s == last3[0] for s in last3):
        return last3[0], last3[0], last3[1], last3[2]

    # Otherwise hold the prior persisted signal (avoid flip-flop on a single crossover).
    history = load_recent_regime_scores(cfg=cfg, days=2, env=env)
    if history:
        prior = history[-1].get("price_trend", 0)
        return prior, last3[0], last3[1], last3[2]
    return 0, last3[0], last3[1], last3[2]


def signal_market_breadth(api, cfg=None, env="live"):
    """
    Signal 2: % of constituents above their N-SMA.
      sp500 mode: S&P 500 (regime_sso)
      basket mode: cfg["breadth_basket"] (regime_world)
    Returns (signal, raw_pct).
    """
    if cfg is None:
        cfg = regime_sso_config
    pct = compute_market_breadth(api, env=env, sample_size=None, cfg=cfg)
    if pct is None:
        return 0, None
    if pct > cfg["breadth_high_threshold"]:
        return 1, pct
    if pct < cfg["breadth_low_threshold"]:
        return -1, pct
    return 0, pct


def signal_volatility_regime(cfg=None):
    """
    Signal 3: VIX level + trajectory (universal — VIX is the global fear gauge).
    Returns (signal, raw_vix_level, vix_5d_pct_change).
    """
    if cfg is None:
        cfg = regime_sso_config
    latest, history = get_vix_data(days=20)
    if latest is None or len(history) < 6:
        return 0, latest, 0.0
    vix_5d_ago = history[5]
    vix_change_pct = (latest - vix_5d_ago) / vix_5d_ago if vix_5d_ago > 0 else 0.0

    if latest > cfg["vix_high"] or vix_change_pct > 0.20:
        return -1, latest, vix_change_pct
    if latest < cfg["vix_low"] and vix_change_pct < 0.10:
        return 1, latest, vix_change_pct
    return 0, latest, vix_change_pct


def signal_trend_strength(api, cfg=None, env="live", price_trend_signal=None):
    """
    Signal 4: ADX > 25 confirms trend; direction inherited from Signal 1.
    Uses cfg["trend_symbol"] (SPY for SSO, URTH for World).
    Returns (signal, raw_adx).
    """
    if cfg is None:
        cfg = regime_sso_config
    symbol = cfg["trend_symbol"]
    bars = get_alpaca_historical_bars(api, symbol, days=60, raw=True)
    if not bars or len(bars) < 30:
        return 0, None
    adx = compute_adx_from_bars(bars, period=cfg["adx_period"])
    if adx is None:
        return 0, None
    if adx > cfg["adx_strong"]:
        if price_trend_signal is None:
            price_trend_signal, _, _, _ = signal_price_trend(api, cfg=cfg, env=env)
        return price_trend_signal, adx
    return 0, adx


def signal_credit_spread(api, cfg=None):
    """
    Signal 5: HYG/LQD ratio vs its 50-SMA (universal — global credit indicator).
    Returns (signal, raw_ratio).
    """
    if cfg is None:
        cfg = regime_sso_config
    period = cfg["credit_sma_period"]
    hyg_closes = get_alpaca_historical_bars(api, "HYG", days=period + 20)
    lqd_closes = get_alpaca_historical_bars(api, "LQD", days=period + 20)
    if not hyg_closes or not lqd_closes or len(hyg_closes) < period or len(lqd_closes) < period:
        return 0, None
    n = min(len(hyg_closes), len(lqd_closes))
    ratios = [h / l for h, l in zip(hyg_closes[-n:], lqd_closes[-n:]) if l > 0]
    if len(ratios) < period:
        return 0, None
    sma = sum(ratios[-period:]) / period
    latest = ratios[-1]
    if latest > sma * 1.002:
        return 1, latest
    if latest < sma * 0.998:
        return -1, latest
    return 0, latest


def signal_news_sentiment(api, cfg=None):
    """
    Signal 6: FinBERT sentiment of last 24h Alpaca news.
    For regime_world, news is filtered to cfg["news_tickers"] (global equity ETFs).
    For regime_sso, cfg["news_tickers"] is None → full Alpaca firehose (US-centric).
    Returns (signal, signed_avg, avg_confidence, n_articles_scored).
    """
    if cfg is None:
        cfg = regime_sso_config
    articles = get_alpaca_news(api,
                                hours_back=cfg["news_lookback_hours"],
                                limit=80,
                                symbols=cfg.get("news_tickers"))
    if len(articles) < cfg["news_min_articles"]:
        return 0, 0.0, 0.0, len(articles)
    signed_avg, n, avg_conf = score_news_sentiment(articles)
    if signed_avg > cfg["news_pos_threshold"]:
        return 1, signed_avg, avg_conf, n
    if signed_avg < cfg["news_neg_threshold"]:
        return -1, signed_avg, avg_conf, n
    return 0, signed_avg, avg_conf, n


def signal_canary_universe(api, cfg=None):
    """
    Signal 7: HYG, EEM, IWM all below/above their 50-SMA = liquidity signal (universal).
    Returns (signal, n_above, n_below).
    """
    if cfg is None:
        cfg = regime_sso_config
    period = cfg["canary_sma_period"]
    above = 0
    below = 0
    valid = 0
    for sym in ("HYG", "EEM", "IWM"):
        closes = get_alpaca_historical_bars(api, sym, days=period + 20)
        if not closes or len(closes) < period:
            continue
        window = closes[-period:]
        sma = sum(window) / period
        valid += 1
        if window[-1] > sma:
            above += 1
        else:
            below += 1
    if valid < 3:
        return 0, above, below
    if below >= 3:
        return -1, above, below
    if above >= 3:
        return 1, above, below
    return 0, above, below


def compute_regime_score(api, cfg=None, env="live"):
    """
    Run all 7 signals + the Fed filter, returning a score dict that includes
    raw values for the re-entry trajectory checks.

    cfg defaults to regime_sso_config for backwards compat.
    """
    if cfg is None:
        cfg = regime_sso_config
    failures = []

    s1, s1_today, s1_yesterday, s1_prior = signal_price_trend(api, cfg=cfg, env=env)
    s2, breadth_pct = signal_market_breadth(api, cfg=cfg, env=env)
    if breadth_pct is None:
        failures.append("market_breadth")
    s3, raw_vix, vix_5d_change = signal_volatility_regime(cfg=cfg)
    if raw_vix is None:
        failures.append("vix")
    s4, raw_adx = signal_trend_strength(api, cfg=cfg, env=env, price_trend_signal=s1)
    if raw_adx is None:
        failures.append("adx")
    s5, raw_credit_ratio = signal_credit_spread(api, cfg=cfg)
    if raw_credit_ratio is None:
        failures.append("credit_spread")
    s6, sentiment_avg, sentiment_conf, sentiment_n = signal_news_sentiment(api, cfg=cfg)
    if sentiment_n < cfg["news_min_articles"]:
        failures.append("news_sentiment")
    s7, canary_above, canary_below = signal_canary_universe(api, cfg=cfg)
    if canary_above + canary_below < 3:
        failures.append("canary_universe")

    composite = s1 + s2 + s3 + s4 + s5 + s6 + s7
    return {
        # The seven signals (each in {-1, 0, +1})
        "price_trend": s1,
        "market_breadth": s2,
        "volatility_regime": s3,
        "trend_strength": s4,
        "credit_spread": s5,
        "news_sentiment": s6,
        "canary_universe": s7,
        "composite": composite,
        # Raw values for trajectory re-checks downstream
        "raw_breadth_pct": breadth_pct,
        "raw_vix": raw_vix,
        "raw_vix_5d_change": vix_5d_change,
        "raw_adx": raw_adx,
        "raw_credit_ratio": raw_credit_ratio,
        "raw_sentiment_avg": sentiment_avg,
        "raw_sentiment_confidence": sentiment_conf,
        "raw_news_n_articles": sentiment_n,
        "raw_canary_above": canary_above,
        "raw_canary_below": canary_below,
        "raw_price_trend_today": s1_today,
        "raw_price_trend_yesterday": s1_yesterday,
        "raw_price_trend_2d_ago": s1_prior,
        # Filters & metadata
        "fed_hike_filter": is_aggressive_rate_hiking_cycle(),
        "signal_failures": failures,
        "computed_at": datetime.datetime.utcnow().isoformat(),
    }


# --- State management ---


def save_regime_score(score, cfg=None, env="live"):
    if cfg is None:
        cfg = regime_sso_config
    coll = cfg["scores_collection"]
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        get_firestore_client().collection(f"{coll}-{env}").document(today).set(score)
    except Exception as e:
        print(f"Failed to persist {cfg['display_name']} score: {e}")


def load_recent_regime_scores(cfg=None, days=40, env="live"):
    """Returns list of score dicts in chronological order (oldest → newest)."""
    if cfg is None:
        cfg = regime_sso_config
    coll = cfg["scores_collection"]
    try:
        docs = (get_firestore_client()
                .collection(f"{coll}-{env}")
                .order_by("computed_at", direction=firestore.Query.DESCENDING)
                .limit(days).stream())
        rows = [d.to_dict() for d in docs]
        rows.reverse()
        return rows
    except Exception as e:
        print(f"Failed to load recent {cfg['display_name']} scores: {e}")
        return []


def regime_state(cfg=None, env="live"):
    """Read the strategy's persisted state, defaulting to in-market."""
    if cfg is None:
        cfg = regime_sso_config
    risk = cfg["risk_asset"]
    try:
        doc = (get_firestore_client()
               .collection(f"strategy-balances-{env}")
               .document(cfg["strategy_key"]).get())
        if doc.exists:
            d = doc.to_dict() or {}
            d.setdefault("position", risk)
            d.setdefault("risk_shares", 0)
            d.setdefault("safe_shares", 0)
            return d
    except Exception:
        pass
    return {"position": risk, "last_change_date": None,
            "risk_shares": 0, "safe_shares": 0, "total_invested": 0}


def save_regime_state(state, cfg=None, env="live"):
    if cfg is None:
        cfg = regime_sso_config
    try:
        get_firestore_client().collection(f"strategy-balances-{env}").document(cfg["strategy_key"]).set(state)
    except Exception as e:
        print(f"Failed to save {cfg['display_name']} state: {e}")


def evaluate_regime_decision(history, current_position, cfg=None):
    """
    Decide what to do given recent score history and current position.
    Returns one of: HOLD, EXIT_SLOW, EXIT_FAST, REENTER_CREDIT_VIX, REENTER_NLP, REENTER_STD.

    Re-entry trajectory checks (Path A, Path B) compare the last-5-days mean of
    a raw value to the first-5-days mean of the lookback window, so we measure
    the *direction of change*, not just the level.
    """
    if cfg is None:
        cfg = regime_sso_config
    risk = cfg["risk_asset"]
    if not history:
        return "HOLD"
    composites = [h.get("composite", 0) for h in history]

    if current_position == risk:
        if len(composites) >= cfg["fast_exit_days"]:
            recent = composites[-cfg["fast_exit_days"]:]
            if all(c <= cfg["fast_exit_score"] for c in recent):
                return "EXIT_FAST"
        if len(composites) >= cfg["slow_exit_days"]:
            recent = composites[-cfg["slow_exit_days"]:]
            if all(c <= cfg["slow_exit_score"] for c in recent):
                return "EXIT_SLOW"
        return "HOLD"

    # Defensive (USFR): block re-entries if Fed is in aggressive hiking cycle.
    # This is the explicit "Fed hiking lock" filter Reddit credits with avoiding
    # 2022's $39K loss from re-entering during bear rallies.
    if history[-1].get("fed_hike_filter"):
        return "HOLD"

    def _trend(window, key):
        """Returns (first_5d_avg, last_5d_avg) for a given raw key, or (None, None)."""
        vals = [h.get(key) for h in window if h.get(key) is not None]
        if len(vals) < 10:
            return None, None
        first = sum(vals[:5]) / 5
        last = sum(vals[-5:]) / 5
        return first, last

    # Path A: Credit-VIX recovery — credit ratio improving AND VIX declining
    # over 4 consecutive weeks AND today's composite > 0.
    days_a = cfg["credit_vix_recovery_weeks"] * 5
    if len(history) >= days_a:
        recent_a = history[-days_a:]
        credit_first, credit_last = _trend(recent_a, "raw_credit_ratio")
        vix_first, vix_last = _trend(recent_a, "raw_vix")
        if (credit_first is not None and vix_first is not None and
                credit_last > credit_first * (1 + cfg["credit_vix_credit_improvement"]) and
                vix_last < vix_first * (1 - cfg["credit_vix_vix_decline"]) and
                composites[-1] > 0):
            return "REENTER_CREDIT_VIX"

    # Path B: NLP-accelerated — composite ≥ +3 for the last 7 trading days AND
    # FinBERT confidence ≥ 0.80 averaged over the last 2 weeks.
    days_b_score = cfg["nlp_acceleration_score_days"]
    days_b_sent = cfg["nlp_acceleration_sentiment_weeks"] * 5
    if len(history) >= max(days_b_score, days_b_sent):
        recent_score = composites[-days_b_score:]
        recent_sent = history[-days_b_sent:]
        confs = [h.get("raw_sentiment_confidence", 0) for h in recent_sent]
        signed = [h.get("raw_sentiment_avg", 0) for h in recent_sent]
        if (all(c >= cfg["reentry_score"] for c in recent_score) and
                len(confs) >= days_b_sent and
                (sum(confs) / len(confs)) >= cfg["nlp_confidence_threshold"] and
                (sum(signed) / len(signed)) > 0):
            return "REENTER_NLP"

    # Path C: Standard mechanical — composite ≥ +3 for 15 consecutive days
    days_c = cfg["standard_reentry_days"]
    if len(composites) >= days_c:
        recent = composites[-days_c:]
        if all(c >= cfg["reentry_score"] for c in recent):
            return "REENTER_STD"

    return "HOLD"


def execute_regime_rotation(api, target, cfg=None, env="live"):
    """Rotate the regime strategy holdings between risk_asset and safe_asset."""
    if cfg is None:
        cfg = regime_sso_config
    name = cfg["display_name"]
    risk = cfg["risk_asset"]
    safe = cfg["safe_asset"]
    state = regime_state(cfg=cfg, env=env)
    held_risk = state.get("risk_shares", 0) or 0
    held_safe = state.get("safe_shares", 0) or 0

    if target == safe and held_risk > 0:
        try:
            order = submit_order(api, risk, held_risk, "sell")
            wait_for_order_fill(api, order["id"])
            order_info = get_order(api, order["id"])
            proceeds = float(order_info.get("filled_avg_price") or 0) * held_risk
            if proceeds > 0:
                safe_price = float(get_latest_trade(api, safe))
                safe_qty = proceeds / safe_price
                buy_order = submit_order(api, safe, safe_qty, "buy")
                wait_for_order_fill(api, buy_order["id"])
                held_safe += safe_qty
                held_risk = 0
            send_telegram_message(f"🛡️ {name}: {risk} → {safe} (defensive rotation, ${proceeds:,.2f} reallocated)")
        except Exception as e:
            send_telegram_message(f"🧭 {name}\n❌ Rotation {risk}→{safe} failed: {e}")
            return f"Rotation failed: {e}"
    elif target == risk and held_safe > 0:
        try:
            order = submit_order(api, safe, held_safe, "sell")
            wait_for_order_fill(api, order["id"])
            order_info = get_order(api, order["id"])
            proceeds = float(order_info.get("filled_avg_price") or 0) * held_safe
            if proceeds > 0:
                risk_price = float(get_latest_trade(api, risk))
                risk_qty = proceeds / risk_price
                buy_order = submit_order(api, risk, risk_qty, "buy")
                wait_for_order_fill(api, buy_order["id"])
                held_risk += risk_qty
                held_safe = 0
            send_telegram_message(f"📈 {name}: {safe} → {risk} (re-entry, ${proceeds:,.2f} reallocated)")
        except Exception as e:
            send_telegram_message(f"🧭 {name}\n❌ Rotation {safe}→{risk} failed: {e}")
            return f"Rotation failed: {e}"

    state.update({
        "position": target,
        "last_change_date": datetime.datetime.now().strftime("%Y-%m-%d"),
        "risk_shares": held_risk,
        "safe_shares": held_safe,
    })
    save_regime_state(state, cfg=cfg, env=env)
    return f"Rotated to {target}"


def daily_regime_check(api, cfg=None, env="live"):
    """Compute today's score, persist, evaluate and rotate if needed."""
    if cfg is None:
        cfg = regime_sso_config
    name = cfg["display_name"]
    try:
        score = compute_regime_score(api, cfg=cfg, env=env)
    except Exception as e:
        send_telegram_message(f"🧭 {name}\n❌ Score computation failed: {e}")
        return f"Score failed: {e}"
    save_regime_score(score, cfg=cfg, env=env)
    history = load_recent_regime_scores(cfg=cfg, days=40, env=env)
    state = regime_state(cfg=cfg, env=env)
    risk = cfg["risk_asset"]
    current = state.get("position", risk)
    decision = evaluate_regime_decision(history, current, cfg=cfg)

    if decision in ("EXIT_SLOW", "EXIT_FAST"):
        execute_regime_rotation(api, cfg["safe_asset"], cfg=cfg, env=env)
    elif decision in ("REENTER_CREDIT_VIX", "REENTER_NLP", "REENTER_STD"):
        execute_regime_rotation(api, cfg["risk_asset"], cfg=cfg, env=env)

    failures = score.get("signal_failures") or []
    sent_avg = score.get("raw_sentiment_avg", 0)
    sent_conf = score.get("raw_sentiment_confidence", 0)
    raw_vix_disp = score.get("raw_vix")
    raw_vix_disp_str = f"{raw_vix_disp:.1f}" if raw_vix_disp else "?"

    msg = (f"🧭 {name} daily | score {score['composite']:+d} | pos {current} | {decision}\n"
           f"  trend {score['price_trend']:+d}  breadth {score['market_breadth']:+d}  "
           f"vol {score['volatility_regime']:+d} (VIX {raw_vix_disp_str})  adx {score['trend_strength']:+d}\n"
           f"  credit {score['credit_spread']:+d}  news {score['news_sentiment']:+d} "
           f"(avg {sent_avg:+.2f}, conf {sent_conf:.2f})  "
           f"canary {score['canary_universe']:+d}  fed_hike={score['fed_hike_filter']}")
    if failures:
        msg += f"\n  ⚠ signal failures: {', '.join(failures)}"
    print(msg)

    # Send Telegram on rotation OR when signals are degraded enough to be untrustworthy
    if decision != "HOLD" or len(failures) >= regime_sso_config["max_signal_failures_before_alert"]:
        send_telegram_message(msg)
    return {"score": score, "decision": decision, "position_before": current,
            "signal_failures": failures}


def backfill_regime_scores(api, cfg=None, days=30, env="live"):
    """
    One-time helper: compute and persist composite scores for the last N trading
    days so the slow exit (15-day window) and re-entry paths have history to read.

    Backfilled scores skip breadth (signal 2) and news (signal 6) — both expensive
    to recompute historically — so the backfilled portion is a 5-signal score
    while the live portion is the full 7-signal score.
    """
    if cfg is None:
        cfg = regime_sso_config
    trend_symbol = cfg["trend_symbol"]
    sma_period = cfg["spy_sma_period"]
    scores_coll = cfg["scores_collection"]
    name = cfg["display_name"]
    print(f"Backfilling {name} scores for last {days} trading days ({trend_symbol} {sma_period}-SMA)...")

    # Pull all the time series we need once. Backfill needs OHLC + timestamps,
    # so request raw bar dicts (not the closes-only default).
    spy_bars = get_alpaca_historical_bars(api, trend_symbol, days=days + sma_period + 20, raw=True)
    hyg_bars = get_alpaca_historical_bars(api, "HYG", days=days + 220, raw=True)
    lqd_bars = get_alpaca_historical_bars(api, "LQD", days=days + 220, raw=True)
    eem_bars = get_alpaca_historical_bars(api, "EEM", days=days + 220, raw=True)
    iwm_bars = get_alpaca_historical_bars(api, "IWM", days=days + 220, raw=True)
    vix_obs = get_fred_series("VIXCLS", limit=days + 30) or []
    vix_by_date = {}
    for o in vix_obs:
        try:
            vix_by_date[o["date"]] = float(o["value"])
        except (ValueError, KeyError, TypeError):
            continue

    if not spy_bars or len(spy_bars) < sma_period:
        return f"Insufficient {trend_symbol} history for backfill"

    written = 0
    last_signal = 0
    # Walk forward through the last N days
    for offset in range(days, 0, -1):
        # Index into each series — bars are oldest-first
        spy_idx = len(spy_bars) - offset
        if spy_idx < sma_period - 1:
            continue
        as_of_iso = spy_bars[spy_idx]["t"][:10]
        # Skip if already persisted
        try:
            existing = (get_firestore_client()
                        .collection(f"{scores_coll}-{env}")
                        .document(as_of_iso).get())
            if existing.exists:
                continue
        except Exception:
            pass

        # Signal 1: trend_symbol SMA + 3-day hysteresis (built from the historical window)
        def _raw1(idx):
            if idx < sma_period - 1 or idx >= len(spy_bars):
                return 0
            closes = [float(b["c"]) for b in spy_bars[idx - sma_period + 1:idx + 1]]
            sma = sum(closes) / sma_period
            close = float(spy_bars[idx]["c"])
            return 1 if close > sma else (-1 if close < sma else 0)

        last3 = [_raw1(spy_idx), _raw1(spy_idx - 1), _raw1(spy_idx - 2)]
        if last3[0] != 0 and all(s == last3[0] for s in last3):
            s1 = last3[0]
        else:
            s1 = last_signal  # carry prior on mixed signal
        last_signal = s1

        # Signal 3: VIX (level + trajectory)
        vix_today = vix_by_date.get(as_of_iso)
        s3 = 0
        raw_vix = None
        vix_5d_change = 0.0
        if vix_today is not None:
            raw_vix = vix_today
            # 5-day change using FRED
            keys = sorted(vix_by_date.keys())
            try:
                pos = keys.index(as_of_iso)
                if pos >= 5:
                    older = vix_by_date[keys[pos - 5]]
                    vix_5d_change = (vix_today - older) / older if older > 0 else 0.0
            except ValueError:
                pass
            if vix_today > cfg["vix_high"] or vix_5d_change > 0.20:
                s3 = -1
            elif vix_today < cfg["vix_low"] and vix_5d_change < 0.10:
                s3 = 1

        # Signal 4: ADX (using SPY bars up to this date)
        s4 = 0
        raw_adx = None
        if spy_idx >= 30:
            adx = compute_adx_from_bars(spy_bars[max(0, spy_idx - 60):spy_idx + 1],
                                         period=cfg["adx_period"])
            raw_adx = adx
            if adx is not None and adx > cfg["adx_strong"]:
                s4 = s1

        # Signal 5: HYG/LQD ratio vs 50-SMA
        s5 = 0
        raw_credit_ratio = None
        period = cfg["credit_sma_period"]
        if (hyg_bars and lqd_bars and
                spy_idx < len(hyg_bars) and spy_idx < len(lqd_bars) and spy_idx >= period):
            ratios = []
            for j in range(spy_idx - period + 1, spy_idx + 1):
                if j < 0 or j >= min(len(hyg_bars), len(lqd_bars)):
                    continue
                lqd_c = float(lqd_bars[j]["c"])
                if lqd_c > 0:
                    ratios.append(float(hyg_bars[j]["c"]) / lqd_c)
            if len(ratios) >= period:
                sma = sum(ratios) / len(ratios)
                latest = ratios[-1]
                raw_credit_ratio = latest
                if latest > sma * 1.002:
                    s5 = 1
                elif latest < sma * 0.998:
                    s5 = -1

        # Signal 7: canary (HYG/EEM/IWM vs 50-SMA)
        s7 = 0
        c_above = 0
        c_below = 0
        for series in (hyg_bars, eem_bars, iwm_bars):
            if not series or spy_idx >= len(series) or spy_idx < cfg["canary_sma_period"]:
                continue
            closes = [float(b["c"]) for b in series[spy_idx - cfg["canary_sma_period"] + 1:spy_idx + 1]]
            if len(closes) < cfg["canary_sma_period"]:
                continue
            sma = sum(closes) / len(closes)
            if closes[-1] > sma:
                c_above += 1
            else:
                c_below += 1
        if c_below >= 3:
            s7 = -1
        elif c_above >= 3:
            s7 = 1

        # Signals 2 (breadth) and 6 (news) skipped during backfill
        s2 = 0
        s6 = 0
        composite = s1 + s2 + s3 + s4 + s5 + s6 + s7

        score = {
            "price_trend": s1,
            "market_breadth": s2,
            "volatility_regime": s3,
            "trend_strength": s4,
            "credit_spread": s5,
            "news_sentiment": s6,
            "canary_universe": s7,
            "composite": composite,
            "raw_breadth_pct": None,
            "raw_vix": raw_vix,
            "raw_vix_5d_change": vix_5d_change,
            "raw_adx": raw_adx,
            "raw_credit_ratio": raw_credit_ratio,
            "raw_sentiment_avg": 0.0,
            "raw_sentiment_confidence": 0.0,
            "raw_news_n_articles": 0,
            "raw_canary_above": c_above,
            "raw_canary_below": c_below,
            "fed_hike_filter": False,
            "signal_failures": ["market_breadth", "news_sentiment"],
            "backfill": True,
            "computed_at": f"{as_of_iso}T00:00:00",
        }
        try:
            (get_firestore_client()
             .collection(f"{scores_coll}-{env}")
             .document(as_of_iso).set(score))
            written += 1
        except Exception as e:
            print(f"  failed to persist backfill {as_of_iso}: {e}")

    msg = f"🧭 {name}: backfilled {written} trading days of score history"
    print(msg)
    send_telegram_message(msg)
    return msg


def make_monthly_buys_regime(api, cfg=None, force_execute=False, investment_calc=None,
                              margin_result=None, skip_order_wait=False, env="live"):
    """Add this month's allocation to whichever asset (risk or safe) the regime is in."""
    if cfg is None:
        cfg = regime_sso_config
    if not force_execute and not check_trading_day(mode="monthly"):
        return "Not first trading day of the month"

    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)

    alloc_key = cfg["alloc_key"]
    name = cfg["display_name"]
    investment_amount = investment_calc["strategy_amounts"].get(alloc_key, 0)
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    buying_power = investment_calc["total_available"] + investment_calc["margin_approved"]
    pct_label = strategy_allocations.get(alloc_key, 0) * 100

    def _skip(reason):
        msg = f"🧭 {name} ({pct_label:.2f}%) — ${investment_amount:,.2f}\n⏭ {reason}"
        send_telegram_message(msg)
        print(reason)
        return reason

    # When margin gates fail and leverage > 1, skip; otherwise we still buy
    # (SHV is a bond, SSO is leveraged so we apply the projected-leverage check below).
    if target_margin == 0 and leverage > 1.0:
        return _skip(f"Skipped — deleveraging required ({leverage:.2f}x)")
    if buying_power < investment_amount:
        return _skip(f"Skipped — insufficient buying power (${buying_power:,.2f})")
    if investment_amount < margin_control_config["min_investment"]:
        return _skip(f"Skipped — ${investment_amount:.2f} below $1.00 minimum")

    state = regime_state(cfg=cfg, env=env)
    risk = cfg["risk_asset"]
    target = state.get("position", risk)

    # Projected leverage check only applies when buying the risk_asset (leveraged ETF)
    if target == risk and target_margin > 0:
        portfolio_value = metrics.get("portfolio_value", 0)
        equity = metrics.get("equity", 0)
        if portfolio_value > 0 and equity > 0:
            projected_leverage = (portfolio_value + investment_amount) / equity
            if projected_leverage >= margin_control_config["max_leverage"]:
                return _skip(f"Skipped — projected leverage {projected_leverage:.3f}x exceeds limit")

    try:
        price = float(get_latest_trade(api, target))
        qty = investment_amount / price
        order = submit_order(api, target, qty, "buy")
        if not skip_order_wait:
            wait_for_order_fill(api, order["id"])
    except Exception as e:
        send_telegram_message(f"🧭 {name}\n❌ Error buying {target}: {e}")
        return f"Failed to buy {target}: {e}"

    if target == risk:
        state["risk_shares"] = state.get("risk_shares", 0) + qty
    else:
        state["safe_shares"] = state.get("safe_shares", 0) + qty
    state["total_invested"] = state.get("total_invested", 0) + investment_amount
    state["last_buy_date"] = datetime.datetime.now().strftime("%Y-%m-%d")
    save_regime_state(state, cfg=cfg, env=env)

    send_telegram_message(
        f"🧭 {name} ({pct_label:.2f}%) — ${investment_amount:,.2f}\n"
        f"Bought {qty:.4f} {target} @ ${price:.2f}\n"
        f"Position: {target}"
    )
    return f"{name} bought {qty:.4f} {target}"


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
    Orchestrator function that runs all six monthly investment strategies.
    Calculates budgets ONCE and distributes them to ensure exact percentage splits.
    
    This prevents the problem of each function independently calculating and over-spending.
    
    Args:
        api: Alpaca API credentials
        force_execute: Bypass trading day check for testing
    
    Returns:
        dict with results from all six strategies
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        print("Not first trading day of the month")
        return {"error": "Not first trading day of the month"}
    
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
    print(f"  RSSB/WTIP ({get_pct('rssb_wtip_allo'):.1f}%): ${strategy_amounts['rssb_wtip_allo']:.2f}")
    print(f"  9-Sig ({get_pct('nine_sig_allo'):.1f}%): ${strategy_amounts['nine_sig_allo']:.2f}")
    print(f"  Dual Momentum ({get_pct('dual_momentum_allo'):.1f}%): ${strategy_amounts['dual_momentum_allo']:.2f}")
    print(f"  Regime SSO ({get_pct('regime_sso_allo'):.1f}%): ${strategy_amounts['regime_sso_allo']:.2f}")
    print(f"  Regime World ({get_pct('regime_world_allo'):.1f}%): ${strategy_amounts['regime_world_allo']:.2f}")
    
    # Send one shared account status message to Telegram before executing strategies
    metrics = margin_result.get("metrics", {})
    gate_results = margin_result.get("gate_results", {})
    trend_emoji = "✅" if gate_results.get("market_trend", False) else "❌"
    spx_price = metrics.get("spx_price", 0)
    spx_sma = metrics.get("spx_sma", 0)
    margin_rate = metrics.get("margin_rate", 0)
    buffer = metrics.get("buffer", 0)
    leverage = metrics.get("leverage", 0)
    equity = metrics.get("equity", 0)
    portfolio_value = metrics.get("portfolio_value", 0)
    margin_decision = "🟢 Margin ON (+10%)" if margin_result.get("allowed", False) else "🔴 Cash-Only"
    
    account_msg = "📊 Monthly Investment — Account Status\n\n"
    account_msg += f"SPX: ${spx_price:,.2f} (SMA: ${spx_sma:,.2f}) {trend_emoji}\n"
    account_msg += f"Margin rate: {margin_rate*100:.1f}% | Buffer: {buffer*100:.1f}% | Leverage: {leverage:.2f}x\n"
    account_msg += f"Decision: {margin_decision}\n\n"
    account_msg += f"Equity: ${equity:,.2f} | Portfolio: ${portfolio_value:,.2f}\n"
    account_msg += f"Investing: ${total_investing:,.2f}\n\n"
    
    # Show per-strategy budget breakdown
    account_msg += "Budget per strategy:\n"
    for label, key in [
        ("HFEA 15%", "hfea_allo"),
        ("SPXL SMA 15%", "spxl_allo"),
        ("RSSB/WTIP 10%", "rssb_wtip_allo"),
        ("9-Sig 5%", "nine_sig_allo"),
        ("Dual Momentum 20%", "dual_momentum_allo"),
        ("Regime SSO 15%", "regime_sso_allo"),
        ("Regime World 20%", "regime_world_allo"),
    ]:
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
    _run("rssb_wtip", "RSSB/WTIP", lambda: make_monthly_buys_rssb_wtip(api, force_execute, investment_calc, margin_result, skip_order_wait, env))
    _run("nine_sig", "9-Sig", lambda: make_monthly_nine_sig_contributions(api, force_execute, investment_calc, margin_result, skip_order_wait, env))
    _run("dual_momentum", "Dual Momentum", lambda: monthly_dual_momentum_strategy(api, force_execute, investment_calc, margin_result, skip_order_wait, env))
    _run("regime_sso", "Regime SSO", lambda: make_monthly_buys_regime(api, cfg=regime_sso_config, force_execute=force_execute, investment_calc=investment_calc, margin_result=margin_result, skip_order_wait=skip_order_wait, env=env))
    _run("regime_world", "Regime World", lambda: make_monthly_buys_regime(api, cfg=regime_world_config, force_execute=force_execute, investment_calc=investment_calc, margin_result=margin_result, skip_order_wait=skip_order_wait, env=env))

    print("\n=== All Monthly Strategies Complete ===")

    # Send a summary so a missing strategy is impossible to overlook
    summary_lines = ["📋 Monthly Orchestrator Summary"]
    label_map = {
        "hfea": "HFEA",
        "spxl": "SPXL SMA",
        "rssb_wtip": "RSSB/WTIP",
        "nine_sig": "9-Sig",
        "dual_momentum": "Dual Momentum",
        "regime_sso": "Regime SSO",
        "regime_world": "Regime World",
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

    mark_monthly_run_complete(env=env)

    return results


def test_monthly_buy_rssb_wtip(api, investment_amount=10.0, force_execute=True, skip_order_wait=False, env="live"):
    """
    Test function to run RSSB/WTIP monthly buy with a custom investment amount.
    Useful for testing the strategy with small amounts (e.g., $10).
    
    Args:
        api: Alpaca API credentials
        investment_amount: Investment amount in dollars (default: $10.0)
        force_execute: Bypass trading day check (default: True for testing)
        skip_order_wait: Skip waiting for order fills (default: False)
        env: Environment ("live" or "paper")
    
    Returns:
        Result from make_monthly_buys_rssb_wtip function
    """
    print("=== Testing RSSB/WTIP Monthly Buy ===")
    print(f"Investment amount: ${investment_amount:.2f}")
    
    # Step 1: Sync cost basis from Alpaca to Firestore BEFORE executing trades
    print("\nStep 1: Syncing cost basis from Alpaca to Firestore...")
    cost_basis_result = recalculate_all_strategies_cost_basis(api, env, silent=True)
    if cost_basis_result.get("success"):
        if cost_basis_result.get("total_difference", 0) != 0:
            print(f"✅ Cost basis synced: ${cost_basis_result['total_difference']:.2f} correction applied")
        else:
            print("✅ Cost basis already in sync")
    else:
        print(f"⚠️  Warning: Cost basis sync had issues: {cost_basis_result.get('error', 'Unknown error')}")
    
    # Step 2: Get margin conditions (needed for strategy function)
    print("\nStep 2: Getting margin conditions...")
    margin_result = check_margin_conditions(api, env=env)
    
    # Step 3: Create custom investment_calc dict with only RSSB/WTIP strategy
    # The function expects this structure but we'll override the amount
    investment_calc = {
        "total_cash": investment_amount,
        "total_reserved": 0,
        "total_available": investment_amount,
        "margin_approved": 0,
        "used_margin": 0,
        "total_investing": investment_amount,
        "strategy_amounts": {
            "rssb_wtip_allo": investment_amount,
            # Set other strategies to 0 (they won't be called anyway)
            "hfea_allo": 0,
            "spxl_allo": 0,
            "nine_sig_allo": 0,
            "dual_momentum_allo": 0,
            "regime_sso_allo": 0,
        },
        "reserved_amounts": {}
    }
    
    # Step 4: Run RSSB/WTIP strategy with custom investment amount
    print("\n=== Executing RSSB/WTIP Monthly Buy ===")
    result = make_monthly_buys_rssb_wtip(
        api, 
        force_execute=force_execute, 
        investment_calc=investment_calc, 
        margin_result=margin_result, 
        skip_order_wait=skip_order_wait, 
        env=env
    )
    
    print("\n=== RSSB/WTIP Test Complete ===")
    
    return result


@app.route("/monthly_invest_all", methods=["POST"])
def monthly_invest_all(request):
    """
    Orchestrator endpoint that runs all three monthly strategies in one coordinated execution.
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


@app.route("/monthly_buy_rssb_wtip", methods=["POST"])
def monthly_buy_rssb_wtip(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return make_monthly_buys_rssb_wtip(api, env=alpaca_environment)


@app.route("/rebalance_rssb_wtip", methods=["POST"])
def rebalance_rssb_wtip(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return rebalance_rssb_wtip_portfolio(api)


@app.route("/monthly_nine_sig_contributions", methods=["POST"])
def monthly_nine_sig_contributions(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return make_monthly_nine_sig_contributions(api, env=alpaca_environment)


@app.route("/quarterly_nine_sig_signal", methods=["POST"])
def quarterly_nine_sig_signal(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return execute_quarterly_nine_sig_signal(api, env=alpaca_environment)


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


@app.route("/daily_regime_check", methods=["POST"])
def daily_regime_check_route(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return daily_regime_check(api, cfg=regime_sso_config, env=alpaca_environment)


@app.route("/monthly_buy_regime_sso", methods=["POST"])
def monthly_buy_regime_sso(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return make_monthly_buys_regime(api, cfg=regime_sso_config, env=alpaca_environment)


@app.route("/backfill_regime_scores", methods=["POST"])
def backfill_regime_scores_route(request):
    """One-shot endpoint: backfill ~30 trading days of historical regime_sso scores."""
    api = set_alpaca_environment(env=alpaca_environment)
    return backfill_regime_scores(api, cfg=regime_sso_config, days=30, env=alpaca_environment)


@app.route("/daily_regime_world_check", methods=["POST"])
def daily_regime_world_check_route(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return daily_regime_check(api, cfg=regime_world_config, env=alpaca_environment)


@app.route("/monthly_buy_regime_world", methods=["POST"])
def monthly_buy_regime_world(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return make_monthly_buys_regime(api, cfg=regime_world_config, env=alpaca_environment)


@app.route("/backfill_regime_world_scores", methods=["POST"])
def backfill_regime_world_scores_route(request):
    """One-shot endpoint: backfill ~30 trading days of historical regime_world scores."""
    api = set_alpaca_environment(env=alpaca_environment)
    return backfill_regime_scores(api, cfg=regime_world_config, days=30, env=alpaca_environment)


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
        "HFEA": ["UPRO", "TMF", "KMLM"],
        "SPXL SMA": ["SPXL", "SGOV"],
        "RSSB/WTIP": ["RSSB", "WTIP", "BIL"],
        "9-Sig": ["TQQQ", "AGG"],
        "Dual Momentum": ["SPUU", "QLD", "EFO", "BND"],
        "Regime SSO": [regime_sso_config["risk_asset"], regime_sso_config["safe_asset"]],
        "Regime World": [regime_world_config["risk_asset"], regime_world_config["safe_asset"]],
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


def run_local(action, env="paper", request="test", force_execute=False, investment_amount=None):
    api = set_alpaca_environment(env=env, use_secret_manager=False)
    if action == "monthly_invest_all":
        return monthly_invest_all_strategies(api, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "monthly_buy_hfea":
        return make_monthly_buys(api, force_execute=force_execute)
    elif action == "rebalance_hfea":
        return rebalance_portfolio(api)
    elif action == "monthly_nine_sig_contributions":
        return make_monthly_nine_sig_contributions(api, force_execute=force_execute, env=env)
    elif action == "quarterly_nine_sig_signal":
        return execute_quarterly_nine_sig_signal(api, force_execute=force_execute, env=env)
    elif action == "monthly_buy_spxl":
        return monthly_buying_sma(api, "SPXL", force_execute=force_execute, env=env)
    elif action in ("sell_spxl_below_200sma", "buy_spxl_above_200sma"):
        return daily_trade_sma(api, "SPXL", env=env)
    elif action == "index_alert":
        return check_unified_index_alert(request, env=env)
    elif action == "monthly_dual_momentum":
        return monthly_dual_momentum_strategy(api, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "monthly_buy_regime_sso":
        return make_monthly_buys_regime(api, cfg=regime_sso_config, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "daily_regime_check":
        return daily_regime_check(api, cfg=regime_sso_config, env=env)
    elif action == "monthly_buy_regime_world":
        return make_monthly_buys_regime(api, cfg=regime_world_config, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "daily_regime_world_check":
        return daily_regime_check(api, cfg=regime_world_config, env=env)
    elif action == "backfill_regime_sso_scores":
        return backfill_regime_scores(api, cfg=regime_sso_config, days=30, env=env)
    elif action == "backfill_regime_world_scores":
        return backfill_regime_scores(api, cfg=regime_world_config, days=30, env=env)
    elif action == "test_monthly_buy_rssb_wtip":
        # Test RSSB/WTIP monthly buy with custom investment amount (default: $10)
        investment = investment_amount if investment_amount is not None else 10.0
        return test_monthly_buy_rssb_wtip(api, investment_amount=investment, force_execute=True, skip_order_wait=True, env=env)
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
            "monthly_nine_sig_contributions",
            "quarterly_nine_sig_signal",
            "monthly_buy_spxl",
            "sell_spxl_below_200sma",
            "buy_spxl_above_200sma",
            "index_alert",
            "monthly_dual_momentum",
            "monthly_buy_regime_sso",
            "daily_regime_check",
            "monthly_buy_regime_world",
            "daily_regime_world_check",
            "backfill_regime_sso_scores",
            "backfill_regime_world_scores",
            "test_monthly_buy_rssb_wtip"
        ],
        required=True,
        help="Action to perform: 'monthly_invest_all' runs all five monthly strategies with coordinated budgets (recommended)",
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
        help="Investment amount for test_monthly_buy_rssb_wtip (default: $10.0)",
    )
    args = parser.parse_args()

    # Run the function locally
    result = run_local(action=args.action, env=args.env, force_execute=args.force, investment_amount=args.investment_amount)
    print(f"\nResult: {result}\n")
