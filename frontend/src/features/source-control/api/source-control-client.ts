// frontend/src/features/source-control/api/source-control-client.ts

import {
  ActionSuccessResponse,
  ApprovalTicketResponse,
  BranchResponse,
  CheckoutBranchRequest,
  CheckSummaryResponse,
  CommitEntryResponse,
  CommitWithStatsResponse,
  ConflictListResponse,
  ConnectionStatusResponse,
  ConnectBeginResponse,
  CreateChangeRequestPayload,
  CreateIssueCommentRequest,
  CreateIssueRequest,
  CreateOperationRequest,
  CreatePullRequestCommentRequest,
  CreatePullRequestRequest,
  CreateReleaseRequest,
  CreateTagRequest,
  DiffSummaryResponse,
  FileContentAtRefResponse,
  IssueCommentResponse,
  IssueListResponse,
  IssueResponse,
  MergeBranchesRequest,
  MergeOperationResponse,
  MergePullRequestRequest,
  MergePullRequestResponse,
  OperationResponse,
  ProviderDescriptorResponse,
  PullRequestCommentResponse,
  PullRequestListResponse,
  PullRequestResponse,
  PullRequestReviewResponse,
  PullRequestTimelineResponse,
  PushBranchResponse,
  RebaseBranchRequest,
  ReleaseListResponse,
  ReleaseResponse,
  RepositoryResponse,
  ResolveConflictsRequest,
  ResolveConflictsResponse,
  SubmitPullRequestReviewRequest,
  TagListResponse,
  TagResponse,
  TreeNodeResponse,
  UpdateIssueStateRequest,
  WorkspaceResponse,
} from "./schemas";

/**
 * Custom error class to normalize backend HTTP errors.
 * Maps FastAPI HTTPExceptions to structured codes for the UI to consume.
 */
export class SourceControlApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public detail: string
  ) {
    super(detail);
    this.name = "SourceControlApiError";
  }
}

const BASE_URL = "/api/source-control";

/**
 * Internal helper to handle fetch requests, validate JSON, and map backend errors.
 */
async function request<T>(
  endpoint: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${BASE_URL}${endpoint}`;

  const response = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...options.headers,
    },
  });

  if (response.ok) {
    if (response.status === 204) return {} as T;
    return response.json() as Promise<T>;
  }

  // Parse error payload safely
  let detail = "An unknown error occurred.";
  try {
    const errorData = await response.json();
    detail = errorData.detail || detail;
  } catch {
    // Ignore JSON parsing errors on error responses
  }

  // Map HTTP status codes to stable error codes matching backend error taxonomy
  let code = "UNKNOWN_ERROR";
  switch (response.status) {
    case 401: {
      code = "AUTH_REQUIRED";
      // Broadcast the auth failure so that any mounted UI
      // (specifically `<AuthReconnectBanner />`) can offer an
      // inline re-connect flow without forcing the user to
      // navigate to the Settings page.
      if (typeof window !== "undefined") {
        window.dispatchEvent(
          new CustomEvent("peldrun:auth-required", {
            detail: { message: detail, provider: "github" },
          })
        );
      }
      break;
    }
    case 403:
      code = "PERMISSION_DENIED";
      break;
    case 404:
      code = "NOT_FOUND";
      break;
    case 409:
      code = "CONFLICT"; // e.g. RemoteStateChangedError
      break;
    case 412:
      code = "REMOTE_STATE_CHANGED";
      break;
    case 422:
      code = "VALIDATION_ERROR"; // Pydantic validation failure
      break;
    case 429:
      code = "RATE_LIMITED";
      break;
    case 501:
      code = "UNSUPPORTED_CAPABILITY";
      break;
    case 502:
      code = "PROVIDER_ERROR";
      break;
  }

  throw new SourceControlApiError(response.status, code, detail);
}

/**
 * Generates the OAuth callback URI dynamically based on the current browser origin.
 * This ensures it works on localhost, custom domains, and any port without hardcoding.
 */
export function getDynamicRedirectUri(provider: string): string {
  if (typeof window === "undefined") return "";
  // The backend requires 127.0.0.1 instead of localhost for security
  const origin = window.location.origin.replace("localhost", "127.0.0.1");
  return `${origin}/api/source-control/oauth/callback/${provider}`;
}

export const sourceControlClient = {
  // ----------------------------------------------------------------------
  // 1. Providers & Connection Management
  // ----------------------------------------------------------------------

  async listProviders(): Promise<ProviderDescriptorResponse[]> {
    return request<ProviderDescriptorResponse[]>("/providers");
  },

  async listConnections(): Promise<ConnectionStatusResponse[]> {
    return request<ConnectionStatusResponse[]>("/connections");
  },

  /**
   * Saves the user's own OAuth App credentials (Client ID & Secret) to the backend vault.
   * NOTE: This endpoint needs to be implemented in the backend router.
   */
  async saveProviderConfig(
    provider: string,
    payload: { client_id: string; client_secret: string }
  ): Promise<ActionSuccessResponse> {
    return request<ActionSuccessResponse>(`/connections/${provider}/config`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async getProviderConfig(provider: string): Promise<{ client_id: string; is_configured: boolean }> {
    return request<{ client_id: string; is_configured: boolean }>(`/connections/${provider}/config`);
  },

  async deleteProviderConfig(provider: string): Promise<ActionSuccessResponse> {
    return request<ActionSuccessResponse>(`/connections/${provider}/config`, {
      method: "DELETE",
    });
  },

  async beginConnection(
    provider: string,
    payload: { redirect_uri?: string; scopes?: string[]; base_url?: string } = {}
  ): Promise<ConnectBeginResponse> {
    // Auto-inject the dynamic redirect URI if not explicitly provided
    const dynamicRedirect = payload.redirect_uri || getDynamicRedirectUri(provider);

    return request<ConnectBeginResponse>(`/connections/${provider}/begin`, {
      method: "POST",
      body: JSON.stringify({ provider, redirect_uri: dynamicRedirect, ...payload }),
    });
  },

  async validateConnection(
    provider: string,
    payload: { force_refresh?: boolean } = {}
  ): Promise<ConnectionStatusResponse> {
    return request<ConnectionStatusResponse>(`/connections/${provider}/validate`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async disconnectProvider(
    provider: string,
    payload: { revoke_upstream_token?: boolean } = { revoke_upstream_token: true }
  ): Promise<ActionSuccessResponse> {
    return request<ActionSuccessResponse>(`/connections/${provider}`, {
      method: "DELETE",
      body: JSON.stringify(payload),
    });
  },

  // ----------------------------------------------------------------------
  // 2. Repositories, Branches & Remote Inspection
  // ----------------------------------------------------------------------

  async listRepositories(
    provider?: string,
    query?: string,
    limit: number = 30
  ): Promise<RepositoryResponse[]> {
    const params = new URLSearchParams();
    if (provider) params.append("provider", provider);
    if (query) params.append("query", query);
    params.append("limit", limit.toString());

    return request<RepositoryResponse[]>(`/repositories?${params.toString()}`);
  },

  async getRepository(
    repositoryId: string,
    provider: string = "github"
  ): Promise<RepositoryResponse> {
    return request<RepositoryResponse>(
      `/repositories/${repositoryId}?provider=${provider}`
    );
  },

  async listBranches(
    repositoryId: string,
    provider: string = "github",
    limit: number = 50
  ): Promise<BranchResponse[]> {
    return request<BranchResponse[]>(
      `/repositories/${repositoryId}/branches?provider=${provider}&limit=${limit}`
    );
  },

  async createBranch(
    repositoryId: string,
    payload: { branch_name: string; from_sha?: string },
    provider: string = "github"
  ): Promise<BranchResponse> {
    return request<BranchResponse>(
      `/repositories/${repositoryId}/branches?provider=${provider}`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
  },

  // ----------------------------------------------------------------------
  // 3. Workspaces, Diffs & CI Checks
  // ----------------------------------------------------------------------

  async createWorkspace(payload: {
    repository_id: string;
    branch_name?: string;
    base_sha?: string;
    provider?: string;
    chat_id?: string; // Manual Mode: creates workspace inside chat's files dir
  }): Promise<WorkspaceResponse> {
    return request<WorkspaceResponse>("/workspaces", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async getWorkspace(workspaceId: string): Promise<WorkspaceResponse> {
    return request<WorkspaceResponse>(`/workspaces/${workspaceId}`);
  },

  /**
   * Delete a workspace from the registry and, optionally, its files
   * on disk.
   *
   * Manual Mode calls this when the user wants to change the
   * repository inside the same chat: `POST /workspaces` refuses to
   * create a second workspace in a chat folder that already contains
   * a `.git` directory, so the existing workspace must be deleted
   * first. Passing `force_delete_files: true` is required for the
   * follow-up `createWorkspace` call to succeed.
   *
   * The default is `false` on purpose: deleting files is
   * destructive and should be an explicit choice at the call site.
   */
  async deleteWorkspace(
    workspaceId: string,
    options: { forceDeleteFiles?: boolean } = {}
  ): Promise<ActionSuccessResponse> {
    const params = new URLSearchParams();
    if (options.forceDeleteFiles !== undefined) {
      params.append(
        "force_delete_files",
        String(options.forceDeleteFiles)
      );
    }
    const query = params.toString();
    const suffix = query ? `?${query}` : "";
    return request<ActionSuccessResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}${suffix}`,
      { method: "DELETE" }
    );
  },

  async getWorkspaceByChat(chatId: string): Promise<WorkspaceResponse | null> {
    return request<WorkspaceResponse | null>(
      `/workspaces/by-chat/${encodeURIComponent(chatId)}`
    );
  },

  async ensureEditableBranch(workspaceId: string): Promise<{ branch_name: string }> {
    return request<{ branch_name: string }>(
      `/workspaces/${encodeURIComponent(workspaceId)}/ensure-editable-branch`,
      { method: "POST" }
    );
  },

  async inspectWorkspaceDiff(
    workspaceId: string,
    baseRef?: string
  ): Promise<DiffSummaryResponse> {
    const query = baseRef ? `?base_ref=${encodeURIComponent(baseRef)}` : "";
    return request<DiffSummaryResponse>(`/workspaces/${workspaceId}/diff${query}`);
  },

  async runChecks(
    workspaceId: string,
    payload: { workspace_id?: string; ref?: string; provider?: string } = {}
  ): Promise<CheckSummaryResponse> {
    return request<CheckSummaryResponse>(`/workspaces/${workspaceId}/checks`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  // ----------------------------------------------------------------------
  // 4. Durable Operations, Approvals & Remote Execution
  // ----------------------------------------------------------------------

  async createOperation(payload: CreateOperationRequest): Promise<OperationResponse> {
    return request<OperationResponse>("/operations", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async getOperation(operationId: string): Promise<OperationResponse> {
    return request<OperationResponse>(`/operations/${operationId}`);
  },

  async requestOperationApproval(operationId: string): Promise<ApprovalTicketResponse> {
    return request<ApprovalTicketResponse>(`/operations/${operationId}/approval`, {
      method: "POST",
    });
  },

  async approveOperation(
    operationId: string,
    payload: { approval_id: string; user_id?: string; notes?: string }
  ): Promise<ApprovalTicketResponse> {
    return request<ApprovalTicketResponse>(`/operations/${operationId}/approve`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async rejectOperation(
    operationId: string,
    payload: { approval_id: string; user_id?: string; reason: string }
  ): Promise<ActionSuccessResponse> {
    return request<ActionSuccessResponse>(`/operations/${operationId}/reject`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async pushApprovedBranch(
    operationId: string,
    payload: { approval_id: string; workspace_id: string; remote_name?: string; set_upstream?: boolean }
  ): Promise<PushBranchResponse> {
    return request<PushBranchResponse>(`/operations/${operationId}/push`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async createChangeRequest(
    operationId: string,
    payload: CreateChangeRequestPayload
  ): Promise<ActionSuccessResponse> {
    return request<ActionSuccessResponse>(`/operations/${operationId}/change-request`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  // ----------------------------------------------------------------------
  // 5. Security Audit Log
  // ----------------------------------------------------------------------

  async listAuditEvents(
    limit: number = 100,
    operationId?: string
  ): Promise<ActionSuccessResponse[]> {
    const params = new URLSearchParams({ limit: limit.toString() });
    if (operationId) params.append("operation_id", operationId);
    return request<ActionSuccessResponse[]>(`/audit?${params.toString()}`);
  },

  // ----------------------------------------------------------------------
  // 6. Git Read-Only Operations (history, tree, historical file content)
  // ----------------------------------------------------------------------
  //
  // These methods are strictly read-only: they inspect the committed
  // history of a workspace and never mutate the working tree, the
  // index, or any branch. They are consumed by the Repository
  // Browser and Commit History views.

  /**
   * Return a page of commits for a workspace.
   *
   * Pagination is stable: two calls with non-overlapping `skip`
   * values return non-overlapping pages, provided the underlying
   * history is unchanged. An empty array is a valid result (empty
   * repository, or all commits filtered out).
   */
  async getLog(
    workspaceId: string,
    options: {
      limit?: number;
      skip?: number;
      ref?: string;
      path?: string;
      author?: string;
    } = {}
  ): Promise<CommitEntryResponse[]> {
    const params = new URLSearchParams();
    if (options.limit !== undefined) {
      params.append("limit", String(options.limit));
    }
    if (options.skip !== undefined) {
      params.append("skip", String(options.skip));
    }
    if (options.ref) params.append("ref", options.ref);
    if (options.path) params.append("path", options.path);
    if (options.author) params.append("author", options.author);

    const query = params.toString();
    const suffix = query ? `?${query}` : "";
    return request<CommitEntryResponse[]>(
      `/workspaces/${encodeURIComponent(workspaceId)}/log${suffix}`
    );
  },

  /**
   * Return a single commit with aggregate file-change statistics.
   */
  async getCommit(
    workspaceId: string,
    sha: string
  ): Promise<CommitWithStatsResponse> {
    return request<CommitWithStatsResponse>(
      `/workspaces/${encodeURIComponent(
        workspaceId
      )}/commits/${encodeURIComponent(sha)}`
    );
  },

  /**
   * Return the hierarchical file tree at a ref.
   *
   * The tree reflects COMMITTED content, not the working tree. Use
   * `inspectWorkspaceDiff` when the caller wants to browse the
   * working tree.
   */
  async getFileTree(
    workspaceId: string,
    ref?: string
  ): Promise<TreeNodeResponse[]> {
    const query = ref ? `?ref=${encodeURIComponent(ref)}` : "";
    return request<TreeNodeResponse[]>(
      `/workspaces/${encodeURIComponent(
        workspaceId
      )}/files/tree${query}`
    );
  },

  /**
   * Read a file at a specific committed ref.
   *
   * Unlike `inspectWorkspaceDiff`, which reads the working tree,
   * this method returns the COMMITTED content. It is the correct
   * API for browsing historical files in the Repository Browser.
   *
   * The `path` is URL-encoded as a single query-string value so
   * that filenames containing slashes, spaces, or non-ASCII
   * characters survive the round-trip without corruption.
   */
  async getFileContent(
    workspaceId: string,
    ref: string,
    path: string
  ): Promise<FileContentAtRefResponse> {
    const params = new URLSearchParams({
      ref,
      path,
    });
    return request<FileContentAtRefResponse>(
      `/workspaces/${encodeURIComponent(
        workspaceId
      )}/file-content?${params.toString()}`
    );
  },

  /**
   * Check out an existing local branch inside the workspace.
   *
   * This is a local-only operation: it does not touch the remote.
   * A non-forced checkout is refused by the backend when the
   * workspace has uncommitted changes, so user work is never
   * silently discarded.
   */
  async checkoutBranch(
    workspaceId: string,
    payload: CheckoutBranchRequest
  ): Promise<WorkspaceResponse> {
    return request<WorkspaceResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}/checkout`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
  },

  // ----------------------------------------------------------------------
  // 7. Pull Requests (GitHub-only in this phase)
  // ----------------------------------------------------------------------

  async listPullRequests(
    repositoryId: string,
    options: {
      state?: string;
      source_branch?: string;
      target_branch?: string;
      page?: number;
      page_size?: number;
      provider?: string;
    } = {}
  ): Promise<PullRequestListResponse> {
    const params = new URLSearchParams();
    if (options.state) params.append("state", options.state);
    if (options.source_branch)
      params.append("source_branch", options.source_branch);
    if (options.target_branch)
      params.append("target_branch", options.target_branch);
    if (options.page !== undefined)
      params.append("page", String(options.page));
    if (options.page_size !== undefined)
      params.append("page_size", String(options.page_size));
    if (options.provider) params.append("provider", options.provider);

    const query = params.toString();
    const suffix = query ? `?${query}` : "";
    return request<PullRequestListResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/pulls${suffix}`
    );
  },

  async getPullRequest(
    repositoryId: string,
    number: string,
    provider: string = "github"
  ): Promise<PullRequestResponse> {
    return request<PullRequestResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/pulls/${encodeURIComponent(number)}?provider=${encodeURIComponent(
        provider
      )}`
    );
  },

  async createPullRequest(
    repositoryId: string,
    payload: Omit<CreatePullRequestRequest, "repository_id"> & {
      repository_id?: string;
    }
  ): Promise<PullRequestResponse> {
    // The backend requires `repository_id` in BOTH the URL and the
    // body, and rejects a mismatch. The caller passes the id via
    // the URL argument; we fill the body from the same value so
    // the two are guaranteed to agree.
    const body: CreatePullRequestRequest = {
      ...payload,
      repository_id: repositoryId,
    };
    return request<PullRequestResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/pulls`,
      {
        method: "POST",
        body: JSON.stringify(body),
      }
    );
  },

  async mergePullRequest(
    repositoryId: string,
    number: string,
    payload: MergePullRequestRequest,
    provider: string = "github"
  ): Promise<MergePullRequestResponse> {
    return request<MergePullRequestResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/pulls/${encodeURIComponent(
        number
      )}/merge?provider=${encodeURIComponent(provider)}`,
      {
        method: "PUT",
        body: JSON.stringify(payload),
      }
    );
  },

  async submitPullRequestReview(
    repositoryId: string,
    number: string,
    payload: SubmitPullRequestReviewRequest,
    provider: string = "github"
  ): Promise<PullRequestReviewResponse> {
    return request<PullRequestReviewResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/pulls/${encodeURIComponent(
        number
      )}/reviews?provider=${encodeURIComponent(provider)}`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
  },

  async createPullRequestComment(
    repositoryId: string,
    number: string,
    payload: CreatePullRequestCommentRequest,
    provider: string = "github"
  ): Promise<PullRequestCommentResponse> {
    return request<PullRequestCommentResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/pulls/${encodeURIComponent(
        number
      )}/comments?provider=${encodeURIComponent(provider)}`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
  },

  async getPullRequestTimeline(
    repositoryId: string,
    number: string,
    provider: string = "github"
  ): Promise<PullRequestTimelineResponse> {
    return request<PullRequestTimelineResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/pulls/${encodeURIComponent(
        number
      )}/timeline?provider=${encodeURIComponent(provider)}`
    );
  },

  // ----------------------------------------------------------------------
  // 8. Issues (GitHub-only in this phase)
  // ----------------------------------------------------------------------

  async listIssues(
    repositoryId: string,
    options: {
      state?: string;
      labels?: string[];
      page?: number;
      page_size?: number;
      provider?: string;
    } = {}
  ): Promise<IssueListResponse> {
    const params = new URLSearchParams();
    if (options.state) params.append("state", options.state);
    if (options.labels && options.labels.length > 0) {
      // The backend expects a comma-separated string; the client
      // joins here so callers never have to know the wire format.
      params.append("labels", options.labels.join(","));
    }
    if (options.page !== undefined)
      params.append("page", String(options.page));
    if (options.page_size !== undefined)
      params.append("page_size", String(options.page_size));
    if (options.provider) params.append("provider", options.provider);

    const query = params.toString();
    const suffix = query ? `?${query}` : "";
    return request<IssueListResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/issues${suffix}`
    );
  },

  async getIssue(
    repositoryId: string,
    number: string,
    provider: string = "github"
  ): Promise<IssueResponse> {
    return request<IssueResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/issues/${encodeURIComponent(number)}?provider=${encodeURIComponent(
        provider
      )}`
    );
  },

  async createIssue(
    repositoryId: string,
    payload: Omit<CreateIssueRequest, "repository_id"> & {
      repository_id?: string;
    }
  ): Promise<IssueResponse> {
    // The backend requires `repository_id` in BOTH the URL and the
    // body, and rejects a mismatch. The caller passes the id via
    // the URL argument; we fill the body from the same value so
    // the two are guaranteed to agree.
    const body: CreateIssueRequest = {
      ...payload,
      repository_id: repositoryId,
    };
    return request<IssueResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/issues`,
      {
        method: "POST",
        body: JSON.stringify(body),
      }
    );
  },

  async updateIssueState(
    repositoryId: string,
    number: string,
    payload: UpdateIssueStateRequest,
    provider: string = "github"
  ): Promise<IssueResponse> {
    return request<IssueResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/issues/${encodeURIComponent(
        number
      )}?provider=${encodeURIComponent(provider)}`,
      {
        method: "PATCH",
        body: JSON.stringify(payload),
      }
    );
  },

  async listIssueComments(
    repositoryId: string,
    number: string,
    provider: string = "github"
  ): Promise<IssueCommentResponse[]> {
    return request<IssueCommentResponse[]>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/issues/${encodeURIComponent(
        number
      )}/comments?provider=${encodeURIComponent(provider)}`
    );
  },

  async createIssueComment(
    repositoryId: string,
    number: string,
    payload: CreateIssueCommentRequest,
    provider: string = "github"
  ): Promise<IssueCommentResponse> {
    return request<IssueCommentResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/issues/${encodeURIComponent(
        number
      )}/comments?provider=${encodeURIComponent(provider)}`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
  },

  // ----------------------------------------------------------------------
  // 9. Merge / Rebase / Conflict Resolution (local workspace)
  // ----------------------------------------------------------------------
  //
  // These methods drive LOCAL Git operations inside a workspace.
  // They never call a provider API and never touch a token.

  async mergeWorkspaceBranches(
    workspaceId: string,
    payload: MergeBranchesRequest
  ): Promise<MergeOperationResponse> {
    return request<MergeOperationResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}/merge`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
  },

  async rebaseWorkspaceBranch(
    workspaceId: string,
    payload: RebaseBranchRequest
  ): Promise<MergeOperationResponse> {
    return request<MergeOperationResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}/rebase`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
  },

  async finalizeWorkspaceOperation(
    workspaceId: string
  ): Promise<MergeOperationResponse> {
    return request<MergeOperationResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}/finalize`,
      { method: "POST" }
    );
  },

  async abortWorkspaceOperation(
    workspaceId: string
  ): Promise<ConflictListResponse> {
    return request<ConflictListResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}/abort`,
      { method: "POST" }
    );
  },

  async listWorkspaceConflicts(
    workspaceId: string
  ): Promise<ConflictListResponse> {
    return request<ConflictListResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}/conflicts`
    );
  },

  async resolveWorkspaceConflicts(
    workspaceId: string,
    payload: ResolveConflictsRequest
  ): Promise<ResolveConflictsResponse> {
    return request<ResolveConflictsResponse>(
      `/workspaces/${encodeURIComponent(
        workspaceId
      )}/conflicts/resolve`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
  },

  // ----------------------------------------------------------------------
  // 10. Local Git Tags
  // ----------------------------------------------------------------------

  async listWorkspaceTags(
    workspaceId: string,
    maxTags: number = 5000
  ): Promise<TagListResponse> {
    return request<TagListResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}/tags?max_tags=${maxTags}`
    );
  },

  async createWorkspaceTag(
    workspaceId: string,
    payload: CreateTagRequest
  ): Promise<TagResponse> {
    return request<TagResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}/tags`,
      {
        method: "POST",
        body: JSON.stringify(payload),
      }
    );
  },

  async deleteWorkspaceTag(
    workspaceId: string,
    name: string
  ): Promise<ActionSuccessResponse> {
    // `name` may contain slashes (e.g. `v1.0.0/rc1`); the backend
    // route uses `{name:path}`, so slashes are preserved. Using
    // `encodeURIComponent` would double-encode them and break the
    // round-trip. Encode per segment instead.
    const encoded = name
      .split("/")
      .map((segment) => encodeURIComponent(segment))
      .join("/");
    return request<ActionSuccessResponse>(
      `/workspaces/${encodeURIComponent(workspaceId)}/tags/${encoded}`,
      { method: "DELETE" }
    );
  },

  // ----------------------------------------------------------------------
  // 11. GitHub Releases
  // ----------------------------------------------------------------------

  async listReleases(
    repositoryId: string,
    options: {
      include_drafts?: boolean;
      page?: number;
      page_size?: number;
      provider?: string;
    } = {}
  ): Promise<ReleaseListResponse> {
    const params = new URLSearchParams();
    if (options.include_drafts !== undefined)
      params.append("include_drafts", String(options.include_drafts));
    if (options.page !== undefined)
      params.append("page", String(options.page));
    if (options.page_size !== undefined)
      params.append("page_size", String(options.page_size));
    if (options.provider) params.append("provider", options.provider);

    const query = params.toString();
    const suffix = query ? `?${query}` : "";
    return request<ReleaseListResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/releases${suffix}`
    );
  },

  async getRelease(
    repositoryId: string,
    releaseId: string,
    provider: string = "github"
  ): Promise<ReleaseResponse> {
    return request<ReleaseResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/releases/${encodeURIComponent(releaseId)}?provider=${encodeURIComponent(
        provider
      )}`
    );
  },

  async createRelease(
    repositoryId: string,
    payload: Omit<CreateReleaseRequest, "repository_id"> & {
      repository_id?: string;
    }
  ): Promise<ReleaseResponse> {
    // The backend requires `repository_id` in BOTH the URL and the
    // body and rejects a mismatch. Fill the body from the URL value
    // so the two are guaranteed to agree.
    const body: CreateReleaseRequest = {
      ...payload,
      repository_id: repositoryId,
    };
    return request<ReleaseResponse>(
      `/repositories/${encodeURIComponent(repositoryId)}/releases`,
      {
        method: "POST",
        body: JSON.stringify(body),
      }
    );
  },

  async deleteRelease(
    repositoryId: string,
    releaseId: string,
    provider: string = "github"
  ): Promise<ActionSuccessResponse> {
    return request<ActionSuccessResponse>(
      `/repositories/${encodeURIComponent(
        repositoryId
      )}/releases/${encodeURIComponent(releaseId)}?provider=${encodeURIComponent(
        provider
      )}`,
      { method: "DELETE" }
    );
  },
};
