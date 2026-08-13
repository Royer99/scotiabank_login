# CLAUDE.md

MongoDB session-store benchmark for a Scotiabank-style login workload. Native
MongoDB design (no Redis mirroring): one collection, one document per
session, `_id` = session UUID. The point-read plan must stay EXPRESS/IDHACK.
Two secondary indexes (`userId_1`, partial `email_1`) serve customer-driven
lookups and must never appear in `get_session`'s plan.

## Commands

```bash
pytest tests/                              # offline, no cluster needed
python src/generate.py --dry-run --limit 3 # inspect sample docs
python src/load.py [--drop]                # full load (run from EC2, resumable)
python src/verify.py [--preflight]         # RTT/cache check + access-path proof
scripts/run_headless.sh                    # warm-up + measured Locust run
scripts/watch_progress.sh                  # live SLO verdict (second terminal)
python scripts/report.py --csv-prefix results
```

## Invariants — do not break

- **Determinism:** every document derives purely from
  `(RANDOM_SEED, kind, global index)` via `model.doc_rng`/`_digest`. Never
  introduce wall-clock time, ordering dependence, or shared RNG state into
  generation — `tests/test_generate.py::test_worker_partitioning_yields_identical_dataset`
  guards this. Timestamps anchor to `model.EPOCH_ANCHOR_MS`, not `now()`.
- **`session_id()` / `user_id()` live only in `src/model.py`** and are
  imported by both generator and harness. The harness relies on them to hit
  existing `_id`s and real `userId`s with zero memory.
- The dataset is **never** written to disk at scale; generation streams into
  `insert_many`. `--jsonl` is for small samples only.
- Loader must stay resumable: checkpoint after every batch, swallow only
  duplicate-key (11000) errors, refuse resume if the worker's assigned range
  changed (that would indicate `GENERATE_WORKERS` was changed mid-load).
- One `MongoClient` per Locust worker process, module scope, `connect=False`.
  Locust's gevent monkey-patching must happen before pymongo import (it does,
  because locust is imported first in locustfile.py).
- TTL index stays behind `ENABLE_TTL_INDEX=false`: ~10% of generated docs are
  already expired and a TTL index would delete them mid-run.
- The `email_1` index is intentionally PARTIAL (`profile.email` exists) so
  the ~15% anonymous sessions don't inflate it. Do not drop the
  `partialFilterExpression`.
- Report percentiles per named operation; never blend ops or quote a mean.

## Safety guardrails — keep these

- Emails use RFC 2606 reserved domains only (`example.com/net/org`,
  `mail.example.ca`). Never introduce a real-looking domain.
- JWTs are structurally faithful RS256 tokens with **random-byte signatures**
  and a `FAKE-` prefixed `kid`. Do not add a real key or sign anything.
- IPs come from RFC 5737 TEST-NET ranges (`192.0.2.0/24`, `198.51.100.0/24`,
  `203.0.113.0/24`) — never a real address.
- Customer numbers are opaque synthetic (`CUST-<12 digits>`), not a real BNS
  identifier format.
- Phone numbers use the +1416 area code with random digits; not real.

## Gotchas

- `SYNTHETIC_USER_COUNT` × sessions-per-user should roughly track
  `TOTAL_DOCUMENT_COUNT`. `config.py` prints the derived average at startup.
- `PROFILE_PRESENT_RATIO` controls the share of docs carrying `profile.email`
  and therefore the `email_1` index selectivity. Anonymous sessions must not
  get a profile block — the partial-index filter depends on that.
- `owner_of_session` uses a Pareto draw so a small fraction of users hold
  many sessions (heavy web-banking / small-business logins). Do not replace
  with a uniform draw — it makes `get_user_sessions` return the same size
  for every principal and hides the customer-realistic tail.
- The harness's `get_user_sessions` derives the `userId` from the SAMPLED
  session index via `owner_of_session`, so the query always finds at least
  the sampled session (plus any siblings the same customer has).
