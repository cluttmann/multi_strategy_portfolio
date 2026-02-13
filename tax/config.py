"""
config.py — Static configuration for the German Tax Engine.

Contains:
- ISIN and Name mappings per ticker symbol
- Teilfreistellung (TFS) classification per ticker
- Basiszins rates (Bundesbank) for Vorabpauschale
- Google Sheet and credential paths
- Alpaca API configuration
"""

from decimal import Decimal
import os

# =============================================================================
# Google Sheets Configuration
# =============================================================================
GOOGLE_SHEET_KEY = "1VScGv0pMgNQLGCloQnyxCahJJiYM_7khUNF-Em-M68w"
CREDENTIALS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "trading-436516-c4449aa3edcc.json",
)

# Tab names in Google Sheet (new tabs — existing tabs are never touched)
TAB_RAW_ACTIVITY = "Raw_Activity"
TAB_OPEN_POSITIONS = "Open_Positions_FIFO"
TAB_TAX_SUMMARY = "Tax_Summary_Yearly"
TAB_REALIZED_TRADES = "Realized_Trades_FIFO"
TAB_DIVIDENDS_DETAIL = "Dividends_Detail"
TAB_KAP_INV_PER_FUND = "KAP_INV_Per_Fund"
TAB_ECB_RATES = "ECB_Rates_Used"

# =============================================================================
# Alpaca API Configuration
# =============================================================================
ALPACA_BASE_URL = "https://api.alpaca.markets"

# Activity types to fetch from Alpaca for tax purposes
ALPACA_ACTIVITY_TYPES = [
    "FILL",      # Order fills (buys and sells)
    "DIV",       # Dividends (gross)
    "DIVFT",     # Dividend — Foreign Tax Withheld
    "DIVNRA",    # Dividend — NRA Withheld
    "DIVCGL",    # Dividend — Capital Gain Long Term
    "DIVCGS",    # Dividend — Capital Gain Short Term
    "DIVROC",    # Dividend — Return of Capital
    "DIVTXEX",   # Dividend — Tax Exempt
    "CSD",       # Cash deposit
    "CSW",       # Cash withdrawal
    "INT",       # Interest (credit/margin/sweep)
    "INTNRA",    # Interest — NRA Withheld
    "JNLC",      # Journal entry (cash)
    "JNLS",      # Journal entry (stock)
    "FEE",       # Fees
    "TRANS",     # Cash transactions
    "MA",        # Merger/Acquisition
    "SC",        # Symbol change
    "REORG",     # Reorganization
]

# =============================================================================
# ISIN Mapping (Symbol → ISIN)
# =============================================================================
SYMBOL_TO_ISIN = {
    # Core HFEA / Strategy ETFs
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
    # Sector Select SPDR ETFs
    "XLC":  "US81369Y8527",
    "XLY":  "US81369Y4070",
    "XLK":  "US81369Y8030",
    "XLV":  "US81369Y2090",
    "XLE":  "US81369Y5069",
    "XLB":  "US81369Y1001",
    "XLRE": "US81369Y8600",
    "XLU":  "US81369Y8865",
    "XLI":  "US81369Y7040",
    "XLF":  "US81369Y6059",
    "XLP":  "US81369Y3080",
    # Other ETFs
    "WTIP": "US97717Y3523",
    "SPUU": "US25459Y1652",
    "BND":  "US9219378356",
    "SCHZ": "US8085248396",
    "RSSB": "US88636J2042",
    "SHV":  "US4642886794",
    "BIL":  "US78468R6633",
    # ProShares Ultra Sector ETFs (2x leveraged)
    "ROM":  "US74347R6936",   # Ultra Technology
    "UYG":  "US74347X6334",   # Ultra Financials
    "DIG":  "US74347G7051",   # Ultra Energy
    "RXL":  "US74347R7355",   # Ultra Health Care
    "UXI":  "US74347R7272",   # Ultra Industrials
    "UGE":  "US74347R7686",   # Ultra Consumer Staples
    "UCC":  "US74347R7504",   # Ultra Consumer Discretionary
    "UPW":  "US74347R6852",   # Ultra Utilities
    "UYM":  "US74347R7769",   # Ultra Materials
    "URE":  "US74347X6250",   # Ultra Real Estate
    "LTL":  "US74347R2638",   # Ultra Communication Services
    # Cash sweep (no ISIN)
    "SWEEPFDIC": "",
}

# =============================================================================
# Name Mapping (Symbol → Full Name)
# =============================================================================
SYMBOL_TO_NAME = {
    # Core HFEA / Strategy ETFs
    "KMLM": "KFA Mount Lucas Managed Futures Index Strategy ETF",
    "UPRO": "ProShares UltraPro S&P 500",
    "TQQQ": "ProShares UltraPro QQQ",
    "SPXL": "Direxion Daily S&P 500 Bull 3X Shares",
    "TMF":  "Direxion Daily 20+ Year Treasury Bull 3X Shares",
    "EFO":  "ProShares Ultra MSCI EAFE",
    "EET":  "ProShares Ultra MSCI Emerging Markets",
    "AGG":  "iShares Core U.S. Aggregate Bond ETF",
    "SSO":  "ProShares Ultra S&P 500",
    "ZROZ": "PIMCO 25+ Year Zero Coupon U.S. Treasury Index ETF",
    "GLD":  "SPDR Gold Shares",
    # Sector Select SPDR ETFs
    "XLC":  "Communication Services Select Sector SPDR Fund",
    "XLY":  "Consumer Discretionary Select Sector SPDR Fund",
    "XLK":  "Technology Select Sector SPDR Fund",
    "XLV":  "Health Care Select Sector SPDR Fund",
    "XLE":  "Energy Select Sector SPDR Fund",
    "XLB":  "Materials Select Sector SPDR Fund",
    "XLRE": "Real Estate Select Sector SPDR Fund",
    "XLU":  "Utilities Select Sector SPDR Fund",
    "XLI":  "Industrial Select Sector SPDR Fund",
    "XLF":  "Financial Select Sector SPDR Fund",
    "XLP":  "Consumer Staples Select Sector SPDR Fund",
    # Other ETFs
    "WTIP": "WisdomTree Inflation Plus Fund",
    "SPUU": "Direxion Daily S&P 500 Bull 2X Shares",
    "BND":  "Vanguard Total Bond Market ETF",
    "SCHZ": "Schwab US Aggregate Bond ETF",
    "RSSB": "Return Stacked Global Stocks & Bonds ETF",
    "SHV":  "iShares 0-1 Year Treasury Bond ETF",
    "BIL":  "SPDR Bloomberg 1-3 Month T-Bill ETF",
    # ProShares Ultra Sector ETFs (2x leveraged)
    "ROM":  "ProShares Ultra Technology",
    "UYG":  "ProShares Ultra Financials",
    "DIG":  "ProShares Ultra Energy",
    "RXL":  "ProShares Ultra Health Care",
    "UXI":  "ProShares Ultra Industrials",
    "UGE":  "ProShares Ultra Consumer Staples",
    "UCC":  "ProShares Ultra Consumer Discretionary",
    "UPW":  "ProShares Ultra Utilities",
    "UYM":  "ProShares Ultra Materials",
    "URE":  "ProShares Ultra Real Estate",
    "LTL":  "ProShares Ultra Communication Services",
    # Cash sweep
    "SWEEPFDIC": "Sweep FDIC",
}

# =============================================================================
# Teilfreistellung (TFS) Classification per Symbol
#
# Per InvStG (Investmentsteuergesetz):
#   - Aktienfonds (equity funds, >51% equities): 30% partial exemption
#   - Mischfonds (mixed funds, >25% equities):   15% partial exemption
#   - Other investment funds / bond funds:         0% partial exemption
#   - Commodity ETCs (e.g. GLD) are NOT funds under InvStG → 0%
#
# Leveraged equity ETFs (e.g. UPRO 3x, SSO 2x) still qualify as
# Aktienfonds because their underlying is >51% equities.
# =============================================================================
SYMBOL_TFS_RATE = {
    # Equity ETFs — 30% Teilfreistellung (including leveraged equity ETFs)
    "UPRO": Decimal("0.30"),
    "TQQQ": Decimal("0.30"),
    "SPXL": Decimal("0.30"),
    "SSO":  Decimal("0.30"),
    "SPUU": Decimal("0.30"),
    "EFO":  Decimal("0.30"),
    "EET":  Decimal("0.30"),
    "RSSB": Decimal("0.30"),
    # Sector Select SPDR (unleveraged equity)
    "XLC":  Decimal("0.30"),
    "XLY":  Decimal("0.30"),
    "XLK":  Decimal("0.30"),
    "XLV":  Decimal("0.30"),
    "XLE":  Decimal("0.30"),
    "XLB":  Decimal("0.30"),
    "XLRE": Decimal("0.30"),
    "XLU":  Decimal("0.30"),
    "XLI":  Decimal("0.30"),
    "XLF":  Decimal("0.30"),
    "XLP":  Decimal("0.30"),
    # ProShares Ultra Sector (2x leveraged equity)
    "ROM":  Decimal("0.30"),
    "UYG":  Decimal("0.30"),
    "DIG":  Decimal("0.30"),
    "RXL":  Decimal("0.30"),
    "UXI":  Decimal("0.30"),
    "UGE":  Decimal("0.30"),
    "UCC":  Decimal("0.30"),
    "UPW":  Decimal("0.30"),
    "UYM":  Decimal("0.30"),
    "URE":  Decimal("0.30"),
    "LTL":  Decimal("0.30"),

    # Bond ETFs — 0% Teilfreistellung
    "TMF":  Decimal("0.00"),
    "AGG":  Decimal("0.00"),
    "ZROZ": Decimal("0.00"),
    "BND":  Decimal("0.00"),
    "SCHZ": Decimal("0.00"),
    "SHV":  Decimal("0.00"),
    "BIL":  Decimal("0.00"),

    # Commodity / Managed Futures / Other — 0% Teilfreistellung
    # GLD is an ETC, not an investment fund under InvStG
    "GLD":  Decimal("0.00"),
    "KMLM": Decimal("0.00"),
    "WTIP": Decimal("0.00"),

    # Cash sweep — not applicable
    "SWEEPFDIC": Decimal("0.00"),
}

# Default TFS rate for unknown symbols (conservative: 0%)
DEFAULT_TFS_RATE = Decimal("0.00")


def get_tfs_rate(symbol: str) -> Decimal:
    """Return the Teilfreistellung rate for a given ticker symbol."""
    return SYMBOL_TFS_RATE.get(symbol, DEFAULT_TFS_RATE)


def get_isin(symbol: str) -> str:
    """Return the ISIN for a given ticker symbol, or empty string if unknown."""
    return SYMBOL_TO_ISIN.get(symbol, "")


def get_name(symbol: str) -> str:
    """Return the full name for a given ticker symbol, or the symbol itself if unknown."""
    return SYMBOL_TO_NAME.get(symbol, symbol)


# =============================================================================
# Basiszins (Bundesbank) for Vorabpauschale Calculation
#
# Published annually by the Deutsche Bundesbank.
# The Basiszins for year N is used to calculate the Vorabpauschale
# that accrues on Jan 1 of year N (for holdings held over Dec 31 → Jan 1).
#
# Formula: Basisertrag = NAV_start * Basiszins * 0.7
# =============================================================================
BASISZINS = {
    2024: Decimal("0.0229"),   # 2.29% — published Jan 2024
    2025: Decimal("0.0253"),   # 2.53% — published Jan 2025
    # Add future years as the Bundesbank publishes them
}

# =============================================================================
# US Withholding Tax Rate (W-8BEN treaty rate)
# =============================================================================
US_WITHHOLDING_RATE = Decimal("0.15")   # 15% for US dividends under treaty

# =============================================================================
# German Tax Rates for Kapitalerträge (capital income)
# =============================================================================
ABGELTUNGSTEUER_RATE = Decimal("0.25")      # 25% flat tax on capital income
SOLI_RATE = Decimal("0.055")                # 5.5% Solidaritätszuschlag on the Abgeltungsteuer
# Kirchensteuer: 9% in Berlin (8% in Bayern/Baden-Württemberg)
# Set to 0 if not a church member
KIRCHENSTEUER_RATE = Decimal("0.00")        # Set to 0.09 for Berlin church members
SPARERPAUSCHBETRAG = Decimal("0")            # Set to 0 — used at other brokers already

# =============================================================================
# Account Start Date (for initial full backfill)
# =============================================================================
ACCOUNT_START_DATE = "2024-01-01"
