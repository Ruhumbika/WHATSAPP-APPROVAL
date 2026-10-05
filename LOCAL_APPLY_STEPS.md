# Local integration changes

These are manual application steps for the supplied PETA controllers. Apply locally first. The ZIP contains gateway files and PETA files in separate folders; do not copy one project's files into the other project.

## 1. Files

Replace the gateway files under `approval_gateway/` with the supplied gateway folder. It includes the global service, server, event queue and worker plus the PETA adapter. The supporting database/repository helpers are included from the supplied baseline; retain newer local versions if your project has since changed those helpers.

Copy the files under the ZIP's `peta/` folder to the corresponding paths of your Laravel project. The existing callback route can continue using `WhatsAppApprovalCallbackController`; replace its previous implementation.

## 2. RequisitionController.php: imports and creation hook

Path: `app/Http/Controllers/Api/Requisitions/RequisitionController.php`.

Add with the imports:

```php
use App\Jobs\Integrations\NotifyApprovalGateway;
```

In `store()`, locate the end of `$requisition = DB::transaction(...)`. Insert this after `});` and before the successful `return response()->json(...)`:

```php
// Resolve the current assigned users after the requisition commits.
if (config('approval_gateway.enabled')) {
    NotifyApprovalGateway::dispatch('requisition_approval', (string) $requisition->id)->afterCommit();
}
```

This placement comes after the request and its items have been saved.

## 3. RequisitionApprovalController.php: details fields

Path: `app/Http/Controllers/Api/Requisitions/RequisitionApprovalController.php`.

Inside `getDetails()`, locate the response's `data` block and add immediately below `'id' => $requisition->id`:

```php
// Expose record scope and lifecycle state to the approval integration.
'company_id' => $requisition->company_id,
'status' => $requisition->status,
'is_closed' => (int) $requisition->is_closed,
```

Add this import once:

```php
use App\Jobs\Integrations\NotifyApprovalGateway;
```

## 4. RequisitionApprovalController.php: serialize actions and notify next step

Both the website and WhatsApp must acquire the same requisition row lock. This prevents simultaneous actions from executing a level twice.

In `approve(Request $request)`, put the **entire existing method body** inside this transaction wrapper:

```php
public function approve(Request $request)
{
    // Serialize every approval channel before resolving the pending level.
    return DB::transaction(function () use ($request) {
        // Existing method body stays here, with the two edits below.
    });
}
```

Inside that existing body, replace:

```php
$requisition = Requisition::find($request->requisition_id);
```

with:

```php
$requisition = Requisition::query()
    ->whereKey($request->integer('requisition_id'))
    ->lockForUpdate()
    ->firstOrFail();
```

After the existing `try/catch` and **before the final successful response**, add:

```php
// Trigger the next workflow lookup only after a successful action commits.
if (config('approval_gateway.enabled')) {
    NotifyApprovalGateway::dispatch('requisition_approval', (string) $requisition->id)->afterCommit();
}
```

Keep the existing inner `DB::transaction(...)`; Laravel supports nested transactions. Keep the existing validation, approval-item creation and response unchanged.

Replace the current short `reject()` method with:

```php
public function reject(Requisition $requisition)
{
    return DB::transaction(function () use ($requisition) {
        // Use the same record lock as all other approval channels.
        $requisition = Requisition::query()
            ->whereKey($requisition->id)
            ->lockForUpdate()
            ->firstOrFail();

        $requisition->status = Requisition::STATUS_REJECTED;
        $requisition->save();

        if (config('approval_gateway.enabled')) {
            NotifyApprovalGateway::dispatch('requisition_approval', (string) $requisition->id)->afterCommit();
        }

        return response()->json([
            'success' => true,
            'message' => 'Requisition rejected successfully',
            'data' => $requisition,
        ]);
    });
}
```

The callback invokes these same methods. Its receipt commits with the ERP action, and a repeated callback returns the stored confirmation without repeating the action. A stale step returns `execution_status: rejected`; an successfully executed approve OR reject returns `execution_status: applied`.

## 5. PDF: preserve the current print route's behavior

In `RequisitionController.php`, replace the method signature:

```php
public function getPrintOut(Requisition $requisition)
```

with:

```php
public function getPrintOut(Requisition $requisition, bool $markPrinted = true)
```

Near the end of the method, replace the existing printed-flag update:

```php
$requisition->is_printed = 1;
$requisition->save();
```

with:

```php
// Approval previews must not mark the requisition as printed.
if ($markPrinted) {
    $requisition->is_printed = 1;
    $requisition->save();
}
```

The existing print endpoint keeps the default `true`. The new integration controller calls it with `false`.

Add this route **inside the same `/api/v1` group as the existing callback route**:

```php
Route::get(
    'whatsapp-approvals/requisitions/{requisition}/document',
    \App\Http\Controllers\Api\WhatsAppApprovalDocumentController::class,
);
```

The new document controller requires the same integration API key configured in PETA's `APPROVAL_GATEWAY_API_KEY`. This is separate from optional ordinary ERP Bearer authentication.

## 6. Local configuration and workers

PETA environment:

```dotenv
APPROVAL_GATEWAY_ENABLED=true
APPROVAL_GATEWAY_SYSTEM=peta
APPROVAL_GATEWAY_URL=http://127.0.0.1:8088
APPROVAL_GATEWAY_API_KEY=<the API key registered for peta; at least 32 characters>
APPROVAL_GATEWAY_CALLBACK_SECRET=<the secret registered for peta; at least 32 characters>
```

Use a configured durable Laravel queue, such as `database` or Redis, instead of `sync`. For a queue worker timeout of 90 seconds, set its queue connection's `retry_after` above 90, for example 120. Ensure the selected queue's tables or Redis service already exist.

Run locally from the PETA project:

```bash
php artisan migrate
php artisan config:clear
php artisan queue:work --queue=approval-gateway --timeout=90 --tries=5
```

Gateway environment:

```dotenv
INTEGRATIONS_CONFIG_PATH=config/integrations.json
PETA_API_BASE_URL=http://127.0.0.1:8000/api/v1
PETA_GATEWAY_API_KEY=<same key as PETA APPROVAL_GATEWAY_API_KEY>
USER_DIRECTORY_ACCESS_TOKEN=<credential for the actual Directory endpoint>
WHATSAPP_TEMPLATE_NAME=workflow_approval_document
WHATSAPP_TEMPLATE_LANGUAGE=en
DRY_RUN_WHATSAPP=true
```

Copy `config/integrations.example.json` to `config/integrations.json` and enable `systems.peta.enabled`. The PETA ordinary access token is optional (`access_token_env: null`). The PDF endpoint uses `document_access_token_env` independently. The Directory client retains its configured authentication.

Register the gateway's integration callback and actor bindings with the existing administration commands. Each assigned user must have an active binding for the actual PETA company ID, Directory UUID, PETA user ID and international phone number. An unregistered or revoked binding prevents delivery; no fallback role-based recipient is selected.

From the gateway project, in separate terminals:

```bash
python3 -m approval_gateway.server
python3 -m approval_gateway.worker --loop --interval 2
```

A worker without `--loop` still processes one batch and exits.

## 7. Validation

Gateway checks:

```bash
python3 -m unittest discover -s tests -p test_workflow_integration.py -v
```

Run PHP syntax checks locally for the new PHP files and both edited requisition controllers, then exercise a real requisition against your Laravel database. The supplied Python tests mock ERP responses and Meta calls; the callback test runs a local HTTP receiver, not Laravel.

Expected flow: create requisition → queued trigger → current assigned user → PDF approval message → signed decision → existing ERP action → receipt → queued trigger for the next step. Closed, rejected and fully approved chains produce no new candidate tasks. Enabling real delivery requires `DRY_RUN_WHATSAPP=false`, an approved English template and valid Meta credentials.
