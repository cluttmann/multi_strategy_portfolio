"""Familie 18 (ACT13D): Drift nach Aktivisten-Einstieg (Schedule 13D).

    python3 -m quant.research.act13d_study --run

MECHANISMUS: Ein Aktivist, der über 5 % kauft, muss offenlegen. Der
angekündigte Wertbeitrag (Governance-Wechsel, Verkauf von Sparten, Rückkäufe)
realisiert sich über MONATE, während die Ankündigung sofort erfolgt. Der Markt
unterreagiert, weil der Ausgang unsicher ist und die Auszahlung langsam kommt —
ein Aufmerksamkeits-/Unsicherheitsargument, kein Informationsvorsprung.
Brav/Jiang/Partnoy/Thomas (JF 2008): +7 % abnormale Rendite um die Ankündigung,
kein Rückfall in den 12 Monaten danach.

QUANTITATIVE VORHERSAGE (Regel R2): Ist es Unterreaktion auf einen
Governance-Schock, muss der Effekt
  (a) bei INITIALEN 13D stärker sein als bei Änderungsmeldungen (13D/A) —
      die Änderung ist bereits bekannte Information,
  (b) bei kleinen Firmen stärker (weniger Analystenabdeckung → mehr
      Unterreaktion), UND das kollidiert mit Regel R3: wenn er NUR in
      unhandelbaren Microcaps lebt, ist er wertlos (siehe OVN_FADE),
  (c) monoton über den Halte-Horizont wachsen (Drift, kein Sprung).

KOSTEN (Regel R1): Haltedauer 20-120 Tage → 20bp Round-Trip verteilt auf
Monate. Der Kostenwall, der CAT/IMOM/GAP/PEAD tötete, greift hier nicht.
Wir handeln erst am Folge-Open, das Filing selbst ist nicht handelbar.

BEKANNTE VERZERRUNG: SECs CIK→Ticker-Map enthält nur AKTUELL notierte Firmen.
Aktivistenziele, die übernommen wurden (positiver Ausgang, oft mit Prämie),
fehlen also — genauso wie die, die auf Null gingen. Richtung des Netto-Bias
unbestimmt, deshalb explizit ausgewiesen und NICHT weggeredet.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.data.bq import query

HORIZONS = [21, 63, 126]        # genau 3 Varianten (Regel R6)
COST_BPS_RT = 20.0              # Round-Trip, konservativ (Live-Messung: ~10bp/Seite)
TRAIN_END = "2019-12-31"


def load_events(path="quant/research/_13d_pilot.parquet") -> pd.DataFrame:
    """Initiale SC-13D-Events. Nutzt BQ, wenn der Backfill schon lief."""
    try:
        df = query("""
          SELECT date, symbol, form FROM `trading-436516.quant.sec_13d_filings`
          WHERE form = 'SC 13D'""")
        if len(df) > 500:
            print(f"Events aus BigQuery: {len(df):,}")
            return df
    except Exception:  # noqa: BLE001
        pass
    df = pd.read_parquet(path)
    print(f"Events aus Pilot-Parquet: {len(df):,}")
    return df[["date", "symbol", "form"]]


def load_prices(symbols: list[str], start: str) -> pd.DataFrame:
    q = ", ".join(repr(s) for s in symbols)
    df = query(f"""
      SELECT date, symbol, adjusted_close AS ac, close * volume AS dvol
      FROM `trading-436516.quant.eod_bars`
      WHERE symbol IN ({q}) AND adjusted_close > 0 AND date >= '{start}'""")
    df["date"] = pd.to_datetime(df["date"])
    return df


def build_panel(ev: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    ev = ev.copy()
    ev["date"] = pd.to_datetime(ev["date"])
    syms = sorted(ev["symbol"].unique())
    start = (ev["date"].min() - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    px_l = load_prices(syms + ["SPY"], start)
    px = px_l.pivot_table(index="date", columns="symbol", values="ac").sort_index()
    dv = px_l.pivot_table(index="date", columns="symbol", values="dvol"
                          ).sort_index().rolling(20).mean()
    return px, dv


def event_returns(ev: pd.DataFrame, px: pd.DataFrame, dv: pd.DataFrame,
                  h: int) -> pd.DataFrame:
    """Abnormale Rendite je Event: Einstieg t+1, Ausstieg t+1+h, minus SPY."""
    if "SPY" not in px.columns:
        raise RuntimeError("SPY fehlt im Kurspanel")
    spy = px["SPY"]
    dates = px.index
    rows = []
    for _, e in ev.iterrows():
        s, d0 = e["symbol"], e["date"]
        if s not in px.columns or s == "SPY":
            continue
        pos = dates.searchsorted(d0, side="right")   # erster Tag NACH dem Filing
        if pos + h >= len(dates):
            continue
        d_in, d_out = dates[pos], dates[pos + h]
        p_in, p_out = px[s].iloc[pos], px[s].iloc[pos + h]
        if not (np.isfinite(p_in) and np.isfinite(p_out)) or p_in <= 0:
            continue
        r = p_out / p_in - 1
        rm = spy.iloc[pos + h] / spy.iloc[pos] - 1
        if abs(r) > 3.0:               # Artefaktschutz wie in portfolio_sim
            continue
        adv = dv[s].iloc[pos] if s in dv.columns else np.nan
        rows.append({"symbol": s, "date_in": d_in, "date_out": d_out,
                     "ret": r, "mkt": rm, "abn": r - rm, "adv": adv,
                     "px_in": p_in})
    return pd.DataFrame(rows)


def tstat(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / (x.std() / np.sqrt(len(x)))) if len(x) > 2 else np.nan


def portfolio(er: pd.DataFrame, px: pd.DataFrame, h: int,
              max_pos=0.10, cost_bps=COST_BPS_RT) -> pd.Series:
    """Gleichgewichtete Positionen, jede h Tage gehalten, Cap je Name.

    Beta-neutral gegen SPY: die Long-Seite wird mit dem gleichen Notional
    SPY-Short kompensiert (die Sleeve verdient das abnormale Alpha, nicht das
    Marktbeta — sonst korreliert sie mit allem im Portfolio).
    """
    ret = px.pct_change()
    idx = px.index
    book: dict[pd.Timestamp, list[str]] = {}
    for _, e in er.iterrows():
        i0 = idx.searchsorted(e["date_in"])
        for j in range(i0, min(i0 + h, len(idx))):
            book.setdefault(idx[j], []).append(e["symbol"])
    rows, prev = [], set()
    for d in idx:
        names = book.get(d, [])
        if not names:
            prev = set()
            continue
        w = min(max_pos, 1.0 / len(names))
        gross = w * len(names)
        r_long = float(np.nansum([ret[s].get(d, 0.0) for s in names
                                  if s in ret.columns])) * w
        r_mkt = float(ret["SPY"].get(d, 0.0)) * gross
        cur = set(names)
        turn = w * (len(cur - prev) + len(prev - cur))
        rows.append({"date": d, "net_ret": r_long - r_mkt
                     - turn * cost_bps / 2 / 1e4, "n": len(names),
                     "gross": gross})
        prev = cur
    df = pd.DataFrame(rows).set_index("date")
    return df["net_ret"], df


def stats(r: pd.Series, ann=252) -> dict:
    r = r.dropna()
    if len(r) < 200:
        return {}
    eq = (1 + r).cumprod()
    return {"n": len(r), "sharpe": r.mean() / r.std() * np.sqrt(ann),
            "cagr": eq.iloc[-1] ** (ann / len(r)) - 1,
            "maxdd": (eq / eq.cummax() - 1).min(),
            "vol": r.std() * np.sqrt(ann)}


def returns(h: int = 63, max_pos: float = 0.10) -> pd.Series:
    """Entry-Point für die Discovery-Pipeline."""
    ev = load_events()
    # BigQuery liefert DATE als dbdate; build_panel konvertiert nur seine
    # eigene Kopie, event_returns bekäme sonst datetime.date und scheitert am
    # searchsorted gegen den DatetimeIndex.
    ev = ev.copy()
    ev["date"] = pd.to_datetime(ev["date"])
    px, dv = build_panel(ev)
    er = event_returns(ev, px, dv, h)
    s, _ = portfolio(er, px, h, max_pos=max_pos)
    return s


def run():
    ev = load_events()
    ev["date"] = pd.to_datetime(ev["date"])
    print(f"{len(ev):,} initiale 13D | {ev['symbol'].nunique():,} Symbole | "
          f"{ev['date'].min():%Y-%m} → {ev['date'].max():%Y-%m}")
    px, dv = build_panel(ev)
    print(f"Kurspanel: {px.shape[1]:,} Symbole × {len(px):,} Tage "
          f"({1 - ev['symbol'].isin(px.columns).mean():.1%} der Events ohne Kurse)\n")

    # ── Vorhersage (c): wächst die Drift monoton mit dem Horizont? ───────────
    print("═══ VORHERSAGE (c): Drift über den Horizont (alle Events) ═══")
    print(f"{'H':>4s} {'N':>6s} {'abn. Rendite':>13s} {'t':>6s} "
          f"{'Median':>8s} {'>0':>6s} {'bp/Trade':>9s}")
    ers = {}
    for h in [5, 10, 21, 42, 63, 126, 252]:
        er = event_returns(ev, px, dv, h)
        ers[h] = er
        if len(er) < 50:
            continue
        print(f"{h:4d} {len(er):6,} {er['abn'].mean():+12.2%} "
              f"{tstat(er['abn']):6.2f} {er['abn'].median():+8.2%} "
              f"{(er['abn'] > 0).mean():6.1%} "
              f"{er['abn'].mean()*1e4:9.0f}")

    # ── G2: Kostenschranke ───────────────────────────────────────────────────
    print(f"\n═══ G2-Kostenschranke: braucht > {2*COST_BPS_RT:.0f}bp/Trade ═══")
    for h in HORIZONS:
        bp = ers[h]["abn"].mean() * 1e4
        ok = "PASS" if bp > 2 * COST_BPS_RT else "FAIL"
        print(f"  H={h:3d}: {bp:+6.0f}bp brutto → {ok}")

    # ── Vorhersage (b) + Regel R3: Liquiditätstiers ──────────────────────────
    print("\n═══ VORHERSAGE (b) / Regel R3: nach Liquiditätstier (H=63) ═══")
    er = ers[63].dropna(subset=["adv"])
    er["tier"] = pd.qcut(er["adv"], 4,
                         labels=["<Q1 winzig", "Q2", "Q3", ">Q4 liquide"])
    for t in er["tier"].cat.categories:
        s = er[er["tier"] == t]
        print(f"  {t:12s} N={len(s):4,} ADV≈${s['adv'].median()/1e6:7.1f}M "
              f"abn {s['abn'].mean():+7.2%} (t={tstat(s['abn']):5.2f})")
    trade = er[er["adv"] >= 5e6]
    print(f"  → handelbar (ADV≥$5M): N={len(trade):,} "
          f"abn {trade['abn'].mean():+.2%} (t={tstat(trade['abn']):.2f}), "
          f"{trade['abn'].mean()*1e4:.0f}bp vs. {2*COST_BPS_RT:.0f}bp Schranke")

    # ── Vorhersage (a): initial vs. Änderung ─────────────────────────────────
    print("\n═══ VORHERSAGE (a): initiale 13D vs. 13D/A ═══")
    try:
        amend = query("""SELECT date, symbol, form
                         FROM `trading-436516.quant.sec_13d_filings`
                         WHERE form = 'SC 13D/A'""")
        amend["date"] = pd.to_datetime(amend["date"])
        ea = event_returns(amend, px, dv, 63)
        print(f"  initial  N={len(ers[63]):5,} abn {ers[63]['abn'].mean():+.2%} "
              f"(t={tstat(ers[63]['abn']):.2f})")
        print(f"  13D/A    N={len(ea):5,} abn {ea['abn'].mean():+.2%} "
              f"(t={tstat(ea['abn']):.2f})")
        print("  → Vorhersage (a) " + ("bestätigt"
              if ers[63]["abn"].mean() > ea["abn"].mean() else "WIDERLEGT"))
    except Exception as e:  # noqa: BLE001
        print(f"  (13D/A nur nach BQ-Backfill prüfbar: {e})")

    # ── Regel R4: Regime ─────────────────────────────────────────────────────
    print("\n═══ Regel R4: nach Regime (H=63, handelbares Tier) ═══")
    for lab, sub in (("bis 2021", trade[trade["date_in"] <= "2021-12-31"]),
                     ("2022+", trade[trade["date_in"] > "2021-12-31"])):
        if len(sub) > 20:
            print(f"  {lab:9s} N={len(sub):4,} abn {sub['abn'].mean():+7.2%} "
                  f"(t={tstat(sub['abn']):5.2f})")

    # ── Sleeve-Simulation ────────────────────────────────────────────────────
    print("\n═══ SLEEVE (beta-neutral, EW, Cap 10 %, 20bp Round-Trip) ═══")
    best, bs = None, -9
    for h in HORIZONS:
        r, d = portfolio(ers[h][ers[h]["adv"] >= 5e6], px, h)
        s = stats(r)
        if not s:
            continue
        print(f"  H={h:3d}: Sharpe {s['sharpe']:5.2f} | CAGR {s['cagr']:+7.1%} "
              f"| Vol {s['vol']:5.1%} | MaxDD {s['maxdd']:6.1%} | "
              f"Ø {d['n'].mean():4.1f} Positionen")
        if s["sharpe"] > bs:
            best, bs = h, s["sharpe"]
    if best is None:
        print("  keine auswertbare Variante")
        return
    r_best, _ = portfolio(ers[best][ers[best]["adv"] >= 5e6], px, best)

    print("\n═══ ORTHOGONALITÄT ═══")
    from quant.research.discovery import live_sleeve_returns, portfolio_delta
    ex = live_sleeve_returns()
    before, after, rhos = portfolio_delta(r_best, ex, bs)
    for nm, rho in rhos.items():
        print(f"  ρ(ACT13D, {nm}) = {rho:+.3f}")
    print(f"  Portfolio-Sharpe {before:.2f} → {after:.2f} (Δ {after-before:+.3f})")

    print("\n═══ G5: Trials protokollieren ═══")
    from quant.research.trials_registry import log_trial
    for h in HORIZONS:
        r, _ = portfolio(ers[h][ers[h]["adv"] >= 5e6], px, h)
        try:
            log_trial("ACT13D", r, variant=f"H={h}d ADV≥5M",
                      verdict="KANDIDAT" if h == best else "Variante",
                      notes="13D-Aktivisten-Drift, beta-neutral, EW")
        except Exception as e:  # noqa: BLE001
            print(f"  H={h}: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if not a.run:
        ap.print_help()
        sys.exit(1)
    run()
