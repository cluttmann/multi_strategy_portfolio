"""
ecb_rates.py — Fetch official ECB EUR/USD daily reference rates.

Uses the frankfurter.app API (free, no API key, official ECB data).
ECB publishes rates for business days only; weekends/holidays fall back
to the most recent prior business day rate.

All rates are returned as Decimal for precision.

Usage:
    from tax.ecb_rates import get_ecb_rate, get_ecb_rates_bulk

    rate = get_ecb_rate("2024-06-15")          # Decimal('1.0702')
    rates = get_ecb_rates_bulk("2024-01-01", "2024-12-31")  # {date_str: Decimal}
"""

import requests
from decimal import Decimal
from datetime import datetime, timedelta

# In-memory cache: { "YYYY-MM-DD": Decimal("1.xxxx") }
# Stores the EUR/USD rate (how many USD per 1 EUR)
_rate_cache: dict[str, Decimal] = {}

# frankfurter.app base URL
_BASE_URL = "https://api.frankfurter.app"


def get_ecb_rate(date_str: str) -> Decimal:
    """
    Get the ECB EUR/USD reference rate for a specific date.

    The rate represents how many USD you get for 1 EUR.
    To convert USD → EUR: amount_eur = amount_usd / rate

    If the date is a weekend or holiday, the API automatically returns
    the most recent prior business day rate. We also handle this with
    a fallback loop for robustness.

    Args:
        date_str: Date in "YYYY-MM-DD" format.

    Returns:
        Decimal EUR/USD rate for that date.
    """
    # Check cache first
    if date_str in _rate_cache:
        return _rate_cache[date_str]

    # Fetch from frankfurter.app
    # The API automatically falls back to the last available rate
    # for weekends/holidays when using a specific date endpoint
    url = f"{_BASE_URL}/{date_str}"
    params = {"to": "USD"}

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    rate = Decimal(str(data["rates"]["USD"]))
    actual_date = data.get("date", date_str)

    # Cache both the actual date and the requested date
    _rate_cache[actual_date] = rate
    _rate_cache[date_str] = rate

    return rate


def get_ecb_rates_bulk(start_date: str, end_date: str) -> dict[str, Decimal]:
    """
    Fetch ECB EUR/USD rates for a date range in a single API call.

    This is much more efficient than calling get_ecb_rate() for each date.
    The API returns rates only for business days; we forward-fill weekends
    and holidays with the preceding business day rate.

    Args:
        start_date: Start date "YYYY-MM-DD" (inclusive).
        end_date: End date "YYYY-MM-DD" (inclusive).

    Returns:
        Dict mapping every calendar date in the range to a Decimal rate.
    """
    url = f"{_BASE_URL}/{start_date}..{end_date}"
    params = {"to": "USD"}

    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    # Parse the business-day rates from the API response
    business_day_rates = {}
    for date_str, rate_dict in data.get("rates", {}).items():
        rate = Decimal(str(rate_dict["USD"]))
        business_day_rates[date_str] = rate
        _rate_cache[date_str] = rate

    # Forward-fill to cover every calendar day in the range
    result = {}
    current = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    last_known_rate = None

    # Get the sorted business day rates for forward-filling
    sorted_dates = sorted(business_day_rates.keys())
    if sorted_dates:
        last_known_rate = business_day_rates[sorted_dates[0]]

    while current <= end:
        ds = current.strftime("%Y-%m-%d")
        if ds in business_day_rates:
            last_known_rate = business_day_rates[ds]
        if last_known_rate is not None:
            result[ds] = last_known_rate
            _rate_cache[ds] = last_known_rate
        current += timedelta(days=1)

    return result


def usd_to_eur(amount_usd: Decimal, eur_usd_rate: Decimal) -> Decimal:
    """
    Convert a USD amount to EUR using the ECB EUR/USD reference rate.

    The ECB rate = how many USD per 1 EUR.
    So: EUR = USD / rate

    Args:
        amount_usd: Amount in USD.
        eur_usd_rate: ECB EUR/USD rate (e.g. 1.08 means 1 EUR = 1.08 USD).

    Returns:
        Amount in EUR, rounded to 2 decimal places.
    """
    if eur_usd_rate == 0:
        return Decimal("0.00")
    return (amount_usd / eur_usd_rate).quantize(Decimal("0.01"))


def preload_rates_for_dates(dates: list[str]) -> None:
    """
    Efficiently preload ECB rates for a list of dates.

    Groups dates into contiguous ranges and fetches them in bulk.
    Dates already in cache are skipped.

    Args:
        dates: List of date strings in "YYYY-MM-DD" format.
    """
    # Filter out dates already in cache
    needed = sorted(set(d for d in dates if d not in _rate_cache))
    if not needed:
        return

    # Fetch the entire range in one call (most efficient)
    start = needed[0]
    end = needed[-1]
    print(f"  Fetching ECB rates from {start} to {end}...")
    get_ecb_rates_bulk(start, end)
    print(f"  Loaded {len(_rate_cache)} rates into cache.")
