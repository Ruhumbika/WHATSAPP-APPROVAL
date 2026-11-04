"""Two-phase callback signing-secret rotation for gateway-owned ERP webhooks.

1. Stage a future secret and deliver it securely to the ERP developer.
2. ERP receiver accepts both current and future secrets.
3. Operator activates the staged secret in the gateway.
4. ERP developer retires the old secret after confirming signed callbacks.
No Meta/WhatsApp settings or ERP source access are needed.
"""
from __future__ import annotations

import argparse
import secrets

from .config import load_settings
from .db import connection, utc_now


def _current(conn, system: str):
    if not isinstance(system, str) or not system.strip():
        raise ValueError("A registered ERP system is required")
    row = conn.execute(
        """SELECT i.callback_secret,c.active FROM integrations i
           JOIN api_clients c ON c.system_name=i.system_name
           WHERE i.system_name=?""", (system,),
    ).fetchone()
    if row is None or row["active"] != 1:
        raise ValueError("An active, registered ERP integration is required")
    return row


def stage_callback_secret(conn, system: str) -> str:
    _current(conn, system)
    pending = conn.execute(
        "SELECT 1 FROM integration_callback_secret_staging WHERE system_name=?",
        (system,),
    ).fetchone()
    if pending:
        raise ValueError("A callback signing-secret change is already staged")
    secret = secrets.token_urlsafe(48)
    now = utc_now()
    conn.execute(
        """INSERT INTO integration_callback_secret_staging
           (system_name,next_secret,staged_at) VALUES (?,?,?)""",
        (system, secret, now),
    )
    conn.execute(
        """INSERT INTO integration_callback_secret_events
           (system_name,event_type,created_at) VALUES (?,'secret_staged',?)""",
        (system, now),
    )
    return secret


def activate_callback_secret(conn, system: str) -> None:
    current = _current(conn, system)
    pending = conn.execute(
        "SELECT next_secret FROM integration_callback_secret_staging WHERE system_name=?",
        (system,),
    ).fetchone()
    if pending is None:
        raise ValueError("No staged callback secret exists")
    updated = conn.execute(
        """UPDATE integrations SET callback_secret=?
           WHERE system_name=? AND callback_secret=?""",
        (pending["next_secret"], system, current["callback_secret"]),
    )
    if updated.rowcount != 1:
        raise ValueError("Callback secret changed concurrently")
    conn.execute(
        "DELETE FROM integration_callback_secret_staging WHERE system_name=?",
        (system,),
    )
    conn.execute(
        """INSERT INTO integration_callback_secret_events
           (system_name,event_type,created_at) VALUES (?,'secret_activated',?)""",
        (system, utc_now()),
    )


def abort_callback_secret(conn, system: str) -> None:
    _current(conn, system)
    cursor = conn.execute(
        "DELETE FROM integration_callback_secret_staging WHERE system_name=?",
        (system,),
    )
    if cursor.rowcount != 1:
        raise ValueError("No staged callback secret exists")
    conn.execute(
        """INSERT INTO integration_callback_secret_events
           (system_name,event_type,created_at) VALUES (?,'secret_aborted',?)""",
        (system, utc_now()),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--stage", action="store_true")
    action.add_argument("--activate", action="store_true")
    action.add_argument("--abort", action="store_true")
    parser.add_argument("--production-approved", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    if settings.app_env == "production" and not args.production_approved:
        parser.error("Production secret changes require --production-approved")
    try:
        with connection(settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if args.stage:
                secret = stage_callback_secret(conn, args.system)
            elif args.activate:
                activate_callback_secret(conn, args.system)
                secret = None
            else:
                abort_callback_secret(conn, args.system)
                secret = None
    except ValueError as exc:
        parser.error(str(exc))
    if args.stage:
        print("Future callback signing secret (save securely now):", secret)
        print("Send only to authorized ERP developer; do not activate until")
        print("their receiver can verify BOTH old and new secret.")
    elif args.activate:
        print("Staged callback secret activated:", args.system)
    else:
        print("Staged callback secret abandoned; current secret unchanged:", args.system)


if __name__ == "__main__":
    main()
