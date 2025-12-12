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


def get_cached_market_data(symbol, data_type):
    """
    Get cached market data from Firestore to avoid redundant Alpaca API calls.
    Cache expires after 5 minutes. Works across all Cloud Functions.
    
    Args:
        symbol: Market symbol (e.g., "SPY", "URTH", "EEM", "EFA")
        data_type: "price", "sma200", "sma255", or state fields
    
    Returns:
        Cached value or None if not cached/expired/unavailable
    """
    try:
        # Normalize symbol for Firestore document ID (remove special chars)
        doc_id = symbol.replace("^", "").replace(".", "_")
        
        doc_ref = get_firestore_client().collection("market-data").document(doc_id)
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


def get_all_market_data(symbol):
    """
    Get ALL market data for a symbol efficiently.
    Use this when you need multiple metrics (price, sma200, sma255, states).
    If cache is stale, fetches fresh and calculates all metrics at once.
    
    Args:
        symbol: Stock symbol (e.g., "SPY", "URTH")
    
    Returns:
        dict with all market data: price, sma200, sma255, sma200_state, sma255_state, timestamp
        Or None if cache is stale (triggers update)
    
    Example:
        data = get_all_market_data("SPY")
        if data is None:
            data = update_market_data("SPY")
        spy_price = data["price"]
        spy_sma = data["sma200"]
    """
    try:
        # Normalize symbol for Firestore document ID
        doc_id = symbol.replace("^", "").replace(".", "_")
        
        doc_ref = get_firestore_client().collection("market-data").document(doc_id)
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


def set_cached_market_data(symbol, data_type, value):
    """
    Cache market data to Firestore to avoid redundant Alpaca API calls.
    Accessible across all Cloud Functions. Automatically expires after 5 minutes.
    
    Args:
        symbol: Market symbol
        data_type: "price", "sma200", or "sma255"
        value: Data value to cache
    """
    try:
        # Normalize symbol for Firestore document ID (remove special chars)
        doc_id = symbol.replace("^", "").replace(".", "_")
        
        doc_ref = get_firestore_client().collection("market-data").document(doc_id)
        
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


def get_alpaca_historical_bars(api, symbol, days=400):
    """
    Fetch historical daily bars from Alpaca using IEX feed.
    Primary data source for all SMA calculations (no rate limiting).
    
    Args:
        api: Alpaca API credentials dict
        symbol: Stock symbol (e.g., "SPY", "URTH")
        days: Number of calendar days of history to fetch (default 400 for 200-day SMA)
    
    Returns:
        List of closing prices (most recent last), or None on error
    """
    try:
        from datetime import datetime, timedelta
        
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
        
        response = requests.get(url, headers=get_auth_headers(api), params=params)
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
        
    except Exception as e:
        print(f"Alpaca historical fetch failed for {symbol}: {e}")
        return None


def get_latest_trade(api, symbol):
    """
    Get latest trade price from Alpaca.
    No fallback - raises error if Alpaca data unavailable.
    
    Args:
        api: Alpaca API credentials dict
        symbol: Stock symbol
    
    Returns:
        Latest trade price
    """
    symbol = symbol.upper()
    market_data_base_url = "https://data.alpaca.markets"
    url = f"{market_data_base_url}/v2/stocks/{symbol}/trades/latest"
    
    response = requests.get(url, headers=get_auth_headers(api))
    response.raise_for_status()
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


def check_margin_conditions(api):
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
            spy_data = get_all_market_data("SPY")
            if spy_data is None:
                spy_data = update_market_data("SPY")
            
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
def save_nine_sig_quarterly_data(quarter_id, tqqq_balance, agg_balance, signal_line, action, quarterly_contributions):
    """Save quarterly data following 3Sig methodology for next quarter's calculations"""
    doc_ref = get_firestore_client().collection("nine-sig-quarters").document(quarter_id)
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


def get_previous_quarter_tqqq_balance():
    """Get previous quarter's TQQQ ending balance for signal line calculation"""
    docs = get_firestore_client().collection("nine-sig-quarters").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(1).stream()
    for doc in docs:
        data = doc.to_dict()
        return data.get("previous_tqqq_balance", 0)
    return 0


def track_nine_sig_monthly_contribution(amount):
    """
    Track actual 9-Sig monthly contribution for quarterly signal calculation.
    Handles Firestore unavailability gracefully for local testing.
    """
    try:
        current_month = datetime.datetime.now().strftime("%Y-%m")
        doc_ref = get_firestore_client().collection("nine-sig-monthly-contributions").document(current_month)
        doc_ref.set({
            "month": current_month,
            "amount": amount,
            "timestamp": datetime.datetime.utcnow()
        })
    except Exception as e:
        print(f"Warning: Could not track 9-Sig contribution to Firestore: {e}")


def get_quarterly_nine_sig_contributions():
    """
    Get sum of actual 9-Sig contributions made in the current quarter.
    Returns 0 if Firestore is unavailable (local testing).
    """
    try:
        today = datetime.datetime.now()
        
        # Determine current quarter's start month
        quarter_start_month = ((today.month - 1) // 3) * 3 + 1
        quarter_start = datetime.datetime(today.year, quarter_start_month, 1)
        
        # Get all monthly contributions from this quarter
        docs = get_firestore_client().collection("nine-sig-monthly-contributions").where(
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
        
        response = requests.get(url, headers=get_auth_headers(api), params=params)
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


def count_ignored_sell_signals():
    """Count how many sell signals have been ignored in the current crash protection period"""
    try:
        # Get recent quarters with ignored sell signals
        docs = get_firestore_client().collection("nine-sig-quarters").where("action_taken", "==", "SELL_IGNORED").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(4).stream()
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
        margin_result = check_margin_conditions(api)
    
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
            track_nine_sig_monthly_contribution(investment_amount)
            
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
        margin_result = check_margin_conditions(api)
    
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
        margin_result = check_margin_conditions(api)
    
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

    # Calculate underweight amounts
    rssb_underweight = max(0, target_rssb_value - rssb_value)
    wtip_underweight = max(0, target_wtip_value - wtip_value)
    total_underweight = rssb_underweight + wtip_underweight

    # If perfectly balanced, use standard split
    if total_underweight == 0:
        rssb_amount = investment_amount * rssb_allocation
        wtip_amount = investment_amount * wtip_allocation
    else:
        # Allocate proportionally based on underweight amounts
        rssb_amount = (rssb_underweight / total_underweight) * investment_amount
        wtip_amount = (wtip_underweight / total_underweight) * investment_amount

    # Get current prices for RSSB and WTIP
    rssb_price = float(get_latest_trade(api, "RSSB"))
    wtip_price = float(get_latest_trade(api, "WTIP"))
    
    # Check if we can use BIL funds to buy WTIP (if BIL + new investment reaches threshold)
    bil_available_for_wtip = 0
    bil_amount_to_sell = 0
    if bil_value > 0:
        # Check if BIL + new investment would allow us to buy at least 1 WTIP share
        total_available_for_wtip = bil_value + wtip_amount
        potential_wtip_shares = round(total_available_for_wtip / wtip_price)
        if potential_wtip_shares >= 1:
            # Calculate exactly how much we need from BIL (only what's needed, with 1% buffer for price fluctuations)
            wtip_shares_we_can_buy = potential_wtip_shares
            wtip_cost_needed = wtip_shares_we_can_buy * wtip_price * 1.01  # 1% buffer
            bil_amount_to_sell = max(0, min(bil_value, wtip_cost_needed - wtip_amount))
            if bil_amount_to_sell > 0:
                bil_available_for_wtip = bil_amount_to_sell
                wtip_amount += bil_available_for_wtip
    
    # Calculate number of shares to buy
    rssb_shares_to_buy = rssb_amount / rssb_price
    # WTIP doesn't support fractional shares on Alpaca - round to whole shares
    wtip_shares_to_buy = round(wtip_amount / wtip_price)
    
    # Handle WTIP non-fractionable shares and BIL holding fund
    uninvested_wtip_amount = 0
    original_wtip_amount = wtip_amount - bil_available_for_wtip  # Original allocation from new investment
    
    if wtip_shares_to_buy < 1:
        # Can't buy WTIP - calculate uninvested amount (only from new investment, not BIL)
        uninvested_wtip_amount = original_wtip_amount
        wtip_shares_to_buy = 0
        wtip_amount = 0
        # If we added BIL funds but still can't buy, we need to return those BIL funds
        if bil_available_for_wtip > 0:
            # Don't use BIL funds if we can't buy
            bil_available_for_wtip = 0
    else:
        # Adjust wtip_amount to reflect actual purchase
        wtip_amount = wtip_shares_to_buy * wtip_price
        # Calculate uninvested: if we used BIL, the uninvested is only from new investment portion
        if bil_available_for_wtip > 0:
            # We used BIL funds, so uninvested is the difference between what we allocated and what we spent
            spent_from_new_investment = wtip_amount - bil_available_for_wtip
            uninvested_wtip_amount = max(0, original_wtip_amount - spent_from_new_investment)
        else:
            # No BIL used, uninvested is the difference
            uninvested_wtip_amount = max(0, original_wtip_amount - wtip_amount)
    
    # If we're using BIL funds to buy WTIP, sell only what we need (do this first to calculate leftover)
    bil_leftover_after_wtip = 0
    if bil_amount_to_sell > 0 and wtip_shares_to_buy >= 1:
        # Calculate exact amount needed: cost of WTIP shares we'll buy (with small buffer)
        actual_wtip_cost = wtip_shares_to_buy * wtip_price * 1.01  # 1% buffer for price fluctuations
        bil_amount_needed = max(0, actual_wtip_cost - (wtip_amount - bil_available_for_wtip))
        bil_shares_to_sell = bil_amount_needed / bil_price if bil_price > 0 else 0
        
        if bil_shares_to_sell > 0:
            # Note: We'll execute this sell order later, but calculate leftover now
            actual_wtip_cost_final = wtip_shares_to_buy * wtip_price
            bil_leftover_after_wtip = max(0, bil_amount_needed - actual_wtip_cost_final)
            # Update bil_value for calculations below (we'll actually sell later)
            bil_value -= bil_amount_needed
    
    # Handle BIL holding fund for uninvested WTIP amounts and leftover from BIL sale
    bil_shares_to_buy = 0
    bil_amount_to_buy = 0
    total_bil_to_add = uninvested_wtip_amount + bil_leftover_after_wtip
    
    if total_bil_to_add > 0:
        # Check if we can add to BIL holding fund
        # Note: bil_value was already reduced if we sold BIL, so we need to account for that
        current_bil_value_after_sale = bil_value  # This is already updated if we sold
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
                    # We already bought WTIP, so add to the existing purchase
                    wtip_shares_to_buy += excess_wtip_shares
                    wtip_amount += excess_wtip_shares * wtip_price
                    print(f"Using excess ${excess_amount:.2f} to buy additional {excess_wtip_shares} shares of WTIP")
                else:
                    # Still can't buy WTIP, add excess to BIL if under max
                    if current_bil_value_after_sale + bil_amount_to_buy + excess_amount <= rssb_wtip_holding_fund_max:
                        bil_amount_to_buy += excess_amount
                        bil_shares_to_buy = bil_amount_to_buy / bil_price if bil_price > 0 else 0
                    else:
                        # Can't add to BIL (over max) and can't buy WTIP - this money will remain as cash
                        print(f"Warning: ${excess_amount:.2f} cannot be invested (BIL at max, WTIP too expensive)")
    
    print(f"RSSB/WTIP Strategy - Investment: ${investment_amount:.2f}")
    print(f"Current positions: {current_positions}")
    print(f"Total invested: ${total_invested:.2f}")
    print(f"BIL holding fund: {get_holding_fund_shares(api, rssb_wtip_holding_fund):.6f} shares (${bil_value:.2f})")
    
    # Execute market orders with enhanced tracking
    shares_bought = []
    trades_executed = []
    
    # If we're using BIL funds to buy WTIP, execute the sell order (we calculated this above)
    if bil_amount_to_sell > 0 and wtip_shares_to_buy >= 1:
        actual_wtip_cost = wtip_shares_to_buy * wtip_price * 1.01
        bil_amount_needed = max(0, actual_wtip_cost - (wtip_amount - bil_available_for_wtip))
        bil_shares_to_sell = bil_amount_needed / bil_price if bil_price > 0 else 0
        
        if bil_shares_to_sell > 0:
            try:
                # Sell only the amount of BIL we actually need
                sell_order = submit_order(api, rssb_wtip_holding_fund, bil_shares_to_sell, "sell")
                if not skip_order_wait:
                    wait_for_order_fill(api, sell_order["id"])
                
                bil_shares -= bil_shares_to_sell
                trades_executed.append(f"Sold {bil_shares_to_sell:.6f} shares of {rssb_wtip_holding_fund} (${bil_amount_needed:.2f}) to buy WTIP")
                print(f"Sold {bil_shares_to_sell:.6f} shares of {rssb_wtip_holding_fund} (${bil_amount_needed:.2f}) to buy WTIP")
                if bil_leftover_after_wtip > 0:
                    print(f"Leftover from BIL sale after WTIP purchase: ${bil_leftover_after_wtip:.2f}")
            except Exception as e:
                error_msg = f"RSSB/WTIP: Failed to sell {rssb_wtip_holding_fund}: {str(e)}"
                print(error_msg)
                send_telegram_message(error_msg)
                return error_msg
    
    # Buy RSSB and WTIP
    for symbol, qty, amount in [("RSSB", rssb_shares_to_buy, rssb_amount), ("WTIP", wtip_shares_to_buy, wtip_amount)]:
        if qty > 0:
            try:
                order = submit_order(api, symbol, qty, "buy")
                if not skip_order_wait:
                    wait_for_order_fill(api, order["id"])
                
                shares_bought.append(qty)
                trades_executed.append(f"Bought {qty:.6f} shares of {symbol} for ${amount:.2f}")
                print(f"Bought {qty:.6f} shares of {symbol} for ${amount:.2f}")
                send_telegram_message(f"RSSB/WTIP: Bought {qty:.6f} shares of {symbol} for ${amount:.2f}")
                
            except Exception as e:
                error_msg = f"RSSB/WTIP: Failed to buy {symbol}: {str(e)}"
                print(error_msg)
                send_telegram_message(error_msg)
                return error_msg
    
    # Buy BIL holding fund if needed
    if bil_shares_to_buy > 0:
        try:
            bil_order = submit_order(api, rssb_wtip_holding_fund, bil_shares_to_buy, "buy")
            if not skip_order_wait:
                wait_for_order_fill(api, bil_order["id"])
            
            bil_shares += bil_shares_to_buy
            bil_value += bil_amount_to_buy
            trades_executed.append(f"Bought {bil_shares_to_buy:.6f} shares of {rssb_wtip_holding_fund} (${bil_amount_to_buy:.2f}) - holding fund")
            print(f"Bought {bil_shares_to_buy:.6f} shares of {rssb_wtip_holding_fund} for ${bil_amount_to_buy:.2f} (holding fund)")
            send_telegram_message(f"RSSB/WTIP: Bought {bil_shares_to_buy:.6f} shares of {rssb_wtip_holding_fund} (holding fund)")
            
        except Exception as e:
            error_msg = f"RSSB/WTIP: Failed to buy {rssb_wtip_holding_fund}: {str(e)}"
            print(error_msg)
            send_telegram_message(error_msg)
            return error_msg
    
    # Update Firestore with new positions (even if no trades executed, update holding fund)
    if trades_executed or bil_shares_to_buy > 0 or bil_available_for_wtip > 0:
        total_invested += investment_amount
        current_positions.update({
            "RSSB": current_positions.get("RSSB", 0) + rssb_shares_to_buy,
            "WTIP": current_positions.get("WTIP", 0) + wtip_shares_to_buy
        })
        
        # Update holding fund position (get fresh from Alpaca to be accurate)
        updated_bil_shares = get_holding_fund_shares(api, rssb_wtip_holding_fund)
        holding_fund_position[rssb_wtip_holding_fund] = updated_bil_shares
        
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
        margin_result = check_margin_conditions(api)
    
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
    telegram_msg += f"• Investment amount: ${investment_amount:.2f}\n"
    telegram_msg += f"• Total invested: ${new_total_invested:.2f}\n"
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
        spxl_data = balances.get("SPXL_SMA", {})
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
            bil_shares_to_sell = bil_value_to_use / bil_price if bil_price > 0 else 0
            
            if bil_shares_to_sell > 0:
                actual_wtip_cost = wtip_shares_needed * wtip_price
                bil_leftover = bil_value_to_use - actual_wtip_cost
                bil_rebalance_leftover += max(0, bil_leftover)
                
                rebalance_actions.append((rssb_wtip_holding_fund, bil_shares_to_sell, "sell"))
                rebalance_actions.append(("WTIP", wtip_shares_needed, "buy"))
                print(f"Using ${bil_value_to_use:.2f} from BIL holding fund to buy {wtip_shares_needed} shares of WTIP")

    # Execute rebalancing actions
    for symbol, qty, action in rebalance_actions:
        if qty > 0:
            order = submit_order(api, symbol, qty, action)
            action_verb = "Bought" if action == "buy" else "Sold"
            wait_for_order_fill(api, order["id"])
            print(f"RSSB/WTIP: {action_verb} {qty:.6f} shares of {symbol} to rebalance.")
            send_telegram_message(
                f"RSSB/WTIP: {action_verb} {qty:.6f} shares of {symbol} to rebalance."
            )
    
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


def execute_quarterly_nine_sig_signal(api, force_execute=False):
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
        
        # Step 1: Determine the Quarter's Signal Line
        previous_tqqq_balance = get_previous_quarter_tqqq_balance()
        
        # Get actual contributions made during this quarter (dynamic amounts)
        quarterly_contributions = get_quarterly_nine_sig_contributions()
        half_quarterly_contributions = quarterly_contributions * 0.5
        
        # Signal Line = Previous TQQQ Balance × 1.09 + (Half of Quarterly Contributions)
        if previous_tqqq_balance == 0 and total_portfolio > 0:
            # First quarter: Set signal line as 80% of total portfolio
            signal_line = total_portfolio * nine_sig_config["target_allocation"]["tqqq"]
            send_telegram_message("9-Sig: First quarter initialization - setting 80/20 target allocation")
        else:
            signal_line = (previous_tqqq_balance * (1 + nine_sig_config["quarterly_growth_rate"])) + half_quarterly_contributions
        
        # Step 2: Determine Action (Buy, Sell, or Hold)
        difference = current_tqqq_balance - signal_line
        tolerance = nine_sig_config["tolerance_amount"]
        
        # Step 3: Execute the Trade
        if abs(difference) < tolerance:
            action = "HOLD"
            send_telegram_message(f"9-Sig: HOLD - TQQQ ${current_tqqq_balance:.2f} within tolerance of signal line ${signal_line:.2f}")
            
        elif difference < 0:
            # BUY Signal: Need more TQQQ
            amount_to_buy = abs(difference)
            action = "BUY"
            
            # Step 4: Check for bond rebalancing on buy signals
            agg_percentage = current_agg_balance / total_portfolio if total_portfolio > 0 else 0
            if agg_percentage > nine_sig_config["bond_rebalance_threshold"]:
                # Add excess bonds to the buy amount
                target_agg_balance = total_portfolio * nine_sig_config["target_allocation"]["agg"]
                excess_agg = current_agg_balance - target_agg_balance
                amount_to_buy += excess_agg
                send_telegram_message(f"9-Sig: Rebalancing excess AGG (${excess_agg:.2f}) during buy signal")
            
            if current_agg_balance >= amount_to_buy:
                # Execute buy trade
                tqqq_price = float(get_latest_trade(api, "TQQQ"))
                agg_price = float(get_latest_trade(api, "AGG"))
                
                agg_shares_to_sell = amount_to_buy / agg_price
                tqqq_shares_to_buy = amount_to_buy / tqqq_price
                
                # Sell AGG first, then buy TQQQ
                sell_order = submit_order(api, "AGG", agg_shares_to_sell, "sell")
                wait_for_order_fill(api, sell_order["id"])
                
                buy_order = submit_order(api, "TQQQ", tqqq_shares_to_buy, "buy")
                wait_for_order_fill(api, buy_order["id"])
                
                send_telegram_message(f"9-Sig: BUY signal executed - Bought ${amount_to_buy:.2f} TQQQ (sold AGG)")
            else:
                # Insufficient AGG funds
                send_telegram_message(f"9-Sig: BUY signal but insufficient AGG (${current_agg_balance:.2f} < ${amount_to_buy:.2f}) - HOLDING existing positions")
                action = "HOLD_INSUFFICIENT_FUNDS"
                
        else:
            # SELL Signal: Too much TQQQ
            amount_to_sell = difference
            action = "SELL"
            
            # Step 5: Check for "30 Down, Stick Around" rule
            if check_spy_30_down_rule():
                ignored_count = count_ignored_sell_signals()
                
                if ignored_count < 4:
                    action = "SELL_IGNORED"
                    send_telegram_message(f"9-Sig: SELL signal IGNORED due to '30 Down, Stick Around' rule (SPY down >30%). Ignored {ignored_count + 1}/4 signals.")
                else:
                    send_telegram_message("9-Sig: Resuming normal operation after ignoring 4 sell signals")
            
            if action == "SELL":
                # Execute sell trade
                tqqq_price = float(get_latest_trade(api, "TQQQ"))
                agg_price = float(get_latest_trade(api, "AGG"))
                
                tqqq_shares_to_sell = amount_to_sell / tqqq_price
                agg_shares_to_buy = amount_to_sell / agg_price
                
                # Sell TQQQ first, then buy AGG
                sell_order = submit_order(api, "TQQQ", tqqq_shares_to_sell, "sell")
                wait_for_order_fill(api, sell_order["id"])
                
                buy_order = submit_order(api, "AGG", agg_shares_to_buy, "buy")
                wait_for_order_fill(api, buy_order["id"])
                
                send_telegram_message(f"9-Sig: SELL signal executed - Sold ${amount_to_sell:.2f} TQQQ (bought AGG)")
        
        # Save quarterly data for next calculation
        current_quarter = f"{datetime.datetime.now().year}-Q{((datetime.datetime.now().month-1)//3+1)}"
        save_nine_sig_quarterly_data(
            current_quarter,
            current_tqqq_balance,
            current_agg_balance, 
            signal_line,
            action,
            quarterly_contributions
        )
        
        # Report final allocations
        updated_positions = {p["symbol"]: float(p["market_value"]) for p in list_positions(api)}
        updated_total = updated_positions.get("TQQQ", 0) + updated_positions.get("AGG", 0)
        if updated_total > 0:
            tqqq_pct = updated_positions.get("TQQQ", 0) / updated_total
            agg_pct = updated_positions.get("AGG", 0) / updated_total
            send_telegram_message(f"9-Sig allocation: TQQQ {tqqq_pct:.1%}, AGG {agg_pct:.1%} (Target: 80/20)")
        
        return f"9-Sig quarterly signal: {action}"
    
    except Exception as e:
        error_msg = f"9-Sig quarterly signal failed: {str(e)}"
        print(error_msg)
        send_telegram_message(error_msg)
        return error_msg


# Unified function to fetch all market data and calculate all SMAs at once
def update_market_data(symbol):
    """
    Fetch fresh market data from Alpaca and calculate ALL metrics in one operation.
    ALWAYS calculates and saves: price, sma200, sma255, sma200_state, sma255_state.
    This ensures complete consistency across all symbols and makes the system extensible.
    
    Args:
        symbol: Stock symbol (e.g., "SPY", "URTH")
    
    Returns:
        dict with keys: price, sma200, sma255, sma200_state, sma255_state, timestamp
    """
    print(f"Fetching fresh market data for {symbol} from Alpaca IEX feed")
    
    # Get API credentials
    api = set_alpaca_environment(env=alpaca_environment)
    
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
    doc_ref = get_firestore_client().collection("market-data").document(doc_id)
    
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
        spy_data = get_all_market_data("SPY")
        if spy_data is None:
            spy_data = update_market_data("SPY")
        
        sma_200 = spy_data["sma200"]
        latest_price = spy_data["price"]
    else:
        return f"Unknown symbol: {symbol}"

    # If not provided by orchestrator, calculate independently
    if margin_result is None:
        margin_result = check_margin_conditions(api)
    
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
    spxl_data = balances.get(f"{symbol}_SMA", {})
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
            
            save_balance(f"{symbol}_SMA", {
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
                    save_balance(f"{symbol}_SMA", {
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


def daily_trade_sma(api, symbol):
    if not check_trading_day(mode="daily"):
        send_telegram_message(f"Market closed today. Skipping 200SMA. for {symbol}")
        return "Market closed today."

    # Use SPY as S&P 500 proxy for SPXL trading decisions
    if symbol == "SPXL":
        # Get all SPY market data at once (efficient single fetch/read)
        spy_data = get_all_market_data("SPY")
        if spy_data is None:
            spy_data = update_market_data("SPY")
        
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
            existing_data = load_balances().get(f"{symbol}_SMA", {})
            updated_sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
            holding_fund_position = existing_data.get("holding_fund_position", {})
            holding_fund_position[spxl_sma_holding_fund] = updated_sgov_shares
            
            save_balance(symbol + "_SMA", {
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
                    existing_data = load_balances().get(f"{symbol}_SMA", {})
                    updated_sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
                    holding_fund_position = existing_data.get("holding_fund_position", {})
                    holding_fund_position[spxl_sma_holding_fund] = updated_sgov_shares
                    
                    save_balance(symbol + "_SMA", {
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
            existing_data = load_balances().get(f"{symbol}_SMA", {})
            holding_fund_position = existing_data.get("holding_fund_position", {})
            updated_sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
            holding_fund_position[spxl_sma_holding_fund] = updated_sgov_shares
            
            save_balance(symbol + "_SMA", {
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
        existing_data = load_balances().get(f"{symbol}_SMA", {})
        holding_fund_position = existing_data.get("holding_fund_position", {})
        updated_sgov_shares = get_holding_fund_shares(api, spxl_sma_holding_fund)
        holding_fund_position[spxl_sma_holding_fund] = updated_sgov_shares
        
        if position:
            invested = float(position["market_value"])
            current_shares = float(position["qty"])
            
            save_balance(symbol + "_SMA", {
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
            save_balance(symbol + "_SMA", {
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
    telegram_key, chat_id = get_telegram_secrets()
    url = f"https://api.telegram.org/bot{telegram_key}/sendMessage"
    data = {"chat_id": chat_id, "text": message}
    response = requests.post(url, data=data)
    return response.status_code


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
    
    Args:
        index_symbol: Stock symbol (e.g., "SPY", "URTH")
    
    Returns:
        tuple: (current_price, all_time_high)
    """
    try:
        # Get API credentials
        api = set_alpaca_environment(env=alpaca_environment)
        
        # Fetch 5 years of data from Alpaca (max available with Basic plan)
        from datetime import datetime, timedelta
        
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
        
        response = requests.get(url, headers=get_auth_headers(api), params=params)
        response.raise_for_status()
        
        data = response.json()
        bars = data.get("bars", [])
        
        if not bars:
            raise ValueError(f"No Alpaca data returned for {index_symbol}")
        
        # Get all-time high and current close from bars
        all_time_high = max(bar['h'] for bar in bars)
        current_price = bars[-1]['c']
        
        return current_price, all_time_high
        
    except Exception as e:
        print(f"Error fetching index data for {index_symbol}: {e}")
        raise


def get_index_sma_state(index_symbol, sma_period):
    """
    Load the previous SMA state for an index from Firestore.
    
    Args:
        index_symbol: Market symbol (e.g., "^GSPC")
        sma_period: SMA period (e.g., 200, 255)
    
    Returns:
        dict with keys: state, timestamp
        Returns None if no previous state exists
    """
    try:
        # Normalize symbol for Firestore document ID
        doc_id = index_symbol.replace("^", "").replace(".", "_")
        
        doc_ref = get_firestore_client().collection("market-data").document(doc_id)
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


def save_index_sma_state(index_symbol, sma_period, state, price, sma_value):
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
    """
    try:
        # Normalize symbol for Firestore document ID
        doc_id = index_symbol.replace("^", "").replace(".", "_")
        
        doc_ref = get_firestore_client().collection("market-data").document(doc_id)
        
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


def was_last_hour_alert_sent_today(index_symbol, sma_period):
    """
    Check if a last-hour confirmation alert was already sent today.
    
    Args:
        index_symbol: Market symbol
        sma_period: SMA period
    
    Returns:
        bool: True if alert was already sent today, False otherwise
    """
    try:
        # Normalize symbol for Firestore document ID
        doc_id = index_symbol.replace("^", "").replace(".", "_")
        
        doc_ref = get_firestore_client().collection("market-data").document(doc_id)
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


def mark_last_hour_alert_sent(index_symbol, sma_period):
    """
    Mark that a last-hour confirmation alert was sent today.
    Updates the unified market-data document with the alert date.
    
    Args:
        index_symbol: Market symbol
        sma_period: SMA period
    """
    try:
        # Normalize symbol for Firestore document ID
        doc_id = index_symbol.replace("^", "").replace(".", "_")
        
        doc_ref = get_firestore_client().collection("market-data").document(doc_id)
        
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




def check_unified_index_alert(request):
    """Unified index alert function that can handle multiple indices and alert types"""
    
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
            market_data = get_all_market_data(index_symbol)
            if market_data is None:
                market_data = update_market_data(index_symbol)
            
            current_price = market_data["price"]
            
            # Get appropriate SMA based on period
            if sma_period == 255:
                sma_value = market_data["sma255"]
            elif sma_period == 200:
                sma_value = market_data["sma200"]
            else:
                # For any other period, calculate dynamically using Alpaca
                api = set_alpaca_environment(env=alpaca_environment)
                
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
            previous_state_data = get_index_sma_state(index_symbol, sma_period)
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
            already_sent_last_hour = was_last_hour_alert_sent_today(index_symbol, sma_period)
            
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
                        mark_last_hour_alert_sent(index_symbol, sma_period)
            
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
                    mark_last_hour_alert_sent(index_symbol, sma_period)
            
            # Save current state to Firestore (always update)
            save_index_sma_state(index_symbol, sma_period, current_state, current_price, sma_value)
            
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
        margin_result = check_margin_conditions(api)
    
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
        margin_result = check_margin_conditions(api)
    
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
        spy_data = get_all_market_data("SPY")
        if spy_data is None:
            spy_data = update_market_data("SPY")
        
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
    # Include holding fund value in total strategy value
    current_value_data = get_sector_momentum_value(api)
    current_value = current_value_data["total_value"] + shv_value
    total_to_allocate = current_value + investment_amount
    
    print(f"Current strategy value: ${current_value:.2f}")
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
        target_allocation_per_sector = total_to_allocate * sector_momentum_config["target_allocation_per_sector"]
        
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
        # Sector ETFs are non-fractionable (like WTIP), so we need to round to whole shares
        sector_etfs = sector_momentum_config["sector_etfs"]
        bond_etf = sector_momentum_config["bond_etf"]
        
        # Track uninvested amounts per sector and actual purchases
        uninvested_amounts = {}
        total_uninvested = 0
        actual_sector_purchases = {}  # Track actual purchase costs per sector
        
        # Check if we can use SHV funds to buy sectors (if SHV + new investment reaches threshold)
        shv_available_for_sectors = 0
        if shv_value > 0:
            # Try to use SHV funds to buy sectors if we're close to threshold
            # Calculate if SHV + investment would allow buying at least 1 share of any sector
            avg_sector_price = sum([float(get_latest_trade(api, ticker)) for ticker in top_3_sectors]) / len(top_3_sectors)
            potential_shares_with_shv = round((shv_value + investment_amount) / avg_sector_price)
            if potential_shares_with_shv >= 1:
                shv_available_for_sectors = min(shv_value, investment_amount * 0.5)  # Use up to 50% of investment amount from SHV
        
        for ticker in top_3_sectors:
            try:
                current_price = float(get_latest_trade(api, ticker))
                # Use actual shares from Alpaca, fallback to Firestore if not found
                current_shares = actual_positions.get(ticker, current_positions.get(ticker, 0))
                
                # Calculate target allocation
                # Include SHV funds if available for this ticker
                ticker_allocation_from_shv = shv_available_for_sectors / len(top_3_sectors) if shv_available_for_sectors > 0 else 0
                total_allocation_for_ticker = target_allocation_per_sector + ticker_allocation_from_shv
                
                # Calculate target shares
                target_shares = total_allocation_for_ticker / current_price
                shares_delta = target_shares - current_shares
                
                # Check if this is a non-fractionable ETF (all sector ETFs are non-fractionable)
                is_non_fractionable = ticker in sector_etfs
                
                if abs(shares_delta) > 0.01:  # Only trade if difference is meaningful
                    if shares_delta > 0:
                        # Buy more shares
                        if is_non_fractionable:
                            # Round to whole shares for non-fractionable ETFs
                            whole_shares_to_buy = round(shares_delta)
                            
                            # Calculate amount available for this ticker (from new investment, proportional)
                            # Each sector gets investment_amount / 3 for new investment
                            new_investment_per_sector = investment_amount / len(top_3_sectors)
                            amount_available_for_ticker = new_investment_per_sector + ticker_allocation_from_shv
                            
                            # Check if we can afford at least 1 whole share
                            if whole_shares_to_buy >= 1 and amount_available_for_ticker >= current_price:
                                actual_cost = whole_shares_to_buy * current_price
                                buy_order = submit_order(api, ticker, whole_shares_to_buy, "buy")
                                if not skip_order_wait:
                                    wait_for_order_fill(api, buy_order["id"])
                                trades_executed.append(f"Bought {whole_shares_to_buy:.0f} shares of {ticker} (rebalancing to 33.33%, rounded from {shares_delta:.4f})")
                                print(f"Bought {whole_shares_to_buy:.0f} shares of {ticker} (rounded from {shares_delta:.4f})")
                                
                                # Track actual purchase cost and SHV portion used
                                actual_sector_purchases[ticker] = actual_cost
                                if ticker_allocation_from_shv > 0:
                                    # SHV portion used is min of allocated and actual cost
                                    shv_portion_used = min(ticker_allocation_from_shv, actual_cost)
                                    shv_available_for_sectors -= shv_portion_used
                            else:
                                # Can't buy this sector - track uninvested amount from new investment
                                if amount_available_for_ticker < current_price:
                                    uninvested_amounts[ticker] = new_investment_per_sector
                                    total_uninvested += new_investment_per_sector
                                    print(f"Cannot buy {ticker}: need ${current_price:.2f}, have ${amount_available_for_ticker:.2f}")
                        else:
                            # SCHZ and other fractionable ETFs can use fractional shares
                            buy_order = submit_order(api, ticker, shares_delta, "buy")
                            if not skip_order_wait:
                                wait_for_order_fill(api, buy_order["id"])
                            trades_executed.append(f"Bought {shares_delta:.4f} shares of {ticker} (rebalancing to 33.33%)")
                            print(f"Bought {shares_delta:.4f} shares of {ticker}")
                    else:
                        # Sell shares - round down to whole shares (Alpaca doesn't allow fractional short sales)
                        shares_to_sell = abs(shares_delta)
                        whole_shares_to_sell = int(shares_to_sell)  # Round down to whole shares
                        if whole_shares_to_sell > 0:
                            sell_order = submit_order(api, ticker, whole_shares_to_sell, "sell")
                            if not skip_order_wait:
                                wait_for_order_fill(api, sell_order["id"])
                            trades_executed.append(f"Sold {whole_shares_to_sell:.0f} shares of {ticker} (rebalancing to 33.33%, rounded down from {shares_to_sell:.4f})")
                            print(f"Sold {whole_shares_to_sell:.0f} shares of {ticker}")
                            
                            # Note: Fractional shares that couldn't be sold remain in the position
                            # They don't become "uninvested" - they're still invested in that sector
                        else:
                            print(f"Skipping sell of {ticker}: {shares_to_sell:.4f} shares is less than 1 whole share")
                            # Can't sell - fractional shares remain in the position
                
            except Exception as e:
                error_msg = f"Failed to rebalance {ticker}: {e}"
                print(error_msg)
                send_telegram_message(f"Sector Momentum Error: {error_msg}")
                return error_msg
        
        # Handle SHV holding fund: sell if used, buy if we have uninvested amounts
        shv_shares_to_buy = 0
        shv_amount_to_buy = 0
        shv_leftover_after_sectors = 0
        
        # If we used SHV funds to buy sectors, calculate exact amount needed and sell only that
        if shv_available_for_sectors > 0 and shv_value > 0 and len(actual_sector_purchases) > 0:
            # Calculate total SHV actually used based on actual purchases
            total_shv_used = 0
            initial_shv_per_sector = shv_available_for_sectors / len(top_3_sectors)
            
            for ticker in top_3_sectors:
                if ticker in actual_sector_purchases:
                    # Sector was bought - SHV portion is min of allocated and actual cost
                    actual_cost = actual_sector_purchases[ticker]
                    shv_portion_used = min(initial_shv_per_sector, actual_cost)
                    total_shv_used += shv_portion_used
                else:
                    # Sector couldn't be bought - its SHV allocation is leftover
                    shv_leftover_after_sectors += initial_shv_per_sector
            
            # Sell only the amount of SHV we actually used (with 1% buffer)
            if total_shv_used > 0:
                shv_amount_to_sell = total_shv_used * 1.01  # 1% buffer
                shv_shares_to_sell = shv_amount_to_sell / shv_price if shv_price > 0 else 0
                
                if shv_shares_to_sell > 0 and shv_amount_to_sell <= shv_value:
                    try:
                        sell_order = submit_order(api, holding_fund_ticker, shv_shares_to_sell, "sell")
                        if not skip_order_wait:
                            wait_for_order_fill(api, sell_order["id"])
                        
                        # Calculate leftover: we sold shv_amount_to_sell but only used total_shv_used
                        actual_leftover = shv_amount_to_sell - total_shv_used
                        shv_leftover_after_sectors += max(0, actual_leftover)
                        
                        shv_shares -= shv_shares_to_sell
                        shv_value -= shv_amount_to_sell
                        trades_executed.append(f"Sold {shv_shares_to_sell:.6f} shares of {holding_fund_ticker} (${shv_amount_to_sell:.2f}) to buy sectors")
                        print(f"Sold {shv_shares_to_sell:.6f} shares of {holding_fund_ticker} (${shv_amount_to_sell:.2f}) to buy sectors")
                        if shv_leftover_after_sectors > 0:
                            print(f"Leftover from SHV sale: ${shv_leftover_after_sectors:.2f}")
                    except Exception as e:
                        error_msg = f"Sector Momentum: Failed to sell {holding_fund_ticker}: {str(e)}"
                        print(error_msg)
                        send_telegram_message(error_msg)
                        return error_msg
        
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
        
        # Buy SHV holding fund if needed
        if shv_shares_to_buy > 0:
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
                return error_msg
        
        # Update Firestore with sector positions
        # Get actual positions after trades to store accurate whole share counts
        updated_actual_positions = get_sector_momentum_positions(api)
        new_positions = {}
        for ticker in top_3_sectors:
            # Use actual position from Alpaca if available, otherwise calculate target
            if ticker in updated_actual_positions:
                new_positions[ticker] = updated_actual_positions[ticker]
            else:
                try:
                    current_price = float(get_latest_trade(api, ticker))
                    target_shares = target_allocation_per_sector / current_price
                    # Round to whole shares for sector ETFs (non-fractionable)
                    if ticker in sector_etfs:
                        new_positions[ticker] = round(target_shares)
                    else:
                        new_positions[ticker] = target_shares
                except Exception as e:
                    print(f"Error updating position for {ticker}: {e}")
        
        # Update holding fund position (get fresh from Alpaca to be accurate)
        updated_shv_shares = get_holding_fund_shares(api, holding_fund_ticker)
        holding_fund_position[holding_fund_ticker] = updated_shv_shares
        
        save_balance("sector_momentum", {
            "total_invested": total_invested + investment_amount,
            "current_positions": new_positions,
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
                
                # Update Firestore
                save_balance("sector_momentum", {
                    "total_invested": total_invested + investment_amount,
                    "current_positions": {bond_etf: schz_shares},
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
    
    # Calculate margin conditions and investment amounts ONCE
    print("=== Monthly Investment Orchestrator ===")
    print("Calculating budgets for all strategies...")
    
    margin_result = check_margin_conditions(api)
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
    return make_monthly_buys(api)


@app.route("/rebalance_hfea", methods=["POST"])
def rebalance_hfea(request):
    api = set_alpaca_environment(
        env=alpaca_environment
    )  # or 'paper' based on your needs
    return rebalance_portfolio(api)


@app.route("/monthly_buy_golden_hfea_lite", methods=["POST"])
def monthly_buy_golden_hfea_lite(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return make_monthly_buys_golden_hfea_lite(api)


@app.route("/rebalance_golden_hfea_lite", methods=["POST"])
def rebalance_golden_hfea_lite(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return rebalance_golden_hfea_lite_portfolio(api)


@app.route("/monthly_buy_rssb_wtip", methods=["POST"])
def monthly_buy_rssb_wtip(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return make_monthly_buys_rssb_wtip(api)


@app.route("/rebalance_rssb_wtip", methods=["POST"])
def rebalance_rssb_wtip(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return rebalance_rssb_wtip_portfolio(api)


@app.route("/monthly_nine_sig_contributions", methods=["POST"])
def monthly_nine_sig_contributions(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return make_monthly_nine_sig_contributions(api)


@app.route("/quarterly_nine_sig_signal", methods=["POST"])
def quarterly_nine_sig_signal(request):
    api = set_alpaca_environment(env=alpaca_environment)
    return execute_quarterly_nine_sig_signal(api)


@app.route("/monthly_buy_spxl", methods=["POST"])
def monthly_buy_spxl(request):
    api = set_alpaca_environment(
        env=alpaca_environment
    )  # or 'paper' based on your needs
    result = monthly_buying_sma(api, "SPXL")
    print(result)
    return result, 200


@app.route("/daily_trade_spxl_200sma", methods=["POST"])
def daily_trade_spxl_200sma(request):
    api = set_alpaca_environment(
        env=alpaca_environment
    )  # or 'paper' based on your needs
    result = daily_trade_sma(api, "SPXL")
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
        result = monthly_dual_momentum_strategy(api)
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
        result = monthly_sector_momentum_strategy(api)
        return jsonify({"result": result}), 200
    except Exception as e:
        error_message = f"Sector Momentum Strategy error: {str(e)}"
        print(error_message)
        send_telegram_message(error_message)
        return jsonify({"error": error_message}), 500


@app.route("/index_alert", methods=["POST"])
def index_alert(request):
    return check_unified_index_alert(request)


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


def run_local(action, env="paper", request="test", force_execute=False):
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
        return make_monthly_nine_sig_contributions(api, force_execute=force_execute)
    elif action == "quarterly_nine_sig_signal":
        return execute_quarterly_nine_sig_signal(api, force_execute=force_execute)
    elif action == "monthly_buy_spxl":
        return monthly_buying_sma(api, "SPXL", force_execute=force_execute)
    elif action == "sell_spxl_below_200sma":
        return daily_trade_sma(api, "SPXL")
    elif action == "buy_spxl_above_200sma":
        return daily_trade_sma(api, "SPXL")
    elif action == "index_alert":
        return check_unified_index_alert(request)
    elif action == "monthly_dual_momentum":
        return monthly_dual_momentum_strategy(api, force_execute=force_execute, skip_order_wait=True, env=env)
    elif action == "monthly_sector_momentum":
        return monthly_sector_momentum_strategy(api, force_execute=force_execute, skip_order_wait=True, env=env)
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
            "monthly_sector_momentum"
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
    args = parser.parse_args()

    # Run the function locally
    result = run_local(action=args.action, env=args.env, force_execute=args.force)
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
