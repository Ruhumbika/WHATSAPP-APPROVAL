"""Isolated, loopback-only developer sandbox bootstrap.

No real WhatsApp delivery and no access to production data or credentials.
This creates an empty test database, a disabled integration config, and a
single set of sandbox-only credentials. Operator configures ERP verifier later.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from .config import load_settings
from .db import connection, init_db
from .provision_erp import provision
from .upgrade import migrate


def create_local_sandbox(root: Path, port: int = 8098) -> dict[str, str]:
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Port must be 1024-65535")
    if not isinstance(root, Path) or not root.is_absolute() or root.is_symlink():
        raise ValueError("Sandbox directory must be an absolute non-symlink path")
    if root.exists() or not root.parent.is_dir() or root.parent.is_symlink():
        raise ValueError("Sandbox directory must be new and parent must exist")
    settings = load_settings()
    if settings.app_env == "production":
        raise ValueError("Cannot bootstrap a sandbox from production environment")
    source_db = Path(settings.database_path).resolve()
    if root.resolve() == source_db or root.resolve() in source_db.parents:
        raise ValueError("Sandbox overlaps the configured gateway database")
    root.mkdir(mode=0o700)
    db_path = root / "sandbox.sqlite3"
    env_path = root / "sandbox.env"
    config_path = root / "integrations.json"
    try:
        config_path.write_text('{"systems": {}}\n', encoding="utf-8")
        config_path.chmod(0o600)
        sandbox = replace(
            settings, app_env="testing", host="127.0.0.1", port=port,
            database_path=str(db_path), log_path=str(root / "sandbox.log"),
            public_base_url=f"http://127.0.0.1:{port}",
            dry_run_whatsapp=True, whatsapp_access_token="",
            whatsapp_phone_number_id="", meta_app_secret="",
            meta_verify_token="", integrations_config_path=str(config_path),
        )
        init_db(sandbox)
        migrate(sandbox)
        with connection(sandbox) as conn:
            api_key, callback_secret = provision(
                conn, "sandbox_erp", "https://sandbox-erp.example.test/callback",
                ("sandbox",),
            )
        db_path.chmod(0o600)
        env_values = {
            "APP_ENV": "testing", "APP_HOST": "127.0.0.1",
            "APP_PORT": str(port), "DATABASE_PATH": str(db_path),
            "LOG_PATH": str(root / "sandbox.log"),
            "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
            "DRY_RUN_WHATSAPP": "true",
            "INTEGRATIONS_CONFIG_PATH": str(config_path),
            "WHATSAPP_ACCESS_TOKEN": "", "WHATSAPP_PHONE_NUMBER_ID": "",
            "META_APP_SECRET": "", "META_VERIFY_TOKEN": "",
        }
        env_path.write_text(
            "".join(f"{key}={value}\n" for key, value in env_values.items()),
            encoding="utf-8",
        )
        env_path.chmod(0o600)
        return {
            "directory": str(root),
            "env_file": str(env_path),
            "api_key": api_key,
            "callback_secret": callback_secret,
        }
    except Exception:
        for name in ("sandbox.sqlite3", "sandbox.sqlite3-wal",
                     "sandbox.sqlite3-shm", "sandbox.log",
                     "integrations.json", "sandbox.env"):
            path = root / name
            if path.is_file() and not path.is_symlink():
                path.unlink()
        root.rmdir()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--port", type=int, default=8098)
    args = parser.parse_args()
    try:
        report = create_local_sandbox(Path(args.directory), args.port)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print("Sandbox directory:", report["directory"])
    print("Sandbox environment:", report["env_file"])
    print("Sandbox-only API key (save securely):", report["api_key"])
    print("Sandbox-only callback secret (save securely):", report["callback_secret"])
    print("No WhatsApp delivery or ERP workflow verifier is configured.")


if __name__ == "__main__":
    main()
