"""Polymarket Wallet-Forensik — Pilot auf einem aufgelösten Headline-Markt.

    python3 -m quant.research.polymarket_wallets --market-slug <slug> --run
    python3 -m quant.research.polymarket_wallets --auto-iran --run

Zieht ALLE Fills eines Markts (Data-API, wallet-aufgelöst, public), berechnet
pro Wallet realisierten P&L und Entry-Timing, und prüft die Kernfrage der
Insider-These: Haben die profitabelsten Wallets VOR den großen
Odds-Sprüngen gekauft (Information) oder danach (Momentum)?
"""

import argparse
import json
import sys
import time

import numpy as np
import pandas as pd
import requests

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"


def find_iran_market() -> dict | None:
    s = requests.get(f"{GAMMA}/public-search",
                     params={"q": "US strikes Iran", "limit_per_type": 6},
                     timeout=30).json()
    for e in sorted(s.get("events") or [],
                    key=lambda x: -float(x.get("volume") or 0)):
        full = requests.get(f"{GAMMA}/events", params={"slug": e.get("slug")},
                            timeout=30).json()
        for m in (full[0].get("markets", []) if full else []):
            if m.get("closed") and float(m.get("volume") or 0) > 5e6:
                return m
    return None


def pull_trades(condition_id: str, max_fills: int = 150_000) -> pd.DataFrame:
    rows, offset = [], 0
    while len(rows) < max_fills:
        r = requests.get(f"{DATA}/trades",
                         params={"market": condition_id, "limit": 500,
                                 "offset": offset, "takerOnly": "false"},
                         timeout=30)
        if r.status_code == 429:
            time.sleep(5)
            continue
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if offset % 10_000 == 0:
            print(f"  {offset:,} fills ...", flush=True)
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df["price"] = df["price"].astype(float)
    df["size"] = df["size"].astype(float)
    return df


def analyze(m: dict, trades: pd.DataFrame):
    outcomes = json.loads(m.get("outcomes") or '["Yes","No"]')
    prices = json.loads(m.get("outcomePrices") or "[null,null]")
    yes_won = prices and float(prices[0]) > 0.5
    print(f"\nMarkt: {m['question']}")
    print(f"Aufgelöst: {'JA' if yes_won else 'NEIN'} | Fills: {len(trades):,} | "
          f"Wallets: {trades['proxyWallet'].nunique():,}")

    # Wallet-P&L: YES-Position zu Auflösungswert bewerten
    t = trades.copy()
    t["yes_delta"] = np.where(
        (t["outcome"] == outcomes[0]) & (t["side"] == "BUY"), t["size"],
        np.where((t["outcome"] == outcomes[0]) & (t["side"] == "SELL"),
                 -t["size"],
        np.where((t["outcome"] == outcomes[1]) & (t["side"] == "BUY"),
                 -t["size"], t["size"])))
    t["cash_flow"] = np.where(
        t["outcome"] == outcomes[0],
        np.where(t["side"] == "BUY", -t["price"] * t["size"],
                 t["price"] * t["size"]),
        np.where(t["side"] == "BUY", -(1 - t["price"]) * -t["size"] * -1,
                 0.0))
    # sauberer: alles in YES-Äquivalenten rechnen
    t["yes_price"] = np.where(t["outcome"] == outcomes[0], t["price"],
                              1 - t["price"])
    t["cash"] = -t["yes_delta"] * t["yes_price"]
    g = t.groupby("proxyWallet").agg(
        yes_pos=("yes_delta", "sum"), cash=("cash", "sum"),
        n_fills=("size", "count"), first_ts=("ts", "min"),
        vwap_num=("cash", "sum"))
    g["pnl"] = g["cash"] + g["yes_pos"] * (1.0 if yes_won else 0.0)
    g = g.sort_values("pnl", ascending=False)

    print(f"\nTop-10 Wallets nach P&L (USD):")
    for w, r in g.head(10).iterrows():
        wt = t[t["proxyWallet"] == w]
        # Anteil der Käufe zu niedrigen Odds (<30c) = früh/gegen den Markt
        cheap = wt[(wt["yes_delta"] > 0) & (wt["yes_price"] < 0.30)]["yes_delta"].sum()
        tot = wt[wt["yes_delta"] > 0]["yes_delta"].sum()
        name = (wt["pseudonym"].iloc[0] or "")[:18]
        print(f"  {w[:10]}… {name:18s} P&L=${r.pnl:>10,.0f}  fills={int(r.n_fills):5d}  "
              f"Käufe<30c: {cheap/max(tot,1):4.0%}")

    # Timing: größte Tages-Odds-Sprünge vs. Kaufverhalten der Top-Wallets
    daily = t.set_index("ts").sort_index()["yes_price"].resample("1D").last().ffill()
    jumps = daily.diff().abs().nlargest(5)
    print(f"\nGrößte Tages-Sprünge der Odds:")
    top_wallets = set(g.head(20).index)
    for d, jmp in jumps.items():
        day = d.normalize()
        before = t[(t["ts"] >= day - pd.Timedelta(days=3)) & (t["ts"] < day)
                   & (t["proxyWallet"].isin(top_wallets))]
        buys_before = before[before["yes_delta"] > 0]["yes_delta"].sum()
        sells_before = -before[before["yes_delta"] < 0]["yes_delta"].sum()
        print(f"  {day.date()}  Δ={daily.diff()[d]:+.2f}  "
              f"Top-20-Wallets 3 Tage davor: +{buys_before:,.0f} YES gekauft, "
              f"{sells_before:,.0f} verkauft")

    return g, t


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--auto-iran", action="store_true")
    p.add_argument("--market-slug")
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    m = find_iran_market() if args.auto_iran else None
    if m is None and args.market_slug:
        ev = requests.get(f"{GAMMA}/events",
                          params={"slug": args.market_slug}, timeout=30).json()
        if ev:
            m = max(ev[0]["markets"], key=lambda x: float(x.get("volume") or 0))
    if m is None:
        print("kein Markt gefunden")
        sys.exit(1)
    print(f"ziehe Fills für: {m['question'][:70]}")
    trades = pull_trades(m["conditionId"])
    g, t = analyze(m, trades)
    g.head(200).to_parquet("quant/_staging/pm_wallets_pilot.parquet")
