// frontend/src/features/source-control/components/manual/ManualStepIndicator.tsx

import React from "react";
import { useManualModeStore } from "../../state/manual-mode-store";
import { MANUAL_STEPS, stepNumber } from "../../utils/manual-mode-steps";
import { Check } from "lucide-react";

/**
 * Visual progress indicator showing the current step of the manual
 * workflow.
 *
 * The indicator is purely presentational: it reads the current step
 * from the store and maps it to a 1..5 progression via
 * `stepNumber()`. Transient states (`pushing`, `pushed`) map to the
 * final step (5) so the bar does not collapse while a push is in
 * flight or immediately after success.
 *
 * Renders nothing during `idle` and `error` states — in both cases
 * `stepNumber()` returns 0 and every step is shown as gray.
 */
export function ManualStepIndicator() {
  const { step } = useManualModeStore();
  const currentNumber = stepNumber(step);

  return (
    <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-card/40 shrink-0">
      {MANUAL_STEPS.map((s, idx) => {
        const isDone = currentNumber > s.order;
        const isCurrent = currentNumber === s.order;
        return (
          <React.Fragment key={s.id}>
            <div className="flex flex-col items-center">
              <div
                className={`flex h-7 w-7 items-center justify-center rounded-full text-xs font-medium ${
                  isDone
                    ? "bg-green-500 text-white"
                    : isCurrent
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted text-muted-foreground"
                }`}
                aria-current={isCurrent ? "step" : undefined}
              >
                {isDone ? <Check size={14} /> : s.order}
              </div>
              <span
                className={`mt-1 text-[10px] ${
                  isCurrent
                    ? "text-foreground font-medium"
                    : "text-muted-foreground"
                }`}
              >
                {s.label}
              </span>
            </div>
            {idx < MANUAL_STEPS.length - 1 && (
              <div
                className={`flex-1 h-0.5 mx-2 ${
                  isDone ? "bg-green-500" : "bg-muted"
                }`}
                aria-hidden="true"
              />
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}
