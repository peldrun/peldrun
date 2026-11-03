### `docs/providers/09-installation-flow.md`

```markdown
<!-- docs/providers/09-installation-flow.md -->
# Installation Flow

> **Complete reference** for what happens when a user installs a Provider
> Package.
>
> **Reading time**: 15-18 minutes.
>
> **Who this is for**: Anyone debugging an install, or who wants to
> understand what the safety checks actually do.
>
> **What you'll learn**: The six-step install pipeline, what each step
> validates, how failures are handled, and how to recover from a broken
> install.

---

## Table of Contents

- [Overview](#overview)
- [The six-step pipeline](#the-six-step-pipeline)
- [Step 1 — Validate](#step-1--validate)
- [Step 2 — Conflict check](#step-2--conflict-check)
- [Step 3 — Extract](#step-3--extract)
- [Step 4 — Promote](#step-4--promote)
- [Step 5 — Environment](#step-5--environment)
- [Step 6 — Register](#step-6--register)
- [Rollback behavior](#rollback-behavior)
- [Time estimates](#time-estimates)
- [Common failures](#common-failures)
- [Verifying an install](#verifying-an-install)
- [Managing installed packages](#managing-installed-packages)
- [Storage layout](#storage-layout)

---

## Overview

A Provider Package install is a **6-step pipeline**. Every step is atomic:
either it fully succeeds, or the install aborts and the state is rolled back
to its pre-install condition.

### The two paths

| Transport | Path | Time |
|-----------|------|------|
| **`compatible`** | 1 → 2 → 3 → 4 → **skip 5** → 6 | ~2 seconds |
| **`native` / `custom`** | 1 → 2 → 3 → 4 → 5 → 6 | ~10-60 seconds (pip install dominates) |

**The only difference**: `compatible` packages ship no code, so no venv is
created (step 5 is skipped). Everything else is identical.

### Where it runs

The install pipeline runs **in the main backend process**, not in a worker.
The upload endpoint calls `provider_package_manager.install_bundle()`, which
runs the pipeline synchronously (with `async` wrappers around the pip
subprocess).

### What gets called

```
POST /api/store/providers/upload
  ↓
routers/store/providers.py: upload_provider_package()
  ↓
provider_package_manager.install_bundle(zip_bytes, ...)
  ↓
  ├── validate_zip()                    [Step 1]
  ├── _check_conflicts()                [Step 2]
  ├── safe_extract()                    [Step 3]
  ├── os.replace(staging, version_dir)  [Step 4]
  ├── DependencyInstaller.create_environment()  [Step 5]
  └── registry.install_version()        [Step 6]
```

### The result

On success, `install_bundle()` returns a `ProviderPackageInstallResult`:

```python
ProviderPackageInstallResult(
    id="gigachat",
    version="1.0.0",
    hash="a1b2c3...",
    name="GigaChat (Sber)",
    display_name="GigaChat",
    description="...",
    size_bytes=10550,
    file_count=4,
    transport="native",
    features={...},
    permissions={...},
    install_report=ProviderPackageInstallReport(
        dependencies_installed=True,
        dependencies_skipped=False,
        venv_path="/path/to/storage/store/providers/gigachat/environments/1.0.0",
        duration_ms=42350,
        steps=[
            ProviderPackageStepReport(name="validate", status="ok", duration_ms=15),
            ProviderPackageStepReport(name="conflict_check", status="ok", duration_ms=2),
            ProviderPackageStepReport(name="extract", status="ok", duration_ms=8),
            ProviderPackageStepReport(name="promote", status="ok", duration_ms=3),
            ProviderPackageStepReport(name="environment", status="ok", duration_ms=41980),
            ProviderPackageStepReport(name="register", status="ok", duration_ms=5),
        ],
        warnings=[],
    ),
    warnings=[],
)
```

The `install_report.steps` field shows the timing of each step — useful for
diagnosing slow installs.

---

## The six-step pipeline

| # | Step | Purpose | Failure action |
|---|------|---------|----------------|
| 1 | Validate | Check ZIP safety and manifest validity | Abort, no state change |
| 2 | Conflict check | Reject builtin IDs, idempotent re-installs, hash mismatches | Abort, no state change |
| 3 | Extract | Unzip into a staging directory | Abort, clean staging |
| 4 | Promote | Atomically move staging to `versions/<v>/` | Abort, clean staging + version dir |
| 5 | Environment | Create venv + pip install (native only) | Abort, clean version dir + venv |
| 6 | Register | Persist to `registry.json` + `active.json` | Abort, clean version dir + venv |

**Key invariant**: Steps 1-3 do not modify the final install location. Only
step 4 onward touches `versions/<v>/`. This means a failure at any of steps
1-3 leaves no trace.

**Atomic commit point**: Step 6 is the commit. Before step 6, no state is
persisted. After step 6, the package is fully installed.

---

## Step 1 — Validate

### What it does

`validate_zip(zip_bytes)` performs every safety check on the raw uploaded
ZIP **before any file is written to disk**.

### Checks performed

**Size limits**:
- Compressed size ≤ 100 MB (`MAX_PACKAGE_SIZE_BYTES`)
- Uncompressed size ≤ 500 MB (`MAX_UNCOMPRESSED_BYTES`)
- Compression ratio ≤ 100:1 (`MAX_COMPRESSION_RATIO`) — archive bomb defense

**File count**: ≤ 2000 files (`MAX_FILE_COUNT`)

**Path safety** (for every file in the ZIP):
- No `..` traversal
- No absolute paths (Unix or Windows)
- No backslashes
- No NUL bytes
- Path length ≤ 1024 characters (`MAX_PATH_LENGTH`)

**Symlink rejection**: Every entry is checked for `S_IFLNK` in its Unix mode.
Symlinks are unconditionally rejected.

**Manifest presence**: The ZIP must contain `manifest.json` at the root, or
inside a single top-level folder.

**Manifest Pydantic validation**: The manifest is parsed as
`ProviderManifest`. Every validator runs (semver, id pattern, transport
consistency, etc.).

**Entry-point file presence** (native/custom only): The module path in
`entry_point` must resolve to a real `.py` file in the ZIP.

### Result

A `ValidationReport`:

```python
ValidationReport(
    ok=True,
    errors=[],
    warnings=["manifest.json was located inside 'gigachat/'; contents will be flattened during extraction."],
    file_count=4,
    compressed_size=10550,
    uncompressed_size=22000,
    sha256="a1b2c3d4...",
    manifest=ProviderManifest(...),
    manifest_path_in_zip="gigachat/manifest.json",
    strip_prefix="gigachat/",
    entry_point_file="gigachat/gigachat_peldrun/adapter.py",
)
```

### Failure handling

If `validation.ok == False`, the install aborts. No files are written. The
exact errors are returned to the caller:

```
POST /api/store/providers/upload → 400 Bad Request
{
  "detail": {
    "error": "Unsafe path '../etc/passwd': path contains '..' traversal.",
    "code": "PROVIDER_PACKAGE_SECURITY_VIOLATION",
    "details": {
      "errors": ["Unsafe path '../etc/passwd': path contains '..' traversal."]
    }
  }
}
```

The error class depends on the failure type:

| Failure | Exception | HTTP status |
|---------|-----------|-------------|
| Path safety violation | `ProviderPackageSecurityError` | 400 |
| Archive bomb | `ProviderPackageSecurityError` | 400 |
| Manifest not found | `ProviderPackageManifestError` | 400 |
| Manifest validation error | `ProviderPackageManifestError` | 400 |
| Entry point missing | `ProviderPackageManifestError` | 400 |
| Size limit exceeded | `ProviderPackageValidationError` | 400 |

### Common issues at this step

- **Manifest inside two folders** — The validator supports root manifest or a
  single-folder manifest, not 2-level nesting
- **Multiple manifests** — Ambiguity attack prevention
- **Entry point does not exist** — e.g., `gigachat_peldrun.adapter` but the
  file is at `gigachat_peldrun/core/adapter.py`
- **Wrong version** — `"latest"` is rejected; must be semver

---

## Step 2 — Conflict check

### What it does

`_check_conflicts(manifest, sha256)` detects collisions with existing
packages **before any filesystem operation**. Three checks:

### Check A — Builtin ID collision

The manifest's `id` must not match a built-in adapter's identifier:

- `openai_compat`
- `lmstudio`
- `ollama`
- `openai_native`
- `anthropic`
- `google_genai`
- `ollama_native`
- `lmstudio_native`

If it does, the install aborts with `ProviderPackageConflictError`:

```
Provider ID 'anthropic' is already used by a built-in adapter.
Provider Packages cannot overwrite built-in providers. Use a different
'id' in manifest.json.
```

**Why this matters**: A package that tried to install as `"anthropic"` would
override the built-in Anthropic adapter, breaking every existing connection.
The check prevents this accidentally and maliciously.

### Check B — Idempotent re-install

If the same version is already installed AND the SHA-256 matches, the install
is a **no-op**. The existing entry is returned unchanged.

```
Re-installing "gigachat" v1.0.0 with identical content — skipping install.
```

**Behavior**:
- No filesystem changes
- No pip install
- No registry update
- The API returns the existing entry with `warnings: ["This version was already installed with identical content. No files were modified."]`

**Why this matters**: A user who accidentally double-clicks "Install" does not
re-run a 40-second pip install.

### Check C — Hash mismatch

If the same version is already installed BUT the SHA-256 differs, the install
aborts with `ProviderPackageConflictError`:

```
Version '1.0.0' of provider 'gigachat' is already installed with
different content (installed hash: a1b2c3..., new hash: d4e5f6...).
Bump the 'version' in manifest.json, or uninstall the existing package
first.
```

**Why this matters**: Two different ZIPs with the same version would
silently overwrite each other, causing chaos. The user must explicitly choose:
- Bump the version to install alongside
- Uninstall the existing version to replace it

### Result

Returns `None` if the install should proceed, or the existing entry if it is
an idempotent re-install.

### What triggers this check for compatible packages

For `compatible` packages, there is no ZIP. The manager generates a synthetic
manifest dict from the catalog item, then computes a **canonical hash** from
the manifest content (via `_hash_manifest_canonical`). This gives the same
idempotency guarantee:

- Two identical catalog items → same hash → idempotent re-install
- Same catalog item with different features → different hash → conflict

---

## Step 3 — Extract

### What it does

`safe_extract(zip_bytes, staging_dir)` unzips the archive into a temporary
staging directory.

### Where it writes

A unique staging directory under `<storage>/store/providers/.staging/`:

```
<storage>/store/providers/
└── .staging/
    └── pkg_gigachat_a1b2c3d4_<uuid>/
        ├── manifest.json
        ├── requirements.txt
        └── gigachat_peldrun/
            ├── __init__.py
            └── adapter.py
```

The staging directory is on the **same filesystem** as the final location, so
the next step can use `os.replace` (atomic rename) instead of a copy.

### Path safety — defense in depth

Even though `validate_zip` already checked every path, `safe_extract`
**re-checks** every entry immediately before extraction. This is the
defense-in-depth against TOCTOU:

- If a malicious process swapped the ZIP between validation and extraction
- If a filesystem bug allowed a path to change
- If the validation logic itself had a bug

...the extraction refuses to write outside the staging directory.

For each entry, it:
1. Re-validates the path (no `..`, no absolutes, no backslashes)
2. Re-validates it is not a symlink
3. Resolves the target path
4. Asserts the resolved target is a descendant of the staging root
5. Writes the file in 64KB chunks

### Handling the strip prefix

If the manifest was inside a single top-level folder (e.g.,
`gigachat/manifest.json`), the validator recorded `strip_prefix="gigachat/"`.
The extraction strips that prefix from every entry, flattening the structure:

```
ZIP structure:                    Staging structure:
gigachat/                         manifest.json
├── manifest.json                 requirements.txt
├── requirements.txt              gigachat_peldrun/
└── gigachat_peldrun/             ├── __init__.py
    ├── __init__.py               └── adapter.py
    └── adapter.py
```

### Failure handling

If extraction fails (I/O error, disk full, permission denied), the staging
directory is deleted and the install aborts with `ProviderPackageStorageError`:

```
{
  "detail": {
    "error": "Extraction failed: ...",
    "code": "PROVIDER_PACKAGE_STORAGE_ERROR",
    "details": {
      "failed_paths": ["gigachat_peldrun/adapter.py"],
      "errors": ["Permission denied"]
    }
  }
}
```

### Junk stripping

After extraction, the manager removes archiver metadata that should never
reach the final directory:

- `__MACOSX/` — macOS archiver metadata
- `.DS_Store` — macOS Finder cache
- `Thumbs.db` — Windows thumbnail cache
- `.Spotlight-V100/` — macOS Spotlight index
- `.fseventsd/` — macOS file events

This ensures the version directory contains only the package author's files.

---

## Step 4 — Promote

### What it does

Atomically moves the staging directory to its final location:
`<storage>/store/providers/<id>/versions/<version>/`.

### The atomic move

```python
os.replace(str(staging_dir), str(version_dir))
```

**Why `os.replace` is atomic**:
- Same filesystem → single `rename()` syscall → atomic
- Across filesystems → `copy + rename` → not atomic

Since `validate_zip` and `safe_extract` both write to
`<storage>/store/providers/.staging/`, and the final location is
`<storage>/store/providers/<id>/versions/<version>/`, both are under the same
storage root. Same filesystem. Atomic.

### Preconditions

Before `os.replace`, the manager checks:

1. **Orphaned version directory** — if `versions/<v>/` exists but the registry
   does not know about it, it is left over from a crashed previous install.
   The manager deletes it before promoting the new one.

2. **Parent exists** — `versions/` is created if not present.

3. **Target does not exist** — `os.replace` on a directory requires the
   target to be absent or empty. The manager ensures neither exists.

### Writing `_peldrun_meta.json`

After the move, the manager writes `_peldrun_meta.json` in the version
directory:

```json
{
  "schema_version": 1,
  "provider_id": "gigachat",
  "version": "1.0.0",
  "sha256": "a1b2c3d4...",
  "installed_at": "2025-01-01T00:00:00Z",
  "transport": "native",
  "api_contract": "peldrun.llm.provider.v1",
  "entry_point": "gigachat_peldrun.adapter:GigaChatAdapter",
  "base_adapter": null,
  "file_count": 4,
  "uncompressed_bytes": 22000
}
```

This file is **PELDRUN-owned** (not from the ZIP) and records the install-time
facts. The plugin host reads it to verify content integrity before launching a
worker.

### Failure handling

If `os.replace` fails (permission error, cross-filesystem link), both the
staging directory and the partial version directory are cleaned up, and the
install aborts.

---

## Step 5 — Environment

### What it does

For `native` / `custom` transports, creates an isolated Python virtual
environment and installs the package's dependencies.

For `compatible` transports, this step is **skipped**.

### What gets created

```
<storage>/store/providers/gigachat/
└── environments/
    └── 1.0.0/
        ├── bin/python                  ← the venv's interpreter
        ├── bin/pip
        ├── lib/python3.12/site-packages/
        │   ├── gigachat/
        │   ├── httpx/
        │   └── pydantic/
        └── pyvenv.cfg
```

### The sequence

1. **Validate Python version** — The manifest's `runtime.python` specifier
   (e.g., `">=3.12,<3.13"`) is validated against the running interpreter.
   Mismatch → `ProviderPackageEnvironmentError`.

2. **Create venv** — `venv.EnvBuilder(system_site_packages=False,
   with_pip=True, upgrade_deps=True)`.

3. **Install dependencies** — In order of preference:
   - `requirements.lock` (hash-pinned, preferred)
   - `requirements.txt`
   - `pyproject.toml` (via `pip install -e .`)
   - `manifest.dependencies` (fallback)

4. **Set timeout** — 10 minutes per `pip` invocation. A slow network or a
   huge dependency tree is tolerated; a wedged pip process is killed.

### Idempotency

If the venv already exists with the interpreter present, the step is
**idempotent** — the existing venv is reused:

```
skipped_reason: "existing_venv_reused"
```

This means:
- A second install of the same version does not re-create the venv (Step 2
  already handled that as an idempotent re-install)
- A rollback to a previous version restores the exact environment that was
  validated at install time

### Failure handling

If venv creation or pip install fails, the version directory and the partial
venv are deleted. The install aborts with `ProviderPackageEnvironmentError`:

```
{
  "detail": {
    "error": "pip install failed (exit 1, label=requirements.txt).",
    "code": "PROVIDER_PACKAGE_ENVIRONMENT_ERROR",
    "details": {
      "label": "requirements.txt",
      "returncode": 1,
      "pip_log_tail": "ERROR: Could not find a version that satisfies the requirement invalid-package (from versions: none)\nERROR: No matching distribution found for invalid-package"
    }
  }
}
```

The `pip_log_tail` contains the last 8KB of pip's output — usually enough to
diagnose the failure.

### Time cost

| Scenario | Time |
|----------|------|
| No dependencies | ~1s (venv creation) |
| 1-2 small packages | ~5-15s |
| `anthropic` SDK (transitive deps) | ~20-40s |
| `boto3` (AWS SDK) | ~30-60s |
| Cold cache, slow network | 60-120s |

This is the only slow step. All others complete in under 100ms.

---

## Step 6 — Register

### What it does

Persists the package to the registry (`registry.json` and `active.json`).

### The registry update

```python
registry.install_version(
    provider_id="gigachat",
    version_record=ProviderPackageVersionRecord(
        version="1.0.0",
        hash="a1b2c3d4...",
        installed_at="2025-01-01T00:00:00Z",
        size_bytes=10550,
        file_count=4,
        entry_point="gigachat_peldrun.adapter:GigaChatAdapter",
        class_name="GigaChatAdapter",
    ),
    name="GigaChat (Sber)",
    display_name="GigaChat",
    description="...",
    transport="native",
    features={...},
    permissions={...},
    connection_fields=[...],
    runtime_hints={...},
    author="PELDRUN Community",
    author_url="https://github.com/peldrun",
    homepage="https://developers.sber.ru",
    source="uploaded",
    set_as_active=True,
)
```

### What gets written

**`registry.json`**:
```json
{
  "schema_version": 1,
  "updated_at": "2025-01-01T00:00:00Z",
  "packages": {
    "gigachat": {
      "id": "gigachat",
      "name": "GigaChat (Sber)",
      "display_name": "GigaChat",
      "active_version": "1.0.0",
      "active_hash": "a1b2c3d4...",
      "enabled": true,
      "installed_versions": [
        {
          "version": "1.0.0",
          "hash": "a1b2c3d4...",
          "installed_at": "2025-01-01T00:00:00Z",
          "size_bytes": 10550,
          "file_count": 4,
          "entry_point": "gigachat_peldrun.adapter:GigaChatAdapter",
          "class_name": "GigaChatAdapter"
        }
      ],
      "transport": "native",
      "api_contract": "peldrun.llm.provider.v1",
      "features": {...},
      "permissions": {...},
      "connection_fields": [...],
      "runtime_hints": {...},
      "source": "uploaded",
      "author": "PELDRUN Community",
      "author_url": "https://github.com/peldrun",
      "homepage": "https://developers.sber.ru"
    }
  }
}
```

**`gigachat/active.json`**:
```json
{
  "provider_id": "gigachat",
  "version": "1.0.0",
  "hash": "a1b2c3d4...",
  "activated_at": "2025-01-01T00:00:00Z"
}
```

### Atomic writes

Both files are written via `tmp_file → os.replace`. A crash mid-write leaves
the previous file untouched.

### The commit point

Step 6 is the commit. Before step 6:
- Files exist in `versions/<v>/`
- Venv exists in `environments/<v>/`
- But the registry does not know about them

If the process crashes between step 5 and step 6, the files are orphaned.
The next install of the same package deletes them and re-creates.

After step 6, the package is fully installed. The registry is the source of
truth — a package is "installed" if and only if it appears in
`registry.json`.

---

## Rollback behavior

Here is what happens on failure at each step.

### Failure at step 1 (validate)

**State**: Nothing changed.
**Cleanup**: None needed.

### Failure at step 2 (conflict check)

**State**: Nothing changed.
**Cleanup**: None needed.

### Failure at step 3 (extract)

**State**: Partial staging directory may exist.
**Cleanup**: `staging_dir` is deleted via `shutil.rmtree`.

### Failure at step 4 (promote)

**State**: Version directory may be partially moved or `_peldrun_meta.json`
may be missing.
**Cleanup**: `version_dir` and `staging_dir` are deleted.

### Failure at step 5 (environment)

**State**: Files exist in `versions/<v>/`, venv may be partial.
**Cleanup**: `version_dir` is deleted and any partial venv is deleted.

**Why delete the version directory**: A package without a working venv
cannot function. Leaving it would show in the UI as "installed" but every
request would fail. Better to leave no trace.

### Failure at step 6 (register)

**State**: Files and venv exist, but registry may be partially updated.
**Cleanup**: `version_dir` and venv are deleted. The registry's
`install_version()` method is atomic — a failed write leaves the previous
`registry.json` intact.

### What about the storage after a crash?

If the PELDRUN process is killed mid-install, the following may remain:

- A staging directory in `.staging/` — orphaned, cleaned by the next install
  or by a periodic cleaner
- A version directory with no registry entry — orphaned, cleaned by the next
  install of the same package
- A venv with no version directory — orphaned, cleaned manually if needed

**Orphaned state does not affect the runtime**: The provider registry only
sees what `registry.json` says. Orphaned files are inert.

**Manual cleanup** (if needed):

```bash
# Orphaned staging dirs
rm -rf <storage>/store/providers/.staging/*

# Orphaned version dirs (check registry first!)
ls <storage>/store/providers/*/versions/
# Compare against registry.json to identify orphans
```

---

## Time estimates

| Transport | Package | Time |
|-----------|---------|------|
| compatible | Any (no code) | 2-3 seconds |
| native | No dependencies | 3-5 seconds |
| native | 1 small dependency (`httpx`) | 8-15 seconds |
| native | Anthropic SDK | 20-40 seconds |
| native | AWS SDK (`boto3`) | 30-60 seconds |
| native | Cold cache, slow network | 60-120 seconds |

**Failure to complete within 10 minutes**: The pip subprocess is killed and
the install aborts with `ProviderPackageEnvironmentError`.

---

## Common failures

### "manifest.json is missing"

**Cause**: ZIP does not contain `manifest.json` at the root or inside a
single top-level folder.

**Fix**:
```bash
unzip -l your-package.zip
```
Verify the file is present. If it is inside `your-package/manifest.json`, the
ZIP has a single-folder layout — the validator handles this (flattens it). If
it is inside `your-package/v1/manifest.json`, the layout is ambiguous and is
rejected.

### "Unsafe path '../etc/passwd'"

**Cause**: The ZIP contains a file with a `..` in its path. This is either a
malformed archive or a Zip Slip attack.

**Fix**: Check the archive:
```bash
unzip -l your-package.zip | grep '\.\.'
```
Rebuild the archive if the file is legitimate (the path should be relative,
with no `..`).

### "Archive size exceeds 100 MB"

**Cause**: The ZIP is larger than `MAX_PACKAGE_SIZE_BYTES` (100 MB).

**Fix**: Remove unnecessary files from the package. A Provider Package should
not contain large data files — only code.

### "Compression ratio exceeds 100:1"

**Cause**: The ZIP is highly compressible, which suggests a zip bomb. This is
either a legitimate highly-compressible package (e.g., a large text file) or an
archive bomb attack.

**Fix**: If the package is legitimate, split it into smaller parts or
uncompress the offending file.

### "Provider ID 'anthropic' is already used by a built-in adapter"

**Cause**: You chose an `id` that matches a built-in adapter.

**Fix**: Choose a different `id`. For example, `"acme-anthropic"` or
`"my-anthropic-proxy"`.

### "Version '1.0.0' is already installed with different content"

**Cause**: You uploaded a ZIP with the same version as an existing install,
but the content differs.

**Fix**: Either:
1. Bump the `version` in `manifest.json` to `1.0.1` (installs alongside)
2. Uninstall the existing `1.0.0` first (replaces it)

### "entry_point module 'gigachat_peldrun.adapter' does not resolve"

**Cause**: The `entry_point` field names a module that does not exist in the
ZIP.

**Fix**: Verify the file exists:
```bash
unzip -l your-package.zip | grep adapter
```
Adjust `entry_point` to match the actual file path. The module path uses dots
(`gigachat_peldrun.adapter`), the file path uses slashes
(`gigachat_peldrun/adapter.py`).

### "pip install failed (exit 1)"

**Cause**: One or more dependencies could not be installed.

**Fix**: Check the `pip_log_tail` in the error response. Common causes:
- Typos in `requirements.txt` (e.g., `pydantics` instead of `pydantic`)
- Incompatible version pins (e.g., requiring `httpx==0.20.0` but another
  package needs `httpx>=0.25`)
- Missing package on PyPI

**Test locally first**:
```bash
python -m venv test-venv
source test-venv/bin/activate
pip install -r requirements.txt
```

### "pip install timed out after 600s"

**Cause**: The pip install took longer than 10 minutes. Usually a slow
network or an unreachable PyPI mirror.

**Fix**: Check network connectivity. If you use a private mirror, ensure it is
configured in `pip.conf`.

### "Could not create virtual environment"

**Cause**: The `venv` module is not available in this Python build, or the
storage directory is not writable.

**Fix**: Verify:
```bash
python -m venv test-venv
```
If that works, check storage permissions:
```bash
touch <storage>/store/providers/.test-write && rm <storage>/store/providers/.test-write
```

### "Registry file unreadable"

**Cause**: `registry.json` is corrupted (invalid JSON, hand-edited with a
typo).

**Fix**: The registry is designed to be resilient — a corrupted file causes
the manager to start from an empty index, but the file is **not** deleted.
Inspect and repair:

```bash
cat <storage>/store/providers/registry.json | python -m json.tool
```

If it is unrepairable, back it up and delete it:
```bash
mv <storage>/store/providers/registry.json <storage>/store/providers/registry.json.bak
```

The next install will create a fresh empty registry. Existing version
directories will be orphaned and can be cleaned manually.

---

## Verifying an install

### After the install

The upload modal closes and the card appears in the "Installed" tab. Here is
how to verify everything worked:

**1. The card appears**:
- Name matches `display_name` (or `name`)
- Transport badge is correct (Built-in / Compatible / Custom)
- Status badge is "Not configured" (until you add a connection)

**2. The registry was updated**:

```bash
cat <storage>/store/providers/registry.json | python -m json.tool
```

Look for your package's `id` in `packages`.

**3. Files are in place**:

```bash
ls -la <storage>/store/providers/<id>/versions/<version>/
```

Should show `manifest.json`, `_peldrun_meta.json`, and the package's Python
files (for native/custom).

**4. Venv exists** (for native/custom):

```bash
ls -la <storage>/store/providers/<id>/environments/<version>/bin/
```

Should show `python` (or `python.exe` on Windows).

**5. Dependencies are installed**:

```bash
<storage>/store/providers/<id>/environments/<version>/bin/pip list
```

Should show the packages declared in `requirements.txt` plus their transitive
dependencies.

**6. Health check passes** (via API):

```bash
curl http://localhost:8088/api/store/providers/<id>/health
```

Expected:
```json
{
  "status": "ok",
  "health": {
    "id": "gigachat",
    "version": "1.0.0",
    "hash": "a1b2c3...",
    "status": "healthy",
    "checks": [
      {"name": "active_pointer", "passed": true, "message": "..."},
      {"name": "version_directory", "passed": true, "message": "..."},
      {"name": "manifest_present", "passed": true, "message": "readable"},
      {"name": "entry_point_present", "passed": true, "message": "ok"},
      {"name": "venv_present", "passed": true, "message": "..."}
    ]
  }
}
```

If any check fails, the `status` becomes `"degraded"` or `"broken"` and the
failing check is identified.

### What if health is "degraded" or "broken"?

**`degraded`** — Some checks passed. Usually a missing file (manifest deleted,
entry point renamed). Fix:
1. Uninstall the package
2. Reinstall it

**`broken`** — All checks failed. The version directory is missing entirely.
Fix:
1. Uninstall
2. Reinstall

**`healthy`** — Everything is in place.

---

## Managing installed packages

### How to uninstall

Click **Delete** on the card, then confirm. This:

1. Removes every version directory: `<id>/versions/*`
2. Removes every venv: `<id>/environments/*`
3. Removes the `active.json` pointer
4. Removes the entry from `registry.json`
5. Removes the provider directory itself if empty

**What is preserved**:
- Every connection in `models_vault.json.provider_connections` — **not
  deleted**. This is intentional: if the user reinstalls the package, their
  credentials are still there.

**What is not preserved**:
- The Python code, venv, and package metadata — all deleted.

### How to reinstall

After uninstall, upload the ZIP again. The install runs the full 6-step
pipeline (no shortcuts).

If the connection still exists, it appears automatically. The user's
credentials survive uninstall/reinstall cycles.

### How to upgrade

Upload a ZIP with a bumped version (e.g., `1.0.0` → `1.1.0`). The install:

1. Validates the new ZIP
2. Conflict check passes (different version)
3. Extract into staging
4. Promote to `versions/1.1.0/`
5. Create new venv for `1.1.0`
6. Update registry:
   - Appends `1.1.0` to `installed_versions[]`
   - Preserves `1.0.0` in the list (rollback target)
   - Sets `active_version = "1.1.0"`
   - Updates `active.json`

**Old version files and venv are preserved**. Both can be reactivated.

### How to roll back

Click **Activate** on an older version in the Details drawer. This:

1. Verifies the older version's files are on disk
2. Verifies the older version's venv exists
3. Updates `registry.json` (`active_version = "1.0.0"`)
4. Updates `active.json`

**No files are moved**. The switch is a pointer update. Rollback is instant.

### How to jump forward

After a rollback, you can activate the newer version again. Same mechanism,
same instant switch.

---

## Storage layout

Here is the complete tree after installing one native package and one
compatible package:

```
<storage>/store/providers/
├── registry.json                                  # Global index
├── .staging/                                      # Temp dir (empty after install)
│
├── gigachat/                                      # Native package
│   ├── active.json                                # {"version": "1.0.0"}
│   ├── versions/
│   │   ├── 1.0.0/                                 # v1.0.0 files
│   │   │   ├── manifest.json
│   │   │   ├── _peldrun_meta.json
│   │   │   ├── requirements.txt
│   │   │   └── gigachat_peldrun/
│   │   │       ├── __init__.py
│   │   │       └── adapter.py
│   │   └── 1.1.0/                                 # v1.1.0 files (after upgrade)
│   │       ├── manifest.json
│   │       ├── _peldrun_meta.json
│   │       ├── requirements.txt
│   │       └── gigachat_peldrun/
│   │           ├── __init__.py
│   │           └── adapter.py
│   └── environments/
│       ├── 1.0.0/                                 # v1.0.0 venv (preserved)
│       │   ├── bin/python
│       │   └── lib/python3.12/site-packages/
│       └── 1.1.0/                                 # v1.1.0 venv (active)
│           ├── bin/python
│           └── lib/python3.12/site-packages/
│
└── acme-ai/                                       # Compatible package
    ├── active.json                                # {"version": "1.0.0"}
    └── versions/
        └── 1.0.0/                                 # No venv needed
            ├── manifest.json
            └── _peldrun_meta.json
```

**Key observations**:

1. `registry.json` is the single source of truth for "which packages are
   installed"
2. Each version has its own files (never modified after install)
3. Each version has its own venv (native only — compatible has none)
4. `active.json` per package points to the currently active version
5. `.staging/` is empty after a successful install — it is used only during
   extraction

### Reclaiming disk space

To reclaim space from old versions:

1. **Roll back is preserved by default** — do not delete old versions unless
   you are sure you will never need them
2. If you want to force-delete old versions:
   - Uninstall the package (deletes everything)
   - Reinstall the current version only
   - Or manually: `rm -rf <id>/versions/<old-version> <id>/environments/<old-version>` (update registry after)

---

## What's next

- **Need to add a connection after install?** → [Connection Management](10-connection-management.md)
- **Hitting an install failure?** → [Troubleshooting](14-troubleshooting.md)
- **Need the manifest reference?** → [Manifest Reference](04-manifest-reference.md)
- **Curious about security?** → [Security Model](12-security-model.md)
```

---
