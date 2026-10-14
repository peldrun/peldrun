// frontend/src/app/settings/setup/tabs/llm/hooks/useDiscoveryTarget.ts
"use client";

import { useCallback, useState } from "react";
import type { DiscoveryTarget } from "../types";

/* -------------------------------------------------------------------------- */
/* Hook contract                                                              */
/* -------------------------------------------------------------------------- */

export interface UseDiscoveryTargetResult {
  /** Currently opened discovery target, or `null` when the modal is closed. */
  discoveryTarget: DiscoveryTarget | null;
  /** Opens the discovery modal for the given provider or custom endpoint. */
  openDiscovery: (target: DiscoveryTarget) => void;
  /** Closes the discovery modal and clears the active target. */
  closeDiscovery: () => void;
}

/* -------------------------------------------------------------------------- */
/* Hook                                                                       */
/* -------------------------------------------------------------------------- */

/**
 * Manages the state of the model-discovery modal.
 *
 * The modal is shared between cloud providers and custom endpoints; this
 * hook only tracks *which* target is currently being inspected. Persistence
 * and model merging are handled by the caller (see `handleSaveDiscovered`
 * in the main LLM tab) so that the same hook stays reusable.
 */
export function useDiscoveryTarget(): UseDiscoveryTargetResult {
  const [discoveryTarget, setDiscoveryTarget] = useState<DiscoveryTarget | null>(
    null
  );

  const openDiscovery = useCallback((target: DiscoveryTarget) => {
    setDiscoveryTarget(target);
  }, []);

  const closeDiscovery = useCallback(() => {
    setDiscoveryTarget(null);
  }, []);

  return { discoveryTarget, openDiscovery, closeDiscovery };
}
