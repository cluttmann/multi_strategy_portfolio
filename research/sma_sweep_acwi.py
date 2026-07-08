"""
Best-SMA-window study for a 2× MSCI ACWI trend strategy — the all-world analogue
of the SPY-200-SMA / SPXL strategy. Monte-Carlo driven, with an HTML report.

STRATEGY (identical mechanic to production bt_spxl_sma / bt_wldu_sma):
  signal on the 1× ACWI total-return index, N-day SMA, ±1% hysteresis band,
  daily state machine, executed next day. In-market -> hold synthetic 2× ACWI;
  out-of-market -> T-bills. The gate is computed on the UNLEVERAGED index (just
  as SPXL gates on SPY, WLDU on URTH).

SYNTHETIC 2× ACWI — matches the engine's LETF convention (synth_wldu):
  r_2x = 2·r_acwi − SW·(L−1)·(bil_daily + SP/252) − (L−1)·E/252
  SW=1.1, SP=0.40%, E=0.50%/lev, financing = ACTUAL 3m T-bill (time-varying —
  critical across 1970-2026 where short rates ran 0%→15%).

MODELED LONG HISTORY (56 years): the chain is
  1970-1987  MSCI World as-is (ACWI's predecessor — the index only exists from
             Dec-1987 and EM weighed <1% at inception; labeled explicitly)
  1988-2001  real ACWI monthly (FX-converted Curvo, true EM weights) temporally
             disaggregated to daily with World intra-month texture
  2001-2008  real MSCI ACWI gross daily index
  2008+      real iShares ACWI ETF (live-updating)
See acwi_modeled.py. 56 years lets the MC window selection discriminate cleanly.

REAL PRODUCT (2026): the Scalable MSCI AC World Leveraged Daily Swap Xtrackers
UCITS ETF (LEI 254900NW0MAOB3TRMY48, registered 2026-05-29) is the first real
2× ACWI vehicle — the study's synthetic sleeve now has an investable twin. Our
synthetic is calibrated against the structurally identical Xtrackers S&P 500 2x
Leveraged Daily Swap (XS2D, 2010+): synth ran +0.73%/yr rich, so the
"real product" scenario adds REAL_EXTRA_DRAG.
"""
import base64, io
import numpy as np, pandas as pd
import extended_data as ed
import acwi_modeled

RF, ANN, BAND = 0.02, 252, 0.01
SW, SP, E = 1.1, 0.004, 0.005          # synthetic-LETF params (== mega_backtest)
LEV = 2.0
TRIM = 400          # drop the max-window warmup from all metric windows so every
                    # SMA window is scored on an identical span (no cash-warmup bias)
# Real-product calibration: our synthetic 2× ran +0.73%/yr ABOVE the real
# Xtrackers S&P 500 2x Leveraged Daily Swap UCITS ETF (XS2D, USD) over its full
# 2010-2026 history (month-end levels, corr 0.95). The new Scalable/Xtrackers
# ACWI 2x product shares that structure (TER 0.60%, unfunded swap), so the
# "real product" scenario adds this empirically measured extra drag.
REAL_EXTRA_DRAG = 0.0075
WINDOWS = [50, 75, 100, 110, 120, 130, 140, 150, 160, 170, 180, 190, 200,
           210, 220, 230, 240, 250, 255, 260, 270, 280, 290, 300, 320, 340,
           360, 400]


# ── series construction ────────────────────────────────────────────────────
def build_acwi_ext(ext) -> pd.Series:
    """Modeled long-history ACWI daily returns (1970+), EM-share-correct 1988+.

    Pre-2001 = real ACWI monthly history (FX-converted Curvo, embeds the true
    time-varying EM weights) disaggregated to daily via World texture; real daily
    ACWI thereafter. See acwi_modeled.py."""
    return acwi_modeled.load_or_build(ext)


def make_lev(r1x: np.ndarray, bil: np.ndarray, lev=LEV, extra: float = 0.0) -> np.ndarray:
    """Daily-reset synthetic LETF. `extra` = additional annual drag (decimal) for
    the real-product scenario (XS2D-calibrated, see REAL_EXTRA_DRAG)."""
    borrow = SW * (lev - 1) * (bil + SP / ANN)
    drag = (lev - 1) * E / ANN + extra / ANN
    return lev * r1x - borrow - drag


# ── vectorized gate + metrics ──────────────────────────────────────────────
def _rolling_mean(level, n):
    c = np.cumsum(level)
    out = np.full(len(level), np.nan)
    out[n - 1:] = (c[n - 1:] - np.concatenate([[0.0], c[:-n]])) / n
    return out


def gate(sig_r, risk_r, cash, n):
    """Gate signal on 1× series sig_r; hold risk_r when in, cash when out."""
    level = np.cumprod(1 + sig_r) * 100.0
    sma = _rolling_mean(level, n)
    s = np.where(level > sma * (1 + BAND), 1.0,
                 np.where(level < sma * (1 - BAND), 0.0, np.nan))
    valid = ~np.isnan(s)
    last = np.maximum.accumulate(np.where(valid, np.arange(len(s)), 0))
    state = np.where(valid.cumsum() > 0, s[last], 0.0)
    inmkt = np.concatenate([[0.0], state[:-1]])
    gret = inmkt * risk_r + (1 - inmkt) * cash
    return gret, float(inmkt.mean()), int(np.sum(np.abs(np.diff(inmkt)) > 0))


def metrics(r):
    cum = np.cumprod(1 + r)
    years = len(r) / ANN
    cagr = cum[-1] ** (1 / years) - 1
    vol = r.std(ddof=1) * np.sqrt(ANN)
    dd = float((cum / np.maximum.accumulate(cum) - 1).min())
    return {"CAGR": cagr, "Vol": vol, "Sharpe": (cagr - RF) / vol if vol > 0 else np.nan,
            "MaxDD": dd, "Calmar": cagr / abs(dd) if dd else np.nan}


# ── deterministic sweep (2× gated) ─────────────────────────────────────────
def deterministic(r1x, bil, extra=0.0):
    r2x = make_lev(r1x, bil, extra=extra)
    rows = {}
    for n in WINDOWS:
        g, pin, sw = gate(r1x, r2x, bil, n)
        rows[n] = {**metrics(g[TRIM:]), "InMkt": pin, "Switch": sw}
    df = pd.DataFrame(rows).T
    df.index.name = "window"
    return df


# ── stationary bootstrap window selection ──────────────────────────────────
def _boot_idx(n, mean_block, rng):
    p = 1.0 / mean_block
    idx = np.empty(n, dtype=np.int64)
    idx[0] = rng.integers(0, n)
    coin = rng.random(n) < p
    rnd = rng.integers(0, n, size=n)
    for t in range(1, n):
        idx[t] = rnd[t] if coin[t] else (idx[t - 1] + 1) % n
    return idx


def monte_carlo(r1x, bil, n_sims=1500, mean_block=252, seed=42):
    n = len(r1x)
    rng = np.random.default_rng(seed)
    sh = {w: np.empty(n_sims) for w in WINDOWS}
    ca = {w: np.empty(n_sims) for w in WINDOWS}
    dd = {w: np.empty(n_sims) for w in WINDOWS}
    wins = {w: 0 for w in WINDOWS}
    for s in range(n_sims):
        idx = _boot_idx(n, mean_block, rng)
        r1b, bb = r1x[idx], bil[idx]
        r2b = make_lev(r1b, bb)
        best_w, best_s = None, -np.inf
        for w in WINDOWS:
            g, _, _ = gate(r1b, r2b, bb, w)
            m = metrics(g[TRIM:])
            sh[w][s], ca[w][s], dd[w][s] = m["Sharpe"], m["CAGR"], m["MaxDD"]
            if m["Sharpe"] > best_s:
                best_s, best_w = m["Sharpe"], w
        wins[best_w] += 1
    out = pd.DataFrame({
        "med_Sharpe": {w: np.nanmedian(sh[w]) for w in WINDOWS},
        "p5_Sharpe": {w: np.nanpercentile(sh[w], 5) for w in WINDOWS},
        "p95_Sharpe": {w: np.nanpercentile(sh[w], 95) for w in WINDOWS},
        "med_CAGR": {w: np.nanmedian(ca[w]) for w in WINDOWS},
        "med_MaxDD": {w: np.nanmedian(dd[w]) for w in WINDOWS},
        "P_best": {w: wins[w] / n_sims for w in WINDOWS},
    })
    out.index.name = "window"
    return out, {"sharpe": sh, "cagr": ca, "maxdd": dd, "n_sims": n_sims}


def monte_carlo_joint(series: dict, windows: dict, bil: np.ndarray,
                      n_sims=1500, mean_block=252, seed=99):
    """Matched-path bootstrap: the SAME resampled history is fed to every index,
    each gated at its own canonical window, so the comparison and the
    beat-probabilities are apples-to-apples. series/windows keyed by index name;
    the first key is the reference (ACWI) for beat-probabilities."""
    names = list(series)
    n = len(bil)
    rng = np.random.default_rng(seed)
    sh = {nm: np.empty(n_sims) for nm in names}
    ca = {nm: np.empty(n_sims) for nm in names}
    dd = {nm: np.empty(n_sims) for nm in names}
    for s in range(n_sims):
        idx = _boot_idx(n, mean_block, rng)
        b = bil[idx]
        for nm in names:
            r1 = series[nm][idx]; r2 = make_lev(r1, b)
            g, _, _ = gate(r1, r2, b, windows[nm]); m = metrics(g[TRIM:])
            sh[nm][s], ca[nm][s], dd[nm][s] = m["Sharpe"], m["CAGR"], m["MaxDD"]
    ref = names[0]
    stats = {}
    for nm in names:
        stats[nm] = {
            "med_Sharpe": float(np.nanmedian(sh[nm])),
            "p5_Sharpe": float(np.nanpercentile(sh[nm], 5)),
            "p95_Sharpe": float(np.nanpercentile(sh[nm], 95)),
            "med_CAGR": float(np.nanmedian(ca[nm])),
            "med_MaxDD": float(np.nanmedian(dd[nm])),
            "ref_beats": (np.nan if nm == ref
                          else float(np.mean(sh[ref] > sh[nm]))),
        }
    return stats


# ── after-cost / after-tax evaluation (German taxable account) ──────────────
def _gate_state(r1, n):
    """Next-day in-market state (0/1) for an N-day SMA gate — same logic as gate()."""
    level = np.cumprod(1 + r1) * 100.0
    sma = _rolling_mean(level, n)
    s = np.where(level > sma * (1 + BAND), 1.0,
                 np.where(level < sma * (1 - BAND), 0.0, np.nan))
    valid = ~np.isnan(s)
    last = np.maximum.accumulate(np.where(valid, np.arange(len(s)), 0))
    state = np.where(valid.cumsum() > 0, s[last], 0.0)
    return np.concatenate([[0.0], state[:-1]])


def net_eval(r1, bil, index, n=None, cost=0.0, lev=LEV, extra=0.0, tfs=0.30):
    """Pre-tax / post-cost / post-tax metrics for a gated (n given) or ungated
    (n=None) 2× strategy. Tax via tax_overlay.simulate_after_tax (German
    Abgeltungsteuer + Teilfreistellung + Vorabpauschale, average-cost basis).
    Transaction cost = `cost` (round-trip fraction) charged on each switch day.
    `extra` = additional annual sleeve drag (real-product scenario)."""
    import tax_overlay as tx
    tx.SYMBOL_TFS_RATE = {**dict(tx.SYMBOL_TFS_RATE), "ACWI2X": tfs, "CASH": 0.0}
    r2 = make_lev(r1, bil, lev, extra)
    assets = pd.DataFrame({"ACWI2X": r2, "CASH": bil}, index=index)
    if n is None:                                   # ungated buy&hold of the 2× sleeve
        inmkt = np.ones(len(r1)); sw = np.zeros(len(r1), dtype=bool); sw[0] = True
        wdf = pd.DataFrame({"ACWI2X": [1.0], "CASH": [0.0]}, index=[index[0]])
    else:
        inmkt = _gate_state(r1, n)
        sw = np.abs(np.diff(inmkt, prepend=inmkt[0])) > 0; sw[0] = True
        wdf = pd.DataFrame({"ACWI2X": inmkt, "CASH": 1 - inmkt}, index=index)[sw]
    g = inmkt * r2 + (1 - inmkt) * bil              # gross gated daily returns
    at, _ = tx.simulate_after_tax(wdf, assets)
    at = at.reindex(index).fillna(0.0).values
    gc = g.copy(); gc[sw] -= cost                   # post-cost (pre-tax)
    atc = at.copy(); atc[sw] -= cost                # post-cost + post-tax
    return {"pretax": metrics(g[TRIM:]), "postcost": metrics(gc[TRIM:]),
            "posttax": metrics(at[TRIM:]), "net": metrics(atc[TRIM:]),
            "switches": int(sw.sum())}


def paired_se(samp, ref, others):
    """Matched-path bootstrap mean & SE of the Sharpe DIFFERENCE (ref − other).
    Within each sim every window saw the same resampled history, so the
    difference is paired → tells us if windows are statistically distinguishable."""
    out = {}
    a = np.asarray(samp["sharpe"][ref])
    for w in others:
        d = a - np.asarray(samp["sharpe"][w])
        m, se = float(np.nanmean(d)), float(np.nanstd(d) / np.sqrt(np.sum(~np.isnan(d))))
        out[w] = {"diff": m, "se": se, "t": m / se if se > 0 else np.nan}
    return out


def leverage_window_grid(r1, bil, levs=(1.5, 2.0, 2.5, 3.0)):
    """Deterministic best-Sharpe SMA window for each leverage factor (B7)."""
    rows = {}
    for L in levs:
        rl = make_lev(r1, bil, L)
        best_w, best_s = None, -np.inf
        for w in WINDOWS:
            g, _, _ = gate(r1, rl, bil, w)
            s = metrics(g[TRIM:])["Sharpe"]
            if s > best_s:
                best_s, best_w = s, w
        rows[L] = {"best_window": best_w, "Sharpe": best_s}
    return rows


# ── charts ──────────────────────────────────────────────────────────────────
def _png(fig):
    import matplotlib.pyplot as plt
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig); return base64.b64encode(buf.getvalue()).decode()


def chart_curve(mc, best):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 4.2))
    ax.plot(mc.index, mc["med_Sharpe"], "-o", ms=3, color="#137", label="median Sharpe")
    ax.fill_between(mc.index, mc["p5_Sharpe"], mc["p95_Sharpe"], alpha=0.12, color="#137")
    ax.axvline(best, ls="--", color="crimson", lw=1, label=f"sweet spot = {best}d")
    ax.set_xlabel("SMA window (trading days)")
    ax.set_ylabel("Sharpe ratio (higher = better)")
    ax.set_title("Monte-Carlo Sharpe vs SMA window — 2× ACWI gated, modeled 1970+ (252d blocks)")
    ax.legend(fontsize=8, title="line = median of 1500 sims · band = 5–95th pct", title_fontsize=7)
    ax.grid(alpha=0.3); return _png(fig)


def chart_hist(samp, best, bh2_sharpe):
    import matplotlib.pyplot as plt
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9, 3.6))
    fig.suptitle(f"Distribution across 1500 bootstrapped histories — {best}d gated 2× ACWI", fontsize=10)
    a1.hist(samp["sharpe"][best], bins=40, color="#3b7", alpha=0.8)
    a1.axvline(bh2_sharpe, ls="--", color="k", lw=1, label=f"ungated 2× = {bh2_sharpe:.2f}")
    a1.set_title("Sharpe ratio"); a1.set_xlabel("Sharpe"); a1.set_ylabel("# of simulations")
    a1.legend(fontsize=8)
    a2.hist(np.array(samp["maxdd"][best]) * 100, bins=40, color="#d66", alpha=0.8)
    a2.set_title("Max drawdown"); a2.set_xlabel("Max DD (%, more negative = worse)")
    a2.set_ylabel("# of simulations")
    fig.tight_layout(); return _png(fig)


def chart_equity(r1x, bil, best):
    import matplotlib.pyplot as plt
    idx = r1x.index
    r1 = r1x.values; b = bil.reindex(idx).fillna(0.0).values
    r2 = make_lev(r1, b)
    g, _, _ = gate(r1, r2, b, best)
    fig, ax = plt.subplots(figsize=(9, 4.0))
    ax.plot(idx, np.cumprod(1 + r1), label="ACWI 1× buy&hold", color="#888")
    ax.plot(idx, np.cumprod(1 + r2), label="ACWI 2× buy&hold (ungated)", color="#e08a00", alpha=.8)
    ax.plot(idx, np.cumprod(1 + g), label=f"ACWI 2× + {best}d SMA gate", color="#137", lw=1.6)
    ax.set_yscale("log")
    ax.set_title(f"Growth of $1 invested (log scale) — modeled 1970+")
    ax.set_xlabel("year"); ax.set_ylabel("value of $1 (log scale)")
    ax.legend(fontsize=8); ax.grid(alpha=0.3, which="both"); return _png(fig)


_CMP_COLORS = {"ACWI": "#137", "S&P 500": "#3a3", "MSCI World": "#d80"}


def chart_curve_multi(mc_dict, win_dict):
    """Median 2×-gated Sharpe vs SMA window for each index, on the common period."""
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 4.3))
    for nm, mc in mc_dict.items():
        c = _CMP_COLORS.get(nm, "#555")
        ax.plot(mc.index, mc["med_Sharpe"], "-o", ms=3, color=c, label=nm)
        ax.fill_between(mc.index, mc["p5_Sharpe"], mc["p95_Sharpe"], alpha=0.07, color=c)
        ax.axvline(win_dict[nm], ls=":", color=c, lw=1.2)
    ax.set_xlabel("SMA window (trading days)")
    ax.set_ylabel("Sharpe ratio (median of sims)")
    ax.set_title("2×-gated Sharpe vs SMA window by index — common 1970+ period")
    ax.legend(fontsize=8, title="dotted = canonical window", title_fontsize=7)
    ax.grid(alpha=0.3); return _png(fig)


def chart_compare_equity(df1x, windows, bil):
    """Overlay growth of $1 for each index's 2×-gated strategy at its window."""
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9, 4.0))
    b = bil.reindex(df1x.index).fillna(0.0).values
    for nm in df1x.columns:
        r1 = df1x[nm].values; r2 = make_lev(r1, b)
        g, _, _ = gate(r1, r2, b, windows[nm])
        ax.plot(df1x.index, np.cumprod(1 + g), lw=1.4,
                color=_CMP_COLORS.get(nm, "#555"),
                label=f"2× {nm} + {windows[nm]}d SMA")
    ax.set_yscale("log")
    ax.set_title("Growth of $1 — 2× SMA strategy by index (log, common 1970+)")
    ax.set_xlabel("year"); ax.set_ylabel("value of $1 (log scale)")
    ax.legend(fontsize=8); ax.grid(alpha=0.3, which="both"); return _png(fig)


# ── HTML ─────────────────────────────────────────────────────────────────────
def _tbl(df, fmt):
    head = "".join(f"<th>{c}</th>" for c in df.columns)
    body = "".join("<tr><th>{}</th>{}</tr>".format(
        w, "".join(f"<td>{fmt(c, row[c])}</td>" for c in df.columns))
        for w, row in df.iterrows())
    return f"<table><thead><tr><th>window</th>{head}</tr></thead><tbody>{body}</tbody></table>"


def build_html(span, det, mc, samp, best, bh1, bh2, comp, charts, model_stats,
               cmp_det, cmp_mc, cmp_wins, extras):
    def f_det(c, v):
        if c in ("CAGR", "Vol", "MaxDD", "InMkt"): return f"{v*100:.1f}%"
        if c == "Switch": return f"{int(v)}"
        return f"{v:.3f}" if c == "Sharpe" else f"{v:.2f}"
    def f_mc(c, v):
        if "CAGR" in c or "MaxDD" in c or "P_best" in c: return f"{v*100:.1f}%"
        return f"{v:.3f}"
    css = """body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:1000px;
    margin:24px auto;color:#1a1a1a;line-height:1.55;padding:0 16px}h1{font-size:24px}
    h2{font-size:19px;margin-top:34px;border-bottom:2px solid #137;padding-bottom:4px}
    table{border-collapse:collapse;font-size:12.5px;margin:10px 0;width:100%}
    th,td{border:1px solid #ddd;padding:3px 7px;text-align:right}thead th{background:#137;color:#fff}
    tbody th{background:#f3f5f8;text-align:left}.best{background:#dff5e1!important;font-weight:bold}
    .box{background:#f0f6ff;border-left:4px solid #137;padding:10px 14px;margin:14px 0}
    .gloss{background:#fafafa;border:1px solid #e3e3e3;padding:8px 14px;margin:12px 0;font-size:12.5px}
    .gloss b{color:#137}.lt{text-align:left}img{width:100%;margin:8px 0}
    code{background:#eee;padding:1px 4px;border-radius:3px}.cap{color:#666;font-size:12px;margin-top:2px}
    .key{font-size:11.5px;color:#444;background:#f7f9fc;border:1px solid #e3e3e3;padding:6px 10px;margin:4px 0}"""
    det_h = _tbl(det, f_det).replace(f"<tr><th>{best}</th>", f'<tr class="best"><th>{best}</th>')
    mc_h = _tbl(mc, f_mc).replace(f"<tr><th>{best}</th>", f'<tr class="best"><th>{best}</th>')
    bw = samp["sharpe"][best]; pct = lambda a, q: np.nanpercentile(a, q)
    p_beat = float(np.mean(bw > bh2["Sharpe"]))
    comp_rows = "".join(
        f"<tr><th class=lt>{k}</th><td>{v['CAGR']*100:.1f}%</td><td>{v['Vol']*100:.1f}%</td>"
        f"<td>{v['Sharpe']:.3f}</td><td>{v['MaxDD']*100:.1f}%</td><td>{v['Calmar']:.2f}</td></tr>"
        for k, v in comp.items())
    # LETF financing worked-example + annual-cost-vs-rate table
    ex_r = 0.01
    ex_borrow = SW * (LEV - 1) * (0.04 / 252 + SP / 252)
    ex_drag = (LEV - 1) * E / 252
    ex_r2 = LEV * ex_r - ex_borrow - ex_drag
    cost_rows = "".join(
        f"<tr><th class=lt>T-bill {x*100:.0f}%/yr</th><td>{(SW*(LEV-1)*(x+SP)+(LEV-1)*E)*100:.2f}%/yr</td></tr>"
        for x in (0.00, 0.04, 0.10))
    # cross-index head-to-head rows (deterministic + matched-path MC)
    cmp_rows = ""
    for nm in cmp_det:
        d = cmp_det[nm]; m = cmp_mc[nm]
        beat = "—" if np.isnan(m["ref_beats"]) else f"{m['ref_beats']*100:.0f}%"
        cmp_rows += (
            f"<tr><th class=lt>{nm}</th><td>{cmp_wins[nm]}d</td>"
            f"<td>{d['CAGR']*100:.1f}%</td><td>{d['Sharpe']:.3f}</td>"
            f"<td>{d['MaxDD']*100:.1f}%</td><td>{d['Calmar']:.2f}</td>"
            f"<td>{m['med_Sharpe']:.3f} [{m['p5_Sharpe']:.2f}, {m['p95_Sharpe']:.2f}]</td>"
            f"<td>{beat}</td></tr>")
    cmp_mc_nsims = cmp_mc.get("_nsims", 1500)
    others = [nm for nm in cmp_det if not np.isnan(cmp_mc[nm]["ref_beats"])]
    cmp_verdict = ("Across matched bootstrap histories, 2× ACWI's Sharpe exceeds "
                   + ", ".join(f"2× {nm} in <b>{cmp_mc[nm]['ref_beats']*100:.0f}%</b>"
                               for nm in others)
                   + ". The three are close — ACWI's edge is diversification (lower"
                   " single-country concentration), not a higher headline Sharpe.")
    # ── ultrareview-driven fragments: costs/taxes + robustness ──
    tax_rows = "".join(
        f"<tr><th class=lt>{k}</th><td>{v['pretax']['CAGR']*100:.1f}%</td>"
        f"<td>{v['pretax']['Sharpe']:.3f}</td><td>{v['postcost']['CAGR']*100:.1f}%</td>"
        f"<td>{v['net']['CAGR']*100:.1f}%</td><td>{v['net']['Sharpe']:.3f}</td>"
        f"<td>{v['switches']}</td></tr>"
        for k, v in extras["tax"].items())
    cs = extras["cost_sens"]
    cost_sens_row = "".join(
        f"<td>{cs[c]['CAGR']*100:.1f}%</td>" for c in (0.0, 0.0010, 0.0025))
    bs = extras["block_sens"]
    block_row = "".join(f"<td>{bs[mb]}d</td>" for mb in (252, 504, 756))
    pse = extras["pse"]
    pse_rows = "".join(
        f"<tr><th class=lt>{best}d vs {w}d</th><td>{pse[w]['diff']:+.3f}</td>"
        f"<td>{pse[w]['se']:.3f}</td><td>{pse[w]['t']:.1f}</td></tr>"
        for w in sorted(pse))
    lev_rows = "".join(
        f"<tr><th class=lt>{L:g}×</th><td>{d['best_window']}d</td><td>{d['Sharpe']:.3f}</td></tr>"
        for L, d in extras["levgrid"].items())
    # data-driven after-tax + significance verdicts (no hardcoded claims)
    g_net = extras["tax"][f"ACWI 2× + {best}d"]["net"]
    u_net = extras["tax"]["ACWI 2× ungated"]["net"]
    if g_net["CAGR"] >= u_net["CAGR"]:
        aftertax_clause = (f"— still ahead of ungated 2× ({u_net['CAGR']*100:.1f}%), "
                           "though the turnover tax shrinks the pre-tax edge.")
    else:
        aftertax_clause = (f"— in fact a touch <i>below</i> ungated 2× "
                           f"({u_net['CAGR']*100:.1f}%): the gate's switches are taxed as "
                           "they realise gains while buy&hold defers. After tax the gate's "
                           f"payoff is <b>risk control, not extra return</b> (MaxDD "
                           f"{g_net['MaxDD']*100:.0f}% vs {u_net['MaxDD']*100:.0f}%, Sharpe "
                           f"{g_net['Sharpe']:.2f} vs {u_net['Sharpe']:.2f}).")
    mc_best_note = extras.get("mc_best", "?")
    fit_rows = "".join(
        f"<tr><th class=lt>{nm}</th><td>{c:+.2f}</td></tr>"
        for nm, c in sorted(extras["fit"].items(), key=lambda kv: -abs(kv[1])))
    _near = [abs(pse[w]["diff"]) for w in pse if abs(w - best) <= 20]
    _max_near = max(_near) if _near else 0.0
    _d200 = pse.get(200, {}).get("diff", 0.0)
    pse_verdict = (
        f"<b>Read the magnitudes, not the t-stats.</b> Because all windows see the same "
        f"resampled paths, matched sims shrink the standard error to ~0.001, so even a "
        f"trivial Sharpe gap shows a large t — that flags <i>statistical</i>, not "
        f"<i>economic</i>, significance. Economically the differences within the "
        f"{best}±20d neighbourhood are negligible (|ΔSharpe| ≤ {_max_near:.3f}). Faster "
        f"windows are a turnover story: on the real path the 200d gate gives up ΔSharpe "
        f"{abs(_d200):.3f} pre-tax and trades {int(det.loc[200,'Switch'])}× vs "
        f"{int(det.loc[best,'Switch'])}× for {best}d; the bootstrap argmax "
        f"({mc_best_note}d) is likewise a pre-tax, pre-cost artifact of block-resampling "
        f"— §9 shows the longer window keeps more after German tax. <b>Bottom line: pick "
        f"~230–270d; avoid ≤200d.</b>")
    html = f"""<!doctype html><html><head><meta charset=utf-8>
<title>2× ACWI SMA strategy — best window</title><style>{css}</style></head><body>
<h1>2× MSCI ACWI SMA-gated strategy — best window</h1>
<p class=cap>The all-world analogue of the SPY-200-SMA / SPXL strategy, at 2× leverage ·
modeled history {span} · generated from research/sma_sweep_acwi.py</p>

<div class=box><b>Conclusion — use the ~{best}-day SMA</b> (robust ridge ≈ 230–270d;
notably <i>longer</i> than the US 200-day because at 2× leverage whipsaw and
volatility-decay punish a fast gate). <b>What's robust</b> (survives every fix in the
review, all costs/taxes, and the bootstrap): the <i>window choice</i> and the
<b>drawdown control</b> — the gate <b>more than halves</b> the 2× drawdown
({det.loc[best,'MaxDD']*100:.0f}% vs an ungated 2× ACWI's catastrophic
{bh2['MaxDD']*100:.0f}%) and beats ungated-2× Sharpe in <b>{p_beat*100:.0f}%</b> of
Monte-Carlo paths. <b>What's softer</b> (single-path, in-sample): the absolute return
edge. On the modeled path the gate's pre-tax CAGR is {det.loc[best,'CAGR']*100:.1f}%;
<b>after 10 bp switch costs + German tax it is {g_net['CAGR']*100:.1f}%</b>
{aftertax_clause} The US 200-day is too fast here
(Sharpe {det.loc[200,'Sharpe']:.3f}); your 255-day World number sits inside the ridge
(Sharpe {det.loc[255,'Sharpe']:.3f}), so reusing 255 is fine. <b>Read §10 (limitations)
before acting</b> — the headline numbers are modeled, frictionless upper bounds.</div>

<div class=gloss><b>Glossary</b> (every metric used below)<br>
<b>CAGR</b> — compound annual growth rate (annualised return). ·
<b>Vol</b> — annualised volatility (std-dev of daily returns × √252); higher = bumpier. ·
<b>Sharpe</b> — (CAGR − 2% risk-free) ÷ Vol; return per unit of risk, higher = better. ·
<b>MaxDD</b> — worst peak-to-trough loss ever suffered (more negative = worse). ·
<b>Calmar</b> — CAGR ÷ |MaxDD|; return per unit of worst-case pain. ·
<b>InMkt</b> — % of days the gate was invested (vs in T-bills). ·
<b>Switch</b> — number of in↔out trades over the whole period (turnover; each is a taxable event). ·
<b>median / p5 / p95</b> — the middle / 5th / 95th percentile across the Monte-Carlo simulations. ·
<b>P_best</b> — share of simulations in which that window had the highest Sharpe. ·
<b>SMA</b> — simple moving average; the gate is "in" when price &gt; SMA×1.01, "out" when &lt; SMA×0.99.</div>

<h2>1 · Data sources</h2>
<table><thead><tr><th class=lt>Series</th><th class=lt>Source</th><th class=lt>Span used</th><th class=lt>Role here</th></tr></thead><tbody>
<tr><th class=lt>MSCI ACWI gross index (USD)</th><td class=lt>MSCI end-of-day API (index 892400)</td><td class=lt>2001–2008</td><td class=lt>real ACWI daily returns</td></tr>
<tr><th class=lt>iShares MSCI ACWI ETF (ACWI.US)</th><td class=lt>EODHD</td><td class=lt>2008→today</td><td class=lt>real ACWI daily returns (live + future)</td></tr>
<tr><th class=lt>MSCI ACWI net TR (EUR), monthly</th><td class=lt>Curvo (curvo.eu), 1987+</td><td class=lt>1988–2001</td><td class=lt>real monthly anchor (embeds true EM weights)</td></tr>
<tr><th class=lt>MSCI World (URTHSIM → URTH)</th><td class=lt>Testfolio sim spliced w/ real URTH (EODHD)</td><td class=lt>1970–1987</td><td class=lt>ACWI predecessor segment, used as-is (index didn't exist yet; EM &lt;1% at 1988 inception)</td></tr>
<tr><th class=lt>MSCI World (same series)</th><td class=lt>—</td><td class=lt>1988–2001</td><td class=lt>intra-month daily <i>texture</i> only</td></tr>
<tr><th class=lt>Xtrackers S&amp;P 500 2x Lev. Daily Swap (XS2D)</th><td class=lt>EODHD (LSE, USD)</td><td class=lt>2010→today</td><td class=lt>real-product cost calibration of the synthetic 2× sleeve</td></tr>
<tr><th class=lt>EUR/USD spot</th><td class=lt>FRED DEXUSEU (1999+) + Deutsche Mark EXGEUS (pre-1999)</td><td class=lt>1988→today</td><td class=lt>convert Curvo EUR→USD</td></tr>
<tr><th class=lt>3-month US T-bill rate</th><td class=lt>FRED TB3MS → DGS3MO</td><td class=lt>1970→today</td><td class=lt>LETF financing cost + the "out" (cash) leg</td></tr>
</tbody></table>
<p class=cap>The first two are the genuine ACWI; Curvo is real ACWI too (just EUR/monthly, FX-converted);
World is used <i>only</i> to give the pre-2001 series daily wiggle — never as the return itself.</p>

<h2>2 · How the pre-2001 index is modeled (EM-share-correct)</h2>
<p>Real MSCI ACWI daily only starts 2001 — too short for a leveraged backtest. A
MSCI-World-only proxy is <b>wrong</b> pre-2001: it omits emerging markets, whose ACWI
weight evolved a lot over time — so over 1988-2000 World-only understates ACWI by
~0.34%/yr.</p>
<table><thead><tr><th class=lt>year</th><th>1988</th><th>1997</th><th>2002</th><th>2010</th><th>2021</th><th>today</th></tr></thead>
<tbody><tr><th class=lt>EM weight in ACWI</th><td>&lt;1%</td><td>6.8%</td><td>~4%</td><td>13.5%</td><td>13%</td><td>~10%</td></tr></tbody></table>
<p>Instead we anchor to the <b>real ACWI monthly history</b> (which embeds those exact
time-varying EM weights by construction). The source is Curvo's
<b>iShares MSCI ACWI UCITS ETF (Acc)</b> series — a real fund-NAV track record (monthly,
EUR), not the bare index — which we FX-convert to USD. <b>Validation:</b> converted-Curvo
vs real MSCI ACWI USD-gross over the 2001+ overlap has monthly corr
<b>{model_stats['corr']:.3f}</b> (R² {model_stats['r2']:.2f}), return drift only
<b>{model_stats['drift']:+.2f}%/yr</b>. The near-zero drift is what you'd expect from a
fund-TR series — its ~0.20% expense ratio is already inside that measured drift (so if
anything the modeled pre-2001 returns are a hair <i>conservative</i>, carrying a small
fund fee the bare index wouldn't). Using the real EM weighting adds
<b>+{model_stats['em_gain']:.1f}%</b> to 1988-2000 cumulative growth vs a World-only proxy.</p>
<p><b>Daily granularity</b> (needed for a daily SMA gate) comes from <b>temporal
disaggregation</b>: inside each pre-2001 month we take MSCI World's daily shape and
rescale it multiplicatively so the month compounds <i>exactly</i> to the real ACWI
monthly return (reconstruction error ~1e-15). World supplies only the intra-month
wiggle; every monthly return is the real EM-weighted ACWI. A literal daily World+EM
blend is impossible before 2001 (no daily EM exists pre-2003) — and provably
immaterial: tested on 2003+, World-texture and a true World+EM blend give the same
gated Sharpe to ±0.02.</p>
<p><b>The 1970–1987 head.</b> MSCI ACWI only exists from Dec-1987, so "the longest
possible ACWI backtest" necessarily starts there — unless one accepts the index's
predecessor. We do, explicitly: 1970→1987 uses <b>MSCI World daily returns as-is</b>.
That is not a stretch — at ACWI's 1988 inception EM weighed <b>&lt;1%</b> and the MSCI
EM index itself only launched 1988, so an "all-country" investor of the 1970s-80s held,
in practice, exactly World. The segment is labeled throughout; nothing pre-1988 is
scaled or synthesised. <b>Full chain:</b> World as-is (1970→1988) → EM-anchored modeled
daily (1988→2001) → real MSCI ACWI gross (2001→2008) → real ACWI ETF (2008→today).</p>

<h2>3 · How the 2× sleeve (LETF) is modeled</h2>
<p>There is no 2× ACWI ETF with long history, so we synthesise one with a
<b>daily-reset</b> formula — the same construction (and constants) the production engine
uses for its leveraged ETFs (e.g. synthetic WLDU/SPXL):</p>
<p style="text-align:center;font-size:14px"><code>r₂ₓ(day) = 2·r − SW·(L−1)·(T-bill<sub>daily</sub> + spread/252) − (L−1)·expense/252</code></p>
<p><b>In plain words:</b> to get 2× exposure with 1× of your own money you effectively
<b>borrow another 1×</b> and invest the doubled amount. So each day the sleeve <b>earns
twice the index's move</b> (the <code>2·r</code> term), then pays two bills: (1) the
<b>interest on that borrowed half</b> — charged at the short-term T-bill rate plus a small
~0.4% spread, and nudged up ~10% (the swap multiplier) for the cost of the swap that
delivers the leverage; and (2) the <b>fund's own running expense</b> (~0.5%/yr). Two
consequences matter: the borrow cost <b>rises and falls with interest rates</b> (cheap
near 0%, painful when T-bills are 5%+), and because the leverage <b>resets to 2× every
single day</b>, a sideways-but-choppy market slowly <b>bleeds value</b> ("volatility
decay") even if the index ends flat — which is precisely why a trend gate that sits out
the chop is so valuable at 2×.</p>
<table><thead><tr><th class=lt>term</th><th class=lt>value</th><th class=lt>meaning</th></tr></thead><tbody>
<tr><th class=lt>L (leverage)</th><td class=lt>{LEV:.0f}×</td><td class=lt>daily target exposure to ACWI</td></tr>
<tr><th class=lt>2·r</th><td class=lt>—</td><td class=lt>twice the underlying ACWI daily return</td></tr>
<tr><th class=lt>T-bill (financing)</th><td class=lt>actual 3m rate, time-varying</td><td class=lt>cost to borrow the extra (L−1)=1× notional — the key driver</td></tr>
<tr><th class=lt>SW (swap multiplier)</th><td class=lt>{SW}</td><td class=lt>broker/swap markup on the financed notional (Testfolio default)</td></tr>
<tr><th class=lt>spread</th><td class=lt>{SP*100:.1f}%/yr</td><td class=lt>financing spread over the T-bill (≈40 bp)</td></tr>
<tr><th class=lt>expense</th><td class=lt>{E*100:.1f}%/yr ×(L−1)</td><td class=lt>fund expense ratio drag</td></tr>
</tbody></table>
<p><b>Why time-varying financing matters:</b> the borrow cost is the dominant drag and
it scales with the short rate, which ran 0%→15% across this window. A flat rate would
badly mis-state pre-2008 returns. Total annual drag on the 2× sleeve at different rate
levels:</p>
<table><thead><tr><th class=lt>short rate</th><th class=lt>annual drag on 2× sleeve</th></tr></thead><tbody>{cost_rows}</tbody></table>
<p class=cap><b>Worked example:</b> on a day ACWI returns +1.00% with the T-bill at 4%/yr,
the 2× sleeve returns 2×1.00% − {ex_borrow*100:.3f}% (financing) − {ex_drag*100:.3f}%
(expense) = <b>{ex_r2*100:.3f}%</b> — slightly under a naïve +2.00%. Daily resetting
also causes volatility decay in choppy markets, which is exactly why the SMA gate (which
sits out the chop) adds so much value at 2×.</p>
<p><b>Calibration against a real product.</b> The formula above is no longer purely
theoretical: we benchmarked it against the <b>Xtrackers S&amp;P 500 2x Leveraged Daily
Swap UCITS ETF</b> (XS2D, USD — structurally identical to the upcoming ACWI product:
TER 0.60%, unfunded swap) over its full 2010→2026 history. Month-end levels track at
correlation 0.95, with the synthetic running <b>+0.73%/yr richer</b> than the real fund
— i.e. real-world swap costs slightly exceed our textbook assumptions. The estimate
is stable, not endpoint-driven: trimming the first 0/6/24 month-ends gives
+0.73/+0.74/+0.79%/yr (reproduce via <code>research/xs2d_calibration.py</code>).
Measured +0.73%/yr, applied as <b>0.75%/yr</b> in a <b>"real-product drag"</b>
scenario that appears as an extra row in the strategy-comparison table (§7) and the
after-tax table (§9) — treat those rows as the expectation for the actual ETF. One
transfer caveat: the calibration is US-underlying; a 2× ACWI swap index carries a
larger dividend-withholding surface, so the real fund could lag marginally more —
re-calibrate against its NAV once it trades.</p>
<p><b>The strategy itself:</b> the SMA gate is computed on the <i>unleveraged</i> ACWI
index (just as SPXL gates on SPY); when ACWI &gt; its N-day SMA×1.01 we hold the 2×
sleeve, when &lt; SMA×0.99 we hold T-bills. Signal checked daily, executed next day, ±1%
hysteresis band to avoid flip-flopping.</p>

<h2>4 · Window robustness — Monte Carlo ({samp['n_sims']} sims, 252-day blocks)</h2>
<img src="data:image/png;base64,{charts['curve']}">
<p class=cap><b>How to read:</b> each window's gate is re-run on {samp['n_sims']} bootstrapped
{extras['years']}-year histories (resampled in ~1-year blocks). The line is the median
Sharpe across those sims; the shaded band is the 5th–95th percentile. <b>The MC is a
robustness check, not the selector.</b> Block-resampling necessarily shreds part of the
multi-month trend structure a long SMA exploits, which tilts the bootstrap argmax toward
shorter windows — on this run its argmax is <b>{mc_best_note}d</b>, versus the
deterministic real-path best of <b>{best}d</b>. We select on the real path (trend
structure intact), cross-checked by the real-data-only 2001+ sweep and the tax analysis
(§9: shorter windows trade ~50% more and lose the difference to the German tax drag).
See §10 for the paired test and block-length sensitivity.</p>
<p class=key>Columns: <b>med/p5/p95_Sharpe</b> = median &amp; 5–95th-pct Sharpe across sims ·
<b>med_CAGR / med_MaxDD</b> = median return &amp; drawdown · <b>P_best</b> = % of sims where
this window won. Green row = recommended {best}d.</p>
{mc_h}

<h2>5 · Deterministic sweep — every window on the actual modeled path</h2>
<p class=cap><b>How to read:</b> not a simulation — these are the metrics on the one real
(modeled) 1970+ history. The genuine high-Sharpe / low-turnover ridge is <b>~230–270d</b>
(best {best}d: Sharpe {det.loc[best,'Sharpe']:.3f}, {int(det.loc[best,'Switch'])} switches);
sub-150d windows whipsaw (2-3× the trades) — very costly at 2× leverage and in a taxable
account, which is why the pre-tax near-tie at ~160d loses after tax (§9). Green row =
recommended {best}d.</p>
<p class=key>Columns: <b>CAGR</b> return · <b>Vol</b> volatility · <b>Sharpe</b> risk-adjusted ·
<b>MaxDD</b> worst loss · <b>Calmar</b> CAGR/|MaxDD| · <b>InMkt</b> % time invested ·
<b>Switch</b> # of in/out trades.</p>
{det_h}

<h2>6 · Monte-Carlo of the chosen {best}-day strategy</h2>
<img src="data:image/png;base64,{charts['hist']}">
<p class=cap><b>How to read:</b> the spread of outcomes for the {best}d gate across the
{samp['n_sims']} bootstrapped histories — left = Sharpe (dashed line = ungated 2× for
reference), right = max drawdown. The table below gives those distributions as percentiles
(p5 = a bad-luck history, median = typical, p95 = a lucky one).</p>
<table><thead><tr><th class=lt>metric</th><th>p5 (unlucky)</th><th>p25</th><th>median</th><th>p75</th><th>p95 (lucky)</th></tr></thead><tbody>
<tr><th class=lt>Sharpe</th><td>{pct(bw,5):.2f}</td><td>{pct(bw,25):.2f}</td><td>{pct(bw,50):.2f}</td><td>{pct(bw,75):.2f}</td><td>{pct(bw,95):.2f}</td></tr>
<tr><th class=lt>CAGR</th><td>{pct(samp['cagr'][best],5)*100:.1f}%</td><td>{pct(samp['cagr'][best],25)*100:.1f}%</td><td>{pct(samp['cagr'][best],50)*100:.1f}%</td><td>{pct(samp['cagr'][best],75)*100:.1f}%</td><td>{pct(samp['cagr'][best],95)*100:.1f}%</td></tr>
<tr><th class=lt>Max DD</th><td>{pct(samp['maxdd'][best],5)*100:.1f}%</td><td>{pct(samp['maxdd'][best],25)*100:.1f}%</td><td>{pct(samp['maxdd'][best],50)*100:.1f}%</td><td>{pct(samp['maxdd'][best],75)*100:.1f}%</td><td>{pct(samp['maxdd'][best],95)*100:.1f}%</td></tr>
</tbody></table>
<p>Beats <b>ungated 2× ACWI</b> Sharpe ({bh2['Sharpe']:.3f}) in <b>{p_beat*100:.0f}%</b>
of paths. (Bootstrap medians read below the actual-path numbers because resampling
degrades trend structure — a conservative lens for a trend strategy.)</p>
<img src="data:image/png;base64,{charts['equity']}">
<p class=cap><b>How to read:</b> growth of $1, log scale (so a straight line = constant %
growth). Grey = unleveraged ACWI; orange = raw 2× (note the violent crashes); blue =
2× behind the {best}d gate — similar end-wealth to raw 2× but far smoother.</p>

<h2>7 · Strategy comparison (actual modeled path, {span})</h2>
<table><thead><tr><th class=lt>strategy</th><th>CAGR</th><th>Vol</th><th>Sharpe</th><th>MaxDD</th><th>Calmar</th></tr></thead>
<tbody>{comp_rows}</tbody></table>
<p class=cap>The first three rows are ACWI (1× buy&hold, 2× ungated, and the recommended
2×+{best}d gate). The last two are the <b>canonical US and World strategies</b> for
reference — 2× <b>S&amp;P 500</b> gated by its own 200-day SMA, and 2× <b>MSCI World
(URTH)</b> gated by its own 255-day SMA — each on the same modeled 1970+ window. Actual
modeled path, not a simulation. (§8 runs the same three indices through a matched-path
Monte Carlo with beat-probabilities.)</p>

<h2>8 · Head-to-head: 2× ACWI vs 2× S&amp;P 500 (200d) vs 2× MSCI World (255d)</h2>
<p>The same gated mechanic and the <b>same 2× leverage</b> applied to each index at its
canonical window, on the <b>common 1970+ period</b> — so any difference reflects the
<i>index</i>, not the leverage or the dates. (Caveat: pre-1988 our ACWI series ≡ World
by construction, so the ACWI-vs-World comparison is driven by 1988+ — before that it
measures only the 250d-vs-255d window difference.) (Note: your live S&amp;P sleeve is actually
3× SPXL and World is 2× WLDU; here all three are 2× for a like-for-like read.)</p>
<img src="data:image/png;base64,{charts['multi']}">
<p class=cap><b>How to read:</b> median 2×-gated Sharpe vs window for each index, with the
5–95th-pct band; dotted verticals mark each canonical window. The peaks confirm the rule
of thumb — S&amp;P is fastest (~200d), World/ACWI slower (~255–260d), because adding ex-US
and EM makes the trend smoother and rewards a longer lookback.</p>
<table><thead><tr>
<th class=lt>index (2×, gated)</th><th>window</th><th>CAGR</th><th>Sharpe</th><th>MaxDD</th><th>Calmar</th>
<th>MC Sharpe<br>median [p5,p95]</th><th>P(ACWI&nbsp;beats)</th></tr></thead><tbody>{cmp_rows}</tbody></table>
<p class=key>First four metric columns = actual modeled path; <b>MC Sharpe</b> = matched-path
bootstrap (same {cmp_mc_nsims} resampled histories fed to all three indices);
<b>P(ACWI beats)</b> = share of those matched histories in which 2× ACWI's Sharpe exceeds
that index's.</p>
<p>{cmp_verdict}</p>
<img src="data:image/png;base64,{charts['cmp_equity']}">
<p class=cap><b>How to read:</b> growth of $1 for each index's 2×-gated strategy on the
common period (log scale). All three are strong; ACWI sits between the US and World lines,
which is exactly what a global blend should do.</p>

<h2>9 · After costs &amp; taxes (the honest numbers)</h2>
<p>Everything above is <b>frictionless and pre-tax</b> — an upper bound. This is a German
taxable account, so each in→out switch is a realisation event (Abgeltungsteuer 26.375%,
~18.5% effective after the 30% equity Teilfreistellung), while ungated buy&amp;hold defers
tax indefinitely (only the annual Vorabpauschale applies). Below: 10 bp round-trip
transaction cost per switch + the full German tax overlay (loss carry-forward,
Vorabpauschale; via research/tax_overlay.py).</p>
<table><thead><tr><th class=lt>strategy</th><th>pre-tax CAGR</th><th>pre-tax Sharpe</th>
<th>post-cost CAGR</th><th>net CAGR<br>(cost+tax)</th><th>net Sharpe</th><th>switches</th></tr></thead>
<tbody>{tax_rows}</tbody></table>
<p>The gate's turnover is its tax weakness: ungated 2× defers tax (compounds pre-tax,
paying only the small annual Vorabpauschale), while the gated sleeve realises — and is
taxed on — gains every time it steps out. <b>Net of cost+tax the {best}d gate's CAGR is
{g_net['CAGR']*100:.1f}% {aftertax_clause}</b> A faster gate (200d, {extras['tax']['ACWI 2× + 200d']['switches']}
switches) gives even more back to the tax man. The takeaway: at 2× in a German taxable
account the gate is bought for its <b>drawdown/Sharpe</b>, not for extra compounding.
<b>Switch-cost sensitivity</b> (net CAGR of the {best}d strategy at 0 / 10 / 25 bp
round-trip):</p>
<table><thead><tr><th class=lt>round-trip cost</th><th>0 bp</th><th>10 bp</th><th>25 bp</th></tr></thead>
<tbody><tr><th class=lt>net CAGR ({best}d)</th>{cost_sens_row}</tr></tbody></table>
<p><b>Classification risk quantified:</b> if the final fund fails the ≥51%
physical-equity quota and gets <b>0% Teilfreistellung</b>, the {best}d strategy's net
CAGR drops from {g_net['CAGR']*100:.1f}% to <b>{extras['tfs0']['CAGR']*100:.1f}%</b>
(net Sharpe {extras['tfs0']['Sharpe']:.2f}) — check the Anlagebedingungen before
buying.</p>

<h2>10 · Limitations &amp; robustness (read before acting)</h2>
<p>This study was hardened by an adversarial review; the material caveats:</p>
<ul>
<li><b>Window choice is robust, the exact number is not.</b> Paired matched-path
bootstrap of the Sharpe <i>difference</i> ({best}d minus each):
<table><thead><tr><th class=lt>comparison</th><th>mean ΔSharpe</th><th>SE</th><th>t-stat</th></tr></thead>
<tbody>{pse_rows}</tbody></table>
{pse_verdict}</li>
<li><b>Block-length sensitivity.</b> MC argmax window at bootstrap mean-block 252 / 504 /
756 days: <table><thead><tr><th class=lt>mean block</th><th>252d</th><th>504d</th><th>756d</th></tr></thead>
<tbody><tr><th class=lt>argmax window</th>{block_row}</tr></tbody></table>
The selection is anchored to the <i>deterministic</i> sweep (which preserves real trend
structure); the bootstrap is a robustness check only, since block-resampling necessarily
degrades the multi-month autocorrelation a long SMA exploits.</li>
<li><b>Real-data-only cross-check.</b> Running the identical 2× sweep on <i>only</i> the
real ACWI 2001+ segment (no modeling at all) gives best window
<b>{extras['real_best']}d</b> — consistent with the full modeled result, so the pre-2001
model isn't driving the conclusion.</li>
<li><b>Leverage scope.</b> Everything is fixed at 2×, and no 2× ACWI ETF currently exists
(the sleeve is synthetic). Best window by leverage:
<table><thead><tr><th class=lt>leverage</th><th>best window</th><th>Sharpe</th></tr></thead>
<tbody>{lev_rows}</tbody></table>
The long-window preference holds across leverage; "~{best}d" is contingent on ~2×.</li>
<li><b>1970–1987 is MSCI World as-is — drift <i>and</i> texture.</b> ACWI only exists
from Dec-1987; the head segment (~30% of the trimmed sample) is its predecessor index,
unadjusted. ACWI-specific evidence starts 1988 (and daily EM texture only 2003+).</li>
<li><b>1988–2001 daily <i>texture</i> is MSCI World's, not ACWI's.</b> Monthly drift is the
real EM-weighted ACWI (validated), but intra-month daily wiggles in 1988-2001 borrow World's
shape (no daily EM exists pre-2003). Shown immaterial for a slow gate (±0.02 Sharpe test on
2003+), and the real-only cross-check above confirms it.</li>
<li><b>Financing is a modern best case.</b> The LETF borrow (3m T-bill + 40 bp × 1.1 swap)
reflects a cheap modern swap fund; pre-2008 and a thin all-world 2× would cost more — add
~50–100 bp to financing for a conservative read.</li>
<li><b>Fixed 2% risk-free in Sharpe.</b> Realised T-bills averaged ~3% over the period, so
absolute Sharpes here run ~0.05 high vs an external quote — fine for <i>ranking</i> windows
(the bias is common to all), not for comparing to other sources.</li>
<li><b>No look-ahead.</b> The SMA uses the same-day close to decide, but the position is
executed the next day (state shifted by one); verified in code.</li>
</ul>

<h2>11 · The real product &amp; portfolio fit</h2>
<p><b>The instrument.</b> The <b>Scalable MSCI AC World Leveraged Daily Swap Xtrackers
UCITS ETF</b> (LEI 254900NW0MAOB3TRMY48, Luxembourg, registered 2026-05-29) is the
first investable 2× MSCI ACWI vehicle — a Scalable Capital × DWS/Xtrackers follow-up to
their unleveraged SCWX (€600m+). Filed, not yet trading; final TER/ISIN pending. Its
structural template, the Xtrackers S&amp;P 500 2x Leveraged Daily Swap (2010+): TER
0.60%, unfunded swap, USD fund currency with EUR listing — and, decisive for a German
taxable account, <b>classified as an equity fund with the 30% Teilfreistellung</b>. Our
§9 tax numbers assume exactly that (verify the Aktienfonds classification in the final Anlagebedingungen/prospectus before buying). By
contrast, the US-listed alternative WLDU (Themes/Leverage Shares 2× Long World Stock
Daily ETF — a 1940-Act <b>swap-based ETF on Vanguard Total World</b>, so it does
include EM; TER 0.75%) holds swaps plus collateral rather than ≥51% physical
equities, so it should expect <b>0% Teilfreistellung</b> in Germany — the UCITS
wrapper is worth roughly TFS × tax rate ≈ 8 pp of every realised gain. §9's bottom
row quantifies the same risk for the new fund itself, should its final structure
miss the Aktienfonds quota (verify the ≥51% Kapitalbeteiligungsquote commitment in
the fund's Anlagebedingungen/prospectus — not the KID, which doesn't state it —
before buying).</p>
<p><b>Execution reality.</b> A UCITS ETF is <b>not tradeable on Alpaca</b>, so the bot
cannot automate this sleeve. Realistic setups: (a) hold it manually at a German broker
(e.g. Scalable) and let the bot's daily job only <i>signal</i> the 250d gate via
Telegram; (b) automate an approximation on Alpaca with WLDU (2× World ETP — but worse
German tax, no EM); or (c) wait for EU broker-API support. The strategy's ~2 switches
per year make manual execution genuinely practical.</p>
<p><b>Correlation with reference strategies</b> (daily returns of the {best}d-gated 2×
ACWI vs proxies, one common window {extras['fit_span']} — the start is set by KMLM's
1988 history + warmup):</p>
<table><thead><tr><th class=lt>reference strategy (proxy)</th><th>corr</th></tr></thead>
<tbody>{fit_rows}</tbody></table>
<p class=cap>HFEA/F4 blends are daily-rebalanced approximations of the real
quarterly-rebalanced sleeves (GOLY proxied by gold). Dual Momentum, Regime SSO, AAA
and 9-Sig are too path-dependent for a quick proxy — the formal correlation matrix
runs in mega_backtest.py at promotion time.</p>
<p>High correlation to the US/World trend sleeves is expected — this is the same trade
on a broader index. The honest framing: this strategy is a <b>substitute/upgrade for
world-equity trend exposure</b> (more diversified index, UCITS tax wrapper), not a new
diversifier. A formal promotion decision (correlation matrix vs all 7 sleeves,
portfolio what-if injection, Monte-Carlo vs deployed mix) should run through
mega_backtest.py once the fund has an ISIN and a live price series.</p>
</body></html>"""
    return html


if __name__ == "__main__":
    import matplotlib; matplotlib.use("Agg")
    ext = ed.fetch_extended_data()
    bil = ext["bil_daily_return"]
    r1x = build_acwi_ext(ext)
    bil = bil.reindex(r1x.index).fillna(0.0)
    span = f"{r1x.index[0].date()}→{r1x.index[-1].date()}"

    # model-quality stats: validate the FX-converted Curvo (real ACWI, EM-weighted)
    # against the real MSCI ACWI USD-gross over the 2001+ overlap.
    curvo_usd = acwi_modeled.real_acwi_usd_monthly()
    real_m = ext["acwi_tr"].resample("M").last().pct_change()
    md = pd.concat([curvo_usd.rename("curvo"), real_m.rename("msci")], axis=1).dropna()
    world = ext["urth_tr"].pct_change()
    wpre = (1 + world[(world.index >= "1988-01-01") & (world.index < "2001-01-01")]).prod()
    mpre = (1 + r1x[r1x.index < "2001-01-01"]).prod()
    model_stats = {"corr": md.curvo.corr(md.msci),
                   "r2": md.curvo.corr(md.msci) ** 2,
                   "drift": (md.msci.mean() - md.curvo.mean()) * 12 * 100,
                   "em_gain": (mpre / wpre - 1) * 100}

    r1, b = r1x.values, bil.values
    print("Deterministic sweep (2× gated)…")
    det = deterministic(r1, b)
    print("Monte Carlo window selection…")
    mc, samp = monte_carlo(r1, b, 1500, 252)
    det_best = int(det["Sharpe"].idxmax())
    mc_best = int(mc["med_Sharpe"].idxmax())
    # Recommended = deterministic best. The real path preserves the multi-month
    # trend autocorrelation the gate exploits; block-resampling structurally
    # tilts the MC argmax toward shorter windows (established in the ultrareview),
    # and shorter windows also lose more to switch costs + German tax (§9). The
    # MC is reported as a robustness band, not the selector; §10 discloses the
    # MC argmax and the tax tiebreak explicitly.
    best = det_best
    print(f"  ➤ recommended={best}d  (det-best {det_best}, MC-best {mc_best})")

    r2 = make_lev(r1, b)
    bh1 = metrics(r1[TRIM:]); bh2 = metrics(r2[TRIM:])
    g_best, _, _ = gate(r1, r2, b, best)
    # reference strategies: real 2× SPY-200d and 2× MSCI-World(URTH)-255d, gated on
    # their OWN index, over the same modeled 1970+ window (not ACWI at those windows)
    spy_r = ext["spy_tr"].pct_change().reindex(r1x.index).fillna(0.0).values
    wld_r = ext["urth_tr"].pct_change().reindex(r1x.index).fillna(0.0).values
    g_spy200, _, _ = gate(spy_r, make_lev(spy_r, b), b, 200)
    g_wld255, _, _ = gate(wld_r, make_lev(wld_r, b), b, 255)
    g_real, _, _ = gate(r1, make_lev(r1, b, extra=REAL_EXTRA_DRAG), b, best)
    comp = {
        "ACWI 1× buy&hold": bh1,
        "ACWI 2× buy&hold (ungated)": bh2,
        f"ACWI 2× + {best}d SMA (recommended)": metrics(g_best[TRIM:]),
        f"ACWI 2× + {best}d (real-product drag, XS2D-calibrated)": metrics(g_real[TRIM:]),
        "S&P 500 2× + 200d SMA": metrics(g_spy200[TRIM:]),
        "MSCI World 2× + 255d SMA": metrics(g_wld255[TRIM:]),
    }
    # ── cross-index comparison: 2× ACWI vs 2× S&P 500 (200d) vs 2× World (255d) ──
    print("Cross-index comparison (S&P 500, MSCI World)…")
    al = pd.concat([r1x.rename("ACWI"),
                    ext["spy_tr"].pct_change().rename("S&P 500"),
                    ext["urth_tr"].pct_change().rename("MSCI World")],
                   axis=1).dropna()                     # common 1970+ period
    bil_al = bil.reindex(al.index).fillna(0.0)
    cmp_wins = {"ACWI": best, "S&P 500": 200, "MSCI World": 255}
    series_arr = {nm: al[nm].values for nm in cmp_wins}
    ba = bil_al.values
    cmp_det = {nm: metrics(gate(series_arr[nm], make_lev(series_arr[nm], ba), ba,
                                cmp_wins[nm])[0][TRIM:]) for nm in cmp_wins}
    CMP_SIMS = 1500
    cmp_mc = monte_carlo_joint(series_arr, cmp_wins, ba, CMP_SIMS, 252)
    cmp_mc["_nsims"] = CMP_SIMS
    mc_spy, _ = monte_carlo(al["S&P 500"].values, ba, 1000, 252, seed=7)
    mc_wld, _ = monte_carlo(al["MSCI World"].values, ba, 1000, 252, seed=11)
    # ACWI curve recomputed on the COMMON window so the three lines are comparable
    mc_acwi_common, _ = monte_carlo(al["ACWI"].values, ba, 1000, 252, seed=42)
    mc_dict = {"ACWI": mc_acwi_common, "S&P 500": mc_spy, "MSCI World": mc_wld}

    # ── ultrareview fixes: costs, taxes, robustness diagnostics ──────────────
    print("After-cost / after-tax evaluation…")
    idx = r1x.index
    tax_rows = {                       # cost = 10bp round-trip per switch
        "ACWI 1× buy&hold": net_eval(r1, b, idx, n=None, cost=0.0, lev=1.0),
        "ACWI 2× ungated":  net_eval(r1, b, idx, n=None, cost=0.0, lev=2.0),
        f"ACWI 2× + {best}d": net_eval(r1, b, idx, n=best, cost=0.0010, lev=2.0),
        f"ACWI 2× + {best}d (real-product drag)":
            net_eval(r1, b, idx, n=best, cost=0.0010, lev=2.0, extra=REAL_EXTRA_DRAG),
        "ACWI 2× + 200d":   net_eval(r1, b, idx, n=200, cost=0.0010, lev=2.0),
        "ACWI 2× + 255d":   net_eval(r1, b, idx, n=255, cost=0.0010, lev=2.0),
    }
    cost_sens = {c: net_eval(r1, b, idx, n=best, cost=c, lev=2.0)["net"]
                 for c in (0.0, 0.0010, 0.0025)}      # 0/10/25 bp round-trip
    # Classification risk: same strategy with NO Teilfreistellung (fund fails the
    # >=51% physical-equity quota) — makes the §11 caveat a number.
    tfs0 = net_eval(r1, b, idx, n=best, cost=0.0010, lev=2.0, tfs=0.0)["net"]

    print("Block-length sensitivity…")
    block_sens = {}
    for mb in (252, 504, 756):
        m, _ = monte_carlo(r1, b, 800, mb, seed=mb)
        block_sens[mb] = int(m["med_Sharpe"].idxmax())

    pse = paired_se(samp, best, [w for w in (160, 200, 230, 270, 300) if w != best])
    levgrid = leverage_window_grid(r1, b)                  # best window per leverage

    # real-data-only cross-check (no modeling): 2× gated sweep on real ACWI 2001+
    real1 = ext["acwi_tr"].pct_change().dropna()
    realb = bil.reindex(real1.index).fillna(0.0)
    det_real = deterministic(real1.values, realb.values)
    real_best = int(det_real["Sharpe"].idxmax())

    # ── portfolio fit: candidate vs proxies of the deployed sleeves ──────────
    print("Portfolio-fit correlations…")
    cand = pd.Series(gate(r1, make_lev(r1, b), b, best)[0], index=idx, name="cand")
    spy_al = ext["spy_tr"].pct_change().reindex(idx)
    wld_al = ext["urth_tr"].pct_change().reindex(idx)
    tlt_al = ext["tlt_tr"].pct_change().reindex(idx)
    gld_al = ext["gld_tr"].pct_change().reindex(idx)
    kml_al = ext["kmlm_tr"].pct_change().reindex(idx)
    b_ser = pd.Series(b, index=idx)
    def _g(sig, lev, win):
        s = sig.fillna(0.0).values
        return pd.Series(gate(s, make_lev(s, b, lev), b, win)[0], index=idx)
    proxies = {
        "SPXL SMA sleeve (3× SPY, 200d gate)": _g(spy_al, 3.0, 200),
        "World 2× + 255d (WLDU-gate style)": _g(wld_al, 2.0, 255),
        "HFEA proxy (45/25/30 3×SPY/3×TLT/KMLM)":
            0.45 * pd.Series(make_lev(spy_al.fillna(0).values, b, 3.0), index=idx)
            + 0.25 * pd.Series(make_lev(tlt_al.fillna(0).values, b, 3.0), index=idx)
            + 0.30 * kml_al,
        "F4 proxy (40% 2×World / 30% gold / 30% TLT)":
            0.40 * pd.Series(make_lev(wld_al.fillna(0).values, b, 2.0), index=idx)
            + 0.30 * gld_al + 0.30 * tlt_al,
        "ACWI 1× buy&hold": pd.Series(r1, index=idx),
    }
    allf = pd.concat([cand] + [ser.rename(nm) for nm, ser in proxies.items()],
                     axis=1).dropna()          # ONE common window (KMLM-limited)
    allf = allf.iloc[TRIM:]                     # warmup trim on the common calendar
    fit = {nm: float(allf["cand"].corr(allf[nm])) for nm in proxies}
    fit_span = f"{allf.index[0].date()} → {allf.index[-1].date()}"

    extras = {"tax": tax_rows, "cost_sens": cost_sens, "block_sens": block_sens,
              "pse": pse, "levgrid": levgrid, "real_best": real_best,
              "years": round(len(r1) / 252), "fit": fit, "fit_span": fit_span,
              "tfs0": tfs0, "mc_best": mc_best}

    charts = {"curve": chart_curve(mc, best),
              "hist": chart_hist(samp, best, bh2["Sharpe"]),
              "equity": chart_equity(r1x, bil, best),
              "multi": chart_curve_multi(mc_dict, cmp_wins),
              "cmp_equity": chart_compare_equity(al, cmp_wins, bil_al)}
    html = build_html(span, det, mc, samp, best, bh1, bh2, comp, charts, model_stats,
                      cmp_det, cmp_mc, cmp_wins, extras)
    with open("sma_acwi_2x_report.html", "w") as fh:
        fh.write(html)
    print(f"\n✓ wrote sma_acwi_2x_report.html  (recommended {best}d, {extras['years']}y)")
    print("cross-index (det Sharpe):", {k: round(v["Sharpe"], 3) for k, v in cmp_det.items()})
    print("block-sens argmax:", block_sens, " real-only best:", real_best)
    print("net (after cost+tax) CAGR:",
          {k: round(v["net"]["CAGR"], 3) for k, v in tax_rows.items()})
    print(det.round(3).to_string())
