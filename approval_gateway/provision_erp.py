"""Provision an ERP's gateway credentials and callback atomically.

Operator-only CLI. Does not register/activate a workflow adapter or identity.
The generated credentials are printed exactly once: capture them securely.
"""
from __future__ import annotations

import argparse
import secrets
from urllib.parse import urlsplit

from .config import load_settings
from .db import connection, init_db
from .repository import create_api_client
from .upgrade import migrate


def validate_callback_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("Callback URL is required")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise ValueError("Invalid callback URL") from None
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
        raise ValueError("Callback must be an HTTPS URL without query or credentials")
    return value


def provision(conn, system: str, callback_url: str) -> tuple[str, str]:
    if (not isinstance(system, str) or not system.isascii()
            or not 1 <= len(system) <= 80
            or not all(c.islower() or c.isdigit() or c in "_-" for c in system)
            or not system[0].isalpha()):
        raise ValueError("System must be a lowercase slug")
    callback_url = validate_callback_url(callback_url)
    conn.execute("BEGIN IMMEDIATE")
    if conn.execute(
        "SELECT 1 FROM api_clients WHERE system_name = ?", (system,)
    ).fetchone() or conn.execute(
        "SELECT 1 FROM integrations WHERE system_name = ?", (system,)
    ).fetchone():
        raise ValueError("System exists; use separately reviewed credential rotation")
    api_key = "wha_" + secrets.token_urlsafe(40)
    callback_secret = secrets.token_urlsafe(48)
    create_api_client(conn, system, api_key)
    conn.execute(
        "INSERT INTO integrations (system_name, callback_url, callback_secret) "
        "VALUES (?, ?, ?)",
        (system, callback_url, callback_secret),
    )
    return api_key, callback_secret


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", required=True)
    parser.add_argument("--callback-url", required=True)
    args = parser.parse_args()
    settings = load_settings()
    if settings.app_env == "production":
        # Require explicit intent to avoid accidental production credential creation.
        parser.add_argument("--production-approved", action="store_true")
        args = parser.parse_args()
        if not args.production_approved:
            parser.error("Production provisioning requires --production-approved")
    init_db(settings)
    migrate(settings)
    try:
        with connection(settings) as conn:
            key, secret = provision(conn, args.system, args.callback_url)
    except ValueError as exc:
        parser.error(str(exc))
    print("System:", args.system)
    print("Gateway API key (save now):", key)
    print("Callback signing secret (save now):", secret)
    print("Verification endpoint, verified actor bindings and adapter registration "
          "are NOT provisioned. Integration is not live until separately configured.")


if __name__ == "__main__":
    main()
