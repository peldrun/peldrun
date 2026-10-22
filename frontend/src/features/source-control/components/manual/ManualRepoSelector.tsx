// frontend/src/features/source-control/components/manual/ManualRepoSelector.tsx

import React, { useEffect, useState } from "react";
import { useManualModeStore } from "../../state/manual-mode-store";
import {
  sourceControlClient,
  SourceControlApiError,
} from "../../api/source-control-client";
import { BranchResponse, RepositoryResponse } from "../../api/schemas";
import { AlertCircle, ArrowRight, RefreshCw } from "lucide-react";

/**
 * Step 1 of Manual Mode: select provider, repository, and base branch.
 *
 * This component is intentionally self-contained — it uses
 * `sourceControlClient` directly instead of an external hook. Two
 * reasons:
 *
 *   1. Manual Mode should not depend on the Agent Mode's data hooks.
 *      They may change signatures independently.
 *   2. The component owns its loading/error state, so failures here
 *      are locally visible and do not leak into other steps.
 *
 * On "Continue", it writes the selection to the store and advances
 * the step to "creating-workspace". The actual workspace creation is
 * the responsibility of ManualWorkspaceCreator (step 2).
 */
export function ManualRepoSelector() {
  const {
    provider,
    repository,
    branch,
    setProvider,
    setRepository,
    setBranch,
    setStep,
  } = useManualModeStore();

  // Local UI state (not persisted across steps — intentionally).
  const [repositories, setRepositories] = useState<RepositoryResponse[]>([]);
  const [branches, setBranches] = useState<BranchResponse[]>([]);
  const [isLoadingRepos, setIsLoadingRepos] = useState(false);
  const [isLoadingBranches, setIsLoadingBranches] = useState(false);
  const [error, setLocalError] = useState<string | null>(null);

  // ---- Load repositories once (or when provider changes) ----
  useEffect(() => {
    let cancelled = false;

    async function load() {
      setIsLoadingRepos(true);
      setLocalError(null);
      try {
        const repos = await sourceControlClient.listRepositories(
          provider,
          undefined,
          50
        );
        if (!cancelled) setRepositories(repos);
      } catch (err) {
        if (cancelled) return;
        const msg =
          err instanceof SourceControlApiError
            ? err.detail
            : "Failed to load repositories.";
        setLocalError(msg);
      } finally {
        if (!cancelled) setIsLoadingRepos(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [provider]);

  // ---- Load branches when repository changes ----
  useEffect(() => {
    if (!repository) {
      setBranches([]);
      return;
    }

    let cancelled = false;

    async function load() {
      setIsLoadingBranches(true);
      setLocalError(null);
      try {
        const brs = await sourceControlClient.listBranches(
          repository!.repository_id,
          provider,
          50
        );
        if (cancelled) return;
        setBranches(brs);
        // If the currently-selected branch is not in the list,
        // snap to the repository's default branch.
        const stillValid = brs.some((b) => b.name === branch);
        if (!stillValid) {
          const fallback =
            brs.find((b) => b.is_default)?.name ||
            repository!.default_branch ||
            "main";
          setBranch(fallback);
        }
      } catch (err) {
        if (cancelled) return;
        const msg =
          err instanceof SourceControlApiError
            ? err.detail
            : "Failed to load branches.";
        setLocalError(msg);
      } finally {
        if (!cancelled) setIsLoadingBranches(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repository?.repository_id, provider]);

  // ---- Handlers ----
  const handleRepoChange = (repoId: string) => {
    const repo = repositories.find((r) => r.repository_id === repoId) ?? null;
    setRepository(repo);
  };

  const handleContinue = () => {
    if (!repository || !branch) return;
    setStep("creating-workspace");
  };

  const canContinue =
    !!repository && !!branch && !isLoadingRepos && !isLoadingBranches;

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">
          Select Repository
        </h3>
        <p className="text-xs text-muted-foreground mb-4">
          Choose the repository you want to work on. It will be cloned into
          your current chat's files folder.
        </p>
      </div>

      {/* Local error (repo/branch load failures) */}
      {error && (
        <div className="flex items-start rounded-md bg-red-50 p-3 border border-red-200">
          <AlertCircle className="h-4 w-4 text-red-600 mr-2 flex-shrink-0 mt-0.5" />
          <p className="text-xs text-red-700 flex-1">{error}</p>
        </div>
      )}

      {/* Provider */}
      <div>
        <label className="block text-xs font-medium text-gray-700 mb-1">
          Provider
        </label>
        <select
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          className="w-full rounded-md border border-border px-3 py-2 text-sm bg-background"
        >
          <option value="github">GitHub</option>
        </select>
      </div>

      {/* Repository */}
      <div>
        <div className="flex items-center justify-between mb-1">
          <label className="block text-xs font-medium text-gray-700">
            Repository
          </label>
          {isLoadingRepos && (
            <RefreshCw size={12} className="animate-spin text-muted-foreground" />
          )}
        </div>
        <select
          value={repository?.repository_id ?? ""}
          onChange={(e) => handleRepoChange(e.target.value)}
          disabled={isLoadingRepos}
          className="w-full rounded-md border border-border px-3 py-2 text-sm bg-background disabled:opacity-50"
        >
          <option value="">
            {isLoadingRepos ? "Loading repositories…" : "-- Select a repository --"}
          </option>
          {repositories.map((repo) => (
            <option key={repo.repository_id} value={repo.repository_id}>
              {repo.full_name}
              {repo.is_private ? " (private)" : ""}
            </option>
          ))}
        </select>
      </div>

      {/* Branch */}
      <div>
        <div className="flex items-center justify-between mb-1">
          <label className="block text-xs font-medium text-gray-700">
            Base Branch
          </label>
          {isLoadingBranches && (
            <RefreshCw size={12} className="animate-spin text-muted-foreground" />
          )}
        </div>
        <select
          value={branch}
          onChange={(e) => setBranch(e.target.value)}
          disabled={!repository || isLoadingBranches}
          className="w-full rounded-md border border-border px-3 py-2 text-sm bg-background disabled:opacity-50"
        >
          {branches.length === 0 && (
            <option value={branch}>
              {isLoadingBranches ? "Loading branches…" : branch}
            </option>
          )}
          {branches.map((b) => (
            <option key={b.name} value={b.name}>
              {b.name}
              {b.is_default ? " (default)" : ""}
              {b.is_protected ? " 🔒" : ""}
            </option>
          ))}
        </select>
        {branch && branches.find((b) => b.name === branch)?.is_protected && (
          <p className="mt-1 text-[11px] text-yellow-700">
            ⚠️ This branch is protected. Pushing directly may be denied by
            policy.
          </p>
        )}
      </div>

      {/* Continue */}
      <button
        onClick={handleContinue}
        disabled={!canContinue}
        className="w-full flex items-center justify-center gap-2 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        Continue to Create Workspace
        <ArrowRight size={16} />
      </button>
    </div>
  );
}
