// frontend/src/features/source-control/components/manual/ManualPRCreator.tsx
import React from "react";
import { useManualModeStore } from "../../state/manual-mode-store";
import { useManualMode } from "../../hooks/useManualMode";
import { ArrowRight } from "lucide-react";

export function ManualPRCreator() {
  const { prTitle, prBody, setPrTitle, setPrBody } = useManualModeStore();
  const { requestPRApproval } = useManualMode();
  const [isSubmitting, setIsSubmitting] = React.useState(false);

  const handleSubmit = async () => {
    setIsSubmitting(true);
    try {
      await requestPRApproval();
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">
          Create Pull Request
        </h3>
        <p className="text-xs text-muted-foreground mb-4">
          Provide a title and description for your pull request.
        </p>
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-700 mb-1">Title</label>
        <input
          type="text"
          value={prTitle}
          onChange={(e) => setPrTitle(e.target.value)}
          placeholder="e.g. feat: add new feature"
          className="w-full rounded-md border border-border px-3 py-2 text-sm bg-background"
        />
      </div>

      <div>
        <label className="block text-xs font-medium text-gray-700 mb-1">Description</label>
        <textarea
          value={prBody}
          onChange={(e) => setPrBody(e.target.value)}
          placeholder="Explain what you changed and why."
          rows={4}
          className="w-full rounded-md border border-border px-3 py-2 text-sm bg-background resize-none"
        />
      </div>

      <button
        onClick={handleSubmit}
        disabled={isSubmitting || !prTitle.trim()}
        className="w-full flex items-center justify-center gap-2 rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 disabled:opacity-50"
      >
        {isSubmitting ? "Requesting approval..." : "Request PR Approval"}
        {!isSubmitting && <ArrowRight size={16} />}
      </button>
    </div>
  );
}
