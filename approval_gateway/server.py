from __future__ import annotations
import json
import logging
import re
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from .config import Settings, load_settings
from .db import init_db
from .integrations.bootstrap import bootstrap_integrations
from .meta import extract_responses
from .security import verify_meta_signature
from .service import ServiceError, cancel, create, decide, read_request
from .upgrade import migrate
from .workflow_events import accept_workflow_event, init_workflow_events

logger = logging.getLogger(__name__)
APP_VERSION = "2.2"
MAX_BODY_BYTES = 262_144
SOCKET_TIMEOUT_SECONDS = 15
_REQUEST_PATH = re.compile(
    r"/api/approval-requests/(AGR-[A-Z0-9]{1,96})(/cancel)?"
)


def _reject_json_constant(value: str) -> None:
    raise ValueError("Non-finite JSON values are not supported.")


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:

    # Duplicate keys must not silently overwrite earlier input.
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ApprovalGateway"
    sys_version = ""

    @property
    def settings(self) -> Settings:
        return self.server.settings

    def log_message(self, fmt: str, *args: Any) -> None:

        # Exclude paths, query tokens, credentials and request bodies.
        logger.info("HTTP request completed.")

    def _header(self, name: str) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise ServiceError(f"Duplicate {name} header.")
        return values[0] if values else None

    def _write(
        self,
        body: bytes,
        status: int,
        content_type: str,
    ) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def reply(self, data: Any, status: int = 200) -> None:
        body = json.dumps(
            data,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self._write(body, status, "application/json; charset=utf-8")

    def body(self) -> bytes:

        # Require one explicit body length; chunked requests are unsupported.
        if self._header("Transfer-Encoding") is not None:
            raise ServiceError("Transfer-Encoding is unsupported.", 400)
        raw_length = self._header("Content-Length")
        if raw_length is None:
            raise ServiceError("Content-Length is required.", 411)
        if re.fullmatch(r"[0-9]{1,10}", raw_length) is None:
            raise ServiceError("Invalid Content-Length.")
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            raise ServiceError("Request body is too large.", 413)
        content_type = self._header("Content-Type") or ""
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise ServiceError("Content-Type must be application/json.", 415)
        body = self.rfile.read(length)
        if len(body) != length:
            raise ServiceError("Incomplete request body.")
        return body

    @staticmethod
    def parse_json(body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(
                body.decode("utf-8"),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_json_object,
            )
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise ServiceError("Invalid JSON.") from None
        if not isinstance(data, dict):
            raise ServiceError("JSON object required.")
        return data

    def key(self) -> str:
        authorization = self._header("Authorization") or ""
        scheme, separator, key = authorization.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not separator
            or not key
            or any(char.isspace() for char in key)
            or len(key) > 4096
        ):
            raise ServiceError("A valid Bearer key is required.", 401)
        return key

    def _verify_webhook(self, query: str) -> None:

        # The verification handshake is separate from POST signature checks.
        token = self.settings.meta_verify_token
        if not token.strip() or token == "change-me":
            raise ServiceError("Webhook verification is not configured.", 503)
        try:
            values = parse_qs(
                query,
                keep_blank_values=True,
                max_num_fields=10,
            )
        except ValueError:
            raise ServiceError("Invalid verification query.") from None
        supplied = values.get("hub.verify_token", [])
        challenge = values.get("hub.challenge", [])
        if (
            values.get("hub.mode") != ["subscribe"]
            or len(supplied) != 1
            or not secrets.compare_digest(
                supplied[0].encode("utf-8"),
                token.encode("utf-8"),
            )
        ):
            raise ServiceError("Verification failed.", 403)
        if (
            len(challenge) != 1
            or re.fullmatch(r"[0-9]{1,200}", challenge[0]) is None
        ):
            raise ServiceError("Invalid verification challenge.")
        self._write(
            challenge[0].encode("ascii"),
            200,
            "text/plain; charset=utf-8",
        )

    def _receive_webhook(self) -> None:
        settings = self.settings

        logger.info("WhatsApp webhook received.")

        if (
            not settings.meta_app_secret.strip()
            or re.fullmatch(
                r"[0-9]+",
                settings.whatsapp_phone_number_id,
            )
            is None
        ):
            logger.error("WhatsApp webhook configuration invalid; HTTP 503.")
            raise ServiceError("Webhook processing is not configured.", 503)

        raw_body = self.body()

        # Authenticate the original request before parsing its contents.
        if not verify_meta_signature(
            raw_body,
            self._header("X-Hub-Signature-256"),
            settings.meta_app_secret,
        ):
            logger.warning("WhatsApp signature rejected; HTTP 403.")
            raise ServiceError("Invalid Meta signature.", 403)

        payload = self.parse_json(raw_body)
        responses = list(
            extract_responses(
                payload,
                expected_phone_number_id=settings.whatsapp_phone_number_id,
            )
        )
        logger.info(
            "WhatsApp signature verified; extracted decisions=%d.",
            len(responses),
        )

        results = []
        for response in responses:
            try:
                result = decide(settings, response)
                results.append(result)
                logger.info(
                    "WhatsApp decision processed=%s; reason=%s.",
                    result.get("processed"),
                    result.get("reason", ""),
                )
            except ServiceError as exc:
                logger.warning(
                    "WhatsApp decision rejected; service status=%s; reason=%s.",
                    exc.status,
                    str(exc),
                )

                # Preserve provider retries for transient integration failures.
                if exc.status >= 500:
                    raise

                results.append(
                    {
                        "processed": False,
                        "reason": str(exc),
                    }
                )

        logger.info("WhatsApp webhook acknowledged; HTTP 200.")
        self.reply({"ok": True, "results": results})

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/health":
            if method != "GET":
                raise ServiceError("Method not allowed.", 405)
            self.reply({"status": "ok", "version": APP_VERSION})
            return
        if path == "/webhook/whatsapp":
            if method == "GET":
                self._verify_webhook(parsed.query)
            elif method == "POST":
                self._receive_webhook()
            else:
                raise ServiceError("Method not allowed.", 405)
            return
            # Persist workflow triggers before acknowledging ERP delivery.
        if path == "/api/workflow-events":
            if method != "POST":
                raise ServiceError("Method not allowed.", 405)

            key = self.key()
            data = self.parse_json(self.body())
            result, created = accept_workflow_event(
                self.settings,
                key,
                data,
            )
            self.reply(result, 202 if created else 200)
            return
        if path == "/api/approval-requests":
            if method != "POST":
                raise ServiceError("Method not allowed.", 405)
            key = self.key()
            data = self.parse_json(self.body())
            request, created = create(self.settings, key, data)
            self.reply(
                {"approval_request": request, "created": created},
                201 if created else 200,
            )
            return
        match = _REQUEST_PATH.fullmatch(path)
        if match is not None:
            reference, cancel_suffix = match.groups()
            expected_method = "POST" if cancel_suffix else "GET"
            if method != expected_method:
                raise ServiceError("Method not allowed.", 405)
            key = self.key()
            if cancel_suffix:
                self.reply(cancel(self.settings, key, reference))
            else:
                self.reply(
                    {
                        "approval_request": read_request(
                            self.settings,
                            key,
                            reference,
                        )
                    }
                )
            return
        raise ServiceError("Not found.", 404)

    def handle_request(self, method: str) -> None:
        try:
            try:
                self._dispatch(method)
            except ServiceError as exc:
                self.reply({"error": str(exc)}, exc.status)
            except sqlite3.OperationalError:
                logger.error("Database operation failed.")
                self.reply({"error": "Service temporarily unavailable."}, 503)
            except Exception:
                logger.error("Unexpected request processing failure.")
                self.reply({"error": "Internal error."}, 500)
        except (ConnectionError, TimeoutError):

            # A disconnected client must not trigger another response attempt.
            logger.info("HTTP connection ended before completion.")

    def do_GET(self) -> None:
        self.handle_request("GET")

    def do_POST(self) -> None:
        self.handle_request("POST")

    def do_PUT(self) -> None:
        self.handle_request("PUT")

    def do_PATCH(self) -> None:
        self.handle_request("PATCH")

    def do_DELETE(self) -> None:
        self.handle_request("DELETE")


class Server(ThreadingHTTPServer):

    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__(
            (settings.host, settings.port),
            GatewayHandler,
        )

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(SOCKET_TIMEOUT_SECONDS)
        return sock, address

    def handle_error(self, request, client_address) -> None:
        logger.error("HTTP handler failed.")


def main() -> None:

    # Load settings at startup rather than during module import.
    settings = load_settings()
    Path(settings.log_path).parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(settings.log_path, encoding="utf-8"),
        ],
    )
    # Initialize all enabled adapters before opening the HTTP listener.
    registered_systems = bootstrap_integrations(settings)
    logger.info("Workflow adapters initialized: %d.", len(registered_systems))

    init_db(settings)
    migrate(settings)
    init_workflow_events(settings)

    with Server(settings) as server:
        logger.info("Approval gateway started.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Approval gateway stopping.")


if __name__ == "__main__":
    main()
