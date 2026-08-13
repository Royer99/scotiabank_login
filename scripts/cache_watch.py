"""Live WiredTiger cache-hit-ratio tail for a MongoDB Atlas cluster.

Samples ``serverStatus().wiredTiger.cache`` every *interval* seconds and
prints the hit ratio for the just-elapsed window, plus a small sparkline of
recent history so a warming cluster is visible at a glance.

Complements ``scripts/watch_progress.sh``: that one shows client-observed
latency; this one shows the server-side cache health that explains it. Run
both in adjacent terminals during a benchmark and screenshot together for
the customer report.

Standalone usage:
    python scripts/cache_watch.py              # 5s window, forever
    python scripts/cache_watch.py --interval 2 # 2s window
    python scripts/cache_watch.py --once       # single sample, then exit
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import load_config  # noqa: E402

MB = 1024 * 1024
GB = 1024 * MB
SPARK_CHARS = "▁▂▃▄▅▆▇█"
HISTORY_LEN = 40


def _client(uri: str) -> Any:
    from pymongo import MongoClient
    return MongoClient(uri, maxPoolSize=2, serverSelectionTimeoutMS=5000)


def _sample(db: Any) -> dict[str, int]:
    """Read the two WT counters + a couple of context metrics.

    Counters are monotonic since server start — only the delta between two
    samples is meaningful.
    """
    wt = db.command("serverStatus")["wiredTiger"]["cache"]
    return {
        "requested": wt["pages requested from the cache"],
        "read_in":   wt["pages read into cache"],
        "bytes_in":  wt.get("bytes read into cache", 0),
        "evicted":   wt.get("pages evicted from the cache",
                            wt.get("pages evicted by application threads", 0)),
        "in_cache":  wt.get("bytes currently in the cache", 0),
        "max":       wt.get("maximum bytes configured", 0),
    }


def _spark(values: list[float], lo: float = 0.0, hi: float = 100.0) -> str:
    """Render *values* as a Unicode sparkline scaled to [lo, hi]."""
    if not values:
        return ""
    span = hi - lo
    out = []
    for v in values:
        idx = int(max(0.0, min(1.0, (v - lo) / span)) * (len(SPARK_CHARS) - 1))
        out.append(SPARK_CHARS[idx])
    return "".join(out)


def _fmt_bytes(n: float) -> str:
    if n >= GB:
        return f"{n / GB:6.2f} GB"
    return f"{n / MB:6.1f} MB"


def print_snapshot(prev: dict[str, int], curr: dict[str, int],
                   elapsed: float, history: deque) -> None:
    """One line: window hit ratio, page rate, disk-read rate, cache fullness,
    plus a sparkline of the last HISTORY_LEN samples."""
    d_req = curr["requested"] - prev["requested"]
    d_miss = curr["read_in"] - prev["read_in"]
    d_bytes = curr["bytes_in"] - prev["bytes_in"]

    hit = 100.0 * (1.0 - d_miss / d_req) if d_req > 0 else float("nan")
    history.append(hit if hit == hit else 0.0)  # NaN check

    req_rps = d_req / elapsed if elapsed > 0 else 0.0
    miss_rps = d_miss / elapsed if elapsed > 0 else 0.0
    disk_bps = d_bytes / elapsed if elapsed > 0 else 0.0
    fill = 100.0 * curr["in_cache"] / curr["max"] if curr["max"] > 0 else 0.0

    ts = datetime.now(tz=timezone.utc).strftime("%H:%M:%SZ")
    spark = _spark(list(history), lo=80.0, hi=100.0) if len(history) > 1 else ""

    hit_str = f"{hit:6.2f}%" if hit == hit else "  n/a "
    verdict = ""
    if hit == hit:
        if hit >= 99.0:
            verdict = "warm"
        elif hit >= 95.0:
            verdict = "warming"
        else:
            verdict = "cold"

    print(f"{ts}  hit={hit_str}  {spark}  "
          f"req/s={req_rps:>7,.0f}  miss/s={miss_rps:>6,.0f}  "
          f"disk={_fmt_bytes(disk_bps)}/s  fill={fill:5.1f}%  {verdict}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--interval", type=float, default=5.0,
                   help="seconds between samples (default 5)")
    p.add_argument("--once", action="store_true",
                   help="take one sample after --interval seconds, then exit")
    args = p.parse_args()
    cfg = load_config(quiet=True)

    db = _client(cfg.mongodb_uri)[cfg.mongodb_db]
    try:
        prev = _sample(db)
    except Exception as e:
        raise SystemExit(
            f"could not read wiredTiger cache stats: {e}\n"
            "(Atlas Free/Shared clusters and some restricted users cannot see "
            "serverStatus. Use a project user with at least readWriteAnyDatabase "
            "or the built-in monitoring role.)"
        )

    print(f"watching WiredTiger cache on {cfg.mongodb_db} — Ctrl-C to stop")
    print(f"cache size: {_fmt_bytes(prev['max'])}   "
          f"interval: {args.interval:g}s   sparkline scale: 80–100% hit\n")

    history: deque = deque(maxlen=HISTORY_LEN)
    t_prev = time.monotonic()
    try:
        while True:
            time.sleep(args.interval)
            t_curr = time.monotonic()
            curr = _sample(db)
            print_snapshot(prev, curr, t_curr - t_prev, history)
            prev, t_prev = curr, t_curr
            if args.once:
                return
    except KeyboardInterrupt:
        print("\nstopped watching.")


if __name__ == "__main__":
    main()
