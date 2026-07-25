"""Ehrliche Portfolio-Erwartung aus den echten Sleeve-Renditereihen.

    python3 -m quant.research.portfolio_math

Ersetzt die Faustformel S̄·√(N/(1+(N−1)ρ̄)) durch die volle, geschrumpfte
Korrelationsmatrix. Drei Fallen, in die der erste Versuch dieses Moduls
gelaufen ist und die hier bewusst geschlossen sind:

1. FREQUENZ. EOMT ist eine MONATSreihe (73 Beobachtungen). Mit √252
   annualisiert ergab sie Sharpe 2.36 statt 0.52 — Faktor √(252/12) = 4.6 —
   und dominierte damit das ganze Portfolio-Ergebnis. Jeder Sleeve wird jetzt
   mit SEINER Frequenz annualisiert (`ANN`).
2. ÜBERLAPPUNG. Tages-ρ zwischen Reihen mit 22 gemeinsamen Beobachtungen ist
   Rauschen. Korrelationen werden auf MONATSrenditen geschätzt (gemeinsamer
   Nenner) und nur genutzt, wenn ≥ MIN_OVERLAP Monate vorliegen; sonst gilt
   ein konservativer Prior. Negative ρ werden auf 0 gekappt — ein
   Diversifikationsgeschenk, das man nicht verdient hat.
3. FENSTERWAHL. Die Reihen aus `sleeve_health` sind Rekonstruktionen der
   letzten ~3 Jahre für die Drift-Erkennung. Als Prognose taugen sie nicht
   (ONX zeigt dort 0.96 statt der validierten 0.55). Eingang sind deshalb die
   VALIDIERTEN Kennzahlen aus `BASELINES`, die Rekonstruktionen nur als
   Gegenprobe mit ausgewiesener Stichprobengröße.
"""

import numpy as np
import pandas as pd

from quant.ops.sleeve_health import BASELINES
from quant.research.discovery import (ANN, MIN_OVERLAP, RHO_PRIOR, SHRINK,
                                      combined_sharpe, corr_matrix,
                                      live_sleeve_returns, sharpe_of)

REGT_MAX = 1.9          # Overnight-Hebel, unter dem 2x-Limit
LIVE_HAIRCUT = 0.75     # gemessene Live-Kosten vs. Backtest-Annahme


def main():
    rets = live_sleeve_returns()
    names = sorted(rets)

    print("═══ GEGENPROBE: Rekonstruktionen des Health-Monitors ═══")
    print("(kurze Fenster, NUR zur Drift-Erkennung — nicht als Prognose)")
    rows = []
    for nm in names:
        r = rets[nm].dropna()
        a = ANN.get(nm, 252)
        rows.append({"sleeve": nm, "freq": f"1/{a}", "n": len(r),
                     "jahre": len(r) / a,
                     "sharpe_fenster": r.mean() / r.std() * np.sqrt(a),
                     "sharpe_validiert": BASELINES[nm]["sharpe"],
                     "sharpe_2022_val": BASELINES[nm]["regime_2022"]})
    tab = pd.DataFrame(rows).set_index("sleeve")
    print(tab.to_string(formatters={
        "jahre": "{:.1f}".format, "sharpe_fenster": "{:.2f}".format,
        "sharpe_validiert": "{:.2f}".format, "sharpe_2022_val": "{:.2f}".format}))

    C, N = corr_matrix(rets)
    print(f"\n═══ KORRELATIONEN auf MONATSrenditen (Prior {RHO_PRIOR:.2f} "
          f"bei < {MIN_OVERLAP} gemeinsamen Monaten) ═══")
    print(C.to_string(float_format=lambda x: f"{x:.2f}"))
    print("gemeinsame Monate:")
    print(N.to_string())
    thin = [(a, b) for i, a in enumerate(C.index) for b in C.index[i + 1:]
            if N.loc[a, b] < MIN_OVERLAP]
    if thin:
        print(f"→ Prior benutzt für {len(thin)} Paare: "
              + ", ".join(f"{a}/{b}" for a, b in thin))

    c = C.loc[names, names].values
    s_full = np.array([BASELINES[nm]["sharpe"] for nm in names])
    s_now = np.array([BASELINES[nm]["regime_2022"] for nm in names])

    print(f"\n═══ KOMBINIERTER SHARPE — S_p = √(sᵀC⁻¹s), long-only, "
          f"Schrumpfung {SHRINK:.0%} ═══")
    for lab, s in (("Voll-Sample (validiert)", s_full),
                   ("aktuelles Regime 2022+ (Regel R4 — das prognostiziert)",
                    s_now)):
        sp = combined_sharpe(s, c)
        contrib = {}
        for i, nm in enumerate(names):
            k = [j for j in range(len(names)) if j != i]
            contrib[nm] = sp - combined_sharpe(s[k], c[np.ix_(k, k)])
        print(f"\n  {lab}: S_p = {sp:.2f}")
        print("    Sharpes: " + "  ".join(f"{nm} {v:.2f}"
                                          for nm, v in zip(names, s)))
        print("    Beitrag: " + "  ".join(
            f"{nm} {v:+.3f}" for nm, v in
            sorted(contrib.items(), key=lambda x: -x[1])))

    sp = combined_sharpe(s_now, c)
    print("\n═══ RENDITEERWARTUNG im aktuellen Regime ═══")
    print("  (CAGR ≈ S_p × Portfolio-Vol × Hebel, dann Live-Haircut)")
    for vol in (0.10, 0.15, 0.20):
        line = f"  Vol-Target {vol:>4.0%}:"
        for lev, lab in ((1.0, "1.0x"), (1.5, "1.5x"), (REGT_MAX, "1.9x")):
            g = sp * vol * lev
            line += f"   {lab} {g*LIVE_HAIRCUT:+5.1%}"
        print(line + "   (nach Haircut)")
    # ── Warum die Kelly-Grenze hier NICHT die Antwort ist ────────────────────
    # S_p²/2 ist das Wachstumsmaximum bei FREI WÄHLBAREM Hebel. Der dafür
    # nötige Hebel ist S_p/σ — bei S_p=1.03 und 15 % Vol sind das 6.9x. Reg-T
    # erlaubt 1.9x über Nacht. Die Kelly-Zahl gegen ein Renditeziel zu halten
    # (wie im ersten Entwurf dieses Moduls) suggeriert Erreichbarkeit, wo die
    # Bilanzrestriktion längst bindet.
    vol_unlev = 0.15
    kelly_lev = sp / vol_unlev
    print(f"\n  Voll-Kelly-Obergrenze bei S_p={sp:.2f}: {sp**2/2:+.1%}/Jahr — "
          f"aber der dafür nötige Hebel ist {kelly_lev:.1f}x bei "
          f"{vol_unlev:.0%} Vol; Reg-T erlaubt {REGT_MAX:.1f}x über Nacht.")
    erreichbar = sp * vol_unlev * REGT_MAX * LIVE_HAIRCUT
    print(f"  ERREICHBAR unter Reg-T: {erreichbar:+.1%}/Jahr "
          f"({sp:.2f} × {vol_unlev:.0%} × {REGT_MAX:.1f}x × "
          f"{LIVE_HAIRCUT:.0%} Haircut)")
    lev_50 = 0.50 / (sp * vol_unlev * LIVE_HAIRCUT)
    print(f"  Für 50 %/Jahr nötiger Hebel bei heutigem S_p: {lev_50:.1f}x "
          f"→ {lev_50/REGT_MAX:.1f}x über dem Reg-T-Limit")

    # ── Die eigentliche Obergrenze: S̄/√ρ̄ ───────────────────────────────────
    n_now = len(names)
    s_bar = float(s_now.mean())
    rho_bar = float(c[np.triu_indices(n_now, 1)].mean())
    ceiling = s_bar / np.sqrt(rho_bar) if rho_bar > 0 else np.inf
    print(f"\n  ═ ASYMPTOTISCHE OBERGRENZE ═")
    print(f"  Für N→∞ gilt S_p → S̄/√ρ̄ = {s_bar:.2f}/√{rho_bar:.2f} "
          f"= {ceiling:.2f}. Mehr Sleeves DERSELBEN Qualität und Korrelation "
          f"bringen darüber hinaus nichts.")
    print(f"  Daraus folgt eine Renditegrenze von "
          f"{ceiling*vol_unlev*REGT_MAX*LIVE_HAIRCUT:+.1%}/Jahr unter Reg-T — "
          f"unabhängig von der Zahl der Sleeves.")
    s_need = 0.50 / (vol_unlev * REGT_MAX * LIVE_HAIRCUT)
    print(f"  50 %/Jahr braucht S_p ≥ {s_need:.2f}. Das ist bei ρ̄={rho_bar:.2f} "
          f"nur mit Ø-Sharpe ≥ {s_need*np.sqrt(rho_bar):.2f} erreichbar "
          f"(aktuell {s_bar:.2f}) …")
    rho_need = (s_bar / s_need) ** 2
    print(f"  … oder bei heutigem Ø-Sharpe {s_bar:.2f} nur mit "
          f"ρ̄ ≤ {rho_need:.3f} (aktuell {rho_bar:.2f}).")
    for n_add in (5, 10, 20, 50):
        n_tot = n_now + n_add
        sp_n = s_bar * np.sqrt(n_tot / (1 + (n_tot - 1) * rho_bar))
        print(f"    +{n_add:2d} gleichwertige Sleeves (N={n_tot:2d}): "
              f"S_p {sp_n:.2f} → {sp_n*vol_unlev*REGT_MAX*LIVE_HAIRCUT:+.1%}/Jahr")

    print("\n═══ DSR MIT KORRIGIERTEM VERSUCHSZÄHLER ═══")
    from quant.research.trials_registry import (deflated_sharpe, family_trials,
                                                family_sharpe_var,
                                                program_hit_rate, trial_stats)
    n_glob, glob_var = trial_stats()
    print(f"{'Familie':10s} {'Sharpe':>7s} {'n_obs':>6s} {'n_fam':>6s} "
          f"{'DSR_fam':>8s} {'SR*_fam':>8s} {'DSR_glob':>9s}")
    for nm in names:
        r = rets[nm].dropna()
        a = ANN.get(nm, 252)
        sh = BASELINES[nm]["sharpe"]
        nf = max(family_trials(nm), 1)
        fv = family_sharpe_var(nm) or glob_var
        kw = dict(n_obs=len(r), skew=float(r.skew()),
                  kurt=float(r.kurtosis() + 3), sharpe_var_trials=fv, ann=a)
        d_f = deflated_sharpe(sh, n_trials=nf, **kw)
        d_g = deflated_sharpe(sh, n_trials=max(n_glob, 1), **kw)
        print(f"{nm:10s} {sh:7.2f} {len(r):6d} {nf:6d} {d_f['dsr']:8.3f} "
              f"{d_f['sr_star']:8.2f} {d_g['dsr']:9.3f}")
    val, tested = program_hit_rate()
    print(f"\n  Programm-Selektionseffekt (separat, NICHT im DSR): "
          f"{val}/{tested} Familien mit Kandidatenstatus, "
          f"{n_glob} Versuche protokolliert")


if __name__ == "__main__":
    main()
