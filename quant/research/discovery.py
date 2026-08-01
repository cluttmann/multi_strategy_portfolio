"""Discovery-Pipeline — Hypothese → Gates → Verdikt → (bei Bestehen) Live.

    python3 -m quant.research.discovery --queue        # Warteschlange zeigen
    python3 -m quant.research.discovery --run <id>    # einen Kandidaten testen
    python3 -m quant.research.discovery --run-all     # alle offenen testen

ARCHITEKTUR (nach FactFin/XALPHA, nicht nach TradingAgents): Die Hypothese
darf von einem LLM kommen, aber die WAHRHEIT sprechen ausschließlich diese
Gates. Der Generator ist nie Schiedsrichter über seine eigene Idee.

GATES, in dieser Reihenfolge (Abbruch beim ersten Fehlschlag — billige Gates
zuerst, damit teure Rechenzeit nur für Überlebende anfällt):
  G0  DATEN-EIGNUNG: Ist der Mechanismus aus vorhandenen Tabellen berechenbar?
      (XALPHA-Lektion: killt den Großteil der Verschwendung vor dem ersten Backtest)
  G1  MECHANISMUS: ökonomische Begründung + quantitative Vorhersage vorhanden?
      (Suchregel R2 — ohne testbare Skalierung ist es Data-Mining)
  G2  KOSTEN-SCREEN: Brutto-Edge/Trade > 2× realistische Kosten?
      (Suchregel R1 — hier starben CAT/IMOM/GAP/PEAD)
  G3  HOLDOUT: letzte 5 Jahre nie für Parameterwahl benutzt, Sharpe > 0.3?
  G4  REGIME: 2022+ separat ausgewiesen, nicht negativ
  G5  DEFLATED SHARPE > 0.95 bei ehrlicher Versuchszahl (vorregistrierte,
      enge Variantenmenge — Suchregel R6)
  G6  LIQUIDITÄTSTIER: Edge lebt nicht nur im untersten Tier (Suchregel R3)
  G7  ORTHOGONALITÄT: ρ zu allen Live-Sleeves; erwarteter Portfolio-Beitrag
      ΔS_p ≥ 0.03 (Suchregel R5)
Bestehen alle → Eintrag ins Sleeve-Register (= automatisch live).
"""

import argparse
import sys

import numpy as np
import pandas as pd
import yaml

QUEUE_PATH = "quant/research/hypothesis_queue.yaml"
KILL_PATH = "quant/research/kill_registry.yaml"


def load_queue() -> list[dict]:
    with open(QUEUE_PATH) as f:
        return yaml.safe_load(f).get("kandidaten", [])


def load_rules() -> dict:
    with open(KILL_PATH) as f:
        return yaml.safe_load(f)


def live_sleeve_returns() -> dict[str, pd.Series]:
    """Renditereihen aller Live-Sleeves für die Orthogonalitätsprüfung."""
    import os
    from quant.config import STAGING_DIR
    from quant.ops import sleeve_health as sh
    out = {}
    try:
        sim = pd.read_parquet(os.path.join(STAGING_DIR,
                                           "sim_wf_v2_full.parquet"))
        s = sim["net_ret"]; s.index = pd.to_datetime(s.index)
        out["XSR"] = s
    except Exception:  # noqa: BLE001
        pass
    for nm, fn in (("ONX", sh.onx_returns), ("VOLC", sh.volc_returns),
                   ("EOMT", sh.eomt_returns), ("DTRD", sh.dtrd_returns)):
        try:
            r = fn()
            if len(r):
                out[nm] = r
        except Exception:  # noqa: BLE001
            pass
    return out


SHRINK = 0.30          # Ledoit-Wolf-artige Schrumpfung der Korrelationsmatrix
SHARPE_CAP = 1.50      # Einzelschätzer darf die Kombination nicht dominieren


def combined_sharpe(sharpes: np.ndarray, corr: np.ndarray) -> float:
    """Sharpe der optimalen Kombination: S_p = √(sᵀ C⁻¹ s), long-only.

    WARUM NICHT die Durchschnittsformel S̄·√(N/(1+(N−1)ρ̄)): die setzt
    GLEICHGEWICHTUNG und einen EINHEITLICHEN ρ voraus. Dadurch senkt jeder
    Sleeve mit unterdurchschnittlichem Sharpe das Ergebnis — auch ein völlig
    unkorrelierter, der real hilft. Das hat in dieser Session ACT13D (ρ≈0,
    Sharpe 0.50) ein negatives ΔS_p gegeben, was mathematisch nicht sein kann.
    Die quadratische Form ist monoton: eine zusätzliche Spalte kann S_p nie
    senken. Geschrumpft, weil geschätzte Korrelationen sonst überoptimieren.
    """
    n = len(sharpes)
    if n == 0:
        return 0.0
    s = np.clip(np.asarray(sharpes, float), -SHARPE_CAP, SHARPE_CAP)
    c = (1 - SHRINK) * np.asarray(corr, float) + SHRINK * np.eye(n)
    try:
        w = np.linalg.solve(c, s)
    except np.linalg.LinAlgError:
        return float(np.sqrt(np.maximum((s ** 2).sum() / n, 0)))
    # Long-only: negative Gewichte einmal ausschließen und neu lösen — wir
    # würden einen eigenen Sleeve nicht shorten.
    if (w < 0).any():
        keep = w > 0
        if not keep.any():
            return float(max(s.max(), 0.0))
        s, c = s[keep], c[np.ix_(keep, keep)]
        try:
            w = np.linalg.solve(c, s)
        except np.linalg.LinAlgError:
            return float(max(s.max(), 0.0))
    return float(np.sqrt(max(float(s @ w), 0.0)))


# Beobachtungsfrequenz je Sleeve. EOMT ist eine MONATSreihe — mit √252
# annualisiert ergibt sie Sharpe 2.36 statt 0.52 und dominiert das ganze
# Portfolio-Ergebnis. Jede Sharpe-Rechnung hier nutzt SEINE Frequenz.
ANN = {"XSR": 252, "ONX": 252, "VOLC": 252, "EOMT": 12, "DTRD": 252,
       "CTREND": 365}
MIN_OVERLAP = 36        # Monate, unter denen ρ nicht geschätzt wird
RHO_PRIOR = 0.20        # konservativer Ersatz bei zu kurzer Überlappung


def sharpe_of(r: pd.Series, name: str | None = None, ann: int | None = None
              ) -> float:
    r = r.dropna()
    a = ann or ANN.get(name or "", 252)
    return float(r.mean() / r.std() * np.sqrt(a)) if len(r) > 2 else 0.0


def monthly(r: pd.Series) -> pd.Series:
    """Auf Monatsrenditen verdichten — gemeinsamer Nenner für Korrelationen."""
    r = r.dropna()
    if len(r) == 0:
        return r
    return (1 + r).resample("ME").prod() - 1


def corr_matrix(rets: dict[str, pd.Series]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """ρ auf MONATSrenditen. Tages-ρ zwischen Reihen mit 22 gemeinsamen Tagen
    ist Rauschen; negative ρ werden auf 0 gekappt (ein Diversifikationsgeschenk,
    das man nicht verdient hat)."""
    m = {nm: monthly(r) for nm, r in rets.items()}
    names = sorted(m)
    C = pd.DataFrame(np.eye(len(names)), index=names, columns=names)
    N = pd.DataFrame(0, index=names, columns=names)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            j = pd.DataFrame({"a": m[a], "b": m[b]}).dropna()
            N.loc[a, b] = N.loc[b, a] = len(j)
            rho = (max(float(j.corr().iloc[0, 1]), 0.0)
                   if len(j) >= MIN_OVERLAP else RHO_PRIOR)
            C.loc[a, b] = C.loc[b, a] = rho
    return C, N


def portfolio_delta(new: pd.Series, existing: dict[str, pd.Series],
                    sharpe_new: float, ann=252) -> tuple[float, float, dict]:
    """ΔS_p durch Aufnahme des Kandidaten — volle Korrelationsmatrix auf
    Monatsbasis, Sharpes je Sleeve mit dessen eigener Frequenz."""
    names = sorted(existing)
    if not names:
        return sharpe_new, sharpe_new, {}
    sharpes = np.array([sharpe_of(existing[nm], nm) for nm in names])
    C, _ = corr_matrix(existing)
    before = combined_sharpe(sharpes, C.loc[names, names].values)

    with_new = {**existing, "__neu__": new}
    C2, N2 = corr_matrix(with_new)
    rhos = {nm: float(C2.loc["__neu__", nm]) for nm in names}
    order = names + ["__neu__"]
    after = combined_sharpe(np.append(sharpes, sharpe_new),
                            C2.loc[order, order].values)
    return before, after, rhos


def evaluate(cand: dict, verbose=True) -> dict:
    """Führt die Gates für einen Kandidaten aus. Erwartet, dass das Modul
    des Kandidaten eine Funktion `returns(**params) -> pd.Series` anbietet."""
    import importlib
    res = {"id": cand["id"], "gates": {}, "verdikt": "OFFEN"}

    def fail(gate, msg):
        res["gates"][gate] = f"FAIL — {msg}"
        res["verdikt"] = "VERWORFEN"
        if verbose:
            print(f"  {gate}: FAIL — {msg}")
        return res

    def ok(gate, msg=""):
        res["gates"][gate] = f"PASS {msg}".strip()
        if verbose:
            print(f"  {gate}: PASS {msg}")

    # G0 Daten-Eignung
    from quant.data.bq import query
    missing = []
    for t in cand.get("braucht_tabellen", []):
        try:
            query(f"SELECT 1 FROM `trading-436516.quant.{t}` LIMIT 1")
        except Exception:  # noqa: BLE001
            missing.append(t)
    if missing:
        return fail("G0-Daten", f"Tabellen fehlen: {missing}")
    ok("G0-Daten", f"{len(cand.get('braucht_tabellen', []))} Tabellen vorhanden")

    # G1 Mechanismus
    if not cand.get("mechanismus") or not cand.get("quantitative_vorhersage"):
        return fail("G1-Mechanismus",
                    "ohne Mechanismus + quantitative Vorhersage (Regel R2)")
    ok("G1-Mechanismus")

    # Renditereihe berechnen
    mod_name, fn_name = cand["implementierung"].rsplit(".", 1)
    try:
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, fn_name)
    except Exception as e:  # noqa: BLE001
        return fail("Impl", f"{cand['implementierung']} nicht ladbar: {e}")
    try:
        r = fn(**(cand.get("params") or {}))
    except Exception as e:  # noqa: BLE001
        return fail("Impl", f"Ausführung fehlgeschlagen: {e}")
    if r is None or len(r.dropna()) < 250:
        return fail("Impl", "zu wenige Beobachtungen")
    r = r.dropna()
    r.index = pd.to_datetime(r.index)
    ann = int(cand.get("ann", 252))

    def stats(x):
        x = x.dropna()
        if len(x) < 20:
            return None
        eq = (1 + x).cumprod()
        return {"sharpe": x.mean() / x.std() * np.sqrt(ann),
                "cagr": eq.iloc[-1] ** (ann / len(x)) - 1,
                "maxdd": (eq / eq.cummax() - 1).min(), "n": len(x)}

    full = stats(r)
    res["sharpe_full"] = full["sharpe"]
    res["cagr_full"] = full["cagr"]

    # G2 Kosten-Screen (der Kandidat liefert seine Brutto-bp/Trade selbst)
    edge_bp = cand.get("brutto_bp_pro_trade")
    kosten_bp = cand.get("kosten_bp_pro_trade", 10.0)
    if edge_bp is not None and edge_bp < 2 * kosten_bp:
        return fail("G2-Kosten",
                    f"Brutto {edge_bp:.1f}bp < 2x Kosten ({2*kosten_bp:.1f}bp) — Regel R1")
    ok("G2-Kosten", f"Brutto {edge_bp}bp vs 2x Kosten {2*kosten_bp:.0f}bp"
       if edge_bp else "(Kosten im Backtest enthalten)")

    # G3 Holdout
    ho_start = cand.get("holdout_start", "2020-01-01")
    ho = stats(r.loc[ho_start:])
    if not ho:
        return fail("G3-Holdout", "kein Holdout-Fenster")
    res["sharpe_holdout"] = ho["sharpe"]
    if ho["sharpe"] < 0.3:
        return fail("G3-Holdout", f"Holdout-Sharpe {ho['sharpe']:.2f} < 0.30")
    ok("G3-Holdout", f"Sharpe {ho['sharpe']:.2f}, CAGR {ho['cagr']:+.1%}")

    # G4 Regime 2022+
    reg = stats(r.loc["2022-01-01":])
    res["sharpe_2022"] = reg["sharpe"] if reg else None
    if reg and reg["sharpe"] < 0:
        return fail("G4-Regime", f"2022+ Sharpe {reg['sharpe']:.2f} < 0")
    ok("G4-Regime", f"2022+ Sharpe {reg['sharpe']:.2f}" if reg else "(zu kurz)")

    # G5 Deflated Sharpe
    from quant.research.trials_registry import (deflated_sharpe,
                                                family_sharpe_var, trial_stats)
    n_trials, glob = trial_stats()
    fam_var = family_sharpe_var(cand["familie"]) or glob
    d = deflated_sharpe(full["sharpe"], full["n"], float(r.skew()),
                        float(r.kurtosis() + 3), n_trials, fam_var, ann)
    res["dsr"] = d["dsr"]
    if d["dsr"] <= 0.95:
        return fail("G5-DSR", f"DSR {d['dsr']:.3f} ≤ 0.95 (SR* {d['sr_star']:.2f})")
    ok("G5-DSR", f"DSR {d['dsr']:.3f} (SR* {d['sr_star']:.2f})")

    # G6 Liquiditätstier — vom Kandidaten bestätigt
    if not cand.get("liquiditaetstier_geprueft"):
        return fail("G6-Liquidität",
                    "Liquiditätstier nicht geprüft (Regel R3)")
    ok("G6-Liquidität")

    # G7 Orthogonalität + Portfolio-Beitrag
    existing = live_sleeve_returns()
    before, after, rhos = portfolio_delta(r, existing, full["sharpe"])
    res["rhos"] = rhos
    res["delta_sp"] = after - before
    if after - before < 0.03:
        return fail("G7-Portfolio",
                    f"ΔS_p {after-before:+.3f} < 0.03 "
                    f"(S_p {before:.2f}→{after:.2f}) — Regel R5")
    ok("G7-Portfolio", f"ΔS_p {after-before:+.3f} (S_p {before:.2f}→{after:.2f}), "
       f"max|ρ| {max(abs(v) for v in rhos.values()):.2f}" if rhos else "")

    # G8 Live-Pfad — ohne handelbares Signal gibt es keine Beförderung.
    # Das ist die Lehre aus VOLC/EOMT: "validiert" ohne Executor verdient nichts.
    ls = cand.get("live_signal")
    if not ls:
        return fail("G8-Livepfad", "keine `live_signal`-Funktion deklariert")
    try:
        lmod, lfn = ls.rsplit(".", 1)
        f = getattr(importlib.import_module(lmod), lfn)
        w, why = f()
        assert isinstance(w, dict) and isinstance(why, str)
        g = sum(abs(v) for v in w.values())
        assert g <= 1.001, f"Gross {g:.2f} > 1.0"
    except Exception as e:  # noqa: BLE001
        return fail("G8-Livepfad", f"{ls} nicht handelbar: {e}")
    ok("G8-Livepfad", f"{len(w)} Positionen, gross {g:.0%} — «{why}»")

    res["verdikt"] = "VALIDIERT"
    return res


def promote(cand: dict, res: dict) -> None:
    """Validierten Kandidaten in `promoted.yaml` eintragen → damit live.

    Der Eintrag ist absichtlich Daten und kein Code: `registry.py` liest die
    Datei beim Import, der generische Executor handelt jede Spec, und die
    Cloud-Scheduler iterieren das Register. Nach dem Push ist der Sleeve am
    nächsten Handelstag im Markt.
    """
    from quant.sleeves.registry import PROMOTED_PATH
    with open(PROMOTED_PATH) as f:
        doc = yaml.safe_load(f) or {}
    lst = doc.get("befoerdert") or []
    if any(e.get("name") == cand["sleeve_name"] for e in lst):
        print(f"  (schon befördert: {cand['sleeve_name']})")
        return
    lst.append({
        "name": cand["sleeve_name"],
        "beschreibung": cand["beschreibung"],
        "live_signal": cand["live_signal"],
        "alloc": float(cand.get("alloc", 0.10)),
        "freq": cand.get("freq", "daily"),
        "tif": cand.get("tif", "cls"),
        "ann": int(cand.get("ann", 252)),
        "tags": list(cand.get("tags") or []),
        "notes": cand.get("notes", ""),
        "metriken": {"sharpe_full": round(float(res["sharpe_full"]), 3),
                     "sharpe_now": round(float(res.get("sharpe_2022") or 0), 3),
                     "sharpe_holdout": round(float(res["sharpe_holdout"]), 3),
                     "dsr": round(float(res["dsr"]), 3),
                     "delta_sp": round(float(res["delta_sp"]), 3)},
    })
    doc["befoerdert"] = lst
    with open(PROMOTED_PATH, "w") as f:
        f.write("# Von der Discovery-Pipeline BEFÖRDERTE Sleeves — siehe "
                "Kopfkommentar in git-Historie.\n")
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
    print(f"  → BEFÖRDERT: {cand['sleeve_name']} @ "
          f"{cand.get('alloc', 0.10):.0%} Allokation, ist ab dem nächsten "
          f"Scheduler-Lauf live")


def run(cand_id: str | None = None, run_all=False, promote_enabled=True,
        notify_fn=None):
    """Kandidaten durch die Gates schicken.

    `promote_enabled=False` ist der Cloud-Modus: die Gates laufen, das Ergebnis
    wird gemeldet, aber der Eintrag ins Sleeve-Register bleibt ein Commit.
    Gründe: (1) der Container hat kein persistentes Dateisystem, ein Schreiben
    in promoted.yaml wäre nach der Execution weg; (2) eine neue Strategie ohne
    menschliche Prüfung scharf zu schalten, ist genau die Art irreversibler
    Aktion, die eine Freigabe braucht — auch auf einem Paper-Konto.
    """
    q = load_queue()
    if run_all:
        # Zurückgestellte/verworfene Kandidaten nicht jede Woche neu rechnen
        todo = [c for c in q
                if str(c.get("status", "offen")).startswith("offen")]
        if not todo:
            msg = ("Discovery: Warteschlange leer — keine 'offen'-Kandidaten "
                   "diese Woche zu testen")
            if notify_fn:
                notify_fn(msg)
            print(msg)
            return
    else:
        todo = [c for c in q if c["id"] == cand_id]
        if not todo:
            print(f"Kandidat '{cand_id}' nicht in der Warteschlange")
            return
    rules = load_rules()
    print(f"Kill-Registry: {len(rules['familien'])} gekillte Familien, "
          f"{len(rules['suchregeln'])} bindende Suchregeln\n")
    results = []
    for c in todo:
        print(f"═══ {c['id']}: {c['beschreibung']} ═══")
        r = evaluate(c)
        results.append(r)
        print(f"  → {r['verdikt']}\n")
    val = [r for r in results if r["verdikt"] == "VALIDIERT"]
    print(f"Ergebnis: {len(val)}/{len(results)} validiert")
    by_id = {c["id"]: c for c in todo}
    for r in val:
        print(f"  {r['id']}: Sharpe {r['sharpe_full']:.2f} "
              f"(Holdout {r['sharpe_holdout']:.2f}), ΔS_p {r['delta_sp']:+.3f}")
        if promote_enabled:
            promote(by_id[r["id"]], r)
        else:
            print("  (Melde-Modus — Beförderung braucht einen Commit)")
    if notify_fn:
        if val:
            notify_fn("DISCOVERY: " + " | ".join(
                f"{r['id']} besteht ALLE Gates — Sharpe {r['sharpe_full']:.2f}, "
                f"Holdout {r['sharpe_holdout']:.2f}, DSR {r['dsr']:.3f}, "
                f"ΔS_p {r['delta_sp']:+.3f}" for r in val)
                + " → Beförderung per Commit freigeben")
        elif results:
            gates = {r["id"]: next((k for k, v in r["gates"].items()
                                    if v.startswith("FAIL")), "?")
                     for r in results}
            notify_fn(f"Discovery: 0/{len(results)} bestanden ("
                      + ", ".join(f"{k}@{v}" for k, v in gates.items()) + ")")
    return results


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--queue", action="store_true")
    p.add_argument("--run")
    p.add_argument("--run-all", action="store_true")
    a = p.parse_args()
    if a.queue:
        for c in load_queue():
            print(f"{c['id']:14s} {c.get('status','offen'):10s} "
                  f"{c['beschreibung']}")
    elif a.run or a.run_all:
        run(a.run, a.run_all)
    else:
        p.print_help()
        sys.exit(1)
