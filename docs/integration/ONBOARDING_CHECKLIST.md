# Generic ERP — Onboarding and Release Checklist

This checklist is for the gateway operator and the developer integrating a new ERP. Do not add live credentials to the repository, Postman collection, screenshots, or logs.

## ERP developer deliverables

- [ ] Unique `source_system` identifier and list of `request_type` values
- [ ] Stable source record ID and distinct `step_id`; change `workflow_version` when the record/assignment changes
- [ ] Source-owned current-assignment endpoint: `POST /api/approvals/verify` (or another registered fixed HTTPS path) with the precise request and response in [GENERIC_ERP.md](GENERIC_ERP.md)
- [ ] Verification checks real record, company, active approver, Directory UUID, step, version, permissions and trusted phone, never trusts asserted permissions from gateway request
- [ ] Source-owned signed callback endpoint, with HMAC verification of raw bytes, timestamp, matching event ID, and atomic deduplication
- [ ] ERP executes through its existing transactional approval services; callbacks confirm `applied` only after commit
- [ ] Repeat applied callback gets the same acknowledgement; uncommitted actions do not acknowledge applied
- [ ] Subsequent workflow steps are emitted only by source ERP after its transaction commits
- [ ] Business-specific fields belong in the nested `payload`; no additional top-level keys

## Gateway operator tasks (one-time per ERP)

- [ ] Create a source-specific API client key through approved operator process, not from a public API
- [ ] Register a fixed HTTPS callback URL and unique >=32 character callback secret
- [ ] Set a separate >=32 character verification token in operator-managed secret environment
- [ ] Add opt-in `generic:build_adapter` entry under that source system in `INTEGRATIONS_CONFIG_PATH`; preserve all existing system entries
- [ ] Configure restricted egress/allowlist for trusted verification target, correct domain/TLS and network boundaries
- [ ] Confirm workers, database backups, request/audit logs and alerting
- [ ] Ensure separate sandbox environment / no real user messages during certification

## Contract and security certification

- [ ] `python3 -m unittest tests.test_generic_erp_contract -v`
- [ ] `python3 -m unittest tests.test_generic_erp_e2e -v`
- [ ] `python3 -m unittest discover -s tests -q`
- [ ] `git diff --check`
- [ ] Validate Postman JSON with `python3 -m json.tool docs/integration/postman/WhatsApp-Approval-Gateway.postman_collection.json >/dev/null`
- [ ] Validate OpenAPI YAML with a compatible OpenAPI 3.1 validator and check that the path/required fields match `server.py` / `service.py`
- [ ] Confirm wrong credentials, revoked actor, company mismatch, stale step/version and unknown reference fail closed
- [ ] Confirm duplicate intake, duplicate WhatsApp decision and callback retries cannot apply an ERP action twice
- [ ] Confirm gateway `approved` differs from ERP `execution_status=applied`
- [ ] Confirm signed callback verification rejects tampering/stale timestamps
- [ ] Test with a **real ERP sandbox** and a registered test identity before production
- [ ] Obtain ERP owner sign-off, merge reviewed branch after local tests and deploy through GitHub-to-VPS workflow

## Developer hand-off

Give the ERP team: gateway base URL, source-specific API key (via secret channel), system ID, standard approval payload, ERP verification request/response example, callback signature contract, and Postman collection. The ERP team must implement verification + callback endpoints; gateway does not need ERP-specific Python source code.

**Non-goals:** self-service public onboarding, trusting phone numbers or permission claims from inbound payloads, authorization solely from possession of API key, or auto-deployment on a passing unit suite.
