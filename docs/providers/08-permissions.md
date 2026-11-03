### `docs/providers/08-permissions.md`

```markdown
<!-- docs/providers/08-permissions.md -->
# Permissions — Complete Reference

> **Complete reference** for the `permissions` block in `manifest.json`.
>
> **Reading time**: 12-15 minutes.
>
> **Who this is for**: Package authors who need to understand what their
> adapter can and cannot do at runtime — and anyone evaluating the security
> model before installing a package.
>
> **What you'll learn**: The six allowed permissions, the three forbidden ones,
> how the runtime enforces them, and what the system does NOT protect against.

---

## Table of Contents

- [Overview](#overview)
- [The permission model](#the-permission-model)
- [Allowed permissions](#allowed-permissions)
- [Forbidden permissions](#forbidden-permissions)
- [Runtime enforcement](#runtime-enforcement)
- [When permissions are checked](#when-permissions-are-checked)
- [Threat model](#threat-model)
- [Per-provider examples](#per-provider-examples)
- [Testing permissions](#testing-permissions)
- [Common mistakes](#common-mistakes)
- [Anti-patterns](#anti-patterns)
- [Checklist](#checklist)

---

## Overview

Permissions declare what a provider's adapter is allowed to do at runtime.
They are **declared in the manifest** at install time and **enforced by the
runtime** at request time.

### Why permissions exist

A provider adapter is third-party code. It runs inside PELDRUN's worker
subprocess, with access to:

- The connection's API key
- An HTTP client
- The Python interpreter

Without a permission model, the adapter could:
- Read another connection's credentials
- Reach any URL (including `169.254.169.254` for cloud metadata theft)
- Read workspace files
- Spawn shell processes

The permission model constrains these. It is **declarative** (the package
declares what it needs) and **enforced** (the runtime blocks anything not
declared).

### The four permission categories

Every action an adapter can take falls into one of four buckets:

| Category | Example | Enforced by |
|----------|---------|-------------|
| **Allowed** | `llm.generate`, `models.discover` | Declared in manifest, granted to adapter |
| **Forbidden** | `workspace.read`, `shell.execute` | **Rejected at install time** |
| **Scoped** | `network.connect` (allowlist), `secrets.read:connection` (own connection only) | Declared in manifest, enforced with scope |
| **Implicit** | `usage.read` (own invocations only) | Never needs declaration |

This document details each.

---

## The permission model

### The three fields

A `permissions` block has exactly three fields:

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": ["connection.api_key"],
  "workspace_access": false,
  "shell_execute": false
}
```

| Field | Type | Values |
|-------|------|--------|
| `network` | string | `"none"` or `"connection_endpoint"` |
| `secrets` | string[] | `[]` or `["connection.api_key"]` |
| `workspace_access` | bool | **Must be `false`** |
| `shell_execute` | bool | **Must be `false`** |

### Why so few fields?

The model is intentionally narrow. Every action an adapter can take is covered
by one of the above. Adding a new field requires a PELDRUN release — because
the runtime enforcement code must be updated too.

This is a **feature**: it prevents "helpful" additions that silently open
security holes. The small surface is auditable.

### The `extra="forbid"` rule

The `PackagePermissions` Pydantic model uses `extra="forbid"`. Any field not
in the list above causes an install error:

```json
{
  "permissions": {
    "network": "connection_endpoint",
    "secrets": ["connection.api_key"],
    "workspace_access": false,
    "shell_execute": false,
    "filesystem_access": true   // ← ValidationError
  }
}
```

There is no "just try it and see" mode. The model is strict.

---

## Allowed permissions

Three of the four possible permission fields can be set to grant capabilities.
Each is documented below.

### `network: "connection_endpoint"`

**What it grants**: The adapter can make HTTP(S) requests to the **host and
port** of the connection's `base_url`.

**What it does NOT grant**:
- Requests to any other host
- Requests to `169.254.169.254` (cloud metadata)
- Requests to `localhost` (unless `base_url` is localhost)
- Requests with a different scheme (`http` vs `https`)
- Requests with a different port

**The allowlist is strict**. If the connection's `base_url` is
`https://api.openai.com/v1`, the adapter can only reach
`https://api.openai.com:443/...`. Not `https://api.openai.com:8080/...`. Not
`https://openai.com/...`. Not `http://api.openai.com/...`.

**Special case — localhost equivalence**: The runtime treats
`localhost`, `127.0.0.1`, `::1`, and `0.0.0.0` as equivalent. This lets a
connection with `base_url = "http://localhost:11434"` reach
`http://127.0.0.1:11434/api/chat`. Necessary because Ollama and LM Studio
bind to different local interfaces on different platforms.

**Hardcoded denylist**: Even if `base_url` is set to one of the following,
the request is **blocked unconditionally**:

- `169.254.169.254` (AWS / GCP / Azure instance metadata)
- `metadata.google.internal` (GCP internal metadata)
- `169.254.170.2` (ECS task metadata)
- `100.100.100.200` (Alibaba Cloud metadata)

These are the classic SSRF targets. Even a misconfigured `base_url` cannot
reach them. If you legitimately need to reach one of these hosts (you almost
certainly do not), file an issue.

**Example**:

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": ["connection.api_key"],
  "workspace_access": false,
  "shell_execute": false
}
```

**When to use `"network": "none"`**: When the adapter operates entirely
offline. No real provider does this today, but the flag is reserved for future
local-only adapters (e.g., a tokenizer-only "provider").

### `secrets: ["connection.api_key"]`

**What it grants**: The adapter can read **its OWN connection's** `api_key`.

**What it does NOT grant**:
- Reading other connections' API keys
- Reading the API key from another package's connection
- Reading secrets from `config.toml` or environment variables
- Reading secrets from the workspace

**The scope is absolute**. The adapter receives a `ConnectionContext` with
its own `api_key`. It has no other mechanism to access secrets.

**The empty list**: `"secrets": []` means "this adapter does not read any
secrets". Useful for local providers (Ollama, LM Studio) that do not require
authentication.

**Example**:

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": ["connection.api_key"],
  "workspace_access": false,
  "shell_execute": false
}
```

### Implicit permissions

Some actions do not require a declared permission because they are granted
implicitly by the fact that the adapter is running:

| Implicit permission | Granted automatically? |
|---------------------|------------------------|
| `llm.generate` | ✅ Yes (any loaded adapter can generate) |
| `llm.stream` | ✅ Yes (same) |
| `models.discover` | ✅ Yes (if `features.model_discovery` is true) |
| `usage.read` | ✅ Yes (only for its own invocations) |

You do not need to declare these in the manifest. They are covered by the
existing framework.

**Why implicit?** Because every adapter needs them. Requiring the declaration
would add noise to the manifest without adding security — an adapter that can
talk to a provider can always generate.

The actual security boundary is **network + secrets**, not the LMM actions.

---

## Forbidden permissions

These three permissions exist in the `PermissionKind` enum but **cannot be
declared in a manifest**. Attempting to declare them causes an install failure.

### `workspace.read` / `workspace_access: true`

**What it would grant**: Reading files in the workspace (the chat's
`<chat_files>` directory).

**Why forbidden**: Provider adapters never need to read workspace files. They
only talk to the LLM provider's HTTP API. Workspace access is for tools (e.g.,
`file_saver`, `bash`), not for adapters.

**If you need to read a file**: Use a `file` connection field. The user
provides the path, and the adapter opens it directly via Python's `open()`.
This is not "workspace access" — it is explicit user-supplied file reference.

**What happens if declared**:

```json
"permissions": {
  "workspace_access": true    // ← install fails
}
```

```
ValidationError: workspace_access must be false. Provider adapters never
need filesystem access; they only communicate with the provider endpoint
configured on the connection.
```

### `workspace.write` / (also triggered by `workspace_access: true`)

**What it would grant**: Writing files in the workspace.

**Why forbidden**: Same as `workspace.read` — provider adapters do not write
workspace files. If a provider's adapter needs to save a token cache, use the
package's own directory (`package_dir`) or a temp file.

### `shell.execute` / `shell_execute: true`

**What it would grant**: Spawning shell processes (`subprocess.run("rm -rf /")`).

**Why forbidden**: Provider adapters never need to spawn processes. Anything
that requires a shell is a red flag — it usually means the adapter is trying
to do something it should not.

**If you need to invoke a CLI tool**: You cannot. Use an Extension Package
(MCP server) instead — that is what MCP servers are for.

**What happens if declared**:

```json
"permissions": {
  "shell_execute": true    // ← install fails
}
```

```
ValidationError: shell_execute must be false. Provider adapters are not
permitted to spawn processes.
```

### Summary table

| Permission | Field | Allowed value |
|-----------|-------|--------------|
| Workspace read | `workspace_access` | **`false`** |
| Workspace write | `workspace_access` | **`false`** |
| Shell execute | `shell_execute` | **`false`** |

There is no way to enable these. Not through configuration, not through
environment variables, not through any runtime override. The schema rejects
them at parse time, before any file touches disk.

---

## Runtime enforcement

Permissions are declared in the manifest, but enforcement happens at runtime —
in the worker subprocess, at the moment a forbidden action is attempted.

### The `RuntimePermissionEnforcer`

Every worker builds a `RuntimePermissionEnforcer` on startup, after
`LOAD_ADAPTER`:

```python
# backend/peldrun/llm/plugin_host/permissions.py

class RuntimePermissionEnforcer:
    def __init__(self, *, provider_id, connection_id, declared_permissions, ...):
        ...

    def require(self, permission: str) -> None:
        """Raise PluginPermissionDeniedError if permission is not granted."""
        ...

    def assert_url_allowed(self, url: str) -> None:
        """Raise URLNotAllowedError if URL is not in the allowlist."""
        ...
```

The enforcer is passed to the adapter's constructor via `context.profile` (as
part of the `ProviderContext`) and used internally by the transport wrapper.

### Network allowlist enforcement

The enforcer wraps every outgoing HTTP request via `AllowlistAsyncTransport`:

```python
class AllowlistAsyncTransport:
    async def handle_async_request(self, request):
        url = str(request.url)
        self._enforcer.assert_url_allowed(url)
        return await self._inner.handle_async_request(request)
```

The wrapper is installed **before the adapter is imported**:

```python
# worker.py — startup sequence
1. capture_stdout_for_protocol()
2. import plugin_host modules
3. send READY
4. on LOAD_ADAPTER:
   a. build RuntimePermissionEnforcer
   b. install_httpx_guard(enforcer)   ← before adapter import
   c. import adapter
   d. instantiate adapter
```

**Why before import**: If the adapter (or its SDK) creates an `httpx.AsyncClient`
at module import time, that client is already subject to the allowlist. If we
imported the adapter first, the client would be created without a guard, and
the adapter could reach any URL.

### Redirect target enforcement

The wrapper checks **every** request, including redirect-following requests
that httpx creates internally. A `302 Location: https://evil.com` from a
compromised provider endpoint is blocked at the second hop.

### Secret isolation

The adapter receives only its own `ConnectionContext`:

```python
# adapter sees:
context.connection.api_key  # its own connection's key
context.connection.base_url # its own connection's URL
context.extra_params        # its own connection's custom fields
```

The adapter does **not** see:

```python
# adapter cannot reach:
other_connection.api_key    # no mechanism
config.toml                 # no mechanism
os.environ                  # unless the adapter reads it directly
workspace files             # not accessible via any API
```

**Exception — environment variables**: If the adapter calls
`os.environ.get("MY_KEY")` directly, it reads the worker process's environment.
Since `create_subprocess_exec` inherits the parent environment by default, the
worker sees every variable PELDRUN's main process sees.

This is **not** a leak of PELDRUN's own secrets — PELDRUN does not put secrets
in environment variables. But if the operator set `AWS_SECRET_ACCESS_KEY` in
their shell before starting PELDRUN, an adapter could read it.

**If you are concerned about this**: Do not set secrets in PELDRUN's process
environment. Use PELDRUN's own connection management instead.

### Secret access via `get_api_key()`

The adapter does **not** read `context.connection.api_key` directly. It goes
through the enforcer:

```python
api_key = enforcer.get_api_key()   # returns None if permission not declared
```

`get_api_key()` returns `None` when `secrets.read:connection` is not granted.
An adapter that does not declare the permission sees `None` even if the key
is present in the connection.

**Why `None` and not an exception**: Because the adapter is third-party code.
An exception could be caught and ignored. `None` is silent — the adapter
either handles it (and skips a code path) or fails at the API level (401 from
the provider).

### Error surfacing

When the enforcer blocks an action, it raises:

| Action | Exception | Description |
|--------|-----------|-------------|
| Permission not granted | `PluginPermissionDeniedError` | Generic permission violation |
| URL not in allowlist | `URLNotAllowedError` | SSRF / unauthorized URL |

Both exceptions include:
- The provider id
- The connection id
- The attempted action (URL or permission name)
- The declared permissions (for context)

The supervisor rehydrates these into canonical `ProviderError` subclasses. The
caller sees a clear error message.

### Error example

```
ProviderError (code: "plugin_url_not_allowed"):
  Provider package 'gigachat' (connection 'conn_abc123') attempted to
  reach host 'evil.com', but the connection's base_url uses host
  'gigachat.devices.sberbank.ru'. Host must match.

  Details: {
    "attempted_url": "https://evil.com/api/data",
    "attempted_host": "evil.com",
    "allowed_host": "gigachat.devices.sberbank.ru",
    "reason": "host_mismatch",
    "connection_id": "conn_abc123"
  }
```

---

## When permissions are checked

Permissions are checked at **three points** in the lifecycle.

### 1. Manifest validation (install time)

The manifest's `permissions` block is validated by Pydantic when the package
is uploaded:

```python
class PackagePermissions(BaseModel):
    @field_validator("workspace_access")
    def _reject_workspace_access(cls, v):
        if v:
            raise ValueError("workspace_access must be false...")
        return v

    @field_validator("shell_execute")
    def _reject_shell_execute(cls, v):
        if v:
            raise ValueError("shell_execute must be false...")
        return v
```

Any forbidden permission causes an install failure with a clear error. The
user never sees a broken package — it is rejected before extraction.

### 2. Enforcer construction (worker startup)

The enforcer is built when the worker receives `LOAD_ADAPTER`. It validates:

- Every declared permission is in `KNOWN_PERMISSIONS`
- No forbidden permission is declared
- The `network` field is `"none"` or `"connection_endpoint"`
- The `secrets` array contains only recognized values

A corrupt IPC payload (from a compromised supervisor) is rejected here. The
worker cannot be tricked into granting extra permissions.

### 3. Action enforcement (request time)

The enforcer is called **at the moment an action is attempted**:

- Before every HTTP request → `assert_url_allowed(url)`
- Before every API key read → `get_api_key()` (checks permission silently)
- When the adapter needs a specific permission → `require(perm)`

This is **defense in depth**. Even if the first two checks were bypassed (via
a compromised supervisor, a memory corruption bug), the third check catches
the violation at the point of use.

### Non-checks

Some actions are **not** checked at request time because they cannot be
violated:

| Action | Why no check needed |
|--------|-------------------|
| Token count reading | Only for its own invocations |
| Streaming | Granted implicitly |
| Model discovery | Granted implicitly if feature enabled |
| Reasoning parameters | Handled by `RequestCompiler`, not the enforcer |

---

## Threat model

The permission system is a **defense-in-depth** measure, not a sandbox. Here
is what it protects against and what it does not.

### What it protects against

✅ **Accidental SSRF** — An adapter that hardcodes a URL instead of using
`base_url` is caught by the allowlist.

✅ **Misbehaving SDKs** — A provider's SDK that tries to reach its telemetry
endpoint is blocked (unless it is the same host as `base_url`).

✅ **Cross-connection secret access** — An adapter cannot read another
connection's API key, even if it knows the connection ID.

✅ **Wrong permission declarations** — A package that declares
`workspace_access: true` is rejected at install.

✅ **Broken or malicious manifests** — Pydantic validation catches malformed
or forbidden declarations before any file lands on disk.

### What it does NOT protect against

❌ **A malicious package with valid permissions** — A package that declares
only `network.connect` and `secrets.read:connection` can still:
- Exfiltrate its own API key to the provider it declares in `base_url`
- Send arbitrary data to the provider's API
- Log the user's messages to `stderr`
- Slow down the worker (rate-limit itself)

❌ **Raw socket connections** — An adapter that uses `socket.socket()` directly
bypasses the httpx allowlist. It can reach any host.

❌ **`requests` library** — An adapter that imports `requests` (not `httpx`)
bypasses the allowlist. Only `httpx.AsyncClient` is wrapped.

❌ **Environment variable reads** — An adapter can call
`os.environ.get("ANY_VAR")` and read any variable present in the worker's
environment (inherited from PELDRUN's main process).

❌ **File I/O beyond declared fields** — An adapter can call
`open("/etc/passwd")` and read any file the PELDRUN user can read. `file`
connection fields are only a UI convention, not a runtime restriction.

❌ **Subprocess spawning** — An adapter can call `subprocess.run("rm -rf ~")`
and delete the user's home directory. The `shell_execute: false` declaration
is only a manifest-level declaration, not a runtime block.

### Why not a full sandbox?

A full sandbox (seccomp, AppArmor, containers) is **not possible** with
Python's in-process model. Any Python code that runs inside the worker can:

- Call `os.system()`
- Call `socket.socket()`
- `import os; os.remove(...)`
- Read any file the user can read

To prevent this, we would need OS-level isolation. The current PELDRUN
architecture does not do this.

### What PELDRUN actually provides

The permission system prevents **accidents**, not **attacks**. A malicious
package author can bypass every runtime restriction by writing Python code
that uses low-level APIs instead of `httpx`.

The installation-time checks (`workspace_access: false`,
`shell_execute: false`, manifest validation) prevent **honest mistakes** — a
well-intentioned adapter that tries to use the workspace or spawn a process
is rejected at install, before it can cause damage.

**The real security boundary is user trust**: `install` is a manual action.
The user reviews the package before installing it. The permission system is
metadata to help them make that decision — it is not a technical enforcement
mechanism.

### What OS-level sandboxing would add (future)

The architecture already supports sandboxing without changes:

| Sandboxing layer | What it would enforce |
|------------------|----------------------|
| `seccomp` | Block `socket()` for hosts not in the allowlist, block `execve` |
| `AppArmor` / `SELinux` | Restrict file access to the package directory |
| Network namespace | Block all egress except to `base_url` |
| Container runtime | Full isolation (gVisor, Kata Containers, Firecracker) |

The `RuntimePermissionEnforcer` API is designed to be extended with OS-level
hooks. When a future PELDRUN version ships with container-based workers, the
existing permission model becomes a real sandbox — no manifest changes needed.

**Until then**: Treat provider packages like any other pip-installable Python
package. Only install from authors you trust.

---

## Per-provider examples

### Minimal (local provider, no auth)

Ollama, LM Studio, or a local inference server that requires no credentials
and runs on `localhost`.

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": [],
  "workspace_access": false,
  "shell_execute": false
}
```

**Why `secrets: []`**: Local servers typically ignore the API key. The
connection's `api_key` field is empty or a dummy value ("EMPTY"). The adapter
does not need to read it.

**Why `network: "connection_endpoint"`**: The adapter still needs to make
HTTP requests to `http://localhost:11434/api/chat` — that is the provider's
endpoint.

### Standard cloud provider (with API key)

OpenAI, Anthropic, DeepSeek, Groq, Mistral, or any cloud provider that uses
a Bearer token.

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": ["connection.api_key"],
  "workspace_access": false,
  "shell_execute": false
}
```

**Why `secrets: ["connection.api_key"]`**: The adapter needs to read the
key to add `Authorization: Bearer <key>` to each request.

### Provider with custom TLS

GigaChat, or any provider that requires a custom CA bundle.

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": ["connection.api_key"],
  "workspace_access": false,
  "shell_execute": false
}
```

**No additional permission needed**. Reading the CA bundle file is not
"workspace access" — the user provided the path via a connection field.

### Provider with SigV4 signing

AWS Bedrock, or any provider that requires request signing.

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": ["connection.api_key"],
  "workspace_access": false,
  "shell_execute": false
}
```

**Same permissions as standard**. The SigV4 signing is done by the adapter's
SDK (`boto3`), not by PELDRUN. The adapter still only needs to reach the
provider's endpoint and read its own credentials.

### Provider that needs to read an SSH key

Some enterprise providers require an SSH key for authentication. This is
**not** a normal pattern.

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": ["connection.api_key"],
  "workspace_access": false,
  "shell_execute": false
}
```

**The SSH key is a connection field**, not a permission:

```json
"connection_fields": [
  {
    "key": "ssh_private_key_path",
    "label": "SSH Private Key",
    "type": "secret_file",
    "required": true,
    "placeholder": "/home/user/.ssh/provider_key"
  }
]
```

The adapter opens the file directly:

```python
ssh_key_path = extra.get("ssh_private_key_path")
with open(ssh_key_path, "r") as f:
    private_key = f.read()
```

This is **not** blocked — `file` connection fields are explicit
user-supplied paths. The permission model does not restrict file reads
(otherwise the CA bundle use case would break).

### Provider that needs local IPC (rare, not recommended)

A provider that talks to a local daemon via a Unix socket (e.g., a hardware
security module).

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": ["connection.api_key"],
  "workspace_access": false,
  "shell_execute": false
}
```

**This is problematic**. The adapter would connect to
`unix:///var/run/provider.sock`, which is not an HTTP URL. The allowlist only
covers `http` and `https` schemes.

**Workaround**: The adapter can use `socket.socket(socket.AF_UNIX)` directly,
which is not blocked by the allowlist. But this is **bypassing** the
permission model, and is not recommended.

If you genuinely need this, contact the PELDRUN team — a future version may
add `network.unix_socket` permission.

### Example table

| Provider type | `network` | `secrets` |
|--------------|-----------|-----------|
| Local (Ollama, LM Studio) | `connection_endpoint` | `[]` |
| Cloud (OpenAI, Anthropic) | `connection_endpoint` | `["connection.api_key"]` |
| Cloud with custom TLS | `connection_endpoint` | `["connection.api_key"]` |
| AWS / Azure / GCP | `connection_endpoint` | `["connection.api_key"]` |
| Provider with SSH key | `connection_endpoint` | `["connection.api_key"]` |
| Provider with custom IPC | `connection_endpoint` | `["connection.api_key"]` (with warning) |

**The `permissions` block is nearly identical for every provider**. The
differences are in `connection_fields` and `runtime_hints`, not in
permissions. This is intentional — permissions describe what the adapter can
do with its process, not what it needs from the user.

---

## Testing permissions

### Local test — verify the enforcer blocks unauthorized URLs

You can test the allowlist locally without a full PELDRUN install:

```python
# test_permissions.py
import asyncio
from peldrun.llm.plugin_host.permissions import (
    RuntimePermissionEnforcer,
    URLNotAllowedError,
)

async def main():
    enforcer = RuntimePermissionEnforcer(
        provider_id="test",
        connection_id="test-conn",
        declared_permissions=["network.connect", "secrets.read:connection"],
        connection_base_url="https://api.example.com/v1",
        connection_api_key="sk-test",
    )

    # Should pass
    enforcer.assert_url_allowed("https://api.example.com/v1/chat")
    print("✅ Same host, same port — allowed")

    # Should raise
    try:
        enforcer.assert_url_allowed("https://evil.com/steal")
        print("❌ Should have raised")
    except URLNotAllowedError as e:
        print(f"✅ Blocked: {e}")

    # Should raise
    try:
        enforcer.assert_url_allowed("https://api.example.com:8080/chat")
        print("❌ Should have raised")
    except URLNotAllowedError as e:
        print(f"✅ Blocked (port mismatch): {e}")

    # Should raise (metadata IP)
    try:
        enforcer.assert_url_allowed("http://169.254.169.254/metadata")
        print("❌ Should have raised")
    except URLNotAllowedError as e:
        print(f"✅ Blocked (metadata IP): {e}")

asyncio.run(main())
```

### Local test — verify forbidden permissions are rejected

```python
from peldrun.omweb.provider_packages.schema import PackagePermissions
from pydantic import ValidationError

def test_forbidden_permissions():
    # Should raise
    try:
        PackagePermissions(workspace_access=True)
        print("❌ Should have raised")
    except ValidationError as e:
        print(f"✅ Blocked: workspace_access=true")

    # Should raise
    try:
        PackagePermissions(shell_execute=True)
        print("❌ Should have raised")
    except ValidationError as e:
        print(f"✅ Blocked: shell_execute=true")

    # Should pass
    perms = PackagePermissions(
        network="connection_endpoint",
        secrets=["connection.api_key"],
        workspace_access=False,
        shell_execute=False,
    )
    print(f"✅ Valid: {perms.network}")
```

### Integration test — verify a live request is blocked

Install a package that declares `network: "none"`, then try to use it. The
first request will fail with:

```
PluginPermissionDeniedError (code: "plugin_permission_denied"):
  Provider package 'test' attempted a network call but did not declare
  the 'network.connect' permission.
```

This verifies end-to-end enforcement.

---

## Common mistakes

### Mistake 1 — declaring `workspace_access: true`

```json
"permissions": {
  "workspace_access": true   // ← INSTALL FAILS
}
```

**Fix**: Set to `false`. If you need to read a file, use a `file` connection
field instead.

### Mistake 2 — declaring `shell_execute: true`

```json
"permissions": {
  "shell_execute": true      // ← INSTALL FAILS
}
```

**Fix**: Set to `false`. If you need to spawn a process, use an MCP server
extension instead.

### Mistake 3 — omitting the `secrets` field

```json
"permissions": {
  "network": "connection_endpoint",
  "workspace_access": false,
  "shell_execute": false
}
// ← missing "secrets"
```

**This is valid** — `secrets` defaults to `[]`. But if your adapter calls
`context.connection.api_key`, it will get `None` because the permission was
not granted.

**Fix**: Add `"secrets": ["connection.api_key"]` if you need the API key.

### Mistake 4 — declaring `network: "any"` or `"*"`

```json
"permissions": {
  "network": "any"           // ← INSTALL FAILS
}
```

**Fix**: Use `"connection_endpoint"`. There is no "unrestricted network"
mode.

### Mistake 5 — attempting to bypass the allowlist

```python
# WRONG — bypasses the allowlist
import socket
s = socket.socket()
s.connect(("evil.com", 443))
```

This works (the allowlist only wraps `httpx.AsyncClient`), but it violates
the spirit of the permission model. The package author must not do this.

**Fix**: Use the SDK's HTTP client (which goes through the allowlist).

### Mistake 6 — attempting to read `context.connection.api_key` without the permission

```python
api_key = context.connection.api_key   # returns None if not granted
client = MySDKClient(api_key=api_key)  # ← 401 on every request
```

The API key is `None` because the permission was not granted. Your adapter
will silently fail with 401s.

**Fix**: Declare `"secrets": ["connection.api_key"]` in the manifest.

### Mistake 7 — using `os.environ` to bypass the enforcer

```python
# WRONG
api_key = os.environ.get("MY_PROVIDER_API_KEY")
```

The environment variable must be set on PELDRUN's main process. If it is not,
the adapter gets `None`. If it is set, the adapter bypasses PELDRUN's
connection management.

**Fix**: Read the API key via `context.connection.api_key`, and declare
`supports_env_vars: true` in `runtime_hints` if you want an env var fallback.

---

## Anti-patterns

### ❌ Declaring every permission "just in case"

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": ["connection.api_key"],
  "workspace_access": false,
  "shell_execute": false
}
// Even though the adapter never reads the API key
```

**Problem**: The user sees a warning "may read the API key" even though the
adapter never does. Trust erosion.

**Fix**: Declare only what you use.

### ❌ No `secrets` for local providers that do need auth

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": [],   // ← WRONG if the adapter reads the key
  "workspace_access": false,
  "shell_execute": false
}
```

**Problem**: Some local providers (e.g., a self-hosted vLLM server with auth)
do require an API key. If your adapter reads it but does not declare the
permission, the key is `None`.

**Fix**: Declare `"secrets": ["connection.api_key"]`.

### ❌ Trying to work around workspace_access

```python
# WRONG — reads workspace via os.environ, bypassing enforcer
workspace_root = os.environ.get("PELDRUN_WORKSPACE_ROOT")
for f in os.listdir(workspace_root):
    ...
```

**Problem**: This bypasses the permission model. It is not a supported use
case, and may break in future versions.

**Fix**: Use a `file` connection field. The user provides the path, and the
adapter opens it explicitly.

### ❌ Using permissions to gate features

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": ["connection.api_key"],
  "workspace_access": false,
  "shell_execute": false,
  "tool_calling": true    // ← does not exist
}
```

**Problem**: `tool_calling` is not a permission. It is a feature flag
(`features.tool_calling`). Permissions are not for feature gating.

**Fix**: Move `tool_calling` to `features`.

### ❌ Assuming the enforcer prevents all attacks

**Problem**: The enforcer is defense-in-depth, not a sandbox. A malicious
package can bypass it via raw sockets, `subprocess`, or `os.environ`.

**Fix**: Treat every package as trusted code. Only install from authors you
trust. The permission system helps honest packages stay honest; it does not
stop malicious ones.

### ❌ Trying to add new permissions to the manifest

```json
"permissions": {
  "network": "connection_endpoint",
  "secrets": ["connection.api_key"],
  "workspace_access": false,
  "shell_execute": false,
  "gpu_access": true      // ← ValidationError
}
```

**Problem**: `extra="forbid"` rejects unknown fields. Adding a new permission
requires a PELDRUN code change (schema + enforcement), not just a manifest
declaration.

**Fix**: Use the existing permissions. If you need more, file an issue with
the PELDRUN team.

---

## Checklist

Before shipping your package:

### Manifest

- [ ] `permissions.network` is `"connection_endpoint"` (or `"none"` for
      offline adapters)
- [ ] `permissions.secrets` contains only `"connection.api_key"` (or is empty)
- [ ] `permissions.workspace_access` is `false`
- [ ] `permissions.shell_execute` is `false`
- [ ] No unknown fields in `permissions`

### Adapter code

- [ ] Reads API key via `context.connection.api_key` (not `os.environ`)
- [ ] Uses `httpx` (via the SDK) — not `requests` or `socket`
- [ ] Does not attempt to read workspace files
- [ ] Does not attempt to spawn processes
- [ ] Handles `api_key is None` gracefully
- [ ] Maps SDK errors to `ProviderError` subclasses
- [ ] Does not use `except BaseException`

### Testing

- [ ] Runs in local test (`python test_adapter.py`)
- [ ] Test connection succeeds (`validate_connection()` returns `{ok: true}`)
- [ ] Discover models returns results
- [ ] Chat message succeeds
- [ ] Streaming works
- [ ] Cancellation works (Stop button)

### Security review

- [ ] The adapter does not bypass the allowlist (no raw sockets, no
      `requests` library)
- [ ] The adapter does not read environment variables that contain PELDRUN
      secrets
- [ ] The adapter only reads files explicitly provided via `file` /
      `secret_file` connection fields
- [ ] The adapter does not attempt to spawn processes
- [ ] Every `open()` call uses a path from a connection field, not a
      hardcoded path
- [ ] No hardcoded API keys or credentials
- [ ] No hardcoded URLs (always use `base_url` from the connection)

### Documentation

- [ ] The manifest declares every required field
- [ ] `runtime_hints` correctly describes any UI warnings
- [ ] The package's README (if any) explains the security model
- [ ] The user is warned about `requires_custom_tls` /
      `requires_token_refresh` if applicable

---

## What's next

- **Need the manifest reference?** → [Manifest Reference](04-manifest-reference.md)
- **Need to write the adapter?** → [Adapter Protocol](06-adapter-protocol.md)
- **Need to understand the architecture?** → [Architecture](01-architecture.md)
- **Need real-world examples?** → [Examples](13-examples.md)
- **Concerned about security?** → [Security Model](12-security-model.md) for
  the full threat model (this doc covers just the permission aspect)
```
