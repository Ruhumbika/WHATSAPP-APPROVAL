"""Generic ERP approval guard: no source-specific Python adapter required.

A registered ERP must expose a current-step verification endpoint. API bearer
credentials authorize intake; the independently configured verification endpoint
authorizes the actual person, company, record, version and workflow step.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import UUID

from .permissions import IntegrationUnavailable, WorkflowActor, WorkflowDenied

MAX_RESPONSE_BYTES = 32768


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new_url):
        return None


def _pairs(items):
    result = {}
    for key, val in items:
        if key in result:
            raise ValueError("Duplicate response key")
        result[key] = val
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON value")


def _field(value, name, maximum=200):
    if (not isinstance(value, str) or not value.strip()
            or len(value) > maximum or value != value.strip()):
        raise IntegrationUnavailable(f"Invalid verified {name}")
    return value


class GenericWorkflowGuard:
    def __init__(self, verify_url: str, token: str,
                 *, allow_local_http: bool = False, timeout_seconds: int = 10):
        parsed = urlsplit(verify_url)
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("Invalid verification URL") from exc
        loopback = (allow_local_http and parsed.scheme == "http"
                    and parsed.hostname in {"127.0.0.1", "localhost", "::1"})
        if (not parsed.hostname or (parsed.scheme != "https" and not loopback)
                or parsed.username or parsed.password or parsed.query
                or parsed.fragment or any(ord(c) <= 32 or ord(c) == 127
                                           for c in verify_url)):
            raise ValueError("Verification URL must use HTTPS (or test loopback)")
        if (not isinstance(token, str) or len(token) < 32
                or any(ord(c) <= 32 or ord(c) == 127 for c in token)):
            raise ValueError("Verification token must be >=32 printable characters")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30:
            raise ValueError("Invalid verification timeout")
        self.verify_url = verify_url
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.opener = urllib.request.build_opener(_NoRedirect())

    def authorize(self, request: Mapping[str, Any]) -> WorkflowActor:
        scope = ("company", "request_type", "reference_id",
                 "step_id", "actor_directory_uuid")
        for key in scope:
            _field(request.get(key), key)
        try:
            actor_uuid = str(UUID(request["actor_directory_uuid"]))
        except (ValueError, AttributeError):
            raise WorkflowDenied("Invalid actor identity") from None
        if actor_uuid != request["actor_directory_uuid"]:
            raise WorkflowDenied("Actor identity must be normalized")
        # Verification is independent from client-declared phone, role and authority.
        # Never send stored gateway evidence to the ERP as a grant.
        body = {key: request[key] for key in scope}
        if "workflow_version" in request:
            body["workflow_version"] = _field(
                request["workflow_version"], "workflow_version")
        raw = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        req = urllib.request.Request(
            self.verify_url, data=raw, method="POST",
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with self.opener.open(req, timeout=self.timeout_seconds) as response:
                if response.status == 403:
                    raise WorkflowDenied("ERP rejected approval assignment")
                if response.status != 200:
                    raise IntegrationUnavailable("Unexpected ERP verification status")
                if response.headers.get_content_type() != "application/json":
                    raise IntegrationUnavailable("ERP response must be JSON")
                content = response.read(MAX_RESPONSE_BYTES + 1)
            if len(content) > MAX_RESPONSE_BYTES:
                raise IntegrationUnavailable("ERP response too large")
            verified = json.loads(content.decode("utf-8"),
                                  object_pairs_hook=_pairs,
                                  parse_constant=_reject_constant)
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 404, 409, 422}:
                raise WorkflowDenied("ERP rejected approval assignment") from None
            raise IntegrationUnavailable("ERP verification failed") from None
        except (urllib.error.URLError, OSError, TimeoutError, ValueError,
                RecursionError, UnicodeError) as exc:
            raise IntegrationUnavailable("ERP verification unavailable") from exc

        if not isinstance(verified, dict) or set(verified) != {
                "authorized", "company", "request_type", "reference_id",
                "step_id", "actor_directory_uuid", "source_user_id",
                "phone_number", "module_id", "record_digest"}:
            raise IntegrationUnavailable("ERP verification contract mismatch")
        if verified["authorized"] is not True:
            raise WorkflowDenied("ERP approval assignment denied")
        for key in scope:
            if verified[key] != body[key]:
                raise WorkflowDenied(f"ERP verification {key} mismatch")
        for key in ("source_user_id", "module_id", "record_digest"):
            _field(verified[key], key)
        phone = _field(verified["phone_number"], "phone_number", 16)
        if not re.fullmatch(r"\+?[1-9][0-9]{6,14}", phone):
            raise IntegrationUnavailable("ERP returned invalid phone")
        if ("workflow_version" in body
                and verified["record_digest"] != body["workflow_version"]):
            raise WorkflowDenied("ERP record version changed")
        return WorkflowActor(
            directory_uuid=actor_uuid,
            source_user_id=verified["source_user_id"],
            phone_number=phone,
            module_id=verified["module_id"],
            step_id=verified["step_id"],
            record_digest=verified["record_digest"])


def build_adapter(options: Mapping[str, object],
                  environment: Mapping[str, str]) -> GenericWorkflowGuard:
    if (not isinstance(options, dict)
            or set(options) - {"verification_url", "token_env",
                               "allow_local_http", "timeout_seconds"}
            or not {"verification_url", "token_env"}.issubset(options)):
        raise ValueError("Invalid generic integration options")
    variable = options["token_env"]
    if not isinstance(variable, str) or not variable.isidentifier():
        raise ValueError("token_env must be environment variable name")
    token = environment.get(variable, "")
    return GenericWorkflowGuard(
        options["verification_url"], token,
        allow_local_http=options.get("allow_local_http", False),
        timeout_seconds=options.get("timeout_seconds", 10))
