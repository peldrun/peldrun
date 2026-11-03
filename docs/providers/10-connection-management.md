### `docs/providers/10-connection-management.md`

```markdown
<!-- docs/providers/10-connection-management.md -->
# Connection Management

> **Complete reference** for the connection lifecycle.
>
> **Reading time**: 15-18 minutes.
>
> **Who this is for**: Anyone using or configuring an installed Provider
> Package — and developers who need to understand how the runtime resolves
> credentials at request time.
>
> **What you'll learn**: What connections are, why packages can have multiple,
> how the active connection is chosen, how secrets are stored and masked, and
> every lifecycle operation.

---

## Table of Contents

- [What connections are](#what-connections-are)
- [Connections vs packages](#connections-vs-packages)
- [Multiple connections per package](#multiple-connections-per-package)
- [Active vs enabled](#active-vs-enabled)
- [The lifecycle](#the-lifecycle)
- [Create a connection](#create-a-connection)
- [Edit a connection](#edit-a-connection)
- [Delete a connection](#delete-a-connection)
- [Enable / disable](#enable--disable)
- [Activate](#activate)
- [Test](#test)
- [Secret masking and preservation](#secret-masking-and-preservation)
- [Storage format](#storage-format)
- [Runtime resolution](#runtime-resolution)
- [What happens with no active connection](#what-happens-with-no-active-connection)
- [Common issues](#common-issues)
- [Advanced patterns](#advanced-patterns)

---

## What connections are

A **connection** is a saved set of credentials + configuration for a Provider
Package. It is what actually lets the package talk to the provider's API.

Installing a package is not enough — the package needs:

- A base URL (the provider's endpoint)
- An API key (or OAuth credentials, or whatever the package declares)
- Any custom fields (`scope`, `region`, `project_id`, ...)

That is what a connection provides.

### Analogy

Think of a package as a **device driver**, and a connection as a **configured
device**:

- The driver knows *how to talk* to the hardware.
- The connection knows *which specific hardware* to talk to and *with what
  credentials*.

You can install the driver once and configure multiple devices. Same for
packages: install once, configure multiple connections.

---

## Connections vs packages

| Aspect | Package | Connection |
|--------|---------|------------|
| What it is | Installable code + manifest | Saved credentials + config |
| Where it lives | `storage/store/providers/<id>/` | `models_vault.json.provider_connections` |
| How to create | Upload ZIP or install from catalog | Click "Configure" → fill form → save |
| Can be deleted after install? | Yes (uninstalls) | Yes (removes credentials only) |
| How many per package? | One active version per package | **Multiple** connections per package |
| Survives uninstall? | ❌ Deleted | ✅ Preserved |
| Who owns it? | Package author | End user |

### Why two separate concepts?

Because they change at different rates:

- **Packages** change when the author publishes a new version (~monthly)
- **Connections** change when the user adds a new account (~rarely)

If connections lived inside packages, adding a second OpenAI key would
require reinstalling the package. Splitting them means the user can have
"Personal" and "Work" connections side-by-side without touching the package.

---

## Multiple connections per package

A single package can have **any number of connections**. The most common
patterns:

### Pattern 1 — Personal vs Work

```
gigachat
├── Personal    ← enabled, active
└── Work        ← enabled
```

The user can switch between them by clicking "Activate" on the one they want
to use.

### Pattern 2 — Multiple regions

```
aws-bedrock
├── US East     ← enabled
├── EU West     ← enabled, active
└── AP Southeast ← disabled (temporary)
```

Same credentials, different regions. Useful for latency optimization.

### Pattern 3 — Dev vs Prod

```
openrouter
├── Dev Account    ← enabled (low balance)
├── Prod Account   ← enabled, active
└── Testing Account ← disabled
```

Same provider, different billing accounts. Test on one, ship on another.

### Rule — only one is active

Even with multiple enabled connections, **only one can be active** at a time.
The active connection is the one the runtime uses for every request.

Activating a connection deactivates all others for the same package (see
[Activate](#activate) below).

---

## Active vs enabled

Two boolean flags per connection. They are independent but often confused.

### The four states

| State | `enabled` | `is_active` | Runtime behavior |
|-------|-----------|-------------|------------------|
| **Fully active** | ✅ | ✅ | **Used for requests** |
| Active but disabled | ❌ | ✅ | **Not used** (see below) |
| Enabled but not active | ✅ | ❌ | Not used (a sibling is active) |
| Disabled and inactive | ❌ | ❌ | Not used |

### What each flag means

**`enabled`** — "Is this connection allowed to be used at all?"

- `true` — The connection can be activated.
- `false` — The connection is temporarily disabled. It remains in the list
  but cannot be used.

**`is_active`** — "Is this THE connection the runtime should use?"

- `true` — The runtime uses this connection for requests.
- `false` — Some other connection (or none) is active.

### The intersection rule

The runtime uses a connection for a request **only if**:

```python
connection.is_active == True AND connection.enabled == True
```

**Why both**: Consider a user who disables their active connection
temporarily (e.g., to test a sibling). They expect:

- Their existing chats to fail with "connection disabled" (not silently fall
  back to a different one)
- The sibling to remain "configured but not active"

With the intersection rule, the runtime returns `None` for "active
connection" — and the caller reports "no active connection" rather than
silently switching credentials.

### Why the two flags exist

**Without `enabled`**: To temporarily disable a connection, the user would
have to delete and recreate it. That loses the credentials.

**Without `is_active`**: Multiple connections would be "active" simultaneously.
The runtime would not know which one to pick.

### Common patterns

**Disable temporarily**:
```
Personal (active, enabled)     ← in use
Work     (active, disabled)    ← NOT in use
```
After disabling "Personal", the runtime sees no active connection and reports
an error until the user enables or activates something else.

**Activate another without disabling**:
```
Personal (inactive, enabled)   ← available
Work     (active, enabled)     ← in use
```
Both enabled, one active. This is the normal state.

---

## The lifecycle

Here is the complete state machine for a single connection:

```
[NOT EXISTS]
    │
    │  User fills form + clicks "Create"
    ▼
[ENABLED + INACTIVE]         ← new connection (when a sibling is already active)
    │
    │  User clicks "Activate"
    ▼
[ENABLED + ACTIVE]           ← runtime uses this connection
    │
    │  User clicks "Disable"
    ▼
[ENABLED + ACTIVE] → wait, no. Disable sets enabled=false.
```

Let me redraw it properly:

```
                    ┌───────────────────┐
                    │  NOT EXISTS       │
                    └─────────┬─────────┘
                              │ create
                              ▼
              ┌───────────────────────────────┐
              │  ENABLED + INACTIVE           │
              │  (or ENABLED + ACTIVE if      │
              │   created with "set as active")│
              └───────┬───────────────┬───────┘
                      │               │
             disable  │               │ activate
                      ▼               ▼
              ┌───────────────┐  ┌────────────────┐
              │  DISABLED +   │  │  ENABLED +     │
              │  INACTIVE     │  │  ACTIVE        │
              └───────┬───────┘  └────────┬───────┘
                      │                   │
             enable   │                   │ disable
                      │                   ▼
                      │           ┌────────────────┐
                      │           │  DISABLED +    │
                      │           │  ACTIVE        │
                      │           └────────┬───────┘
                      │                    │
                      │           enable   │
                      └────────────────────┘
                              (back to enabled)
```

### The six operations

| Operation | Endpoint | Effect |
|-----------|----------|--------|
| **Create** | `POST /connections` | New connection, possibly active |
| **Edit** | `PUT /connections/{id}` | Updates fields (with secret preservation) |
| **Delete** | `DELETE /connections/{id}` | Removes connection permanently |
| **Enable** | `POST /connections/{id}/enable` | Sets `enabled=true` |
| **Disable** | `POST /connections/{id}/disable` | Sets `enabled=false` (keeps `is_active`) |
| **Activate** | `POST /connections/{id}/activate` | Sets `is_active=true`, deactivates siblings, sets `enabled=true` |

Each is detailed below.

---

## Create a connection

### Via the UI

1. Click **Configure** on the package card
2. If there are no connections, the form auto-opens in create mode
3. Otherwise, click **"Add New Connection"** in the list view
4. Fill in the fields (see [Connection Fields](05-connection-fields.md))
5. Optionally check "Set as active connection" (default: on)
6. Click **Create Connection**

### Via the API

```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Personal",
    "fields": {
      "credentials": "dGVzdDp0ZXN0",
      "scope": "PERS",
      "verify_ssl": true
    },
    "is_active": true
  }'
```

### What happens server-side

1. **Package check** — verify `gigachat` is installed (404 otherwise)
2. **Defaults** — apply name (if empty, uses package id), `enabled=true`,
   `created_at=now`
3. **Activation logic** — if `is_active=true`, deactivate every sibling
   connection of the same package
4. **Persist** — write to `models_vault.json.provider_connections[]`
5. **Return** — the saved connection with masked secret fields

### Response

```json
{
  "status": "ok",
  "connection": {
    "id": "conn_6c87efd8a1b2c3d4",
    "provider_package_id": "gigachat",
    "name": "Personal",
    "enabled": true,
    "is_active": true,
    "fields": {
      "credentials": "••••••••",
      "scope": "PERS",
      "verify_ssl": true
    },
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-01T00:00:00Z"
  }
}
```

Notice: `credentials` is masked. `scope` and `verify_ssl` are not (they are
not `type: "password"` or `type: "secret_file"`).

### Failure cases

| HTTP status | Code | Cause |
|-------------|------|-------|
| 404 | `PROVIDER_PACKAGE_NOT_FOUND` | Package not installed |
| 400 | `INVALID_REQUEST` | Missing `fields` or `name` |
| 500 | `STORAGE_ERROR` | Cannot write `models_vault.json` |

---

## Edit a connection

### Via the UI

1. Click **Configure** on the package card
2. Click the **→** (arrow) button on the connection row you want to edit
3. Update fields
4. Click **Save Changes**

### Via the API

```bash
curl -X PUT http://localhost:8088/api/store/providers/gigachat/connections/conn_6c87efd8a1b2c3d4 \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Personal (updated)",
    "fields": {
      "scope": "B2B"
    },
    "is_active": true
  }'
```

### Partial updates

The API supports **partial updates** — you do not need to send every field.
Any field omitted from `fields` is **preserved** from the current stored
value:

```json
// Current stored:
{"credentials": "dGVzdDp0ZXN0", "scope": "PERS", "verify_ssl": true}

// PUT payload:
{"fields": {"scope": "B2B"}}

// Result:
{"credentials": "dGVzdDp0ZXN0", "scope": "B2B", "verify_ssl": true}
```

This is important for the secret-masking flow (see
[Secret masking and preservation](#secret-masking-and-preservation)).

### Explicit clear

To clear a field, send it explicitly as `null`:

```json
{"fields": {"verify_ssl": null}}
```

Or as an empty string:

```json
{"fields": {"verify_ssl": ""}}
```

The distinction between `null` and `""` does not matter for most adapters —
both result in the field being absent or empty.

### Changing `is_active`

Passing `is_active: true` in the update **activates** this connection
(deactivates siblings). Passing `is_active: false` **deactivates** it without
activating anyone else.

**Edge case**: If you deactivate the currently active connection without
activating another, the package ends up with no active connection. The runtime
starts failing requests for this package with "no active connection".

### Changing `enabled`

The `PUT` endpoint does not accept `enabled`. Use the dedicated
`/enable` and `/disable` endpoints (see [Enable / disable](#enable--disable)).

**Why**: To prevent accidental state changes in an edit that is only
supposed to update fields.

### Failure cases

| HTTP status | Code | Cause |
|-------------|------|-------|
| 404 | `PROVIDER_CONNECTION_NOT_FOUND` | Connection id does not exist |
| 404 | `PROVIDER_PACKAGE_NOT_FOUND` | Package was uninstalled |
| 400 | `INVALID_REQUEST` | Missing required field in payload |

---

## Delete a connection

### Via the UI

1. Click **Configure**
2. Click the **trash** icon on the connection row
3. Confirm the deletion

### Via the API

```bash
curl -X DELETE http://localhost:8088/api/store/providers/gigachat/connections/conn_6c87efd8a1b2c3d4
```

### What happens

1. **Verify** — the connection exists and belongs to this package (404
   otherwise)
2. **Remove** — delete the entry from `models_vault.json.provider_connections`
3. **Persist** — atomic write
4. **Return** — `{"status": "ok", "connection_id": "...", "removed": true}`

### What is NOT deleted

- The package itself (it remains installed)
- Other connections of the same package
- Any other data (models, metadata, budgets)

### What happens if you delete the active connection

The package ends up with no active connection (unless a sibling is
implicitly activated, which is NOT the case — deleting the active connection
leaves the package without one).

**The runtime then returns `None` for "active connection"** and falls back to
`openai_compat`. See [What happens with no active connection](#what-happens-with-no-active-connection) below.

### Idempotency

The endpoint returns 404 if the connection was already deleted. This matches
the usual REST semantics.

### Re-creating after delete

If you recreate a connection with the same name, it gets a **new UUID**
(never reuse). From the runtime's perspective, it is a completely new
connection.

**The user's previous credentials are lost** — they must re-enter them. This
is intentional: a deleted connection is gone.

---

## Enable / disable

### Enable

**Effect**: Sets `enabled = true`. Does **not** change `is_active`.

**When to use**: Reactivating a temporarily disabled connection.

**UI**: Click **Enable** on the row.

**API**:
```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections/conn_.../enable
```

### Disable

**Effect**: Sets `enabled = false`. Does **not** change `is_active`.

**When to use**: Temporarily taking a connection out of service without
deleting it.

**UI**: Click **Disable** on the row.

**API**:
```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections/conn_.../disable
```

### Behavior after disable

If the connection was **active**, disabling it leaves it `is_active=true`,
`enabled=false`. The intersection rule (`is_active AND enabled`) means the
runtime no longer uses it. The package appears "no active connection" until:

1. The user re-enables the connection, OR
2. The user activates a sibling, OR
3. The user creates a new connection with `is_active=true`

### Behavior after enable

If the connection was active-but-disabled, enabling it immediately makes it
"fully active" again — because `is_active` was preserved.

### Common scenarios

**Scenario 1 — Temporarily pause billing on a connection**:
```
Before:  Personal (active, enabled), Work (inactive, enabled)
Disable: Personal → Personal (active, disabled), Work (inactive, enabled)
Result:  Runtime reports "no active connection"
         User must explicitly activate "Work" to switch
```

**Scenario 2 — Re-enable after pause**:
```
Before:  Personal (active, disabled)
Enable:  Personal → Personal (active, enabled)
Result:  Runtime uses Personal again
```

**Scenario 3 — Deactivate one, activate another**:
```
Before:  Personal (active, enabled), Work (inactive, enabled)
Activate: Work → Personal (inactive, enabled), Work (active, enabled)
Result:  Runtime uses Work
```

Notice: `disable` + `activate` is not needed. Just `activate` on the new
one — the runtime deactivates siblings automatically.

---

## Activate

### Effect

Activating a connection:

1. Sets `is_active = true` on the target connection
2. Sets `is_active = false` on all **sibling** connections (same package)
3. Sets `enabled = true` on the target connection (even if it was disabled)

Point 3 is intentional: "activate" is a superset of "enable". If a user
clicks "Activate" on a disabled connection, they obviously want it to work.

### UI

Click the **star** icon on the row (visible only on inactive connections —
the active one already shows the "Active" badge).

### API

```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections/conn_.../activate
```

### Idempotency

Activating an already-active connection is a no-op — it returns the connection
unchanged. This is useful for idempotent API clients.

### The activation flow

```
User clicks "Activate" on Work
  ↓
POST /api/store/providers/{id}/connections/{work_id}/activate
  ↓
save_provider_connection(
  id=work_id,
  is_active=True,
  enabled=True
)
  ↓
Storage logic:
  - Loads all sibling connections for the same provider_package_id
  - Sets is_active=False on all of them
  - Saves the target as is_active=True, enabled=True
  ↓
Returns the updated connection
```

### What changes in the runtime

The next `generate()` or `stream()` call to any chat using this provider
now uses the newly activated connection's credentials, URL, and fields.

**Existing connections stay alive**: If a request was in flight when the user
activated a different connection, that request completes with the OLD
connection. Only new requests use the new one.

This is intentional: pulling the rug out from a running request causes
"authentication revoked" errors that confuse the user.

### Failure cases

| HTTP status | Code | Cause |
|-------------|------|-------|
| 404 | `PROVIDER_CONNECTION_NOT_FOUND` | Connection id does not exist |
| 404 | `PROVIDER_PACKAGE_NOT_FOUND` | Package was uninstalled |

---

## Test

### Effect

Probes the connection by calling the provider's discovery endpoint. This is a
**read-only** operation — no chat completion is sent, no tokens are consumed.

### What gets called

The `test` endpoint:

1. Loads the connection's fields
2. Un-masks secret fields (uses the stored value)
3. Builds a `ConnectionContext`
4. Calls `provider_factory.create()` to get an adapter
5. Calls `adapter.validate_connection()`
6. Returns `{ok, message, latency_ms}`

### UI

Click the **⚡** icon on the connection row.

The row shows one of:

```
✓ Connected — 245 ms
```

or

```
✗ Probe failed — HTTP 401: Invalid API key
```

### API

```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections/conn_.../test
```

**Response**:

```json
{
  "status": "ok",
  "ok": true,
  "message": "Connected",
  "latency_ms": 245.5
}
```

Or on failure:

```json
{
  "status": "ok",
  "ok": false,
  "message": "Authentication failed: invalid credentials",
  "latency_ms": 12.3,
  "code": "auth_failed",
  "status_code": 401
}
```

### What test does NOT do

| Not tested | Why |
|-----------|-----|
| The model name | Test uses a cheap endpoint, not the model-specific one |
| The chat pipeline | Test does not send a chat completion |
| Tool calling | Test does not exercise tool support |
| Streaming | Test does not open a streaming connection |
| Vision | Test does not send images |

**Test verifies**: The base URL is reachable, the credentials are valid, and
the provider responds to a minimal request.

### Test with masked fields

When you click "Test" from the UI, the form may show `credentials: "••••••••"`
(masked). The test endpoint uses the **stored** value, not the masked one.
This works transparently — the user does not need to un-mask first.

### When to test

**After creating a connection**: To verify the credentials work before using
them in a chat.

**After editing credentials**: To verify the new credentials work.

**When chats start failing**: To isolate whether the problem is credentials
or something else.

### Common test failures

| Failure | Cause | Fix |
|---------|-------|-----|
| `HTTP 401` | Invalid API key | Re-enter the key |
| `HTTP 403` | Valid key, no permission | Check the account tier |
| `HTTP 404` | Wrong base URL | Verify the URL path (`/v1`, `/api/v1`, etc.) |
| `Connection timeout` | Wrong host or port | Verify the URL is reachable |
| `SSL error` | Missing CA bundle | Add the CA bundle path |
| `Invalid scope` | Wrong `scope` value | Try a different scope |

---

## Secret masking and preservation

This section explains one of the trickiest interactions in the UI.

### The problem

The user opens the Configure modal to edit their connection. They see:

```
Authorization Key
[••••••••••••••••••••••••••••]
```

This is the stored value, but it is **masked** — the user does not see the
real key. This is correct: if the user shares their screen, the key is not
visible.

But now the user changes the **scope** and clicks "Save Changes". What
happens to the API key?

- **Option A** (bad): The form submits `credentials: "••••••••"`. The backend
  overwrites the real key with the mask. Every future request fails with
  `401`.
- **Option B** (good): The form submits **only changed fields**. The `credentials`
  field is not in the payload, so the backend preserves the real value.

PELDRUN uses Option B.

### How it works

The `ProviderConfigureModal` tracks the original values at load time:

```typescript
form.originalValues = { credentials: "••••••••", scope: "PERS" }
form.values = { credentials: "••••••••", scope: "B2B" }   // after edit
```

On save, it computes the **diff**:

```typescript
const changedFields = {}
for (const [key, current] of Object.entries(form.values)) {
  const original = form.originalValues[key]
  if (current !== original) {
    changedFields[key] = current
  }
}
// changedFields = { scope: "B2B" }
```

The PUT payload contains only `{ fields: { scope: "B2B" } }`. The backend's
`save_provider_connection()` merges:

```python
merged_fields = dict(existing.get("fields") or {})   # {credentials: "real", scope: "PERS"}
merged_fields.update(body.fields or {})              # {credentials: "real", scope: "B2B"}
```

The `credentials` value is preserved.

### What if the user changes the secret?

If the user types a new credentials value:

```typescript
form.originalValues = { credentials: "••••••••", scope: "PERS" }
form.values = { credentials: "new-real-credentials", scope: "PERS" }
```

The diff includes `credentials`:

```typescript
changedFields = { credentials: "new-real-credentials" }
```

The PUT payload contains the new value. The backend's merge replaces the old
value with the new one. Done.

### What if the user clears the secret?

If the user clicks "Clear" on the field (or empties it):

```typescript
form.values = { credentials: "", scope: "PERS" }
```

The diff includes `credentials: ""`. The backend's merge **replaces** the
value with empty string (if the update logic allows). This is intentional —
the user explicitly cleared the field.

**Caveat**: The backend's `save_provider_connection()` does not distinguish
between "user cleared the field" and "user did not touch the field but
submitted empty". Both result in the same behavior.

**Workaround**: If you want to clear a secret, edit the connection and remove
the field entirely by explicitly submitting it. Or, more simply, delete the
connection and re-create it.

### The rule for package authors

**Never read the masked value directly.** In your adapter, always check:

```python
credentials = extra.get("credentials") or ""
if credentials == "••••••••":
    raise ProviderAuthError(
        "Credentials appear to be masked. Please re-enter them.",
        provider="my-provider",
    )
```

This is defensive — the runtime's merge logic should have preserved the real
value, but if a bug ever bypasses it, the adapter catches it.

**Better**: Rely on the merge logic and never see the masked value. The masked
placeholder should never reach the adapter.

---

## Storage format

### Where connections live

`<backend>/data/models_vault.json`, under the `provider_connections` key:

```json
{
  "cloud_vault": [...],
  "custom_endpoints": [...],
  "lmstudio_vault": {...},
  "ollama_vault": {...},
  "scanned_models": [...],
  "provider_connections": [
    {
      "id": "conn_6c87efd8a1b2c3d4",
      "provider_package_id": "gigachat",
      "name": "Personal",
      "enabled": true,
      "is_active": true,
      "fields": {
        "credentials": "dGVzdDp0ZXN0",
        "scope": "PERS",
        "verify_ssl": true
      },
      "created_at": "2025-01-01T00:00:00Z",
      "updated_at": "2025-01-01T00:00:00Z"
    },
    {
      "id": "conn_1234567890abcdef",
      "provider_package_id": "gigachat",
      "name": "Work",
      "enabled": true,
      "is_active": false,
      "fields": {
        "credentials": "d29yazp3b3Jr",
        "scope": "B2B",
        "ca_bundle_path": "/etc/ssl/certs/ca.pem",
        "verify_ssl": true
      },
      "created_at": "2025-01-02T00:00:00Z",
      "updated_at": "2025-01-02T00:00:00Z"
    }
  ]
}
```

### Field reference

| Field | Type | Meaning |
|-------|------|---------|
| `id` | `string` | Stable UUID (`conn_` prefix) |
| `provider_package_id` | `string` | The package's `manifest.id` |
| `name` | `string` | User-facing label |
| `enabled` | `bool` | Whether the connection can be used |
| `is_active` | `bool` | Whether this is THE active connection for the package |
| `fields` | `dict` | All field values (keyed by `ConnectionFieldSpec.key`) |
| `created_at` | `string` | ISO 8601 timestamp |
| `updated_at` | `string` | ISO 8601 timestamp (updated on every save) |

### Secrets are NOT masked in storage

The `fields` dict contains the **real** values, not the masked ones. Masking
is a UI-only concern.

**Why**: The runtime needs the real values to build HTTP requests. Masking on
disk would require a separate un-masking step with a decryption key — a
significant complexity increase for a minimal security gain (the file is
already protected by filesystem permissions).

**If you need encryption at rest**: Use full-disk encryption or an
OS-level file encryption tool. PELDRUN does not encrypt `models_vault.json`
today.

### Isolation from `custom_endpoints`

`provider_connections` is a **separate** array from `custom_endpoints`:

- `custom_endpoints` — for the LLM Settings UI (built-in providers like
  `openai_compat`, `anthropic`, `google_genai`)
- `provider_connections` — for the Store → Providers UI (external packages)

The two arrays do not overlap. A connection in one is invisible to the other.
This prevents the LLM Settings UI from showing package connections (which
would confuse users) and vice versa.

### Atomic writes

`save_vault_to_disk()` uses `tmp_file → os.replace`. A crash mid-write leaves
the previous file untouched. Concurrent writes are serialized by the
filesystem.

### Manual editing

You can hand-edit `models_vault.json` to:

- Move a connection to a different package (update `provider_package_id`)
- Bulk-enable/disable connections
- Clone a connection (copy the entry, generate a new UUID)

**After editing**, restart the backend so the in-memory caches see the change.

---

## Runtime resolution

When a user sends a chat message, the runtime needs to know **which
connection** to use for the provider.

### The resolution function

```python
# backend/omweb/routers/config_rtr.py

def get_active_connection_for_package(package_id):
    """
    Return the currently-active connection for a package, or None.

    "Active" means: is_active == True AND enabled == True.

    An active-but-disabled connection is treated as "no active connection" —
    the runtime will refuse to use it.
    """
    for conn in list_provider_connections(package_id):
        if conn.get("is_active") and conn.get("enabled"):
            return conn
    return None
```

### When it is called

`dispatcher._resolve_provider_and_extras()` calls it during every request:

```python
async def run_direct_chat(job_id, prompt, llm_override):
    ...
    active_llm = load_llm_config()
    provider_id = active_llm["provider"]   # e.g., "gigachat"

    # Resolve which connection to use
    provider_id, extra_params = _resolve_provider_and_extras(
        active_llm, connection_id=job_id
    )

    # extra_params now contains:
    # {
    #   "_peldrun_plugin": {...},
    #   "_peldrun_base_url": "https://gigachat.devices.sberbank.ru/api/v1",
    #   "_peldrun_api_key": "real-credentials",
    #   "scope": "PERS",
    #   "verify_ssl": true,
    #   "_peldrun_connection_id": "conn_6c87efd8a1b2c3d4",
    # }
```

### What happens next

The `extra_params` are:

1. Stored in `active_llm["_peldrun_extra_params"]` (agent mode) or
   passed to `ConnectionContext(extra_params=...)` (direct chat)
2. Popped out of `extra_params` at the call site (the `_peldrun_*` prefixed
   keys are internal)
3. The remaining keys (`scope`, `verify_ssl`) are passed to the adapter via
   `ConnectionContext.extra_params`

The `base_url` and `api_key` override the ones from `active_llm["base_url"]`
and `active_llm["api_key"]`. This is important: the package's connection
**owns** its credentials, not the LLM Settings config.

### Reading the resolved values in the adapter

```python
class GigaChatAdapter:
    def __init__(self, context):
        # Standard fields (already overridden by the connection's values)
        base_url = context.connection.base_url   # "https://gigachat.devices.sberbank.ru/api/v1"
        api_key = context.connection.api_key     # "real-credentials"

        # Custom fields
        extra = dict(context.connection.extra_params or {})
        extra.pop("_peldrun_plugin", None)       # Remove the reserved key
        scope = extra.get("scope", "PERS")
        verify_ssl = extra.get("verify_ssl", True)
```

### Multiple packages, multiple connections

The resolution is **per-package**. A chat using `gigachat` uses Gigachat's
active connection. A chat using `acme-ai` uses Acme's active connection. They
do not interfere.

```
Chat A → provider = "gigachat"     → uses "Personal" (Gigachat's active)
Chat B → provider = "acme-ai"      → uses "Default" (Acme's active)
Chat C → provider = "gigachat"     → uses "Personal" (same as A)
```

### What if the connection was deleted mid-chat?

If a connection is deleted while a chat is running:

- **Existing requests** continue with the connection that was active when they
  started
- **New requests** see no active connection → fall back to `openai_compat`
  (see below)

The user must create a new connection to resume using the package.

---

## What happens with no active connection

If a package is installed but has **no active connection**, the runtime
cannot build the `ConnectionContext` from the package's credentials. The
dispatcher falls back to `openai_compat` with the user's existing
`active_llm` config.

### The fallback path

```
User sends a message → provider = "gigachat"
  ↓
_resolve_provider_and_extras():
  - finds "gigachat" registration ✅
  - source == external ✅
  - class_path contains "custom_plugin" ✅
  - builds descriptor ✅
  - get_active_connection_for_package("gigachat") → None ❌
  ↓
Logs: "Package 'gigachat' is registered but has NO active connection.
       Configure it in Store → Providers first.
       Falling back to openai_compat for this request."
  ↓
Returns ("openai_compat", {})
  ↓
The request goes through the standard OpenAI-compatible adapter
```

### What the user sees

The request likely fails with:

```
ProviderError: HTTP 404 — https://api.openai.com/v1/models returned 404
```

Or, if `active_llm["base_url"]` points to a valid OpenAI-compatible endpoint,
the request might succeed — but using the wrong provider.

### The user-facing fix

The UI should have already warned the user:

- The package card shows **"Not configured"** (amber badge)
- The Configure button is styled as primary (blue) instead of secondary

The user's fix:

1. Click **Configure**
2. Create a connection
3. **Activate** it
4. Send the message again

### Why this design?

Three reasons:

1. **Prevents silent failures** — If the dispatcher raised an error instead of
   falling back, the user would see a confusing "no adapter" error. The
   fallback at least uses a working code path.

2. **Preserves existing configs** — A user who never installs a package keeps
   using `openai_compat` exactly as before.

3. **Fails visibly** — The eventual failure is a clear 404 from the wrong
   endpoint, not a mysterious "adapter not found".

**The card's "Not configured" badge is the primary defense** — the user is
warned before they send a message. The fallback is a safety net.

### How to check

From the UI:

- The card shows "Not configured" (amber)
- Configure modal shows zero connections (or connections with no "Active"
  badge)

From the API:

```bash
curl http://localhost:8088/api/store/providers/gigachat
```

Response includes:

```json
{
  "provider": {...},
  "has_active_connection": false,
  "active_connection_id": null,
  "active_connection_name": null,
  "connections_count": 0
}
```

If `has_active_connection: false`, the user needs to configure.

---

## Common issues

### "Not configured" badge won't go away

**Cause**: The card only shows "Connected" when a connection has
`is_active=true` AND `enabled=true`.

**Fix**:
1. Open Configure
2. Verify a connection exists and is enabled
3. Click **Activate** on it

**Note**: If the connection is enabled but inactive (no "Active" badge), the
card still shows "Not configured". Only activation clears the badge.

### Activate button not appearing

**Cause**: The connection is already active. There is nothing to activate.

**Check**: The row shows the "Active" badge (green with star). If yes, the
package is already using this connection.

### Test fails but chat works

**Cause**: Some providers return different results for `/models` (used by
test) versus `/chat/completions` (used by chat). The test endpoint uses
`/models`, which is usually free but may be disabled on some accounts.

**Fix**: Test with a chat message instead. If chat works, the credentials are
fine — the test endpoint is just restricted.

### Cannot save after changing a secret

**Cause**: The form submitted the masked value (`"••••••••"`), which the
backend interpreted as the new value. Every future request fails with `401`.

**Fix** (unrecoverable):
1. Delete the connection
2. Create a new one
3. Re-enter the real credentials

**Prevention**: Do not edit the secret field if you do not intend to change
it. The form tracks diffs, but if the value is edited (even to the same mask),
it submits the change.

### Multiple connections, wrong one active

**Cause**: Creating a connection with `is_active: true` automatically
deactivates siblings. If you created a new connection thinking "this will be
a backup", you may have just deactivated the one you were using.

**Fix**: Click **Activate** on the connection you actually want to use.

### Connection delete affects running chats

**Behavior**: Deleting a connection does NOT cancel in-flight requests. Those
complete with the deleted connection's credentials.

**Next request**: Fails with "no active connection" fallback.

**Fix**: Create a new connection before deleting the old one, or activate a
sibling first.

### "Masked value" error from the adapter

**Cause**: The adapter received `"••••••••"` instead of the real credential.
This should not happen (the backend's merge preserves the real value), but
if it does, it means:

- The user's previous save overwrote the real value with the mask
- OR the adapter read a masked value in some edge case

**Fix**: Same as "Cannot save after changing a secret" — delete and recreate.

### Connection exists but appears in wrong package

**Cause**: `models_vault.json` was hand-edited with the wrong
`provider_package_id`. Or the connection was created before the package was
uninstalled/reinstalled with a different id.

**Fix**: Edit `models_vault.json` directly:

```json
{
  "id": "conn_...",
  "provider_package_id": "gigachat",   ← fix this
  ...
}
```

Or delete the connection and re-create it.

---

## Advanced patterns

### Sharing credentials across packages

Some providers offer the same API across multiple endpoint flavors (e.g., a
self-hosted OpenAI-compatible proxy for multiple model families).

**Pattern**: Create separate connections with the same API key but different
`base_url` values. There is no "credential sharing" mechanism — each
connection stores its own fields.

**Why not share**: Because a package's connection is scoped to that package.
A user who uninstalls the package also removes the connection (unless they
keep it). Sharing across packages would make this ambiguous.

### Environment-based credentials

Some adapters support environment variables as a fallback:

```json
{
  "runtime_hints": {
    "supports_env_vars": true
  }
}
```

**Setup**:

1. Set the environment variable before starting PELDRUN:
   ```bash
   export MY_PROVIDER_API_KEY="sk-..."
   peldrun start
   ```
2. Create a connection with an empty `api_key` field.
3. The adapter reads from the env var as fallback.

**Security note**: The environment variable is visible to every adapter in
the worker process. Do not use this for high-value secrets if you have
multiple packages installed.

### Programmatic connection management

For CI/CD or automated deployments, use the REST API:

```bash
# Get all connections for a package
curl http://localhost:8088/api/store/providers/gigachat/connections

# Create a new connection
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections \
  -H "Content-Type: application/json" \
  -d '{"name": "CI", "fields": {"credentials": "'"$CREDS"'"}, "is_active": true}'

# Wait for tests
sleep 1

# Verify
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections/$CONN_ID/test
```

**Idempotency**: The API is not idempotent — re-creating a connection creates
a new UUID. Wrap in a script that checks for existing connections first.

### Migrating from `custom_endpoints`

If you previously added a provider through LLM Settings → Custom Endpoints,
you can migrate it to the Store → Providers flow:

1. **Read the old endpoint**:
   ```bash
   curl http://localhost:8088/api/config/vault | jq '.custom_endpoints[] | select(.providerId == "gigachat")'
   ```

2. **Create a new connection** in the Store with the same fields.

3. **Delete the old endpoint** from `custom_endpoints`.

**Why migrate**: The Store → Providers flow gives you:
- Multiple connections
- Enable/disable without deletion
- Test button
- Health checks

The LLM Settings flow still works, but is more limited.

---

## What's next

- **Need the API reference?** → [API Reference](11-api-reference.md)
- **Need to understand the runtime resolution?** → [Architecture](01-architecture.md#provider-registry-and-connections)
- **Curious about secret storage?** → [Security Model](12-security-model.md)
- **Hitting a connection issue?** → [Troubleshooting](14-troubleshooting.md)
```

---
