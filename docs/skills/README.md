```markdown
# PELDRUN Skills

Skills are self-contained, versioned instruction packs that augment an
agent's system prompt with domain-specific guidance. They are **instructional
only** — they never execute code, never access the network, and never write
to the filesystem beyond reading their own package files. Their purpose is to
encode a repeatable workflow (a code-review checklist, a test-planning
procedure, a security-audit routine) so that both chat invocations and agent
assignments produce consistent, high-quality output.

This document is the operator reference: what a skill is, how it is installed,
managed, invoked, and diagnosed. For the authoring guide (how to write your
own skill package), see [creating-skills.md](./creating-skills.md).

---

## Table of Contents

1. [Concept](#1-concept)
2. [Where skills are used](#2-where-skills-are-used)
3. [Two ways to invoke a skill](#3-two-ways-to-invoke-a-skill)
4. [Package layout](#4-package-layout)
5. [Installing a skill](#5-installing-a-skill)
6. [Managing installed skills](#6-managing-installed-skills)
7. [Assigning skills to agents](#7-assigning-skills-to-agents)
8. [Invoking a skill from chat](#8-invoking-a-skill-from-chat)
9. [Runtime injection](#9-runtime-injection)
10. [Lifecycle states](#10-lifecycle-states)
11. [Storage layout](#11-storage-layout)
12. [REST API reference](#12-rest-api-reference)
13. [Security model](#13-security-model)
14. [Troubleshooting](#14-troubleshooting)
15. [Reference example](#15-reference-example)

---

## 1. Concept

A skill answers the question: **"How should the model approach this class of
task?"**

- A **tool** answers: *"What can the model do?"* (read a file, fetch a URL,
  run a shell command, query an MCP server.)
- A **skill** answers: *"How should the model do it?"* (a checklist, a
  reasoning frame, an output format, a set of domain-specific heuristics.)

The two are complementary. A skill like `code_review` does not add a new
capability — it changes the way the model applies its existing capabilities
(file_reader, bash, str_replace_editor) to the task of reviewing code.

**Key properties:**

- **Instructional only.** Skills cannot execute code in the current release.
  The manifest schema accepts only `execution.type = "instructions"`.
- **Reusable.** The same skill works from chat (`/code_review`), from any
  agent it is assigned to, and (in a future release) from a Workflow node.
- **Safe by default.** A skill that fails to load is skipped with a warning.
  A broken skill can never crash a chat.
- **Deterministic.** When multiple skills are active, they are injected in
  a stable, alphabetized order so prompt caching and reproducibility hold.

---

## 2. Where skills are used

Skills show up in three surfaces:

| Surface | Purpose | Reach |
|---------|---------|-------|
| **Chat composer (`/`)** | One-shot invocation for the current message | Frontend `/` menu → `invoked_skills` in the run payload |
| **Agent assignment** | Ambient instruction loaded on every run of an agent | `/stores` → Agents → ✨ Skills modal → `agent_settings.json` |
| **Store UI (`/stores/skills`)** | Install, enable, disable, delete, preview | Direct HTTP against `/api/skills/*` |

The two invocation channels — chat `/` and agent assignment — are **merged
at run time**. If the same skill is both assigned and invoked, it is injected
once, with the source marked `"invoked"` (the user's explicit choice for this
turn wins over the agent's ambient assignment).

---

## 3. Two ways to invoke a skill

### 3.1 From chat: `/skill_id`

The chat composer (`chat-landing.tsx` on the home page, `composer.tsx` in an
existing chat) exposes every `chat_invocable` skill as a slash command.

```
Type:    /code_review
Shows:   ✨ /code_review  —  Systematic code review checklist
Pick:    A chip appears above the editor
Send:    The run payload includes invoked_skills=["code_review"]
```

- **Search works.** The `/` menu matches against skill id, display name,
  description, and tags. Typing `/review` finds `code_review` because its
  tags include `review`. Typing `/code-review` also works (hyphens are
  normalized to underscores).
- **Multiple skills per message.** Pick several; all are injected.
- **Disabled skills are hidden.** If an operator disables a skill, it does
  not appear in `/` until re-enabled.

### 3.2 From an agent: assignment

Open `/stores` → **Agents** → the ✨ icon on any agent card.

```
Manifest Skills:  [x] code_review     ← in the agent's manifest
                  [ ] test_planning   ← also in the manifest
Available Store:  [x] security_audit  ← added via extra_skills
Effective:        code_review, test_planning, security_audit
```

Assignments are stored in `backend/storage/store/agents/agent_settings.json`
under the agent's id, alongside `disabled_tools` and `disabled_mcp_servers`.
This keeps one source of truth per agent.

- **Assignable** requires `capabilities.agent_assignable = true` and
  `status = "active"` in the skill.
- **Deprecated** skills cannot be assigned.
- **Locked** agents (with `settings.skills_editable = false`) cannot be
  modified — the UI disables the save button and the backend returns 403.

---

## 4. Package layout

A skill package is a directory containing `manifest.json` and the instructions
file (`SKILL.md` by default), both at the package root:

```
code-review/
├── manifest.json                # required
├── SKILL.md                     # required (or the file named in execution.instructions_file)
├── icon.svg                     # optional (referenced by manifest.icon)
├── references/                  # optional (loaded on demand)
│   ├── checklist.md
│   └── style-guide.md
└── assets/                      # optional (not loaded by the runtime)
    └── example.png
```

**Rejected at install time:**

- Any wrapping directory (`code-review-v1/code-review/manifest.json` fails).
- Executables (`.exe`, `.dll`, `.so`, `.dylib`).
- Bytecode (`.pyc`, `.pyo`, `.pyd`).
- Symlinks.
- `.git/`, `.svn/`, `node_modules/`, `.venv/`.
- Files > 5 MiB each; archives > 100 MiB uncompressed.
- More than 500 files; path depth > 10.

The full validation rules are documented in
[creating-skills.md](./creating-skills.md#2-package-structure).

---

## 5. Installing a skill

### 5.1 From the Store UI

1. Open `/stores`.
2. Click **Skills** in the sidebar.
3. Click **Install Skill (.zip)** in the header (or the button in the empty
   state).
4. Pick a `.zip` package.
5. Click **Install Skill**.

You will see:

- **"Skill Installed"** — first-time install.
- **"Skill Updated"** — the same skill ID was already present and the new
  version replaced it.
- **"Skill Install Notice"** — a non-fatal warning from the installer (for
  example, the `id` field was normalized from `code-review` to `code_review`).

### 5.2 From the API

```bash
curl -X POST http://127.0.0.1:8088/api/skills/install \
  -F "file=@code-review.zip"
```

Response (success):

```json
{
  "status": "success",
  "skill": {
    "id": "code_review",
    "version": "1.0.0",
    "name": "Code Review",
    "status": "active",
    ...
  },
  "was_upgrade": false,
  "warnings": []
}
```

Response (rejection):

```json
{
  "detail": {
    "error": "Skill archive failed structural validation.",
    "code": "SKILL_VALIDATION_FAILED",
    "details": {
      "errors": ["missing required file: manifest.json at zip root"]
    }
  }
}
```

The full error code list is in [§14](#14-troubleshooting).

### 5.3 What happens on disk

1. The ZIP is validated (structure + security) in memory.
2. Contents are extracted to a staging directory
   (`backend/storage/store/skills/.tmp_<uuid>/`).
3. `manifest.json` is parsed and normalized: `id` is lowercased,
   hyphens/dots → underscores, invalid characters removed.
4. `SKILL.md` frontmatter is parsed.
5. `icon_svg` (if present) is sanitized.
6. `peldrun_min_version` / `peldrun_max_version` are checked against the
   running `peldrun.__version__`.
7. The staging directory is atomically renamed to
   `backend/storage/store/skills/<id>/`. Any existing package with the same
   ID is moved to `.trash_<uuid>/` first (recoverable if the rename fails).
8. The registry is refreshed so the skill appears immediately.

### 5.4 Idempotency

Re-uploading the **same** ZIP (same `id`, same `version`, same content hash):

- Returns `was_upgrade: false`.
- Does **not** touch the filesystem.
- Does **not** touch `skills_state.json`.

Re-uploading a **different** ZIP with the **same** `version`:

- Returns `was_upgrade: true` and emits a warning asking you to bump the
  version next time.
- Replaces the previous package.

This is useful during development: iterate on a `1.0.0` package without
spamming version bumps. For anything published, always bump.

---

## 6. Managing installed skills

Open `/stores` → **Skills**. Each row is one installed skill.

### Row anatomy

```
[icon]  Code Review                                    ⓘ  🗑  ●
        CUSTOM  ACTIVE  ✓  v1.0.0
        Find real defects with evidence, ordered by severity.
        [review] [audit] [security] [quality] [code]
        ✨ invoke /code_review   🤖 agent-assignable
```

| Element | Meaning |
|---------|---------|
| **icon** | The skill's icon (`icon_svg` or `icon` file), or a Sparkles glyph if none. |
| **BUILT-IN / CUSTOM** | Whether the skill ships with PELDRUN or was installed by the user. |
| **ACTIVE / DISABLED** | The global activation state. |
| **✓ shield** | `trust: "verified"` in the manifest. |
| **v1.0.0** | The installed version. |
| **tags** | From `manifest.tags` — used by the `/` menu search. |
| **✨ invoke /id** | The skill is `chat_invocable`. |
| **🤖 agent-assignable** | The skill is `agent_assignable`. |
| **ⓘ** | Open the details drawer (instructions preview, metadata, examples). |
| **🗑** | Delete the package (only for custom skills). |
| **● toggle** | Enable / disable globally. |

### Actions

| Action | Effect |
|--------|--------|
| **Toggle off** | The skill is hidden from `/` and from agent injection. Package files remain. |
| **Toggle on** | The skill returns to `/` and to agent injection. |
| **Details (ⓘ)** | Read-only drawer with the SKILL.md body, metadata, tags, examples, and links. |
| **Delete (🗑)** | Removes the package files from disk permanently. Custom skills only. |

Every action that mutates state fires a `peldrun:skills-changed` browser event
so open chat sessions immediately refresh their `/` menu without a reload.

---

## 7. Assigning skills to agents

Open `/stores` → **Agents** → the ✨ icon on any agent card.

The modal has three sections:

### 7.1 Manifest Skills

Skills declared in the agent's manifest (`skills: [...]` in the agent's
`manifest.json`). Each row is a checkbox:

- **Checked** → the skill is active for this agent.
- **Unchecked** → the skill is added to `disabled_skills` in
  `agent_settings.json`, so the runtime skips it.

Rows that reference a skill ID which is no longer installed render with a
`not in registry` badge. Clicking them still works (adds to `disabled`), so
you can clean up stale references.

### 7.2 Available Store Skills

Skills that are installed and not already in the agent's manifest. Each row
is a checkbox:

- **Checked** → the skill is added to `extra_skills`.
- **Unchecked** → the skill is not used by this agent (default).

**Blocked rows** (disabled, deprecated, or `agent_assignable: false` in the
skill manifest) appear but cannot be selected. The tooltip explains why.

### 7.3 Effective Preview

A live preview of the runtime skill list:

```
Effective:  [✨ code_review] [✨ security_audit] [✨ test_planning]
```

This is what the engine will inject. It updates as you toggle checkboxes.

### 7.4 Where the state is stored

All assignments live in
`backend/storage/store/agents/agent_settings.json`:

```json
{
  "code_architect": {
    "disabled_skills": ["test_planning"],
    "extra_skills":    ["code_review"],
    "disabled_tools":  [],
    "extra_tools":     []
  }
}
```

One source of truth per agent. Tool overrides, MCP overrides, and skill
overrides coexist in the same record; updating one does not erase the others.

### 7.5 Locking an agent's skills

An agent whose manifest contains:

```json
"settings": { "skills_editable": false }
```

cannot have its skills modified. The Skills modal renders read-only (save
button disabled, checkboxes grayed out), and `PUT /api/store/agents/{id}/skills`
returns **403 Forbidden**.

---

## 8. Invoking a skill from chat

### 8.1 The `/` menu

In the chat composer, type `/`:

```
Slash Commands
─────────────────────────────────────
📟 /clear                Reset conversation
🤖 /agent                Switch to Agent mode
💬 /chat                 Switch to Chat mode
✨ /code_review          Systematic code review checklist
✨ /test_planning        Test-planning workflow
```

The menu:

- **Filters as you type** — `/cod` narrows to `/code_review`.
- **Matches tags** — `/review` finds `code_review` via its `review` tag.
- **Matches display names** — `/audit` finds `code_review` (tag), and
  `/code` matches the id prefix.
- **Normalizes hyphens** — `/code-review` resolves to `code_review`.
- **Ranks results** — label prefix > tag prefix > contains.

### 8.2 The skill chip

Selecting a skill does not insert text into the editor. It adds a chip above
the editor:

```
✨ Skills   [✨ Code Review ×]   [✨ Test Planning ×]
```

- **×** removes the skill from the current message only.
- The chips clear automatically on send.
- Multiple skills can be stacked per message.

### 8.3 What the model receives

The system prompt gains a block at the end:

```
[SKILL INSTRUCTIONS]
--- BEGIN SKILL: code_review@1.0.0 ---
[Metadata: name="Code Review", tags=[review, audit, security]]
<the SKILL.md body, verbatim>
--- END SKILL: code_review@1.0.0 ---
```

If multiple skills are active, they are concatenated in the order described
in [§9](#9-runtime-injection).

### 8.4 Cancelling a skill mid-message

Click the **×** on the chip. The skill is not injected for that message, but
the assignment (if any) remains in place for future messages.

---

## 9. Runtime injection

When the engine (`backend/omweb/engines/peldrun_engine.py`) starts a run, it
calls `omweb.skills.assignments.resolve_skill_injection` with:

- `agent_id` — the agent's id.
- `invoked_skill_ids` — the list from the payload's `invoked_skills` field
  (populated by the dispatcher from the chat composer).

### Merge algorithm

```
1. Read assigned skills:
      effective_skills = (manifest.skills − disabled_skills) ∪ extra_skills

2. Normalize invoked skills:
      lowercase → underscores → deduplicate

3. Merge with invoked winning:
      for each id in invoked: source[id] = "invoked"
      for each id in assigned and not in invoked: source[id] = "assigned"

4. Order deterministically:
      invoked skills alphabetized first,
      then assigned-not-already-invoked alphabetized.

5. Load each skill:
      for each id in order:
          record = registry.get_skill(id)
          if not record or not record.enabled: skip
          if source == "invoked" and not chat_invocable: skip
          if source == "assigned" and not agent_assignable: skip
          block = loader.get_injection_block(record)
          append to output
```

Failures at step 5 are logged as warnings and the skill is skipped. The run
continues with the surviving skills.

### What gets injected

The concatenated blocks are prepended to the system prompt under a
`[SKILL INSTRUCTIONS]` header, after:

- the agent's `system_prompt`,
- `[OPERATIONAL DIRECTIVE]`,
- `[MCP SERVER ACCESS]` (if MCP servers are active),
- the Generative UI directive (if enabled).

The skill content lands **last** so the model reads it with full context of
its operational rules and available tools.

### Context budget

The skills block counts against the model's context window like any other
system prompt content. On large skills, this can squeeze out recent dialogue.
The `limits.max_instructions_bytes` cap (default 256 KiB) is a hard ceiling;
the practical recommendation is to keep `SKILL.md` under 8 KiB.

### Diagnostics

The runtime metadata includes the injection result:

```json
{
  "injected_skills": ["code_review", "test_planning"],
  "skill_warnings": ["Skill 'stale_id' is not installed; skipping."]
}
```

These appear in the run's diagnostic log and in the run timeline (if enabled).

---

## 10. Lifecycle states

A skill has **three independent** boolean states. Do not conflate them:

| State | Storage | Meaning |
|-------|---------|---------|
| **Installed** | Package on disk | The skill files exist and passed install validation. |
| **Enabled** | `skills_state.json` | The skill may be used at runtime. |
| **Assigned** | `agent_settings.json` | The skill is available to a specific agent. |

### State matrix

| Installed | Enabled | Assigned | `/` menu | Agent injection |
|-----------|---------|----------|----------|-----------------|
| ✓ | ✓ | — | ✓ | — |
| ✓ | ✓ | ✓ | ✓ | ✓ |
| ✓ | — | ✓ | — | — (skipped silently) |
| ✓ | — | — | — | — |
| — | — | ✓ (stale) | — | — (warning logged) |

### Deprecation overrides

A skill with `stability = "deprecated"` or `deprecated = true` is treated
as if `chat_invocable = false` and `agent_assignable = false`, regardless of
the declared `capabilities`. It remains installed and enableable but is
invisible to both runtime channels.

### Deleting versus disabling

| Operation | Files removed | State cleared | Reversible |
|-----------|---------------|---------------|------------|
| **Disable** (toggle off) | No | Status → `"disabled"` | Yes (toggle on) |
| **Delete** | Yes | Removed from registry; state entry pruned on next discovery | No |
| **Deprecate** (author action) | No | Flags set in manifest | Yes (author action) |

---

## 11. Storage layout

```
backend/storage/store/skills/
├── skills_state.json                # global activation state
├── code_review/
│   ├── manifest.json
│   ├── SKILL.md
│   ├── .installed.json              # install metadata (content hash, source)
│   ├── references/
│   └── assets/
├── test_planning/
│   └── ...
└── .tmp_<uuid>/                     # transient staging (cleaned on startup)
```

### `skills_state.json`

```json
{
  "schema_version": 1,
  "updated_at": "2025-01-15T10:30:00+00:00",
  "states": {
    "code_review": "active",
    "test_planning": "disabled"
  }
}
```

Only skills present on disk appear in `states`. Orphan entries are pruned
automatically on the next discovery pass.

### `.installed.json`

```json
{
  "schema_version": 1,
  "skill_id": "code_review",
  "version": "1.0.0",
  "name": "Code Review",
  "description": "Systematic code review for bugs, security, and maintainability.",
  "content_hash": "sha256:abc123def4567890",
  "installed_at": "2025-01-15T10:30:00+00:00",
  "source_filename": "code-review-v1.0.0.zip",
  "size_bytes": 15234,
  "file_count": 8,
  "original_id": "code-review"
}
```

`original_id` preserves the pre-normalization id (useful for tracing
back to the source repository).

### Crash recovery

If the process dies mid-install:

- `.tmp_<uuid>/` — staging directory; cleaned on next startup or next install.
- `.trash_<uuid>/` — previous package moved aside during upgrade; restored
  to its original name on the next commit attempt, or cleaned up.

Both are handled by `SkillManager.cleanup_orphans()`, called in the FastAPI
lifespan handler and before every install.

---

## 12. REST API reference

All routes are registered under `/api/skills` (see
`backend/omweb/routers/skills.py`). Errors follow the shape
`{ "detail": { "error": str, "code": str, "details": {} } }`.

### `GET /api/skills`

List installed skills.

**Query params:**

| Param | Default | Description |
|-------|---------|-------------|
| `search` | — | Free-form query. Matches id, name, display_name, tags, description. |
| `include_disabled` | `true` | Include `status = "disabled"` skills. |
| `include_deprecated` | `true` | Include skills marked deprecated. |
| `include_broken` | `false` | Include packages that failed validation (in a separate `broken` map). |
| `limit` | `100` | Max results (search mode only). |

**Response:**

```json
{
  "status": "success",
  "skills": [ { ...SkillSummary... } ],
  "total": 1,
  "broken": { "old_skill": "Manifest 'id' invalid..." }
}
```

### `GET /api/skills/{skill_id}`

Full detail: summary fields + `manifest` + `frontmatter`.

### `GET /api/skills/{skill_id}/icon.svg`

Returns `image/svg+xml`. The content is sanitized server-side. Cached for
1 hour.

Returns 404 if the skill declares no icon.

### `GET /api/skills/{skill_id}/preview`

Returns the instructions text for the details drawer:

```json
{
  "status": "success",
  "skill_id": "code_review",
  "version": "1.0.0",
  "instructions": "...",
  "instructions_length": 3326,
  "frontmatter": { "name": "code_review", ... }
}
```

### `POST /api/skills/install`

`multipart/form-data` with a `file` field containing the `.zip`.

Returns 200 with `{ status, skill, was_upgrade, warnings }` on success, or
4xx with the structured error on rejection.

### `POST /api/skills/{skill_id}/toggle`

Flips the status between `active` and `disabled`. Returns the updated
`SkillSummary`.

### `DELETE /api/skills/{skill_id}`

Deletes the package from disk. Returns `{ status: "success", deleted: true }`.

### Agent assignments

Under `/api/store/agents`:

### `GET /api/store/agents/{agent_id}/skills`

```json
{
  "status": "ok",
  "manifest_skills": [],
  "disabled_skills": [],
  "extra_skills": ["code_review"],
  "effective_skills": ["code_review"],
  "skills_editable": true,
  "resolved_skills": [
    {
      "id": "code_review",
      "resolved": true,
      "name": "Code Review",
      "version": "1.0.0",
      "enabled": true,
      "is_deprecated": false,
      "agent_assignable": true
    }
  ],
  "available_skills": [ ...SkillSummary-lite... ]
}
```

### `PUT /api/store/agents/{agent_id}/skills`

Body: `{ "disabled_skills": [...], "extra_skills": [...] }`.

Validation:

- Every id must resolve to an installed skill.
- Every `extra_skills` entry must be `enabled`, `agent_assignable`, and not
  `deprecated`.
- The agent must have `skills_editable = true` (else 403).

---

## 13. Security model

### Threat: malicious ZIP contents

The validator rejects:

- **Path traversal**: any `..` segment or absolute path inside the archive.
- **Symlinks**: extracted entry that points elsewhere on the filesystem.
- **Forbidden file types**: `.exe`, `.dll`, `.so`, `.dylib`, bytecode, VCS
  metadata, env folders.
- **Zip bombs**: per-file cap (5 MiB), total cap (100 MiB), entry count cap
  (500), depth cap (10).

All validations run **before any byte is written to disk**. The validator is
shared with the tool bundles subsystem (`omweb.tools.bundles.validator`) and
has been the test target of `test_archive_security.py` since that subsystem
shipped.

### Threat: malicious SVG in `icon_svg`

The sanitizer (`omweb.skills.manifest.sanitize_svg`) enforces:

- **Element whitelist** (no `script`, `foreignObject`, `use`, `image`,
  `style`, `animate*`).
- **Attribute whitelist** (no `on*`, no `href`, no `xlink:href`, no `style`).
- **Value blacklist** (`javascript:`, `vbscript:`, `data:`, `@import`,
  `expression(`).
- **Local-only `url(#id)` references.**
- **Size caps** (32 KiB, 200 elements, depth 8).
- **ID prefixing** to prevent CSS collisions with the host page.

The sanitized SVG is what gets served at `/api/skills/{id}/icon.svg`; the
client does a second defense-in-depth pass.

### Threat: prompt injection via `SKILL.md`

A skill's body is **injected into the system prompt**. This is by design —
the operator is expected to trust installed skills the way they trust
installed tools.

Mitigations:

- **Only operators can install.** There is no auto-discovery from public
  sources. Every install is an explicit, audited action.
- **`trust` field** distinguishes verified publishers from community
  packages. The shield badge is visible in the store card.
- **`signature` field** (optional) records a content hash; future releases
  will verify it against a trusted key.
- **The skill is delimited.** The injected text is wrapped in
  `--- BEGIN SKILL: id@version ---` / `--- END SKILL: id@version ---` so the
  model can distinguish instructions from user content.

### Threat: code execution

**Not possible in this release.** `execution.type` accepts only
`"instructions"`; there is no handler execution, no shell, no Python
import. Adding `handler.py` support is a planned future feature that will
require a separate approval workflow.

### Threat: resource exhaustion

The `limits.max_instructions_bytes` cap, the `MAX_REFERENCE_BYTES` per-file
cap, and the `MAX_TOTAL_REFERENCES_BYTES` cap prevent a skill from consuming
unbounded disk or context. A skill that fails these checks is skipped at load
time; the run continues.

---

## 14. Troubleshooting

### The `/` menu does not show a skill I installed

1. `GET /api/skills` — is it present?
   - If yes → continue.
   - If no → see "Install rejects" below.
2. `GET /api/skills/{id}` — check `enabled`:
   - `false` → toggle it on in `/stores` → Skills.
3. Check `chat_invocable` is `true` (should be unless the manifest sets it
   to `false`).
4. Check `is_deprecated` is `false`.
5. Reload the chat page. The `/` menu refreshes on `peldrun:skills-changed`;
   if the event fired before the page loaded, a reload is needed.

### Install rejects with `SKILL_MANIFEST_INVALID`

The `manifest.json` fails schema validation. The error response's
`details.path` points to the failing field. Common causes:

| Error | Fix |
|-------|-----|
| `id must match ^[a-z0-9][a-z0-9_]{2,63}$` | Use lowercase, digits, underscores, start with letter/digit, ≥3 chars. |
| `schema_version major must be 1` | Use a `1.x.y` string. |
| `execution.type must be 'instructions'` | Only `"instructions"` is accepted. |

### Install rejects with `SKILL_VALIDATION_FAILED`

Structural problems with the package itself:

| Error | Fix |
|-------|-----|
| `missing required file: manifest.json at zip root` | Zip the contents, not the wrapping folder. |
| `Skill package is missing SKILL.md` | Ensure the file named in `execution.instructions_file` exists at root. |
| `instructions_too_large` | Reduce `SKILL.md` or raise `limits.max_instructions_bytes`. |
| `Missing '---' YAML frontmatter` | `SKILL.md` must start with `---` on line 1. |

### Install rejects with `SKILL_SECURITY_VIOLATION`

A dangerous element, attribute, value, or file type was found. The error
names the specific violation:

| Error | Fix |
|-------|-----|
| `svg_forbidden_element: script` | Remove `<script>` from `icon_svg`. |
| `svg_external_url` | All `url(...)` refs must be `#local-id`. |
| `forbidden file type: *.exe` | Remove executables from the package. |

### The skill is injected but the model ignores it

- **Instructions too long.** Trim to the essential checklist. Aim for
  < 8 KiB.
- **Instructions too vague.** "Check for bugs" is weaker than a numbered
  list of specific checks.
- **Try `/` invocation instead of assignment.** Some models weight explicit
  user-triggered skills more than ambient ones.
- **Try a larger model.** Long system prompts are the first casualty on
  small models.

### The `/` menu shows a skill that I deleted

A stale cache in the browser. Reload the page. The `peldrun:skills-changed`
event fires after deletion, but if a tab was open in the background when
the delete happened, its event may have been missed. A reload fixes it.

### `PUT /api/store/agents/{id}/skills` returns 403

The agent's manifest has `settings.skills_editable: false`. This is a
deliberate lock. If the agent is custom, edit its manifest (via the
AgentBuilderModal) to remove the flag. If the agent is built-in and locked,
you cannot assign skills to it — this is the intended behavior.

### "This skill is not agent-assignable"

The manifest has `capabilities.agent_assignable: false` (or the skill is
deprecated). Either use it from `/` only, or edit the manifest (if you
author the skill) to allow assignment.

---

## 15. Reference example

A complete, working skill is included at
[`examples/code-review/`](./examples/code-review/). It demonstrates:

- All the manifest fields that matter for a real skill (author, license,
  tags, compatibility, capabilities).
- A `SKILL.md` with concrete instructions and a defined output format.
- Instructions for packaging it as a ZIP.

To install it:

```powershell
cd docs/skills/examples
Compress-Archive -Path ".\code-review\*" -DestinationPath ".\code-review.zip" -Force

# Then install via the API
curl -X POST http://127.0.0.1:8088/api/skills/install `
  -F "file=@code-review.zip"
```

Or drag the ZIP into `/stores` → Skills → **Install Skill**.

Once installed:

- Type `/code` in any chat to see the slash menu offer `✨ /code_review`.
- Open `/stores` → Agents → ✨ on `peldrun` to assign it.
- Run a task — the model's response follows the review checklist.
```
