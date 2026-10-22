// frontend/src/features/source-control/components/github/PullRequestPanel.tsx

import React, { useState } from "react";
import { PullRequestList } from "./PullRequestList";
import { CreatePullRequest } from "./CreatePullRequest";
import { PullRequestDetail } from "./PullRequestDetail";
import type { PullRequestResponse } from "../../api/schemas";

export interface PullRequestPanelProps {
  repositoryId: string;
  provider?: string;
  /** Optional current branch, used as the default source branch. */
  currentBranch?: string;
}

type Mode = "list" | "create" | "detail";

/**
 * Top-level container for pull request views.
 *
 * Owns the list/create mode switch and the "selected PR number"
 * state. A follow-up revision will add a third mode ("detail") and
 * route `onSelect` from the list into it; the current revision
 * simply does nothing on row click, so the user can still browse
 * the list without a half-built detail view.
 *
 * The `listKey` counter is bumped after a successful creation so
 * that the list re-fetches and the new PR appears immediately.
 * Using a `key` prop is intentional: it forces `PullRequestList`
 * to fully re-mount, which is cheaper to reason about than an
 * imperative "reload" callback threaded through multiple layers.
 */
export function PullRequestPanel({
  repositoryId,
  provider = "github",
  currentBranch,
}: PullRequestPanelProps) {
  const [mode, setMode] = useState<Mode>("list");
  const [selectedNumber, setSelectedNumber] = useState<string | null>(
    null
  );
  const [listKey, setListKey] = useState(0);

  const handleCreated = (pr: PullRequestResponse) => {
    // After creation, jump straight into the new PR's detail view
    // so the user can review what was opened without another click.
    setSelectedNumber(pr.number);
    setMode("detail");
    setListKey((k) => k + 1);
  };

  const handleBack = () => {
    setMode("list");
    setSelectedNumber(null);
    // Re-mount the list so it reflects any state change that
    // occurred on the detail view (merge, review, new comment).
    setListKey((k) => k + 1);
  };

  if (mode === "create") {
    return (
      <CreatePullRequest
        repositoryId={repositoryId}
        provider={provider}
        defaultSourceBranch={currentBranch}
        onCreated={handleCreated}
        onCancel={() => setMode("list")}
      />
    );
  }

  if (mode === "detail" && selectedNumber) {
    return (
      <PullRequestDetail
        repositoryId={repositoryId}
        number={selectedNumber}
        provider={provider}
        onBack={handleBack}
      />
    );
  }

  return (
    <PullRequestList
      key={listKey}
      repositoryId={repositoryId}
      provider={provider}
      onSelect={(number) => {
        setSelectedNumber(number);
        setMode("detail");
      }}
      onCreateNew={() => setMode("create")}
    />
  );
}
