"""Reference verification for external ERP webhook receivers.

The ERP application must additionally perform its own transaction, authorizations,
deduplication and business action. This module only authenticates transport.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time


def verify_gateway_webhook(
    raw_body: bytes,
    timestamp: str | None,
    signature: str | None,
    callback_secret: str,
    *,
    now: int | None = None,
) -> bool:
    """Validate an unchanged raw request body against the gateway HMAC contract."""
    if (
        not isinstance(raw_body, bytes)
        or not isinstance(timestamp, str)
        or not re.fullmatch(r"[1-9][0-9]{0,11}", timestamp)
        or not isinstance(signature, str)
        or not re.fullmatch(r"[a-f0-9]{64}", signature)
        or not isinstance(callback_secret, str)
        or len(callback_secret) < 32
    ):
        return False
    current = int(time.time()) if now is None else now
    if type(current) is not int:
        return False
    delta = current - int(timestamp)
    if delta > 300 or delta < -30:
        return False
    expected = hmac.new(
        callback_secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
