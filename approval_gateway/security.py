from __future__ import annotations

import hashlib
import hmac
import re
import secrets

# Accept SHA-256 digests in their canonical lowercase hexadecimal form.
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def hash_api_key(api_key: str) -> str:
    # Hash the exact credential; whitespace is not silently removed.
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("API key must be a non-empty string.")

    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def verify_api_key(api_key: str, expected_hash: str) -> bool:
    # Reject malformed credentials before comparing their hashes.
    if (
        not isinstance(api_key, str)
        or not api_key.strip()
        or not isinstance(expected_hash, str)
        or _SHA256_PATTERN.fullmatch(expected_hash) is None
    ):
        return False

    return secrets.compare_digest(
        hash_api_key(api_key),
        expected_hash,
    )


def verify_meta_signature(
    raw_body: bytes,
    header_signature: str | None,
    app_secret: str,
) -> bool:
    # Missing secrets or malformed signatures must fail verification.
    if (
        not isinstance(raw_body, bytes)
        or not isinstance(app_secret, str)
        or not app_secret.strip()
        or not isinstance(header_signature, str)
        or not header_signature.startswith("sha256=")
    ):
        return False

    received_digest = header_signature[len("sha256=") :]

    if _SHA256_PATTERN.fullmatch(received_digest) is None:
        return False

    # Sign the original request bytes before JSON parsing or reformatting.
    expected_digest = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return secrets.compare_digest(
        expected_digest,
        received_digest,
    )


def normalize_phone(phone_number: str) -> str:
    # Require an international number; country conversion belongs to the adapter.
    if not isinstance(phone_number, str):
        raise ValueError("Phone number must be a string.")

    value = phone_number.strip()

    if not value:
        raise ValueError("Phone number cannot be empty.")

    # Remove display separators while rejecting letters and other characters.
    if re.fullmatch(r"\+?[0-9 ()-]+", value) is None:
        raise ValueError("Phone number contains unsupported characters.")

    digits = re.sub(r"[ ()-]", "", value.removeprefix("+"))

    # Store country-code-prefixed digits without the display '+'.
    if digits.startswith("0") or not 1 <= len(digits) <= 15:
        raise ValueError(
            "Phone number must include a country code "
            "and contain at most 15 digits."
        )

    return digits
