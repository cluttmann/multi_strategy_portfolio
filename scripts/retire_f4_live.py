"""Safely retire the former F4 sleeve from Alpaca and reallocate its proceeds."""

from copy import deepcopy


F4_SYMBOLS = ("WLDU", "GOLY", "TLT")


def extract_f4_positions(positions):
    positions_by_symbol = {position.get("symbol"): position for position in positions}
    sale_plan = []

    for symbol in F4_SYMBOLS:
        position = positions_by_symbol.get(symbol)
        if not position:
            continue

        quantity = float(position.get("qty_available", position.get("qty", 0)) or 0)
        if quantity <= 0:
            continue

        total_quantity = float(position.get("qty", quantity) or quantity)
        if position.get("current_price") is not None:
            price = float(position["current_price"])
        elif total_quantity > 0:
            price = abs(float(position.get("market_value", 0) or 0)) / total_quantity
        else:
            price = 0.0

        sale_plan.append(
            {
                "symbol": symbol,
                "qty": quantity,
                "estimated_price": price,
                "estimated_proceeds": quantity * price,
            }
        )

    return sale_plan


def project_post_sale_margin(margin_result, estimated_proceeds):
    projected = deepcopy(margin_result)
    metrics = projected.setdefault("metrics", {})
    metrics["cash"] = float(metrics.get("cash", 0)) + max(0.0, float(estimated_proceeds))
    return projected


def cap_investment_calculation(calculation, proceeds):
    capped = deepcopy(calculation)
    original_total = max(0.0, float(calculation.get("total_investing", 0) or 0))
    budget = min(original_total, max(0.0, float(proceeds)))
    scale = budget / original_total if original_total > 0 else 0.0

    capped["total_investing"] = budget
    capped["strategy_amounts"] = {
        key: float(amount) * scale
        for key, amount in calculation.get("strategy_amounts", {}).items()
    }

    available_cash = min(
        max(0.0, float(calculation.get("total_available", 0) or 0)),
        budget,
    )
    capped["total_available"] = available_cash
    capped["margin_approved"] = max(0.0, budget - available_cash)
    return capped
