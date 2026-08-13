"""Post-load verification and pre-flight checks.

Proves — rather than asserts — the access paths and environment:
count vs configured total, index inventory, ``explain()`` on both read paths,
storage vs data size, raw RTT against the latency budget, and hot-working-set
size against the cluster's WiredTiger cache.

Standalone usage:
    python src/verify.py                # everything
    python src/verify.py --preflight    # RTT + cache sizing only (fast)
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config, load_config  # noqa: E402
from generate import build_session_doc  # noqa: E402
from model import (  # noqa: E402
    SESSION_REQUIRED_FIELDS,
    owner_of_session,
    sample_session_index,
    session_id,
    user_id,
)

MB = 1024 * 1024


def _client(cfg: Config):
    from pymongo import MongoClient
    return MongoClient(cfg.mongodb_uri, maxPoolSize=8)


def preflight(cfg: Config) -> None:
    """Measure raw RTT and compare hot-set estimate to the WT cache size."""
    client = _client(cfg)
    db = client[cfg.mongodb_db]

    rtts = []
    db.command("ping")  # connection + TLS warm-up, excluded
    for _ in range(30):
        t0 = time.perf_counter()
        db.command("ping")
        rtts.append((time.perf_counter() - t0) * 1000)
    p50 = statistics.median(rtts)
    p95 = sorted(rtts)[int(len(rtts) * 0.95)]
    print(f"raw RTT to cluster: p50={p50:.2f} ms  p95={p95:.2f} ms  "
          f"(read SLO {cfg.read_latency_slo_ms} ms)")
    if p50 > cfg.read_latency_slo_ms * 0.5:
        print(f"  WARNING: network alone consumes >50% of the "
              f"{cfg.read_latency_slo_ms} ms budget. Is the load generator in "
              "the cluster's region?")

    try:
        stats = db.command("collStats", cfg.mongodb_collection)
        id_index = stats.get("indexSizes", {}).get("_id_", 0)
        hot_bytes = stats["size"] * cfg.hot_set_ratio + id_index
        print(f"hot-set estimate: {hot_bytes / MB:,.0f} MB "
              f"(dataSize {stats['size'] / MB:,.0f} MB x HOT_SET_RATIO "
              f"{cfg.hot_set_ratio} + _id index {id_index / MB:,.0f} MB)")
    except Exception:
        print("hot-set estimate: collection not loaded yet; run after src/load.py")
        hot_bytes = None
    try:
        wt = db.command("serverStatus")["wiredTiger"]["cache"]
        cache = int(wt["maximum bytes configured"])
        print(f"WiredTiger cache on primary: {cache / MB:,.0f} MB")
        if hot_bytes and hot_bytes > cache * 0.8:
            print("  WARNING: hot set does not fit comfortably in cache; p99 will be "
                  "NVMe-bound. Use a larger tier or lower HOT_SET_RATIO honestly.")
    except Exception as e:
        print(f"  (serverStatus unavailable on this user/tier: {e})")


def verify_count(cfg: Config) -> None:
    """Compare loaded document count against the configured total."""
    coll = _client(cfg)[cfg.mongodb_db][cfg.mongodb_collection]
    n = coll.estimated_document_count()
    print(f"document count (estimated): {n:,}   configured: {cfg.total_document_count:,}"
          f"{'' if n == cfg.total_document_count else '  (may lag on fresh loads)'}")
    print("indexes:", [i["name"] for i in coll.list_indexes()])


def verify_shape(cfg: Config) -> None:
    """Spot-check a stored document against the canonical field set."""
    coll = _client(cfg)[cfg.mongodb_db][cfg.mongodb_collection]
    d = coll.find_one({"_id": session_id(cfg.random_seed, 0)})
    assert d, "session doc 0 missing"
    missing = SESSION_REQUIRED_FIELDS - set(d)
    assert not missing, f"session doc missing required fields: {missing}"
    print("stored document shape matches the required field set")


JWT_CLAIMS = {
    "iss", "sub", "aud", "iat", "exp", "scope", "azp",
    "https://scotiabank.com/customerId", "https://scotiabank.com/segment",
    "https://scotiabank.com/channel", "https://scotiabank.com/authMethod",
    "https://scotiabank.com/mfaVerified",
}


def verify_jwt(cfg: Config, sample_size: int = 200) -> None:
    """Decode accessToken JWTs from STORED documents and validate structure.

    Checks, per sampled session: 3 base64url segments; RS256 header with a
    FAKE- kid (proves no real token leaked in); the namespaced claim set the
    generator emits; and a 342-char signature (2048-bit RSA-length).
    """
    def b64(s: str) -> dict:
        return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))

    coll = _client(cfg)[cfg.mongodb_db][cfg.mongodb_collection]
    docs = list(coll.aggregate([{"$sample": {"size": sample_size}}]))
    assert docs, "no session documents in the collection"
    kids = set()
    for d in docs:
        tok = d["accessToken"]
        parts = tok.split(".")
        assert len(parts) == 3, f"{d['_id']}: token is not a 3-segment JWT"
        header = b64(parts[0])
        assert header.get("alg") == "RS256" and header.get("typ") == "JWT", \
            f"{d['_id']}: bad JWT header {header}"
        assert str(header.get("kid", "")).startswith("FAKE-"), \
            f"{d['_id']}: kid not FAKE- marked — a real token may have leaked in"
        kids.add(header["kid"])
        payload = b64(parts[1])
        missing = JWT_CLAIMS - set(payload)
        assert not missing, f"{d['_id']}: payload missing claims {missing}"
        assert len(parts[2]) == 342, f"{d['_id']}: signature length {len(parts[2])} != 342"
        assert payload["azp"] == d["userId"], f"{d['_id']}: azp != userId"
    print(f"JWT check: {len(docs)} stored tokens decoded — RS256 header, "
          f"expected claim set, 342-char signature; signing kid(s): {sorted(kids)}")


def verify_explain(cfg: Config) -> None:
    """Print winning plans for all three read paths.

    Point read by ``_id`` must be IDHACK / EXPRESS (no secondary index).
    ``userId`` and ``profile.email`` lookups must use their dedicated indexes.
    """
    coll = _client(cfg)[cfg.mongodb_db][cfg.mongodb_collection]
    rng = random.Random(1)

    i = sample_session_index(rng, cfg.total_document_count, cfg.hot_set_ratio,
                             cfg.hot_access_share)
    sid = session_id(cfg.random_seed, i)
    exp = coll.find({"_id": sid}).explain()
    plan = exp["queryPlanner"]["winningPlan"]
    stage = plan.get("stage") or plan.get("queryPlan", {}).get("stage")
    stats = exp["executionStats"]
    print(f"point read winning plan: {stage}  "
          f"(examined docs={stats['totalDocsExamined']}, keys={stats['totalKeysExamined']}, "
          f"serverside {stats['executionTimeMillis']} ms)")
    assert stage in ("IDHACK", "EXPRESS_IXSCAN", "EXPRESS_CLUSTERED_IXSCAN"), \
        f"point read is not an _id express/idhack lookup: {json.dumps(plan)[:400]}"

    # userId path: derive a real stored userId client-side (deterministic
    # generation), project down to session-list fields.
    j = owner_of_session(cfg.random_seed, i, cfg.synthetic_user_count)
    uid = user_id(cfg.random_seed, j)
    projection = {"channel": 1, "deviceInfo": 1, "lastUsedDate": 1, "expiresAt": 1}
    exp = coll.find({"userId": uid}, projection).sort("lastUsedDate", -1) \
              .limit(cfg.user_sessions_limit).explain()
    stats = exp["executionStats"]
    plan_json = json.dumps(exp["queryPlanner"]["winningPlan"])
    assert "userId_1" in plan_json, f"userId lookup not using userId_1: {plan_json[:400]}"
    print(f"userId lookup: uses userId_1, returned={stats['nReturned']}, "
          f"keys={stats['totalKeysExamined']}, serverside {stats['executionTimeMillis']} ms")

    # email path: derive a real email from a sampled non-anonymous session.
    doc = None
    for k in range(50):
        d = build_session_doc(cfg, k)
        if "profile" in d:
            doc = d
            break
    assert doc, "no non-anonymous session in first 50 — check PROFILE_PRESENT_RATIO"
    exp = coll.find({"profile.email": doc["profile"]["email"]},
                    {"profile": 1, "deviceInfo": 1, "lastUsedDate": 1}) \
              .limit(1).explain()
    stats = exp["executionStats"]
    plan_json = json.dumps(exp["queryPlanner"]["winningPlan"])
    assert "email_1" in plan_json, f"email lookup not using email_1: {plan_json[:400]}"
    print(f"email lookup: uses email_1, returned={stats['nReturned']}, "
          f"keys={stats['totalKeysExamined']}, serverside {stats['executionTimeMillis']} ms")


def verify_storage(cfg: Config) -> None:
    """Report logical vs on-disk size so the compression ratio is a fact."""
    db = _client(cfg)[cfg.mongodb_db]
    st = db.command("collStats", cfg.mongodb_collection)
    if st["storageSize"] < st["size"] * 0.01:
        print(f"dataSize {st['size'] / MB:,.1f} MB | storageSize not yet reported by "
              "WiredTiger (fresh load) — rerun in a minute for the compression ratio")
        return
    print(f"dataSize {st['size'] / MB:,.1f} MB | storageSize {st['storageSize'] / MB:,.1f} MB "
          f"| compression {st['size'] / max(st['storageSize'], 1):.2f}x "
          f"| avgObjSize {st['avgObjSize']} B")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--preflight", action="store_true", help="RTT + cache sizing only")
    args = p.parse_args()
    cfg = load_config()
    preflight(cfg)
    if args.preflight:
        return
    print()
    verify_count(cfg)
    verify_shape(cfg)
    verify_jwt(cfg)
    verify_explain(cfg)
    verify_storage(cfg)


if __name__ == "__main__":
    main()
