"""Payment Advice support using the existing PETA identity and workflow policies."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from .peta import PetaClient, PetaWorkflowGuard, _detailed_message_fields
from .directory_client import _positive_id
from .permissions import IntegrationUnavailable, WorkflowDenied

REQUEST_TYPE = "payment_advice_approval"
PREFIX = "whatsapp-approvals/payment-advices"


class PaymentAdviceClient(PetaClient):
    """Expose the shared guard interface through dedicated read-only ERP routes."""

    def _get(self, path):
        if len(self.access_token) < 32:
            raise WorkflowDenied(
                "PETA Payment Advice integration credential is missing"
            )
        return super()._get(path)

    def get_requisition(self, reference_id):
        reference_id = _positive_id(reference_id)
        response = self._get(f"{PREFIX}/{reference_id}/details")
        try:
            record = response["data"]
            if response.get("success") is not True or not isinstance(
                record, dict
            ):
                raise ValueError("Invalid details response")
            if _positive_id(record["id"]) != reference_id:
                raise ValueError("Record identity mismatch")
            _positive_id(record["company_id"])
            if record["status"] not in {
                "DRAFT",
                "SUBMITTED",
                "PENDING",
                "APPROVED",
                "PARTIALLY_APPROVED",
                "REJECTED",
                "CANCELLED",
                "POSTED",
                "CLOSED",
            }:
                raise ValueError("Unknown status")
            if type(record["is_closed"]) is not bool:
                raise ValueError("Invalid closed flag")
            terminal = record["status"] not in {"PENDING", "SUBMITTED"}
            if record["is_closed"] != terminal:
                raise ValueError("Inconsistent record lifecycle")
            if not isinstance(record["items"], list) or not record["items"]:
                raise ValueError("Missing items")
            seen = set()
            for item in record["items"]:
                item_id = _positive_id(item["payment_request_item_id"])
                if item_id in seen:
                    raise ValueError("Duplicate items")
                seen.add(item_id)
                for key in ("amount", "review_amount"):
                    value = item[key]
                    if isinstance(value, bool):
                        raise ValueError("Invalid amount")
                    amount = Decimal(str(value))
                    if not amount.is_finite() or amount < 0:
                        raise ValueError("Invalid amount")
                if Decimal(str(item["review_amount"])) > Decimal(
                    str(item["amount"])
                ):
                    raise ValueError("Review amount exceeds requested amount")
            return record
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise IntegrationUnavailable(
                "Invalid PETA Payment Advice details"
            ) from exc

    def get_pending_step(self, reference_id):
        reference_id = _positive_id(reference_id)
        response = self._get(f"{PREFIX}/{reference_id}/approval-chain")
        try:
            if (
                _positive_id(response["data"]["payment_request_id"])
                != reference_id
            ):
                raise ValueError("Record identity mismatch")
            # Reuse the established strict level/assignment parser, internally only.
            response["data"]["requisition_id"] = reference_id
            return self._parse_step(response, reference_id)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise IntegrationUnavailable(
                "Invalid Payment Advice approval chain"
            ) from exc

    def get_document(self, reference_id):
        reference_id = _positive_id(reference_id)
        # Reuse bounded PDF validation and redirect protection from PetaClient.
        return self._read_payment_pdf(reference_id)

    def _read_payment_pdf(self, reference_id):
        import urllib.request
        import urllib.error

        if len(self.document_access_token) < 32:
            raise WorkflowDenied(
                "PETA document integration credential is missing"
            )
        request = urllib.request.Request(
            f"{self.base_url}/{PREFIX}/{reference_id}/document",
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
                    raise ValueError("Expected a PDF")
                content = response.read(20 * 1024 * 1024 + 1)
            if (
                not content.startswith(b"%PDF-")
                or len(content) > 20 * 1024 * 1024
            ):
                raise ValueError("Invalid PDF content or size")
            return content
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise IntegrationUnavailable(
                "Payment Advice PDF is unavailable"
            ) from exc


class PaymentAdviceGuard(PetaWorkflowGuard):
    def authorize(self, request):
        if request.get("request_type") != REQUEST_TYPE:
            raise WorkflowDenied("Unsupported Payment Advice request type")
        # The inherited guard verifies the current record, step, actor and digest.
        return super().authorize(
            {**request, "request_type": "requisition_approval"}
        )

    def resolve_workflow_event(self, event):
        if event.get("request_type") != REQUEST_TYPE:
            raise WorkflowDenied("Unsupported Payment Advice event type")
        tasks = super().resolve_workflow_event(
            {**event, "request_type": "requisition_approval"}
        )
        result = []
        for task in tasks:
            task["request_type"] = REQUEST_TYPE
            task["message"] = task["message"].replace(
                "Requisition ", "Payment Advice ", 1
            )
            payload = task["payload"]
            payload["payment_request_id"] = payload.pop("requisition_id")
            payload["details"] = (
                payload["record_snapshot"].get("remarks")
                or "Review attached Payment Advice"
            )
            payload["decision_scope"] = (
                "Approve the reviewed item amounts at the current approval-chain level"
            )
            result.append(task)
        return tuple(result)

    def prepare_delivery(self, settings, request):
        if request.get("request_type") != REQUEST_TYPE:
            raise WorkflowDenied("Unsupported Payment Advice delivery type")
        if settings.whatsapp_template_name == "requisition_approval_document":
            raise WorkflowDenied(
                "Payment Advice requires a generic workflow document template"
            )
        details = json.loads(request["payload_json"])
        snapshot = details.get("record_snapshot")
        if not isinstance(snapshot, dict):
            raise IntegrationUnavailable("Payment Advice snapshot is missing")
        if (
            settings.whatsapp_template_name
            == "workflow_approval_detail_document_v2"
        ):
            presentation = dict(snapshot)
            presentation["requisition_type"] = "Payment Advice"
            presentation["items"] = []
            for item in snapshot["items"]:
                presentation["items"].append(
                    {
                        "currency": item["currency"],
                        "amount_before_vat": item["review_amount"],
                        "vat_amount": "0",
                        "total_amount": item["review_amount"],
                        "accounts": [
                            {
                                "account_name": item.get("description")
                                or "Payment item",
                                "currency": item["currency"],
                                "amount": item["review_amount"],
                            }
                        ],
                    }
                )
            details.update(_detailed_message_fields(presentation))
            # Payment Advice has no independent VAT breakdown; do not invent one.
            details["vat_display"] = "Not separately stated in Payment Advice"
        if settings.whatsapp_template_name not in {
            "workflow_approval_document",
            "workflow_approval_detail_document_v2",
        }:
            raise WorkflowDenied("Unsupported Payment Advice template")
        reference = request["source_reference_id"]
        if not isinstance(reference, str) or not re.fullmatch(
            r"[1-9][0-9]*", reference
        ):
            raise WorkflowDenied("Invalid Payment Advice reference")
        if not details.get("document_media_id"):
            if settings.dry_run_whatsapp:
                media_id = "1"
            else:
                from ..upload_document import upload_pdf

                content = self.peta.get_document(int(reference))
                with tempfile.TemporaryDirectory(
                    prefix="payment-advice-pdf-"
                ) as directory:
                    path = Path(directory) / f"payment-advice-{reference}.pdf"
                    path.write_bytes(content)
                    try:
                        media_id = str(upload_pdf(settings, path))
                    except RuntimeError as exc:
                        raise IntegrationUnavailable(
                            "Payment Advice PDF upload failed"
                        ) from exc
            details["document_media_id"] = media_id
        details["document_filename"] = f"payment-advice-{reference}.pdf"
        return details


class PetaWorkflowRouter:
    def __init__(self, requisition, payment):
        self.requisition = requisition
        self.payment = payment
        # Preserve operator diagnostics used for Requisition.
        self.peta = requisition.peta

    def _guard(self, request):
        request_type = request.get("request_type")
        if request_type == "requisition_approval":
            return self.requisition
        if request_type == REQUEST_TYPE:
            return self.payment
        raise WorkflowDenied("PETA workflow request type is not configured")

    def authorize(self, request):
        return self._guard(request).authorize(request)

    def resolve_workflow_event(self, event):
        return self._guard(event).resolve_workflow_event(event)

    def prepare_delivery(self, settings, request):
        return self._guard(request).prepare_delivery(settings, request)


def with_payment_advice(requisition):
    source = requisition.peta
    client = PaymentAdviceClient(
        source.base_url,
        source.document_access_token,
        allow_local_http=source.base_url.startswith("http://"),
        timeout_seconds=source.timeout_seconds,
        document_access_token=source.document_access_token,
    )
    return PetaWorkflowRouter(
        requisition,
        PaymentAdviceGuard(
            client, requisition.directory, requisition.local_calling_code
        ),
    )
