// frontend/src/features/source-control/hooks/useRepositories.ts

import { useState, useCallback, useEffect } from "react";
import { sourceControlClient, SourceControlApiError } from "../api/source-control-client";
import { RepositoryResponse, BranchResponse } from "../api/schemas";

/**
 * Custom hook for fetching and managing repositories and their branches.
 * Provides state for the repository list, branch list, and current selections.
 */
export function useRepositories(initialProvider: string = "github") {
  // --- State ---
  const [repositories, setRepositories] = useState<RepositoryResponse[]>([]);
  const [branches, setBranches] = useState<BranchResponse[]>([]);

  const [selectedRepository, setSelectedRepository] = useState<RepositoryResponse | null>(null);
  const [selectedBranch, setSelectedBranch] = useState<BranchResponse | null>(null);

  const [isLoadingRepos, setIsLoadingRepos] = useState<boolean>(false);
  const [isLoadingBranches, setIsLoadingBranches] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // --- Actions ---

  /**
   * Fetches repositories accessible to the authenticated user.
   * Can be filtered by a search query.
   */
  const fetchRepositories = useCallback(async (query?: string, provider: string = initialProvider) => {
    setIsLoadingRepos(true);
    setError(null);
    try {
      const data = await sourceControlClient.listRepositories(provider, query);
      setRepositories(data);
    } catch (err) {
      if (err instanceof SourceControlApiError) {
        setError(err.detail);
      } else {
        setError("Failed to load repositories.");
      }
    } finally {
      setIsLoadingRepos(false);
    }
  }, [initialProvider]);

  /**
   * Fetches branches for a specific repository.
   */
  const fetchBranches = useCallback(async (repositoryId: string, provider: string = initialProvider) => {
    setIsLoadingBranches(true);
    setError(null);
    try {
      const data = await sourceControlClient.listBranches(repositoryId, provider);
      setBranches(data);

      // Auto-select the default branch if none is currently selected
      setSelectedBranch((current) => {
        if (!current) {
          return data.find((b) => b.is_default) || data[0] || null;
        }
        return current;
      });
    } catch (err) {
      if (err instanceof SourceControlApiError) {
        setError(err.detail);
      } else {
        setError("Failed to load branches.");
      }
    } finally {
      setIsLoadingBranches(false);
    }
  }, [initialProvider]);

  /**
   * Selects a repository and automatically fetches its branches.
   */
  const handleSelectRepository = useCallback((repo: RepositoryResponse) => {
    setSelectedRepository(repo);
    setSelectedBranch(null); // Reset branch selection when repo changes
    setBranches([]);
    fetchBranches(repo.repository_id, repo.provider);
  }, [fetchBranches]);

  /**
   * Selects a specific branch.
   */
  const handleSelectBranch = useCallback((branch: BranchResponse) => {
    setSelectedBranch(branch);
  }, []);

  /**
   * Resets the entire repository and branch selection state.
   */
  const clearSelection = useCallback(() => {
    setSelectedRepository(null);
    setSelectedBranch(null);
    setBranches([]);
    setError(null);
  }, []);

  // Fetch repositories on mount
  useEffect(() => {
    fetchRepositories();
  }, [fetchRepositories]);

  return {
    // Data
    repositories,
    branches,
    selectedRepository,
    selectedBranch,

    // Loading & Error States
    isLoadingRepos,
    isLoadingBranches,
    error,

    // Actions
    fetchRepositories,
    fetchBranches,
    selectRepository: handleSelectRepository,
    selectBranch: handleSelectBranch,
    clearSelection,
  };
}
