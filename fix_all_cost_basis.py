#!/usr/bin/env python3
"""
Fix cost basis for ALL strategies by recalculating from Alpaca.

This script pulls the actual cost_basis from Alpaca positions and updates
the total_invested field in Firestore to match reality for all strategies.

Usage:
    python fix_all_cost_basis.py
"""

import sys
sys.path.insert(0, '.')

from main import (
    set_alpaca_environment,
    recalculate_all_strategies_cost_basis,
    list_positions,
    alpaca_environment
)

def main():
    print("=" * 80)
    print("ALL STRATEGIES - Cost Basis Recalculation Tool")
    print("=" * 80)
    print()
    print("NOTE: This script now uses the consolidated function from main.py")
    print("The same function is automatically called at the start of monthly investments.")
    print()
    
    # Set up Alpaca API
    print(f"Connecting to Alpaca ({alpaca_environment} environment)...")
    api = set_alpaca_environment(env=alpaca_environment)
    print("Connected!")
    print()
    
    # Get all positions from Alpaca (for display purposes)
    print("Fetching all positions from Alpaca...")
    positions = list_positions(api)
    print(f"Found {len(positions)} total positions")
    print()
    
    # Show all positions
    print("=" * 80)
    print("ALL POSITIONS IN ALPACA:")
    print("=" * 80)
    total_account_cost_basis = 0
    total_account_market_value = 0
    
    for position in positions:
        symbol = position.get("symbol")
        qty = float(position.get("qty", 0))
        cost_basis = float(position.get("cost_basis", 0))
        market_value = float(position.get("market_value", 0))
        avg_entry = float(position.get("avg_entry_price", 0))
        current_price = float(position.get("current_price", 0))
        unrealized_pl = float(position.get("unrealized_pl", 0))
        unrealized_plpc = float(position.get("unrealized_plpc", 0)) * 100
        
        total_account_cost_basis += cost_basis
        total_account_market_value += market_value
        
        print(f"{symbol:8} | Shares: {qty:10.4f} | Cost: ${cost_basis:10.2f} | "
              f"Value: ${market_value:10.2f} | P/L: ${unrealized_pl:8.2f} ({unrealized_plpc:+6.2f}%)")
    
    print("-" * 80)
    print(f"{'TOTALS':8} | {'':10} | Cost: ${total_account_cost_basis:10.2f} | "
          f"Value: ${total_account_market_value:10.2f} | "
          f"P/L: ${total_account_market_value - total_account_cost_basis:8.2f} "
          f"({((total_account_market_value / total_account_cost_basis - 1) * 100) if total_account_cost_basis > 0 else 0:+6.2f}%)")
    print()
    
    # Use the consolidated function from main.py
    result = recalculate_all_strategies_cost_basis(api, env=alpaca_environment, silent=False)
    
    if result.get("success"):
        print()
        print("=" * 80)
        print("✅ ALL STRATEGIES UPDATED!")
        print("=" * 80)
        return 0
    else:
        print()
        print("=" * 80)
        print(f"❌ ERROR: {result.get('error', 'Unknown error')}")
        print("=" * 80)
        return 1

if __name__ == "__main__":
    sys.exit(main())
