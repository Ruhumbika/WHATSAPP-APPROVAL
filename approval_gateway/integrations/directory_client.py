"""Read service-scoped Directory identities; ERP owns approval authorization."""

from __future__ import annotations
import json
import urllib.error
import urllib.request
from typing import Any, Mapping
from urllib.parse import urlsplit, urlencode
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
        *,
        service_id: int,
        source_user_key: str | None = None,
        shared_user_ids: bool = False,
        timeout_seconds: int = 10,
    ) -> None:
        url = urlsplit(base_url)
        url.port
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(ord(c) <= 32 or ord(c) == 127 for c in base_url)
        ):
            raise ValueError(
                "Directory base URL must use HTTPS without credentials"
            )
        if (
            not isinstance(access_token, str)
            or not access_token
            or any(ord(c) < 33 or ord(c) == 127 for c in access_token)
        ):
            raise ValueError("A valid Directory system token is required")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise ValueError(
                "Directory timeout must be between 1 and 60 seconds"
            )
        if type(shared_user_ids) is not bool:
            raise ValueError("shared_user_ids must be boolean")
        if source_user_key is not None and (
            not isinstance(source_user_key, str) or not source_user_key.strip()
        ):
            raise ValueError("Invalid source user mapping key")
        if source_user_key is None and not shared_user_ids:
            raise ValueError(
                "Configure a mapping key or explicitly shared user IDs"
            )
        self.base_url = base_url.rstrip("/")
        if not self.base_url.endswith("/api/v1/client"):
            raise ValueError("Directory base URL must end with /api/v1/client")
        self.access_token = access_token
        self.service_id = _positive_id(service_id)
        self.source_user_key = source_user_key
        self.shared_user_ids = shared_user_ids
        self.timeout_seconds = timeout_seconds
        self.opener = urllib.request.build_opener(_RejectRedirects())

    def get_service_details(
        self,
        *,
        user_id: int | None = None,
        user_uuid: str | None = None,
    ) -> tuple[DirectoryUser, dict[str, Any]]:
        try:
            if (user_id is None) == (user_uuid is None):
                raise ValueError("Provide exactly one Directory ID or UUID")
            query = {"service_id": self.service_id}
            if user_uuid is not None:
                user_uuid = str(UUID(user_uuid))
                query["user_uuid"] = user_uuid
            else:
                query["user_id"] = _positive_id(user_id)
            payload = self._get("user-service-details?" + urlencode(query))
            if payload.get("status") != "success":
                raise ValueError("Unsuccessful Directory response")
            data = payload["data"]
            profile = data["user_details"]
            service = data["service_details"]
            if not isinstance(profile, dict) or not isinstance(service, dict):
                raise ValueError("Expected user and service details")
            directory_id = _positive_id(profile["id"])
            canonical = str(UUID(profile["uuid"]))
            if (user_id is not None and directory_id != user_id) or (
                user_uuid is not None and canonical != user_uuid
            ):
                raise ValueError("Directory identity mismatch")
            if _positive_id(service["id"]) != self.service_id:
                raise ValueError("Directory service mismatch")
            # Contract: a successful scoped response means Directory has already
            # checked account eligibility. Do not repeat its internal status checks.
            active = True
            mapping = profile.get("company_user_id")
            if mapping is None:
                mapping = {}
            if not isinstance(mapping, dict):
                raise ValueError("Expected ERP user mappings")
            source_ids = []
            for key, value in mapping.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("Invalid mapping key")
                if value is None or value == "":
                    continue
                if (
                    isinstance(value, str)
                    and value.isascii()
                    and value.isdigit()
                ):
                    value = int(value)
                source_ids.append((key, str(_positive_id(value))))
            user = DirectoryUser(
                directory_uuid=canonical,
                directory_user_id=directory_id,
                active=active,
                service_ids=frozenset({self.service_id}),
                granted_permissions=frozenset(),
                source_user_ids=tuple(sorted(source_ids)),
            )
            return user, dict(profile, _verified_service={'id': self.service_id, 'name': service.get('name', '')})
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise IntegrationUnavailable(
                "Invalid Directory identity/service response"
            ) from exc

    def get_user(self, user_id: int, service_id: int) -> DirectoryUser:
        if service_id != self.service_id:
            raise IntegrationUnavailable(
                "Directory service does not match integration"
            )
        return self.get_service_details(user_id=user_id)[0]

    def source_user_id(self, user: DirectoryUser) -> int:
        mapping = dict(user.source_user_ids)
        if (
            self.source_user_key is not None
            and self.source_user_key in mapping
        ):
            return _positive_id(int(mapping[self.source_user_key]))
        if self.shared_user_ids:
            return user.directory_user_id
        raise IntegrationUnavailable(
            "ERP user mapping is missing for this integration"
        )

    def _get(self, path: str) -> dict[str, Any]:
        # * Bound response size and omit credentials and response bodies from errors.*
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
