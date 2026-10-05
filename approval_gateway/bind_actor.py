"""Register an operator-verified identity mapping for a system/company scope."""

from __future__ import annotations

import argparse
from uuid import UUID

from .config import load_settings
from .db import connection, init_db
from .security import normalize_phone
from .upgrade import migrate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--system",
        required=True,
        help="Registered source system identifier.",
    )
    parser.add_argument(
        "--company",
        required=True,
        help="Company scope used by approval requests.",
    )
    parser.add_argument(
        "--directory-uuid",
        required=True,
        help="Verified User Directory UUID.",
    )
    parser.add_argument(
        "--approver-id",
        required=True,
        type=int,
        help="Gateway approver ID created during registration.",
    )
    parser.add_argument(
        "--source-user-id",
        help="Optional local user ID in the source ERP.",
    )

    args = parser.parse_args()

    for field in ("system", "company"):
        value = getattr(args, field).strip()

        if not value or len(value) > 200:
            parser.error(f"{field} must contain 1–200 characters.")

        setattr(args, field, value)

    if args.approver_id <= 0:
        parser.error("approver-id must be a positive integer.")

    # Normalize UUID representation to match request validation.
    try:
        directory_uuid = str(UUID(args.directory_uuid.strip()))
    except ValueError:
        parser.error("A valid Directory UUID is required.")

    source_user_id = args.source_user_id

    if source_user_id is not None:
        source_user_id = source_user_id.strip()

        if not source_user_id or len(source_user_id) > 200:
            parser.error("source-user-id must contain 1–200 characters.")

    settings = load_settings()
    init_db(settings)
    migrate(settings)

    with connection(settings) as conn:
        conn.execute("BEGIN IMMEDIATE")

        client = conn.execute(
            """
            SELECT id
            FROM api_clients
            WHERE system_name = ? AND active = 1
            """,
            (args.system,),
        ).fetchone()

        if client is None:
            parser.error("An active API client is required.")

        approver = conn.execute(
            """
            SELECT *
            FROM approvers
            WHERE id = ? AND active = 1
            """,
            (args.approver_id,),
        ).fetchone()

        if approver is None:
            parser.error("An active gateway approver is required.")

        # Company scope is independent of the source-system identifier.
        if approver["company"] not in {args.company, "all"}:
            parser.error("Approver is outside the requested company scope.")

        try:
            normalized_phone = normalize_phone(approver["phone_number"])
        except ValueError:
            parser.error("Approver phone is not a valid international number.")

        if normalized_phone != approver["phone_number"]:
            parser.error("Normalize the stored approver phone before binding.")

        existing = conn.execute(
            """
            SELECT *
            FROM actor_bindings
            WHERE system_name = ?
              AND company = ?
              AND directory_uuid = ?
            """,
            (args.system, args.company, directory_uuid),
        ).fetchone()

        # Registration must not silently change or reactivate an identity.
        if existing is not None:
            if (
                existing["active"] != 1
                or existing["approver_id"] != args.approver_id
                or existing["source_user_id"] != source_user_id
            ):
                parser.error(
                    "Binding already exists with different or disabled settings. "
                    "Review affected tasks before changing the mapping."
                )

            binding_id = existing["id"]
            created = False

        else:
            cursor = conn.execute(
                """
                INSERT INTO actor_bindings (
                    system_name,
                    company,
                    directory_uuid,
                    source_user_id,
                    approver_id,
                    active
                )
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    args.system,
                    args.company,
                    directory_uuid,
                    source_user_id,
                    args.approver_id,
                ),
            )

            binding_id = cursor.lastrowid
            created = True

    print(
        "Actor binding registered."
        if created
        else "Actor binding already registered; mapping unchanged."
    )
    print(f"Binding ID: {binding_id}")
    print(f"Source system: {args.system}")
    print(f"Company scope: {args.company}")
    print(f"Gateway approver ID: {args.approver_id}")
    print(
        "Registration records an operator-provided mapping; "
        "it does not verify phone ownership or ERP permissions."
    )


if __name__ == "__main__":
    main()
