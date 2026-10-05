"""Shared permission policies and ERP workflow authorization contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol
from uuid import UUID


class IntegrationUnavailable(RuntimeError):
    """The integration could not obtain a trustworthy response."""


class WorkflowDenied(RuntimeError):
    """The current ERP workflow does not authorize the requested action."""


# Describe the actor and operation for integrations using Directory permissions.
@dataclass(frozen=True)
class PermissionContext:
    directory_uuid: str
    source_system: str
    company_id: str
    module: str
    action: str
    record_id: str
    step_id: str
    workflow_version: str
    source_user_id: str | None = None


# Keep Directory identity, grants and ERP user mappings separate.
@dataclass(frozen=True)
class DirectoryUser:
    directory_uuid: str
    directory_user_id: int
    active: bool
    service_ids: frozenset[int]
    granted_permissions: frozenset[tuple[int, int]]
    source_user_ids: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ActionPolicy:
    service_id: int
    permission_ids: frozenset[int]
    source_user_key: str | None = None

    def __post_init__(self) -> None:
        ids = (self.service_id, *self.permission_ids)

        if not self.permission_ids or any(
            type(value) is not int or value <= 0 for value in ids
        ):
            raise ValueError(
                "Positive service and permission IDs are required."
            )

        if self.source_user_key is not None and (
            not isinstance(self.source_user_key, str)
            or not self.source_user_key.strip()
        ):
            raise ValueError("source_user_key must be a non-empty string.")


@dataclass(frozen=True)
class PermissionResult:
    allowed: bool
    reason: str


# Adapters own HTTP requests and normalize their responses.
class DirectoryPermissionClient(Protocol):
    def get_user(self, directory_uuid: str) -> DirectoryUser: ...


class WorkflowPermissionProvider(Protocol):
    def check(
        self,
        context: PermissionContext,
        user: DirectoryUser,
    ) -> PermissionResult: ...


class ApprovalPermissionProvider(Protocol):
    def check(self, context: PermissionContext) -> PermissionResult: ...


class UnconfiguredPermissionProvider:
    def check(self, context: PermissionContext) -> PermissionResult:
        return PermissionResult(
            False,
            "Permission integration is not configured.",
        )


# Use this provider only where Directory grants form part of authorization.
class DirectoryApprovalPermissionProvider:
    def __init__(
        self,
        directory: DirectoryPermissionClient,
        policies: Mapping[tuple[str, str, str], ActionPolicy],
        workflows: Mapping[str, WorkflowPermissionProvider],
    ) -> None:
        self.directory = directory
        self.policies = dict(policies)
        self.workflows = dict(workflows)

    def check(self, context: PermissionContext) -> PermissionResult:
        # Require complete operation scope before looking up a policy.
        fields = (
            context.source_system,
            context.company_id,
            context.module,
            context.action,
            context.record_id,
            context.step_id,
            context.workflow_version,
        )

        if any(
            not isinstance(value, str) or not value.strip() for value in fields
        ):
            return PermissionResult(
                False,
                "Complete operation scope is required.",
            )

        try:
            actor_uuid = str(UUID(context.directory_uuid))
        except (ValueError, TypeError, AttributeError):
            return PermissionResult(
                False,
                "Valid Directory UUID is required.",
            )

        policy = self.policies.get(
            (
                context.source_system,
                context.module,
                context.action,
            )
        )
        workflow = self.workflows.get(context.source_system)

        if policy is None or workflow is None:
            return PermissionResult(
                False,
                "Action integration is not configured.",
            )

        try:
            # Fetch current grants rather than trusting caller-supplied permissions.
            user = self.directory.get_user(actor_uuid)

            if user.directory_uuid != actor_uuid or user.active is not True:
                return PermissionResult(
                    False,
                    "Active Directory identity required.",
                )

            if policy.service_id not in user.service_ids:
                return PermissionResult(
                    False,
                    "Service access is not assigned.",
                )

            required = {
                (policy.service_id, permission_id)
                for permission_id in policy.permission_ids
            }

            if not required.issubset(user.granted_permissions):
                return PermissionResult(
                    False,
                    "Required permissions are not assigned.",
                )

            # ERP user mappings identify users; they do not grant company access.
            if policy.source_user_key is not None:
                matches = [
                    value
                    for key, value in user.source_user_ids
                    if key == policy.source_user_key
                ]

                if (
                    len(matches) != 1
                    or not isinstance(matches[0], str)
                    or not matches[0].strip()
                    or matches[0] != context.source_user_id
                ):
                    return PermissionResult(
                        False,
                        "ERP user mapping does not match.",
                    )

            # The ERP must also authorize this specific record and workflow step.
            result = workflow.check(context, user)

            if result.allowed is not True:
                return PermissionResult(False, result.reason)

            return PermissionResult(
                True,
                "Directory and ERP checks passed.",
            )

        except IntegrationUnavailable:
            return PermissionResult(
                False,
                "Authorization integration is unavailable.",
            )


# Carry verified assignment and snapshot evidence back to the global service.
@dataclass(frozen=True)
class WorkflowActor:
    directory_uuid: str
    source_user_id: str
    phone_number: str
    module_id: str
    step_id: str
    record_digest: str


# Every ERP adapter implements this contract using its own workflow rules.
class WorkflowGuard(Protocol):
    def authorize(
        self,
        request: Mapping[str, object],
    ) -> WorkflowActor: ...
