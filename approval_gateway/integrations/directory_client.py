"""Read current User Directory permissions through the verified API endpoint."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import UUID

from .permissions import DirectoryUser, IntegrationUnavailable

# Bound large permission responses before decoding JSON.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


# Reject redirects so the Directory token stays on the configured endpoint.
class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _positive_id(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("Expected a positive integer ID")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON value")


class UserDirectoryClient:
    def __init__(
        self,
        base_url: str,
        access_token: str,
        user_ids: Mapping[str, int],
        service_access_permissions: Mapping[int, int],
        timeout_seconds: int = 10,
    ) -> None:
        # Use operator-provided UUID mappings; verify them on every response.
        url = urlsplit(base_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(char.isspace() for char in base_url)
        ):
            raise ValueError(
                "Directory base URL must use HTTPS without credentials"
            )
        if not access_token or any(
            ord(c) < 33 or ord(c) == 127 for c in access_token
        ):
            raise ValueError("A valid Directory access token is required")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise ValueError(
                "Directory timeout must be between 1 and 60 seconds"
            )

        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout_seconds = timeout_seconds
        self.user_ids: dict[str, int] = {}
        for identity, user_id in user_ids.items():
            canonical = str(UUID(identity))
            if canonical in self.user_ids:
                raise ValueError("Duplicate Directory identity mapping")
            self.user_ids[canonical] = _positive_id(user_id)
        self.service_access_permissions = {
            _positive_id(service): _positive_id(permission)
            for service, permission in service_access_permissions.items()
        }
        self.opener = urllib.request.build_opener(_RejectRedirects())

    def get_user(self, directory_uuid: str) -> DirectoryUser:
        # The API uses numeric Directory IDs, not local ERP user IDs.
        try:
            canonical = str(UUID(directory_uuid))
            user_id = self.user_ids[canonical]
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise IntegrationUnavailable(
                "Directory identity is not registered"
            ) from exc
        payload = self._get(f"users/{user_id}/permissions")
        try:
            return self._parse_user(payload, canonical, user_id)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise IntegrationUnavailable(
                "Invalid Directory permission response"
            ) from exc

    def _get(self, path: str) -> dict[str, Any]:
        # Bound response size and omit credentials and response bodies from errors.
        request = urllib.request.Request(
            f"{self.base_url}/{path}",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self.opener.open(
                request, timeout=self.timeout_seconds
            ) as response:
                if response.status != 200:
                    raise ValueError("Unexpected HTTP status")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError(
                        f"Directory response exceeds "
                        f"{MAX_RESPONSE_BYTES // (1024 * 1024)} MiB"
                    )
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            return payload
        except (
            urllib.error.URLError,
            OSError,
            ValueError,
            RecursionError,
        ) as exc:
            raise IntegrationUnavailable("Directory request failed") from exc

    def _parse_user(
        self, payload: dict[str, Any], directory_uuid: str, user_id: int
    ) -> DirectoryUser:
        # Bind the returned numeric ID and UUID to the requested identity.
        if payload.get("status") != "success":
            raise ValueError("Directory response was unsuccessful")
        user = payload["user_details"]
        if not isinstance(user, dict):
            raise ValueError("Expected user details")
        if (
            _positive_id(user["id"]) != user_id
            or str(UUID(user["uuid"])) != directory_uuid
        ):
            raise ValueError("Directory identity mismatch")
        active_flag = user["is_active"]
        if type(active_flag) is not int or active_flag not in (0, 1):
            raise ValueError("Invalid account status")
        # Until ban semantics are confirmed, any ban marker prevents authorization.
        active = (
            active_flag == 1
            and user["banned_at"] is None
            and user["banned_until"] is None
        )

        # Only checked=true grants permission; ignore the undocumented dat field.
        groups = payload["data"]
        if not isinstance(groups, list):
            raise ValueError("Expected service groups")
        grants: set[tuple[int, int]] = set()
        seen_services: set[int] = set()
        for group in groups:
            service_id = _positive_id(group["service_id"])
            if service_id in seen_services:
                raise ValueError("Duplicate service group")
            seen_services.add(service_id)
            permissions = group["permissions"]
            if not isinstance(permissions, list):
                raise ValueError("Expected service permissions")
            seen_permissions: set[int] = set()
            for permission in permissions:
                permission_id = _positive_id(permission["id"])
                checked = permission["checked"]
                if (
                    type(checked) is not bool
                    or permission_id in seen_permissions
                ):
                    raise ValueError("Invalid or duplicate permission")
                seen_permissions.add(permission_id)
                if checked:
                    grants.add((service_id, permission_id))
        service_ids = frozenset(
            service
            for service, permission in self.service_access_permissions.items()
            if (service, permission) in grants
        )

        # Preserve optional ERP mappings without guessing aliases or company access.
        mapping = user["company_user_id"]
        if mapping is None:
            mapping = {}
        if not isinstance(mapping, dict):
            raise ValueError("Expected ERP user mappings")
        source_ids: list[tuple[str, str]] = []
        for key, value in mapping.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("Invalid mapping key")
            if value is None or value == "":
                continue
            if type(value) is int and value > 0:
                source_ids.append((key, str(value)))
            elif isinstance(value, str) and value.strip():
                source_ids.append((key, value.strip()))
            else:
                raise ValueError("Invalid mapped user ID")
        return DirectoryUser(
            directory_uuid=directory_uuid,
            directory_user_id=user_id,
            active=active,
            service_ids=service_ids,
            granted_permissions=frozenset(grants),
            source_user_ids=tuple(sorted(source_ids)),
        )
