y
<!-- docs/SOURCE_CONTROL_INTEGRATION.md -->

# PELDRUN — Source Control Integration

**Version:** 1.0
**Last updated:** 2026-10-23
**Scope:** Full Git + GitHub management inside PELDRUN
**Status:** MVP complete (5 phases), production-ready for local-first use

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Quick Start](#2-quick-start)
3. [Architecture](#3-architecture)
4. [Backend — Domain Layer](#4-backend--domain-layer)
5. [Backend — Git Engine](#5-backend--git-engine)
6. [Backend — Provider Adapters](#6-backend--provider-adapters)
7. [Backend — Facade](#7-backend--facade)
8. [Backend — HTTP Router](#8-backend--http-router)
9. [Backend — Security](#9-backend--security)
10. [Backend — Operations Journal](#10-backend--operations-journal)
11. [Backend — Schemas](#11-backend--schemas)
12. [Frontend — API Layer](#12-frontend--api-layer)
13. [Frontend — Components](#13-frontend--components)
14. [Features Reference](#14-features-reference)
15. [API Reference](#15-api-reference)
16. [Code Examples](#16-code-examples)
17. [Maintenance Guide](#17-maintenance-guide)
18. [Testing Guide](#18-testing-guide)
19. [Extending the System](#19-extending-the-system)
20. [Known Limitations](#20-known-limitations)
21. [Roadmap](#21-roadmap)
22. [Glossary](#22-glossary)

---

## 1. Introduction

### 1.1 What This Subsystem Does

The **Source Control Integration** allows PELDRUN users to perform Git and
GitHub operations without leaving the application:

- Browse a repository's file tree, commit history, and blame data
- Create, switch, merge, and delete branches
- Open, review, and merge Pull Requests
- Open, comment on, and close Issues
- Create and manage Tags and Releases
- Resolve merge conflicts from a rich 3-way interface

The subsystem is **provider-neutral** at its core (GitLab, Bitbucket, and
self-managed GitHub/GitLab are supported by the domain layer) but only the
**GitHub adapter** is currently wired end-to-end.

### 1.2 Design Goals

| Goal | How it is achieved |
|------|-------------------|
| **Local-first** | All Git operations run through a local CLI wrapper. No server-side Git required. |
| **Safe by default** | Policy engine + approval gate + audit log gate every remote write. |
| **Provider-agnostic** | Domain models (`RepositoryRef`, `BranchRef`, ...) are free of provider-specific fields. |
| **Deterministic** | Diff hashes, tree structures, and commit listings are stable across runs. |
| **Testable** | Every service accepts dependency injection. No global state. |

### 1.3 Non-Goals

- **Not a GitHub clone.** PELDRUN does not reimplement GitHub's web UI.
- **Not a CI/CD runner.** It surfaces CI check status; it does not execute pipelines.
- **Not a git server.** It drives the user's local `git` binary; it does not host repositories.
- **Not a code editor.** File contents are read-only; editing happens in the Editor Tab.

---

## 2. Quick Start

### 2.1 Prerequisites

| Dependency | Version | Notes |
|-----------|---------|-------|
| Python | 3.11+ | Uses `str \| Path` type hints |
| Node.js | 18+ | Vite + TypeScript |
| Git | 2.20+ | Required for `worktree`, `--force-with-lease` |
| GitHub token | `repo`, `read:user`, `user:email` | OAuth flow handles this automatically |

### 2.2 Directory Layout

```
peldrun/
├── backend/
│   ├── integrations/source_control/       ← domain + git engine + providers
│   │   ├── domain/                        ← enums, models, policies
│   │   ├── git/                           ← local Git wrappers (log, merge, tag, ...)
│   │   ├── providers/github/              ← GitHub REST services
│   │   ├── ports/                         ← Protocols / interfaces
│   │   ├── operations/                    ← journal, state machine, idempotency
│   │   ├── schemas/                       ← Pydantic models
│   │   ├── security/                      ← policy, approval, audit
│   │   └── errors.py                      ← error taxonomy
│   ├── omweb/
│   │   ├── routers/source_control.py      ← ALL HTTP endpoints
│   │   └── services/source_control_facade.py  ← orchestration
│   └── data/source_control/               ← runtime data (journal, connections)
│
├── frontend/
│   └── src/features/source-control/
│       ├── api/
│       │   ├── schemas.ts                 ← Zod schemas
│       │   └── source-control-client.ts   ← typed HTTP client
│       ├── components/
│       │   ├── SourceControlPanel.tsx     ← top-level (Agent / Manual / Browse)
│       │   ├── git/                       ← Repository, History, Branches, Merge, Conflicts, Tags
│       │   └── github/                    ← PRs, Issues, Releases
│       ├── hooks/                         ← (mostly reserved)
│       ├── state/                         ← Zustand stores
│       └── utils/                         ← (mostly reserved)
│
└── storage/
    └── chats/<chat_id>/files/             ← workspaces live here (per-chat)
```

### 2.3 Running Locally

```bash
# Backend
cd backend
uvicorn omweb.main:app --port 8088 --reload

# Frontend
cd frontend
npm run dev
```

### 2.4 Verifying Installation

```bash
# Backend health
curl http://127.0.0.1:8088/api/source-control/providers

# Frontend
open http://localhost:5173        # or whatever Vite prints
# Navigate to any chat → "Browse" tab
```

---

## 3. Architecture

### 3.1 High-Level Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                    Frontend (React + TS)                     │
│                                                              │
│  SourceControlPanel                                          │
│  ├── AgentModePanel                                          │
│  ├── ManualModePanel                                         │
│  └── GitBrowserPanel                                         │
│      ├── RepositoryBrowser   ──┐                             │
│      ├── CommitHistory       ──┤                             │
│      ├── BranchManager       ──┼──→ sourceControlClient      │
│      ├── PullRequestPanel    ──┤      (fetch + Zod)          │
│      ├── IssuePanel          ──┤                             │
│      ├── TagManager          ──┤                             │
│      └── ReleaseManager      ──┘                             │
└──────────────────────────────┬───────────────────────────────┘
                               │ HTTP /api/source-control/*
┌──────────────────────────────▼───────────────────────────────┐
│                    FastAPI Router                            │
│  source_control.py — 60+ endpoints                           │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│                  SourceControlFacade                         │
│  Orchestrates: providers + git + approvals + audit + journal │
└──┬──────────┬──────────┬──────────┬──────────┬───────────────┘
   │          │          │          │          │
   ▼          ▼          ▼          ▼          ▼
┌─────┐  ┌──────┐  ┌─────────┐  ┌────────┐  ┌──────────┐
│ Git │  │GitHub│  │ Policy  │  │Approval│  │ Journal  │
│ CLI │  │ REST │  │ Engine  │  │  Gate  │  │(durable) │
└─────┘  └──────┘  └─────────┘  └────────┘  └──────────┘
```

### 3.2 Request Flow — "Create a Pull Request"

```
1. User fills CreatePullRequest form → clicks "Create"
2. sourceControlClient.createPullRequest(repoId, payload)
3. POST /api/source-control/repositories/{repoId}/pulls
4. FastAPI validates payload against CreatePullRequestRequest
5. Calls facade.create_pull_request(payload)
6. Facade:
     a. Resolves provider token (TokenService)
     b. Resolves RepositoryRef (adapter.get_repository)
     c. Policy check (PolicyEngine.require → merge is allowed?)
     d. Delegates to GitHubPullRequestService.create_pull_request
     e. Redacts title/body
     f. Writes audit event
7. Returns PullRequestResponse
8. Frontend switches to PullRequestDetail with the new PR number
```

### 3.3 Layer Responsibilities

| Layer | Responsibility | Must NOT |
|-------|----------------|----------|
| **Domain** (`domain/`) | Pure types: `RepositoryRef`, `BranchRef`, policies | Touch the network or filesystem |
| **Git Engine** (`git/`) | Local Git operations via `subprocess` | Make remote calls or hold state |
| **Providers** (`providers/`) | HTTP calls to the provider API | Enforce policy or approvals |
| **Security** (`security/`) | Policy, approval, audit | Perform Git or HTTP operations |
| **Operations** (`operations/`) | State machine, journal, idempotency | Decide business policy |
| **Facade** (`omweb/services/`) | Orchestration only | Contain business logic |
| **Router** (`omweb/routers/`) | HTTP translation, validation | Touch `subprocess` or `httpx` directly |
| **Frontend** | UI, state, HTTP calls | Access the vault or the token |

---

## 4. Backend — Domain Layer

### 4.1 Enums (`domain/enums.py`)

| Enum | Values | Usage |
|------|--------|-------|
| `ProviderId` | `github`, `gitlab`, `bitbucket` | Adapter selection |
| `ChangeRequestState` | `open`, `draft`, `closed`, `merged`, `unknown` | PR state |
| `FileChangeType` | `added`, `modified`, `deleted`, `renamed`, `copied`, `type_changed`, `unmerged`, `unknown` | Diff classification |
| `OperationStatus` | `created`, `editing`, `review_required`, `approved`, `pushed`, `change_request_created`, `cancelled`, `failed_final`, `rejected`, ... | State machine |
| `ApprovalStatus` | `pending`, `granted`, `rejected`, `expired`, `consumed`, `revoked` | Approval tickets |
| `CheckStatus` / `CheckConclusion` | GitHub-style CI values | CI checks |

### 4.2 Models (`domain/models.py`)

Frozen dataclasses, no I/O:

```python
@dataclass(frozen=True)
class RepositoryRef:
    provider: ProviderId
    account_id: str
    repository_id: str
    namespace: str
    name: str
    clone_url: str
    web_url: str
    default_branch: str
    is_private: bool
    base_url: Optional[str] = None
```

Other key types: `BranchRef`, `CommitRef`, `ChangeRequestRef`, `WorkspaceRef`,
`FileDiff`, `DiffSummary`, `CheckRunRef`, `CheckSummary`, `OperationRecord`,
`ApprovalTicket`, `AuthSession`.

### 4.3 Policies (`domain/policies.py`)

Pure functions that decide what is allowed:

```python
def is_protected_branch(name: str) -> bool:
    """main, master, release/*, prod/* are protected."""

def is_workflow_path(path: str) -> bool:
    """`.github/workflows/**`, `.gitlab-ci.yml`, `Jenkinsfile`, etc."""
```

---

## 5. Backend — Git Engine

The `git/` package is the **only** place that spawns `git` processes. Every
manager follows the same shape:

- Accepts `GitExecutable` and `WorkspacePathPolicy` via constructor
- Exposes `async` methods
- Raises domain errors (`ValidationError`, `WorkspaceNotFoundError`, `ConflictError`, ...)
- Registers a `get_default_*` / `set_default_*` / `reset_default_*` singleton

### 5.1 `executable.py` — The Safe Wrapper

**Responsibilities:**
- Build a sanitized environment (strips `GIT_TRACE`, `GIT_ASKPASS`, `LD_PRELOAD`, ...)
- Enforce timeouts and byte caps
- Redact tokens from stdout/stderr
- Reject NUL bytes in arguments

```python
git = get_default_git_executable()
result = await git.run(
    ["log", "-n5", "--format=%H%x00%an%x00%at%x1e"],
    cwd=workspace_path,
    timeout_seconds=30,
)
```

### 5.2 `workspace.py` — Workspace Lifecycle

- `create_workspace(repo, base_sha, source_branch, use_worktree, target_path)`
- `get_workspace(id)` / `list_workspaces()` / `delete_workspace(id)`
- `refresh_workspace_status(id)`
- `cleanup_orphans()`

Workspaces live under `storage/chats/<chat_id>/files/` (chat-scoped) or
`<workspace_root>/<repo>__<id>` (default).

### 5.3 `branch.py` — Branch Management

- `create_local_branch`, `checkout_branch`, `delete_local_branch`
- `list_local_branches`, `branch_exists`, `get_branch_ref`
- Protected branches require `force=True` to delete or rename

### 5.4 `commit.py` — Commit Creation

- `create_commit`, `amend_commit`
- Uses `git commit -F -` (message via stdin, not argv) so newlines/Unicode work
- Injects `user.name` / `user.email` via `-c` flags (no `~/.gitconfig` dependency)

### 5.5 `diff.py` — Diff Computation

- `compute_diff` returns `DiffSummary` (files + patches)
- Diff hash is deterministic: `SHA-256(canonical_json(file_list))` — this is what approval tickets bind to
- Patches capped at 1 MiB per file

### 5.6 `push.py` — Remote Push

- `push_branch`, `delete_remote_branch`, `reconcile_push`
- **Never** uses `--force`; uses `--force-with-lease` with an explicit SHA
- Credentials via ephemeral `GIT_ASKPASS` script (never embedded in URLs)
- Handles ambiguous timeouts by reconciling against the remote

### 5.7 `status.py` — Status Reading

- `read_status`, `is_clean`, `read_conflicts`, `read_modified`, `read_untracked`

### 5.8 `conflict.py` — Conflict Inspection

- `detect_operation_state` — detects in-progress merge / rebase / cherry-pick / revert
- `list_conflicts` — parses `git ls-files -u -z`
- `resolve_using_ours` / `resolve_using_theirs` / `mark_resolved`
- `abort_operation`

### 5.9 `log.py` — History Reading **(NEW)**

The backend for the Repository Browser and Commit History.

```python
class GitLogReader:
    async def read_log(workspace_id, *, workspace_path, limit=50, skip=0,
                       ref=None, path=None, author=None,
                       since=None, until=None, first_parent=False) -> Sequence[CommitRef]

    async def read_commit(workspace_id, *, workspace_path, sha) -> CommitWithStats

    async def get_commit_patch(workspace_id, *, workspace_path, sha, max_bytes) -> str

    async def list_tree(workspace_id, *, workspace_path, ref=None) -> Sequence[TreeNode]

    async def read_file_at_ref(workspace_id, *, workspace_path, ref, path, max_bytes) -> str
```

The `TreeNode` dataclass is recursive, shaped for `react-arborist`:

```python
@dataclass(frozen=True)
class TreeNode:
    name: str
    path: str
    type: str  # "file" | "directory"
    size: Optional[int] = None
    children: Tuple["TreeNode", ...] = ()
```

### 5.10 `merge.py` — Merge / Rebase **(NEW)**

```python
class GitMergeManager:
    async def merge_branch(workspace_id, *, source_branch, target_branch,
                           strategy="merge"|"squash"|"ff-only",
                           commit_message, workspace_path) -> MergeOutcome

    async def finalize_merge(workspace_id, *, commit_message, workspace_path) -> MergeOutcome

    async def rebase_branch(workspace_id, *, branch, onto, workspace_path) -> MergeOutcome

    async def finalize_rebase(workspace_id, *, workspace_path) -> MergeOutcome

    async def abort_operation(workspace_id, *, workspace_path) -> OperationState
```

`MergeOutcome.kind` is one of:
- `"clean"` — completed successfully
- `"conflicted"` — paused for conflict resolution
- `"up_to_date"` — nothing to do

### 5.11 `tag.py` — Local Tags **(NEW)**

```python
class GitTagManager:
    async def list_tags(workspace_id, *, workspace_path, max_tags) -> Sequence[TagRef]
    async def get_tag(workspace_id, *, workspace_path, name) -> TagRef
    async def create_tag(workspace_id, *, workspace_path, name, message=None,
                         target_sha=None, force=False) -> TagRef
    async def delete_tag(workspace_id, *, workspace_path, name) -> None
```

A tag with a non-empty `message` is annotated (`git tag -a -m`); without one it is
lightweight (`git tag`). This matches GitHub UI behavior.

**Important:** this manager does NOT push tags to the remote. That is a separate
remote-write operation and would need its own approval workflow.

---

## 6. Backend — Provider Adapters

All adapters share `client.py` (`GitHubClient`), which handles retries,
rate limiting, and error mapping. The token is passed as `Authorization: Bearer`.

### 6.1 `pull_requests.py` — Pull Requests + Merge

```python
class GitHubPullRequestService:
    async def create_pull_request(...) -> ChangeRequestRef
    async def get_pull_request(...) -> ChangeRequestRef
    async def list_pull_requests(...) -> Sequence[ChangeRequestRef]
    async def merge_pull_request(  # NEW
        repo, number, merge_method="merge"|"squash"|"rebase",
        commit_title=None, commit_message=None,
        delete_source_branch=False, expected_head_sha=None, token,
    ) -> MergeResult
```

`merge_pull_request` maps GitHub's 405 (not mergeable) to `ConflictError` and
409 (head moved) to `RemoteStateChangedError`.

### 6.2 `reviews.py` — Reviews and Comments **(NEW)**

```python
class GitHubReviewsService:
    async def submit_review(repo, number, event="APPROVE"|"REQUEST_CHANGES"|"COMMENT",
                             body=None, comments=(), token) -> ReviewRef
    async def list_reviews(repo, number, token) -> Sequence[ReviewRef]
    async def create_review_comment(repo, number, body, path, line, side="RIGHT",
                                     commit_id=None, token) -> ReviewCommentRef
    async def create_issue_comment(repo, number, body, token) -> IssueCommentRef
    async def list_comments(repo, number, token) -> tuple[IssueCommentRef, ...], tuple[ReviewCommentRef, ...]
```

**Quirk:** general PR comments live on `/issues/{n}/comments`, not `/pulls/{n}/comments`.
This is GitHub's design and the service mirrors it.

### 6.3 `issues.py` — Issues **(NEW)**

```python
class GitHubIssueService:
    async def list_issues(repo, *, token, state="open", labels=None,
                          page=1, page_size=100, max_results=500,
                          include_pull_requests=False) -> Sequence[IssueRef]
    async def get_issue(repo, *, number, token) -> IssueRef
    async def create_issue(repo, *, title, body, labels, assignees, token) -> IssueRef
    async def update_issue_state(repo, *, number, state, state_reason=None, token) -> IssueRef
    async def list_comments(repo, *, number, token) -> Sequence[IssueCommentRef]
    async def create_comment(repo, *, number, body, token) -> IssueCommentRef
```

**Quirk:** GitHub returns **both issues and PRs** from `/issues`. The service
filters out PRs unless `include_pull_requests=True`.

### 6.4 `releases.py` — Releases **(NEW)**

```python
class GitHubReleaseService:
    async def list_releases(repo, *, token, include_drafts=False, page=1,
                            page_size=30, max_results=200) -> Sequence[ReleaseRef]
    async def get_release(repo, *, release_id, token) -> ReleaseRef
    async def create_release(repo, *, tag_name, name, body, target_commitish,
                              draft, prerelease, token) -> ReleaseRef
    async def delete_release(repo, *, release_id, token) -> None
```

**Important:** `delete_release` deletes the release object only — the underlying
Git tag is preserved. This mirrors GitHub's behavior.

### 6.5 Other Adapters (pre-existing)

- `branches.py` — branch listing
- `checks.py` — CI check runs
- `repositories.py` — repo listing + file content
- `auth.py` — OAuth token exchange + validation

---

## 7. Backend — Facade

`SourceControlFacade` (`omweb/services/source_control_facade.py`) is the single
orchestration point. It **does not contain business logic** — it wires services.

### 7.1 Initialization

```python
facade = SourceControlFacade(
    registry=...,
    token_service=...,
    vault=...,
    policy_engine=...,
    approval_gate=...,
    operation_service=...,
    workspace_manager=...,
    diff_engine=...,
    push_manager=...,
    log_reader=...,          # Phase 1
    branch_manager=...,      # Phase 1
    pr_service=...,          # Phase 2
    reviews_service=...,     # Phase 2
    merge_manager=...,       # Phase 4
    conflict_manager=...,    # Phase 4
    tag_manager=...,         # Phase 5
    release_service=...,     # Phase 5
)
```

All arguments are optional. When omitted, the process-wide default singleton is
used, guaranteeing that the facade sees the same workspaces the agent created.

### 7.2 Facade Method Groups

| Section | Methods |
|---------|---------|
| Connection management | `list_providers`, `list_connections`, `save_provider_config`, `begin_connection`, `complete_oauth_callback`, `validate_connection`, `disconnect_provider` |
| Repos + branches | `list_repositories`, `get_repository`, `list_branches`, `create_branch`, `read_file` |
| Workspaces | `create_workspace`, `get_workspace`, `get_workspace_by_chat`, `ensure_editable_branch`, `cleanup_workspaces`, `cleanup_orphaned_operations`, `inspect_workspace_diff`, `run_checks` |
| Operations | `create_operation`, `get_operation`, `request_operation_approval`, `approve_operation`, `reject_operation`, `push_approved_branch`, `create_change_request` |
| Audit | `list_audit_events` |
| **Git read-only** (Phase 1) | `read_workspace_log`, `read_workspace_commit`, `list_workspace_tree`, `read_file_at_ref`, `checkout_workspace_branch` |
| **Pull requests** (Phase 2) | `list_pull_requests`, `get_pull_request`, `create_pull_request`, `merge_pull_request`, `submit_pr_review`, `create_pr_comment`, `list_pr_reviews_and_comments` |
| **Issues** (Phase 3) | `list_issues`, `get_issue`, `create_issue`, `update_issue_state`, `list_issue_comments`, `create_issue_comment` |
| **Merge / rebase** (Phase 4) | `merge_workspace_branches`, `rebase_workspace_branch`, `finalize_workspace_operation`, `abort_workspace_operation`, `list_workspace_conflicts`, `resolve_workspace_conflicts` |
| **Tags** (Phase 5) | `list_workspace_tags`, `create_workspace_tag`, `delete_workspace_tag` |
| **Releases** (Phase 5) | `list_releases`, `get_release`, `create_release`, `delete_release` |

### 7.3 Lazy Provider Guards

Some methods raise `UnsupportedCapabilityError` for non-GitHub providers:

```python
def _get_github_pr_service(self, provider_id: ProviderId) -> GitHubPullRequestService:
    if provider_id not in _PR_CAPABLE_PROVIDERS:  # frozenset({ProviderId.GITHUB})
        raise UnsupportedCapabilityError(
            "Pull request operations are only supported for GitHub.",
            details={"provider": provider_id.value},
        )
    if self._pr_service is None:
        self._pr_service = GitHubPullRequestService()
    return self._pr_service
```

The same pattern exists for `_get_github_reviews_service`,
`_get_github_issue_service`, and `_get_github_release_service`.

---

## 8. Backend — HTTP Router

All source-control endpoints live in a **single** file:
`backend/omweb/routers/source_control.py`.

### 8.1 Why One File?

The `try: import integrations.*` + `except ImportError: import backend.integrations.*`
pattern is required in every file. Adding a second file meant duplicating
those imports, the error helper, and the router prefix. Consolidating was the
lesser evil.

The route file is ~3000 lines. If it grows much further, split into sub-routers
via `APIRouter` + `include_router`.

### 8.2 Route Ordering Rules

FastAPI matches in declaration order. The `{repository_id:path}` converter is
**greedy** and will swallow everything after it. Therefore:

**Correct order:**
```
GET    /repositories/{repository_id:path}/pulls      ← more specific first
GET    /repositories/{repository_id:path}/issues
GET    /repositories/{repository_id:path}/releases
GET    /repositories/{repository_id:path}            ← catch-all last
```

**Wrong order** produces routes like `/repositories/my/repo/pulls/1` being
matched as `repository_id = "my/repo/pulls/1"` and 404-ing.

### 8.3 Error Handling

Every endpoint delegates to `handle_source_control_error`, which maps domain
errors via `SourceControlError.http_status`:

| Error | HTTP |
|-------|------|
| `AuthRequiredError`, `AuthExpiredError` | 401 |
| `PermissionDeniedError`, `PolicyDeniedError`, `ProtectedBranchError` | 403 |
| `NotFoundError` | 404 |
| `ConflictError`, `RemoteStateChangedError` | 409 |
| `ValidationError` | 400 |
| `ApprovalRequiredError` | 428 |
| `UnsupportedCapabilityError` | 501 |
| `RateLimitedError` | 429 |
| `CommandTimeoutError` | 504 |
| Anything else | 500 |

---

## 9. Backend — Security

### 9.1 Authentication

The router does **not** enforce authentication. The deployment model is:

- Single-user local installation, OR
- Behind an authenticating reverse proxy

If exposed to a network, mount behind auth middleware.

### 9.2 Authorization (Policy Engine)

`PolicyEngine.require(ctx)` is called **before** any remote write:

```python
policy_ctx = PolicyResolutionContext(
    operation="push_branch",
    provider=provider_id,
    target_branch=ticket.target_branch,
    actor="system",
)
self.policy_engine.require(policy_ctx)
```

Policies enforce:
- Push to `main` / `master` / `release/*` → denied
- Merge / delete branch → denied by default
- Workflow file changes → elevated approval

### 9.3 Approval Gate

Destructive operations consume a **single-use, content-bound approval ticket**:

```python
ticket = self.approval_gate.consume(
    approval_id=payload.approval_id,
    expected_operation_id=operation_id,
    expected_diff_sha256=current_hash,  # re-computed at execution time
    ...
)
```

The ticket binds to a specific diff hash. If the diff changes between approval
and execution, the ticket is rejected.

### 9.4 Token Handling

- Tokens live in the OS credential vault (`CredentialVaultPort`)
- The facade reads them via `TokenService.get_access_token(...)`
- `GitPushManager` passes them via a temporary `GIT_ASKPASS` script
- Tokens are **never** logged, embedded in URLs, or stored in `.git/config`

### 9.5 Audit Log

Every mutating operation writes an audit event via `self._audit(...)`:

```python
self._audit(
    AuditEventType.OPERATION_CREATE,
    action="create_operation",
    message=f"Operation '{payload.operation_type}' created.",
    payload={...},
    operation_id=record.operation_id,
    repository_id=record.repository_id,
    provider=provider_id.value,
    actor="agent",
)
```

Events are append-only and queryable via `GET /api/source-control/audit`.

---

## 10. Backend — Operations Journal

### 10.1 State Machine

Operations move through these states (defined in `operations/models.py`):

```
CREATED
  → REPOSITORY_RESOLVED
    → WORKSPACE_READY
      → EDITING
        → CHECKING
          → REVIEW_REQUIRED
            → APPROVED
              → PUSHING
                → PUSHED
                  → CHANGE_REQUEST_CREATED
```

Failure branches: `FAILED_RETRYABLE`, `FAILED_FINAL`, `REJECTED`,
`CANCELLED`, `BLOCKED`, `STALE_REVIEW_REQUIRED`.

Transitions are validated by `is_valid_transition(from, to)`. The service
rejects any transition not in `ALLOWED_TRANSITIONS`.

### 10.2 Durability

The journal (`operations/journal.py`) persists each operation as two files:

```
backend/data/source_control/journal/operations/
  op_abc123.json       ← OperationRecord (JSON)
  op_abc123.jsonl      ← OperationEvents (JSON Lines, append-only)
```

Writes are atomic via `tempfile.mkstemp` + `os.replace` with retry on Windows
`WinError 5`.

### 10.3 Idempotency

`operations/idempotency.py` prevents duplicate execution:

```python
key = compute_key(
    kind="push_branch",
    provider=ProviderId.GITHUB,
    repository_id="owner/repo",
    source_branch="feature/x",
    target_branch="feature/x",
    base_sha=base,
    diff_sha256=diff_hash,
)
registry.register(key, operation_id)  # raises ConflictError if duplicate
```

### 10.4 Recovery

On startup, `OperationRecoveryService.recover_all()` brings the journal to a
consistent state:

- `PUSHING` operations → reconciled with remote (or marked stale)
- `EDITING` / `CHECKING` → marked `FAILED_RETRYABLE`
- `REVIEW_REQUIRED` / `APPROVED` → left as-is (user resumes)
- `CANCEL_REQUESTED` → marked `CANCELLED`

---

## 11. Backend — Schemas

`schemas/requests.py` — Pydantic models for incoming payloads:

```python
class CreatePullRequestRequest(BaseSCRequest):
    repository_id: str
    source_branch: str
    target_branch: str
    title: str = Field(..., max_length=256)
    body: Optional[str] = Field(default="", max_length=65_536)
    draft: bool = True
    provider: str = "github"
```

`schemas/responses.py` — Pydantic models for outgoing payloads:

```python
class PullRequestResponse(BaseSCResponse):
    provider: str
    repository_id: str
    number: str
    web_url: str
    state: str  # "open" | "closed" | "merged" | "draft" | "unknown"
    source_branch: str
    target_branch: str
    title: str
    body: Optional[str]
    is_draft: bool
    author_login: Optional[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    merged_at: Optional[datetime]
```

All request schemas inherit from `BaseSCRequest`:
- `str_strip_whitespace=True`
- `extra="forbid"` — typos are rejected, not silently ignored
- `frozen=True` — immutable

All response schemas inherit from `BaseSCResponse`:
- `frozen=True`
- `from_attributes=True` — can be built from domain objects

---

## 12. Frontend — API Layer

### 12.1 `schemas.ts` — Zod Schemas

Every Pydantic schema has a Zod mirror:

```typescript
export const PullRequestResponseSchema = z.object({
  provider: z.string(),
  repository_id: z.string(),
  number: z.string(),
  web_url: z.string(),
  state: z.string(),
  source_branch: z.string(),
  target_branch: z.string(),
  title: z.string().default(""),
  body: z.string().nullable().optional(),
  is_draft: z.boolean().default(false),
  author_login: z.string().nullable().optional(),
  created_at: z.string().datetime().nullable().optional(),
  updated_at: z.string().datetime().nullable().optional(),
  merged_at: z.string().datetime().nullable().optional(),
});

export type PullRequestResponse = z.infer<typeof PullRequestResponseSchema>;
```

### 12.2 `source-control-client.ts` — HTTP Client

A single `request<T>()` helper wraps `fetch`:

```typescript
async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/source-control${endpoint}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (response.ok) return response.json() as Promise<T>;
  // ... error mapping via SourceControlApiError
}
```

Every endpoint has a typed method:

```typescript
export const sourceControlClient = {
  async createPullRequest(
    repositoryId: string,
    payload: Omit<CreatePullRequestRequest, "repository_id"> & { repository_id?: string },
  ): Promise<PullRequestResponse> {
    const body = { ...payload, repository_id: repositoryId };
    return request<PullRequestResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/pulls`,
      { method: "POST", body: JSON.stringify(body) },
    );
  },
  // ... more methods
};
```

---

## 13. Frontend — Components

### 13.1 Component Tree

```
SourceControlPanel                     ← top-level tab bar (Agent / Manual / Browse)
├── AgentModePanel
│   ├── ApprovalHeader
│   ├── FileChangeList
│   ├── DiffViewer
│   ├── CheckResults
│   ├── OperationTimeline
│   └── ApprovalDialog
├── ManualModePanel
│   └── (5-step wizard)
└── BrowsePanel
    └── GitBrowserPanel               ← tabs: Files / History / Branches / PRs / Issues / Tags / Releases
        ├── RepositoryBrowser
        │   └── FileViewer
        ├── CommitHistory
        ├── BranchManager
        │   ├── MergeDialog
        │   └── ConflictResolver
        ├── PullRequestPanel
        │   ├── PullRequestList
        │   ├── PullRequestDetail
        │   └── CreatePullRequest
        ├── IssuePanel
        │   ├── IssueList
        │   ├── IssueDetail
        │   └── CreateIssue
        ├── TagManager
        └── ReleaseManager
```

### 13.2 Common Component Pattern

All feature components follow the same shape:

```typescript
export function SomeComponent({ workspaceId }: Props) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    sourceControlClient.someMethod(workspaceId)
      .then((res) => { if (!cancelled) setData(res); })
      .catch((err) => { if (!cancelled) setError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [workspaceId]);

  // render loading / error / data
}
```

**Key rules:**
- Per-effect `cancelled` flag prevents stale state after unmount
- Loading and error are always shown; never silently swallow failures
- No `setState` during render
- All API calls go through `sourceControlClient` (never `fetch` directly)

### 13.3 Tab Integration

`GitBrowserPanel` owns all tabs. Adding a new tab requires:

1. Extend `type Tab` union
2. Add a `<TabButton>` in the header
3. Add the `{tab === "newtab" && <Component />}` block

```typescript
type Tab = "files" | "history" | "branches" | "pulls" | "issues" | "tags" | "releases";
```

---

## 14. Features Reference

### 14.1 Repository Browser

- **Files tab** — expandable tree (react-arborist), click file to view content
- **Ref selector** — input box lets user switch to any tag/SHA
- **Empty repos** return "No files" instead of an error

Backed by:
- `GET /workspaces/{id}/files/tree` → `list_workspace_tree`
- `GET /workspaces/{id}/file-content?ref=&path=` → `read_file_at_ref`

### 14.2 Commit History

- Paginated list (30 per page, "Load more" button)
- Each row: SHA, author, relative time, message subject
- Expand → fetches stats (files changed, insertions, deletions)
- "Browse files at this commit" → switches Files tab to that SHA

Backed by:
- `GET /workspaces/{id}/log` → `read_workspace_log`
- `GET /workspaces/{id}/commits/{sha}` → `read_workspace_commit`

### 14.3 Branch Manager

- Lists remote branches (with `default` / `protected` badges)
- Checkout switches the local workspace to a branch
- Create opens a form to create a remote branch
- Merge opens `MergeDialog` (Phase 4)
- Conflict banner appears when a merge/rebase is in progress

Backed by:
- `GET /repositories/{id}/branches`
- `POST /repositories/{id}/branches`
- `POST /workspaces/{id}/checkout`

### 14.4 Pull Requests

- **List:** filter by state (open / closed / merged / all)
- **Detail:** title, body, reviews, comments, merge box, review box
- **Create:** form with branch selectors, title, body, draft toggle
- **Merge:** choose method (merge / squash / rebase), optional delete source branch
- **Review:** approve / request changes / comment

Backed by:
- `GET /repositories/{id}/pulls`
- `GET /repositories/{id}/pulls/{number}`
- `POST /repositories/{id}/pulls`
- `PUT /repositories/{id}/pulls/{number}/merge`
- `POST /repositories/{id}/pulls/{number}/reviews`
- `POST /repositories/{id}/pulls/{number}/comments`
- `GET /repositories/{id}/pulls/{number}/timeline`

### 14.5 Issues

- **List:** filter by state
- **Detail:** title, body, labels, comments, close/reopen button
- **Create:** form with title, body, labels, assignees

**Note:** GitHub returns PRs from `/issues`. The backend filters them out by
default; pass `include_pull_requests=true` to include them.

Backed by:
- `GET /repositories/{id}/issues`
- `GET /repositories/{id}/issues/{number}`
- `POST /repositories/{id}/issues`
- `PATCH /repositories/{id}/issues/{number}`
- `GET /repositories/{id}/issues/{number}/comments`
- `POST /repositories/{id}/issues/{number}/comments`

### 14.6 Merge & Conflicts

- **MergeDialog** — choose source/target/strategy (merge/squash/ff-only)
- **ConflictResolver** — modal shows conflicted files; each row has "Ours" / "Theirs"
  buttons; bulk actions (resolve all selected as ours/theirs/manual)
- **Finalize** — commits the merge once all conflicts are resolved
- **Abort** — cancels the in-progress operation

Backed by:
- `POST /workspaces/{id}/merge`
- `POST /workspaces/{id}/rebase`
- `POST /workspaces/{id}/finalize`
- `POST /workspaces/{id}/abort`
- `GET /workspaces/{id}/conflicts`
- `POST /workspaces/{id}/conflicts/resolve`

### 14.7 Tags (Local)

- Lists local Git tags, filter by annotated/lightweight
- Create form (name, message, target SHA, force toggle)
- Delete (with confirmation)

**Important:** tags are local only. Pushing them to the remote requires a
separate operation (not implemented).

Backed by:
- `GET /workspaces/{id}/tags`
- `POST /workspaces/{id}/tags`
- `DELETE /workspaces/{id}/tags/{name:path}`

### 14.8 Releases (GitHub)

- Lists releases, drafts excluded by default
- Create form (tag, title, notes, target branch, draft/prerelease)
- Delete (with confirmation; tag NOT deleted)
- External link to GitHub release page

Backed by:
- `GET /repositories/{id}/releases`
- `GET /repositories/{id}/releases/{release_id}`
- `POST /repositories/{id}/releases`
- `DELETE /repositories/{id}/releases/{release_id}`

---

## 15. API Reference

### 15.1 Endpoint Summary

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/providers` | List registered providers |
| `GET` | `/connections` | List connection statuses |
| `POST` | `/connections/{provider}/config` | Save OAuth client credentials |
| `POST` | `/connections/{provider}/begin` | Start OAuth flow |
| `GET` | `/oauth/callback/{provider}` | OAuth callback (HTML) |
| `DELETE` | `/connections/{provider}` | Disconnect |
| `GET` | `/repositories` | List repos |
| `GET` | `/repositories/{id}/branches` | List branches |
| `POST` | `/repositories/{id}/branches` | Create branch |
| `GET` | `/repositories/{id}/file` | Read file |
| **Pull Requests** |
| `GET` | `/repositories/{id}/pulls` | List PRs |
| `GET` | `/repositories/{id}/pulls/{n}` | Get PR |
| `POST` | `/repositories/{id}/pulls` | Create PR |
| `PUT` | `/repositories/{id}/pulls/{n}/merge` | Merge PR |
| `POST` | `/repositories/{id}/pulls/{n}/reviews` | Submit review |
| `POST` | `/repositories/{id}/pulls/{n}/comments` | Comment on PR |
| `GET` | `/repositories/{id}/pulls/{n}/timeline` | Reviews + comments |
| **Issues** |
| `GET` | `/repositories/{id}/issues` | List issues |
| `GET` | `/repositories/{id}/issues/{n}` | Get issue |
| `POST` | `/repositories/{id}/issues` | Create issue |
| `PATCH` | `/repositories/{id}/issues/{n}` | Update state |
| `GET` | `/repositories/{id}/issues/{n}/comments` | List comments |
| `POST` | `/repositories/{id}/issues/{n}/comments` | Create comment |
| **Releases** |
| `GET` | `/repositories/{id}/releases` | List releases |
| `GET` | `/repositories/{id}/releases/{rid}` | Get release |
| `POST` | `/repositories/{id}/releases` | Create release |
| `DELETE` | `/repositories/{id}/releases/{rid}` | Delete release |
| **Workspaces** |
| `POST` | `/workspaces` | Create workspace |
| `GET` | `/workspaces/{id}` | Get workspace |
| `GET` | `/workspaces/by-chat/{chat_id}` | Workspace for chat |
| `POST` | `/workspaces/{id}/checkout` | Checkout branch |
| `GET` | `/workspaces/{id}/diff` | Workspace diff |
| `POST` | `/workspaces/{id}/checks` | Run checks |
| `GET` | `/workspaces/{id}/log` | Commit history |
| `GET` | `/workspaces/{id}/commits/{sha}` | Commit with stats |
| `GET` | `/workspaces/{id}/files/tree` | File tree at ref |
| `GET` | `/workspaces/{id}/file-content` | File at ref |
| **Merge & Conflicts** |
| `POST` | `/workspaces/{id}/merge` | Merge branches |
| `POST` | `/workspaces/{id}/rebase` | Rebase |
| `POST` | `/workspaces/{id}/finalize` | Finalize in-progress |
| `POST` | `/workspaces/{id}/abort` | Abort in-progress |
| `GET` | `/workspaces/{id}/conflicts` | List conflicts |
| `POST` | `/workspaces/{id}/conflicts/resolve` | Resolve |
| **Tags** |
| `GET` | `/workspaces/{id}/tags` | List tags |
| `POST` | `/workspaces/{id}/tags` | Create tag |
| `DELETE` | `/workspaces/{id}/tags/{name:path}` | Delete tag |
| **Operations** |
| `POST` | `/operations` | Create operation |
| `GET` | `/operations/{id}` | Get operation |
| `POST` | `/operations/{id}/approval` | Request approval |
| `POST` | `/operations/{id}/approve` | Approve |
| `POST` | `/operations/{id}/reject` | Reject |
| `POST` | `/operations/{id}/push` | Push approved branch |
| `POST` | `/operations/{id}/change-request` | Open PR |
| **Audit** |
| `GET` | `/audit` | Audit log |

### 15.2 Example — Create Issue

**Request:**
```http
POST /api/source-control/repositories/owner%2Frepo/issues
Content-Type: application/json

{
  "repository_id": "owner/repo",
  "title": "Login button does not respond",
  "body": "Steps to reproduce:\n1. Open the app\n2. Click Login",
  "labels": ["bug", "P1"],
  "assignees": ["octocat"],
  "provider": "github"
}
```

**Response (200):**
```json
{
  "provider": "github",
  "repository_id": "owner/repo",
  "number": "42",
  "web_url": "https://github.com/owner/repo/issues/42",
  "state": "open",
  "title": "Login button does not respond",
  "body": "Steps to reproduce:\n1. Open the app\n2. Click Login",
  "author_login": "octocat",
  "labels": ["bug", "P1"],
  "comments_count": 0,
  "is_pull_request": false,
  "state_reason": null,
  "created_at": "2026-10-23T08:15:00Z",
  "updated_at": "2026-10-23T08:15:00Z",
  "closed_at": null
}
```

### 15.3 Example — Merge Branches

**Request:**
```http
POST /api/source-control/workspaces/ws_abc123/merge
Content-Type: application/json

{
  "source_branch": "feature/foo",
  "target_branch": "main",
  "strategy": "merge",
  "commit_message": "Merge feature/foo"
}
```

**Response — clean merge (200):**
```json
{
  "workspace_id": "ws_abc123",
  "kind": "clean",
  "operation_state": "none",
  "conflicts": [],
  "head_sha": "abc123...",
  "message": "Merge made by the 'ort' strategy."
}
```

**Response — conflicted (200):**
```json
{
  "workspace_id": "ws_abc123",
  "kind": "conflicted",
  "operation_state": "merging",
  "conflicts": [
    {
      "path": "src/main.py",
      "has_base": true,
      "has_ours": true,
      "has_theirs": true,
      "is_delete_modify": false,
      "is_add_add": false
    }
  ],
  "head_sha": "abc123...",
  "message": "Merge paused for conflict resolution."
}
```

### 15.4 Error Codes

| HTTP | Error Class | When |
|------|------------|------|
| 400 | `ValidationError` | Malformed payload, invalid branch name, path traversal |
| 401 | `AuthRequiredError` | No connection or token |
| 403 | `PermissionDeniedError` | Policy denial, protected branch |
| 404 | `NotFoundError` | Repo, branch, PR, issue, tag, workspace missing |
| 409 | `ConflictError`, `RemoteStateChangedError` | Tag exists, merge conflict, remote moved |
| 422 | Pydantic validation | Type mismatch, missing field |
| 428 | `ApprovalRequiredError` | Operation needs approval |
| 429 | `RateLimitedError` | GitHub API rate limit |
| 501 | `UnsupportedCapabilityError` | Non-GitHub provider for GitHub-only feature |
| 504 | `CommandTimeoutError` | Git command exceeded timeout |

---

## 16. Code Examples

### 16.1 Calling the API from React

```typescript
import { sourceControlClient } from "../api/source-control-client";

// List PRs
const listResponse = await sourceControlClient.listPullRequests("owner/repo", {
  state: "open",
  page: 1,
  page_size: 30,
});
console.log(`${listResponse.total} open PRs`);

// Create a PR
const pr = await sourceControlClient.createPullRequest("owner/repo", {
  source_branch: "feature/login",
  target_branch: "main",
  title: "Add login button",
  body: "Closes #42",
  draft: false,
  provider: "github",
});
window.open(pr.web_url, "_blank");

// Merge a PR
const merged = await sourceControlClient.mergePullRequest("owner/repo", pr.number, {
  merge_method: "squash",
  delete_source_branch: true,
});
console.log(merged.merged ? "Merged!" : "Conflict: " + merged.message);
```

### 16.2 Adding a New Backend Endpoint

Suppose you want `GET /workspaces/{id}/blame?ref=&path=`.

**Step 1 — Add the service method** (`git/blame.py`):

```python
class GitBlameManager:
    async def blame(self, workspace_id, *, workspace_path, ref, path) -> Sequence[BlameLine]:
        target = self._policy.resolve_within_root(workspace_path, must_exist=True)
        result = await self._git.run(
            ["blame", "--porcelain", ref, "--", path],
            cwd=target,
        )
        return _parse_porcelain_blame(result.stdout)
```

**Step 2 — Add the facade method** (`omweb/services/source_control_facade.py`):

```python
async def blame_workspace_file(
    self, workspace_id: str, *, ref: str, path: str
) -> Sequence[BlameLineResponse]:
    ws_ref = self.workspace_manager.get_workspace(workspace_id)
    if ws_ref is None:
        raise NotFoundError(f"Workspace '{workspace_id}' was not found.")
    lines = await self.blame_manager.blame(
        workspace_id,
        workspace_path=ws_ref.workspace_path,
        ref=ref,
        path=path,
    )
    return tuple(self._to_blame_line_response(l) for l in lines)
```

**Step 3 — Add the router endpoint** (`omweb/routers/source_control.py`):

```python
@router.get(
    "/workspaces/{workspace_id}/blame",
    response_model=Sequence[BlameLineResponse],
)
async def blame_workspace_file(
    workspace_id: str,
    ref: str = Query(...),
    path: str = Query(...),
    facade: SourceControlFacade = Depends(get_source_control_facade),
) -> Sequence[BlameLineResponse]:
    try:
        return await facade.blame_workspace_file(
            workspace_id=workspace_id, ref=ref, path=path
        )
    except Exception as exc:
        handle_source_control_error(exc)
```

**Step 4 — Test:**

```bash
python -c "from omweb.routers.source_control import router; print([r.path for r in router.routes if 'blame' in r.path])"
```

### 16.3 Adding a New Frontend Component

**Step 1 — Add the Zod schema** (`api/schemas.ts`):

```typescript
export const BlameLineResponseSchema = z.object({
  line_no: z.number().int(),
  sha: z.string(),
  author: z.string(),
  message: z.string(),
  content: z.string(),
});
export type BlameLineResponse = z.infer<typeof BlameLineResponseSchema>;
```

**Step 2 — Add the client method** (`api/source-control-client.ts`):

```typescript
async getBlame(
  workspaceId: string,
  ref: string,
  path: string,
): Promise<BlameLineResponse[]> {
  const params = new URLSearchParams({ ref, path });
  return request<BlameLineResponse[]>(
    `/workspaces/${encodeURIComponent(workspaceId)}/blame?${params}`,
  );
}
```

**Step 3 — Create the component:**

```tsx
// components/git/BlameView.tsx
export function BlameView({ workspaceId, ref, path }: Props) {
  const [lines, setLines] = useState<BlameLineResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    sourceControlClient.getBlame(workspaceId, ref, path)
      .then((data) => { if (!cancelled) setLines(data); })
      .catch((err) => { if (!cancelled) setError(err.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [workspaceId, ref, path]);

  if (loading) return <div>Loading blame…</div>;
  if (error) return <div className="text-red-700">{error}</div>;
  return (
    <table>
      {lines.map((l) => (
        <tr key={l.line_no}>
          <td>{l.line_no}</td>
          <td>{l.sha.slice(0, 8)}</td>
          <td>{l.author}</td>
          <td><code>{l.content}</code></td>
        </tr>
      ))}
    </table>
  );
}
```

**Step 4 — Wire into `GitBrowserPanel.tsx`:**

```typescript
type Tab = "files" | "history" | "branches" | "pulls" | "issues" | "tags" | "releases" | "blame";
// ...
<TabButton active={tab === "blame"} onClick={() => setTab("blame")} icon={<FileSearch />} label="Blame" />
// ...
{tab === "blame" && <BlameView workspaceId={workspaceId} ref={browseRef} path={selectedFile} />}
```

### 16.4 Adding a New Provider (e.g. GitLab)

**Step 1 — Add the enum value** (`domain/enums.py`):

```python
class ProviderId(str, Enum):
    GITHUB = "github"
    GITLAB = "gitlab"      # already present
    BITBUCKET = "bitbucket"
```

**Step 2 — Implement the adapter** (`providers/gitlab/`):

Mirror the structure of `providers/github/`:
- `client.py` (HTTP wrapper)
- `repositories.py`, `branches.py`, `pull_requests.py`,
  `issues.py`, `releases.py`, `reviews.py`, `checks.py`, `auth.py`
- `manifest.py`, `mapper.py`, `errors.py`

Each must implement the protocols in `ports/provider.py`.

**Step 3 — Register it** (`integrations/source_control/__init__.py`):

```python
def register_builtin_providers(registry: ProviderRegistry) -> None:
    from .providers.github.adapter import GitHubProviderAdapter
    from .providers.gitlab.adapter import GitLabProviderAdapter
    registry.register("github", GitHubProviderAdapter())
    registry.register("gitlab", GitLabProviderAdapter())
```

**Step 4 — Enable GitHub-only features for the new provider:**

```python
# source_control_facade.py
_PR_CAPABLE_PROVIDERS = frozenset({ProviderId.GITHUB, ProviderId.GITLAB})
```

And update `_get_github_pr_service` / `_get_github_reviews_service` etc. to
dispatch on provider.

---

## 17. Maintenance Guide

### 17.1 Logs

| Log | Location | What's in it |
|-----|----------|--------------|
| Backend server log | stdout (uvicorn) | HTTP requests, errors, audit events |
| Journal events | `backend/data/source_control/journal/operations/*.jsonl` | One line per state transition |
| Audit log | Same directory, queried via `GET /audit` | User/agent actions |
| Connection registry | `backend/data/source_control/connections.json` | Non-secret account metadata |
| Provider configs | `backend/data/source_control/provider_configs.json` | OAuth client IDs (secrets in OS vault) |

### 17.2 Data Locations

| Data | Path |
|------|------|
| Workspaces | `storage/chats/<chat_id>/files/` or `~/.peldrun/workspaces/` |
| Journal | `backend/data/source_control/journal/` |
| Workspace registry | `backend/data/source_control/workspaces.json` |
| Tokens | OS credential vault (Windows Credential Manager / macOS Keychain / Linux Secret Service) |

### 17.3 Common Issues

**Problem:** `Workspace 'ws_xyz' was not found` but the folder exists.

**Cause:** The workspace registry lost the entry (e.g. server restart).

**Fix:** The facade auto-recovers via `inspect_workspace_diff` by scanning
`storage/chats/*/files/.peldrun/workspace.json`. If the marker is missing, run
`POST /api/source-control/workspaces/cleanup` then re-create the workspace.

---

**Problem:** `AuthRequiredError: Credentials for 'github' are missing.`

**Cause:** The token expired and refresh failed.

**Fix:** `POST /api/source-control/connections/github/validate` with
`{force_refresh: true}`. If that fails, disconnect and reconnect.

---

**Problem:** `ConflictError: Tag 'v1.0.0' already exists.`

**Cause:** A tag with the same name exists locally.

**Fix:** Either pass `force: true` (overwrite) or delete the existing tag first.

---

**Problem:** `UnsupportedCapabilityError: Pull request operations are only supported for GitHub.`

**Cause:** Tried PR operations with `provider: "gitlab"`.

**Fix:** Only GitHub is wired today. Extend the facade to support GitLab (see §16.4).

---

**Problem:** Merge fails with `WorkspaceDirtyError`.

**Cause:** Workspace has uncommitted changes.

**Fix:** Commit or stash them first. (Stash is not implemented; commit is the
current path.)

### 17.4 Backup and Restore

**Backup:**
```bash
cp -r backend/data/source_control /path/to/backup/
cp -r storage/chats /path/to/backup/
```

**Restore:**
```bash
cp -r /path/to/backup/source_control backend/data/
cp -r /path/to/backup/chats storage/
# Restart the server; the registry re-reads automatically.
```

### 17.5 Cleanup Operations

| Endpoint | Purpose |
|----------|---------|
| `POST /workspaces/cleanup` | Removes orphaned workspace entries (folder gone) |
| `POST /operations/cleanup?dry_run=true` | Previews orphaned journal operations |
| `POST /operations/cleanup` | Deletes orphaned journal operations |

### 17.6 Health Checks

```bash
# Backend up
curl http://127.0.0.1:8088/api/source-control/providers

# Database of connections
curl http://127.0.0.1:8088/api/source-control/connections

# Audit log
curl http://127.0.0.1:8088/api/source-control/audit?limit=10
```

---

## 18. Testing Guide

### 18.1 Smoke Tests (Backend)

```bash
cd backend

# Import every module
python -c "from omweb.main import app; print('OK')"

# Verify all source-control routes registered
python -c "from omweb.routers.source_control import router; print(len(router.routes), 'routes')"

# Verify facade methods
python -c "from omweb.services.source_control_facade import SourceControlFacade; print([m for m in dir(SourceControlFacade) if not m.startswith('_')])"
```

### 18.2 Smoke Tests (Frontend)

```bash
cd frontend
npx tsc --noEmit
```

### 18.3 Manual Test Flows

| # | Flow | Steps | Expected |
|---|------|-------|----------|
| 1 | Browse files | Browse → Files → click a file | File content appears |
| 2 | Commit history | Browse → History → click a commit → expand | Stats appear |
| 3 | Branch checkout | Browse → Branches → click "Checkout" on non-current branch | Branch becomes current |
| 4 | Create PR | Browse → PRs → New → fill → Create | PR appears in list, detail view opens |
| 5 | Merge PR | Open a PR → Merge box → Merge | PR state becomes "merged" |
| 6 | Create issue | Browse → Issues → New → fill → Create | Issue appears |
| 7 | Comment on issue | Open issue → Comment box → Comment | Comment appears in thread |
| 8 | Merge branches | Browse → Branches → Merge → choose → Merge | Clean merge OR Conflict Resolver opens |
| 9 | Resolve conflict | In Conflict Resolver → click "Ours" | File disappears from list |
| 10 | Create tag | Browse → Tags → New → fill → Create | Tag appears |
| 11 | Create release | Browse → Releases → New → fill → Publish | Release appears |

### 18.4 Automated Tests (Future)

The subsystem has unit tests for the security, journal, and policy modules
in `backend/tests/`. Integration tests against a live GitHub account should
be added in a follow-up:

- Use a dedicated test repository
- Use a fine-grained PAT with minimal scope
- Clean up test branches/tags/PRs after each run

---

## 19. Extending the System

### 19.1 Adding a New Feature — Checklist

1. **Domain model** (if needed): `domain/models.py`
2. **Enum values** (if needed): `domain/enums.py`
3. **Service implementation** in `git/` or `providers/<provider>/`
4. **Request schema**: `schemas/requests.py`
5. **Response schema**: `schemas/responses.py`
6. **Re-export** in `schemas/__init__.py`
7. **Facade method**: `omweb/services/source_control_facade.py`
8. **Router endpoint**: `omweb/routers/source_control.py`
9. **Frontend Zod schema**: `api/schemas.ts`
10. **Frontend client method**: `api/source-control-client.ts`
11. **Frontend component**: `components/{git,github}/`
12. **Integration** in `GitBrowserPanel.tsx` (new tab) or elsewhere
13. **Documentation**: update this file

### 19.2 Adding a New Git Operation

Any operation that runs a `git` command belongs in `git/`:

```python
class GitSomeManager:
    def __init__(self, *, git=None, path_policy=None):
        self._git = git or get_default_git_executable()
        self._policy = path_policy or get_default_path_policy()

    async def some_operation(
        self, workspace_id: str, *, workspace_path: Union[str, Path]
    ) -> SomeResult:
        target = self._policy.resolve_within_root(workspace_path, must_exist=True)
        result = await self._git.run(
            ["some", "git", "subcommand"],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            raise CommandFailedError("...", details={"stderr": result.stderr[:500]})
        return _parse_output(result.stdout)
```

**Register a singleton:**

```python
_DEFAULT_MANAGER = None
_DEFAULT_LOCK = threading.RLock()

def get_default_some_manager() -> GitSomeManager:
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        if _DEFAULT_MANAGER is None:
            _DEFAULT_MANAGER = GitSomeManager()
        return _DEFAULT_MANAGER
```

### 19.3 Adding a New Tab to the Browse Panel

1. Extend `type Tab` union in `GitBrowserPanel.tsx`
2. Add `<TabButton>` in the header
3. Add `{tab === "newtab" && <YourComponent />}` in the content area
4. (If it should open in the detail view) wire `onResult`/`onSelect` callbacks

---

## 20. Known Limitations

### 20.1 Features Deferred (with Reason)

| Feature | Deferred because | Impact |
|---------|------------------|--------|
| **Blame view** | Not implemented in Phase 1-5 | Users must run `git blame` manually |
| **Stash** | Merge requires clean tree; stash is a workaround | Users must commit WIP |
| **Cherry-pick / revert** | Considered but not in MVP | Use CLI |
| **Push tags to remote** | Requires approval workflow | Tags are local-only |
| **Release asset upload** | Requires uploads.github.com flow | Users upload via GitHub UI |
| **Inline review comments UI** | Backend supports it; frontend uses general comments only | Reviewers comment on the whole PR |
| **Code search** | Depends on GitHub search API or a local index | Not implemented |
| **GitHub Actions UI** | Out of scope | Users visit GitHub directly |
| **Branch graph** in History | Considered; visual complexity | Linear list only |
| **Markdown rendering** | Avoided `react-markdown` dependency | PR/Issue bodies show as pre-formatted text |
| **File search in Repository Browser** | Requires client-side index or filter | Users scroll the tree |

### 20.2 Known Bugs / Limitations

- **`GET /repositories/{id:path}` is greedy** — any endpoint declared after it
  will be shadowed. All feature routes must be declared *before* it. (This is
  enforced by convention; add a test if you refactor.)
- **`routes: 20` observation** — if `len(app.routes)` prints ~20 instead of
  ~80, the router may not be mounted in `omweb/main.py`. Verify with
  `app.include_router(source_control.router)` in the main app.
- **Binary file detection** in `read_file_at_ref` returns `is_binary=False`
  always. `GitExecutable` decodes stdout as UTF-8, erasing NUL bytes. Binary
  content is silently replaced with `\ufffd` characters.
- **Markdown rendering** is plain text. If you need rich rendering, add
  `react-markdown` and change `<pre>` blocks to `<ReactMarkdown>`.
- **Rate limits** — 60 req/hr for unauthenticated GitHub. Authenticated: 5000/hr.
  Rate limit state is exposed via `GitHubClient.rate_limit` but not surfaced in
  the UI.

### 20.3 Compatibility

| Component | Tested with | Notes |
|-----------|-------------|-------|
| Git | 2.20+ | Older versions lack `--force-with-lease` and `worktree` |
| Python | 3.11+ | `str \| Path` requires 3.10+ |
| Node | 18+ | Vite 5 |
| Browser | Modern (Chrome/Firefox/Safari) | No IE support |

---

## 21. Roadmap

### Phase 6 — Advanced Git (est. 3-4 days)

| Feature | Effort | Value |
|---------|--------|-------|
| Blame view | 1 day | Medium |
| Stash list + apply + drop | 1 day | Medium |
| Cherry-pick | 0.5 day | Low |
| Revert | 0.5 day | Low |
| Push tags to remote | 1 day | High (currently missing) |

### Phase 7 — Polish (est. 2-3 days)

| Feature | Effort | Value |
|---------|--------|-------|
| Markdown rendering (PR/Issue bodies) | 0.5 day | High |
| Inline review comments UI | 1 day | Medium |
| Branch graph in History | 1 day | Medium |
| File search in Repository Browser | 0.5 day | Medium |
| Binary file preview | 0.5 day | Low |
| Rate limit indicator | 0.5 day | Low |

### Phase 8 — Multi-Provider (est. 3-4 days)

| Feature | Effort | Value |
|---------|--------|-------|
| GitLab adapter (full) | 2 days | High |
| Bitbucket adapter (full) | 2 days | Medium |
| Unified PR/Issue UI (provider-agnostic) | 1 day | High |

### Phase 9 — Advanced Remote Operations (est. 2-3 days)

| Feature | Effort | Value |
|---------|--------|-------|
| Release asset upload | 1 day | Medium |
| GitHub Actions UI (workflow runs) | 2 days | Low |
| Code search (GitHub Search API) | 1 day | Medium |

---

## 22. Glossary

| Term | Definition |
|------|------------|
| **Workspace** | An isolated local Git repository owned by PELDRUN, bound to one remote repo and one base commit. |
| **Operation** | A durable workflow (e.g. `push_branch`) tracked by the journal with a state machine and an audit log. |
| **Approval ticket** | A single-use, content-bound authorization for a remote write. Invalidated if the diff changes. |
| **Ref** | A Git reference: branch, tag, or commit SHA. |
| **Worktree** | A Git feature allowing multiple working directories for one repository. PELDRUN uses it for workspaces. |
| **Base SHA** | The commit the workspace was created from. Diffs are computed against it. |
| **Diff hash** | `SHA-256(canonical_json(file_list))`. Deterministic; used to bind approval tickets. |
| **Facade** | The orchestration layer; the only class that coordinates Git + providers + security + journal. |
| **Policy context** | The parameters passed to `PolicyEngine.require` (`operation`, `provider`, `target_branch`, ...). |
| **Provider** | A source-control service (GitHub, GitLab, Bitbucket). Adapters implement `SourceControlProvider`. |
| **Adapter** | A class implementing the provider protocols. One per provider. |

---

**End of Document**

For questions or contributions, see `CONTRIBUTING.md` (if it exists) or open an
issue in the project repository.
```

---



### 17.6 Health Checks

[...الكود الحالي...]

---

#### ⚠️ Note on FastAPI 0.143+ route enumeration

In **FastAPI 0.143.0** (pre-release), `app.include_router(...)` wraps each
included router in an internal `_IncludedRouter` object. This wrapper does NOT
expose a `.path` attribute and does NOT expose `.routes` as a public property.

**Consequence:** the following pattern returns misleading results:

```python
# ❌ This reports "0 routes" on FastAPI 0.143+ even though routing works fine
has_sc = any("source-control" in getattr(r, "path", "") for r in app.routes)
print(has_sc)  # False — WRONG
