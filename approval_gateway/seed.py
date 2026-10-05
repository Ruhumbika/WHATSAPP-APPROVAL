from __future__ import annotations

import argparse
import secrets

from .config import load_settings
from .db import connection, init_db
from .repository import create_api_client, upsert_approver
from .security import normalize_phone
from .upgrade import migrate


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a local API client and a named test approver."
    )

    # Use explicit identity details instead of invented role-based users.
    parser.add_argument(
        "--system",
        required=True,
        help="Source system identifier, for example: peta.",
    )
    parser.add_argument(
        "--company",
        required=True,
        help="Company scope used by approval requests.",
    )
    parser.add_argument(
        "--approver-name",
        required=True,
        help="Actual name of the test approver.",
    )
    parser.add_argument(
        "--approver-role",
        required=True,
        help="Descriptive role; this does not grant approval permissions.",
    )
    parser.add_argument(
        "--approver-phone",
        required=True,
        help="International WhatsApp number including the country code.",
    )

    args = parser.parse_args()

    for field in ("system", "company", "approver_name", "approver_role"):
        value = getattr(args, field).strip()

        if not value or len(value) > 200:
            parser.error(f"{field} must contain 1–200 characters.")

        setattr(args, field, value)

    try:
        phone = normalize_phone(args.approver_phone)
    except ValueError as exc:
        parser.error(str(exc))

    settings = load_settings()

    # Demo registration must not modify a production or live-delivery setup.
    if settings.app_env not in {"local", "testing"}:
        parser.error(
            "This script is restricted to local/testing environments."
        )

    if not settings.dry_run_whatsapp:
        parser.error("This script requires DRY_RUN_WHATSAPP=true.")

    init_db(settings)
    migrate(settings)

    generated_key = None

    with connection(settings) as conn:
        # Serialize registration to prevent concurrent duplicate recipients.
        conn.execute("BEGIN IMMEDIATE")

        client = conn.execute(
            "SELECT id, active FROM api_clients WHERE system_name = ?",
            (args.system,),
        ).fetchone()

        if client is not None and client["active"] != 1:
            parser.error(
                "API client is disabled; explicit reactivation is required."
            )

        if client is None:
            generated_key = secrets.token_urlsafe(32)
            create_api_client(conn, args.system, generated_key)

        # Existing client keys remain unchanged when the script is rerun.
        upsert_approver(
            conn,
            args.approver_name,
            args.approver_role,
            args.company,
            phone,
        )

        approvers = conn.execute(
            """
            SELECT id, active
            FROM approvers
            WHERE role = ?
              AND company = ?
              AND phone_number = ?
            """,
            (args.approver_role, args.company, phone),
        ).fetchall()

        if len(approvers) != 1:
            parser.error(
                "Recipient registration is ambiguous; review duplicates."
            )

        approver = approvers[0]

        if approver["active"] != 1:
            parser.error(
                "Approver is disabled; explicit reactivation is required."
            )

        approver_id = approver["id"]

    # Display a new credential only after the transaction commits.
    print("Local registration completed.")
    print(f"Source system: {args.system}")
    print(f"Company scope: {args.company}")
    print(f"Approver name: {args.approver_name}")
    print(f"Gateway approver ID: {approver_id}")

    if generated_key is not None:
        print(f"New API key: {generated_key}")
        print("Store this key securely; the database stores only its hash.")
    else:
        print("Existing API key retained.")

    print(
        "Actor binding and integration callback must be registered separately."
    )


if __name__ == "__main__":
    main()
