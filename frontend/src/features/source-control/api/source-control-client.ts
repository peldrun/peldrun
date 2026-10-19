// frontend/src/features/source-control/api/source-control-client.ts

import {
  ActionSuccessResponse,
  ApprovalTicketResponse,
  BranchResponse,
  CheckSummaryResponse,
  ConnectionStatusResponse,
  ConnectBeginResponse,
  CreateChangeRequestPayload,
  CreateOperationRequest,
  DiffSummaryResponse,
  OperationResponse,
  ProviderDescriptorResponse,
  PushBranchResponse,
  RepositoryResponse,
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
    case 401:
      code = "AUTH_REQUIRED";
      break;
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
  }): Promise<WorkspaceResponse> {
    return request<WorkspaceResponse>("/workspaces", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  async getWorkspace(workspaceId: string): Promise<WorkspaceResponse> {
    return request<WorkspaceResponse>(`/workspaces/${workspaceId}`);
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
};
