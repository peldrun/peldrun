# PELDRUN Source Control Integration — Complete Engineering Guide

**Version:** 1.0 (October 2026)
**Audience:** Backend developers, frontend developers, QA engineers, DevOps
**Status:** Production-ready core, with documented debts for v2

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [The Five Layers](#2-the-five-layers)
3. [Request Lifecycle (End-to-End)](#3-request-lifecycle-end-to-end)
4. [Security Model — Approval Workflow](#4-security-model--approval-workflow)
5. [Operation State Machine](#5-operation-state-machine)
6. [Local Git Workspace](#6-local-git-workspace)
7. [HTTP Router Reference](#7-http-router-reference)
8. [Frontend Developer Guide](#8-frontend-developer-guide)
9. [Testing Guide](#9-testing-guide)
10. [Known Issues & Technical Debt](#10-known-issues--technical-debt)
11. [Roadmap — v2 and Next Phase](#11-roadmap--v2-and-next-phase)
12. [Troubleshooting Guide](#12-troubleshooting-guide)

---

## 1. System Overview

### 1.1 What is PELDRUN Source Control?

PELDRUN Source Control Integration (SCI) lets AI agents work with Git repositories (GitHub first, GitLab and Bitbucket later) **under strict human control**. An agent can:

- List repositories, read files, create branches
- Inspect diffs locally, run CI checks
- Request approval for remote writes
- Push approved branches and open pull requests

**Every remote write requires explicit human approval.** The system is **local-first**: Git operations run on the user's machine; only network calls go to the provider API.

### 1.2 The Core Principle

> **OAuth authorization does not equal consent to every future write.**

A token proves the *technical ability* to act. PELDRUN's local policy and per-operation approval decide *whether to act*.

### 1.3 The Two Halves

```
┌─────────────────────────────────────────────────────────────┐
│  "Do work locally"                                          │
│  ─────────────────                                          │
│  Clone → branch → edit → diff → test → approve → push       │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  "Only what the user approved goes remote"                  │
│  ─────────────────────────────────────────                  │
│  Approval ticket bound to exact diff → single-use → push    │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. The Five Layers

The system is built from **five independent layers**. Each layer has one job. Adding a new provider, tool, or test category touches exactly one layer.

```
┌──────────────────────────────────────────────────────────────┐
│ Layer 1: HTTP Router                                         │
│   omweb/routers/source_control.py                            │
│   Purpose: expose REST endpoints, translate errors to HTTP   │
├──────────────────────────────────────────────────────────────┤
│ Layer 2: Application Facade                                  │
│   omweb/services/source_control_facade.py                    │
│   Purpose: orchestrate services, enforce policy/approval     │
├──────────────────────────────────────────────────────────────┤
│ Layer 3: Integration Services                                │
│   integrations/source_control/                               │
│   Purpose: the actual work — Git, providers, security, ops   │
├──────────────────────────────────────────────────────────────┤
│ Layer 4: Core Tool Runtime                                   │
│   peldrun/tools/                                             │
│   Purpose: agent-facing tool interface (dispatch actions)    │
├──────────────────────────────────────────────────────────────┤
│ Layer 5: Web Tool Runtime                                    │
│   omweb/tools/  +  omweb/adapters/core_adapter_parts/        │
│   Purpose: JSON manifests, discovery, Web↔Core bridge        │
└──────────────────────────────────────────────────────────────┘
```

### 2.1 Layer 1 — HTTP Router

**File:** `backend/omweb/routers/source_control.py`

**Responsibility:** Convert HTTP requests into facade calls, and facade exceptions into HTTP responses. It has **no business logic**.

**Every endpoint:**
```python
@router.post("/operations/{operation_id}/approve")
async def approve_operation(
    operation_id: str,
    payload: ApproveOperationRequest,
    facade: SourceControlFacade = Depends(get_source_control_facade),
):
    try:
        return await facade.approve_operation(operation_id, payload)
    except Exception as exc:
        handle_source_control_error(exc)  # maps domain error → HTTP status
```

**Error mapping** (see `handle_source_control_error`):
- `AuthRequiredError` → 401
- `PermissionDeniedError` → 403
- `NotFoundError` → 404
- `ValidationError` → 422
- `RateLimitedError` → 429
- `ConflictError` → 409
- `RemoteStateChangedError` → 412
- Other `SourceControlError` → 502
- Any other exception → 500 (logged with full trace)

### 2.2 Layer 2 — Application Facade

**File:** `backend/omweb/services/source_control_facade.py`

**Responsibility:** Wire services together, enforce cross-cutting rules, maintain the connection registry. **No Git or HTTP code lives here** — it only *calls* other services.

**Key responsibilities:**
- `create_operation` — validate policy → create operation in journal
- `request_operation_approval` — compute diff → advance state → create ticket
- `approve_operation` / `reject_operation` — grant/revoke ticket → transition
- `push_approved_branch` — validate ticket → get token → push → transition
- `create_change_request` — validate ticket → open PR → transition
- `list_audit_events` — read audit log

**Connection registry:**
The facade stores `_connections` in `workspaces/source_control/connections.json`. This is **only** provider/account metadata; **tokens live in the OS vault**.

### 2.3 Layer 3 — Integration Services

**Folder:** `backend/integrations/source_control/`

This is the heart of the system. It contains **six sub-areas**:

| Sub-area | Purpose |
|---|---|
| `domain/` | Frozen dataclasses: `RepositoryRef`, `DiffSummary`, `ApprovalTicket`, etc. |
| `security/` | Vault, OAuth, approval gate, policy engine, audit log, redaction, token service |
| `git/` | Safe subprocess wrapper, workspace manager, diff engine, push manager, path policy |
| `operations/` | Durable journal, state machine, idempotency, recovery |
| `providers/` | Provider adapters (GitHub today, GitLab/Bitbucket later) |
| `tools/` | The 12 sub-tools the dispatcher calls |

**Key invariant:** Every class in this folder is **provider-neutral**. `SourceControlProvider` (a Protocol) is the only provider-specific interface.

### 2.4 Layer 4 — Core Tool Runtime

**Folder:** `backend/peldrun/tools/`

**Responsibility:** Provide the agent-facing tool interface. Agents call tools by name; the runtime validates arguments, enforces timeout policy, and returns a `ToolResult`.

**Key classes:**
- `BaseTool` — abstract base. Subclasses implement `_arun(**kwargs)`.
- `ToolResult` — Pydantic model returned from every tool.
- `ToolRegistry` — collection of tools available to the agent.
- `SourceControlTool` (`builtins/source_control_tool.py`) — the **dispatcher**.

**The dispatcher** is the agent's only entry point for source-control work:

```
Agent calls:    source_control(action="push_branch", operation_id="op_1", approval_id="app_1")
                ↓
Dispatcher:     _ACTION_MAP["push_branch"] → PushBranchTool
                ↓
Sub-tool:       run(ToolContext(operation_id="op_1"), approval_id="app_1")
                ↓
Facade-level:   facade.push_approved_branch("op_1", payload)
```

**Action aliases** the model may emit:
- `list_repos` → `list_repositories`
- `read` → `read_file`
- `diff` → `inspect_diff`
- `pr` → `create_change_request`
- ... (see `_ACTION_ALIASES` in `source_control_tool.py`)

### 2.5 Layer 5 — Web Tool Runtime

**Folder:** `backend/omweb/tools/`

**Responsibility:** Discover and register tools for the Web UI. Tools are declared via **JSON manifests** in `omweb/tools/builtins/*.json` — *not* Python modules.

**Two components:**

**A. JSON manifests** (`builtins/source_control.json`):
```json
{
  "id": "source_control",
  "name": "Source Control",
  "description": "...",
  "category": "system",
  "safety_level": "safe",
  "is_builtin": true,
  "parameters": {
    "type": "object",
    "properties": { "action": {...}, "params": {...}, ... },
    "required": ["action"]
  }
}
```

**B. Adapters** (`omweb/adapters/core_adapter_parts/tools_adapters/*_adapter.py`):
```python
ADAPTER_SPEC = AdapterSpec(
    tool_names=("source_control",),
    factory=create_source_control_executor,
)
```
The adapter factory:
1. Receives `workspace_dir`
2. Creates the Core `SourceControlTool` instance
3. Returns an async callable that mirrors the Core tool's signature

**Discovery is automatic.** `executors.py` scans `tools_adapters/` at import time and registers every `ADAPTER_SPEC`. **Adding a new adapter requires exactly one new file.**

---

## 3. Request Lifecycle (End-to-End)

Here is what happens when the user says "push my changes" via the agent:

### Step 1 — Agent Decides

```
Agent (LLM): "User approved. I should call source_control with action=push_branch."
```

### Step 2 — Tool Runtime Dispatches

**Agent → `ToolRegistry.aexecute("source_control", {...})`**
**→ `SourceControlTool.aexecute(...)`**
**→ `BaseTool.aexecute`** (validates schema, enforces timeout)
**→ `SourceControlTool._arun(...)`** (dispatcher logic)

### Step 3 — Dispatcher Routes to Sub-tool

```python
# Inside _arun
canonical_action = self._normalize_action(kwargs["action"])  # "push_branch"
subtool = self._get_subtool(canonical_action)                # PushBranchTool
context = IntegrationToolContext(operation_id=..., run_id=...)
result = await subtool.run(context, **call_params)           # sub-tool work
```

### Step 4 — Sub-tool Calls Facade

**`PushBranchTool.execute(context, approval_id="...")`** → calls `SourceControlFacade.push_approved_branch(...)`

### Step 5 — Facade Validates Everything

```python
# SourceControlFacade.push_approved_branch
record = await operation_service.try_get_operation(operation_id)   # load journal
ticket = approval_gate.get_ticket(approval_id)                     # load ticket
current_hash = await diff_engine.compute_diff(...)                 # recompute
approval_gate.consume(approval_id, expected_diff_sha256=current_hash, ...)  # BINDING CHECK
token = token_service.get_access_token(...)                        # vault
push_result = await push_manager.push_branch(..., token=token)     # actual push
await operation_service.transition(operation_id, PUSHED)           # journal update
```

### Step 6 — Result Flows Back

```
PushResult → SourceControlTool._to_core_result → ToolResult → Agent
```

The agent sees:
```python
ToolResult(
    output={"pushed_sha": "abc...", "branch_name": "feature/x"},
    is_error=False,
    metadata={"action": "push_branch", "status": "success"}
)
```

### Visual Summary

```
User says "push"
     │
     ▼
Agent (LLM) ────────────────────────┐
     │                              │
     │ call source_control tool     │ reads JSON manifest
     ▼                              │
ToolRegistry ───────────────────────┘
     │
     ▼
SourceControlTool.aexecute()                [Layer 4]
     │
     ▼
SourceControlTool._arun()  (dispatch)
     │
     ▼
PushBranchTool.run(ToolContext, ...)        [Layer 3]
     │
     ▼
SourceControlFacade.push_approved_branch()  [Layer 2]
     │
     ├── approval_gate.consume()  ← BINDING CHECK
     ├── token_service.get_access_token()
     ├── push_manager.push_branch()
     └── operation_service.transition(PUSHED)
     │
     ▼
ToolResult ← dispatcher ← ToolRegistry ← Agent
     │
     ▼
User sees "Pushed successfully"
```

---

## 4. Security Model — Approval Workflow

This is the **most important part of the system**. Every remote write goes through four independent checks.

### 4.1 The Four Layers of Defense

| # | Check | Where | Purpose |
|---|---|---|---|
| 1 | **Provider token** | `TokenService` + OS vault | Technical capability (can we act at all?) |
| 2 | **Policy** | `PolicyEngine.require()` | Class-level permission (is this type of action allowed?) |
| 3 | **Approval ticket** | `ApprovalGate.consume()` | Per-operation consent (did the user approve *this diff*?) |
| 4 | **Remote state** | `push_manager._remote_head_sha()` | Race protection (has the remote moved since review?) |

**All four must pass.** If any fails, the write is refused.

### 4.2 Approval Ticket Lifecycle

```
           PENDING   ──grant()──►   GRANTED   ──consume()──►   CONSUMED
              │                        │                          │
              │                        │                          └── (single use)
              │                        │
              └──reject()──►  REJECTED │
              └──revoke()──►  REVOKED  │
              └──(timeout)► EXPIRED    │
```

**States:**
- **PENDING** — created by `request_operation_approval`, waiting for user
- **GRANTED** — user clicked "Approve" in the UI
- **CONSUMED** — an operation used it (single-use)
- **REJECTED** — user clicked "Reject"
- **REVOKED** — operation was cancelled (e.g., remote moved)
- **EXPIRED** — TTL passed (default 15 minutes)

### 4.3 Content Binding

A ticket binds to **six fields**:

```python
ApprovalTicket(
    operation_id="op_123",
    repository_id="repo_456",
    operation="push_branch",
    base_sha="aaaa...",           # the commit the user reviewed
    diff_sha256="bbbb...",        # SHA-256 of the exact diff
    target_branch="feature/x",
)
```

**Before consuming**, the facade re-fetches all six from live state. If any differ, `ApprovalInvalidError` is raised.

**Why this matters:** Without content binding, an agent could:
1. Ask user to approve diff A
2. Receive approval
3. Silently replace files with diff B
4. Push diff B under the approval

Binding prevents this: the ticket stores `diff_sha256 = SHA256(A)`, and consumption fails if the current diff hash ≠ `SHA256(A)`.

### 4.4 What the Agent Cannot Do

| Action | Why it fails |
|---|---|
| Pass `approved=True` | Not a parameter |
| Invent an `approval_id` | `ApprovalGate.get_ticket()` returns `None` |
| Use `consume_approval()` (legacy) | Raises `NotImplementedError` |
| Reuse a ticket | `ApprovalConsumedError` |
| Use another operation's ticket | `ApprovalInvalidError` (operation_id mismatch) |
| Modify the ticket in memory | Dataclass is `frozen=True` |
| Forge a ticket from JSON | Gate reads from its own internal dict, not from caller input |

### 4.5 What the User Sees

**Before approving**, the UI shows:
- Repository + provider + target branch
- Base commit SHA + current HEAD SHA (with "remote state changed" warning)
- Full unified diff (syntax-highlighted, truncated at 500 lines)
- File list with additions/deletions counts
- Checks summary (passed/failed/pending)
- Proposed commit message + PR title/body
- Warnings for: workflow file changes, binary files, secret detection, protected branch

The approval button is specific: **"Approve & Push Branch"** or **"Approve & Create Pull Request"** — never a generic "Approve".

---

## 5. Operation State Machine

Every operation lives in a **durable journal** (`FileOperationJournal` or `InMemoryOperationJournal`) and transitions through a strict state machine.

### 5.1 States

```
CREATED → REPOSITORY_RESOLVED → WORKSPACE_READY → EDITING → CHECKING
                                                              │
                                                              ▼
                                                        REVIEW_REQUIRED
                                                              │
                                          ┌───────────────────┼───────────────────┐
                                          ▼                   ▼                   ▼
                                      APPROVED            REJECTED          STALE_REVIEW_REQUIRED
                                          │                                       │
                                          ▼                                       │
                                       PUSHING                                    │
                                          │                                       │
                                          ▼                                       │
                                        PUSHED  ◄────────────────────────────────┘
                                          │
                                          ▼
                                  CHANGE_REQUEST_CREATED
```

### 5.2 Terminal vs Intermediate

**Terminal** (cannot transition out):
- `CHANGE_REQUEST_CREATED`
- `CANCELLED`
- `FAILED_FINAL`
- `REJECTED`

**NOT terminal** (v2 clarification):
- `PUSHED` — this is intermediate; it can transition to `CHANGE_REQUEST_CREATED`

> **Fixed in v1.0:** Earlier versions incorrectly treated `PUSHED` as terminal, which broke `create_change_request` after a successful push. The fix is at `integrations/source_control/operations/models.py` — `PUSHED` is excluded from `TERMINAL_STATUSES`.

### 5.3 The `request_operation_approval` Auto-Advance

Operations are created in `CREATED`. To reach `REVIEW_REQUIRED`, they must pass through `REPOSITORY_RESOLVED`, `WORKSPACE_READY`, and `EDITING`.

**The facade handles this automatically** (see `_advance_to_editing`):
```python
# Inside request_operation_approval
await self._advance_to_editing(operation_id, record)
ticket = self.approval_gate.create_ticket(...)
await self.operation_service.transition(operation_id, REVIEW_REQUIRED)
```

**Why this design:** the presence of a workspace with a computable diff proves that those intermediate states were reached out-of-band. The facade declares the fact rather than re-deriving it.

### 5.4 Event Timeline

Every transition writes one `OperationEvent`:
```python
OperationEvent(
    operation_id="op_123",
    sequence=5,
    event_type=STATE_TRANSITION,
    occurred_at=datetime(...),
    payload={"from_status": "review_required", "to_status": "approved"},
)
```

**The API returns events with every `get_operation` call.** The UI renders them as a timeline.

---

## 6. Local Git Workspace

### 6.1 Workspace Isolation

A **workspace** is a PELDRUN-owned directory under `workspaces/source_control/<safe-repo>__<workspace-id>/`. It contains exactly one clone or worktree.

**Every path is validated** through `WorkspacePathPolicy`:
- Must resolve inside `workspaces/source_control/`
- `..` traversal rejected
- Symlinks that escape the root rejected
- Control characters rejected
- Length limits enforced

### 6.2 Safe Git Execution

**All Git commands go through `GitExecutable`** (`git/executable.py`):

```python
await git.run(["status", "--porcelain"], cwd=workspace_path)
```

**`GitExecutable` guarantees:**
- `asyncio.create_subprocess_exec` (never `shell=True`)
- Arguments as a list, never a string
- Sanitized environment (no `GIT_TRACE`, no `GIT_ASKPASS`)
- Timeout (default 60s; long ops override)
- Output capped at 5 MiB per stream
- Redaction: any token in output is replaced with `***REDACTED***`

### 6.3 Clone with Auth Token

Private repositories need authentication during clone. The workspace manager handles this **without persisting credentials**:

```python
# Inside GitWorkspaceManager._clone
clean_url = _strip_url_credentials(clone_url)
working_url = _inject_token_in_url(clean_url, auth_token)  # https://x-access-token:TOKEN@host/path
try:
    await git.run(["clone", working_url, target])
finally:
    # Always sanitize .git/config, even on failure
    await git.run(["remote", "set-url", "origin", clean_url], cwd=target)
```

**Result:** `.git/config` contains only `https://github.com/owner/repo.git` (no token).

### 6.4 Push with Force-With-Lease

**`GitPushManager.push_branch`** uses:
```python
args = [
    "push", "--porcelain", "--atomic", "--no-thin",
    f"--force-with-lease=refs/heads/{destination}:{lease_sha}",
    remote, f"{local}:{destination}",
]
```

**`--force-with-lease=<ref>:<sha>`** ensures: "only push if the remote is still at `<sha>`". The facade passes the ticket's `base_sha` as the lease value.

**Plain `--force` is never used.**

---

## 7. HTTP Router Reference

### 7.1 Endpoints (all under `/api/source-control`)

#### Providers & Connections
```
GET    /providers
GET    /connections
POST   /connections/{provider}/begin          # start OAuth
GET    /oauth/callback/{provider}             # OAuth callback
POST   /connections/{provider}/validate       # refresh token
DELETE /connections/{provider}                # disconnect + revoke
```

#### Repositories, Branches, Files
```
GET    /repositories                          # list
GET    /repositories/{repository_id}          # get
GET    /repositories/{repository_id}/branches
POST   /repositories/{repository_id}/branches
GET    /repositories/{repository_id}/file     # read remote file
```

#### Workspaces, Diffs, Checks
```
POST   /workspaces
GET    /workspaces/{workspace_id}
GET    /workspaces/{workspace_id}/diff
POST   /workspaces/{workspace_id}/checks
```

#### Operations & Approvals
```
POST   /operations
GET    /operations/{operation_id}
POST   /operations/{operation_id}/approval    # request ticket
POST   /operations/{operation_id}/approve     # user grants
POST   /operations/{operation_id}/reject      # user rejects
POST   /operations/{operation_id}/push        # consume + push
POST   /operations/{operation_id}/change-request
```

#### Audit
```
GET    /audit
```

### 7.2 Common Response Shape

Every response is Pydantic-validated. Example `OperationResponse`:
```json
{
  "operation_id": "op_a31b9d0192",
  "operation_type": "push_branch",
  "provider": "github",
  "repository_id": "repo_1",
  "target_branch": "feature/x",
  "current_state": "review_required",
  "workspace_id": "ws_abc",
  "approval_id": "app_xyz",
  "change_request_url": null,
  "error_code": null,
  "error_message": null,
  "created_at": "2026-10-18T10:00:00Z",
  "updated_at": "2026-10-18T10:05:00Z",
  "events": [
    {
      "event_id": "1",
      "from_state": "",
      "to_state": "created",
      "timestamp": "2026-10-18T10:00:00Z",
      "metadata": {}
    }
  ]
}
```

**Note:** `current_state` uses **lowercase** (`.value` of the enum). Frontend must not assume uppercase.

### 7.3 Error Response Shape

```json
{
  "detail": "Push to protected branch 'main' is forbidden."
}
```

`detail` is user-safe (already redacted). Errors 5xx return `{"detail": "Internal server error"}` without leaking internals.

---

## 8. Frontend Developer Guide

### 8.1 The Five Screens You Must Build

| Screen | Purpose | Priority |
|---|---|---|
| **Connections** | OAuth flow, connection status | P0 |
| **Repository Picker** | List/select repos | P0 |
| **Workspace** | File tree + branch + fetch | P0 |
| **Review & Approval** | The critical screen | **P0 (highest)** |
| **Operation Timeline** | Durable operation history | P1 |
| **Audit Log** | Security events | P2 |

### 8.2 Connection Flow (OAuth PKCE)

```
User clicks "Connect GitHub"
     │
     ▼
POST /connections/github/begin
     │  → { authorization_url, state, expires_in_seconds }
     ▼
Open authorization_url in system browser
     │
     ▼
GitHub redirects to http://127.0.0.1:8765/oauth/callback?code=...&state=...
     │
     ▼
Browser sends GET /oauth/callback/github?code=...&state=...
     │
     ▼
Response: "Connected to GitHub as 'octocat'"
```

**Frontend must:**
- Use the system browser (never embed credentials in PELDRUN)
- Handle `state` validation (facade does this server-side)
- Show failure states clearly

### 8.3 Repository Picker

```typescript
// Simple pattern
async function listRepos(provider: string) {
  const res = await fetch(`/api/source-control/repositories?provider=${provider}`);
  if (!res.ok) {
    const err = await res.json();
    showError(err.detail);
    return [];
  }
  return res.json(); // RepositoryResponse[]
}
```

**Never trust** `repository_id` as a stable identifier across providers — always carry `(provider, repository_id)` together.

### 8.4 The Review & Approval Screen (Most Important)

This is where users consent to remote writes. It must show everything.

**Required elements:**

```
┌──────────────────────────────────────────────────────────────┐
│ REPOSITORY                                                   │
│   GitHub · peldrun/sample-repo                               │
│                                                              │
│ BRANCHES                                                     │
│   Feature → Target:  feature/x → main                        │
│   Base SHA: a1b2c3d4...                                      │
│   Head SHA: e5f6g7h8...                                      │
│                                                              │
│ ⚠ WARNING: Remote branch moved since review. Re-review.      │
│                                                              │
│ CHANGES (3 files, +42 −8)                                    │
│   ✓ src/main.py         +38 −4                               │
│   ✓ src/utils.py        +4  −4                               │
│   ⚠ .github/workflows/ci.yml  +0 −0  ← workflow file         │
│                                                              │
│ DIFF PREVIEW                                                 │
│   [unified diff, syntax highlighted, scrollable]             │
│                                                              │
│ CHECKS                                                       │
│   ✓ unit-tests         passed                                │
│   ✓ lint               passed                                │
│   ⏳ integration-tests  running                               │
│                                                              │
│ PROPOSED COMMIT MESSAGE                                      │
│   "Add rate limiting to API endpoints"                       │
│                                                              │
│ PROPOSED PR TITLE                                            │
│   "Add rate limiting"                                        │
│                                                              │
│ ────────────────────────────────────────────────────────     │
│ [ Keep Local Changes ]  [ Reject ]  [ Approve & Push Branch ]│
└──────────────────────────────────────────────────────────────┘
```

**Critical rules:**

1. **No generic "Approve" button.** Buttons must name the exact side effect.
2. **Re-fetch diff before showing approval.** If `diff_sha256` changed since `request_operation_approval`, show `STALE_REVIEW_REQUIRED`.
3. **Show all warnings.** Workflow file changes, binary files, secret findings, protected branch.
4. **Confirm destructive actions separately.** "Discard Local Changes" is a second dialog.
5. **Never show a false success.** If checks were skipped, say "checks skipped".

### 8.5 Operation Timeline

Render as a vertical timeline:

```
2026-10-18 10:00:00  Operation created
2026-10-18 10:00:05  Repository resolved → peldrun/sample-repo
2026-10-18 10:00:08  Workspace created (ws_abc)
2026-10-18 10:01:00  Files modified (3 files)
2026-10-18 10:01:15  Checks executed (2 passed, 1 running)
2026-10-18 10:02:00  Approval requested
2026-10-18 10:03:00  User approved the diff
2026-10-18 10:04:00  Branch pushed (sha e5f6g7h8)
2026-10-18 10:05:00  Pull request #42 created
```

Each event links to the audit log entry for forensic detail.

### 8.6 Error Handling Patterns

```typescript
// Standard pattern for every API call
async function callAPI<T>(path: string, options?: RequestInit): Promise<T | null> {
  const res = await fetch(`/api/source-control${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options?.headers },
  });
  if (res.ok) return res.json();

  const err = await res.json().catch(() => ({ detail: "Unknown error" }));

  switch (res.status) {
    case 401: showReconnectPrompt(); break;
    case 403: showPermissionDenied(err.detail); break;
    case 404: showNotFound(err.detail); break;
    case 409: showConflict(err.detail); break;
    case 412: showStateChangedDialog(); break; // remote moved
    case 422: showValidationErrors(err.detail); break;
    case 429: showRateLimit(); break;
    default:  showGenericError(err.detail);
  }
  return null;
}
```

**Never retry** on 4xx. Retry only on 5xx, with exponential backoff.

### 8.7 Local Development

```bash
# Backend
cd D:/AI/peldrun
./start-backend    # → http://localhost:8088

# Frontend (assumes same repo)
cd frontend
npm install
npm run dev        # → http://localhost:3000
```

**The backend already allows CORS** in development (`allow_origins=["*"]`).

---

## 9. Testing Guide

### 9.1 How to Run Tests

```bash
# All source-control tests
cd D:/AI/peldrun
python -m pytest backend/tests/source_control -v

# A single file
python -m pytest backend/tests/source_control/test_approval_gate.py -v

# A single test
python -m pytest "backend/tests/source_control/test_approval_gate.py::test_single_use_ticket_consumption" -v

# With coverage
python -m pytest backend/tests/source_control --cov=backend/integrations/source_control --cov-report=html
```

### 9.2 Test Categories

| File | What it tests | Count |
|---|---|---|
| `test_approval_gate.py` | Ticket lifecycle, binding, single-use | 14 |
| `test_policy_engine.py` | Default policy, capability gaps, audit | 12 |
| `test_facade_smoke.py` | Wiring contracts between services | 15 |
| `test_facade_integration.py` | End-to-end facade flows | 13 |
| `test_facade_remote_flows.py` | Push/PR flows (mocked managers) | 8 |
| `test_approval_bypass.py` | Adversarial approval tests | 13 |
| `test_command_injection.py` | Shell injection defense | 38 |
| `test_path_traversal.py` | Path escape defense | 26 (+2 symlink skip) |
| `test_sc_tools.py` | Core dispatcher behavior | 28 |
| `test_git_workspace.py` | Local Git lifecycle | 8 |
| `test_github_client.py` | Provider adapter mapping | 6 |
| `test_operations_journal.py` | Durable journal | 6 |
| `test_redaction.py` | Secret redaction | 12 |
| `test_sc_router.py` | HTTP error mapping | 7 |
| `test_vault.py` | Credential vault | 7 |
| `test_domain_models.py` | Dataclass invariants | 39 |

**Total: 260 collected, 258 passed, 2 skipped.**

### 9.3 Test Design Principles

**1. Every test asserts on observable behavior**, not internal state.
```python
# ✅ Good
assert result.current_state == "approved"

# ❌ Bad
assert facade._operation_service._cache["op_1"].status == OperationStatus.APPROVED
```

**2. Tests that attack the system are grouped in `security/`.**
```python
# File: tests/source_control/security/test_approval_bypass.py
def test_ticket_from_different_operation_is_rejected(approval_gate):
    """ATTACK: Use operation A's ticket to approve operation B."""
    ...
```

**3. Every attack has a "sanity check" companion.**
```python
def test_valid_ticket_is_accepted():     # ✅ attack fails
    ...

def test_normal_branch_name_is_accepted():  # ✅ legit input works
    ...
```

**4. Fixtures live in `conftest.py`.** No mocks in test bodies except when testing specific behavior.

**5. Tests use `InMemoryOperationJournal`, `InMemoryAuditLogger`, `InMemoryVault`.** Nothing touches disk.

**6. `test_sc_tools.py` imports from `peldrun.tools.*`, NOT `backend.peldrun.tools.*`.**

> **Why:** The Core tool itself uses `peldrun.tools.*`. If the test imports from `backend.peldrun.tools.*`, Python creates a *second* class identity for `ToolResult`, and every `isinstance` check fails. Always import from the same module tree the code under test uses.

### 9.4 Running Tests in CI

```yaml
# .github/workflows/test.yml (example)
name: test
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r backend/requirements.txt
      - run: cd backend && python -m pytest tests/source_control -v
```

**On Linux CI**, the two symlink tests will pass (Windows lacks the required permissions by default).

---

## 10. Known Issues & Technical Debt

These are documented, bounded problems that do not break current functionality. All scheduled for v2.

### 10.1 Production Code Debt

| # | Location | Issue | Impact | Fix |
|---|---|---|---|---|
| 1 | `peldrun/tools/{collection,registry,mcp_adapter,mcp_client}.py` | Absolute `from peldrun.tools.*` imports | If a launcher imports `backend.peldrun.tools.*`, a second class tree appears and `isinstance` fails | Convert to relative imports (`from .contract import ...`) — safe because the package's own `__init__.py` and `base.py` already use that style |
| 2 | `omweb/tools/registry.py` | Line `self._loaded_custom_instances[tool_id_original := entry.id] = instance` — `tool_id_original` is unused | Dead code; harmless but confusing | Delete the walrus assignment |
| 3 | `providers/__init__.py` | `register_builtin_providers(registry=...)` logs a warning on every duplicate call | Noisy logs in tests (one WARNING per test that instantiates a facade) | Return `"skipped"` status silently when already registered, or use `overwrite=True` in the facade |
| 4 | `security/approval.py` | `consume_approval()` (legacy) raises `NotImplementedError` but is still exported | No caller uses it, but the API surface invites mistakes | Remove entirely in v2 after confirming no external caller depends on it |
| 5 | `omweb/services/source_control_facade.py` | `datetime.utcnow()` (in a helper) | Python 3.12 deprecation warning | Replace with `datetime.now(timezone.utc)` |

### 10.2 Environment-Specific

| # | Location | Issue | Impact | Fix |
|---|---|---|---|---|
| 6 | `test_path_traversal.py` | 2 symlink tests skip on Windows | Coverage gap on Windows (Linux CI runs them) | Enforce CI on Linux (already recommended) |
| 7 | `test_sc_router.py::test_approve_operation_success` | `pydantic type_adapter.py:410` DeprecationWarning | Warning in test output | Internal to Pydantic; resolve by upgrading Pydantic in a separate PR |
| 8 | `starlette/testclient.py:53` | `anyio.abc.BlockingPortal` deprecated | Warning in test output | Internal to Starlette; upgrade dependency |

### 10.3 Architectural Notes

| # | Note |
|---|---|
| 9 | `SourceControlTool` exists in two places: `peldrun/tools/builtins/source_control_tool.py` (Core, correct) and `integrations/source_control/tools/base.py` (ABC for sub-tools). Both are needed; do not merge. |
| 10 | The Web layer's `omweb/tools/` uses JSON manifests exclusively. Do NOT create `.py` files in that folder — they will not be discovered. The Python source of truth is `peldrun/tools/builtins/`. |
| 11 | `test_sc_tools.py` must import from `peldrun.tools.*`. Other tests in `tests/source_control/` import from `backend.integrations.*` — that is intentional and correct. Do not mix. |

---

## 11. Roadmap — v2 and Next Phase

### 11.1 Required Before v2 Release

**All are non-blocking for current functionality** but must be addressed before tagging v2.

- [ ] Resolve items 1-5 in §10.1 (production code debt)
- [ ] Add CI on Linux so symlink tests run (item 6)
- [ ] Add contract test for `_ensure_tool_result` handling of dicts
- [ ] Add integration test against a disposable GitHub test repository (marked `@pytest.mark.integration`, opt-in via `GITHUB_TEST_TOKEN`)
- [ ] Document the `connections.json` schema versioning story
- [ ] Add Prometheus metrics for approval gate latency, push success rate, policy denial rate

### 11.2 v2 Feature Backlog (Prioritized)

| Priority | Feature | Why | Effort |
|---|---|---|---|
| **P0** | **GitLab adapter** | Second provider proves the abstraction; largest user base after GitHub | 1 sprint |
| **P0** | **Bitbucket Cloud adapter** | Third provider; completes the "not just GitHub" promise | 1 sprint |
| **P1** | **Self-managed GitLab** | Enterprise users; requires SSRF protections, base URL validation | 2 sprints |
| **P1** | **Merge workflow** | Currently disabled by default policy; needs its own approval | 1 sprint |
| **P1** | **Protected branch rules** | Per-repository policy (not global default) | 1 sprint |
| **P2** | **Comments/review on PRs** | Agent can respond to review comments | 1 sprint |
| **P2** | **GitHub Actions integration** | Trigger workflows after push | 1 sprint |
| **P3** | **Commit signing** | GPG/SSH signing for commits | 1 sprint |
| **P3** | **Submodules** | Currently rejected by validation | 1 sprint |

### 11.3 v2 Architectural Improvements

| Improvement | Rationale |
|---|---|
| **Unify `ToolResult` across layers** | Currently three variants (Core Pydantic, Integration dataclass, Web `_ensure_tool_result`). One adapter is enough — unify to Pydantic and delete the dataclass. |
| **SQLite-backed operation journal** | `FileOperationJournal` works but is O(n) for `list_operations`. SQLite gives O(log n) queries and atomic transactions. |
| **Explicit operation phases** | Current `_advance_to_editing` is a workaround. Introduce a `phase` field separate from `status` so the machine handles sub-steps cleanly. |
| **Recovery service integration** | `recovery.py` exists but is not called at startup. Wire it in so in-flight operations resume correctly. |
| **Structured metrics** | Emit `peldrun_approval_gate_latency_seconds`, `peldrun_push_success_total`, etc. |

### 11.4 v3 (Long-term)

- **Multi-user support** (currently single-user desktop)
- **Hosted OAuth broker** (optional, for teams that want a central token issuer)
- **Agent-to-agent delegation** with scoped approvals
- **Policy DSL** (currently policy is code; move to YAML/JSON for user customization)

---

## 12. Troubleshooting Guide

### 12.1 "TypeError: Can't instantiate abstract class SourceControlTool"

**Cause:** `BaseTool` declares `_arun` as `@abstractmethod`, but the subclass implemented `aexecute` instead.

**Fix:** Rename `aexecute` → `_arun` in the subclass. `BaseTool.aexecute` is the public wrapper; `_arun` is the actual implementation.

### 12.2 "ImportError: cannot import name ToolExecutionPolicy from peldrun.tools.contract"

**Cause:** Two `peldrun` packages on `sys.path` (e.g., `peldrun-core` still installed).

**Fix:** `pip uninstall peldrun` or rename the stray folder. Then re-run tests.

### 12.3 "isinstance(result, ToolResult) returns False"

**Cause:** Two module trees (`peldrun.tools.contract` vs `backend.peldrun.tools.contract`). Python creates two distinct class objects.

**Fix:** Import from the same tree the code-under-test uses. For `test_sc_tools.py` this means `peldrun.tools.*`, not `backend.peldrun.tools.*`.

### 12.4 "AttributeError: 'ReadFileTool' object has no attribute 'aexecute'"

**Cause:** The integration sub-tools implement `run(context, **kwargs)`, not `aexecute`.

**Fix:** In the dispatcher, call `subtool.run(context, **params)` — not `subtool.aexecute(**params)`.

### 12.5 "ConflictError: Invalid transition from 'approved' to 'change_request_created'"

**Cause:** `PUSHED` was incorrectly treated as terminal (v1.0 bug, now fixed).

**Fix:** Verify `integrations/source_control/operations/models.py` has `PUSHED` **outside** `TERMINAL_STATUSES`. Restart the process; the import cache holds old class definitions.

### 12.6 "PermissionDeniedError: Pushing to protected branch 'main' is forbidden"

**Cause:** Normal behavior. Direct pushes to `main`, `master`, `develop`, `release`, `prod`, `stage` are denied by policy.

**Fix:** Push to a feature branch and open a pull request. That's the intended flow.

### 12.7 "ValidationError: base_sha must be a 64-character lowercase hex digest"

**Cause:** A 40-char SHA-1 was passed where a 64-char SHA-256 is expected (approval tickets bind to SHA-256).

**Fix:** Tests that build tickets manually must use 64-char constants:
```python
_BASE_SHA = "a" * 64  # not "a" * 40
```

### 12.8 "Provider does not satisfy the SourceControlProvider protocol"

**Cause:** `MockSourceControlProvider` fails the runtime `isinstance` check.

**Fix:** Register the mock directly via `registry._providers["github"] = mock_provider`, or implement the missing protocol methods.

### 12.9 "git_binary path contains disallowed characters" (Windows)

**Cause:** `_SHELL_METACHARACTERS` previously included `()` and `{}`, breaking `C:\Program Files (x86)\...`.

**Fix:** Confirm the regex excludes `\`, `(`, `)`, `{`, `}` (they are safe without a shell). This was fixed in v1.0; if you see this error, the running binary is stale.

### 12.10 Agent tool not discovered by the LLM

**Cause checklist:**
1. Is the JSON manifest in `omweb/tools/builtins/*.json`? (Required for discovery.)
2. Is the adapter in `omweb/adapters/core_adapter_parts/tools_adapters/*_adapter.py` with an `ADAPTER_SPEC`? (Required for execution.)
3. Verify discovery:
   ```python
   from omweb.adapters.core_adapter_parts.tools_adapters._contract import AdapterRegistry
   print(AdapterRegistry.all_tool_names())
   ```
   Should include `'source_control'`.
4. Verify manifest loading:
   ```python
   from omweb.tools.registry import tool_registry
   tools = {t['id']: t for t in tool_registry.list_tools()}
   print('source_control' in tools)  # True
   ```
5. If both are missing, restart the backend — discovery runs at import time.

### 12.11 Approval ticket expired in under a minute

**Cause:** Default TTL is 15 minutes; but if the system clock jumped (VM suspend/resume), tickets can appear expired.

**Fix:** Verify system clock. In tests, use `ttl_seconds=300` explicitly.

### 12.12 Push succeeds but journal still shows PUSHING

**Cause:** The transition to `PUSHED` failed after the remote push succeeded (network blip, journal write error).

**Diagnosis:** Check the audit log for `push_success` and `transition` events. The push result is authoritative; the state can be reconciled via the recovery service.

**Fix:** Run recovery:
```python
from integrations.source_control.operations.recovery import get_default_recovery_service
report = await get_default_recovery_service().recover_all()
print(report.to_dict())
```

---

## Appendix A — Key File Map

For quick navigation, here are the most important files:

```
backend/
├── omweb/
│   ├── main.py                                    ← App entrypoint
│   ├── routers/source_control.py                  ← Layer 1
│   ├── services/source_control_facade.py          ← Layer 2
│   ├── tools/
│   │   ├── registry.py                            ← Layer 5: discovery
│   │   └── builtins/source_control.json           ← Layer 5: manifest
│   └── adapters/core_adapter_parts/
│       ├── executors.py                           ← Layer 5: auto-discovery
│       ├── resolve_tool_runtime.py                ← Layer 5: resolver
│       └── tools_adapters/
│           ├── _contract.py
│           ├── _helpers.py
│           └── source_control_adapter.py          ← Layer 5: adapter
│
├── peldrun/tools/
│   ├── base.py                                    ← Layer 4: BaseTool
│   ├── contract.py                                ← Layer 4: ToolResult
│   ├── registry.py                                ← Layer 4: registry
│   └── builtins/source_control_tool.py            ← Layer 4: dispatcher
│
└── integrations/source_control/
    ├── domain/                                    ← Layer 3: models
    ├── security/                                  ← Layer 3: vault, approval, policy
    ├── git/                                       ← Layer 3: safe subprocess, workspace
    ├── operations/                                ← Layer 3: journal, state machine
    ├── providers/github/                          ← Layer 3: GitHub adapter
    └── tools/                                     ← Layer 3: 12 sub-tools
```




## Appendix B — Testing Checklist for New Developers

Before submitting a PR that touches source-control code, run:

```bash
# 1. Full test suite
cd D:/AI/peldrun/backend
python -m pytest tests/source_control -v

# 2. Specific areas you changed
python -m pytest tests/source_control/security -v          # if security/
python -m pytest tests/source_control/test_facade_integration.py -v  # if facade
python -m pytest tests/source_control/test_sc_tools.py -v  # if dispatcher

# 3. Adapter discovery sanity
python -c "
import sys; sys.path.insert(0, 'backend')
from omweb.adapters.core_adapter_parts.tools_adapters._contract import AdapterRegistry
from omweb.adapters.core_adapter_parts.executors import _discover_adapters
_discover_adapters()
assert 'source_control' in AdapterRegistry.all_tool_names()
print('OK')
"
```

**Expected: 258 passed, 2 skipped.** Any other count means something is wrong.

---

## Appendix C — Glossary

| Term | Meaning |
|---|---|
| **Approval ticket** | A single-use, content-bound token that authorizes one remote write |
| **Base SHA** | The commit the user's diff was computed against (usually the target branch's HEAD at review time) |
| **Diff SHA-256** | Cryptographic hash of the exact file changes; binds approval to content |
| **Facade** | The orchestration layer that wires services together |
| **Durable journal** | Append-only record of operation state and events, survives restarts |
| **Dispatcher** | The Core tool that routes `action` to the right sub-tool |
| **Adapter** | The bridge between Web-layer tool discovery and Core-layer execution |
| **Capability** | A declared provider feature (e.g., `merge_change_request=False` for some providers) |
| **Redaction** | Replacing secrets (`ghp_*`, Bearer tokens) with `***REDACTED***` |
| **Lease** | Git's `--force-with-lease` mechanism that refuses pushes when the remote moved |

---

## End of Document

**Status:** This document describes PELDRUN Source Control Integration v1.0 as of October 2026.
**Maintenance:** Update after each major refactor. Add new items to §10 and §11 as they appear.
**Contact:** For questions, check `tests/source_control/` first — the tests are executable documentation.

**Thank you for reading. Build well, ship safely.**






frontend/
├── src/
│   ├── app/
│   │   └── source-control/
│   │       └── page.tsx
│   └── features/
│       └── source-control/
│           ├── api/
│           │   ├── schemas.ts
│           │   └── source-control-client.ts
│           ├── components/
│           │   ├── ApprovalDialog.tsx
│           │   ├── BranchPicker.tsx
│           │   ├── ChangeRequestPanel.tsx
│           │   ├── ChangeSummary.tsx
│           │   ├── CheckResults.tsx
│           │   ├── DiffViewer.tsx
│           │   ├── DisconnectProviderDialog.tsx
│           │   ├── FileChangeList.tsx
│           │   ├── OperationTimeline.tsx
│           │   ├── PermissionMatrix.tsx
│           │   ├── ProviderConnectDialog.tsx
│           │   ├── ProviderConnectionCard.tsx
│           │   ├── RepositoryOverview.tsx
│           │   └── RepositoryPicker.tsx
│           ├── hooks/
│           │   ├── useOperation.ts
│           │   ├── useProviders.ts
│           │   └── useRepositories.ts
│           ├── state/
│           │   └── source-control-store.ts
│           ├── types/
│           │   └── index.ts
│           └── utils/
│               ├── diff-format.ts
│               └── permission-labels.ts



# Complete Tree of All Source Control Files in the Backend

## 🌳 Unified Tree (organized by layers)

```
backend/
│
├── 📄 main.py                                                 ← App entrypoint (registers router)
│
├── 📁 omweb/                                                  ═══ Layer 1: HTTP & Facade ═══
│   │
│   ├── 📁 routers/
│   │   ├── __init__.py                                        ← exports source_control
│   │   └── source_control.py                                  ★ Layer 1: HTTP router
│   │
│   ├── 📁 services/
│   │   └── source_control_facade.py                           ★ Layer 2: Facade (orchestration)
│   │
│   ├── 📁 tools/                                              ═══ Layer 5: Web Tool Runtime ═══
│   │   ├── __init__.py
│   │   ├── base.py                                            ← SovereignBaseTool (Pydantic)
│   │   ├── registry.py                                        ← ToolRegistry (JSON manifests)
│   │   ├── builtins/
│   │   │   ├── source_control.json                            ★ Layer 5: JSON manifest
│   │   │   └── ... (other tool manifests: bash.json, etc.)
│   │   └── bundles/                                           ← Bundle system (Phase 4)
│   │       ├── __init__.py
│   │       ├── errors.py
│   │       ├── extractor.py
│   │       ├── loader.py
│   │       ├── manager.py
│   │       ├── schema.py
│   │       ├── storage.py
│   │       └── validator.py
│   │
│   └── 📁 adapters/
│       └── core_adapter_parts/
│           ├── executors.py                                   ← auto-discovery of adapters
│           ├── resolve_tool_runtime.py                        ← tool ID → executor
│           ├── schemas.py                                     ← CANONICAL_TOOL_SCHEMAS
│           ├── web_tool_adapter.py                            ← WebToolAdapter wrapper
│           └── tools_adapters/
│               ├── __init__.py
│               ├── _contract.py                               ← AdapterSpec + AdapterRegistry
│               ├── _helpers.py                                ← workspace, ToolResult normalization
│               ├── source_control_adapter.py                  ★ Layer 5: source_control bridge
│               ├── str_replace_editor_adapter.py
│               ├── http_fetch_adapter.py
│               └── ... (bash_adapter, python_execute_adapter, etc.)
│
├── 📁 peldrun/                                                ═══ Layer 4: Core Tool Runtime ═══
│   └── 📁 tools/
│       ├── __init__.py
│       ├── base.py                                            ← BaseTool (ABC, `_arun`)
│       ├── contract.py                                        ← ToolResult, ToolExecutionPolicy
│       ├── collection.py
│       ├── registry.py                                        ← Core ToolRegistry
│       ├── mcp_adapter.py
│       ├── mcp_client.py
│       └── builtins/
│           ├── __init__.py
│           ├── source_control_tool.py                         ★ Layer 4: Dispatcher
│           ├── str_replace_editor.py
│           ├── http_fetch.py
│           ├── human_input.py
│           └── file_ops.py
│
├── 📁 integrations/                                           ═══ Layer 3: Integration ═══
│   └── 📁 source_control/
│       │
│       ├── __init__.py                                        ← public exports
│       ├── capabilities.py                                    ← ProviderCapabilities
│       ├── errors.py                                          ← error hierarchy
│       ├── registry.py                                        ← ProviderRegistry
│       ├── schemas.py                                         ← API DTOs (Pydantic)
│       │
│       ├── 📁 domain/
│       │   ├── __init__.py
│       │   ├── enums.py                                       ← ProviderId, OperationStatus, ...
│       │   ├── models.py                                      ← RepositoryRef, DiffSummary, ...
│       │   └── policies.py                                    ← SourceControlPolicy, evaluate_policy
│       │
│       ├── 📁 security/
│       │   ├── __init__.py
│       │   ├── approval.py                                    ★ ApprovalGate (single-use, bound)
│       │   ├── audit.py                                       ← AuditLogger (event log)
│       │   ├── oauth_flow.py                                  ← OAuthFlowCoordinator (PKCE)
│       │   ├── policy_engine.py                               ← PolicyEngine (check/require)
│       │   ├── redaction.py                                   ← redact_text, redact_url, ...
│       │   ├── token_service.py                               ← TokenService (vault access)
│       │   └── vault.py                                       ← CredentialVault (OS keyring)
│       │
│       ├── 📁 git/
│       │   ├── __init__.py
│       │   ├── executable.py                                  ★ GitExecutable (safe subprocess)
│       │   ├── workspace.py                                   ★ GitWorkspaceManager
│       │   ├── path_policy.py                                 ← WorkspacePathPolicy
│       │   ├── status.py                                      ← GitStatusReader
│       │   ├── diff.py                                        ← GitDiffEngine
│       │   ├── branch.py                                      ← GitBranchManager
│       │   ├── commit.py                                      ← GitCommitManager
│       │   ├── push.py                                        ← GitPushManager
│       │   └── conflict.py                                    ← GitConflictManager
│       │
│       ├── 📁 operations/
│       │   ├── __init__.py
│       │   ├── models.py                                      ← OperationTransition, ALLOWED_TRANSITIONS
│       │   ├── journal.py                                     ← OperationJournal (durable)
│       │   ├── service.py                                     ← OperationService (state machine)
│       │   ├── idempotency.py                                 ← IdempotencyRegistry
│       │   └── recovery.py                                    ← OperationRecoveryService
│       │
│       ├── 📁 ports/                                          ← Provider-neutral interfaces
│       │   ├── __init__.py
│       │   ├── provider.py                                    ← SourceControlProvider (Protocol)
│       │   ├── auth.py                                        ← TokenInfo, AuthResult, ...
│       │   ├── repository.py                                  ← DiffPort, PushPort, ...
│       │   ├── change_request.py
│       │   └── checks.py
│       │
│       ├── 📁 providers/                                      ← Concrete adapters
│       │   ├── __init__.py                                    ← register_builtin_providers
│       │   └── github/
│       │       ├── __init__.py                                ← GitHubProviderAdapter
│       │       ├── manifest.py                                ← endpoints, scopes
│       │       ├── auth.py                                    ← token exchange, revoke
│       │       ├── client.py                                  ← HTTP client (httpx)
│       │       ├── repositories.py
│       │       ├── branches.py
│       │       ├── pull_requests.py
│       │       ├── checks.py
│       │       ├── mapper.py                                  ← GitHub JSON → domain models
│       │       └── errors.py
│       │
│       └── 📁 tools/                                          ═══ Layer 3: 12 Sub-tools ═══
│           ├── __init__.py                                    ← BUILTIN_TOOL_NAMES, register_*
│           ├── base.py                                        ← SourceControlTool (ABC), ToolContext
│           ├── list_providers.py
│           ├── list_repositories.py
│           ├── get_repository.py
│           ├── read_file.py
│           ├── list_branches.py
│           ├── create_workspace.py
│           ├── create_branch.py
│           ├── inspect_diff.py
│           ├── run_checks.py
│           ├── request_approval.py
│           ├── push_branch.py
│           └── create_change_request.py
│
└── 📁 tests/                                                  ═══ Tests ═══
    └── 📁 source_control/
        ├── __init__.py                                        ← constants, metadata
        ├── conftest.py                                        ← fixtures (mock_provider, vault, ...)
        │
        ├── test_domain_models.py                              ← 39 dataclass invariants
        ├── test_vault.py                                      ← 7 vault tests
        ├── test_redaction.py                                  ← 12 secret-hiding tests
        ├── test_approval_gate.py                              ← 14 ticket lifecycle tests
        ├── test_policy_engine.py                              ← 12 policy tests
        ├── test_git_workspace.py                              ← 8 workspace tests
        ├── test_github_client.py                              ← 6 adapter mapping tests
        ├── test_operations_journal.py                         ← 6 journal tests
        ├── test_sc_router.py                                  ← 7 HTTP error tests
        ├── test_facade_smoke.py                               ← 15 contract tests
        ├── test_facade_integration.py                         ← 13 integration tests
        ├── test_facade_remote_flows.py                        ← 8 remote-flow tests
        ├── test_sc_tools.py                                   ← 28 dispatcher tests
        │
        └── security/
            ├── __init__.py
            ├── test_approval_bypass.py                        ← 13 adversarial tests
            ├── test_command_injection.py                      ← 38 injection tests
            └── test_path_traversal.py                         ← 26 traversal tests (+2 skip)
```

---

## 📊 Statistics Summary

| Metric | Count |
|---|---|
| **Core Source Control files** | **~75 files** |
| **Architectural layers** | **5 layers** |
| **Test files** | **16 files** |
| **Tests** | **260 (258 passed + 2 skipped)** |
| **Sub-tools** | **12** |
| **HTTP endpoints** | **~20** |
| **Active providers** | **1 (GitHub)** + GitLab/Bitbucket (future) |

---

## 🎯 File Distribution by Layer

```
Layer 5: Web Tool Runtime       ~15 files   (JSON + adapters + bundles)
Layer 4: Core Tool Runtime       ~10 files   (BaseTool + dispatcher)
Layer 3: Integration            ~55 files   (domain + security + git + ops + tools + providers)
Layer 2: Facade                   1 file    (orchestration)
Layer 1: HTTP Router              1 file    (routers)
Tests                            16 files   (260 tests)
```

---

## 🔑 Critical Files (★) — Most Frequently Changed Points

| ★ | File | Role |
|---|---|---|
| ★ | `omweb/routers/source_control.py` | Every new endpoint starts here |
| ★ | `omweb/services/source_control_facade.py` | Orchestration logic — changes affect all tests |
| ★ | `omweb/tools/builtins/source_control.json` | Tool declaration for the LLM |
| ★ | `omweb/adapters/.../tools_adapters/source_control_adapter.py` | Web ↔ Core bridge |
| ★ | `peldrun/tools/builtins/source_control_tool.py` | Dispatcher — routes actions |
| ★ | `integrations/source_control/security/approval.py` | The core security gate |
| ★ | `integrations/source_control/git/executable.py` | All Git commands pass through here |
| ★ | `integrations/source_control/operations/models.py` | State machine |

---

## 🔍 Quick Reference — Where to Make Changes

**To add a new endpoint:**
1. `omweb/routers/source_control.py` — add the route
2. `omweb/services/source_control_facade.py` — add the method
3. `integrations/source_control/schemas.py` — add the DTO
4. `tests/source_control/test_sc_router.py` — add the test

**To add a new action for the agent:**
1. `integrations/source_control/tools/` — add a new sub-tool
2. `integrations/source_control/tools/__init__.py` — register it
3. `peldrun/tools/builtins/source_control_tool.py` — add to `_ACTION_MAP` and `_ACTION_ALIASES`
4. `omweb/tools/builtins/source_control.json` — add the `action` to the `enum`
5. `tests/source_control/test_sc_tools.py` — add the dispatch test

**To add a new provider (GitLab):**
1. `integrations/source_control/providers/gitlab/` — create the folder + all files (copy from github/)
2. `integrations/source_control/providers/__init__.py` — add to `_PROVIDER_FACTORIES`
3. `integrations/source_control/domain/enums.py` — ensure `ProviderId.GITLAB` exists
4. **No changes** to the Facade, tools, or tests (this is the goal of Port/Adapter)

---

## 💾 Estimated Size of Each Layer

| Layer | Estimated LOC |
|---|---|
| **Layer 5** (Web) | ~2,500 |
| **Layer 4** (Core) | ~2,000 |
| **Layer 3** (Integration) | ~12,000 |
| **Layer 2** (Facade) | ~1,800 |
| **Layer 1** (Router) | ~500 |
| **Tests** | ~8,000 |
| **Total** | **~27,000 LOC** |

---

## 🗂️ Related Files in Other Folders

| File | Why It Matters |
|---|---|
| `backend/requirements.txt` | Depends on: `keyring`, `httpx`, `pydantic`, `fastapi`, `cryptography` |
| `backend/omweb/config.py` | Defines `STORAGE_ROOT` (used by the Facade) |
| `backend/pytest.ini` | `pythonpath = .` — important for module tree consistency |
| `backend/tests/__init__.py` | Required (tests are `backend.tests.*`) |

---

**This tree represents the current state of the project after all fixes. Any new addition should follow the same layered pattern.**
