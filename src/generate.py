"""Synthetic data generator emitting the session document shape.

Every document derives purely from ``(RANDOM_SEED, kind, index)``, so output
is byte-identical for a given seed regardless of worker count or generation
order. No real customer data appears anywhere: emails use RFC 2606 reserved
domains, phone/customer numbers are random, JWT signatures are random bytes,
and the signing kid is ``FAKE-`` prefixed.

Standalone usage:
    python src/generate.py --dry-run --limit 3
    python src/generate.py --jsonl out.jsonl --limit 10000
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import Config, load_config  # noqa: E402
from model import (  # noqa: E402
    AUTH_METHODS,
    CHANNELS,
    SEGMENTS,
    _AM_TOTAL,
    _CH_TOTAL,
    doc_rng,
    owner_of_session,
    session_created_ms,
    session_id,
    session_uuid,
    shard_hint,
    user_id,
    weighted_pick,
)

_BROWSERS = ["Chrome", "Safari", "Firefox", "Edge", "Samsung Internet", "Chrome Mobile"]
_OS = [("iOS", ["17.5.1", "18.0", "18.1.1"]), ("Android", ["13", "14", "15"]),
       ("Mac OS X", ["14.5", "15.1"]), ("Windows", ["10", "11"])]
_MODELS = ["iPhone14,5", "iPhone15,3", "SM-G991B", "SM-A546E", "Pixel 8",
           "Moto G54", "iPad13,4", "Generic Desktop"]
_APP_VERSIONS = ["7.24.1", "7.25.0", "7.25.1", "7.26.0"]
_SCOPES = "openid profile email accounts:read payments:read"

# Curated ASCII names (Canadian retail banking realistic mix). Not faker:
# hash-seeded random.Random with curated lists is faster and version-stable
# for the determinism tests.
_FIRST_NAMES = ["Michael", "Emma", "Liam", "Olivia", "Noah", "Sophia", "Ethan",
                "Ava", "Lucas", "Isabella", "William", "Mia", "James", "Zoe",
                "Benjamin", "Chloe", "Jacob", "Amelia", "Alexander", "Charlotte",
                "Priya", "Amir", "Wei", "Fatima", "Diego", "Nora", "Rohan",
                "Maya", "Omar", "Layla"]
_LAST_NAMES = ["Smith", "Brown", "Tremblay", "Roy", "MacDonald", "Nguyen",
               "Patel", "Singh", "Chen", "Wang", "Kim", "Garcia", "Martin",
               "Wilson", "Taylor", "Anderson", "Thompson", "Lee", "Khan",
               "Ahmed", "Rossi", "Dubois", "Cohen", "OConnor", "Bergeron"]
# RFC 2606 reserved domains only, so no real address can appear.
_EMAIL_DOMAINS = ["example.com", "example.net", "example.org", "mail.example.ca"]


def _fake_ip(rng: Any) -> str:
    # RFC 5737 TEST-NET ranges only; never a real customer IP.
    net = rng.choice(["192.0.2", "198.51.100", "203.0.113"])
    return f"{net}.{rng.randrange(1, 255)}"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _signing_kid(seed: int) -> str:
    """One fake signing-key id per dataset (kids are per key, not per token)."""
    import hashlib
    return "FAKE-" + hashlib.blake2b(f"kid:{seed}".encode(), digest_size=8).hexdigest()


def _fake_jwt(rng: Any, payload: dict[str, Any], kid: str) -> str:
    """RS256-shaped JWT: real base64url header + payload, signature is 256
    random bytes (encoded to a 342-char segment, the length of a 2048-bit
    RSA signature). Structurally faithful, cryptographically invalid on
    purpose — the ``FAKE-`` kid marks it as synthetic."""
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    return ".".join((
        _b64url(json.dumps(header, separators=(",", ":")).encode()),
        _b64url(json.dumps(payload, separators=(",", ":")).encode()),
        _b64url(rng.randbytes(256)),
    ))


def _payload_size_target(cfg: Config, rng: Any) -> int:
    # Right-skewed: mobile-app sessions with a stored profile run larger than
    # bare ATM sessions. p50 ~ target, mean ~ 1.15x target.
    return int(cfg.payload_target_bytes * min(rng.lognormvariate(-0.05, 0.35), 2.0))


def build_session_doc(cfg: Config, i: int) -> dict[str, Any]:
    """Session document for global index *i*.

    The document mirrors what an Auth-service session cache would carry: the
    issued access token (JWT), the granted scope, device context, and (for
    non-anonymous sessions) a small profile block used to render greetings /
    support lookups. The ``_id`` is the session UUID; ``userId`` is the
    Scotiabank customer number.
    """
    rng = doc_rng(cfg.random_seed, "sess", i)
    sid = session_id(cfg.random_seed, i)
    uuid = session_uuid(cfg.random_seed, i)

    owner_idx = owner_of_session(cfg.random_seed, i, cfg.synthetic_user_count)
    cust = user_id(cfg.random_seed, owner_idx)
    segment = SEGMENTS[owner_idx % len(SEGMENTS)]

    channel = weighted_pick(rng, CHANNELS, _CH_TOTAL)
    auth_method = weighted_pick(rng, AUTH_METHODS, _AM_TOTAL)

    created_ms = session_created_ms(cfg.random_seed, i, cfg.total_document_count,
                                    cfg.session_ttl_days, cfg.expired_session_ratio)
    expires_ms = created_ms + cfg.session_ttl_days * 86_400_000
    last_used_ms = created_ms + rng.randrange(0, max(1, cfg.session_ttl_days * 86_400_000 // 4))

    os_name, versions = rng.choice(_OS)
    is_mobile = channel == "mobile_app"

    device_info = {
        "channel": channel,
        "model": rng.choice(_MODELS) if is_mobile else "Generic Desktop",
        "browser": None if is_mobile else rng.choice(_BROWSERS),
        "os": os_name,
        "osVersion": rng.choice(versions),
        "appVersion": rng.choice(_APP_VERSIONS) if is_mobile else None,
        "ip": _fake_ip(rng),
        "userAgent": (
            f"ScotiaMobile/{rng.choice(_APP_VERSIONS)} "
            f"({os_name} {rng.choice(versions)})" if is_mobile
            else f"Mozilla/5.0 ({os_name}) {rng.choice(_BROWSERS)}/1.0"
        ),
    }

    iat = created_ms // 1000
    exp = iat + cfg.session_ttl_days * 86_400
    jwt_claims: dict[str, Any] = {
        "iss": "https://auth.qa.scotia.example/",
        "sub": f"auth0|{rng.getrandbits(96):024x}",
        "aud": ["https://api.qa.scotia.example/",
                "https://auth.qa.scotia.example/userinfo"],
        "iat": iat,
        "exp": exp,
        "scope": _SCOPES,
        "azp": cust,
        "https://scotiabank.com/customerId": cust,
        "https://scotiabank.com/segment": segment,
        "https://scotiabank.com/channel": channel,
        "https://scotiabank.com/authMethod": auth_method,
        "https://scotiabank.com/mfaVerified": auth_method in ("biometric", "mfa_push"),
    }

    doc: dict[str, Any] = {
        "_id": sid,
        "userId": cust,
        "segment": segment,
        "channel": channel,
        "authMethod": auth_method,
        "scope": _SCOPES,
        "tokenType": "Bearer",
        "expiresIn": cfg.session_ttl_days * 86_400,
        "accessToken": _fake_jwt(rng, jwt_claims, _signing_kid(cfg.random_seed)),
        "deviceInfo": device_info,
        "createdAt": datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc),
        "expiresAt": datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc),
        "lastUsedDate": datetime.fromtimestamp(last_used_ms / 1000, tz=timezone.utc),
        "shard": shard_hint(cfg.random_seed, i),
    }

    if rng.random() < cfg.profile_present_ratio:
        first = rng.choice(_FIRST_NAMES)
        last = rng.choice(_LAST_NAMES)
        # Deterministic-ish email: name + 3-digit suffix keeps duplicates
        # rare across ~16M docs without needing a global uniqueness check.
        suffix = rng.randrange(1000)
        doc["profile"] = {
            "email": f"{first.lower()}.{last.lower()}{suffix:03d}@{rng.choice(_EMAIL_DOMAINS)}",
            "emailVerified": rng.random() < 0.95,
            "firstName": first,
            "lastName": last,
            "phoneNumber": f"+1416{rng.randrange(10**7):07d}",
            "preferredLanguage": rng.choice(["en-CA", "fr-CA"]),
        }

    if rng.random() >= 0.02:  # ~2% of sessions are PERSISTed (no TTL field)
        doc["ttlSeconds"] = cfg.session_ttl_days * 86_400

    base = len(json.dumps(_to_jsonable(doc), separators=(",", ":")))
    pad = _payload_size_target(cfg, rng) - base - 20
    if pad > 0:
        # base64 of random bytes: incompressible-ish so storage figures stay honest.
        doc["padding"] = base64.b64encode(rng.randbytes(pad * 3 // 4)).decode()

    return doc


def iter_session_docs(cfg: Config, start: int, end: int) -> Iterator[dict[str, Any]]:
    """Stream session documents for global indices ``[start, end)``."""
    for i in range(start, end):
        yield build_session_doc(cfg, i)


def _to_jsonable(doc: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in doc.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true",
                   help="print sample documents as JSON and exit")
    p.add_argument("--limit", type=int, default=5,
                   help="number of documents to emit")
    p.add_argument("--jsonl", metavar="PATH",
                   help="write --limit docs as JSONL to PATH; never use this "
                        "for the full dataset")
    args = p.parse_args()
    cfg = load_config(require_uri=False)

    if args.dry_run:
        for d in iter_session_docs(cfg, 0, min(args.limit, cfg.total_document_count)):
            print(json.dumps(_to_jsonable(d), indent=2))
        return

    if args.jsonl:
        n = min(args.limit, cfg.total_document_count)
        with open(args.jsonl, "w") as f:
            for d in iter_session_docs(cfg, 0, n):
                f.write(json.dumps(_to_jsonable(d), separators=(",", ":")) + "\n")
        print(f"wrote {n} session docs to {args.jsonl}")
        return

    p.error("choose --dry-run or --jsonl (full-scale loading is src/load.py's job)")


if __name__ == "__main__":
    main()
