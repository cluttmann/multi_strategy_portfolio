"""Polymarket ↔ Aktienmarkt — erste empirische Verknüpfung.

    python3 -m quant.research.polymarket_study --run

Zieht Tageshistorien equity-relevanter Polymarket-Märkte (Gamma/CLOB-API,
öffentlich, kein Auth) und prüft:
  1. Gleichzeitige Korrelation: Odds-Änderung vs. SPY/TLT/VIX am selben Tag
  2. Lead-Lag beide Richtungen: führen Odds die Aktien (t-1 → t) oder
     folgen sie ihnen? (Richtung des Informationsflusses)
Ehrliche Erwartung: Fed-Odds duplizieren CME-FedWatch (kein Lead erwartet);
Rezessions-/Wahl-/Geopolitik-Odds sind die Kandidaten für eigenständige
Information.
"""

import argparse
import json
import sys

import numpy as np
import pandas as pd
import requests

from quant.research.exotic_sleeves import alpaca_daily, fred

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Kuratierte equity-relevante Märkte (Slug oder Suchbegriff, Label)
TARGETS = [
    ("presidential-election-winner-2024", "Trump", "Wahl 2024 (Trump)"),
    (None, "US recession by end of 2026?", "US-Rezession 2026"),
    (None, "US recession in 2025?", "US-Rezession 2025"),
    (None, "Fed rate cut in 2025?", "Fed-Senkung 2025"),
    (None, "Will Powell be replaced", "Powell-Ablösung"),
    (None, "US strikes Iran", "Geopolitik: Iran"),
]


def find_market(slug: str | None, q: str):
    if slug:
        ev = requests.get(f"{GAMMA}/events", params={"slug": slug},
                          timeout=30).json()
        if ev:
            ms = ev[0]["markets"]
            m = next((x for x in ms if q.lower() in x.get("question", "").lower()),
                     max(ms, key=lambda x: float(x.get("volume") or 0)))
            return m
        return None
    s = requests.get(f"{GAMMA}/public-search",
                     params={"q": q, "limit_per_type": 8}, timeout=30).json()
    evs = s.get("events") or []
    for e in sorted(evs, key=lambda x: -float(x.get("volume") or 0)):
        ms = e.get("markets") or []
        if not ms:
            full = requests.get(f"{GAMMA}/events",
                                params={"slug": e.get("slug")}, timeout=30).json()
            ms = full[0].get("markets", []) if full else []
        exact = [x for x in ms if q.lower().rstrip("?") in
                 x.get("question", "").lower()]
        cands = exact or ms
        if cands:
            return max(cands, key=lambda x: float(x.get("volume") or 0))
    return None


def history(market: dict) -> pd.Series | None:
    try:
        tok = json.loads(market["clobTokenIds"])[0]
    except Exception:  # noqa: BLE001
        return None
    h = requests.get(f"{CLOB}/prices-history",
                     params={"market": tok, "interval": "max",
                             "fidelity": 1440}, timeout=30).json()
    pts = h.get("history") or []
    if len(pts) < 30:
        return None
    s = pd.Series({pd.Timestamp(p["t"], unit="s").normalize(): p["p"]
                   for p in pts})
    return s[~s.index.duplicated(keep="last")].sort_index()


def run():
    spy = alpaca_daily("SPY", "2021-01-01")["c"].pct_change()
    tlt = alpaca_daily("TLT", "2021-01-01")["c"].pct_change()
    vix = fred("VIXCLS", start="2021-01-01").diff()
    spy.index = pd.to_datetime(spy.index).normalize()
    tlt.index = pd.to_datetime(tlt.index).normalize()

    rows = []
    for slug, q, label in TARGETS:
        m = find_market(slug, q)
        if m is None:
            print(f"{label}: kein Markt gefunden")
            continue
        s = history(m)
        if s is None:
            print(f"{label}: zu wenig Historie")
            continue
        dodds = s.diff().dropna()
        dodds = dodds[dodds.abs() > 0]  # tote Tage raus
        df = pd.DataFrame({"dodds": dodds, "spy": spy, "tlt": tlt,
                           "vix": vix}).dropna()
        if len(df) < 25:
            print(f"{label}: zu wenig Überlappung ({len(df)})")
            continue
        r = {
            "markt": label,
            "n_tage": len(df),
            "vol_usd": float(m.get("volume") or 0),
            # gleichzeitig
            "spy_same": df["dodds"].corr(df["spy"]),
            "vix_same": df["dodds"].corr(df["vix"]),
            # Odds führen Aktien?
            "odds_lead_spy": df["dodds"].shift(1).corr(df["spy"]),
            # Aktien führen Odds?
            "spy_lead_odds": df["spy"].shift(1).corr(df["dodds"]),
        }
        rows.append(r)
        print(f"{label:24s} n={r['n_tage']:4d}  same-day: SPY {r['spy_same']:+.2f} "
              f"VIX {r['vix_same']:+.2f} | odds→SPY(t+1) {r['odds_lead_spy']:+.2f} "
              f"| SPY→odds(t+1) {r['spy_lead_odds']:+.2f}")

    out = pd.DataFrame(rows)
    out.to_parquet("quant/_staging/polymarket_corr.parquet", index=False)
    print("\nLesehilfe: |corr| < ~0.10 ist bei diesen Stichproben Rauschen. "
          "Entscheidend ist die Spalte odds→SPY(t+1): nur dort wäre "
          "handelbare Information.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    args = p.parse_args()
    if not args.run:
        p.print_help()
        sys.exit(1)
    run()
