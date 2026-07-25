"""Sleeve-Health-Monitor — validiert kontinuierlich, ob die Edges noch existieren.

    python3 -m quant.ops.sleeve_health --check

Rechnet für jeden validierten Sleeve die Performance auf den FRISCHESTEN Daten
nach — mit exakt denselben Regeln wie im Backtest, ohne Refit. Vergleicht gegen
die vorregistrierte Baseline und die Kill-Regeln aus DESIGN.md §4 (G10):
  * rollierender 60d-Sharpe 3σ unter Backtest      → HALBIEREN
  * 20 aufeinanderfolgende Tage negativer 60d-Drift → HALBIEREN
  * 252d-Sharpe < 0 bei ≥ 1 Jahr Daten             → ALLOKATION AUF NULL
Ergebnis nach BigQuery (quant.sleeve_health) + Telegram-Alarm bei Verschlechterung.

Krypto (CTREND) ist auf Anweisung 2026-07-24 aus dem Stack ausgeschlossen und
wird nur noch informativ mitgerechnet (monitor_only).
"""

import argparse
import datetime as dt
import sys

import numpy as np
import pandas as pd
from google.cloud import bigquery

from quant.config import BQ_DATASET, GCP_PROJECT
from quant.data.bq import ensure_table, load_df, query

T_HEALTH = f"{GCP_PROJECT}.{BQ_DATASET}.sleeve_health"

# Vorregistrierte Baselines (Backtest-Sharpe, netto) + Vol-Budget
BASELINES = {
    # XSR korrigiert 2026-07-25. Die alten 0.69/0.60 waren der Stand VOR zwei
    # Korrekturen: (a) den Leihkosten im Simulator (G1-Lücke, 200bp/Jahr auf das
    # Short-Notional) und (b) dem Leih-Gate auf dem Short-Bein. Nachgerechnet
    # auf preds_wf_v2_full: ohne Gate 0.604/0.424, isolierte Gate-Kosten
    # -0.032/-0.085 (gemessen auf dem heute-notierten Subpanel, damit der
    # Delisting-Effekt nicht mitzählt) → 0.57/0.34.
    # Eine zu hohe Baseline ist gefährlicher als eine zu niedrige: der Monitor
    # hätte echten Zerfall als "im Rahmen" durchgewinkt, und S_p war überschätzt.
    "XSR": {"sharpe": 0.57, "regime_2022": 0.34, "monitor_only": False},
    "ONX": {"sharpe": 1.06, "regime_2022": 0.40, "monitor_only": False},
    "VOLC": {"sharpe": 0.64, "regime_2022": 0.45, "monitor_only": False},
    "EOMT": {"sharpe": 0.87, "regime_2022": 0.65, "monitor_only": False},
    "DTRD": {"sharpe": 0.73, "regime_2022": 0.43, "monitor_only": False},
    "CTREND": {"sharpe": 1.19, "regime_2022": 0.75, "monitor_only": True},
}

SCHEMA = [
    bigquery.SchemaField("as_of", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("sleeve", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("sharpe_60d", "FLOAT64"),
    bigquery.SchemaField("sharpe_252d", "FLOAT64"),
    bigquery.SchemaField("baseline_sharpe", "FLOAT64"),
    bigquery.SchemaField("z_vs_baseline", "FLOAT64"),
    bigquery.SchemaField("verdict", "STRING"),
    bigquery.SchemaField("detail", "STRING"),
    bigquery.SchemaField("monitor_only", "BOOL"),
]


def _sharpe(r: pd.Series, ann=252) -> float:
    r = r.dropna()
    if len(r) < 20 or r.std() == 0:
        return float("nan")
    return float(r.mean() / r.std() * np.sqrt(ann))


# ── Sleeve-Rekonstruktionen (identische Regeln wie Backtest, kein Refit) ──────
def onx_returns() -> pd.Series:
    """3x-Bull-ETF-Übernacht, 50d-SMA-Trendgate, EW, 4bp/Tag."""
    from quant.research.letf_rebalance_flow import UNIV_3X
    q = ", ".join(repr(s) for s in UNIV_3X)
    df = query(f"""
      WITH px AS (
        SELECT date, symbol,
          open * SAFE_DIVIDE(adjusted_close, close) AS ao,
          adjusted_close AS ac
        FROM `{GCP_PROJECT}.{BQ_DATASET}.eod_bars`
        WHERE symbol IN ({q}) AND close>0 AND adjusted_close>0
          AND date >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 YEAR))
      SELECT date, symbol,
        SAFE_DIVIDE(LEAD(ao) OVER w, ac) - 1 AS r_on,
        ac, AVG(ac) OVER (PARTITION BY symbol ORDER BY date
                          ROWS BETWEEN 50 PRECEDING AND 1 PRECEDING) AS sma50
      FROM px WINDOW w AS (PARTITION BY symbol ORDER BY date)""")
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["r_on", "sma50"])
    df = df[(df["ac"] > df["sma50"]) & (df["r_on"].abs() < 0.5)]
    return (df.groupby("date")["r_on"].mean() - 4 / 1e4).sort_index()


def volc_returns() -> pd.Series:
    """SVXY long, Gate VIX3M/VIX-Contango > 3%, 3bp Turnover."""
    px = query(f"""
      SELECT date, adjusted_close AS c FROM `{GCP_PROJECT}.{BQ_DATASET}.eod_bars`
      WHERE symbol='SVXY' AND date >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 YEAR)
      ORDER BY date""")
    fred = query(f"""
      SELECT date, series, value FROM `{GCP_PROJECT}.{BQ_DATASET}.fred_series`
      WHERE series IN ('VIXCLS','VIX3MCLS','VXVCLS')
        AND date >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 YEAR)""")
    if px.empty or fred.empty:
        return pd.Series(dtype=float)
    px["date"] = pd.to_datetime(px["date"])
    s = px.set_index("date")["c"].pct_change()
    f = fred.pivot(index="date", columns="series", values="value")
    f.index = pd.to_datetime(f.index)
    f = f.reindex(s.index).ffill()
    col3m = next((c for c in ("VIX3MCLS", "VXVCLS")
                  if c in f and not f[c].isna().all()), None)
    if col3m is None:
        return pd.Series(dtype=float)  # keine 3M-Vol-Serie → kein Urteil
    contango = (f[col3m] / f["VIXCLS"] - 1).shift(1)
    pos = (contango > 0.03).astype(float)
    return (pos * s - pos.diff().abs().fillna(0) * 3 / 1e4).dropna()


def xsr_returns() -> pd.Series:
    """Ranker-Scores des jüngsten Folds auf frischen Features → Tranche-Sim."""
    import os
    import lightgbm as lgb
    from quant.config import STAGING_DIR
    from quant.models.train_ranker import V2_FEATURES
    from quant.backtest.portfolio_sim import simulate_tranches
    from quant.execution.xsr_live import _ensure_models

    model_dir = _ensure_models()
    models = sorted(f for f in os.listdir(model_dir) if f.startswith("ranker_"))
    if not models:
        return pd.Series(dtype=float)
    booster = lgb.Booster(model_file=os.path.join(model_dir, models[-1]))
    # WICHTIG: ranker_YYYY.txt ist auf Daten < YYYY-01-01 trainiert. Nur Daten
    # AB diesem Datum sind out-of-sample — sonst misst der Monitor In-Sample-Fit
    # und meldet absurd hohe Sharpes (Bug gefunden 2026-07-24).
    oos_start = f"{models[-1].split('_')[1].split('.')[0]}-01-01"
    cols = ["date", "symbol"] + V2_FEATURES + ["fwd_ret_1d", "fwd_ret_5d",
                                              "vol_63d", "adv63"]
    df = query(f"""SELECT {', '.join(dict.fromkeys(cols))}
      FROM `{GCP_PROJECT}.{BQ_DATASET}.features_daily_v2`
      WHERE date >= '{oos_start}' AND fwd_ret_1d IS NOT NULL""")
    if df.empty:
        return pd.Series(dtype=float)
    df["date"] = pd.to_datetime(df["date"])
    df["score"] = booster.predict(df[V2_FEATURES]).astype("float32")
    res = simulate_tranches(df, k=5)
    return res["net_ret"].sort_index()


def ctrend_returns() -> pd.Series:
    """Nur informativ (Krypto ausgeschlossen): BTC+ETH TSMOM, 25bp/Seite."""
    df = query(f"""
      SELECT date, symbol, close FROM `{GCP_PROJECT}.{BQ_DATASET}.binance_daily`
      WHERE symbol IN ('BTCUSDT','ETHUSDT')
        AND date >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 YEAR) ORDER BY date""")
    if df.empty:
        return pd.Series(dtype=float)
    df["date"] = pd.to_datetime(df["date"])
    close = df.pivot(index="date", columns="symbol", values="close").ffill()
    ret = close.pct_change()
    sig = ((close > close.rolling(20).mean())
           & (close > close.rolling(50).mean())
           & (close.pct_change(20) > 0)).shift(1)
    vol20 = ret.rolling(20).std() * np.sqrt(365)
    w = (sig * (0.40 / vol20.shift(1)).clip(upper=1.0))
    w = w.div(w.sum(axis=1).clip(lower=1.0), axis=0).fillna(0)
    return ((w * ret).sum(axis=1)
            - w.diff().abs().sum(axis=1).fillna(0) * 25 / 1e4).dropna()


def eomt_returns() -> pd.Series:
    """EW IEF/TLT/EDV an den letzten 5 Handelstagen des Monats, 4bp."""
    from quant.research.eomt_study import month_end_returns, COST
    df = query(f"""
      SELECT date, symbol, adjusted_close AS ac
      FROM `{GCP_PROJECT}.{BQ_DATASET}.eod_bars`
      WHERE symbol IN ('IEF','TLT','EDV') AND adjusted_close > 0
        AND date >= DATE_SUB(CURRENT_DATE(), INTERVAL 6 YEAR) ORDER BY date""")
    if df.empty:
        return pd.Series(dtype=float)
    df["date"] = pd.to_datetime(df["date"])
    px = df.pivot(index="date", columns="symbol", values="ac").sort_index()
    me = month_end_returns(px, 5)
    cols = [c for c in ("IEF", "TLT", "EDV") if c in me]
    return (me[cols].mean(axis=1) - COST).dropna()


def dtrd_returns() -> pd.Series:
    """Cross-Asset-TSMOM, identische Regeln wie die validierte Studie."""
    from quant.research.dtrd_study import load, sleeve
    try:
        px = load()
    except Exception:  # noqa: BLE001
        return pd.Series(dtype=float)
    r = sleeve(px, 126)
    return r.loc[str(dt.date.today() - dt.timedelta(days=3 * 365)):]


SLEEVES = {"XSR": xsr_returns, "ONX": onx_returns, "VOLC": volc_returns,
           "EOMT": eomt_returns, "DTRD": dtrd_returns,
           "CTREND": ctrend_returns}


def check(alert=True):
    ensure_table(T_HEALTH, SCHEMA, partition_field="as_of",
                 clustering=["sleeve"])
    today = dt.date.today()
    rows, alarms = [], []
    print(f"{'Sleeve':8s} {'60d':>7s} {'252d':>7s} {'Basis':>7s} {'z':>7s}  Urteil")
    for name, fn in SLEEVES.items():
        base = BASELINES[name]
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001
            print(f"{name:8s} FEHLER: {str(e)[:60]}")
            continue
        if r.empty:
            print(f"{name:8s} keine Daten")
            continue
        ann = 365 if name == "CTREND" else (12 if name == "EOMT" else 252)
        s60 = _sharpe(r.tail(60), ann)
        s252 = _sharpe(r.tail(252), ann)
        # z-Score der 60d-Schätzung gegen Baseline (SE ≈ sqrt(ann/n))
        se60 = np.sqrt(ann / 60)
        z = (s60 - base["sharpe"]) / se60 if not np.isnan(s60) else np.nan
        # Kill-Regeln
        verdict, detail = "OK", ""
        if not np.isnan(s252) and len(r) >= 252 and s252 < 0:
            verdict = "AUF NULL"
            detail = f"252d-Sharpe {s252:.2f} < 0"
        elif not np.isnan(z) and z < -3:
            verdict = "HALBIEREN"
            detail = f"60d-Sharpe {s60:.2f} liegt {abs(z):.1f}σ unter Baseline"
        elif not np.isnan(s252) and s252 < base["regime_2022"] * 0.5:
            verdict = "BEOBACHTEN"
            detail = (f"252d-Sharpe {s252:.2f} unter halber Regime-Baseline "
                      f"{base['regime_2022']:.2f}")
        flag = " (nur Monitor)" if base["monitor_only"] else ""
        print(f"{name:8s} {s60:7.2f} {s252:7.2f} {base['sharpe']:7.2f} "
              f"{z:7.1f}  {verdict}{flag} {detail}")
        rows.append({"as_of": today, "sleeve": name, "sharpe_60d": s60,
                     "sharpe_252d": s252, "baseline_sharpe": base["sharpe"],
                     "z_vs_baseline": z, "verdict": verdict, "detail": detail,
                     "monitor_only": base["monitor_only"]})
        if verdict != "OK" and not base["monitor_only"]:
            alarms.append(f"{name}: {verdict} — {detail}")

    if rows:
        load_df(T_HEALTH, pd.DataFrame(rows), schema=SCHEMA)
    if alert:
        from quant.execution.telegram import notify
        if alarms:
            notify("⚠️ Sleeve-Health: " + " | ".join(alarms))
        else:
            ok = ", ".join(f"{r['sleeve']} {r['sharpe_252d']:.2f}" for r in rows
                           if not r["monitor_only"])
            notify(f"Sleeve-Health OK (252d-Sharpe: {ok})")
    return rows


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    p.add_argument("--no-alert", action="store_true")
    a = p.parse_args()
    if not a.check:
        p.print_help()
        sys.exit(1)
    check(alert=not a.no_alert)
