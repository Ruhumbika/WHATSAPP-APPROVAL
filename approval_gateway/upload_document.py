"""Upload an operator-selected local PDF to Meta and return its media ID."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .config import Settings, load_settings

# Gateway limits are independent of Meta's supported media limits.
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_RESPONSE_BYTES = 65_536
UPLOAD_TIMEOUT_SECONDS = 60


class MediaUploadError(RuntimeError):
    pass


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    # Never forward the access token to a redirected endpoint.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_pdf(path: str | Path) -> bytes:
    path = Path(path).expanduser()

    if path.suffix.lower() != ".pdf":
        raise ValueError("Choose a local file with a .pdf extension.")

    # Inspect the opened file and bound the read even if its size changes.
    try:
        with path.open("rb") as handle:
            file_info = os.fstat(handle.fileno())

            if not stat.S_ISREG(file_info.st_mode):
                raise ValueError("PDF input must be a regular file.")

            if not 0 < file_info.st_size <= MAX_PDF_BYTES:
                raise ValueError("Gateway PDF upload limit is 20 MiB.")

            content = handle.read(MAX_PDF_BYTES + 1)

    except OSError:
        raise ValueError("The selected PDF could not be opened.") from None

    if not content or len(content) > MAX_PDF_BYTES:
        raise ValueError("Gateway PDF upload limit is 20 MiB.")

    # This checks the header only, not PDF integrity or document correctness.
    if not content.startswith(b"%PDF-"):
        raise ValueError("The selected file has no PDF header.")

    return content


def _multipart_body(content: bytes, boundary: str) -> bytes:
    # Use a fixed filename so local paths never enter multipart headers.
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="messaging_product"\r\n'
        "\r\n"
        "whatsapp\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="type"\r\n'
        "\r\n"
        "application/pdf\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="Approval.pdf"\r\n'
        "Content-Type: application/pdf\r\n"
        "\r\n"
    ).encode("ascii")

    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")

    return prefix + content + suffix


def upload_pdf(settings: Settings, path: str | Path) -> str:
    # Simulation must never perform a real media upload.
    if settings.dry_run_whatsapp:
        raise ValueError("Real PDF upload requires DRY_RUN_WHATSAPP=false.")

    token = settings.whatsapp_access_token
    phone_id = settings.whatsapp_phone_number_id
    version = settings.whatsapp_api_version

    if not token.strip():
        raise ValueError("WhatsApp access token is missing.")

    if re.fullmatch(r"[0-9]+", phone_id) is None:
        raise ValueError("Invalid WhatsApp phone number ID.")

    if re.fullmatch(r"v[0-9]+\.[0-9]+", version) is None:
        raise ValueError("Invalid Graph API version format.")

    content = _read_pdf(path)
    boundary = "gateway-" + uuid.uuid4().hex

    # Avoid a boundary collision with the uploaded content.
    while boundary.encode("ascii") in content:
        boundary = "gateway-" + uuid.uuid4().hex

    request = urllib.request.Request(
        (f"https://graph.facebook.com/" f"{version}/{phone_id}/media"),
        data=_multipart_body(content, boundary),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )

    opener = urllib.request.build_opener(_RejectRedirects())

    try:
        with opener.open(
            request,
            timeout=UPLOAD_TIMEOUT_SECONDS,
        ) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)

    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()

        raise MediaUploadError(
            f"Meta rejected the PDF upload: HTTP {status}."
        ) from None

    except (urllib.error.URLError, TimeoutError, OSError):
        raise MediaUploadError(
            "Upload connection failed; Meta may have received the PDF."
        ) from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise MediaUploadError("Meta upload response is too large.")

    try:
        result = json.loads(raw)
    except ValueError:
        raise MediaUploadError("Meta returned invalid JSON.") from None

    media_id = result.get("id") if isinstance(result, dict) else None

    if (
        not isinstance(media_id, str)
        or re.fullmatch(r"[0-9]+", media_id) is None
    ):
        raise MediaUploadError("Meta returned no valid media ID.")

    return media_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "pdf_path",
        help="Path to the trusted local approval PDF.",
    )
    args = parser.parse_args()

    try:
        media_id = upload_pdf(load_settings(), args.pdf_path)
    except (ValueError, MediaUploadError) as exc:
        parser.exit(1, f"Upload failed: {exc}\n")

    # Output can be copied into the approval request's document_media_id.
    print(json.dumps({"document_media_id": media_id}))


if __name__ == "__main__":
    main()
