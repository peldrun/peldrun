// frontend/src/features/source-control/api/schemas.ts

import { z } from "zod";

/**
 * Zod schemas mirroring the backend Pydantic models.
 * Updated to match `backend/integrations/source_control/schemas/` exactly.
 */

// ----------------------------------------------------------------------
// Enums & Shared Types
// ----------------------------------------------------------------------

export const OperationStateSchema = z.enum([
  "created",
  "repository_resolved",
  "workspace_ready",
  "editing",
  "checking",
  "review_required",
  "approved",
  "pushing",
  "pushed",
  "change_request_created",
  "cancelled",
  "failed_final",
  "rejected",
  "stale_review_required",
]);

export const FileChangeTypeSchema = z.enum([
  "added",
  "modified",
  "deleted",
  "renamed",
  "copied",
  "type_changed",
  "unmerged",
  "unknown",
]);

// ----------------------------------------------------------------------
// Domain Models & Responses
// ----------------------------------------------------------------------

export const OperationEventResponseSchema = z.object({
  event_id: z.string(),
  from_state: z.string(),
  to_state: z.string(),
  timestamp: z.string().datetime(),
  metadata: z.record(z.string(), z.any()).default({}),
});

export const FileDiffResponseSchema = z.object({
  path: z.string(),
  change_type: z.string(),
  insertions: z.number().int().default(0),
  deletions: z.number().int().default(0),
  old_path: z.string().nullable().optional(),
  is_binary: z.boolean().default(false),
  is_workflow_file: z.boolean().default(false),
});

export const DiffSummaryResponseSchema = z.object({
  base_sha: z.string(),
  head_sha: z.string(),
  diff_sha256: z.string(),
  total_files: z.number().int().default(0),
  insertions: z.number().int().default(0),
  deletions: z.number().int().default(0),
  touches_workflow_files: z.boolean().default(false),
  workflow_files: z.array(z.string()).default([]),
  has_binary_files: z.boolean().default(false),
  files: z.array(FileDiffResponseSchema).default([]),
  diff_sample: z.string().nullable().optional(),
  is_truncated: z.boolean().default(false),
});

export const ApprovalTicketResponseSchema = z.object({
  approval_id: z.string(),
  operation_id: z.string(),
  operation_type: z.string(),
  repository_id: z.string(),
  target_branch: z.string(),
  base_sha: z.string(),
  diff_sha256: z.string(),
  total_files: z.number().int().default(0),
  touches_workflow_files: z.boolean().default(false),
  is_protected_target: z.boolean().default(false),
  approved_by_user: z.boolean().default(false),
  expires_at: z.string().datetime(),
  created_at: z.string().datetime(),
});

export const RepositoryResponseSchema = z.object({
  provider: z.string(),
  repository_id: z.string(),
  namespace: z.string(),
  name: z.string(),
  full_name: z.string(),
  default_branch: z.string().default("main"),
  web_url: z.string(),
  clone_url: z.string(),
  is_private: z.boolean().default(false),
  description: z.string().nullable().optional(),
  metadata: z.record(z.string(), z.any()).default({}),
});

export const BranchResponseSchema = z.object({
  name: z.string(),
  head_sha: z.string().nullable().optional(),
  is_default: z.boolean().default(false),
  is_protected: z.boolean().default(false),
});

export const ConnectionStatusResponseSchema = z.object({
  provider: z.string(),
  is_connected: z.boolean(),
  account_id: z.string().nullable().optional(),
  username: z.string().nullable().optional(),
  base_url: z.string().default("https://github.com"),
  granted_scopes: z.array(z.string()).default([]),
  last_validated_at: z.string().datetime().nullable().optional(),
});

export const ProviderCapabilitiesResponseSchema = z.object({
  list_repositories: z.boolean().default(true),
  read_files: z.boolean().default(true),
  create_branch: z.boolean().default(true),
  push_branch: z.boolean().default(true),
  create_change_request: z.boolean().default(true),
  list_checks: z.boolean().default(false),
  comments: z.boolean().default(false),
  merge_change_request: z.boolean().default(false),
  self_managed_instance: z.boolean().default(false),
});

export const ProviderDescriptorResponseSchema = z.object({
  provider_id: z.string(),
  display_name: z.string(),
  is_connected: z.boolean(),
  is_configured: z.boolean().default(false), // Added to track if OAuth keys are set
  capabilities: ProviderCapabilitiesResponseSchema,
  web_base_url: z.string(),
  api_base_url: z.string(),
});

export const ConnectBeginResponseSchema = z.object({
  provider: z.string(),
  authorization_url: z.string(),
  state: z.string(),
  expires_in_seconds: z.number().int().default(600),
});

// ----------------------------------------------------------------------
// Operation & Workspace Responses
// ----------------------------------------------------------------------

export const OperationResponseSchema = z.object({
  operation_id: z.string(),
  operation_type: z.string(),
  provider: z.string(),
  repository_id: z.string(),
  target_branch: z.string(),
  current_state: OperationStateSchema,
  workspace_id: z.string().nullable().optional(),
  approval_id: z.string().nullable().optional(),
  change_request_url: z.string().nullable().optional(),
  error_code: z.string().nullable().optional(),
  error_message: z.string().nullable().optional(),
  created_at: z.string().datetime(),
  updated_at: z.string().datetime(),
  events: z.array(OperationEventResponseSchema).default([]),
});

export const WorkspaceResponseSchema = z.object({
  workspace_id: z.string(),
  repository_id: z.string(),
  current_branch: z.string(),
  base_sha: z.string(),
  head_sha: z.string().nullable().optional(),
  is_dirty: z.boolean().default(false),
  created_at: z.string().datetime(),
});

export const CheckRunResponseSchema = z.object({
  name: z.string(),
  status: z.string(),
  conclusion: z.string().nullable().optional(),
  web_url: z.string().nullable().optional(),
  started_at: z.string().datetime().nullable().optional(),
  completed_at: z.string().datetime().nullable().optional(),
});

export const CheckSummaryResponseSchema = z.object({
  ref: z.string(),
  overall_state: z.string(),
  is_passing: z.boolean().default(false),
  total_count: z.number().int().default(0),
  success_count: z.number().int().default(0),
  failure_count: z.number().int().default(0),
  pending_count: z.number().int().default(0),
  message: z.string(),
  runs: z.array(CheckRunResponseSchema).default([]),
});

export const PushBranchResponseSchema = z.object({
  status: z.string().default("success"),
  approval_id: z.string(),
  operation_id: z.string(),
  branch_name: z.string(),
  pushed_sha: z.string(),
  remote_name: z.string().default("origin"),
  upstream_ref: z.string().nullable().optional(),
  message: z.string(),
});

export const ActionSuccessResponseSchema = z.object({
  status: z.string().default("success"),
  message: z.string(),
});

// ----------------------------------------------------------------------
// Request Payloads
// ----------------------------------------------------------------------

export const CreateOperationRequestSchema = z.object({
  operation_type: z.string(),
  repository_id: z.string(),
  target_branch: z.string(),
  workspace_id: z.string().optional(),
  title: z.string().optional(),
  description: z.string().optional(),
  provider: z.string().default("github"),
});

export const ApproveOperationRequestSchema = z.object({
  approval_id: z.string(),
  user_id: z.string().default("user"),
  notes: z.string().optional(),
});

export const RejectOperationRequestSchema = z.object({
  approval_id: z.string(),
  user_id: z.string().default("user"),
  reason: z.string(),
});

export const PushBranchRequestSchema = z.object({
  approval_id: z.string(),
  workspace_id: z.string(),
  remote_name: z.string().default("origin"),
  set_upstream: z.boolean().default(true),
});

export const CreateChangeRequestPayloadSchema = z.object({
  repository_id: z.string(),
  source_branch: z.string(),
  target_branch: z.string(),
  title: z.string(),
  body: z.string().optional(),
  draft: z.boolean().default(true),
  approval_id: z.string().optional(),
  provider: z.string().default("github"),
});

export const SaveProviderConfigRequestSchema = z.object({
  client_id: z.string(),
  client_secret: z.string(),
});

// ----------------------------------------------------------------------
// Inferred TypeScript Types
// ----------------------------------------------------------------------

export type OperationState = z.infer<typeof OperationStateSchema>;
export type OperationEventResponse = z.infer<typeof OperationEventResponseSchema>;
export type FileDiffResponse = z.infer<typeof FileDiffResponseSchema>;
export type DiffSummaryResponse = z.infer<typeof DiffSummaryResponseSchema>;
export type ApprovalTicketResponse = z.infer<typeof ApprovalTicketResponseSchema>;
export type RepositoryResponse = z.infer<typeof RepositoryResponseSchema>;
export type BranchResponse = z.infer<typeof BranchResponseSchema>;
export type ConnectionStatusResponse = z.infer<typeof ConnectionStatusResponseSchema>;
export type ProviderCapabilitiesResponse = z.infer<typeof ProviderCapabilitiesResponseSchema>;
export type ProviderDescriptorResponse = z.infer<typeof ProviderDescriptorResponseSchema>;
export type ConnectBeginResponse = z.infer<typeof ConnectBeginResponseSchema>;
export type OperationResponse = z.infer<typeof OperationResponseSchema>;
export type WorkspaceResponse = z.infer<typeof WorkspaceResponseSchema>;
export type CheckRunResponse = z.infer<typeof CheckRunResponseSchema>;
export type CheckSummaryResponse = z.infer<typeof CheckSummaryResponseSchema>;
export type PushBranchResponse = z.infer<typeof PushBranchResponseSchema>;
export type ActionSuccessResponse = z.infer<typeof ActionSuccessResponseSchema>;

export type CreateOperationRequest = z.infer<typeof CreateOperationRequestSchema>;
export type ApproveOperationRequest = z.infer<typeof ApproveOperationRequestSchema>;
export type RejectOperationRequest = z.infer<typeof RejectOperationRequestSchema>;
export type PushBranchRequest = z.infer<typeof PushBranchRequestSchema>;
export type CreateChangeRequestPayload = z.infer<typeof CreateChangeRequestPayloadSchema>;
export type SaveProviderConfigRequest = z.infer<typeof SaveProviderConfigRequestSchema>;
