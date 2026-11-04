"""Register a generic ERP adapter without changing gateway source code.

Operator-only tool. It updates the existing integrations JSON config and
requires an already provisioned API client, callback, company scope and secret
environment variable. A controlled gateway+worker reload is required.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path

from .config import load_settings, load_environment
from .db import connection
from .integrations.generic import build_adapter


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate integration configuration key")
        result[key] = value
    return result


def add_generic_adapter(settings, system: str, verify_url: str,
                        token_env: str) -> None:
    if not isinstance(system, str) or not re.fullmatch(
        r"[a-z][a-z0-9_-]{0,79}", system
    ):
        raise ValueError("Invalid integration system name")
    options = {"verify_url": verify_url, "verify_token_env": token_env}
    build_adapter(options, load_environment())  # Fail closed on missing token/invalid URL.
    path_value = settings.integrations_config_path
    if not path_value:
        raise ValueError("INTEGRATIONS_CONFIG_PATH must already be configured")
    path = Path(path_value)
    if not path.is_file() or path.is_symlink():
        raise ValueError("Operator integration configuration file is missing")
    with connection(settings) as conn:
        client = conn.execute(
            "SELECT active FROM api_clients WHERE system_name=?", (system,)
        ).fetchone()
        callback = conn.execute(
            "SELECT 1 FROM integrations WHERE system_name=?", (system,)
        ).fetchone()
        scope = conn.execute(
            "SELECT 1 FROM integration_company_scopes WHERE system_name=? LIMIT 1",
            (system,),
        ).fetchone()
        if not (client and client["active"] == 1 and callback and scope):
            raise ValueError("Provision an active ERP, webhook and company scope first")

    lock_path = path.with_name(path.name + ".onboard.lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        content = path.read_bytes()
        if len(content) > 1024 * 1024:
            raise ValueError("Integration config exceeds allowed size")
        try:
            config = json.loads(
                content, object_pairs_hook=_unique_object,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON number")),
            )
        except (UnicodeError, ValueError) as exc:
            raise ValueError("Invalid integration configuration") from exc
        if not isinstance(config, dict) or set(config) != {"systems"} or not isinstance(config["systems"], dict):
            raise ValueError("Invalid integration configuration layout")
        if system in config["systems"]:
            raise ValueError("Integration adapter already exists; never overwrite automatically")
        config["systems"][system] = {
            "enabled": True,
            "factory": "generic:build_adapter",
            "options": options,
        }
        new_content = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode()
        if len(new_content) > 1024 * 1024:
            raise ValueError("Integration config exceeds allowed size")
        descriptor, temporary = tempfile.mkstemp(
            prefix=".integrations-", dir=str(path.parent)
        )
        try:
            os.fchmod(descriptor, path.stat().st_mode & 0o777)
            with os.fdopen(descriptor, "wb") as dest:
                dest.write(new_content)
                dest.flush()
                os.fsync(dest.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", required=True)
    parser.add_argument("--verify-url", required=True)
    parser.add_argument("--verify-token-env", required=True)
    parser.add_argument("--production-approved", action="store_true")
    args = parser.parse_args()
    settings = load_settings()
    if settings.app_env == "production" and not args.production_approved:
        parser.error("Production changes require --production-approved")
    try:
        add_generic_adapter(
            settings, args.system, args.verify_url, args.verify_token_env
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print("Generic integration configured for:", args.system)
    print("Controlled gateway and worker restart required to load the adapter.")
    print("No Meta/WhatsApp credentials are provided to the ERP.")


if __name__ == "__main__":
    main()
