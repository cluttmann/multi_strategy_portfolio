"""
sheets_sync.py — Google Sheets integration for the German Tax Engine.

Manages three tabs in the existing Google Sheet (never touches other tabs):
  - Raw_Activity:        All Alpaca activities (append-only, dedupe by activity_id)
  - Open_Positions_FIFO: Current open lots per symbol (full rewrite each run)
  - Tax_Summary_Yearly:  Per-year aggregates for WISO (full rewrite each run)

Uses gspread with a service account for authentication.

Usage:
    from tax.sheets_sync import SheetsSync

    sync = SheetsSync()
    existing = sync.read_raw_activities()
    sync.append_raw_activities(new_activities)
    sync.write_open_positions(lots)
    sync.write_tax_summary(summaries)
"""

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import time

from tax.config import (
    GOOGLE_SHEET_KEY, CREDENTIALS_PATH,
    TAB_RAW_ACTIVITY, TAB_OPEN_POSITIONS, TAB_TAX_SUMMARY,
    TAB_REALIZED_TRADES, TAB_DIVIDENDS_DETAIL, TAB_KAP_INV_PER_FUND, TAB_ECB_RATES,
)


# Google Sheets API scopes
_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
]

# Column headers for each tab
RAW_ACTIVITY_HEADERS = [
    "id", "activity_type", "date", "transaction_time", "symbol",
    "isin", "name", "side", "qty", "price", "net_amount",
    "per_share_amount", "description", "order_id", "order_status",
    "cum_qty", "leaves_qty", "type", "status",
    "ecb_rate", "amount_eur",
]

OPEN_POSITIONS_HEADERS = [
    "symbol", "isin", "buy_date", "qty_remaining",
    "buy_price_usd", "ecb_rate", "cost_eur_per_unit", "total_cost_eur",
]

TAX_SUMMARY_HEADERS = [
    "tax_year",
    "total_dividends_gross_eur", "total_dividends_tfs_eur",
    "total_realized_gains_eur", "total_realized_gains_tfs_eur",
    "total_realized_losses_eur", "total_realized_losses_tfs_eur",
    "vorabpauschale_eur",
    "foreign_tax_paid_eur",
    "interest_income_eur",
    "wiso_anlage_kap_zeile_7",
    "wiso_anlage_kap_zeile_12",
    "wiso_anlage_kap_zeile_51",
]

# Headers for the Realized_Trades_FIFO tab (full FIFO audit trail)
REALIZED_TRADES_HEADERS = [
    "symbol", "isin", "buy_date", "sell_date", "qty",
    "buy_price_usd", "sell_price_usd",
    "buy_fx_rate", "sell_fx_rate",
    "cost_eur", "proceeds_eur", "gain_loss_eur",
    "tfs_rate", "taxable_gain_eur",
]

# Headers for the Dividends_Detail tab (per-event dividend detail)
DIVIDENDS_DETAIL_HEADERS = [
    "date", "symbol", "isin", "name", "activity_type",
    "gross_usd", "ecb_rate", "gross_eur",
    "withholding_usd", "withholding_eur",
    "tfs_rate", "taxable_eur",
]

# Headers for the KAP_INV_Per_Fund tab (per-ISIN yearly summary for Anlage KAP-INV)
KAP_INV_PER_FUND_HEADERS = [
    "tax_year", "symbol", "isin", "name", "tfs_rate",
    "dividends_gross_eur", "dividends_tfs_eur",
    "realized_gains_eur", "realized_gains_tfs_eur",
    "realized_losses_eur", "realized_losses_tfs_eur",
    "vorabpauschale_before_tfs_eur", "vorabpauschale_after_tfs_eur",
    "withholding_tax_eur",
    "total_taxable_income_eur",
]

# Headers for the ECB_Rates_Used tab (FX rate documentation)
ECB_RATES_HEADERS = [
    "date", "eur_usd_rate", "source",
]


class SheetsSync:
    """
    Google Sheets synchronization for the German Tax Engine.

    Connects to the existing Google Sheet and manages three dedicated tabs.
    Existing tabs in the sheet are never modified.
    """

    def __init__(self):
        """Initialize gspread client and open the spreadsheet."""
        creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, _SCOPES)
        self.client = gspread.authorize(creds)
        self.spreadsheet = self.client.open_by_key(GOOGLE_SHEET_KEY)
        print(f"  Connected to Google Sheet: {self.spreadsheet.title}")

    def _get_or_create_worksheet(self, tab_name: str, headers: list[str], rows: int = 1000) -> gspread.Worksheet:
        """
        Get an existing worksheet by name, or create it with headers.

        Args:
            tab_name: Name of the tab.
            headers: List of column header strings.
            rows: Initial number of rows to allocate.

        Returns:
            gspread.Worksheet object.
        """
        try:
            ws = self.spreadsheet.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            print(f"  Creating new tab: {tab_name}")
            ws = self.spreadsheet.add_worksheet(title=tab_name, rows=rows, cols=len(headers))
            # Write headers
            ws.append_row(headers, value_input_option="USER_ENTERED")
            time.sleep(1)  # Respect rate limits
        return ws

    # =========================================================================
    # Raw_Activity Tab — Append-only, deduplicate by activity_id
    # =========================================================================

    def read_raw_activities(self) -> list[dict]:
        """
        Read all existing rows from the Raw_Activity tab.

        Returns:
            List of activity dicts (keyed by header names).
            Empty list if tab doesn't exist yet.
        """
        try:
            ws = self.spreadsheet.worksheet(TAB_RAW_ACTIVITY)
            records = ws.get_all_records()
            return records
        except gspread.exceptions.WorksheetNotFound:
            return []

    def get_last_activity_id(self) -> str | None:
        """
        Get the most recent activity_id from the Raw_Activity tab.

        The tab stores activities in chronological order (oldest first).
        The last row has the most recent activity.

        Returns:
            The latest activity_id string, or None if tab is empty/missing.
        """
        try:
            ws = self.spreadsheet.worksheet(TAB_RAW_ACTIVITY)
            all_values = ws.get_all_values()
            if len(all_values) <= 1:  # Only header or empty
                return None
            # Last row, first column (id)
            return all_values[-1][0] if all_values[-1][0] else None
        except gspread.exceptions.WorksheetNotFound:
            return None

    def append_raw_activities(self, activities: list[dict]) -> int:
        """
        Append new activity rows to Raw_Activity, deduplicating by 'id'.

        Args:
            activities: List of activity dicts with keys matching RAW_ACTIVITY_HEADERS.

        Returns:
            Number of rows actually appended.
        """
        if not activities:
            return 0

        ws = self._get_or_create_worksheet(TAB_RAW_ACTIVITY, RAW_ACTIVITY_HEADERS)

        # Read existing IDs to prevent duplicates
        existing_ids = set()
        try:
            id_column = ws.col_values(1)  # Column A = id
            existing_ids = set(id_column[1:])  # Skip header
        except Exception:
            pass

        # Filter out already-existing activities
        new_rows = []
        for act in activities:
            act_id = act.get("id", "")
            if act_id and act_id in existing_ids:
                continue
            row = [str(act.get(h, "")) for h in RAW_ACTIVITY_HEADERS]
            new_rows.append(row)

        if not new_rows:
            print("  No new activities to append.")
            return 0

        # Append in batches to respect Google Sheets rate limits
        batch_size = 50
        for i in range(0, len(new_rows), batch_size):
            batch = new_rows[i:i + batch_size]
            ws.append_rows(batch, value_input_option="USER_ENTERED")
            if i + batch_size < len(new_rows):
                time.sleep(1)  # Respect rate limits between batches

        print(f"  Appended {len(new_rows)} new activities to {TAB_RAW_ACTIVITY}")
        return len(new_rows)

    # =========================================================================
    # Open_Positions_FIFO Tab — Full rewrite each run
    # =========================================================================

    def write_open_positions(self, lots: list[dict]) -> None:
        """
        Write the current open FIFO positions to the sheet.
        Completely replaces the tab content (derived data).

        Args:
            lots: List of lot dicts from FIFOEngine.get_open_lots_as_dicts().
        """
        ws = self._get_or_create_worksheet(TAB_OPEN_POSITIONS, OPEN_POSITIONS_HEADERS)

        # Clear existing data (keep header row)
        ws.clear()
        time.sleep(0.5)

        # Write header + data
        rows = [OPEN_POSITIONS_HEADERS]
        for lot in lots:
            row = [str(lot.get(h, "")) for h in OPEN_POSITIONS_HEADERS]
            rows.append(row)

        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")

        print(f"  Wrote {len(lots)} open positions to {TAB_OPEN_POSITIONS}")

    # =========================================================================
    # Tax_Summary_Yearly Tab — Full rewrite each run
    # =========================================================================

    def write_tax_summary(self, summaries: list[dict]) -> None:
        """
        Write yearly tax summaries to the sheet.
        Completely replaces the tab content (derived data).

        Args:
            summaries: List of summary dicts from build_yearly_summary().
        """
        ws = self._get_or_create_worksheet(TAB_TAX_SUMMARY, TAX_SUMMARY_HEADERS)

        # Clear existing data (keep header row)
        ws.clear()
        time.sleep(0.5)

        # Write header + data
        rows = [TAX_SUMMARY_HEADERS]
        for summary in summaries:
            row = [str(summary.get(h, "")) for h in TAX_SUMMARY_HEADERS]
            rows.append(row)

        if rows:
            ws.append_rows(rows, value_input_option="USER_ENTERED")

        print(f"  Wrote {len(summaries)} yearly summaries to {TAB_TAX_SUMMARY}")

    # =========================================================================
    # Realized_Trades_FIFO Tab — Full rewrite each run (FIFO audit trail)
    # =========================================================================

    def write_realized_trades(self, trades: list[dict]) -> None:
        """
        Write the full FIFO-matched realized trades audit trail.
        Each row shows one lot consumed by a sell: buy date, sell date,
        qty, prices, FX rates, EUR amounts, TFS, and taxable gain.

        This is the document the Finanzamt may request as proof of your
        FIFO calculation.

        Args:
            trades: List of realized trade dicts from FIFOEngine.get_realized_trades_as_dicts().
        """
        ws = self._get_or_create_worksheet(TAB_REALIZED_TRADES, REALIZED_TRADES_HEADERS)

        # Full rewrite — derived data
        ws.clear()
        time.sleep(0.5)

        rows = [REALIZED_TRADES_HEADERS]
        for t in trades:
            row = [str(t.get(h, "")) for h in REALIZED_TRADES_HEADERS]
            rows.append(row)

        if rows:
            # Write in batches for large trade lists
            batch_size = 100
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                ws.append_rows(batch, value_input_option="USER_ENTERED")
                if i + batch_size < len(rows):
                    time.sleep(1)

        print(f"  Wrote {len(trades)} realized trades to {TAB_REALIZED_TRADES}")

    # =========================================================================
    # Dividends_Detail Tab — Full rewrite each run (per-event dividend detail)
    # =========================================================================

    def write_dividends_detail(self, dividend_rows: list[dict]) -> None:
        """
        Write per-event dividend detail showing gross, withholding, TFS
        for every dividend and withholding event.

        This maps to what WISO expects in Anlage KAP-INV per-fund reporting
        and serves as documentation for the Finanzamt.

        Args:
            dividend_rows: List of dicts with keys matching DIVIDENDS_DETAIL_HEADERS.
        """
        ws = self._get_or_create_worksheet(TAB_DIVIDENDS_DETAIL, DIVIDENDS_DETAIL_HEADERS)

        ws.clear()
        time.sleep(0.5)

        rows = [DIVIDENDS_DETAIL_HEADERS]
        for d in dividend_rows:
            row = [str(d.get(h, "")) for h in DIVIDENDS_DETAIL_HEADERS]
            rows.append(row)

        if rows:
            batch_size = 100
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                ws.append_rows(batch, value_input_option="USER_ENTERED")
                if i + batch_size < len(rows):
                    time.sleep(1)

        print(f"  Wrote {len(dividend_rows)} dividend detail rows to {TAB_DIVIDENDS_DETAIL}")

    # =========================================================================
    # KAP_INV_Per_Fund Tab — Full rewrite (per-ISIN yearly summary)
    # =========================================================================

    def write_kap_inv_per_fund(self, fund_rows: list[dict]) -> None:
        """
        Write per-ISIN yearly summary for Anlage KAP-INV.

        German tax law requires reporting investment fund income per fund
        (per ISIN). This tab provides one row per fund per year with:
        dividends, realized gains/losses, Vorabpauschale, withholding tax.

        Args:
            fund_rows: List of dicts with keys matching KAP_INV_PER_FUND_HEADERS.
        """
        ws = self._get_or_create_worksheet(TAB_KAP_INV_PER_FUND, KAP_INV_PER_FUND_HEADERS)

        ws.clear()
        time.sleep(0.5)

        rows = [KAP_INV_PER_FUND_HEADERS]
        for f in fund_rows:
            row = [str(f.get(h, "")) for h in KAP_INV_PER_FUND_HEADERS]
            rows.append(row)

        if rows:
            batch_size = 100
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                ws.append_rows(batch, value_input_option="USER_ENTERED")
                if i + batch_size < len(rows):
                    time.sleep(1)

        print(f"  Wrote {len(fund_rows)} per-fund summaries to {TAB_KAP_INV_PER_FUND}")

    # =========================================================================
    # ECB_Rates_Used Tab — Full rewrite (FX rate documentation)
    # =========================================================================

    def write_ecb_rates(self, rate_rows: list[dict]) -> None:
        """
        Write all ECB EUR/USD rates used in the calculations.

        This documents which official ECB rates were applied to each
        transaction date, serving as proof for the Finanzamt.

        Args:
            rate_rows: List of dicts with "date", "eur_usd_rate", "source".
        """
        ws = self._get_or_create_worksheet(TAB_ECB_RATES, ECB_RATES_HEADERS)

        ws.clear()
        time.sleep(0.5)

        rows = [ECB_RATES_HEADERS]
        for r in rate_rows:
            row = [str(r.get(h, "")) for h in ECB_RATES_HEADERS]
            rows.append(row)

        if rows:
            batch_size = 200
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                ws.append_rows(batch, value_input_option="USER_ENTERED")
                if i + batch_size < len(rows):
                    time.sleep(1)

        print(f"  Wrote {len(rate_rows)} ECB rate entries to {TAB_ECB_RATES}")
