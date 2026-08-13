# Scotiabank login session-store benchmark on MongoDB Atlas

Demonstrates that a login/auth session store for a large retail banking
customer runs on MongoDB Atlas with **point reads under a 10 ms SLO** at
16 million sessions and a sustained target RPS, while a modest write load
runs concurrently. One collection, one document per session, `_id` = session
UUID, and the hot read path is `find_one({_id: ...})` — a free `_id` index
resolves as `EXPRESS` / `IDHACK` (proven by `verify.py`, not asserted).

No Redis is involved. The document shape is native MongoDB: a session
envelope holding the issued access token (a structurally-faithful, cryptographically
invalid RS256 JWT), the granted scope, the device context, and — for
non-anonymous logins — a small profile block used by support lookups.

## Data model

One collection, one document per session:

```js
{ _id: "<session uuid>",
  userId: "CUST-000000012345",
  segment: "retail" | "small_business" | "wealth" | "commercial",
  channel: "mobile_app" | "web" | "atm" | "api",
  authMethod: "biometric" | "password" | "mfa_push" | "otp_sms",
  scope: "openid profile email accounts:read payments:read",
  tokenType: "Bearer",
  expiresIn: 86400,
  accessToken: "<RS256 JWT — FAKE-kid, random signature>",
  deviceInfo: { channel, model, browser, os, osVersion, appVersion, ip, userAgent },
  profile: { email, emailVerified, firstName, lastName, phoneNumber, preferredLanguage },
  createdAt: ISODate(), expiresAt: ISODate(), lastUsedDate: ISODate(),
  ttlSeconds: 86400,
  shard: "bucket3",       // metadata, not indexed
  padding: "<b64>" }       // to hit PAYLOAD_TARGET_BYTES honestly
```

**Zero secondary indexes on the primary access path.** `_id` is the session
UUID; its unique index comes for free and the point read is an `EXPRESS` /
`IDHACK` plan. Two secondary indexes serve customer-driven lookups (which is
where a plain KV store would need to fall back to full scans):

| index      | keys                     | serves                             |
|------------|--------------------------|------------------------------------|
| `userId_1` | `{userId: 1}`            | list a customer's active sessions  |
| `email_1`  | `{profile.email: 1}`     | support lookup by email (partial: `profile.email` exists) |

`email_1` is **partial** because ~15% of sessions are anonymous (pre-login
flows: password recovery, enrollment) and carry no profile block; excluding
them keeps the index small and equality on the field still uses it. Neither
appears in `find_one({_id: ...})` — `verify.py` prints the winning plans.

Optional: TTL index on `expiresAt` (`ENABLE_TTL_INDEX=false` by default —
the dataset carries past expiries on purpose and would delete itself
mid-benchmark).

## Scale

`TOTAL_DOCUMENT_COUNT=16_000_000` sessions across `SYNTHETIC_USER_COUNT=3.2M`
customers (Pareto-skewed ownership). Roughly 45 GB logical at the default
`PAYLOAD_TARGET_BYTES=2800`; `verify.py` prints the actual `dataSize` vs
`storageSize` so the WiredTiger compression ratio is a reported fact.

**Generate from an EC2 instance in the Atlas cluster's region. This is a
prerequisite, not an optimization** — pushing tens of GB over an office
connection is a non-starter; in-region it is minutes. The dataset is never
written to disk: workers generate and insert in one streaming pass.

## Setup (EC2)

1. Launch the instance in the **same AWS region** as the Atlas cluster
   (cross-region RTT alone can exceed the 10 ms budget) and add it to the
   Atlas IP access list (or use VPC peering / private endpoint).
2. Clone the repo onto the host, then:

```bash
scripts/ec2_setup.sh          # python venv, deps, ulimit -n 65535
cp .env.example .env          # fill in MONGODB_URI; .env is gitignored
source .venv/bin/activate
python src/verify.py --preflight
```

The pre-flight prints raw RTT against the latency budget (so it's clear how
much of the 10 ms is network before any load runs) and the hot-set estimate
next to the cluster's actual WiredTiger cache size, warning if the tier is
undersized — catch it here, not in bad percentiles afterward.

### Cluster sizing

What must fit in cache is the **hot working set**, not the dataset:
`dataSize × HOT_SET_RATIO` plus the `_id` index — roughly 2.3 GB + index at
the defaults. Atlas gives WiredTiger about half of instance RAM; pick a tier
whose cache comfortably exceeds the pre-flight's hot-set figure. Reads that
miss cache land on NVMe and are still fast, but they are what will move p99.

## Load

```bash
python src/load.py            # resumable; ~GENERATE_WORKERS processes
python src/load.py --drop     # start over
```

- Workers own contiguous ID ranges; every document derives purely from
  `(RANDOM_SEED, kind, index)`, so the dataset is byte-identical regardless
  of worker count.
- Progress line: docs/s, elapsed, ETA, per-worker counts.
- Checkpoints to `LOAD_CHECKPOINT_PATH.w<n>` after every batch — an
  interrupted load **resumes**; duplicate keys from resume overlap are
  swallowed. Never restarts from zero.
- On completion: `count`, `avgObjSize`, `dataSize`, `storageSize`, index
  sizes.
- Inspect sample documents anytime without touching the cluster:
  `python src/generate.py --dry-run --limit 3`.

Then prove the state: `python src/verify.py` (count, stored shape, JWT
structure, `explain()` for all three read paths, storage/compression).

## Benchmark

```bash
scripts/run_headless.sh                                 # standalone
# distributed: one worker per vCPU
LOCUST_MODE=master EXPECT_WORKERS=8 scripts/run_headless.sh
LOCUST_MODE=worker MASTER_HOST=<master-ip> scripts/run_headless.sh   # x8
```

Workers must reach the master on TCP 5557. Every run starts with a discarded
warm-up phase so TLS handshakes, pool growth, and first-touch page faults
stay out of the reported percentiles.

Operations:

| name                | MongoDB op                                                | notes |
|---------------------|-----------------------------------------------------------|-------|
| `get_session`       | `find_one({_id})`                                         | **the SLO applies here** |
| `get_user_sessions` | `find({userId}).sort(lastUsedDate: -1).limit(N)` (proj)   | uses `userId_1` |
| `find_by_email`     | `find_one({profile.email}, projection)`                   | uses `email_1` (partial) |
| `update_session`    | `update_one({_id}, {$set: {lastUsedDate}})`               | writer |
| `insert_session`    | `insert_one(<full session doc>)`                          | writer |

Lookups derive their inputs client-side via `build_session_doc` (generation
is deterministic), so they always hit real stored values with zero memory or
precomputed lists. `find_by_email` and `get_user_sessions` project the
result down to profile/device/recency fields — the ~1 KB JWT never leaves
the server, cutting the response ~10x vs returning full docs.

Notes on honesty:

- **Access distribution:** `HOT_ACCESS_SHARE` (80%) of reads hit the most
  recent `HOT_SET_RATIO` (5%) of sessions; the rest are uniform over the
  cold range. Quote latency numbers only next to this distribution — a
  latency figure without its access pattern is not meaningful.
- One `MongoClient` per Locust worker process, `connect=False`, created at
  module scope. Startup asserts `MONGODB_MAX_POOL_SIZE` covers concurrent
  users so pool queuing is never reported as database latency.
- `constant_throughput` pacing makes `TARGET_READ_RPS` a target, not an
  emergent property of think time.

## Watching progress in real time

Three options, pick whichever fits the demo:

**1. Locust's built-in headless output.** The measured run already streams a
per-op table to stdout every ~2 s (RPS, min/mean/max, failures). No extra
step needed — just watch the terminal where `run_headless.sh` is running.

**2. Locust web UI (best for a customer screen share).** Skip
`run_headless.sh` and start Locust with the UI, then open port 8089:

```bash
source .env && ulimit -n 65535
.venv/bin/locust -f locustfile.py \
  --web-host 0.0.0.0 --web-port 8089 \
  --csv results --csv-full-history --html results_report.html
# open http://<ec2-public-ip>:8089 (open the SG for TCP 8089)
```

You get live charts of RPS, response-time percentiles, and per-op tables.

**3. Live SLO verdict in a second terminal.** While `run_headless.sh` is
running, open a second SSH session and:

```bash
scripts/watch_progress.sh                 # default: results, 2s refresh
scripts/watch_progress.sh myrun 1         # custom prefix, 1s refresh
```

This tails `results_stats_history.csv` (Locust writes to it every 2 s under
`--csv-full-history`) and re-renders the per-op p50/p95/p99 table with a
`PASS`/`FAIL` verdict against `READ_LATENCY_SLO_MS` for `get_session`. Ctrl-C
to stop — it doesn't touch the run.

## Reading the final results

```bash
python scripts/report.py --csv-prefix results
```

Prints **p50 / p95 / p99 / max per named operation** (never a bare mean) and
writes `results_latency.png` — latency over time with the
`READ_LATENCY_SLO_MS` line drawn on it. The measured run also writes
Locust's standard HTML report (`results_report.html`: RPS, response-time
percentiles, and user-count charts) — scp it off the EC2 to view.

Two numbers must never be conflated:

- **Client-observed latency** — what Locust times from EC2, network
  included. This is the customer-facing number; the pre-flight RTT tells you
  its floor.
- **Server-side execution time** — `verify.py`'s `explain()` output, plus
  Atlas metrics (operation execution time, cache hit ratio, queue depth)
  over the run window.

## Repository layout

```
src/config.py     .env loading + validation, fail fast
src/model.py      envelope shape, deterministic key derivation (shared by
                  generator AND harness — any index -> valid _id in O(1))
src/generate.py   synthetic generator; --dry-run / --jsonl for inspection
src/load.py       multiprocess streaming loader, checkpoint/resume
src/verify.py     pre-flight (RTT, cache sizing) + post-load proofs
locustfile.py     the harness
scripts/          EC2 setup, headless runner, live progress viewer,
                  CSV -> table + chart
tests/            determinism, checkpoint resume, config validation
```

Run tests locally: `pytest tests/` (no cluster needed).
