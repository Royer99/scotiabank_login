#!/usr/bin/env bash
# Live percentile table for a Locust run in progress. Reads the
# --csv-full-history file that scripts/run_headless.sh writes; keeps ticking
# every INTERVAL seconds; exits on Ctrl-C. Run in a SECOND terminal while the
# Locust run is executing in the first.
#
#   scripts/watch_progress.sh             # default results_stats_history.csv, 2s
#   scripts/watch_progress.sh myrun 1     # custom prefix + 1s interval
set -euo pipefail
cd "$(dirname "$0")/.."

CSV_PREFIX="${1:-${CSV_PREFIX:-results}}"
INTERVAL="${2:-2}"

PY=".venv/bin/python"
[ -x "$PY" ] || PY=python3
exec "$PY" scripts/report.py --csv-prefix "$CSV_PREFIX" --watch --interval "$INTERVAL"
