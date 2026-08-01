"""MERGARB-Backtest — Merger-Arbitrage auf angekündigte Cash-Übernahmen.

    python3 -m quant.research.mergarb_study --run

Familie MERGARB in quant/research/hypothesis_queue.yaml. Phase 1
(docs/superpowers/specs/2026-07-30-weg-zu-50-cagr-design.md): nur Cash-Deals
(Aktientausch braucht ein Hedge-Bein gegen den Käufer — Phase 2). Terminal-
Datum ist ein Proxy: eine Aktie, die aus eod_bars verschwindet, gilt als
GESCHLOSSEN (Delisting = Closing); eine Aktie, die über den Horizont hinaus
weiterhandelt UND spürbar vom Angebotspreis abgedriftet ist, gilt als
GEBROCHEN. Das ist eine Näherung (kein 8-K-Terminierungs-Scan) — bewusst
offengelegt wie die Delta-Proxy-Notiz in options_phase_a.py.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.config import BQ_DATASET, GCP_PROJECT
from quant.data.bq import query
from quant.data.merger_ingest import T_DEALS
from quant.research.exotic_sleeves import fred

MAX_HORIZON_DAYS = 270
BREAK_DRIFT_PCT = 0.15   # Abweichung vom letzten Preis, ab der ein noch
                         # offener, über-den-Horizont-hinaus laufender Deal
                         # als gebrochen gilt statt als weiter offen


def resolve_terminal_date(prices: pd.Series, announce_date,
                          max_horizon_days: int = MAX_HORIZON_DAYS
                          ) -> tuple[pd.Timestamp | None, str]:
    """prices: tägliche Kursreihe des Ziels AB dem Ankündigungstag (Index =
    Handelstage, wie sie in eod_bars vorliegen — kein künstliches Auffüllen).
    Liefert (Terminaldatum, Status) mit Status ∈ {closed, break, open,
    no_data}. no_data: die Kursreihe hat keine Beobachtung nahe dem
    Ankündigungstag (typischerweise Ticker-Wiederverwendung in eod_bars,
    nicht ein tatsächlich offener Deal) — siehe Plausibilitäts-Gate unten."""
    prices = prices.dropna().sort_index()
    ann = pd.Timestamp(announce_date)
    if prices.empty:
        return None, "open"
    # Plausibilitäts-Gate: hat die Kursreihe überhaupt Beobachtungen nahe dem
    # Ankündigungstag? Wenn nicht, deckt eod_bars den Deal-Zeitraum für dieses
    # Symbol gar nicht ab — typischerweise Ticker-Wiederverwendung (das Symbol
    # wurde später einer anderen, unabhängigen Firma zugewiesen) statt eines
    # tatsächlich noch offenen Deals. Das ist ein eigener Status, kein "open".
    near_announce = prices.loc[ann - pd.Timedelta(days=15):
                                ann + pd.Timedelta(days=15)]
    if near_announce.empty:
        return None, "no_data"
    horizon_end = ann + pd.Timedelta(days=max_horizon_days)
    last_obs = prices.index[-1]
    # Symbol handelt nicht mehr, aber der allgemeine Handelskalender läuft
    # weiter (der Aufrufer übergibt nur Kurse bis "heute") → Delisting = Close.
    today = pd.Timestamp.today().normalize()
    trading_gap = (today - last_obs).days
    if trading_gap > 10:
        return last_obs, "closed"
    if last_obs <= horizon_end:
        return None, "open"
    tail = prices.loc[ann:horizon_end]
    if tail.empty:
        return None, "open"
    drift = abs(prices.iloc[-1] / tail.iloc[-1] - 1)
    if drift > BREAK_DRIFT_PCT:
        return horizon_end, "break"
    return None, "open"


def deal_return_series(announce_date, terminal_date,
                       prices: pd.Series) -> pd.Series:
    """Tägliche einfache Renditen von announce_date bis terminal_date
    (inklusive), aus der Kursreihe des Ziels."""
    p = prices.dropna().sort_index()
    p = p.loc[pd.Timestamp(announce_date):pd.Timestamp(terminal_date)]
    if len(p) < 2:
        return pd.Series(dtype=float)
    return p.pct_change().dropna()


def _load_price_history(symbols: list[str], min_date) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    syms = ", ".join(repr(s) for s in symbols)
    df = query(f"""
      SELECT date, symbol, adjusted_close AS px
      FROM `{GCP_PROJECT}.{BQ_DATASET}.eod_bars`
      WHERE symbol IN ({syms}) AND date >= '{min_date}' AND adjusted_close > 0
      ORDER BY symbol, date""")
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_deals(cash_only: bool = True) -> pd.DataFrame:
    df = query(f"SELECT * FROM `{T_DEALS}`")
    if df.empty:
        return df
    if cash_only:
        # NICHT auf consideration_type == "cash" filtern: fast alle echten
        # Cash-Deals (Splunk, Nuance, Activision, Twitter) klassifizieren als
        # "mixed", weil Merger-Filings praktisch immer auch die Cash-Abgeltung
        # von Mitarbeiter-Optionen/RSUs in "shares of ... common stock"-
        # Sprache beschreiben, was den Aktientausch-Regex auslöst — selbst bei
        # einem für die eigentlichen Aktionäre zu 100% Cash-Deal. Der
        # eigentliche Filter ist "hat einen extrahierten Cash-Preis", nicht
        # "exakt als cash klassifiziert" (s. Task-2-Nacharbeit).
        df = df[df["deal_price_cash"].notna()
               & (df["consideration_type"] != "stock")]
    return df.reset_index(drop=True)


def _resolved_deals() -> pd.DataFrame:
    """Deals + ihr Terminalstatus, je aus eod_bars nachgerechnet."""
    deals = load_deals(cash_only=True)
    if deals.empty:
        return deals
    prices = _load_price_history(list(deals["symbol"].unique()),
                                 deals["announce_date"].min())
    if prices.empty:
        return pd.DataFrame()
    by_sym = {s: g.set_index("date")["px"] for s, g in prices.groupby("symbol")}
    rows = []
    for _, d in deals.iterrows():
        px = by_sym.get(d["symbol"])
        if px is None:
            continue
        term, status = resolve_terminal_date(px, d["announce_date"])
        rows.append({**d.to_dict(), "terminal_date": term, "status": status,
                     "announce_px": px.loc[pd.Timestamp(d["announce_date"]):]
                                      .iloc[0] if len(px.loc[pd.Timestamp(
                                          d["announce_date"]):]) else np.nan})
    return pd.DataFrame(rows)


def returns(**params) -> pd.Series:
    """Discovery-Pipeline-Entry-Point (discovery.evaluate erwartet
    `fn(**params) -> pd.Series`). Nur Tage, an denen mindestens ein Deal
    offen ist — auf Tagen ohne Position künstlich 0 einzutragen würde die
    annualisierte Sharpe gegen ein Buch verwässern, das ungenutztes Kapital
    tatsächlich reinvestiert (siehe Design-Spec, Hebel 2)."""
    resolved = _resolved_deals()
    if resolved.empty:
        return pd.Series(dtype=float)
    closed = resolved[resolved["status"].isin(["closed", "break"])]
    if closed.empty:
        return pd.Series(dtype=float)
    prices = _load_price_history(list(closed["symbol"].unique()),
                                 closed["announce_date"].min())
    by_sym = {s: g.set_index("date")["px"] for s, g in prices.groupby("symbol")}
    per_deal = []
    for _, d in closed.iterrows():
        px = by_sym.get(d["symbol"])
        if px is None or pd.isna(d["terminal_date"]):
            continue
        r = deal_return_series(d["announce_date"], d["terminal_date"], px)
        if len(r):
            per_deal.append(r)
    if not per_deal:
        return pd.Series(dtype=float)
    wide = pd.concat(per_deal, axis=1)
    return wide.mean(axis=1, skipna=True).dropna()


def live_weights() -> tuple[dict, str]:
    """G8-Entry-Point: aktuell offene Cash-Deals, gleichgewichtet, gross<=1.0."""
    resolved = _resolved_deals()
    if resolved.empty:
        return {}, "keine Deal-Daten (fail-closed)"
    open_deals = resolved[resolved["status"] == "open"]
    if open_deals.empty:
        return {}, "kein offener Cash-Deal → flat"
    w = 1.0 / len(open_deals)
    weights = {s: w for s in open_deals["symbol"].unique()}
    return weights, f"{len(weights)} offene Cash-Deals, EW"


def check_predictions() -> dict:
    """Die drei vorregistrierten Vorhersagen aus hypothesis_queue.yaml
    (id: MERGARB). (b) Cash>Aktientausch ist in Phase 1 NICHT prüfbar (keine
    Aktientausch-Preisextraktion) — wird als 'nicht_pruefbar' ausgewiesen,
    nicht stillschweigend übersprungen."""
    resolved = _resolved_deals()
    out = {"a_monotonie": None, "b_cash_vs_stock": "nicht_pruefbar_phase1",
          "c_vix_bruchrate": None}
    closed = resolved[resolved["status"].isin(["closed", "break"])].copy()
    if len(closed) >= 5:
        closed["spread"] = (closed["deal_price_cash"] / closed["announce_px"]
                            - 1)
        closed["holding_ret"] = np.where(
            closed["status"] == "closed",
            closed["deal_price_cash"] / closed["announce_px"] - 1, np.nan)
        closed["decile"] = pd.qcut(closed["spread"], min(5, closed["spread"]
                                                          .nunique()),
                                   duplicates="drop")
        by_decile = closed.groupby("decile")["holding_ret"].mean()
        out["a_monotonie"] = by_decile.to_dict()
        out["a_monoton"] = bool(by_decile.is_monotonic_increasing)
    if len(closed) >= 5:
        vix = fred("VIXCLS", start="2015-01-01")
        vix_at_announce = vix.reindex(pd.to_datetime(closed["announce_date"]),
                                      method="ffill")
        closed["vix"] = vix_at_announce.values
        high = closed[closed["vix"] > 25]
        low = closed[closed["vix"] <= 25]
        out["c_vix_bruchrate"] = {
            "hoch_vix": float(high["status"].eq("break").mean()) if len(high) else None,
            "niedrig_vix": float(low["status"].eq("break").mean()) if len(low) else None,
        }
    return out


def run():
    deals = load_deals(cash_only=False)
    print(f"{len(deals):,} Deals in quant.merger_deals "
          f"({(deals['consideration_type'] == 'cash').sum() if len(deals) else 0} "
          "Cash)")
    r = returns()
    if r.empty:
        print("keine auswertbare Rendite-Serie (zu wenige resolved Deals)")
        return
    ann = 252
    sharpe = r.mean() / r.std() * np.sqrt(ann) if r.std() > 0 else 0.0
    print(f"Sharpe (nur Tage mit offenem Deal): {sharpe:.2f}, n={len(r)}")
    preds = check_predictions()
    print("Vorhersagen:", preds)
    w, why = live_weights()
    print(f"live_weights: {why} ({len(w)} Positionen)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    a = p.parse_args()
    if not a.run:
        p.print_help()
        sys.exit(1)
    run()
