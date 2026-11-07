# PELDRUN Skills

Skills are self-contained, versioned instruction packs that augment an
agent's system prompt with domain-specific guidance. They are **instructional
only** — they never execute code, never access the network, and never write
to the filesystem beyond reading their own package files. Their purpose is to
encode a repeatable workflow (a code-review checklist, a test-planning
procedure, a security-audit routine) so that both chat invocations and agent
assignments produce consistent, high-quality output.

A skill can be **simple** (a single instruction document) or a **router**
(a parent skill that dispatches to one of several specialized sub-skills).
Router skills pair naturally with **Progressive Disclosure** and the
**BM25 Skill Router** to keep the system prompt small even when dozens of
skills are installed.

This document is the operator reference: what a skill is, how it is
installed, managed, invoked, and diagnosed. For the authoring guide (how to
write your own skill package, including router skills and sub-skills), see
[creating-skills.md](./creating-skills.md).

---

## Table of Contents

1. [Concept](#1-concept)
2. [Progressive Disclosure](#2-progressive-disclosure)
3. [Router Pattern (Sub-Skills)](#3-router-pattern-sub-skills)
4. [BM25 Skill Router](#4-bm25-skill-router)
5. [Where skills are used](#5-where-skills-are-used)
6. [Package layout](#6-package-layout)
7. [Installing a skill](#7-installing-a-skill)
8. [Managing installed skills](#8-managing-installed-skills)
9. [Editing skill content](#9-editing-skill-content)
10. [Assigning skills to agents](#10-assigning-skills-to-agents)
11. [Invoking a skill from chat](#11-invoking-a-skill-from-chat)
12. [Runtime injection](#12-runtime-injection)
13. [Skill tools](#13-skill-tools)
14. [Lifecycle states](#14-lifecycle-states)
15. [Storage layout](#15-storage-layout)
16. [REST API reference](#16-rest-api-reference)
17. [Security model](#17-security-model)
18. [Troubleshooting](#18-troubleshooting)
19. [Reference example](#19-reference-example)

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
- **Composable.** A skill can declare **sub-skills** and act as a router
  (see §3). This is the mechanism that lets PELDRUN scale to hundreds of
  skills without paying the token cost on every message.

---

## 2. Progressive Disclosure

Prior to Progressive Disclosure, every assigned skill's full body was
injected into the system prompt on **every** run. That worked up to about
10 skills; beyond that, the system prompt ballooned to thousands of tokens
of guidance the model mostly ignored.

Progressive Disclosure replaces "inject everything" with a three-tier model
that loads content only when it is needed:

| Tier | What it contains | When it is loaded | Typical size |
|------|------------------|-------------------|--------------|
| **L1: Catalog** | id + name + description + sub-skill names | Always (in every run) | 50–150 tokens per skill |
| **L2: Instructions** | The full `SKILL.md` body (parent or sub-skill) | On demand, via the `load_skill` / `load_sub_skill` tool | 500–2000 tokens |
| **L3: Resources** | Reference files under `references/` | On demand, via the `load_reference` tool | Variable |

### What this looks like at runtime

When Progressive Disclosure is enabled, the engine builds a **catalog**
listing the top-K most relevant skills (see §4), appends it to the system
prompt, and exposes three tools to the model:

- `load_skill(skill_id)` — fetch a skill's full body.
- `load_sub_skill(parent_id, sub_skill_id)` — fetch a sub-skill's body.
- `load_reference(skill_id, rel_path)` — fetch a reference file.

The model then decides at inference time which skills it actually needs. If
the user's message is about SQL injection, the model loads only the
`security_audit` sub-skill from `code_review`, not the entire code-review
toolkit.

### Feature flag

Progressive Disclosure is controlled by a single environment variable:

```
PELDRUN_PROGRESSIVE_DISCLOSURE=true    # default
```

- **`true`** (default): catalog + skill tools.
- **`false`**: legacy full-body injection. Every assigned skill's entire
  `SKILL.md` is injected on every run, no tools are exposed. Useful for
  environments that cannot upgrade their model harness, or as an emergency
  rollback.

The flag is read **on every run**, not cached at startup. Changing it takes
effect on the next run without a restart. Startup logs record the effective
mode:

```
INFO:omweb.main:Skills progressive disclosure: ENABLED (PELDRUN_PROGRESSIVE_DISCLOSURE=true (default)).
```

### Backward compatibility

Skills without `sub_skills` work unchanged in either mode. The catalog
simply lists them at the top level; the model calls `load_skill` to fetch
their full body. The behavior of a plain skill is identical to the pre-
feature runtime, just deferred by one tool call.

---

## 3. Router Pattern (Sub-Skills)

A **router skill** is a skill whose body is short and whose job is to
dispatch to one of several specialized **sub-skills**. Each sub-skill is a
complete `SKILL.md` document with its own instructions, output format, and
trigger keywords.

### Why routers exist

A deep single skill (e.g. `debugging`) accumulates guidance for every
technique — root-cause tracing, defense in depth, methodology, verification
— until its body exceeds the useful context budget. Splitting it into
sub-skills lets the model load only the technique relevant to the current
question:

```
debugging (router)
├── root_cause_tracing        (~800 tokens)
├── defense_in_depth          (~600 tokens)
├── systematic_debugging      (~700 tokens)
└── verification_before_completion  (~500 tokens)
```

Before: every debugging run cost ~2600 tokens (all four bodies).
After: a debugging run costs ~200 tokens (router only) + ~800 tokens (the
chosen sub-skill). That is an **85% reduction** for a single-skill package.

### The router body

The parent `SKILL.md` for a router is a short dispatch prompt: it lists the
sub-skills, describes when to use each, and instructs the model to call
`load_sub_skill`. It does **not** contain the review instructions themselves.

```markdown
---
name: code_review
description: Systematic code review with router-based sub-skill selection.
---

# Code Review Router

You have access to four specialized sub-skills.

## Sub-Skills

### 1. correctness_analysis
Use when: The user has code with potential logic bugs...

### 2. security_audit
Use when: The user mentions security, injection, secrets, auth...

(...)

## How to Activate

Call `load_sub_skill(parent_id="code_review", sub_skill_id="<chosen>")`.
```

### The manifest declaration

Sub-skills are declared in the parent's `manifest.json`:

```json
"sub_skills": [
  {
    "id": "security_audit",
    "description": "Identify security vulnerabilities — injection, secrets, auth bypass.",
    "triggers": ["security", "injection", "xss", "sql", "auth", "secret"],
    "instructions_file": "sub_skills/security_audit/SKILL.md"
  },
  {
    "id": "correctness_analysis",
    "description": "Identify logic bugs, edge cases, incorrect control flow.",
    "triggers": ["bug", "logic", "null", "crash", "correctness"],
    "instructions_file": "sub_skills/correctness_analysis/SKILL.md"
  }
]
```

Each entry contributes to **two** things:

1. **BM25 routing** — `triggers` and `description` are indexed so the parent
   ranks higher when the user's message matches any of its sub-skills.
   The parent never loses a match to a sibling sub-skill's tags.
2. **Model-side dispatch** — the catalog and the router body both list the
   sub-skill IDs and descriptions, so the model knows what to call.

### When to use a router

Use a router when:

- The skill naturally covers **distinct aspects** of a task (correctness,
  security, error handling, maintainability).
- The combined body would exceed ~2,000 tokens.
- Different invocations should produce **structurally different** output
  (a security audit and a maintainability review have different templates).

Do **not** use a router when:

- The skill has one clear job with a single output format.
- The sub-skills would each be under 300 tokens (the dispatch overhead is
  not worth it).

The recommended ceiling is **50 sub-skills per router**. Beyond that, the
parent body itself becomes long enough that splitting the parent into
multiple router skills is the better move.

### Reference example

The `code_review` skill shipped with PELDRUN is a router with four
sub-skills (see §19). It is the canonical example for the pattern.

---

## 4. BM25 Skill Router

The BM25 Skill Router is the ranking layer that picks the top-K most
relevant skills for a given user message. It exists so that the catalog
injected into the system prompt can stay small even when hundreds of skills
are installed.

### How it works

1. Every skill is reduced to a searchable **corpus**: its `id`, `name`,
   `description`, `short_description`, `tags`, and its **sub-skills'
   `triggers`** (hoisted to the parent level).
2. At run time, the user's message is tokenized and scored against each
   corpus using **BM25Okapi** with standard parameters (`k1=1.5`, `b=0.75`).
3. The top-K skills (default K=5) with a score above `min_score=0.1` are
   selected for the catalog.

### Determinism

BM25 is deterministic: the same skill set and the same query produce the
same ranking, run after run. This is essential for prompt caching — an
identical catalog produces an identical prompt prefix.

### Preprocessing

- **Tokenization**: ASCII alphanumerics + underscores, lowercased. Identifiers
  like `code_review` stay as a single token.
- **Stopwords**: A fixed set of ~60 English stopwords is filtered from both
  the corpus and the query. Without this filter, a query like
  *"what is the weather in tokyo"* matches the word `the` in any skill
  description and produces false positives.
- **Non-ASCII input is unsupported**: Arabic, CJK, and other non-Latin
  queries tokenize to an empty list and yield no matches. Multilingual
  routing is out of scope for this release.

### The tiny-corpus fallback

BM25's IDF formula becomes non-positive when a query term appears in half
or more of the corpus. In a corpus of 1 or 2 skills, EVERY matching term
gets a negative IDF, so the top-scoring skill ends up with a **negative**
score and is filtered out by `min_score`.

To prevent this from making the feature dead-on-arrival for new installs,
the router detects the degenerate case (`top_score <= 0`) and falls back to
a simple **overlap count**: the score becomes the number of query tokens
that appear in the skill's corpus. This preserves the "no match → no
result" semantics while keeping the ranking meaningful for N ≤ 2.

For N ≥ 3, the standard BM25 path is used automatically.

### Explicit invocations bypass BM25

A skill invoked via `/<skill-id>` in the composer is **always** included in
the catalog, regardless of its BM25 score. The user's explicit intent
overrides the ranker.

---

## 5. Where skills are used

Skills show up in four surfaces:

| Surface | Purpose | Reach |
|---------|---------|-------|
| **Chat composer (`/`)** | One-shot invocation for the current message | Frontend `/` menu → `invoked_skills` in the run payload |
| **Agent assignment** | Ambient instruction loaded on every run of an agent | `/stores` → Agents → ✨ Skills modal → `agent_settings.json` |
| **Store UI (`/stores/skills`)** | Install, enable, disable, edit, delete, preview, ROUTER badge | Direct HTTP against `/api/skills/*` |
| **LLM tool calls** | Model fetches skill bodies on demand (Progressive Disclosure) | `load_skill`, `load_sub_skill`, `load_reference` |

The two invocation channels — chat `/` and agent assignment — are **merged
at run time**. If the same skill is both assigned and invoked, it is injected
once, with the source marked `"invoked"` (the user's explicit choice for this
turn wins over the agent's ambient assignment).

---

## 6. Package layout

A skill package is a directory containing `manifest.json` and the instructions
file (`SKILL.md` by default), both at the package root.

### Simple skill

```
code-review/
├── manifest.json
├── SKILL.md
└── references/           # optional
    └── style-guide.md
```

### Router skill

```
debugging/
├── manifest.json                       # declares sub_skills
├── SKILL.md                            # router body (short)
├── icon.svg                            # optional
└── sub_skills/
    ├── root_cause_tracing/
    │   └── SKILL.md
    ├── defense_in_depth/
    │   └── SKILL.md
    ├── systematic_debugging/
    │   └── SKILL.md
    └── verification_before_completion/
        └── SKILL.md
```

**Rejected at install time:**

- Any wrapping directory (`code-review-v1/code-review/manifest.json` fails).
- Executables (`.exe`, `.dll`, `.so`, `.dylib`).
- Bytecode (`.pyc`, `.pyo`, `.pyd`).
- Symlinks.
- `.git/`, `.svn/`, `node_modules/`, `.venv/`.
- Files > 5 MiB each; archives > 100 MiB uncompressed.
- More than 500 files; path depth > 10.
- Any sub-skill path that escapes the parent package (containment check).

The full validation rules are documented in
[creating-skills.md](./creating-skills.md#2-package-structure).

---

## 7. Installing a skill

### From the Store UI

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

### From the API

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
    "version": "2.0.0",
    "has_sub_skills": true,
    "sub_skills_count": 4,
    ...
  },
  "was_upgrade": true,
  "warnings": []
}
```

### What happens on disk

1. The ZIP is validated (structure + security) in memory.
2. Contents are extracted to a staging directory
   (`backend/storage/store/skills/.tmp_<uuid>/`).
3. `manifest.json` is parsed and normalized: `id` is lowercased,
   hyphens/dots → underscores, invalid characters removed.
4. The `sub_skills` array is validated:
   - At most 50 entries.
   - Every `id` matches `^[a-z0-9][a-z0-9_]{2,63}$` and is unique in the
     parent.
   - Every `description` is 10–500 characters.
   - Every `instructions_file` matches
     `^sub_skills/…\.md$` and contains no `..` segment.
5. `SKILL.md` frontmatter is parsed for the router body.
6. Each sub-skill's `SKILL.md` is validated on first load (not at install
   time — the runtime checks containment on every `load_sub_skill` call).
7. `icon_svg` (if present) is sanitized.
8. `peldrun_min_version` / `peldrun_max_version` are checked against the
   running `peldrun.__version__`.
9. The staging directory is atomically renamed to
   `backend/storage/store/skills/<id>/`. Any existing package with the same
   ID is moved to `.trash_<uuid>/` first.
10. The registry is refreshed so the skill appears immediately.

### Idempotency

Re-uploading the **same** ZIP (same `id`, same `version`, same content hash):

- Returns `was_upgrade: false`.
- Does **not** touch the filesystem.
- Does **not** touch `skills_state.json`.

Re-uploading a **different** ZIP with the **same** `version`:

- Returns `was_upgrade: true` and emits a warning asking you to bump the
  version next time.
- Replaces the previous package.

---

## 8. Managing installed skills

Open `/stores` → **Skills**. Each row is one installed skill.

### Row anatomy

**Simple skill:**

```
[icon]  Code Review                                    ⓘ  ✏  🗑  ●
        CUSTOM  ACTIVE  ✓  v1.0.0
        Find real defects with evidence, ordered by severity.
        [review] [audit] [security] [quality] [code]
        ✨ invoke /code_review   🤖 agent-assignable
```

**Router skill** (adds the ROUTER badge):

```
[icon]  Code Review                                    ⓘ  ✏  🗑  ●
        CUSTOM  ACTIVE  ROUTER · 4  ✓  v2.0.0
        Systematic code review with router-based sub-skill selection.
        [review] [audit] [security] [quality] [code] [router]
        ✨ invoke /code_review   🤖 agent-assignable
```

| Element | Meaning |
|---------|---------|
| **icon** | The skill's icon (`icon_svg` or `icon` file), or a Sparkles glyph. |
| **BUILT-IN / CUSTOM** | Whether the skill ships with PELDRUN or was installed by the user. |
| **ACTIVE / DISABLED** | The global activation state. |
| **ROUTER · N** | Present only for router skills. N is the sub-skill count. Violet badge. |
| **✓ shield** | `trust: "verified"` in the manifest. |
| **v2.0.0** | The installed version. |
| **tags** | From `manifest.tags` — used by the `/` menu search. |
| **ⓘ** | Open the details drawer. |
| **✏** | Open the SKILL.md editor. Only shown when `user_editable: true`. |
| **🗑** | Delete the package (custom skills only). |
| **● toggle** | Enable / disable globally. |

### Actions

| Action | Effect |
|--------|--------|
| **Toggle off** | The skill is hidden from `/` and from agent injection. Package files remain. |
| **Toggle on** | The skill returns to `/` and to agent injection. |
| **Details (ⓘ)** | Read-only drawer with the SKILL.md body, metadata, tags, examples, links, and (for routers) the sub-skills list. |
| **Edit (✏)** | Opens the SKILL.md editor. See §9. |
| **Delete (🗑)** | Removes the package files from disk permanently. Custom skills only. |

Every action that mutates state fires a `peldrun:skills-changed` browser event
so open chat sessions immediately refresh their `/` menu without a reload.

### The details drawer

For a **simple skill**, the drawer shows: description, tags, capability
chips, author, license, links, and the SKILL.md body (collapsible).

For a **router skill**, the drawer adds a **Sub-Skills** section between the
capability chips and the About block. Each sub-skill row shows:

- Its `id` in monospace.
- Its `description`.
- Its trigger keywords as small chips.
- An **EXCLUSIVE** badge if `capabilities.exclusive: true`.
- A **pencil button** to open that sub-skill's body in the editor.

Clicking the pencil opens the same editor modal but pre-focused on the
sub-skill's `SKILL.md` (see §9).

---

## 9. Editing skill content

Users can edit two things from the Store UI:

1. The **router body** of any `user_editable` skill.
2. The **body** of any sub-skill within that router.

Both paths share the same editor modal, the same Lexical WYSIWYG, and the
same save pipeline. The only difference is which file on disk the save
operation targets.

### Editing the router body

1. Click the **pencil icon (✏)** on the skill card.
2. The modal opens with the title `Edit — Code Review`.
3. The current `SKILL.md` body appears (frontmatter is shown read-only above
   the editor).
4. Make changes. The **Save Changes** button enables when the body differs
   from the loaded version.
5. Click **Save Changes**.

The save hits `PUT /api/skills/{skill_id}/instructions`. The server:

1. Verifies `user_editable: true`.
2. Verifies the body is non-empty and does not start with `---`.
3. Verifies the body is under `limits.max_instructions_bytes`.
4. Reads the existing `SKILL.md`, extracts the frontmatter block verbatim.
5. Recomposes frontmatter + new body, re-parses to validate.
6. Writes atomically via `.md.tmp` + `os.replace`.
7. Refreshes the registry.

### Editing a sub-skill body

1. Open the details drawer for a router skill.
2. Click the **pencil icon** on a sub-skill row.
3. The modal opens with the title `Edit — Code Review › security_audit`.
   The subtitle confirms `editing sub-skill`.
4. The sub-skill's `SKILL.md` body appears (its own frontmatter, not the
   router's, is shown read-only).
5. Save as above.

The save hits
`PUT /api/skills/{skill_id}/sub-skills/{sub_skill_id}/instructions`. The
server performs the same checks, PLUS:

1. Verifies the parent declares a sub-skill with that `sub_skill_id`.
2. Verifies the resolved file path stays inside the parent package
   (containment check — defense in depth, since the manifest validation
   already enforces this).
3. Reads the sub-skill file, extracts its frontmatter, and preserves it.

### What cannot be edited from the UI

- **Manifest metadata.** `description`, `tags`, `capabilities`, `version` —
  changing these requires editing `manifest.json` and reinstalling the
  package. This is deliberate: version and capabilities affect runtime
  behavior globally.
- **Sub-skill IDs.** The sub-skill ID is the dispatch key used by
  `load_sub_skill`. Changing it would break the router body's references.
- **Sub-skill triggers.** These are indexed by BM25 and declared in the
  manifest. Same reasoning as above.
- **The frontmatter block.** Preserved verbatim server-side from the
  existing file. If you need to change `name` or `description` inside the
  frontmatter, reinstall the package.

### File-size limit

Every save enforces the same ceiling as the parent's
`limits.max_instructions_bytes` (default 256 KiB, hard ceiling 1 MiB). The
editor shows a live counter: `12,345 / 262,144 bytes`. Saves that exceed
the ceiling are rejected with a `422` and an explanatory error message.

---

## 10. Assigning skills to agents

Open `/stores` → **Agents** → the ✨ icon on any agent card.

The modal has three sections:

### 10.1 Manifest Skills

Skills declared in the agent's manifest (`skills: [...]` in the agent's
`manifest.json`). Each row is a checkbox:

- **Checked** → the skill is active for this agent.
- **Unchecked** → the skill is added to `disabled_skills` in
  `agent_settings.json`, so the runtime skips it.

### 10.2 Available Store Skills

Skills that are installed and not already in the agent's manifest. Each row
is a checkbox. Blocked rows (disabled, deprecated, or `agent_assignable:
false`) appear but cannot be selected.

### 10.3 Effective Preview

A live preview of the runtime skill list. For router skills, the preview
shows the parent ID only — sub-skills are loaded on demand by the model at
runtime.

### 10.4 Where the state is stored

All assignments live in
`backend/storage/store/agents/agent_settings.json`. Tool overrides, MCP
overrides, and skill overrides coexist in the same record; updating one does
not erase the others.

### 10.5 Locking an agent's skills

An agent whose manifest contains `"settings": { "skills_editable": false }`
cannot have its skills modified. The Skills modal renders read-only and
`PUT /api/store/agents/{id}/skills` returns **403 Forbidden**.

---

## 11. Invoking a skill from chat

### The `/` menu

Type `/` in the composer. Every `chat_invocable`, non-deprecated skill is
offered as a slash command.

- **Search works.** Matches against id, display name, description, and tags.
- **Hyphen normalization.** `/code-review` resolves to `code_review`.
- **Multiple skills per message.** Pick several; all are injected.

### Router skills in `/`

Selecting a router skill via `/` includes only the router's own body in the
catalog. The model then decides (based on the message content) whether to
call `load_sub_skill` to fetch a specific sub-skill. This is the intended
behavior — the router pattern exists precisely so that selecting the parent
does not require committing to a specific sub-skill upfront.

### The skill chip

Selecting a skill adds a chip above the editor:

```
✨ Skills   [✨ Code Review ×]   [✨ Test Planning ×]
```

`×` removes the skill from the current message only. Chips clear on send.

### What the model receives (Progressive Disclosure enabled)

When the run starts, the engine builds a catalog from the top-K BM25-ranked
skills (plus any explicitly invoked skills), and appends it to the system
prompt:

```
[SKILL INSTRUCTIONS]
You have access to the following skills. ...
AVAILABLE SKILLS:
- id='code_review' | name='Code Review' | Systematic code review with router-based sub-skill selection.
  sub-skills:
    * sub_skill='correctness_analysis': Identify logic bugs...
    * sub_skill='security_audit': Identify security vulnerabilities...
    * sub_skill='error_handling_review': Review exception hierarchies...
    * sub_skill='maintainability_review': Review code for clarity...
```

The model then calls `load_skill` / `load_sub_skill` to fetch actual
content. See §12 and §13 for the full flow.

### What the model receives (Progressive Disclosure disabled)

Every assigned and invoked skill's full body is injected at the bottom of
the system prompt, wrapped in delimiters:

```
--- BEGIN SKILL: code_review@2.0.0 ---
[Metadata: name="Code Review", tags=[review, audit, security]]
# Code Review Router
...
--- END SKILL: code_review@2.0.0 ---
```

This is the pre-feature behavior and is preserved as an escape hatch.

---

## 12. Runtime injection

When the engine (`backend/omweb/engines/peldrun_engine.py`) starts a run, it
branches on the `PELDRUN_PROGRESSIVE_DISCLOSURE` flag.

### Mode A — Progressive Disclosure (default)

1. **Read the candidate pool.** Every enabled, non-deprecated skill.
2. **Rank against the user's message.** BM25 selects the top-K (default K=5).
3. **Include invoked skills.** Any skill invoked via `/` is added on top,
   regardless of BM25 score.
4. **Build the catalog.** Render the selected skills as a compact text block
   listing IDs, names, descriptions, and sub-skill names. Respect a 4,000-
   character budget (skills beyond the budget are omitted with a "N more
   omitted" notice).
5. **Register the skill tools.** Build three `WebToolAdapter` instances
   (`load_skill`, `load_sub_skill`, `load_reference`) and expose them to the
   model alongside the run's normal tools.
6. **Inject the catalog.** Append it to the system prompt under
   `[SKILL INSTRUCTIONS]`.

The model then drives the flow. A typical run looks like:

```
User: "Audit this Python code for SQL injection..."
  ↓
Model: tool_call load_skill("code_review")
  ↓
Tool returns: router body + sub_skill list
  ↓
Model: tool_call load_sub_skill("code_review", "security_audit")
  ↓
Tool returns: sub-skill body
  ↓
Model: generates the final answer following the sub-skill's output format
```

### Mode B — Legacy full injection

1. **Read the assigned + invoked skills.** Merged as described in §1.
2. **Load each skill's full body.**
3. **Concatenate in deterministic order** (invoked first, then assigned, all
   alphabetized).
4. **Inject the combined block** under `[SKILL INSTRUCTIONS]`.

No tools are exposed. The model sees every body but has no way to fetch
additional content.

### Ordering and precedence (Mode B)

- **Invoked skills** are injected first, in alphabetical order.
- **Assigned skills** are injected second, skipping any already injected
  as invoked.
- A skill appearing in both channels is injected once, marked `"invoked"`.

### Failure handling (both modes)

A skill that fails to load (missing file, oversized body, unsafe SVG) is
**skipped** with a warning. The run continues with the surviving skills. A
broken skill can never crash a chat.

For sub-skills, the failure mode is finer-grained: a sub-skill that fails
to load returns `is_error: true` from `load_sub_skill`, and the model is
informed of the reason via the tool's error payload. The rest of the run
proceeds normally.

### Diagnostics

The runtime metadata records the injection result:

```json
{
  "injected_skills": ["code_review"],
  "skill_warnings": [],
  "progressive_disclosure": true,
  "skill_injection_mode": "catalog",
  "skill_catalog_chars": 1029
}
```

These appear in the run's diagnostic log and in the run timeline (if
enabled).

---

## 13. Skill tools

Three tools are exposed to the model only when Progressive Disclosure is
enabled. They live in `backend/omweb/skills/tools/` and are registered as
`WebToolAdapter` instances in the run's `adapted_core_tools` list.

### `load_skill(skill_id: str)`

Returns the full body of a skill, wrapped in delimiters.

**Response (success):**

```json
{
  "ok": true,
  "skill_id": "code_review",
  "version": "2.0.0",
  "has_sub_skills": true,
  "sub_skills": ["correctness_analysis", "security_audit", "..."],
  "content": "--- BEGIN SKILL: code_review@2.0.0 ---\n# Code Review Router\n...\n--- END SKILL: code_review@2.0.0 ---"
}
```

**Response (error):** `{ "ok": false, "error": "...", "code": "SKILL_NOT_FOUND" | "SKILL_DISABLED" | "SKILL_LOAD_FAILED" }`.

### `load_sub_skill(parent_id: str, sub_skill_id: str)`

Returns the full body of a sub-skill within a router.

**Response (success):**

```json
{
  "ok": true,
  "parent_id": "code_review",
  "sub_skill_id": "security_audit",
  "content": "--- BEGIN SUB-SKILL: code_review/security_audit ---\n# Security Audit\n...\n--- END SUB-SKILL: code_review/security_audit ---"
}
```

**Response (error)** codes:

- `INVALID_ARGUMENT` — empty input.
- `SKILL_NOT_FOUND` — parent does not resolve.
- `SKILL_DISABLED` — parent is not active.
- `NOT_A_ROUTER` — parent declares no sub-skills.
- `SUB_SKILL_NOT_FOUND` — sub ID not declared (response includes `available` list).
- `SKILL_SECURITY_VIOLATION` — path escapes the parent package (containment).
- `SUB_SKILL_FILE_MISSING` — declared file is absent on disk.
- `SUB_SKILL_LOAD_FAILED` — read failed or file exceeded size cap.

### `load_reference(skill_id: str, rel_path: str)`

Returns the content of a reference file declared under the skill's
`execution.references_dir`.

**Response (success):**

```json
{
  "ok": true,
  "skill_id": "code_review",
  "version": "2.0.0",
  "rel_path": "checklist.md",
  "content": "--- BEGIN REFERENCE: code_review:checklist.md ---\n...\n--- END REFERENCE: code_review:checklist.md ---"
}
```

**Response (error)** codes:

- `INVALID_ARGUMENT` — empty input.
- `SKILL_NOT_FOUND` — skill does not resolve.
- `SKILL_DISABLED` — skill is not active.
- `REFERENCE_LOAD_FAILED` — file missing, empty, oversized, or skill has
  no `references_dir`.
- `REFERENCE_SECURITY_VIOLATION` — traversal, absolute path, disallowed
  extension, symlink, or containment escape.

### Guarantees

All three tools share these properties:

- **Never raise.** Every failure is encoded as `{ "ok": false, ... }`.
- **Containment enforced.** No file outside the skill's package directory
  can be read, even if a manifest is corrupted or a sub-skill is edited in
  memory.
- **Size caps enforced.** Both `load_instructions` and `load_sub_skill` cap
  file reads at `limits.max_instructions_bytes` before reading.
- **Delimiters.** Every successful response is wrapped in
  `--- BEGIN ... ---` / `--- END ... ---` so the model can distinguish
  skill content from user content.

---

## 14. Lifecycle states

A skill has **three independent** boolean states. Do not conflate them:

| State | Storage | Meaning |
|-------|---------|---------|
| **Installed** | Package on disk | The skill files exist and passed install validation. |
| **Enabled** | `skills_state.json` | The skill may be used at runtime. |
| **Assigned** | `agent_settings.json` | The skill is available to a specific agent. |

### State matrix

| Installed | Enabled | Assigned | `/` menu | Catalog (PD) | Full injection |
|-----------|---------|----------|----------|--------------|----------------|
| ✓ | ✓ | — | ✓ | ✓ (if ranked) | — |
| ✓ | ✓ | ✓ | ✓ | ✓ (if ranked) | ✓ |
| ✓ | — | ✓ | — | — (silently skipped) | — |
| ✓ | — | — | — | — | — |
| — | — | ✓ (stale) | — | — (warning logged) | — |

### Deprecation overrides

A skill with `stability = "deprecated"` or `deprecated = true` is treated
as if `chat_invocable = false` and `agent_assignable = false`, regardless of
the declared `capabilities`. It remains installed and enableable but is
invisible to both runtime channels.

### Sub-skills

Sub-skills do not have independent lifecycle state. They are always
"available" as long as their parent is installed and enabled. Editing a
sub-skill's body does not affect its ID, its triggers, or the parent's
routing behavior.

---

## 15. Storage layout

```
backend/storage/store/skills/
├── skills_state.json                # global activation state
├── code_review/                     # router skill
│   ├── manifest.json
│   ├── SKILL.md                     # router body
│   ├── .installed.json
│   ├── icon.svg                     # optional
│   ├── references/                  # optional
│   │   └── checklist.md
│   └── sub_skills/
│       ├── correctness_analysis/
│       │   └── SKILL.md
│       ├── security_audit/
│       │   └── SKILL.md
│       ├── error_handling_review/
│       │   └── SKILL.md
│       └── maintainability_review/
│           └── SKILL.md
├── test_planning/                   # simple skill
│   ├── manifest.json
│   └── SKILL.md
└── .tmp_<uuid>/                     # transient staging
```

### `skills_state.json`

```json
{
  "schema_version": 1,
  "updated_at": "2026-11-07T10:30:00+00:00",
  "states": {
    "code_review": "active",
    "test_planning": "disabled"
  }
}
```

Only skills present on disk appear in `states`. Orphan entries are pruned
on the next discovery pass.

### `.installed.json`

```json
{
  "schema_version": 1,
  "skill_id": "code_review",
  "version": "2.0.0",
  "name": "Code Review",
  "description": "Systematic code review with router-based sub-skill selection.",
  "content_hash": "sha256:abc123def4567890",
  "installed_at": "2026-11-07T10:30:00+00:00",
  "source_filename": "code-review-v2.0.0.zip",
  "size_bytes": 21534,
  "file_count": 12,
  "original_id": "code-review"
}
```

### Crash recovery

If the process dies mid-install:

- `.tmp_<uuid>/` — staging directory; cleaned on next startup or install.
- `.trash_<uuid>/` — previous package moved aside during upgrade; restored
  to its original name on the next commit attempt, or cleaned up.

Both are handled by `SkillManager.cleanup_orphans()`, called in the FastAPI
lifespan handler and before every install.

---

## 16. REST API reference

All routes are registered under `/api/skills` unless noted. Errors follow
the shape `{ "detail": { "error": str, "code": str, "details": {} } }`.

### `GET /api/skills`

List installed skills.

**Query params:**

| Param | Default | Description |
|-------|---------|-------------|
| `search` | — | Free-form query. Matches id, name, display_name, tags, description. |
| `include_disabled` | `true` | Include `status = "disabled"` skills. |
| `include_deprecated` | `true` | Include skills marked deprecated. |
| `include_broken` | `false` | Include packages that failed validation. |
| `limit` | `100` | Max results (search mode only). |

**Response:**

```json
{
  "status": "success",
  "skills": [
    {
      "id": "code_review",
      "version": "2.0.0",
      "name": "Code Review",
      "has_sub_skills": true,
      "sub_skills_count": 4,
      "..."
    }
  ],
  "total": 1,
  "broken": {}
}
```

### `GET /api/skills/{skill_id}`

Full detail: summary fields + `manifest` + `frontmatter` + `sub_skills`
array (each entry has `id`, `description`, `triggers`, `capabilities`;
`instructions_file` is deliberately excluded).

### `GET /api/skills/{skill_id}/sub-skills`

List the sub-skills of a router.

**Response:**

```json
{
  "status": "success",
  "parent_id": "code_review",
  "has_sub_skills": true,
  "sub_skills": [
    {
      "id": "security_audit",
      "description": "Identify security vulnerabilities...",
      "triggers": ["security", "injection", "xss", "sql", "auth", "secret"],
      "capabilities": { "exclusive": false }
    }
  ],
  "count": 4
}
```

### `GET /api/skills/{skill_id}/icon.svg`

Returns `image/svg+xml`. Content is sanitized server-side. Cached for 1
hour. Returns 404 if the skill declares no icon.

### `GET /api/skills/{skill_id}/preview`

Returns the router body's instructions text:

```json
{
  "status": "success",
  "skill_id": "code_review",
  "version": "2.0.0",
  "instructions": "# Code Review Router\n...",
  "instructions_length": 3326,
  "frontmatter": { "name": "code_review", "..." }
}
```

### `GET /api/skills/{skill_id}/sub-skills/{sub_skill_id}/preview`

Returns a sub-skill's instructions text:

```json
{
  "status": "success",
  "skill_id": "code_review",
  "sub_skill_id": "security_audit",
  "version": "2.0.0",
  "instructions": "# Security Audit\n...",
  "instructions_length": 1953,
  "frontmatter": { "name": "security_audit", "..." }
}
```

### `PUT /api/skills/{skill_id}/instructions`

Body: `{ "instructions": "new body without frontmatter" }`. Replaces the
router body. Frontmatter preserved server-side. Returns 403 if the skill is
not `user_editable`, 422 if validation fails.

### `PUT /api/skills/{skill_id}/sub-skills/{sub_skill_id}/instructions`

Body: `{ "instructions": "new sub-skill body" }`. Same contract, but
targets the sub-skill's file. Errors: 404 if the sub-skill ID is not
declared, 403 if path escapes the package, 422 for validation failures.

### `POST /api/skills/install`

`multipart/form-data` with a `file` field containing the `.zip`. Returns
`{ status, skill, was_upgrade, warnings }` on success.

### `POST /api/skills/{skill_id}/toggle`

Flips the status between `active` and `disabled`. Returns the updated
`SkillSummary`.

### `DELETE /api/skills/{skill_id}`

Deletes the package from disk. Returns `{ status: "success", deleted: true }`.

### Agent assignments

Under `/api/store/agents`:

### `GET /api/store/agents/{agent_id}/skills`

Returns the current assignment state with resolved metadata.

### `PUT /api/store/agents/{agent_id}/skills`

Body: `{ "disabled_skills": [...], "extra_skills": [...] }`. Every ID must
resolve to an installed skill. `extra_skills` entries must additionally be
enabled, `agent_assignable`, and not deprecated. Returns 403 if the agent
is locked.

---

## 17. Security model

### Threat: malicious ZIP contents

The validator rejects path traversal, symlinks, forbidden file types
(executables, bytecode, VCS metadata, environment folders), and zip bombs.
All validations run **before any byte is written to disk**.

### Threat: sub-skill path escape

Two independent layers protect against a sub-skill file reading outside
its parent package:

1. **Manifest validation** (at install time) — every `instructions_file`
   must match `^sub_skills/…\.md$` and contain no `..` segment.
2. **Runtime containment** (every `load_sub_skill` call) — the resolved
   path must satisfy `candidate.resolve().relative_to(package_dir.resolve())`.
   If a future code path mutates `record.sub_skills` in memory without
   going through the manifest validator, this layer still catches the
   escape.

Both layers run on every `load_sub_skill` and every
`PUT .../sub-skills/.../instructions`.

### Threat: malicious SVG in `icon_svg`

The sanitizer enforces an element whitelist (no `script`, `foreignObject`,
`use`, `image`, `style`, `animate*`), an attribute whitelist (no `on*`, no
`href`, no `style`), and a value blacklist (`javascript:`, `vbscript:`,
`data:`, `@import`, `expression(`). The sanitized SVG is what gets served
at `/api/skills/{id}/icon.svg`; the client does a second defense-in-depth
pass.

### Threat: prompt injection via `SKILL.md`

A skill's body is injected into the system prompt. This is by design — the
operator is expected to trust installed skills the way they trust installed
tools.

Mitigations:

- Only operators can install. There is no auto-discovery from public sources.
- The `trust` field distinguishes verified publishers from community
  packages. The shield badge is visible in the store card.
- The `signature` field (optional) records a content hash.
- The injected text is delimited (`--- BEGIN SKILL: ... ---`).

### Threat: catalog poisoning via manifest fields

The catalog block lists only `id`, `name`, `description`, and sub-skill
names/descriptions. Every one of these fields is validated at install time
(pattern, length caps). Newlines are collapsed to spaces before rendering,
so a manifest cannot inject a fake `- id='evil' | name='evil'` line.

### Threat: resource exhaustion

- `limits.max_instructions_bytes` (default 256 KiB) caps body size.
- `MAX_REFERENCE_BYTES` (512 KiB) caps reference files.
- `MAX_TOTAL_REFERENCES_BYTES` (2 MiB) caps the aggregate.
- `sub_skills` is capped at 50 per parent.
- The catalog block is capped at 4,000 characters.

Every cap is enforced before reading. A skill that fails any check is
skipped at load time; the run continues.

### Threat: code execution

**Not possible in this release.** `execution.type` accepts only
`"instructions"`; there is no handler execution, no shell, no Python
import.

---

## 18. Troubleshooting

### Progressive Disclosure is off and I want it on

Check the startup log for:

```
INFO:omweb.main:Skills progressive disclosure: ENABLED (PELDRUN_PROGRESSIVE_DISCLOSURE=true (default)).
```

If it says `DISABLED`, set the environment variable and restart (or, since
the flag is read on every run, just change the process env — the next run
picks it up):

```powershell
$env:PELDRUN_PROGRESSIVE_DISCLOSURE="true"
```

### The `/` menu does not show a skill I installed

1. `GET /api/skills` — is it present?
2. `GET /api/skills/{id}` — check `enabled`:
   - `false` → toggle it on in `/stores` → Skills.
3. Check `chat_invocable` is `true`.
4. Check `is_deprecated` is `false`.
5. Reload the chat page.

### The model calls `load_skill` but never `load_sub_skill`

The model read the router body but chose not to dispatch to a sub-skill.
Possible causes:

- **The user's query was ambiguous.** Try a more specific prompt:
  `"audit for SQL injection"` instead of `"review this"`.
- **The router body does not clearly direct the model.** Make sure it
  contains a `## How to Activate` section that names `load_sub_skill`
  explicitly.
- **The model is too small.** Sub-7B models sometimes ignore the dispatch
  instruction. Try a larger model.

### BM25 returns no skills

Two common causes:

1. **Only 1–2 skills installed.** The tiny-corpus fallback should kick in,
   but if it does not, check `omweb/skills/router/bm25_router.py` is on
   the current version.
2. **Non-ASCII message.** Arabic, CJK, and other non-Latin scripts tokenize
   to an empty list. Route via explicit `/` invocation instead.

### Install rejects with `SKILL_MANIFEST_INVALID`

The `manifest.json` fails schema validation. The error response's
`details.path` points to the failing field. Common sub-skill causes:

| Error | Fix |
|-------|-----|
| `sub_skills[0].id must match …` | Use lowercase, digits, underscores, ≥3 chars. |
| `Duplicate sub_skill id: 'x'` | Two entries share the same `id`. |
| `sub_skills[0].instructions_file must …` | Must start with `sub_skills/` and end with `.md`. |
| `sub_skills[0].instructions_file must not contain '..' segments` | Remove `..` from the path. |
| `Manifest declares N sub-skills; max is 50` | Split the parent into multiple routers. |

### Install rejects with `SKILL_VALIDATION_FAILED`

Structural issues:

| Symptom | Fix |
|---------|-----|
| `missing required file: manifest.json at zip root` | Zip the contents, not the wrapping folder. |
| `Skill package is missing SKILL.md` | Ensure the file named in `execution.instructions_file` exists at the package root. |
| `instructions_too_large` | Reduce `SKILL.md` or raise `limits.max_instructions_bytes`. |

### `load_sub_skill` returns `SUB_SKILL_NOT_FOUND`

The `sub_skill_id` passed by the model does not match any sub-skill
declared in the parent's manifest. The error response includes an
`available` list of valid IDs. This is usually the model's fault (it
hallucinated an ID), not the manifest's. If the model repeats the mistake
across runs, try shortening the sub-skill IDs or improving their
descriptions in the router body.

### `load_sub_skill` returns `SKILL_SECURITY_VIOLATION`

The resolved file path escapes the parent package. This should only happen
if the manifest was hand-edited after install, or a test harness mutated
`record.sub_skills` in memory. Check the backend log for the exact
`rel` path.

### `PUT .../sub-skills/.../instructions` returns 403

The parent skill is not `user_editable`. Built-in skills and skills shipped
with `user_editable: false` cannot be edited from the UI. Reinstall the
package with `user_editable: true` if you authored it.

### The catalog is bigger than I expected

The catalog block is capped at 4,000 characters (~1,000 tokens). If you
have many skills with long descriptions, the top-K skills may fill the
budget quickly. To reduce size:

- Shorten `description` fields (the catalog uses `description`, not
  `store_description`).
- Shorten sub-skill `description` fields in the manifest.
- Lower `top_k` (default 5) if you consistently see 5+ irrelevant entries.

---

## 19. Reference example

The `code_review` skill is PELDRUN's canonical router example. It
demonstrates every feature of the router pattern:

- A parent `manifest.json` declaring four sub-skills with triggers.
- A short router body in `SKILL.md` directing the model to
  `load_sub_skill`.
- Four sub-skill bodies (each 1.5–2.3 KiB) under `sub_skills/`.
- `user_editable: true` on the parent so operators can tweak the router
  body or any sub-skill body from the UI.

### Structure

```
code_review/
├── manifest.json
├── SKILL.md                            # router body
└── sub_skills/
    ├── correctness_analysis/SKILL.md
    ├── security_audit/SKILL.md
    ├── error_handling_review/SKILL.md
    └── maintainability_review/SKILL.md
```

### Manifest excerpt

```json
{
  "schema_version": "1.0.0",
  "id": "code_review",
  "version": "2.0.0",
  "name": "Code Review",
  "description": "Systematic code review with router-based sub-skill selection.",
  "tags": ["review", "audit", "security", "quality", "code", "router"],
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
      "description": "Identify logic bugs, edge cases, incorrect control flow.",
      "triggers": ["bug", "logic", "off-by-one", "null", "undefined", "edge case", "crash"],
      "instructions_file": "sub_skills/correctness_analysis/SKILL.md"
    },
    {
      "id": "security_audit",
      "description": "Identify security vulnerabilities — injection, secrets, auth bypass.",
      "triggers": ["security", "injection", "xss", "sql", "auth", "token", "secret"],
      "instructions_file": "sub_skills/security_audit/SKILL.md"
    },
    {
      "id": "error_handling_review",
      "description": "Review exception hierarchies, retry logic, fallbacks, error propagation.",
      "triggers": ["exception", "error", "try", "catch", "retry", "fallback", "logging"],
      "instructions_file": "sub_skills/error_handling_review/SKILL.md"
    },
    {
      "id": "maintainability_review",
      "description": "Review code for clarity, coupling, duplication, naming.",
      "triggers": ["refactor", "complexity", "naming", "duplication", "maintainability"],
      "instructions_file": "sub_skills/maintainability_review/SKILL.md"
    }
  ],
  "limits": { "max_instructions_bytes": 262144 },
  "user_editable": true
}
```

### Runtime flow (Progressive Disclosure enabled)

```
User: "Audit this Python code for SQL injection:
       cursor.execute(f'SELECT * FROM users WHERE id = {user_id}')"
  ↓
[System prompt] contains catalog listing code_review + 4 sub-skills
  ↓
Model: tool_call load_skill(skill_id="code_review")
  ↓
[Tool result] --- BEGIN SKILL: code_review@2.0.0 --- ... --- END SKILL ---
  ↓
Model: tool_call load_sub_skill(parent_id="code_review", sub_skill_id="security_audit")
  ↓
[Tool result] --- BEGIN SUB-SKILL: code_review/security_audit --- ... --- END ---
  ↓
Model: final answer following security_audit's output format:
      ## [Critical] Injection: SQL injection
      **Location**: get_user.py:42
      **Evidence**: cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
      **Attack**: "1' OR '1'='1"
      **Impact**: returns all users
      **Fix**: use parameterized query
```

### Testing the example

```python
from fastapi.testclient import TestClient
from omweb.main import app

client = TestClient(app)

# Verify the router is installed with 4 sub-skills
r = client.get("/api/skills/code_review/sub-skills").json()
assert r["count"] == 4
assert {s["id"] for s in r["sub_skills"]} == {
    "correctness_analysis", "security_audit",
    "error_handling_review", "maintainability_review",
}

# Verify each sub-skill loads
from omweb.skills.tools.load_sub_skill import execute
for sub_id in ["correctness_analysis", "security_audit",
                "error_handling_review", "maintainability_review"]:
    res = execute("code_review", sub_id)
    assert res["ok"] is True
    assert res["content"].startswith(f"--- BEGIN SUB-SKILL: code_review/{sub_id} ---")
```

### Try it in the UI

1. Open `/stores` → **Skills**. The `code_review` card shows the
   **`ROUTER · 4`** badge.
2. Click **ⓘ** — the drawer opens with the four sub-skills.
3. Click the pencil on `security_audit` — the editor opens on that sub-
   skill's body. Make a small change; save.
4. Open a chat. Type `/code_review`. Send a SQL-injection prompt. Watch
   the model call `load_skill` → `load_sub_skill` in the run timeline.
5. Verify the answer follows the sub-skill's output template.

### Packaging and installing

```powershell
cd backend/storage/store/skills
Compress-Archive -Path ".\code_review\*" -DestinationPath ".\code_review.zip" -Force

curl -X POST http://127.0.0.1:8088/api/skills/install `
  -F "file=@code_review.zip"
```

Or drag the ZIP into `/stores` → Skills → **Install Skill**.
