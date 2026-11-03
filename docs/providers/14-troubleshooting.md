### `docs/providers/14-troubleshooting.md`

```markdown
<!-- docs/providers/14-troubleshooting.md -->
# Troubleshooting

> **Practical guide** to diagnosing and fixing every common error in the
> Provider Packages system.
>
> **Reading time**: 20-30 minutes (use as reference, not linear read).
>
> **Who this is for**: Anyone hitting an error — whether during install,
> configure, test, or use.
>
> **How to use this document**:
> 1. Find your error message in the table of contents
> 2. Follow the diagnostic steps
> 3. If the fix doesn't work, use the "Getting help" section

---

## Table of Contents

- [First steps — 5-minute diagnostic](#first-steps--5-minute-diagnostic)
- [Install errors](#install-errors)
  - [PROVIDER_PACKAGE_WRONG_FILE_TYPE](#provider_package_wrong_file_type)
  - [PROVIDER_PACKAGE_EMPTY_UPLOAD](#provider_package_empty_upload)
  - [PROVIDER_PACKAGE_INVALID](#provider_package_invalid)
  - [PROVIDER_PACKAGE_SECURITY_VIOLATION](#provider_package_security_violation)
  - [PROVIDER_PACKAGE_MANIFEST_INVALID](#provider_package_manifest_invalid)
  - [PROVIDER_PACKAGE_CONFLICT](#provider_package_conflict)
  - [PROVIDER_PACKAGE_ENVIRONMENT_ERROR](#provider_package_environment_error)
  - [PROVIDER_PACKAGE_STORAGE_ERROR](#provider_package_storage_error)
- [Adapter load errors](#adapter-load-errors)
  - [ADAPTER_LOAD_FAILED](#adapter_load_failed)
  - [ADAPTER_NOT_LOADED](#adapter_not_loaded)
  - [STARTUP_TIMEOUT](#startup_timeout)
- [Request failures](#request-failures)
  - [auth_failed (401)](#auth_failed-401)
  - [permission_denied (403)](#permission_denied-403)
  - [rate_limited (429)](#rate_limited-429)
  - [quota_exceeded](#quota_exceeded)
  - [timeout](#timeout)
  - [unavailable (5xx)](#unavailable-5xx)
  - [context_length_exceeded](#context_length_exceeded)
  - [unsupported_feature](#unsupported_feature)
- [Streaming issues](#streaming-issues)
  - [Stream hangs after tokens appear](#stream-hangs-after-tokens-appear)
  - [Stream never starts](#stream-never-starts)
  - [Stream stops mid-response](#stream-stops-mid-response)
  - [Stream cancelled unexpectedly](#stream-cancelled-unexpectedly)
- [Connection issues](#connection-issues)
  - [Configure button does nothing](#configure-button-does-nothing)
  - [Not configured badge won't go away](#not-configured-badge-wont-go-away)
  - [Test fails but chat works](#test-fails-but-chat-works)
  - [Masked value error](#masked-value-error)
  - [No active connection fallback](#no-active-connection-fallback)
- [Health check failures](#health-check-failures)
  - [active_pointer failed](#active_pointer-failed)
  - [version_directory failed](#version_directory-failed)
  - [manifest_present failed](#manifest_present-failed)
  - [entry_point_present failed](#entry_point_present-failed)
  - [venv_present failed](#venv_present-failed)
- [Performance issues](#performance-issues)
  - [First request is very slow](#first-request-is-very-slow)
  - [Every request is slow](#every-request-is-slow)
  - [Streaming is slower than expected](#streaming-is-slower-than-expected)
- [Where to find logs](#where-to-find-logs)
- [Getting help](#getting-help)

---

## First steps — 5-minute diagnostic

Before diving into specifics, run through these five checks. Most errors
resolve here.

### 1. Is the package installed and enabled?

**In the UI**:
- Store → Providers → Installed tab
- Does your package appear?
- Is the toggle switch on (green)?

**Via API**:
```bash
curl http://localhost:8088/api/store/providers | jq '.providers[] | {id, enabled}'
```

Expected: your package with `"enabled": true`.

### 2. Is there an active connection?

**In the UI**:
- Configure modal → the connection should show "Active" (green star badge)

**Via API**:
```bash
curl http://localhost:8088/api/store/providers/YOUR_PACKAGE_ID | jq '.has_active_connection'
```

Expected: `true`.

### 3. Does the test button work?

**In the UI**: Configure → ⚡ button on the connection row.

**Via API**:
```bash
CONN_ID=$(curl -s http://localhost:8088/api/store/providers/YOUR_PACKAGE_ID/connections | jq -r '.connections[0].id')
curl -X POST http://localhost:8088/api/store/providers/YOUR_PACKAGE_ID/connections/$CONN_ID/test
```

Expected: `{"ok": true, "latency_ms": ...}`.

If it fails, note the exact error — it points to the root cause (auth, network,
wrong URL).

### 4. Is the package healthy?

```bash
curl http://localhost:8088/api/store/providers/YOUR_PACKAGE_ID/health | jq '.health.status'
```

Expected: `"healthy"`.

If `"degraded"` or `"broken"`, see [Health check failures](#health-check-failures).

### 5. Are there backend errors?

Check the backend logs:

```bash
# If running via start-backend script
tail -f logs/backend.log | grep -i "provider\|plugin_host"

# If running via uvicorn directly
# Logs go to your terminal
```

Look for:
- `Provider package ... failed`
- `Worker for provider ... crashed`
- `Transport error`

If you see these, they contain the real error.

### When the 5 checks pass

If all five pass but the chat still fails, the issue is likely in:
- Model selection (wrong model name)
- Chat configuration (LLM Settings picked a different provider)
- The specific request (context too long, tools misconfigured)

Jump to the relevant section below.

---

## Install errors

### PROVIDER_PACKAGE_WRONG_FILE_TYPE

**Message**: `Provider Packages must be uploaded as .zip archives.`

**Cause**: The uploaded file does not end in `.zip`.

**Fix**:
1. Verify the file extension: `ls -la your-file.zip`
2. Re-zip if it is `.rar`, `.7z`, or a folder:
   ```bash
   cd your-package-folder
   zip -r your-package.zip .
   ```

**Note**: The check is on the filename, not the content. Renaming a `.rar`
to `.zip` will pass this check but fail at the extract step.

---

### PROVIDER_PACKAGE_EMPTY_UPLOAD

**Message**: `Uploaded file is empty.`

**Cause**: The uploaded file has zero bytes.

**Fix**:
1. Check the file: `ls -la your-package.zip`
2. If zero bytes, re-zip.

**Common cause**: A failed download from the source, or a `zip` command that
failed silently.

---

### PROVIDER_PACKAGE_INVALID

**Message**: Depends on the failure. Examples:

- `manifest.json is missing.`
- `Archive contains zero files.`
- `Archive contains N files, exceeding the limit of 2000.`
- `Total uncompressed size ... exceeds the limit of ...`
- `entry_point module 'X' does not resolve`

**Diagnosis**:

```bash
# Inspect the ZIP contents
unzip -l your-package.zip

# Verify it is a valid ZIP
unzip -t your-package.zip
```

**Common causes**:

| Cause | Fix |
|-------|-----|
| No `manifest.json` at root | Move it to the root, or wrap everything in a single top-level folder |
| Manifest inside 2+ folders deep | Flatten to root or single-folder |
| More than 2000 files | Split into multiple packages or remove unnecessary files |
| Uncompressed > 500 MB | Remove large data files |
| `entry_point` points to missing file | Verify file path in the ZIP matches `entry_point` |

**Verify the manifest location**:

```bash
unzip -l your-package.zip | grep manifest.json
```

**Expected** (any of these is fine):
```
1234  2025-01-01 00:00   manifest.json
1234  2025-01-01 00:00   my-package/manifest.json
```

**Wrong** (rejected):
```
1234  2025-01-01 00:00   my-package/v1/manifest.json
```

**Verify `entry_point` resolves**:

For `entry_point: "gigachat_peldrun.adapter:GigaChatAdapter"`, the ZIP
must contain one of:
```
gigachat_peldrun/adapter.py
gigachat_peldrun/adapter/__init__.py
src/gigachat_peldrun/adapter.py
src/gigachat_peldrun/adapter/__init__.py
```

---

### PROVIDER_PACKAGE_SECURITY_VIOLATION

**Message**: Depends on the failure. Examples:

- `Unsafe path '../etc/passwd': path contains '..' traversal.`
- `Symlink entry 'link' is not permitted.`
- `Compression ratio 150:1 exceeds the limit of 100:1.`
- `path exceeds 1024 characters`

**Cause**: The ZIP contains a potentially dangerous structure.

**Diagnosis**:

```bash
# Check for path traversal
unzip -l your-package.zip | grep '\.\.'

# Check for symlinks
unzip -l your-package.zip | grep '^l'
```

**Fix**:

| Cause | Fix |
|-------|-----|
| `..` in a path | Rebuild the ZIP — the file paths should be relative |
| Absolute path | Rebuild with relative paths |
| Symlink | Remove it — Provider Packages must ship regular files |
| Archive bomb | Reduce compression (see below) |
| Path > 1024 chars | Rename files to shorter paths |

**If the package is legitimate but flagged**:

For archive bombs (compression ratio > 100:1):

```bash
# Check the ratio
zipinfo -t your-package.zip

# Rebuild with lower compression
cd your-package-folder
zip -r -0 your-package.zip .   # -0 = store only (no compression)
```

For symlinks:

```bash
# Find the symlink
find . -type l

# Replace it with the file it points to
cp -L link target_file
rm link
```

**If you believe this is a false positive**: Do not disable the check.
Instead, report it as a bug at the PELDRUN repository with the ZIP attached.

---

### PROVIDER_PACKAGE_MANIFEST_INVALID

**Message**: Depends on the field. Examples:

- `manifest.id: String should match pattern '^[a-z0-9][a-z0-9._-]*$'`
- `manifest.version: version 'latest' must be semver`
- `manifest.transport: Compatible packages must declare 'base_adapter'`
- `manifest.permissions.workspace_access: workspace_access must be false`
- `manifest.dependencies.0: Every entry in 'dependencies' must be a non-empty PEP 508 specifier string`

**Cause**: A field in `manifest.json` fails Pydantic validation.

**Diagnosis**:

The error message contains `manifest.<field>: <reason>`. Read it carefully —
it tells you exactly which field failed and why.

**Common fixes**:

| Field | Wrong | Right |
|-------|-------|-------|
| `id` | `"Acme AI"` | `"acme-ai"` |
| `id` | `"Acme-AI"` | `"acme-ai"` |
| `version` | `"latest"` | `"1.0.0"` |
| `version` | `"1.0"` | `"1.0.0"` |
| `transport` | missing | `"compatible"` or `"native"` or `"custom"` |
| `base_adapter` | missing (compatible) | `"openai_compat"` |
| `entry_point` | `"adapter"` (native) | `"module.adapter:ClassName"` |
| `permissions.workspace_access` | `true` | `false` |
| `permissions.shell_execute` | `true` | `false` |
| `secrets` | `["api_key"]` | `["connection.api_key"]` |

**Verify locally** before upload:

```python
# validate_manifest.py
import json
from omweb.provider_packages.schema import ProviderManifest

with open("manifest.json") as f:
    manifest_dict = json.load(f)

try:
    manifest = ProviderManifest.model_validate(manifest_dict)
    print("✅ Valid manifest")
    print(f"  ID: {manifest.id}")
    print(f"  Version: {manifest.version}")
    print(f"  Transport: {manifest.transport.value}")
except Exception as e:
    print("❌ Invalid manifest:")
    for error in e.errors() if hasattr(e, "errors") else [str(e)]:
        print(f"  {error}")
```

**Unknown field errors**:

If you see `manifest.field_name: Extra inputs are not permitted`, you have a
typo or a field that does not exist. Check the [Manifest Reference](04-manifest-reference.md)
for the complete list.

Common typos:
- `dependecies` → `dependencies`
- `conn_fields` → `connection_fields`
- `runtime_hint` → `runtime_hints`

---

### PROVIDER_PACKAGE_CONFLICT

**Message**: One of:

- `Provider ID 'anthropic' is already used by a built-in adapter`
- `Version '1.0.0' of provider 'gigachat' is already installed with different content`

**Cause A — Builtin collision**:

You chose an `id` that matches a built-in adapter. Forbidden ids:
- `openai_compat`
- `lmstudio`
- `ollama`
- `openai_native`
- `anthropic`
- `google_genai`
- `ollama_native`
- `lmstudio_native`

**Fix**: Choose a different `id`:
- `"my-anthropic"` instead of `"anthropic"`
- `"anthropic-proxy"` instead of `"anthropic"`
- `"acme-anthropic"` instead of `"anthropic"`

**Cause B — Hash mismatch**:

You uploaded a ZIP with the same version as an existing install, but the
content differs (different SHA-256).

**Fix** (two options):

1. **Bump the version** (recommended):
   ```json
   "version": "1.0.1"   // ← was 1.0.0
   ```
   This installs alongside the existing version. Both are preserved.

2. **Uninstall the existing version first**:
   ```bash
   curl -X DELETE http://localhost:8088/api/store/providers/YOUR_ID
   ```
   Then reinstall. **Warning**: this deletes all version history. The user's
   connections are preserved.

**Note**: Re-uploading the EXACT same ZIP (same version, same hash) is not a
conflict — it is an idempotent re-install (no-op with a warning).

---

### PROVIDER_PACKAGE_ENVIRONMENT_ERROR

**Message**: Depends on the failure. Examples:

- `pip install failed (exit 1, label=requirements.txt).`
- `pip install timed out after 600s`
- `Could not create virtual environment`
- `Provider package requires Python >=3.13, but the running interpreter is 3.12.0`

**Diagnosis**:

The error response contains a `details.pip_log_tail` field with the last 8KB
of pip output. **Read it.** It usually contains the exact cause.

**Common causes**:

#### Python version mismatch

**Message**: `Provider package requires Python >=3.13, but the running interpreter is 3.12.0`

**Fix**: Either:
1. Lower the requirement in `manifest.runtime.python` to match PELDRUN's
   Python version
2. Upgrade PELDRUN's Python to the required version (rare — most providers
   work with 3.12)

#### Missing package on PyPI

**Message**: `ERROR: Could not find a version that satisfies the requirement X`

**Fix**: Check the package name in `requirements.txt`. Typos are common:
- `reqeusts` → `requests`
- `pydantics` → `pydantic`
- `anthopic` → `anthropic`

Test locally:

```bash
python -m venv test-venv
source test-venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

#### Incompatible version pins

**Message**: `ERROR: Cannot install X==1.0 and Y==2.0 because these package versions have conflicting dependencies`

**Fix**: Relax or adjust the version pins. Common conflicts:
- `httpx==0.20.0` (old) vs `httpx>=0.25` (required by another package)
- `pydantic==1.x` vs `pydantic>=2.0`

Test locally with a fresh venv — if it fails there, it will fail during install.

#### Network timeout

**Message**: `pip install timed out after 600s`

**Fix**:
- Check network connectivity to PyPI
- If using a private mirror, verify `pip.conf` is correct
- For very large dependencies (AWS SDK, Google Cloud SDK), consider caching
  wheels locally

#### `venv` module unavailable

**Message**: `Could not create virtual environment`

**Fix**: Verify the `venv` module works:

```bash
python -m venv test-venv
```

If that fails, install the `python3-venv` package (Linux) or reinstall Python
(Windows).

#### Storage not writable

**Message**: `Could not create environments root`

**Fix**: Check disk permissions:

```bash
ls -la <storage>/store/providers/
touch <storage>/store/providers/.test && rm <storage>/store/providers/.test
```

If that fails, fix the permissions or point `PELDRUN_STORAGE_ROOT` to a
writable location.

---

### PROVIDER_PACKAGE_STORAGE_ERROR

**Message**: Depends. Examples:

- `Failed to persist registry at /path/to/registry.json`
- `Failed to promote package files into place`
- `Failed to write install metadata`

**Diagnosis**: The error response has a `details.path` field with the exact
path that failed.

**Common causes**:

| Cause | Fix |
|-------|-----|
| Disk full | Free space |
| Read-only filesystem | Remount rw, or fix permissions |
| Permission denied | `chmod` the storage directory |
| File locked (Windows) | Close the process holding the lock |

**Verify**:

```bash
ls -la <storage>/store/providers/
df -h .   # disk space
```

---

## Adapter load errors

### ADAPTER_LOAD_FAILED

**Message**: Occurs when the worker tries to load the adapter.

**Where to see it**: In the chat error or in backend logs.

**Cause**: One of:

1. `ImportError` — a dependency is not installed in the venv
2. `AttributeError` — the class name in `entry_point` does not exist
3. `TypeError` — the constructor signature is wrong
4. `ValueError` — the constructor raised (e.g., missing required field)

**Diagnosis**:

Check the venv exists and the adapter imports:

```bash
VENV_PYTHON=<storage>/store/providers/YOUR_ID/environments/VERSION/bin/python

# On Windows: <storage>\store\providers\YOUR_ID\environments\VERSION\Scripts\python.exe

$VENV_PYTHON -c "from module.adapter import ClassName; print('OK')"
```

If that fails, the error message tells you what's wrong.

**Common causes**:

| Error | Cause | Fix |
|-------|-------|-----|
| `ModuleNotFoundError: No module named 'x'` | Dependency not installed | Add to `requirements.txt`, reinstall |
| `ImportError: cannot import name 'X'` | Wrong import in adapter | Fix the import |
| `AttributeError: module has no attribute 'ClassName'` | Class name typo | Fix `entry_point` in manifest |
| `TypeError: __init__() got unexpected keyword argument 'context'` | Legacy constructor | Add `ADAPTER_STYLE = "v1"` and accept `context` |
| `ValueError: ...` from constructor | Missing required field | Check the field is passed from connection |

**Test locally**:

```python
# test_load.py
import os
os.environ["PATH"] = f"{VENV_PATH}/bin:" + os.environ["PATH"]

from gigachat_peldrun.adapter import GigaChatAdapter
print("Import OK")
```

---

### ADAPTER_NOT_LOADED

**Message**: `Adapter not loaded. Send a LOAD_ADAPTER request before GENERATE.`

**Cause**: A request was made to the worker before `LOAD_ADAPTER` completed.

**This should not happen** — the supervisor sends `LOAD_ADAPTER` immediately
after `READY` and waits for the acknowledgment. If you see this error, there
is a race condition in the supervisor.

**Fix**:
1. Check backend logs for `LOAD_ADAPTER` timing
2. Restart PELDRUN
3. If the error persists, file a bug

---

### STARTUP_TIMEOUT

**Message**: `Worker did not become ready within 30s` or
`Worker did not acknowledge LOAD_ADAPTER within 30s`

**Cause**: The worker subprocess did not respond within 30 seconds.

**Common causes**:

| Cause | Diagnosis | Fix |
|-------|-----------|-----|
| Slow Python import | Logs show `import` in progress | Increase `WORKER_STARTUP_TIMEOUT_SECONDS` |
| Adapter constructor blocks | Logs show hanging in `__init__` | Fix the adapter — no network calls in `__init__` |
| Venv is broken | `venv_python` returns error | Reinstall the package |
| Subprocess cannot spawn | OS-level issue | Check system resources |

**Diagnosis**:

Manually spawn the worker:

```bash
<venv_python> -m peldrun.llm.plugin_host.worker

# You should see:
# (JSON line) {"v":1,"type":"ready",...}
```

If it hangs, the adapter's `__init__` is likely blocking.

If it errors immediately, the venv is broken.

**Fix**:

- **Slow import**: This is normal for heavy SDKs (boto3 can take 5-10s on
  cold start). Subsequent requests reuse the worker.
- **Hanging `__init__`**: Refactor the adapter — no network calls during
  construction. Move them to `generate()` or `validate_connection()`.
- **Broken venv**: Uninstall and reinstall.

---

## Request failures

These are errors during `generate()` or `stream()`. They appear in the chat
UI or in the API response.

### auth_failed (401)

**Message**: `HTTP 401 — Authentication failed`

**Cause**: Invalid, expired, or missing API key.

**Diagnosis**:

1. **Test the connection**:
   ```bash
   CONN_ID=$(curl -s http://localhost:8088/api/store/providers/YOUR_ID/connections | jq -r '.connections[0].id')
   curl -X POST http://localhost:8088/api/store/providers/YOUR_ID/connections/$CONN_ID/test
   ```
   Expected: `{"ok": true}`. If `false`, the credentials are wrong.

2. **Check the key format**:
   - OpenAI: starts with `sk-`
   - Anthropic: starts with `sk-ant-`
   - GigaChat: base64-encoded `client_id:client_secret`
   - AWS: starts with `AKIA`

3. **Check for expired tokens**:
   - Yandex IAM: expires after 12h
   - AWS STS: expires after 1h
   - GigaChat OAuth: expires after 30 min

**Fix**:
- Re-enter the API key
- Regenerate expired tokens
- Verify the account tier (e.g., Claude access on a free tier)

---

### permission_denied (403)

**Message**: `HTTP 403 — Permission denied`

**Cause**: Valid credentials, but the account is not allowed to perform this
action.

**Common causes**:

| Provider | Cause | Fix |
|----------|-------|-----|
| OpenAI | No access to the model (e.g., GPT-4 on free tier) | Upgrade or pick a different model |
| Anthropic | Claude not enabled for this org | Enable Claude in Console |
| AWS | IAM user lacks `bedrock:InvokeModel` permission | Add permission |
| GigaChat | B2B scope without a contract | Use PERS scope, or contract with Sber |

**Diagnosis**: The error message usually names the required permission.

**Fix**: Update account permissions, or use a different account.

---

### rate_limited (429)

**Message**: `HTTP 429 — Rate limited`

**Cause**: Too many requests. The provider's rate limit for your account or
model tier was exceeded.

**Diagnosis**:

Check `retry_after_seconds` in the error details (if present).

**Fix**:

1. **Wait** — retryable errors auto-retry after a delay
2. **Reduce request frequency** — batch requests where possible
3. **Upgrade tier** — higher quotas on higher tiers
4. **Use multiple connections** — activate a different account temporarily

**If you control the client**: Implement exponential backoff (PELDRUN's
`ProviderRateLimitError.retryable == True` triggers automatic retry).

---

### quota_exceeded

**Message**: `HTTP 429 — Quota exceeded` or `Insufficient credits`

**Cause**: Account out of credits or hit the monthly quota. Distinct from
rate limit — this is permanent until the user tops up.

**Diagnosis**: The error is `ProviderQuotaExceededError`, which has
`retryable: false` (unlike rate limit).

**Fix**:
1. Add credits to the account
2. Switch to a different connection (different billing account)
3. Switch to a different provider

**How to distinguish from rate limit**: The error message contains "quota"
or "credit" rather than "rate limit". The `code` field is `quota_exceeded`.

---

### timeout

**Message**: `Timeout` or `The provider did not respond within 30s`

**Cause**: The provider did not return a response within the configured
timeout.

**Diagnosis**:

| Timeout | Default | Where |
|---------|---------|-------|
| Request timeout | 600s | `REQUEST_TIMEOUT_SECONDS` |
| Stream first chunk | 90s | `STREAM_FIRST_CHUNK_TIMEOUT_SECONDS` |
| Stream inter-chunk | 120s | `STREAM_INTER_CHUNK_TIMEOUT_SECONDS` |
| Worker startup | 30s | `WORKER_STARTUP_TIMEOUT_SECONDS` |

**Common causes**:

| Cause | Fix |
|-------|-----|
| Provider is down | Check provider status page |
| Network issues | Retry, check firewall |
| Slow model (thinking) | Increase `STREAM_FIRST_CHUNK_TIMEOUT_SECONDS` |
| Provider rate-limited silently | Check with `test` endpoint |

**Diagnosis**:

```bash
# Test from the host
curl -w "Time: %{time_total}s\n" https://api.provider.com/v1/models
```

If the curl takes > 30s, the provider is slow. If it returns instantly but
PELDRUN times out, the issue is in PELDRUN's adapter code.

**Fix**:

For slow models (e.g., reasoning models that think for minutes):

```bash
# In the backend environment
export PELDRUN_LLM_FIRST_CHUNK_TIMEOUT=180
export PELDRUN_LLM_INTER_CHUNK_TIMEOUT=300
```

Restart PELDRUN for the env vars to take effect.

---

### unavailable (5xx)

**Message**: `HTTP 500`, `HTTP 502`, `HTTP 503`, `HTTP 504`

**Cause**: The provider's server is broken or unavailable.

**Diagnosis**:

| Status | Meaning |
|--------|---------|
| 500 | Provider's internal error |
| 502 | Bad gateway (proxy issue) |
| 503 | Service unavailable (maintenance, overload) |
| 504 | Gateway timeout (upstream slow) |

**Fix**:
1. **Wait** — retryable, auto-retry after backoff
2. **Check provider status** — open their status page
3. **Try a different provider** — if the outage is extended

**If 5xx persists for > 1 hour**: The provider is likely having an outage.
Switch providers temporarily.

---

### context_length_exceeded

**Message**: `HTTP 400 — Context length exceeded` or `prompt is too long`

**Cause**: The request (messages + tools + system prompt) exceeds the model's
context window.

**Diagnosis**:

The error usually contains:
- `maximum context length: N tokens`
- `request had M tokens`

**Fix**:

1. **Reduce input**:
   - Shorten the system prompt
   - Remove old chat turns
   - Clear tool definitions if not needed

2. **Use a larger context model**:
   - `claude-3-5-sonnet` → `claude-3-opus` (increases from 200K to 200K, same)
   - `gpt-4-turbo` → `gpt-4o` (increases from 128K to 128K, same)
   - `gemini-1.5-pro` (1M context)

3. **Adjust context budget**:
   ```bash
   # In Model Budgets tab
   # Increase context_window for this model
   ```

4. **Enable sliding window**:
   PELDRUN's `tokenizer.py` already implements sliding-window truncation for
   chat. If it's not kicking in, the model's `max_context_length` metadata
   might be missing. Set it manually in Model Budgets.

---

### unsupported_feature

**Message**: `HTTP 400 — Unsupported parameter: X` or `This model does not support X`

**Cause**: The adapter sent a parameter the model or API does not accept.

**Common cases**:

| Parameter | Cause | Fix |
|-----------|-------|-----|
| `reasoning_effort` | Model does not support reasoning | Disable reasoning in model settings |
| `tools` | Model does not support tool calling | Disable tools for this provider |
| `temperature` | Reasoning models reject it | Already handled by `RequestCompiler` |
| `max_tokens` | Some APIs use `max_output_tokens` | Adapter maps it |

**Diagnosis**: The error names the unsupported parameter. Note it.

**Fix**:

1. **If the capability is truly unsupported**: Disable it in the model's
   connection or feature settings. The card's Details drawer has the toggles.

2. **If the model actually supports it but the adapter is wrong**:
   File a bug against the package. The adapter should translate the parameter
   correctly.

3. **If you enabled it via manual override**: The capability resolver should
   have caught this. If it didn't, the manifest's `features` block might be
   missing the feature declaration. Add it:
   ```json
   "features": {
     "tool_calling": true   // ← add this
   }
   ```

---

## Streaming issues

### Stream hangs after tokens appear

**Symptom**: Tokens arrive, then streaming stops. The chat shows "running"
but no new content.

**Cause**: The adapter did not yield a `MESSAGE_COMPLETE` event. The supervisor
is waiting for it.

**Diagnosis**:

Backend logs show:
```
Stream for provider 'x' stalled — no events for 300s
```

**Fix**: The adapter's `stream()` method must always yield `MESSAGE_COMPLETE`.
Common cause:

```python
# WRONG
async def stream(self, request):
    async for chunk in sdk.stream():
        yield LLMStreamEvent(...)
    # ← no MESSAGE_COMPLETE

# RIGHT
async def stream(self, request):
    try:
        async for chunk in sdk.stream():
            yield LLMStreamEvent(...)
    finally:
        yield LLMStreamEvent(
            type=StreamEventType.MESSAGE_COMPLETE,
            finish_reason="stop",
        )
```

**Fix**: File a bug against the package, or reinstall a newer version.

**Temporary workaround**: The stall timeout (300s) will fire and cancel the
stream with a clear error.

---

### Stream never starts

**Symptom**: Chat shows "running" but no tokens arrive.

**Cause**: One of:

1. Worker is not started
2. `STREAM_START` did not reach the worker
3. Adapter's `stream()` is blocking (sync SDK)

**Diagnosis**:

Backend logs show:
```
Worker for provider 'x' is ready (pid=...)
[Bridge] Streaming call failed
```

If no "Worker ready" line appears, the worker never started.

If "Worker ready" appears but no streaming, the adapter is blocking.

**Fix**:

| Cause | Fix |
|-------|-----|
| Worker not ready | Check startup timeout errors |
| Adapter blocks | Convert sync SDK calls to `asyncio.to_thread` |
| Network timeout | Check provider status |

**Diagnosis command**:

```bash
# Check if a worker is running for this provider
ps aux | grep "peldrun.llm.plugin_host.worker"

# On Windows:
tasklist | findstr python
```

If no worker is running, the issue is upstream (dispatcher or supervisor).

---

### Stream stops mid-response

**Symptom**: Tokens arrive, then stop. The chat shows partial content.

**Cause**: One of:

1. Provider closed the stream (network error, server crash)
2. Adapter raised an exception
3. Stream timeout (inter-chunk > 120s)

**Diagnosis**:

Backend logs show:
```
[LLM CLIENT] Stream interrupted for model 'x'
Stream for provider 'y' stopped responding after N chunk(s)
```

**Fix**:

- **Provider issue**: Retry, or switch providers
- **Stream timeout**: If the model thinks for > 120s between chunks (rare,
  but possible with reasoning models), increase `STREAM_INTER_CHUNK_TIMEOUT`
- **Adapter bug**: File a bug against the package

---

### Stream cancelled unexpectedly

**Symptom**: Chat shows "cancelled" but the user did not click Stop.

**Cause**: One of:

1. Chat was closed
2. Another cancel request arrived
3. Worker crashed and cancelled all streams

**Diagnosis**:

Backend logs show:
```
[LLM CLIENT] Stream cancelled for model 'x'
Cancelled by operator
```

Check who triggered the cancel.

**Fix**:

- **User closed chat**: Not a bug — expected behavior
- **Worker crashed**: Check worker logs for crash reason
- **Multiple cancels**: If a user clicked Stop twice, the second is ignored

**If cancellation happens spuriously**: File a bug with the exact sequence of
user actions.

---

## Connection issues

### Configure button does nothing

**Symptom**: Clicking "Configure" on a package card does not open the modal.

**Cause**: JavaScript error, or the modal component is not mounted.

**Diagnosis**:

Open browser DevTools → Console. Look for errors.

**Common errors**:

```
Cannot read properties of undefined (reading 'connection_fields')
```

This means the package entry does not have the `connection_fields` field.
**Fix**: Reinstall the package (the field was added in a schema update).

```
ProviderConfigureModal is not defined
```

This means the component is not imported. **Fix**: Reload the page (dev
server should rebuild automatically). If it persists, rebuild the frontend:

```bash
cd frontend
npm run build
```

---

### Not configured badge won't go away

**Symptom**: The card shows "Not configured" even though a connection exists.

**Cause**: The connection exists but is not **active** or not **enabled**.

**Diagnosis**:

```bash
curl http://localhost:8088/api/store/providers/YOUR_ID | jq '{
  has_active_connection,
  active_connection_name,
  connections_count
}'
```

Expected:
```json
{
  "has_active_connection": true,
  "active_connection_name": "Personal",
  "connections_count": 1
}
```

If `has_active_connection: false` while `connections_count > 0`, the connection
exists but is not active.

**Fix**:
1. Open Configure
2. Verify the connection is enabled (green toggle)
3. Click **Activate** on the connection
4. Reload the page

---

### Test fails but chat works

**Symptom**: Test returns `ok: false`, but sending a chat message works.

**Cause**: The test endpoint uses a cheap probe (`GET /models`), but the
model endpoint may be more permissive.

**Common cases**:

| Provider | Cause |
|----------|-------|
| Anthropic | `/models` may be unavailable on older keys |
| Some proxies | `/models` not implemented |
| Providers with model-specific auth | Auth applies only to `/chat` |

**Fix**: Ignore the test failure. If chat works, the connection is fine.
The test is a best-effort probe, not a full verification.

**Note**: Some providers require `models.list` scope in addition to
`chat.completions`. If your key only has chat scope, test will fail but
chat works.

---

### Masked value error

**Symptom**: Chat fails with an auth error. The error contains `"••••••••"` in
place of the real credential.

**Cause**: The user's connection has the mask placeholder as the actual value.

**How it happens**:
- The user edited the form and the form submitted the mask
- The backend overwrote the real value with the mask
- The adapter received the mask and sent it to the provider → 401

**Fix** (unrecoverable):

1. Delete the connection:
   ```bash
   curl -X DELETE http://localhost:8088/api/store/providers/YOUR_ID/connections/CONN_ID
   ```
2. Create a new connection:
   ```bash
   curl -X POST http://localhost:8088/api/store/providers/YOUR_ID/connections \
     -H "Content-Type: application/json" \
     -d '{
       "name": "Personal",
       "fields": {"credentials": "real-value-here", ...},
       "is_active": true
     }'
   ```

**Prevention**:
- The form tracks diffs. If you edit a field, it submits the change.
- Do not edit the secret field if you do not intend to change it.
- If you accidentally edit it and change your mind, click Cancel — do not
  save.

---

### No active connection fallback

**Symptom**: Chat uses a different provider than expected. Backend logs show:

```
Package 'gigachat' is registered but has NO active connection.
Falling back to openai_compat for this request.
```

**Cause**: The package has no active connection (either not created, not
activated, or active-but-disabled).

**Diagnosis**:

```bash
curl http://localhost:8088/api/store/providers/YOUR_ID | jq '.has_active_connection'
```

Expected: `true`. If `false`, the fallback is active.

**Fix**:

1. **Create a connection** if none exists
2. **Activate a connection** if one exists but is inactive
3. **Enable a connection** if it is active-but-disabled

**Check both flags**:

```bash
curl http://localhost:8088/api/store/providers/YOUR_ID/connections | jq '.connections[] | {name, enabled, is_active}'
```

Both `enabled` and `is_active` must be `true`.

---

## Health check failures

The health check response has a `status` and a list of `checks`. Each check is
either passed or failed.

### active_pointer failed

**Message**: `pointer=<version> registry=<version>`

**Cause**: The `active.json` pointer disagrees with `registry.json`.

**Diagnosis**:

```bash
cat <storage>/store/providers/YOUR_ID/active.json
```

Compare with:

```bash
cat <storage>/store/providers/registry.json | jq '.packages.YOUR_ID.active_version'
```

**Fix**:

If they disagree, `active.json` is stale. Update it:

```bash
# Option 1: Activate the current version via API
curl -X POST http://localhost:8088/api/store/providers/YOUR_ID/activate \
  -H "Content-Type: application/json" \
  -d '{"version": "CURRENT_VERSION"}'

# Option 2: Manually fix active.json (careful!)
cat > <storage>/store/providers/YOUR_ID/active.json <<EOF
{
  "provider_id": "YOUR_ID",
  "version": "CURRENT_VERSION",
  "hash": "CURRENT_HASH",
  "activated_at": "2025-01-01T00:00:00Z"
}
EOF
```

---

### version_directory failed

**Message**: `<path>`

**Cause**: The version directory does not exist on disk.

**Fix**:
1. Check the path in the message
2. Verify the directory exists: `ls <path>`
3. If missing, uninstall and reinstall the package

```bash
curl -X DELETE http://localhost:8088/api/store/providers/YOUR_ID
curl -X POST http://localhost:8088/api/store/providers/upload -F "file=@YOUR_PACKAGE.zip"
```

---

### manifest_present failed

**Message**: `missing or corrupt`

**Cause**: The `manifest.json` file is missing or invalid.

**Diagnosis**:

```bash
cat <storage>/store/providers/YOUR_ID/versions/VERSION/manifest.json | jq .
```

If this fails, the file is corrupt.

**Fix**: Uninstall and reinstall the package.

---

### entry_point_present failed

**Message**: `entry_point module file is missing`

**Cause**: The `entry_point` in the manifest points to a file that does not
exist in the version directory.

**Diagnosis**:

```bash
# Read the entry_point
cat <storage>/store/providers/YOUR_ID/versions/VERSION/manifest.json | jq '.entry_point'

# Check if the file exists
ls <storage>/store/providers/YOUR_ID/versions/VERSION/<module_path>.py
```

**Fix**: Uninstall and reinstall the package. If the file is genuinely missing
from the ZIP, the package is broken — report a bug.

---

### venv_present failed

**Message**: `missing`

**Cause**: The virtual environment for the active version is missing.

**Diagnosis**:

```bash
ls <storage>/store/providers/YOUR_ID/environments/VERSION/bin/python
```

If missing, the venv was deleted (manually or by a cleanup script).

**Fix**: Uninstall and reinstall. The package will re-create the venv.

**Note**: This check only runs for `native` / `custom` packages. Compatible
packages skip it (they have no venv).

---

## Performance issues

### First request is very slow

**Symptom**: The first chat after installing a native package takes 30+ seconds.

**Cause**: This is normal — the worker subprocess is spawned on the first
request. Cold import of heavy SDKs (boto3, google-cloud) can take 10-30s.

**Diagnosis**:

Backend logs show:
```
Worker for provider 'x' is ready (pid=...) in 25000ms
```

**Fix**:
- **Accept the cost**: Only the first request is slow. Subsequent requests
  reuse the worker.
- **Warm the worker**: Trigger a warm-up request after install:
  ```bash
  curl -X POST http://localhost:8088/api/store/providers/YOUR_ID/connections/CONN_ID/test
  ```
- **Reduce SDK imports**: If you wrote the adapter, import SDK modules lazily
  inside `generate()` rather than at module top.

---

### Every request is slow

**Symptom**: Every chat message takes > 30s, even after the first one.

**Cause**: One of:

1. **Large context**: Long messages = more tokens to send + more to process
2. **Slow model**: Some models genuinely take 30s+ per response
3. **Network latency**: The provider is far away
4. **Slow SDK**: The adapter uses a slow client (sync SDK on event loop)

**Diagnosis**:

Check the backend logs for `latency_ms` in the response. Compare with:

```bash
time curl https://api.provider.com/v1/models
```

If the provider's API is fast but PELDRUN is slow, the adapter is the issue.

**Fix**:

| Cause | Fix |
|-------|-----|
| Large context | Reduce message length or clear history |
| Slow model | Use a faster model variant |
| Network latency | Deploy PELDRUN closer to the provider |
| Slow SDK | Report a bug against the package |

---

### Streaming is slower than expected

**Symptom**: Tokens arrive, but slowly. The response takes 2-3× longer than
the same request in the provider's web UI.

**Cause**: One of:

1. **IPC overhead**: Every token passes through JSON serialization
2. **Slow consumer**: The frontend renders slowly
3. **Buffered streaming**: The adapter falls back to buffered mode

**Diagnosis**:

Check if native streaming is used:

```bash
curl http://localhost:8088/api/store/providers/YOUR_ID | jq '.features.native_streaming'
```

If `false`, the adapter is buffered — tokens arrive in batches, not
individually.

**Fix**:

| Cause | Fix |
|-------|-----|
| IPC overhead | Normal for native packages (compatible is faster) |
| Slow consumer | Check browser performance |
| Buffered streaming | The adapter is not declaring `native_streaming: true`, or the SDK does not support streaming |

**If you wrote the adapter**: Verify `stream()` yields `CONTENT_DELTA` events
as the SDK produces them, not at the end.

---

## Where to find logs

### Backend logs

**If using `start-backend`**:
```bash
tail -f logs/backend.log | grep -i "provider\|plugin_host"
```

**If using `uvicorn` directly**: Logs go to stdout.

**Log levels**: The default is `INFO`. For more detail:

```bash
# In the backend environment
export PELDRUN_LOG_LEVEL=DEBUG
export LOG_LEVEL=DEBUG
```

Restart PELDRUN.

### Worker logs

The worker subprocess inherits `stderr` from the main process. Its output
appears in the same logs.

Look for:
```
[worker gigachat] ...
```

### Frontend logs

Browser DevTools → Console. Look for:
- `[useProviderActions] ...`
- `[useStoreData] ...`
- Network errors (in the Network tab)

### Storage inspection

All state is in JSON files:

```bash
# Registry (all installed packages)
cat <storage>/store/providers/registry.json | jq .

# Active pointer
cat <storage>/store/providers/YOUR_ID/active.json | jq .

# Connections
cat <storage>/data/models_vault.json | jq '.provider_connections'
```

### Diagnostics endpoint

```bash
curl http://localhost:8088/api/system/info
```

Returns backend version, Python version, system info.

---

## Getting help

### Before asking

1. **Search the error message** in this document
2. **Check backend logs** for the full traceback
3. **Reproduce with the API** — curl commands give reproducible results
4. **Note what you tried** — even if it failed

### Where to ask

| Channel | Use for |
|---------|---------|
| **GitHub Issues** | Bugs, feature requests |
| **GitHub Discussions** | "How do I...?" questions |
| **Discord / Slack** (if available) | Quick questions |

### What to include

When reporting a bug, include:

1. **Package details**:
   ```bash
   curl http://localhost:8088/api/store/providers/YOUR_ID | jq .
   ```

2. **Health check**:
   ```bash
   curl http://localhost:8088/api/store/providers/YOUR_ID/health | jq .
   ```

3. **Backend version**:
   ```bash
   curl http://localhost:8088/api/system/info | jq .
   ```

4. **The exact error**:
   - Full error message
   - Full stack trace from logs
   - What you were doing when it happened

5. **Minimal reproduction**:
   - The smallest `manifest.json` that triggers the issue
   - The exact curl command
   - The exact response

6. **What you already tried**:
   - Steps taken
   - Results of each attempt

### Template for bug reports

```markdown
## Summary
[One sentence]

## Environment
- PELDRUN version: `curl http://localhost:8088/api/system/info | jq .version`
- Python version: `python --version`
- OS: [Linux / macOS / Windows + version]
- Package: [id and version]

## Steps to reproduce
1. [Step]
2. [Step]
3. [Step]

## Expected behavior
[What should happen]

## Actual behavior
[What actually happens]

## Error message
[Paste the exact error]

## Logs
[Paste the relevant backend logs]
```

### Getting the backend logs

```bash
# Last 100 lines
tail -100 logs/backend.log

# Filter for the error
grep -A 20 "Provider package" logs/backend.log

# Full traceback
grep -B 2 -A 30 "Traceback" logs/backend.log | tail -50
```

### If it's a package bug

The bug might be in the package author's adapter, not PELDRUN. In that case:

1. **Contact the package author** — via the package's homepage / GitHub
2. **Include the package version** so they can reproduce
3. **Do not file it against PELDRUN** unless the issue is clearly in the
   framework (manifest validation, IPC, permissions)

**How to tell**:

| Error | Where it belongs |
|-------|------------------|
| Install fails | PELDRUN (validation, extraction) |
| Adapter load fails | Package author (import, constructor) |
| Request fails with 401 | User (credentials) or package (wrong header) |
| Request fails with 500 | Provider (server) or package (adapter bug) |
| Streaming hangs | Package author (missing MESSAGE_COMPLETE) |
| Health check fails | PELDRUN (filesystem) or package (missing file) |

---

## Quick reference — error code → first action

| Code | First action |
|------|-------------|
| `PROVIDER_PACKAGE_WRONG_FILE_TYPE` | Rename to `.zip` |
| `PROVIDER_PACKAGE_EMPTY_UPLOAD` | Re-download the ZIP |
| `PROVIDER_PACKAGE_INVALID` | `unzip -l` and check contents |
| `PROVIDER_PACKAGE_SECURITY_VIOLATION` | `unzip -l \| grep '\.\.'` and check for symlinks |
| `PROVIDER_PACKAGE_MANIFEST_INVALID` | Read `manifest.<field>` error, fix field |
| `PROVIDER_PACKAGE_CONFLICT` | Change `id` or bump `version` |
| `PROVIDER_PACKAGE_ENVIRONMENT_ERROR` | Read `pip_log_tail`, test `pip install` locally |
| `PROVIDER_PACKAGE_STORAGE_ERROR` | Check disk space and permissions |
| `PROVIDER_PACKAGE_NOT_FOUND` | Reinstall the package |
| `PROVIDER_CONNECTION_NOT_FOUND` | Refresh the Configure modal |
| `auth_failed` | Test the connection, re-enter credentials |
| `rate_limited` | Wait and retry |
| `quota_exceeded` | Add credits or switch provider |
| `timeout` | Check provider status, increase timeout |
| `unavailable` | Retry, check provider status page |
| `context_length_exceeded` | Shorten messages or use a larger model |
| `unsupported_feature` | Disable the feature in model settings |
| `plugin_url_not_allowed` | The adapter is trying to reach a URL outside `base_url` |
| `plugin_permission_denied` | The adapter is trying to use a permission it did not declare |

---

## What's next

- **Need to build a package?** → [Quickstart — Compatible](02-quickstart-compatible.md) or [Quickstart — Native](03-quickstart-native.md)
- **Need the manifest reference?** → [Manifest Reference](04-manifest-reference.md)
- **Need the adapter contract?** → [Adapter Protocol](06-adapter-protocol.md)
- **Need to understand the API?** → [API Reference](11-api-reference.md)
- **Need security context?** → [Security Model](12-security-model.md)
- **Need working examples?** → [Examples](13-examples.md)
```

---
