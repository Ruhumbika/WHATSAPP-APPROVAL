"""Register the callback endpoint and signing secret for a source system."""

from __future__ import annotations

import argparse
import os
import secrets
from getpass import getpass
from urllib.parse import urlsplit

from .config import load_settings
from .db import connection, init_db
from .upgrade import migrate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--system",
        required=True,
        help="Registered source system identifier.",
    )
    parser.add_argument(
        "--callback-url",
        required=True,
        help="Source-system endpoint that receives signed decisions.",
    )

    args = parser.parse_args()
    system = args.system.strip()
    callback_url = args.callback_url.strip()

    if not system or len(system) > 200:
        parser.error("system must contain 1–200 characters.")

    settings = load_settings()

    # Require HTTPS except for explicit local/testing loopback endpoints.
    try:
        url = urlsplit(callback_url)
        url.port

        local_http = (
            settings.app_env in {"local", "testing"}
            and url.scheme == "http"
            and url.hostname in {"127.0.0.1", "localhost", "::1"}
        )

        if (
            not callback_url
            or any(char.isspace() for char in callback_url)
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or (url.scheme != "https" and not local_http)
        ):
            raise ValueError()

    except ValueError:
        parser.error(
            "Provide an HTTPS callback URL without credentials, "
            "query or fragment. Local/testing loopback HTTP is allowed."
        )

    # Avoid placing the signing secret in command-line arguments.
    callback_secret = os.environ.get("APPROVAL_CALLBACK_SECRET")

    if callback_secret is None:
        callback_secret = getpass("Integration callback secret: ")

    if (
        len(callback_secret) < 32
        or callback_secret != callback_secret.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in callback_secret)
    ):
        parser.error(
            "Callback secret must contain at least 32 characters "
            "without surrounding whitespace or control characters."
        )

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
            (system,),
        ).fetchone()

        if client is None:
            parser.error("Register an active API client first.")

        existing = conn.execute(
            "SELECT * FROM integrations WHERE system_name = ?",
            (system,),
        ).fetchone()

        # Reruns are harmless; configuration changes require separate maintenance.
        if existing is not None:
            same_secret = secrets.compare_digest(
                existing["callback_secret"].encode("utf-8"),
                callback_secret.encode("utf-8"),
            )

            if existing["callback_url"] != callback_url or not same_secret:
                parser.error(
                    "Integration already exists with different settings. "
                    "Use a planned configuration change instead of overwriting it."
                )

            created = False

        else:
            conn.execute(
                """
                INSERT INTO integrations (
                    system_name,
                    callback_url,
                    callback_secret
                )
                VALUES (?, ?, ?)
                """,
                (system, callback_url, callback_secret),
            )
            created = True

    print(
        "Integration registered."
        if created
        else "Integration already registered; settings unchanged."
    )
    print(f"Source system: {system}")
    print(f"Callback URL: {callback_url}")


if __name__ == "__main__":
    main()
