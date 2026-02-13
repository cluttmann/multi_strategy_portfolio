"""
tax_calc.py — German tax calculations per InvStG (Investmentsteuergesetz).

Implements:
- Dividend aggregation with gross/net/withholding tracking
- Vorabpauschale (advance lump sum) for ETFs held over year-end
- Yearly tax summary builder for WISO Steuer / Anlage KAP & KAP-INV

Usage:
    from tax.tax_calc import aggregate_dividends, calculate_vorabpauschale, build_yearly_summary
"""

from decimal import Decimal
from collections import defaultdict

from tax.config import (
    get_tfs_rate, BASISZINS, get_isin, get_name,
    ABGELTUNGSTEUER_RATE, SOLI_RATE, KIRCHENSTEUER_RATE, SPARERPAUSCHBETRAG,
)
from tax.ecb_rates import usd_to_eur
from tax.fifo import RealizedTrade


# =============================================================================
# Dividend Aggregation
# =============================================================================

def aggregate_dividends(
    activities: list[dict],
    fx_rates: dict[str, Decimal],
) -> dict[int, dict]:
    """
    Aggregate dividend income and withholding tax per calendar year.

    Processes DIV (gross dividend), DIVFT/DIVNRA (withholding tax),
    DIVCGL/DIVCGS (capital gain distributions), and DIVROC (return of capital).

    Args:
        activities: List of activity dicts from Raw_Activity.
        fx_rates: Dict of { "YYYY-MM-DD": Decimal(EUR/USD rate) }.

    Returns:
        Dict keyed by year: {
            year: {
                "gross_dividends_usd": Decimal,
                "gross_dividends_eur": Decimal,
                "withholding_tax_usd": Decimal,
                "withholding_tax_eur": Decimal,
                "tfs_adjusted_dividends_eur": Decimal,
                "capital_gains_distributions_eur": Decimal,
                "return_of_capital_eur": Decimal,
                "by_symbol": { symbol: { ... per-symbol breakdown } },
            }
        }
    """
    yearly = defaultdict(lambda: {
        "gross_dividends_usd": Decimal("0"),
        "gross_dividends_eur": Decimal("0"),
        "withholding_tax_usd": Decimal("0"),
        "withholding_tax_eur": Decimal("0"),
        "tfs_adjusted_dividends_eur": Decimal("0"),
        "capital_gains_distributions_eur": Decimal("0"),
        "return_of_capital_eur": Decimal("0"),
        "by_symbol": defaultdict(lambda: {
            "gross_eur": Decimal("0"),
            "withholding_eur": Decimal("0"),
            "tfs_adjusted_eur": Decimal("0"),
        }),
    })

    for act in activities:
        atype = act.get("activity_type", "")
        date = act.get("date", "")
        if not date or len(date) < 4:
            continue

        year = int(date[:4])
        symbol = act.get("symbol", "")
        fx_rate = fx_rates.get(date, Decimal("1"))

        # --- Gross dividends ---
        if atype == "DIV":
            # net_amount for DIV is typically the gross dividend amount
            amount_usd = abs(Decimal(act.get("net_amount", "0") or "0"))
            amount_eur = usd_to_eur(amount_usd, fx_rate)
            tfs_rate = get_tfs_rate(symbol)

            yearly[year]["gross_dividends_usd"] += amount_usd
            yearly[year]["gross_dividends_eur"] += amount_eur
            yearly[year]["tfs_adjusted_dividends_eur"] += (
                amount_eur * (1 - tfs_rate)
            ).quantize(Decimal("0.01"))
            yearly[year]["by_symbol"][symbol]["gross_eur"] += amount_eur
            yearly[year]["by_symbol"][symbol]["tfs_adjusted_eur"] += (
                amount_eur * (1 - tfs_rate)
            ).quantize(Decimal("0.01"))

        # --- Withholding tax (Foreign Tax / NRA Withheld) ---
        elif atype in ("DIVFT", "DIVNRA", "INTNRA"):
            # Withholding amounts are typically negative (debited from account)
            amount_usd = abs(Decimal(act.get("net_amount", "0") or "0"))
            amount_eur = usd_to_eur(amount_usd, fx_rate)

            yearly[year]["withholding_tax_usd"] += amount_usd
            yearly[year]["withholding_tax_eur"] += amount_eur
            yearly[year]["by_symbol"][symbol]["withholding_eur"] += amount_eur

        # --- Capital gain distributions ---
        elif atype in ("DIVCGL", "DIVCGS"):
            amount_usd = abs(Decimal(act.get("net_amount", "0") or "0"))
            amount_eur = usd_to_eur(amount_usd, fx_rate)
            yearly[year]["capital_gains_distributions_eur"] += amount_eur

        # --- Return of capital (reduces cost basis, not taxable) ---
        elif atype == "DIVROC":
            amount_usd = abs(Decimal(act.get("net_amount", "0") or "0"))
            amount_eur = usd_to_eur(amount_usd, fx_rate)
            yearly[year]["return_of_capital_eur"] += amount_eur

    return dict(yearly)


# =============================================================================
# Interest Income Aggregation
# =============================================================================

def aggregate_interest(
    activities: list[dict],
    fx_rates: dict[str, Decimal],
) -> dict[int, Decimal]:
    """
    Aggregate interest income (sweep, margin, etc.) per calendar year.

    Args:
        activities: List of activity dicts.
        fx_rates: Dict of { "YYYY-MM-DD": Decimal(EUR/USD rate) }.

    Returns:
        Dict { year: total_interest_eur }.
    """
    yearly = defaultdict(Decimal)

    for act in activities:
        atype = act.get("activity_type", "")
        if atype != "INT":
            continue

        date = act.get("date", "")
        if not date or len(date) < 4:
            continue

        year = int(date[:4])
        amount_usd = Decimal(act.get("net_amount", "0") or "0")
        fx_rate = fx_rates.get(date, Decimal("1"))
        amount_eur = usd_to_eur(abs(amount_usd), fx_rate)

        yearly[year] += amount_eur

    return dict(yearly)


# =============================================================================
# Vorabpauschale (Advance Lump Sum) — § 18 InvStG
# =============================================================================

def calculate_vorabpauschale(
    symbol: str,
    year: int,
    nav_start_eur: Decimal,
    nav_end_eur: Decimal,
    distributions_eur: Decimal,
) -> dict:
    """
    Calculate the Vorabpauschale (advance lump sum) for an ETF held over
    the turn of the year (Dec 31 → Jan 1).

    Formula per InvStG § 18:
      1. Basisertrag = NAV_start * Basiszins * 0.7
      2. If Basisertrag <= 0 → Vorabpauschale = 0
      3. Vorabpauschale = max(0, Basisertrag - Ausschüttungen)
      4. Cap: Vorabpauschale <= max(0, NAV_end - NAV_start) (actual value gain)
      5. Apply Teilfreistellung: taxable = Vorabpauschale * (1 - TFS rate)

    Note: For US distributing ETFs, if dividends paid exceed the Basisertrag,
    the Vorabpauschale is zero. It only kicks in when distributions are low
    relative to the computed base return.

    Args:
        symbol: Ticker symbol.
        year: The year for which to calculate (e.g. 2025 = held over Dec 31, 2024 → Jan 1, 2025).
        nav_start_eur: Total position value in EUR on Jan 1 of year (or first day of holding).
        nav_end_eur: Total position value in EUR on Dec 31 of year.
        distributions_eur: Total distributions (dividends) received in EUR during the year.

    Returns:
        Dict with calculation details:
        {
            "symbol", "year", "basiszins", "basisertrag_eur",
            "distributions_eur", "vorabpauschale_before_tfs_eur",
            "tfs_rate", "vorabpauschale_after_tfs_eur"
        }
    """
    basiszins = BASISZINS.get(year, Decimal("0"))
    tfs_rate = get_tfs_rate(symbol)

    # Step 1: Basisertrag (base return)
    basisertrag = (nav_start_eur * basiszins * Decimal("0.7")).quantize(Decimal("0.01"))

    # Step 2: If base return is zero or negative, no Vorabpauschale
    if basisertrag <= 0:
        vorabpauschale_raw = Decimal("0.00")
    else:
        # Step 3: Subtract actual distributions
        vorabpauschale_raw = max(Decimal("0"), basisertrag - distributions_eur)

        # Step 4: Cap at actual value gain (if any)
        actual_gain = nav_end_eur - nav_start_eur
        if actual_gain > 0:
            vorabpauschale_raw = min(vorabpauschale_raw, actual_gain)
        else:
            # No value gain → no Vorabpauschale
            vorabpauschale_raw = Decimal("0.00")

    # Step 5: Apply Teilfreistellung
    vorabpauschale_after_tfs = (
        vorabpauschale_raw * (1 - tfs_rate)
    ).quantize(Decimal("0.01"))

    return {
        "symbol": symbol,
        "isin": get_isin(symbol),
        "name": get_name(symbol),
        "year": year,
        "basiszins": str(basiszins),
        "basisertrag_eur": str(basisertrag),
        "nav_start_eur": str(nav_start_eur),
        "nav_end_eur": str(nav_end_eur),
        "distributions_eur": str(distributions_eur),
        "vorabpauschale_before_tfs_eur": str(vorabpauschale_raw),
        "tfs_rate": str(tfs_rate),
        "vorabpauschale_after_tfs_eur": str(vorabpauschale_after_tfs),
    }


# =============================================================================
# Yearly Tax Summary Builder (WISO Steuer / Anlage KAP + KAP-INV)
# =============================================================================

def build_yearly_summary(
    year: int,
    realized_trades: list[RealizedTrade],
    dividend_data: dict,
    interest_eur: Decimal,
    vorabpauschale_items: list[dict],
) -> dict:
    """
    Build a yearly tax summary row that maps to WISO Steuer fields.

    This aggregates all income types for a given tax year and produces
    the values needed for Anlage KAP and Anlage KAP-INV.

    Args:
        year: Tax year (e.g. 2024).
        realized_trades: List of RealizedTrade objects for this year.
        dividend_data: Dividend aggregation dict for this year (from aggregate_dividends).
        interest_eur: Total interest income in EUR for this year.
        vorabpauschale_items: List of Vorabpauschale dicts for this year.

    Returns:
        Dict with WISO-ready tax summary fields.
    """
    # --- Realized gains and losses from FIFO ---
    total_gains_before_tfs = Decimal("0")
    total_losses_before_tfs = Decimal("0")
    total_gains_after_tfs = Decimal("0")
    total_losses_after_tfs = Decimal("0")

    for trade in realized_trades:
        if trade.sell_date[:4] != str(year):
            continue

        if trade.gain_loss_eur >= 0:
            total_gains_before_tfs += trade.gain_loss_eur
            total_gains_after_tfs += trade.taxable_gain_eur
        else:
            total_losses_before_tfs += trade.gain_loss_eur  # Negative
            total_losses_after_tfs += trade.taxable_gain_eur  # Also negative (TFS-adjusted)

    # --- Dividends ---
    gross_div_eur = dividend_data.get("gross_dividends_eur", Decimal("0"))
    tfs_div_eur = dividend_data.get("tfs_adjusted_dividends_eur", Decimal("0"))
    withholding_eur = dividend_data.get("withholding_tax_eur", Decimal("0"))

    # --- Vorabpauschale ---
    total_vorabpauschale = Decimal("0")
    for vp in vorabpauschale_items:
        total_vorabpauschale += Decimal(vp.get("vorabpauschale_after_tfs_eur", "0"))

    # --- Estimated German Tax Liability ---
    # Net taxable income = positive income + losses (losses are negative)
    zeile_7 = (tfs_div_eur + total_gains_after_tfs + interest_eur + total_vorabpauschale)
    zeile_12 = total_losses_after_tfs  # Negative

    net_taxable = (zeile_7 + zeile_12).quantize(Decimal("0.01"))

    # Apply Sparerpauschbetrag (€1,000 for singles)
    sparerpauschbetrag_used = min(SPARERPAUSCHBETRAG, max(Decimal("0"), net_taxable))
    taxable_after_freibetrag = max(Decimal("0"), net_taxable - sparerpauschbetrag_used)

    # Abgeltungsteuer (25% flat tax)
    abgeltungsteuer = (taxable_after_freibetrag * ABGELTUNGSTEUER_RATE).quantize(Decimal("0.01"))

    # Solidaritätszuschlag (5.5% of Abgeltungsteuer)
    soli = (abgeltungsteuer * SOLI_RATE).quantize(Decimal("0.01"))

    # Kirchensteuer (0% if not church member, 9% in Berlin if member)
    kirchensteuer = (abgeltungsteuer * KIRCHENSTEUER_RATE).quantize(Decimal("0.01"))

    # Total German tax before foreign tax credit
    total_german_tax = abgeltungsteuer + soli + kirchensteuer

    # Foreign tax credit — capped at German tax on the foreign income
    # (the Finanzamt credits up to what German tax would have been)
    foreign_tax_credit = min(withholding_eur, abgeltungsteuer).quantize(Decimal("0.01"))

    # Final tax due (what you actually owe the Finanzamt)
    tax_due = max(Decimal("0"), total_german_tax - foreign_tax_credit).quantize(Decimal("0.01"))

    return {
        "tax_year": str(year),
        # Dividends
        "total_dividends_gross_eur": str(gross_div_eur.quantize(Decimal("0.01"))),
        "total_dividends_tfs_eur": str(tfs_div_eur.quantize(Decimal("0.01"))),
        # Realized gains (positive)
        "total_realized_gains_eur": str(total_gains_before_tfs.quantize(Decimal("0.01"))),
        "total_realized_gains_tfs_eur": str(total_gains_after_tfs.quantize(Decimal("0.01"))),
        # Realized losses (negative)
        "total_realized_losses_eur": str(total_losses_before_tfs.quantize(Decimal("0.01"))),
        "total_realized_losses_tfs_eur": str(total_losses_after_tfs.quantize(Decimal("0.01"))),
        # Vorabpauschale
        "vorabpauschale_eur": str(total_vorabpauschale.quantize(Decimal("0.01"))),
        # Foreign tax paid (creditable against German tax)
        "foreign_tax_paid_eur": str(withholding_eur.quantize(Decimal("0.01"))),
        # Interest income
        "interest_income_eur": str(interest_eur.quantize(Decimal("0.01"))),
        # WISO mapping
        "wiso_anlage_kap_zeile_7": str(zeile_7.quantize(Decimal("0.01"))),
        "wiso_anlage_kap_zeile_12": str(zeile_12.quantize(Decimal("0.01"))),
        "wiso_anlage_kap_zeile_51": str(withholding_eur.quantize(Decimal("0.01"))),
        # Estimated German tax liability
        "net_taxable_income_eur": str(net_taxable),
        "sparerpauschbetrag_used_eur": str(sparerpauschbetrag_used.quantize(Decimal("0.01"))),
        "taxable_after_freibetrag_eur": str(taxable_after_freibetrag.quantize(Decimal("0.01"))),
        "abgeltungsteuer_eur": str(abgeltungsteuer),
        "solidaritaetszuschlag_eur": str(soli),
        "kirchensteuer_eur": str(kirchensteuer),
        "total_german_tax_eur": str(total_german_tax.quantize(Decimal("0.01"))),
        "foreign_tax_credit_eur": str(foreign_tax_credit),
        "tax_due_eur": str(tax_due),
    }


# =============================================================================
# Dividends Detail — Per-event list for documentation
# =============================================================================

def build_dividends_detail(
    activities: list[dict],
    fx_rates: dict[str, Decimal],
) -> list[dict]:
    """
    Build a detailed per-event list of all dividend and withholding transactions.

    Each dividend event becomes one row showing: date, symbol, ISIN, name,
    activity type, gross amounts in USD and EUR, withholding amounts,
    and the TFS-adjusted taxable amount.

    This is the document the Finanzamt can request as proof of dividend income
    and foreign tax paid. It also feeds into WISO's Anlage KAP-INV per-fund input.

    Args:
        activities: All activity dicts from Raw_Activity.
        fx_rates: Dict { "YYYY-MM-DD": Decimal(EUR/USD rate) }.

    Returns:
        List of dicts, one per dividend/withholding event, sorted by date.
    """
    detail_rows = []

    # Dividend-related activity types
    dividend_types = {"DIV", "DIVFT", "DIVNRA", "INTNRA", "DIVCGL", "DIVCGS", "DIVROC", "DIVTXEX"}

    for act in activities:
        atype = act.get("activity_type", "")
        if atype not in dividend_types:
            continue

        date = act.get("date", "")
        symbol = act.get("symbol", "")
        if not date or not symbol:
            continue

        fx_rate = fx_rates.get(date, Decimal("1"))
        amount_usd = Decimal(act.get("net_amount", "0") or "0")
        amount_usd_abs = abs(amount_usd)
        amount_eur = usd_to_eur(amount_usd_abs, fx_rate)
        tfs_rate = get_tfs_rate(symbol)

        row = {
            "date": date,
            "symbol": symbol,
            "isin": get_isin(symbol),
            "name": get_name(symbol),
            "activity_type": atype,
            "gross_usd": "",
            "ecb_rate": str(fx_rate),
            "gross_eur": "",
            "withholding_usd": "",
            "withholding_eur": "",
            "tfs_rate": str(tfs_rate),
            "taxable_eur": "",
        }

        if atype == "DIV":
            # Gross dividend
            row["gross_usd"] = str(amount_usd_abs)
            row["gross_eur"] = str(amount_eur)
            taxable = (amount_eur * (1 - tfs_rate)).quantize(Decimal("0.01"))
            row["taxable_eur"] = str(taxable)

        elif atype in ("DIVFT", "DIVNRA", "INTNRA"):
            # Withholding tax (shown as positive for clarity)
            row["withholding_usd"] = str(amount_usd_abs)
            row["withholding_eur"] = str(amount_eur)

        elif atype in ("DIVCGL", "DIVCGS"):
            # Capital gain distribution — treated like dividend income
            row["gross_usd"] = str(amount_usd_abs)
            row["gross_eur"] = str(amount_eur)
            taxable = (amount_eur * (1 - tfs_rate)).quantize(Decimal("0.01"))
            row["taxable_eur"] = str(taxable)

        elif atype == "DIVROC":
            # Return of capital — note: reduces cost basis, not directly taxable
            row["gross_usd"] = str(amount_usd_abs)
            row["gross_eur"] = str(amount_eur)
            row["taxable_eur"] = "0.00"  # ROC is not taxable income

        elif atype == "DIVTXEX":
            # Tax-exempt dividend
            row["gross_usd"] = str(amount_usd_abs)
            row["gross_eur"] = str(amount_eur)
            row["taxable_eur"] = "0.00"

        detail_rows.append(row)

    # Sort by date, then symbol
    detail_rows.sort(key=lambda r: (r["date"], r["symbol"]))
    return detail_rows


# =============================================================================
# KAP-INV Per-Fund Yearly Summary — per-ISIN reporting for Anlage KAP-INV
# =============================================================================

def build_kap_inv_per_fund(
    years: list[int],
    realized_trades: list[RealizedTrade],
    activities: list[dict],
    fx_rates: dict[str, Decimal],
    vorabpauschale_items: list[dict],
) -> list[dict]:
    """
    Build a per-fund (per-ISIN) yearly summary for Anlage KAP-INV.

    German tax law requires reporting investment fund income per fund.
    For each fund and each tax year, this computes:
      - Dividends (gross and after TFS)
      - Realized gains and losses (before and after TFS)
      - Vorabpauschale (before and after TFS)
      - Withholding tax paid
      - Total taxable income

    Only funds with actual activity in a given year are included.

    Args:
        years: List of tax years to report.
        realized_trades: All realized trades from FIFOEngine.
        activities: All activity dicts from Raw_Activity.
        fx_rates: ECB rates.
        vorabpauschale_items: All Vorabpauschale dicts.

    Returns:
        List of dicts, one per fund per year, sorted by year then symbol.
    """
    # Aggregate dividends per (year, symbol)
    div_by_fund = defaultdict(lambda: defaultdict(lambda: {
        "gross_eur": Decimal("0"),
        "tfs_adjusted_eur": Decimal("0"),
        "withholding_eur": Decimal("0"),
    }))

    for act in activities:
        atype = act.get("activity_type", "")
        date = act.get("date", "")
        symbol = act.get("symbol", "")
        if not date or len(date) < 4 or not symbol:
            continue
        year = int(date[:4])
        fx_rate = fx_rates.get(date, Decimal("1"))

        if atype == "DIV":
            amount_usd = abs(Decimal(act.get("net_amount", "0") or "0"))
            amount_eur = usd_to_eur(amount_usd, fx_rate)
            tfs_rate = get_tfs_rate(symbol)
            div_by_fund[year][symbol]["gross_eur"] += amount_eur
            div_by_fund[year][symbol]["tfs_adjusted_eur"] += (
                amount_eur * (1 - tfs_rate)
            ).quantize(Decimal("0.01"))

        elif atype in ("DIVFT", "DIVNRA", "INTNRA"):
            amount_usd = abs(Decimal(act.get("net_amount", "0") or "0"))
            amount_eur = usd_to_eur(amount_usd, fx_rate)
            div_by_fund[year][symbol]["withholding_eur"] += amount_eur

    # Aggregate realized trades per (year, symbol)
    trades_by_fund = defaultdict(lambda: defaultdict(lambda: {
        "gains_eur": Decimal("0"),
        "gains_tfs_eur": Decimal("0"),
        "losses_eur": Decimal("0"),
        "losses_tfs_eur": Decimal("0"),
    }))

    for trade in realized_trades:
        year = int(trade.sell_date[:4])
        symbol = trade.symbol
        if trade.gain_loss_eur >= 0:
            trades_by_fund[year][symbol]["gains_eur"] += trade.gain_loss_eur
            trades_by_fund[year][symbol]["gains_tfs_eur"] += trade.taxable_gain_eur
        else:
            trades_by_fund[year][symbol]["losses_eur"] += trade.gain_loss_eur
            trades_by_fund[year][symbol]["losses_tfs_eur"] += trade.taxable_gain_eur

    # Index Vorabpauschale items by (year, symbol)
    vp_by_fund = defaultdict(lambda: defaultdict(lambda: {
        "before_tfs": Decimal("0"),
        "after_tfs": Decimal("0"),
    }))
    for vp in vorabpauschale_items:
        year = vp["year"]
        symbol = vp["symbol"]
        vp_by_fund[year][symbol]["before_tfs"] += Decimal(vp.get("vorabpauschale_before_tfs_eur", "0"))
        vp_by_fund[year][symbol]["after_tfs"] += Decimal(vp.get("vorabpauschale_after_tfs_eur", "0"))

    # Collect all (year, symbol) combinations with any activity
    all_fund_years = set()
    for year in years:
        for symbol in div_by_fund.get(year, {}):
            all_fund_years.add((year, symbol))
        for symbol in trades_by_fund.get(year, {}):
            all_fund_years.add((year, symbol))
        for symbol in vp_by_fund.get(year, {}):
            all_fund_years.add((year, symbol))

    # Build output rows
    result = []
    for year, symbol in sorted(all_fund_years):
        # Skip cash sweep and empty symbols
        if not symbol or symbol == "SWEEPFDIC":
            continue

        tfs_rate = get_tfs_rate(symbol)
        div = div_by_fund.get(year, {}).get(symbol, {
            "gross_eur": Decimal("0"),
            "tfs_adjusted_eur": Decimal("0"),
            "withholding_eur": Decimal("0"),
        })
        trades = trades_by_fund.get(year, {}).get(symbol, {
            "gains_eur": Decimal("0"),
            "gains_tfs_eur": Decimal("0"),
            "losses_eur": Decimal("0"),
            "losses_tfs_eur": Decimal("0"),
        })
        vp = vp_by_fund.get(year, {}).get(symbol, {
            "before_tfs": Decimal("0"),
            "after_tfs": Decimal("0"),
        })

        # Total taxable income = TFS-adjusted dividends + TFS-adjusted gains
        # + TFS-adjusted losses + Vorabpauschale after TFS
        total_taxable = (
            div["tfs_adjusted_eur"]
            + trades["gains_tfs_eur"]
            + trades["losses_tfs_eur"]  # negative
            + vp["after_tfs"]
        ).quantize(Decimal("0.01"))

        result.append({
            "tax_year": str(year),
            "symbol": symbol,
            "isin": get_isin(symbol),
            "name": get_name(symbol),
            "tfs_rate": str(tfs_rate),
            "dividends_gross_eur": str(div["gross_eur"].quantize(Decimal("0.01"))),
            "dividends_tfs_eur": str(div["tfs_adjusted_eur"].quantize(Decimal("0.01"))),
            "realized_gains_eur": str(trades["gains_eur"].quantize(Decimal("0.01"))),
            "realized_gains_tfs_eur": str(trades["gains_tfs_eur"].quantize(Decimal("0.01"))),
            "realized_losses_eur": str(trades["losses_eur"].quantize(Decimal("0.01"))),
            "realized_losses_tfs_eur": str(trades["losses_tfs_eur"].quantize(Decimal("0.01"))),
            "vorabpauschale_before_tfs_eur": str(vp["before_tfs"].quantize(Decimal("0.01"))),
            "vorabpauschale_after_tfs_eur": str(vp["after_tfs"].quantize(Decimal("0.01"))),
            "withholding_tax_eur": str(div["withholding_eur"].quantize(Decimal("0.01"))),
            "total_taxable_income_eur": str(total_taxable),
        })

    return result
