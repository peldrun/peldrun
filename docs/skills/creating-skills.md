```markdown
# Creating Skills

A skill is a self-contained, versioned instruction package that augments an
agent's system prompt with domain-specific guidance. Skills are **instructional
only** — they never execute code. Their purpose is to encode a repeatable
workflow (a code review checklist, a test-planning procedure, a security audit
routine) so that both chat invocations and agent assignments produce consistent,
high-quality output.

This document is the authoring reference. For installing and managing skills at
runtime, see [README.md](./README.md).

---

## Table of Contents

1. [Quick start](#1-quick-start)
2. [Package structure](#2-package-structure)
3. [manifest.json reference](#3-manifestjson-reference)
4. [SKILL.md format](#4-skillmd-format)
5. [Icons](#5-icons)
6. [Capabilities](#6-capabilities)
7. [Execution model](#7-execution-model)
8. [Versioning and compatibility](#8-versioning-and-compatibility)
9. [Packaging for installation](#9-packaging-for-installation)
10. [Testing your skill](#10-testing-your-skill)
11. [Best practices](#11-best-practices)
12. [Complete worked example](#12-complete-worked-example)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Quick start

The absolute minimum viable skill is two files:

```
my_skill/
├── manifest.json
└── SKILL.md
```

**`manifest.json`**

```json
{
  "schema_version": "1.0.0",
  "id": "my_skill",
  "version": "1.0.0",
  "name": "My Skill",
  "description": "A short one-line description.",
  "execution": {
    "type": "instructions"
  }
}
```

**`SKILL.md`**

```markdown
---
name: my_skill
description: A short one-line description.
---

# Instructions

1. Do the first thing.
2. Do the second thing.
3. Report your findings clearly.
```

Zip the **contents** (not the folder) and install via
`POST /api/skills/install`, or through the Store UI at `/stores` → Skills.

Everything below expands on this minimum.

---

## 2. Package structure

A skill package is a directory. When installed, PELDRUN copies the entire
directory into `backend/storage/store/skills/<skill_id>/`.

### Required files

| File | Purpose |
|------|---------|
| `manifest.json` | Declarative metadata + validation schema target. |
| `SKILL.md` | The instructions injected into the model's system prompt. Filename is configurable via `execution.instructions_file`. |

Both files **must live at the package root**. Wrapping them in an extra
directory (e.g. `my-skill-v1/my-skill/manifest.json`) is rejected at install
time.

### Optional files

| File | Purpose |
|------|---------|
| `icon.svg` | Icon file referenced by `manifest.icon`. |
| `references/*` | On-demand reference material (specs, style guides, API docs). Loaded lazily by the runtime when the skill declares `execution.references_dir`. |
| `assets/*` | Free-form support files (screenshots, sample data). Not loaded automatically. |
| `README.md` | Human-readable package documentation. **Not loaded** by the runtime. |

### Files that are explicitly forbidden

The install pipeline rejects packages containing:

- Executable binaries: `.exe`, `.dll`, `.so`, `.dylib`
- Python bytecode: `.pyc`, `.pyo`, `.pyd`
- VCS metadata: `.git/`, `.svn/`, `.hg/`
- Environment folders: `.venv/`, `venv/`, `node_modules/`
- Cache directories: `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`
- Symbolic links
- Files exceeding 5 MiB each
- Archives exceeding 100 MiB total uncompressed size
- More than 500 files per package
- File paths deeper than 10 segments

> **Why so strict?** Skills are installed from untrusted ZIPs. Every
> restriction above maps to a documented attack vector (path traversal,
> binary smuggling, symlink escape, zip bombs). The validator runs before
> any file is written to disk.

---

## 3. manifest.json reference

The authoritative schema is at
[`manifest.schema.json`](./manifest.schema.json). It is enforced at install
time (with `jsonschema`) and again at discovery time (fail-open — a skill that
fails validation is skipped, never crashes the runtime).

### Required fields

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | string | Semver. **Major version must be `1`.** Example: `"1.0.0"`. |
| `id` | string | Canonical identifier. Pattern: `^[a-z0-9][a-z0-9_]{2,63}$`. |
| `version` | string | Semver release version. Example: `"1.0.0"`. |
| `name` | string | Human-readable name (1–120 chars). |
| `description` | string | Short description (1–500 chars). Used in the `/` menu and store card. |
| `execution` | object | Execution block. See below. |

### `execution` block

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | string | `"instructions"` | **Only `"instructions"` is accepted.** Custom handlers (`handler.py`) are not supported in this release. |
| `instructions_file` | string | `"SKILL.md"` | Filename at the package root. **No directory separators allowed.** |
| `references_dir` | string | *(unset)* | Optional directory name for on-demand reference files. |

### Identity and authoring fields

| Field | Type | Notes |
|-------|------|-------|
| `display_name` | string | Optional override for `name` in the UI. |
| `short_description` | string | Optional shorter variant for the store card. |
| `store_description` | string | Longer markdown description for the details drawer. |
| `usage_notes` | string | Free-form guidance shown in the details drawer. Not injected into the prompt. |
| `author` | object | `{ "name": str, "url": uri, "email": email }` |
| `publisher_id` | string | Reverse-DNS publisher identifier (e.g. `org.peldrun.official`). |
| `trust` | enum | `"verified"` \| `"community"` \| `"unverified"` (default). Only `verified` earns the shield badge. |
| `signature` | string | Optional `sha256:<64 hex>`. Recorded but not yet enforced. |
| `license` | string | SPDX identifier or `"proprietary"`. |
| `license_url` | uri | |
| `homepage` | uri | |
| `docs_url` | uri | |
| `demo_url` | uri \| null | |
| `screenshots` | array\<uri\> | Max 10 items. |
| `release_date` | string | `YYYY-MM-DD`. |
| `stability` | enum | `"experimental"` \| `"beta"` \| `"stable"` (default) \| `"deprecated"`. Deprecated skills are hidden from the `/` menu and agent assignment. |
| `changelog_url` | uri | |
| `tags` | array\<string\> | Max 20, each ≤32 chars. Used in the `/` menu search. |
| `supported_languages` | array\<string\> | ISO 639-1 codes. |
| `i18n` | object | Per-language overrides keyed by language code. |

### `capabilities` block

Controls how the skill can be invoked.

```json
"capabilities": {
  "chat_invocable": true,
  "agent_assignable": true,
  "workflow_node": false
}
```

| Field | Default | Effect |
|-------|---------|--------|
| `chat_invocable` | `true` | Enables `/<skill_id>` invocation from the chat composer. |
| `agent_assignable` | `true` | Enables assignment via `/stores` → Agents → Skills. |
| `workflow_node` | `false` | **Reserved.** Declaration of intent for future Workflow integration. No runtime effect in the current release. |

> **Deprecated skills** have `chat_invocable` and `agent_assignable`
> effectively forced to `false` at runtime, regardless of these flags.

### `compatibility` block

```json
"compatibility": {
  "peldrun_min_version": "0.2.0",
  "peldrun_max_version": "1.0.0"
}
```

Both fields are optional. When `peldrun.__version__` is available on the host,
the install pipeline rejects skills whose declared window does not include it.
When `__version__` is unavailable (development checkouts), the check is skipped
rather than failing every skill.

### `limits` block

```json
"limits": {
  "max_instructions_bytes": 262144
}
```

Hard cap on the size of the instructions file when injected into the prompt.
Defaults to 256 KiB. Anything larger is rejected at load time. The purpose is
to prevent a runaway `SKILL.md` from blowing the model's context budget.

### `settings` block

Optional user-configurable schema, mirroring the tool manifest's
`settings.schema` convention.

```json
"settings": {
  "schema": {
    "type": "object",
    "properties": {
      "severity_threshold": {
        "type": "string",
        "enum": ["low", "medium", "high"],
        "default": "low"
      }
    }
  },
  "defaults": { "severity_threshold": "low" },
  "required": []
}
```

The runtime does not currently consume these settings — they are reserved for
future per-skill configuration. Declaring them now is safe and will not break
on upgrade.

### `examples` block

Advisory examples shown in the details drawer. **Not** injected into the
prompt.

```json
"examples": [
  {
    "description": "Review a Python patch",
    "invocation": "/code_review",
    "context": "User has attached a .py file.",
    "expected_outcome": "Findings grouped by severity with file:line refs."
  }
]
```

### Lifecycle fields

```json
"deprecated": false,
"deprecation_message": null,
"replaces": [],
"replaced_by": null
```

- `deprecated: true` — hides the skill from `/` and agent assignment.
- `replaces` — skill IDs this skill supersedes.
- `replaced_by` — skill ID that supersedes this one.

---

## 4. SKILL.md format

`SKILL.md` is a Markdown document with a **YAML frontmatter block** at the
top. The body (everything after the closing `---`) is the text that gets
injected into the model's system prompt.

### Structure

```markdown
---
name: code_review
description: Systematic code review for bugs, security, and maintainability.
---

# Code Review

You are performing a systematic code review...

## Checklist

1. ...

## Output Format

...
```

### Frontmatter requirements

- **Must start with `---`** on the first line.
- **Must contain a closing `---`** on its own line.
- Must parse as a YAML mapping (`key: value` pairs).

Supported frontmatter fields:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Recommended | Matches the manifest `id`. Informational. |
| `description` | Recommended | Matches the manifest `description`. Informational. |
| `license` | Optional | SPDX identifier. |
| `compatibility` | Optional | Free-form string. Informational. |

> **The frontmatter is metadata only.** The runtime does **not** use it for
> routing or validation — the manifest is authoritative. But the frontmatter
> is preserved and shown in the details drawer, so filling it in is strongly
> recommended.

### Body guidelines

The body is the model's instruction set. Write it as you would write
instructions for a competent colleague who has no prior context:

- **Be specific.** "Check for null checks" is weaker than "Verify every
  `.get()` call handles the missing-key case explicitly."
- **Structure with headers.** The model parses `##` and `###` reliably.
- **Include an output format.** Without one, the model improvises, and
  different runs produce different report shapes.
- **Include examples when relevant.** A short "Before/After" block is
  understood better than a paragraph of description.
- **Do not include personal data or secrets.** The body lands in a prompt
  that may be logged by the LLM provider.

### Size limit

The body (after frontmatter extraction) is capped at
`limits.max_instructions_bytes` (default 256 KiB). Files over this limit are
rejected at load time with a clear error.

### Runtime injection format

When the engine injects a skill's instructions into the system prompt, it
wraps them in delimiters so the model and humans can see exactly where the
skill content begins and ends:

```
--- BEGIN SKILL: code_review@1.0.0 ---
[Metadata: name="Code Review", tags=[review, audit, security]]
# Code Review

You are performing a systematic code review...
--- END SKILL: code_review@1.0.0 ---
```

Nothing in `SKILL.md` needs to produce these delimiters — the runtime adds
them automatically.

---

## 5. Icons

A skill can declare an icon in **one** of two ways. Both are optional; a
skill without an icon renders a Lucide `Sparkles` glyph.

### Option A: inline SVG (`icon_svg`)

```json
"icon_svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"><path fill=\"currentColor\" d=\"M12 2L2 7l10 5 10-5-10-5z\"/></svg>"
```

**Constraints (enforced at install time):**

- Must be well-formed XML.
- Root element must be `<svg>`.
- At most 32 KiB.
- At most 200 elements, depth ≤ 8.
- No `<!DOCTYPE>` or `<!ENTITY>` declarations.
- **Element whitelist:** `svg`, `g`, `path`, `circle`, `ellipse`, `rect`,
  `line`, `polyline`, `polygon`, `defs`, `linearGradient`, `radialGradient`,
  `stop`, `clipPath`, `mask`, `title`, `desc`.
- **Attribute whitelist:** geometry/paint/transform attributes plus `id`,
  `role`, `aria-*`, `data-*`. **No** `class`, `style`, `href`, `xlink:href`,
  `on*`.
- Forbidden value substrings: `javascript:`, `vbscript:`, `data:`,
  `@import`, `expression(`.
- Any `url(...)` reference must be a local `#id`.

Any violation → the whole package is rejected. The error names the specific
element, attribute, or value that triggered the rejection.

### Option B: relative SVG file (`icon`)

```json
"icon": "icons/code_review.svg"
```

The path is resolved inside the package. Symlinks are rejected, and
containment is enforced (`..` is a hard error). The file is read at
`GET /api/skills/{id}/icon.svg` and sanitized with the same rules as
`icon_svg`.

### Priority

If a skill declares **both**, `icon_svg` wins.

### What NOT to use

- **`icon_url` is not supported.** External icons leak the user's IP to
  third parties, break when the URL rots, and create a runtime dependency
  on an unrelated service. Use one of the two options above.

---

## 6. Capabilities

The `capabilities` block tells the runtime where a skill may be invoked.

| Flag | Value | Runtime effect |
|------|-------|----------------|
| `chat_invocable` | `true` | Appears in the `/` typeahead menu. Selecting it injects `invoked_skills=[id]` into the run payload. |
| `chat_invocable` | `false` | Not in `/` menu. Can still be assigned to agents. |
| `agent_assignable` | `true` | Can be added to an agent via `/stores` → Agents → Skills modal. |
| `agent_assignable` | `false` | Cannot be assigned. Can still be invoked via `/` if `chat_invocable`. |
| `workflow_node` | `true` | **Reserved.** No runtime effect today. A future Workflow engine will use it to determine node eligibility. |

A skill that is neither `chat_invocable` nor `agent_assignable` is installed
but inert — useful only as a reference / documentation package.

**Deprecated skills** are hidden from both surfaces regardless of these flags.

---

## 7. Execution model

Skills are **instruction injectors**, not code executors.

When a run starts, the engine:

1. Resolves the agent's assigned skills (from `agent_settings.json`).
2. Merges any skills explicitly invoked via `/` in the chat composer.
3. Loads each skill's `SKILL.md` body from disk.
4. Wraps each body in `--- BEGIN SKILL: id@version ---` delimiters.
5. Appends the concatenated block to the agent's system prompt.

That's it. There is no code execution, no network access, no filesystem
access beyond reading the skill's own files.

### Failure modes

A skill that fails to load (missing file, oversized body, unsafe SVG) is
**skipped** with a warning. The run continues with the surviving skills. A
broken skill can never crash a chat.

### Ordering and precedence

- **Invoked skills** (via `/`) are injected first, in alphabetical order.
- **Assigned skills** are injected second, in alphabetical order, skipping
  any that were already injected as invoked.
- A skill appearing in **both** channels is injected **once**, with the
  source marked as `"invoked"`.

The ordering is deterministic across runs, which matters for prompt caching
and reproducibility.

---

## 8. Versioning and compatibility

### Semver rules

- `version` follows `MAJOR.MINOR.PATCH` (`MAJOR.MINOR.PATCH-prerelease`).
- `schema_version` follows the same pattern, but its **MAJOR must be `1`**.
  A future breaking change to the manifest schema will bump this to `2`,
  and old installs will be rejected with a clear error.

### Version bumps

- **Patch** (`1.0.0` → `1.0.1`): typo fixes, clarifications, small
  instruction tweaks.
- **Minor** (`1.0.0` → `1.1.0`): new sections, additional checklists,
  non-breaking behavior changes.
- **Major** (`1.0.0` → `2.0.0`): the skill produces structurally different
  output, changes the intended invocation context, or drops a capability.

### Upgrades at install time

Re-installing a skill with a different version **fully replaces** the previous
package. There is no rollback: the previous files are moved to a trash
directory and deleted after the new package is committed.

Re-installing the **same version** with **identical content** is a no-op (the
install returns `was_upgrade: false` and does not touch the filesystem).

Re-installing the **same version** with **different content** emits a warning
and proceeds. This is useful for iterating on a skill, but the recommended
path is to bump the patch version so downstream consumers see the change.

### `peldrun_min_version` / `peldrun_max_version`

Both use plain semver (`MAJOR.MINOR.PATCH`) — no ranges, no operators. The
check is: `min_version <= running_version <= max_version`. Omitting either
side leaves that end of the window unbounded.

---

## 9. Packaging for installation

### ZIP layout

The ZIP **must contain** `manifest.json` and the instructions file at its
**root**. Do not wrap them in a directory:

```
code-review.zip
├── manifest.json        ✓ at root
├── SKILL.md
└── references/
    └── checklist.md
```

Not:

```
code-review.zip
└── code-review/         ✗ wrapping directory
    ├── manifest.json
    └── SKILL.md
```

### Creating the ZIP

**PowerShell:**

```powershell
Compress-Archive -Path ".\code-review\*" -DestinationPath ".\code-review.zip" -Force
```

Note the `\*` — this zips the **contents** of the folder, not the folder
itself.

**Bash / macOS:**

```bash
cd code-review && zip -r ../code-review.zip . && cd ..
```

**Python:**

```python
import zipfile
from pathlib import Path

src = Path("code-review")
with zipfile.ZipFile("code-review.zip", "w", zipfile.ZIP_DEFLATED) as zf:
    for f in src.rglob("*"):
        if f.is_file():
            zf.write(f, f.relative_to(src))
```

### Validating before install

The install endpoint runs the full validator. To catch issues earlier, you
can call the validator directly:

```python
from omweb.tools.bundles.validator import validate_zip

report = validate_zip(Path("code-review.zip").read_bytes())
print(report.ok)
for err in report.errors:
    print("ERROR:", err)
for warn in report.warnings:
    print("WARN:", warn)
```

---

## 10. Testing your skill

### Test 1: Install via HTTP

```python
from fastapi.testclient import TestClient
from omweb.main import app
from pathlib import Path

client = TestClient(app)
zip_bytes = Path("code-review.zip").read_bytes()

r = client.post(
    "/api/skills/install",
    files={"file": ("code-review.zip", zip_bytes, "application/zip")},
)
print(r.status_code)
print(r.json())
```

**Expected**: `200` with `{ "status": "success", "skill": {...} }`.

### Test 2: Verify discovery

```python
r = client.get("/api/skills")
for s in r.json()["skills"]:
    print(s["id"], s["version"], s["status"])
```

### Test 3: Verify instructions load

```python
r = client.get("/api/skills/code_review/preview")
print(r.json()["instructions_length"])
print(r.json()["instructions"][:200])
```

### Test 4: Verify injection

```python
from omweb.skills.assignments import resolve_skill_injection

# As invoked
inj = resolve_skill_injection("peldrun", ["code_review"])
assert "code_review" in [s["id"] for s in inj.skills]
assert "--- BEGIN SKILL: code_review@1.0.0 ---" in inj.text

# As assigned
client.put(
    "/api/store/agents/peldrun/skills",
    json={"disabled_skills": [], "extra_skills": ["code_review"]},
)
inj = resolve_skill_injection("peldrun", [])
assert any(
    s["id"] == "code_review" and s["source"] == "assigned"
    for s in inj.skills
)
```

### Test 5: End-to-end in the UI

1. Open `/stores` → **Skills**.
2. Install your ZIP.
3. Open `/` (landing) or any chat.
4. Type `/` — your skill should appear as `✨ /<your_id>`.
5. Select it — a chip appears above the editor.
6. Send a message — the model's response should reflect the skill's
   instructions.
7. Open `/stores` → **Agents** → ✨ on an agent — assign the skill.
8. Run the agent — the skill's instructions are injected automatically.

---

## 11. Best practices

### Writing the manifest

- **Use a stable, specific ID.** `code_review`, not `review`. IDs are
  global — a collision with an existing skill forces you to rename.
- **Fill in `short_description`.** It is what the `/` menu shows. Keep it
  under ~120 characters.
- **Set `trust: "verified"` only if you have a publisher relationship
  with PELDRUN.** The field is checked server-side; misusing it will
  surface as a broken badge.
- **Declare `compatibility.peldrun_min_version`.** It prevents confusing
  failures on older deployments.
- **Prefer `icon_svg`** over `icon`. It removes a file from the package
  and avoids an extra HTTP round-trip on the store page.

### Writing SKILL.md

- **Open with a one-sentence purpose.** "You are performing a systematic
  code review" is better than starting with a numbered list.
- **Use `##` for major sections, `###` for sub-sections.** The model's
  Markdown parsing is reliable, but deep nesting (`####`) is not.
- **Give a concrete output format.** Without one, output varies wildly
  across runs and models.
- **Keep it under 8 KiB if you can.** Every byte is injected on every run
  and counts against the context budget.
- **Test with a small model.** If a 7B model can follow the
  instructions, a 70B will too; the reverse is not true.

### Testing

- **Install a draft, then iterate.** Reinstalling the same patch version
  is a fast loop; `was_upgrade` tells you whether the disk was touched.
- **Test `/` invocation with a short prompt.** Skill instructions should
  be clear enough to guide a single-sentence request.
- **Test agent assignment.** Some skills read better as ambient
  instructions than as explicit invocations. If the model behaves
  inconsistently with `/`, try assigning instead.

### Versioning

- **Bump the patch version for every published change.** Consumers rely
  on `version` to distinguish revisions.
- **Never delete a version from a published skill.** Install the new
  version; the old files are atomically replaced.
- **Deprecate, don't remove.** If you retire a skill, set
  `deprecated: true` with a `deprecation_message` pointing to the
  replacement.

---

## 12. Complete worked example

The canonical example shipped with PELDRUN is `code_review`. Its full source
is at [`examples/code-review/`](./examples/code-review/).

### `examples/code-review/manifest.json`

```json
{
  "schema_version": "1.0.0",
  "id": "code_review",
  "version": "1.0.0",
  "name": "Code Review",
  "display_name": "Code Review",
  "description": "Systematic code review for bugs, security issues, and maintainability problems.",
  "short_description": "Find real defects with evidence, ordered by severity.",
  "store_description": "A structured code review workflow that inspects correctness, security, error handling, maintainability, and tests. Every finding is backed by a quoted excerpt and a concrete fix recommendation.",
  "tags": ["review", "audit", "security", "quality", "code"],
  "author": {
    "name": "PELDRUN Team",
    "url": "https://peldrun.ai"
  },
  "publisher_id": "org.peldrun.official",
  "trust": "verified",
  "license": "MIT",
  "homepage": "https://peldrun.ai/skills/code_review",
  "release_date": "2025-01-15",
  "stability": "stable",
  "capabilities": {
    "chat_invocable": true,
    "agent_assignable": true,
    "workflow_node": false
  },
  "execution": {
    "type": "instructions",
    "instructions_file": "SKILL.md"
  },
  "compatibility": {
    "peldrun_min_version": "0.2.0"
  },
  "limits": {
    "max_instructions_bytes": 262144
  }
}
```

### `examples/code-review/SKILL.md`

```markdown
---
name: code_review
description: Systematic code review that finds bugs, security issues, and maintainability problems in a codebase or patch.
---

# Code Review

You are performing a **systematic code review**. Your goal is to find real
defects and risk, not to produce a style report. Every finding must be
backed by evidence from the code you actually read.

## Review Checklist

### 1. Correctness

- Does the code do what its name and comments say it does?
- Are all branches and error paths handled?
- Are edge cases (empty input, null, zero, negative, concurrent access) handled?
- Do loops terminate on all inputs?

### 2. Security

- Is user input validated before use?
- Are secrets hard-coded, logged, or sent over insecure channels?
- Are file paths confined to an intended root (`..` traversal)?
- Are authorization checks present at every privileged boundary?

### 3. Error Handling

- Are exceptions caught, or silently swallowed?
- Does a failed operation leave state in a half-updated condition?

### 4. Maintainability

- Are names accurate and specific?
- Is any block doing more than one thing that would be easier to test if split?
- Is there dead code, unreachable branches, or stale comments?

### 5. Tests

- Do the changes include tests for the new behaviour?
- Do the existing tests still exercise the changed code path?

## Output Format

For each finding:

    ### [Severity] Short title

    **Location:** path/to/file.py:LINE
    **Issue:** One or two sentences.
    **Evidence:**
    ```
    code excerpt
    ```
    **Fix:** Concrete recommendation.

Severity levels: Critical / High / Medium / Low.

## Rules

1. Do not invent findings.
2. Quote the code you are critiquing.
3. Verify before flagging.
4. Separate verified defects from suggestions.
5. Report files you cannot review explicitly.
```

### Packaging

```powershell
cd docs/skills/examples
Compress-Archive -Path ".\code-review\*" -DestinationPath ".\code-review.zip" -Force
```

### Install and try

```bash
curl -X POST http://127.0.0.1:8088/api/skills/install \
  -F "file=@docs/skills/examples/code-review.zip"
```

Then open any chat and type `/code_review`, or assign it to an agent via
`/stores` → Agents → ✨.

---

## 13. Troubleshooting

### `SKILL_MANIFEST_INVALID` on install

The `manifest.json` failed schema validation. The error response contains a
`details.path` array pinpointing the failing field. Common causes:

| Symptom | Fix |
|---------|-----|
| `id must match ^[a-z0-9][a-z0-9_]{2,63}$` | Use lowercase letters, digits, underscores. Must start with letter or digit, ≥3 chars. |
| `schema_version unsupported major` | Use a `1.x.y` string. |
| `missing required field: execution` | Add the `execution` object. |
| `execution.type must be 'instructions'` | Only `"instructions"` is accepted in this release. |

### `SKILL_VALIDATION_FAILED` on install

Structural issues. Common causes:

| Symptom | Fix |
|---------|-----|
| `missing required file: manifest.json at zip root` | Zip the **contents**, not the wrapping folder. |
| `Skill package is missing SKILL.md` | Ensure the file named in `execution.instructions_file` exists at the package root. |
| `instructions_too_large` | Reduce `SKILL.md` size or raise `limits.max_instructions_bytes`. |
| `Missing '---' YAML frontmatter` | `SKILL.md` must start with `---` on line 1. |

### `SKILL_SECURITY_VIOLATION` on install

A forbidden element, attribute, or value was detected. The error names it
precisely. Common causes:

| Symptom | Fix |
|---------|-----|
| `svg_forbidden_element: script` | Remove `<script>` from `icon_svg`. |
| `svg_external_url` | All `url(...)` refs must be `#local-id`. |
| `svg_too_large` | Icon > 32 KiB. Simplify or use a file. |
| `forbidden file type: *.exe` | Remove the executable from the package. |

### Skill installs but doesn't appear in `/`

- Check `capabilities.chat_invocable` is `true`.
- Check `stability !== "deprecated"` and `deprecated !== true`.
- Check status is `"active"` (not disabled) in `/stores` → Skills.
- Reload the page — the `/` menu refreshes on the `peldrun:skills-changed`
  event, which fires on install/toggle/delete.

### Skill is injected but the model ignores it

- **The instructions may be too long.** Trim to the essential checklist.
- **The instructions may be too vague.** "Check for bugs" is weaker than
  a concrete numbered checklist.
- **Try `/` invocation instead of assignment.** Some models prioritize
  explicit invocations over ambient instructions.
- **Try a larger model.** Skills are compositional guidance; small models
  sometimes ignore the second half of a long system prompt.

### `peldrun_min_version` rejects a valid install

- Check `peldrun.__version__` on the host: `python -c "import peldrun; print(peldrun.__version__)"`.
- If the field is unset (development checkout), the check is skipped
  automatically. If it prints a value, that value must satisfy the
  declared window.

### Skill was deleted but the ID is still shown in an agent's skill modal

- The agent's `agent_settings.json` still references the deleted skill ID
  via `extra_skills` or `manifest.skills`. The Skills modal shows these as
  `resolved: false` with a "not in registry" badge.
- Delete the stale reference by opening the agent's Skills modal and
  un-checking the ghost entry.

---

## Summary checklist

Before publishing a skill, verify:

- [ ] `manifest.json` parses as JSON and passes the schema.
- [ ] `id` is lowercase, ≥3 chars, starts with letter/digit.
- [ ] `version` is semver (`MAJOR.MINOR.PATCH`).
- [ ] `execution.type` is `"instructions"`.
- [ ] `SKILL.md` starts with `---` YAML frontmatter.
- [ ] The instructions file (default `SKILL.md`) exists at the package root.
- [ ] `capabilities` reflects the intended invocation surfaces.
- [ ] `tags` include terms a user might type in `/`.
- [ ] If an icon is declared, it passes the SVG sanitizer.
- [ ] The package zips **contents**, not the folder.
- [ ] Install succeeds via `POST /api/skills/install`.
- [ ] The skill appears in `GET /api/skills`.
- [ ] `/` invocation works in the chat composer.
- [ ] Assignment works via `/stores` → Agents → Skills.

Once all boxes are checked, the skill is ready to publish.
```
