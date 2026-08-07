"""MERGARB-Erweiterung: Aktientausch-Deals mit Käufer-Hedge (Phase 2 aus der
ursprünglichen MERGARB-Spec, hypothesis_queue.yaml: "Aktientauschangebote
brauchen ein Hedge-Bein gegen den Käufer").

    python3 -m quant.research.mergarb_swap_study --run

DATENPROBLEM (ehrlich offengelegt, nicht umgangen): die volle Käufer-/
Umtauschverhältnis-Extraktion aus Freitext bräuchte eine echte LLM-
Extraktion (in dieser Umgebung kein API-Key konfiguriert). Statt einer
Regex-Näherung mit unsicherem Namens-Matching wird hier NUR die Teilmenge
genutzt, bei der EDGAR beide Parteien schon eindeutig unter derselben
Accession mit unterschiedlichem Ticker führt (die "gemeinsame Filing"-
Mechanik aus merger_ingest.py — Käufer und Ziel reichen dieselbe 425/
DEFM14A gemeinsam ein). Das sind 113 von ~2.900 Aktientausch-Deals — ein
kleinerer, aber echter Test ohne Namens-Rateraten.

ZIEL/KÄUFER-ZUORDNUNG (Heuristik, offengelegt): ohne das exakte
Umtauschverhältnis kann man Ziel und Käufer nicht aus der Bilanzgröße
allein bestimmen (manche Fusionen sind "unter Gleichen", manche Ziele
sind größer als der Käufer). Verwendet wird die Ankündigungs-Fenster-
Rendite: das Ziel bekommt fast immer den größeren positiven Kurssprung
(Teil-Einpreisung der Übernahmeprämie), der Käufer bleibt flach oder fällt
leicht (Marktsorge um Überzahlung) — Standard-Mikrostruktur bei M&A-
Ankündigungen. Symbol mit der höheren 3-Tage-Rendite = Ziel.

MECHANISMUS: identisch zu MERGARB (Versicherungsprämie gegen Deal-Bruch-
Risiko), aber die Position ist Dollar-neutral long Ziel / short Käufer
statt long Ziel / Cash — das Umtauschverhältnis-Risiko wird gehedgt
(ohne die exakte Ratio: Dollar-neutral ist eine Näherung an die
ratio-neutrale Standardkonstruktion).

QUANTITATIVE VORHERSAGE (Regel R2): identisch zu MERGARB — (a) Rendite
skaliert mit dem Ankündigungsspread, (c) ρ zu bestehenden Sleeves niedrig.
Vorhersage (b, Cash>Aktientausch) entfällt — genau dieser Vergleich wäre
jetzt zwischen den zwei MERGARB-Familien möglich, aber mit nur 113 Swap-
Deals zu klein für einen belastbaren Vergleich (offengelegt, nicht
getestet).

VORREGISTRIERT (Regel R6): 2 Varianten — Haltedauer bis Delisting (kein
horizon-Parameter, das Ziel wird bis zum Closing gehalten) x
{alle Paare, nur Paare mit |Ankündigungs-Spread| < 50%} — die zweite
Variante ist ein einfacher Plausibilitäts-Cutoff analog zu MERGARBs
PLAUSIBLE_SPREAD_BAND, kein Parameter-Sweep im eigentlichen Sinne.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.data.bq import query

MAX_HORIZON_DAYS = 270
CLIP_DAILY_RETURN = 0.50


def load_pairs() -> pd.DataFrame:
    return query("""
      WITH pairs AS (
        SELECT source_accession,
               ARRAY_AGG(STRUCT(symbol, announce_date) ORDER BY symbol) AS r
        FROM `trading-436516.quant.merger_deals`
        WHERE consideration_type IN ('stock', 'mixed')
        GROUP BY source_accession
        HAVING COUNT(DISTINCT symbol) = 2
      )
      SELECT source_accession, r[OFFSET(0)].symbol AS sym_a,
             r[OFFSET(1)].symbol AS sym_b, r[OFFSET(0)].announce_date AS ann
      FROM pairs""")


def _load_prices(symbols: list[str], min_date) -> dict[str, pd.Series]:
    syms = ", ".join(repr(s) for s in symbols)
    df = query(f"""
      SELECT date, symbol, adjusted_close AS px
      FROM `trading-436516.quant.eod_bars`
      WHERE symbol IN ({syms}) AND date >= '{min_date}' AND adjusted_close > 0
      ORDER BY symbol, date""")
    df["date"] = pd.to_datetime(df["date"])
    return {s: g.set_index("date")["px"] for s, g in df.groupby("symbol")}


def assign_target_acquirer(pairs: pd.DataFrame, px: dict[str, pd.Series],
                           window_days: int = 5) -> pd.DataFrame:
    """Ziel = das Symbol mit der höheren Ankündigungsrendite über ein
    ±window_days-Fenster (Standard-M&A-Mikrostruktur: das Ziel bekommt den
    positiven Kurssprung, der Käufer bleibt flach/fällt leicht). window_days
    ist die vorregistrierte Sweep-Achse (Regel R6, 3 Varianten: 3/5/10 Tage)
    — die einzige legitime "hätten wir anders wählen können"-Größe hier, da
    der Datenartefakt-Filter und der Spread-Cutoff aus Datenqualitäts-
    gründen abgeleitet sind, nicht aus Modellwahl. Offengelegte Heuristik,
    keine Garantie — Paare, bei denen beide Symbole keine sinnvolle Rendite
    haben, werden verworfen."""
    rows = []
    for _, p in pairs.iterrows():
        ann = pd.Timestamp(p["ann"])
        rets = {}
        for s in (p["sym_a"], p["sym_b"]):
            ps = px.get(s)
            if ps is None:
                continue
            window = ps.loc[ann - pd.Timedelta(days=window_days):
                            ann + pd.Timedelta(days=window_days)]
            if len(window) < 2:
                continue
            rets[s] = float(window.iloc[-1] / window.iloc[0] - 1)
        if len(rets) < 2:
            continue
        # Datenartefakt-Schutz (gefunden 2026-08-02): KOSN/BMY zeigte hier
        # eine "Ziel-Ankündigungsrendite" von +225% — physikalisch
        # unmöglich für eine echte Übernahmeprämie (reale Prämien liegen
        # bei 20-40%, nie in der Nähe von 200%+). Dasselbe Artefakt-Muster
        # wie ATTO im ursprünglichen mergarb_study.py (korrupter eod_bars-
        # Tick oder Ticker-Wiederverwendung). Ein Paar mit so einem Wert
        # wird verworfen statt die Ziel/Käufer-Zuordnung und die Sharpe
        # damit zu verzerren.
        if max(abs(v) for v in rets.values()) > 0.75:
            continue
        target = max(rets, key=rets.get)
        acquirer = p["sym_a"] if target == p["sym_b"] else p["sym_b"]
        rows.append({"source_accession": p["source_accession"],
                     "announce_date": ann, "target": target,
                     "acquirer": acquirer, "target_ann_ret": rets[target],
                     "acquirer_ann_ret": rets[acquirer]})
    return pd.DataFrame(rows)


def resolve_terminal(px_t: pd.Series, ann: pd.Timestamp,
                     max_horizon_days: int = MAX_HORIZON_DAYS
                     ) -> tuple[pd.Timestamp | None, str]:
    """Vereinfachte Terminal-Logik (kein Umtauschverhältnis verfügbar, daher
    keine ratio-basierte Bruch-Erkennung wie in mergarb_study.py — nur
    Delisting-Proxy übernommen, offengelegte Einschränkung)."""
    p = px_t.dropna().sort_index()
    if p.empty:
        return None, "open"
    near = p.loc[ann - pd.Timedelta(days=15):ann + pd.Timedelta(days=15)]
    if near.empty:
        return None, "no_data"
    last_obs = p.index[-1]
    today = pd.Timestamp.today().normalize()
    if (today - last_obs).days > 10:
        if (last_obs - ann).days > 2 * max_horizon_days:
            return None, "no_data"
        return last_obs, "closed"
    return None, "open"


def deal_return_series(ann, term, px_t: pd.Series, px_a: pd.Series) -> pd.Series:
    """Dollar-neutral: Ziel-Rendite minus Käufer-Rendite je Tag."""
    rt = px_t.dropna().sort_index().loc[pd.Timestamp(ann):pd.Timestamp(term)]
    ra = px_a.dropna().sort_index().reindex(rt.index).ffill()
    if len(rt) < 2:
        return pd.Series(dtype=float)
    ret_t = rt.pct_change().dropna()
    ret_a = ra.pct_change().reindex(ret_t.index).fillna(0.0)
    return (ret_t - ret_a).clip(-CLIP_DAILY_RETURN, CLIP_DAILY_RETURN)


def returns(window_days: int = 5, spread_filter: bool = True) -> pd.Series:
    """Discovery-Pipeline-Entry-Point. spread_filter bleibt fest True
    (Datenqualitäts-Cutoff, kein Sweep-Parameter) — window_days ist die
    vorregistrierte G5-Sweep-Achse."""
    pairs = load_pairs()
    if pairs.empty:
        return pd.Series(dtype=float)
    all_syms = list(set(pairs["sym_a"]) | set(pairs["sym_b"]))
    px = _load_prices(all_syms, pairs["ann"].min())
    ta = assign_target_acquirer(pairs, px, window_days=window_days)
    if ta.empty:
        return pd.Series(dtype=float)
    if spread_filter:
        spread = ta["target_ann_ret"] - ta["acquirer_ann_ret"]
        ta = ta[spread.abs() <= 0.5]
    per_deal = []
    for _, d in ta.iterrows():
        px_t, px_a = px.get(d["target"]), px.get(d["acquirer"])
        if px_t is None or px_a is None:
            continue
        term, status = resolve_terminal(px_t, d["announce_date"])
        if status != "closed":
            continue
        r = deal_return_series(d["announce_date"], term, px_t, px_a)
        if len(r):
            per_deal.append(r)
    if not per_deal:
        return pd.Series(dtype=float)
    wide = pd.concat(per_deal, axis=1)
    return wide.mean(axis=1, skipna=True).dropna()


def live_weights() -> tuple[dict, str]:
    """G8-Entry-Point: aktuell offene Aktientausch-Paare."""
    pairs = load_pairs()
    if pairs.empty:
        return {}, "keine Aktientausch-Paare (fail-closed)"
    all_syms = list(set(pairs["sym_a"]) | set(pairs["sym_b"]))
    px = _load_prices(all_syms, pairs["ann"].min())
    ta = assign_target_acquirer(pairs, px)
    if ta.empty:
        return {}, "keine auswertbaren Paare → flat"
    cutoff = pd.Timestamp.today().normalize() - pd.Timedelta(days=MAX_HORIZON_DAYS)
    open_deals = []
    for _, d in ta.iterrows():
        if pd.Timestamp(d["announce_date"]) < cutoff:
            continue
        px_t = px.get(d["target"])
        if px_t is None:
            continue
        _, status = resolve_terminal(px_t, d["announce_date"])
        if status == "open":
            open_deals.append(d)
    if not open_deals:
        return {}, "kein offenes Aktientausch-Paar → flat"
    w = 0.5 / len(open_deals)
    weights = {}
    for d in open_deals:
        weights[d["target"]] = weights.get(d["target"], 0.0) + w
        weights[d["acquirer"]] = weights.get(d["acquirer"], 0.0) - w
    return (weights, f"{len(open_deals)} offene Aktientausch-Paare, "
                     f"long Ziel/short Käufer, gross {sum(abs(v) for v in weights.values()):.0%}")


def stats(r: pd.Series, ann=252) -> dict:
    r = r.dropna()
    if len(r) < 100:
        return {}
    eq = (1 + r).cumprod()
    return {"n": len(r), "sharpe": r.mean() / r.std() * np.sqrt(ann),
            "cagr": eq.iloc[-1] ** (ann / len(r)) - 1,
            "maxdd": (eq / eq.cummax() - 1).min()}


def run():
    pairs = load_pairs()
    print(f"{len(pairs)} Ziel+Käufer-Paare über gemeinsame Accession")
    all_syms = list(set(pairs["sym_a"]) | set(pairs["sym_b"]))
    px = _load_prices(all_syms, pairs["ann"].min())
    ta = assign_target_acquirer(pairs, px)
    print(f"{len(ta)} Paare mit auswertbarer Ankündigungsrendite\n")
    print("Beispiele (Ziel, Käufer, Ziel-Ankündigungsrendite):")
    print(ta[["target", "acquirer", "target_ann_ret", "acquirer_ann_ret"]]
          .head(10).to_string())

    print("\n═══ Variantenwahl (window_days ∈ {3,5,10}, Regel R6) ═══")
    from quant.research.trials_registry import log_trial
    results = {}
    for wd in (3, 5, 10):
        label = f"window={wd}d"
        r = returns(window_days=wd, spread_filter=True)
        s = stats(r)
        results[label] = (r, s)
        print(f"  {label:16s}: n={s.get('n', 0):4d}  "
              f"Sharpe {s.get('sharpe', float('nan')):6.2f}  "
              f"CAGR {s.get('cagr', float('nan')):+7.1%}  "
              f"MaxDD {s.get('maxdd', float('nan')):7.1%}")
        if s:
            try:
                log_trial("MERGARB_SWAP", r, variant=f"{label}-v2", ann=252,
                          notes="Aktientausch-Hedge, 113 accession-gepaarte "
                                "Deals, Artefakt-Filter + window_days-Sweep "
                                "(ersetzt v1-Trials #85-88, die keine "
                                "vorregistrierte within-family-Varianz hatten)")
            except Exception as e:  # noqa: BLE001
                print(f"    (trials_registry: {e})")

    best_label = max(results, key=lambda k: results[k][1].get("sharpe", -9))
    r_best, s_best = results[best_label]
    if not s_best:
        print("\nkeine auswertbare Variante (zu wenige Beobachtungen)")
        return

    print(f"\n═══ HOLDOUT/2022+ ({best_label}) ═══")
    ho = stats(r_best.loc["2020-01-01":])
    print(f"Holdout 2020+: Sharpe {ho.get('sharpe', float('nan')):.2f} | "
          f"CAGR {ho.get('cagr', float('nan')):+.1%}")
    r22 = stats(r_best.loc["2022-01-01":])
    print(f"2022+: Sharpe {r22.get('sharpe', float('nan')):.2f} | "
          f"CAGR {r22.get('cagr', float('nan')):+.1%}")

    print("\n═══ ORTHOGONALITÄT ═══")
    from quant.research.discovery import live_sleeve_returns, portfolio_delta
    ex = live_sleeve_returns()
    before, after, rhos = portfolio_delta(r_best, ex, s_best["sharpe"])
    for nm, rho in rhos.items():
        print(f"  ρ(MERGARB_SWAP, {nm}) = {rho:+.3f}")
    print(f"  Portfolio-Sharpe {before:.2f} → {after:.2f} "
          f"(Δ {after-before:+.3f})")

    w, why = live_weights()
    print(f"\nlive_weights: {why} ({len(w)} Positionen)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()
    if not a.run:
        ap.print_help()
        sys.exit(1)
    run()
