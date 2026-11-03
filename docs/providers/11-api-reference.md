### `docs/providers/11-api-reference.md`

```markdown
<!-- docs/providers/11-api-reference.md -->
# API Reference

> **Complete reference** for every REST endpoint in the Provider Packages
> system.
>
> **Reading time**: 20-25 minutes.
>
> **Base path**: `/api/store/providers`
>
> **Who this is for**: Developers integrating with the API directly (CI/CD,
> scripts, alternative UIs), and anyone debugging a failing request.

---

## Table of Contents

- [Overview](#overview)
- [Authentication](#authentication)
- [Response format](#response-format)
- [Error format](#error-format)
- [Error code reference](#error-code-reference)
- [Package management](#package-management)
  - [List installed packages](#list-installed-packages)
  - [Get single package](#get-single-package)
  - [Upload a package](#upload-a-package)
  - [Install from catalog](#install-from-catalog)
  - [Enable a package](#enable-a-package)
  - [Disable a package](#disable-a-package)
  - [Activate a version](#activate-a-version)
  - [Delete a package](#delete-a-package)
  - [Check health](#check-health)
- [Catalog](#catalog)
  - [List catalog items](#list-catalog-items)
  - [Get single catalog item](#get-single-catalog-item)
  - [Refresh catalog](#refresh-catalog)
- [Connection management](#connection-management)
  - [List connections](#list-connections)
  - [Create a connection](#create-a-connection)
  - [Update a connection](#update-a-connection)
  - [Delete a connection](#delete-a-connection)
  - [Enable a connection](#enable-a-connection)
  - [Disable a connection](#disable-a-connection)
  - [Activate a connection](#activate-a-connection)
  - [Test a connection](#test-a-connection)
- [Complete workflows](#complete-workflows)
- [Versioning](#versioning)
- [Rate limits](#rate-limits)

---

## Overview

The Provider Packages API lives under `/api/store/providers` on the main
PELDRUN backend (default port `8088`).

### Base URL

```
http://localhost:8088/api/store/providers
```

In production deployments behind a reverse proxy, the base URL is whatever
hostname the UI is served from. All examples below use `localhost:8088` for
clarity.

### Endpoint groups

| Group | Purpose | Count |
|-------|---------|-------|
| Package management | Install, list, enable/disable, delete, health | 9 |
| Catalog | Browse and install from the store catalog | 3 |
| Connection management | Create and manage credentials | 8 |
| **Total** | | **20** |

### Content type

- **Requests**: `application/json` (except `POST /upload`, which uses
  `multipart/form-data`)
- **Responses**: `application/json`

### Related endpoints

Some provider-related functionality lives under `/api/config` rather than
`/api/store/providers`:

| Endpoint | Purpose |
|----------|---------|
| `GET /api/config/fetch-models` | Discover models from a provider |
| `GET /api/config/models-metadata` | Cached model metadata |
| `GET /api/config/vault` | Full `models_vault.json` |
| `POST /api/config/vault` | Update `models_vault.json` |

These are documented separately in the LLM Settings docs. The Provider
Packages API calls into the same storage layer but exposes only the subset
relevant to plugin packages.

---

## Authentication

**None in the current version.**

PELDRUN's API is designed for single-tenant, local-first deployment. It does
not ship with authentication. All requests are trusted by the backend.

### Deployment model

The API is intended to be reachable only from the local machine or a private
network. Do **not** expose the PELDRUN backend to the public internet without
adding authentication (via a reverse proxy, VPN, or PELDRUN's future auth
module).

### If you need auth now

Wrap the backend with a reverse proxy that requires authentication:

- **Nginx** + `auth_basic`
- **Traefik** + ForwardAuth middleware
- **Cloudflare Access** (for cloud deployments)

The API itself does not check credentials. If you expose it directly to the
internet, anyone can install packages — a severe security risk.

---

## Response format

### Success responses

Every successful response returns HTTP 200-299 with a JSON body. The body
always contains a `status` field:

```json
{
  "status": "ok",
  "...": "..."
}
```

Field-level responses follow a consistent convention:

| Field | Meaning |
|-------|---------|
| `status` | Always `"ok"` for success |
| `<entity>` | The main object (e.g., `provider`, `connection`, `catalog`) |
| `<entity>s` | Lists (e.g., `providers`, `connections`) |
| `total` | Count, when a list is present |

**Example — list response**:

```json
{
  "status": "ok",
  "providers": [...],
  "total": 3
}
```

**Example — single-object response**:

```json
{
  "status": "ok",
  "provider": {...}
}
```

**Example — action response**:

```json
{
  "status": "ok",
  "provider_id": "gigachat",
  "removed": true
}
```

### Timestamps

All timestamps are ISO 8601 UTC strings:

```json
"created_at": "2025-01-01T12:34:56.789Z"
```

### Null vs absent

When a field is `null`, the JSON contains `"field": null`. When it is absent,
the field is not in the response. Consumers should not rely on this distinction
for control flow.

---

## Error format

### Structured errors

Every error response has this shape:

```json
{
  "detail": {
    "error": "Human-readable message",
    "code": "STABLE_ERROR_CODE",
    "details": {
      "...": "field-level or context-specific information"
    }
  }
}
```

| Field | Type | Purpose |
|-------|------|---------|
| `detail.error` | `string` | Human-readable message. Safe to display to users. |
| `detail.code` | `string` | Stable identifier for programmatic handling. |
| `detail.details` | `object` | Structured context (varies by endpoint). |

### Unstructured errors

FastAPI internal errors (e.g., routing issues) return the standard shape:

```json
{
  "detail": "Not Found"
}
```

### Network errors

If the request fails before reaching the backend (network down, DNS failure),
the fetch client sees a network error. The `ProviderApiRequestError` class in
`lib/providerApi.ts` normalizes this to `code: "NETWORK_ERROR"` with
`status: 0`.

---

## Error code reference

Complete list of error codes and the HTTP status they come with.

| Code | HTTP | Meaning |
|------|------|---------|
| `PROVIDER_PACKAGE_INVALID` | 400 | ZIP failed validation (structural) |
| `PROVIDER_PACKAGE_SECURITY_VIOLATION` | 400 | ZIP tripped a security guard (Zip Slip, bomb) |
| `PROVIDER_PACKAGE_MANIFEST_INVALID` | 400 | Manifest Pydantic validation failed |
| `PROVIDER_PACKAGE_NOT_FOUND` | 404 | Package not installed |
| `PROVIDER_PACKAGE_CONFLICT` | 409 | Builtin collision, hash mismatch, or duplicate |
| `PROVIDER_PACKAGE_STORAGE_ERROR` | 500 | Disk write failure |
| `PROVIDER_PACKAGE_ENVIRONMENT_ERROR` | 500 | venv creation or pip failure |
| `PROVIDER_PACKAGE_WRONG_FILE_TYPE` | 400 | Uploaded file is not `.zip` |
| `PROVIDER_PACKAGE_EMPTY_UPLOAD` | 400 | Uploaded file is empty |
| `PROVIDER_PACKAGE_UPLOAD_READ_FAILED` | 400 | Could not read uploaded file |
| `PROVIDER_PACKAGE_NOT_INSTALLABLE` | 400 | Catalog item has no `download_url` or checksum |
| `PROVIDER_PACKAGE_CATALOG_NOT_FOUND` | 404 | Catalog item id does not exist |
| `PROVIDER_PACKAGE_CATALOG_ERROR` | 400 | Catalog download or checksum failed |
| `PROVIDER_PACKAGE_INSTALL_FAILED` | 500 | Unexpected error during install |
| `PROVIDER_PACKAGE_UNINSTALL_FAILED` | 500 | Unexpected error during uninstall |
| `PROVIDER_CONNECTION_NOT_FOUND` | 404 | Connection id does not exist |
| `INVALID_RESPONSE` | 200 | Backend returned 200 but missing expected field |

### Handling errors

**Recommended pattern in client code**:

```typescript
try {
  const result = await uploadProvider(file);
} catch (err) {
  if (err instanceof ProviderApiRequestError) {
    if (err.code === "PROVIDER_PACKAGE_CONFLICT") {
      // Show "Bump the version or uninstall first" hint
    } else if (err.code === "PROVIDER_PACKAGE_SECURITY_VIOLATION") {
      // Show a red banner with mandatory acknowledgment
    } else {
      // Show err.message
    }
  }
}
```

---

## Package management

### List installed packages

```
GET /api/store/providers
```

Returns every installed provider package, regardless of enabled state.

#### Query parameters

| Param | Type | Default | Meaning |
|-------|------|---------|---------|
| `enabled_only` | `boolean` | `false` | When true, return only enabled packages |

#### Example

```bash
curl http://localhost:8088/api/store/providers
```

#### Response

```json
{
  "status": "ok",
  "providers": [
    {
      "id": "gigachat",
      "name": "GigaChat (Sber)",
      "display_name": "GigaChat",
      "description": "GigaChat is a family of Russian language models from Sber.",
      "active_version": "1.0.0",
      "active_hash": "a1b2c3d4e5f6...",
      "enabled": true,
      "installed_versions": [
        {
          "version": "1.0.0",
          "hash": "a1b2c3d4e5f6...",
          "installed_at": "2025-01-01T00:00:00Z",
          "size_bytes": 10550,
          "file_count": 4,
          "entry_point": "gigachat_peldrun.adapter:GigaChatAdapter",
          "class_name": "GigaChatAdapter"
        }
      ],
      "transport": "native",
      "api_contract": "peldrun.llm.provider.v1",
      "features": {
        "native_streaming": true,
        "tool_calling": true
      },
      "permissions": {
        "network": "connection_endpoint",
        "secrets": ["connection.api_key"]
      },
      "tags": ["gigachat", "sber", "russian"],
      "author": "PELDRUN Community",
      "author_url": "https://github.com/peldrun",
      "homepage": "https://developers.sber.ru",
      "source": "uploaded",
      "connection_fields": [...],
      "runtime_hints": {...},
      "has_active_connection": true,
      "active_connection_id": "conn_6c87efd8a1b2c3d4",
      "active_connection_name": "Personal"
    }
  ],
  "total": 1
}
```

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 500 | — | Unexpected error reading registry |

---

### Get single package

```
GET /api/store/providers/{provider_id}
```

#### Path parameters

| Param | Type | Meaning |
|-------|------|---------|
| `provider_id` | `string` | The package's canonical id |

#### Query parameters

| Param | Type | Default | Meaning |
|-------|------|---------|---------|
| `include_manifest` | `boolean` | `false` | Include the raw on-disk `manifest.json` |

#### Example

```bash
# Basic detail
curl http://localhost:8088/api/store/providers/gigachat

# With raw manifest
curl 'http://localhost:8088/api/store/providers/gigachat?include_manifest=true'
```

#### Response

```json
{
  "status": "ok",
  "provider": {
    "id": "gigachat",
    "name": "GigaChat (Sber)",
    "...": "...",
    "has_active_connection": true,
    "active_connection_id": "conn_6c87efd8a1b2c3d4",
    "active_connection_name": "Personal"
  },
  "manifest": {
    "schema_version": "1.0.0",
    "kind": "peldrun.llm_provider",
    "id": "gigachat",
    "...": "..."
  }
}
```

If the manifest file is missing (rare — e.g., disk corruption):

```json
{
  "status": "ok",
  "provider": {...},
  "manifest": null,
  "manifest_unavailable_reason": "The manifest.json file for the active version could not be read."
}
```

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_PACKAGE_NOT_FOUND` | Package not installed |

---

### Upload a package

```
POST /api/store/providers/upload
```

Install a Provider Package from an uploaded ZIP.

#### Request

**Content type**: `multipart/form-data`

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `file` | `File` | ✅ | The `.zip` archive |
| `install_dependencies` | `boolean` (as string) | — | When `"false"`, skip pip install |

#### Example

```bash
curl -X POST http://localhost:8088/api/store/providers/upload \
  -F "file=@gigachat-1.0.0.zip" \
  -F "install_dependencies=true"
```

#### Response (success)

```json
{
  "status": "ok",
  "provider": {
    "id": "gigachat",
    "version": "1.0.0",
    "hash": "a1b2c3d4...",
    "name": "GigaChat (Sber)",
    "display_name": "GigaChat",
    "description": "...",
    "size_bytes": 10550,
    "file_count": 4,
    "transport": "native",
    "features": {...},
    "permissions": {...},
    "install_report": {
      "dependencies_installed": true,
      "dependencies_skipped": false,
      "venv_path": "/path/to/storage/store/providers/gigachat/environments/1.0.0",
      "duration_ms": 42350,
      "steps": [
        {"name": "validate", "status": "ok", "duration_ms": 15, "detail": "4 files, 22000 bytes"},
        {"name": "conflict_check", "status": "ok", "duration_ms": 2, "detail": "new_install"},
        {"name": "extract", "status": "ok", "duration_ms": 8, "detail": "4 files"},
        {"name": "promote", "status": "ok", "duration_ms": 3, "detail": "versions/1.0.0/"},
        {"name": "environment", "status": "ok", "duration_ms": 41980, "detail": "installed"},
        {"name": "register", "status": "ok", "duration_ms": 5, "detail": "gigachat@1.0.0"}
      ],
      "warnings": []
    },
    "warnings": []
  },
  "registry": {
    "total_providers": 1
  }
}
```

#### Response (idempotent re-install)

If the exact same ZIP (same version, same hash) is uploaded again:

```json
{
  "status": "ok",
  "provider": {...},
  "warnings": [
    "This version was already installed with identical content. No files were modified."
  ]
}
```

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 400 | `PROVIDER_PACKAGE_WRONG_FILE_TYPE` | File is not `.zip` |
| 400 | `PROVIDER_PACKAGE_EMPTY_UPLOAD` | File is empty |
| 400 | `PROVIDER_PACKAGE_UPLOAD_READ_FAILED` | Could not read file |
| 400 | `PROVIDER_PACKAGE_SECURITY_VIOLATION` | Zip Slip / archive bomb / symlink |
| 400 | `PROVIDER_PACKAGE_MANIFEST_INVALID` | Manifest validation failed |
| 409 | `PROVIDER_PACKAGE_CONFLICT` | Builtin collision or hash mismatch |
| 500 | `PROVIDER_PACKAGE_ENVIRONMENT_ERROR` | venv / pip failure |
| 500 | `PROVIDER_PACKAGE_STORAGE_ERROR` | Disk write failure |
| 500 | `PROVIDER_PACKAGE_INSTALL_FAILED` | Unexpected error |

---

### Install from catalog

```
POST /api/store/providers/{provider_id}/install-from-catalog
```

Install a catalog item by its catalog `id`. For `compatible` transports, this
is a manifest-only install (no download). For `native` / `custom`, this
downloads and verifies the ZIP first.

#### Path parameters

| Param | Type | Meaning |
|-------|------|---------|
| `provider_id` | `string` | The **catalog item's** id (not the installed package id) |

**Naming note**: `provider_id` in the URL is the catalog item's `id`. When
installed, the package's `id` becomes `manifest.id`, which is usually the same
string but is a distinct namespace.

#### Example

```bash
curl -X POST http://localhost:8088/api/store/providers/openai-compatible-custom/install-from-catalog
```

#### Response

Same shape as [Upload a package](#upload-a-package).

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_PACKAGE_CATALOG_NOT_FOUND` | Catalog item does not exist |
| 400 | `PROVIDER_PACKAGE_NOT_INSTALLABLE` | Item has no download URL or checksum |
| 400 | `PROVIDER_PACKAGE_CATALOG_ERROR` | Download or checksum mismatch |
| 409 | `PROVIDER_PACKAGE_CONFLICT` | Builtin collision or hash mismatch |
| 500 | `PROVIDER_PACKAGE_INSTALL_FAILED` | Unexpected error |

---

### Enable a package

```
POST /api/store/providers/{provider_id}/enable
```

Re-enable a previously disabled package.

#### Example

```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/enable
```

#### Response

```json
{
  "status": "ok",
  "provider": {...}
}
```

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_PACKAGE_NOT_FOUND` | Package not installed |

---

### Disable a package

```
POST /api/store/providers/{provider_id}/disable
```

Disable a package without uninstalling it. Disabled packages remain registered
but are skipped by the runtime.

#### Example

```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/disable
```

#### Response

Same shape as [Enable a package](#enable-a-package).

---

### Activate a version

```
POST /api/store/providers/{provider_id}/activate
```

Switch to a previously installed version. Used for rollback and roll-forward.

#### Request

**Content type**: `application/json`

```json
{
  "version": "1.0.0"
}
```

#### Example

```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/activate \
  -H "Content-Type: application/json" \
  -d '{"version": "1.0.0"}'
```

#### Response

```json
{
  "status": "ok",
  "provider": {...},
  "active_version": "1.0.0"
}
```

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_PACKAGE_NOT_FOUND` | Package or version not installed |
| 500 | `PROVIDER_PACKAGE_STORAGE_ERROR` | Version files missing from disk |

**Note**: Activating a version that was never installed returns 404
(not 400) because from the caller's perspective it is indistinguishable from
"not found".

---

### Delete a package

```
DELETE /api/store/providers/{provider_id}
```

Uninstall a package: remove every version, every venv, the active pointer, and
the registry entry.

**Connections are preserved** in `models_vault.json.provider_connections`.
Reinstalling the same package brings them back.

#### Example

```bash
curl -X DELETE http://localhost:8088/api/store/providers/gigachat
```

#### Response

```json
{
  "status": "ok",
  "provider_id": "gigachat",
  "removed": true
}
```

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_PACKAGE_NOT_FOUND` | Package not installed |
| 500 | `PROVIDER_PACKAGE_UNINSTALL_FAILED` | Unexpected error |

---

### Check health

```
GET /api/store/providers/{provider_id}/health
```

Run filesystem checks on a package's active version.

#### Example

```bash
curl http://localhost:8088/api/store/providers/gigachat/health
```

#### Response

```json
{
  "status": "ok",
  "health": {
    "id": "gigachat",
    "version": "1.0.0",
    "hash": "a1b2c3d4...",
    "status": "healthy",
    "checks": [
      {
        "name": "active_pointer",
        "passed": true,
        "message": "pointer=1.0.0 registry=1.0.0"
      },
      {
        "name": "version_directory",
        "passed": true,
        "message": "/path/to/versions/1.0.0"
      },
      {
        "name": "manifest_present",
        "passed": true,
        "message": "readable"
      },
      {
        "name": "entry_point_present",
        "passed": true,
        "message": "ok"
      },
      {
        "name": "venv_present",
        "passed": true,
        "message": "/path/to/environments/1.0.0"
      }
    ]
  }
}
```

**`status` values**:
- `"healthy"` — All checks passed
- `"degraded"` — Some failed but the package is loadable
- `"broken"` — All failed; the package is unusable

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_PACKAGE_NOT_FOUND` | Package not installed |

---

## Catalog

### List catalog items

```
GET /api/store/providers/catalog
```

Return every item in the merged catalog (built-in + custom).

#### Example

```bash
curl http://localhost:8088/api/store/providers/catalog
```

#### Response

```json
{
  "status": "ok",
  "catalog": [
    {
      "id": "openai-compatible-custom",
      "name": "OpenAI-Compatible (Custom Endpoint)",
      "display_name": "OpenAI-Compatible",
      "description": "Connect any OpenAI-compatible endpoint...",
      "version": "1.0.0",
      "transport": "compatible",
      "base_adapter": "openai_compat",
      "features": {...},
      "permissions": {...},
      "download_url": null,
      "checksum_sha256": null,
      "author": "PELDRUN",
      "homepage": null,
      "tags": ["openai-compatible", "custom-endpoint", "builtin"],
      "connection_fields": [],
      "runtime_hints": {},
      "installable": true,
      "installability_reason": "Installable — reuses a built-in adapter; no code is shipped and no download is required.",
      "permission_warnings": [
        "May make outbound HTTP requests to the connection's configured endpoint.",
        "May read the API key for THIS connection only."
      ]
    }
  ],
  "total": 7
}
```

#### Field notes

| Field | Meaning |
|-------|---------|
| `installable` | Computed by backend. `true` iff the item can be installed now. |
| `installability_reason` | Human-readable explanation. Render verbatim in tooltip. |
| `permission_warnings` | Pre-computed warning strings. Render in the install confirmation dialog. |

---

### Get single catalog item

```
GET /api/store/providers/catalog/{item_id}
```

#### Example

```bash
curl http://localhost:8088/api/store/providers/catalog/openai-compatible-custom
```

#### Response

Same shape as one element of the list, but wrapped:

```json
{
  "status": "ok",
  "item": {...}
}
```

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_PACKAGE_CATALOG_NOT_FOUND` | Item does not exist |

---

### Refresh catalog

```
POST /api/store/providers/catalog/refresh
```

Reload the catalog from disk. Does **not** perform any network calls — the
catalog file is a local artifact.

#### Example

```bash
curl -X POST http://localhost:8088/api/store/providers/catalog/refresh
```

#### Response

```json
{
  "status": "ok",
  "total": 7,
  "message": "Catalog reloaded: 7 item(s)."
}
```

---

## Connection management

### List connections

```
GET /api/store/providers/{provider_id}/connections
```

Return every saved connection for a package. Password and secret_file fields
are **masked** in the response.

#### Example

```bash
curl http://localhost:8088/api/store/providers/gigachat/connections
```

#### Response

```json
{
  "status": "ok",
  "provider_id": "gigachat",
  "connections": [
    {
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
  ],
  "total": 1
}
```

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_PACKAGE_NOT_FOUND` | Package not installed |

---

### Create a connection

```
POST /api/store/providers/{provider_id}/connections
```

#### Request

**Content type**: `application/json`

```json
{
  "name": "Personal",
  "fields": {
    "credentials": "dGVzdDp0ZXN0",
    "scope": "PERS",
    "verify_ssl": true
  },
  "is_active": true
}
```

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `name` | `string` | ✅ | User-facing label (1-120 chars) |
| `fields` | `object` | ✅ | Field values keyed by `ConnectionFieldSpec.key` |
| `is_active` | `boolean` | — | When true, becomes the active connection (deactivates siblings). Default `false`. |

#### Example

```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Personal",
    "fields": {"credentials": "dGVzdDp0ZXN0", "scope": "PERS"},
    "is_active": true
  }'
```

#### Response

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
      "scope": "PERS"
    },
    "created_at": "2025-01-01T00:00:00Z",
    "updated_at": "2025-01-01T00:00:00Z"
  }
}
```

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_PACKAGE_NOT_FOUND` | Package not installed |
| 400 | `INVALID_REQUEST` | Missing required field (name or fields) |

---

### Update a connection

```
PUT /api/store/providers/{provider_id}/connections/{connection_id}
```

#### Request

Same body as [Create a connection](#create-a-connection).

**Important**: `fields` supports **partial updates**. Omitted fields are
preserved from the current stored value.

```json
{
  "name": "Personal",
  "fields": {
    "scope": "B2B"
  }
}
```

This updates only `scope` — `credentials` and `verify_ssl` are preserved.

#### Example

```bash
curl -X PUT http://localhost:8088/api/store/providers/gigachat/connections/conn_6c87efd8a1b2c3d4 \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Personal (updated)",
    "fields": {"scope": "B2B"}
  }'
```

#### Response

Same shape as [Create a connection](#create-a-connection).

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_CONNECTION_NOT_FOUND` | Connection id does not exist |
| 404 | `PROVIDER_PACKAGE_NOT_FOUND` | Package not installed |

---

### Delete a connection

```
DELETE /api/store/providers/{provider_id}/connections/{connection_id}
```

#### Example

```bash
curl -X DELETE http://localhost:8088/api/store/providers/gigachat/connections/conn_6c87efd8a1b2c3d4
```

#### Response

```json
{
  "status": "ok",
  "connection_id": "conn_6c87efd8a1b2c3d4",
  "removed": true
}
```

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_CONNECTION_NOT_FOUND` | Connection id does not exist |

---

### Enable a connection

```
POST /api/store/providers/{provider_id}/connections/{connection_id}/enable
```

Sets `enabled = true`. Does **not** change `is_active`.

#### Example

```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections/conn_.../enable
```

#### Response

```json
{
  "status": "ok",
  "connection": {...}
}
```

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_CONNECTION_NOT_FOUND` | Connection id does not exist |

---

### Disable a connection

```
POST /api/store/providers/{provider_id}/connections/{connection_id}/disable
```

Sets `enabled = false`. Does **not** change `is_active`.

#### Example

```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections/conn_.../disable
```

#### Response

Same shape as [Enable a connection](#enable-a-connection).

---

### Activate a connection

```
POST /api/store/providers/{provider_id}/connections/{connection_id}/activate
```

Sets `is_active = true` on the target, `is_active = false` on all siblings,
and `enabled = true` on the target.

#### Example

```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections/conn_.../activate
```

#### Response

```json
{
  "status": "ok",
  "connection": {
    "id": "conn_...",
    "is_active": true,
    "enabled": true,
    "...": "..."
  }
}
```

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_CONNECTION_NOT_FOUND` | Connection id does not exist |

---

### Test a connection

```
POST /api/store/providers/{provider_id}/connections/{connection_id}/test
```

Probe the connection by calling the provider's discovery endpoint. Uses stored
values, even if fields are masked in the UI.

#### Example

```bash
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections/conn_.../test
```

#### Response (success)

```json
{
  "status": "ok",
  "ok": true,
  "message": "Connected",
  "latency_ms": 245.5
}
```

#### Response (failure)

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

**Note**: The HTTP status is always 200 — even on failure. The `ok` field
carries the success/failure status. This is intentional: the endpoint itself
succeeded, it just reported that the connection is not working.

#### Errors

| HTTP | Code | Cause |
|------|------|-------|
| 404 | `PROVIDER_CONNECTION_NOT_FOUND` | Connection id does not exist |

---

## Complete workflows

### Workflow 1 — Install from catalog, configure, use

```bash
# 1. Browse the catalog
curl http://localhost:8088/api/store/providers/catalog | jq '.catalog[] | {id, name, installable}'

# 2. Install a compatible package (no ZIP needed)
curl -X POST http://localhost:8088/api/store/providers/openai-compatible-custom/install-from-catalog

# 3. Create a connection
curl -X POST http://localhost:8088/api/store/providers/openai-compatible-custom/connections \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My OpenAI",
    "fields": {
      "base_url": "https://api.openai.com/v1",
      "api_key": "sk-..."
    },
    "is_active": true
  }'

# 4. Test the connection
CONN_ID=$(curl -s http://localhost:8088/api/store/providers/openai-compatible-custom/connections | jq -r '.connections[0].id')
curl -X POST http://localhost:8088/api/store/providers/openai-compatible-custom/connections/$CONN_ID/test

# 5. The provider is now available as "openai-compatible-custom" in chats
```

### Workflow 2 — Upload a native package and manage versions

```bash
# 1. Upload v1.0.0
curl -X POST http://localhost:8088/api/store/providers/upload \
  -F "file=@gigachat-1.0.0.zip"

# 2. Configure a connection
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Personal",
    "fields": {"credentials": "dGVzdA==", "scope": "PERS"},
    "is_active": true
  }'

# 3. Upload v1.1.0 (upgrade)
curl -X POST http://localhost:8088/api/store/providers/upload \
  -F "file=@gigachat-1.1.0.zip"

# 4. Both versions are now in installed_versions[]
curl http://localhost:8088/api/store/providers/gigachat | jq '.provider.installed_versions[] | .version'
# Output: "1.1.0", "1.0.0"

# 5. Roll back to v1.0.0
curl -X POST http://localhost:8088/api/store/providers/gigachat/activate \
  -H "Content-Type: application/json" \
  -d '{"version": "1.0.0"}'

# 6. Jump forward again
curl -X POST http://localhost:8088/api/store/providers/gigachat/activate \
  -H "Content-Type: application/json" \
  -d '{"version": "1.1.0"}'

# 7. Uninstall
curl -X DELETE http://localhost:8088/api/store/providers/gigachat
```

### Workflow 3 — Multiple connections with active selection

```bash
PROVIDER="gigachat"

# 1. Create "Personal" (active)
PERSONAL_ID=$(curl -s -X POST http://localhost:8088/api/store/providers/$PROVIDER/connections \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Personal",
    "fields": {"credentials": "cGVyc29uYWw=", "scope": "PERS"},
    "is_active": true
  }' | jq -r '.connection.id')

# 2. Create "Work" (inactive)
WORK_ID=$(curl -s -X POST http://localhost:8088/api/store/providers/$PROVIDER/connections \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Work",
    "fields": {"credentials": "d29yaw==", "scope": "B2B", "ca_bundle_path": "/path/ca.pem"},
    "is_active": false
  }' | jq -r '.connection.id')

# 3. Verify Personal is active
curl http://localhost:8088/api/store/providers/$PROVIDER | jq '.active_connection_name'
# Output: "Personal"

# 4. Switch to Work
curl -X POST http://localhost:8088/api/store/providers/$PROVIDER/connections/$WORK_ID/activate

# 5. Verify Work is active
curl http://localhost:8088/api/store/providers/$PROVIDER | jq '.active_connection_name'
# Output: "Work"

# 6. Temporarily disable Work (no active connection now)
curl -X POST http://localhost:8088/api/store/providers/$PROVIDER/connections/$WORK_ID/disable

# 7. Re-enable Work
curl -X POST http://localhost:8088/api/store/providers/$PROVIDER/connections/$WORK_ID/enable
```

### Workflow 4 — CI/CD deployment

```bash
# 1. Install all required packages
for pkg in acme-ai gigachat aws-bedrock; do
  curl -X POST http://localhost:8088/api/store/providers/$pkg/install-from-catalog
done

# 2. Configure credentials from environment
curl -X POST http://localhost:8088/api/store/providers/gigachat/connections \
  -H "Content-Type: application/json" \
  -d '{
    "name": "CI",
    "fields": {"credentials": "'"$GIGACHAT_CREDS"'", "scope": "PERS"},
    "is_active": true
  }'

# 3. Verify all installations are healthy
for pkg in acme-ai gigachat aws-bedrock; do
  status=$(curl -s http://localhost:8088/api/store/providers/$pkg/health | jq -r '.health.status')
  echo "$pkg: $status"
  if [ "$status" != "healthy" ]; then
    echo "❌ $pkg is not healthy"
    exit 1
  fi
done

echo "✅ All packages healthy"
```

---

## Versioning

The API follows **semantic versioning** on its contract, independent of
PELDRUN's release version.

### Current version

**`peldrun.llm.provider.v1`** — stable.

### Compatibility guarantees

- **Adding new endpoints**: Non-breaking. Older clients ignore them.
- **Adding optional fields to responses**: Non-breaking. Older clients ignore them.
- **Adding required fields to requests**: **Breaking**. Requires a version bump.
- **Changing existing field shapes**: **Breaking**. Requires a version bump.
- **Removing endpoints**: **Breaking**. Requires a major version bump and a
  migration guide.

### What is NOT considered breaking

- Adding new error codes
- Adding optional query parameters
- Changing order of fields in JSON
- Changing whitespace in JSON

### Deprecation policy

When an endpoint or field is deprecated:

1. A warning appears in the OpenAPI docs
2. A `Deprecation` header is added to responses
3. A removal date is announced at least 90 days in advance
4. The removal happens in a major version bump

### Contract version vs API version

The **manifest's `api_contract`** field is separate from the API's
version. The API version describes the REST interface. The contract version
describes the Python adapter interface. Both evolve independently.

---

## Rate limits

**None in the current version.**

The API does not throttle requests. All endpoints respond as fast as the
underlying operations (which are usually disk-bound, not CPU-bound).

### Practical limits

Some operations have natural limits:

| Operation | Limit |
|-----------|-------|
| Upload ZIP size | 100 MB |
| Uncompressed size | 500 MB |
| File count in ZIP | 2,000 |
| pip install timeout | 600s |
| Concurrent installs | Not limited (but not recommended > 2) |

### If you are building automation

For large deployments (100+ installs):

- **Serial installs**: 1 at a time, to avoid filesystem contention on
  `registry.json`
- **Batched tests**: Use the `test` endpoint in parallel (each call is
  independent)

The backend handles concurrent requests without corruption, but parallel
installs to the same `registry.json` serialize at the filesystem lock. Too
many parallel installs reduce throughput.

### Future rate limiting

If PELDRUN adds authentication in a future version, rate limiting will be
added alongside. The endpoints and their shape will not change.

---

## What's next

- **Need to understand the security model?** → [Security Model](12-security-model.md)
- **Need real-world examples?** → [Examples](13-examples.md)
- **Hitting an error?** → [Troubleshooting](14-troubleshooting.md)
- **Need to build a package first?** → [Quickstart — Compatible](02-quickstart-compatible.md) or [Quickstart — Native](03-quickstart-native.md)
```

