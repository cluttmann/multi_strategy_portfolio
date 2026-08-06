"""
parqet_sync.py — Sync Alpaca activities into the Parqet import tabs.

Writes two tabs in the Google Sheet, in the exact format Parqet expects:

  parqet_import_equity:
    ID | date(DD.MM.YYYY) | isin | type | price | shares | fee | tax | currency
                              Buy/Sell/Dividend             0,00  0,00   USD

  parquet_import_cash:
    date(DD.MM.YYYY) | amount | tax | fee | type | holding | currency
                                            TransferIn / TransferOut / Interest

Both import tabs are *cleared and rewritten* each run, holding only the
delta still to upload to Parqet.  Each tab is backed by a master log that
is append-only:
  - equity master log → 'parqet_import_prep' (already in the sheet)
  - cash   master log → 'parquet_cash_prep'  (auto-created on first run)
Max-ID / max-date lookups read from the master log so state is never lost
when the import tab is cleared.

Fees and taxes (see LOOKBACK_DAYS / CONVERSION_FEE_MARKER below):
  - the ~1.5% FX "conversion fee" on a deposit goes in the TransferIn row's `fee`
  - per-trade REG/TAF/CAT fees go in the equity row's `fee`
  - DIVNRA withholding tax goes in the Dividend row's `tax`

Modes:

  default            — find the latest row already logged in each tab, re-scan from
                       LOOKBACK_DAYS before it, and emit whatever isn't already
                       logged (dedup by ID for equity, by row fingerprint for cash).
                       The lookback exists because Alpaca back-dates some activities.
  --since YYYY-MM-DD — override: re-scan everything since this date for both tabs.
                       Still deduped, so it will not re-emit already-logged rows.
  --equity-only / --cash-only — restrict to one tab.

Usage:
    python3 parqet_sync.py
    python3 parqet_sync.py --since 2026-01-01
    python3 parqet_sync.py --cash-only
    python3 parqet_sync.py --env paper        # paper account (debug only)
"""

import argparse
import os
import sys
import time
from datetime import datetime, timedelta

import alpaca_trade_api as tradeapi
import gspread
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
GOOGLE_SHEET_KEY = "1KoC18yB994pGYG-Ft7Y85mN_iOfMuSt03u6kKf2eMUY"
CREDENTIALS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "trading-436516-c4449aa3edcc.json",
)
EQUITY_TAB = "parqet_import_equity"          # delta-to-upload, cleared each run
EQUITY_MASTER_TAB = "parqet_import_prep"     # append-only master log
EQUITY_HEADERS = ["ID", "date", "isin", "type", "price", "shares", "fee", "tax", "currency"]

CASH_TAB = "parquet_import_cash"             # delta-to-upload, cleared each run
CASH_MASTER_TAB = "parquet_cash_prep"        # append-only master log (created if missing)
CASH_HEADERS = ["date", "amount", "tax", "fee", "type", "holding", "currency"]
HOLDING_ID = "hld_6807dff65442f34b2be71adf"

# Alpaca posts some activities (notably DIV) with a `date` *earlier* than the day
# they land in the API. A pure max-ID/max-date watermark loses those forever: once
# the watermark moves past their date they are never fetched again. Three June-2026
# dividends (SHV/TLT/AGG) were lost exactly this way. So always re-scan a trailing
# window and rely on ID/fingerprint dedup instead of the watermark alone.
LOOKBACK_DAYS = 75

# FEE activities come in two flavours that belong in different places:
#   "Funding Wallet incoming alpaca conversion fee" — the ~1.5% FX fee on a deposit,
#       belongs in the `fee` column of that day's TransferIn row.
#   REG / TAF / CAT regulatory fees — per-trade, belong in the `fee` column of the
#       equity row. Previously these were summed onto a same-day transfer row and
#       silently dropped entirely on days with no transfer.
CONVERSION_FEE_MARKER = "conversion fee"

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
]

# Mirrors the lookup in alpaca_statement.ipynb. Keep in sync when new tickers
# enter the strategy. Symbols without an ISIN are skipped (Parqet needs ISIN).
SYMBOL_TO_ISIN = {
    "KMLM": "US5007676522",
    "UPRO": "US74347X8645",
    "TQQQ": "US74347X8314",
    "SPXL": "US25459W8626",
    "TMF":  "US25459W5408",
    "EFO":  "US74347X5005",
    "EET":  "US74347X3026",
    "AGG":  "US4642872265",
    "SSO":  "US74347R1077",
    "ZROZ": "US72201R8824",
    "GLD":  "US78463V1070",
    "XLC":  "US81369Y8527",
    "XLY":  "US81369Y4070",
    "XLK":  "US81369Y8030",
    "WTIP": "US97717Y3523",
    "SPUU": "US25459Y1652",
    "BND":  "US9219378356",
    "XLV":  "US81369Y2090",
    "XLE":  "US81369Y5069",
    "XLB":  "US81369Y1001",
    "XLRE": "US81369Y8600",
    "XLU":  "US81369Y8865",
    "XLI":  "US81369Y7040",
    "XLF":  "US81369Y6059",
    "SCHZ": "US8085248396",
    "XLP":  "US81369Y3080",
    "RSSB": "US88636J2042",
    "SGOV": "US46436E7186",
    "SHV":  "US4642886794",
    "BIL":  "US78468R6633",
    "ROM":  "US74347R6936",
    "UYG":  "US74347X6334",
    "DIG":  "US74347G7051",
    "RXL":  "US74347R7355",
    "UXI":  "US74347R7272",
    "UGE":  "US74347R7686",
    "UCC":  "US74347R7504",
    "UPW":  "US74347R6852",
    "UYM":  "US74347R7769",
    "URE":  "US74347X6250",
    "LTL":  "US74347R2638",
    "DBC":  "US46138B1035",
    "GOLY": "US86280R8786",
    "QLD":  "US74347R2067",
    "SAA":  "US74347R8189",
    "TLT":  "US4642874329",
    "WLDU": "US88340C4877",
    "USFR": "US97717Y5270",
    "UBT":  "US74347R1721",
    "UST":  "US74347R1804",
    "UGL":  "US74347W6012",
    "NTSD": "US97717Y2467",
}


# -----------------------------------------------------------------------------
# Alpaca fetch helpers
# -----------------------------------------------------------------------------
def fetch_activities(api, activity_type: str, after_iso: str, page_size: int = 100):
    """Page through every activity of the given type after a date.

    Alpaca's activities endpoint returns a plain list and paginates by passing the
    *id of the last row of the previous page* as `page_token`. The previous version
    read `chunk.next_page_token`, which never exists on a list, so it always broke
    after the first page and silently capped every fetch at 100 rows — truncating
    any historical `--since` backfill without a word.
    """
    out, seen, token = [], set(), None
    while True:
        chunk = list(api.get_activities(
            activity_types=activity_type,
            after=after_iso,
            direction="asc",
            page_size=page_size,
            page_token=token,
        ))
        if not chunk:
            break
        fresh = [a for a in chunk if getattr(a, "id", None) not in seen]
        out.extend(fresh)
        seen.update(a.id for a in chunk if getattr(a, "id", None))
        if len(chunk) < page_size:
            break
        token = chunk[-1].id
    return out


def classify_fees(fees):
    """Split FEE activities into (conversion-fee-by-date, trade-fee-by-date).

    Conversion fees are the ~1.5% FX charge on a deposit and belong on the
    TransferIn row. REG/TAF/CAT fees are per-trade and belong on the equity row.
    """
    conv, trade = {}, {}
    for f in fees:
        raw = f._raw
        d = (raw.get("date") or (raw.get("transaction_time") or "")[:10])[:10]
        if not d:
            continue
        try:
            amt = abs(float(raw.get("net_amount") or 0))
        except (TypeError, ValueError):
            continue
        bucket = conv if CONVERSION_FEE_MARKER in (raw.get("description") or "") else trade
        bucket[d] = bucket.get(d, 0.0) + amt
    return conv, trade


def build_withholding_map(divnras):
    """(date, symbol) -> withholding tax withheld, from DIVNRA activities."""
    tax = {}
    for a in divnras:
        raw = a._raw
        d = (raw.get("date") or (raw.get("transaction_time") or "")[:10])[:10]
        sym = (raw.get("symbol") or "").strip()
        if not (d and sym):
            continue
        try:
            amt = -float(raw.get("net_amount") or 0)
        except (TypeError, ValueError):
            continue
        tax[(d, sym)] = tax.get((d, sym), 0.0) + amt
    return tax


def build_symbol_lookup(activities):
    """Some FILL activities omit `symbol` but carry an order_id — build a map."""
    lookup = {}
    for act in activities:
        raw = act._raw
        sym = (raw.get("symbol") or "").strip()
        if not sym:
            continue
        for key in (raw.get("order_id"), raw.get("id")):
            if key:
                lookup[key] = sym
    return lookup


def resolve_symbol(raw: dict, lookup: dict) -> str:
    sym = (raw.get("symbol") or "").strip()
    if sym:
        return sym
    for key in (raw.get("order_id"), raw.get("id")):
        if key and key in lookup:
            return lookup[key]
    return ""


# -----------------------------------------------------------------------------
# Formatting
# -----------------------------------------------------------------------------
def fmt_date_de(date_or_ts: str) -> str:
    """ISO timestamp (`2026-05-13T13:49:41.293Z`) or `YYYY-MM-DD` → `DD.MM.YYYY`."""
    if "T" in date_or_ts:
        d = datetime.fromisoformat(date_or_ts.replace("Z", "+00:00")).date()
    else:
        d = datetime.strptime(date_or_ts[:10], "%Y-%m-%d").date()
    return d.strftime("%d.%m.%Y")


def fmt_decimal_dot(x, max_dec: int = 6) -> str:
    """Format float with up to `max_dec` decimals, trimming trailing zeros."""
    s = f"{float(x):.{max_dec}f}".rstrip("0").rstrip(".")
    return s or "0"


# -----------------------------------------------------------------------------
# Row builders — return None to skip the activity
# -----------------------------------------------------------------------------
def fmt_money_de(x) -> str:
    """2-decimal money with a German decimal comma, matching the '0,00' convention."""
    return f"{float(x):.2f}".replace(".", ",")


def fill_to_row(raw: dict, lookup: dict, fee: float = 0.0):
    side = (raw.get("side") or "").lower()
    if side not in ("buy", "sell"):
        return None
    sym = resolve_symbol(raw, lookup)
    isin = SYMBOL_TO_ISIN.get(sym, "")
    if not isin:
        return None
    price = (raw.get("price") or "").strip()
    qty = (raw.get("qty") or "").strip()
    ts = raw.get("transaction_time", "")
    if not (price and qty and ts):
        return None
    return [
        raw.get("id", ""),
        fmt_date_de(ts),
        isin,
        "Buy" if side == "buy" else "Sell",
        price,
        qty,
        fmt_money_de(fee),
        "0,00",
        "USD",
    ]


def div_to_row(raw: dict, lookup: dict, tax: float = 0.0):
    sym = resolve_symbol(raw, lookup)
    isin = SYMBOL_TO_ISIN.get(sym, "")
    if not isin:
        return None
    per_share = (raw.get("per_share_amount") or "").strip()
    net = (raw.get("net_amount") or "").strip()
    date_s = raw.get("date") or (raw.get("transaction_time") or "")[:10]
    if not (per_share and net and date_s):
        return None
    try:
        ps_f = float(per_share)
        if ps_f <= 0:
            return None
        shares = float(net) / ps_f
    except (ValueError, ZeroDivisionError):
        return None
    if shares <= 0:
        return None
    return [
        raw.get("id", ""),
        fmt_date_de(date_s),
        isin,
        "Dividend",
        per_share,
        fmt_decimal_dot(shares),
        "0,00",
        fmt_money_de(tax),
        "USD",
    ]


def _norm_amount(s: str) -> str:
    """'465.04' / '465,04' / '465.0400' -> a single canonical form for dedup."""
    try:
        return f"{float(str(s).strip().replace(',', '.')):.2f}"
    except (TypeError, ValueError):
        return str(s).strip()


def _iso_to_date(s: str):
    """Parse Alpaca's `date` (YYYY-MM-DD) or `transaction_time` (ISO) → date."""
    if not s:
        return None
    try:
        if "T" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# -----------------------------------------------------------------------------
# Tab writers
# -----------------------------------------------------------------------------
def _write_tab(ws, headers, rows, label):
    print(f"[{label}] Clearing tab '{ws.title}' and writing {len(rows)} rows…")
    ws.clear()
    time.sleep(0.5)
    ws.append_row(headers, value_input_option="USER_ENTERED")
    if rows:
        batch = 100
        for i in range(0, len(rows), batch):
            ws.append_rows(rows[i:i + batch], value_input_option="USER_ENTERED")
            if i + batch < len(rows):
                time.sleep(1)
    print(f"[{label}] Done. Tab '{ws.title}' now holds {len(rows)} row(s).")


# -----------------------------------------------------------------------------
# Equity sync (Buy / Sell / Dividend)
# -----------------------------------------------------------------------------
def _max_id_in_col_a(ws):
    """Return the lexically largest non-empty value in column A (excl. header)."""
    col = ws.col_values(1)[1:]
    ids = [v.strip() for v in col if v.strip()]
    return max(ids) if ids else None


def sync_equity(api, gc, since_override):
    ss = gc.open_by_key(GOOGLE_SHEET_KEY)
    ws_import = ss.worksheet(EQUITY_TAB)
    ws_master = ss.worksheet(EQUITY_MASTER_TAB)

    # Dedup is by the *full set* of IDs already logged, not by a max-ID cutoff, so a
    # re-scanned window can't re-emit anything already uploaded to Parqet.
    existing_ids = set(v.strip() for v in ws_master.col_values(1)[1:] if v.strip())
    existing_ids |= set(v.strip() for v in ws_import.col_values(1)[1:] if v.strip())

    if since_override:
        since_date = since_override
        print(f"[equity] Mode: --since={since_date}")
    else:
        candidates = [m for m in (_max_id_in_col_a(ws_import), _max_id_in_col_a(ws_master)) if m]
        if not candidates:
            sys.exit(f"[equity] Both '{EQUITY_TAB}' and '{EQUITY_MASTER_TAB}' are empty; pass --since YYYY-MM-DD.")
        max_id = max(candidates)
        try:
            watermark = datetime.strptime(max_id[:8], "%Y%m%d").date()
        except ValueError:
            sys.exit(f"[equity] Couldn't parse date prefix from ID {max_id!r}")
        since_date = watermark - timedelta(days=LOOKBACK_DAYS)
        print(f"[equity] Mode: max-id ({max_id} → watermark {watermark}, "
              f"re-scanning from {since_date} with a {LOOKBACK_DAYS}d lookback)")

    after_iso = since_date.strftime("%Y-%m-%dT00:00:00Z")
    print(f"[equity] Fetching Alpaca activities after {after_iso}…")

    fills = fetch_activities(api, "FILL", after_iso)
    divs = fetch_activities(api, "DIV", after_iso)
    divnras = fetch_activities(api, "DIVNRA", after_iso)
    fees = fetch_activities(api, "FEE", after_iso)
    print(f"[equity]   FILL: {len(fills)}  DIV: {len(divs)}  "
          f"DIVNRA: {len(divnras)}  FEE: {len(fees)}")

    all_acts = list(fills) + list(divs)
    lookup = build_symbol_lookup(all_acts)
    withholding = build_withholding_map(divnras)
    _, trade_fees = classify_fees(fees)

    # Per-trade REG/TAF/CAT fees are reported as a daily aggregate ("...for proceed of
    # 3 trades on <date>"), so they can't be split per fill. Put the day's total on
    # that day's largest sell (else largest buy) — total preserved, no invented precision.
    fee_for_id = {}
    by_day = {}
    for act in fills:
        raw = act._raw
        d = (raw.get("transaction_time") or "")[:10]
        by_day.setdefault(d, []).append(raw)
    for day, raws in by_day.items():
        if day not in trade_fees:
            continue
        eligible = [r for r in raws if SYMBOL_TO_ISIN.get(resolve_symbol(r, lookup), "")]
        if not eligible:
            continue
        sells = [r for r in eligible if (r.get("side") or "").lower() == "sell"]

        def notional(r):
            try:
                return float(r.get("qty") or 0) * float(r.get("price") or 0)
            except (TypeError, ValueError):
                return 0.0

        fee_for_id[max(sells or eligible, key=notional).get("id", "")] = trade_fees[day]

    rows = []
    skipped_dup = skipped_no_isin = skipped_other = 0
    unmapped_symbols = set()
    tax_applied = 0.0

    for act in all_acts:
        raw = act._raw
        aid = raw.get("id", "")
        if aid in existing_ids:
            skipped_dup += 1
            continue
        atype = raw.get("activity_type", "")
        if atype == "FILL":
            row = fill_to_row(raw, lookup, fee=fee_for_id.get(aid, 0.0))
        elif atype == "DIV":
            key = ((raw.get("date") or (raw.get("transaction_time") or "")[:10])[:10],
                   (raw.get("symbol") or "").strip())
            tax = withholding.get(key, 0.0)
            row = div_to_row(raw, lookup, tax=tax)
            if row is not None and tax:
                withholding[key] = 0.0  # one dividend row per (date, symbol) carries it
                tax_applied += tax
        else:
            row = None
        if row is None:
            sym = resolve_symbol(raw, lookup)
            if sym and sym not in SYMBOL_TO_ISIN:
                unmapped_symbols.add(sym)
                skipped_no_isin += 1
            else:
                skipped_other += 1
            continue
        rows.append(row)

    rows.sort(key=lambda r: r[0], reverse=True)  # newest-first by ID

    print(f"[equity] Rows to write: {len(rows)}")
    if skipped_dup:
        print(f"[equity]   skipped (already logged): {skipped_dup}")
    if skipped_no_isin:
        print(f"[equity]   skipped (unmapped ISIN): {skipped_no_isin} — symbols: {sorted(unmapped_symbols)}")
    if skipped_other:
        print(f"[equity]   skipped (malformed/incomplete): {skipped_other}")
    fee_total = sum(fee_for_id.get(r[0], 0.0) for r in rows)
    if fee_total or tax_applied:
        print(f"[equity]   attached ${fee_total:.2f} trade fees, "
              f"${tax_applied:.2f} dividend withholding tax")

    _write_tab(ws_import, EQUITY_HEADERS, rows, "equity")

    if rows:
        print(f"[equity] Appending {len(rows)} row(s) to master log '{EQUITY_MASTER_TAB}'…")
        ws_master.append_rows(rows, value_input_option="USER_ENTERED")
        print("[equity] Master log updated.")


# -----------------------------------------------------------------------------
# Cash sync (TransferIn / TransferOut / Interest)
# -----------------------------------------------------------------------------
def _get_or_create_worksheet(ss, tab_name, headers, rows=1000):
    try:
        ws = ss.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        print(f"  Creating new tab '{tab_name}'.")
        ws = ss.add_worksheet(title=tab_name, rows=rows, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        time.sleep(0.5)
    return ws


def _max_date_in_col_a(ws):
    """Return the latest DD.MM.YYYY date in column A (excl. header), or None."""
    col = ws.col_values(1)[1:]
    dates = []
    for v in col:
        v = v.strip()
        if not v:
            continue
        try:
            dates.append(datetime.strptime(v, "%d.%m.%Y").date())
        except ValueError:
            pass
    return max(dates) if dates else None


def sync_cash(api, gc, since_override):
    ss = gc.open_by_key(GOOGLE_SHEET_KEY)
    ws_import = ss.worksheet(CASH_TAB)
    ws_master = _get_or_create_worksheet(ss, CASH_MASTER_TAB, CASH_HEADERS)

    # One-time bootstrap: if the master log is empty (first run, fresh tab),
    # migrate whatever's currently in the import tab into it so the existing
    # state seeds the max-date lookup.
    if _max_date_in_col_a(ws_master) is None:
        existing_rows = [r for r in ws_import.get_all_values()[1:] if any(c.strip() for c in r)]
        if existing_rows:
            print(f"[cash] Bootstrapping '{CASH_MASTER_TAB}' with {len(existing_rows)} row(s) from '{CASH_TAB}'.")
            ws_master.append_rows(existing_rows, value_input_option="USER_ENTERED")
            time.sleep(0.5)

    # Cash activities carry no stable ID, so dedup is by fingerprint. Deliberately
    # key on (date, amount, type) only — NOT the whole row. `fee` is a derived
    # attribute, so including it means any change to how fees are computed makes
    # already-uploaded rows look new and re-emits them as Parqet duplicates. That
    # bit us when per-trade fees stopped being folded into transfer rows (a 6,99 ->
    # 6,98 change on one deposit was enough).
    def _fp(row):
        return (row[0].strip(), _norm_amount(row[1]), row[4].strip())

    existing_fingerprints = {
        _fp(r) for r in ws_master.get_all_values()[1:]
        if len(r) >= 5 and any(c.strip() for c in r)
    }
    existing_fingerprints |= {
        _fp(r) for r in ws_import.get_all_values()[1:]
        if len(r) >= 5 and any(c.strip() for c in r)
    }

    if since_override:
        since_date = since_override
        print(f"[cash] Mode: --since={since_date}")
    else:
        candidates = [d for d in (_max_date_in_col_a(ws_import), _max_date_in_col_a(ws_master)) if d]
        if not candidates:
            sys.exit(f"[cash] Both '{CASH_TAB}' and '{CASH_MASTER_TAB}' are empty; pass --since YYYY-MM-DD.")
        max_date = max(candidates)
        since_date = max_date - timedelta(days=LOOKBACK_DAYS)
        print(f"[cash] Mode: max-date in sheets = {max_date}; "
              f"re-scanning from {since_date} with a {LOOKBACK_DAYS}d lookback")

    after_iso = since_date.strftime("%Y-%m-%dT00:00:00Z")
    print(f"[cash] Fetching Alpaca cash activities after {after_iso}…")

    csds = fetch_activities(api, "CSD", after_iso)
    csws = fetch_activities(api, "CSW", after_iso)
    ints = fetch_activities(api, "INT", after_iso)
    intnras = fetch_activities(api, "INTNRA", after_iso)
    jnlcs = fetch_activities(api, "JNLC", after_iso)
    fees = fetch_activities(api, "FEE", after_iso)
    print(f"[cash]   CSD: {len(csds)}  CSW: {len(csws)}  INT: {len(ints)}  "
          f"INTNRA: {len(intnras)}  JNLC: {len(jnlcs)}  FEE: {len(fees)}")

    # Only the FX conversion fee belongs on a transfer row. Per-trade REG/TAF/CAT fees
    # are handled by sync_equity; previously they were folded in here on transfer days
    # and silently dropped on every other day.
    conv_fees, trade_fees = classify_fees(fees)
    if trade_fees:
        print(f"[cash]   {len(trade_fees)} day(s) of per-trade fees "
              f"(${sum(trade_fees.values()):.2f}) belong to the equity tab, not here")

    rows = []
    skipped_other = 0
    for act in list(csds) + list(csws) + list(ints) + list(intnras) + list(jnlcs):
        raw = act._raw
        atype = raw.get("activity_type", "")
        date_iso = raw.get("date") or (raw.get("transaction_time") or "")[:10]
        act_date = _iso_to_date(date_iso)
        if act_date is None:
            skipped_other += 1
            continue
        try:
            net = float(raw.get("net_amount") or 0)
        except ValueError:
            skipped_other += 1
            continue

        if atype == "CSD":
            type_str = "TransferIn"
            amount = net
            fee_amt = conv_fees.get(date_iso[:10], 0.0)
        elif atype == "CSW":
            type_str = "TransferOut"
            amount = abs(net)
            fee_amt = conv_fees.get(date_iso[:10], 0.0)
        elif atype in ("INT", "INTNRA", "JNLC"):
            # INT is margin interest (negative). JNLC covers withholding refunds and
            # margin-interest credits — cash movements with no better Parqet type;
            # a negative Interest amount is explicitly supported by parqet.com/basic_cash.csv.
            type_str = "Interest"
            amount = net
            fee_amt = 0.0
        else:
            skipped_other += 1
            continue

        rows.append([
            act_date.strftime("%d.%m.%Y"),
            fmt_decimal_dot(amount, max_dec=2),
            "0",
            fmt_decimal_dot(fee_amt, max_dec=2),
            type_str,
            HOLDING_ID,
            "USD",
        ])

    before = len(rows)
    rows = [r for r in rows if _fp(r) not in existing_fingerprints]
    skipped_dup = before - len(rows)

    # Newest-first to match the equity tab convention.
    rows.sort(key=lambda r: datetime.strptime(r[0], "%d.%m.%Y").date(), reverse=True)

    print(f"[cash] Rows to write: {len(rows)}")
    if skipped_dup:
        print(f"[cash]   skipped (already logged): {skipped_dup}")
    if skipped_other:
        print(f"[cash]   skipped (malformed/unknown subtype): {skipped_other}")

    _write_tab(ws_import, CASH_HEADERS, rows, "cash")

    if rows:
        print(f"[cash] Appending {len(rows)} row(s) to master log '{CASH_MASTER_TAB}'…")
        ws_master.append_rows(rows, value_input_option="USER_ENTERED")
        print("[cash] Master log updated.")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Sync Alpaca activities → Parqet import tabs in Google Sheets."
    )
    parser.add_argument(
        "--since",
        help="YYYY-MM-DD — emit activities since this date for both tabs "
             "(overrides the default max-ID/max-date in-sheet mode).",
    )
    parser.add_argument("--equity-only", action="store_true",
                        help="Only sync parqet_import_equity (skip cash).")
    parser.add_argument("--cash-only", action="store_true",
                        help="Only sync parquet_import_cash (skip equity).")
    parser.add_argument(
        "--env", choices=["live", "paper"], default="live",
        help="Which Alpaca account (default: live).",
    )
    args = parser.parse_args()
    if args.equity_only and args.cash_only:
        sys.exit("--equity-only and --cash-only are mutually exclusive.")

    load_dotenv()
    if args.env == "live":
        api_key = os.getenv("ALPACA_API_KEY_LIVE")
        api_secret = os.getenv("ALPACA_SECRET_KEY_LIVE")
        base_url = "https://api.alpaca.markets"
    else:
        api_key = os.getenv("ALPACA_API_KEY_PAPER")
        api_secret = os.getenv("ALPACA_SECRET_KEY_PAPER")
        base_url = "https://paper-api.alpaca.markets"
    if not (api_key and api_secret):
        sys.exit(f"Missing Alpaca {args.env} credentials in .env")

    creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, SCOPES)
    gc = gspread.authorize(creds)
    api = tradeapi.REST(api_key, api_secret, base_url)

    since_override = (
        datetime.strptime(args.since, "%Y-%m-%d").date() if args.since else None
    )

    if not args.cash_only:
        sync_equity(api, gc, since_override)
        print()
    if not args.equity_only:
        sync_cash(api, gc, since_override)


if __name__ == "__main__":
    main()
