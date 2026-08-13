#!/usr/bin/env bash
# Locust web UI + N local workers on the same EC2. Removes the single-process
# CPU ceiling that "CPU usage above 90%" warnings signal.
#
# Usage:
#   scripts/run_web_distributed.sh                # N = $(nproc), local UI
#   WORKERS=4 scripts/run_web_distributed.sh      # override worker count
#   WEB_MODE=lan scripts/run_web_distributed.sh   # bind 0.0.0.0 instead of 127.0.0.1
#
# Ctrl-C on the master terminates workers via the trap below.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo ".env missing — copy .env.example and fill it in" >&2; exit 1; }
set -a; source .env; set +a

LOCUST=".venv/bin/locust"
[ -x "$LOCUST" ] || LOCUST=locust
WORKERS="${WORKERS:-$(nproc)}"
PORT="${WEB_PORT:-8089}"
CSV_PREFIX="${CSV_PREFIX:-results}"
BIND_HOST="127.0.0.1"
[ "${WEB_MODE:-local}" = "lan" ] && BIND_HOST="0.0.0.0"

soft_limit=$(ulimit -n)
if [ "$soft_limit" != "unlimited" ] && [ "$soft_limit" -lt 8192 ]; then
  echo "WARNING: ulimit -n is $soft_limit; raise it (scripts/ec2_setup.sh) or connections will fail" >&2
fi

# Clean up any workers from a previous session before spawning new ones.
pkill -f 'locust -f locustfile.py --worker' 2>/dev/null || true
sleep 0.5

WORKER_PIDS=()
cleanup() {
  echo
  echo "== stopping $WORKERS workers"
  for pid in "${WORKER_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "== spawning $WORKERS Locust workers (logs -> worker_<n>.log)"
for i in $(seq 1 "$WORKERS"); do
  "$LOCUST" -f locustfile.py --worker --master-host 127.0.0.1 \
    > "worker_${i}.log" 2>&1 &
  WORKER_PIDS+=($!)
done

echo "== starting master with web UI on ${BIND_HOST}:${PORT}"
echo "   waiting for $WORKERS workers to connect..."
exec "$LOCUST" -f locustfile.py \
  --master --expect-workers "$WORKERS" \
  --web-host "$BIND_HOST" --web-port "$PORT" \
  --csv "$CSV_PREFIX" --csv-full-history \
  --html "${CSV_PREFIX}_report.html"
