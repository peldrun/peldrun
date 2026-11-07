<!-- docs/skills/skill-catalog.md -->

# Skill Catalog

> One-click skill installation from a remote GitHub-hosted catalog.

The Skill Catalog is a PELDRUN subsystem that lets users browse, preview, and install Skills from a curated remote catalog — without leaving the Store UI and without manually downloading ZIP files. It is the skill-subsystem analogue of the Provider Packages Catalog (`omweb/provider_packages/catalog.py`), and both share the same architectural pattern: four source layers, disk-cached remote fetch, and SHA-256-verified downloads.

**Status:** Stable. Backend + frontend implemented and shipping.

**Related documentation:**
- [Provider Packages Catalog](../providers/catalog.md) — the sibling subsystem this one mirrors.
- [Skills subsystem overview](./README.md) — manifest schema, lifecycle, and injection model.
- [Creating a skill](./creating-skills.md) — how to author a skill package from scratch.
- [Skill Catalog repository](https://github.com/peldrunai/peldrun-skill-catalog) — the official remote catalog this instance fetches by default.

---

## Table of contents

1. [Why it exists](#1-why-it-exists)
2. [Architecture](#2-architecture)
3. [User-facing flow](#3-user-facing-flow)
4. [Backend](#4-backend)
   - [Module layout](#41-module-layout)
   - [Data model](#42-data-model)
   - [Sources and merge precedence](#43-sources-and-merge-precedence)
   - [Caching strategy](#44-caching-strategy)
   - [Download and verification pipeline](#45-download-and-verification-pipeline)
   - [Installation delegation](#46-installation-delegation)
5. [Frontend](#5-frontend)
   - [Types](#51-types)
   - [API client](#52-api-client)
   - [Hook](#53-hook)
   - [Components](#54-components)
   - [Modals](#55-modals)
6. [HTTP API reference](#6-http-api-reference)
7. [Environment variables](#7-environment-variables)
8. [Error codes](#8-error-codes)
9. [Security model](#9-security-model)
10. [Publishing a skill to the catalog](#10-publishing-a-skill-to-the-catalog)
11. [End-to-end install flow](#11-end-to-end-install-flow)
12. [Troubleshooting](#12-troubleshooting)
13. [File checklist](#13-file-checklist)
14. [Comparison with the Provider Packages Catalog](#14-comparison-with-the-provider-packages-catalog)
15. [Known limitations and future work](#15-known-limitations-and-future-work)

---

## 1. Why it exists

Before the Skill Catalog, installing a new skill required six steps:

1. Find the skill's source repository on GitHub.
2. Locate or build a `.zip` release artifact.
3. Download the ZIP to disk manually.
4. Open **Store → Skills → Install Skill (.zip)**.
5. Upload the ZIP through the file picker.
6. Wait for extraction, validation, and atomic install (~2-5 seconds).

The Skill Catalog reduces this to three:

1. Open **Store → Skills → Catalog**.
2. Click **Install** on the desired card.
3. Watch the progress modal — done.

Beyond the UX improvement, the catalog provides:

- **Discovery.** Users see what is available without knowing GitHub URLs.
- **Updates.** Cards show an **Update available** badge when the catalog advertises a newer version than what is installed.
- **Integrity.** Every install verifies a SHA-256 checksum, blocking supply-chain tampering.
- **Preview-only mode.** Unfinished or paid skills can be listed without being installable, letting users plan ahead.

The Skill Catalog is **additive** — manual ZIP upload is unchanged and remains the primary install path for local or private skills.

---

## 2. Architecture

The subsystem spans three separate concerns:

```
┌───────────────────────────────────────────────────────────────────┐
│   External: peldrun-skill-catalog (GitHub Pages)                  │
│   - catalog.json — the machine-readable index                     │
│   - Each skill is a ZIP hosted on its own repo's GitHub Releases  │
└───────────────────────────────┬───────────────────────────────────┘
                                │ HTTPS GET (24h positive TTL,
                                │           5min negative cache)
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│   Backend: omweb/skills/catalog/                                  │
│   - SkillCatalog — 4-layer merge + disk cache                     │
│   - 4 HTTP endpoints under /api/skills/catalog/*                  │
│   - Delegates install to SkillManager.install_skill               │
└───────────────────────────────┬───────────────────────────────────┘
                                │ JSON over HTTP
                                ▼
┌───────────────────────────────────────────────────────────────────┐
│   Frontend: app/stores/                                           │
│   - skillCatalogApi.ts — typed HTTP client                        │
│   - useSkillActions — catalog state + install orchestration       │
│   - SkillsSection — Installed / Catalog two-tab UI                │
│   - CatalogSkillCard — card per item                              │
│   - CatalogSkillInstallModal — progress + result UI               │
│   - CatalogSkillDetailsModal — full metadata view                 │
└───────────────────────────────────────────────────────────────────┘
```

The external repository only publishes a single JSON file plus versioned ZIP assets. It has **no runtime coupling** to PELDRUN — it is a static site.

---

## 3. User-facing flow

### Happy path — install a skill

1. User opens **Store → Skills**. The default tab is **Installed**.
2. User clicks the **Catalog** tab. The frontend lazy-loads `GET /api/skills/catalog` on first open.
3. A grid of cards renders — each card shows:
   - Brand-colored left accent bar (`brand_color`)
   - Name + version
   - `ROUTER · N` badge if `has_sub_skills: true`
   - `Preview only` badge if `installable: false`
   - `Installed` / `Update available` badges when applicable
   - `FROM CATALOG` appears on the installed card after install
4. User clicks **Details** (optional) → `CatalogSkillDetailsModal` shows full metadata, all install warnings, publisher info, and links.
5. User clicks **Install**:
   - `CatalogSkillInstallModal` opens with a simulated 5-phase progress bar (500 ms per phase).
   - Backend does the real work: download → SHA-256 verify → install → refresh registry.
   - On response, the modal swaps to a **real** success or error banner using the actual backend payload.
6. On success:
   - `skills` list is refetched (`reload()`).
   - `peldrun:skills-changed` event fires, so the chat composer re-registers slash commands.
   - The card shows **Installed**; the Installed tab shows the new skill with a **FROM CATALOG** badge.

### Preview-only item — Install is disabled

When `installable: false` (missing `download_url` or `checksum_sha256`):

- The button is disabled.
- The tooltip reads the exact `installability_reason` string from the backend.
- If the user somehow triggers install (e.g. via API), the backend rejects it with `400 SKILL_CATALOG_NOT_INSTALLABLE` before any download begins.

---

## 4. Backend

### 4.1 Module layout

```
backend/omweb/skills/catalog/
├── __init__.py                    Public API re-exports
├── catalog.py                     SkillCatalog class + module constants
├── schema.py                      SkillCatalogItem (Pydantic v2)
└── builtin_catalog.json           Fallback catalog shipped with PELDRUN
```

### 4.2 Data model

`SkillCatalogItem` (Pydantic v2) mirrors the shape of `ProviderPackageCatalogItem`, with skill-specific additions.

**Identity & source**

| Field | Type | Required | Notes |
|---|---|:-:|---|
| `id` | `str` | ✅ | Canonical skill ID (`[a-z0-9][a-z0-9_]{2,63}`). Normalized via `normalize_skill_id` on load. |
| `name` | `str` | ✅ | Technical name. |
| `display_name` | `str \| None` | — | Shown on cards; falls back to `name`. |
| `description` | `str` | — | Card text. |
| `version` | `str` | ✅ | Semver. Compared against installed version for update detection. |
| `download_url` | `str \| None` | — | Must be `https://`. Presence required for install. |
| `checksum_sha256` | `str \| None` | — | 64 lowercase hex chars. Presence required for install. |

**Branding**

| Field | Type | Notes |
|---|---|---|
| `brand_color` | `str \| None` | `#RRGGBB`. Rendered as left border accent. |
| `icon` | `str \| None` | Icon name (`icon_type="name"`) or inline SVG (`icon_type="svg"`). |
| `icon_type` | `"name" \| "svg"` | Default `"name"`. |

**Publisher**

| Field | Type | Notes |
|---|---|---|
| `author` | `str \| None` | Flat publisher name. |
| `author_url` | `str \| None` | |
| `developer` | `DeveloperInfo \| None` | Structured: name, url, github_username, verified. |
| `homepage` / `repository_url` / `docs_url` | `str \| None` | |
| `license` | `str \| None` | SPDX identifier. |

**Skill-specific**

| Field | Type | Notes |
|---|---|---|
| `tags` | `List[str]` | Discovery and filtering. |
| `has_sub_skills` | `bool` | True for router skills. |
| `sub_skills_count` | `int` | Must be > 0 iff `has_sub_skills`. |
| `trust` | `"verified" \| "community" \| "unverified"` | Renders shield icon when `verified`. |
| `stability` | `"experimental" \| "beta" \| "stable" \| "deprecated"` | Deprecated still installable; warning shown. |
| `release_date` | `str \| None` | ISO-8601 date. |
| `deprecation_message` | `str \| None` | |
| `install_warnings` | `List[str]` | Shown in the install confirmation dialog. |

**Computed at load time (not from catalog.json)**

| Field | Type | Notes |
|---|---|---|
| `installable` | `bool` | `True` iff `download_url` AND valid SHA-256 are present. |
| `installability_reason` | `str` | Human-readable; rendered as a button tooltip. |

The catalog JSON file **must not** include `installable` or `installability_reason` — they are derived server-side. The validator rejects items that declare them (via `additionalProperties: false`).

### 4.3 Sources and merge precedence

`SkillCatalog` merges four sources, lowest priority first:

| Priority | Source | Location | When loaded |
|:-:|---|---|---|
| 1 | **builtin** | `omweb/skills/catalog/builtin_catalog.json` | At construction |
| 2 | **remote** | `PELDRUN_SKILL_CATALOG_URL` (default: GitHub Pages) | On `ensure_fresh()` |
| 3 | **custom** | `PELDRUN_SKILL_CATALOG_PATH` | At construction |
| 4 | **extra** | `extra_paths` constructor argument | At construction (tests) |

Merge rule: a later source overrides an earlier one **only for items with the same `id`**. `code_review` in the remote catalog replaces `code_review` from builtin but does not affect `debugging`.

The merged result is stored in `self._items`, which `list_items()` returns.

Example: builtin has 3 items (`code_review`, `debugging`, `test_planning`); remote has 1 (`code_review` updated). Merged `_items` has 3 items; `code_review` comes from remote; `debugging` and `test_planning` come from builtin.

### 4.4 Caching strategy

Two independent TTLs govern remote fetches:

| Cache | Constant | Default | Resets on |
|---|---|---|---|
| **Positive TTL** | `REMOTE_CATALOG_TTL_SECONDS` | 24 hours | Successful fetch |
| **Negative TTL** | `REMOTE_CATALOG_NEGATIVE_TTL_SECONDS` | 5 minutes | Failed fetch |

Both are bypassed by `refresh(force=True)` — used by the **Refresh** button and by `POST /api/skills/catalog/refresh`.

**Why negative cache matters:** Without it, every `GET /api/skills/catalog` would re-attempt the remote fetch when the remote is down. During a GitHub Pages outage, the tab would hit the network on every user interaction, flooding logs and burning a network round-trip per render. The 5-minute negative cache dampens this while still allowing automatic recovery.

**Disk persistence:** on successful fetch, the merged item set is written to `<backend>/data/skill_catalog_remote_cache.json` with a `_cache_meta` block containing `source_url` and `fetched_at`. On startup, `_load_remote_cache` restores it only if `source_url` matches the currently effective URL (defends against stale cache after an operator changes the environment variable).

**Offline behavior:** when the backend starts with a valid cache on disk and no network, the cache is served immediately (subject to its age). If the age exceeds the positive TTL and the network is still down, the builtin catalog is used exclusively until the next successful fetch.

### 4.5 Download and verification pipeline

`install_from_catalog` executes these steps:

1. **Resolve the item.** `get_or_raise(item_id)` raises `SKILL_CATALOG_ITEM_NOT_FOUND` (404) if the ID is unknown.
2. **Check installability.** `is_installable(item)` must be `True`. Otherwise `SKILL_CATALOG_NOT_INSTALLABLE` (400).
3. **Validate URL scheme.** Must be `http://` or `https://`. Otherwise `SKILL_CATALOG_UNSAFE_URL` (400).
4. **Stream the ZIP** with:
   - 60-second total timeout (`DOWNLOAD_TIMEOUT_SECONDS`)
   - 10 MB hard cap (`MAX_SKILL_ZIP_BYTES`)
   - Max 3 redirect hops (`MAX_DOWNLOAD_REDIRECTS`)
   - SHA-256 hashing on the fly (`hashlib.sha256`)
5. **Compare checksum.** Mismatch → `SKILL_CATALOG_CHECKSUM_MISMATCH` (400). No file written.
6. **Delegate install** to `SkillManager.install_skill(zip_bytes, source_filename=..., source="catalog")` in a thread (`asyncio.to_thread`).

Step 6 is the critical design decision: the install path is **identical** to the manual upload path. The catalog does not re-implement zip validation, manifest schema checks, SVG sanitization, or sub-skill containment — all those live in `SkillManager` and are called by both paths. This eliminates any class of vulnerability unique to the catalog.

### 4.6 Installation delegation

`SkillManager.install_skill` performs the standard pipeline:

- `validate_zip` (path traversal, symlinks, entry count, size caps — shared with Tool Bundles)
- `safe_extract` to a staging dir
- `_normalize_manifest_id_inplace` (hyphens → underscores)
- `load_skill_package` (manifest schema, SVG sanitization, sub-skills validation)
- compatibility check (`peldrun_min_version`)
- atomic rename to `storage/store/skills/<id>/`
- registry refresh

The catalog passes `source="catalog"` through to `SkillStorage`, which records it in `.installed.json`. This is what makes the **FROM CATALOG** badge possible on the installed card.

---

## 5. Frontend

### 5.1 Types

Defined in `frontend/src/app/stores/_types/store.ts`:

- **`SkillCatalogItem`** — mirrors the backend Pydantic model exactly.
- **`SkillCatalogInstallState`** — derived cross-reference against installed skills (`isInstalled`, `isEnabled`, `installedVersion`, `installedSkill`).
- **`SkillCatalogListResponse`** — `GET /api/skills/catalog` response shape, including optional `status_info`.
- **`SkillCatalogRefreshResponse`** — `POST /api/skills/catalog/refresh` response. Note `status: "success" | "error"` — the `"error"` case is a valid HTTP 200 response (remote fetch failed but builtin still serves).
- **`CatalogSkillInstallResult`** — discriminated union: `{success: true, skillId, version, name}` or `{success: false, error, code?}`. Consumed by `CatalogSkillInstallModal`.

`SkillSummary` was extended with a `source` field (`"uploaded" | "catalog" | "builtin"`) and an optional `brand_color` field.

### 5.2 API client

`frontend/src/lib/skillCatalogApi.ts` — deliberately independent of `providerApi.ts` (no shared error type, no shared request helper):

| Export | Endpoint | Throws on |
|---|---|---|
| `listSkillCatalog()` | `GET /api/skills/catalog` | Network / non-2xx |
| `getSkillCatalogItem(id)` | `GET /api/skills/catalog/{id}` | 404 / network |
| `refreshSkillCatalog()` | `POST /api/skills/catalog/refresh` | Network / non-2xx (but **not** on `status: "error"` body) |
| `installSkillFromCatalog(id)` | `POST /api/skills/catalog/{id}/install` | Any failure |
| `formatSkillCatalogError(err)` | — | Never (formats) |

Errors thrown are `SkillCatalogApiError` (extends `Error`) with `{ message, code, details, status }`.

### 5.3 Hook

`useSkillActions` (in `frontend/src/app/stores/_hooks/useSkillActions.ts`) owns the catalog state:

| State | Type | Purpose |
|---|---|---|
| `catalogItems` | `SkillCatalogItem[]` | Cached list from last load |
| `isCatalogLoading` | `boolean` | Initial load in flight |
| `isCatalogRefreshing` | `boolean` | Force refresh in flight |
| `catalogError` | `string \| null` | Last load/refresh error |
| `catalogEnabled` | `boolean` | Kill switch from `status_info.enabled` |
| `installingCatalogId` | `string \| null` | Item being installed (disables card button) |
| `catalogInstallItem` | `SkillCatalogItem \| null` | Modal driver — non-null means open |
| `catalogInstallResult` | `CatalogSkillInstallResult \| null` | Modal result — null means running |
| `catalogDetailsItem` | `SkillCatalogItem \| null` | Details modal driver |

Actions:

- `handleLoadCatalog()` — fetches the list (bypasses the client wrapper to read `status_info.enabled`).
- `handleRefreshCatalog()` — force fetch; surfaces `status: "error"` as a warning toast.
- `handleInstallFromCatalog(item)` — sets `catalogInstallItem`, calls `installSkillFromCatalog`, sets `catalogInstallResult`, refetches skills on success.
- `handleCloseCatalogInstall()` — clears modal state; no-op if `installingCatalogId !== null`.
- `handleOpenCatalogDetails(item)` / `handleCloseCatalogDetails()` — toggle details modal.

### 5.4 Components

**`SkillsSection.tsx`** — two-tab container:

- **Installed** tab: unchanged from pre-catalog behavior (`SkillCard` list).
- **Catalog** tab: lazy loads on first open; four states (loading, error, empty, populated).
- Tab bar hidden entirely when `catalogEnabled === false` (backend kill switch).

**`CatalogSkillCard.tsx`** — one card per catalog item:

- Left border accent from `brand_color`.
- Badges: `ROUTER · N`, `Preview only`, `Installed`, `Update available`, `Deprecated`, verified shield.
- Warnings preview: first `install_warning` + `+N more` count.
- Actions: **Details** (always), **Install** / **Update** / **Preview** / **View Installed** (conditional).

**`SkillCard.tsx`** — installed skill card. Extended with:

- Left border accent from `skill.brand_color`.
- **FROM CATALOG** badge when `skill.source === "catalog"`.

### 5.5 Modals

**`CatalogSkillInstallModal.tsx`** — progress + result:

- Five simulated phases (Downloading → Verifying → Extracting → Validating → Installing).
- Each phase advances every 400 ms while the fetch is in flight.
- Live log console with `[system]` / `[error]` tags.
- On response, swaps to a **real** success/error banner using the backend payload.
- Footer button becomes **Done** on success, **Close** on failure, **Installing…** while running.

The phases are simulated because the backend is synchronous (typically 2-5 seconds total). The **results are real** — the error banner shows the exact `SkillCatalogApiError.message` and `.code` from the backend. This is a deliberate UX trade-off: identical look-and-feel to `ProviderUploadModal`'s catalog mode, without the job-polling infrastructure that provider packages need (skills have no venv, no pip).

**`CatalogSkillDetailsModal.tsx`** — read-only metadata:

- Description, all `install_warnings`, tags, publisher, links.
- `installability_reason` verbatim.
- Install button state machine (Install / Update / Already installed / Preview only).
- Does **not** fetch — uses only the catalog item already in memory.

---

## 6. HTTP API reference

All endpoints live under `/api/skills/catalog` and are mounted via a separate `catalog_router` in `main.py`, **registered before** the main `skills_router`. This ordering is critical: the main router declares `GET /{skill_id}`, which would otherwise match `/catalog` as `skill_id="catalog"`. FastAPI matches routes in registration order, not by specificity.

### `GET /api/skills/catalog`

List every item, merged across all four sources.

Response:

```json
{
  "status": "success",
  "items": [ /* SkillCatalogItem[] */ ],
  "total": 3,
  "status_info": {
    "enabled": true,
    "item_count": 3,
    "builtin_count": 3,
    "custom_count": 0,
    "extra_count": 0,
    "remote_count": 1,
    "remote_url_configured": true,
    "remote_url_effective": "https://peldrunai.github.io/peldrun-skill-catalog/catalog.json",
    "remote_cache_age_seconds": 99.24,
    "remote_last_attempt_age_seconds": 99.24,
    "remote_negative_cache_ttl_seconds": 300,
    "remote_last_error": null
  }
}
```

Never raises — if remote fetch fails, the builtin catalog is returned and `remote_last_error` carries the reason.

### `GET /api/skills/catalog/{item_id}`

Single item. `404 SKILL_CATALOG_ITEM_NOT_FOUND` if unknown.

```json
{ "status": "success", "item": { /* SkillCatalogItem */ } }
```

### `POST /api/skills/catalog/refresh`

Force-fetch the remote catalog, bypassing both TTLs.

**Two valid HTTP 200 outcomes:**

Success (remote fetch worked):

```json
{
  "status": "success",
  "total": 3,
  "message": "Catalog refreshed (3 items).",
  "status_info": { /* same as above */ }
}
```

Soft failure (remote fetch failed but builtin still serves):

```json
{
  "status": "error",
  "total": 3,
  "message": "Skill catalog HTTP 404 from ...",
  "status_info": { "remote_last_error": "Skill catalog HTTP 404 ...", ... }
}
```

The frontend treats `status: "error"` as a warning toast, not an error toast.

### `POST /api/skills/catalog/{item_id}/install`

Download, verify, install. **Synchronous** — typically 2-5 seconds.

Success:

```json
{
  "status": "success",
  "skill": { /* SkillSummaryResponse — with source="catalog" */ },
  "was_upgrade": false,
  "warnings": []
}
```

Failure: standard `{error, code, details}` shape with appropriate HTTP status (400 / 404 / 500).

---

## 7. Environment variables

| Variable | Default | Effect |
|---|---|---|
| `PELDRUN_SKILL_CATALOG_URL` | `https://peldrunai.github.io/peldrun-skill-catalog/catalog.json` | Remote fetch URL. Set to `""` to disable remote entirely (offline mode). |
| `PELDRUN_SKILL_CATALOG_PATH` | *(unset)* | Local override catalog file. Higher priority than remote. |
| `PELDRUN_SKILL_CATALOG_ENABLED` | `"true"` | Kill switch. Set to `"false"` to hide the Catalog tab entirely. |

**URL resolution precedence** (from `_resolve_remote_catalog_url`):

1. `PELDRUN_SKILL_CATALOG_URL` set (including empty string) → use that value
2. Otherwise → use `DEFAULT_REMOTE_CATALOG_URL`

Empty string is a valid value meaning "no remote fetching" — it is not the same as unset.

---

## 8. Error codes

All errors follow the standard `{error: str, code: str, details: dict}` shape used across the tool bundles and provider packages subsystems.

| Code | HTTP | Meaning |
|---|:-:|---|
| `SKILL_CATALOG_ITEM_NOT_FOUND` | 404 | Item ID not in merged catalog |
| `SKILL_CATALOG_INVALID_ITEM` | 400 | Defensive: item missing required install fields (should not happen if `is_installable` was `True`) |
| `SKILL_CATALOG_NOT_INSTALLABLE` | 400 | Item has no `download_url` or valid SHA-256 |
| `SKILL_CATALOG_UNSAFE_URL` | 400 | `download_url` is not `http(s)://` |
| `SKILL_CATALOG_DOWNLOAD_FAILED` | 400 | Generic HTTP error from the ZIP host |
| `SKILL_CATALOG_HTTP_ERROR` | 400 | `httpx.HTTPError` during download |
| `SKILL_CATALOG_TIMEOUT` | 400 | 60-second download timeout exceeded |
| `SKILL_CATALOG_TOO_LARGE` | 400 | ZIP exceeds 10 MB |
| `SKILL_CATALOG_EMPTY_BODY` | 400 | Server returned empty ZIP body |
| `SKILL_CATALOG_CHECKSUM_MISMATCH` | 400 | SHA-256 mismatch — supply-chain guard |
| `SKILL_CATALOG_INSTALL_FAILED` | 500 | Unexpected error in `SkillManager.install_skill` |

Downstream errors from `SkillManager` (manifest invalid, SVG unsafe, sub-skill escape) propagate unchanged with their own codes (`SKILL_MANIFEST_INVALID`, `SKILL_SECURITY_VIOLATION`, etc.).

---

## 9. Security model

The catalog adds **three** new attack surfaces over manual ZIP upload, all mitigated:

| Threat | Mitigation |
|---|---|
| **Malicious catalog.json** | Pydantic validation; malformed items skipped with warning; schema rejects unknown fields |
| **MITM on catalog fetch** | HTTPS-only scheme check at resolution + `httpx` redirect policy |
| **Compromised GitHub Pages** | SHA-256 in the catalog protects against malicious ZIP swaps; attacker would need to compromise both the catalog host and the ZIP host |
| **MITM on ZIP download** | SHA-256 mandatory — no install without a valid checksum |
| **Zip bomb** | `MAX_SKILL_ZIP_BYTES` (10 MB) + `validate_zip` entry-count and size caps |
| **Path traversal in ZIP** | `safe_extract` (shared with Tool Bundles — battle-tested) |
| **Malicious manifest** | `load_skill_package` schema validation (identical to upload path) |
| **Sub-skill path traversal** | `_validate_sub_skills` (from Router feature) |
| **Malicious SVG** | `sanitize_svg` (twice: at install + at render) |
| **Catalog size exhaustion** | `REMOTE_CATALOG_MAX_BYTES` (2 MB) |
| **Download hangs** | 60-second hard timeout |
| **Redirect loop** | `MAX_DOWNLOAD_REDIRECTS` (3) |
| **Installation race** | `SkillManager` writes atomically via staging + rename |

**Critical design decision:** the catalog does **not** re-implement any part of the install pipeline. It calls `SkillManager.install_skill` — the exact same code path as manual upload. This means every security control that the upload path enforces is automatically enforced for catalog installs. Adding a control (e.g. a new manifest field) requires touching only `SkillManager`, not the catalog.

**Trust model:** the `trust` field (`verified` / `community` / `unverified`) is informational only — it does not gate installation. Only the SHA-256 gates installation. This keeps the installability criterion mechanical and auditable: an item either has a valid checksum or it does not.

---

## 10. Publishing a skill to the catalog

See the external repository's documentation:

- [Adding a Skill](https://github.com/peldrunai/peldrun-skill-catalog/blob/main/docs/ADDING_A_SKILL.md) — step-by-step guide.

In short:

1. Author the skill package (`manifest.json` + `SKILL.md` + optional `sub_skills/`).
2. Build a deterministic ZIP (same input → same bytes → same SHA-256).
3. Publish it as a GitHub Release asset in the skill's own repository.
4. Compute the SHA-256.
5. Add or update the item in `peldrun-skill-catalog/catalog.json`.
6. Run `python scripts/validate.py` locally.
7. Open a PR. CI validates before merge.

`brand_color` in `manifest.json` is automatically picked up by the installed card **and** must be mirrored into `catalog.json` for the catalog card — the backend does not inject it. If they differ, the two cards render different accents. The `peldrun-skill-catalog` build script (in its own repository) is responsible for keeping them in sync when publishing.

---

## 11. End-to-end install flow

```
User clicks Install on CatalogSkillCard
        │
        ▼
useSkillActions.handleInstallFromCatalog(item)
        │
        ├─ setCatalogInstallItem(item)      ← opens CatalogSkillInstallModal
        ├─ setCatalogInstallResult(null)    ← "running" state
        └─ setInstallingCatalogId(item.id)  ← disables card button
        │
        ▼
skillCatalogApi.installSkillFromCatalog(item.id)
        │
        ▼  POST /api/skills/catalog/{id}/install
        │
catalog_router.install_skill_from_catalog(item_id)
        │
        ▼
SkillCatalog.install_from_catalog(item_id)
        │
        ├─ ensure_fresh()                    ← respects TTLs
        ├─ get_or_raise(item_id)             ← 404 if unknown
        ├─ is_installable(item)?             ← 400 if preview-only
        ├─ _download_zip(url, sha256, hint)
        │     ├─ scheme check (HTTP/HTTPS)
        │     ├─ stream GET with size cap + timeout
        │     ├─ hash incrementally
        │     └─ compare SHA-256
        └─ asyncio.to_thread(
              SkillManager.install_skill,
              zip_bytes,
              source_filename="<id>-<version>.zip",
              source="catalog",
           )
                ├─ cleanup_orphans()
                ├─ SkillStorage.install_skill(zip, source="catalog")
                │     ├─ validate_zip
                │     ├─ safe_extract to staging
                │     ├─ normalize manifest.id
                │     ├─ load_skill_package (schema, SVG, sub-skills)
                │     ├─ write .installed.json (source="catalog")
                │     └─ atomic rename to final location
                └─ registry.refresh_skill(skill_id)
        │
        ▼  JSON response
        │
useSkillActions
        ├─ setCatalogInstallResult({success: true, skillId, version, name})
        ├─ await reload()                    ← Installed tab refetches
        └─ window.dispatchEvent("peldrun:skills-changed")
        │
        ▼
UI updates:
  - CatalogSkillInstallModal shows green success banner
  - CatalogSkillCard shows "Installed" badge
  - Installed tab shows new SkillCard with "FROM CATALOG" badge + brand_color accent
```

**Failure paths** (each surfaces the exact backend error string and code):

- 404 item not found → red banner, `SKILL_CATALOG_ITEM_NOT_FOUND`
- 400 preview-only → red banner, `SKILL_CATALOG_NOT_INSTALLABLE`
- 400 checksum mismatch → red banner, `SKILL_CATALOG_CHECKSUM_MISMATCH`
- 400 timeout / too-large / HTTP error → red banner with matching code
- 500 install failed → red banner, `SKILL_CATALOG_INSTALL_FAILED`

---

## 12. Troubleshooting

### The Catalog tab shows items from before my catalog update

The positive TTL is 24 hours. Force a refresh:

```bash
curl -X POST http://localhost:8088/api/skills/catalog/refresh
```

Or restart the backend — the on-disk cache is loaded at startup, but a fresh process will not re-fetch until TTL expires or `force` is called.

### The Catalog tab shows a "Preview only" badge for an item I marked installable

Check the item in `catalog.json`:

- `download_url` must be present and `https://`.
- `checksum_sha256` must be present and 64 lowercase hex chars.

If either is missing, the backend computes `installable: false` and the button is disabled. The `installability_reason` on the card explains which field is missing.

### Install fails with "checksum mismatch"

The ZIP at `download_url` no longer matches the `checksum_sha256` in the catalog. Either:

- The Release asset was re-uploaded after the checksum was recorded.
- The download URL points to a different ZIP than the one whose hash is recorded.

Recompute the SHA-256 of the current ZIP and update `catalog.json`.

### The backend keeps logging "Skill catalog HTTP 404"

The remote URL is broken or the file is not published. Verify with:

```bash
curl -sL "https://peldrunai.github.io/peldrun-skill-catalog/catalog.json" | head -20
```

If it returns HTML (a GitHub 404 page), the file is not published at that path. Check:

- GitHub Pages is enabled in the repository settings (`Settings → Pages → Source: main / root`).
- The Actions workflow that deploys Pages ran successfully.

After fixing, force a refresh:

```bash
curl -X POST http://localhost:8088/api/skills/catalog/refresh
```

### Installed card does not show the brand color that the catalog card showed

The catalog's `brand_color` is display-only metadata. For the installed card to render the same accent, `brand_color` must be present in the **skill package's `manifest.json`** — because `SkillSummary.brand_color` is read from the on-disk manifest, not from the catalog.

Two ways to keep them in sync:

1. Author `brand_color` in `manifest.json` (recommended for skills you control).
2. Rely on the `peldrun-skill-catalog` build pipeline to inject it at packaging time (for third-party skills).

Without one of these, the catalog card renders with the brand color and the installed card falls back to the theme's default border color.

### The Catalog tab is not visible at all

Check the kill switch:

```bash
echo $env:PELDRUN_SKILL_CATALOG_ENABLED
```

If it is `"false"`, the tab is hidden. Unset it or set it to `"true"` and restart the backend.

### Remote fetch works from curl but not from the backend

Check `status_info.remote_last_error`:

```bash
curl -s http://localhost:8088/api/skills/catalog | python -c "import sys,json; print(json.load(sys.stdin)['status_info'])"
```

Common causes:

- Backend has no outbound network (firewall, proxy, DNS).
- `PELDRUN_SKILL_CATALOG_URL` was set to a wrong URL.
- Corporate SSL inspection is intercepting `httpx` requests (rare; check httpx logs).

### The cache is stale after I changed `PELDRUN_SKILL_CATALOG_URL`

`_load_remote_cache` checks `_cache_meta.source_url` against the currently effective URL. If they differ, the cache is discarded. So a URL change forces a fresh fetch on next `ensure_fresh()`. No manual cache clear is needed.

---

## 13. File checklist

### Backend (new)

- `backend/omweb/skills/catalog/__init__.py`
- `backend/omweb/skills/catalog/catalog.py`
- `backend/omweb/skills/catalog/schema.py`
- `backend/omweb/skills/catalog/builtin_catalog.json`

### Backend (modified)

- `backend/omweb/skills/registry.py` — `SkillRecord.source` field + `to_summary_dict` export + `_load_one_package` reads `.installed.json`
- `backend/omweb/skills/storage.py` — `install_skill(..., source="uploaded")` kwarg + `.installed.json` records it
- `backend/omweb/skills/manager.py` — passes `source` through to `SkillStorage`
- `backend/omweb/routers/skills.py` — `catalog_router` sub-router with 4 endpoints + `SkillSummaryResponse.source` + `SkillSummaryResponse.brand_color`
- `backend/omweb/main.py` — registers `skills_rtr.catalog_router` **before** `skills_rtr.router`
- `backend/omweb/skills/_manifest_schema.json` — adds optional `brand_color` field

### Frontend (new)

- `frontend/src/lib/skillCatalogApi.ts`
- `frontend/src/app/stores/_components/CatalogSkillCard.tsx`
- `frontend/src/app/stores/_modals/CatalogSkillInstallModal.tsx`
- `frontend/src/app/stores/_modals/CatalogSkillDetailsModal.tsx`

### Frontend (modified)

- `frontend/src/app/stores/_types/store.ts` — `SkillCatalogItem`, `SkillCatalogInstallState`, `SkillCatalogListResponse`, `SkillCatalogRefreshResponse`, `CatalogSkillInstallResult`; extends `SkillSummary` with `source` and `brand_color`
- `frontend/src/app/stores/_hooks/useSkillActions.ts` — catalog state and actions
- `frontend/src/app/stores/_sections/SkillsSection.tsx` — Installed / Catalog tab bar
- `frontend/src/app/stores/_components/SkillCard.tsx` — brand_color accent + FROM CATALOG badge
- `frontend/src/app/stores/page.tsx` — wires the new section props and modals

### External (separate repository)

- `peldrun-skill-catalog/` — see [the repository README](https://github.com/peldrunai/peldrun-skill-catalog)

---

## 14. Comparison with the Provider Packages Catalog

| Aspect | Provider Catalog | Skill Catalog |
|---|---|---|
| Transport types | 3 (native/compatible/custom) | 1 (markdown-only) |
| Package format | ZIP + Python code + manifest | ZIP + manifest + SKILL.md + optional sub_skills/ |
| Isolation | Dedicated venv + pip install | None |
| Install duration | 5-30 s (venv) + 30 s – 10 min (pip) | 2-5 s total |
| Install mode | Async with job polling | **Synchronous** |
| Permissions declared | `network.connect`, `secrets.read`, ... | None (instructional only) |
| Warnings | `permission_warnings` (derived) | `install_warnings` (authored) |
| Router pattern | N/A | First-class (`has_sub_skills`) |
| Installed record source | `ProviderPackageEntry.source` | `SkillRecord.source` |
| Backend endpoints | `/catalog`, `/catalog/{id}`, `/catalog/refresh`, `/{id}/install-from-catalog` | `/catalog`, `/catalog/{id}`, `/catalog/refresh`, `/catalog/{id}/install` |
| Frontend tabs | Installed + Catalog | Installed + Catalog |
| Frontend card | `ProviderPackageCard` + `CatalogItemCard` | `SkillCard` + `CatalogSkillCard` |
| Frontend badge | FROM CATALOG | FROM CATALOG |
| Cache location | `<backend>/data/catalog_remote_cache.json` | `<backend>/data/skill_catalog_remote_cache.json` |
| Positive TTL | 24 h | 24 h |
| Negative TTL | 5 min | 5 min |
| Size cap (catalog) | 2 MB | 2 MB |
| Size cap (ZIP) | 100 MB | **10 MB** (skills are small) |
| Download timeout | 120 s | **60 s** (skills are small) |

**Rule of thumb:** every pattern in `provider_packages/catalog.py` has a direct analogue in `skills/catalog/catalog.py`, with adjustments only for size and timeout constants.

---

## 15. Known limitations and future work

**Install is synchronous.** The current design trades job polling for simplicity: skills are small and typically install in under 5 seconds. If a future skill package grows to tens of megabytes, this will need to become async. The migration path is straightforward — reuse the provider packages' install-job infrastructure. The frontend `CatalogSkillInstallModal` already has the phase/chips structure needed to render streaming progress.

**Brand color is duplicated.** The author must declare `brand_color` in both `manifest.json` and `catalog.json`. A future enhancement could have the backend inject it into the manifest during catalog install (extract → mutate → re-zip → install). This would eliminate the duplication at the cost of complicating `catalog.py`.

**No signature verification.** The catalog trusts the SHA-256 in `catalog.json`. If an attacker compromises both GitHub Pages and the release asset host, they can serve a malicious ZIP with a matching hash. A future enhancement is to sign `catalog.json` itself (minisign / sigstore) and verify the signature with a pinned public key.

**No install rollback.** Skills are single-version. Installing `v2.0.0` over `v1.0.0` deletes `v1.0.0`. Provider packages support version activation; skills do not (the skill body is always "current"). This is acceptable because skill content is instructional text, not executable code — the rollback cost is low.

**Catalog fetch is not multi-worker safe.** The singleton `SkillCatalog` is process-local. In a multi-worker deployment each worker maintains its own `_remote_loaded_at` and disk-cache TTL, leading to N fetches per TTL cycle. Acceptable for single-worker (default); a future change would centralize the cache in Redis or a shared directory.

**Details modal does not preview instructions.** `CatalogSkillDetailsModal` shows metadata only — it does not fetch the skill body. The reason is simple: the skill is not installed yet, so there is no `/api/skills/{id}/preview` endpoint to call. Adding one would require the backend to extract and inspect the ZIP's `SKILL.md` during preview — doable but adds latency to a read-only flow. The install modal's success banner is the first point where the skill's actual content is confirmed valid.
