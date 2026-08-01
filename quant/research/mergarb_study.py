"""MERGARB-Backtest — Merger-Arbitrage auf angekündigte Cash-Übernahmen.

    python3 -m quant.research.mergarb_study --run

Familie MERGARB in quant/research/hypothesis_queue.yaml. Phase 1
(docs/superpowers/specs/2026-07-30-weg-zu-50-cagr-design.md): nur Cash-Deals
(Aktientausch braucht ein Hedge-Bein gegen den Käufer — Phase 2). Terminal-
Datum ist ein Proxy: eine Aktie, die aus eod_bars verschwindet, gilt als
GESCHLOSSEN (Delisting = Closing); eine Aktie, die über den Horizont hinaus
weiterhandelt UND spürbar vom Angebotspreis abgedriftet ist, gilt als
GEBROCHEN. Das ist eine Näherung (kein 8-K-Terminierungs-Scan) — bewusst
offengelegt wie die Delta-Proxy-Notiz in options_phase_a.py.
"""

import argparse
import sys

import numpy as np
import pandas as pd

from quant.config import BQ_DATASET, GCP_PROJECT
from quant.data.bq import query
from quant.data.merger_ingest import T_DEALS
from quant.research.exotic_sleeves import fred

MAX_HORIZON_DAYS = 270
BREAK_DRIFT_PCT = 0.15   # Abweichung vom letzten Preis, ab der ein noch
                         # offener, über-den-Horizont-hinaus laufender Deal
                         # als gebrochen gilt statt als weiter offen
CLIP_DAILY_RETURN = 0.50   # Einzeltitel-Tagesbewegungen jenseits dessen sind
                            # praktisch immer Datenartefakte (falscher Tick,
                            # Ticker-Wiederverwendung mitten in der Serie —
                            # s. ATTO im Task-3-Report), nicht reale Merger-
                            # Arb-Ökonomie (ein echtes Bruch-Gap liegt bei
                            # -30% bis -40%, nie bei mehreren hundert Prozent).
                            # Gleiches Prinzip wie SHARPE_CAP in
                            # discovery.combined_sharpe: ein einzelner
                            # Extremwert soll das Aggregat nicht dominieren.
IMPLAUSIBLE_SPREAD_PCT = 2.0   # >200% Spread zwischen deal_price_cash und
                                # announce_px ist für einen echten Merger-Arb-
                                # Deal praktisch ausgeschlossen (reale Spreads
                                # bei Ankündigung liegen weit unter 100%,
                                # selbst bei hohem Bruchrisiko). Ein Wert
                                # darüber deutet auf einen falsch aufgelösten
                                # Ticker in quant.merger_deals hin (Task-2-
                                # Datenproblem: derselbe Ticker wurde zwei
                                # unterschiedlichen CIKs zugeordnet, z.B.
                                # ARJ/LZR — s. Task-3-Report), nicht auf einen
                                # echten Deal. Wird hier verworfen statt still
                                # die Sharpe zu verzerren.
PLAUSIBLE_SPREAD_BAND = (-0.05, 0.25)
# Zweite, engere Plausibilitätsstufe über IMPLAUSIBLE_SPREAD_PCT hinaus
# (Review-Fund C2): EDGAR indexiert ein gemeinsames 425/DEFM14A unter BEIDEN
# Parteien-CIKs mit derselben Accession, und collect() behält beide Zeilen
# bewusst (sonst ginge der delistete Ziel-Ticker verloren, s. dortiger
# Kommentar). Folge: der KÄUFER landet mit dem Cash-Preis des ZIELS in
# quant.merger_deals — ökonomisch bedeutungslos für sein eigenes Papier
# (bestätigt: CSCO mit WebEx' $57.00, LLY mit ImClones $70.00, dazu PFE/VZ).
# Erkennbar am Spread: ein NEGATIVER Spread (Angebotspreis unter Marktpreis)
# ist bei einer echten Merger-Arb-Longposition definitorisch unmöglich, und
# reale Spreads bei Ankündigung liegen praktisch immer unter +25%. Die
# saubere Lösung wäre Ziel/Käufer-Disambiguierung im Ingester — Phase 2,
# bewusst nicht hier. Bis dahin: Band-Filter statt stiller Verzerrung.


def resolve_terminal_date(prices: pd.Series, announce_date,
                          max_horizon_days: int = MAX_HORIZON_DAYS,
                          deal_price_cash: float | None = None
                          ) -> tuple[pd.Timestamp | None, str]:
    """prices: tägliche Kursreihe des Ziels AB dem Ankündigungstag (Index =
    Handelstage, wie sie in eod_bars vorliegen — kein künstliches Auffüllen).
    Liefert (Terminaldatum, Status) mit Status ∈ {closed, break, open,
    no_data}. no_data: die Kursreihe hat keine Beobachtung nahe dem
    Ankündigungstag (typischerweise Ticker-Wiederverwendung in eod_bars,
    nicht ein tatsächlich offener Deal) — siehe Plausibilitäts-Gate unten.

    deal_price_cash: der Angebotspreis. Wird für die Bruch-Erkennung
    gebraucht — "gebrochen" heißt laut Modul-Docstring und Design-Spec, dass
    der Kurs vom ANGEBOTSPREIS abgedriftet ist (ein noch laufender Deal
    handelt nahe am Angebot, ein geplatzter fällt auf sein Stand-alone-Niveau
    zurück). Die Vorversion maß stattdessen die Drift gegen den Kurs AM
    Horizontende und benutzte deal_price_cash gar nicht — das erkennt einen
    Bruch nur, wenn er NACH dem Horizontende passiert, und übersieht jeden
    Deal, der schon innerhalb des Horizonts geplatzt ist und danach flach
    auf dem tieferen Niveau weiterhandelt (Review-Fund I8). Ohne
    deal_price_cash (None/NaN/<=0) fällt die Funktion auf das alte
    Horizontende-Verhalten zurück, damit sie ohne Deal-Terms testbar
    bleibt."""
    prices = prices.dropna().sort_index()
    ann = pd.Timestamp(announce_date)
    if prices.empty:
        return None, "open"
    # Plausibilitäts-Gate: hat die Kursreihe überhaupt Beobachtungen nahe dem
    # Ankündigungstag? Wenn nicht, deckt eod_bars den Deal-Zeitraum für dieses
    # Symbol gar nicht ab — typischerweise Ticker-Wiederverwendung (das Symbol
    # wurde später einer anderen, unabhängigen Firma zugewiesen) statt eines
    # tatsächlich noch offenen Deals. Das ist ein eigener Status, kein "open".
    near_announce = prices.loc[ann - pd.Timedelta(days=15):
                                ann + pd.Timedelta(days=15)]
    if near_announce.empty:
        return None, "no_data"
    horizon_end = ann + pd.Timedelta(days=max_horizon_days)
    last_obs = prices.index[-1]
    # Symbol handelt nicht mehr, aber der allgemeine Handelskalender läuft
    # weiter (der Aufrufer übergibt nur Kurse bis "heute") → Delisting = Close.
    today = pd.Timestamp.today().normalize()
    trading_gap = (today - last_obs).days
    if trading_gap > 10:
        if (last_obs - ann).days > 2 * max_horizon_days:
            # Ein "Closing" das >2x den Horizont braucht (>~18 Monate) ist bei
            # einem echten M&A-Deal praktisch ausgeschlossen — fast immer
            # Ticker-Wiederverwendung: das Symbol wurde später einer anderen,
            # unabhängigen Firma zugewiesen, die selbst irgendwann delisted
            # wurde (s. TMA-Fall im Task-3-Report: 14 Jahre "Haltedauer").
            return None, "no_data"
        return last_obs, "closed"
    if last_obs <= horizon_end:
        return None, "open"
    tail = prices.loc[ann:horizon_end]
    if tail.empty:
        return None, "open"
    ref = (float(deal_price_cash)
           if deal_price_cash is not None and pd.notna(deal_price_cash)
           and float(deal_price_cash) > 0 else float(tail.iloc[-1]))
    drift = abs(prices.iloc[-1] / ref - 1)
    if drift > BREAK_DRIFT_PCT:
        return horizon_end, "break"
    return None, "open"


def deal_return_series(announce_date, terminal_date,
                       prices: pd.Series) -> pd.Series:
    """Tägliche einfache Renditen von announce_date bis terminal_date
    (inklusive), aus der Kursreihe des Ziels."""
    p = prices.dropna().sort_index()
    p = p.loc[pd.Timestamp(announce_date):pd.Timestamp(terminal_date)]
    if len(p) < 2:
        return pd.Series(dtype=float)
    return p.pct_change().dropna()


def _load_price_history(symbols: list[str], min_date) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    syms = ", ".join(repr(s) for s in symbols)
    df = query(f"""
      SELECT date, symbol, adjusted_close AS px
      FROM `{GCP_PROJECT}.{BQ_DATASET}.eod_bars`
      WHERE symbol IN ({syms}) AND date >= '{min_date}' AND adjusted_close > 0
      ORDER BY symbol, date""")
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df


def load_deals(cash_only: bool = True) -> pd.DataFrame:
    df = query(f"SELECT * FROM `{T_DEALS}`")
    if df.empty:
        return df
    if cash_only:
        # NICHT auf consideration_type == "cash" filtern: fast alle echten
        # Cash-Deals (Splunk, Nuance, Activision, Twitter) klassifizieren als
        # "mixed", weil Merger-Filings praktisch immer auch die Cash-Abgeltung
        # von Mitarbeiter-Optionen/RSUs in "shares of ... common stock"-
        # Sprache beschreiben, was den Aktientausch-Regex auslöst — selbst bei
        # einem für die eigentlichen Aktionäre zu 100% Cash-Deal. Der
        # eigentliche Filter ist "hat einen extrahierten Cash-Preis", nicht
        # "exakt als cash klassifiziert" (s. Task-2-Nacharbeit).
        df = df[df["deal_price_cash"].notna()
               & (df["consideration_type"] != "stock")]
    return df.reset_index(drop=True)


def _resolved_deals() -> pd.DataFrame:
    """Deals + ihr Terminalstatus, je aus eod_bars nachgerechnet."""
    deals = load_deals(cash_only=True)
    if deals.empty:
        return deals
    prices = _load_price_history(list(deals["symbol"].unique()),
                                 deals["announce_date"].min())
    if prices.empty:
        return pd.DataFrame()
    by_sym = {s: g.set_index("date")["px"] for s, g in prices.groupby("symbol")}
    rows = []
    n_no_px, no_px_syms = 0, set()
    for _, d in deals.iterrows():
        px = by_sym.get(d["symbol"])
        if px is None:
            # eod_bars kennt das Symbol im gesamten Abfragefenster nicht.
            # Wird wie jeder andere Filter hier gezählt und offengelegt statt
            # still zu verschwinden (Review-Fund I6).
            n_no_px += 1
            no_px_syms.add(d["symbol"])
            continue
        term, status = resolve_terminal_date(
            px, d["announce_date"], deal_price_cash=d["deal_price_cash"])
        after = px.loc[pd.Timestamp(d["announce_date"]):]
        announce_px = float(after.iloc[0]) if len(after) else np.nan
        # REALISIERTE Rendite über die tatsächliche Haltedauer (Ankündigung →
        # Terminaldatum), aus derselben Kursreihe und mit demselben
        # ±CLIP_DAILY_RETURN-Deckel wie returns() — nicht aus der
        # Angebotspreis-Formel. Die Vorversion rechnete in
        # check_predictions() sowohl "spread" ALS AUCH "holding_ret" aus
        # deal_price_cash/announce_px-1; der Monotonietest verglich damit
        # eine Größe mit sich selbst und war eine Tautologie (Review-Fund
        # C1). NaN für noch offene Deals — die haben keine Haltedauer.
        realized = np.nan
        if term is not None and status in ("closed", "break"):
            r = deal_return_series(d["announce_date"], term, px)
            if len(r):
                realized = float(
                    (1 + r.clip(-CLIP_DAILY_RETURN, CLIP_DAILY_RETURN))
                    .prod() - 1)
        rows.append({**d.to_dict(), "terminal_date": term, "status": status,
                     "announce_px": announce_px, "realized_ret": realized})
    if n_no_px:
        print(f"{n_no_px} Deals ohne eod_bars-Kursdeckung verworfen "
              f"({len(no_px_syms)} Symbole)")
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    signed = out["deal_price_cash"] / out["announce_px"] - 1
    implausible = signed.abs() > IMPLAUSIBLE_SPREAD_PCT
    if implausible.any():
        n = int(implausible.sum())
        syms = sorted(out.loc[implausible, "symbol"].unique().tolist())
        print(f"{n} Deals mit unplausiblem Spread (>200%) verworfen: {syms}")
        out = out[~implausible].reset_index(drop=True)
        signed = signed[~implausible].reset_index(drop=True)
    lo, hi = PLAUSIBLE_SPREAD_BAND
    off_band = ~signed.between(lo, hi)   # NaN-Spreads fallen hier mit heraus
    if off_band.any():
        n = int(off_band.sum())
        n_neg = int((signed < lo).sum())
        syms = sorted(out.loc[off_band, "symbol"].unique().tolist())
        print(f"{n} Deals mit Spread außerhalb [{lo:.0%},{hi:.0%}] verworfen "
              f"({n_neg} davon negativ) — Akquisiteur-Zeilen mit dem "
              f"Cash-Preis des Ziels bzw. Ticker-Fehlauflösung: "
              f"{len(syms)} Symbole, z.B. {syms[:15]}")
        out = out[~off_band].reset_index(drop=True)
    return out


def returns(**params) -> pd.Series:
    """Discovery-Pipeline-Entry-Point (discovery.evaluate erwartet
    `fn(**params) -> pd.Series`). Nur Tage, an denen mindestens ein Deal
    offen ist — auf Tagen ohne Position künstlich 0 einzutragen würde die
    annualisierte Sharpe gegen ein Buch verwässern, das ungenutztes Kapital
    tatsächlich reinvestiert (siehe Design-Spec, Hebel 2)."""
    resolved = _resolved_deals()
    if resolved.empty:
        return pd.Series(dtype=float)
    closed = resolved[resolved["status"].isin(["closed", "break"])]
    if closed.empty:
        return pd.Series(dtype=float)
    prices = _load_price_history(list(closed["symbol"].unique()),
                                 closed["announce_date"].min())
    by_sym = {s: g.set_index("date")["px"] for s, g in prices.groupby("symbol")}
    per_deal = []
    per_deal_symbols = []
    for _, d in closed.iterrows():
        px = by_sym.get(d["symbol"])
        if px is None or pd.isna(d["terminal_date"]):
            continue
        r = deal_return_series(d["announce_date"], d["terminal_date"], px)
        if len(r):
            per_deal.append(r)
            per_deal_symbols.append(d["symbol"])
    if not per_deal:
        return pd.Series(dtype=float)
    # Verteidigungs-Deckel: eine Einzeltitel-Tagesrendite jenseits von
    # CLIP_DAILY_RETURN ist so gut wie nie reale Merger-Arb-Ökonomie, sondern
    # ein Datenartefakt (falscher eod_bars-Tick oder Ticker-Wiederverwendung
    # mitten in der Serie). Wird gekappt statt das Aggregat zu dominieren —
    # gleiches Prinzip wie SHARPE_CAP in discovery.combined_sharpe.
    n_clipped = 0
    clipped_symbols = set()
    for i, s in enumerate(per_deal):
        over = s.abs() > CLIP_DAILY_RETURN
        if over.any():
            n_clipped += int(over.sum())
            clipped_symbols.add(per_deal_symbols[i])
            per_deal[i] = s.clip(-CLIP_DAILY_RETURN, CLIP_DAILY_RETURN)
    if n_clipped:
        print(f"{n_clipped} Tagesrenditen über ±{CLIP_DAILY_RETURN:.0%} "
              f"gekappt (Datenartefakte): {sorted(clipped_symbols)}")
    wide = pd.concat(per_deal, axis=1)
    return wide.mean(axis=1, skipna=True).dropna()


def live_weights() -> tuple[dict, str]:
    """G8-Entry-Point: aktuell offene Cash-Deals, gleichgewichtet, gross<=1.0.

    Aktualitäts-Gate (Review-Fund I7): "offen" allein reicht nicht — rund die
    Hälfte der so klassifizierten Deals wurde vor Jahren angekündigt (ältester
    Fall 2007) und fällt nur wegen Lücken in der Auflösungskette durch. Ein
    Deal, der schon länger als sein eigener plausibler Horizont läuft, ist
    keine handelbare Live-Position, sondern ein Datenartefakt. Fail-closed
    wie die Margin-Gates im ETF-Bot: im Zweifel keine Position."""
    resolved = _resolved_deals()
    if resolved.empty:
        return {}, "keine Deal-Daten (fail-closed)"
    cutoff = (pd.Timestamp.today().normalize()
              - pd.Timedelta(days=MAX_HORIZON_DAYS))
    ann = pd.to_datetime(resolved["announce_date"])
    open_deals = resolved[(resolved["status"] == "open") & (ann >= cutoff)]
    if open_deals.empty:
        return {}, "kein offener Cash-Deal → flat"
    w = 1.0 / len(open_deals)
    weights = {s: w for s in open_deals["symbol"].unique()}
    return (weights, f"{len(weights)} offene Cash-Deals (Ankündigung jünger "
                     f"als {MAX_HORIZON_DAYS}d), EW")


def check_predictions() -> dict:
    """Die drei vorregistrierten Vorhersagen aus hypothesis_queue.yaml
    (id: MERGARB). (b) Cash>Aktientausch ist in Phase 1 NICHT prüfbar (keine
    Aktientausch-Preisextraktion) — wird als 'nicht_pruefbar' ausgewiesen,
    nicht stillschweigend übersprungen."""
    out = {"a_monotonie": None, "b_cash_vs_stock": "nicht_pruefbar_phase1",
          "c_vix_bruchrate": None}
    resolved = _resolved_deals()
    if resolved.empty:
        return out
    closed = resolved[resolved["status"].isin(["closed", "break"])].copy()
    if len(closed) >= 5:
        closed["spread"] = (closed["deal_price_cash"] / closed["announce_px"]
                            - 1)
        # holding_ret = REALISIERTE Rendite über die tatsächliche Haltedauer
        # (aus _resolved_deals, s. dort). Bruch-Deals bleiben drin: der Test
        # fragt, ob ein höherer Ankündigungsspread im Mittel mehr Rendite
        # BRINGT — die Brüche sind genau das Risiko, für das die Prämie
        # bezahlt wird, sie herauszunehmen würde den Test schönen.
        closed["holding_ret"] = closed["realized_ret"]
        mono = closed.dropna(subset=["spread", "holding_ret"]).copy()
        if len(mono) >= 5:
            mono["decile"] = pd.qcut(mono["spread"],
                                     min(5, mono["spread"].nunique()),
                                     duplicates="drop")
            by_decile = mono.groupby("decile",
                                     observed=True)["holding_ret"].mean()
            out["a_monotonie"] = by_decile.to_dict()
            out["a_n"] = int(len(mono))
            out["a_monoton"] = bool(by_decile.is_monotonic_increasing)
    if len(closed) >= 5:
        vix = fred("VIXCLS", start="2015-01-01")
        vix_at_announce = vix.reindex(pd.to_datetime(closed["announce_date"]),
                                      method="ffill")
        closed["vix"] = vix_at_announce.values
        high = closed[closed["vix"] > 25]
        low = closed[closed["vix"] <= 25]
        out["c_vix_bruchrate"] = {
            "hoch_vix": float(high["status"].eq("break").mean()) if len(high) else None,
            "niedrig_vix": float(low["status"].eq("break").mean()) if len(low) else None,
        }
    return out


def run():
    deals = load_deals(cash_only=False)
    # Nutzbar = "hat einen extrahierten Cash-Preis", NICHT
    # consideration_type=='cash' — letzteres unterzählt massiv (fast jeder
    # echte Cash-Deal klassifiziert als "mixed", s. load_deals-Kommentar).
    n_px = int(deals["deal_price_cash"].notna().sum()) if len(deals) else 0
    n_cash = int((deals["consideration_type"] == "cash").sum()) if len(deals) else 0
    print(f"{len(deals):,} Deals in quant.merger_deals — {n_px:,} mit "
          f"extrahiertem Cash-Preis (nutzbar); nur {n_cash:,} tragen das "
          "Label consideration_type='cash' (unzuverlässig)")
    r = returns()
    if r.empty:
        print("keine auswertbare Rendite-Serie (zu wenige resolved Deals)")
        return
    ann = 252
    sharpe = r.mean() / r.std() * np.sqrt(ann) if r.std() > 0 else 0.0
    print(f"Sharpe (nur Tage mit offenem Deal): {sharpe:.2f}, n={len(r)}")
    preds = check_predictions()
    print("Vorhersagen:", preds)
    w, why = live_weights()
    print(f"live_weights: {why} ({len(w)} Positionen)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    a = p.parse_args()
    if not a.run:
        p.print_help()
        sys.exit(1)
    run()
