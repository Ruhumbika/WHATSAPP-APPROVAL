"""Read-only ERP integration configuration preflight, without displaying secrets.

This cannot prove phone ownership, live Meta delivery or ERP transaction
execution. It reports configuration only; never labels an ERP production ready.
"""
from __future__ import annotations

import argparse
import json

from .config import load_settings
from .db import connection


def inspect_integration(conn, system: str) -> dict[str, object]:
    if not isinstance(system, str) or not system.strip():
        raise ValueError("System is required")
    client = conn.execute(
        "SELECT active FROM api_clients WHERE system_name=?", (system,)
    ).fetchone()
    integration = conn.execute(
        "SELECT callback_url FROM integrations WHERE system_name=?", (system,)
    ).fetchone()
    scopes = [row["company"] for row in conn.execute(
        "SELECT company FROM integration_company_scopes "
        "WHERE system_name=? ORDER BY company", (system,)
    )]
    bindings = conn.execute(
        "SELECT COUNT(*) FROM actor_bindings b "
        "JOIN approvers a ON a.id=b.approver_id "
        "WHERE b.system_name=? AND b.active=1 AND a.active=1",
        (system,),
    ).fetchone()[0]
    checks = {
        "registered_client": client is not None,
        "active_client": bool(client and client["active"] == 1),
        "registered_callback": integration is not None,
        "registered_company_scopes": bool(scopes),
        "active_actor_bindings": bindings > 0,
    }
    return {
        "system": system,
        "configuration_checks": checks,
        "allowed_companies": scopes,
        "active_actor_binding_count": bindings,
        "configuration_complete": all(checks.values()),
        "integration_ready": False,
        "remaining_external_verification": [
            "WhatsApp phone ownership proof",
            "ERP permission and transaction tests",
            "Real Meta message delivery and callback E2E tests",
            "Deployment adapter configuration and HTTPS endpoint reachability",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", required=True)
    args = parser.parse_args()
    settings = load_settings()
    with connection(settings) as conn:
        report = inspect_integration(conn, args.system)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
