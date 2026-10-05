# Document approval setup — v2.1

This adds document sending to v2. It does not replace ERP approval chains. Keep your existing configuration and SQLite database when upgrading. Back up both first; extract this release into a separate folder and copy its approval_gateway code into your existing project. Do not overwrite your credentials, registered integrations, routing rules or database. No new DB migration is required by the document feature.

## 1. Create the matching Meta template

On the SAME WABA used by your gateway, create:
- Name: requisition_approval_document
- Language: Swahili (sw)
- Category: Utility (Meta decides eligibility/category)
- Parameter format: Number / positional
- Header: Document. Upload a synthetic sample PDF for review (no confidential real data).
- Footer: none
- Quick reply buttons in this exact order: Approve, Reject

Body:

Ombi la idhini — {{1}}

Requisition: {{2}}
Mwombaji: {{3}}
Idara: {{4}}
Jumla: {{5}}
Sababu: {{6}}

Tafadhali kagua document iliyoambatanishwa kabla ya kufanya uamuzi.

Chagua Approve kuidhinisha au Reject kukataa ombi hili.

Sample values: PETA Holding; REQ-9; Workshop Manager; Workshop; TZS 250,000.00; Kubadilisha vipuri vilivyochakaa.

The review sample is NOT the PDF sent on every request. Each real request supplies its own uploaded Meta media ID. Wait for Approved before sending.

## 2. Configuration

Keep current token, app secret, phone number ID and verify token. Change only:

WHATSAPP_TEMPLATE_NAME=requisition_approval_document
WHATSAPP_TEMPLATE_LANGUAGE=sw
DRY_RUN_WHATSAPP=false

Existing legacy approval_request template remains supported when selected instead.
Restart gateway after configuration changes. Exported environment variables override files. API version stays at your configured supported version.

## 3. Upload an ERP PDF snapshot

Run from the project directory:

python3 -m approval_gateway.upload_document /absolute/path/Requisition_REQ-DOC-10.pdf

Returns: {"document_media_id":"..."}. This is a REAL authenticated upload to Meta and requires dry run off. Files must be local trusted PDFs with a PDF signature, maximum 20 MiB (gateway limit). The API never accepts arbitrary local file paths. The gateway does not fetch ERP document URLs.

For ERP automation, the ERP may upload via POST /PHONE_NUMBER_ID/media (multipart: messaging_product=whatsapp, type=application/pdf, file=PDF) using protected credentials, then include the returned ID. Alternatively build an authenticated ERP-to-gateway upload endpoint in a future change; that endpoint is NOT included here. Do not store Meta credentials in browser code.

## 4. Create a fresh request

Edit sample_document_approval_request.json, replacing payload.document_media_id with the numeric returned ID. Use a fresh reference matching an actual test record and the current workflow version/step. Keep source_system/company aligned with existing registrations/routing. Example sample uses REQ-DOC-10.

curl -sS http://127.0.0.1:8088/api/approval-requests -H "Authorization: Bearer $GATEWAY_KEY" -H 'Content-Type: application/json' --data-binary @sample_document_approval_request.json

Parameter mapping:
1 company (top-level)
2 reference_id (top-level)
3 payload.requested_by
4 payload.department
5 amount (top-level), formatted with payload.currency (TZS if absent)
6 payload.reason
Header: payload.document_media_id; filename: payload.document_filename (default Requisition.pdf).

All six body values must be present/nonempty; amount is needed for the formatted total. Whitespace is flattened for template parameters. A field above 1000 characters is rejected. Template text and actual ERP snapshot must agree.

## 5. Deliver and decide

python3 -m approval_gateway.worker

Manager receives PDF + summary + buttons. Press Approve or Reject. The webhook verifies the Meta signature, identifies the manager by phone, matches approve_AGR-... / reject_AGR-..., and records the decision using existing safeguards.

python3 -m approval_gateway.worker

This attempts the registered ERP callback. Query the returned AGR reference via GET /api/approval-requests/AGR-... with GATEWAY_KEY. A decision status of approved does not mean the ERP executed it: execution_status must be applied after the authenticated callback confirms it. sent means Meta accepted submission; it is not proof the recipient received/read it.

If sending fails, inspect approval_request.delivery_error. Missing/wrong template, language, media ID, credentials or recipient/account restrictions can cause failures. Worker retries later; running immediately may yield delivery_attempts=0 until retry is due. Use new requests rather than modifying/cancelling an already approved test request.

## Boundaries

Local verification: 16 tests. Real Meta template approval, delivery and ERP execution must still be tested in your environment. No automatic PDF generation, public file hosting, ERP upload HTTP endpoint, rejection reason collection or stable tunnel provisioning is included. Existing prototype deployment limitations remain. Expired/deleted media IDs require re-upload and a correctly versioned new request. ERP must enforce transactional event deduplication, authorization and workflow-version/snapshot consistency; this release does not implement those ERP checks.
