"""Register ERP adapters without embedding ERP-specific rules in the gateway core."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .permissions import IntegrationUnavailable, WorkflowGuard


# Replace the immutable registry once during application startup.
_guards: Mapping[str, WorkflowGuard] = MappingProxyType({})


def configure_workflow_guards(guards: Mapping[str, WorkflowGuard]) -> None:
    registered: dict[str, WorkflowGuard] = {}
    for system_name, guard in guards.items():
        if (
            not isinstance(system_name, str)
            or not system_name
            or system_name != system_name.strip()
            or len(system_name) > 200
        ):
            raise ValueError("A valid source-system name is required")
        if not callable(getattr(guard, "authorize", None)):
            raise ValueError("Every ERP adapter must implement authorize()")
        registered[system_name] = guard
    global _guards
    _guards = MappingProxyType(registered)


def get_workflow_guard(system_name: str) -> WorkflowGuard:
    # Unconfigured systems cannot bypass workflow authorization.
    guard = _guards.get(system_name)
    if guard is None:
        raise IntegrationUnavailable("Workflow adapter is not configured")
    return guard
