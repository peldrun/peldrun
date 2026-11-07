# Creating Skills

A skill is a self-contained, versioned instruction package that augments an
agent's system prompt with domain-specific guidance. Skills are **instructional
only** — they never execute code. Their purpose is to encode a repeatable
workflow (a code review checklist, a test-planning procedure, a security audit
routine) so that both chat invocations and agent assignments produce consistent,
high-quality output.

A skill can be **simple** (a single instruction document) or a **router**
(a parent skill that dispatches to specialized sub-skills). Router skills are
the mechanism that lets PELDRUN scale to hundreds of skills without paying the
full token cost on every message.

This document is the authoring reference. For installing and managing skills at
runtime, see [README.md](./README.md).

---

## Table of Contents

1. [Quick start](#1-quick-start)
2. [Package structure](#2-package-structure)
3. [manifest.json reference](#3-manifestjson-reference)
4. [sub_skills reference](#4-sub_skills-reference)
5. [SKILL.md format](#5-skillmd-format)
6. [Router skill authoring](#6-router-skill-authoring)
7. [Icons](#7-icons)
8. [Capabilities](#8-capabilities)
9. [Execution model](#9-execution-model)
10. [Progressive Disclosure integration](#10-progressive-disclosure-integration)
11. [BM25 routing considerations](#11-bm25-routing-considerations)
12. [Versioning and compatibility](#12-versioning-and-compatibility)
13. [Packaging for installation](#13-packaging-for-installation)
14. [Testing your skill](#14-testing-your-skill)
15. [Best practices](#15-best-practices)
16. [Complete worked example](#16-complete-worked-example)
17. [Troubleshooting](#17-troubleshooting)

---

## 1. Quick start

### 1.1 Minimal simple skill

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

### 1.2 Minimal router skill

A router skill adds a `sub_skills` array and a sub-skills directory:

```
my_router/
├── manifest.json
├── SKILL.md                       # short router body
└── sub_skills/
    ├── aspect_a/
    │   └── SKILL.md
    └── aspect_b/
        └── SKILL.md
```

**`manifest.json`**

```json
{
  "schema_version": "1.0.0",
  "id": "my_router",
  "version": "1.0.0",
  "name": "My Router",
  "description": "Router skill with two specialized sub-skills.",
  "execution": {
    "type": "instructions",
    "instructions_file": "SKILL.md"
  },
  "sub_skills": [
    {
      "id": "aspect_a",
      "description": "Handle aspect A of the task.",
      "triggers": ["a", "alpha"],
      "instructions_file": "sub_skills/aspect_a/SKILL.md"
    },
    {
      "id": "aspect_b",
      "description": "Handle aspect B of the task.",
      "triggers": ["b", "beta"],
      "instructions_file": "sub_skills/aspect_b/SKILL.md"
    }
  ]
}
```

**`SKILL.md`** (router body)

```markdown
---
name: my_router
description: Router skill with two specialized sub-skills.
---

# My Router

You have access to two specialized sub-skills. Read the user's request
and pick the single most relevant one.

## Sub-Skills

### 1. aspect_a
Use when: The user asks about A-related tasks.

### 2. aspect_b
Use when: The user asks about B-related tasks.

## How to Activate

Call `load_sub_skill(parent_id="my_router", sub_skill_id="<chosen>")`.
```

Each sub-skill file is a full `SKILL.md` with its own frontmatter and body.

Everything below expands on these minimums.

---

## 2. Package structure

A skill package is a directory. When installed, PELDRUN copies the entire
directory into `backend/storage/store/skills/<skill_id>/`.

### 2.1 Required files

| File | Purpose |
|------|---------|
| `manifest.json` | Declarative metadata + validation schema target. |
| `SKILL.md` | The instructions injected into the model's system prompt. Filename is configurable via `execution.instructions_file`. |

Both files **must live at the package root**. Wrapping them in an extra
directory (e.g. `my-skill-v1/my-skill/manifest.json`) is rejected at install
time.

### 2.2 Optional files

| File | Purpose |
|------|---------|
| `icon.svg` | Icon file referenced by `manifest.icon`. |
| `references/*` | On-demand reference material (specs, style guides, API docs). Loaded lazily by `load_reference` when the skill declares `execution.references_dir`. |
| `assets/*` | Free-form support files (screenshots, sample data). Not loaded automatically. |
| `sub_skills/<id>/SKILL.md` | Sub-skill bodies for router skills. |
| `README.md` | Human-readable package documentation. **Not loaded** by the runtime. |

### 2.3 Forbidden files

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

### 2.4 Sub-skill directory layout

Sub-skill files must live under `<package_root>/sub_skills/`. The path in
`manifest.sub_skills[i].instructions_file` must start with `sub_skills/` and
end with `.md`. Nested folders under `sub_skills/` are allowed:

```
sub_skills/security_audit/SKILL.md                ✓
sub_skills/security_audit/checklist/SKILL.md      ✓
sub_skills/SKILL.md                                ✗ (needs an id folder)
sub_skills/../etc/passwd.md                        ✗ (traversal rejected)
```

The parent skill's containment check runs on every `load_sub_skill` call,
independent of the install-time validation. A sub-skill whose resolved path
escapes the package is rejected at runtime with `SKILL_SECURITY_VIOLATION`.

---

## 3. manifest.json reference

The authoritative schema is at
[`manifest.schema.json`](./manifest.schema.json). It is enforced at install
time (with `jsonschema`) and again at discovery time (fail-open — a skill
that fails validation is skipped, never crashes the runtime).

### 3.1 Required fields

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | string | Semver. **Major version must be `1`.** Example: `"1.0.0"`. |
| `id` | string | Canonical identifier. Pattern: `^[a-z0-9][a-z0-9_]{2,63}$`. |
| `version` | string | Semver release version. Example: `"1.0.0"`. |
| `name` | string | Human-readable name (1–120 chars). |
| `description` | string | Short description (1–500 chars). Used in the `/` menu, store card, and (for router skills) the BM25 corpus. |
| `execution` | object | Execution block. See §3.2. |

### 3.2 `execution` block

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | string | `"instructions"` | **Only `"instructions"` is accepted.** Custom handlers (`handler.py`) are not supported in this release. |
| `instructions_file` | string | `"SKILL.md"` | Filename at the package root. **No directory separators allowed.** |
| `references_dir` | string | *(unset)* | Optional directory name for on-demand reference files. |

### 3.3 Identity and authoring fields

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
| `tags` | array\<string\> | Max 20, each ≤32 chars. Indexed by BM25. Strongly recommended for skill discoverability. |
| `supported_languages` | array\<string\> | ISO 639-1 codes. |
| `i18n` | object | Per-language overrides keyed by language code. |

### 3.4 `capabilities` block

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

### 3.5 `compatibility` block

```json
"compatibility": {
  "peldrun_min_version": "0.2.0",
  "peldrun_max_version": "1.0.0"
}
```

Both fields are optional. When `peldrun.__version__` is available on the host,
the install pipeline rejects skills whose declared window does not include it.
When `__version__` is unavailable (development checkouts), the check is
skipped rather than failing every skill.

### 3.6 `limits` block

```json
"limits": {
  "max_instructions_bytes": 262144
}
```

Hard cap on the size of the instructions file (and of every sub-skill file)
when read at runtime. Defaults to 256 KiB; the absolute ceiling is 1 MiB.
Anything larger is rejected with a clear error.

### 3.7 `settings` block

Optional user-configurable schema, mirroring the tool manifest's
`settings.schema` convention. The runtime does **not** consume these today —
reserved for future per-skill configuration.

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

### 3.8 `examples` block

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

### 3.9 Lifecycle fields

```json
"deprecated": false,
"deprecation_message": null,
"replaces": [],
"replaced_by": null
```

- `deprecated: true` — hides the skill from `/` and agent assignment.
- `replaces` — skill IDs this skill supersedes.
- `replaced_by` — skill ID that supersedes this one.

### 3.10 `user_editable` field

```json
"user_editable": true
```

When `true` (the default), the operator can edit the skill's `SKILL.md` body
(and every sub-skill's body) from the Store UI. Built-in skills are always
treated as `false`, regardless of this field.

Set `false` for governed packages or skills shipped as part of a managed
deployment. The editor button (pencil icon) is hidden and the PUT endpoints
return 403.

---

## 4. sub_skills reference

The `sub_skills` array turns a skill into a **router**. Each entry declares
one specialized sub-skill.

### 4.1 Shape

```json
"sub_skills": [
  {
    "id": "security_audit",
    "description": "Identify security vulnerabilities — injection, secrets, auth bypass.",
    "triggers": ["security", "injection", "xss", "sql", "auth", "secret"],
    "instructions_file": "sub_skills/security_audit/SKILL.md",
    "capabilities": {
      "exclusive": false
    }
  }
]
```

### 4.2 Field reference

| Field | Required | Constraints | Effect |
|-------|----------|-------------|--------|
| `id` | yes | `^[a-z0-9][a-z0-9_]{2,63}$`, unique within the parent | Dispatch key used by `load_sub_skill`. Also the sub-skill's identity in the UI and API. |
| `description` | yes | 10–500 chars | Shown in the catalog, the router body, and the details drawer. Indexed by BM25. |
| `triggers` | no | Max 20 items, each 1–64 chars | Keywords for BM25. Hoisted to the parent, so a query matching any trigger ranks the parent higher. |
| `instructions_file` | yes | `^sub_skills/…\.md$`, no `..` | Relative path (within the parent package) to the sub-skill's `SKILL.md`. |
| `capabilities.exclusive` | no | boolean, default `false` | Reserved: when `true`, only one sub-skill may be loaded at a time within the parent. |

### 4.3 The `maxItems: 50` limit

The array is capped at 50 entries. Beyond that, the parent's own body
becomes long enough that the router pattern stops paying off — the better
move is to split the parent into multiple router skills with their own
sub-skills.

### 4.4 Duplicate IDs

Duplicate sub-skill IDs within a single parent are rejected at install time:

```json
{
  "detail": {
    "error": "Duplicate sub_skill id: 'security_audit'.",
    "code": "SKILL_MANIFEST_INVALID",
    "details": {
      "field": "sub_skills",
      "duplicate_id": "security_audit",
      "reason": "duplicate_id"
    }
  }
}
```

Two different parents may have sub-skills with the same ID. The dispatch
key is `(parent_id, sub_skill_id)`, so `code_review/verification` and
`debugging/verification` are distinct.

### 4.5 What happens at runtime

1. BM25 indexes each sub-skill's `triggers` under the **parent** skill.
   A query "audit for SQL injection" ranks `code_review` higher because its
   `security_audit` sub-skill has a `sql` trigger.
2. The parent's `SKILL.md` body is what the model sees on `load_skill`.
   That body lists the sub-skill IDs and descriptions.
3. The model calls `load_sub_skill(parent_id, sub_skill_id)` to fetch the
   body of the chosen sub-skill.
4. Only that sub-skill's body is added to the conversation. The other
   sub-skills' bodies are never loaded.

### 4.6 Interaction with `user_editable`

Editing the parent body requires `user_editable: true` on the **parent**.
Editing a sub-skill body requires the same flag — sub-skills inherit their
parent's editability. There is no per-sub-skill editability toggle.

---

## 5. SKILL.md format

`SKILL.md` is a Markdown document with a **YAML frontmatter block** at the
top. The body (everything after the closing `---`) is the text that gets
injected into the model's system prompt.

### 5.1 Structure

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

### 5.2 Frontmatter requirements

- **Must start with `---`** on the first line.
- **Must contain a closing `---`** on its own line.
- Must parse as a YAML mapping (`key: value` pairs).
- A UTF-8 BOM at the start of the file is stripped automatically.

Supported frontmatter fields:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Recommended | Matches the manifest `id`. Informational. |
| `description` | Recommended | Matches the manifest `description`. Informational. |
| `license` | Optional | SPDX identifier. |
| `compatibility` | Optional | Free-form string. Informational. |

> **The frontmatter is metadata only.** The runtime does **not** use it for
> routing or validation — the manifest is authoritative. But the frontmatter
> is preserved across edits and shown in the details drawer, so filling it
> in is strongly recommended.

### 5.3 Body guidelines

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

### 5.4 Size limit

The body (after frontmatter extraction) is capped at
`limits.max_instructions_bytes` (default 256 KiB). Files over this limit are
rejected at load time with a clear error.

The practical recommendation is to keep a **simple** skill's body under
**8 KiB**, and a **router** skill's body under **2 KiB**. Both targets are
well below the hard cap; they exist because every byte counts against the
model's context budget on every run.

### 5.5 Runtime injection format

When the engine loads a skill body (whether via legacy full injection or
the `load_skill` tool), it wraps the content in delimiters so the model and
humans can see exactly where the skill content begins and ends:

```
--- BEGIN SKILL: code_review@2.0.0 ---
[Metadata: name="Code Review", tags=[review, audit, security]]
# Code Review Router

You have access to four specialized sub-skills...
--- END SKILL: code_review@2.0.0 ---
```

Sub-skills use a different delimiter form:

```
--- BEGIN SUB-SKILL: code_review/security_audit ---
# Security Audit

You are reviewing code for security...
--- END SUB-SKILL: code_review/security_audit ---
```

Nothing in `SKILL.md` needs to produce these delimiters — the runtime adds
them automatically.

---

## 6. Router skill authoring

A router skill is a skill whose job is to dispatch to one of several
sub-skills. This section covers the design of the router body and how to
choose sub-skill boundaries.

### 6.1 When to use a router

Use a router when **all** of the following are true:

1. The task naturally splits into **distinct aspects** with different
   output formats or different checklists (correctness, security, error
   handling, maintainability).
2. The combined body of all sub-skills would exceed **~2,000 tokens**.
3. The skill is expected to be invoked **frequently** — the token savings
   compound over time.

Do **not** use a router when:

- The skill has one clear job with a single output format.
- The sub-skills would each be under **300 tokens** (the dispatch overhead
  is not worth it — the model can handle one 300-token body).
- The skill is used **once per session** — the token savings from a router
  are a marathon, not a sprint.

### 6.2 The router body

The router body is a **short dispatch prompt**. It does not contain the
actual instructions — those live in the sub-skills.

**Structure:**

1. **One-sentence purpose.** "You are a code review router."
2. **Sub-skills section.** For each sub-skill, a `### <id>` header followed
   by a one-line "Use when:" description.
3. **How to activate.** An explicit call to `load_sub_skill` with the
   parent ID and the sub-skill ID.
4. **Global output rules** (optional). If all sub-skills share a common
   output format (delimiters, severity levels, etc.), declare it once in
   the router body. Individual sub-skills can then be shorter.

**Example skeleton:**

```markdown
---
name: code_review
description: Systematic code review with router-based sub-skill selection.
---

# Code Review Router

You have access to **four** specialized review sub-skills. Read the
user's request carefully and select the single most relevant sub-skill.

## Sub-Skills

### 1. correctness_analysis
Use when: The user has code with potential logic bugs...

### 2. security_audit
Use when: The user mentions security, injection, secrets, auth...

### 3. error_handling_review
Use when: The code involves try/catch, exception hierarchies, retries...

### 4. maintainability_review
Use when: The user asks about refactoring, complexity, naming...

## How to Activate

Call `load_sub_skill(parent_id="code_review", sub_skill_id="<chosen>")`.

Do NOT try to load more than one sub-skill in the same turn unless the
user's request explicitly covers multiple aspects. Prefer ONE.

## Global Output Rules (apply to all sub-skills)

Every finding MUST include:

1. **Location**: file:line reference.
2. **Severity**: Critical / High / Medium / Low.
3. **Evidence**: a quoted excerpt from the code.
4. **Impact**: what goes wrong if not fixed.
5. **Fix**: a concrete code change.
```

### 6.3 Choosing sub-skill boundaries

Good boundaries have three properties:

1. **Mutually exclusive.** A typical user request maps cleanly to exactly
   one sub-skill. If two sub-skills could both answer the same request,
   the model will vacillate.
2. **Individually meaningful.** Each sub-skill's body makes sense on its
   own, without needing the parent to provide missing context.
3. **Differentiated by triggers.** The `triggers` array for each sub-skill
   should use distinct vocabulary. Overlapping triggers confuse BM25.

**Anti-patterns:**

- **Severity levels as sub-skills.** `critical_review` / `medium_review` /
  `low_review` — the severity is an output detail, not a dispatch dimension.
- **Input format as sub-skills.** `python_review` / `javascript_review` /
  `go_review` — the language rarely changes the review approach; the
  checklist does.
- **Chronological phases as sub-skills.** `plan_review` / `do_review` /
  `verify_review` — the phase is chosen by the workflow, not the user
  message.

**Good patterns:**

- **Concern-based.** correctness / security / error handling /
  maintainability.
- **Role-based.** designer / implementer / reviewer / operator.
- **Artifact-based.** docs / code / tests / deployment.

### 6.4 Router body length

Keep the router body under **2 KiB**. It contributes to the catalog's token
budget on every run. Two rules of thumb:

- Sub-skill `### id` header + "Use when:" line: ~40 tokens each.
- Global output rules: ~200 tokens.
- Everything else (intro, dispatch instructions): ~100 tokens.

A 4-sub-skill router with global output rules should be ~500 tokens.

### 6.5 Testing a router

Before publishing, verify:

1. **The `/sub-skills` endpoint returns all sub-skills** with their
   triggers.
2. **`load_skill` returns the router body** with `has_sub_skills: true`
   and the correct `sub_skills` array.
3. **`load_sub_skill` succeeds for every declared ID.**
4. **BM25 ranks the parent correctly** for queries that should match a
   sub-skill. Run through a few representative prompts:

   ```python
   from omweb.skills.router.bm25_router import BM25SkillRouter
   from omweb.skills.registry import skill_registry

   router = BM25SkillRouter(skill_registry.list_skills())
   for q in ["audit for SQL injection", "this function is too complex",
             "the retry logic is broken"]:
       assert router.rank(q, top_k=1)[0][0].id == "code_review"
   ```

5. **The model dispatches correctly end-to-end.** Run a representative
   prompt through an agent and verify the SSE stream shows
   `load_skill(code_review)` followed by
   `load_sub_skill(code_review, <expected_sub>)`.

---

## 7. Icons

A skill can declare an icon in **one** of two ways. Both are optional; a
skill without an icon renders a Lucide `Sparkles` glyph.

### 7.1 Option A: inline SVG (`icon_svg`)

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

### 7.2 Option B: relative SVG file (`icon`)

```json
"icon": "icons/code_review.svg"
```

The path is resolved inside the package. Symlinks are rejected, and
containment is enforced (`..` is a hard error). The file is read at
`GET /api/skills/{id}/icon.svg` and sanitized with the same rules as
`icon_svg`.

### 7.3 Priority

If a skill declares **both**, `icon_svg` wins.

### 7.4 What NOT to use

- **`icon_url` is not supported.** External icons leak the user's IP to
  third parties, break when the URL rots, and create a runtime dependency
  on an unrelated service. Use one of the two options above.

### 7.5 Client-side rendering

The frontend fetches the icon via `GET /api/skills/{id}/icon.svg`, sanitizes
the SVG a second time (defense in depth), caches it keyed by
`<id>@<version>`, and renders it inline. A skill with no icon gets the
Lucide `Sparkles` fallback.

---

## 8. Capabilities

The `capabilities` block tells the runtime where a skill may be invoked.

| Flag | Value | Runtime effect |
|------|-------|----------------|
| `chat_invocable` | `true` | Appears in the `/` typeahead menu. Selecting it injects `invoked_skills=[id]` into the run payload. |
| `chat_invocable` | `false` | Not in `/` menu. Can still be assigned to agents. |
| `agent_assignable` | `true` | Can be added to an agent via `/stores` → Agents → Skills modal. |
| `agent_assignable` | `false` | Cannot be assigned. Can still be invoked via `/` if `chat_invocable`. |
| `workflow_node` | `true` | **Reserved.** No runtime effect today. |

A skill that is neither `chat_invocable` nor `agent_assignable` is installed
but inert — useful only as a reference / documentation package.

**Deprecated skills** are hidden from both surfaces regardless of these flags.

### 8.1 Sub-skill capabilities

Sub-skills inherit the runtime visibility of their parent. If the parent is
`chat_invocable`, the model can dispatch to any of its sub-skills. If the
parent is `agent_assignable`, so are its sub-skills.

The only sub-skill-specific capability is `exclusive`:

```json
"capabilities": { "exclusive": true }
```

**Reserved.** When implemented, an exclusive sub-skill may not be loaded at
the same time as any sibling. The current runtime does not enforce this; the
field is recorded for future use.

---

## 9. Execution model

Skills are **instruction injectors**, not code executors.

### 9.1 Legacy flow (Progressive Disclosure disabled)

When a run starts, the engine:

1. Resolves the agent's assigned skills (from `agent_settings.json`).
2. Merges any skills explicitly invoked via `/` in the chat composer.
3. Loads each skill's `SKILL.md` body from disk.
4. Wraps each body in `--- BEGIN SKILL: id@version ---` delimiters.
5. Appends the concatenated block to the agent's system prompt.

No code execution, no network access, no filesystem access beyond reading
the skill's own files.

### 9.2 Progressive Disclosure flow (default)

When a run starts, the engine:

1. Builds a **catalog** of the top-K BM25-ranked skills (see §10 and §11)
   plus any explicitly invoked skills.
2. Appends the catalog to the system prompt under `[SKILL INSTRUCTIONS]`.
3. Registers the three skill tools (`load_skill`, `load_sub_skill`,
   `load_reference`) alongside the run's normal tools.

The model then decides, at inference time, which skill bodies to fetch.
Typical flow for a router skill:

```
tool_call: load_skill("code_review")
tool_result: router body + sub_skill list
tool_call: load_sub_skill("code_review", "security_audit")
tool_result: sub-skill body
(final answer)
```

### 9.3 Failure modes

A skill that fails to load (missing file, oversized body, unsafe SVG) is
**skipped** with a warning in legacy mode. In Progressive Disclosure mode,
the corresponding tool call returns `{ "ok": false, "code": ... }` and the
model is informed of the reason.

A broken skill can never crash a chat. A broken sub-skill can never crash
a router dispatch.

### 9.4 Determinism

Both modes are deterministic:

- **Legacy:** injected skills are ordered alphabetically by source
  (invoked first, then assigned), and within each bucket alphabetically
  by ID.
- **Progressive Disclosure:** the catalog is built from a deterministic
  BM25 ranking, ties broken by ID.

This matters for prompt caching. An identical set of skills and an
identical user message produce an identical prompt prefix, which the LLM
provider can cache across runs.

---

## 10. Progressive Disclosure integration

Progressive Disclosure is the runtime feature that makes skills scale to
hundreds of packages without paying the full token cost on every message.
The authoring consequences are:

### 10.1 Descriptions matter more than ever

In legacy mode, the model saw every assigned skill's full body and could
ignore irrelevant ones at generation time. In Progressive Disclosure mode,
the model sees only the catalog **first**, and picks which bodies to load
based on the catalog entries.

The catalog shows:

- `id`
- `name`
- `description` (from the manifest, not the body)
- Sub-skill IDs and descriptions (for routers)

**Therefore**: your manifest `description` field is now a first-class
dispatch signal. A vague description (`"A skill for reviewing"`) will lose
to a specific one (`"Find real defects with evidence, ordered by severity"`).

Best practices for descriptions:

- **Lead with the action.** "Find", "Audit", "Generate", "Refactor".
- **State the domain.** "…in Python", "…for SQL queries".
- **Include a distinguishing detail.** "…with severity levels", "…covering
  the OpenAPI spec".
- **Keep under ~120 chars.** Longer descriptions get truncated in the
  catalog with an ellipsis.

### 10.2 Sub-skill descriptions are dispatch signals too

For router skills, the catalog also lists sub-skill descriptions. Those
should be one-liners that say **what** and **when**:

```
* sub_skill='security_audit': Identify security vulnerabilities — injection, secrets, auth bypass.
```

Compare to a weak version:

```
* sub_skill='security_audit': Security stuff.
```

The weak version gives the model almost no signal for choosing between
`security_audit` and `error_handling_review`.

### 10.3 Triggers fill the recall gap

BM25 indexes `triggers` in addition to descriptions. Use them to catch
synonyms and domain-specific jargon that the description does not spell
out:

```json
"triggers": ["security", "injection", "xss", "sql", "auth", "token", "secret", "vulnerability"]
```

The model will not see the triggers in the catalog (they are implementation
detail), but the BM25 ranking uses them to include the parent skill in the
top-K when the user's message matches any trigger term.

### 10.4 Tag choices affect discovery

Tags are indexed by BM25 as well. They are useful for broad categories
(`"review"`, `"audit"`, `"security"`) that a user might type in the `/`
menu, but less useful for specific terminology that triggers already
capture.

### 10.5 Sizing guidance

| Skill type | Body size | Rationale |
|------------|-----------|-----------|
| Simple, single-purpose | < 8 KiB | One `load_skill` call fetches it. |
| Router | < 2 KiB | The body is dispatched on every `load_skill` call; it must be short. |
| Sub-skill | < 8 KiB | Same guidance as simple skills. |

---

## 11. BM25 routing considerations

The BM25 Skill Router ranks skills against the user's message. Authors
indirectly control the ranking through **what they write** in the manifest.

### 11.1 What is indexed

For every skill, the router builds a searchable corpus from:

- `id`
- `name`
- `description` (manifest)
- `short_description` (manifest)
- `tags` (manifest)
- **All sub-skill `triggers`** (hoisted to the parent)

The **body** (`SKILL.md`) is **not** indexed. Long bodies would drown out
the routing signal.

### 11.2 Tokenization

- ASCII alphanumerics + underscores.
- Lowercased.
- English stopwords removed (a fixed set of ~60 high-frequency function
  words).
- Non-ASCII input (Arabic, CJK, Cyrillic) tokenizes to an empty list and
  yields no matches.

### 11.3 Authoring consequences

**Do:**

- Put distinct terms in `triggers` that the model cannot infer from
  `description`. E.g., if the skill is about SQL injection, add `"sql"`,
  `"xss"`, `"injection"`, `"prepared-statement"`.
- Use specific nouns in `description` (`"vulnerability"`, `"checklist"`,
  `"severity"`).
- Include English terms in the frontmatter `description` even if the body
  is in another language — the corpus is ASCII-only.

**Do not:**

- Repeat common words (`"code"`, `"review"`, `"file"`) in every sub-skill's
  triggers. They dilute the signal and match everything.
- Use multi-word phrases as triggers unless they are domain-standard.
  BM25 tokenizes them on whitespace anyway.
- Rely on non-English triggers. They are silently stripped.

### 11.4 The tiny-corpus fallback

When only 1–2 skills are installed, BM25's IDF formula goes negative and
the router falls back to a simple token-overlap count. This is automatic
and affects no authoring decisions. But it means that in the tiny-corpus
case, the ranking is more sensitive to raw term overlap — trigger coverage
matters more, and noise words matter less.

### 11.5 Testing BM25 ranking

Before publishing a router skill, verify that it ranks correctly for the
messages your users are likely to send:

```python
from omweb.skills.registry import skill_registry
from omweb.skills.router.bm25_router import BM25SkillRouter

skills = skill_registry.list_skills(include_disabled=False)
router = BM25SkillRouter(skills)

assert router.rank("audit for SQL injection", top_k=1)[0][0].id == "code_review"
assert router.rank("this function is too complex", top_k=1)[0][0].id == "code_review"
assert router.rank("the retry logic swallows exceptions", top_k=1)[0][0].id == "code_review"
assert router.rank("what is the weather", top_k=1) == []
```

If a query you expect to match does not, add a `trigger` that includes the
missing term.

---

## 12. Versioning and compatibility

### 12.1 Semver rules

- `version` follows `MAJOR.MINOR.PATCH` (`MAJOR.MINOR.PATCH-prerelease`).
- `schema_version` follows the same pattern, but its **MAJOR must be `1`**.
  A future breaking change to the manifest schema will bump this to `2`,
  and old installs will be rejected with a clear error.

### 12.2 Version bumps

| Change | Bump |
|--------|------|
| Typo fixes, clarifications, small instruction tweaks | **Patch** |
| New sections, additional checklists, non-breaking behavior changes | **Minor** |
| **Adding sub-skills** (new dispatch dimension) | **Minor** |
| **Removing or renaming a sub-skill** (breaking for existing chat history and agent assignments) | **Major** |
| The skill produces structurally different output, or drops a capability | **Major** |

Renaming a sub-skill is breaking: the `sub_skill_id` appears in historical
SSE events and in agent trace logs. If a rename is unavoidable, publish it
as a new major version and keep the old sub-skill available under the old
ID for one minor version of overlap.

### 12.3 Upgrades at install time

Re-installing a skill with a different version **fully replaces** the
previous package. There is no rollback.

Re-installing the **same version** with **identical content** is a no-op.

Re-installing the **same version** with **different content** emits a
warning and proceeds. Useful during development; for published packages,
always bump the patch version.

### 12.4 `peldrun_min_version` / `peldrun_max_version`

Both use plain semver (`MAJOR.MINOR.PATCH`) — no ranges, no operators. The
check is: `min_version <= running_version <= max_version`. Omitting either
side leaves that end of the window unbounded.

**Important**: `sub_skills` support was added in a specific PELDRUN version.
If your skill uses `sub_skills`, set `peldrun_min_version` to that version
or later. Otherwise, older deployments will install the skill and then fail
to dispatch its sub-skills.

---

## 13. Packaging for installation

### 13.1 ZIP layout

The ZIP **must contain** `manifest.json` and the instructions file at its
**root**. Do not wrap them in a directory.

**Simple skill:**

```
code-review.zip
├── manifest.json        ✓ at root
├── SKILL.md
└── references/
    └── checklist.md
```

**Router skill:**

```
code-review.zip
├── manifest.json
├── SKILL.md             # router body
└── sub_skills/
    ├── security_audit/
    │   └── SKILL.md
    └── correctness_analysis/
        └── SKILL.md
```

Not:

```
code-review.zip
└── code-review/         ✗ wrapping directory
    ├── manifest.json
    └── SKILL.md
```

### 13.2 Creating the ZIP

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

### 13.3 Validating before install

The install endpoint runs the full validator. To catch issues earlier:

```python
from omweb.tools.bundles.validator import validate_zip

report = validate_zip(Path("code-review.zip").read_bytes())
print(report.ok)
for err in report.errors:
    print("ERROR:", err)
```

To validate the manifest directly:

```python
import json
from omweb.skills.manifest import (
    validate_manifest_schema,
    _validate_sub_skills,
)

manifest = json.loads(Path("code-review/manifest.json").read_text())
validate_manifest_schema(manifest)
_validate_sub_skills(manifest)
print("Manifest OK")
```

### 13.4 Verifying sub-skill files exist

For router skills, verify every declared `instructions_file` is present in
the package and resolves inside it:

```python
import json
from pathlib import Path

pkg = Path("code-review")
manifest = json.loads((pkg / "manifest.json").read_text())

for sub in manifest.get("sub_skills", []):
    rel = sub["instructions_file"]
    target = (pkg / rel).resolve()
    target.relative_to(pkg.resolve())  # raises if traversal
    assert target.is_file(), f"Missing: {rel}"
    print(f"OK: {rel} ({target.stat().st_size} bytes)")
```

---

## 14. Testing your skill

### 14.1 Test 1: Install via HTTP

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

### 14.2 Test 2: Verify discovery

```python
r = client.get("/api/skills")
for s in r.json()["skills"]:
    print(
        s["id"],
        s["version"],
        s["status"],
        "router" if s["has_sub_skills"] else "",
    )
```

For a router skill, expect `router` in the output and
`sub_skills_count > 0`.

### 14.3 Test 3: Verify sub-skills are declared

```python
r = client.get("/api/skills/code_review/sub-skills")
data = r.json()
assert data["has_sub_skills"] is True
assert data["count"] == 4
for sub in data["sub_skills"]:
    print(sub["id"], sub["triggers"])
    assert sub["triggers"], "sub-skill has no triggers"
```

### 14.4 Test 4: Verify each sub-skill loads

```python
from omweb.skills.tools.load_sub_skill import execute

for sub_id in ["correctness_analysis", "security_audit",
                "error_handling_review", "maintainability_review"]:
    res = execute("code_review", sub_id)
    assert res["ok"] is True
    assert res["content"].startswith(
        f"--- BEGIN SUB-SKILL: code_review/{sub_id} ---"
    )
    print(f"OK: {sub_id} ({len(res['content'])} chars)")
```

### 14.5 Test 5: Verify the router body loads

```python
from omweb.skills.tools.load_skill import execute

res = execute("code_review")
assert res["ok"] is True
assert res["has_sub_skills"] is True
assert len(res["sub_skills"]) == 4
assert "Code Review Router" in res["content"]
print(f"OK: router body ({len(res['content'])} chars)")
```

### 14.6 Test 6: Verify BM25 ranking

```python
from omweb.skills.registry import skill_registry
from omweb.skills.router.bm25_router import BM25SkillRouter

router = BM25SkillRouter(skill_registry.list_skills())
for query in [
    "audit for SQL injection",
    "this function is too complex",
    "the retry logic swallows exceptions",
]:
    top = router.rank(query, top_k=1)
    assert top and top[0][0].id == "code_review", (query, top)
    print(f"OK: {query!r} -> {top[0][0].id}")
```

### 14.7 Test 7: Verify catalog block

```python
from omweb.skills.assignments import resolve_skill_injection_catalog

inj = resolve_skill_injection_catalog(
    agent_id="peldrun",
    invoked_skill_ids=[],
    user_message="audit this code for SQL injection",
)
assert "code_review" in inj.text
assert "security_audit" in inj.text
assert "load_sub_skill" in inj.text
print(f"OK: catalog block ({len(inj.text)} chars)")
```

### 14.8 Test 8: End-to-end in the UI

1. Open `/stores` → **Skills**.
2. Install your ZIP.
3. Confirm the card shows the **`ROUTER · N`** badge (violet).
4. Open the details drawer via **ⓘ** and confirm the sub-skills section
   lists every declared sub-skill.
5. Click the pencil on a sub-skill and confirm the editor opens with the
   correct body.
6. Save a small edit; confirm the toast says
   `Sub-skill updated — <name> › <sub_id> instructions saved.`
7. Open a chat with an agent. Send a prompt that matches a sub-skill.
8. In DevTools → Network → SSE stream, confirm the flow:
   - `TOOL_CALL load_skill(code_review)`
   - `TOOL_CALL load_sub_skill(code_review, <expected_sub>)`
   - `FINAL`

---

## 15. Best practices

### 15.1 Writing the manifest

- **Use a stable, specific ID.** `code_review`, not `review`. IDs are
  global — a collision with an existing skill forces you to rename.
- **Fill in `short_description`.** It is what the `/` menu shows.
- **Set `trust: "verified"` only if you have a publisher relationship
  with PELDRUN.**
- **Declare `compatibility.peldrun_min_version`.** Especially important for
  router skills (which require a version that supports `sub_skills`).
- **Prefer `icon_svg`** over `icon`. It removes a file from the package.
- **Keep `user_editable: true`** unless you have a reason to lock the
  skill. Operators appreciate being able to tweak prompts.

### 15.2 Writing SKILL.md (simple skills)

- **Open with a one-sentence purpose.**
- **Use `##` for major sections, `###` for sub-sections.**
- **Give a concrete output format.**
- **Keep it under 8 KiB if you can.**
- **Test with a small model.** If a 7B model can follow the instructions,
  a 70B will too.

### 15.3 Writing router skills

- **Keep the router body under 2 KiB.**
- **Use `### <sub_id>` headers** — the model parses them reliably.
- **State "Use when" in one line per sub-skill.**
- **Declare global output rules once** in the router body if all sub-skills
  share them.
- **Explicitly instruct ONE sub-skill** unless the request genuinely spans
  multiple aspects. `"Prefer ONE."` is a strong sentence for small models.

### 15.4 Writing sub-skills

- **Each sub-skill should stand alone.** A reader who loaded only that
  sub-skill (without the router context) should still be able to act on
  it.
- **Do not reference other sub-skills.** If two sub-skills must
  cooperate, that is a sign that the boundary is wrong.
- **Keep sub-skill bodies under 8 KiB.**
- **Use the same output format across sub-skills** if the router body
  declared a global one.

### 15.5 Versioning

- **Bump the patch version for every published change.**
- **Never delete a version from a published skill.**
- **Deprecate, don't remove.**

### 15.6 Testing

- **Test `/` invocation with a short prompt.**
- **Test agent assignment.** Some skills read better as ambient
  instructions than as explicit invocations.
- **Test the router dispatch across representative prompts.**
- **Verify BM25 ranking before publishing a router.** A router that does
  not rank correctly for its intended prompts is dead on arrival.

---

## 16. Complete worked example

The canonical router example shipped with PELDRUN is `code_review` v2.0.0.
Its full source lives under `backend/storage/store/skills/code_review/`.

### 16.1 Package tree

```
code_review/
├── manifest.json
├── SKILL.md                                       # router body
└── sub_skills/
    ├── correctness_analysis/SKILL.md              # 1,845 bytes
    ├── security_audit/SKILL.md                    # 1,953 bytes
    ├── error_handling_review/SKILL.md             # 2,022 bytes
    └── maintainability_review/SKILL.md            # 1,993 bytes
```

### 16.2 `manifest.json`

```json
{
  "schema_version": "1.0.0",
  "id": "code_review",
  "version": "2.0.0",
  "name": "Code Review",
  "display_name": "Code Review",
  "description": "Systematic code review with router-based sub-skill selection.",
  "short_description": "Route to the right review aspect — correctness, security, errors, or maintainability.",
  "store_description": "A router-based code review skill. The parent routes to one of four specialized sub-skills based on the user's request...",
  "tags": ["review", "audit", "security", "quality", "code", "router"],
  "author": {
    "name": "PELDRUN Team",
    "url": "https://peldrun.ai"
  },
  "publisher_id": "org.peldrun.official",
  "trust": "verified",
  "license": "MIT",
  "homepage": "https://peldrun.ai/skills/code_review",
  "release_date": "2026-11-08",
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
  "sub_skills": [
    {
      "id": "correctness_analysis",
      "description": "Identify logic bugs, edge cases, off-by-one errors, and incorrect control flow with cited evidence.",
      "triggers": ["bug", "logic", "off-by-one", "null", "undefined", "edge case", "crash", "correctness"],
      "instructions_file": "sub_skills/correctness_analysis/SKILL.md"
    },
    {
      "id": "security_audit",
      "description": "Identify security vulnerabilities — injection, secrets, auth bypass, unsafe deserialization.",
      "triggers": ["security", "injection", "xss", "sql", "auth", "token", "secret", "vulnerability"],
      "instructions_file": "sub_skills/security_audit/SKILL.md"
    },
    {
      "id": "error_handling_review",
      "description": "Review exception hierarchies, retry logic, fallbacks, and error propagation for robustness.",
      "triggers": ["exception", "error", "try", "catch", "retry", "fallback", "logging"],
      "instructions_file": "sub_skills/error_handling_review/SKILL.md"
    },
    {
      "id": "maintainability_review",
      "description": "Review code for clarity, coupling, duplication, naming, and future-change cost.",
      "triggers": ["refactor", "complexity", "naming", "duplication", "maintainability", "readability"],
      "instructions_file": "sub_skills/maintainability_review/SKILL.md"
    }
  ],
  "compatibility": {
    "peldrun_min_version": "0.2.0"
  },
  "limits": { "max_instructions_bytes": 262144 },
  "user_editable": true,
  "examples": [
    {
      "description": "Security-focused review",
      "invocation": "/code_review security audit on auth.py",
      "context": "User attached an authentication module.",
      "expected_outcome": "Router selects security_audit; findings grouped by severity with payloads."
    }
  ]
}
```

### 16.3 `SKILL.md` (router body)

```markdown
---
name: code_review
description: Systematic code review with router-based sub-skill selection.
---

# Code Review Router

You are a code review router. You have access to **four** specialized
review sub-skills. Read the user's request carefully and select the
**single most relevant** sub-skill (or more only if the request
genuinely spans multiple aspects).

## Sub-Skills

### 1. correctness_analysis
Use when: The user has code with potential **logic bugs** — off-by-one
errors, null/undefined handling, race conditions, edge cases, wrong
operators, resource leaks. The request is about "does this code do
what it claims?"

### 2. security_audit
Use when: The user mentions **security, injection, secrets, auth,
vulnerabilities, XSS, SQL injection, SSRF, path traversal**. The
request is about "can an attacker exploit this?"

### 3. error_handling_review
Use when: The code involves **try/catch, exception hierarchies,
retries, fallbacks, error propagation**. The request is about
"what happens when things go wrong?"

### 4. maintainability_review
Use when: The user asks about **refactoring, complexity, naming,
duplication, dead code, architectural clarity**. The request is
about "how hard will this be to change later?"

## How to Activate

Call `load_sub_skill(parent_id="code_review", sub_skill_id="<chosen>")`.

Do **NOT** try to load more than one sub-skill in the same turn unless
the user's request explicitly covers multiple aspects. Prefer ONE.

## Global Output Rules (apply to all sub-skills)

Every finding MUST include:

1. **Location**: `file:line` reference (or a quoted excerpt when no
   line number is available).
2. **Severity**: Critical / High / Medium / Low.
3. **Evidence**: a quoted excerpt from the code.
4. **Impact**: what goes wrong if not fixed.
5. **Fix**: a concrete code change (a diff or a minimal patch).

**Never report a finding without evidence.** If you cannot cite a
specific line or excerpt, do NOT report the finding.
```

### 16.4 Sub-skill excerpt (`security_audit/SKILL.md`)

```markdown
---
name: security_audit
description: Identify security vulnerabilities — injection, secrets, auth bypass, unsafe deserialization.
---

# Security Audit

You are reviewing code for **security**: can an attacker make this
code do something the developer did not intend?

## Threat Categories

1. **Injection** — SQL, command, LDAP, XPath, template, header injection.
2. **Secrets in code** — API keys, passwords, tokens, private keys.
3. **Authentication / authorization bypass** — missing checks, TOCTOU.
4. **Insecure deserialization** — `pickle.loads`, `yaml.load`, `eval`.
5. **Path traversal** — `..` or absolute paths in user input.
6. **Unsafe cryptography** — MD5 / SHA-1, ECB mode, hardcoded salts.
7. **SSRF** — user-controlled URLs without host validation.
8. **Insecure randomness** — `random` for security tokens.

## Method

For each candidate:

1. **Is the input attacker-controlled?**
2. **Does the code use it in a sink without proper sanitization?**
3. **Can you construct a concrete payload that demonstrates the issue?**

## Output Format

    ## [Severity] <Category>: <Short title>
    **Location**: `path/to/file.py:42`
    **Evidence**:
    ```
    <quoted code>
    ```
    **Attack**: <concrete payload or scenario>
    **Impact**: <what an attacker gains>
    **Fix**:
    ```
    <minimal patch>
    ```

## Rules

- **No theoretical findings.** Demonstrate the attack or drop it.
- Never suggest hardcoding a secret in source.
- For vulnerable dependencies, cite the CVE identifier.
- Prefer fixing at the sink (parameterized query) over the source.
```

### 16.5 Packaging and installing

```powershell
cd backend/storage/store/skills
Compress-Archive -Path ".\code_review\*" -DestinationPath ".\code_review.zip" -Force

curl -X POST http://127.0.0.1:8088/api/skills/install `
  -F "file=@code_review.zip"
```

Or drag the ZIP into `/stores` → Skills → **Install Skill**.

### 16.6 Verifying the example

```python
from fastapi.testclient import TestClient
from omweb.main import app

client = TestClient(app)

# Router is registered
r = client.get("/api/skills/code_review").json()
assert r["skill"]["has_sub_skills"] is True
assert r["skill"]["sub_skills_count"] == 4
assert "instructions_file" not in r["skill"]["sub_skills"][0]

# Sub-skills list
r = client.get("/api/skills/code_review/sub-skills").json()
assert r["count"] == 4
assert {s["id"] for s in r["sub_skills"]} == {
    "correctness_analysis", "security_audit",
    "error_handling_review", "maintainability_review",
}

# Tools work
from omweb.skills.tools.load_skill import execute as load_skill
from omweb.skills.tools.load_sub_skill import execute as load_sub

res = load_skill("code_review")
assert res["ok"] and res["has_sub_skills"] and len(res["sub_skills"]) == 4

res = load_sub("code_review", "security_audit")
assert res["ok"] and "Security Audit" in res["content"]

print("All checks passed")
```

---

## 17. Troubleshooting

### 17.1 `SKILL_MANIFEST_INVALID` on install

The `manifest.json` failed schema validation. The error response contains a
`details.path` array pinpointing the failing field.

| Symptom | Fix |
|---------|-----|
| `id must match ^[a-z0-9][a-z0-9_]{2,63}$` | Use lowercase, digits, underscores, start with letter/digit, ≥3 chars. |
| `schema_version unsupported major` | Use a `1.x.y` string. |
| `missing required field: execution` | Add the `execution` object. |
| `execution.type must be 'instructions'` | Only `"instructions"` is accepted in this release. |

### 17.2 `SKILL_MANIFEST_INVALID` — sub-skill specific errors

| Symptom | Fix |
|---------|-----|
| `sub_skills[0].id must match …` | Same rules as the parent `id`. |
| `Duplicate sub_skill id: 'x'` | Two entries share the same ID. |
| `sub_skills[0].description must be 10–500 chars` | Too short or too long. |
| `sub_skills[0].instructions_file must match 'sub_skills/...'` | Must start with `sub_skills/` and end with `.md`. |
| `sub_skills[0].instructions_file must not contain '..'` | Remove traversal segments. |
| `Manifest declares N sub-skills; max is 50` | Split the parent into multiple routers. |

### 17.3 `SKILL_VALIDATION_FAILED` on install

Structural issues:

| Symptom | Fix |
|---------|-----|
| `missing required file: manifest.json at zip root` | Zip the **contents**, not the wrapping folder. |
| `Skill package is missing SKILL.md` | Ensure the file named in `execution.instructions_file` exists at the package root. |
| `instructions_too_large` | Reduce `SKILL.md` size or raise `limits.max_instructions_bytes`. |
| `Missing '---' YAML frontmatter` | `SKILL.md` must start with `---` on line 1. |

### 17.4 `SKILL_SECURITY_VIOLATION` on install

A forbidden element, attribute, or value was detected. The error names it
precisely.

| Symptom | Fix |
|---------|-----|
| `svg_forbidden_element: script` | Remove `<script>` from `icon_svg`. |
| `svg_external_url` | All `url(...)` refs must be `#local-id`. |
| `svg_too_large` | Icon > 32 KiB. Simplify or use a file. |
| `forbidden file type: *.exe` | Remove the executable from the package. |

### 17.5 Skill installs but doesn't appear in `/`

- Check `capabilities.chat_invocable` is `true`.
- Check `stability !== "deprecated"` and `deprecated !== true`.
- Check status is `"active"` (not disabled) in `/stores` → Skills.
- Reload the page.

### 17.6 Router installs but the model never calls `load_sub_skill`

- **The message does not clearly match a sub-skill.** Try a more specific
  prompt: `"audit for SQL injection"` instead of `"review this"`.
- **The router body does not list the sub-skills clearly.** Make sure
  each sub-skill has a `### <id>` section with a "Use when:" line.
- **The model is too small.** Sub-7B models sometimes ignore the dispatch
  instruction. Try a larger model.
- **The `## How to Activate` section is missing or vague.** Explicitly name
  `load_sub_skill` and show the call signature.

### 17.7 `load_sub_skill` returns `SUB_SKILL_NOT_FOUND`

The model passed a `sub_skill_id` that does not match any declared ID. The
error response includes an `available` list. This is usually the model's
fault (hallucinated ID), not the manifest's.

If it repeats across runs:

- Shorten the sub-skill IDs (long IDs are harder for the model to copy
  verbatim).
- Improve the "Use when:" lines in the router body.
- Include a `## Sub-Skills` section that lists the IDs prominently.

### 17.8 `load_sub_skill` returns `SKILL_SECURITY_VIOLATION`

The resolved file path escapes the parent package. Check the backend log
for the exact `rel` path. This should only happen if:

- The manifest was hand-edited after install.
- A test harness mutated `record.sub_skills` in memory.

### 17.9 Sub-skill body is not being respected by the model

- **The sub-skill body is too vague.** "Check for bugs" is weaker than a
  numbered checklist.
- **The sub-skill body is too long.** Trim below 8 KiB.
- **The router body's dispatch instruction was ignored.** Make sure
  `## How to Activate` names `load_sub_skill` explicitly.
- **The sub-skill's output format is too complex.** Simplify or remove.

### 17.10 BM25 ranks an unrelated skill higher than the router

Two common causes:

1. **The unrelated skill's description contains a keyword from the user's
   query.** Add a trigger to your router's most relevant sub-skill that
   matches the query term more specifically.
2. **The router's sub-skill triggers overlap with unrelated skills' tags.**
   Trim overlapping triggers or use more distinctive vocabulary.

Use the diagnostic script from §11.5 to see the actual rankings.

### 17.11 `PUT .../sub-skills/.../instructions` returns 403

The parent skill is not `user_editable`. Built-in skills and skills shipped
with `user_editable: false` cannot be edited from the UI. Reinstall the
package with `user_editable: true` if you authored it.

### 17.12 Editing a sub-skill silently fails with 422

The body being saved fails one of:

- Empty body.
- Body starts with `---` (frontmatter duplication).
- Body exceeds `limits.max_instructions_bytes`.
- The recomposed file (frontmatter + new body) fails `parse_skill_md`.

Check the response's `details.reason` field for the exact cause.

### 17.13 `peldrun_min_version` rejects a valid install

- Check `peldrun.__version__` on the host:
  `python -c "import peldrun; print(peldrun.__version__)"`.
- If the field is unset (development checkout), the check is skipped
  automatically.

### 17.14 Sub-skill file is missing after install

Two causes:

1. **The `instructions_file` path was mistyped** in the manifest. Verify
   each declared path resolves to a real file inside the package before
   zipping.
2. **The ZIP was created with a wrapping directory.** The `sub_skills/`
   tree must be at the package root, not under an extra folder.

---

## Summary checklist

Before publishing a **simple** skill, verify:

- [ ] `manifest.json` parses as JSON and passes the schema.
- [ ] `id` is lowercase, ≥3 chars, starts with letter/digit.
- [ ] `version` is semver (`MAJOR.MINOR.PATCH`).
- [ ] `execution.type` is `"instructions"`.
- [ ] `SKILL.md` starts with `---` YAML frontmatter.
- [ ] The instructions file exists at the package root.
- [ ] `capabilities` reflects the intended invocation surfaces.
- [ ] `tags` include terms a user might type in `/`.
- [ ] If an icon is declared, it passes the SVG sanitizer.
- [ ] The package zips **contents**, not the folder.
- [ ] Install succeeds via `POST /api/skills/install`.
- [ ] The skill appears in `GET /api/skills`.
- [ ] `/` invocation works in the chat composer.
- [ ] Assignment works via `/stores` → Agents → Skills.

Before publishing a **router** skill, additionally verify:

- [ ] `sub_skills` array is present and non-empty.
- [ ] Every sub-skill `id` is unique, matches `^[a-z0-9][a-z0-9_]{2,63}$`.
- [ ] Every sub-skill `description` is 10–500 chars and disambiguates from
      siblings.
- [ ] Every sub-skill has at least one `trigger`.
- [ ] Every `instructions_file` exists under `sub_skills/…` and ends in
      `.md`.
- [ ] The router body lists each sub-skill with a `### <id>` header and a
      "Use when:" line.
- [ ] The router body's `## How to Activate` section explicitly names
      `load_sub_skill(parent_id, sub_skill_id)`.
- [ ] The router body is under 2 KiB.
- [ ] Each sub-skill body is under 8 KiB.
- [ ] `GET /api/skills/{id}/sub-skills` returns all sub-skills with
      triggers.
- [ ] `load_skill(id)` returns `has_sub_skills: true` and the correct
      `sub_skills` array.
- [ ] `load_sub_skill(id, sub_id)` succeeds for every declared ID.
- [ ] BM25 ranks the router correctly for representative queries.
- [ ] An end-to-end agent run shows `load_skill` → `load_sub_skill` in the
      SSE stream.

Once all applicable boxes are checked, the skill is ready to publish.
