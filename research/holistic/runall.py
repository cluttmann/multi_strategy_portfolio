import sys, time, numpy as np, pandas as pd
sys.path.insert(0,'/Users/carl/Coding/hfea_strategy/research')
from scipy import stats
from holistic.prep import load
from holistic.engine import run, metrics
from holistic.grid import build

CRISES={"Dotcom":("2000-03-24","2002-10-09"),"Finanzkrise":("2007-10-09","2009-03-09"),
        "Covid":("2020-02-19","2020-03-23"),"Zinsschock 2022":("2022-01-03","2022-12-30"),
        "Zollschock 2025":("2025-02-19","2025-04-08")}
IS_END="2014-12-31"; OOS_START="2015-01-02"

def win(x,cash,a,b):
    y=x.loc[a:b]
    if len(y)<10: return None,None
    nav=(1+y).cumprod(); return float(nav.iloc[-1]-1), float((nav/nav.cummax()-1).min())

def main():
    lv,fin,days,log=load(); g=build()
    rows=[]; rets={}
    t0=time.time()
    for i,s in enumerate(g):
        r=run(s,lv,fin,days)
        if r is None: continue
        m=metrics(r,fin)
        if m is None: continue
        mi=metrics(r.loc[:IS_END],fin); mo=metrics(r.loc[OOS_START:],fin)
        row=dict(name=s.name,family=s.family,leverage=s.leverage,note=s.note,
                 universe="|".join(s.universe),**m)
        row["sharpe_is"]=mi["sharpe"] if mi else np.nan
        row["sharpe_oos"]=mo["sharpe"] if mo else np.nan
        row["cagr_oos"]=mo["cagr"] if mo else np.nan
        row["delta"]=row["sharpe_oos"]-row["sharpe_is"]
        for cn,(a,b) in CRISES.items():
            tr,dd=win(r,fin,a,b); row[f"k_{cn}"]=tr; row[f"kdd_{cn}"]=dd
        rows.append(row); rets[s.name]=r
        if (i+1)%200==0: print(f"  {i+1}/{len(g)}  ({time.time()-t0:.0f}s)",flush=True)
    df=pd.DataFrame(rows)
    # Deflated Sharpe mit tatsaechlichem N
    N=len(df); e=0.5772156649
    v=(df.sharpe/np.sqrt(252)).std(ddof=1)
    sr0=v*((1-e)*stats.norm.ppf(1-1/N)+e*stats.norm.ppf(1-1/(N*np.e)))
    dsr=[]
    for _,r_ in df.iterrows():
        x=rets[r_["name"]].dropna(); ex=x-fin.reindex(x.index).fillna(0)
        n=len(ex); d=ex.mean()/ex.std(ddof=1)
        sk=stats.skew(ex); ku=stats.kurtosis(ex,fisher=False)
        sd=np.sqrt(max((1-sk*d+(ku-1)/4*d**2)/(n-1),1e-12))
        dsr.append(float(stats.norm.cdf((d-sr0)/sd)))
    df["dsr"]=dsr; df["N_versuche"]=N; df["sr0_zufall"]=sr0*np.sqrt(252)
    df.to_parquet("/tmp/holistic_panel/results.parquet",index=False)
    pd.DataFrame(rets).to_parquet("/tmp/holistic_panel/returns.parquet")
    print(f"\n{N} Konfigurationen in {time.time()-t0:.0f}s")
    print(f"Erwarteter max. Sharpe rein zufaellig bei N={N}: {sr0*np.sqrt(252):.3f}")
    print(f"Bestehen die Deflation (DSR>0.95): {(df.dsr>0.95).sum()} von {N}")
    return df

if __name__=="__main__":
    main()
