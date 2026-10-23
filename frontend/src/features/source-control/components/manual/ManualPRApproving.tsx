// frontend/src/features/source-control/components/manual/ManualPRApproving.tsx
import React, { useState } from "react";
import { useManualModeStore } from "../../state/manual-mode-store";
import { useManualMode } from "../../hooks/useManualMode";
import { CheckCircle2 } from "lucide-react";

export function ManualPRApproving() {
  const { prTitle } = useManualModeStore();
  const { submitPR } = useManualMode();
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleApprove = async () => {
    setIsSubmitting(true);
    try {
      await submitPR();
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleBack = () => {
    useManualModeStore.getState().setStep("creating-pr");
  };

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-foreground mb-1">
          Approve PR Creation
        </h3>
        <p className="text-xs text-muted-foreground mb-4">
          You are about to open a pull request titled: <strong>{prTitle}</strong>
        </p>
      </div>

      <button
        onClick={handleApprove}
        disabled={isSubmitting}
        className="w-full flex items-center justify-center gap-2 rounded-md bg-green-600 text-white px-4 py-3 text-sm font-semibold hover:bg-green-700 disabled:opacity-50"
      >
        {isSubmitting ? (
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-white border-r-transparent" />
        ) : (
          <CheckCircle2 size={18} />
        )}
        {isSubmitting ? "Creating PR..." : "Approve & Create PR"}
      </button>

      <button
        onClick={handleBack}
        disabled={isSubmitting}
        className="w-full text-xs text-muted-foreground hover:text-foreground"
      >
        ← Back to edit details
      </button>
    </div>
  );
}
