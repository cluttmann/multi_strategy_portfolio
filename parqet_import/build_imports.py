"""Build Parqet import files from Alpaca ground truth.

Output (semicolon-delimited, German decimal commas, matching parqet.com/basic.csv
and parqet.com/basic_cash.csv exactly):

  parqet_full_equity.csv  — every Buy/Sell/Dividend since inception, with per-trade
                            regulatory fees in `fee` and DIVNRA withholding in `tax`
  parqet_full_cash.csv    — TransferIn (deposits, with FX conversion fee) + Interest
  parqet_missing_equity.csv — cherry-pick: only what Parqet lacks today

READ-ONLY against Alpaca; writes local files only.
"""
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

OUT = os.path.dirname(os.path.abspath(__file__))
REPO = "/Users/carl/Coding/hfea_strategy"
sys.path.insert(0, REPO)
from parqet_sync import SYMBOL_TO_ISIN, HOLDING_ID  # noqa: E402

alpaca = json.load(open(os.path.join(OUT, "alpaca_raw.json")))
extra = json.load(open(os.path.join(OUT, "alpaca_extra.json")))
DIVNRA, JNLC = extra["DIVNRA"], extra["JNLC"]


def de(x, dec=6):
    """Float -> German decimal string, trailing zeros trimmed."""
    s = f"{float(x):.{dec}f}".rstrip("0").rstrip(".")
    if s in ("", "-0"):
        s = "0"
    return s.replace(".", ",")


def d10(r):
    return (r.get("date") or (r.get("transaction_time") or "")[:10])[:10]


# ---------------------------------------------------------------- fees
# 30 "Funding Wallet incoming alpaca conversion fee" -> belong on the deposit.
# 66 REG/TAF/CAT regulatory fees -> belong in the trade rows' `fee` column.
conv_fees, trade_fees_by_day = [], defaultdict(float)
for r in alpaca["FEE"]:
    amt = abs(float(r.get("net_amount") or 0))
    desc = r.get("description") or ""
    if "conversion fee" in desc:
        conv_fees.append((d10(r), amt))
    else:
        trade_fees_by_day[d10(r)] += amt

# ---------------------------------------------------------------- dividends
# Cancel reversal pairs: a negative DIV that exactly reverses an earlier positive
# DIV of the same symbol + per-share rate (the RSSB 2025-12-30 -> 2026-05-25
# reclassification). Both sides drop out, leaving the corrected re-booked row.
divs = sorted(alpaca["DIV"], key=lambda r: (d10(r), r.get("id")))
cancelled = set()
for neg in divs:
    if float(neg.get("net_amount") or 0) >= 0 or neg["id"] in cancelled:
        continue
    for pos in divs:
        if pos["id"] in cancelled or pos["id"] == neg["id"]:
            continue
        if (float(pos.get("net_amount") or 0) == -float(neg["net_amount"])
                and pos.get("symbol") == neg.get("symbol")
                and pos.get("per_share_amount") == neg.get("per_share_amount")
                and d10(pos) < d10(neg)):
            cancelled |= {pos["id"], neg["id"]}
            print(f"  cancelled reversal pair: {neg.get('symbol')} "
                  f"{d10(pos)} +{pos['net_amount']} <-> {d10(neg)} {neg['net_amount']}")
            break

# DIVNRA withholding, netted per (date, symbol) with its own reversals cancelled out
tax_by_key = defaultdict(float)
for r in DIVNRA:
    tax_by_key[(d10(r), r.get("symbol"))] += -float(r.get("net_amount") or 0)

# ---------------------------------------------------------------- equity rows
EQ_HEADER = ["id", "date", "isin", "type", "price", "shares", "fee", "tax", "currency"]
eq_rows, skipped = [], []

fills = sorted(alpaca["FILL"], key=lambda r: (d10(r), r.get("id")))
# pick the fill on each day that carries that day's regulatory fee: largest sell,
# else largest buy. Keeps the total exact without inventing sub-cent precision.
fee_target = {}
by_day = defaultdict(list)
for r in fills:
    by_day[d10(r)].append(r)
for day, rs in by_day.items():
    if day not in trade_fees_by_day:
        continue
    sells = [r for r in rs if (r.get("side") or "").lower() == "sell"]
    pick = max(sells or rs, key=lambda r: float(r["qty"]) * float(r["price"]))
    fee_target[pick["id"]] = trade_fees_by_day[day]

unplaced = dict(trade_fees_by_day)
for r in fills:
    side = (r.get("side") or "").lower()
    isin = SYMBOL_TO_ISIN.get((r.get("symbol") or "").strip(), "")
    if side not in ("buy", "sell") or not isin:
        skipped.append(("FILL", r.get("id"), r.get("symbol"), "no isin/side"))
        continue
    fee = fee_target.get(r["id"], 0.0)
    if fee:
        unplaced.pop(d10(r), None)
    eq_rows.append([r["id"], d10(r), isin, "Buy" if side == "buy" else "Sell",
                    de(r["price"]), de(r["qty"]), de(fee, 2), "0", "USD"])

for r in divs:
    if r["id"] in cancelled:
        continue
    net = float(r.get("net_amount") or 0)
    ps = float(r.get("per_share_amount") or 0)
    isin = SYMBOL_TO_ISIN.get((r.get("symbol") or "").strip(), "")
    if net <= 0 or ps <= 0 or not isin:
        skipped.append(("DIV", r.get("id"), r.get("symbol"), f"net={net} ps={ps}"))
        continue
    key = (d10(r), r.get("symbol"))
    tax = tax_by_key.get(key, 0.0)
    tax_by_key[key] = 0.0  # attach once per (date,symbol) group
    eq_rows.append([r["id"], d10(r), isin, "Dividend", de(ps), de(net / ps),
                    "0", de(tax, 2), "USD"])

# Any withholding left unattached belongs to a cancelled reversal pair (the RSSB
# reclassification). Roll it onto that symbol's last surviving dividend row so the
# tax total stays exact.
isin_of = SYMBOL_TO_ISIN
for (dt, sym), amt in list(tax_by_key.items()):
    if abs(amt) < 1e-9:
        continue
    isin = isin_of.get(sym, "")
    cands = [r for r in eq_rows if r[3] == "Dividend" and r[2] == isin]
    if not cands:
        print(f"  WARNING: {amt:+.2f} withholding for {sym} {dt} has no dividend row")
        continue
    tgt = max(cands, key=lambda r: r[1])
    tgt[7] = de(float(tgt[7].replace(",", ".")) + amt, 2)
    print(f"  rolled {amt:+.2f} withholding ({sym} {dt}, cancelled pair) "
          f"onto {sym} {tgt[1]} -> tax {tgt[7]}")
    tax_by_key[(dt, sym)] = 0.0

eq_rows.sort(key=lambda x: (x[1], x[0]))

# ---------------------------------------------------------------- cash rows
CASH_HEADER = ["date", "amount", "tax", "fee", "type", "holding", "currency"]
cash_rows = []
pool = list(conv_fees)
for r in sorted(alpaca["CSD"], key=lambda r: d10(r)):
    amt = float(r.get("net_amount") or 0)
    day = d10(r)
    # pair the deposit with its conversion fee: same day, closest to 1.5% of amount
    same = [i for i, (fd, _) in enumerate(pool) if fd == day]
    fee = 0.0
    if same:
        i = min(same, key=lambda i: abs(pool[i][1] - amt * 0.015))
        fee = pool.pop(i)[1]
    cash_rows.append([day, de(amt, 2), "0", de(fee, 2), "TransferIn", HOLDING_ID, "USD"])

for r in sorted(alpaca["INT"], key=lambda r: d10(r)):
    cash_rows.append([d10(r), de(r.get("net_amount"), 2), "0", "0",
                      "Interest", HOLDING_ID, "USD"])

for r in sorted(JNLC, key=lambda r: d10(r)):
    cash_rows.append([d10(r), de(r.get("net_amount"), 2), "0", "0",
                      "Interest", HOLDING_ID, "USD"])

cash_rows.sort(key=lambda x: x[0])


def write(name, header, rows):
    p = os.path.join(OUT, name)
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";", lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    print(f"  wrote {name}: {len(rows)} rows")


print("\n=== reversal cancellation ===")
print("=== writing files ===")
write("parqet_full_equity.csv", EQ_HEADER, eq_rows)
write("parqet_full_cash.csv", CASH_HEADER, cash_rows)

# cherry-pick: dividends Parqet is missing today
MISSING_IDS = {"2026-06-04SHV", "2026-06-04TLT", "2026-06-04AGG", "2026-08-05BND"}
miss = [r for r in eq_rows if r[3] == "Dividend"
        and (r[1] + [k for k, v in SYMBOL_TO_ISIN.items() if v == r[2]][0]) in MISSING_IDS]
write("parqet_missing_equity.csv", EQ_HEADER, miss)

# ---------------------------------------------------------------- verify
print("\n=== reconciliation of generated files vs Alpaca ===")
buys = sum(float(r[4].replace(",", ".")) * float(r[5].replace(",", "."))
           for r in eq_rows if r[3] == "Buy")
sells = sum(float(r[4].replace(",", ".")) * float(r[5].replace(",", "."))
            for r in eq_rows if r[3] == "Sell")
divs_amt = sum(float(r[4].replace(",", ".")) * float(r[5].replace(",", "."))
               for r in eq_rows if r[3] == "Dividend")
eq_fee = sum(float(r[6].replace(",", ".")) for r in eq_rows)
eq_tax = sum(float(r[7].replace(",", ".")) for r in eq_rows)
ti = sum(float(r[1].replace(",", ".")) for r in cash_rows if r[4] == "TransferIn")
ti_fee = sum(float(r[3].replace(",", ".")) for r in cash_rows if r[4] == "TransferIn")
inte = sum(float(r[1].replace(",", ".")) for r in cash_rows if r[4] == "Interest")

a_int = sum(float(r["net_amount"]) for r in alpaca["INT"])
a_jnl = sum(float(r["net_amount"]) for r in JNLC)
a_dnr = sum(float(r["net_amount"]) for r in DIVNRA)

print(f"  Buy  rows {sum(1 for r in eq_rows if r[3]=='Buy'):3}  gross {buys:>12,.2f}")
print(f"  Sell rows {sum(1 for r in eq_rows if r[3]=='Sell'):3}  gross {sells:>12,.2f}")
print(f"  Div  rows {sum(1 for r in eq_rows if r[3]=='Dividend'):3}  gross {divs_amt:>12,.2f}"
      f"   (Alpaca DIV net {sum(float(r['net_amount']) for r in alpaca['DIV']):,.2f})")
print(f"  equity fee total {eq_fee:>8,.2f}  (Alpaca REG/TAF/CAT {sum(trade_fees_by_day.values()):,.2f})")
print(f"  equity tax total {eq_tax:>8,.2f}  (Alpaca DIVNRA {-a_dnr:,.2f})")
print(f"  TransferIn {ti:>12,.2f} fee {ti_fee:,.2f}  (Alpaca CSD {sum(float(r['net_amount']) for r in alpaca['CSD']):,.2f}, conv fee {sum(a for _,a in conv_fees):,.2f})")
print(f"  Interest   {inte:>12,.2f}          (Alpaca INT {a_int:,.2f} + JNLC {a_jnl:,.2f} = {a_int+a_jnl:,.2f})")
print(f"  unplaced trade fees: {unplaced} (sum {sum(unplaced.values()):.2f})")
print(f"  skipped rows: {len(skipped)} -> {skipped}")
cash = ti - ti_fee + inte + divs_amt - eq_tax - eq_fee - (buys - sells)
print(f"\n  implied cash from generated files: {cash:,.2f}")
print(f"  Alpaca activity-derived cash:      -1,160.83  (broker reports -1,168.76)")
