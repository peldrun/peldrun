// frontend/src/features/source-control/components/manual/ManualPRCreated.tsx
import React from "react";
import { useManualModeStore } from "../../state/manual-mode-store";
import { GitPullRequest, ExternalLink } from "lucide-react";

export function ManualPRCreated() {
  const { prUrl, chatId } = useManualModeStore();

  const handleFinish = () => {
    if (chatId) {
      useManualModeStore.getState().activate(chatId);
    } else {
      useManualModeStore.getState().reset();
    }
  };

  return (
    <div className="flex flex-col items-center py-6 px-2 text-center">
      <GitPullRequest className="h-12 w-12 text-purple-500 mb-4" />
      <h3 className="text-lg font-semibold text-foreground">
        Pull Request Created
      </h3>
      <p className="text-sm text-muted-foreground mt-2">
        Your changes have been submitted for review.
      </p>

      {prUrl && (
        <a
          href={prUrl}
          target="_blank"
          rel="noreferrer"
          className="mt-6 flex items-center justify-center gap-2 rounded-md bg-primary text-primary-foreground px-6 py-2 text-sm font-medium hover:bg-primary/90 transition-colors"
        >
          <ExternalLink size={16} />
          Open Pull Request on GitHub
        </a>
      )}

      <button
        onClick={handleFinish}
        className="mt-6 text-sm text-muted-foreground hover:text-foreground"
      >
        Start a new workflow
      </button>
    </div>
  );
}
