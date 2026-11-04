"""Privileged lifecycle operations for an already registered ERP API credential.

Operator-only. Never accepts keys or secrets on the command line. Rotating a key
invalidates its predecessor immediately, while revocation disables the client.
"""
from __future__ import annotations

import argparse
import secrets

from .config import load_settings
from .db import connection, init_db, utc_now
from .security import hash_api_key
from .upgrade import migrate


def rotate_api_key(conn, system: str) -> str:
    """Caller owns transaction; rotate a currently active client only."""
    if not isinstance(system, str) or not system.strip():
        raise ValueError("A registered system name is required")
    row = conn.execute(
        "SELECT active FROM api_clients WHERE system_name=?", (system,)
    ).fetchone()
    if row is None or row["active"] != 1:
        raise ValueError("Only an active integration can rotate its API key")
    key = "wha_" + secrets.token_urlsafe(40)
    now = utc_now()
    cursor = conn.execute(
        "UPDATE api_clients SET api_key_hash=?,updated_at=? "
        "WHERE system_name=? AND active=1",
        (hash_api_key(key), now, system),
    )
    if cursor.rowcount != 1:
        raise ValueError("API client changed during rotation")
    conn.execute(
        "INSERT INTO integration_credential_events "
        "(system_name,event_type,created_at) VALUES (?,?,?)",
        (system, "api_key_rotated", now),
    )
    return key


def revoke_client(conn, system: str) -> None:
    """Disable a client; existing tasks are not silently deleted."""
    if not isinstance(system, str) or not system.strip():
        raise ValueError("A registered system name is required")
    now = utc_now()
    cursor = conn.execute(
        "UPDATE api_clients SET active=0,updated_at=? "
        "WHERE system_name=? AND active=1", (now, system),
    )
    if cursor.rowcount != 1:
        raise ValueError("Only an active integration can be revoked")
    conn.execute(
        "INSERT INTO integration_credential_events "
        "(system_name,event_type,created_at) VALUES (?,?,?)",
        (system, "client_revoked", now),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", required=True)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--rotate-key", action="store_true")
    actions.add_argument("--revoke", action="store_true")
    parser.add_argument("--production-approved", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    if settings.app_env == "production" and not args.production_approved:
        parser.error("Production credential changes require --production-approved")
    init_db(settings)
    migrate(settings)
    try:
        with connection(settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if args.rotate_key:
                new_key = rotate_api_key(conn, args.system)
            else:
                revoke_client(conn, args.system)
                new_key = None
    except ValueError as exc:
        parser.error(str(exc))
    if new_key is not None:
        print("New API key (save securely now):", new_key)
        print("Previous API key is invalidated immediately.")
    else:
        print("ERP API client revoked:", args.system)


if __name__ == "__main__":
    main()
