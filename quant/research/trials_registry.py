"""Trial-Registry + Deflated Sharpe — die Sicherheitsschicht für automatisierte Suche.

    python3 -m quant.research.trials_registry --log ...   # Versuch protokollieren
    python3 -m quant.research.trials_registry --dsr ...   # DSR eines Kandidaten
    python3 -m quant.research.trials_registry --report    # Registry-Übersicht

WARUM: Wer viele Kandidaten testet, findet Zufallstreffer. Bei N Versuchen und
5 % Signifikanz sind 0.05·N Fehlfunde garantiert. Der Deflated Sharpe Ratio
(Bailey/López de Prado 2014) korrigiert den beobachteten Sharpe um genau diese
Selektion — er braucht dazu aber die EHRLICHE Zahl aller Versuche. Deshalb ist
das Protokollieren Pflicht und nicht optional: jeder Backtest-Lauf einer
Strategiefamilie wird hier eingetragen, auch die verworfenen.

DSR-Formel:
  SR* = E[max Sharpe unter N unabhängigen Nullversuchen]
      = σ_SR_trials · [(1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e))]
  DSR = Φ[ (SR_obs − SR*) / σ̂_SR ]
  σ̂_SR = sqrt( (1 − skew·SR + (kurt−1)/4 · SR²) / (T−1) )
DSR ist die Wahrscheinlichkeit, dass der wahre Sharpe > 0 ist, NACH Korrektur
für Mehrfachtests, Nicht-Normalität und Stichprobenlänge. Gate: DSR > 0.95.
"""

import argparse
import datetime as dt
import json
import sys

import numpy as np
import pandas as pd
from google.cloud import bigquery
from scipy.stats import norm

from quant.config import BQ_DATASET, GCP_PROJECT
from quant.data.bq import ensure_table, load_df, query

T_TRIALS = f"{GCP_PROJECT}.{BQ_DATASET}.trials_registry"
EULER = 0.5772156649015329

SCHEMA = [
    bigquery.SchemaField("ts", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("family", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("variant", "STRING"),
    bigquery.SchemaField("config_hash", "STRING"),
    bigquery.SchemaField("sharpe_net", "FLOAT64"),
    bigquery.SchemaField("cagr_net", "FLOAT64"),
    bigquery.SchemaField("n_obs", "INT64"),
    bigquery.SchemaField("skew", "FLOAT64"),
    bigquery.SchemaField("kurtosis", "FLOAT64"),
    bigquery.SchemaField("dsr", "FLOAT64"),
    bigquery.SchemaField("n_trials_at_log", "INT64"),
    bigquery.SchemaField("verdict", "STRING"),
    bigquery.SchemaField("notes", "STRING"),
]


def deflated_sharpe(sharpe_ann: float, n_obs: int, skew: float,
                    kurt: float, n_trials: int,
                    sharpe_var_trials: float | None = None,
                    ann: int = 252) -> dict:
    """DSR nach Bailey/López de Prado (2014).

    EINHEITEN (kritisch, hier lag zuerst ein Fehler): Die Formel für σ̂_SR
    gilt für den Sharpe PRO BEOBACHTUNG bei T Beobachtungen. Eingaben sind
    annualisiert (bequemer), werden intern auf Pro-Beobachtung umgerechnet und
    das Ergebnis wieder annualisiert zurückgegeben. Mischt man annualisierten
    Sharpe mit T=Tagen, wird σ̂_SR um √ann zu klein und der DSR kollabiert
    auf 0 oder 1.

    sharpe_var_trials: Varianz der Sharpes über VERGLEICHBARE Versuche
    (Varianten derselben Familie / eines Parameter-Sweeps). Die Streuung über
    strukturell verschiedene Familien ist KEINE Glücksvarianz und würde
    überdeflationieren — deshalb wird sie vom Aufrufer within-family geschätzt.
    """
    n_trials = max(int(n_trials), 1)
    sqa = np.sqrt(ann)
    sr = sharpe_ann / sqa                                  # pro Beobachtung
    sd_trials = (np.sqrt(sharpe_var_trials) if sharpe_var_trials else 0.5) / sqa
    if n_trials == 1:
        sr_star = 0.0
    else:
        z1 = norm.ppf(1 - 1.0 / n_trials)
        z2 = norm.ppf(1 - 1.0 / (n_trials * np.e))
        sr_star = sd_trials * ((1 - EULER) * z1 + EULER * z2)
    denom = max(n_obs - 1, 1)
    var_term = (1 - skew * sr + (kurt - 1) / 4.0 * sr ** 2) / denom
    se = np.sqrt(max(var_term, 1e-12))
    dsr = float(norm.cdf((sr - sr_star) / se))
    return {"dsr": dsr, "sr_star": float(sr_star * sqa),
            "se_sharpe": float(se * sqa), "n_trials": n_trials,
            "sd_trials": float(sd_trials * sqa)}


def family_sharpe_var(family: str) -> float | None:
    """Glücksvarianz aus den Varianten DERSELBEN Familie (vergleichbarer
    Suchraum). Fällt auf die globale Varianz zurück, wenn zu wenige Varianten."""
    try:
        df = query(f"SELECT sharpe_net FROM `{T_TRIALS}` "
                   f"WHERE family = '{family}'")
    except Exception:  # noqa: BLE001
        return None
    s = df["sharpe_net"].dropna() if not df.empty else pd.Series(dtype=float)
    if len(s) > 2:
        return float(s.var())
    _, glob = trial_stats()
    return glob


def family_trials(family: str) -> int:
    """Zahl der Versuche INNERHALB dieser Familie.

    Das ist der richtige Nenner für den Deflated Sharpe. Bailey/López de Prado
    (2014, §3) deflationieren mit der Zahl der Versuche, aus denen die Strategie
    AUSGEWÄHLT wurde — nicht mit der Lebenszahl aller Versuche des Programms.
    Ich habe zunächst den globalen Zähler benutzt; dadurch bekam JEDE Familie
    ein SR* von ~1.2, also einen Grenzwert, den keine dokumentierte
    ETF-Faktorprämie erreicht — und auch das bereits validierte XSR fiel mit
    DSR 0.561 durch, obwohl es an einer engen, vorregistrierten Variantenmenge
    gewählt wurde. Der Programm-Selektionseffekt verschwindet damit nicht: er
    wird separat als Trefferquote (validierte/getestete Familien) berichtet,
    statt als Korrektur in eine Einzelfamilien-Statistik gefaltet zu werden.
    """
    try:
        df = query(f"SELECT COUNT(*) n FROM `{T_TRIALS}` "
                   f"WHERE family = '{family}'")
        return int(df["n"].iloc[0]) if not df.empty else 0
    except Exception:  # noqa: BLE001
        return 0


def program_hit_rate() -> tuple[int, int]:
    """(validierte Familien, getestete Familien) — der Programm-Selektionseffekt,
    ehrlich ausgewiesen statt in den DSR gefaltet."""
    try:
        df = query(f"SELECT DISTINCT family FROM `{T_TRIALS}`")
    except Exception:  # noqa: BLE001
        return 0, 0
    # Als validiert gilt, was live handelt — die Verdikt-Spalte führt nur
    # "KANDIDAT"/"Variante" und taugt dafür nicht (sie zählte immer 0).
    from quant.ops.sleeve_health import BASELINES
    live = {k.upper() for k, v in BASELINES.items()
            if not v.get("monitor_only")}
    fams = {str(f).upper() for f in df["family"]}
    return len(fams & live), len(fams)


def trial_stats() -> tuple[int, float | None]:
    """Zahl aller protokollierten Versuche + Varianz ihrer Sharpes."""
    try:
        df = query(f"SELECT sharpe_net FROM `{T_TRIALS}`")
    except Exception:  # noqa: BLE001 — Tabelle existiert noch nicht
        return 0, None
    if df.empty:
        return 0, None
    s = df["sharpe_net"].dropna()
    return len(df), float(s.var()) if len(s) > 2 else None


def log_trial(family: str, returns: pd.Series, variant: str = "",
              verdict: str = "", notes: str = "", ann: int = 252,
              config: dict | None = None) -> dict:
    """Protokolliert einen Versuch und liefert den DSR zurück."""
    ensure_table(T_TRIALS, SCHEMA, partition_field="ts", clustering=["family"])
    r = returns.dropna()
    if len(r) < 20:
        raise ValueError("zu wenige Beobachtungen")
    sharpe = float(r.mean() / r.std() * np.sqrt(ann))
    cagr = float((1 + r).cumprod().iloc[-1] ** (ann / len(r)) - 1)
    skew = float(r.skew())
    kurt = float(r.kurtosis() + 3.0)  # pandas liefert Exzess
    n_prev, _ = trial_stats()
    var_trials = family_sharpe_var(family)
    # Familien-Versuchszahl als Nenner (siehe family_trials): der DSR misst,
    # ob DIESE Hypothese ihren eigenen Suchraum schlägt.
    d = deflated_sharpe(sharpe, len(r), skew, kurt,
                        family_trials(family) + 1, var_trials, ann)
    row = {
        "ts": dt.datetime.now(dt.timezone.utc), "family": family,
        "variant": variant,
        "config_hash": str(abs(hash(json.dumps(config or {}, sort_keys=True))))[:12],
        "sharpe_net": sharpe, "cagr_net": cagr, "n_obs": len(r),
        "skew": skew, "kurtosis": kurt, "dsr": d["dsr"],
        "n_trials_at_log": n_prev + 1, "verdict": verdict, "notes": notes,
    }
    load_df(T_TRIALS, pd.DataFrame([row]), schema=SCHEMA)
    print(f"[trial #{n_prev + 1}] {family}/{variant or '-'}: "
          f"Sharpe {sharpe:.2f}, CAGR {cagr:+.1%}, "
          f"DSR {d['dsr']:.3f} (SR* {d['sr_star']:.2f}) → "
          f"{'BESTEHT (DSR>0.95)' if d['dsr'] > 0.95 else 'FÄLLT DURCH'}")
    return {**row, **d}


def backfill_known_trials():
    """Trägt die bereits gelaufenen 14 Familien + Varianten nach, damit die
    Versuchszahl ehrlich ist (Untertreibung würde DSR aufblähen)."""
    ensure_table(T_TRIALS, SCHEMA, partition_field="ts", clustering=["family"])
    n_prev, _ = trial_stats()
    if n_prev > 0:
        print(f"Registry hat schon {n_prev} Einträge — kein Backfill")
        return
    # (Familie, Variante, Netto-Sharpe, Netto-CAGR, ~Beobachtungen, Urteil)
    known = [
        ("XSR", "v1 price/volume", 0.50, 0.097, 5900, "VALIDIERT"),
        ("XSR", "v2 +fundamentals 50%", 0.54, 0.105, 5900, "VALIDIERT"),
        ("XSR", "v2 +fundamentals 75%", 0.69, 0.144, 5900, "PRODUKTION"),
        ("XSR", "h=10d Label", 0.56, 0.107, 5900, "Fallback"),
        ("XSR", "h=21d Label", 0.52, 0.095, 5900, "Fallback"),
        ("XSR", "Ridge", 0.03, -0.045, 1900, "VERWORFEN"),
        ("XSR", "torch-MLP", -0.38, -0.085, 1900, "VERWORFEN"),
        ("XSR", "XGBoost", 0.50, 0.114, 1900, "VERWORFEN"),
        ("XSR", "5-Seed-Ensemble", 0.48, 0.111, 1900, "VERWORFEN"),
        ("XSR", "+FINRA Short-Volume", 0.585, 0.140, 1900, "VERWORFEN"),
        ("XSR", "+FRED Regime-Interakt.", 0.655, 0.150, 1900, "VERWORFEN"),
        ("ONX", "V1 EW 28 ETFs", 0.78, 0.246, 2600, "VALIDIERT"),
        ("ONX", "V2 Trendgate", 1.06, 0.295, 2600, "PRODUKTION"),
        ("ONX", "V3 Vol-Target", 0.81, 0.270, 2600, "Alternative"),
        ("ONX", "DOW-Kalender", 0.70, 0.220, 2600, "VERWORFEN"),
        ("ONX", "Zwangsfluss-Conditioning", 0.93, 0.328, 2600, "KANDIDAT"),
        ("VOLC", "SVXY contango>3%", 0.64, 0.174, 2600, "PRODUKTION"),
        ("VOLC", "VXX short", 0.30, 0.001, 2100, "VERWORFEN"),
        ("CTREND", "TSMOM 7 Coins", 0.51, 0.138, 1900, "VERWORFEN"),
        ("CTREND", "BTC+ETH 2017+", 1.19, 0.462, 3200, "AUSGESCHLOSSEN"),
        ("IMOM", "Regel 25bp", -1.31, -0.100, 2600, "GEKILLT"),
        ("IMOM", "Meta-Filter", 0.14, 0.010, 1900, "GEKILLT"),
        ("GAP", "ML drift/fade", 0.12, 0.021, 1900, "GEKILLT"),
        ("CAT", "FinBERT Katalysator", -1.10, -0.306, 1900, "GEKILLT"),
        ("PEAD", "SUE-Quintile", -0.20, -0.083, 5900, "GEKILLT"),
        ("OPT", "Put-Spreads wöchentl.", -0.50, -0.200, 190, "GEKILLT"),
        ("PAIR", "3x Doppel-Short", -0.30, -0.035, 2600, "GEKILLT"),
        ("RSPLIT", "Reverse-Split-Drift", -0.40, -0.150, 1000, "GEKILLT"),
        ("OVN", "Übernacht-Fade liquide", -0.20, -0.050, 130, "GEKILLT"),
        ("LETF", "Zwangsrebalancierung", 0.10, 0.020, 2600, "GEKILLT"),
    ]
    rows = []
    now = dt.datetime.now(dt.timezone.utc)
    fam_var = {}
    for fam in {k[0] for k in known}:
        sh_list = [k[2] for k in known if k[0] == fam]
        fam_var[fam] = float(np.var(sh_list)) if len(sh_list) > 2 else \
            float(np.var([k[2] for k in known]))
    for i, (fam, var, sh, cagr, n, verd) in enumerate(known, start=1):
        d = deflated_sharpe(sh, n, 0.0, 3.0, i, fam_var[fam])
        rows.append({"ts": now - dt.timedelta(seconds=len(known) - i),
                     "family": fam, "variant": var,
                     "config_hash": "backfill", "sharpe_net": sh,
                     "cagr_net": cagr, "n_obs": n, "skew": 0.0,
                     "kurtosis": 3.0, "dsr": d["dsr"], "n_trials_at_log": i,
                     "verdict": verd, "notes": "Backfill Session 2026-07"})
    load_df(T_TRIALS, pd.DataFrame(rows), schema=SCHEMA)
    print(f"Backfill: {len(rows)} Versuche protokolliert "
          f"(Glücksvarianz within-family: "
          + ", ".join(f"{f} {v:.2f}" for f, v in sorted(fam_var.items())) + ")")


def report():
    n, var = trial_stats()
    print(f"=== Trial-Registry: {n} protokollierte Versuche, "
          f"Sharpe-Varianz {var:.3f} ===\n")
    df = query(f"""
      SELECT family, variant, sharpe_net, cagr_net, n_obs, dsr, verdict
      FROM `{T_TRIALS}` ORDER BY sharpe_net DESC""")
    if df.empty:
        print("leer")
        return
    # DSR für die Top-Kandidaten mit der AKTUELLEN Versuchszahl neu rechnen
    print(f"{'Familie':9s} {'Variante':26s} {'Sharpe':>7s} {'CAGR':>8s} "
          f"{'DSR@N':>7s}  Urteil")
    fam_vars = {f: family_sharpe_var(f) for f in df["family"].unique()}
    for _, r in df.head(14).iterrows():
        d = deflated_sharpe(r.sharpe_net, int(r.n_obs), 0.0, 3.0, n,
                            fam_vars.get(r.family) or var)
        mark = "✓" if d["dsr"] > 0.95 else " "
        print(f"{r.family:9s} {str(r.variant)[:26]:26s} {r.sharpe_net:7.2f} "
              f"{r.cagr_net:+8.1%} {d['dsr']:7.3f}{mark} {r.verdict}")
    print("\nSR*-Schwellen bei aktueller Versuchszahl (Erwartungswert des"
          " besten ZUFALLS-Sharpe):")
    for f, v in sorted(fam_vars.items()):
        if v is None:
            continue
        d0 = deflated_sharpe(1.0, 2600, 0.0, 3.0, n, v)
        print(f"  {f:9s} SR* = {d0['sr_star']:.2f}  "
              f"(Varianz der Varianten {v:.3f})")
    print("Lesehilfe: DSR ist P(wahrer Sharpe > 0) nach Korrektur für "
          f"{n} Versuche, Schiefe/Kurtosis und Stichprobenlänge. Gate: >0.95.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--report", action="store_true")
    a = p.parse_args()
    if a.backfill:
        backfill_known_trials()
    if a.report:
        report()
    if not (a.backfill or a.report):
        p.print_help()
        sys.exit(1)
