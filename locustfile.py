"""Locust harness for the Scotiabank session-store benchmark.

Operation mapping:
  get_session          find_one({_id: ...})                # SLO applies here
  get_user_sessions    find({userId: ...}).sort(...).limit
  find_by_email        find_one({profile.email: ...}, proj)
  update_session       update_one $set lastUsedDate
  insert_session       insert_one full session doc

Lookup values are derived client-side via the deterministic generator (no
in-memory tables), so the harness always hits real stored keys/emails.

Locust monkey-patches gevent before this module is imported, so importing
pymongo here is safe. ONE MongoClient per worker process, at module scope,
with connect=False — never per User or per task.
"""

from __future__ import annotations

import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from locust import User, constant_throughput, events, task

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import bson  # noqa: E402  (ships with pymongo)
from pymongo import MongoClient  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

from config import load_config  # noqa: E402
from generate import build_session_doc  # noqa: E402
from model import (  # noqa: E402
    owner_of_session,
    sample_session_index,
    session_id,
    user_id,
)

CFG = load_config()
CLIENT: MongoClient = MongoClient(
    CFG.mongodb_uri,
    maxPoolSize=CFG.max_pool_size,
    readPreference=CFG.read_preference,
    w=CFG.write_concern,
    connect=False,
    appname="locust-scotia-login-bench",
)
COLL = CLIENT[CFG.mongodb_db][CFG.mongodb_collection]

# Total op rate implied by the read target; each user (reader or writer)
# carries an equal share so reads land at ~TARGET_READ_RPS.
_TOTAL_RPS = CFG.target_read_rps / max(1.0 - CFG.write_ratio, 0.01)
_PER_USER_RPS = _TOTAL_RPS / CFG.locust_users

# Weights for the read task mix. Point read is the residual.
_POINT_SHARE = max(0.0, 1.0 - CFG.user_lookup_ratio - CFG.email_lookup_ratio)
_W_POINT = max(1, round(_POINT_SHARE * 100))
_W_USER = max(1, round(CFG.user_lookup_ratio * 100))
_W_EMAIL = max(1, round(CFG.email_lookup_ratio * 100))

# Response projection for lookups: profile + device + recency. Avoids shipping
# the ~1 KB JWT over the wire when the caller only wants "is it me?".
LOOKUP_PROJECTION = {"userId": 1, "channel": 1, "profile": 1,
                     "deviceInfo": 1, "lastUsedDate": 1, "expiresAt": 1}
USER_LIST_PROJECTION = {"channel": 1, "deviceInfo.userAgent": 1,
                        "deviceInfo.ip": 1, "lastUsedDate": 1, "expiresAt": 1}

_COUNTERS = {"user_lookup_reqs": 0, "user_lookup_hits": 0,
             "update_sent": 0, "update_matched": 0}


@events.init.add_listener
def _assert_pool(environment: Any, **kw: Any) -> None:
    # Undersized pools queue in the driver and get reported as DB latency.
    assert CFG.max_pool_size >= CFG.locust_users, (
        f"MONGODB_MAX_POOL_SIZE={CFG.max_pool_size} must be >= per-process users "
        f"(worst case LOCUST_USERS={CFG.locust_users}); raise the pool or add workers"
    )


@events.test_stop.add_listener
def _report_stats(environment: Any, **kw: Any) -> None:
    if _COUNTERS["user_lookup_reqs"]:
        print(f"[user lookup] {_COUNTERS['user_lookup_reqs']} queries, "
              f"{_COUNTERS['user_lookup_hits']} returned >=1 session")
    if _COUNTERS["update_sent"]:
        print(f"[writes] update_session matched "
              f"{_COUNTERS['update_matched']}/{_COUNTERS['update_sent']}")


def _fire(name: str, start: float, response_length: int = 0,
          exc: Exception | None = None) -> None:
    events.request.fire(
        request_type="mongo", name=name,
        response_time=(time.perf_counter() - start) * 1000,
        response_length=response_length, exception=exc,
    )


def _timed(name: str, fn: Any) -> Any:
    start = time.perf_counter()
    try:
        result = fn()
    except Exception as e:  # timeouts, transient network errors -> Locust failures
        _fire(name, start, exc=e)
        return None
    _fire(name, start, response_length=_blen(result))
    return result


def _blen(result: Any) -> int:
    if isinstance(result, dict):
        return len(bson.encode(result))
    if isinstance(result, list):
        return sum(len(bson.encode(d)) for d in result)
    return 0


class SessionReader(User):
    """Point reads and the two secondary-index lookups, recency-skewed."""

    weight = max(1, round((1.0 - CFG.write_ratio) * 100))
    wait_time = constant_throughput(_PER_USER_RPS)

    def __init__(self, environment: Any) -> None:
        super().__init__(environment)
        self.rng = random.Random()

    def _sample_index(self) -> int:
        return sample_session_index(self.rng, CFG.total_document_count,
                                    CFG.hot_set_ratio, CFG.hot_access_share)

    @task(_W_POINT)
    def get_session(self) -> None:
        """Point read by ``_id``. The SLO applies to this operation."""
        sid = session_id(CFG.random_seed, self._sample_index())
        _timed("get_session", lambda: COLL.find_one({"_id": sid}))

    @task(_W_USER)
    def get_user_sessions(self) -> None:
        """List a customer's most recent sessions (userId_1 index)."""
        i = self._sample_index()
        j = owner_of_session(CFG.random_seed, i, CFG.synthetic_user_count)
        uid = user_id(CFG.random_seed, j)

        def do() -> list:
            return list(COLL.find({"userId": uid}, USER_LIST_PROJECTION)
                        .sort("lastUsedDate", -1).limit(CFG.user_sessions_limit))

        res = _timed("get_user_sessions", do)
        if res is not None:
            _COUNTERS["user_lookup_reqs"] += 1
            if res:
                _COUNTERS["user_lookup_hits"] += 1

    @task(_W_EMAIL)
    def find_by_email(self) -> None:
        """Support lookup by email (email_1 partial index)."""
        # Anonymous sessions (~15%) have no profile. Try a few draws.
        prof = None
        for _ in range(8):
            d = build_session_doc(CFG, self._sample_index())
            prof = d.get("profile")
            if prof:
                break
        if not prof:
            return
        _timed("find_by_email",
               lambda: COLL.find_one({"profile.email": prof["email"]},
                                     LOOKUP_PROJECTION))


class SessionWriter(User):
    """Concurrent write load at WRITE_RATIO of total ops."""

    weight = max(1, round(CFG.write_ratio * 100))
    wait_time = constant_throughput(_PER_USER_RPS)

    def __init__(self, environment: Any) -> None:
        super().__init__(environment)
        self.rng = random.Random()

    @task(6)
    def update_session(self) -> None:
        """Refresh lastUsedDate on an existing session ($set inside doc)."""
        i = sample_session_index(self.rng, CFG.total_document_count,
                                 CFG.hot_set_ratio, CFG.hot_access_share)
        now = datetime.now(timezone.utc)
        res = _timed("update_session", lambda: COLL.update_one(
            {"_id": session_id(CFG.random_seed, i)},
            {"$set": {"lastUsedDate": now}},
        ))
        if res is not None:
            _COUNTERS["update_sent"] += 1
            _COUNTERS["update_matched"] += res.matched_count

    @task(2)
    def insert_session(self) -> None:
        """New login -> insert a full session doc (indices above the read range)."""
        i = self.rng.randrange(CFG.total_document_count * 10,
                               CFG.total_document_count * 11)
        doc = build_session_doc(CFG, i)

        def do() -> dict[str, Any]:
            try:
                COLL.insert_one(doc)
            except DuplicateKeyError:
                pass  # same high index drawn twice; the doc exists, fine
            return doc

        _timed("insert_session", do)


if CFG.write_ratio == 0:
    # WRITE_RATIO=0 -> read-only benchmark: unregister the writer entirely
    # (its class weight floors at 1, so it would still spawn otherwise).
    del SessionWriter
