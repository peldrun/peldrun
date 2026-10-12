// Path: frontend/src/components/chat/chat-container/hooks/use-metadata-vault.ts
"use client";

import { useEffect, useState } from "react";
import { fetchServerMetadata } from "@/lib/modelMetadata";

/**
 * Loads and keeps the server metadata vault in sync.
 *
 * The vault is fetched once on mount and then refreshed whenever a
 * `omweb:metadata-updated` custom event is dispatched (e.g. after a
 * model list refresh triggered from the settings UI).
 */
export function useMetadataVault(): Record<string, any> {
  const [metadataVault, setMetadataVault] = useState<Record<string, any>>({});

  useEffect(() => {
    fetchServerMetadata().then((data) => setMetadataVault(data || {}));
    const onMetadataUpdate = (e: any) => setMetadataVault(e.detail || {});
    window.addEventListener("omweb:metadata-updated", onMetadataUpdate);
    return () =>
      window.removeEventListener("omweb:metadata-updated", onMetadataUpdate);
  }, []);

  return metadataVault;
}
