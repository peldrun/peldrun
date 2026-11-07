# Router Pattern

The **router pattern** is the mechanism PELDRUN uses to split a large skill
into a parent **router** plus several **sub-skills**, so the model loads only
the guidance it needs for the current task. It is the primary driver of
token savings in PELDRUN's Progressive Disclosure system, and it pairs
naturally with the **BM25 Skill Router** to keep the system prompt small
even when dozens of skills are installed.

This document is a focused guide to the pattern: what it is, when to use it,
how to build one, and how it behaves at runtime. For the general skill
authoring reference, see [creating-skills.md](./creating-skills.md). For the
runtime details of Progressive Disclosure and the skill tools, see
[progressive-disclosure.md](./progressive-disclosure.md).

---

## Table of Contents

1. [The problem](#1-the-problem)
2. [The pattern](#2-the-pattern)
3. [Anatomy of a router skill](#3-anatomy-of-a-router-skill)
4. [Choosing sub-skill boundaries](#4-choosing-sub-skill-boundaries)
5. [Manifest declaration](#5-manifest-declaration)
6. [The router body](#6-the-router-body)
7. [Sub-skill bodies](#7-sub-skill-bodies)
8. [Runtime dispatch flow](#8-runtime-dispatch-flow)
9. [BM25 interaction](#9-bm25-interaction)
10. [Editing sub-skills from the UI](#10-editing-sub-skills-from-the-ui)
11. [Versioning and evolution](#11-versioning-and-evolution)
12. [Testing a router](#12-testing-a-router)
13. [Anti-patterns](#13-anti-patterns)
14. [Complete example: `code_review` v2](#14-complete-example-code_review-v2)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. The problem

A **deep, single-file skill** accumulates guidance for every technique that
falls under its domain. A `debugging` skill, written honestly, ends up with:

- Root-cause tracing (~800 tokens)
- Defense in depth (~600 tokens)
- Systematic methodology (~700 tokens)
- Verification before completion (~500 tokens)
- Plus a shared preamble and rules (~300 tokens)

That is **~2,900 tokens** injected into the system prompt on every run,
regardless of what the user asked. For a user who only wants the
verification checklist, 80% of that is noise.

Two failure modes emerge as skills multiply:

1. **Token bloat.** Ten deep skills × 2,900 tokens = ~29,000 tokens of
   guidance on every message, regardless of relevance.
2. **Context dilution.** A model reading 29,000 tokens of skills to answer
   a 20-token question performs worse than a model reading 800 tokens of
   precisely the right guidance. Attention is finite.

The naive fix — "just don't inject the whole skill, let the model pick" —
does not work, because the model cannot pick what it cannot see. Without a
catalog, it does not know what skills exist.

---

## 2. The pattern

The router pattern splits a deep skill into two levels:

```
parent (router)
├── sub-skill A
├── sub-skill B
├── sub-skill C
└── sub-skill D
```

- The **parent** is a short dispatch prompt. It lists the sub-skills,
  describes when to use each, and instructs the model to fetch the chosen
  one. Its body is **~200–800 tokens**.
- Each **sub-skill** is a complete `SKILL.md` with its own instructions,
  output format, and trigger keywords. Bodies are **~400–2,000 tokens**.

### Token economics

| Scenario | Legacy (single-file) | Router |
|----------|----------------------|--------|
| Load parent | — | 300 tokens |
| Load chosen sub-skill | — | 800 tokens |
| **Total per run (single aspect)** | **2,900 tokens** | **1,100 tokens** |
| **Total per run (two aspects)** | 2,900 tokens | 1,900 tokens |
| **Marginal cost per additional aspect** | 0 | +800 tokens |

The router wins because **the model only pays for the aspects it actually
uses**. A two-aspect request costs less than the legacy skill. A one-aspect
request costs much less. An unrelated request costs nothing (the router
never enters the catalog — see §9).

For a skill used daily across a team, the savings compound into the tens of
millions of tokens per year.

### The pattern in the wider system

The router pattern is **not a substitute** for Progressive Disclosure — it
is what makes Progressive Disclosure pay off at scale. Progressive
Disclosure selects the top-K skills to advertise in the catalog; the router
pattern splits each skill so the model can fetch only the parts it needs.
Used together, they deliver the ~90% token reduction described in the
original engineering plan.

See [progressive-disclosure.md](./progressive-disclosure.md) for the full
runtime picture.

---

## 3. Anatomy of a router skill

### 3.1 Directory layout

```
debugging/
├── manifest.json                                       # declares sub_skills
├── SKILL.md                                            # router body (short)
├── icon.svg                                            # optional
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

Every sub-skill lives under `<package_root>/sub_skills/<id>/SKILL.md`. Nested
folders under `sub_skills/` are allowed (e.g.
`sub_skills/root_cause_tracing/checklist/SKILL.md`), but the common layout
is one directory per sub-skill.

### 3.2 The three files that make a router

| File | Purpose | Size target |
|------|---------|-------------|
| `manifest.json` | Declares `sub_skills` array with IDs, descriptions, triggers, and instructions paths. | ~1–3 KiB |
| `SKILL.md` (parent) | The dispatch prompt: lists sub-skills, tells the model to call `load_sub_skill`. | < 2 KiB |
| `sub_skills/*/SKILL.md` (each) | A complete, self-contained instructions document for one aspect. | < 8 KiB each |

### 3.3 What makes a skill a "router"

A skill is a router **if and only if** its `manifest.json` declares a
non-empty `sub_skills` array. The parent `SKILL.md` body is the dispatch
prompt; the sub-skill bodies are the actual instructions.

A router with **zero sub-skills** is not a router — it is a regular skill
whose body happens to be short. The `has_sub_skills` flag in the API
(`GET /api/skills`) reflects `len(sub_skills) > 0`.

### 3.4 What the runtime exposes

For a router skill, the runtime exposes four capabilities:

1. **`load_skill(parent_id)`** — returns the parent body plus metadata:
   ```json
   {
     "ok": true,
     "skill_id": "debugging",
     "version": "1.0.0",
     "has_sub_skills": true,
     "sub_skills": ["root_cause_tracing", "defense_in_depth", "..."],
     "content": "--- BEGIN SKILL: debugging@1.0.0 ---\n# Debugging Router\n...\n--- END SKILL ---"
   }
   ```
2. **`load_sub_skill(parent_id, sub_skill_id)`** — returns the sub-skill body.
3. **`GET /api/skills/{id}/sub-skills`** — HTTP endpoint listing sub-skills.
4. **Store UI** — a `ROUTER · N` badge, a sub-skills section in the details
   drawer, and pencil-icon editing for each sub-skill.

---

## 4. Choosing sub-skill boundaries

The single most important design decision for a router is **where to draw
the boundaries**. A well-designed router dispatches cleanly on every prompt.
A poorly designed one leaves the model vacillating between two sub-skills.

### 4.1 The three properties of good boundaries

**Mutually exclusive.**

For any typical user request, exactly one sub-skill is the right answer. If
two sub-skills could both respond to the same request, the model will pick
nondeterministically, and different runs will produce different dispatch
decisions.

**Individually meaningful.**

Each sub-skill's body stands on its own. A reader who loaded only that
sub-skill (without the parent context) should still be able to act on it.
If a sub-skill's body starts with "as described above in the router", the
boundary is wrong.

**Differentiated by triggers.**

The `triggers` array for each sub-skill uses **distinct vocabulary**.
Overlapping triggers confuse BM25 and cause the wrong sub-skill to be
suggested at the catalog level.

### 4.2 Patterns that work

**Concern-based** — the most common and the most robust.

```
code_review/
├── correctness_analysis
├── security_audit
├── error_handling_review
└── maintainability_review
```

Each concern is a well-defined, mutually exclusive aspect of code review.
A user asking "is this vulnerable to SQL injection?" clearly maps to
`security_audit`. A user asking "why does this crash?" clearly maps to
`correctness_analysis`.

**Role-based** — good for skills that change output format based on who is
reading.

```
release_notes/
├── for_engineers
├── for_operators
└── for_executives
```

**Artifact-based** — good when the review or generation strategy differs
sharply per artifact type.

```
documentation/
├── api_reference
├── user_guide
└── architecture_doc
```

**Phase-based** — good for workflow skills where the phase is chosen by
context, not by the user.

```
deployment/
├── pre_flight_checks
├── rollout
└── post_deployment_verification
```

### 4.3 Patterns that do not work

**Severity levels as sub-skills.**

```
review/
├── critical_review
├── medium_review
└── low_review
```

Severity is an **output detail**, not a dispatch dimension. The model
cannot know the severity before reading the code. Worse, a real review
will surface critical, medium, AND low findings in a single pass — the
severity-based router forces it to pick one and miss the others.

**Language as sub-skills.**

```
review/
├── python_review
├── javascript_review
└── go_review
```

The language rarely changes the review approach; the checklist does. A
`python_review` and `javascript_review` will each be 80% identical. Merge
them into one sub-skill with language-specific examples in the body.

**Chronological phases as sub-skills** for skills where the phase is not
user-selectable.

```
debugging/
├── hypothesis_phase
├── test_phase
└── fix_phase
```

The user does not ask "please give me the test phase". They ask "why does
this crash?" — and the phase is chosen by the model during the debugging
workflow, not before it. Sub-skilling by phase fragments the workflow.

**Micro-skills.**

```
review/
├── check_null
├── check_bounds
├── check_types
├── check_concurrency
├── ... (50 more)
```

If each sub-skill is under **300 tokens**, the dispatch overhead exceeds
the token savings. The model could just read a single 3,000-token body.
The rule of thumb: **only split when each sub-skill is over ~400 tokens**.

**Fat sub-skills with a thin router.**

```
review/
├── everything_about_security  (8,000 tokens)
└── everything_else            (6,000 tokens)
```

If a sub-skill is 8,000 tokens, it should itself be a router. Nesting
routers is allowed (a sub-skill can declare its own `sub_skills` — see §11),
but two-way splits rarely pay off.

### 4.4 The 50 sub-skill ceiling

The `sub_skills` array is capped at 50 entries by the manifest schema. This
is a deliberate ceiling, not an arbitrary one. Beyond 50 sub-skills:

- The parent body's dispatch prompt itself becomes long enough that the
  token savings collapse.
- BM25's routing quality degrades (more candidates → more confusion).
- The model's decision quality degrades (more than ~10 choices and it
  starts ignoring options).

If you find yourself needing more than 50, split the parent into multiple
routers. E.g. instead of one `code_review` with 60 aspects, ship
`code_review_core` (20 aspects) and `code_review_advanced` (20 aspects).

---

## 5. Manifest declaration

The `sub_skills` array is what turns a skill into a router. It is declared
alongside the normal manifest fields.

### 5.1 Minimal example

```json
{
  "schema_version": "1.0.0",
  "id": "debugging",
  "version": "1.0.0",
  "name": "Debugging Toolkit",
  "description": "Systematic debugging with router-based sub-skill selection.",
  "execution": {
    "type": "instructions",
    "instructions_file": "SKILL.md"
  },
  "sub_skills": [
    {
      "id": "root_cause_tracing",
      "description": "Trace a bug to its origin with evidence (stack trace, logs, bisection).",
      "triggers": ["bug", "crash", "stack trace", "exception", "error"],
      "instructions_file": "sub_skills/root_cause_tracing/SKILL.md"
    },
    {
      "id": "defense_in_depth",
      "description": "Harden a fix with multiple defensive layers (guards, validation, tests).",
      "triggers": ["harden", "production", "edge case", "robust"],
      "instructions_file": "sub_skills/defense_in_depth/SKILL.md"
    }
  ]
}
```

### 5.2 Field reference

| Field | Required | Constraints |
|-------|----------|-------------|
| `id` | yes | `^[a-z0-9][a-z0-9_]{2,63}$`, unique within the parent |
| `description` | yes | 10–500 chars. Shown in the catalog, router body, and drawer. |
| `triggers` | no | Max 20, each 1–64 chars. Indexed by BM25. |
| `instructions_file` | yes | `^sub_skills/…\.md$`, no `..` segment |
| `capabilities.exclusive` | no | boolean, default `false`. Reserved. |

### 5.3 Validation rules

Every `sub_skills` entry is validated at install time and at load time. The
following are rejected:

- `id` that does not match the pattern, or that duplicates a sibling.
- `description` shorter than 10 chars or longer than 500.
- `triggers` items that are empty, longer than 64 chars, or exceed 20 total.
- `instructions_file` that does not start with `sub_skills/`, does not
  end with `.md`, or contains `..`.
- More than 50 sub-skills total.
- Anything that makes the manifest fail JSON Schema validation.

Rejected manifests return a structured error with `details.reason`
(`duplicate_id`, `invalid_pattern`, `path_traversal`, `too_many`, …) so
authors can pinpoint the exact violation.

### 5.4 Sub-skill path containment

The `instructions_file` path is validated at three levels:

1. **Schema pattern** — must match `^sub_skills/…\.md$`.
2. **Explicit `..` check** in `_validate_sub_skills` — the pattern itself
   permits `.` in filenames, so a literal `..` segment is checked
   separately.
3. **Runtime containment** in `load_sub_skill` — every call resolves the
   path and asserts `candidate.relative_to(package_dir)`. This protects
   against any future code path that mutates `record.sub_skills` in
   memory without going through the validator.

All three layers run on every access. A sub-skill file that escapes the
parent package is rejected with `SKILL_SECURITY_VIOLATION`, regardless of
how the manifest was produced.

---

## 6. The router body

The parent `SKILL.md` is a **short dispatch prompt**. It does not contain
the actual review instructions — those live in the sub-skills.

### 6.1 Structure

A well-formed router body has four sections:

1. **Intro** — one to three sentences establishing the role.
2. **Sub-Skills** — one `### <id>` header per sub-skill, followed by a
   "Use when:" line.
3. **How to Activate** — an explicit call to `load_sub_skill` with the
   parent ID and the sub-skill ID. This is what makes the router work; a
   vague instruction here causes the model to guess and miss.
4. **Global Output Rules** (optional) — shared requirements that apply to
   all sub-skills. Declaring them once keeps each sub-skill body shorter.

### 6.2 Skeleton

```markdown
---
name: debugging
description: Systematic debugging with router-based sub-skill selection.
---

# Debugging Router

You have access to **four** specialized debugging sub-skills. Read the
user's request carefully and select the single most relevant one.

## Sub-Skills

### 1. root_cause_tracing
Use when: The user has a bug or crash and needs to find **WHY** it
happens. The request is about "trace this to its origin".

### 2. defense_in_depth
Use when: A fix exists and needs **hardening** for production.

### 3. systematic_debugging
Use when: The bug is **hard to reproduce** or non-deterministic.

### 4. verification_before_completion
Use when: A fix is in place and needs **verification** before
declaring done.

## How to Activate

Call `load_sub_skill(parent_id="debugging", sub_skill_id="<chosen>")`.

Do **NOT** try to load more than one sub-skill in the same turn unless
the user's request explicitly covers multiple aspects. Prefer ONE.

## Global Output Rules

Every finding MUST include:

1. **Location**: file:line reference.
2. **Severity**: Critical / High / Medium / Low.
3. **Evidence**: a quoted excerpt from the code.
4. **Impact**: what goes wrong if not fixed.
5. **Fix**: a concrete code change.

**Never report a finding without evidence.**
```

### 6.3 Length target

**Under 2 KiB** (roughly 500 tokens). Every byte of the router body is
paid on every `load_skill` call, so the cost compounds. If the router body
grows beyond 2 KiB:

- The sub-skill descriptions are too long → trim to one line each.
- The global output rules are too detailed → move them into the sub-skills.
- The intro is repeating context that the model already has → trim.

### 6.4 What NOT to put in the router body

- **The actual instructions.** Those belong in sub-skills.
- **Full checklists.** Same reason.
- **Any reference to a specific sub-skill's content.** The router is
  agnostic to what each sub-skill says.
- **The sub-skill bodies themselves** as examples. The model does not
  need them; the catalog and the router body's descriptions are enough.

### 6.5 Why "Prefer ONE" matters

Small and mid-sized models, when faced with a multi-choice dispatch, often
try to be thorough by loading **two** sub-skills "just in case". This
defeats the entire token-economics argument.

The router body must explicitly say **"Prefer ONE"** (or equivalent). It
is one sentence, but it changes the model's behavior significantly on
smaller models. Test with a 7B model — if it loads two sub-skills per
turn, the router body is not directive enough.

Multi-load is still valid when the user's request genuinely spans two
aspects (e.g. "review this for security and maintainability"), and the
router body should acknowledge that case. But it should present single-load
as the default.

---

## 7. Sub-skill bodies

A sub-skill is a **complete, self-contained instructions document**. It has
its own YAML frontmatter, its own structure, and its own output format.

### 7.1 Frontmatter

```markdown
---
name: security_audit
description: Identify security vulnerabilities — injection, secrets, auth bypass.
---
```

The `name` should match the sub-skill `id` from the manifest. The
`description` should match the manifest's `description` (or a truncated
version of it). These are informational — the manifest is authoritative —
but keeping them in sync avoids confusion in the drawer and the editor.

### 7.2 Structure

A well-formed sub-skill body usually has:

1. **One-sentence purpose.** "You are reviewing code for security..."
2. **Checklist / threat categories / method.** The concrete guidance.
3. **Output format.** A concrete template the model can follow.
4. **Rules.** Dos and don'ts specific to this sub-skill.

If the router body declared a **global output format**, the sub-skill can
omit it (or just extend it).

### 7.3 Self-containment

The sub-skill body **must not reference the parent or sibling sub-skills**.
A reader with only that sub-skill body should be able to act on it:

**Bad:**
```markdown
As described in the router, you should look for security issues.
This builds on the correctness analysis.
```

**Good:**
```markdown
You are reviewing code for security vulnerabilities. Focus on:
1. Injection (SQL, command, LDAP, ...)
2. Secrets
3. Auth bypass
...
```

### 7.4 Output format

Sub-skills with a shared output format should declare it **once**, in the
router body's `## Global Output Rules` section. Sub-skills then just
reference the categories.

Sub-skills with **distinct** output formats should declare their own. E.g.:

- `correctness_analysis` output: `[Severity] Title | Location | Evidence | Fix`.
- `security_audit` output: `[Severity] Category: Title | Location | Evidence | Attack | Impact | Fix`.
- `maintainability_review` output: `[Severity] Title | Location | Evidence | Cost | Refactor`.

Different output shapes are a **feature** of routers — each sub-skill is
optimized for its concern.

### 7.5 Length target

**Under 8 KiB** (roughly 2,000 tokens). This matches the guidance for
simple skills. Sub-skills larger than 8 KiB should themselves be routers
(see §11 on nesting).

### 7.6 Testing a sub-skill in isolation

A well-written sub-skill should produce a usable answer **without loading
the router body first**. To test:

1. Open a chat with an agent.
2. Ask the model to invoke `load_sub_skill("parent_id", "sub_id")` explicitly
   (or use a prompt that clearly maps to it).
3. Verify the answer follows the sub-skill's output format and rules.

If the answer is missing context that only the router provided, the
sub-skill is not self-contained.

---

## 8. Runtime dispatch flow

The complete flow when a user's message matches a router skill, with
Progressive Disclosure enabled:

```
1. User sends a message to an agent.
        ↓
2. Engine builds the catalog:
   - BM25-ranks all enabled skills against the message.
   - Picks top-K (default 5).
   - Includes explicitly invoked skills (via /).
   - Renders a catalog block listing id + name + description +
     sub-skill names/descriptions for routers.
        ↓
3. Engine appends the catalog to the system prompt
   under [SKILL INSTRUCTIONS].
        ↓
4. Engine registers skill tools:
   load_skill, load_sub_skill, load_reference.
        ↓
5. Model sees the catalog. It picks a skill that seems relevant.
        ↓
6. Model calls load_skill("debugging").
        ↓
7. Tool returns:
   --- BEGIN SKILL: debugging@1.0.0 ---
   # Debugging Router
   ... (the router body)
   --- END SKILL: debugging@1.0.0 ---
   + metadata: has_sub_skills=true, sub_skills=[...]
        ↓
8. Model reads the router body. It picks a sub-skill.
        ↓
9. Model calls load_sub_skill("debugging", "root_cause_tracing").
        ↓
10. Tool returns:
    --- BEGIN SUB-SKILL: debugging/root_cause_tracing ---
    # Root Cause Tracing
    ... (the sub-skill body)
    --- END SUB-SKILL: debugging/root_cause_tracing ---
        ↓
11. Model generates the final answer, following the sub-skill's
    output format.
        ↓
12. Engine collects the tool calls and dispatches them as SSE events
    to the frontend (TOOL_CALL, OBSERVATION, CHUNK, FINAL).
```

### 8.1 Why the two-step flow

The two-step flow (`load_skill` then `load_sub_skill`) exists because the
model needs to know the *available* sub-skill IDs before choosing one. The
catalog provides the *parent* ID; the router body provides the *sub-skill*
IDs. Neither is enough on its own.

A one-step alternative — where the catalog lists every sub-skill globally,
and the model calls `load_sub_skill` directly — was considered and rejected.
It would require listing every sub-skill of every skill in the catalog,
which is exactly the token bloat the router pattern was designed to avoid.

### 8.2 Event visibility

In the SSE stream, both tool calls are visible:

```
event: tool_call
data: {"name": "load_skill", "arguments": "{\"skill_id\": \"debugging\"}"}

event: observation
data: {"tool_name": "load_skill", "output": "--- BEGIN SKILL: debugging@1.0.0 ---..."}

event: tool_call
data: {"name": "load_sub_skill", "arguments": "{\"parent_id\": \"debugging\", \"sub_skill_id\": \"root_cause_tracing\"}"}

event: observation
data: {"tool_name": "load_sub_skill", "output": "--- BEGIN SUB-SKILL: debugging/root_cause_tracing ---..."}

event: final
data: {"result": "...final answer..."}
```

This visibility is intentional — operators and users can see exactly which
skills were loaded and why. It also makes debugging router dispatch easy.

### 8.3 Legacy mode (Progressive Disclosure disabled)

When `PELDRUN_PROGRESSIVE_DISCLOSURE=false`:

- No catalog.
- No skill tools.
- The router body (only) is injected for any assigned or invoked router.
- Sub-skills are **not** loaded — the model has no way to fetch them.
- The model sees the router body and is **expected to answer from it
  alone**.

This means **routers do not work correctly in legacy mode**. The router
body is a dispatch prompt, not an instruction document. If the operator
disables Progressive Disclosure, they should also not assign router skills
to agents — the model will read the dispatch prompt and produce a poor
answer.

The feature flag exists for backward compatibility with non-router skills,
not for routers. See [progressive-disclosure.md](./progressive-disclosure.md)
for the full migration guidance.

---

## 9. BM25 interaction

Routers interact with BM25 in a specific way that authors should understand.

### 9.1 The parent is indexed, not the sub-skills

BM25 builds a corpus for each **skill**. For a router skill, the corpus
is built from:

- The parent's `id`, `name`, `description`, `short_description`, `tags`.
- **All sub-skills' `triggers`** (hoisted to the parent).

Sub-skill `description` fields are **not** indexed directly by BM25 — only
their `triggers`. This is deliberate:

- Sub-skill descriptions are already reflected in the parent via the
  catalog.
- Triggers exist precisely to catch keyword matches.
- Indexing both would double-count.

**Practical consequence**: if you want BM25 to match a specific term to
a router skill, put that term in either the parent's `description` /
`tags` or in a sub-skill's `triggers`.

### 9.2 Trigger design

Because sub-skill triggers are hoisted to the parent, two rules apply:

**Do**:

- Add distinctive keywords to each sub-skill's `triggers`:
  ```json
  "triggers": ["injection", "xss", "sql", "auth", "token", "secret"]
  ```
- Include domain-specific jargon the model might not infer from the
  description:
  ```json
  "triggers": ["off-by-one", "null", "undefined", "edge case"]
  ```
- Include synonyms for the user's likely vocabulary:
  ```json
  "triggers": ["bug", "defect", "issue", "error", "crash", "exception"]
  ```

**Do not**:

- Repeat the same trigger in multiple sub-skills. It dilutes the signal
  at the parent level.
- Include common English words (`"the"`, `"is"`, `"for"`). They are
  already filtered by the stopword list.
- Include multi-word phrases as single triggers. BM25 tokenizes on
  whitespace anyway; `"memory leak"` is treated as two separate tokens.

### 9.3 Ranking implications

The router pattern has a **subtle but important** interaction with BM25:
because triggers are hoisted, a query that matches **any** sub-skill's
trigger will rank the **parent** higher. This is exactly what you want —
the parent should be in the top-K whenever any of its sub-skills would be
relevant, because the parent is what exposes the sub-skill list.

However, it also means that **a very trigger-heavy router can dominate the
top-K** at the expense of genuinely unrelated skills. If your `code_review`
has 4 sub-skills × 8 triggers each = 32 indexed terms, it will outrank a
documentation skill on almost any programming-adjacent query.

**Mitigation**: keep triggers focused. 5–8 distinct, high-signal keywords
per sub-skill is plenty. 20 (the maximum) is almost never necessary and
hurts recall for other skills.

### 9.4 Testing BM25 for routers

Before publishing, verify that the router ranks correctly:

```python
from omweb.skills.registry import skill_registry
from omweb.skills.router.bm25_router import BM25SkillRouter

skills = skill_registry.list_skills(include_disabled=False)
router = BM25SkillRouter(skills)

# Each of these should rank code_review at position 0
for query in [
    "audit for SQL injection",
    "this function is too complex",
    "the retry logic swallows exceptions",
    "review my code for bugs",
]:
    top = router.rank(query, top_k=1)
    assert top and top[0][0].id == "code_review", f"Failed: {query!r} -> {top}"

# Unrelated queries should return []
for query in ["what is the weather", "tell me a joke"]:
    assert router.rank(query, top_k=1) == [], f"False positive: {query!r}"
```

If a query you expect to match does not, add a trigger that captures its
distinctive term. If an unrelated query matches, remove the trigger that
is causing the false positive.

### 9.5 The tiny-corpus fallback

When only 1–2 skills are installed, BM25's IDF goes negative and the router
falls back to simple token-overlap counting. This is transparent to router
authors but explains a behavioral quirk: in a two-skill corpus, a single
overlap term is enough to rank a skill. If you are testing a router against
only one other skill and it appears to over-match, install two more skills
or use `use_bm25=False` to see the token-overlap ranking directly.

See [progressive-disclosure.md](./progressive-disclosure.md) §6 for the
implementation details.

---

## 10. Editing sub-skills from the UI

Operators can edit **both** the router body and each sub-skill body from the
Store UI, without touching the filesystem or reinstalling the package.

### 10.1 Editing the router body

1. Open `/stores` → Skills.
2. Click the **pencil icon (✏)** on the skill card.
3. The editor opens with the title `Edit — Code Review`.
4. Edit the body. Frontmatter is shown read-only above the editor.
5. Click **Save Changes**.

The save hits `PUT /api/skills/{skill_id}/instructions`. Frontmatter is
preserved server-side.

### 10.2 Editing a sub-skill body

1. Open `/stores` → Skills.
2. Click **ⓘ** on the router skill's card to open the drawer.
3. In the **Sub-Skills** section, click the **pencil icon** next to any
   sub-skill row.
4. The editor opens with the title `Edit — Code Review › security_audit`.
   The subtitle confirms `editing sub-skill`.
5. Edit the body. The sub-skill's own frontmatter is shown read-only.
6. Click **Save Changes**.

The save hits
`PUT /api/skills/{skill_id}/sub-skills/{sub_skill_id}/instructions`.

### 10.3 What cannot be edited

From the UI, you can edit **only the body** of a `SKILL.md` file. You
**cannot** edit:

- The `sub_skills` array in `manifest.json` (adding, removing, or renaming
  sub-skills).
- The sub-skill IDs.
- The sub-skill `triggers`.
- The router's `description`, `tags`, or `capabilities`.
- The frontmatter of any `SKILL.md` file.

Changing any of the above requires editing the source package and
reinstalling it. This is deliberate: `sub_skills` metadata is indexed by
BM25 and referenced by historical SSE events, so changing it at runtime
would break reproducibility.

### 10.4 The editor contract

The editor enforces the same validation as the install pipeline:

- Body must be non-empty.
- Body must not start with `---` (no frontmatter duplication).
- Body must fit `limits.max_instructions_bytes`.
- The recomposed file (frontmatter + new body) must re-parse cleanly
  through `parse_skill_md`.

Failed saves return `422` with a `details.reason` field pinpointing the
cause. The editor shows the error as a toast and keeps the modal open so
the operator can fix the issue.

### 10.5 Saving effect on runtime

After a successful save:

- The registry is refreshed.
- The next `load_skill` / `load_sub_skill` call from the model reads the
  new body from disk (no cache to invalidate).
- No restart or reinstall is required.

The save affects the **next run**, not the current one. If a run is
in-flight when the save happens, the model's tool call will read the new
body (since the loader is not cached). This is usually fine — mid-flight
edits are rare — but it means a run's behavior can depend on wall-clock
timing if two operators edit concurrently.

---

## 11. Versioning and evolution

Routers evolve over time — new sub-skills are added, old ones are retired,
boundaries are redrawn. The version bump rules differ from simple skills
because sub-skill IDs are part of the **public contract**.

### 11.1 When to bump

| Change | Bump | Rationale |
|--------|------|-----------|
| Fix a typo in a sub-skill body | **Patch** | Behavior unchanged. |
| Improve a sub-skill's output format | **Patch** | Better output for the same inputs. |
| Add a new sub-skill | **Minor** | Non-breaking; existing dispatch paths unchanged. |
| Add a trigger to an existing sub-skill | **Minor** | Non-breaking; may improve routing recall. |
| Rewrite a sub-skill's body entirely | **Patch or Minor** | Patch if the output shape is preserved; Minor if improved. |
| **Rename a sub-skill ID** | **Major** | Breaks historical references. |
| **Remove a sub-skill** | **Major** | Breaks dispatch for prior prompts. |
| **Change a sub-skill to `exclusive`** | **Minor** | Non-breaking today (unused). |
| **Turn a simple skill into a router** | **Major** | Changes the parent body's structure. |

### 11.2 Renaming a sub-skill

Sub-skill IDs appear in:

- Historical SSE events (tool call arguments).
- Run traces (the run diagnostic metadata).
- Any user documentation that mentions the sub-skill by name.

Renaming a sub-skill is a **breaking change** in the strict semver sense.
The correct procedure:

1. Publish a new **major** version.
2. Keep the old sub-skill available under the old ID for at least one
   minor version of overlap (so historical traces still resolve).
3. Update the router body to point to the new ID.
4. Document the rename in the changelog.

If the rename is a genuine replacement (not just a cleaner name), the
`replaces` field in the manifest can be used to declare the migration.

### 11.3 Removing a sub-skill

Same as renaming — major bump. There is no way to "deprecate" a sub-skill
independently; the parent is either installed with that sub-skill or it is
not.

If the sub-skill's content is being merged into a sibling, the correct
move is:

1. Copy the content into the sibling's body.
2. Add a trigger to the sibling that covers the old sub-skill's domain.
3. Remove the old sub-skill in a major version bump.

### 11.4 Nested routers

A sub-skill can itself be a router — i.e., its body is a dispatch prompt
for its own sub-sub-skills. This is valid but rarely useful. The pattern
only pays off when:

- The sub-skill is large enough (> 2,000 tokens) to justify splitting.
- The sub-skill has clear sub-boundaries.

In practice, **two levels of nesting is the practical maximum**. A third
level of nesting is a sign the skill design needs rethinking.

If you do nest, the API and runtime support it transparently:

- `load_sub_skill("parent_id", "sub_id")` returns the sub-skill body (which
  is itself a router).
- The model then calls
  `load_sub_skill("parent_id", "sub_id", "sub_sub_id")` — wait, this is
  not yet supported in the runtime.

**Correction**: The current runtime does **not** support nested routers.
`load_sub_skill` accepts only `(parent_id, sub_skill_id)`; there is no
third level. If you need to split a sub-skill further, promote it to a
top-level router and link it from the parent via a sibling sub-skill.

### 11.5 Backward compatibility guarantees

Across minor versions, the following are guaranteed stable:

- Sub-skill IDs (renames require major bumps).
- The `load_sub_skill` contract (`parent_id`, `sub_skill_id` → body).
- The `/api/skills/{id}/sub-skills` response shape.
- The catalog rendering format for router entries.

Across patch versions, everything is stable.

Across major versions, anything may change.

---

## 12. Testing a router

### 12.1 Structural tests

Verify the manifest is well-formed and every declared file exists:

```python
import json
from pathlib import Path

pkg = Path("backend/storage/store/skills/code_review")
manifest = json.loads((pkg / "manifest.json").read_text())

assert len(manifest["sub_skills"]) == 4
ids = {s["id"] for s in manifest["sub_skills"]}
assert ids == {
    "correctness_analysis", "security_audit",
    "error_handling_review", "maintainability_review",
}

for sub in manifest["sub_skills"]:
    rel = sub["instructions_file"]
    target = (pkg / rel).resolve()
    target.relative_to(pkg.resolve())  # containment
    assert target.is_file(), f"Missing: {rel}"
    print(f"OK: {sub['id']} -> {rel} ({target.stat().st_size} bytes)")
```

### 12.2 Install-time tests

After installing the router via `POST /api/skills/install`, verify:

```python
from fastapi.testclient import TestClient
from omweb.main import app

client = TestClient(app)

# 1. Router is registered with correct sub_skills_count
r = client.get("/api/skills/code_review").json()
assert r["skill"]["has_sub_skills"] is True
assert r["skill"]["sub_skills_count"] == 4

# 2. Detail endpoint exposes the sub_skills array (without
#    instructions_file — that is deliberately hidden)
subs = r["skill"]["sub_skills"]
assert len(subs) == 4
for sub in subs:
    assert "instructions_file" not in sub, "instructions_file must NOT leak"

# 3. Dedicated sub-skills endpoint
r2 = client.get("/api/skills/code_review/sub-skills").json()
assert r2["has_sub_skills"] is True
assert r2["count"] == 4
for sub in r2["sub_skills"]:
    assert sub["triggers"], f"{sub['id']} has no triggers"
```

### 12.3 Tool-level tests

Verify the three skill tools work correctly against the router:

```python
from omweb.skills.tools.load_skill import execute as load_skill
from omweb.skills.tools.load_sub_skill import execute as load_sub

# 1. Parent loads with sub_skills metadata
res = load_skill("code_review")
assert res["ok"] is True
assert res["has_sub_skills"] is True
assert len(res["sub_skills"]) == 4
assert "Code Review Router" in res["content"]

# 2. Every sub-skill loads
for sub_id in ["correctness_analysis", "security_audit",
                "error_handling_review", "maintainability_review"]:
    res = load_sub("code_review", sub_id)
    assert res["ok"] is True, res
    assert res["content"].startswith(
        f"--- BEGIN SUB-SKILL: code_review/{sub_id} ---"
    )
    print(f"OK: {sub_id} ({len(res['content'])} chars)")

# 3. Unknown sub-skill returns SUB_SKILL_NOT_FOUND with available list
res = load_sub("code_review", "nonexistent")
assert res["ok"] is False
assert res["code"] == "SUB_SKILL_NOT_FOUND"
assert len(res["available"]) == 4

# 4. Non-router parent returns NOT_A_ROUTER (if you have a simple skill
#    installed alongside the router)
res = load_sub("simple_skill_id", "anything")
assert res["ok"] is False
assert res["code"] == "NOT_A_ROUTER"
```

### 12.4 BM25 ranking tests

```python
from omweb.skills.registry import skill_registry
from omweb.skills.router.bm25_router import BM25SkillRouter

router = BM25SkillRouter(skill_registry.list_skills())

for query in [
    "audit for SQL injection",
    "this function is too complex",
    "the retry logic swallows exceptions",
    "my code has a null pointer bug",
]:
    top = router.rank(query, top_k=1)
    assert top and top[0][0].id == "code_review", f"{query!r} -> {top}"

for query in ["what is the weather", "hello world"]:
    assert router.rank(query, top_k=1) == [], f"False positive: {query!r}"
```

### 12.5 Catalog-level tests

```python
from omweb.skills.assignments import resolve_skill_injection_catalog

inj = resolve_skill_injection_catalog(
    agent_id="peldrun",
    invoked_skill_ids=[],
    user_message="audit for SQL injection",
)

assert "code_review" in inj.text
assert "security_audit" in inj.text
assert "load_skill" in inj.text
assert "load_sub_skill" in inj.text
assert "AVAILABLE SKILLS" in inj.text
```

### 12.6 End-to-end tests

The definitive test — a full agent run — cannot be automated at the unit
level. Perform manually:

1. Open the frontend at `/stores` → Skills.
2. Confirm the `code_review` card shows the **`ROUTER · 4`** violet badge.
3. Click **ⓘ** to open the drawer. Confirm the **Sub-Skills** section
   lists all four sub-skills with triggers and pencil icons.
4. Open a chat with an agent.
5. Send: `"Audit this Python code for SQL injection: cursor.execute(f\"SELECT * FROM users WHERE id = {user_id}\")"`
6. In DevTools → Network → SSE stream, confirm:
   - `TOOL_CALL: load_skill({"skill_id": "code_review"})`
   - `TOOL_CALL: load_sub_skill({"parent_id": "code_review", "sub_skill_id": "security_audit"})`
   - `FINAL` with an answer that follows `security_audit`'s output format.

If the model picks a different sub-skill, see §15.

### 12.7 Editing tests

1. In the drawer, click the pencil on `security_audit`.
2. Confirm the editor title is `Edit — Code Review › security_audit`.
3. Add a temporary line (`## Test marker`).
4. Save. Confirm the toast: `Sub-skill updated — Code Review › security_audit instructions saved.`
5. Reopen the editor. Confirm the marker is present.
6. Remove the marker. Save again.
7. Verify on disk: `security_audit/SKILL.md` no longer contains the marker.
8. Verify via `load_sub_skill`: the content is unchanged from the original.

---

## 13. Anti-patterns

### 13.1 Micro-routers

Splitting a 500-token skill into four 125-token sub-skills. The dispatch
overhead (parent body + one `load_skill` + one `load_sub_skill` round-trip)
exceeds the savings. **Rule of thumb**: only split when each sub-skill is
over ~400 tokens.

### 13.2 Overlapping boundaries

Two sub-skills that could both respond to the same prompt. The model will
vacillate between them, and different runs will produce different
dispatches. See §4.3 for examples (severity, language, chronological
phases).

### 13.3 Missing "Prefer ONE"

A router body that says "pick the right sub-skill" without explicitly
telling the model to prefer single loads. Smaller models will load two
sub-skills "just in case" and the token savings evaporate.

### 13.4 Referencing siblings from a sub-skill

A sub-skill body that says "as described in the correctness analysis".
Sub-skills must be self-contained. If they need context, either move that
context into the router body's global rules or duplicate it in each
sub-skill.

### 13.5 Router body too long

A router body that is 4–6 KiB because it embeds full checklists. The whole
point of a router is a short dispatch prompt. If the router body is
approaching the size of a sub-skill, the split is not paying off.

### 13.6 Nested routers

A router whose sub-skill is itself a router. The current runtime does not
support three-level dispatch. Two levels is the practical maximum; three
is a design smell.

### 13.7 Using `sub_skills` without `load_sub_skill` support

If the runtime is configured with `PELDRUN_PROGRESSIVE_DISCLOSURE=false`,
routers do not work — the model sees only the router body and cannot fetch
sub-skills. If you are shipping a router, you must ensure the deployment
has Progressive Disclosure enabled. See
[progressive-disclosure.md](./progressive-disclosure.md) §3 for the flag
behavior.

### 13.8 Sub-skill triggers that repeat the parent's vocabulary

If every sub-skill's triggers include `"review"`, `"code"`, `"check"`,
BM25 has no signal to distinguish them. Reserve `"review"` and `"code"` for
the **parent's** tags/description, and use **distinct** terms in each
sub-skill's triggers.

### 13.9 Versioning churn

Renaming a sub-skill in a patch bump. Sub-skill IDs are part of the public
contract (see §11). Renames require a major bump.

### 13.10 Editing sub-skill content without bumping the version

If you edit a sub-skill from the UI (which updates the file on disk) and
do not bump the manifest's `version`, then:

- The `.installed.json` still reflects the old version.
- Any operator who reinstalls the package will lose the edit.
- The Store UI shows the old version, which is misleading.

For development iterations this is fine. For anything that will be used
by a team, bump the manifest and reinstall.

---

## 14. Complete example: `code_review` v2

The canonical router shipped with PELDRUN is `code_review` v2.0.0. It is a
working example of every feature of the router pattern.

### 14.1 Structure

```
code_review/
├── manifest.json                       # 3,109 bytes
├── SKILL.md                            # router body (~2.0 KiB)
└── sub_skills/
    ├── correctness_analysis/SKILL.md   # 1,845 bytes
    ├── security_audit/SKILL.md         # 1,953 bytes
    ├── error_handling_review/SKILL.md  # 2,022 bytes
    └── maintainability_review/SKILL.md # 1,993 bytes
```

### 14.2 The manifest (abbreviated)

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
      "description": "Identify logic bugs, edge cases, off-by-one errors, incorrect control flow.",
      "triggers": ["bug", "logic", "off-by-one", "null", "undefined",
                    "edge case", "crash", "correctness"],
      "instructions_file": "sub_skills/correctness_analysis/SKILL.md"
    },
    {
      "id": "security_audit",
      "description": "Identify security vulnerabilities — injection, secrets, auth bypass.",
      "triggers": ["security", "injection", "xss", "sql", "auth",
                    "token", "secret", "vulnerability"],
      "instructions_file": "sub_skills/security_audit/SKILL.md"
    },
    {
      "id": "error_handling_review",
      "description": "Review exception hierarchies, retry logic, fallbacks, error propagation.",
      "triggers": ["exception", "error", "try", "catch", "retry",
                    "fallback", "logging"],
      "instructions_file": "sub_skills/error_handling_review/SKILL.md"
    },
    {
      "id": "maintainability_review",
      "description": "Review code for clarity, coupling, duplication, naming.",
      "triggers": ["refactor", "complexity", "naming", "duplication",
                    "maintainability", "readability"],
      "instructions_file": "sub_skills/maintainability_review/SKILL.md"
    }
  ],
  "compatibility": { "peldrun_min_version": "0.2.0" },
  "limits": { "max_instructions_bytes": 262144 },
  "user_editable": true
}
```

### 14.3 The router body

See §6.2 for the full body. The key sections:

- `## Sub-Skills` — one `### <id>` per sub-skill with a "Use when:" line.
- `## How to Activate` — explicit `load_sub_skill` call.
- `## Global Output Rules` — the shared output template.

### 14.4 Runtime trace

A complete run for the query `"Audit this Python code for SQL injection:
cursor.execute(f'SELECT * FROM users WHERE id = {user_id}')"`:

```
STEP 1 — Catalog injection
  System prompt includes:
    AVAILABLE SKILLS:
    - id='code_review' | name='Code Review' | ...
      sub-skills:
        * sub_skill='correctness_analysis': ...
        * sub_skill='security_audit': ...
        ...

STEP 2 — Model calls load_skill
  TOOL_CALL: load_skill({"skill_id": "code_review"})
  OBSERVATION:
    --- BEGIN SKILL: code_review@2.0.0 ---
    # Code Review Router
    ...
    --- END SKILL: code_review@2.0.0 ---

STEP 3 — Model calls load_sub_skill
  TOOL_CALL: load_sub_skill({"parent_id": "code_review",
                              "sub_skill_id": "security_audit"})
  OBSERVATION:
    --- BEGIN SUB-SKILL: code_review/security_audit ---
    # Security Audit
    ...
    --- END SUB-SKILL: code_review/security_audit ---

STEP 4 — Model generates the final answer
  Following security_audit's output format:
    ## [Critical] Injection: SQL injection
    **Location**: get_user.py:42
    **Evidence**: cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
    **Attack**: "1' OR '1'='1"
    **Impact**: returns all users
    **Fix**: use parameterized query
  TOOL_CALL: terminate
```

Total tokens: ~9,800 (including catalog, two tool responses, and the final
answer). Compare to legacy full injection of `code_review` v2 (would inject
the router body only — because the sub-skill bodies would not be fetchable,
the model would give a poorer answer). Or to legacy full injection of the
pre-router `code_review` v1 (~3,200 tokens of instructions) — the router
version gives better answers at roughly the same cost.

### 14.5 What makes it work

Three design decisions distinguish this router:

1. **The sub-skills are concerns, not phases or severities.** Each is a
   distinct aspect of review, mutually exclusive by content.
2. **The router body is 2 KiB, not 8.** It fits comfortably in the catalog
   budget and leaves room for other skills.
3. **The global output format lives in the router body.** The sub-skills
   only need to reference it, so each is 1,800–2,000 bytes instead of
   3,000+.

---

## 15. Troubleshooting

### 15.1 The model loads `load_skill` but never `load_sub_skill`

**Symptom**: SSE stream shows one tool call, then `FINAL`.

**Causes**:

- **The router body does not instruct dispatch.** Check that the body
  contains an explicit `## How to Activate` section naming `load_sub_skill`.
- **The model is too small.** Sub-7B models often ignore dispatch
  instructions, especially when the router body is longer than ~500
  tokens. Try a larger model.
- **The user's message is ambiguous.** "Review this" does not clearly
  map to any sub-skill. Try a specific prompt: "audit for SQL injection".

**Fix**: re-test with a specific prompt and a 13B+ model. If it works,
the router is fine — the issue was the model or the prompt.

### 15.2 The model loads two sub-skills per turn

**Symptom**: SSE stream shows two `load_sub_skill` calls.

**Cause**: The router body does not strongly discourage multi-load.

**Fix**: Add an explicit line to the router body:

> Do **NOT** try to load more than one sub-skill in the same turn unless
> the user's request explicitly covers multiple aspects. Prefer ONE.

Then re-test. Small models respond well to capitalized directives.

### 15.3 The model picks the wrong sub-skill

**Symptom**: A security question triggers `correctness_analysis`.

**Causes**:

- **The router body's "Use when:" lines are ambiguous.** Rewrite them
  to be more specific and mutually exclusive.
- **The sub-skill `description` fields are vague.** Improve them in the
  manifest.
- **Triggers overlap.** Two sub-skills share a trigger. BM25 routes at
  the parent level, but the model's choice at the dispatch level depends
  on the sub-skill descriptions in the router body. Fix the
  descriptions first.

**Fix**: tighten both the router body descriptions and the manifest
descriptions. If the model still picks wrong, the boundaries are wrong
(see §4).

### 15.4 `load_sub_skill` returns `SUB_SKILL_NOT_FOUND`

**Symptom**: Tool call fails with `{"ok": false, "code": "SUB_SKILL_NOT_FOUND"}`.

**Cause**: The model passed a `sub_skill_id` that is not declared in the
manifest. Either the model hallucinated the ID, or a typo in the router
body.

**Diagnosis**: Check the error's `available` list — it contains the valid
IDs. Compare to the ID the model passed.

**Fix**:

- If the model hallucinated: shorten the IDs (longer IDs are harder to
  copy verbatim), improve the "Use when:" lines to be more discriminating.
- If the router body has a typo: fix the body via the editor.

### 15.5 `load_sub_skill` returns `SKILL_SECURITY_VIOLATION`

**Symptom**: Tool call fails with `SKILL_SECURITY_VIOLATION`.

**Cause**: The resolved sub-skill file path escapes the parent package.
This should not happen for a well-formed package — it typically indicates:

- A manifest was hand-edited after install.
- A test harness mutated `record.sub_skills` in memory.

**Diagnosis**: Check the backend log for the exact `rel` path and
`package_dir`.

**Fix**: reinstall the package from a trusted ZIP.

### 15.6 The router does not appear in the catalog

**Symptom**: The model never sees `code_review` in the AVAILABLE SKILLS
section.

**Causes**:

- **BM25 ranked it below top-K for this query.** Try a more specific
  prompt or add triggers.
- **Progressive Disclosure is disabled.** Check the startup log.
- **The skill is disabled or deprecated.** Check `GET /api/skills`.

**Fix**: invoke explicitly with `/code_review` — that bypasses BM25 and
forces the skill into the catalog for the current message.

### 15.7 The router body is corrupted

**Symptom**: The parent `SKILL.md` was edited and now fails to load.

**Cause**: Someone saved a body that does not fit the constraints (empty,
starts with `---`, oversized, fails parse).

**Fix**: The editor enforces these constraints, so a UI save cannot
corrupt the file. If the file was edited on disk, restore from a backup
or reinstall the package.

### 15.8 The sub-skill edit does not take effect

**Symptom**: Saved the sub-skill body, but `load_sub_skill` still returns
the old content.

**Causes**:

- **Wrong sub-skill ID.** Confirm the save targeted the right sub-skill
  (check the toast and the editor title).
- **Registry cache.** The registry is refreshed on save, but a concurrent
  run might have loaded the old body before the save. Check the next run.
- **In-flight run.** A run started before the save loaded the old body.
  The next run reads the new body.

**Fix**: run a new test message. If it still fails, check the file on disk:

```powershell
Get-Content "D:\AI\peldrun\backend\storage\store\skills\code_review\sub_skills\security_audit\SKILL.md"
```

If the marker is present in the file but not returned by
`load_sub_skill`, the runtime has a caching bug — file an issue.

### 15.9 BM25 ranks an unrelated skill higher than the router

**Symptom**: A review question ranks `documentation` above `code_review`.

**Causes**:

- **The unrelated skill has stronger trigger overlap.** Check the
  unrelated skill's tags and description.
- **The router's triggers are too narrow.** Add high-signal terms.

**Diagnosis**: use the BM25 diagnostic in §9.4 to see the actual rankings.

**Fix**: add distinctive triggers to the router that capture the missing
vocabulary.

### 15.10 The router pattern is not paying off

**Symptom**: A router does not save tokens compared to a single-file skill.

**Causes**:

- **The sub-skills are too small.** If each is under ~400 tokens, the
  dispatch overhead (parent body + two tool round-trips) is not worth it.
- **The router body is too long.** If the router body is approaching the
  size of a sub-skill, split is not saving anything.
- **The model loads multiple sub-skills per turn.** See §15.2.

**Fix**: measure the actual token usage per run (from the SSE stream's
`final` event, which reports `usage.tokens`) for both the router and the
equivalent single-file skill. If the router does not save 20% or more,
revert to a single file.

---

## Summary

The router pattern is a structural decision:

- **Use it** when a skill has distinct aspects, each meaningful on its own,
  and each > 400 tokens.
- **Do not use it** when the skill has one clear job or when the split
  would create micro-skills.
- **Design boundaries** that are mutually exclusive, individually
  meaningful, and differentiated by triggers.
- **Write the router body** as a short, explicit dispatch prompt. Under
  2 KiB. Include a "Prefer ONE" directive. List each sub-skill with a
  "Use when:" line.
- **Write sub-skills** as complete, self-contained documents. Under 8 KiB.
  Do not reference siblings or the parent.
- **Version carefully** — sub-skill IDs are part of the public contract.
  Renames require major bumps.
- **Test end-to-end** — verify the model actually dispatches through
  `load_skill` → `load_sub_skill` in a real agent run.

For the runtime details of how Progressive Disclosure and the skill tools
interact with routers, see
[progressive-disclosure.md](./progressive-disclosure.md). For the general
authoring reference, see [creating-skills.md](./creating-skills.md).
