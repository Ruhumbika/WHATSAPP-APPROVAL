"""Privileged lifecycle operations for an already registered ERP API credential.

Operator-only. Never accepts keys or secrets on the command line. Rotating a key
invalidates its predecessor immediately, while revocation disables the client.
"""
from __future__ import annotations

import argparse
import secrets
from datetime import datetime, timedelta, timezone

from .config import load_settings
from .db import connection, init_db, utc_now
from .security import hash_api_key
from .upgrade import migrate


def rotate_api_key(conn, system: str, overlap_minutes: int = 0) -> str:
    """Caller owns transaction; rotate a currently active client only."""
    if not isinstance(system, str) or not system.strip():
        raise ValueError("A registered system name is required")
    if type(overlap_minutes) is not int or not 0 <= overlap_minutes <= 1440:
        raise ValueError("Rotation overlap must be between 0 and 1440 minutes")
    row = conn.execute(
        "SELECT active,api_key_hash FROM api_clients WHERE system_name=?", (system,)
    ).fetchone()
    if row is None or row["active"] != 1:
        raise ValueError("Only an active integration can rotate its API key")
    existing = conn.execute(
        "SELECT 1 FROM api_client_key_overlaps WHERE system_name=? AND expires_at>?",
        (system, utc_now()),
    ).fetchone()
    if existing:
        raise ValueError("Finish or revoke the previous key overlap before another rotation")
    key = "wha_" + secrets.token_urlsafe(40)
    now = utc_now()
    # A single previous key can overlap. A new rotation invalidates expired
    # legacy overlap data. The new key remains the canonical credential.
    conn.execute("DELETE FROM api_client_key_overlaps WHERE system_name=?", (system,))
    if overlap_minutes:
        expires_at = (datetime.now(timezone.utc) + timedelta(
            minutes=overlap_minutes
        )).replace(microsecond=0).isoformat()
        conn.execute(
            """INSERT INTO api_client_key_overlaps
               (system_name,previous_key_hash,expires_at,created_at)
               VALUES (?,?,?,?)""",
            (system, row["api_key_hash"], expires_at, now),
        )
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


def finish_rotation(conn, system: str) -> None:
    """Immediately retire the previous credential without disabling the new key."""
    if not isinstance(system, str) or not system.strip():
        raise ValueError("A registered system name is required")
    active = conn.execute(
        "SELECT 1 FROM api_clients WHERE system_name=? AND active=1", (system,)
    ).fetchone()
    if active is None:
        raise ValueError("Only active integrations can finish a rotation")
    deleted = conn.execute(
        "DELETE FROM api_client_key_overlaps WHERE system_name=?", (system,)
    )
    if deleted.rowcount != 1:
        raise ValueError("No previous key overlap exists")
    conn.execute(
        """INSERT INTO integration_credential_events
           (system_name,event_type,created_at)
           VALUES (?,'api_key_overlap_finished',?)""",
        (system, utc_now()),
    )


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
    conn.execute("DELETE FROM api_client_key_overlaps WHERE system_name=?", (system,))
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
    actions.add_argument("--finish-rotation", action="store_true")
    parser.add_argument("--overlap-minutes", type=int, default=0,
                        help="Optional old-key overlap (0 to 1440 minutes).")
    parser.add_argument("--production-approved", action="store_true")
    args = parser.parse_args()
    if not args.rotate_key and args.overlap_minutes != 0:
        parser.error("--overlap-minutes is valid only with --rotate-key")
    settings = load_settings()
    if settings.app_env == "production" and not args.production_approved:
        parser.error("Production credential changes require --production-approved")
    init_db(settings)
    migrate(settings)
    try:
        with connection(settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if args.rotate_key:
                new_key = rotate_api_key(conn, args.system, args.overlap_minutes)
            elif args.finish_rotation:
                finish_rotation(conn, args.system)
                new_key = None
            else:
                revoke_client(conn, args.system)
                new_key = None
    except ValueError as exc:
        parser.error(str(exc))
    if new_key is not None:
        print("New API key (save securely now):", new_key)
        if args.overlap_minutes:
            print(f"Previous API key remains valid for up to {args.overlap_minutes} minutes.")
        else:
            print("Previous API key is invalidated immediately.")
    elif args.finish_rotation:
        print("Previous API key retired; current key remains active:", args.system)
    else:
        print("ERP API client revoked:", args.system)


if __name__ == "__main__":
    main()
