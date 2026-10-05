from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Resolve configuration and storage paths from the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigurationError(ValueError):
    """Raised when a configuration value is missing or invalid."""


def _load_env_file() -> dict[str, str]:

    # Precedence: process environment, .env, then config.env.
    values = {}
    for filename in (".env", "config.env"):
        path = PROJECT_ROOT / filename
        if not path.is_file():
            continue
        for line_number, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or not key.isidentifier():
                raise ConfigurationError(
                    f"{filename}:{line_number}: expected KEY=value."
                )
            value = value.strip()

            # Remove matching outer quotes without altering their contents.
            if value.startswith(("'", '"')):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ConfigurationError(
                        f"{filename}:{line_number}: unmatched quotes."
                    )
                value = value[1:-1]
            values.setdefault(key, value)
    return values


def load_environment() -> dict[str, str]:
    # Share configuration precedence without modifying the process environment.
    values = _load_env_file()
    values.update(os.environ)
    return values


def _get_bool(
    values: dict[str, str],
    name: str,
    default: bool,
) -> bool:
    value = values.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def _get_int(
    values: dict[str, str],
    name: str,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    try:
        value = int(values.get(name, str(default)))
    except ValueError:
        raise ConfigurationError(f"{name} must be an integer.") from None
    if value < minimum or (maximum is not None and value > maximum):
        allowed = (
            f"{minimum}–{maximum}"
            if maximum is not None
            else f"at least {minimum}"
        )
        raise ConfigurationError(f"{name} must be {allowed}.")
    return value


def _resolve_path(value: str, name: str) -> str:
    if not value.strip():
        raise ConfigurationError(f"{name} cannot be empty.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


@dataclass(frozen=True)
class Settings:
    app_env: str
    host: str
    port: int
    database_path: str
    log_path: str
    public_base_url: str
    default_expiry_hours: int
    callback_timeout_seconds: int
    callback_secret: str
    meta_verify_token: str
    meta_app_secret: str
    whatsapp_access_token: str
    whatsapp_phone_number_id: str
    whatsapp_api_version: str
    whatsapp_template_name: str
    whatsapp_template_language: str
    dry_run_whatsapp: bool

    # Integration-specific settings live in a separate operator-managed file.
    integrations_config_path: str | None = None


def _validate_settings(settings: Settings) -> None:
    if settings.app_env not in {"local", "testing", "production"}:
        raise ConfigurationError(
            "APP_ENV must be local, testing, or production."
        )
    required_text = {
        "APP_HOST": settings.host,
        "WHATSAPP_API_VERSION": settings.whatsapp_api_version,
        "WHATSAPP_TEMPLATE_NAME": settings.whatsapp_template_name,
        "WHATSAPP_TEMPLATE_LANGUAGE": settings.whatsapp_template_language,
    }
    for name, value in required_text.items():
        if not value.strip():
            raise ConfigurationError(f"{name} cannot be empty.")
    try:
        url = urlsplit(settings.public_base_url)
        valid_url = (
            url.scheme in {"http", "https"}
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
        )

        # Accessing port also validates malformed port values.
        url.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ConfigurationError(
            "PUBLIC_BASE_URL must be an HTTP(S) URL "
            "without credentials, query, or fragment."
        )
    if settings.app_env == "production":
        if settings.dry_run_whatsapp:
            raise ConfigurationError(
                "Production requires DRY_RUN_WHATSAPP=false."
            )
        if url.scheme != "https":
            raise ConfigurationError(
                "Production requires an HTTPS PUBLIC_BASE_URL."
            )

    # Live delivery requires credentials even when running locally.
    if not settings.dry_run_whatsapp:
        required_secrets = {
            "WHATSAPP_ACCESS_TOKEN": settings.whatsapp_access_token,
            "WHATSAPP_PHONE_NUMBER_ID": settings.whatsapp_phone_number_id,
            "META_APP_SECRET": settings.meta_app_secret,
            "META_VERIFY_TOKEN": settings.meta_verify_token,
        }
        for name, value in required_secrets.items():
            if not value.strip() or value.strip() == "change-me":
                raise ConfigurationError(
                    f"{name} is required for live WhatsApp mode."
                )
        if not (
            settings.whatsapp_phone_number_id.isascii()
            and settings.whatsapp_phone_number_id.isdigit()
        ):
            raise ConfigurationError(
                "WHATSAPP_PHONE_NUMBER_ID must contain digits only."
            )


def load_settings() -> Settings:

    # Read configuration without modifying the process environment.
    values = load_environment()
    settings = Settings(
        app_env=values.get("APP_ENV", "local").strip().lower(),
        host=values.get("APP_HOST", "127.0.0.1").strip(),
        port=_get_int(values, "APP_PORT", 8088, maximum=65535),
        database_path=_resolve_path(
            values.get(
                "DATABASE_PATH",
                "data/approval_gateway.sqlite3",
            ),
            "DATABASE_PATH",
        ),
        log_path=_resolve_path(
            values.get("LOG_PATH", "logs/approval_gateway.log"),
            "LOG_PATH",
        ),
        public_base_url=values.get(
            "PUBLIC_BASE_URL",
            "http://127.0.0.1:8088",
        )
        .strip()
        .rstrip("/"),
        default_expiry_hours=_get_int(
            values,
            "DEFAULT_EXPIRY_HOURS",
            24,
        ),
        callback_timeout_seconds=_get_int(
            values,
            "CALLBACK_TIMEOUT_SECONDS",
            10,
        ),
        callback_secret=values.get("CALLBACK_SECRET", ""),
        meta_verify_token=values.get("META_VERIFY_TOKEN", ""),
        meta_app_secret=values.get("META_APP_SECRET", ""),
        whatsapp_access_token=values.get("WHATSAPP_ACCESS_TOKEN", ""),
        whatsapp_phone_number_id=values.get(
            "WHATSAPP_PHONE_NUMBER_ID",
            "",
        ).strip(),
        whatsapp_api_version=values.get(
            "WHATSAPP_API_VERSION",
            "v23.0",
        ).strip(),
        whatsapp_template_name=values.get(
            "WHATSAPP_TEMPLATE_NAME",
            "workflow_approval_document",
        ).strip(),
        whatsapp_template_language=values.get(
            "WHATSAPP_TEMPLATE_LANGUAGE",
            "en",
        ).strip(),
        dry_run_whatsapp=_get_bool(
            values,
            "DRY_RUN_WHATSAPP",
            True,
        ),
        integrations_config_path=_resolve_path(
            values.get("INTEGRATIONS_CONFIG_PATH", "config/integrations.json"),
            "INTEGRATIONS_CONFIG_PATH",
        ),
    )
    _validate_settings(settings)
    return settings
