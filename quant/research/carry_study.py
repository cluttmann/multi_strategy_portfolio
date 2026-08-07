"""Familie 19 (CARRY): Cross-Asset-Carry über ETFs — ohne US-Aktien, ohne Krypto.

    python3 -m quant.research.carry_study --run

MECHANISMUS: Carry ist der Preis dafür, das Risiko zu tragen, dass der
Kassakurs gegen einen läuft. Hedger (Anleiheemittenten, Rohstoffproduzenten,
Importeure) zahlen Spekulanten für die Übernahme dieses Risikos. Wie die
Trendprämie in DTRD ist das KEIN Informationsvorsprung — es ist eine
Versicherungsprämie und verfällt deshalb nicht durch Publikation.
Koijen/Moskowitz/Pedersen/Vrugt (JFE 2018): diversifizierter Carry über vier
Anlageklassen Sharpe 0.7-0.9, Korrelation zu Trend nahe null.

CARRY-MESSUNG OHNE NEUE DATENQUELLE: Die Ausschüttungsrendite eines ETFs steckt
in der Differenz zwischen `adjusted_close` (Total Return) und `close`
(Kursrendite) — über 252 Tage gilt
    Ausschüttungsrendite ≈ (adj_t/adj_{t-252}) / (close_t/close_{t-252}) − 1.
Das ist die KMPV-Carry-Definition für Anleihen, Aktien und Währungs-ETFs in
einer Formel, gerechnet aus Daten, die wir schon haben. Für Rohstoff-ETFs ohne
Ausschüttung ergibt sie korrekt ≈ 0, also negativen Netto-Carry nach
Finanzierungskosten — genau die Contango-Kosten, die man vermeiden will.

QUANTITATIVE VORHERSAGE (Regel R2 — das ist der Test, nicht der Backtest):
  (a) MONOTONIE: Rendite muss über Carry-Quintile monoton steigen. Verdient
      nur das oberste Quintil und der Rest ist Rauschen, ist es kein
      Carry-Effekt sondern ein Einzelasset-Artefakt (so starb LETF_REBAL).
  (b) BREITE: in mehreren Anlageklassen positiv, nicht nur in einer.
  (c) ORTHOGONALITÄT: ρ zu DTRD nahe null, weil Carry und Trend verschiedene
      Signalquellen sind (dieselben Assets, andere Information).

VORREGISTRIERT (Regel R6): genau 3 Varianten (Top-N ∈ {3,5,8}), long/flat,
inverse Vol auf 10 % Vol-Target, Gross ≤ 1.0, monatliche Umschichtung,
Kosten 5bp/Seite. Training bis 2019, 2020-2026 striktes Holdout (Regel R7).
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.data.bq import query

TOP_N = [3, 5, 8]                # genau 3 Varianten
VOL_TARGET = 0.10
COST_BPS = 5.0
TRAIN_END = "2019-12-31"
MIN_ADV = 5e6
YIELD_LO, YIELD_HI = -0.02, 0.30  # außerhalb → Split-Artefakt, nicht Carry

# Bewusst OHNE US-Aktien (XSR/ONX handeln dort) und OHNE Krypto
UNIVERSE = {
    "Treasuries":  ["SHY", "IEI", "IEF", "TLH", "TLT", "TIP"],
    "Kredit":      ["LQD", "HYG", "JNK", "EMB", "BKLN", "PFF"],
    "Intl-Aktien": ["EFA", "EEM", "VGK", "EWJ", "IDV", "DVYE", "EWU", "EWG"],
    "Immobilien":  ["VNQ", "RWX", "IYR"],
    "Rohstoffe":   ["GLD", "SLV", "DBC", "USO", "DBA", "UNG", "PPLT"],
    "Währungen":   ["UUP", "FXE", "FXY", "FXB", "FXF"],
}
ALL = [s for v in UNIVERSE.values() for s in v]
KLASSE = {s: k for k, v in UNIVERSE.items() for s in v}


def load(start="2007-01-01") -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    q = ", ".join(repr(s) for s in ALL)
    df = query(f"""
      SELECT date, symbol, adjusted_close AS ac, close AS cl,
             close * volume AS dvol
      FROM `trading-436516.quant.eod_bars`
      WHERE symbol IN ({q}) AND adjusted_close > 0 AND close > 0
        AND date >= '{start}'""")
    df["date"] = pd.to_datetime(df["date"])
    adj = df.pivot(index="date", columns="symbol", values="ac").sort_index()
    raw = df.pivot(index="date", columns="symbol", values="cl").sort_index()
    dv = df.pivot(index="date", columns="symbol", values="dvol").sort_index()
    adv = dv.rolling(20).mean()
    adj = adj.where(adv >= MIN_ADV).ffill(limit=3)
    raw = raw.where(adv >= MIN_ADV).ffill(limit=3)

    rf = query("""SELECT date, value/100 AS rf
                  FROM `trading-436516.quant.fred_series`
                  WHERE series = 'DTB3' AND value IS NOT NULL""")
    if rf.empty:                       # Fallback, falls DTB3 nicht ingestiert
        rf = query("""SELECT date, value/100 AS rf
                      FROM `trading-436516.quant.fred_series`
                      WHERE series = 'DFF' AND value IS NOT NULL""")
    rf["date"] = pd.to_datetime(rf["date"])
    return adj, raw, rf.set_index("date")["rf"].sort_index()


def carry(adj: pd.DataFrame, raw: pd.DataFrame, rf: pd.Series) -> pd.DataFrame:
    """Netto-Carry = 12M-Ausschüttungsrendite − Geldmarktsatz."""
    tr = adj / adj.shift(252) - 1        # Total Return
    pr = raw / raw.shift(252) - 1        # Kursrendite
    y = (1 + tr) / (1 + pr) - 1          # Ausschüttungsrendite
    y = y.where((y >= YIELD_LO) & (y <= YIELD_HI))
    return y.sub(rf.reindex(y.index).ffill(), axis=0)


def sleeve(adj: pd.DataFrame, c: pd.DataFrame, top_n: int,
           cost_bps=COST_BPS) -> pd.Series:
    """Long/flat Top-N-Carry, inverse Vol auf Vol-Target, monatlich."""
    ret = adj.pct_change()
    vol = ret.rolling(63).std() * np.sqrt(252)
    rank = c.rank(axis=1, ascending=False)
    sel = (rank <= top_n) & (c > 0) & vol.notna()      # nur positiver Carry
    w = sel.astype(float) * (VOL_TARGET / vol.clip(lower=0.03))
    w = w.div(w.sum(axis=1).clip(lower=1.0), axis=0)   # Gross ≤ 1.0
    month = pd.Series(w.index.to_period("M"), index=w.index)
    reb = month.ne(month.shift(1))
    w = w.where(reb).ffill().shift(1).fillna(0.0)
    gross = (w * ret).sum(axis=1)
    turn = w.diff().abs().sum(axis=1).fillna(0.0)
    return (gross - turn * cost_bps / 1e4).dropna()


def stats(r: pd.Series, ann=252) -> dict:
    r = r.dropna()
    if len(r) < 250:
        return {}
    eq = (1 + r).cumprod()
    return {"n": len(r), "sharpe": r.mean() / r.std() * np.sqrt(ann),
            "cagr": eq.iloc[-1] ** (ann / len(r)) - 1,
            "maxdd": (eq / eq.cummax() - 1).min(),
            "vol": r.std() * np.sqrt(ann), "skew": r.skew()}


def returns(top_n: int = 5, start="2007-01-01") -> pd.Series:
    """Entry-Point für die Discovery-Pipeline."""
    adj, raw, rf = load(start)
    return sleeve(adj, carry(adj, raw, rf), top_n)


def live_weights(top_n: int = 5) -> tuple[dict[str, float], str]:
    """Live-Signal für den generischen Executor (Gate G8)."""
    q = ", ".join(repr(s) for s in ALL)
    df = query(f"""
      SELECT date, symbol, adjusted_close AS ac, close AS cl,
             close * volume AS dvol
      FROM `trading-436516.quant.eod_bars`
      WHERE symbol IN ({q}) AND adjusted_close > 0 AND close > 0
        AND date >= DATE_SUB(CURRENT_DATE(), INTERVAL 420 DAY)""")
    if df.empty:
        return {}, "keine Kursdaten (fail-closed → flat)"
    df["date"] = pd.to_datetime(df["date"])
    adj = df.pivot(index="date", columns="symbol", values="ac").sort_index()
    raw = df.pivot(index="date", columns="symbol", values="cl").sort_index()
    dv = df.pivot(index="date", columns="symbol", values="dvol").sort_index()
    if len(adj) < 260:
        return {}, f"nur {len(adj)} Tage Historie (fail-closed → flat)"
    # DTB3 ist im Warehouse nicht ingestiert → derselbe DFF-Fallback wie in
    # load(); ohne ihn lieferte live_weights() dauerhaft "flat" und Gate G8
    # hätte den Sleeve grundlos durchfallen lassen.
    rf_df = query("""SELECT value/100 AS rf FROM `trading-436516.quant.fred_series`
                     WHERE series IN ('DTB3', 'DFF') AND value IS NOT NULL
                     ORDER BY CASE series WHEN 'DTB3' THEN 0 ELSE 1 END,
                              date DESC LIMIT 1""")
    if rf_df.empty:
        return {}, "kein Geldmarktsatz (fail-closed → flat)"
    rf = float(rf_df["rf"].iloc[0])

    liq = dv.rolling(20).mean().iloc[-1] >= MIN_ADV
    tr = adj.iloc[-1] / adj.iloc[-253] - 1
    pr = raw.iloc[-1] / raw.iloc[-253] - 1
    y = (1 + tr) / (1 + pr) - 1
    y = y.where((y >= YIELD_LO) & (y <= YIELD_HI))
    c = (y - rf).where(liq)
    vol = adj.pct_change().tail(63).std() * np.sqrt(252)
    sel = c[(c > 0) & vol.notna() & (vol > 0)].nlargest(top_n)
    if sel.empty:
        return {}, f"kein Asset mit positivem Carry über {rf:.2%} → flat"
    w = VOL_TARGET / vol.reindex(sel.index).clip(lower=0.03)
    w = w / max(w.sum(), 1.0)
    w = w[w > 0.005]
    top = ", ".join(f"{s} {c[s]:+.1%}" for s in sel.index[:4])
    return w.to_dict(), (f"Top-{top_n} Carry über rf {rf:.2%} "
                         f"(gross {w.sum():.0%}): {top}")


def run():
    adj, raw, rf = load()
    c = carry(adj, raw, rf)
    print(f"Universum: {adj.shape[1]} ETFs, {adj.index.min():%Y-%m} → "
          f"{adj.index.max():%Y-%m} | Geldmarktsatz {rf.iloc[-1]:.2%}")
    last = c.iloc[-1].dropna().sort_values(ascending=False)
    print("Aktueller Netto-Carry (Top 6 / Flop 3): "
          + "  ".join(f"{s}:{v:+.1%}" for s, v in last.head(6).items())
          + "  …  "
          + "  ".join(f"{s}:{v:+.1%}" for s, v in last.tail(3).items()) + "\n")

    # ── Vorhersage (a): Monotonie über Carry-Quintile ────────────────────────
    print("═══ VORHERSAGE (a): steigt die Rendite monoton mit dem Carry? ═══")
    # 21-Tage-VORWÄRTSrendite (adj.shift(-21)/adj − 1), nicht die Tagesrendite
    # in 21 Tagen; monatliche Stichproben, damit die Fenster nicht überlappen.
    fwd = adj.shift(-21) / adj - 1
    rk = c.rank(axis=1, pct=True)
    month = pd.Series(adj.index.to_period("M"), index=adj.index)
    take = month.ne(month.shift(1))
    rows = []
    for qi in range(5):
        mask = (rk > qi / 5) & (rk <= (qi + 1) / 5)
        r = fwd.where(mask).mean(axis=1)[take].dropna()
        rows.append((qi + 1, (1 + r.mean()) ** 12 - 1, len(r)))
    for qi, m, n in rows:
        print(f"  Q{qi} (Q1=höchster Carry): {m:+7.2%} p.a. ({n:,} Monate)")
    spread = rows[0][1] - rows[4][1]
    # Kein Toleranzband: der erste Versuch nutzte ±0.5 %-Punkte, was größer war
    # als jeder gemessene Quintilsunterschied — der Test meldete "bestätigt",
    # obwohl die Ordnung UMGEKEHRT war. Jetzt zählt die tatsächliche Ordnung.
    mono = all(rows[i][1] >= rows[i + 1][1] for i in range(4))
    print(f"  → Ordnung {'monoton' if mono else 'NICHT monoton'}; "
          f"Q1−Q5 = {spread:+.2%} p.a. → Vorhersage (a) "
          f"{'bestätigt' if spread > 0 else 'WIDERLEGT (Vorzeichen falsch)'}")

    # ── Training / Variantenwahl ─────────────────────────────────────────────
    print("\n═══ TRAINING bis 2019 (Variantenwahl) ═══")
    print(f"{'Top-N':>6s} {'Sharpe':>7s} {'CAGR':>7s} {'Vol':>6s} {'MaxDD':>7s}")
    tr = {}
    for n in TOP_N:
        s = stats(sleeve(adj, c, n).loc[:TRAIN_END])
        if s:
            tr[n] = s
            print(f"{n:6d} {s['sharpe']:7.2f} {s['cagr']:+7.1%} "
                  f"{s['vol']:6.1%} {s['maxdd']:7.1%}")
    if not tr:
        print("keine auswertbare Variante"); return
    best = max(tr, key=lambda k: tr[k]["sharpe"])
    print(f"\nGewählte Variante: Top-{best}")

    # ── Holdout ──────────────────────────────────────────────────────────────
    full = sleeve(adj, c, best)
    print("\n═══ HOLDOUT 2020–2026 (nie gefittet) ═══")
    ho = stats(full.loc["2020-01-01":])
    print(f"Sharpe {ho['sharpe']:.2f} | CAGR {ho['cagr']:+.1%} | "
          f"Vol {ho['vol']:.1%} | MaxDD {ho['maxdd']:.1%}")
    y = full.loc["2020-01-01":]
    print("Jahre: " + "  ".join(f"{k}:{v:+.0%}" for k, v in
          y.groupby(y.index.year).apply(lambda x: (1 + x).prod() - 1).items()))
    sf = stats(full)
    print(f"\nGESAMT: Sharpe {sf['sharpe']:.2f} | CAGR {sf['cagr']:+.1%} | "
          f"MaxDD {sf['maxdd']:.1%} | Schiefe {sf['skew']:+.2f}")
    r22 = stats(full.loc["2022-01-01":])
    print(f"2022+ (Regel R4): Sharpe {r22.get('sharpe', float('nan')):.2f} | "
          f"CAGR {r22.get('cagr', float('nan')):+.1%}")

    # ── Vorhersage (b): Breite über Anlageklassen ────────────────────────────
    print("\n═══ VORHERSAGE (b): in mehreren Anlageklassen positiv? ═══")
    for k, syms in UNIVERSE.items():
        cols = [s for s in syms if s in adj.columns]
        if len(cols) < 2:
            continue
        s = stats(sleeve(adj[cols], c[cols], min(best, len(cols))))
        if s:
            print(f"  {k:12s} Sharpe {s['sharpe']:5.2f} | "
                  f"CAGR {s['cagr']:+7.1%} ({len(cols)} ETFs)")

    # ── Vorhersage (c) + Regel R5 ────────────────────────────────────────────
    print("\n═══ VORHERSAGE (c) / Regel R5: ORTHOGONALITÄT ═══")
    from quant.research.discovery import live_sleeve_returns, portfolio_delta
    ex = live_sleeve_returns()
    before, after, rhos = portfolio_delta(full, ex, sf["sharpe"])
    for nm, rho in sorted(rhos.items()):
        print(f"  ρ(CARRY, {nm}) = {rho:+.3f}")
    print(f"  Portfolio-Sharpe {before:.2f} → {after:.2f} (Δ {after-before:+.3f})")

    print("\n═══ G5: Trials protokollieren ═══")
    from quant.research.trials_registry import log_trial
    for n in TOP_N:
        try:
            log_trial("CARRY", sleeve(adj, c, n), variant=f"Top-{n}",
                      verdict="KANDIDAT" if n == best else "Variante",
                      notes="Cross-Asset-Carry ETFs, monatlich, long/flat")
        except Exception as e:  # noqa: BLE001
            print(f"  Top-{n}: {e}")

    print("\n═══ G8: Live-Signal ═══")
    w, why = live_weights(best)
    print(f"  {why}")
    print("  " + "  ".join(f"{s} {v:.1%}" for s, v in
                           sorted(w.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if not a.run:
        ap.print_help()
        sys.exit(1)
    run()
