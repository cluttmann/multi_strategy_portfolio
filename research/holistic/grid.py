"""Praeregistriertes Strategiegitter. Familien A-J aus PREREGISTRATION.md."""
from holistic.engine import Spec

MULTI   = ["us_large","us_small","eafe","em","reit","commodity","gold","tsy_mid","tsy_long"]
BROAD   = MULTI + ["nasdaq","europe","japan","pacific","corp_ig","corp_hy","mgd_futures"]
REGION  = ["us_large","europe","japan","pacific","em"]
SECTORS = ["sec_tech","sec_energy","sec_fin","sec_health","sec_indu","sec_staples","sec_disc","sec_util","sec_mat"]
DIVERS  = ["gold","commodity","mgd_futures","tsy_long","tsy_mid"]
LEVS    = [1.0,1.5,2.0,2.5,3.0]
LBSETS  = {"1/3/6/12":((1,3,6,12),None),"6m":((6,),None),"12m":((12,),None),
           "3/6/12":((3,6,12),None),"schnell 12/4/2/1":((1,3,6,12),(12,4,2,1))}

STATIC = {
 "60/40":                    {"us_large":.60,"agg":.40},
 "All-Weather":              {"us_large":.30,"tsy_long":.40,"tsy_mid":.15,"gold":.075,"commodity":.075},
 "Golden Butterfly":         {"us_large":.20,"us_small":.20,"tsy_long":.20,"tsy_mid":.20,"gold":.20},
 "Permanent Portfolio":      {"us_large":.25,"tsy_long":.25,"gold":.25,"cash":.25},
 "HFEA-Bauart 55/45":        {"us_large":.55,"tsy_long":.45},
 "Welt/Gold/Anleihen":       {"world":.40,"gold":.30,"tsy_long":.30},
 "Welt/Gold/MgdFut":         {"world":.40,"gold":.30,"mgd_futures":.30},
 "Global Equity gleich":     {"us_large":.34,"eafe":.33,"em":.33},
 "Welt 60 / Gold 20 / Tsy 20":{"world":.60,"gold":.20,"tsy_long":.20},
 "Vier Saeulen":             {"world":.35,"tsy_long":.25,"gold":.20,"mgd_futures":.20},
}

def build():
    out=[]
    # A — statische Gewichte
    for nm,fx in STATIC.items():
        for L in LEVS:
            out.append(Spec(f"A {nm} {L:g}x","A Statisch",[k for k in fx if k!="cash"],
                weighting="fixed",fixed=fx,absolute=False,leverage=L,note="feste Gewichte, Quartalsrebal"))
    # B — absoluter Trend (SMA)
    for uni,un in [(["world"],"Welt"),(MULTI,"Multi-Asset"),(REGION,"Regionen")]:
        for sma in [100,150,200,250]:
            for L in LEVS:
                out.append(Spec(f"B Trend{sma} {un} {L:g}x","B Trend",uni,trend_sma=sma,
                    weighting="equal",leverage=L,note=f"Preis>SMA{sma}, sonst Cash"))
    # C/D — relatives + duales Momentum
    for uni,un in [(MULTI,"Multi-Asset"),(BROAD,"Breit"),(REGION,"Regionen")]:
        for n in [2,3,4,5]:
            for lbn,(lb,lw) in LBSETS.items():
                for L in LEVS:
                    for absf,fam in [(False,"C Rel-Momentum"),(True,"D Dual Momentum")]:
                        out.append(Spec(f"{fam[0]} Top{n} {un} {lbn} {L:g}x",fam,uni,top_n=n,
                            lookbacks=lb,lb_weights=lw,absolute=absf,weighting="equal",leverage=L,
                            note=f"Top-{n} nach {lbn}-Momentum"+(", Score<=0 -> Cash" if absf else "")))
    # E — Canary
    for cn,can in [("BAA-Canary",("us_large","em","eafe","agg"))]:
        for n in [3,4]:
            for L in LEVS:
                out.append(Spec(f"E {cn} Top{n} {L:g}x","E Canary",MULTI,top_n=n,canary=can,
                    weighting="equal",leverage=L,note="alle Canaries positiv, sonst voll defensiv"))
    # F — Adaptive Asset Allocation
    for uni,un in [(MULTI,"Multi-Asset"),(BROAD,"Breit")]:
        for n in [3,4,5]:
            for vt in [0.0,0.15,0.20,0.25]:
                for L in LEVS:
                    out.append(Spec(f"F AAA Top{n} {un} vol{int(vt*100) if vt else 0} {L:g}x","F AAA",uni,
                        top_n=n,absolute=True,weighting="invvol",vol_target=vt,leverage=L,
                        note=f"Top-{n}, invers-Vol"+(f", Vol-Ziel {int(vt*100)}%" if vt else "")))
    # G — Risk Parity
    for uni,un in [(MULTI,"Multi-Asset"),(DIVERS,"Diversifikatoren"),(BROAD,"Breit")]:
        for L in LEVS:
            out.append(Spec(f"G RiskParity {un} {L:g}x","G Risk Parity",uni,weighting="invvol",
                absolute=False,leverage=L,note="invers-Vol ueber alle, kein Timing"))
    # H — Sektorrotation
    for n in [2,3,4]:
        for lbn,(lb,lw) in list(LBSETS.items())[:4]:
            for L in LEVS:
                out.append(Spec(f"H Sektor Top{n} {lbn} {L:g}x","H Sektorrotation",SECTORS,top_n=n,
                    lookbacks=lb,lb_weights=lw,absolute=True,weighting="equal",leverage=L,
                    note=f"Top-{n} von 9 US-Sektoren"))
    # I — Regionenrotation mit Vol-Gewichtung
    for n in [2,3]:
        for vt in [0.0,0.20]:
            for L in LEVS:
                out.append(Spec(f"I Region Top{n} vol{int(vt*100)} {L:g}x","I Regionenrotation",REGION,
                    top_n=n,absolute=True,weighting="invvol",vol_target=vt,leverage=L,
                    note=f"Top-{n} von 5 Weltregionen"))
    # J — Trend plus Diversifikator
    for sma in [150,200]:
        for div,dn in [("gold","Gold"),("mgd_futures","MgdFut"),("tsy_long","Langlaeufer")]:
            for L in LEVS:
                out.append(Spec(f"J Welt-Trend{sma} + {dn} {L:g}x","J Trend+Diversifikator",
                    ["world",div],trend_sma=sma,weighting="equal",leverage=L,
                    note=f"Welt im Trend, {dn} als zweites Bein"))
    return out

if __name__=="__main__":
    import collections
    g=build(); c=collections.Counter(s.family for s in g)
    for k,v in sorted(c.items()): print(f"{k:24s} {v:4d}")
    print(f"{'GESAMT':24s} {len(g):4d}")
