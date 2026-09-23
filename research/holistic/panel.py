"""Datenpanel fuer die holistische Strategiesuche. Research only.

Baut Total-Return-Tagesreihen fuer Assetklassen, Regionen und Sektoren.
Langzeit-Proxy (Investmentfonds/Index) wird am Auflagetag des echten ETF
gespliced. Splice-Datum und Quelle werden protokolliert.
"""
from pathlib import Path
import json, os, urllib.request, concurrent.futures as cf
import numpy as np, pandas as pd
from dotenv import dotenv_values

ROOT = Path("/Users/carl/Coding/hfea_strategy")
SIM  = ROOT / "research/data"
CACHE = Path("/tmp/holistic_panel"); CACHE.mkdir(exist_ok=True)
ENV  = dotenv_values(ROOT / ".env")
TOK  = ENV.get("EODHD_TOKEN"); FRED = ENV.get("FREDKEY")

# (key, beschreibung, proxy-symbol, echter ETF, splice-datum)
SPEC = [
 ("us_large","US Large Cap",              None,        "SPY",  None),
 ("us_small","US Small Cap",              "NAESX.US",  "IWM",  "2000-05-26"),
 ("us_smallval","US Small Value",         "VISVX.US",  "IJS",  "2000-07-28"),
 ("us_largeval","US Large Value",         "VIVAX.US",  "VTV",  "2004-01-30"),
 ("nasdaq","Nasdaq-100",                  None,        "QQQ",  None),
 ("eafe","Industrielaender ex USA",       None,        "EFA",  None),
 ("europe","Europa",                      "VEURX.US",  "VGK",  "2005-03-10"),
 ("japan","Japan",                        "FJPNX.US",  "EWJ",  "1996-03-18"),
 ("pacific","Pazifik ex Japan",           "VPACX.US",  "VPL",  "2005-03-10"),
 ("em","Emerging Markets",                "VEIEX.US",  "EEM",  "2003-04-11"),
 ("reit","US REITs",                      "VGSIX.US",  "VNQ",  "2004-09-29"),
 ("gold","Gold",                          None,        "GLD",  None),
 ("silver","Silber",                      None,        "SLV",  None),
 ("commodity","Rohstoffe (GSCI TR)",      "SPGSCITR.INDX","DBC","2006-02-03"),
 ("tsy_mid","Treasuries 7-10J",           None,        "IEF",  None),
 ("tsy_long","Treasuries 20J+",           None,        "TLT",  None),
 ("tips","TIPS",                          "VIPSX.US",  "TIP",  "2003-12-05"),
 ("corp_ig","IG Corporates",              "VWESX.US",  "LQD",  "2002-07-30"),
 ("corp_hy","High Yield",                 "VWEHX.US",  "HYG",  "2007-04-11"),
 ("agg","US Aggregate",                   None,        "BND",  None),
 ("mgd_futures","Managed Futures",        None,        "KMLM", None),
 ("world","MSCI World",                   None,        "URTH", None),
]
SECTORS = [("sec_tech","Technologie","XLK"),("sec_energy","Energie","XLE"),
 ("sec_fin","Financials","XLF"),("sec_health","Healthcare","XLV"),
 ("sec_indu","Industrials","XLI"),("sec_staples","Consumer Staples","XLP"),
 ("sec_disc","Consumer Discretionary","XLY"),("sec_util","Utilities","XLU"),
 ("sec_mat","Materials","XLB")]
# SIM-CSVs fuer die Bausteine mit Testfolio-Langhistorie
SIMFILES = {"us_large":"SPYSIM_daily_returns_1885-2026.csv","eafe":"EFASIM_daily_returns_1970-2026.csv",
 "tsy_mid":"IEFSIM_daily_returns_1962-2026.csv","tsy_long":"TLTSIM_daily_returns_1962-2026.csv",
 "gold":"GLDSIM_daily_returns_1968-2026.csv","silver":"SLVSIM_daily_returns_1968-2026.csv",
 "nasdaq":"QQQSIM_daily_returns_1986-2026.csv","agg":"BND_daily_returns_1986-2026.csv",
 "mgd_futures":"KMLMSIM_daily_returns.csv","world":"URTHSIM_daily_returns_1970-2026.csv"}
SIM_SPLICE = {"us_large":"1993-01-29","eafe":"2001-08-14","tsy_mid":"2002-07-30","tsy_long":"2002-07-30",
 "gold":"2004-11-18","silver":"2006-04-21","nasdaq":"1999-03-10","agg":"2007-04-03",
 "mgd_futures":"2020-12-02","world":"2012-01-12"}
# Reale gehebelte ETFs fuer die Kalibrierung des Hebelmodells
LEV_ETFS = {"SSO":("us_large",2.0),"UPRO":("us_large",3.0),"SPXL":("us_large",3.0),
 "QLD":("nasdaq",2.0),"TQQQ":("nasdaq",3.0),"EFO":("eafe",2.0),"EET":("em",2.0),
 "UST":("tsy_mid",2.0),"UBT":("tsy_long",2.0),"UGL":("gold",2.0),"TMF":("tsy_long",3.0),
 "UWM":("us_small",2.0),"URE":("reit",2.0),"AGQ":("silver",2.0)}

def _eod(sym):
    p = CACHE/f"{sym.replace('.','_')}.json"
    if p.exists(): rows = json.loads(p.read_text())
    else:
        u=f"https://eodhd.com/api/eod/{sym}?from=1985-01-01&to=2026-09-12&period=d&fmt=json&api_token={TOK}"
        rows=json.load(urllib.request.urlopen(u,timeout=90)); p.write_text(json.dumps(rows))
    if not isinstance(rows,list) or len(rows)<50: return None
    d=pd.DataFrame(rows).set_index("date"); d.index=pd.to_datetime(d.index)
    s=pd.to_numeric(d["adjusted_close"],errors="coerce").sort_index().dropna()
    return s[~s.index.duplicated(keep="last")]

def _sim(fn):
    d=pd.read_csv(SIM/fn)
    dc="date" if "date" in d.columns else "Date"; rc="return_pct" if "return_pct" in d.columns else "Return (%)"
    d[dc]=pd.to_datetime(d[dc]); s=d.set_index(dc)[rc].sort_index()/100.0
    s=s[~s.index.duplicated(keep="last")]
    return (1+s).cumprod()*100.0

def _clean(s,cap=0.30):
    r=s.pct_change()
    bad=r.abs()>cap
    if bad.any(): s=s[~bad.reindex(s.index,fill_value=False)]
    return s

def _splice(proxy,real,date):
    """Proxy bis `date`, danach real. Level werden am Splice-Tag verkettet."""
    if proxy is None: return real,"nur echt"
    if real is None or len(real)==0: return proxy,"nur Proxy"
    d=pd.Timestamp(date)
    pre=proxy[proxy.index<d]; post=real[real.index>=d]
    if len(pre)==0: return real,"nur echt"
    if len(post)==0: return proxy,"nur Proxy"
    scale=pre.iloc[-1]/post.iloc[0]
    return pd.concat([pre,post*scale]),f"Proxy bis {date}, danach echt"

def build():
    log={}
    series={}
    todo=[]
    for key,desc,proxy,etf,sd in SPEC:
        todo.append((key,desc,proxy,etf,sd))
    for key,desc,etf in SECTORS:
        todo.append((key,desc,None,etf,None))
    syms=set()
    for key,desc,proxy,etf,sd in todo:
        if proxy: syms.add(proxy)
        if etf: syms.add(etf if "." in etf else etf+".US")
    syms |= {k+".US" for k in LEV_ETFS}
    with cf.ThreadPoolExecutor(10) as ex:
        fetched=dict(zip(syms,ex.map(_eod,syms)))
    for key,desc,proxy,etf,sd in todo:
        real=fetched.get(etf if "." in etf else etf+".US") if etf else None
        if key in SIMFILES and (SIM/SIMFILES[key]).exists():
            p=_sim(SIMFILES[key]); s,note=_splice(p,real,SIM_SPLICE[key]); note="SIM "+note
        else:
            p=fetched.get(proxy) if proxy else None
            s,note=_splice(p,real,sd) if sd else (real,"nur echt")
        if s is None: print(f"  ! {key}: keine Daten"); continue
        s=_clean(s)
        series[key]=s; log[key]={"beschreibung":desc,"proxy":proxy,"etf":etf,"splice":sd,"quelle":note,
                                 "von":str(s.index[0].date()),"bis":str(s.index[-1].date()),"tage":len(s)}
    lev={}
    for t,(base,L) in LEV_ETFS.items():
        s=fetched.get(t+".US")
        if s is not None: lev[t]=_clean(s)
    # Cash + Finanzierung
    def fred(sid):
        p=CACHE/f"fred_{sid}.json"
        if p.exists(): obs=json.loads(p.read_text())
        else:
            u=(f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}"
               f"&api_key={FRED}&file_type=json&observation_start=1990-01-01&observation_end=2026-09-12")
            obs=json.load(urllib.request.urlopen(u,timeout=60))["observations"]; p.write_text(json.dumps(obs))
        d=pd.DataFrame(obs); d["date"]=pd.to_datetime(d.date)
        return pd.to_numeric(d.set_index("date").value,errors="coerce").ffill()
    return series,lev,fred("DGS3MO"),fred("DFF"),log

if __name__=="__main__":
    series,lev,bill,dff,log=build()
    idx=None
    for k,s in series.items():
        idx=s.index if idx is None else idx.union(s.index)
    print(f"\n{'Baustein':14s} {'Beschreibung':26s} {'von':11s} {'bis':11s} {'Tage':>6s}  Quelle")
    for k,v in log.items():
        print(f"{k:14s} {v['beschreibung']:26s} {v['von']:11s} {v['bis']:11s} {v['tage']:6d}  {v['quelle']}")
    print(f"\n{len(series)} Bausteine, {len(lev)} reale Hebel-ETFs zur Kalibrierung")
    out=CACHE/"panel.pkl"
    pd.to_pickle({"series":series,"lev":lev,"bill":bill,"dff":dff,"log":log},out)
    print("gespeichert:",out)
