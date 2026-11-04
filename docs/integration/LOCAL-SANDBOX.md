# Isolated local developer sandbox (experimental)

This guide is for a local-only simulation, not a hosted developer portal or
production test. **The WhatsApp Business account belongs to our gateway.**
External ERP developers never receive Meta tokens, templates or webhook access.

## Bootstrap

From a local checkout of this feature branch, in a **nonproduction** shell:

```bash
python3 -m approval_gateway.local_sandbox \
  --directory /tmp/approval-gateway-sandbox-dev \
  --port 8098
```

The command refuses `APP_ENV=production`, refuses existing folders and
creates an isolated SQLite database, private `sandbox.env` and empty
integration adapter registry. It prints a sandbox-only ERP API key and
callback secret once. Save them privately. These values are test credentials
and must never be used against production.

To run its local HTTP process, import **only** the generated sandbox
environment into a dedicated shell, then start the server:

```bash
set -a
. /tmp/approval-gateway-sandbox-dev/sandbox.env
set +a
python3 -m approval_gateway.server
```

The generated `APP_HOST=127.0.0.1` and
`DRY_RUN_WHATSAPP=true` keep the service on loopback and simulate message
delivery. The configuration explicitly clears Meta credentials and optional
WhatsApp administration features.

Check sandbox health:

```bash
curl -fsS http://127.0.0.1:8098/health
```

Run Postman against `http://127.0.0.1:8098` using the sandbox API key.
The `sandbox_erp` client is registered for company `sandbox`.

## What is not configured automatically

The bootstrap deliberately does **not** invent an independent ERP backend,
an HTTPS verification endpoint, valid actor bindings, a Meta signature or
a live WhatsApp phone conversation. Therefore creating an approval is expected
to be denied until a generic ERP verifier and signed callback receiver are
configured. The sample backend at
[`examples/python/mock_erp_server.py`](examples/python/mock_erp_server.py)
demonstrates the ERP-side contract, but its illustrative identifiers and
record data must be aligned to sandbox company and actor details before use.

For an actual approval lifecycle test, configure a known-safe HTTPS test
verifier using `approval_gateway.configure_generic_erp`, link the actor
through a simulated signed Meta webhook with test-only Meta settings, and
run the approval worker. Keep real Meta credentials and real phone messages
out of this sandbox. Do not present a mocked callback as evidence of
production ERP execution.

## Safety / release constraints

- Never expose the loopback sandbox on a public VPS interface.
- Keep sandbox credentials, SQLite database and environment file out of Git.
- Do not run the sandbox with production environment variables or live Meta tokens.
- Sandbox API responses and dry-run status are not proof of Meta delivery.
- A separately isolated, provisioned, abuse-protected hosted sandbox is still
  required before inviting external developers.
- A real WhatsApp button-to-ERP database acceptance test is still mandatory
  before merging this feature into `main` or deploying production.
