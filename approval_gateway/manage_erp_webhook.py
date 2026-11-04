"""Controlled operator-only update of an ERP callback destination.

Never redirect an unacknowledged decision or a pending approval. The ERP
developer deploys and validates their new HTTPS receiver before the change.
"""
from __future__ import annotations

import argparse

from .config import load_settings
from .db import connection, utc_now
from .provision_erp import validate_callback_url


def update_callback_url(conn, system: str, new_url: str) -> None:
    if not isinstance(system, str) or not system.strip():
        raise ValueError("System name is required")
    new_url = validate_callback_url(new_url)
    row = conn.execute(
        """SELECT c.active,i.callback_url FROM api_clients c
           JOIN integrations i ON i.system_name=c.system_name
           WHERE c.system_name=?""",
        (system,),
    ).fetchone()
    if row is None or row["active"] != 1:
        raise ValueError("An active registered ERP is required")
    if row["callback_url"] == new_url:
        raise ValueError("New callback URL must differ from the current URL")
    unresolved = conn.execute(
        """SELECT 1 FROM approval_requests
           WHERE source_system=? AND (
             status='pending'
             OR (
                status IN ('approved','rejected')
                AND COALESCE(execution_status,'')
                    NOT IN ('applied','rejected')
             )
           ) LIMIT 1""",
        (system,),
    ).fetchone()
    if unresolved is not None:
        raise ValueError(
            "Resolve pending requests and unacknowledged decisions first"
        )
    cursor = conn.execute(
        "UPDATE integrations SET callback_url=? WHERE system_name=? AND callback_url=?",
        (new_url, system, row["callback_url"]),
    )
    if cursor.rowcount != 1:
        raise ValueError("Callback configuration changed during update")
    conn.execute(
        """INSERT INTO integration_webhook_events
           (system_name,previous_url,new_url,event_type,created_at)
           VALUES (?,?,?,'callback_url_updated',?)""",
        (system, row["callback_url"], new_url, utc_now()),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", required=True)
    parser.add_argument("--callback-url", required=True)
    parser.add_argument("--production-approved", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    if settings.app_env == "production" and not args.production_approved:
        parser.error("Production callback update requires --production-approved")
    try:
        with connection(settings) as conn:
            conn.execute("BEGIN IMMEDIATE")
            update_callback_url(conn, args.system, args.callback_url)
    except ValueError as exc:
        parser.error(str(exc))
    print("Registered ERP callback updated:", args.system)
    print("No Meta or WhatsApp provider configuration was changed.")


if __name__ == "__main__":
    main()
