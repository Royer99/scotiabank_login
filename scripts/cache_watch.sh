#!/usr/bin/env bash
# Live WiredTiger cache-hit-ratio tail. Wraps scripts/cache_watch.py so it
# picks up .venv automatically. Run in a THIRD terminal alongside Locust
# (terminal 1) and scripts/watch_progress.sh (terminal 2) — you'll see the
# server-side cache health and the client-observed latency at the same time.
#
#   scripts/cache_watch.sh            # 5s window
#   scripts/cache_watch.sh 2          # 2s window
set -euo pipefail
cd "$(dirname "$0")/.."

INTERVAL="${1:-5}"
PY=".venv/bin/python"
[ -x "$PY" ] || PY=python3
exec "$PY" scripts/cache_watch.py --interval "$INTERVAL"
