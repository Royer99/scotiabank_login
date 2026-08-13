import json
from datetime import datetime, timezone

from conftest import make_cfg
from model import EPOCH_ANCHOR_MS
from generate import build_session_doc, iter_session_docs

ANCHOR = datetime.fromtimestamp(EPOCH_ANCHOR_MS / 1000, tz=timezone.utc)


def test_documents_deterministic_under_seed(cfg):
    a = build_session_doc(cfg, 7)
    b = build_session_doc(make_cfg(), 7)
    assert a == b
    assert build_session_doc(make_cfg(random_seed=43), 7) != a


def test_worker_partitioning_yields_identical_dataset(cfg):
    """Splitting a range across N workers must not change any document."""
    whole = list(iter_session_docs(cfg, 0, 60))
    split = (list(iter_session_docs(cfg, 0, 17))
             + list(iter_session_docs(cfg, 17, 41))
             + list(iter_session_docs(cfg, 41, 60)))
    assert whole == split


def test_access_token_is_rs256_shaped_jwt(cfg):
    """3-segment RS256 JWT: real header + payload, 342-char random signature,
    FAKE- kid so it cannot be mistaken for a real customer token."""
    import base64

    def b64(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    doc = build_session_doc(cfg, 5)
    tok = doc["accessToken"]
    parts = tok.split(".")
    assert len(parts) == 3
    header = json.loads(b64(parts[0]))
    assert header["alg"] == "RS256" and header["typ"] == "JWT"
    assert header["kid"].startswith("FAKE-")
    payload = json.loads(b64(parts[1]))
    assert {"iss", "sub", "aud", "iat", "exp", "scope", "azp",
            "https://scotiabank.com/customerId",
            "https://scotiabank.com/segment",
            "https://scotiabank.com/channel",
            "https://scotiabank.com/authMethod",
            "https://scotiabank.com/mfaVerified"} <= set(payload)
    assert len(parts[2]) == 342  # 2048-bit RSA signature length, random bytes
    assert payload["azp"] == doc["userId"]
    assert payload["https://scotiabank.com/customerId"] == doc["userId"]


def test_profile_present_ratio_matches_config(cfg):
    """profile block appears at ~PROFILE_PRESENT_RATIO across the range,
    with an RFC 2606 reserved-domain email and stable fields."""
    import re

    with_profile = 0
    for i in range(400):
        doc = build_session_doc(cfg, i)
        if "profile" in doc:
            with_profile += 1
            prof = doc["profile"]
            assert set(prof) == {"email", "emailVerified", "firstName",
                                 "lastName", "phoneNumber", "preferredLanguage"}
            assert re.fullmatch(
                r"[a-z]+\.[a-z]+\d{3}@(example\.(com|net|org)|mail\.example\.ca)",
                prof["email"]), prof["email"]
            assert re.fullmatch(r"\+1416\d{7}", prof["phoneNumber"])
            assert prof["preferredLanguage"] in ("en-CA", "fr-CA")
    observed = with_profile / 400
    assert abs(observed - cfg.profile_present_ratio) < 0.06


def test_payload_size_near_target(cfg):
    sizes = [len(json.dumps({k: v for k, v in build_session_doc(cfg, i).items()
                             if k not in ("createdAt", "expiresAt", "lastUsedDate")},
                            default=str, separators=(",", ":")))
             for i in range(300)]
    mean = sum(sizes) / len(sizes)
    assert cfg.payload_target_bytes * 0.75 < mean < cfg.payload_target_bytes * 1.5


def test_expired_ratio_within_tolerance(cfg):
    docs = [build_session_doc(cfg, i) for i in range(cfg.total_document_count)]
    expired = sum(1 for d in docs if d["expiresAt"] < ANCHOR) / len(docs)
    assert abs(expired - cfg.expired_session_ratio) < 0.04
    with_ttl = sum(1 for d in docs if "ttlSeconds" in d) / len(docs)
    assert with_ttl > 0.95  # ~2% intentionally have no TTL field


def test_recency_ordering(cfg):
    """Higher index == newer session; tail sampling equals recency sampling."""
    old = build_session_doc(cfg, 0)["expiresAt"]
    new = build_session_doc(cfg, cfg.total_document_count - 1)["expiresAt"]
    assert new > old


def test_channel_and_auth_method_are_from_known_sets(cfg):
    from model import AUTH_METHODS, CHANNELS
    channels = {c for c, _ in CHANNELS}
    methods = {m for m, _ in AUTH_METHODS}
    for i in range(500):
        d = build_session_doc(cfg, i)
        assert d["channel"] in channels
        assert d["authMethod"] in methods
        # deviceInfo.channel mirrors the top-level channel for the demo
        assert d["deviceInfo"]["channel"] == d["channel"]


def test_ids_are_unique_within_range(cfg):
    ids = [build_session_doc(cfg, i)["_id"] for i in range(2000)]
    assert len(set(ids)) == len(ids)
