import pytest

from config import ConfigError, load_config

NO_ENV = "/nonexistent/.env"  # keep the repo's real .env out of these tests


def _clean(monkeypatch, **env):
    for k in ("MONGODB_URI", "TOTAL_DOCUMENT_COUNT", "SYNTHETIC_USER_COUNT",
              "SESSIONS_PER_USER_SKEW", "WRITE_RATIO", "LOAD_BATCH_SIZE",
              "USER_LOOKUP_RATIO", "EMAIL_LOOKUP_RATIO",
              "PROFILE_PRESENT_RATIO"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_missing_uri_fails_fast(monkeypatch):
    _clean(monkeypatch)
    with pytest.raises(ConfigError, match="MONGODB_URI"):
        load_config(env_file=NO_ENV)


def test_uri_not_required_for_offline_tools(monkeypatch):
    _clean(monkeypatch)
    cfg = load_config(env_file=NO_ENV, require_uri=False, quiet=True)
    assert cfg.total_document_count == 16_000_000


def test_ratio_bounds(monkeypatch):
    _clean(monkeypatch, MONGODB_URI="mongodb://x", WRITE_RATIO="1.5")
    with pytest.raises(ConfigError, match="WRITE_RATIO"):
        load_config(env_file=NO_ENV, quiet=True)


def test_lookup_ratios_must_not_exceed_one(monkeypatch):
    _clean(monkeypatch, MONGODB_URI="mongodb://x",
           USER_LOOKUP_RATIO="0.6", EMAIL_LOOKUP_RATIO="0.5")
    with pytest.raises(ConfigError, match="LOOKUP_RATIO"):
        load_config(env_file=NO_ENV, quiet=True)


def test_pareto_shape_bound(monkeypatch):
    _clean(monkeypatch, MONGODB_URI="mongodb://x", SESSIONS_PER_USER_SKEW="1.0")
    with pytest.raises(ConfigError, match="SKEW"):
        load_config(env_file=NO_ENV, quiet=True)


def test_synthetic_user_count_bounded(monkeypatch):
    _clean(monkeypatch, MONGODB_URI="mongodb://x",
           TOTAL_DOCUMENT_COUNT="100", SYNTHETIC_USER_COUNT="1000")
    with pytest.raises(ConfigError, match="SYNTHETIC_USER_COUNT"):
        load_config(env_file=NO_ENV, quiet=True)


def test_positive_integer_knobs(monkeypatch):
    _clean(monkeypatch, MONGODB_URI="mongodb://x", LOAD_BATCH_SIZE="0")
    with pytest.raises(ConfigError, match="LOAD_BATCH_SIZE"):
        load_config(env_file=NO_ENV, quiet=True)
