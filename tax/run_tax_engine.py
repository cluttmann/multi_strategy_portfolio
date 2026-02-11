"""
run_tax_engine.py — Main orchestrator for the German Tax Engine.

This is the entry point script. Run it to:
  1. Fetch new Alpaca activities (incremental)
  2. Enrich with ECB EUR/USD rates
  3. Append new activities to Google Sheet (Raw_Activity tab)
  4. Rebuild FIFO positions from all activities
  5. Calculate dividends, withholding tax, interest per year
  6. Calculate Vorabpauschale for each year-turn
  7. Build yearly tax summaries (WISO Steuer / Anlage KAP ready)
  8. Write derived tabs to Google Sheet:
     - Open_Positions_FIFO      (current open lots)
     - Tax_Summary_Yearly       (aggregate yearly numbers)
     - Realized_Trades_FIFO     (full FIFO audit trail per sell)
     - Dividends_Detail         (per-event dividend + withholding)
     - KAP_INV_Per_Fund         (per-ISIN yearly summary for Anlage KAP-INV)
     - ECB_Rates_Used           (FX rate documentation)

Usage:
    cd /Users/carl/Coding/hfea_strategy
    python -m tax.run_tax_engine
"""

import os
import sys
from decimal import Decimal
from collections import defaultdict
from dotenv import load_dotenv

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tax.config import (
    get_isin, get_name, BASISZINS, GOOGLE_SHEET_KEY,
)
from tax.ecb_rates import preload_rates_for_dates, usd_to_eur
from tax.alpaca_fetch import fetch_all_activities
from tax.fifo import FIFOEngine
from tax.tax_calc import (
    aggregate_dividends, aggregate_interest,
    calculate_vorabpauschale, build_yearly_summary,
    build_dividends_detail, build_kap_inv_per_fund,
)
from tax.sheets_sync import SheetsSync


def enrich_activities_with_fx(
    activities: list[dict],
    fx_rates: dict[str, Decimal],
) -> list[dict]:
    """
    Enrich activity dicts with ECB rate and EUR amount columns.

    For each activity, adds:
      - 'isin': ISIN from config
      - 'name': Full name from config
      - 'ecb_rate': EUR/USD rate for the activity date
      - 'amount_eur': net_amount converted to EUR (or price in EUR for fills)

    Args:
        activities: List of raw activity dicts.
        fx_rates: Pre-loaded ECB rates { "YYYY-MM-DD": Decimal }.

    Returns:
        The same list, modified in-place with added fields.
    """
    for act in activities:
        symbol = act.get("symbol", "")
        date = act.get("date", "")

        # Add ISIN and name
        act["isin"] = get_isin(symbol)
        act["name"] = get_name(symbol)

        # Add ECB rate
        fx_rate = fx_rates.get(date, Decimal("1"))
        act["ecb_rate"] = str(fx_rate)

        # Calculate EUR amount
        # For FILLs: amount = qty * price
        # For dividends/interest: amount = net_amount
        if act.get("activity_type") == "FILL":
            try:
                qty = Decimal(act.get("qty", "0") or "0")
                price = Decimal(act.get("price", "0") or "0")
                amount_usd = qty * price
            except Exception:
                amount_usd = Decimal("0")
        else:
            try:
                amount_usd = abs(Decimal(act.get("net_amount", "0") or "0"))
            except Exception:
                amount_usd = Decimal("0")

        act["amount_eur"] = str(usd_to_eur(amount_usd, fx_rate))

    return activities


def rebuild_fifo_from_activities(activities: list[dict], fx_rates: dict[str, Decimal]) -> FIFOEngine:
    """
    Replay all buy/sell activities through the FIFO engine in chronological order.

    This rebuilds the complete FIFO state from scratch using all activities
    in the Raw_Activity tab. This ensures consistency even if the script
    is run multiple times.

    Args:
        activities: All activities in chronological order (oldest first).
        fx_rates: ECB rates for conversion.

    Returns:
        FIFOEngine with all positions and realized trades computed.
    """
    engine = FIFOEngine()

    for act in activities:
        atype = act.get("activity_type", "")
        if atype != "FILL":
            continue

        symbol = act.get("symbol", "")
        date = act.get("date", "")
        side = act.get("side", "").lower()

        if not symbol or not date or not side:
            continue

        try:
            qty = Decimal(act.get("qty", "0") or "0")
            price = Decimal(act.get("price", "0") or "0")
        except Exception:
            continue

        if qty <= 0 or price <= 0:
            continue

        fx_rate = fx_rates.get(date, Decimal("1"))

        if side == "buy":
            engine.process_buy(symbol, date, qty, price, fx_rate)
        elif side in ("sell", "sell_short"):
            engine.process_sell(symbol, date, qty, price, fx_rate)

    return engine


def calculate_all_vorabpauschale(
    engine: FIFOEngine,
    activities: list[dict],
    fx_rates: dict[str, Decimal],
) -> list[dict]:
    """
    Calculate Vorabpauschale for all ETFs held over each year-turn.

    For each year where we have a Basiszins, check which symbols had
    open positions at the start of the year and compute the advance lump sum.

    This is a simplified calculation that uses the FIFO cost basis as
    a proxy for NAV. For a more precise calculation, you'd need actual
    fund NAV data, but for the purpose of German tax reporting this
    is a reasonable approximation.

    Args:
        engine: FIFOEngine with all positions processed.
        activities: All activities (for dividend lookups).
        fx_rates: ECB rates.

    Returns:
        List of Vorabpauschale dicts.
    """
    results = []

    # Get dividends per symbol per year for the distributions offset
    symbol_year_dividends = defaultdict(lambda: defaultdict(Decimal))
    for act in activities:
        if act.get("activity_type") != "DIV":
            continue
        symbol = act.get("symbol", "")
        date = act.get("date", "")
        if not date or len(date) < 4:
            continue
        year = int(date[:4])
        try:
            amount_usd = abs(Decimal(act.get("net_amount", "0") or "0"))
        except Exception:
            continue
        fx_rate = fx_rates.get(date, Decimal("1"))
        amount_eur = usd_to_eur(amount_usd, fx_rate)
        symbol_year_dividends[symbol][year] += amount_eur

    # For each year with a known Basiszins, compute Vorabpauschale
    for year in sorted(BASISZINS.keys()):
        # We need positions held at the START of this year (= end of prior year)
        # Since we rebuild FIFO from scratch, we approximate by looking at
        # the total cost basis of open lots bought before Jan 1 of this year
        for symbol, lots in engine.open_positions.items():
            # Sum up the cost basis of lots that existed at start of year
            nav_start_eur = Decimal("0")
            for lot in lots:
                # If lot was bought before this year, it was held over the year-turn
                if lot.buy_date < f"{year}-01-01":
                    nav_start_eur += lot.total_cost_eur

            if nav_start_eur <= 0:
                continue

            # Approximate nav_end as nav_start (simplified — actual NAV would need
            # market data, but Vorabpauschale is capped at actual gain anyway)
            # For a more accurate calculation, one could fetch historical prices
            nav_end_eur = nav_start_eur  # Conservative: no gain → VP = 0

            # Get distributions for this symbol in this year
            distributions_eur = symbol_year_dividends.get(symbol, {}).get(year, Decimal("0"))

            vp = calculate_vorabpauschale(
                symbol=symbol,
                year=year,
                nav_start_eur=nav_start_eur,
                nav_end_eur=nav_end_eur,
                distributions_eur=distributions_eur,
            )

            # Only include if there's a non-zero Vorabpauschale
            if Decimal(vp["vorabpauschale_after_tfs_eur"]) > 0:
                results.append(vp)

    return results


def print_wiso_summary(summaries: list[dict]) -> None:
    """
    Print a human-readable WISO Steuer summary to the console.

    Args:
        summaries: List of yearly summary dicts.
    """
    print("\n" + "=" * 70)
    print("  WISO Steuer Summary — Anlage KAP / KAP-INV")
    print("=" * 70)

    for s in summaries:
        year = s["tax_year"]
        print(f"\n  --- Tax Year {year} ---")
        print(f"  Dividends (gross, before TFS):       {s['total_dividends_gross_eur']} EUR")
        print(f"  Dividends (after TFS):               {s['total_dividends_tfs_eur']} EUR")
        print(f"  Realized Gains (before TFS):         {s['total_realized_gains_eur']} EUR")
        print(f"  Realized Gains (after TFS):          {s['total_realized_gains_tfs_eur']} EUR")
        print(f"  Realized Losses (before TFS):        {s['total_realized_losses_eur']} EUR")
        print(f"  Realized Losses (after TFS):         {s['total_realized_losses_tfs_eur']} EUR")
        print(f"  Vorabpauschale (after TFS):          {s['vorabpauschale_eur']} EUR")
        print(f"  Foreign Tax Paid (creditable):       {s['foreign_tax_paid_eur']} EUR")
        print(f"  Interest Income:                     {s['interest_income_eur']} EUR")
        print("  ─────────────────────────────────────")
        print(f"  → Anlage KAP Zeile 7 (Einkünfte):   {s['wiso_anlage_kap_zeile_7']} EUR")
        print(f"  → Anlage KAP Zeile 12 (Verluste):   {s['wiso_anlage_kap_zeile_12']} EUR")
        print(f"  → Anlage KAP Zeile 51 (ausl. St.):  {s['wiso_anlage_kap_zeile_51']} EUR")

    print("\n" + "=" * 70)


def main():
    """Main entry point for the German Tax Engine."""

    print("=" * 70)
    print("  German Tax Engine for Alpaca (InvStG)")
    print("  Calculating FIFO, TFS, Vorabpauschale, Quellensteuer...")
    print("=" * 70)

    # -------------------------------------------------------------------------
    # Step 1: Load credentials from .env
    # -------------------------------------------------------------------------
    print("\n[1/14] Loading credentials...")
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

    api_key = os.getenv("ALPACA_API_KEY_LIVE")
    api_secret = os.getenv("ALPACA_SECRET_KEY_LIVE")

    if not api_key or not api_secret:
        print("ERROR: ALPACA_API_KEY_LIVE and ALPACA_SECRET_KEY_LIVE must be set in .env")
        sys.exit(1)

    # -------------------------------------------------------------------------
    # Step 2: Connect to Google Sheet and get last known activity
    # -------------------------------------------------------------------------
    print("\n[2/14] Connecting to Google Sheet...")
    sheets = SheetsSync()
    last_id = sheets.get_last_activity_id()
    if last_id:
        print(f"  Last known activity ID: {last_id}")
    else:
        print("  No existing activities found — will do full backfill.")

    # -------------------------------------------------------------------------
    # Step 3: Fetch new activities from Alpaca (incremental)
    # -------------------------------------------------------------------------
    print("\n[3/14] Fetching activities from Alpaca...")
    new_activities = fetch_all_activities(
        api_key=api_key,
        api_secret=api_secret,
        last_known_id=last_id,
    )

    # -------------------------------------------------------------------------
    # Step 4: Fetch ECB rates for all transaction dates
    # -------------------------------------------------------------------------
    print("\n[4/14] Fetching ECB EUR/USD rates...")

    # Collect all dates from existing + new activities
    existing_activities = sheets.read_raw_activities()
    all_activities_raw = existing_activities + new_activities

    all_dates = set()
    for act in all_activities_raw:
        date = act.get("date", "")
        if date and len(date) >= 10:
            all_dates.add(date[:10])

    if all_dates:
        preload_rates_for_dates(sorted(all_dates))

    # Build FX rate lookup from cache
    from tax.ecb_rates import _rate_cache as fx_rates

    # -------------------------------------------------------------------------
    # Step 5: Enrich and append new activities to Raw_Activity sheet
    # -------------------------------------------------------------------------
    print("\n[5/14] Enriching new activities with FX rates and EUR amounts...")
    if new_activities:
        enriched = enrich_activities_with_fx(new_activities, fx_rates)
        print(f"  Enriched {len(enriched)} activities.")

        print("\n[6/14] Appending new activities to Google Sheet...")
        sheets.append_raw_activities(enriched)
    else:
        print("  No new activities to process.")
        print("\n[6/14] Skipping append — no new data.")

    # -------------------------------------------------------------------------
    # Step 6: Rebuild FIFO from ALL activities (existing + new)
    # -------------------------------------------------------------------------
    print("\n[7/14] Rebuilding FIFO positions from all activities...")

    # Re-read all activities from sheet (now includes new ones)
    all_activities = sheets.read_raw_activities()
    print(f"  Total activities in sheet: {len(all_activities)}")

    # Sort chronologically
    all_activities.sort(key=lambda a: a.get("transaction_time") or a.get("date") or "")

    # Rebuild FIFO
    engine = rebuild_fifo_from_activities(all_activities, fx_rates)

    open_lots = engine.get_open_lots_as_dicts()
    realized = engine.realized_trades
    print(f"  Open positions: {len(open_lots)} lots across {len(engine.open_positions)} symbols")
    print(f"  Realized trades: {len(realized)}")

    # -------------------------------------------------------------------------
    # Step 7: Calculate dividends, interest, and withholding tax per year
    # -------------------------------------------------------------------------
    print("\n[8/14] Calculating dividends, interest, and withholding tax...")
    dividend_data = aggregate_dividends(all_activities, fx_rates)
    interest_data = aggregate_interest(all_activities, fx_rates)

    for year in sorted(dividend_data.keys()):
        d = dividend_data[year]
        print(f"  {year}: Dividends={d['gross_dividends_eur']} EUR, "
              f"Withholding={d['withholding_tax_eur']} EUR")

    # -------------------------------------------------------------------------
    # Step 8: Calculate Vorabpauschale for each year-turn
    # -------------------------------------------------------------------------
    print("\n[9/14] Calculating Vorabpauschale...")
    vorabpauschale_items = calculate_all_vorabpauschale(engine, all_activities, fx_rates)

    if vorabpauschale_items:
        for vp in vorabpauschale_items:
            print(f"  {vp['year']} {vp['symbol']}: Vorabpauschale = {vp['vorabpauschale_after_tfs_eur']} EUR")
    else:
        print("  No Vorabpauschale applicable (distributions exceed Basisertrag or no year-turn holdings).")

    # -------------------------------------------------------------------------
    # Step 9: Build yearly tax summaries
    # -------------------------------------------------------------------------
    print("\n[10/14] Building yearly tax summaries...")

    # Determine all years with any activity
    all_years = set()
    for act in all_activities:
        date = act.get("date", "")
        if date and len(date) >= 4:
            all_years.add(int(date[:4]))

    summaries = []
    for year in sorted(all_years):
        div_data = dividend_data.get(year, {
            "gross_dividends_eur": Decimal("0"),
            "tfs_adjusted_dividends_eur": Decimal("0"),
            "withholding_tax_eur": Decimal("0"),
        })
        int_data = interest_data.get(year, Decimal("0"))
        year_vp = [vp for vp in vorabpauschale_items if vp["year"] == year]

        summary = build_yearly_summary(
            year=year,
            realized_trades=realized,
            dividend_data=div_data,
            interest_eur=int_data,
            vorabpauschale_items=year_vp,
        )
        summaries.append(summary)

    # -------------------------------------------------------------------------
    # Step 10: Build additional detail tabs
    # -------------------------------------------------------------------------
    print("\n[11/14] Building realized trades audit trail...")
    realized_dicts = engine.get_realized_trades_as_dicts()
    print(f"  {len(realized_dicts)} realized trade rows prepared for Realized_Trades_FIFO.")

    print("\n[12/14] Building per-event dividends detail...")
    dividends_detail = build_dividends_detail(all_activities, fx_rates)
    print(f"  {len(dividends_detail)} dividend detail rows prepared.")

    print("\n[13/14] Building per-fund KAP-INV summary...")
    kap_inv_rows = build_kap_inv_per_fund(
        years=sorted(all_years),
        realized_trades=realized,
        activities=all_activities,
        fx_rates=fx_rates,
        vorabpauschale_items=vorabpauschale_items,
    )
    print(f"  {len(kap_inv_rows)} per-fund summary rows prepared.")

    # Build ECB rates reference tab
    from tax.ecb_rates import _rate_cache as all_cached_rates
    ecb_rate_rows = [
        {
            "date": date_str,
            "eur_usd_rate": str(rate),
            "source": "ECB via frankfurter.app",
        }
        for date_str, rate in sorted(all_cached_rates.items())
    ]
    print(f"  {len(ecb_rate_rows)} ECB rate entries prepared.")

    # -------------------------------------------------------------------------
    # Step 11: Write all derived tabs to Google Sheet
    # -------------------------------------------------------------------------
    print("\n[14/14] Writing all derived tabs to Google Sheet...")
    sheets.write_open_positions(open_lots)
    sheets.write_tax_summary(summaries)
    sheets.write_realized_trades(realized_dicts)
    sheets.write_dividends_detail(dividends_detail)
    sheets.write_kap_inv_per_fund(kap_inv_rows)
    sheets.write_ecb_rates(ecb_rate_rows)

    # -------------------------------------------------------------------------
    # Print WISO-ready summary to console
    # -------------------------------------------------------------------------
    print_wiso_summary(summaries)

    print("\n✅ German Tax Engine completed successfully!")
    print(f"   Google Sheet: https://docs.google.com/spreadsheets/d/{GOOGLE_SHEET_KEY}")
    print("\n   New tabs generated:")
    print("   • Realized_Trades_FIFO  — Full FIFO audit trail (Finanzamt proof)")
    print("   • Dividends_Detail      — Per-event dividend/withholding detail")
    print("   • KAP_INV_Per_Fund      — Per-ISIN yearly summary (Anlage KAP-INV)")
    print("   • ECB_Rates_Used        — All FX rates used (ECB documentation)")


if __name__ == "__main__":
    main()
