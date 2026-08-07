"""Central configuration for the quant system.

Everything here is shared across data ingestion, research, and execution.
The quant system is fully independent of the leveraged-ETF bot in main.py —
it only shares the .env credentials and (for now) the Alpaca paper account.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# --- GCP -------------------------------------------------------------------
GCP_PROJECT = "trading-436516"
BQ_DATASET = "quant"
BQ_LOCATION = "europe-west3"

T_EOD = f"{GCP_PROJECT}.{BQ_DATASET}.eod_bars"
T_SYMBOLS = f"{GCP_PROJECT}.{BQ_DATASET}.symbols"
T_MINUTE = f"{GCP_PROJECT}.{BQ_DATASET}.minute_bars"
T_NEWS = f"{GCP_PROJECT}.{BQ_DATASET}.news"
T_FEATURES = f"{GCP_PROJECT}.{BQ_DATASET}.features_daily"
T_PREDICTIONS = f"{GCP_PROJECT}.{BQ_DATASET}.predictions"
T_TRADES = f"{GCP_PROJECT}.{BQ_DATASET}.trades"

# --- API credentials ---------------------------------------------------------
# The quant desk trades its own dedicated paper account (PA3IN7QIGPSE,
# created 2026-07-11 with $100k) via ALPACA_*_QNT keys. Data endpoints work
# with any keys, so research falls back to the bot's paper keys until the
# QNT keys are in .env.
EODHD_TOKEN = os.environ["EODHD_TOKEN"]
ALPACA_KEY_PAPER = os.environ.get("ALPACA_API_KEY_QNT") or os.environ["ALPACA_API_KEY_PAPER"]
ALPACA_SECRET_PAPER = os.environ.get("ALPACA_SECRET_KEY_QNT") or os.environ["ALPACA_SECRET_KEY_PAPER"]
QNT_ACCOUNT_DEDICATED = bool(os.environ.get("ALPACA_API_KEY_QNT"))
FRED_KEY = os.environ.get("FREDKEY")

ALPACA_PAPER_BASE = "https://paper-api.alpaca.markets"
ALPACA_DATA_BASE = "https://data.alpaca.markets"

# --- Universe hygiene --------------------------------------------------------
# Exchanges kept at ingest time. Drops OTC/pink-sheet rows, which are not
# tradable on Alpaca and roughly quadruple the row count.
LISTED_EXCHANGES = {
    "NYSE",
    "NASDAQ",
    "AMEX",
    "NYSE ARCA",
    "NYSE MKT",
    "ARCA",
    "BATS",
}

# Tickers owned by the leveraged-ETF bot on the shared paper account.
# The quant system must never touch these.
BOT_TICKERS = {
    "UPRO", "TMF", "KMLM", "SPXL", "TQQQ", "AGG", "SPUU", "QLD", "EFO",
    "SSO", "USFR", "NTSD", "SAA", "EET", "UBT", "UST", "UGL", "DBC",
    "SHV", "WLDU", "GOLY", "TLT", "URTH", "SPY",
}

# Every order the quant system places carries this client_order_id prefix so
# positions/fills can be attributed on the shared paper account.
ORDER_TAG_PREFIX = "QNT"

# --- Local staging -----------------------------------------------------------
STAGING_DIR = os.path.join(os.path.dirname(__file__), "_staging")
