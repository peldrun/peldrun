// frontend/src/features/source-control/components/github/IssuePanel.tsx

import React, { useState } from "react";
import { IssueList } from "./IssueList";
import { IssueDetail } from "./IssueDetail";
import { CreateIssue } from "./CreateIssue";
import type { IssueResponse } from "../../api/schemas";

export interface IssuePanelProps {
  repositoryId: string;
  provider?: string;
}

type Mode = "list" | "detail" | "create";

/**
 * Top-level container for issue views.
 *
 * Owns the list/detail/create mode switch and the selected issue
 * number. The `listKey` counter is bumped whenever we return to
 * the list (after create or detail interaction) so the list
 * re-fetches and reflects the latest state — e.g. an issue that
 * was just closed from the detail view.
 */
export function IssuePanel({
  repositoryId,
  provider = "github",
}: IssuePanelProps) {
  const [mode, setMode] = useState<Mode>("list");
  const [selectedNumber, setSelectedNumber] = useState<string | null>(
    null
  );
  const [listKey, setListKey] = useState(0);

  const handleCreated = (issue: IssueResponse) => {
    setSelectedNumber(issue.number);
    setMode("detail");
    setListKey((k) => k + 1);
  };

  const handleBack = () => {
    setMode("list");
    setSelectedNumber(null);
    setListKey((k) => k + 1);
  };

  if (mode === "create") {
    return (
      <CreateIssue
        repositoryId={repositoryId}
        provider={provider}
        onCreated={handleCreated}
        onCancel={() => setMode("list")}
      />
    );
  }

  if (mode === "detail" && selectedNumber) {
    return (
      <IssueDetail
        repositoryId={repositoryId}
        number={selectedNumber}
        provider={provider}
        onBack={handleBack}
      />
    );
  }

  return (
    <IssueList
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
