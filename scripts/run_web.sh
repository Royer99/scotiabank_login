#!/usr/bin/env bash
# Locust with the WEB UI (real-time charts in a browser) instead of headless.
# Same warm-up + measured flow is up to you to trigger from the UI: click
# "Start" once for a WARMUP_TIME warm-up, discard, click again for the real run.
#
# Exposure modes (default: local only; safest — pair with an SSH tunnel):
#   WEB_MODE=local     bind 127.0.0.1:8089    # default
#   WEB_MODE=lan       bind 0.0.0.0:8089 (no auth — use only on a private VPC)
#   WEB_MODE=authed    bind 0.0.0.0:8089 with --web-auth $WEB_USER:$WEB_PASS
#
# From your laptop:
#   ssh -L 8089:localhost:8089 ec2-user@<ip>     # tunnel
#   scripts/run_web.sh                            # on the EC2, in another shell
#   open http://localhost:8089                    # locally
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo ".env missing — copy .env.example and fill it in" >&2; exit 1; }
set -a; source .env; set +a

LOCUST=".venv/bin/locust"
[ -x "$LOCUST" ] || LOCUST=locust
MODE="${WEB_MODE:-local}"
PORT="${WEB_PORT:-8089}"
CSV_PREFIX="${CSV_PREFIX:-results}"

soft_limit=$(ulimit -n)
if [ "$soft_limit" != "unlimited" ] && [ "$soft_limit" -lt 8192 ]; then
  echo "WARNING: ulimit -n is $soft_limit; raise it (scripts/ec2_setup.sh) or connections will fail" >&2
fi

case "$MODE" in
  local)
    BIND=(--web-host 127.0.0.1 --web-port "$PORT")
    echo "== Locust web UI on 127.0.0.1:$PORT — reach it via SSH tunnel:"
    echo "     ssh -L $PORT:localhost:$PORT <user>@<ec2-ip>"
    ;;
  lan)
    BIND=(--web-host 0.0.0.0 --web-port "$PORT")
    echo "== Locust web UI on 0.0.0.0:$PORT (NO AUTH) — restrict the SG to trusted CIDRs" >&2
    ;;
  authed)
    : "${WEB_USER:?set WEB_USER}"; : "${WEB_PASS:?set WEB_PASS (strong)}"
    BIND=(--web-host 0.0.0.0 --web-port "$PORT" --web-auth "$WEB_USER:$WEB_PASS")
    echo "== Locust web UI on 0.0.0.0:$PORT with basic auth (user: $WEB_USER)"
    ;;
  *) echo "WEB_MODE must be local|lan|authed" >&2; exit 1 ;;
esac

exec "$LOCUST" -f locustfile.py "${BIND[@]}" \
  --csv "$CSV_PREFIX" --csv-full-history \
  --html "${CSV_PREFIX}_report.html"
