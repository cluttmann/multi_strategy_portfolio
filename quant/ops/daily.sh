#!/bin/zsh
# Quant daily operations — run every trading evening after EODHD finality
# (e.g. 03:00 Berlin) via launchd/cron, or manually.
#
#   zsh quant/ops/daily.sh
#
# Order matters: data → features → scores are consumed by the pre-market
# executors the next morning.
set -e
cd "$(dirname "$0")/../.."
export PYTHONUNBUFFERED=1

echo "=== $(date) quant daily ops ==="

echo "--- 1/5 EOD incremental (EODHD bulk, 100 quota units) ---"
python3 -m quant.data.update --eod || echo "WARN: eod update failed"

echo "--- 2/5 news + minute incremental ---"
python3 -m quant.data.update --news --minute || echo "WARN: news/minute failed"

echo "--- 3/5 options chain snapshot (proprietary archive) ---"
python3 -m quant.data.options_archiver --snap || echo "WARN: options snap failed"

echo "--- 4/5 BOATS overnight bars top-up ---"
python3 -m quant.data.boats_ingest --backfill || echo "WARN: boats failed"

echo "--- 5/5 rebuild feature panels ---"
python3 -m quant.features.daily_features --build
python3 -m quant.features.xsr_v2_features --build

echo "=== daily ops complete $(date) ==="

echo "--- 6/6 polymarket macro odds + wallet fills ---"
python3 -m quant.data.polymarket_ingest --update --trades || echo "WARN: polymarket failed"

echo "--- 7/7 public data: borrow snapshot + FRED refresh + FINRA increment ---"
python3 -m quant.data.public_ingest --borrow-snap --finra || echo "WARN: public data failed"
