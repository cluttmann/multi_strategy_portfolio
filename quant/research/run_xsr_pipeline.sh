#!/bin/zsh
# XSR end-to-end: features → walk-forward training → cost-stressed simulation.
# Run after the EOD backfill completes. Total runtime ~1-2h (training bound).
set -e
cd "$(dirname "$0")/../.."
export PYTHONUNBUFFERED=1

echo "=== 1/4 data quality audit ==="
python3 -m quant.data.quality --audit

echo "=== 2/4 features_daily build (BigQuery) ==="
python3 -m quant.features.daily_features --build

echo "=== 3/4 walk-forward training ==="
python3 -m quant.models.train_ranker --walk-forward --start-year 2003

echo "=== 4/4 portfolio simulation ==="
python3 -m quant.backtest.portfolio_sim --run-tag wf_v1
echo "=== XSR pipeline complete ==="
