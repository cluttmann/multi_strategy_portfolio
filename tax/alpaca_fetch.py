"""
alpaca_fetch.py — Incremental activity fetching from Alpaca via REST API.

Uses direct Alpaca v2 REST API calls (GET /v2/account/activities/{type})
because alpaca-py's TradingClient does not expose account activities.

Fetches all account activities (trades, dividends, interest, etc.) and
returns them as a list of flat dicts ready for DataFrame conversion.

Supports incremental fetching: pass in the latest known activity ID
from the Google Sheet to only fetch newer activities.

Usage:
    from tax.alpaca_fetch import fetch_all_activities

    activities = fetch_all_activities(
        api_key="...",
        api_secret="...",
        last_known_id=None,   # None = full backfill
    )
"""

import requests

from tax.config import ALPACA_ACTIVITY_TYPES, ACCOUNT_START_DATE, ALPACA_BASE_URL


def _get_auth_headers(api_key: str, api_secret: str) -> dict:
    """Build Alpaca API authentication headers."""
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }


def _fetch_activities_for_type(
    api_key: str,
    api_secret: str,
    activity_type: str,
    after_date: str | None = None,
    last_known_id: str | None = None,
) -> list[dict]:
    """
    Fetch all activities of a given type from the Alpaca REST API.

    Handles pagination via page_token. Returns activities in the order
    the API returns them (newest first).

    Args:
        api_key: Alpaca API key.
        api_secret: Alpaca API secret.
        activity_type: Activity type string (e.g. "FILL", "DIV").
        after_date: ISO date string to fetch activities after (for full backfill).
        last_known_id: Stop fetching when we reach this ID (for incremental).

    Returns:
        List of raw activity dicts.
    """
    headers = _get_auth_headers(api_key, api_secret)
    url = f"{ALPACA_BASE_URL}/v2/account/activities/{activity_type}"
    all_activities = []
    page_token = None

    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        elif after_date:
            params["after"] = after_date + "T00:00:00Z"

        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        activities = response.json()

        if not activities:
            break

        for act in activities:
            # If incremental, stop when we reach the last known ID
            if last_known_id and act.get("id") == last_known_id:
                return all_activities

            all_activities.append(act)

        # Pagination: if we got a full page, there might be more
        if len(activities) < 100:
            break

        # Use the last activity's ID as page_token for the next page
        page_token = activities[-1].get("id")
        if not page_token:
            break

    return all_activities


def _normalize_activity(raw: dict) -> dict:
    """
    Normalize a raw Alpaca API activity dict into a consistent flat structure.

    Handles both TradeActivity (FILL) and NonTradeActivity objects,
    which have different fields from the API.

    Args:
        raw: Raw JSON dict from Alpaca API.

    Returns:
        Flat dict with all relevant fields; missing fields are empty strings.
    """
    result = {}

    # Common fields
    result["id"] = raw.get("id", "")
    result["activity_type"] = raw.get("activity_type", "")

    # Date handling: trade activities have transaction_time, non-trade have date
    result["transaction_time"] = raw.get("transaction_time", "")
    result["date"] = ""

    # Extract date from transaction_time (trade activities)
    if result["transaction_time"]:
        result["date"] = result["transaction_time"][:10]

    # Non-trade activities have a direct "date" field
    if raw.get("date"):
        result["date"] = str(raw["date"])[:10]

    # Trade fields (FILL)
    result["symbol"] = raw.get("symbol", "")
    result["qty"] = str(raw.get("qty", ""))
    result["price"] = str(raw.get("price", ""))
    result["side"] = raw.get("side", "")
    result["order_id"] = raw.get("order_id", "")
    result["order_status"] = raw.get("order_status", "")
    result["cum_qty"] = str(raw.get("cum_qty", ""))
    result["leaves_qty"] = str(raw.get("leaves_qty", ""))
    result["type"] = raw.get("type", "")

    # Non-trade fields (DIV, INT, etc.)
    result["net_amount"] = str(raw.get("net_amount", ""))
    result["per_share_amount"] = str(raw.get("per_share_amount", ""))
    result["description"] = raw.get("description", "")
    result["status"] = raw.get("status", "")

    return result


def fetch_all_activities(
    api_key: str,
    api_secret: str,
    last_known_id: str | None = None,
    base_url: str | None = None,
) -> list[dict]:
    """
    Fetch account activities from Alpaca, optionally incremental.

    On first run (last_known_id=None): fetches ALL activities since
    ACCOUNT_START_DATE to do a full historical backfill.

    On subsequent runs: fetches only activities newer than last_known_id.

    The Alpaca API returns activities in reverse chronological order
    (newest first). We reverse them to chronological order before returning.

    Args:
        api_key: Alpaca API key.
        api_secret: Alpaca API secret.
        last_known_id: The most recent activity ID already in the sheet.
                       None triggers a full backfill.
        base_url: Optional Alpaca base URL override (not used, kept for compat).

    Returns:
        List of activity dicts in chronological order (oldest first).
    """
    all_activities = []

    for activity_type in ALPACA_ACTIVITY_TYPES:
        print(f"  Fetching activity type: {activity_type}")

        try:
            raw_activities = _fetch_activities_for_type(
                api_key=api_key,
                api_secret=api_secret,
                activity_type=activity_type,
                after_date=ACCOUNT_START_DATE if not last_known_id else None,
                last_known_id=last_known_id,
            )

            # Normalize each raw activity into our standard format
            for raw in raw_activities:
                normalized = _normalize_activity(raw)
                # Ensure activity_type is set (API sometimes omits it)
                if not normalized["activity_type"]:
                    normalized["activity_type"] = activity_type
                all_activities.append(normalized)

            if raw_activities:
                print(f"    → Fetched {len(raw_activities)} {activity_type} activities")

        except requests.exceptions.HTTPError as e:
            # Some activity types may not be available or may return 404
            if e.response is not None and e.response.status_code == 404:
                pass  # Silently skip unsupported activity types
            else:
                print(f"    ⚠ HTTP error fetching {activity_type}: {e}")
        except Exception as e:
            print(f"    ⚠ Error fetching {activity_type}: {e}")
            continue

    # Sort all activities chronologically (oldest first)
    def sort_key(act):
        return act.get("transaction_time") or act.get("date") or ""

    all_activities.sort(key=sort_key)

    print(f"\n  Total new activities fetched: {len(all_activities)}")
    return all_activities
