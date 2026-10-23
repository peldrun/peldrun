// frontend/src/features/source-control/utils/manual-mode-steps.ts

/**
 * Manual Mode step definitions.
 * Each step corresponds to a specific user action and API call.
 */

export type ManualStep =
  | "idle"
  | "select-repo"
  | "creating-workspace"
  | "editing"
  | "reviewing"
  | "approving"
  | "pushing"
  | "pushed"
  | "error";

export interface StepDefinition {
  id: ManualStep;
  label: string;
  description: string;
  order: number;
}

export const MANUAL_STEPS: StepDefinition[] = [
  {
    id: "select-repo",
    label: "Select Repository",
    description:
      "Choose the provider, repository, and branch you want to work on.",
    order: 1,
  },
  {
    id: "creating-workspace",
    label: "Create Workspace",
    description: "Clone the repository into this chat's files folder.",
    order: 2,
  },
  {
    id: "editing",
    label: "Edit Files",
    description: "Click any file to edit it in the Editor tab.",
    order: 3,
  },
  {
    id: "reviewing",
    label: "Inspect & Request Approval",
    description:
      "Review the diff, optionally describe your changes, and request human approval.",
    order: 4,
  },
  {
    id: "approving",
    label: "Approve & Push",
    description: "Approve the ticket and push the branch to the remote.",
    order: 5,
  },
];

/**
 * Map an internal step to the visible step number for the indicator.
 *
 * Returns 0 for idle/error states.
 *
 * "pushing" and "pushed" are transient states within the final
 * "approving" step (order 5). They map to the same visual number
 * so the indicator stays on 5 while the push is in flight or just
 * completed.
 */
export function stepNumber(step: ManualStep): number {
  if (step === "pushing" || step === "pushed") {
    return 5;
  }
  const found = MANUAL_STEPS.find((s) => s.id === step);
  return found?.order ?? 0;
}
