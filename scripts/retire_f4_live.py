"""Safely retire the former F4 sleeve from Alpaca and reallocate its proceeds."""

import argparse
import datetime
import json
import sys
import time
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as bot


F4_SYMBOLS = ("WLDU", "GOLY", "TLT")
CONFIRMATION_TOKEN = "RETIRE_F4_LIVE"
TERMINAL_FAILURE_STATUSES = {"canceled", "expired", "rejected", "replaced", "stopped"}
MATERIAL_POSITION_EPSILON = 0.00001


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


def get_market_clock(api):
    response = bot.alpaca_request_with_retry(
        "GET",
        f"{api['BASE_URL']}/v2/clock",
        headers=bot.get_auth_headers(api),
        label="market clock",
        raise_on_fail=True,
    )
    return response.json()


def capture_snapshot(api):
    account = bot.get_account_info(api)
    if account is None:
        raise RuntimeError("Could not read Alpaca account")
    return {
        "account": account,
        "positions": bot.list_positions(api),
        "pending_orders": bot.get_pending_orders(api),
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def write_audit(audit_path, audit):
    path = Path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(audit, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def strict_wait_for_fill(api, order_id, timeout=300, poll_interval=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        order = bot.get_order(api, order_id)
        status = order.get("status")
        if status == "filled":
            return order
        if status in TERMINAL_FAILURE_STATUSES:
            raise RuntimeError(f"Order {order_id} ended with status {status}")
        time.sleep(poll_interval)
    raise TimeoutError(f"Order {order_id} did not fill within {timeout} seconds")


def remaining_f4_positions(positions):
    return [
        {
            "symbol": position.get("symbol"),
            "qty": float(position.get("qty", 0) or 0),
        }
        for position in positions
        if position.get("symbol") in F4_SYMBOLS
        and abs(float(position.get("qty", 0) or 0)) > MATERIAL_POSITION_EPSILON
    ]


def mark_f4_retired(env, sale_fills, actual_proceeds):
    document = (
        bot.get_firestore_client()
        .collection(f"strategy-balances-{env}")
        .document("f4")
    )
    snapshot = document.get()
    before = snapshot.to_dict() if snapshot.exists else None
    retired_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    retirement_record = {
        "retired": True,
        "retired_at": retired_at,
        "retirement_proceeds": actual_proceeds,
        "retirement_orders": sale_fills,
        "current_positions": {symbol: 0 for symbol in F4_SYMBOLS},
        "current_values": {symbol: 0 for symbol in F4_SYMBOLS},
    }
    document.set(retirement_record, merge=True)
    return {"before": before, "after": retirement_record}


def _strategy_runners(api, investment_calc, margin_result, env):
    return [
        (
            "hfea",
            lambda: bot.make_monthly_buys(
                api, True, investment_calc, margin_result, False, env
            ),
        ),
        (
            "spxl",
            lambda: bot.monthly_buying_sma(
                api, "SPXL", True, investment_calc, margin_result, False, env
            ),
        ),
        (
            "nine_sig",
            lambda: bot.make_monthly_nine_sig_contributions(
                api, True, investment_calc, margin_result, False, env
            ),
        ),
        (
            "dual_momentum",
            lambda: bot.monthly_dual_momentum_strategy(
                api, True, investment_calc, margin_result, False, env
            ),
        ),
        (
            "regime_sso",
            lambda: bot.make_monthly_buys_regime(
                api,
                cfg=bot.regime_sso_config,
                force_execute=True,
                investment_calc=investment_calc,
                margin_result=margin_result,
                skip_order_wait=False,
                env=env,
            ),
        ),
        (
            "aaa",
            lambda: bot.make_monthly_buys_aaa(
                api,
                force_execute=True,
                investment_calc=investment_calc,
                margin_result=margin_result,
                skip_order_wait=False,
                env=env,
            ),
        ),
    ]


def _result_failed(result):
    normalized = str(result).strip().lower()
    return normalized.startswith(("failed", "error", "❌")) or "failed to" in normalized


def execute_strategy_buys(api, investment_calc, margin_result, env, audit, audit_path):
    results = []
    for strategy, run_strategy in _strategy_runners(
        api, investment_calc, margin_result, env
    ):
        result = run_strategy()
        snapshot = capture_snapshot(api)
        audit["steps"].append(
            {
                "stage": f"after_{strategy}",
                "result": str(result),
                "snapshot": snapshot,
            }
        )
        write_audit(audit_path, audit)
        if snapshot["pending_orders"]:
            raise RuntimeError(f"Open order remains after {strategy}")
        if _result_failed(result):
            raise RuntimeError(f"{strategy} failed: {result}")
        results.append({"strategy": strategy, "result": str(result)})
    return results


def build_dry_run(api, env="live"):
    clock = get_market_clock(api)
    snapshot = capture_snapshot(api)
    sale_plan = extract_f4_positions(snapshot["positions"])
    estimated_proceeds = sum(item["estimated_proceeds"] for item in sale_plan)

    result = {
        "status": "ready" if sale_plan else "already_retired",
        "market_is_open": bool(clock.get("is_open")),
        "pending_orders": snapshot["pending_orders"],
        "sell_orders": sale_plan,
        "estimated_gross_proceeds": estimated_proceeds,
        "account": snapshot["account"],
    }
    if snapshot["pending_orders"] or not sale_plan:
        return result

    margin_result = bot.check_margin_conditions(api, env=env)
    projected_margin = project_post_sale_margin(margin_result, estimated_proceeds)
    raw_calculation = bot.calculate_monthly_investments(api, projected_margin, env)
    result["margin_result"] = margin_result
    result["projected_cash_after_sales"] = projected_margin["metrics"].get("cash")
    result["reallocation"] = cap_investment_calculation(
        raw_calculation, estimated_proceeds
    )
    return result


def execute_retirement(api, env="live", confirmation=None, audit_path=None):
    if confirmation != CONFIRMATION_TOKEN:
        raise ValueError(f"Execution requires confirmation token {CONFIRMATION_TOKEN}")

    clock = get_market_clock(api)
    if not clock.get("is_open"):
        raise RuntimeError("US market is closed")

    before_snapshot = capture_snapshot(api)
    if before_snapshot["pending_orders"]:
        raise RuntimeError("Cannot retire F4 while an open order exists")

    sale_plan = extract_f4_positions(before_snapshot["positions"])
    if not sale_plan:
        return {"status": "already_retired", "sell_orders": []}

    resolved_audit_path = Path(audit_path) if audit_path else Path(
        "/tmp"
    ) / f"f4-retirement-{datetime.datetime.now(datetime.timezone.utc):%Y%m%dT%H%M%SZ}.json"
    audit = {
        "status": "executing",
        "env": env,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sale_plan": sale_plan,
        "steps": [{"stage": "before_orders", "snapshot": before_snapshot}],
    }
    write_audit(resolved_audit_path, audit)

    sale_fills = []
    actual_proceeds = 0.0
    try:
        for planned_sale in sale_plan:
            quantity = round(planned_sale["qty"], 6)
            if quantity <= 0:
                continue
            order = bot.submit_order(api, planned_sale["symbol"], quantity, "sell")
            filled_order = strict_wait_for_fill(api, order["id"])
            filled_quantity = float(filled_order.get("filled_qty", 0) or 0)
            filled_price = float(filled_order.get("filled_avg_price", 0) or 0)
            proceeds = filled_quantity * filled_price
            fill_record = {
                "symbol": planned_sale["symbol"],
                "order_id": filled_order.get("id", order["id"]),
                "filled_qty": filled_quantity,
                "filled_avg_price": filled_price,
                "proceeds": proceeds,
            }
            sale_fills.append(fill_record)
            actual_proceeds += proceeds
            audit["steps"].append(
                {
                    "stage": f"after_sell_{planned_sale['symbol']}",
                    "fill": fill_record,
                    "snapshot": capture_snapshot(api),
                }
            )
            write_audit(resolved_audit_path, audit)

        post_sale_snapshot = audit["steps"][-1]["snapshot"]
        residual_positions = remaining_f4_positions(post_sale_snapshot["positions"])
        if residual_positions:
            raise RuntimeError(f"F4 position remains after sells: {residual_positions}")
        if post_sale_snapshot["pending_orders"]:
            raise RuntimeError("Open order remains after F4 sells")

        audit["firestore_retirement"] = mark_f4_retired(
            env, sale_fills, actual_proceeds
        )
        write_audit(resolved_audit_path, audit)

        margin_result = bot.check_margin_conditions(api, env=env)
        raw_calculation = bot.calculate_monthly_investments(api, margin_result, env)
        investment_calc = cap_investment_calculation(
            raw_calculation, actual_proceeds
        )
        audit["post_sale_margin_result"] = margin_result
        audit["investment_calculation"] = investment_calc
        write_audit(resolved_audit_path, audit)

        if investment_calc["total_investing"] < bot.margin_control_config["min_investment"]:
            bot.recalculate_all_strategies_cost_basis(api, env, silent=True)
            final_snapshot = capture_snapshot(api)
            audit["status"] = "sold_without_reallocation"
            audit["completed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            audit["final_snapshot"] = final_snapshot
            write_audit(resolved_audit_path, audit)
            bot.send_telegram_message(
                f"🧹 F4 retired — sold ${actual_proceeds:,.2f}; no permitted buy budget, proceeds reduce margin."
            )
            return {
                "status": "sold_without_reallocation",
                "actual_proceeds": actual_proceeds,
                "strategy_results": [],
                "audit_path": str(resolved_audit_path),
            }

        if not get_market_clock(api).get("is_open"):
            raise RuntimeError("US market closed after sells; no reallocation buys submitted")

        strategy_results = execute_strategy_buys(
            api,
            investment_calc,
            margin_result,
            env,
            audit,
            resolved_audit_path,
        )
        reconciliation = bot.recalculate_all_strategies_cost_basis(
            api, env, silent=True
        )
        final_snapshot = capture_snapshot(api)
        audit["status"] = "completed"
        audit["completed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        audit["strategy_results"] = strategy_results
        audit["cost_basis_reconciliation"] = reconciliation
        audit["final_snapshot"] = final_snapshot
        write_audit(resolved_audit_path, audit)
        bot.send_telegram_message(
            f"✅ F4 retired — sold ${actual_proceeds:,.2f}; reallocated ${investment_calc['total_investing']:,.2f} across six strategies."
        )
        return {
            "status": "completed",
            "actual_proceeds": actual_proceeds,
            "reallocated": investment_calc["total_investing"],
            "strategy_results": strategy_results,
            "audit_path": str(resolved_audit_path),
        }
    except Exception as error:
        audit["status"] = "failed"
        audit["failed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        audit["error"] = str(error)
        write_audit(resolved_audit_path, audit)
        try:
            bot.send_telegram_message(f"❌ F4 retirement stopped: {error}")
        except Exception:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Dry-run or execute the one-time F4 live retirement"
    )
    parser.add_argument("--env", choices=["paper", "live"], default="paper")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--audit-path")
    arguments = parser.parse_args()

    api = bot.set_alpaca_environment(arguments.env, use_secret_manager=False)
    if arguments.dry_run:
        result = build_dry_run(api, env=arguments.env)
    else:
        result = execute_retirement(
            api,
            env=arguments.env,
            confirmation=arguments.confirm,
            audit_path=arguments.audit_path,
        )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
