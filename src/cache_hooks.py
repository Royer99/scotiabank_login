"""Locust event hooks that sample the WiredTiger cache during the test window.

Imported once by ``locustfile.py``. On ``test_start`` (i.e. when you click
Start Swarming in the UI, or when ``--headless`` begins), a gevent greenlet
starts sampling ``serverStatus().wiredTiger.cache`` every
``WT_CACHE_WATCH_INTERVAL`` seconds. On ``test_stop`` it prints a summary and
closes the CSV. What you get:

1. **Locust terminal** — one line per sample: hit %, req/s, miss/s, disk MB/s,
   fill %. Same shape as ``scripts/cache_watch.py`` output.
2. **CSV** — ``<CSV_PREFIX>_cache_history.csv``, one row per sample. Feeds
   ``scripts/report.py`` for a hit-ratio-over-time chart at the end.
3. **Locust UI Statistics tab** — a synthetic ``wt_cache/hit_pct_x100`` row.
   The "Median / 95% / 99%" columns then show the hit-ratio percentiles
   over the run (as basis points — divide by 100 for a %). The customer
   sees cache health in the same table as the app latencies.

Disable with ``WT_CACHE_WATCH_ENABLED=false`` if the Atlas user cannot run
``serverStatus`` (Free/Shared tiers, restricted roles).
"""

from __future__ import annotations

import csv
import os
import time
from pathlib import Path
from typing import Any

import gevent
from locust import events

from config import load_config

_CFG = load_config()

_ENABLED = os.environ.get("WT_CACHE_WATCH_ENABLED", "true").strip().lower() in ("1", "true", "yes")
_INTERVAL = float(os.environ.get("WT_CACHE_WATCH_INTERVAL", "5"))
_CSV_PREFIX = os.environ.get("CSV_PREFIX", "results")
_CSV_PATH = Path(f"{_CSV_PREFIX}_cache_history.csv")
_UI_ROW = os.environ.get("WT_CACHE_UI_ROW", "true").strip().lower() in ("1", "true", "yes")


def _sample_counters(db: Any) -> dict[str, int]:
    """Read the two WT counters plus context. Monotonic since server start."""
    wt = db.command("serverStatus")["wiredTiger"]["cache"]
    return {
        "requested": wt["pages requested from the cache"],
        "read_in":   wt["pages read into cache"],
        "bytes_in":  wt.get("bytes read into cache", 0),
        "in_cache":  wt.get("bytes currently in the cache", 0),
        "max":       wt.get("maximum bytes configured", 0),
    }


class _CacheSampler:
    """Owns the greenlet, the Mongo client, the CSV file, and a running
    min/max/mean of the hit ratio (used for the on-stop summary)."""

    def __init__(self) -> None:
        from pymongo import MongoClient
        self._client = MongoClient(_CFG.mongodb_uri, maxPoolSize=2,
                                   serverSelectionTimeoutMS=5000,
                                   appname="cache-hooks")
        self._db = self._client[_CFG.mongodb_db]
        self._greenlet: Any = None
        self._csv_file: Any = None
        self._writer: Any = None
        self.samples = 0
        self.hit_sum = 0.0
        self.hit_min = 100.0
        self.hit_max = 0.0

    def start(self) -> None:
        # Fail loud but not fatal: if the user can't read serverStatus, the
        # Locust run still proceeds — just without cache visibility.
        try:
            self._prev = _sample_counters(self._db)
        except Exception as e:
            print(f"[cache_hooks] disabled: serverStatus unavailable ({e}). "
                  "Grant the Atlas user the `clusterMonitor` role to enable this.")
            return
        self._t_prev = time.monotonic()
        self._csv_file = open(_CSV_PATH, "w", buffering=1)  # line-buffered
        self._writer = csv.writer(self._csv_file)
        self._writer.writerow(["timestamp_utc", "hit_pct", "req_per_s",
                               "miss_per_s", "disk_mb_per_s", "fill_pct"])
        self._greenlet = gevent.spawn(self._loop)
        print(f"[cache_hooks] sampling WiredTiger cache every {_INTERVAL:g}s "
              f"-> {_CSV_PATH} (UI row: {'on' if _UI_ROW else 'off'})")

    def stop(self) -> None:
        if self._greenlet:
            self._greenlet.kill(block=False)
            self._greenlet = None
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None
        if self.samples:
            print(f"[cache_hooks] run cache-hit ratio: "
                  f"min={self.hit_min:.2f}%  "
                  f"mean={self.hit_sum / self.samples:.2f}%  "
                  f"max={self.hit_max:.2f}%  "
                  f"({self.samples} samples in {_CSV_PATH})")
        try:
            self._client.close()
        except Exception:
            pass

    def _loop(self) -> None:
        while True:
            gevent.sleep(_INTERVAL)
            try:
                curr = _sample_counters(self._db)
            except Exception as e:
                print(f"[cache_hooks] sample error: {e}")
                continue
            t_curr = time.monotonic()
            elapsed = max(t_curr - self._t_prev, 1e-9)
            d_req = curr["requested"] - self._prev["requested"]
            d_miss = curr["read_in"] - self._prev["read_in"]
            d_bytes = curr["bytes_in"] - self._prev["bytes_in"]
            if d_req <= 0:
                # No cache activity in the window — skip; happens if idle.
                self._prev, self._t_prev = curr, t_curr
                continue
            hit = 100.0 * (1.0 - d_miss / d_req)
            req_ps = d_req / elapsed
            miss_ps = d_miss / elapsed
            disk_mbps = d_bytes / elapsed / (1024 * 1024)
            fill = 100.0 * curr["in_cache"] / curr["max"] if curr["max"] else 0.0

            ts = time.strftime("%H:%M:%SZ", time.gmtime())
            print(f"[cache] {ts}  hit={hit:6.2f}%  req/s={req_ps:>7,.0f}  "
                  f"miss/s={miss_ps:>6,.0f}  disk={disk_mbps:5.1f} MB/s  "
                  f"fill={fill:4.1f}%")

            self._writer.writerow([ts, f"{hit:.2f}", f"{req_ps:.0f}",
                                   f"{miss_ps:.0f}", f"{disk_mbps:.1f}",
                                   f"{fill:.1f}"])
            self.samples += 1
            self.hit_sum += hit
            self.hit_min = min(self.hit_min, hit)
            self.hit_max = max(self.hit_max, hit)

            if _UI_ROW:
                # Fake "operation" so the hit ratio shows in the Locust UI's
                # Statistics tab (Median / 95% / 99% become hit-ratio
                # percentiles over the run). Locust stores response_time in
                # ms, so we multiply by 100: 99.42% -> 9942 in the UI.
                events.request.fire(
                    request_type="wt_cache",
                    name="hit_pct_x100",
                    response_time=hit * 100,
                    response_length=0,
                )
                events.request.fire(
                    request_type="wt_cache",
                    name="disk_mb_per_s_x10",
                    response_time=disk_mbps * 10,
                    response_length=0,
                )

            self._prev, self._t_prev = curr, t_curr


_sampler: _CacheSampler | None = None


@events.test_start.add_listener
def _on_start(environment: Any, **kw: Any) -> None:
    if not _ENABLED:
        return
    # Master aggregates worker events but shouldn't itself sample WT (that
    # would double-count on a distributed run); only the standalone/master
    # process needs to sample once.
    if getattr(environment.runner, "worker_index", None) not in (None, 0):
        # Locust worker in distributed mode — let the master do the sampling.
        return
    global _sampler
    _sampler = _CacheSampler()
    _sampler.start()


@events.test_stop.add_listener
def _on_stop(environment: Any, **kw: Any) -> None:
    global _sampler
    if _sampler:
        _sampler.stop()
        _sampler = None
