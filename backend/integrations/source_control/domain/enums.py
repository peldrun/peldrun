from enum import Enum


class ProviderId(str, Enum):
    """Supported source control provider identifiers."""

    GITHUB = "github"
    GITLAB = "gitlab"
    BITBUCKET = "bitbucket"


class ChangeRequestState(str, Enum):
    """Normalized lifecycle state of a Pull Request or Merge Request."""

    OPEN = "open"
    DRAFT = "draft"
    CLOSED = "closed"
    MERGED = "merged"


class OperationKind(str, Enum):
    """Granular classifications of local and remote source control operations."""

    LIST_REPOSITORIES = "list_repositories"
    READ_METADATA = "read_metadata"
    READ_FILE = "read_file"
    LIST_BRANCHES = "list_branches"
    CREATE_WORKSPACE = "create_workspace"
    CREATE_BRANCH = "create_branch"
    INSPECT_DIFF = "inspect_diff"
    RUN_CHECKS = "run_checks"
    REQUEST_APPROVAL = "request_approval"
    PUSH_BRANCH = "push_branch"
    CREATE_CHANGE_REQUEST = "create_change_request"
    MERGE_CHANGE_REQUEST = "merge_change_request"
    DELETE_BRANCH = "delete_branch"


class OperationStatus(str, Enum):
    """State machine states for a durable source control change operation."""

    CREATED = "created"
    REPOSITORY_RESOLVED = "repository_resolved"
    WORKSPACE_READY = "workspace_ready"
    EDITING = "editing"
    CHECKING = "checking"
    REVIEW_REQUIRED = "review_required"
    APPROVED = "approved"
    PUSHING = "pushing"
    PUSHED = "pushed"
    CHANGE_REQUEST_CREATED = "change_request_created"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    STALE_REVIEW_REQUIRED = "stale_review_required"


class ApprovalDecision(str, Enum):
    """Human approval lifecycle decision states."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class CheckStatus(str, Enum):
    """Execution status for CI tests and local verification checks."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILURE = "failure"
    NEUTRAL = "neutral"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class FileChangeType(str, Enum):
    """Type of modification applied to a file in a diff."""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class SourceControlErrorCode(str, Enum):
    """Standardized error codes normalized across all providers."""

    AUTH_REQUIRED = "auth_required"
    AUTH_EXPIRED = "auth_expired"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    NETWORK_ERROR = "network_error"
    CONFLICT = "conflict"
    VALIDATION_ERROR = "validation_error"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    REMOTE_STATE_CHANGED = "remote_state_changed"
    PROVIDER_ERROR = "provider_error"
    WORKSPACE_DIRTY = "workspace_dirty"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_INVALID = "approval_invalid"
    PATH_TRAVERSAL_DETECTED = "path_traversal_detected"
    COMMAND_TIMEOUT = "command_timeout"
