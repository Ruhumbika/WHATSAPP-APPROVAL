from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from .config import Settings
from .security import normalize_phone

logger = logging.getLogger(__name__)

HTTP_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 262_144

# Local limits keep template parameters and filenames manageable.
MAX_TEMPLATE_FIELD_LENGTH = 1000
MAX_FILENAME_LENGTH = 200

_DOCUMENT_FIELDS = {
    "workflow_approval_document": (
        "reference_id",
        "workflow_step",
        "requested_by",
        "details",
        "decision_scope",
    ),
    "requisition_approval_document": (
        "reference_id",
        "requested_by",
        "department",
        "amount_display",
        "reason",
    ),
}


class WhatsAppError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        api_code: int | None = None,
        delivery_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.api_code = api_code
        self.delivery_unknown = delivery_unknown


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    # Do not forward credentials to a redirected endpoint.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")

    value = value.strip()

    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters.")

    return value


class WhatsAppClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._opener = urllib.request.build_opener(_RejectRedirects())

    def _body_parameters(
        self,
        company: str,
        message: str,
        details: dict[str, Any],
    ) -> list[dict[str, str]]:
        template = self.settings.whatsapp_template_name

        # Parameter order must match the registered template.
        if template in _DOCUMENT_FIELDS:
            values = [
                _text(
                    company,
                    "company",
                    MAX_TEMPLATE_FIELD_LENGTH,
                )
            ]

            for field in _DOCUMENT_FIELDS[template]:
                value = _text(
                    details.get(field),
                    field,
                    MAX_TEMPLATE_FIELD_LENGTH,
                )

                values.append(" ".join(value.split()))

        elif template == "approval_request":
            values = [
                _text(company, "company", MAX_TEMPLATE_FIELD_LENGTH),
                _text(message, "message", MAX_TEMPLATE_FIELD_LENGTH),
            ]

        else:
            raise ValueError("Template is not supported by this client.")

        return [{"type": "text", "text": value} for value in values]

    def _document_header(
        self,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        # Use an uploaded Meta media ID rather than a caller-supplied URL.
        media_id = details.get("document_media_id")

        if isinstance(media_id, int) and not isinstance(media_id, bool):
            media_id = str(media_id)

        if (
            not isinstance(media_id, str)
            or re.fullmatch(r"[0-9]+", media_id) is None
        ):
            raise ValueError(
                "document_media_id must be a numeric Meta media ID."
            )

        filename = _text(
            details.get("document_filename", "Approval.pdf"),
            "document_filename",
            MAX_FILENAME_LENGTH,
        )

        if (
            any(char in filename for char in ("/", "\\"))
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)
            or not filename.lower().endswith(".pdf")
        ):
            raise ValueError("document_filename must be a plain PDF filename.")

        return {
            "type": "header",
            "parameters": [
                {
                    "type": "document",
                    "document": {
                        "id": media_id,
                        "filename": filename,
                    },
                }
            ],
        }

    @staticmethod
    def _decision_button(
        index: str,
        action: str,
        reference: str,
    ) -> dict[str, Any]:
        # Preserve the payload format consumed by the webhook parser.
        return {
            "type": "button",
            "sub_type": "quick_reply",
            "index": index,
            "parameters": [
                {
                    "type": "payload",
                    "payload": f"{action}_{reference}",
                }
            ],
        }

    def send_approval_request(
        self,
        to_phone_number: str,
        company: str,
        message: str,
        gateway_reference_id: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        phone = normalize_phone(to_phone_number)

        if details is None:
            details = {}

        if not isinstance(details, dict):
            raise ValueError("details must be an object.")

        reference = _text(
            gateway_reference_id,
            "gateway_reference_id",
            100,
        )

        if re.fullmatch(r"AGR-[A-Z0-9]+", reference) is None:
            raise ValueError("Invalid gateway reference.")

        template = self.settings.whatsapp_template_name
        components = []

        if template in _DOCUMENT_FIELDS:
            components.append(self._document_header(details))

        components.append(
            {
                "type": "body",
                "parameters": self._body_parameters(
                    company,
                    message,
                    details,
                ),
            }
        )

        components.extend(
            [
                self._decision_button("0", "approve", reference),
                self._decision_button("1", "reject", reference),
            ]
        )

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "template",
            "template": {
                "name": template,
                "language": {
                    "code": self.settings.whatsapp_template_language,
                },
                "components": components,
            },
        }

        # Validate the same payload in simulation and live mode.
        if self.settings.dry_run_whatsapp:
            logger.info("WhatsApp approval delivery simulated.")
            return "dry-run-" + reference

        return self._message_id(self._post_graph(payload))

    def send_text(self, to_phone_number: str, text: str) -> None:
        # The caller must confirm an open customer-service window.
        phone = normalize_phone(to_phone_number)
        text = _text(text, "text", 4096)

        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": text},
        }

        if self.settings.dry_run_whatsapp:
            logger.info("WhatsApp text delivery simulated.")
            return

        self._message_id(self._post_graph(payload))

    @staticmethod
    def _message_id(response: dict[str, Any]) -> str:
        # Missing acknowledgement must not be recorded as successful delivery.
        messages = response.get("messages")

        if (
            not isinstance(messages, list)
            or not messages
            or not isinstance(messages[0], dict)
        ):
            raise WhatsAppError(
                "Meta response has no message acknowledgement.",
                delivery_unknown=True,
            )

        message_id = messages[0].get("id")

        if not isinstance(message_id, str) or not message_id.strip():
            raise WhatsAppError(
                "Meta response has no valid message ID.",
                delivery_unknown=True,
            )

        return message_id

    def _post_graph(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        token = self.settings.whatsapp_access_token
        phone_id = self.settings.whatsapp_phone_number_id
        version = self.settings.whatsapp_api_version

        if not token.strip():
            raise WhatsAppError("WhatsApp access token is missing.")

        if re.fullmatch(r"[0-9]+", phone_id) is None:
            raise WhatsAppError("Invalid WhatsApp phone number ID.")

        if re.fullmatch(r"v[0-9]+\.[0-9]+", version) is None:
            raise WhatsAppError("Invalid Graph API version format.")

        url = f"https://graph.facebook.com/" f"{version}/{phone_id}/messages"

        request = urllib.request.Request(
            url,
            data=json.dumps(
                payload,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with self._opener.open(
                request,
                timeout=HTTP_TIMEOUT_SECONDS,
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)

        except urllib.error.HTTPError as exc:
            # Expose error codes without logging raw provider response bodies.
            try:
                with exc:
                    raw_error = exc.read(MAX_RESPONSE_BYTES)

                error_response = json.loads(raw_error)
                error = (
                    error_response.get("error", {})
                    if isinstance(error_response, dict)
                    else {}
                )
                code = error.get("code") if isinstance(error, dict) else None
            except (ValueError, OSError):
                code = None

            api_code = code if type(code) is int else None

            raise WhatsAppError(
                f"Meta rejected the request: HTTP {exc.code}, "
                f"API code {api_code}.",
                http_status=exc.code,
                api_code=api_code,
                delivery_unknown=exc.code >= 500,
            ) from None

        except (urllib.error.URLError, TimeoutError, OSError):
            # A lost response does not prove that Meta rejected the message.
            raise WhatsAppError(
                "Meta connection failed; message acceptance is unknown.",
                delivery_unknown=True,
            ) from None

        if len(raw) > MAX_RESPONSE_BYTES:
            raise WhatsAppError(
                "Meta response exceeded the allowed size.",
                delivery_unknown=True,
            )

        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise WhatsAppError(
                "Meta returned invalid JSON.",
                delivery_unknown=True,
            ) from None

        if not isinstance(result, dict):
            raise WhatsAppError(
                "Meta returned an unexpected response.",
                delivery_unknown=True,
            )

        return result
