"""Exotic high-CAGR-candidate sleeves — honest first-pass backtests.

    python3 -m quant.research.exotic_sleeves --all

E1  Vol carry: short-vol exposure (long SVXY / short VXX) gated by VIX
    term-structure contango + regime filters. The classic high-CAGR trade;
    the question is always the left tail (Feb-2018, Mar-2020).
E2  Crypto trend: long-only time-series momentum on majors, vol-targeted,
    25bp/side taker costs. Alpaca crypto history starts 2021 (one full bear).
E3  3x-ETF overnight: hold leveraged sector ETFs close→open only. The panel
    killed single-stock ONX (costs eat it); on 3x ETFs gross/cost ratio
    triples, so it gets one honest test with trend gating.

All use Alpaca daily bars (SIP) + FRED. Every result reports net-of-cost
CAGR/Sharpe/MaxDD + per-year returns. No parameter search beyond the
pre-registered variants listed here — variants are few and disclosed.
"""

import argparse
import sys

import numpy as np
import pandas as pd
import requests

from quant.config import ALPACA_KEY_PAPER, ALPACA_SECRET_PAPER, FRED_KEY

H = {"APCA-API-KEY-ID": ALPACA_KEY_PAPER, "APCA-API-SECRET-KEY": ALPACA_SECRET_PAPER}


def alpaca_daily(symbol: str, start: str, crypto=False) -> pd.DataFrame:
    if crypto:
        url = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
        params = {"symbols": symbol, "timeframe": "1Day",
                  "start": f"{start}T00:00:00Z", "limit": 10_000}
    else:
        url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
        params = {"timeframe": "1Day", "feed": "sip", "adjustment": "all",
                  "start": f"{start}T00:00:00Z", "limit": 10_000}
    rows = []
    while True:
        r = requests.get(url, headers=H, params=params, timeout=60)
        r.raise_for_status()
        j = r.json()
        bars = j["bars"][symbol] if crypto else j.get("bars") or []
        rows.extend(bars)
        tok = j.get("next_page_token")
        if not tok:
            break
        params["page_token"] = tok
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"]).dt.tz_localize(None).dt.normalize()
    return df.set_index("t")[["o", "h", "l", "c", "v"]].astype(float)


def fred(series: str, start="2015-01-01") -> pd.Series:
    r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                     params={"series_id": series, "api_key": FRED_KEY,
                             "file_type": "json", "observation_start": start},
                     timeout=30).json()
    return pd.Series({pd.Timestamp(o["date"]): float(o["value"])
                      for o in r.get("observations", []) if o["value"] != "."},
                     name=series)


def perf(ret: pd.Series, label: str, days=252):
    ret = ret.dropna()
    if ret.empty:
        print(f"{label}: no data")
        return
    eq = (1 + ret).cumprod()
    yrs = len(ret) / days
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    sh = ret.mean() / ret.std() * np.sqrt(days) if ret.std() > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    exposure = (ret != 0).mean()
    print(f"{label:44s} CAGR={cagr:+7.1%}  Sharpe={sh:5.2f}  MaxDD={dd:6.1%}  "
          f"exposed={exposure:4.0%}")
    yearly = ret.groupby(ret.index.year).apply(lambda r: (1 + r).prod() - 1)
    print(" " * 4 + "  ".join(f"{y}:{v:+.0%}" for y, v in yearly.items()))


# ── E1: vol carry ────────────────────────────────────────────────────────────
def e1_vol_carry():
    print("\n══ E1 VOL CARRY — short-vol gated by term structure ══")
    vix = fred("VIXCLS")
    vix3m = fred("VIX3MCLS")
    if vix3m.empty:
        vix3m = fred("VXVCLS")
    svxy = alpaca_daily("SVXY", "2016-01-01")
    vxx = alpaca_daily("VXX", "2018-01-01")

    for name, px, side, borrow_bps_yr in [("long SVXY (-0.5x vol)", svxy, +1, 0),
                                          ("short VXX (-1x vol)", vxx, -1, 500)]:
        df = pd.DataFrame({"c": px["c"]})
        df["ret"] = df["c"].pct_change()
        df["vix"] = vix.reindex(df.index).ffill()
        df["vix3m"] = vix3m.reindex(df.index).ffill()
        # signals known at close t-1 → position for day t (shift 1)
        contango = (df["vix3m"] / df["vix"] - 1).shift(1)
        vix_lag = df["vix"].shift(1)
        sma50 = df["c"].rolling(50).mean().shift(1)
        trend_ok = (df["c"].shift(1) > sma50) if side > 0 else (df["c"].shift(1) < sma50)

        variants = {
            "unconditional": pd.Series(True, index=df.index),
            "contango>3%": contango > 0.03,
            "contango>3% & vix<25": (contango > 0.03) & (vix_lag < 25),
            "contango>3% & vix<25 & trend": (contango > 0.03) & (vix_lag < 25) & trend_ok,
        }
        for vname, mask in variants.items():
            pos = mask.astype(float)
            strat = pos * df["ret"] * (1 if side > 0 else -1)
            turn = pos.diff().abs().fillna(0)
            cost = turn * 3 / 1e4 + pos * (borrow_bps_yr / 252 / 1e4)
            perf(strat - cost, f"{name} | {vname}")


# ── E2: crypto trend ─────────────────────────────────────────────────────────
CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD", "AVAX/USD", "LTC/USD",
          "DOGE/USD"]


def e2_crypto_trend():
    print("\n══ E2 CRYPTO TREND — long-only TSMOM, vol-targeted, 25bp/side ══")
    px = {}
    for s in CRYPTO:
        try:
            px[s] = alpaca_daily(s, "2021-01-01", crypto=True)["c"]
        except Exception as e:  # noqa: BLE001
            print(f"  {s}: unavailable ({e})")
    close = pd.DataFrame(px).ffill()
    ret = close.pct_change()
    days = 365  # crypto trades every day

    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    mom20 = close.pct_change(20)
    vol20 = ret.rolling(20).std() * np.sqrt(days)

    signal = ((close > sma20) & (close > sma50) & (mom20 > 0)).shift(1)
    w_iv = (0.40 / vol20.shift(1)).clip(upper=1.0)  # 40% vol target per asset
    w = signal * w_iv
    w = w.div(w.sum(axis=1).clip(lower=1.0), axis=0)  # cap gross at 1x

    strat = (w * ret).sum(axis=1)
    turn = w.diff().abs().sum(axis=1).fillna(0)
    cost = turn * 25 / 1e4
    perf(strat - cost, "portfolio TSMOM (net 25bp/side)", days=days)
    perf(strat, "portfolio TSMOM (gross)", days=days)
    bh = ret["BTC/USD"]
    perf(bh, "BTC buy-hold (reference)", days=days)


# ── E3: 3x ETF overnight ─────────────────────────────────────────────────────
LEV3X = ["SOXL", "TECL", "FAS", "UDOW", "LABU", "TNA"]


def e3_overnight_3x():
    print("\n══ E3 3x-ETF OVERNIGHT — close→open hold, 2bp/side ══")
    for sym in LEV3X:
        try:
            df = alpaca_daily(sym, "2016-01-01")
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: unavailable ({e})")
            continue
        on = df["o"] / df["c"].shift(1) - 1          # overnight return
        cost = 4 / 1e4                                # full round trip daily
        sma50 = df["c"].rolling(50).mean().shift(1)
        trend = df["c"].shift(1) > sma50
        perf(on - cost, f"{sym} overnight uncond.")
        perf(on.where(trend, 0.0) - cost * trend.astype(float),
             f"{sym} overnight | >50d SMA")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--all", action="store_true")
    p.add_argument("--e1", action="store_true")
    p.add_argument("--e2", action="store_true")
    p.add_argument("--e3", action="store_true")
    args = p.parse_args()
    if not (args.all or args.e1 or args.e2 or args.e3):
        p.print_help()
        sys.exit(1)
    if args.all or args.e1:
        e1_vol_carry()
    if args.all or args.e2:
        e2_crypto_trend()
    if args.all or args.e3:
        e3_overnight_3x()
