import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from config import Config  # noqa: E402


def make_cfg(**overrides) -> Config:
    """Small, fast Config for tests; same shape as the real defaults."""
    base = dict(
        mongodb_uri="", mongodb_db="test", mongodb_collection="sessions",
        max_pool_size=200, read_preference="primary", write_concern=1,
        total_document_count=1000, synthetic_user_count=200,
        sessions_per_user_skew=1.4, profile_present_ratio=0.85,
        payload_target_bytes=2800, session_ttl_days=1,
        expired_session_ratio=0.10, random_seed=42, enable_ttl_index=False,
        generate_workers=4, load_batch_size=50,
        load_checkpoint_path="./.load_checkpoint.json",
        hot_set_ratio=0.05, hot_access_share=0.80,
        locust_users=200, locust_spawn_rate=20, locust_run_time="10m",
        target_read_rps=2000, write_ratio=0.1,
        user_lookup_ratio=0.15, email_lookup_ratio=0.05,
        user_sessions_limit=25, read_latency_slo_ms=10,
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture
def cfg() -> Config:
    return make_cfg()
