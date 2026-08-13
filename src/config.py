"""Environment-driven configuration. Loaded once, validated, fail fast."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(ValueError):
    """Raised when the environment is missing or inconsistent."""


def _req(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise ConfigError(f"{name} is required (set it in .env)")
    return v


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Config:
    """All knobs for the generator, loader, and Locust harness."""

    mongodb_uri: str
    mongodb_db: str
    mongodb_collection: str
    max_pool_size: int
    read_preference: str
    write_concern: int

    total_document_count: int
    synthetic_user_count: int
    sessions_per_user_skew: float
    profile_present_ratio: float
    payload_target_bytes: int
    session_ttl_days: int
    expired_session_ratio: float
    random_seed: int
    enable_ttl_index: bool

    generate_workers: int
    load_batch_size: int
    load_checkpoint_path: str

    hot_set_ratio: float
    hot_access_share: float

    locust_users: int
    locust_spawn_rate: int
    locust_run_time: str
    target_read_rps: int
    write_ratio: float
    user_lookup_ratio: float
    email_lookup_ratio: float
    user_sessions_limit: int
    read_latency_slo_ms: float

    @property
    def hot_set_size(self) -> int:
        """Number of sessions in the hot (recently created) set."""
        return max(1, int(self.total_document_count * self.hot_set_ratio))


def load_config(env_file: str | os.PathLike[str] | None = None, require_uri: bool = True,
                quiet: bool = False) -> Config:
    """Load .env (or *env_file*), validate, return an immutable Config.

    require_uri=False lets generator dry-runs and tests skip the MongoDB URI.
    *quiet* suppresses informational notes (used by loader worker processes).
    """
    load_dotenv(env_file or Path(".env"))

    cfg = Config(
        mongodb_uri=_req("MONGODB_URI") if require_uri else os.environ.get("MONGODB_URI", ""),
        mongodb_db=os.environ.get("MONGODB_DB", "scotiabank_login"),
        mongodb_collection=os.environ.get("MONGODB_COLLECTION", "sessions"),
        max_pool_size=_int("MONGODB_MAX_POOL_SIZE", 500),
        read_preference=os.environ.get("MONGODB_READ_PREFERENCE", "primary"),
        write_concern=_int("MONGODB_WRITE_CONCERN", 1),
        total_document_count=_int("TOTAL_DOCUMENT_COUNT", 16_000_000),
        synthetic_user_count=_int("SYNTHETIC_USER_COUNT", 3_200_000),
        sessions_per_user_skew=_float("SESSIONS_PER_USER_SKEW", 1.4),
        profile_present_ratio=_float("PROFILE_PRESENT_RATIO", 0.85),
        payload_target_bytes=_int("PAYLOAD_TARGET_BYTES", 2800),
        session_ttl_days=_int("SESSION_TTL_DAYS", 1),
        expired_session_ratio=_float("EXPIRED_SESSION_RATIO", 0.10),
        random_seed=_int("RANDOM_SEED", 42),
        enable_ttl_index=_bool("ENABLE_TTL_INDEX", False),
        generate_workers=_int("GENERATE_WORKERS", 8),
        load_batch_size=_int("LOAD_BATCH_SIZE", 1000),
        load_checkpoint_path=os.environ.get("LOAD_CHECKPOINT_PATH", "./.load_checkpoint.json"),
        hot_set_ratio=_float("HOT_SET_RATIO", 0.05),
        hot_access_share=_float("HOT_ACCESS_SHARE", 0.80),
        locust_users=_int("LOCUST_USERS", 500),
        locust_spawn_rate=_int("LOCUST_SPAWN_RATE", 50),
        locust_run_time=os.environ.get("LOCUST_RUN_TIME", "10m"),
        target_read_rps=_int("TARGET_READ_RPS", 2000),
        write_ratio=_float("WRITE_RATIO", 0.10),
        user_lookup_ratio=_float("USER_LOOKUP_RATIO", 0.15),
        email_lookup_ratio=_float("EMAIL_LOOKUP_RATIO", 0.05),
        user_sessions_limit=_int("USER_SESSIONS_LIMIT", 25),
        read_latency_slo_ms=_float("READ_LATENCY_SLO_MS", 10),
    )
    _validate(cfg, quiet=quiet)
    return cfg


def _validate(cfg: Config, quiet: bool = False) -> None:
    err: list[str] = []
    if cfg.total_document_count < 10:
        err.append("TOTAL_DOCUMENT_COUNT must be at least 10")
    for name in ("expired_session_ratio", "hot_set_ratio", "hot_access_share",
                 "write_ratio", "user_lookup_ratio", "email_lookup_ratio",
                 "profile_present_ratio"):
        v = getattr(cfg, name)
        if not 0.0 <= v <= 1.0:
            err.append(f"{name.upper()} must be in [0, 1], got {v}")
    if cfg.user_lookup_ratio + cfg.email_lookup_ratio > 1.0:
        err.append("USER_LOOKUP_RATIO + EMAIL_LOOKUP_RATIO must not exceed 1.0")
    if cfg.sessions_per_user_skew <= 1.0:
        err.append("SESSIONS_PER_USER_SKEW must be > 1.0 (Pareto shape)")
    if cfg.synthetic_user_count < 1 or cfg.synthetic_user_count > cfg.total_document_count:
        err.append("SYNTHETIC_USER_COUNT must be in [1, TOTAL_DOCUMENT_COUNT]")
    for name in ("generate_workers", "load_batch_size", "locust_users",
                 "payload_target_bytes", "max_pool_size", "user_sessions_limit"):
        if getattr(cfg, name) < 1:
            err.append(f"{name.upper()} must be >= 1")
    if err:
        raise ConfigError("; ".join(err))
    if not quiet:
        ratio = cfg.total_document_count / cfg.synthetic_user_count
        print(f"[config] average sessions/user = {ratio:.1f} "
              f"({cfg.total_document_count:,} docs / {cfg.synthetic_user_count:,} users)")
        # Print the knobs that actually shape the read distribution — makes it
        # obvious at startup whether an .env change took effect.
        print(f"[config] access pattern: HOT_ACCESS_SHARE={cfg.hot_access_share:g} "
              f"HOT_SET_RATIO={cfg.hot_set_ratio:g}  "
              f"(reads: {cfg.hot_access_share*100:.0f}% into most recent "
              f"{cfg.hot_set_ratio*100:.0f}% of sessions)")
        print(f"[config] workload: LOCUST_USERS={cfg.locust_users} "
              f"TARGET_READ_RPS={cfg.target_read_rps} "
              f"WRITE_RATIO={cfg.write_ratio:g}")
