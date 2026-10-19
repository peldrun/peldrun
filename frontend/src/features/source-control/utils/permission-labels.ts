// frontend/src/features/source-control/utils/permission-labels.ts

/**
 * Utility functions and dictionaries to translate technical provider
 * capabilities and OAuth scopes into human-readable labels.
 * Essential for non-technical users to understand what the agent can do.
 */

import { ProviderCapabilitiesResponse } from "../api/schemas";

// ----------------------------------------------------------------------
// Provider Capabilities Translations
// ----------------------------------------------------------------------

type CapabilityKey = keyof ProviderCapabilitiesResponse;

interface CapabilityInfo {
  label: string;
  description: string;
  isHighRisk: boolean;
}

const CAPABILITY_MAP: Record<CapabilityKey, CapabilityInfo> = {
  list_repositories: {
    label: "Discover Repositories",
    description: "View and list repositories you have access to.",
    isHighRisk: false,
  },
  read_files: {
    label: "Read Repository Files",
    description: "Read the contents of files in selected repositories.",
    isHighRisk: false,
  },
  create_branch: {
    label: "Create Branches",
    description: "Create new local or remote branches.",
    isHighRisk: false,
  },
  push_branch: {
    label: "Push Changes",
    description: "Push approved commits to remote branches.",
    isHighRisk: false,
  },
  create_change_request: {
    label: "Open Pull Requests",
    description: "Create Pull Requests or Merge Requests for review.",
    isHighRisk: false,
  },
  list_checks: {
    label: "View CI/CD Checks",
    description: "View the status of automated tests and builds.",
    isHighRisk: false,
  },
  comments: {
    label: "Post Comments",
    description: "Leave comments on pull requests or issues.",
    isHighRisk: false,
  },
  merge_change_request: {
    label: "Merge Pull Requests",
    description: "Merge approved changes into the main branch. (Requires explicit approval)",
    isHighRisk: true,
  },
  self_managed_instance: {
    label: "Self-Managed Instance",
    description: "Connect to a custom, self-hosted Git server.",
    isHighRisk: false,
  },
};

/**
 * Returns user-friendly label and description for a technical capability.
 */
export function getCapabilityInfo(capability: CapabilityKey): CapabilityInfo {
  return CAPABILITY_MAP[capability] || {
    label: "Unknown Capability",
    description: "This capability is not recognized.",
    isHighRisk: true, // Default to high risk for unknown capabilities
  };
}

// ----------------------------------------------------------------------
// OAuth Scopes Translations
// ----------------------------------------------------------------------

interface ScopeInfo {
  label: string;
  isHighRisk: boolean;
}

/**
 * Common OAuth scopes across GitHub and GitLab.
 * Note: GitLab scopes differ slightly from GitHub, but the core concepts overlap.
 */
const SCOPE_MAP: Record<string, ScopeInfo> = {
  // GitHub Scopes
  "repo": { label: "Full control of private repositories", isHighRisk: true },
  "public_repo": { label: "Access to public repositories", isHighRisk: false },
  "read:user": { label: "Read your basic profile info", isHighRisk: false },
  "user:email": { label: "Read your email addresses", isHighRisk: false },
  "workflow": { label: "Update GitHub Action workflows", isHighRisk: true },
  "admin:repo_hook": { label: "Manage repository webhooks", isHighRisk: true },

  // GitLab Scopes
  "api": { label: "Full API access", isHighRisk: true },
  "read_api": { label: "Read-only API access", isHighRisk: false },
  "read_repository": { label: "Read repository data", isHighRisk: false },
  "write_repository": { label: "Write to repository data", isHighRisk: true },
};

/**
 * Returns a human-readable label for a given OAuth scope string.
 */
export function getScopeLabel(scope: string): ScopeInfo {
  return SCOPE_MAP[scope.toLowerCase()] || {
    label: `Custom scope: ${scope}`,
    isHighRisk: true, // Default to true for unknown scopes
  };
}

/**
 * Formats a list of granted scopes into a single readable sentence.
 */
export function formatGrantedScopes(scopes: string[]): string {
  if (!scopes || scopes.length === 0) {
    return "No specific permissions granted.";
  }
  return scopes.map((s) => getScopeLabel(s).label).join(", ");
}
