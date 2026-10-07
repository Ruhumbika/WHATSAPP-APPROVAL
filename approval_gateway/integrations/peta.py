"""Resolve PETA's outstanding approval level and Directory recipients."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID
from urllib.parse import urlsplit
from datetime import date
from decimal import Decimal, InvalidOperation

from .directory_client import (
    MAX_RESPONSE_BYTES,
    UserDirectoryClient,
    _positive_id,
    _RejectRedirects,
    _reject_constant,
    _unique_object,
)
from .permissions import (
    DirectoryUser,
    IntegrationUnavailable,
    WorkflowActor,
    WorkflowDenied,
)


# Keep Directory identity and contact evidence separate from workflow assignment.
@dataclass(frozen=True)
class ApprovalRecipient:
    peta_user_id: int
    directory_user: DirectoryUser
    username: str
    phone_number: str
    phone_verified: bool


@dataclass(frozen=True)
class PendingStep:
    requisition_id: int
    module_id: int
    level_id: int
    level_order: int
    action_name: str
    position_name: str
    assigned_users: tuple[dict[str, Any], ...]


# PETA authentication is optional; local HTTP requires an explicit opt-in.
class PetaClient:
    def __init__(
        self,
        base_url: str,
        access_token: str = "",
        *,
        allow_local_http: bool = False,
        timeout_seconds: int = 10,
        document_access_token: str = "",
    ) -> None:
        url = urlsplit(base_url)
        url.port
        local_http = (
            allow_local_http is True
            and url.scheme == "http"
            and url.hostname in {"127.0.0.1", "localhost", "::1"}
        )
        if (
            not url.hostname
            or (url.scheme != "https" and not local_http)
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(ord(char) <= 32 or ord(char) == 127 for char in base_url)
        ):
            raise ValueError(
                "PETA URL must use HTTPS or explicitly allowed loopback HTTP"
            )
        if not isinstance(access_token, str) or any(
            ord(char) < 33 or ord(char) == 127 for char in access_token
        ):
            raise ValueError("Invalid PETA access token")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise ValueError("PETA timeout must be between 1 and 60 seconds")
        self.base_url = base_url.rstrip("/")
        if not isinstance(document_access_token, str) or any(
            ord(char) < 33 or ord(char) == 127
            for char in document_access_token
        ):
            raise ValueError("Invalid PETA document access token")
        self.document_access_token = document_access_token
        self.access_token = access_token
        self.timeout_seconds = timeout_seconds
        self.opener = urllib.request.build_opener(_RejectRedirects())

    def _get(self, path: str) -> dict[str, Any]:
        # Bound JSON responses and prevent credential forwarding through redirects.
        headers = {"Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        request = urllib.request.Request(
            f"{self.base_url}/{path}",
            headers=headers,
            method="GET",
        )
        try:
            with self.opener.open(
                request, timeout=self.timeout_seconds
            ) as response:
                if response.status != 200:
                    raise ValueError("Unexpected PETA HTTP status")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("PETA response exceeds the size limit")
            result = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if not isinstance(result, dict):
                raise ValueError("PETA response must be an object")
            return result
        except (
            urllib.error.URLError,
            OSError,
            ValueError,
            RecursionError,
        ) as exc:
            raise IntegrationUnavailable("PETA request failed") from exc

    def get_requisition(self, requisition_id: int) -> dict[str, Any]:
        # The existing details route must expose company and lifecycle fields.
        requisition_id = _positive_id(requisition_id)
        response = self._get(f"requisitions/{requisition_id}/details")
        try:
            record = response["data"]
            if response.get("success") is not True or not isinstance(
                record, dict
            ):
                raise ValueError("Unsuccessful PETA details response")
            if _positive_id(record["id"]) != requisition_id:
                raise ValueError("Requisition identity mismatch")
            company = record["company_id"]
            if not (
                (type(company) is int and company > 0)
                or (
                    isinstance(company, str)
                    and re.fullmatch(r"[1-9][0-9]*", company)
                )
            ):
                raise ValueError("Invalid requisition company")
            if type(record["is_closed"]) not in (int, bool) or record[
                "is_closed"
            ] not in (0, 1):
                raise ValueError("Invalid closed flag")
            if record["status"] not in {
                "DRAFT",
                "SUBMITTED",
                "PENDING",
                "APPROVED",
                "REJECTED",
                "CANCELLED",
                "CLOSED",
            }:
                raise ValueError("Unknown requisition status")
            if not isinstance(record["items"], list):
                raise ValueError("Requisition items are missing")
            return record
        except (ValueError, TypeError, KeyError) as exc:
            raise IntegrationUnavailable(
                "Invalid PETA requisition details"
            ) from exc

    def get_document(self, requisition_id: int) -> bytes:
        # Fetch only the dedicated read-only PDF route; never call the print action.
        requisition_id = _positive_id(requisition_id)
        if len(self.document_access_token) < 32:
            raise WorkflowDenied(
                "PETA document integration credential is missing"
            )
        request = urllib.request.Request(
            f"{self.base_url}/whatsapp-approvals/requisitions/{requisition_id}/document",
            headers={
                "Accept": "application/pdf",
                "Authorization": f"Bearer {self.document_access_token}",
            },
        )
        try:
            with self.opener.open(
                request, timeout=self.timeout_seconds
            ) as response:
                if (
                    response.status != 200
                    or response.headers.get_content_type() != "application/pdf"
                ):
                    raise ValueError("Expected a PDF response")
                content = response.read(20 * 1024 * 1024 + 1)
            if (
                not content.startswith(b"%PDF-")
                or len(content) > 20 * 1024 * 1024
            ):
                raise ValueError("Invalid PDF content or size")
            return content
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise IntegrationUnavailable(
                "PETA approval document is unavailable"
            ) from exc

    def get_pending_step(self, requisition_id: int) -> PendingStep | None:
        requisition_id = _positive_id(requisition_id)
        payload = self._get(f"requisitions/{requisition_id}/approval-chain")
        try:
            return self._parse_step(payload, requisition_id)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise IntegrationUnavailable(
                "Invalid PETA approval chain"
            ) from exc

    def _parse_step(
        self, payload: dict[str, Any], requisition_id: int
    ) -> PendingStep | None:
        # Select the first outstanding active level in ERP-defined order.
        if payload.get("success") is not True:
            raise ValueError("PETA approval chain was unsuccessful")
        data = payload["data"]
        if _positive_id(data["requisition_id"]) != requisition_id:
            raise ValueError("Requisition identity mismatch")
        module = data["module"]
        if module is None:
            raise ValueError("No approval module configured")
        module_id = _positive_id(module["id"])
        levels = data["levels"]
        if not isinstance(levels, list) or not levels:
            raise ValueError("No approval levels configured")
        active_levels = []
        seen_ids: set[int] = set()
        seen_orders: set[int] = set()
        for level in levels:
            level_id = _positive_id(level["id"])
            order = _positive_id(level["level_id"])
            if type(level["is_active"]) is not bool:
                raise ValueError("Invalid level activity flag")
            if not level["is_active"]:
                continue
            if level_id in seen_ids or order in seen_orders:
                raise ValueError("Ambiguous approval level ordering")
            seen_ids.add(level_id)
            seen_orders.add(order)
            if level["approval_status"] not in (
                "PENDING",
                "APPROVED",
                "REJECTED",
            ):
                raise ValueError("Unknown approval status")
            active_levels.append(level)
        if not active_levels:
            raise ValueError("No active approval levels")
        # A rejected chain has no next notification candidate.
        if any(
            level["approval_status"] == "REJECTED" for level in active_levels
        ):
            return None
        for level in sorted(active_levels, key=lambda item: item["level_id"]):
            if level["approval_status"] == "APPROVED":
                continue
            assigned = level["approvers"]
            if not isinstance(assigned, list) or not assigned:
                raise ValueError("Pending level has no assigned users")
            users = []
            seen_users: set[int] = set()
            for user in assigned:
                user_id = _positive_id(user["id"])
                if user_id in seen_users:
                    continue
                seen_users.add(user_id)
                users.append(
                    {
                        "id": user_id,
                        "username": user["username"],
                        "email": user["email"],
                    }
                )
            role = level["role"] or {}
            position = level["position"] or {}
            return PendingStep(
                requisition_id=requisition_id,
                module_id=module_id,
                level_id=level["id"],
                level_order=level["level_id"],
                action_name=str(role.get("name") or "Approve"),
                position_name=str(position.get("role_name") or "Approver"),
                assigned_users=tuple(users),
            )
        return None


# Resolve centrally synchronized PETA users through the actual Directory endpoint.
class PetaDirectoryClient(UserDirectoryClient):
    def resolve_recipient(
        self, assigned_user: dict[str, Any]
    ) -> ApprovalRecipient:
        try:
            user_id = _positive_id(assigned_user["id"])
            payload = self._get(f"users/{user_id}/permissions")
            profile = payload["user_details"]
            directory_uuid = str(UUID(profile["uuid"]))
            if (
                payload.get("status") != "success"
                or _positive_id(profile["id"]) != user_id
            ):
                raise ValueError("Directory identity mismatch")
            active_flag = profile["is_active"]
            if type(active_flag) is not int or active_flag != 1:
                raise ValueError("Active Directory identity required")
            if (
                profile["banned_at"] is not None
                or profile["banned_until"] is not None
            ):
                raise ValueError("Directory identity has a ban marker")
            # Directory supplies identity and contacts, not approval-chain authority.
            user = DirectoryUser(
                directory_uuid=directory_uuid,
                directory_user_id=user_id,
                active=True,
                service_ids=frozenset(),
                granted_permissions=frozenset(),
            )
            # Compare identity attributes as well as the centrally synchronized ID.
            for field in ("username", "email"):
                local_value = assigned_user[field]
                remote_value = profile[field]
                if (
                    not isinstance(local_value, str)
                    or not local_value.strip()
                    or not isinstance(remote_value, str)
                    or local_value.strip().casefold()
                    != remote_value.strip().casefold()
                ):
                    raise ValueError(
                        "PETA and Directory identity do not match"
                    )
            phone = profile["phone_number"]
            if not isinstance(phone, str) or not phone.strip():
                raise ValueError("Directory phone number required")
            return ApprovalRecipient(
                peta_user_id=user_id,
                directory_user=user,
                username=profile["username"],
                phone_number=phone.strip(),
                phone_verified=profile["phone_verified_at"] is not None,
            )
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise IntegrationUnavailable(
                "PETA recipient could not be verified"
            ) from exc


# Return candidates; company scope and verified phone bindings are checked before send.
def resolve_recipients(
    peta: PetaClient, directory: PetaDirectoryClient, requisition_id: int
) -> tuple[PendingStep | None, tuple[ApprovalRecipient, ...]]:
    step = peta.get_pending_step(requisition_id)
    if step is None:
        return None, ()
    recipients = tuple(
        directory.resolve_recipient(user) for user in step.assigned_users
    )
    return step, recipients


def _detailed_message_fields(record: Mapping[str, Any]) -> dict[str, str]:
    # Format display fields without modifying the signed ERP snapshot.
    def text(value: Any, fallback: str = "Not provided") -> str:
        if value is None:
            return fallback
        cleaned = " ".join(str(value).split())
        return cleaned or fallback

    def display_date(value: Any) -> str:
        if not value:
            return "Not provided"
        try:
            return date.fromisoformat(str(value)).strftime("%d %b %Y")
        except ValueError:
            raise IntegrationUnavailable("Invalid PETA request date") from None

    def number(value: Any) -> Decimal:
        if value is None or isinstance(value, bool):
            raise IntegrationUnavailable("PETA amount is missing or invalid")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise IntegrationUnavailable("Invalid PETA amount") from None
        if not result.is_finite():
            raise IntegrationUnavailable("Invalid PETA amount")
        return result

    company_name = record.get("company_name")
    if not isinstance(company_name, str) or not company_name.strip():
        raise IntegrationUnavailable("PETA company name is missing")

    items = record.get("items")
    sources = record.get("sources", [])
    if not isinstance(items, list) or not items:
        raise IntegrationUnavailable("PETA request has no items")
    if not isinstance(sources, list):
        raise IntegrationUnavailable("Invalid PETA payment sources")

    # Keep currencies separate and use totals supplied by the ERP.
    totals: dict[str, list[Decimal]] = {}
    summaries: list[str] = []
    for item in items:
        currency = text(item.get("currency"), "")
        if not currency:
            raise IntegrationUnavailable("PETA item currency is missing")

        amounts = totals.setdefault(
            currency, [Decimal("0"), Decimal("0"), Decimal("0")]
        )
        for index, field in enumerate(
            ("amount_before_vat", "vat_amount", "total_amount")
        ):
            amounts[index] += number(item.get(field))

        for material in item.get("materials", []):
            quantity = number(material.get("quantity"))
            rate = number(material.get("rate"))
            summaries.append(
                f"{text(material.get('item_name'))}: "
                f"{quantity:,.2f} {text(material.get('unit'))} "
                f"× {text(material.get('currency'), currency)} {rate:,.2f}"
            )

        for account in item.get("accounts", []):
            summaries.append(
                f"{text(account.get('account_name'))}: "
                f"{text(account.get('currency'), currency)} "
                f"{number(account.get('amount')):,.2f}"
            )

    def amount_display(index: int) -> str:
        return "; ".join(
            f"{currency} {amounts[index]:,.2f}"
            for currency, amounts in sorted(totals.items())
        )

    def source_display(field: str) -> str:
        values = dict.fromkeys(
            text(source.get(field), "")
            for source in sources
            if source.get(field)
        )
        return "; ".join(value for value in values if value) or "Not provided"

    # Summarize lengthy requests explicitly; the PDF retains all item details.
    item_summary = "; ".join(summaries[:3]) or "See attached PDF"
    if len(summaries) > 3:
        item_summary += f"; +{len(summaries) - 3} more entries in PDF"
    if len(item_summary) > 260:
        item_summary = (
            item_summary[:220].rstrip()
            + "… Full item details in attached PDF."
        )

    reason = text(record.get("remarks"))
    if len(reason) > 180:
        reason = reason[:140].rstrip() + "… Full reason in attached PDF."

    return {
        "company_name": text(company_name),
        "reference_id": str(record["id"]),
        "request_title": text(record.get("requisition_type")),
        "requested_by": text(record.get("requested_by")),
        "created_by": text(record.get("created_by")),
        "cost_center": text(record.get("cost_center")),
        "request_date": display_date(record.get("date")),
        "required_date": display_date(record.get("required_date")),
        "fund_direction": text(record.get("fund_direction")),
        "item_summary": item_summary,
        "subtotal_display": amount_display(0),
        "vat_display": amount_display(1),
        "total_display": amount_display(2),
        "payee_display": source_display("payee_name"),
        "payment_method": source_display("mode_of_payment"),
        "reason": reason,
    }


class PetaWorkflowGuard:
    def __init__(
        self,
        peta: PetaClient,
        directory: PetaDirectoryClient,
        local_calling_code: str | None = None,
    ) -> None:
        if local_calling_code is not None and (
            not local_calling_code.isascii()
            or not local_calling_code.isdigit()
            or not 1 <= len(local_calling_code) <= 3
            or local_calling_code.startswith("0")
        ):
            raise ValueError("Invalid local calling code")
        self.peta = peta
        self.directory = directory
        self.local_calling_code = local_calling_code

    def authorize(self, request: Mapping[str, object]) -> WorkflowActor:

        # Read company and record state from PETA, never from a role label.
        if request.get("request_type") != "requisition_approval":
            raise WorkflowDenied("PETA request type is not configured")
        reference = request.get("reference_id")
        if not isinstance(reference, str) or not re.fullmatch(
            r"[1-9][0-9]*", reference
        ):
            raise WorkflowDenied(
                "PETA reference_id must be the numeric requisition ID"
            )
        requisition_id = int(reference)
        record = self.peta.get_requisition(requisition_id)
        company = str(record["company_id"])
        closed = record["is_closed"]
        status = record["status"]
        if company != request.get("company"):
            raise WorkflowDenied("Requisition belongs to a different company")
        if closed or status in ("DRAFT", "REJECTED", "CANCELLED", "CLOSED"):
            raise WorkflowDenied(
                "Requisition is not awaiting a workflow decision"
            )

        # The existing controller may set APPROVED before all chain levels finish.
        step = self.peta.get_pending_step(requisition_id)
        if step is None or str(step.level_id) != request.get("step_id"):
            raise WorkflowDenied(
                "PETA is no longer waiting for this approval step"
            )
        actor = None
        for assigned in step.assigned_users:
            candidate = self.directory.resolve_recipient(assigned)
            if candidate.directory_user.directory_uuid == request.get(
                "actor_directory_uuid"
            ):
                actor = candidate
                break
        if actor is None:
            raise WorkflowDenied(
                "Directory actor is not assigned to the current level"
            )

        phone = self._international_phone(actor.phone_number)
        digest = self._record_digest(record, step)
        return WorkflowActor(
            directory_uuid=actor.directory_user.directory_uuid,
            source_user_id=str(actor.peta_user_id),
            phone_number=phone,
            module_id=str(step.module_id),
            step_id=str(step.level_id),
            record_digest=digest,
        )

    def _international_phone(self, value: str) -> str:
        # Convert local contacts only with an explicitly configured country code.
        phone = re.sub(r"[+\s()\-]", "", value)
        if phone.startswith("0"):
            if self.local_calling_code is None:
                raise WorkflowDenied(
                    "Local phone requires a configured calling code"
                )
            phone = self.local_calling_code + phone[1:]
        if not re.fullmatch(r"[1-9][0-9]{6,14}", phone):
            raise WorkflowDenied(
                "Recipient phone is not a valid international number"
            )
        return phone

    @staticmethod
    def _record_digest(record: Mapping[str, Any], step: PendingStep) -> str:
        # Exclude display-only progress; retain record content and current assignment.
        evidence = {
            "record": {
                key: value
                for key, value in record.items()
                if key != "approval"
            },
            "module_id": step.module_id,
            "level_id": step.level_id,
            "action": step.action_name,
            "assigned_user_ids": sorted(
                user["id"] for user in step.assigned_users
            ),
        }
        try:
            raw = json.dumps(
                evidence,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (ValueError, TypeError, RecursionError) as exc:
            raise IntegrationUnavailable(
                "Invalid PETA workflow snapshot"
            ) from exc
        return hashlib.sha256(raw).hexdigest()

    def prepare_delivery(
        self, settings, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        # Prepare ERP-specific presentation and PDF media inside the adapter.
        details = json.loads(request["payload_json"])

        if (
            settings.whatsapp_template_name
            == "workflow_approval_detail_document_v2"
        ):
            snapshot = details.get("record_snapshot")
            if not isinstance(snapshot, dict):
                raise IntegrationUnavailable("PETA record snapshot is missing")
            details.update(_detailed_message_fields(snapshot))

        if settings.whatsapp_template_name not in {
            "workflow_approval_document",
            "requisition_approval_document",
            "workflow_approval_detail_document_v2",
        }:
            return details
        reference = request["source_reference_id"]
        if details.get("document_media_id"):
            return details
        if settings.dry_run_whatsapp:
            # A simulated ID satisfies local template validation without uploading a file.
            media_id = "1"
        else:
            from ..upload_document import upload_pdf

            content = self.peta.get_document(int(reference))
            with tempfile.TemporaryDirectory(
                prefix="approval-pdf-"
            ) as directory:
                path = Path(directory) / f"requisition-{reference}.pdf"
                path.write_bytes(content)
                try:
                    media_id = str(upload_pdf(settings, path))
                except RuntimeError as exc:
                    raise IntegrationUnavailable(
                        "Approval PDF upload failed"
                    ) from exc
        details["document_media_id"] = media_id
        details["document_filename"] = f"requisition-{reference}.pdf"
        return details

    def resolve_workflow_event(
        self, event: Mapping[str, object]
    ) -> tuple[dict[str, Any], ...]:
        # Read the ERP after each committed action; never advance the chain locally.
        if (
            event.get("source_system") != "peta"
            or event.get("request_type") != "requisition_approval"
        ):
            raise WorkflowDenied("PETA workflow event type is not configured")
        reference = event.get("reference_id")
        if not isinstance(reference, str) or not re.fullmatch(
            r"[1-9][0-9]*", reference
        ):
            raise WorkflowDenied(
                "PETA reference_id must be the numeric requisition ID"
            )
        requisition_id = int(reference)
        record = self.peta.get_requisition(requisition_id)
        if record["is_closed"] or record["status"] in {
            "DRAFT",
            "REJECTED",
            "CANCELLED",
            "CLOSED",
        }:
            return ()
        step, recipients = resolve_recipients(
            self.peta, self.directory, requisition_id
        )
        if step is None:
            return ()
        version = self._record_digest(record, step)
        tasks = []
        for recipient in recipients:
            # Validate contacts before returning any candidate tasks to the core.
            self._international_phone(recipient.phone_number)
            tasks.append(
                {
                    "source_system": "peta",
                    "request_type": "requisition_approval",
                    "reference_id": reference,
                    "company": str(record["company_id"]),
                    "step_id": str(step.level_id),
                    "workflow_version": version,
                    "actor_directory_uuid": recipient.directory_user.directory_uuid,
                    "message": (
                        f"Requisition {reference} requires {step.action_name} "
                        f"by {step.position_name}. Please review the request."
                    ),
                    "payload": {
                        "requisition_id": requisition_id,
                        "approval_chain_module_id": step.module_id,
                        "approval_chain_level_id": step.level_id,
                        "action_name": step.action_name,
                        "reference_id": reference,
                        "workflow_step": f"{step.action_name} — {step.position_name}",
                        "requested_by": record.get("requested_by")
                        or record.get("created_by")
                        or "Not provided",
                        "details": record.get("remarks")
                        or record.get("requisition_type")
                        or "Review attached requisition",
                        "decision_scope": "Current approval-chain level",
                        "record_snapshot": {
                            key: value
                            for key, value in record.items()
                            if key != "approval"
                        },
                        "items": record["items"],
                        "sources": record.get("sources", []),
                    },
                }
            )
        # Reject a changed assignment instead of publishing an outdated event snapshot.
        current = self.peta.get_pending_step(requisition_id)
        if current != step:
            raise IntegrationUnavailable(
                "PETA workflow changed during event resolution"
            )
        return tuple(tasks)


def build_adapter(
    options: Mapping[str, object], environment: Mapping[str, str]
) -> PetaWorkflowGuard:
    # Resolve PETA-specific credentials within the integration layer.
    required = {
        "api_base_url_env",
        "access_token_env",
        "directory_base_url",
        "directory_access_token_env",
        "local_calling_code",
    }
    if set(options) - (
        required | {"allow_local_http", "document_access_token_env"}
    ) or not required.issubset(options):
        raise ValueError("Invalid PETA adapter options")

    def credential(name):
        if not isinstance(name, str) or not name.isidentifier():
            raise ValueError("Expected environment variable name")
        value = environment.get(name)
        if not value or not value.strip():
            raise ValueError("Required integration setting is missing")
        return value

    directory_url = options["directory_base_url"]
    if not isinstance(directory_url, str):
        raise ValueError("Directory base URL is required")
    calling_code = options["local_calling_code"]
    if calling_code is not None and not isinstance(calling_code, str):
        raise ValueError("Calling code must be a string or null")
    token_name = options["access_token_env"]
    if token_name is not None and (
        not isinstance(token_name, str) or not token_name.isidentifier()
    ):
        raise ValueError(
            "PETA token reference must be an environment name or null"
        )
    token = environment.get(token_name, "") if token_name is not None else ""
    allow_http = options.get("allow_local_http", False)
    if type(allow_http) is not bool:
        raise ValueError("allow_local_http must be boolean")
    document_token_name = options.get("document_access_token_env")
    if document_token_name is not None and (
        not isinstance(document_token_name, str)
        or not document_token_name.isidentifier()
    ):
        raise ValueError("Invalid PETA document credential reference")
    document_token = (
        environment.get(document_token_name, "") if document_token_name else ""
    )
    return PetaWorkflowGuard(
        peta=PetaClient(
            credential(options["api_base_url_env"]),
            token,
            allow_local_http=allow_http,
            document_access_token=document_token,
        ),
        directory=PetaDirectoryClient(
            base_url=directory_url,
            access_token=credential(options["directory_access_token_env"]),
            user_ids={},
            service_access_permissions={},
        ),
        local_calling_code=calling_code,
    )
