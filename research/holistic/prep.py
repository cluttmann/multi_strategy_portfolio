"""Panel -> gemeinsames Level-Frame + Cash + Kalendertage."""
import pandas as pd, numpy as np
def load(start="2000-01-03", end="2026-09-11"):
    p = pd.read_pickle("/tmp/holistic_panel/panel.pkl")
    s = p["series"]
    idx = None
    for k in ["us_large","tsy_mid","gold"]:
        i = s[k].loc[start:end].index
        idx = i if idx is None else idx.intersection(i)
    lv = pd.DataFrame({k: v.reindex(idx, method="ffill", limit=5) for k, v in s.items()})
    days = idx.to_series().diff().dt.days.fillna(1).astype(float)
    bill = p["bill"].reindex(idx, method="ffill").ffill().bfill()
    cash = (bill/100*days/365.25)
    dff  = p["dff"].reindex(idx, method="ffill").ffill().bfill()
    fin  = (dff/100*days/365.25)
    lv["cash"] = (1+cash).cumprod()*100
    return lv, fin, days, p["log"]
