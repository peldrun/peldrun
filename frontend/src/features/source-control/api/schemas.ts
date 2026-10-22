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
// Git Read-Only Operations (history, tree, historical file content)
// ----------------------------------------------------------------------
//
// These schemas mirror the backend responses of the read-only Git
// endpoints used by the Repository Browser and Commit History views.
// They are deliberately kept separate from the workspace/diff
// schemas: those describe an editable working tree, while these
// describe the immutable committed history reachable from a ref.

export const CommitEntryResponseSchema = z.object({
  sha: z.string(),
  message: z.string().default(""),
  author_name: z.string().default(""),
  author_email: z.string().default(""),
  committed_at: z.string().datetime(),
  parent_shas: z.array(z.string()).default([]),
});

export const CommitWithStatsResponseSchema = CommitEntryResponseSchema.extend({
  files_changed: z.number().int().default(0),
  insertions: z.number().int().default(0),
  deletions: z.number().int().default(0),
});

/**
 * Recursive file-tree node.
 *
 * The explicit `z.ZodType<TreeNodeResponse>` annotation is required
 * because `z.lazy` produces a self-referential schema whose inferred
 * type cannot be determined without a forward declaration. The
 * subsequent type export at the bottom of this file is therefore
 * omitted for this schema; the type is declared inline here.
 */
export type TreeNodeResponse = {
  name: string;
  path: string;
  type: "file" | "directory";
  size?: number | null;
  children: TreeNodeResponse[];
};

export const TreeNodeResponseSchema: z.ZodType<TreeNodeResponse> = z.lazy(
  () =>
    z.object({
      name: z.string(),
      path: z.string(),
      type: z.enum(["file", "directory"]),
      size: z.number().int().nullable().optional(),
      children: z.array(TreeNodeResponseSchema).default([]),
    })
) as z.ZodType<TreeNodeResponse>;

export const FileContentAtRefResponseSchema = z.object({
  workspace_id: z.string(),
  ref: z.string(),
  path: z.string(),
  is_binary: z.boolean().default(false),
  size_bytes: z.number().int().default(0),
  is_truncated: z.boolean().default(false),
  content: z.string().nullable().optional(),
});

export const CheckoutBranchRequestSchema = z.object({
  branch_name: z.string().min(1).max(200),
  force: z.boolean().default(false),
});

// ----------------------------------------------------------------------
// Pull Requests
// ----------------------------------------------------------------------
//
// Mirror the backend Pydantic schemas in
// `backend/integrations/source_control/schemas/{requests,responses}.py`.

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

export const PullRequestListResponseSchema = z.object({
  items: z.array(PullRequestResponseSchema).default([]),
  total: z.number().int().default(0),
  state: z.string().default("open"),
});

export const PullRequestReviewResponseSchema = z.object({
  review_id: z.string(),
  state: z.string(),
  body: z.string().nullable().optional(),
  author_login: z.string().nullable().optional(),
  submitted_at: z.string().datetime().nullable().optional(),
});

export const PullRequestCommentResponseSchema = z.object({
  comment_id: z.string(),
  body: z.string(),
  author_login: z.string().nullable().optional(),
  created_at: z.string().datetime().nullable().optional(),
  updated_at: z.string().datetime().nullable().optional(),
  path: z.string().nullable().optional(),
  line: z.number().int().nullable().optional(),
  side: z.string().nullable().optional(),
  in_reply_to_id: z.string().nullable().optional(),
});

export const MergePullRequestResponseSchema = z.object({
  status: z.string().default("success"),
  merged: z.boolean().default(false),
  sha: z.string().nullable().optional(),
  message: z.string().default(""),
});

export const PullRequestTimelineResponseSchema = z.object({
  reviews: z.array(PullRequestReviewResponseSchema).default([]),
  general_comments: z.array(PullRequestCommentResponseSchema).default([]),
  inline_comments: z.array(PullRequestCommentResponseSchema).default([]),
});

// --- Request payloads ---

export const CreatePullRequestRequestSchema = z.object({
  repository_id: z.string(),
  source_branch: z.string().min(1).max(200),
  target_branch: z.string().min(1).max(200),
  title: z.string().min(1).max(256),
  body: z.string().max(65_536).optional().default(""),
  draft: z.boolean().default(true),
  provider: z.string().default("github"),
});

export const MergePullRequestRequestSchema = z.object({
  merge_method: z.enum(["merge", "squash", "rebase"]).default("merge"),
  commit_title: z.string().max(256).nullable().optional(),
  commit_message: z.string().max(65_536).nullable().optional(),
  delete_source_branch: z.boolean().default(false),
  expected_head_sha: z.string().max(64).nullable().optional(),
});

export const ReviewCommentPayloadSchema = z.object({
  path: z.string().min(1).max(1024),
  line: z.number().int().min(1),
  body: z.string().min(1).max(65_536),
  side: z.enum(["LEFT", "RIGHT"]).default("RIGHT"),
});

export const SubmitPullRequestReviewRequestSchema = z.object({
  event: z.enum(["APPROVE", "REQUEST_CHANGES", "COMMENT"]),
  body: z.string().max(65_536).nullable().optional(),
  comments: z.array(ReviewCommentPayloadSchema).default([]),
});

export const CreatePullRequestCommentRequestSchema = z.object({
  body: z.string().min(1).max(65_536),
  path: z.string().max(1024).nullable().optional(),
  line: z.number().int().min(1).nullable().optional(),
  side: z.enum(["LEFT", "RIGHT"]).nullable().optional(),
  commit_id: z.string().max(64).nullable().optional(),
});

// ----------------------------------------------------------------------
// Issues
// ----------------------------------------------------------------------
//
// Mirror the backend Pydantic schemas in
// `backend/integrations/source_control/schemas/{requests,responses}.py`.

export const IssueResponseSchema = z.object({
  provider: z.string(),
  repository_id: z.string(),
  number: z.string(),
  web_url: z.string(),
  state: z.string(),
  title: z.string().default(""),
  body: z.string().nullable().optional(),
  author_login: z.string().nullable().optional(),
  labels: z.array(z.string()).default([]),
  comments_count: z.number().int().default(0),
  is_pull_request: z.boolean().default(false),
  state_reason: z.string().nullable().optional(),
  created_at: z.string().datetime().nullable().optional(),
  updated_at: z.string().datetime().nullable().optional(),
  closed_at: z.string().datetime().nullable().optional(),
});

export const IssueListResponseSchema = z.object({
  items: z.array(IssueResponseSchema).default([]),
  total: z.number().int().default(0),
  state: z.string().default("open"),
});

export const IssueCommentResponseSchema = z.object({
  comment_id: z.string(),
  body: z.string(),
  author_login: z.string().nullable().optional(),
  created_at: z.string().datetime().nullable().optional(),
  updated_at: z.string().datetime().nullable().optional(),
});

// --- Request payloads ---

export const CreateIssueRequestSchema = z.object({
  repository_id: z.string(),
  title: z.string().min(1).max(256),
  body: z.string().max(65_536).optional().default(""),
  labels: z.array(z.string()).max(20).default([]),
  assignees: z.array(z.string()).max(10).default([]),
  provider: z.string().default("github"),
});

export const UpdateIssueStateRequestSchema = z.object({
  state: z.enum(["open", "closed"]),
  state_reason: z
    .enum(["completed", "not_planned", "reopened"])
    .nullable()
    .optional(),
});

export const CreateIssueCommentRequestSchema = z.object({
  body: z.string().min(1).max(65_536),
});

// ----------------------------------------------------------------------
// Merge / Rebase / Conflict Resolution
// ----------------------------------------------------------------------
//
// Mirror the backend Pydantic schemas in
// `backend/integrations/source_control/schemas/{requests,responses}.py`.
// These cover LOCAL workspace operations — no remote calls.

export const ConflictFileResponseSchema = z.object({
  path: z.string(),
  has_base: z.boolean().default(false),
  has_ours: z.boolean().default(false),
  has_theirs: z.boolean().default(false),
  is_delete_modify: z.boolean().default(false),
  is_add_add: z.boolean().default(false),
});

export const ConflictListResponseSchema = z.object({
  workspace_id: z.string(),
  operation_state: z.string(),
  files: z.array(ConflictFileResponseSchema).default([]),
  total: z.number().int().default(0),
});

export const MergeOperationResponseSchema = z.object({
  workspace_id: z.string(),
  kind: z.string(),
  operation_state: z.string(),
  conflicts: z.array(ConflictFileResponseSchema).default([]),
  head_sha: z.string().nullable().optional(),
  message: z.string().default(""),
});

export const ResolveConflictsResponseSchema = z.object({
  workspace_id: z.string(),
  operation_state: z.string(),
  resolved_count: z.number().int().default(0),
  remaining: z.array(ConflictFileResponseSchema).default([]),
  message: z.string().default(""),
});

// --- Request payloads ---

export const MergeBranchesRequestSchema = z.object({
  source_branch: z.string().min(1).max(200),
  target_branch: z.string().min(1).max(200),
  strategy: z.enum(["merge", "squash", "ff-only"]).default("merge"),
  commit_message: z.string().max(65_536).nullable().optional(),
});

export const RebaseBranchRequestSchema = z.object({
  branch: z.string().min(1).max(200),
  onto: z.string().min(1).max(200),
});

export const ResolveConflictsRequestSchema = z.object({
  strategy: z.enum(["ours", "theirs", "manual"]),
  paths: z.array(z.string()).default([]),
});

// ----------------------------------------------------------------------
// Tags (local) and Releases (GitHub-only)
// ----------------------------------------------------------------------
//
// TagResponse describes a LOCAL Git tag. ReleaseResponse describes
// a REMOTE GitHub release. The two are distinct: a tag is a local
// pointer, a release is a GitHub-hosted artifact that references a
// tag but is not the same thing.

export const TagResponseSchema = z.object({
  name: z.string(),
  sha: z.string(),
  is_annotated: z.boolean().default(false),
  message: z.string().nullable().optional(),
  tagger_name: z.string().nullable().optional(),
  tagger_email: z.string().nullable().optional(),
  tagged_at: z.string().datetime().nullable().optional(),
});

export const TagListResponseSchema = z.object({
  items: z.array(TagResponseSchema).default([]),
  total: z.number().int().default(0),
});

export const ReleaseResponseSchema = z.object({
  release_id: z.string(),
  tag_name: z.string(),
  name: z.string().default(""),
  body: z.string().nullable().optional(),
  html_url: z.string().default(""),
  is_draft: z.boolean().default(false),
  is_prerelease: z.boolean().default(false),
  author_login: z.string().nullable().optional(),
  target_commitish: z.string().nullable().optional(),
  created_at: z.string().datetime().nullable().optional(),
  published_at: z.string().datetime().nullable().optional(),
});

export const ReleaseListResponseSchema = z.object({
  items: z.array(ReleaseResponseSchema).default([]),
  total: z.number().int().default(0),
});

export const CreateTagRequestSchema = z.object({
  name: z.string().min(1).max(200),
  message: z.string().max(65_536).nullable().optional(),
  target_sha: z.string().max(64).nullable().optional(),
  force: z.boolean().default(false),
});

export const CreateReleaseRequestSchema = z.object({
  repository_id: z.string(),
  tag_name: z.string().min(1).max(200),
  name: z.string().max(256).default(""),
  body: z.string().max(125_000).optional().default(""),
  target_commitish: z.string().max(200).default("main"),
  draft: z.boolean().default(false),
  prerelease: z.boolean().default(false),
  provider: z.string().default("github"),
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

// Git read-only operations
export type CommitEntryResponse = z.infer<typeof CommitEntryResponseSchema>;
export type CommitWithStatsResponse = z.infer<typeof CommitWithStatsResponseSchema>;
// TreeNodeResponse is declared explicitly above (recursive schema),
// so no `z.infer` line is emitted for it here.
export type FileContentAtRefResponse = z.infer<typeof FileContentAtRefResponseSchema>;
export type CheckoutBranchRequest = z.infer<typeof CheckoutBranchRequestSchema>;

// Pull requests
export type PullRequestResponse = z.infer<typeof PullRequestResponseSchema>;
export type PullRequestListResponse = z.infer<typeof PullRequestListResponseSchema>;
export type PullRequestReviewResponse = z.infer<typeof PullRequestReviewResponseSchema>;
export type PullRequestCommentResponse = z.infer<typeof PullRequestCommentResponseSchema>;
export type MergePullRequestResponse = z.infer<typeof MergePullRequestResponseSchema>;
export type PullRequestTimelineResponse = z.infer<typeof PullRequestTimelineResponseSchema>;

export type CreatePullRequestRequest = z.infer<typeof CreatePullRequestRequestSchema>;
export type MergePullRequestRequest = z.infer<typeof MergePullRequestRequestSchema>;
export type ReviewCommentPayload = z.infer<typeof ReviewCommentPayloadSchema>;
export type SubmitPullRequestReviewRequest = z.infer<typeof SubmitPullRequestReviewRequestSchema>;
export type CreatePullRequestCommentRequest = z.infer<typeof CreatePullRequestCommentRequestSchema>;

// Issues
export type IssueResponse = z.infer<typeof IssueResponseSchema>;
export type IssueListResponse = z.infer<typeof IssueListResponseSchema>;
export type IssueCommentResponse = z.infer<typeof IssueCommentResponseSchema>;

export type CreateIssueRequest = z.infer<typeof CreateIssueRequestSchema>;
export type UpdateIssueStateRequest = z.infer<typeof UpdateIssueStateRequestSchema>;
export type CreateIssueCommentRequest = z.infer<typeof CreateIssueCommentRequestSchema>;

// Merge / Rebase / Conflicts
export type ConflictFileResponse = z.infer<typeof ConflictFileResponseSchema>;
export type ConflictListResponse = z.infer<typeof ConflictListResponseSchema>;
export type MergeOperationResponse = z.infer<typeof MergeOperationResponseSchema>;
export type ResolveConflictsResponse = z.infer<typeof ResolveConflictsResponseSchema>;

export type MergeBranchesRequest = z.infer<typeof MergeBranchesRequestSchema>;
export type RebaseBranchRequest = z.infer<typeof RebaseBranchRequestSchema>;
export type ResolveConflictsRequest = z.infer<typeof ResolveConflictsRequestSchema>;

// Tags / Releases
export type TagResponse = z.infer<typeof TagResponseSchema>;
export type TagListResponse = z.infer<typeof TagListResponseSchema>;
export type ReleaseResponse = z.infer<typeof ReleaseResponseSchema>;
export type ReleaseListResponse = z.infer<typeof ReleaseListResponseSchema>;
export type CreateTagRequest = z.infer<typeof CreateTagRequestSchema>;
export type CreateReleaseRequest = z.infer<typeof CreateReleaseRequestSchema>;
