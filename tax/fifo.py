"""
fifo.py — FIFO (First-In-First-Out) lot-matching engine for German tax law.

German tax law (InvStG) requires that when selling securities, the oldest
lots (FIFO) are consumed first. Each lot tracks:
  - The purchase price in USD
  - The ECB EUR/USD rate on the purchase date → cost basis in EUR
  - The sale price in USD
  - The ECB EUR/USD rate on the sale date → proceeds in EUR

Teilfreistellung (TFS) is applied to the realized gain/loss based on
the asset classification (equity ETF = 30%, bond ETF = 0%).

Usage:
    from tax.fifo import FIFOEngine

    engine = FIFOEngine()
    engine.process_buy("UPRO", "2024-03-15", Decimal("10"), Decimal("52.30"), Decimal("1.0876"))
    trades = engine.process_sell("UPRO", "2024-09-20", Decimal("5"), Decimal("61.50"), Decimal("1.1102"))
"""

from dataclasses import dataclass
from decimal import Decimal
from collections import defaultdict

from tax.config import get_tfs_rate


@dataclass
class FIFOLot:
    """
    Represents a single purchase lot tracked for FIFO purposes.

    Each buy creates one or more lots. When selling, the oldest lots
    for that symbol are consumed first (FIFO order).
    """
    symbol: str
    buy_date: str               # "YYYY-MM-DD"
    qty_remaining: Decimal      # Shares still held in this lot
    price_usd: Decimal          # Per-share purchase price in USD
    fx_rate: Decimal            # ECB EUR/USD rate on buy date
    cost_eur_per_unit: Decimal  # Per-share cost in EUR (price_usd / fx_rate)

    @property
    def total_cost_eur(self) -> Decimal:
        """Total cost basis in EUR for remaining shares in this lot."""
        return (self.qty_remaining * self.cost_eur_per_unit).quantize(Decimal("0.01"))


@dataclass
class RealizedTrade:
    """
    Represents a realized gain/loss from a FIFO-matched sale.

    One sell order may produce multiple RealizedTrade entries if it
    consumes shares from multiple lots.
    """
    symbol: str
    isin: str
    buy_date: str               # Date the shares were originally purchased
    sell_date: str              # Date the shares were sold
    qty: Decimal               # Number of shares sold from this lot
    cost_eur: Decimal          # Total cost basis in EUR (qty * cost_eur_per_unit)
    proceeds_eur: Decimal      # Total sale proceeds in EUR
    gain_loss_eur: Decimal     # proceeds_eur - cost_eur (before TFS)
    tfs_rate: Decimal          # Teilfreistellung rate (0.30 for equity, 0.00 for bonds)
    taxable_gain_eur: Decimal  # gain_loss_eur * (1 - tfs_rate) — what goes into Anlage KAP
    buy_fx_rate: Decimal       # ECB rate used for the buy
    sell_fx_rate: Decimal      # ECB rate used for the sell
    buy_price_usd: Decimal     # Original buy price per share
    sell_price_usd: Decimal    # Sell price per share


class FIFOEngine:
    """
    FIFO lot-matching engine for German tax calculation.

    Maintains open positions as a dict of symbol → list of FIFOLot,
    ordered by buy date (oldest first). Sells consume lots in FIFO order.
    """

    def __init__(self):
        # { symbol: [FIFOLot, ...] } — lots ordered oldest-first
        self.open_positions: dict[str, list[FIFOLot]] = defaultdict(list)
        # All realized trades from sells
        self.realized_trades: list[RealizedTrade] = []

    def process_buy(
        self,
        symbol: str,
        date: str,
        qty: Decimal,
        price_usd: Decimal,
        fx_rate: Decimal,
    ) -> None:
        """
        Record a purchase. Creates a new FIFO lot.

        Args:
            symbol: Ticker symbol (e.g. "UPRO").
            date: Purchase date "YYYY-MM-DD".
            qty: Number of shares purchased.
            price_usd: Per-share price in USD.
            fx_rate: ECB EUR/USD rate on the purchase date.
        """
        if qty <= 0:
            return

        cost_eur_per_unit = (price_usd / fx_rate).quantize(Decimal("0.0001"))

        lot = FIFOLot(
            symbol=symbol,
            buy_date=date,
            qty_remaining=qty,
            price_usd=price_usd,
            fx_rate=fx_rate,
            cost_eur_per_unit=cost_eur_per_unit,
        )
        self.open_positions[symbol].append(lot)

    def process_sell(
        self,
        symbol: str,
        date: str,
        qty: Decimal,
        price_usd: Decimal,
        fx_rate: Decimal,
    ) -> list[RealizedTrade]:
        """
        Record a sale. Consumes the oldest lots (FIFO) and computes
        realized gains/losses in EUR with Teilfreistellung applied.

        Args:
            symbol: Ticker symbol.
            date: Sale date "YYYY-MM-DD".
            qty: Number of shares sold.
            price_usd: Per-share sale price in USD.
            fx_rate: ECB EUR/USD rate on the sale date.

        Returns:
            List of RealizedTrade entries (one per consumed lot).
        """
        if qty <= 0:
            return []

        from tax.config import get_isin

        lots = self.open_positions.get(symbol, [])
        if not lots:
            print(f"  ⚠ FIFO WARNING: Selling {qty} {symbol} on {date} but no open lots found!")
            return []

        remaining_to_sell = qty
        trades = []
        tfs_rate = get_tfs_rate(symbol)
        proceeds_eur_per_unit = (price_usd / fx_rate).quantize(Decimal("0.0001"))

        while remaining_to_sell > 0 and lots:
            lot = lots[0]

            # Determine how many shares to take from this lot
            if lot.qty_remaining <= remaining_to_sell:
                # Consume entire lot
                sold_qty = lot.qty_remaining
                remaining_to_sell -= sold_qty
                lots.pop(0)  # Remove fully consumed lot
            else:
                # Partially consume lot
                sold_qty = remaining_to_sell
                lot.qty_remaining -= sold_qty
                remaining_to_sell = Decimal("0")

            # Calculate EUR amounts
            cost_eur = (sold_qty * lot.cost_eur_per_unit).quantize(Decimal("0.01"))
            proceeds_eur = (sold_qty * proceeds_eur_per_unit).quantize(Decimal("0.01"))
            gain_loss_eur = proceeds_eur - cost_eur

            # Apply Teilfreistellung to the gain/loss
            # TFS reduces the taxable portion: taxable = gain * (1 - tfs_rate)
            taxable_gain_eur = (gain_loss_eur * (1 - tfs_rate)).quantize(Decimal("0.01"))

            trade = RealizedTrade(
                symbol=symbol,
                isin=get_isin(symbol),
                buy_date=lot.buy_date,
                sell_date=date,
                qty=sold_qty,
                cost_eur=cost_eur,
                proceeds_eur=proceeds_eur,
                gain_loss_eur=gain_loss_eur,
                tfs_rate=tfs_rate,
                taxable_gain_eur=taxable_gain_eur,
                buy_fx_rate=lot.fx_rate,
                sell_fx_rate=fx_rate,
                buy_price_usd=lot.price_usd,
                sell_price_usd=price_usd,
            )
            trades.append(trade)
            self.realized_trades.append(trade)

        # Floating-point tolerance: ignore sub-penny residuals from fractional shares
        if remaining_to_sell > Decimal("0.001"):
            print(
                f"  ⚠ FIFO WARNING: Could not fully match sell of {qty} {symbol} on {date}. "
                f"Unmatched: {remaining_to_sell} shares."
            )

        return trades

    def get_open_lots_as_dicts(self) -> list[dict]:
        """
        Export all open (unsold) positions as a list of flat dicts.
        Used to write the Open_Positions_FIFO tab in Google Sheets.

        Returns:
            List of dicts with lot details.
        """
        from tax.config import get_isin as _get_isin

        result = []
        for symbol, lots in sorted(self.open_positions.items()):
            for lot in lots:
                if lot.qty_remaining > 0:
                    result.append({
                        "symbol": symbol,
                        "isin": _get_isin(symbol),
                        "buy_date": lot.buy_date,
                        "qty_remaining": str(lot.qty_remaining),
                        "buy_price_usd": str(lot.price_usd),
                        "ecb_rate": str(lot.fx_rate),
                        "cost_eur_per_unit": str(lot.cost_eur_per_unit),
                        "total_cost_eur": str(lot.total_cost_eur),
                    })
        return result

    def get_realized_trades_as_dicts(self) -> list[dict]:
        """
        Export all realized trades as a list of flat dicts.
        Useful for debugging and detailed trade-level reporting.

        Returns:
            List of dicts with realized trade details.
        """
        return [
            {
                "symbol": t.symbol,
                "isin": t.isin,
                "buy_date": t.buy_date,
                "sell_date": t.sell_date,
                "qty": str(t.qty),
                "buy_price_usd": str(t.buy_price_usd),
                "sell_price_usd": str(t.sell_price_usd),
                "buy_fx_rate": str(t.buy_fx_rate),
                "sell_fx_rate": str(t.sell_fx_rate),
                "cost_eur": str(t.cost_eur),
                "proceeds_eur": str(t.proceeds_eur),
                "gain_loss_eur": str(t.gain_loss_eur),
                "tfs_rate": str(t.tfs_rate),
                "taxable_gain_eur": str(t.taxable_gain_eur),
            }
            for t in self.realized_trades
        ]
