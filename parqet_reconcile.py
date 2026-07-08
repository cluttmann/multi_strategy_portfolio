"""
parqet_reconcile.py — Verify a Parqet export against the real Alpaca activities.

This is the *reverse* of parqet_sync.py: instead of pushing Alpaca activities
into Parqet, it takes a Parqet CSV export and checks that every row matches a
real Alpaca activity — catching the two failure modes Parqet's importer is prone
to: a fill imported twice (duplicate) or a fill silently dropped (missing).

Usage
-----
    python3 parqet_reconcile.py --csv "~/Downloads/Alpaca Trading-YYYYMMDD-HHMMSS.csv"
    python3 parqet_reconcile.py --csv export.csv --env live

Exit code is 0 when trades fully reconcile, 1 otherwise (CI-friendly).

How it works
------------
* Trades are matched at the **individual fill level** (sym, side, qty). Alpaca
  fills a fractional order as a whole-share fill + a fractional fill — two FILL
  records under one order_id — and Parqet mirrors exactly that granularity, so
  do NOT aggregate by order_id (you'd get false mismatches).
* The comparison window is capped at the Parqet export's latest row date, so
  Alpaca dividends/interest booked *after* you exported don't show up as
  spurious "missing in Parqet" rows.
* ISIN↔symbol mapping is imported from parqet_sync.SYMBOL_TO_ISIN — single
  source of truth. Unknown ISINs in the CSV are reported, not silently dropped.
"""
import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

from parqet_sync import SYMBOL_TO_ISIN

ISIN_TO_SYMBOL = {v: k for k, v in SYMBOL_TO_ISIN.items()}


def fetch_activities(api, activity_type: str, after_iso: str):
    """Page through every activity of a type, oldest-first.

    NOTE: parqet_sync.fetch_activities pages via ``chunk.next_page_token``, which
    doesn't exist on the plain list tradeapi returns — so it silently stops after
    the first 100. That's harmless for the incremental sync (always <100 new rows)
    but wrong for a full-history reconcile, so we paginate by last-id here.
    """
    out, token = [], None
    while True:
        chunk = api.get_activities(
            activity_types=activity_type, after=after_iso,
            direction="asc", page_size=100, page_token=token,
        )
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 100:
            break
        token = chunk[-1].id
    return out

# Tolerances
PRICE_TOL_PCT = 0.005   # 0.5% — above this a matched trade is flagged
DATE_TOL_DAYS = 1       # trade-date may shift 1 day (Parqet stamps CET midnight)


# -----------------------------------------------------------------------------
# Parsing helpers
# -----------------------------------------------------------------------------
def parse_de_number(s: str) -> float:
    """German number format: '1.513,00' -> 1513.0, '85,2' -> 85.2, '' -> 0.0."""
    s = (s or "").strip()
    if not s:
        return 0.0
    return float(s.replace(".", "").replace(",", "."))


def parse_de_date(s: str):
    return datetime.strptime(s.strip(), "%d.%m.%Y").date()


def load_parqet(path: str):
    """Return (trades, dividends, interest, deposits, unknown_isins, window_end)."""
    with open(os.path.expanduser(path), newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    if not rows:
        sys.exit(f"No rows parsed from {path} — is it a semicolon-delimited Parqet export?")

    trades, divs, interest, deposits = [], [], [], []
    unknown = set()
    all_dates = []
    for r in rows:
        d = parse_de_date(r["date"])
        all_dates.append(d)
        typ = r["type"]
        amt = parse_de_number(r["amount"])
        if typ in ("Buy", "Sell"):
            isin = r["identifier"]
            sym = ISIN_TO_SYMBOL.get(isin)
            if sym is None:
                unknown.add(f"{isin} ({r['holdingname'][:30]})")
            trades.append({
                "date": d, "sym": sym or f"?{isin}", "side": typ.lower(),
                "qty": parse_de_number(r["shares"]), "price": parse_de_number(r["price"]),
                "amount": amt,
            })
        elif typ == "Dividend":
            divs.append({"date": d, "amount": amt})
        elif typ == "Interest":
            interest.append({"date": d, "amount": amt})
        elif typ == "TransferIn":
            deposits.append({"date": d, "amount": amt})
        # other Parqet types (e.g. TransferOut) ignored for now
    return trades, divs, interest, deposits, unknown, max(all_dates)


# -----------------------------------------------------------------------------
# Alpaca helpers
# -----------------------------------------------------------------------------
def act_date(raw: dict):
    """Calendar date of an activity (FILL uses transaction_time, others 'date')."""
    ts = raw.get("transaction_time")
    if ts:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    return datetime.fromisoformat(raw["date"]).date()


def fetch_window(api, after_date, until_date):
    """Fetch every activity type we reconcile, filtered to <= until_date."""
    after_iso = (after_date - timedelta(days=1)).isoformat()
    out = defaultdict(list)
    for atype in ("FILL", "DIV", "CGD", "CSD", "INT"):
        for act in fetch_activities(api, atype, after_iso):
            raw = act._raw
            if act_date(raw) <= until_date:
                out[atype].append(raw)
    return out


# -----------------------------------------------------------------------------
# Reconciliation
# -----------------------------------------------------------------------------
def reconcile_trades(pq_trades, fills):
    """Greedy fill-level match. Returns (matched, unmatched_pq, leftover_al, price_flags, date_flags)."""
    al = [{
        "date": act_date(f), "sym": f["symbol"], "side": f["side"],
        "qty": float(f["qty"]), "price": float(f["price"]),
    } for f in fills]

    pool = defaultdict(list)
    for f in al:
        pool[(f["sym"], f["side"], round(f["qty"], 5))].append(f)

    matched = 0
    unmatched_pq, price_flags, date_flags = [], [], []
    for t in pq_trades:
        cand = pool.get((t["sym"], t["side"], round(t["qty"], 5)))
        if cand:
            o = min(cand, key=lambda o: abs((o["date"] - t["date"]).days))
            cand.remove(o)
            matched += 1
            if abs(o["price"] - t["price"]) > max(0.01, PRICE_TOL_PCT * t["price"]):
                price_flags.append((t, o))
            if abs((o["date"] - t["date"]).days) > DATE_TOL_DAYS:
                date_flags.append((t, o))
        else:
            unmatched_pq.append(t)
    leftover_al = [o for c in pool.values() for o in c]
    return matched, unmatched_pq, leftover_al, price_flags, date_flags


def fmt_money(x):
    return f"${x:,.2f}"


def main():
    ap = argparse.ArgumentParser(description="Reconcile a Parqet export against real Alpaca activities.")
    ap.add_argument("--csv", required=True, help="Path to the Parqet export CSV (semicolon-delimited).")
    ap.add_argument("--env", choices=["live", "paper"], default="live", help="Alpaca account (default: live).")
    args = ap.parse_args()

    load_dotenv()
    suffix = "LIVE" if args.env == "live" else "PAPER"
    api_key = os.getenv(f"ALPACA_API_KEY_{suffix}")
    api_secret = os.getenv(f"ALPACA_SECRET_KEY_{suffix}")
    base_url = "https://api.alpaca.markets" if args.env == "live" else "https://paper-api.alpaca.markets"
    if not (api_key and api_secret):
        sys.exit(f"Missing Alpaca {args.env} credentials in .env")
    api = tradeapi.REST(api_key, api_secret, base_url)

    trades, divs, interest, deposits, unknown, window_end = load_parqet(args.csv)
    window_start = min(t["date"] for t in trades) if trades else window_end
    print(f"Parqet export: {os.path.basename(args.csv)}")
    print(f"Window: {window_start} → {window_end}  (Alpaca activities after {window_end} ignored)")
    print(f"Account: {args.env}\n")

    if unknown:
        print("⚠  Unknown ISINs in CSV (not in parqet_sync.SYMBOL_TO_ISIN) — add them there:")
        for u in sorted(unknown):
            print(f"     {u}")
        print()

    acts = fetch_window(api, window_start, window_end)
    fills = acts["FILL"]

    # --- Trades ---
    matched, unmatched_pq, leftover_al, price_flags, date_flags = reconcile_trades(trades, fills)
    print("=== TRADES (fill-level) ===")
    print(f"  Parqet rows: {len(trades)} | Alpaca fills: {len(fills)}")
    print(f"  matched: {matched}/{len(trades)} | price flags: {len(price_flags)} | date flags: {len(date_flags)}")
    clean = not (unmatched_pq or leftover_al)
    if unmatched_pq:
        print(f"\n  ✗ {len(unmatched_pq)} Parqet trade(s) with NO Alpaca fill "
              f"(possible DUPLICATE or bad import):")
        for t in sorted(unmatched_pq, key=lambda x: x["date"]):
            print(f"      {t['date']} {t['side']:4} {t['sym']:5} qty={t['qty']:.6f} @ {t['price']:.4f}")
    if leftover_al:
        print(f"\n  ✗ {len(leftover_al)} Alpaca fill(s) with NO Parqet row "
              f"(MISSING from Parqet — import these):")
        for o in sorted(leftover_al, key=lambda x: x["date"]):
            print(f"      {o['date']} {o['side']:4} {o['sym']:5} qty={o['qty']:.6f} @ {o['price']:.4f}")
    if price_flags:
        print("\n  ⚠  matched but price differs >0.5%:")
        for t, o in price_flags:
            print(f"      {t['sym']} {t['side']} qty={t['qty']:.4f}: Parqet {t['price']:.4f} vs Alpaca {o['price']:.4f}")
    if date_flags:
        print("\n  ⚠  matched but trade date differs >1 day:")
        for t, o in date_flags:
            print(f"      {t['sym']} {t['side']} qty={t['qty']:.4f}: Parqet {t['date']} vs Alpaca {o['date']}")
    if clean and not price_flags and not date_flags:
        print("  ✓ all trades reconcile exactly")

    # --- Cash-flow categories (sum-level; counts differ by design) ---
    def al_sum(rows):
        return sum(float(r["net_amount"]) for r in rows)

    print("\n=== CASH FLOWS (sum check) ===")
    dep_pq, dep_al = sum(d["amount"] for d in deposits), al_sum(acts["CSD"])
    div_pq = sum(d["amount"] for d in divs)
    div_al = al_sum(acts["DIV"]) + al_sum(acts["CGD"])   # gross; Parqet records gross
    int_pq, int_al = sum(i["amount"] for i in interest), al_sum(acts["INT"])
    for label, pq, al_, n_pq, n_al in [
        ("Deposits ", dep_pq, dep_al, len(deposits), len(acts["CSD"])),
        ("Dividends", div_pq, div_al, len(divs), len(acts["DIV"]) + len(acts["CGD"])),
        ("Interest ", int_pq, int_al, len(interest), len(acts["INT"])),
    ]:
        diff = pq - al_
        mark = "✓" if abs(diff) < 1.0 else "✗"
        print(f"  {mark} {label}: Parqet {fmt_money(pq)} ({n_pq}) vs Alpaca {fmt_money(al_)} ({n_al})  Δ {fmt_money(diff)}")

    print()
    if clean:
        print("RESULT: trades fully reconcile ✓")
        sys.exit(0)
    else:
        print("RESULT: trade discrepancies found — see ✗ rows above ✗")
        sys.exit(1)


if __name__ == "__main__":
    main()
