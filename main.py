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
    "hfea_allo": 0.1625,      # 16.25% to HFEA (reduced from 18.75%)
    "golden_hfea_lite_allo": 0.1625,  # 16.25% to Golden HFEA Lite (reduced from 18.75%)
    "spxl_allo": 0.325,       # 32.5% to SPXL SMA (reduced from 37.5%)
    "rssb_wtip_allo": 0.10,  # 10% to RSSB/WTIP strategy
    "nine_sig_allo": 0.05,   # 5% to 9-Sig strategy
    "dual_momentum_allo": 0.10,  # 10% to Dual Momentum strategy
    "sector_momentum_allo": 0.10,  # 10% to Sector Momentum strategy
}

# Strategy would be to allocate 50% to the SPXL SMA 200 Strategy and 50% to HFEA

# tqqq_investment_amount = monthly_invest * 0.1

upro_allocation = 0.45
tmf_allocation = 0.25
kmlm_allocation = 0.3
# Based on this https://www.reddit.com/r/LETFs/comments/1dyl49a/2024_rletfs_best_portfolio_competition_results/
# and this: https://testfol.io/?d=eJyNT9tKw0AQ%2FZUyzxGStBUaEEGkL1otog8iJYzJJF072a2TtbWE%2FLsTQy8igss%2B7M45cy4NlOxekecoWNWQNFB7FJ%2Fm6AkSiCaT0VkY6YUAyOb7eRzGx3m%2FsUGGJAr1BID5W2psweiNs5AUyDUFkGG9LNhtIQmPn7QQelfFZ0LhnaqJYza2TLfG5h33PGwDWDvxhWPjNOJLAxarLsUV2WxZoax0zdgN1f7abEyuOZXm5UM9hbQc2oymvc2ds6Rsb7IVSS%2FWvxWr1zsvCq5JMrL%2Bu027CCAXLDVzGxyMn%2BYP94Ob2e1s8Dib%2Ft%2F80PFv%2B0u%2BGJ5GGI072wNnVXH1eYoPwx%2B4Z%2F9bIx6ftli0X39%2BpPY%3D

# Golden HFEA Lite allocation (SSO/ZROZ/GLD at 50/25/25)
sso_allocation = 0.50
zroz_allocation = 0.25
gld_allocation = 0.25

# RSSB/WTIP allocation (80/20)
rssb_allocation = 0.80
wtip_allocation = 0.20

# RSSB/WTIP holding fund config (for accumulating funds when WTIP can't be bought)
rssb_wtip_holding_fund = "BIL"
rssb_wtip_holding_fund_max = 70.0  # $70 maximum

# SPXL SMA holding fund config (for T-bills when SPY < 200-SMA)
spxl_sma_holding_fund = "SGOV"  # iShares 0-3 Month Treasury Bond ETF

# Strategy Ticker Ownership
# Each strategy has clear ticker ownership for simplified margin calculations and position tracking:
# - HFEA: UPRO, TMF, KMLM
# - Golden HFEA Lite: SSO, ZROZ, GLD
# - SPXL SMA: SPXL, SGOV (SGOV is holding fund when bearish)
# - RSSB/WTIP: RSSB, WTIP, BIL (BIL is holding fund for uninvested WTIP amounts)
# - 9-Sig: TQQQ, AGG
# - Dual Momentum: SPUU, EFO, BND
# - Sector Momentum: ROM, UYG, DIG, RXL, UXI, UGE, UCC, UPW, UYM, URE, LTL, SCHZ, SHV (SHV is holding fund)

# Strategy ticker ownership mapping for cost basis recalculation
STRATEGY_SYMBOLS = {
    "hfea": ["UPRO", "TMF", "KMLM"],
    "golden_hfea_lite": ["SSO", "ZROZ", "GLD"],
    "spxl_sma": ["SPXL", "SGOV"],
    "rssb_wtip": ["RSSB", "WTIP", "BIL"],
    "nine_sig": ["TQQQ", "AGG"],
    "dual_momentum": ["SPUU", "EFO", "BND"],
    "sector_momentum": ["ROM", "UYG", "DIG", "RXL", "UXI", "UGE", "UCC", "UPW", "UYM", "URE", "LTL", "SCHZ", "SHV"]
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

# Sector Momentum Strategy configuration
# Using 2x leveraged ETFs for enhanced returns
sector_momentum_config = {
    "sector_etfs": [
        "ROM",   # Technology (2x leveraged - ProShares Ultra Technology)
        "UYG",   # Financials (2x leveraged - ProShares Ultra Financials)
        "DIG",   # Energy (2x leveraged - ProShares Ultra Energy)
        "RXL",   # Healthcare (2x leveraged - ProShares Ultra Health Care)
        "UXI",   # Industrials (2x leveraged - ProShares Ultra Industrials)
        "UGE",   # Consumer Staples (2x leveraged - ProShares Ultra Consumer Staples)
        "UCC",   # Consumer Discretionary (2x leveraged - ProShares Ultra Cons. Discretionary)
        "UPW",   # Utilities (2x leveraged - ProShares Ultra Utilities)
        "UYM",   # Materials (2x leveraged - ProShares Ultra Materials)
        "URE",   # Real Estate (2x leveraged - ProShares Ultra Real Estate)
        "LTL"    # Communication Services (2x leveraged - ProShares Ultra Comm. Services)
    ],
    "sector_names": {
        "ROM": "Technology",
        "UYG": "Financials", 
        "DIG": "Energy",
        "RXL": "Healthcare",
        "UXI": "Industrials",
        "UGE": "Consumer Staples",
        "UCC": "Consumer Discretionary",
        "UPW": "Utilities",
        "UYM": "Materials",
        "URE": "Real Estate",
        "LTL": "Communication Services"
    },
    "bond_etf": "SCHZ",  # Bond ETF for bearish periods
    "momentum_weights": {
        "1_month": 0.40,   # 40% weight for 1-month momentum
        "3_month": 0.20,   # 20% weight for 3-month momentum
        "6_month": 0.20,   # 20% weight for 6-month momentum
        "12_month": 0.20   # 20% weight for 12-month momentum
    },
    "lookback_periods": {
        "1_month": 21,     # 21 trading days
        "3_month": 63,     # 63 trading days
        "6_month": 126,    # 126 trading days
        "12_month": 252    # 252 trading days
    },
    "top_sectors_count": 3,         # Select top 3 sectors
    "target_allocation_per_sector": 0.3333,  # 33.33% each
    "spy_sma_period": 200,         # SPY 200-day SMA for trend filter
    "holding_fund_ticker": "SHV",  # Holding fund for accumulating funds when sector ETFs can't be bought
    "holding_fund_max": 250.0,     # $250 maximum
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


def get_cached_market_data(symbol, data_type, env="live"):
    """
    Get cached market data from Firestore to avoid redundant Alpaca API calls.
    Cache expires after 5 minutes. Works across all Cloud Functions.
    
    Args:
        symbol: Market symbol (e.g., "SPY", "URTH", "EEM", "EFA")
        data_type: "price", "sma200", "sma255", or state fields
        env: Environment ("live" or "paper") - determines Firestore collection
    
    Returns:
        Cached value or None if not cached/expired/unavailable
    """
    try:
        # Normalize symbol for Firestore document ID (remove special chars)
        doc_id = symbol.replace("^", "").replace(".", "_")
        
        doc_ref = get_firestore_client().collection(f"market-data-{env}").document(doc_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return None
        
        data = doc.to_dict()
        
        # Check if cache is still fresh
        timestamp = data.get("timestamp")
        if timestamp:
            # Convert both to naive UTC for comparison (handles timezone-aware Firestore timestamps)
            if hasattr(timestamp, 'tzinfo') and timestamp.tzinfo is not None:
                timestamp = timestamp.replace(tzinfo=None)
            
            now_utc = datetime.datetime.utcnow()
            age_seconds = (now_utc - timestamp).total_seconds()
            
            if age_seconds > (CACHE_DURATION_MINUTES * 60):
                return None  # Expired
        
        # Return the requested data type
        return data.get(data_type)
        
    except Exception as e:
        print(f"Warning: Could not read market data cache for {symbol}.{data_type}: {e}")
        return None


def get_all_market_data(symbol, env="live"):
    """
    Get ALL market data for a symbol efficiently.
    Use this when you need multiple metrics (price, sma200, sma255, states).
    If cache is stale, fetches fresh and calculates all metrics at once.
    
    Args:
        symbol: Stock symbol (e.g., "SPY", "URTH")
        env: Environment ("live" or "paper") - determines Firestore collection
    
    Returns:
        dict with all market data: price, sma200, sma255, sma200_state, sma255_state, timestamp
        Or None if cache is stale (triggers update)
    
    Example:
        data = get_all_market_data("SPY", env="live")
        if data is None:
            data = update_market_data("SPY", env="live")
        spy_price = data["price"]
        spy_sma = data["sma200"]
    """
    try:
        # Normalize symbol for Firestore document ID
        doc_id = symbol.replace("^", "").replace(".", "_")
        
        doc_ref = get_firestore_client().collection(f"market-data-{env}").document(doc_id)
        doc = doc_ref.get()
        
        if not doc.exists:
            return None
        
        data = doc.to_dict()
        
        # Check if cache is still fresh
        timestamp = data.get("timestamp")
        if timestamp:
            # Convert both to naive UTC for comparison (handles timezone-aware Firestore timestamps)
            if hasattr(timestamp, 'tzinfo') and timestamp.tzinfo is not None:
                timestamp = timestamp.replace(tzinfo=None)
            
            now_utc = datetime.datetime.utcnow()
            age_seconds = (now_utc - timestamp).total_seconds()
            
            if age_seconds > (CACHE_DURATION_MINUTES * 60):
                return None  # Expired - caller should update
        
        return data
        
    except Exception as e:
        print(f"Warning: Could not read market data for {symbol}: {e}")
        return None


def set_cached_market_data(symbol, data_type, value, env="live"):
    """
    Cache market data to Firestore to avoid redundant Alpaca API calls.
    Accessible across all Cloud Functions. Automatically expires after 5 minutes.
    
    Args:
        symbol: Market symbol
        data_type: "price", "sma200", or "sma255"
        value: Data value to cache
        env: Environment ("live" or "paper") - determines Firestore collection
    """
    try:
        # Normalize symbol for Firestore document ID (remove special chars)
        doc_id = symbol.replace("^", "").replace(".", "_")
        
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
    """
    Create a requests session with retry logic for handling SSL errors and connection issues.
    
    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        backoff_factor: Backoff multiplier for retries (default: 1.0)
        timeout: Request timeout in seconds (default: 30)
    
    Returns:
        requests.Session with retry adapter configured
    """
    from urllib3.util.retry import Retry
    from requests.adapters import HTTPAdapter
    
    # Configure retry strategy
    # Retry on connection errors, read errors, and 5xx server errors
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=[500, 502, 503, 504],  # Retry on server errors
        allowed_methods=["GET", "POST"],  # Only retry safe methods
        raise_on_status=False,  # Don't raise on status, let requests handle it
        connect=max_retries,  # Retry on connection errors
        read=max_retries,  # Retry on read errors
    )
    
    # Create session with retry adapter
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    
    return session


def get_alpaca_historical_bars(api, symbol, days=400):
    """
    Fetch historical daily bars from Alpaca using IEX feed.
    Primary data source for all SMA calculations (no rate limiting).
    Includes explicit SSL error handling with retries.
    
    Args:
        api: Alpaca API credentials dict
        symbol: Stock symbol (e.g., "SPY", "URTH")
        days: Number of calendar days of history to fetch (default 400 for 200-day SMA)
    
    Returns:
        List of closing prices (most recent last), or None on error
    """
    from datetime import datetime, timedelta
    from requests.exceptions import SSLError, ConnectionError, RequestException
    from urllib3.exceptions import SSLError as URLLib3SSLError, MaxRetryError
    
    market_data_base_url = "https://data.alpaca.markets"
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    
    url = f"{market_data_base_url}/v2/stocks/{symbol}/bars"
    params = {
        "start": start_date.strftime("%Y-%m-%d"),
        "end": end_date.strftime("%Y-%m-%d"),
        "timeframe": "1Day",
        "limit": 10000,
        "adjustment": "split",
        "feed": "iex"  # Use IEX feed (included with Basic subscription)
    }
    
    # Manual retry loop for SSL errors with exponential backoff
    # Note: urllib3 retry happens first, then we retry manually if it fails
    max_retries = 5
    for attempt in range(max_retries):
        try:
            # Use retry session with fewer retries since we're doing manual retries
            session = get_retry_session(max_retries=2, backoff_factor=1.0, timeout=60)
            response = session.get(
                url,
                headers=get_auth_headers(api),
                params=params,
                timeout=60  # Longer timeout for large data requests
            )
            response.raise_for_status()
            
            data = response.json()
            bars = data.get("bars", [])
            
            if not bars:
                print(f"No Alpaca bars returned for {symbol}")
                return None
            
            # Extract closing prices
            closes = [bar['c'] for bar in bars]
            print(f"Fetched {len(closes)} bars for {symbol} from Alpaca IEX feed")
            return closes
            
        except (SSLError, URLLib3SSLError, ConnectionError, MaxRetryError) as e:
            # SSL, connection error, or retry exhaustion - retry with exponential backoff
            # Check if it's an SSL-related error (even if wrapped in MaxRetryError)
            is_ssl_error = (
                isinstance(e, (SSLError, URLLib3SSLError)) or
                (isinstance(e, MaxRetryError) and 
                 (hasattr(e, 'reason') and isinstance(e.reason, (SSLError, URLLib3SSLError))) or
                 'SSL' in str(e) or 'SSL' in str(type(e)))
            )
            
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s, 8s, 16s
                error_type = "SSL/Connection" if is_ssl_error else "Connection"
                print(f"{error_type} error for {symbol} (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                print(f"Alpaca historical fetch failed for {symbol} after {max_retries} attempts: {e}")
                return None
        except RequestException as e:
            # Check if it's an SSL-related RequestException
            if 'SSL' in str(e) or isinstance(getattr(e, 'args', [None])[0] if e.args else None, (SSLError, URLLib3SSLError)):
                # SSL-related request error - retry
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"SSL-related request error for {symbol} (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
            # Other request errors - don't retry
            print(f"Alpaca historical fetch failed for {symbol}: {e}")
            return None
        except Exception as e:
            # Check if it's an SSL-related error in the message
            if 'SSL' in str(e) or 'SSLError' in str(type(e)):
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"SSL error (unexpected type) for {symbol} (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
            # Unexpected errors
            print(f"Unexpected error fetching Alpaca data for {symbol}: {e}")
            return None
    
    return None


def get_latest_trade(api, symbol):
    """
    Get latest trade price from Alpaca.
    No fallback - raises error if Alpaca data unavailable.
    Includes explicit SSL error handling with retries.
    
    Args:
        api: Alpaca API credentials dict
        symbol: Stock symbol
    
    Returns:
        Latest trade price
    """
    from requests.exceptions import SSLError, ConnectionError, RequestException
    from urllib3.exceptions import SSLError as URLLib3SSLError, MaxRetryError
    
    symbol = symbol.upper()
    market_data_base_url = "https://data.alpaca.markets"
    url = f"{market_data_base_url}/v2/stocks/{symbol}/trades/latest"
    
    # Manual retry loop for SSL errors with exponential backoff
    # Note: urllib3 retry happens first, then we retry manually if it fails
    max_retries = 5
    for attempt in range(max_retries):
        try:
            # Use retry session with fewer retries since we're doing manual retries
            session = get_retry_session(max_retries=2, backoff_factor=1.0, timeout=60)
            response = session.get(
                url,
                headers=get_auth_headers(api),
                timeout=60
            )
            response.raise_for_status()
            return response.json()["trade"]["p"]
            
        except (SSLError, URLLib3SSLError, ConnectionError, MaxRetryError) as e:
            # SSL, connection error, or retry exhaustion - retry with exponential backoff
            # Check if it's an SSL-related error (even if wrapped in MaxRetryError)
            is_ssl_error = (
                isinstance(e, (SSLError, URLLib3SSLError)) or
                (isinstance(e, MaxRetryError) and 
                 (hasattr(e, 'reason') and isinstance(e.reason, (SSLError, URLLib3SSLError))) or
                 'SSL' in str(e) or 'SSL' in str(type(e)))
            )
            
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s, 8s, 16s
                error_type = "SSL/Connection" if is_ssl_error else "Connection"
                print(f"{error_type} error for {symbol} latest trade (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                error_msg = f"Error fetching latest trade for {symbol} after {max_retries} attempts: {e}"
                print(error_msg)
                raise Exception(error_msg)
        except RequestException as e:
            # Check if it's an SSL-related RequestException
            if 'SSL' in str(e) or isinstance(getattr(e, 'args', [None])[0] if e.args else None, (SSLError, URLLib3SSLError)):
                # SSL-related request error - retry
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"SSL-related request error for {symbol} latest trade (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
            # Other request errors - don't retry
            error_msg = f"Request error fetching latest trade for {symbol}: {e}"
            print(error_msg)
            raise Exception(error_msg)
        except Exception as e:
            # Check if it's an SSL-related error in the message
            if 'SSL' in str(e) or 'SSLError' in str(type(e)):
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"SSL error (unexpected type) for {symbol} latest trade (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
            # Unexpected errors - re-raise
            print(f"Unexpected error fetching latest trade for {symbol}: {e}")
            raise


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


# Function to get secrets from Google Secret Manager
def get_secret(secret_name):
    # We're on Google Cloud
    print(os.getenv("GOOGLE_CLOUD_PROJECT"))
    client = secretmanager.SecretManagerServiceClient()
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")


# Function to dynamically set environment (live or paper)
def set_alpaca_environment(env, use_secret_manager=True):
    if use_secret_manager and is_running_in_cloud():
        print("cloud")
        # On Google Cloud, use Secret Manager
        if env == "live":
            API_KEY = get_secret("ALPACA_API_KEY_LIVE")
            SECRET_KEY = get_secret("ALPACA_SECRET_KEY_LIVE")
            BASE_URL = "https://api.alpaca.markets"
        else:
            API_KEY = get_secret("ALPACA_API_KEY_PAPER")
            SECRET_KEY = get_secret("ALPACA_SECRET_KEY_PAPER")
            BASE_URL = "https://paper-api.alpaca.markets"
    else:
        # Running locally, use .env file (override=True ensures .env takes precedence)
        load_dotenv(override=True)
        if env == "live":
            API_KEY = os.getenv("ALPACA_API_KEY_LIVE")
            SECRET_KEY = os.getenv("ALPACA_SECRET_KEY_LIVE")
            BASE_URL = "https://api.alpaca.markets"
        else:
            API_KEY = os.getenv("ALPACA_API_KEY_PAPER")
            SECRET_KEY = os.getenv("ALPACA_SECRET_KEY_PAPER")
            BASE_URL = "https://paper-api.alpaca.markets"

    # Return credentials dictionary instead of Alpaca API object
    return {"API_KEY": API_KEY, "SECRET_KEY": SECRET_KEY, "BASE_URL": BASE_URL}


def get_telegram_secrets():
    if is_running_in_cloud():
        telegram_key = get_secret("TELEGRAM_KEY")
        chat_id = get_secret("TELEGRAM_CHAT_ID")
    else:
        load_dotenv(override=True)
        telegram_key = os.getenv("TELEGRAM_KEY")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")

    return telegram_key, chat_id


def get_fred_rate():
    """
    Fetch the current Federal Funds Target Rate (Upper Limit) from FRED API.
    
    Returns:
        float: Current FRED rate as a decimal (e.g., 0.0525 for 5.25%), or None on error
    """
    try:
        # Get FRED API key from Secret Manager or env
        if is_running_in_cloud():
            fred_key = get_secret("FREDKEY")
        else:
            load_dotenv(override=True)
            fred_key = os.getenv("FREDKEY")
        
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
    4. Split total by strategy percentages
    
    Note: All strategies now use actual positions (no virtual cash in Firestore),
    so we don't need to subtract reserved amounts. Each strategy's equity is tracked
    via actual Alpaca positions.
    
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
            "reserved_amounts": dict       # Always empty (no reserved cash anymore)
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
    
    # Step 4: Split by strategy percentages
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
        "reserved_amounts": {}  # No reserved amounts anymore
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
    """
    Check if SPY has dropped 30% from all-time high using Alpaca data.
    Uses 2-year period to capture recent all-time highs and crashes.
    """
    try:
        # Get API credentials
        api = set_alpaca_environment(env=alpaca_environment)
        
        # Fetch 2 years of SPY data from Alpaca
        from datetime import datetime, timedelta
        
        market_data_base_url = "https://data.alpaca.markets"
        end_date = datetime.now()
        start_date = end_date - timedelta(days=730)  # 2 years
        
        url = f"{market_data_base_url}/v2/stocks/SPY/bars"
        params = {
            "start": start_date.strftime("%Y-%m-%d"),
            "end": end_date.strftime("%Y-%m-%d"),
            "timeframe": "1Day",
            "limit": 10000,
            "adjustment": "split",
            "feed": "iex"
        }
        
        # Use retry session to handle SSL errors
        session = get_retry_session(max_retries=3, backoff_factor=2.0, timeout=30)
        response = session.get(
            url,
            headers=get_auth_headers(api),
            params=params,
            timeout=30
        )
        response.raise_for_status()
        
        data = response.json()
        bars = data.get("bars", [])
        
        if len(bars) < 10:  # Need sufficient data
            print(f"Insufficient SPY data for 30-down rule: {len(bars)} bars")
            return False
        
        # Get all-time high and current close from bars
        all_time_high = max(bar['h'] for bar in bars)
        current_close = bars[-1]['c']
        
        # Check if current is 30% below the all-time high
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


def get_nine_sig_positions(api):
    """
    Get current 9-Sig strategy positions from Alpaca account.
    
    Args:
        api: Alpaca API credentials dict
    
    Returns:
        dict: Dictionary with ticker -> shares held for 9-Sig symbols (TQQQ, AGG)
    """
    try:
        # Get all positions using the list_positions function
        positions = list_positions(api)
        
        # Filter for 9-Sig symbols only
        nine_sig_positions = {}
        nine_sig_symbols = ["TQQQ", "AGG"]
        
        # positions is a list of dicts from Alpaca API
        for position in positions:
            ticker = position.get("symbol")
            qty = float(position.get("qty", 0))
            if ticker in nine_sig_symbols and qty > 0:
                nine_sig_positions[ticker] = qty
        
        print(f"Current 9-Sig positions from Alpaca: {nine_sig_positions}")
        return nine_sig_positions
        
    except Exception as e:
        print(f"Error getting 9-Sig positions: {e}")
        return {}


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
    Now includes margin-aware logic with dynamic investment amounts and All-or-Nothing approach.
    
    Args:
        api: Alpaca API credentials
        force_execute: Bypass trading day check for testing
        investment_calc: Pre-calculated investment amounts (from orchestrator) - optional
        margin_result: Pre-calculated margin conditions (from orchestrator) - optional
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        print("Not first trading day of the month")
        return "Not first trading day of the month"
    
    if force_execute:
        print("9-Sig: Force execution enabled - bypassing trading day check")
        send_telegram_message("9-Sig: Force execution enabled for testing - bypassing trading day check")
    
    # If not provided by orchestrator, calculate independently
    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)
    
    investment_amount = investment_calc["strategy_amounts"]["nine_sig_allo"]
    
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    
    # Determine available buying power (already calculated in investment_calc)
    buying_power = investment_calc["total_available"] + investment_calc["margin_approved"]
    
    # Check if we should skip investment
    if target_margin == 0:
        # Cash-only mode triggered
        if leverage > 1.0:
            # Still leveraged - must skip to deleverage
            action_taken = f"Skipped - Deleveraging required (leverage: {leverage:.2f}x)"
            send_margin_summary_message(margin_result, "9-Sig", action_taken, investment_calc)
            print(action_taken)
            return action_taken
        # Equity-only but gates failed - skip without Firestore addition
        action_taken = f"Skipped - Margin gates failed (cash-only mode, buying power: ${buying_power:.2f})"
        send_margin_summary_message(margin_result, "9-Sig", action_taken, investment_calc)
        print(action_taken)
        return action_taken
    
    # Check if we have sufficient buying power for full investment (All-or-Nothing)
    if buying_power < investment_amount:
        action_taken = f"Skipped - Insufficient buying power (${buying_power:.2f} < ${investment_amount:.2f})"
        send_margin_summary_message(margin_result, "9-Sig", action_taken, investment_calc)
        print(action_taken)
        return action_taken
    
    # Check minimum investment amount (Alpaca requirement)
    if investment_amount < margin_control_config["min_investment"]:
        action_taken = f"Skipped - Investment amount ${investment_amount:.2f} below Alpaca minimum ($1.00)"
        send_margin_summary_message(margin_result, "9-Sig", action_taken, investment_calc)
        print(action_taken)
        return action_taken
    
    # Check projected leverage after investment to ensure we don't exceed 1.14x
    if target_margin > 0:  # Only check if margin is enabled
        portfolio_value = metrics.get("portfolio_value", 0)
        current_equity = metrics.get("equity", 0)
        
        if portfolio_value > 0 and current_equity > 0:
            projected_portfolio_value = portfolio_value + investment_amount
            projected_equity = current_equity
            
            if projected_equity > 0:
                projected_leverage = projected_portfolio_value / projected_equity
                
                if projected_leverage >= margin_control_config["max_leverage"]:
                    action_taken = f"Skipped - Projected leverage ({projected_leverage:.3f}x) would exceed limit ({margin_control_config['max_leverage']:.2f}x)"
                    send_margin_summary_message(margin_result, "9-Sig", action_taken, investment_calc)
                    print(f"Current leverage: {leverage:.3f}x, Projected leverage: {projected_leverage:.3f}x")
                    print(action_taken)
                    return action_taken
                else:
                    print(f"9-Sig: Leverage check - Current {leverage:.3f}x → Projected {projected_leverage:.3f}x (limit: {margin_control_config['max_leverage']:.2f}x)")
    
    # ALL monthly contributions go to AGG only (core 3Sig rule)
    # Load current strategy state from Firestore
    balances = load_balances(env)
    nine_sig_data = balances.get("nine_sig", {})
    total_invested = nine_sig_data.get("total_invested", 0)
    stored_agg_shares = nine_sig_data.get("current_agg_shares", 0)
    
    # Get actual positions from Alpaca to compare with stored positions
    actual_positions = get_nine_sig_positions(api)
    actual_agg_shares = actual_positions.get("AGG", 0)
    
    # Use actual positions from Alpaca as source of truth if available
    # This ensures we work with real data even if Firestore is out of sync
    if actual_agg_shares > 0:
        current_agg_shares = actual_agg_shares
        if abs(stored_agg_shares - actual_agg_shares) > 0.0001:  # Allow for small floating point differences
            print(f"Warning: Firestore AGG shares ({stored_agg_shares:.6f}) differ from Alpaca ({actual_agg_shares:.6f})")
            print(f"Using actual Alpaca positions as source of truth")
    else:
        current_agg_shares = stored_agg_shares
        if stored_agg_shares > 0:
            print(f"Warning: Could not get AGG position from Alpaca, using Firestore data ({stored_agg_shares:.6f})")
    
    print(f"9-Sig Strategy - Investment: ${investment_amount:.2f}")
    print(f"Current AGG shares (from Alpaca): {current_agg_shares:.6f}")
    print(f"Total invested: ${total_invested:.2f}")
    
    try:
        agg_price = float(get_latest_trade(api, "AGG"))
        agg_shares_to_buy = investment_amount / agg_price
        
        if agg_shares_to_buy > 0:
            order = submit_order(api, "AGG", agg_shares_to_buy, "buy")
            if not skip_order_wait:
                wait_for_order_fill(api, order["id"])
            
            # Calculate new total invested
            new_total_invested = total_invested + investment_amount
            
            # Wait a moment for orders to settle, then sync positions from Alpaca
            # This ensures we capture the actual positions after trades execute
            print("Waiting for orders to settle before syncing positions from Alpaca...")
            time.sleep(2)  # Give Alpaca a moment to process the orders
            
            # Get actual positions from Alpaca (source of truth)
            # This ensures Firestore matches reality even if trades were executed outside this function
            updated_positions = get_nine_sig_positions(api)
            actual_new_agg_shares = updated_positions.get("AGG", 0)
            
            # Use actual positions from Alpaca, falling back to manually calculated if unavailable
            if actual_new_agg_shares > 0:
                new_total_agg_shares = actual_new_agg_shares
                print(f"Synced AGG shares from Alpaca: {new_total_agg_shares:.6f}")
            else:
                # Fallback: manually calculate if we can't get from Alpaca
                print("Warning: Could not get AGG position from Alpaca, using manual calculation")
                new_total_agg_shares = current_agg_shares + agg_shares_to_buy
            
            print(f"9-Sig: Bought {agg_shares_to_buy:.6f} shares of AGG (monthly contribution)")
            
            # Enhanced Telegram message with detailed decision rationale
            telegram_msg = f"🎯 9-Sig Strategy Decision\n\n"
            telegram_msg += f"📊 Monthly Contribution Analysis:\n"
            telegram_msg += f"• Investment amount: ${investment_amount:.2f}\n"
            telegram_msg += f"• Target asset: AGG (Bonds)\n"
            telegram_msg += f"• AGG Price: ${agg_price:.2f}\n"
            telegram_msg += f"• Shares bought: {agg_shares_to_buy:.4f}\n\n"
            telegram_msg += f"🎯 Strategy Logic:\n"
            telegram_msg += f"• Monthly contributions go ONLY to AGG (bonds)\n"
            telegram_msg += f"• Following Jason Kelly's 3Sig methodology\n"
            telegram_msg += f"• Quarterly signals determine TQQQ/AGG allocation\n"
            telegram_msg += f"• Target allocation: 80% TQQQ, 20% AGG\n\n"
            telegram_msg += f"⚡ Trade Execution Summary:\n"
            telegram_msg += f"• Total AGG shares: {new_total_agg_shares:.6f}\n"
            telegram_msg += f"• Total invested: ${new_total_invested:.2f}\n"
            telegram_msg += f"• Monthly contribution tracked for quarterly signals"
            
            send_telegram_message(telegram_msg)
            
            # Track the actual contribution amount for quarterly signal calculation
            track_nine_sig_monthly_contribution(investment_amount, env=env)
            
            # Get TQQQ shares from Alpaca for complete position tracking
            actual_tqqq_shares = updated_positions.get("TQQQ", 0) if updated_positions else 0
            
            # Update Firestore with comprehensive tracking
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
            
            # Create action summary
            action_taken = f"Invested ${investment_amount:.2f} in AGG - {agg_shares_to_buy:.4f} shares"
            send_margin_summary_message(margin_result, "9-Sig", action_taken, investment_calc)
        
        return f"9-Sig monthly contribution: ${investment_amount:.2f} invested in AGG"
    
    except Exception as e:
        error_msg = f"9-Sig monthly contribution failed: {str(e)}"
        print(error_msg)
        send_telegram_message(error_msg)
        return error_msg


def make_monthly_buys_golden_hfea_lite(api, force_execute=False, investment_calc=None, margin_result=None, skip_order_wait=False, env="live"):
    """
    Make monthly Golden HFEA Lite purchases with margin-aware logic and dynamic investment amounts.
    Uses All-or-Nothing approach: invest full amount or skip entirely.
    
    Args:
        api: Alpaca API credentials
        force_execute: Bypass trading day check for testing
        investment_calc: Pre-calculated investment amounts (from orchestrator) - optional
        margin_result: Pre-calculated margin conditions (from orchestrator) - optional
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        print("Not first trading day of the month")
        return "Not first trading day of the month"
    
    if force_execute:
        print("Golden HFEA Lite: Force execution enabled - bypassing trading day check")
        send_telegram_message("Golden HFEA Lite: Force execution enabled for testing - bypassing trading day check")
    
    # If not provided by orchestrator, calculate independently
    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)
    
    investment_amount = investment_calc["strategy_amounts"]["golden_hfea_lite_allo"]
    
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    
    # Determine available buying power (already calculated in investment_calc)
    # buying_power = investment_calc["total_available"] + investment_calc["margin_approved"]
    
    # Check if we should skip investment
    if not target_margin and leverage > 1.0:
        print("Golden HFEA Lite: Skipping investment - margin disabled and still leveraged")
        send_telegram_message("Golden HFEA Lite: Skipping investment - margin disabled and still leveraged")
        return "Golden HFEA Lite: Skipping investment - margin disabled and still leveraged"
    
    if investment_amount < margin_control_config["min_investment"]:
        print(f"Golden HFEA Lite: Skipping investment - amount ${investment_amount:.2f} below minimum")
        send_telegram_message(f"Golden HFEA Lite: Skipping investment - amount ${investment_amount:.2f} below minimum")
        return "Golden HFEA Lite: Skipping investment - amount below minimum"
    
    # Check projected leverage after investment to ensure we don't exceed 1.14x
    if target_margin > 0:  # Only check if margin is enabled
        portfolio_value = metrics.get("portfolio_value", 0)
        current_equity = metrics.get("equity", 0)
        
        if portfolio_value > 0 and current_equity > 0:
            projected_portfolio_value = portfolio_value + investment_amount
            projected_equity = current_equity
            
            if projected_equity > 0:
                projected_leverage = projected_portfolio_value / projected_equity
                
                if projected_leverage >= margin_control_config["max_leverage"]:
                    action_taken = f"Skipped - Projected leverage ({projected_leverage:.3f}x) would exceed limit ({margin_control_config['max_leverage']:.2f}x)"
                    send_telegram_message(f"Golden HFEA Lite: {action_taken}")
                    print(f"Current leverage: {leverage:.3f}x, Projected leverage: {projected_leverage:.3f}x")
                    print(f"Golden HFEA Lite: {action_taken}")
                    return action_taken
                else:
                    print(f"Golden HFEA Lite: Leverage check - Current {leverage:.3f}x → Projected {projected_leverage:.3f}x (limit: {margin_control_config['max_leverage']:.2f}x)")
    
    # Get current Golden HFEA Lite allocations
    (
        sso_diff,
        zroz_diff,
        gld_diff,
        sso_value,
        zroz_value,
        gld_value,
        total_value,
        target_sso_value,
        target_zroz_value,
        target_gld_value,
        current_sso_percent,
        current_zroz_percent,
        current_gld_percent,
    ) = get_golden_hfea_lite_allocations(api)

    # Calculate underweight amounts
    sso_underweight = max(0, target_sso_value - sso_value)
    zroz_underweight = max(0, target_zroz_value - zroz_value)
    gld_underweight = max(0, target_gld_value - gld_value)
    total_underweight = sso_underweight + zroz_underweight + gld_underweight

    # If perfectly balanced, use standard split
    if total_underweight == 0:
        sso_amount = investment_amount * sso_allocation
        zroz_amount = investment_amount * zroz_allocation
        gld_amount = investment_amount * gld_allocation
    else:
        # Allocate proportionally based on underweight amounts
        sso_amount = (sso_underweight / total_underweight) * investment_amount
        zroz_amount = (zroz_underweight / total_underweight) * investment_amount
        gld_amount = (gld_underweight / total_underweight) * investment_amount

    # Get current prices for SSO, ZROZ, and GLD
    sso_price = float(get_latest_trade(api, "SSO"))
    zroz_price = float(get_latest_trade(api, "ZROZ"))
    gld_price = float(get_latest_trade(api, "GLD"))

    # Calculate number of shares to buy
    sso_shares_to_buy = sso_amount / sso_price
    zroz_shares_to_buy = zroz_amount / zroz_price
    gld_shares_to_buy = gld_amount / gld_price

    # Load current strategy state from Firestore
    balances = load_balances(env)
    golden_hfea_lite_data = balances.get("golden_hfea_lite", {})
    total_invested = golden_hfea_lite_data.get("total_invested", 0)
    current_positions = golden_hfea_lite_data.get("current_positions", {})
    
    print(f"Golden HFEA Lite Strategy - Investment: ${investment_amount:.2f}")
    print(f"Current positions: {current_positions}")
    print(f"Total invested: ${total_invested:.2f}")
    
    # Execute market orders with enhanced tracking
    shares_bought = []
    trades_executed = []
    
    for symbol, qty, amount in [("SSO", sso_shares_to_buy, sso_amount), ("ZROZ", zroz_shares_to_buy, zroz_amount), ("GLD", gld_shares_to_buy, gld_amount)]:
        if qty > 0:
            try:
                order = submit_order(api, symbol, qty, "buy")
                if not skip_order_wait:
                    wait_for_order_fill(api, order["id"])
                
                shares_bought.append(qty)
                trades_executed.append(f"Bought {qty:.6f} shares of {symbol} for ${amount:.2f}")
                print(f"Bought {qty:.6f} shares of {symbol} for ${amount:.2f}")
                send_telegram_message(f"Golden HFEA Lite: Bought {qty:.6f} shares of {symbol} for ${amount:.2f}")
                
            except Exception as e:
                error_msg = f"Golden HFEA Lite: Failed to buy {symbol}: {str(e)}"
                print(error_msg)
                send_telegram_message(error_msg)
                return error_msg
    
    if trades_executed:
        # Update Firestore with new positions
        total_invested += investment_amount
        current_positions.update({
            "SSO": current_positions.get("SSO", 0) + sso_shares_to_buy,
            "ZROZ": current_positions.get("ZROZ", 0) + zroz_shares_to_buy,
            "GLD": current_positions.get("GLD", 0) + gld_shares_to_buy
        })
        
        save_balance("golden_hfea_lite", {
            "total_invested": total_invested,
            "current_positions": current_positions,
            "last_updated": datetime.datetime.utcnow().isoformat()
        }, env)
        
        # Send summary message
        summary_msg = f"Golden HFEA Lite Monthly Investment Complete:\n"
        summary_msg += f"Total invested: ${total_invested:.2f}\n"
        summary_msg += f"Trades executed: {len(trades_executed)}\n"
        for trade in trades_executed:
            summary_msg += f"  {trade}\n"
        
        send_telegram_message(summary_msg)
    
    # Send margin summary
    action_taken = f"Invested ${investment_amount:.2f}" if trades_executed else "Skipped investment"
    send_margin_summary_message(margin_result, "Golden HFEA Lite", action_taken, investment_calc)
    
    return "Monthly investment executed."


def make_monthly_buys_rssb_wtip(api, force_execute=False, investment_calc=None, margin_result=None, skip_order_wait=False, env="live"):
    """
    Make monthly RSSB/WTIP purchases with margin-aware logic and dynamic investment amounts.
    Uses All-or-Nothing approach: invest full amount or skip entirely.
    
    Args:
        api: Alpaca API credentials
        force_execute: Bypass trading day check for testing
        investment_calc: Pre-calculated investment amounts (from orchestrator) - optional
        margin_result: Pre-calculated margin conditions (from orchestrator) - optional
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        print("Not first trading day of the month")
        return "Not first trading day of the month"
    
    if force_execute:
        print("RSSB/WTIP: Force execution enabled - bypassing trading day check")
        send_telegram_message("RSSB/WTIP: Force execution enabled for testing - bypassing trading day check")
    
    # If not provided by orchestrator, calculate independently
    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)
    
    investment_amount = investment_calc["strategy_amounts"]["rssb_wtip_allo"]
    
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    
    # Check if we should skip investment
    if not target_margin and leverage > 1.0:
        print("RSSB/WTIP: Skipping investment - margin disabled and still leveraged")
        send_telegram_message("RSSB/WTIP: Skipping investment - margin disabled and still leveraged")
        return "RSSB/WTIP: Skipping investment - margin disabled and still leveraged"
    
    if investment_amount < margin_control_config["min_investment"]:
        print(f"RSSB/WTIP: Skipping investment - amount ${investment_amount:.2f} below minimum")
        send_telegram_message(f"RSSB/WTIP: Skipping investment - amount ${investment_amount:.2f} below minimum")
        return "RSSB/WTIP: Skipping investment - amount below minimum"
    
    # Check projected leverage after investment to ensure we don't exceed 1.14x
    if target_margin > 0:  # Only check if margin is enabled
        portfolio_value = metrics.get("portfolio_value", 0)
        current_equity = metrics.get("equity", 0)
        
        if portfolio_value > 0 and current_equity > 0:
            projected_portfolio_value = portfolio_value + investment_amount
            projected_equity = current_equity
            
            if projected_equity > 0:
                projected_leverage = projected_portfolio_value / projected_equity
                
                if projected_leverage >= margin_control_config["max_leverage"]:
                    action_taken = f"Skipped - Projected leverage ({projected_leverage:.3f}x) would exceed limit ({margin_control_config['max_leverage']:.2f}x)"
                    send_telegram_message(f"RSSB/WTIP: {action_taken}")
                    print(f"Current leverage: {leverage:.3f}x, Projected leverage: {projected_leverage:.3f}x")
                    print(f"RSSB/WTIP: {action_taken}")
                    return action_taken
                else:
                    print(f"RSSB/WTIP: Leverage check - Current {leverage:.3f}x → Projected {projected_leverage:.3f}x (limit: {margin_control_config['max_leverage']:.2f}x)")
    
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
    
    # Calculate target allocations (80% RSSB, 20% WTIP)
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
                    send_telegram_message(error_msg)
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
                        error_msg = f"RSSB/WTIP: Failed to sell {symbol}: {str(e)}"
                        print(error_msg)
                        send_telegram_message(error_msg)
                        # Continue - sell failure shouldn't stop the strategy
            else:
                # WTIP: Round to whole shares, but only sell minimum needed
                shares_to_sell = round(excess_value / price)
                whole_shares_to_sell = int(shares_to_sell)  # Round down to be conservative
                if whole_shares_to_sell > 0:
                    try:
                        sell_order = submit_order(api, symbol, whole_shares_to_sell, "sell")
                        if not skip_order_wait:
                            wait_for_order_fill(api, sell_order["id"])
                        wtip_shares_to_sell = whole_shares_to_sell
                        actual_sold_value = whole_shares_to_sell * price
                        print(f"Sold {whole_shares_to_sell:.0f} shares of {symbol} (${actual_sold_value:.2f}) to reduce overweight from {overweight_pct:.1%} to within {sell_threshold_pct:.1%} threshold")
                    except Exception as e:
                        error_msg = f"RSSB/WTIP: Failed to sell {symbol}: {str(e)}"
                        print(error_msg)
                        send_telegram_message(error_msg)
                        # Continue - sell failure shouldn't stop the strategy
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
                        error_msg = f"RSSB/WTIP: Failed to buy {symbol} with sale proceeds: {str(e)}"
                        print(error_msg)
                        send_telegram_message(error_msg)
                        # Continue - buy failure shouldn't stop the strategy
        
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
                    error_msg = f"RSSB/WTIP: Failed to sell BIL: {str(e)}"
                    print(error_msg)
                    send_telegram_message(error_msg)
                    # Continue - BIL sell failure shouldn't stop the strategy
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
        
        # Send summary message
        summary_msg = f"RSSB/WTIP Monthly Investment Complete:\n"
        summary_msg += f"Total invested: ${total_invested:.2f}\n"
        summary_msg += f"Trades executed: {len(trades_executed)}\n"
        for trade in trades_executed:
            summary_msg += f"  {trade}\n"
        
        send_telegram_message(summary_msg)
    
    # Send margin summary
    action_taken = f"Invested ${investment_amount:.2f}" if trades_executed else "Skipped investment"
    send_margin_summary_message(margin_result, "RSSB/WTIP", action_taken, investment_calc)
    
    return "Monthly investment executed."


def make_monthly_buys(api, force_execute=False, investment_calc=None, margin_result=None, skip_order_wait=False, env="live"):
    """
    Make monthly HFEA purchases with margin-aware logic and dynamic investment amounts.
    Uses All-or-Nothing approach: invest full amount or skip entirely.
    
    Args:
        api: Alpaca API credentials
        force_execute: Bypass trading day check for testing
        investment_calc: Pre-calculated investment amounts (from orchestrator) - optional
        margin_result: Pre-calculated margin conditions (from orchestrator) - optional
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        print("Not first trading day of the month")
        return "Not first trading day of the month"
    
    if force_execute:
        print("HFEA: Force execution enabled - bypassing trading day check")
        send_telegram_message("HFEA: Force execution enabled for testing - bypassing trading day check")
    
    # If not provided by orchestrator, calculate independently
    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)
    
    investment_amount = investment_calc["strategy_amounts"]["hfea_allo"]
    
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    
    # Determine available buying power (already calculated in investment_calc)
    buying_power = investment_calc["total_available"] + investment_calc["margin_approved"]
    
    # Check if we should skip investment
    if target_margin == 0:
        # Cash-only mode triggered
        if leverage > 1.0:
            # Still leveraged - must skip to deleverage
            action_taken = f"Skipped - Deleveraging required (leverage: {leverage:.2f}x)"
            send_margin_summary_message(margin_result, "HFEA", action_taken, investment_calc)
            print(action_taken)
            return action_taken
        # Equity-only but gates failed - skip without Firestore addition
        action_taken = f"Skipped - Margin gates failed (cash-only mode, buying power: ${buying_power:.2f})"
        send_margin_summary_message(margin_result, "HFEA", action_taken, investment_calc)
        print(action_taken)
        return action_taken
    
    # Check if we have sufficient buying power for full investment (All-or-Nothing)
    if buying_power < investment_amount:
        action_taken = f"Skipped - Insufficient buying power (${buying_power:.2f} < ${investment_amount:.2f})"
        send_margin_summary_message(margin_result, "HFEA", action_taken, investment_calc)
        print(action_taken)
        return action_taken
    
    # Check minimum investment amount (Alpaca requirement)
    if investment_amount < margin_control_config["min_investment"]:
        action_taken = f"Skipped - Investment amount ${investment_amount:.2f} below Alpaca minimum ($1.00)"
        send_margin_summary_message(margin_result, "HFEA", action_taken, investment_calc)
        print(action_taken)
        return action_taken
    
    # Check projected leverage after investment to ensure we don't exceed 1.14x
    if target_margin > 0:  # Only check if margin is enabled
        portfolio_value = metrics.get("portfolio_value", 0)
        current_cash = metrics.get("cash", 0)
        # Calculate actual equity: Equity = Portfolio Value + Cash (cash can be negative when using margin)
        # This is more accurate than using Alpaca's equity field directly when margin is involved
        current_equity = portfolio_value + current_cash
        
        if portfolio_value > 0 and current_equity > 0:
            # Calculate projected values after investment
            # When investing using margin:
            # - Portfolio value increases by investment amount (new positions purchased)
            # - Cash decreases by investment amount (becomes more negative)
            # - Equity = Portfolio Value + Cash remains unchanged immediately after purchase
            #   (Both portfolio_value and cash change by same amount: +investment -investment = 0)
            
            # IMPORTANT: Reserved cash (from bearish strategies) is still physically in Alpaca
            # - Alpaca's portfolio_value and equity include ALL cash (reserved + available)
            # - Reserved cash reduces available_cash for investment calculation, but is still part of account
            # - This leverage calculation correctly uses actual portfolio_value from Alpaca
            # - The investment_amount already accounts for reserved cash (via available_cash)
            
            projected_portfolio_value = portfolio_value + investment_amount
            projected_cash = current_cash - investment_amount
            projected_equity = projected_portfolio_value + projected_cash  # Should equal current_equity
            
            # Calculate projected leverage: Portfolio Value / Equity
            if projected_equity > 0:
                projected_leverage = projected_portfolio_value / projected_equity
                
                # Get reserved cash info for debug output
                total_reserved = investment_calc.get("total_reserved", 0)
                
                # Debug output showing actual values used
                print(f"Leverage projection details:")
                print(f"  Portfolio Value: ${portfolio_value:.2f}, Cash: ${current_cash:.2f}")
                print(f"  Calculated Equity (Portfolio Value + Cash): ${current_equity:.2f}")
                if total_reserved > 0:
                    print(f"  Reserved Cash (Firestore): ${total_reserved:.2f} (still in Alpaca account)")
                print(f"  Investment Amount: ${investment_amount:.2f} (from available cash + margin)")
                print(f"  Projected Portfolio Value: ${projected_portfolio_value:.2f}")
                print(f"  Projected Cash: ${projected_cash:.2f}")
                print(f"  Projected Equity: ${projected_equity:.2f}")
                print(f"  Projected Leverage: {projected_leverage:.3f}x")
                
                if projected_leverage >= margin_control_config["max_leverage"]:
                    action_taken = f"Skipped - Projected leverage ({projected_leverage:.3f}x) would exceed limit ({margin_control_config['max_leverage']:.2f}x)"
                    send_margin_summary_message(margin_result, "HFEA", action_taken, investment_calc)
                    print(f"Current leverage: {leverage:.3f}x, Projected leverage: {projected_leverage:.3f}x")
                    print(action_taken)
                    return action_taken
                else:
                    print(f"Leverage check: Current {leverage:.3f}x → Projected {projected_leverage:.3f}x (limit: {margin_control_config['max_leverage']:.2f}x)")
    
    # Proceed with investment - we have sufficient funds
    # Get current portfolio allocations and values from get_hfea_allocations
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

    # Calculate underweight amounts
    upro_underweight = max(0, target_upro_value - upro_value)
    tmf_underweight = max(0, target_tmf_value - tmf_value)
    kmlm_underweight = max(0, target_kmlm_value - kmlm_value)
    total_underweight = upro_underweight + tmf_underweight + kmlm_underweight

    # If perfectly balanced, use standard split
    if total_underweight == 0:
        upro_amount = investment_amount * upro_allocation
        tmf_amount = investment_amount * tmf_allocation
        kmlm_amount = investment_amount * kmlm_allocation
    else:
        # Allocate proportionally based on underweight amounts
        upro_amount = (upro_underweight / total_underweight) * investment_amount
        tmf_amount = (tmf_underweight / total_underweight) * investment_amount
        kmlm_amount = (kmlm_underweight / total_underweight) * investment_amount

    # Get current prices for UPRO, TMF, and KMLM
    upro_price = float(get_latest_trade(api, "UPRO"))
    tmf_price = float(get_latest_trade(api, "TMF"))
    kmlm_price = float(get_latest_trade(api, "KMLM"))

    # Calculate number of shares to buy
    upro_shares_to_buy = upro_amount / upro_price
    tmf_shares_to_buy = tmf_amount / tmf_price
    kmlm_shares_to_buy = kmlm_amount / kmlm_price

    # Load current strategy state from Firestore
    balances = load_balances(env)
    hfea_data = balances.get("hfea", {})
    total_invested = hfea_data.get("total_invested", 0)
    stored_positions = hfea_data.get("current_positions", {})
    
    # Get actual positions from Alpaca to compare with stored positions
    actual_hfea_positions = get_hfea_positions(api)
    
    # Use actual positions from Alpaca as source of truth if available
    # This ensures we work with real data even if Firestore is out of sync
    if actual_hfea_positions:
        current_positions = actual_hfea_positions
        if stored_positions != actual_hfea_positions:
            print(f"Warning: Firestore positions ({stored_positions}) differ from Alpaca ({actual_hfea_positions})")
            print(f"Using actual Alpaca positions as source of truth")
    else:
        current_positions = stored_positions
        print(f"Warning: Could not get positions from Alpaca, using Firestore data")
    
    print(f"HFEA Strategy - Investment: ${investment_amount:.2f}")
    print(f"Current positions (from Alpaca): {current_positions}")
    print(f"Total invested: ${total_invested:.2f}")
    
    # Execute market orders with enhanced tracking
    shares_bought = []
    trades_executed = []
    
    for symbol, qty, amount in [
        ("UPRO", upro_shares_to_buy, upro_amount),
        ("TMF", tmf_shares_to_buy, tmf_amount),
        ("KMLM", kmlm_shares_to_buy, kmlm_amount),
    ]:
        if qty > 0:
            submit_order(api, symbol, qty, "buy")
            if not skip_order_wait:
                # Note: HFEA doesn't have individual order IDs, so we can't wait for specific fills
                pass
            print(f"Bought {qty:.6f} shares of {symbol}.")
            shares_bought.append(f"{symbol}: {qty:.4f} shares")
            trades_executed.append(f"Bought {qty:.4f} shares of {symbol} (${amount:.2f})")
        else:
            print(f"No shares of {symbol} bought due to small amount.")
    
    # Calculate new total invested
    new_total_invested = total_invested + investment_amount
    
    # Wait a moment for orders to settle, then sync positions from Alpaca
    # This ensures we capture the actual positions after trades execute
    if len(trades_executed) > 0:
        print("Waiting for orders to settle before syncing positions from Alpaca...")
        time.sleep(2)  # Give Alpaca a moment to process the orders
    
    # Get actual positions from Alpaca (source of truth)
    # This ensures Firestore matches reality even if trades were executed outside this function
    actual_positions = get_hfea_positions(api)
    
    # Use actual positions from Alpaca, falling back to manually calculated if Alpaca data unavailable
    if actual_positions:
        new_positions = actual_positions
        print(f"Synced positions from Alpaca: {new_positions}")
    else:
        # Fallback: manually update positions if we can't get from Alpaca
        print("Warning: Could not get positions from Alpaca, using manual calculation")
        new_positions = current_positions.copy()
        for symbol, qty in [("UPRO", upro_shares_to_buy), ("TMF", tmf_shares_to_buy), ("KMLM", kmlm_shares_to_buy)]:
            if qty > 0:
                new_positions[symbol] = new_positions.get(symbol, 0) + qty
    
    # Calculate current portfolio value from actual positions
    portfolio_value = upro_value + tmf_value + kmlm_value
    
    # Enhanced Telegram message with detailed decision rationale
    telegram_msg = f"🎯 HFEA Strategy Decision\n\n"
    telegram_msg += f"📊 Allocation Analysis:\n"
    telegram_msg += f"• UPRO (45%): ${upro_amount:.2f} → {upro_shares_to_buy:.4f} shares @ ${upro_price:.2f}\n"
    telegram_msg += f"• TMF (25%): ${tmf_amount:.2f} → {tmf_shares_to_buy:.4f} shares @ ${tmf_price:.2f}\n"
    telegram_msg += f"• KMLM (30%): ${kmlm_amount:.2f} → {kmlm_shares_to_buy:.4f} shares @ ${kmlm_price:.2f}\n\n"
    telegram_msg += f"🎯 Strategy Logic:\n"
    telegram_msg += f"• Three-asset leveraged portfolio (UPRO/TMF/KMLM)\n"
    telegram_msg += f"• Enhanced diversification through managed futures (KMLM)\n"
    telegram_msg += f"• Underweight-based allocation system\n\n"
    telegram_msg += f"⚡ Trade Execution Summary:\n"
    telegram_msg += f"• Total trades executed: {len(trades_executed)}\n"
    for trade in trades_executed:
        telegram_msg += f"  • {trade}\n"
    telegram_msg += f"\n💰 Portfolio Summary:\n"
    telegram_msg += f"• New investment: ${investment_amount:.2f}\n"
    telegram_msg += f"• Portfolio value: ${portfolio_value:.2f}\n"
    telegram_msg += f"• Cumulative contributions: ${new_total_invested:.2f}\n"
    telegram_msg += f"• Current positions: {len([k for k, v in new_positions.items() if v > 0])} assets"
    
    send_telegram_message(telegram_msg)
    
    # Update Firestore with comprehensive tracking
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
        "trades_executed": trades_executed
    }, env)
    
    # Create action summary for margin message
    action_taken = f"Invested ${investment_amount:.2f} - " + ", ".join(shares_bought)
    send_margin_summary_message(margin_result, "HFEA", action_taken, investment_calc)
    
    return "Monthly investment executed."


def get_hfea_positions(api):
    """
    Get current HFEA positions from Alpaca account.
    
    Args:
        api: Alpaca API credentials dict
    
    Returns:
        dict: Dictionary with ticker -> shares held for HFEA symbols (UPRO, TMF, KMLM)
    """
    try:
        # Get all positions using the list_positions function
        positions = list_positions(api)
        
        # Filter for HFEA symbols only
        hfea_positions = {}
        hfea_symbols = ["UPRO", "TMF", "KMLM"]
        
        # positions is a list of dicts from Alpaca API
        for position in positions:
            ticker = position.get("symbol")
            qty = float(position.get("qty", 0))
            if ticker in hfea_symbols and qty > 0:
                hfea_positions[ticker] = qty
        
        print(f"Current HFEA positions from Alpaca: {hfea_positions}")
        return hfea_positions
        
    except Exception as e:
        print(f"Error getting HFEA positions: {e}")
        return {}


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


def get_golden_hfea_lite_allocations(api):
    """
    Get Golden HFEA Lite allocations (SSO/ZROZ/GLD at 50/25/25).
    Returns current values, percentages, target values, and deviations.
    """
    positions = {p["symbol"]: float(p["market_value"]) for p in list_positions(api)}
    sso_value = positions.get("SSO", 0)
    zroz_value = positions.get("ZROZ", 0)
    gld_value = positions.get("GLD", 0)
    total_value = sso_value + zroz_value + gld_value
    
    # Calculate current and target allocations
    current_sso_percent = sso_value / total_value if total_value else 0
    current_zroz_percent = zroz_value / total_value if total_value else 0
    current_gld_percent = gld_value / total_value if total_value else 0
    target_sso_value = total_value * sso_allocation
    target_zroz_value = total_value * zroz_allocation
    target_gld_value = total_value * gld_allocation
    
    # Calculate deviations
    sso_diff = sso_value - target_sso_value
    zroz_diff = zroz_value - target_zroz_value
    gld_diff = gld_value - target_gld_value
    
    return (
        sso_diff,
        zroz_diff,
        gld_diff,
        sso_value,
        zroz_value,
        gld_value,
        total_value,
        target_sso_value,
        target_zroz_value,
        target_gld_value,
        current_sso_percent,
        current_zroz_percent,
        current_gld_percent,
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
    """
    Get current SPXL SMA strategy positions from Alpaca account.
    
    Args:
        api: Alpaca API credentials dict
    
    Returns:
        dict: Dictionary with ticker -> shares held for SPXL SMA symbols (SPXL, SGOV)
    """
    try:
        # Get all positions using the list_positions function
        positions = list_positions(api)
        
        # Filter for SPXL SMA symbols only
        spxl_sma_positions = {}
        spxl_sma_symbols = ["SPXL", spxl_sma_holding_fund]
        
        # positions is a list of dicts from Alpaca API
        for position in positions:
            ticker = position.get("symbol")
            qty = float(position.get("qty", 0))
            if ticker in spxl_sma_symbols and qty > 0:
                spxl_sma_positions[ticker] = qty
        
        print(f"Current SPXL SMA positions from Alpaca: {spxl_sma_positions}")
        return spxl_sma_positions
        
    except Exception as e:
        print(f"Error getting SPXL SMA positions: {e}")
        return {}


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
        # Use lowercase to match other strategies (nine_sig, sector_momentum, etc.)
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
    Get RSSB/WTIP allocations (80/20).
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


def rebalance_golden_hfea_lite_portfolio(api):
    """
    Rebalance Golden HFEA Lite portfolio (SSO/ZROZ/GLD at 50/25/25) quarterly.
    Executes on first trading day of each quarter.
    """
    if not check_trading_day(mode="quarterly"):
        print("Not first trading day of the month in this Quarter")
        return "Not first trading day of the month in this Quarter"
    
    # Get SSO, ZROZ, and GLD values and deviations from target allocation
    (
        sso_diff,
        zroz_diff,
        gld_diff,
        sso_value,
        zroz_value,
        gld_value,
        total_value,
        target_sso_value,
        target_zroz_value,
        target_gld_value,
        current_sso_percent,
        current_zroz_percent,
        current_gld_percent,
    ) = get_golden_hfea_lite_allocations(api)

    # Apply a margin for fees (e.g., 0.5%)
    fee_margin = 0.995

    # If the total value is 0, nothing to rebalance
    if total_value == 0:
        print("No holdings to rebalance for Golden HFEA Lite.")
        send_telegram_message("No holdings to rebalance for Golden HFEA Lite Strategy.")
        return "No holdings to rebalance for Golden HFEA Lite Strategy."

    # Define trade parameters for each ETF
    rebalance_actions = []

    # If SSO is over-allocated, adjust ZROZ or GLD if under-allocated
    if sso_diff > 0:
        if zroz_diff < 0:
            sso_shares_to_sell = min(sso_diff, abs(zroz_diff)) / float(get_latest_trade(api, "SSO"))
            zroz_shares_to_buy = (
                sso_shares_to_sell
                * float(get_latest_trade(api, "SSO"))
                / float(get_latest_trade(api, "ZROZ"))
            ) * fee_margin
            rebalance_actions.append(("SSO", sso_shares_to_sell, "sell"))
            rebalance_actions.append(("ZROZ", zroz_shares_to_buy, "buy"))

        if gld_diff < 0:
            sso_shares_to_sell = min(sso_diff, abs(gld_diff)) / float(get_latest_trade(api, "SSO"))
            gld_shares_to_buy = (
                sso_shares_to_sell
                * float(get_latest_trade(api, "SSO"))
                / float(get_latest_trade(api, "GLD"))
            ) * fee_margin
            rebalance_actions.append(("SSO", sso_shares_to_sell, "sell"))
            rebalance_actions.append(("GLD", gld_shares_to_buy, "buy"))

    # If ZROZ is over-allocated, adjust SSO or GLD if under-allocated
    if zroz_diff > 0:
        if sso_diff < 0:
            zroz_shares_to_sell = min(zroz_diff, abs(sso_diff)) / float(get_latest_trade(api, "ZROZ"))
            sso_shares_to_buy = (
                zroz_shares_to_sell
                * float(get_latest_trade(api, "ZROZ"))
                / float(get_latest_trade(api, "SSO"))
            ) * fee_margin
            rebalance_actions.append(("ZROZ", zroz_shares_to_sell, "sell"))
            rebalance_actions.append(("SSO", sso_shares_to_buy, "buy"))

        if gld_diff < 0:
            zroz_shares_to_sell = min(zroz_diff, abs(gld_diff)) / float(get_latest_trade(api, "ZROZ"))
            gld_shares_to_buy = (
                zroz_shares_to_sell
                * float(get_latest_trade(api, "ZROZ"))
                / float(get_latest_trade(api, "GLD"))
            ) * fee_margin
            rebalance_actions.append(("ZROZ", zroz_shares_to_sell, "sell"))
            rebalance_actions.append(("GLD", gld_shares_to_buy, "buy"))

    # If GLD is over-allocated, adjust SSO or ZROZ if under-allocated
    if gld_diff > 0:
        if sso_diff < 0:
            gld_shares_to_sell = min(gld_diff, abs(sso_diff)) / float(get_latest_trade(api, "GLD"))
            sso_shares_to_buy = (
                gld_shares_to_sell
                * float(get_latest_trade(api, "GLD"))
                / float(get_latest_trade(api, "SSO"))
            ) * fee_margin
            rebalance_actions.append(("GLD", gld_shares_to_sell, "sell"))
            rebalance_actions.append(("SSO", sso_shares_to_buy, "buy"))

        if zroz_diff < 0:
            gld_shares_to_sell = min(gld_diff, abs(zroz_diff)) / float(get_latest_trade(api, "GLD"))
            zroz_shares_to_buy = (
                gld_shares_to_sell
                * float(get_latest_trade(api, "GLD"))
                / float(get_latest_trade(api, "ZROZ"))
            ) * fee_margin
            rebalance_actions.append(("GLD", gld_shares_to_sell, "sell"))
            rebalance_actions.append(("ZROZ", zroz_shares_to_buy, "buy"))

    # Execute rebalancing actions
    for symbol, qty, action in rebalance_actions:
        if qty > 0:
            order = submit_order(api, symbol, qty, action)
            action_verb = "Bought" if action == "buy" else "Sold"
            wait_for_order_fill(api, order["id"])
            print(f"Golden HFEA Lite: {action_verb} {qty:.6f} shares of {symbol} to rebalance.")
            send_telegram_message(
                f"Golden HFEA Lite: {action_verb} {qty:.6f} shares of {symbol} to rebalance."
            )

    # Report completion of rebalancing check
    print("Golden HFEA Lite rebalance check completed.")
    return "Golden HFEA Lite rebalance executed."


def rebalance_rssb_wtip_portfolio(api):
    """
    Rebalance RSSB/WTIP portfolio (80/20) quarterly.
    Executes on first trading day of each quarter.
    Handles non-fractionable shares for WTIP and pending investments.
    """
    if not check_trading_day(mode="quarterly"):
        print("Not first trading day of the month in this Quarter")
        return "Not first trading day of the month in this Quarter"
    
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
    return "RSSB/WTIP rebalance executed."


def rebalance_portfolio(api):
    if not check_trading_day(mode="quarterly"):
        print("Not first trading day of the month in this Quarter")
        return "Not first trading day of the month in this Quarter"
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
    return "Rebalance executed."


def execute_quarterly_nine_sig_signal(api, force_execute=False, env="live"):
    """Execute quarterly 9-sig signal following Jason Kelly's exact 5-step process"""
    if not force_execute and not check_trading_day(mode="quarterly"):
        print("Not first trading day of the quarter")
        return "Not first trading day of the quarter"
    
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
    doc_id = symbol.replace("^", "").replace(".", "_")
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
    Monthly SMA-based investment with margin-aware logic and dynamic investment amounts.
    Uses All-or-Nothing approach: invest full amount or skip entirely.
    Only adds to Firestore when SMA trend is bearish AND account is equity-only.
    
    Args:
        api: Alpaca API credentials
        symbol: Symbol to trade (e.g., "SPXL")
        force_execute: Bypass trading day check for testing
        investment_calc: Pre-calculated investment amounts (from orchestrator) - optional
        margin_result: Pre-calculated margin conditions (from orchestrator) - optional
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        return "Not first trading day of the month"
    
    if force_execute:
        print(f"{symbol} SMA: Force execution enabled - bypassing trading day check")
        send_telegram_message(f"{symbol} SMA: Force execution enabled for testing - bypassing trading day check")

        # Get symbol-specific parameters (use SPY as S&P 500 proxy for SPXL decisions)
        if symbol == "SPXL":
            # Get all SPY market data at once (efficient single fetch/read)
            spy_data = get_all_market_data("SPY", env=env)
            if spy_data is None:
                spy_data = update_market_data("SPY", env=env)
        
        sma_200 = spy_data["sma200"]
        latest_price = spy_data["price"]
    else:
        return f"Unknown symbol: {symbol}"

    # If not provided by orchestrator, calculate independently
    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)
    
    investment_amount = investment_calc["strategy_amounts"]["spxl_allo"]
    
    target_margin = margin_result["target_margin"]
    metrics = margin_result["metrics"]
    leverage = metrics.get("leverage", 1.0)
    
    # Determine available buying power (already calculated in investment_calc)
    buying_power = investment_calc["total_available"] + investment_calc["margin_approved"]

    # Load current strategy state from Firestore
    balances = load_balances(env)
    # Use lowercase to match other strategies
    spxl_data = balances.get("spxl_sma", {}) or balances.get(f"{symbol}_SMA", {})
    total_invested = spxl_data.get("total_invested", 0)
    current_shares = spxl_data.get("current_shares", 0)
    holding_fund_position = spxl_data.get("holding_fund_position", {})
    
    # Get SGOV holding fund current value and shares from Alpaca
    sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
    sgov_value = get_holding_fund_value(api, spxl_sma_holding_fund)
    sgov_price = float(get_latest_trade(api, spxl_sma_holding_fund)) if sgov_value > 0 or investment_amount > 0 else 0
    
    print(f"{symbol}: Investment=${investment_amount:.2f}, Price={latest_price:.2f}, SMA={sma_200:.2f}, Leverage={leverage:.2f}x")
    print(f"Current SPXL shares: {current_shares:.4f}, Total invested: ${total_invested:.2f}")
    print(f"{spxl_sma_holding_fund} holding fund: {sgov_shares:.6f} shares (${sgov_value:.2f})")
    
    # Check SMA trend
    if latest_price > sma_200 * (1 + margin):
        # Bullish trend - attempt to buy
        
        # Check if we should skip investment
        if target_margin == 0:
            # Cash-only mode triggered
            if leverage > 1.0:
                # Still leveraged - must skip to deleverage
                action_taken = f"Skipped - Deleveraging required (leverage: {leverage:.2f}x)"
                send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
                print(action_taken)
                return action_taken
            # Equity-only but gates failed - skip without Firestore addition
            action_taken = f"Skipped - Margin gates failed (cash-only mode, buying power: ${buying_power:.2f})"
            send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
            print(action_taken)
            return action_taken
        
        # Check if we have sufficient buying power for full investment (All-or-Nothing)
        if buying_power < investment_amount:
            action_taken = f"Skipped - Insufficient buying power (${buying_power:.2f} < ${investment_amount:.2f})"
            send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
            print(action_taken)
            return action_taken
        
        # Check minimum investment amount (Alpaca requirement)
        if investment_amount < margin_control_config["min_investment"]:
            action_taken = f"Skipped - Investment amount ${investment_amount:.2f} below Alpaca minimum ($1.00)"
            send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
            print(action_taken)
            return action_taken
        
        # Check projected leverage after investment to ensure we don't exceed 1.14x
        if target_margin > 0:  # Only check if margin is enabled
            portfolio_value = metrics.get("portfolio_value", 0)
            current_equity = metrics.get("equity", 0)
            
            if portfolio_value > 0 and current_equity > 0:
                projected_portfolio_value = portfolio_value + investment_amount
                projected_equity = current_equity
                
                if projected_equity > 0:
                    projected_leverage = projected_portfolio_value / projected_equity
                    
                    if projected_leverage >= margin_control_config["max_leverage"]:
                        action_taken = f"Skipped - Projected leverage ({projected_leverage:.3f}x) would exceed limit ({margin_control_config['max_leverage']:.2f}x)"
                        send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
                        print(f"Current leverage: {leverage:.3f}x, Projected leverage: {projected_leverage:.3f}x")
                        print(action_taken)
                        return action_taken
                    else:
                        print(f"Leverage check: Current {leverage:.3f}x → Projected {projected_leverage:.3f}x (limit: {margin_control_config['max_leverage']:.2f}x)")
        
        # If we have SGOV, sell it first to buy SPXL
        trades_executed = []
        if sgov_shares > 0:
            try:
                sell_order = submit_order(api, spxl_sma_holding_fund, sgov_shares, "sell")
                if not skip_order_wait:
                    wait_for_order_fill(api, sell_order["id"])
                trades_executed.append(f"Sold {sgov_shares:.6f} shares of {spxl_sma_holding_fund} (${sgov_value:.2f}) to buy {symbol}")
                print(f"Sold {sgov_shares:.6f} shares of {spxl_sma_holding_fund} (${sgov_value:.2f})")
                send_telegram_message(f"{symbol} SMA: Sold {sgov_shares:.6f} shares of {spxl_sma_holding_fund} to switch to {symbol}")
            except Exception as e:
                error_msg = f"Failed to sell {spxl_sma_holding_fund}: {str(e)}"
                print(error_msg)
                send_telegram_message(f"{symbol} SMA Error: {error_msg}")
                return error_msg
        
        # Execute purchase
        price = get_latest_trade(api, symbol)
        print(f"Executing buy: price={price}")
        shares_to_buy = investment_amount / price

        if shares_to_buy > 0:
            order = submit_order(api, symbol, shares_to_buy, "buy")
            if not skip_order_wait:
                wait_for_order_fill(api, order["id"])
            
            # Calculate new totals
            new_total_shares = current_shares + shares_to_buy
            new_total_invested = total_invested + investment_amount
            
            # Enhanced Telegram message with detailed decision rationale
            telegram_msg = f"🎯 {symbol} SMA Strategy Decision\n\n"
            telegram_msg += f"📊 Trend Analysis:\n"
            telegram_msg += f"• SPY Price: ${latest_price:.2f}\n"
            telegram_msg += f"• SPY 200-SMA: ${sma_200:.2f}\n"
            telegram_msg += f"• Trend Status: 🟢 BULLISH (Price > SMA + {margin:.1%})\n"
            telegram_msg += f"• Margin: {margin:.1%} band around SMA\n\n"
            telegram_msg += f"🎯 Strategy Logic:\n"
            telegram_msg += f"• Trend-following with market timing\n"
            telegram_msg += f"• Uses SPY as S&P 500 proxy for {symbol} decisions\n"
            telegram_msg += f"• Exits during downtrends to avoid drawdowns\n\n"
            telegram_msg += f"⚡ Trade Execution Summary:\n"
            telegram_msg += f"• Investment amount: ${investment_amount:.2f}\n"
            telegram_msg += f"• Target asset: {symbol}\n"
            telegram_msg += f"• Shares bought: {shares_to_buy:.4f}\n"
            telegram_msg += f"• Price per share: ${price:.2f}\n"
            telegram_msg += f"• Total shares: {new_total_shares:.4f}\n"
            telegram_msg += f"• Total invested: ${new_total_invested:.2f}"
            
            send_telegram_message(telegram_msg)
            
            # Update Firestore with comprehensive tracking
            # Clear holding fund position since we sold SGOV
            updated_sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
            holding_fund_position[spxl_sma_holding_fund] = updated_sgov_shares
            
            save_balance("spxl_sma", {
                "total_invested": new_total_invested,
                "current_shares": new_total_shares,
                "holding_fund_position": holding_fund_position,
                "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
                "last_trade": {
                    "action": "buy",
                    "shares": shares_to_buy,
                    "price": price,
                    "amount": investment_amount
                },
                "trend_analysis": {
                    "spy_price": latest_price,
                    "spy_sma_200": sma_200,
                    "trend_status": "bullish",
                    "margin_band": margin
                }
            }, env)
            
            action_taken = f"Bought {shares_to_buy:.4f} shares of {symbol} (${investment_amount:.2f})"
            if trades_executed:
                action_taken += f" - {', '.join(trades_executed)}"
            send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
            return f"Bought {shares_to_buy:.6f} shares of {symbol}."
        else:
            action_taken = f"Amount too small to buy {symbol} shares"
            send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
            return f"Amount too small to buy {symbol} shares."
    else:
        # Bearish trend (below SMA) - buy SGOV T-bills instead of SPXL
        trades_executed = []
        
        # Check if we should skip investment
        if target_margin == 0:
            # Cash-only mode triggered
            if leverage > 1.0:
                # Still leveraged - must skip to deleverage
                action_taken = f"Skipped - Deleveraging required (leverage: {leverage:.2f}x)"
                send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
                print(action_taken)
                return action_taken
            # Equity-only but gates failed - skip
            action_taken = f"Skipped - Margin gates failed (cash-only mode, buying power: ${buying_power:.2f})"
            send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
            print(action_taken)
            return action_taken
        
        # Check if we have sufficient buying power for full investment (All-or-Nothing)
        if buying_power < investment_amount:
            action_taken = f"Skipped - Insufficient buying power (${buying_power:.2f} < ${investment_amount:.2f})"
            send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
            print(action_taken)
            return action_taken
        
        # Check minimum investment amount (Alpaca requirement)
        if investment_amount < margin_control_config["min_investment"]:
            action_taken = f"Skipped - Investment amount ${investment_amount:.2f} below Alpaca minimum ($1.00)"
            send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
            print(action_taken)
            return action_taken
        
        # Buy SGOV T-bills when bearish
        if sgov_price > 0:
            sgov_shares_to_buy = investment_amount / sgov_price
            
            if sgov_shares_to_buy > 0:
                try:
                    sgov_order = submit_order(api, spxl_sma_holding_fund, sgov_shares_to_buy, "buy")
                    if not skip_order_wait:
                        wait_for_order_fill(api, sgov_order["id"])
                    
                    new_total_invested = total_invested + investment_amount
                    updated_sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
                    holding_fund_position[spxl_sma_holding_fund] = updated_sgov_shares
                    
                    trades_executed.append(f"Bought {sgov_shares_to_buy:.6f} shares of {spxl_sma_holding_fund} (${investment_amount:.2f})")
                    print(f"Bought {sgov_shares_to_buy:.6f} shares of {spxl_sma_holding_fund} for ${investment_amount:.2f}")
                    
                    # Enhanced Telegram message
                    telegram_msg = f"🎯 {symbol} SMA Strategy Decision\n\n"
                    telegram_msg += f"📊 Trend Analysis:\n"
                    telegram_msg += f"• SPY Price: ${latest_price:.2f}\n"
                    telegram_msg += f"• SPY 200-SMA: ${sma_200:.2f}\n"
                    telegram_msg += f"• Trend Status: 🔴 BEARISH (Price < SMA - {margin:.1%})\n"
                    telegram_msg += f"• Margin: {margin:.1%} band around SMA\n\n"
                    telegram_msg += f"🎯 Strategy Logic:\n"
                    telegram_msg += f"• Trend-following with market timing\n"
                    telegram_msg += f"• Uses SPY as S&P 500 proxy for {symbol} decisions\n"
                    telegram_msg += f"• Exits {symbol} during downtrends, holds T-bills ({spxl_sma_holding_fund})\n\n"
                    telegram_msg += f"⚡ Trade Execution Summary:\n"
                    telegram_msg += f"• Investment amount: ${investment_amount:.2f}\n"
                    telegram_msg += f"• Target asset: {spxl_sma_holding_fund} (T-bills)\n"
                    telegram_msg += f"• Shares bought: {sgov_shares_to_buy:.6f}\n"
                    telegram_msg += f"• Price per share: ${sgov_price:.2f}\n"
                    telegram_msg += f"• Total invested: ${new_total_invested:.2f}"
                    
                    send_telegram_message(telegram_msg)
                    
                    # Update Firestore
                    save_balance("spxl_sma", {
                        "total_invested": new_total_invested,
                        "current_shares": current_shares,  # Keep SPXL shares (if any)
                        "holding_fund_position": holding_fund_position,
                        "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
                        "last_trade": {
                            "action": "buy_tbill",
                            "shares": sgov_shares_to_buy,
                            "price": sgov_price,
                            "amount": investment_amount
                        },
                        "trend_analysis": {
                            "spy_price": latest_price,
                            "spy_sma_200": sma_200,
                            "trend_status": "bearish",
                            "margin_band": margin
                        }
                    }, env)
                    
                    action_taken = f"Bought {sgov_shares_to_buy:.6f} shares of {spxl_sma_holding_fund} (${investment_amount:.2f}) - bearish market"
                    send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
                    return f"Bought {sgov_shares_to_buy:.6f} shares of {spxl_sma_holding_fund} (${investment_amount:.2f})"
                except Exception as e:
                    error_msg = f"Failed to buy {spxl_sma_holding_fund}: {str(e)}"
                    print(error_msg)
                    send_telegram_message(f"{symbol} SMA Error: {error_msg}")
                    return error_msg
            else:
                action_taken = f"Amount too small to buy {spxl_sma_holding_fund} shares"
                send_margin_summary_message(margin_result, f"{symbol} SMA", action_taken, investment_calc)
                return f"Amount too small to buy {spxl_sma_holding_fund} shares."
        else:
            error_msg = f"Could not get price for {spxl_sma_holding_fund}"
            print(error_msg)
            send_telegram_message(f"{symbol} SMA Error: {error_msg}")
            return error_msg


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


def send_margin_summary_message(margin_result, strategy_name, action_taken, investment_calc=None):
    """
    Send consolidated monthly margin summary to Telegram.
    
    Args:
        margin_result: Dict from check_margin_conditions() with gate results and metrics
        strategy_name: Name of the strategy (e.g., "HFEA", "SPXL SMA", "9-Sig")
        action_taken: Description of action taken (e.g., "Bought X shares", "Skipped - insufficient funds")
        investment_calc: Optional dict from calculate_monthly_investments() with investment breakdown
    """
    metrics = margin_result.get("metrics", {})
    gate_results = margin_result.get("gate_results", {})
    errors = margin_result.get("errors", [])
    
    # Build the message
    message_parts = [f"📊 {strategy_name} Monthly Update\n"]
    
    # Check for errors first
    if errors:
        message_parts.append("⚠️ ERRORS DETECTED - Defaulting to Cash-Only Mode")
        for error in errors:
            message_parts.append(f"  • {error}")
        message_parts.append("")
    
    # Market Trend
    spx_price = metrics.get("spx_price", 0)
    spx_sma = metrics.get("spx_sma", 0)
    trend_emoji = "✅" if gate_results.get("market_trend", False) else "❌"
    message_parts.append(f"Market Trend: {trend_emoji} SPX ${spx_price:.2f} (200-SMA: ${spx_sma:.2f})")
    
    # Margin Rate
    margin_rate = metrics.get("margin_rate", 0)
    fred_rate = metrics.get("fred_rate", 0)
    spread = metrics.get("spread", 0)
    rate_emoji = "✅" if gate_results.get("margin_rate", False) else "❌"
    message_parts.append(f"Margin Rate: {rate_emoji} {margin_rate*100:.1f}% (FRED {fred_rate*100:.1f}% + {spread*100:.1f}%)")
    
    # Buffer
    buffer = metrics.get("buffer", 0)
    buffer_emoji = "✅" if gate_results.get("buffer", False) else "❌"
    message_parts.append(f"Buffer: {buffer_emoji} {buffer*100:.1f}%")
    
    # Leverage
    leverage = metrics.get("leverage", 0)
    leverage_emoji = "✅" if gate_results.get("leverage", False) else "❌"
    message_parts.append(f"Leverage: {leverage_emoji} {leverage:.2f}x")
    
    # Decision
    message_parts.append("")
    if margin_result.get("allowed", False):
        message_parts.append("Decision: 🟢 Margin ENABLED (+10%)")
    else:
        message_parts.append("Decision: 🔴 Cash-Only Mode")
    
    # Investment Calculation (if provided)
    if investment_calc:
        message_parts.append("\n💰 Monthly Investment Calculation:")
        message_parts.append(f"Total Cash: ${investment_calc['total_cash']:,.2f}")
        if investment_calc['total_reserved'] > 0:
            message_parts.append(f"Reserved (bearish): ${investment_calc['total_reserved']:,.2f}")
            # Show which strategies are reserved
            for key, value in investment_calc['reserved_amounts'].items():
                message_parts.append(f"  • {key}: ${value:,.2f}")
        message_parts.append(f"Available: ${investment_calc['total_available']:,.2f}")
        if investment_calc['margin_approved'] > 0:
            message_parts.append(f"Margin Approved: ${investment_calc['margin_approved']:,.2f}")
        message_parts.append("━━━━━━━━━━━━━━━━━━━━━━")
        message_parts.append(f"Total Investing: ${investment_calc['total_investing']:,.2f}")
        
        # Show this strategy's allocation
        strategy_key = None
        if "HFEA" in strategy_name:
            strategy_key = "hfea_allo"
            pct = "47.5%"
        elif "9-Sig" in strategy_name:
            strategy_key = "nine_sig_allo"
            pct = "5%"
        elif "SMA" in strategy_name:
            strategy_key = "spxl_allo"
            pct = "47.5%"
        
        if strategy_key and strategy_key in investment_calc['strategy_amounts']:
            message_parts.append(f"\nThis Strategy ({pct}): ${investment_calc['strategy_amounts'][strategy_key]:,.2f}")
    
    # Account Info
    equity = metrics.get("equity", 0)
    portfolio_value = metrics.get("portfolio_value", 0)
    message_parts.append(f"\nAccount: Equity ${equity:,.2f} | Portfolio ${portfolio_value:,.2f}")
    
    # Action Taken
    message_parts.append(f"\nAction: {action_taken}")
    
    # Send the consolidated message
    full_message = "\n".join(message_parts)
    send_telegram_message(full_message)


# Function to get the chat title
def get_chat_title():
    telegram_key, chat_id = get_telegram_secrets()
    url = f"https://api.telegram.org/bot{telegram_key}/getChat?chat_id={chat_id}"
    response = requests.get(url)
    chat_info = response.json()

    if chat_info["ok"]:
        return chat_info["result"].get("title", "")
    else:
        return None


def get_index_data(index_symbol):
    """
    Fetch the all-time high and current price for an index using Alpaca.
    Uses 5 years of data (maximum available with Basic subscription).
    Includes explicit SSL error handling with retries.
    
    Args:
        index_symbol: Stock symbol (e.g., "SPY", "URTH")
    
    Returns:
        tuple: (current_price, all_time_high)
    """
    from datetime import datetime, timedelta
    from requests.exceptions import SSLError, ConnectionError, RequestException
    from urllib3.exceptions import SSLError as URLLib3SSLError, MaxRetryError
    
    # Get API credentials
    api = set_alpaca_environment(env=alpaca_environment)
    
    # Fetch 5 years of data from Alpaca (max available with Basic plan)
    market_data_base_url = "https://data.alpaca.markets"
    end_date = datetime.now()
    start_date = end_date - timedelta(days=1825)  # 5 years
    
    url = f"{market_data_base_url}/v2/stocks/{index_symbol}/bars"
    params = {
        "start": start_date.strftime("%Y-%m-%d"),
        "end": end_date.strftime("%Y-%m-%d"),
        "timeframe": "1Day",
        "limit": 10000,
        "adjustment": "split",
        "feed": "iex"
    }
    
    # Manual retry loop for SSL errors with exponential backoff
    # Note: urllib3 retry happens first, then we retry manually if it fails
    max_retries = 5
    for attempt in range(max_retries):
        try:
            # Use retry session with fewer retries since we're doing manual retries
            # This prevents urllib3 from exhausting all retries before we can handle it
            session = get_retry_session(max_retries=2, backoff_factor=1.0, timeout=60)
            response = session.get(
                url,
                headers=get_auth_headers(api),
                params=params,
                timeout=60  # Longer timeout for large data requests
            )
            response.raise_for_status()
            
            data = response.json()
            bars = data.get("bars", [])
            
            if not bars:
                raise ValueError(f"No Alpaca data returned for {index_symbol}")
            
            # Get all-time high and current close from bars
            all_time_high = max(bar['h'] for bar in bars)
            current_price = bars[-1]['c']
            
            return current_price, all_time_high
            
        except (SSLError, URLLib3SSLError, ConnectionError, MaxRetryError) as e:
            # SSL, connection error, or retry exhaustion - retry with exponential backoff
            # Check if it's an SSL-related error (even if wrapped in MaxRetryError)
            is_ssl_error = (
                isinstance(e, (SSLError, URLLib3SSLError)) or
                (isinstance(e, MaxRetryError) and 
                 (hasattr(e, 'reason') and isinstance(e.reason, (SSLError, URLLib3SSLError))) or
                 'SSL' in str(e) or 'SSL' in str(type(e)))
            )
            
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s, 8s, 16s
                error_type = "SSL/Connection" if is_ssl_error else "Connection"
                print(f"{error_type} error for {index_symbol} (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            else:
                error_msg = f"Error fetching index data for {index_symbol} after {max_retries} attempts: {e}"
                print(error_msg)
                raise Exception(error_msg)
        except RequestException as e:
            # Check if it's an SSL-related RequestException
            if 'SSL' in str(e) or isinstance(getattr(e, 'args', [None])[0] if e.args else None, (SSLError, URLLib3SSLError)):
                # SSL-related request error - retry
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"SSL-related request error for {index_symbol} (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
            # Other request errors - don't retry
            error_msg = f"Request error fetching index data for {index_symbol}: {e}"
            print(error_msg)
            raise Exception(error_msg)
        except Exception as e:
            # Check if it's an SSL-related error in the message
            if 'SSL' in str(e) or 'SSLError' in str(type(e)):
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    print(f"SSL error (unexpected type) for {index_symbol} (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
            # Unexpected errors - re-raise
            print(f"Unexpected error fetching index data for {index_symbol}: {e}")
            raise


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
        doc_id = index_symbol.replace("^", "").replace(".", "_")
        
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
        doc_id = index_symbol.replace("^", "").replace(".", "_")
        
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
        doc_id = index_symbol.replace("^", "").replace(".", "_")
        
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
        doc_id = index_symbol.replace("^", "").replace(".", "_")
        
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
        dual_momentum_symbols = ["SPUU", "EFO", "BND"]
        
        total_value = 0
        current_position = None
        shares_held = 0
        
        # positions is a list of dicts from Alpaca API
        for position in positions:
            ticker = position.get("symbol")
            if ticker in dual_momentum_symbols:
                position_value = float(position.get("market_value", 0))
                qty = float(position.get("qty", 0))
                total_value += position_value
                if position_value > 0:
                    current_position = ticker
                    shares_held = qty
        
        return {
            "total_value": total_value,
            "current_position": current_position,
            "shares_held": shares_held,
            "position_value": total_value
        }
    except Exception as e:
        print(f"Error getting dual momentum position value: {e}")
        return {
            "total_value": 0,
            "current_position": None,
            "shares_held": 0,
            "position_value": 0
        }


def calculate_12_month_returns(api, symbol):
    """
    Calculate 12-month return (252 trading days) for a symbol.
    
    Args:
        api: Alpaca API credentials
        symbol: Symbol to calculate return for
    
    Returns:
        float: 12-month return or None if error
    """
    try:
        # Get current price
        current_price = float(get_latest_trade(api, symbol))
        
        # Get price from 252 trading days ago
        bars = get_alpaca_historical_bars(api, symbol, days=400)
        
        if len(bars) < 252:
            print(f"Warning: Only {len(bars)} days of data available for {symbol}")
            return None
        
        # Get price from 252 trading days ago
        price_252_days_ago = bars[-253]  # -253 because -252 would be 251 days ago
        
        if price_252_days_ago == 0:
            return None
        
        return (current_price / price_252_days_ago) - 1
        
    except Exception as e:
        print(f"Error calculating 12-month return for {symbol}: {e}")
        return None


def calculate_multi_period_momentum(api, ticker):
    """
    Calculate multi-period momentum score for a sector ETF.
    
    Uses weighted combination of 1-month (40%), 3-month (20%), 6-month (20%), and 12-month (20%) returns.
    
    Args:
        api: Alpaca API credentials
        ticker: Sector ETF ticker (e.g., 'XLK', 'XLF')
    
    Returns:
        float: Weighted composite momentum score or None if error
    """
    try:
        # Get current price
        current_price = float(get_latest_trade(api, ticker))
        
        # Get historical bars (need 252+ days for 12-month calculation)
        bars = get_alpaca_historical_bars(api, ticker, days=400)
        
        if len(bars) < 252:
            print(f"Warning: Only {len(bars)} days of data available for {ticker}")
            return None
        
        # Calculate returns for each period
        returns = {}
        weights = sector_momentum_config["momentum_weights"]
        periods = sector_momentum_config["lookback_periods"]
        
        for period_name, days in periods.items():
            try:
                # Get price from N days ago
                price_n_days_ago = bars[-(days + 1)]  # +1 because we want exactly N days ago
                
                if price_n_days_ago == 0:
                    print(f"Warning: Zero price {days} days ago for {ticker}")
                    return None
                
                # Calculate return
                period_return = (current_price / price_n_days_ago) - 1
                returns[period_name] = period_return
                
            except Exception as e:
                print(f"Error calculating {period_name} return for {ticker}: {e}")
                return None
        
        # Calculate weighted composite score
        composite_score = (
            weights["1_month"] * returns["1_month"] +
            weights["3_month"] * returns["3_month"] +
            weights["6_month"] * returns["6_month"] +
            weights["12_month"] * returns["12_month"]
        )
        
        return composite_score
        
    except Exception as e:
        print(f"Error calculating multi-period momentum for {ticker}: {e}")
        return None


def rank_sectors_by_momentum(api):
    """
    Rank all sector ETFs by their multi-period momentum scores.
    
    Args:
        api: Alpaca API credentials
    
    Returns:
        list: List of tuples (ticker, momentum_score) sorted by score descending
    """
    print("Calculating momentum scores for all sector ETFs...")
    
    sector_scores = []
    sector_etfs = sector_momentum_config["sector_etfs"]
    
    for ticker in sector_etfs:
        print(f"Calculating momentum for {ticker}...")
        momentum_score = calculate_multi_period_momentum(api, ticker)
        
        if momentum_score is not None:
            sector_scores.append((ticker, momentum_score))
            print(f"{ticker}: {momentum_score:.4f} ({momentum_score:.2%})")
        else:
            print(f"Warning: Could not calculate momentum for {ticker}")
    
    # Sort by momentum score (descending)
    sector_scores.sort(key=lambda x: x[1], reverse=True)
    
    print("\nSector momentum rankings:")
    for i, (ticker, score) in enumerate(sector_scores, 1):
        print(f"{i:2d}. {ticker}: {score:.4f} ({score:.2%})")
    
    return sector_scores


def get_sector_momentum_positions(api):
    """
    Get current sector ETF positions from Alpaca account.
    
    Args:
        api: Alpaca API credentials dict
    
    Returns:
        dict: Dictionary with ticker -> shares held for sector ETFs only
    """
    try:
        # Get all positions using the list_positions function
        positions = list_positions(api)
        
        # Filter for sector ETFs only
        sector_positions = {}
        sector_etfs = sector_momentum_config["sector_etfs"]
        bond_etf = sector_momentum_config["bond_etf"]
        
        # Include both sector ETFs and bond ETF
        allowed_tickers = sector_etfs + [bond_etf]
        
        # positions is a list of dicts from Alpaca API
        for position in positions:
            ticker = position.get("symbol")
            qty = float(position.get("qty", 0))
            if ticker in allowed_tickers and qty > 0:
                sector_positions[ticker] = qty
        
        print(f"Current sector momentum positions: {sector_positions}")
        return sector_positions
        
    except Exception as e:
        print(f"Error getting sector momentum positions: {e}")
        return {}


def get_sector_momentum_value(api):
    """
    Calculate total value of sector momentum strategy positions.
    
    Args:
        api: Alpaca API credentials
    
    Returns:
        dict: Dictionary with total_value, position_breakdown, and invested_amount
    """
    try:
        # Get current positions
        positions = get_sector_momentum_positions(api)
        
        if not positions:
            return {
                "total_value": 0,
                "position_breakdown": {},
                "invested_amount": 0
            }
        
        # Calculate current value for each position
        position_breakdown = {}
        total_value = 0
        
        for ticker, shares in positions.items():
            try:
                current_price = float(get_latest_trade(api, ticker))
                position_value = shares * current_price
                position_breakdown[ticker] = {
                    "shares": shares,
                    "price": current_price,
                    "value": position_value
                }
                total_value += position_value
                
            except Exception as e:
                print(f"Error calculating value for {ticker}: {e}")
                position_breakdown[ticker] = {
                    "shares": shares,
                    "price": 0,
                    "value": 0
                }
        
        # Get invested amount from Firestore
        balances = load_balances()
        sector_data = balances.get("sector_momentum", {})
        invested_amount = sector_data.get("total_invested", 0)
        
        return {
            "total_value": total_value,
            "position_breakdown": position_breakdown,
            "invested_amount": invested_amount
        }
        
    except Exception as e:
        print(f"Error calculating sector momentum value: {e}")
        return {
            "total_value": 0,
            "position_breakdown": {},
            "invested_amount": 0
        }


def monthly_dual_momentum_strategy(api, force_execute=False, investment_calc=None, margin_result=None, skip_order_wait=False, env="live"):
    """
    Dual Momentum Strategy implementation with SPUU/EFO/BND.
    
    Combines relative momentum (SPUU vs EFO) and absolute momentum (winner > 0%).
    Handles both monthly contributions and position switching.
    
    Args:
        api: Alpaca API credentials
        force_execute: Bypass trading day check for testing
        investment_calc: Pre-calculated investment amounts (from orchestrator) - optional
        margin_result: Pre-calculated margin conditions (from orchestrator) - optional
    
    Returns:
        str: Result message
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        print("Not first trading day of the month")
        return "Not first trading day of the month"
    
    if force_execute:
        print("Dual Momentum: Force execution enabled - bypassing trading day check")
        send_telegram_message("Dual Momentum: Force execution enabled for testing - bypassing trading day check")
    
    # If not provided by orchestrator, calculate independently
    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)
    
    investment_amount = investment_calc["strategy_amounts"]["dual_momentum_allo"]
    
    # Load current strategy state from Firestore
    balances = load_balances(env)
    dual_momentum_data = balances.get("dual_momentum", {})
    total_invested = dual_momentum_data.get("total_invested", 0)
    current_position = dual_momentum_data.get("current_position", None)
    shares_held = dual_momentum_data.get("shares_held", 0)
    
    print(f"Dual Momentum Strategy - Investment: ${investment_amount:.2f}")
    print(f"Current position: {current_position}, Shares: {shares_held:.4f}")
    print(f"Total invested: ${total_invested:.2f}")
    
    # Calculate 12-month returns for underlying assets (SPY and EFA)
    # Note: We compare the underlying assets for momentum, but invest in leveraged versions
    print("Calculating 12-month momentum on underlying assets...")
    spy_return = calculate_12_month_returns(api, "SPY")
    efa_return = calculate_12_month_returns(api, "EFA")
    
    if spy_return is None or efa_return is None:
        error_msg = "Failed to calculate momentum returns - skipping strategy"
        print(error_msg)
        send_telegram_message(f"Dual Momentum Error: {error_msg}")
        return error_msg
    
    # Determine relative momentum winner (compare underlying assets)
    if spy_return > efa_return:
        winner = "SPUU"  # Invest in SPUU when SPY wins
        winner_return = spy_return  # Use underlying return for absolute momentum check
        winner_underlying = "SPY"
    else:
        winner = "EFO"  # Invest in EFO when EFA wins
        winner_return = efa_return  # Use underlying return for absolute momentum check
        winner_underlying = "EFA"
    
    # Apply absolute momentum check
    if winner_return > 0:
        target_position = winner
    else:
        target_position = "BND"
    
    print(f"SPY 12-month return: {spy_return:.2%}")
    print(f"EFA 12-month return: {efa_return:.2%}")
    print(f"Winner: {winner} ({winner_return:.2%}, underlying: {winner_underlying})")
    print(f"Target position: {target_position}")
    
    # Check if we need to switch positions
    position_changed = current_position != target_position
    
    if position_changed:
        print(f"Position change required: {current_position} -> {target_position}")
        
        # Sell current position if exists
        if current_position is not None and shares_held > 0:
            try:
                sell_order = submit_order(api, current_position, shares_held, "sell")
                wait_for_order_fill(api, sell_order["id"])
                print(f"Sold {shares_held:.4f} shares of {current_position}")
                send_telegram_message(f"Dual Momentum: Sold {shares_held:.4f} shares of {current_position}")
            except Exception as e:
                error_msg = f"Failed to sell {current_position}: {e}"
                print(error_msg)
                send_telegram_message(f"Dual Momentum Error: {error_msg}")
                return error_msg
        
        # Calculate total value to invest (existing + new)
        current_value = get_dual_momentum_position_value(api)["total_value"]
        total_to_invest = current_value + investment_amount
        
        # Buy new position
        if total_to_invest > 0:
            try:
                target_price = float(get_latest_trade(api, target_position))
                shares_to_buy = total_to_invest / target_price
                
                buy_order = submit_order(api, target_position, shares_to_buy, "buy")
                if not skip_order_wait:
                    wait_for_order_fill(api, buy_order["id"])
                
                print(f"Bought {shares_to_buy:.4f} shares of {target_position}")
                
                # Enhanced Telegram message with detailed decision rationale
                telegram_msg = f"🎯 Dual Momentum Strategy Decision\n\n"
                telegram_msg += f"📊 Momentum Analysis (Underlying Assets):\n"
                telegram_msg += f"• SPY 12-month return: {spy_return:.2%}\n"
                telegram_msg += f"• EFA 12-month return: {efa_return:.2%}\n"
                telegram_msg += f"• Relative winner: {winner} ({winner_return:.2%}, underlying: {winner_underlying})\n\n"
                telegram_msg += f"🎯 Decision Logic:\n"
                if winner_return > 0:
                    telegram_msg += f"• Absolute momentum: POSITIVE ({winner_return:.2%} > 0%)\n"
                    telegram_msg += f"• Action: Invest in {winner} (relative + absolute momentum winner)\n"
                else:
                    telegram_msg += f"• Absolute momentum: NEGATIVE ({winner_return:.2%} ≤ 0%)\n"
                    telegram_msg += f"• Action: Invest in BND (safety during negative momentum)\n\n"
                telegram_msg += f"💰 Trade Details:\n"
                telegram_msg += f"• Investment amount: ${investment_amount:.2f}\n"
                telegram_msg += f"• Target asset: {target_position}\n"
                telegram_msg += f"• Shares bought: {shares_to_buy:.4f}\n"
                telegram_msg += f"• Price per share: ${target_price:.2f}\n"
                telegram_msg += f"• Total invested: ${total_invested + investment_amount:.2f}"
                
                send_telegram_message(telegram_msg)
                
                # Update Firestore
                save_balance("dual_momentum", {
                    "total_invested": total_invested + investment_amount,
                    "current_position": target_position,
                    "shares_held": shares_to_buy,
                    "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
                    "last_momentum_check": {
                        "spy_return": spy_return,
                        "efa_return": efa_return,
                        "winner": winner,
                        "winner_underlying": winner_underlying,
                        "signal": target_position
                    }
                }, env)
                
            except Exception as e:
                error_msg = f"Failed to buy {target_position}: {e}"
                print(error_msg)
                send_telegram_message(f"Dual Momentum Error: {error_msg}")
                return error_msg
    
    else:
        # No position change needed, just add to existing position
        if investment_amount > 0:
            try:
                target_price = float(get_latest_trade(api, target_position))
                additional_shares = investment_amount / target_price
                
                buy_order = submit_order(api, target_position, additional_shares, "buy")
                if not skip_order_wait:
                    wait_for_order_fill(api, buy_order["id"])
                
                new_total_shares = shares_held + additional_shares
                new_total_invested = total_invested + investment_amount
                
                print(f"Added {additional_shares:.4f} shares of {target_position}")
                
                # Update Firestore
                save_balance("dual_momentum", {
                    "total_invested": new_total_invested,
                    "current_position": target_position,
                    "shares_held": new_total_shares,
                    "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
                    "last_momentum_check": {
                        "spy_return": spy_return,
                        "efa_return": efa_return,
                        "winner": winner,
                        "winner_underlying": winner_underlying,
                        "signal": target_position
                    }
                }, env)
                
            except Exception as e:
                error_msg = f"Failed to add to {target_position}: {e}"
                print(error_msg)
                send_telegram_message(f"Dual Momentum Error: {error_msg}")
                return error_msg
    
    # Calculate and report strategy performance
    final_position_value = get_dual_momentum_position_value(api)
    final_total_invested = total_invested + investment_amount
    strategy_return = (final_position_value["total_value"] / final_total_invested - 1) if final_total_invested > 0 else 0
    
    # Enhanced final summary
    summary_msg = f"🎯 Dual Momentum Strategy Summary\n\n"
    summary_msg += f"📊 Final Position: {target_position}\n"
    summary_msg += f"💰 Total Invested: ${final_total_invested:.2f}\n"
    summary_msg += f"📈 Current Value: ${final_position_value['total_value']:.2f}\n"
    summary_msg += f"📊 Strategy Return: {strategy_return:.2%}\n\n"
    summary_msg += f"🔍 Decision Recap:\n"
    summary_msg += f"• SPY Return: {spy_return:.2%}\n"
    summary_msg += f"• EFA Return: {efa_return:.2%}\n"
    summary_msg += f"• Winner: {winner} ({winner_return:.2%}, underlying: {winner_underlying})\n"
    summary_msg += f"• Final Choice: {target_position} {'(momentum winner)' if target_position == winner else '(safety bonds)'}"
    
    print(summary_msg)
    send_telegram_message(summary_msg)
    
    result_msg = f"Dual Momentum Strategy completed. Position: {target_position}, Return: {strategy_return:.2%}"
    
    return result_msg


def monthly_sector_momentum_strategy(api, force_execute=False, investment_calc=None, margin_result=None, skip_order_wait=False, env="live"):
    """
    Sector Momentum Rotation Strategy implementation.
    
    Invests in top 3 performing sector ETFs based on multi-period momentum,
    with SPY 200-SMA trend filtering. Switches to SCHZ bonds when SPY < 200-SMA.
    
    Args:
        api: Alpaca API credentials
        force_execute: Bypass trading day check for testing
        investment_calc: Pre-calculated investment amounts (from orchestrator) - optional
        margin_result: Pre-calculated margin conditions (from orchestrator) - optional
    
    Returns:
        str: Result message
    """
    if not force_execute and not check_trading_day(mode="monthly"):
        print("Not first trading day of the month")
        return "Not first trading day of the month"
    
    if force_execute:
        print("Sector Momentum: Force execution enabled - bypassing trading day check")
        send_telegram_message("Sector Momentum: Force execution enabled for testing - bypassing trading day check")
    
    # If not provided by orchestrator, calculate independently
    if margin_result is None:
        margin_result = check_margin_conditions(api, env=env)
    
    if investment_calc is None:
        investment_calc = calculate_monthly_investments(api, margin_result, env)
    
    investment_amount = investment_calc["strategy_amounts"]["sector_momentum_allo"]
    
    # Load current strategy state from Firestore
    balances = load_balances(env)
    sector_data = balances.get("sector_momentum", {})
    total_invested = sector_data.get("total_invested", 0)
    current_positions = sector_data.get("current_positions", {})
    holding_fund_position = sector_data.get("holding_fund_position", {})
    
    # Get holding fund (SHV) current value and shares from Alpaca
    holding_fund_ticker = sector_momentum_config["holding_fund_ticker"]
    holding_fund_max = sector_momentum_config["holding_fund_max"]
    shv_shares = get_holding_fund_shares(api, holding_fund_ticker)
    shv_value = get_holding_fund_value(api, holding_fund_ticker)
    shv_price = float(get_latest_trade(api, holding_fund_ticker)) if shv_value > 0 or investment_amount > 0 else 0
    
    print(f"Sector Momentum Strategy - Investment: ${investment_amount:.2f}")
    print(f"Current positions: {current_positions}")
    print(f"Total invested: ${total_invested:.2f}")
    print(f"{holding_fund_ticker} holding fund: {shv_shares:.6f} shares (${shv_value:.2f})")
    
    # Check SPY 200-SMA trend filter using cached market data
    print("Checking SPY 200-SMA trend filter...")
    try:
        # Get all SPY market data at once (efficient single fetch/read)
        spy_data = get_all_market_data("SPY", env=env)
        if spy_data is None:
            spy_data = update_market_data("SPY", env=env)
        
        spy_price = spy_data["price"]
        spy_sma = spy_data["sma200"]
        
        if spy_sma is None:
            error_msg = "Failed to get SPY SMA - skipping strategy"
            print(error_msg)
            send_telegram_message(f"Sector Momentum Error: {error_msg}")
            return error_msg
        
        # Use 1% margin band for consistent trend filtering with SPXL strategy
        spy_above_sma_current = spy_price > spy_sma * (1 + margin)
        print(f"SPY: ${spy_price:.2f}, 200-SMA: ${spy_sma:.2f}, Margin: {margin:.1%}, Above SMA: {spy_above_sma_current}")
        
    except Exception as e:
        error_msg = f"Error checking SPY SMA: {e}"
        print(error_msg)
        send_telegram_message(f"Sector Momentum Error: {error_msg}")
        return error_msg
    
    # Get actual current positions from Alpaca (not just Firestore)
    actual_positions = get_sector_momentum_positions(api)
    
    # Calculate current strategy value from actual Alpaca positions
    # Total available = current sector positions + SHV holding fund + new investment
    current_value_data = get_sector_momentum_value(api)
    current_sector_value = current_value_data["total_value"]  # Only sector positions, not SHV
    total_to_allocate = current_sector_value + shv_value + investment_amount
    
    print(f"Current sector positions value: ${current_sector_value:.2f}")
    print(f"SHV holding fund: ${shv_value:.2f}")
    print(f"New investment: ${investment_amount:.2f}")
    print(f"Total to allocate: ${total_to_allocate:.2f}")
    
    trades_executed = []
    
    if spy_above_sma_current:
        # Sector Mode: Invest in top 3 sectors
        print("SPY above 200-SMA: Proceeding with sector selection")
        
        # Rank sectors by momentum
        sector_rankings = rank_sectors_by_momentum(api)
        
        if len(sector_rankings) < 3:
            error_msg = "Not enough sectors with valid momentum data"
            print(error_msg)
            send_telegram_message(f"Sector Momentum Error: {error_msg}")
            return error_msg
        
        # Select top 3 sectors
        top_3_sectors = [ticker for ticker, score in sector_rankings[:3]]
        print(f"Top 3 sectors: {top_3_sectors}")
        
        # Calculate target allocation per sector (33.33% each)
        # This includes: current positions + SHV + new investment, all allocated to top 3 sectors
        target_allocation_per_sector = total_to_allocate * sector_momentum_config["target_allocation_per_sector"]
        print(f"Target allocation per sector: ${target_allocation_per_sector:.2f} (33.33% of ${total_to_allocate:.2f})")
        
        # Sell sectors not in top 3 (use actual positions from Alpaca)
        sectors_to_sell = [ticker for ticker in actual_positions.keys() if ticker not in top_3_sectors]
        
        # Reallocate pending investments from dropped sectors to new top 3
        for ticker in sectors_to_sell:
            shares_to_sell = actual_positions[ticker]
            if shares_to_sell > 0:
                try:
                    # Round down to whole shares (Alpaca doesn't allow fractional short sales)
                    whole_shares_to_sell = int(shares_to_sell)
                    if whole_shares_to_sell > 0:
                        sell_order = submit_order(api, ticker, whole_shares_to_sell, "sell")
                        if not skip_order_wait:
                            wait_for_order_fill(api, sell_order["id"])
                        if whole_shares_to_sell < shares_to_sell:
                            trades_executed.append(f"Sold {whole_shares_to_sell:.0f} shares of {ticker} (dropped from top 3, rounded down from {shares_to_sell:.4f})")
                            print(f"Sold {whole_shares_to_sell:.0f} shares of {ticker} (rounded down from {shares_to_sell:.4f})")
                        else:
                            trades_executed.append(f"Sold {whole_shares_to_sell:.0f} shares of {ticker} (dropped from top 3)")
                            print(f"Sold {whole_shares_to_sell:.0f} shares of {ticker}")
                    else:
                        print(f"Skipping sell of {ticker}: {shares_to_sell:.4f} shares is less than 1 whole share")
                except Exception as e:
                    error_msg = f"Failed to sell {ticker}: {e}"
                    print(error_msg)
                    send_telegram_message(f"Sector Momentum Error: {error_msg}")
                    return error_msg
        
        
        # Rebalance to target allocations for top 3 sectors (use actual positions from Alpaca)
        # Strategy: MINIMIZE SELLS to avoid taxable events
        # 1. First, use new investment + SHV to buy underweight sectors
        # 2. Only sell overweight sectors if absolutely necessary after using all new funds
        sector_etfs = sector_momentum_config["sector_etfs"]
        bond_etf = sector_momentum_config["bond_etf"]
        
        # Track uninvested amounts per sector and actual purchases
        uninvested_amounts = {}
        total_uninvested = 0
        actual_sector_purchases = {}  # Track actual purchase costs per sector
        
        # Calculate available new funds (investment + SHV) for buying
        available_new_funds = investment_amount + shv_value
        funds_used = 0
        
        # Step 1: Calculate target values and identify underweight/overweight sectors
        sector_targets = {}
        sector_current_values = {}
        sector_underweight = {}  # Sectors that need buying
        sector_overweight = {}   # Sectors that need selling
        
        for ticker in top_3_sectors:
            current_price = float(get_latest_trade(api, ticker))
            current_shares = actual_positions.get(ticker, 0)
            current_value = current_shares * current_price
            target_value = target_allocation_per_sector
            
            sector_targets[ticker] = target_value
            sector_current_values[ticker] = current_value
            
            value_delta = target_value - current_value
            if value_delta > 0.01:  # Underweight - needs buying
                sector_underweight[ticker] = value_delta
            elif value_delta < -0.01:  # Overweight - might need selling
                sector_overweight[ticker] = abs(value_delta)
        
        # Step 2: Use new funds to buy underweight sectors first (minimize sells)
        for ticker in sorted(sector_underweight.keys(), key=lambda x: sector_underweight[x], reverse=True):
            if funds_used >= available_new_funds:
                break  # No more funds available
                
            try:
                current_price = float(get_latest_trade(api, ticker))
                current_shares = actual_positions.get(ticker, 0)
                current_value = sector_current_values[ticker]
                target_value = sector_targets[ticker]
                value_delta = target_value - current_value
                
                # Calculate how much we can afford with remaining funds
                remaining_funds = available_new_funds - funds_used
                max_we_can_buy = min(value_delta, remaining_funds)
                
                if max_we_can_buy >= current_price:  # Can buy at least 1 share
                    shares_to_buy = round(max_we_can_buy / current_price)
                    if shares_to_buy >= 1:
                        actual_cost = shares_to_buy * current_price
                        buy_order = submit_order(api, ticker, shares_to_buy, "buy")
                        if not skip_order_wait:
                            wait_for_order_fill(api, buy_order["id"])
                        trades_executed.append(f"Bought {shares_to_buy:.0f} shares of {ticker} (${actual_cost:.2f} to reach target ${target_value:.2f})")
                        print(f"Bought {shares_to_buy:.0f} shares of {ticker} (${actual_cost:.2f} to reach target ${target_value:.2f})")
                        
                        actual_sector_purchases[ticker] = actual_cost
                        funds_used += actual_cost
                        
                        # Update current value after purchase
                        sector_current_values[ticker] += actual_cost
                    else:
                        # Can't buy even 1 share - track uninvested
                        uninvested_amounts[ticker] = max_we_can_buy
                        total_uninvested += max_we_can_buy
                        print(f"Cannot buy {ticker}: need ${current_price:.2f} for 1 share, but only have ${max_we_can_buy:.2f}")
                else:
                    # Not enough funds - track uninvested
                    uninvested_amounts[ticker] = max_we_can_buy
                    total_uninvested += max_we_can_buy
                    print(f"Insufficient funds for {ticker}: need ${value_delta:.2f}, have ${remaining_funds:.2f}")
            except Exception as e:
                error_msg = f"Failed to buy {ticker}: {e}"
                print(error_msg)
                send_telegram_message(f"Sector Momentum Error: {error_msg}")
                return error_msg
        
        # Step 3: Only sell overweight sectors if we still need to after using all new funds
        # Calculate remaining imbalance after using new funds
        for ticker in top_3_sectors:
            try:
                current_price = float(get_latest_trade(api, ticker))
                current_value = sector_current_values[ticker]  # Updated after purchases
                target_value = sector_targets[ticker]
                value_delta = target_value - current_value
                
                # Only sell if still overweight after using new funds
                if value_delta < -0.01:  # Still overweight
                    shares_to_sell = round(abs(value_delta) / current_price)
                    whole_shares_to_sell = int(shares_to_sell)  # Round down to whole shares
                    
                    if whole_shares_to_sell > 0:
                        sell_order = submit_order(api, ticker, whole_shares_to_sell, "sell")
                        if not skip_order_wait:
                            wait_for_order_fill(api, sell_order["id"])
                        trades_executed.append(f"Sold {whole_shares_to_sell:.0f} shares of {ticker} (rebalancing to 33.33%, rounded down from {shares_to_sell:.4f})")
                        print(f"Sold {whole_shares_to_sell:.0f} shares of {ticker} (rebalancing after using new funds)")
                    else:
                        print(f"Skipping sell of {ticker}: {shares_to_sell:.4f} shares is less than 1 whole share")
            except Exception as e:
                error_msg = f"Failed to rebalance {ticker}: {e}"
                print(error_msg)
                send_telegram_message(f"Sector Momentum Error: {error_msg}")
                return error_msg
                
            except Exception as e:
                error_msg = f"Failed to rebalance {ticker}: {e}"
                print(error_msg)
                send_telegram_message(f"Sector Momentum Error: {error_msg}")
                return error_msg
        
        # Handle SHV holding fund: sell SHV to fund sector purchases
        # Since SHV is included in total_to_allocate, we need to sell it to fund purchases
        shv_shares_to_buy = 0
        shv_amount_to_buy = 0
        shv_leftover_after_sectors = 0
        
        # Calculate total purchases made (to determine how much SHV to sell)
        total_purchases = sum(actual_sector_purchases.values())
        
        # If we made purchases and have SHV, sell SHV to fund them
        # SHV was part of total_to_allocate, so we need to sell it to cover purchases
        # Strategy: Sell SHV to fund purchases that exceed what new investment can cover
        if total_purchases > 0 and shv_value > 0:
            # Calculate how much SHV to sell: 
            # If purchases exceed new investment, we need SHV to cover the difference
            # Otherwise, we still sell SHV since it's part of the allocation and should be converted to sectors
            shv_amount_to_sell = min(shv_value, total_purchases)
            
            if shv_amount_to_sell > 0:
                shv_shares_to_sell = shv_amount_to_sell / shv_price if shv_price > 0 else 0
                
                # Get actual available SHV shares right before selling
                actual_shv_shares_available = get_holding_fund_shares(api, holding_fund_ticker)
                shv_shares_to_sell = min(shv_shares_to_sell, actual_shv_shares_available)
                
                if shv_shares_to_sell > 0.0001:  # Only sell if meaningful amount
                    try:
                        sell_order = submit_order(api, holding_fund_ticker, shv_shares_to_sell, "sell")
                        if not skip_order_wait:
                            wait_for_order_fill(api, sell_order["id"])
                        
                        actual_shv_sold_value = shv_shares_to_sell * shv_price
                        shv_shares -= shv_shares_to_sell
                        shv_value -= actual_shv_sold_value
                        trades_executed.append(f"Sold {shv_shares_to_sell:.6f} shares of {holding_fund_ticker} (${actual_shv_sold_value:.2f}) to fund sector purchases")
                        print(f"Sold {shv_shares_to_sell:.6f} shares of {holding_fund_ticker} (${actual_shv_sold_value:.2f}) to fund sector purchases")
                    except Exception as e:
                        error_msg = f"Sector Momentum: Failed to sell {holding_fund_ticker}: {str(e)}"
                        print(error_msg)
                        send_telegram_message(error_msg)
                        # Continue even if SHV sell fails - purchases may have already been made
                        print("Continuing despite SHV sell failure...")
        
        # If we have uninvested amounts or leftover from SHV sale, add to SHV holding fund (up to max)
        total_shv_to_add = total_uninvested + shv_leftover_after_sectors
        if total_shv_to_add > 0:
            # Note: shv_value was already reduced if we sold SHV
            current_shv_value_after_sale = shv_value  # This is already updated if we sold
            shv_value_after_investment = current_shv_value_after_sale + total_shv_to_add
            if shv_value_after_investment <= holding_fund_max:
                # Can add all leftover/uninvested amount to SHV
                shv_amount_to_buy = total_shv_to_add
                shv_shares_to_buy = shv_amount_to_buy / shv_price if shv_price > 0 else 0
            else:
                # Can only add up to max, try to buy sectors with excess
                shv_amount_to_buy = holding_fund_max - current_shv_value_after_sale
                if shv_amount_to_buy > 0:
                    shv_shares_to_buy = shv_amount_to_buy / shv_price if shv_price > 0 else 0
                
                # Try to buy sectors with excess
                excess_amount = total_shv_to_add - shv_amount_to_buy
                if excess_amount > 0:
                    # Distribute excess to sectors that need it
                    for ticker, uninvested in uninvested_amounts.items():
                        if excess_amount > 0:
                            ticker_price = float(get_latest_trade(api, ticker))
                            excess_shares = round(excess_amount / len(uninvested_amounts) / ticker_price)
                            if excess_shares >= 1:
                                try:
                                    excess_buy_order = submit_order(api, ticker, excess_shares, "buy")
                                    if not skip_order_wait:
                                        wait_for_order_fill(api, excess_buy_order["id"])
                                    trades_executed.append(f"Bought {excess_shares:.0f} shares of {ticker} (from excess after SHV max)")
                                    print(f"Bought {excess_shares:.0f} shares of {ticker} (excess after SHV)")
                                    excess_amount -= (excess_shares * ticker_price)
                                except Exception as e:
                                    print(f"Failed to buy {ticker} with excess: {e}")
        
        # Buy SHV holding fund if needed (only if amount meets minimum order size)
        if shv_shares_to_buy > 0:
            # Check if the order amount meets Alpaca's minimum ($1.00)
            if shv_amount_to_buy < margin_control_config["min_investment"]:
                print(f"Skipping SHV purchase: amount ${shv_amount_to_buy:.2f} is below minimum order size (${margin_control_config['min_investment']:.2f})")
                print(f"Leftover ${shv_amount_to_buy:.2f} will remain uninvested (too small for any order)")
            else:
                try:
                    shv_buy_order = submit_order(api, holding_fund_ticker, shv_shares_to_buy, "buy")
                    if not skip_order_wait:
                        wait_for_order_fill(api, shv_buy_order["id"])
                    shv_shares += shv_shares_to_buy
                    shv_value += shv_amount_to_buy
                    trades_executed.append(f"Bought {shv_shares_to_buy:.6f} shares of {holding_fund_ticker} (${shv_amount_to_buy:.2f}) - holding fund")
                    print(f"Bought {shv_shares_to_buy:.6f} shares of {holding_fund_ticker} for ${shv_amount_to_buy:.2f} (holding fund)")
                    send_telegram_message(f"Sector Momentum: Bought {shv_shares_to_buy:.6f} shares of {holding_fund_ticker} (holding fund)")
                except Exception as e:
                    error_msg = f"Sector Momentum: Failed to buy {holding_fund_ticker}: {str(e)}"
                    print(error_msg)
                    send_telegram_message(error_msg)
                    # Don't return error - continue with other operations even if SHV buy fails
                    print("Continuing despite SHV purchase failure...")
        
        # Update Firestore with sector positions
        # ALWAYS use actual positions from Alpaca - don't calculate or filter
        updated_actual_positions = get_sector_momentum_positions(api)
        
        # Use ALL actual positions from Alpaca, not just top 3
        # This ensures Firestore matches reality even if positions exist outside top 3
        new_positions = updated_actual_positions.copy()  # Use all actual positions
        
        # Update holding fund position (get fresh from Alpaca to be accurate)
        updated_shv_shares = get_holding_fund_shares(api, holding_fund_ticker)
        holding_fund_position[holding_fund_ticker] = updated_shv_shares
        
        save_balance("sector_momentum", {
            "total_invested": total_invested + investment_amount,
            "current_positions": new_positions,  # All actual positions from Alpaca
            "holding_fund_position": holding_fund_position,
            "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
            "top_3_sectors": top_3_sectors,
            "spy_above_sma": True,
            "last_momentum_scores": dict(sector_rankings[:5])  # Top 5 for reference
        }, env)
        
    else:
        # Bond Mode: Sell all sectors, invest in SCHZ
        print("SPY below 200-SMA: Switching to bond mode (SCHZ)")
        
        bond_etf = sector_momentum_config["bond_etf"]
        sector_etfs = sector_momentum_config["sector_etfs"]
        
        # Sell all sector positions (use actual positions from Alpaca, not Firestore)
        # Filter to only sell sector ETFs (not SCHZ if it's already held)
        for ticker, shares in actual_positions.items():
            # Only sell sector ETFs, not the bond ETF
            if ticker in sector_etfs and shares > 0:
                try:
                    # Round down to whole shares (sector ETFs are non-fractionable)
                    whole_shares_to_sell = int(shares)
                    if whole_shares_to_sell > 0:
                        sell_order = submit_order(api, ticker, whole_shares_to_sell, "sell")
                        if not skip_order_wait:
                            wait_for_order_fill(api, sell_order["id"])
                        if whole_shares_to_sell < shares:
                            trades_executed.append(f"Sold {whole_shares_to_sell:.0f} shares of {ticker} (rounded down from {shares:.4f})")
                            print(f"Sold {whole_shares_to_sell:.0f} shares of {ticker} (rounded down from {shares:.4f})")
                        else:
                            trades_executed.append(f"Sold {whole_shares_to_sell:.0f} shares of {ticker}")
                            print(f"Sold {whole_shares_to_sell:.0f} shares of {ticker}")
                    else:
                        print(f"Skipping sell of {ticker}: {shares:.4f} shares is less than 1 whole share")
                except Exception as e:
                    error_msg = f"Failed to sell {ticker}: {e}"
                    print(error_msg)
                    send_telegram_message(f"Sector Momentum Error: {error_msg}")
                    return error_msg
        
        # Invest all in SCHZ
        if total_to_allocate > 0:
            try:
                schz_price = float(get_latest_trade(api, bond_etf))
                schz_shares = total_to_allocate / schz_price
                
                buy_order = submit_order(api, bond_etf, schz_shares, "buy")
                if not skip_order_wait:
                    wait_for_order_fill(api, buy_order["id"])
                
                trades_executed.append(f"Bought {schz_shares:.4f} shares of {bond_etf} (bear market protection)")
                print(f"Bought {schz_shares:.4f} shares of {bond_etf}")
                
                # Update holding fund position (get fresh from Alpaca)
                updated_shv_shares = get_holding_fund_shares(api, holding_fund_ticker)
                holding_fund_position[holding_fund_ticker] = updated_shv_shares
                
                # Get ALL actual positions from Alpaca (not just calculated SCHZ)
                updated_actual_positions = get_sector_momentum_positions(api)
                
                # Update Firestore with ALL actual positions from Alpaca
                save_balance("sector_momentum", {
                    "total_invested": total_invested + investment_amount,
                    "current_positions": updated_actual_positions,  # All actual positions from Alpaca
                    "holding_fund_position": holding_fund_position,
                    "last_trade_date": datetime.datetime.now().strftime("%Y-%m-%d"),
                    "top_3_sectors": [],
                    "spy_above_sma": False,
                    "last_momentum_scores": {}
                }, env)
                
            except Exception as e:
                error_msg = f"Failed to buy {bond_etf}: {e}"
                print(error_msg)
                send_telegram_message(f"Sector Momentum Error: {error_msg}")
                return error_msg
    
    # Calculate and report strategy performance
    final_value_data = get_sector_momentum_value(api)
    final_total_invested = total_invested + investment_amount
    strategy_return = (final_value_data["total_value"] / final_total_invested - 1) if final_total_invested > 0 else 0
    
    # Prepare comprehensive Telegram report
    telegram_msg = "🎯 Sector Momentum Strategy Decision\n\n"
    
    # Trend filter analysis
    telegram_msg += f"📈 Trend Filter Analysis:\n"
    telegram_msg += f"• SPY Price: ${spy_price:.2f}\n"
    telegram_msg += f"• SPY 200-SMA: ${spy_sma:.2f}\n"
    telegram_msg += f"• Trend Status: {'🟢 BULLISH' if spy_above_sma_current else '🔴 BEARISH'}\n"
    telegram_msg += f"• Decision: {'Invest in sectors' if spy_above_sma_current else 'Switch to bonds (SCHZ)'}\n\n"
    
    if spy_above_sma_current and len(sector_rankings) >= 5:
        # Multi-period momentum analysis
        telegram_msg += f"📊 Multi-Period Momentum Analysis:\n"
        telegram_msg += f"• Weights: 1M(40%), 3M(20%), 6M(20%), 12M(20%)\n"
        telegram_msg += f"• All sector scores calculated:\n"
        for i, (ticker, score) in enumerate(sector_rankings[:5], 1):
            sector_name = sector_momentum_config["sector_names"].get(ticker, ticker)
            telegram_msg += f"  {i}. {ticker} ({sector_name}): {score:.2%}\n"
        telegram_msg += f"\n🎯 Selection Logic:\n"
        top_3_with_names = [f"{ticker} ({sector_momentum_config['sector_names'].get(ticker, ticker)})" for ticker in top_3_sectors]
        telegram_msg += f"• Top 3 sectors selected: {', '.join(top_3_with_names)}\n"
        telegram_msg += f"• Allocation: 33.33% each\n"
        telegram_msg += f"• Investment per sector: ${target_allocation_per_sector:.2f}\n\n"
    else:
        telegram_msg += f"🔒 Bond Mode Activated:\n"
        telegram_msg += f"• Reason: SPY below 200-SMA (bearish trend)\n"
        telegram_msg += f"• Action: Sell all sectors, invest in SCHZ (Bonds)\n"
        telegram_msg += f"• Bond ETF: {bond_etf}\n\n"
    
    # Trade execution summary
    telegram_msg += f"⚡ Trade Execution Summary:\n"
    telegram_msg += f"• Total trades executed: {len(trades_executed)}\n"
    if trades_executed:
        for trade in trades_executed:
            telegram_msg += f"  • {trade}\n"
    telegram_msg += f"\n💰 Portfolio Summary:\n"
    telegram_msg += f"• Total invested: ${final_total_invested:.2f}\n"
    telegram_msg += f"• Current value: ${final_value_data['total_value']:.2f}\n"
    telegram_msg += f"• Strategy return: {strategy_return:.2%}"
    
    print(telegram_msg)
    send_telegram_message(telegram_msg)
    
    result_msg = f"Sector Momentum Strategy completed. Return: {strategy_return:.2%}"
    return result_msg


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
    
    print(f"Total investing power: ${investment_calc['total_investing']:.2f}")
    print(f"  HFEA (17.5%): ${investment_calc['strategy_amounts']['hfea_allo']:.2f}")
    print(f"  Golden HFEA Lite (17.5%): ${investment_calc['strategy_amounts']['golden_hfea_lite_allo']:.2f}")
    print(f"  SPXL (35%): ${investment_calc['strategy_amounts']['spxl_allo']:.2f}")
    print(f"  RSSB/WTIP (5%): ${investment_calc['strategy_amounts']['rssb_wtip_allo']:.2f}")
    print(f"  9-Sig (5%): ${investment_calc['strategy_amounts']['nine_sig_allo']:.2f}")
    print(f"  Dual Momentum (10%): ${investment_calc['strategy_amounts']['dual_momentum_allo']:.2f}")
    print(f"  Sector Momentum (10%): ${investment_calc['strategy_amounts']['sector_momentum_allo']:.2f}")
    
    # Run all six strategies with pre-calculated budgets
    results = {}
    
    print("\n=== Executing HFEA ===")
    results["hfea"] = make_monthly_buys(api, force_execute, investment_calc, margin_result, skip_order_wait, env)
    
    print("\n=== Executing Golden HFEA Lite ===")
    results["golden_hfea_lite"] = make_monthly_buys_golden_hfea_lite(api, force_execute, investment_calc, margin_result, skip_order_wait, env)
    
    print("\n=== Executing SPXL SMA ===")
    results["spxl"] = monthly_buying_sma(api, "SPXL", force_execute, investment_calc, margin_result, skip_order_wait, env)
    
    print("\n=== Executing RSSB/WTIP ===")
    results["rssb_wtip"] = make_monthly_buys_rssb_wtip(api, force_execute, investment_calc, margin_result, skip_order_wait, env)
    
    print("\n=== Executing 9-Sig ===")
    results["nine_sig"] = make_monthly_nine_sig_contributions(api, force_execute, investment_calc, margin_result, skip_order_wait, env)
    
    print("\n=== Executing Dual Momentum ===")
    results["dual_momentum"] = monthly_dual_momentum_strategy(api, force_execute, investment_calc, margin_result, skip_order_wait, env)
    
    print("\n=== Executing Sector Momentum ===")
    results["sector_momentum"] = monthly_sector_momentum_strategy(api, force_execute, investment_calc, margin_result, skip_order_wait, env)
    
    print("\n=== All Monthly Strategies Complete ===")
    
    return results


def monthly_invest_rssb_sector_momentum_custom(api, total_budget=300.0, force_execute=True, skip_order_wait=False, env="live"):
    """
    Special occasion function to run only RSSB/WTIP and Sector Momentum strategies
    with a custom budget, split 50/50 between the two strategies.
    
    Args:
        api: Alpaca API credentials
        total_budget: Total budget to invest (default: $300)
        force_execute: Bypass trading day check (default: True for special occasions)
        skip_order_wait: Skip waiting for order fills (default: False)
        env: Environment ("live" or "paper")
    
    Returns:
        dict with results from both strategies
    """
    print("=== Special Occasion: RSSB/WTIP + Sector Momentum Investment ===")
    print(f"Total budget: ${total_budget:.2f}")
    
    # Split budget 50/50 between the two strategies
    rssb_wtip_amount = total_budget / 2.0
    sector_momentum_amount = total_budget / 2.0
    
    print(f"  RSSB/WTIP: ${rssb_wtip_amount:.2f}")
    print(f"  Sector Momentum: ${sector_momentum_amount:.2f}")
    
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
    
    # Step 2: Get margin conditions (needed for strategy functions)
    print("\nStep 2: Getting margin conditions...")
    margin_result = check_margin_conditions(api, env=env)
    
    # Step 3: Create custom investment_calc dict with only our two strategies
    # The functions expect this structure but we'll override the amounts
    investment_calc = {
        "total_cash": total_budget,
        "total_reserved": 0,
        "total_available": total_budget,
        "margin_approved": 0,
        "used_margin": 0,
        "total_investing": total_budget,
        "strategy_amounts": {
            "rssb_wtip_allo": rssb_wtip_amount,
            "sector_momentum_allo": sector_momentum_amount,
            # Set other strategies to 0 (they won't be called anyway)
            "hfea_allo": 0,
            "golden_hfea_lite_allo": 0,
            "spxl_allo": 0,
            "nine_sig_allo": 0,
            "dual_momentum_allo": 0,
        },
        "reserved_amounts": {}
    }
    
    # Step 4: Run both strategies with custom budget
    results = {}
    
    print("\n=== Executing RSSB/WTIP ===")
    results["rssb_wtip"] = make_monthly_buys_rssb_wtip(
        api, 
        force_execute=force_execute, 
        investment_calc=investment_calc, 
        margin_result=margin_result, 
        skip_order_wait=skip_order_wait, 
        env=env
    )
    
    print("\n=== Executing Sector Momentum ===")
    results["sector_momentum"] = monthly_sector_momentum_strategy(
        api, 
        force_execute=force_execute, 
        investment_calc=investment_calc, 
        margin_result=margin_result, 
        skip_order_wait=skip_order_wait, 
        env=env
    )
    
    print("\n=== Special Occasion Investment Complete ===")
    
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
            "golden_hfea_lite_allo": 0,
            "spxl_allo": 0,
            "nine_sig_allo": 0,
            "dual_momentum_allo": 0,
            "sector_momentum_allo": 0,
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


@app.route("/monthly_buy_golden_hfea_lite", methods=["POST"])
def monthly_buy_golden_hfea_lite(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return make_monthly_buys_golden_hfea_lite(api, env=alpaca_environment)


@app.route("/rebalance_golden_hfea_lite", methods=["POST"])
def rebalance_golden_hfea_lite(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return rebalance_golden_hfea_lite_portfolio(api)


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
    Executes monthly dual momentum strategy with SPUU/EFO/BND.
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


@app.route("/monthly_sector_momentum", methods=["POST"])
def monthly_sector_momentum(request):
    """
    Cloud Function endpoint for Sector Momentum Strategy.
    Executes monthly sector momentum rotation strategy with top 3 sector ETFs.
    """
    try:
        api = set_alpaca_environment(env=alpaca_environment)
        result = monthly_sector_momentum_strategy(api, env=alpaca_environment)
        return jsonify({"result": result}), 200
    except Exception as e:
        error_message = f"Sector Momentum Strategy error: {str(e)}"
        print(error_message)
        send_telegram_message(error_message)
        return jsonify({"error": error_message}), 500


@app.route("/index_alert", methods=["POST"])
def index_alert(request):
    return check_unified_index_alert(request, env=alpaca_environment)


# @app.route('/monthly_buy_tqqq', methods=['POST'])
# def monthly_buy_tqqq(request):
#     api = set_alpaca_environment(env=alpaca_environment)  # or 'paper' based on your needs
#     return make_monthly_buy_tqqq(api)

# @app.route('/sell_tqqq_below_200sma', methods=['POST'])
# def sell_tqqq_below_200sma(request):
#     api = set_alpaca_environment(env=alpaca_environment)  # or 'paper' based on your needs
#     return sell_tqqq_if_below_200sma(api)

# @app.route('/buy_tqqq_above_200sma', methods=['POST'])
# def buy_tqqq_above_200sma(request):
#     api = set_alpaca_environment(env=alpaca_environment)  # or 'paper' based on your needs
#     return buy_tqqq_if_above_200sma(api)


def run_local(action, env="paper", request="test", force_execute=False, investment_amount=None):
    api = set_alpaca_environment(env=env, use_secret_manager=False)
    if action == "monthly_invest_all":
        return monthly_invest_all_strategies(api, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "monthly_buy_hfea":
        return make_monthly_buys(api, force_execute=force_execute)
    elif action == "rebalance_hfea":
        return rebalance_portfolio(api)
    elif action == "monthly_buy_golden_hfea_lite":
        return make_monthly_buys_golden_hfea_lite(api, force_execute=force_execute)
    elif action == "rebalance_golden_hfea_lite":
        return rebalance_golden_hfea_lite_portfolio(api)
    elif action == "monthly_nine_sig_contributions":
        return make_monthly_nine_sig_contributions(api, force_execute=force_execute, env=env)
    elif action == "quarterly_nine_sig_signal":
        return execute_quarterly_nine_sig_signal(api, force_execute=force_execute, env=env)
    elif action == "monthly_buy_spxl":
        return monthly_buying_sma(api, "SPXL", force_execute=force_execute, env=env)
    elif action == "sell_spxl_below_200sma":
        return daily_trade_sma(api, "SPXL", env=env)
    elif action == "buy_spxl_above_200sma":
        return daily_trade_sma(api, "SPXL", env=env)
    elif action == "index_alert":
        return check_unified_index_alert(request, env=env)
    elif action == "monthly_dual_momentum":
        return monthly_dual_momentum_strategy(api, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "monthly_sector_momentum":
        return monthly_sector_momentum_strategy(api, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "monthly_invest_rssb_sector_custom":
        # Special occasion: RSSB/WTIP + Sector Momentum with $300 budget
        return monthly_invest_rssb_sector_momentum_custom(api, total_budget=300.0, force_execute=True, skip_order_wait=True, env=env)
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
            "monthly_buy_golden_hfea_lite",
            "rebalance_golden_hfea_lite",
            "monthly_nine_sig_contributions",
            "quarterly_nine_sig_signal",
            "monthly_buy_spxl",
            "sell_spxl_below_200sma",
            "buy_spxl_above_200sma",
            "index_alert",
            "monthly_dual_momentum",
            "monthly_sector_momentum",
            "monthly_invest_rssb_sector_custom",
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
    # save_balance("SPXL_SMA", 100)

# local execution:
# RECOMMENDED - Run all monthly strategies with coordinated budgets:
# python3 main.py --action monthly_invest_all --env paper --force
#
# Individual strategy execution (for testing):
# python3 main.py --action monthly_buy_hfea --env paper --force
# python3 main.py --action monthly_buy_spxl --env paper --force
# python3 main.py --action monthly_nine_sig_contributions --env paper --force
#
# Other actions:
# python3 main.py --action rebalance_hfea --env paper
# python3 main.py --action quarterly_nine_sig_signal --env paper --force
# python3 main.py --action sell_spxl_below_200sma --env paper
# python3 main.py --action buy_spxl_above_200sma --env paper
# python3 main.py --action index_alert --env paper  # For unified index alerts (use with request body)

# consider shifting to short term bonds when 200sma is below https://app.alpaca.markets/trade/BIL?asset_class=stocks
