"""Risiko-Budget-Studie: Ist das Kapital über die Live-Sleeves falsch verteilt?

    python3 -m quant.research.risk_budget_study --run

MOTIVATION (Nutzer-Hypothese 2026-08-06): "es gibt sicher Sleeves, die sich
gut mit Hebel machen lassen und ohne nicht gut funktionieren." Genau das ist
DTRDs Bauart — `dtrd_study.sleeve()` kappt Gross bei 1.0 ("kein Hebel") bei
10 % Vol-Target und realisiert dadurch ~6 % Vol. Ein Sleeve mit gutem Sharpe,
der nur 6 % Vol trägt, kann bei 15 % Allokation NICHTS zur Portfoliorendite
beitragen — egal wie gut sein Sharpe ist. Umgekehrt frisst XSR (2× Gross,
~23 % Vol) den Großteil des Risikobudgets.

Das ist der KONSTRUKTIONS-Hebel (K2/K3 in ROAD_TO_50.md), der nie gebaut
wurde — er braucht keine neue Alpha-Quelle, nur eine andere Verteilung des
vorhandenen Risikos. Deshalb ist er von der S̄/√ρ̄-Decke NICHT betroffen: die
Decke begrenzt S_p, aber die heutige Allokation erreicht S_p gar nicht.

METHODIK, und warum sie so streng sein muss:
  * VOLLE Historien, nicht die Health-Monitor-Rekonstruktionen. Letztere sind
    absichtlich auf 3-6 Jahre gekürzt (Drift-Erkennung) — XSR hatte darin nur
    7 Monatsbeobachtungen, und ALLE seine Korrelationen waren der Prior 0.20,
    nicht gemessen. Auf solchen Zahlen "optimale Gewichte" zu rechnen ist
    Rauschverstärkung.
  * SPLIT-SAMPLE: Gewichte werden auf der ERSTEN Hälfte geschätzt und auf der
    ZWEITEN bewertet. Mean-Variance auf demselben Sample, auf dem man es
    bewertet, ist garantiert besser als alles andere und sagt nichts aus
    ("error maximization", Michaud 1989). Nur der Split zeigt, ob die
    Umverteilung real ist.
  * Zusätzlich zwei SCHÄTZFREIE Schemata (Inverse-Vol, ERC/Risk-Parity), die
    keine Sharpe-Schätzung brauchen. Wenn schon die trägt, ist das Ergebnis
    robust gegen Sharpe-Schätzfehler — der fragilste Input überhaupt.
"""

import argparse
import sys

import numpy as np
import pandas as pd

REGT_MAX = 1.9        # Overnight-Hebel (identisch zu portfolio_math.py)
LIVE_HAIRCUT = 0.75   # gemessene Live-Kosten vs. Backtest-Annahme
VOL_TARGET = 0.15     # Portfolio-Vol-Ziel für die Hebel-Rechnung
MIN_OVERLAP = 36      # Monate, unter denen eine Korrelation nicht geschätzt wird
SHRINK = 0.30         # Korrelations-Schrumpfung (wie discovery.py)

# ── Skalierung: WARUM zwei Größen nötig sind ──────────────────────────────────
# KORRIGIERT 2026-08-06 (Bug im ersten Lauf dieser Studie): jede Sleeve-
# Renditereihe ist auf IHREM EIGENEN internen Gross gemessen. Wer die Reihen
# direkt mit Dollar-Gewichten kombiniert, vergleicht Äpfel mit Birnen.
#   * XSR: portfolio_sim.GROSS_LEVERAGE = 2.0 → die Reihe gilt für ein
#     2.0×-Gross-Buch (1× long + 1× short).
#   * DTRD: dtrd_study.sleeve() kappt Gross bei 1.0 → Reihe gilt per 1.0×.
#   * EOMT: EW über IEF/TLT/EDV, Summe 1.0 → per 1.0×.
#   * MERGARB: live_weights() summiert auf 1.0 → per 1.0×.
# Rechengröße ist deshalb der MULTIPLIKATOR m_i auf die Einheitsreihe:
#     Portfoliorendite = Σ m_i · r_i
#     belegtes Konto-Gross = Σ m_i · internes_gross_i   ≤ REGT_MAX
# Damit kostet eine Einheit XSR-Rendite DOPPELT so viel Bilanz wie eine
# Einheit DTRD — ein zweiter, unabhängiger Grund zur Untergewichtung, sobald
# das Gross (nicht die Vol) bindet.
INTERNAL_GROSS = {"XSR": 2.0, "DTRD": 1.0, "EOMT": 1.0, "MERGARB": 1.0}

# Aktuelle Live-Multiplikatoren, aus dem Code gelesen:
#   XSR  : xsr_live.SLEEVE_ALLOC 0.40/Seite → 0.80 Gross → m = 0.80/2.0 = 0.40
#   EOMT : registry.py alloc 0.20 → m = 0.20
#   DTRD : registry.py alloc 0.15 → m = 0.15
#   MERGARB: promoted.yaml alloc 0.12 → m = 0.12
CURRENT_M = {"XSR": 0.40, "EOMT": 0.20, "DTRD": 0.15, "MERGARB": 0.12}


def load_full_series() -> dict[str, pd.Series]:
    """Volle Historien je Sleeve — bewusst NICHT sleeve_health's Kurzfenster."""
    import os

    from quant.config import STAGING_DIR
    out: dict[str, pd.Series] = {}

    # XSR: der walk-forward OOS-Record (2003+), nicht die Live-Rekonstruktion
    p = os.path.join(STAGING_DIR, "sim_wf_v2_full.parquet")
    if os.path.exists(p):
        s = pd.read_parquet(p)["net_ret"]
        s.index = pd.to_datetime(s.index)
        out["XSR"] = s.dropna()

    # DTRD: volle Studie (2004+), nicht auf 3 Jahre gekürzt
    try:
        from quant.research.dtrd_study import load, sleeve
        out["DTRD"] = sleeve(load(), 126).dropna()
    except Exception as e:  # noqa: BLE001
        print(f"  DTRD nicht ladbar: {e}")

    # EOMT: volle Historie statt der 6-Jahres-Kürzung im Monitor
    try:
        from quant.data.bq import query
        from quant.research.eomt_study import COST, month_end_returns
        df = query("""
          SELECT date, symbol, adjusted_close AS ac
          FROM `trading-436516.quant.eod_bars`
          WHERE symbol IN ('IEF','TLT','EDV') AND adjusted_close > 0
          ORDER BY date""")
        df["date"] = pd.to_datetime(df["date"])
        px = df.pivot(index="date", columns="symbol", values="ac").sort_index()
        me = month_end_returns(px, 5)
        cols = [c for c in ("IEF", "TLT", "EDV") if c in me]
        out["EOMT"] = (me[cols].mean(axis=1) - COST).dropna()
    except Exception as e:  # noqa: BLE001
        print(f"  EOMT nicht ladbar: {e}")

    # MERGARB: volle Deal-Historie
    try:
        from quant.research.mergarb_study import returns as mergarb_returns
        out["MERGARB"] = mergarb_returns().dropna()
    except Exception as e:  # noqa: BLE001
        print(f"  MERGARB nicht ladbar: {e}")

    return out


def to_monthly(r: pd.Series) -> pd.Series:
    r = r.dropna()
    r.index = pd.to_datetime(r.index)
    if len(r) == 0:
        return r
    # EOMT ist bereits monatlich (eine Beobachtung je Monatsende) — resample
    # ist dort ein No-Op und damit unschädlich.
    return ((1 + r).resample("ME").prod() - 1).dropna()


def sleeve_stats(r: pd.Series, ann: int) -> dict:
    r = r.dropna()
    eq = (1 + r).cumprod()
    yrs = len(r) / ann
    return {
        "n": len(r), "jahre": yrs,
        "sharpe": float(r.mean() / r.std() * np.sqrt(ann)),
        "vol": float(r.std() * np.sqrt(ann)),
        "cagr": float(eq.iloc[-1] ** (1 / yrs) - 1) if yrs > 0 else np.nan,
        "maxdd": float((eq / eq.cummax() - 1).min()),
    }


def corr_with_counts(M: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """ρ auf Monatsrenditen; unter MIN_OVERLAP wird NICHT geschätzt (NaN),
    damit ein Prior nicht als Messung durchgeht (die Falle im ersten Versuch
    dieser Analyse: XSRs ρ waren alle exakt 0.20 = der Prior)."""
    names = list(M.columns)
    C = pd.DataFrame(np.eye(len(names)), index=names, columns=names)
    N = pd.DataFrame(0, index=names, columns=names, dtype=int)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            j = M[[a, b]].dropna()
            N.loc[a, b] = N.loc[b, a] = len(j)
            rho = float(j.corr().iloc[0, 1]) if len(j) >= MIN_OVERLAP else np.nan
            C.loc[a, b] = C.loc[b, a] = rho
    return C, N


def _shrunk(C: np.ndarray, fill: float) -> np.ndarray:
    C = np.where(np.isnan(C), fill, C)
    np.fill_diagonal(C, 1.0)
    return (1 - SHRINK) * C + SHRINK * np.eye(len(C))


def weights_current(names, vols, C) -> np.ndarray:
    w = np.array([CURRENT_M.get(n, 0.0) for n in names], float)
    return w / w.sum()


def scale_to_gross(names, w: np.ndarray, gross_budget: float) -> np.ndarray:
    """Normierte Gewichte → Multiplikatoren m, die das Gross-Budget ausschöpfen.

    m = w · k mit k so, dass Σ m_i·internes_gross_i = gross_budget. Schemata,
    die bilanzsparsame Sleeves bevorzugen, bekommen dadurch automatisch ein
    größeres k — das ist der Punkt: unter einer GROSS-Restriktion (Reg-T) ist
    Gross-Effizienz Teil der Optimierung, nicht ein Nachgedanke.
    """
    ig = np.array([INTERNAL_GROSS[n] for n in names], float)
    cost = float((w * ig).sum())
    k = gross_budget / cost if cost > 0 else 0.0
    return w * k


def weights_inverse_vol(names, vols, C) -> np.ndarray:
    w = 1.0 / np.asarray(vols, float)
    return w / w.sum()


def weights_erc(names, vols, C) -> np.ndarray:
    """Equal Risk Contribution, iterativ. Braucht KEINE Sharpe-Schätzung."""
    n = len(names)
    S = np.outer(vols, vols) * C
    w = np.ones(n) / n
    for _ in range(2000):
        mrc = S @ w                       # marginal risk contribution
        rc = w * mrc                      # risk contribution
        target = rc.mean()
        w = w * (target / np.maximum(rc, 1e-12)) ** 0.1
        w = np.clip(w, 1e-6, None)
        w = w / w.sum()
    return w


def weights_mean_variance(names, vols, C, sharpes) -> np.ndarray:
    """w ∝ D⁻¹ C⁻¹ s, long-only (negative auf 0), normiert."""
    Cinv = np.linalg.inv(C)
    v = Cinv @ np.asarray(sharpes, float)
    w = v / np.asarray(vols, float)
    w = np.clip(w, 0.0, None)
    if w.sum() <= 0:
        return np.ones(len(names)) / len(names)
    return w / w.sum()


def portfolio_series(M: pd.DataFrame, m: np.ndarray) -> pd.Series:
    """Monatsrendite = Σ m_i·r_i auf dem VOLLSTÄNDIGEN Panel (complete case).

    Bewusst keine Renormierung fehlender Sleeves: m ist ein absoluter
    Multiplikator, keine Anteilsquote — Renormieren würde die Skala zerstören
    und damit genau die Hebelfrage verfälschen, um die es hier geht. Statt
    dessen werden nur Monate benutzt, in denen ALLE Sleeves Daten haben; das
    vermeidet zugleich, "Daten fehlen" mit "Sleeve war flat" zu verwechseln.
    """
    Mc = M.dropna()
    return (Mc * m).sum(axis=1)


def report_scheme(label: str, names: list, M: pd.DataFrame, w: np.ndarray,
                  gross: float) -> dict:
    m = scale_to_gross(names, w, gross)
    r = portfolio_series(M, m)
    st = sleeve_stats(r, 12)
    cagr = st["sharpe"] * st["vol"] * LIVE_HAIRCUT
    print(f"  {label:22s} Sharpe {st['sharpe']:5.2f}  Vol {st['vol']:6.1%}  "
          f"MaxDD {st['maxdd']:7.1%}  CAGR {cagr:+6.1%}  |  m = " +
          " ".join(f"{n}:{x:.2f}" for n, x in zip(names, m)))
    return {"label": label, **st, "cagr": cagr, "m": m}


def run():
    print("═══ VOLLE SLEEVE-HISTORIEN (nicht die Monitor-Kurzfenster) ═══")
    series = load_full_series()
    ANN_MAP = {"XSR": 252, "DTRD": 252, "MERGARB": 252, "EOMT": 12}
    rows = []
    for nm in sorted(series):
        st = sleeve_stats(series[nm], ANN_MAP[nm])
        rows.append({"sleeve": nm, "freq": f"1/{ANN_MAP[nm]}", **st})
    tab = pd.DataFrame(rows).set_index("sleeve")
    print(tab.to_string(formatters={
        "jahre": "{:.1f}".format, "sharpe": "{:.2f}".format,
        "vol": "{:.1%}".format, "cagr": "{:+.1%}".format,
        "maxdd": "{:.1%}".format}))

    M = pd.DataFrame({nm: to_monthly(series[nm]) for nm in sorted(series)})
    names = list(M.columns)
    print(f"\nMonatspanel: {M.index.min().date()} → {M.index.max().date()}, "
          f"{len(M)} Monate")
    print("Beobachtungen je Sleeve: " +
          "  ".join(f"{n} {int(M[n].notna().sum())}" for n in names))

    C, N = corr_with_counts(M)
    print(f"\n═══ KORRELATIONEN (Monatsbasis; NaN = < {MIN_OVERLAP} "
          f"gemeinsame Monate, NICHT geschätzt) ═══")
    print(C.to_string(float_format=lambda x: f"{x:.2f}"))
    print("gemeinsame Monate:")
    print(N.to_string())
    off = C.values[np.triu_indices(len(names), 1)]
    measured = off[~np.isnan(off)]
    fill = float(measured.mean()) if len(measured) else 0.20
    if np.isnan(off).any():
        print(f"→ {int(np.isnan(off).sum())} Paare zu dünn; für die "
              f"Optimierung mit dem MITTEL der gemessenen ρ ({fill:.2f}) "
              f"gefüllt — als Prior deklariert, nicht als Messung.")

    vols = np.array([M[n].std() * np.sqrt(12) for n in names])
    sharpes = np.array([M[n].mean() / M[n].std() * np.sqrt(12) for n in names])
    Cs = _shrunk(C.values.copy(), fill)
    ig = np.array([INTERNAL_GROSS[n] for n in names], float)

    cur_m = np.array([CURRENT_M[n] for n in names])
    cur_gross = float((cur_m * ig).sum())
    print(f"\nAktuelles Konto-Gross: {cur_gross:.2f}x von {REGT_MAX}x Reg-T "
          f"→ {REGT_MAX - cur_gross:.2f}x ungenutzt")

    print("\n═══ GEWICHTUNGSSCHEMATA (normiert auf Summe 1, VOR Gross-Skalierung) ═══")
    schemes = {
        "aktuell (live)": weights_current(names, vols, Cs),
        "inverse Vol": weights_inverse_vol(names, vols, Cs),
        "ERC/Risk-Parity": weights_erc(names, vols, Cs),
        "Mean-Variance": weights_mean_variance(names, vols, Cs, sharpes),
    }
    print("  " + f"{'Schema':22s}" + "".join(f"{n:>10s}" for n in names))
    for lab, w in schemes.items():
        print(f"  {lab:22s}" + "".join(f"{x:9.1%} " for x in w))
    print("  " + f"{'(Vol per 1x Gross)':22s}" +
          "".join(f"{v/g:9.1%} " for v, g in zip(vols, ig)))
    print("  " + f"{'(Sharpe)':22s}" + "".join(f"{s:9.2f} " for s in sharpes))
    print("  " + f"{'(internes Gross)':22s}" + "".join(f"{g:9.1f} " for g in ig))

    print(f"\n═══ IN-SAMPLE, alle auf Reg-T {REGT_MAX}x Gross skaliert "
          f"(optimistisch — Gewichte auf DIESEN Daten geschätzt) ═══")
    for lab, w in schemes.items():
        report_scheme(lab, names, M, w, REGT_MAX)
    print(f"\n  Vergleich: aktuelle Allokation bei ihrem HEUTIGEN Gross "
          f"({cur_gross:.2f}x), nicht hochskaliert:")
    report_scheme("aktuell @heute", names, M, schemes["aktuell (live)"],
                  cur_gross)

    # ── Der entscheidende Test: Split-Sample ────────────────────────────────
    print("\n═══ SPLIT-SAMPLE (Gewichte auf 1. Hälfte, bewertet auf 2.) ═══")
    print("Nur das sagt, ob die Umverteilung echt ist oder Kurvenanpassung.")
    mid = len(M) // 2
    A, B = M.iloc[:mid], M.iloc[mid:]
    # Nur Sleeves, die in BEIDEN Hälften genug Daten haben
    usable = [n for n in names
              if A[n].notna().sum() >= 24 and B[n].notna().sum() >= 12]
    dropped = [n for n in names if n not in usable]
    if dropped:
        print(f"  ausgelassen (zu wenig Daten in einer Hälfte): {dropped}")
    if len(usable) < 2:
        print("  → Split-Sample nicht rechenbar: zu wenige Sleeves mit "
              "Historie in beiden Hälften. Das IST das Ergebnis — die "
              "Umverteilung ist auf diesen Daten nicht validierbar.")
        return
    A2, B2 = A[usable], B[usable]
    print(f"  1. Hälfte {A2.index.min().date()}→{A2.index.max().date()} "
          f"({len(A2)} M) | 2. Hälfte {B2.index.min().date()}→"
          f"{B2.index.max().date()} ({len(B2)} M) | Sleeves: {usable}")
    CA, NA = corr_with_counts(A2)
    offA = CA.values[np.triu_indices(len(usable), 1)]
    measA = offA[~np.isnan(offA)]
    fillA = float(measA.mean()) if len(measA) else 0.20
    CsA = _shrunk(CA.values.copy(), fillA)
    volsA = np.array([A2[n].std() * np.sqrt(12) for n in usable])
    shA = np.array([A2[n].mean() / A2[n].std() * np.sqrt(12) for n in usable])
    schemesA = {
        "aktuell (live)": weights_current(usable, volsA, CsA),
        "inverse Vol": weights_inverse_vol(usable, volsA, CsA),
        "ERC/Risk-Parity": weights_erc(usable, volsA, CsA),
        "Mean-Variance": weights_mean_variance(usable, volsA, CsA, shA),
    }
    print("  Gewichte aus der 1. Hälfte:")
    print("    " + f"{'Schema':22s}" + "".join(f"{n:>10s}" for n in usable))
    for lab, w in schemesA.items():
        print(f"    {lab:22s}" + "".join(f"{x:9.1%} " for x in w))
    print(f"  Ergebnis auf der 2. Hälfte (out-of-sample), alle auf "
          f"{REGT_MAX}x Gross:")
    oos = [report_scheme(lab, usable, B2, w, REGT_MAX)
           for lab, w in schemesA.items()]
    base = next(o for o in oos if o["label"] == "aktuell (live)")
    print("\n  ΔCAGR gegen die aktuelle Allokation, beide auf gleichem Gross")
    print("  (isoliert die UMGEWICHTUNG vom Hochskalieren):")
    for o in oos:
        if o["label"] == "aktuell (live)":
            continue
        d = o["cagr"] - base["cagr"]
        verdict = "BESSER" if d > 0.01 else ("neutral" if d > -0.01 else "SCHLECHTER")
        print(f"    {o['label']:22s} {d:+6.1%}  → {verdict}")

    print("\n  MERKE: dies ist ein Konstruktions-, kein Alpha-Ergebnis. Es "
          "verschiebt vorhandenes Risiko,\n  erzeugt keine neue Renditequelle "
          "— und ist damit NICHT von der S̄/√ρ̄-Decke betroffen.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    a = p.parse_args()
    if not a.run:
        p.print_help()
        sys.exit(1)
    run()
