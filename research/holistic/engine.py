"""Generische, hebelkonsistente Allokations-Engine. Research only.

Signale IMMER auf den ungehebelten Total-Return-Reihen.
Ausfuehrung: Monatsschluss-Signal -> Handel am naechsten Handelsschluss.
Hebel wird auf jedes risikoreiche Bein gleich angewandt; Cash bleibt 1x.
"""
import numpy as np, pandas as pd
from dataclasses import dataclass, field

SPREAD = {1.0:0.0, 1.5:0.005, 2.0:0.005, 2.5:0.010, 3.0:0.020}  # kalibriert
EXPENSE = {1.0:0.0009, 1.5:0.0075, 2.0:0.0095, 2.5:0.0095, 3.0:0.0095}
NO_LEVERAGE = {"silver"}            # Hebelmodell scheitert (+11,6pp)
UNCERTAIN_LEVERAGE = {"reit"}       # +4,3pp, wird markiert

@dataclass
class Spec:
    name: str
    family: str
    universe: list
    defensive: str = "cash"
    top_n: int = 0                      # 0 = alle halten
    lookbacks: tuple = (1,3,6,12)       # Monate
    lb_weights: tuple = None
    absolute: bool = True               # Score<=0 -> defensiv
    weighting: str = "equal"            # equal | invvol | fixed
    fixed: dict = None
    vol_target: float = 0.0             # 0 = aus
    vol_window: int = 60
    canary: tuple = ()                  # alle muessen Score>0 haben
    trend_sma: int = 0                  # >0: Preis>SMA statt Momentum-Score
    leverage: float = 1.0
    note: str = ""

def _monthly_levels(levels):
    per = levels.index.to_period("M"); last = ~per.duplicated(keep="last")
    m = levels.loc[last].copy(); m.index = per[last]
    return m, levels.index[last]

def run(spec, levels, cash_ret, days, cost_bps=5.0, return_weights=False):
    """levels: DataFrame ungehebelter Total-Return-Levels. Gibt Tagesrenditen zurueck."""
    uni = [a for a in spec.universe if a in levels.columns]
    need = set(uni) | set(spec.canary)
    if spec.defensive != "cash": need.add(spec.defensive)
    lv = levels[[c for c in levels.columns if c in need]].dropna()
    if len(lv) < 400: return None
    m, mdates = _monthly_levels(lv)
    lbw = np.array(spec.lb_weights if spec.lb_weights else [1.0]*len(spec.lookbacks), float)
    lbw = lbw/lbw.sum()
    score = sum(w*(m/m.shift(k)-1) for k,w in zip(spec.lookbacks,lbw))
    sma = lv.rolling(spec.trend_sma).mean() if spec.trend_sma else None
    r = lv.pct_change().iloc[1:]
    vol = r.rolling(spec.vol_window).std()*np.sqrt(252)

    W = {}
    valid = score.dropna(how="any")
    if len(valid) < 24: return None
    for i,p in enumerate(valid.index):
        d = mdates[list(score.index).index(p)]
        row = valid.loc[p]
        if spec.canary and any(row.get(c,0) <= 0 for c in spec.canary):
            W[d] = {spec.defensive:1.0}; continue
        if spec.trend_sma:
            elig = [a for a in uni if d in sma.index and lv.at[d,a] > sma.at[d,a]]
        else:
            cand = row.reindex(uni).sort_values(ascending=False,kind="stable")
            elig = list(cand.index[:spec.top_n]) if spec.top_n else list(cand.index)
            if spec.absolute: elig = [a for a in elig if row[a] > 0]
        if not elig: W[d] = {spec.defensive:1.0}; continue
        n_slots = spec.top_n if spec.top_n else len(uni)
        if spec.weighting == "fixed" and spec.fixed:
            w = {a:spec.fixed[a] for a in elig if a in spec.fixed}
            tot = sum(w.values()) or 1.0; w = {a:v/tot for a,v in w.items()}
        elif spec.weighting == "invvol":
            v = {a: max(float(vol.at[d,a]) if d in vol.index and np.isfinite(vol.at[d,a]) else .2, .02) for a in elig}
            inv = {a:1/v[a] for a in elig}; tot=sum(inv.values())
            w = {a:inv[a]/tot for a in elig}
        else:
            w = {a:1.0/len(elig) for a in elig}
        # nicht besetzte Slots gehen defensiv (Top-N-Semantik)
        risky = len(elig)/n_slots if spec.top_n else 1.0
        w = {a:v*risky for a,v in w.items()}
        if spec.vol_target > 0:
            pv = np.sqrt(sum((w.get(a,0)*float(vol.at[d,a] if d in vol.index and np.isfinite(vol.at[d,a]) else .2))**2 for a in elig))
            if pv > 0: 
                k = min(1.0, spec.vol_target/(pv*spec.leverage))
                w = {a:v*k for a,v in w.items()}
        s = sum(w.values())
        if s < 0.9999: w[spec.defensive] = w.get(spec.defensive,0)+(1-s)
        W[d] = w

    # Tages-Gewichtsmatrix, Ausfuehrung 1 Handelstag spaeter
    cols = sorted({a for w in W.values() for a in w})
    wm = pd.DataFrame({c: pd.Series({d: W[d].get(c, 0.0) for d in W}) for c in cols})
    wm = wm.reindex(lv.index).ffill().shift(2).dropna(how="all").fillna(0.0)

    L, sp, ex = spec.leverage, SPREAD[spec.leverage], EXPENSE[spec.leverage]
    idx = wm.index.intersection(r.index)
    wm, rr = wm.loc[idx], r.loc[idx]
    c = cash_ret.reindex(idx).fillna(0.0); dd = days.reindex(idx).fillna(1.0)
    lev_r = {}
    for a in cols:
        if a == "cash": lev_r[a] = c
        elif a in rr.columns:
            eff = 1.0 if a in NO_LEVERAGE else L
            lev_r[a] = (eff*rr[a] - (eff-1)*c - (eff-1)*sp*dd/365.25 - (ex if eff>1 else 0.0009)*dd/365.25).clip(lower=-0.999)
        else: lev_r[a] = c
    lr = pd.DataFrame(lev_r, index=idx)
    port = (wm*lr).sum(axis=1)
    turn = wm.diff().abs().sum(axis=1).fillna(0.0)
    out = (1+port)*(1-turn*cost_bps/10000.0)-1
    if return_weights:
        raw = pd.DataFrame({c: pd.Series({d: W[d].get(c, 0.0) for d in W}) for c in cols}).sort_index()
        return out, raw, lr
    return out

def metrics(x, cash_ret):
    x = x.dropna()
    if len(x) < 252: return None
    ann=252; nav=(1+x).cumprod()
    cagr=nav.iloc[-1]**(ann/len(x))-1
    vol=x.std(ddof=1)*np.sqrt(ann)
    ex=x-cash_ret.reindex(x.index).fillna(0)
    dd=(nav/nav.cummax()-1)
    return dict(cagr=float(cagr), vol=float(vol), sharpe=float(ex.mean()*ann/vol) if vol>0 else np.nan,
                maxdd=float(dd.min()), calmar=float(cagr/abs(dd.min())) if dd.min()<0 else np.nan,
                days=len(x), start=str(x.index[0].date()), end=str(x.index[-1].date()))
