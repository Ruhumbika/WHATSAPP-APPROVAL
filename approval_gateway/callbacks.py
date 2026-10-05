from __future__ import annotations

import hashlib
import hmac
import re
import time

DEFAULT_MAX_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30

_SIGNATURE_PATTERN = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP_PATTERN = re.compile(r"[1-9][0-9]{0,11}")


def verify_callback(
    raw_body: bytes,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> bool:
    # Reject malformed inputs before computing the signature.
    if (
        not isinstance(raw_body, bytes)
        or not isinstance(secret, str)
        or not secret.strip()
        or not isinstance(timestamp, str)
        or not isinstance(signature, str)
        or type(max_age_seconds) is not int
        or max_age_seconds <= 0
    ):
        return False

    if (
        _TIMESTAMP_PATTERN.fullmatch(timestamp) is None
        or _SIGNATURE_PATTERN.fullmatch(signature) is None
    ):
        return False

    # Limit replay time while allowing a small amount of clock skew.
    age_seconds = int(time.time()) - int(timestamp)

    if age_seconds > max_age_seconds or age_seconds < -MAX_FUTURE_SKEW_SECONDS:
        return False

    # Authenticate the exact timestamp header and original request bytes.
    signed_content = timestamp.encode("ascii") + b"." + raw_body

    expected_signature = hmac.new(
        secret.encode("utf-8"),
        signed_content,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(
        expected_signature,
        signature,
    )
