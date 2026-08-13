"""Document envelope and deterministic key derivation.

This module is the single source of truth for the session document shape and
for key derivation. Both the generator and the Locust harness import it, so
any random index can be turned into a valid ``_id`` in O(1) with no lookup
table.

The design is native MongoDB (no Redis mirroring): one collection, one
document per session, ``_id`` = session UUID. The point read is
``find_one({_id: ...})``, which resolves as ``EXPRESS`` / ``IDHACK`` on the
free ``_id`` index — no secondary index touches the hot read path.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timezone
from typing import Any

# Fixed anchor for deterministic timestamps: generation must be byte-identical
# under a given seed, so wall-clock time never enters document content.
EPOCH_ANCHOR_MS = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp() * 1000)

# Banking-side context observed as reasonable defaults for a retail online
# banking session store. Weights are rough but stable and hashed against.
CHANNELS: list[tuple[str, int]] = [
    ("mobile_app", 62),   # majority of daily logins
    ("web", 28),
    ("atm", 6),
    ("api", 4),           # first-party integrations / open banking
]
_CH_TOTAL = sum(w for _, w in CHANNELS)

AUTH_METHODS: list[tuple[str, int]] = [
    ("biometric", 55),    # face/fingerprint on registered device
    ("password", 25),
    ("mfa_push", 15),
    ("otp_sms", 5),
]
_AM_TOTAL = sum(w for _, w in AUTH_METHODS)

SEGMENTS = ["retail", "small_business", "wealth", "commercial"]

# Top-level document fields, used by shape tests and verify.py.
SESSION_DOC_FIELDS = {
    "_id", "userId", "channel", "authMethod", "segment",
    "accessToken", "scope", "tokenType", "expiresIn",
    "deviceInfo", "profile", "padding",
    "createdAt", "expiresAt", "lastUsedDate", "ttlSeconds",
}
SESSION_REQUIRED_FIELDS = {
    "_id", "userId", "channel", "authMethod", "segment",
    "accessToken", "createdAt", "expiresAt", "lastUsedDate",
}


def _digest(*parts: object, size: int = 20) -> bytes:
    return hashlib.blake2b(":".join(str(p) for p in parts).encode(), digest_size=size).digest()


def session_uuid(seed: int, i: int) -> str:
    """Deterministic UUID4-shaped session id for index *i* under *seed*."""
    h = _digest(seed, i, size=16).hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def session_id(seed: int, i: int) -> str:
    """Full session document ``_id``. Kept as the UUID string (no prefix).

    A namespaced prefix would inflate the _id index for no lookup benefit;
    the customer field (``userId``) is what filters at the application layer.
    """
    return session_uuid(seed, i)


def user_id(seed: int, j: int) -> str:
    """Deterministic customer identifier for principal *j*.

    Uses a Scotiabank-style opaque customer number: a fixed prefix plus a
    12-digit id. Not a real BNS format — synthetic on purpose.
    """
    n = int.from_bytes(_digest("cust", seed, j, size=8), "big") % 10**12
    return f"CUST-{n:012d}"


def owner_of_session(seed: int, i: int, user_count: int) -> int:
    """Which customer index owns session *i*, under a Pareto-style skew.

    A small fraction of principals hold many sessions (heavy web-banking
    users, small_business with multiple staff). Cheap to compute, purely a
    function of (seed, i, user_count).
    """
    # Draw a Pareto sample from a per-index RNG, clamp to [0, user_count).
    rng = random.Random(int.from_bytes(_digest("own", seed, i, size=8), "big"))
    # Shape 1.4 spreads across users but concentrates a long tail on low ids.
    p = rng.paretovariate(1.4)
    # Invert: heavy users at low ids. Scale so mean ~ user_count / e.
    j = int((p - 1.0) * user_count / 3.0) % user_count
    return j


def shard_hint(seed: int, i: int) -> str:
    """Optional bucket label (retained for per-bucket reporting; not indexed)."""
    return f"bucket{int.from_bytes(_digest('shard', seed, i, size=2), 'big') % 8 + 1}"


def doc_rng(seed: int, kind: str, i: int) -> random.Random:
    """Per-document RNG derived from (seed, kind, index) only.

    Guarantees document content is independent of generation order and worker
    count.
    """
    return random.Random(int.from_bytes(_digest(kind, seed, i, size=8), "big"))


def session_created_ms(seed: int, i: int, session_count: int, ttl_days: int,
                       expired_ratio: float) -> int:
    """Deterministic creation time for session *i*, older for lower indices.

    Creation times spread over a window sized so that exactly
    ``expired_ratio`` of sessions have ``created + ttl`` in the past relative
    to EPOCH_ANCHOR_MS. The newest sessions are the highest indices, which is
    what makes tail-sampling equal recency-sampling.
    """
    ttl_ms = ttl_days * 86_400_000
    window_ms = ttl_ms / (1.0 - expired_ratio) if expired_ratio < 1.0 else ttl_ms * 10
    frac = (i + 1) / session_count
    jitter = doc_rng(seed, "ts", i).uniform(0, window_ms / session_count)
    return int(EPOCH_ANCHOR_MS - window_ms * (1.0 - frac) - jitter)


def sample_session_index(rng: random.Random, session_count: int,
                         hot_set_ratio: float, hot_access_share: float) -> int:
    """Draw a session index under the recency-skewed access distribution.

    ``hot_access_share`` of draws land uniformly in the most recent
    ``hot_set_ratio`` of indices (the tail); the rest are uniform over the
    cold range.
    """
    hot = max(1, int(session_count * hot_set_ratio))
    boundary = session_count - hot
    if rng.random() < hot_access_share or boundary == 0:
        return rng.randrange(boundary, session_count)
    return rng.randrange(0, boundary)


def weighted_pick(rng: random.Random, options: list[tuple[str, int]], total: int) -> str:
    """Weighted deterministic pick from a (value, weight) list."""
    sel = rng.randrange(total)
    for value, w in options:
        if sel < w:
            return value
        sel -= w
    raise AssertionError("unreachable")


def ensure_indexes(coll: Any, enable_ttl_index: bool) -> list[str]:
    """Create the secondary indexes; returns names created.

    Hot read path (``find_one({_id: ...})``) needs none: ``_id``'s unique
    index comes for free and resolves as EXPRESS / IDHACK.

    Two customer-driven query indexes:

    | index    | keys                       | serves                          |
    |----------|----------------------------|---------------------------------|
    | userId_1 | ``{userId: 1}``            | list a customer's active sessions |
    | email_1  | ``{profile.email: 1}``     | support account lookup by email |

    ``email_1`` is a PARTIAL index because ~15% of sessions are anonymous
    (pre-login flows) and carry no profile block; excluding them keeps the
    index small and the equality-filter plan uses it (equality implies
    ``$exists: true``). Neither index appears in ``find_one({_id: ...})``.
    """
    created: list[str] = [
        coll.create_index([("userId", 1)], name="userId_1"),
        coll.create_index(
            [("profile.email", 1)], name="email_1",
            partialFilterExpression={"profile.email": {"$exists": True}},
        ),
    ]
    if enable_ttl_index:
        # Off by default: the dataset carries past expiries on purpose and
        # would delete them mid-benchmark, moving p99 as it churns.
        created.append(coll.create_index(
            [("expiresAt", 1)], name="ttl_expiresAt", expireAfterSeconds=0,
        ))
    return created
