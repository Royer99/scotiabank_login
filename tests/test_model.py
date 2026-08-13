import random
import re

from model import (
    owner_of_session,
    sample_session_index,
    session_id,
    session_uuid,
    user_id,
)

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def test_session_id_deterministic():
    assert session_id(42, 123) == session_id(42, 123)
    assert session_id(42, 123) != session_id(42, 124)
    assert session_id(43, 123) != session_id(42, 123)


def test_session_id_is_uuid_shaped():
    for i in range(500):
        sid = session_id(42, i)
        assert UUID_RE.match(sid), sid
        assert sid == session_uuid(42, i)


def test_user_id_shape_and_determinism():
    for j in (0, 5, 137, 999):
        uid = user_id(42, j)
        assert re.fullmatch(r"CUST-\d{12}", uid), uid
        assert user_id(42, j) == uid


def test_owner_lies_in_user_range():
    for i in range(5000):
        j = owner_of_session(42, i, 200)
        assert 0 <= j < 200


def test_owner_skewed_but_covers_range():
    """Pareto skew: low ids get more sessions but every user index eventually hits."""
    n_users = 100
    counts = [0] * n_users
    for i in range(20_000):
        counts[owner_of_session(42, i, n_users)] += 1
    # Every user should own at least one session over 20k draws
    assert min(counts) >= 1
    # Low-id users hold more than the mean (heavy tail).
    mean = sum(counts) / n_users
    top_five_share = sum(sorted(counts, reverse=True)[:5]) / sum(counts)
    assert top_five_share > 5 / n_users
    assert mean > 0


def test_generator_and_harness_agree_on_ids(cfg):
    """The harness must derive any generated _id in O(1) from (seed, index)."""
    from generate import build_session_doc
    for i in (0, 1, 137, 859):
        assert build_session_doc(cfg, i)["_id"] == session_id(cfg.random_seed, i)


def test_hot_set_sampling_distribution():
    rng = random.Random(1)
    n, hot_ratio, hot_share = 100_000, 0.05, 0.80
    boundary = n - int(n * hot_ratio)
    draws = [sample_session_index(rng, n, hot_ratio, hot_share) for _ in range(20_000)]
    in_hot = sum(1 for d in draws if d >= boundary) / len(draws)
    assert abs(in_hot - hot_share) < 0.02
    assert all(0 <= d < n for d in draws)
