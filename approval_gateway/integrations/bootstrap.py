"""Load configured ERP adapters before the gateway accepts requests."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

from ..config import ConfigurationError, Settings, load_environment
from .registry import configure_workflow_guards


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Invalid JSON constant")


def bootstrap_integrations(settings: Settings) -> tuple[str, ...]:
    # Read the operator-managed configuration without exposing credentials.
    if not settings.integrations_config_path:
        raise ConfigurationError("INTEGRATIONS_CONFIG_PATH is required.")
    try:
        with Path(settings.integrations_config_path).open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Configuration too large")
        config = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                            parse_constant=_constant)
        if not isinstance(config, dict) or set(config) != {"systems"}:
            raise ValueError("Expected systems configuration")
        systems = config["systems"]
        if not isinstance(systems, dict):
            raise ValueError("systems must be an object")
    except (OSError, ValueError, RecursionError) as exc:
        raise ConfigurationError("Cannot read valid integration configuration.") from exc

    # Credential references use the same .env and process precedence as Settings.
    environment = load_environment()
    adapters = {}
    for system, entry in systems.items():
        if not isinstance(entry, dict) or set(entry) != {"enabled", "factory", "options"}:
            raise ConfigurationError(f"Invalid integration entry: {system}.")
        if type(entry["enabled"]) is not bool:
            raise ConfigurationError(f"enabled must be boolean: {system}.")
        if not entry["enabled"]:
            continue
        factory = entry["factory"]
        options = entry["options"]
        if not isinstance(factory, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*:[a-z][a-z0-9_]*", factory
        ) or not isinstance(options, dict):
            raise ConfigurationError(f"Invalid adapter factory/options: {system}.")

        # Factories are trusted application code inside the integrations package.
        module_name, function_name = factory.split(":")
        try:
            module = importlib.import_module(f"{__package__}.{module_name}")
            builder = getattr(module, function_name)
            if not callable(builder):
                raise ValueError("Adapter factory is not callable")
            adapters[system] = builder(options, environment)
        except Exception:
            raise ConfigurationError(f"Cannot initialize integration: {system}.") from None

    # Publish only after every enabled adapter has initialized successfully.
    try:
        configure_workflow_guards(adapters)
    except ValueError:
        raise ConfigurationError("Invalid adapter registry configuration.") from None
    return tuple(sorted(adapters))
