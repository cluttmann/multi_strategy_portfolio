"""Hebelmodell + Kalibrierung gegen reale gehebelte ETFs."""
import numpy as np, pandas as pd
from scipy.optimize import minimize_scalar

def daily_leverage(r, cash, days, L, spread, expense):
    """Taeglicher Reset. Nur das Notional ueber 1 wird finanziert."""
    if L < 1: raise ValueError("L>=1")
    f = L*r - (L-1)*cash - (L-1)*spread*days/365.25 - expense*days/365.25
    return f.clip(lower=-0.999)

def calibrate(panel):
    s, lev, bill, dff = panel["series"], panel["lev"], panel["bill"], panel["dff"]
    rows=[]
    for etf,(base,L) in {"SSO":("us_large",2.0),"UPRO":("us_large",3.0),"SPXL":("us_large",3.0),
        "QLD":("nasdaq",2.0),"TQQQ":("nasdaq",3.0),"EFO":("eafe",2.0),"EET":("em",2.0),
        "UST":("tsy_mid",2.0),"UBT":("tsy_long",2.0),"UGL":("gold",2.0),"TMF":("tsy_long",3.0),
        "UWM":("us_small",2.0),"URE":("reit",2.0),"AGQ":("silver",2.0)}.items():
        if etf not in lev or base not in s: continue
        real=lev[etf].pct_change().dropna(); und=s[base].pct_change().dropna()
        j=real.index.intersection(und.index)
        if len(j)<500: continue
        d=j.to_series().diff().dt.days.fillna(1).astype(float)
        c=(dff.reindex(j,method="ffill").shift(1).bfill()/100*d/365.25)
        def err(x):
            m=daily_leverage(und[j],c,d,L,x,0.0095)
            return float(np.sqrt(((m-real[j])**2).mean()))
        r_=minimize_scalar(err,bounds=(0.0,0.05),method="bounded")
        sp=r_.x
        m=daily_leverage(und[j],c,d,L,sp,0.0095)
        te=float((m-real[j]).std()*np.sqrt(252))
        cagr_m=(1+m).prod()**(252/len(j))-1; cagr_r=(1+real[j]).prod()**(252/len(j))-1
        rows.append(dict(etf=etf,basis=base,L=L,tage=len(j),spread=sp,
                         tracking_err=te,korr=float(np.corrcoef(m,real[j])[0,1]),
                         cagr_modell=cagr_m,cagr_real=cagr_r,abw=cagr_m-cagr_r,
                         von=str(j[0].date())))
    return pd.DataFrame(rows)

if __name__=="__main__":
    p=pd.read_pickle("/tmp/holistic_panel/panel.pkl")
    df=calibrate(p)
    pd.set_option("display.width",220)
    print("=== Kalibrierung des Hebelmodells gegen reale ETFs ===\n")
    print(f"{'ETF':6s} {'Basis':12s} {'L':>4s} {'Tage':>6s} {'Spread':>8s} {'TrackErr':>9s} {'Korr':>6s} {'CAGR Mod':>9s} {'CAGR real':>10s} {'Diff':>7s}")
    for _,r in df.sort_values("L").iterrows():
        print(f"{r.etf:6s} {r.basis:12s} {r.L:4.1f} {int(r.tage):6d} {r.spread*100:7.2f}% {r.tracking_err*100:8.2f}% "
              f"{r.korr:6.4f} {r.cagr_modell*100:8.1f}% {r.cagr_real*100:9.1f}% {r.abw*100:6.2f}pp")
    print(f"\nMedian-Spread: {df.spread.median()*100:.2f}%  |  Median-Tracking-Error: {df.tracking_err.median()*100:.2f}%")
    print("Median |CAGR-Abweichung|: %.2fpp  |  min Korrelation: %.4f" % (df["abw"].abs().median()*100, df.korr.min()))
    df.to_csv("/tmp/holistic_panel/leverage_calibration.csv",index=False)
