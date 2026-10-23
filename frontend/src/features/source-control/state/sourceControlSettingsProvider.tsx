// frontend/src/features/source-control/state/SourceControlSettingsProvider.tsx

"use client";

import { useEffect } from "react";
import { useSourceControlSettings } from "./source-control-settings-store";

/**
 * Mount once at the app root to hydrate the settings store from the
 * backend before any source-control UI decides whether to render.
 *
 * Renders nothing; it only triggers the one-shot `hydrate()` call.
 * Mount it inside `layout.tsx` next to the other providers.
 */
export function SourceControlSettingsProvider() {
  const hydrate = useSourceControlSettings((s) => s.hydrate);
  const hydrated = useSourceControlSettings((s) => s.hydrated);

  useEffect(() => {
    if (!hydrated) void hydrate();
  }, [hydrate, hydrated]);

  return null;
}
