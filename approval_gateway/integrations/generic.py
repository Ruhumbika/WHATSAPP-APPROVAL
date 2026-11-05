"""Generic ERP workflow verification adapter.

Each ERP supplies one HTTPS endpoint which confirms the current actor,
assignment, record state and immutable workflow digest. Fail closed.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import UUID
from collections.abc import Mapping

from ..security import normalize_phone
from .permissions import WorkflowActor, WorkflowDenied, IntegrationUnavailable


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GenericWorkflowGuard:
    def __init__(self, verify_url: str, token: str):
        url = urlsplit(verify_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or url.query:
            raise ValueError("Verification endpoint must be HTTPS")
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("Verification token is required")
        self.verify_url = verify_url
        self.token = token

    def authorize(self, request: Mapping[str, object]) -> WorkflowActor:
        values = {name: request.get(name) for name in (
            "company", "reference_id", "request_type", "step_id",
            "actor_directory_uuid", "workflow_version"
        )}
        if any(not isinstance(values[k], str) or not values[k]
               for k in ("company", "reference_id", "request_type",
                         "step_id", "actor_directory_uuid")):
            raise WorkflowDenied("Workflow identifiers missing")
        payload = request.get("payload", {})
        if not isinstance(payload, dict):
            raise WorkflowDenied("Invalid workflow payload")
        values["payload"] = {k: v for k, v in payload.items() if k != "_gateway_workflow"}
        try:
            body = json.dumps(values, sort_keys=True, allow_nan=False).encode()
        except (ValueError, TypeError):
            raise WorkflowDenied("Invalid workflow verification data") from None
        if len(body) > 65536:
            raise WorkflowDenied("Workflow verification request too large")
        req = urllib.request.Request(
            self.verify_url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token})
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=8) as resp:
                if resp.status != 200:
                    raise IntegrationUnavailable("ERP verification failed")
                raw = resp.read(16385)
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (403, 409, 422):
                raise WorkflowDenied("ERP denied workflow") from None
            raise IntegrationUnavailable("ERP verification failed") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise IntegrationUnavailable("ERP verification unavailable") from None
        if len(raw) > 16384:
            raise IntegrationUnavailable("ERP verification response too large")
        try:
            result = json.loads(raw)
        except ValueError:
            raise IntegrationUnavailable("Invalid ERP verification response") from None
        if not isinstance(result, dict) or result.get("authorized") is not True:
            raise WorkflowDenied("ERP did not authorize this workflow")
        actor = result.get("actor")
        if not isinstance(actor, dict):
            raise IntegrationUnavailable("Invalid ERP actor")
        try:
            directory_uuid = str(UUID(actor["directory_uuid"]))
            source_user_id = actor["source_user_id"]
            phone = normalize_phone(actor["phone_number"])
            module_id = actor["module_id"]
            step_id = actor["step_id"]
            digest = actor["record_digest"]
        except (KeyError, TypeError, ValueError):
            raise IntegrationUnavailable("Invalid ERP actor") from None
        if directory_uuid != str(UUID(values["actor_directory_uuid"])) or step_id != values["step_id"]:
            raise WorkflowDenied("Actor or step mismatch")
        if any(not isinstance(x, str) or not x.strip() or len(x) > 200
               for x in (source_user_id, module_id, step_id, digest)):
            raise IntegrationUnavailable("Invalid ERP workflow evidence")
        return WorkflowActor(directory_uuid, source_user_id, phone, module_id, step_id, digest)


def build_adapter(options: Mapping[str, object],
                  environment: Mapping[str, str]) -> GenericWorkflowGuard:
    if set(options) != {"verify_url", "verify_token_env"}:
        raise ValueError("Invalid generic adapter configuration")
    variable = options["verify_token_env"]
    if not isinstance(variable, str) or not variable.isidentifier():
        raise ValueError("Invalid token environment reference")
    return GenericWorkflowGuard(options["verify_url"], environment.get(variable, ""))
