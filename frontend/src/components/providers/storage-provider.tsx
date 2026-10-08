// Path: frontend/src/components/providers/storage-provider.tsx
"use client";

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  type ReactNode,
} from "react";

import { configureCookieWriter } from "@/lib/storage/client";
import { setStorageCookie } from "@/app/actions/storage";
import type { StorageKey, StorageValue } from "@/lib/storage/schema";

export type CookieSnapshot = Partial<{
  [K in StorageKey]: StorageValue<K>;
}>;

interface StorageContextValue {
  cookies: CookieSnapshot;
}

const StorageContext = createContext<StorageContextValue | null>(null);

interface StorageProviderProps {
  children: ReactNode;
  cookieSnapshot: CookieSnapshot;
}

/**
 * StorageProvider initializes application storage context and bridges
 * cookie snapshots between SSR and client-side reactive subscribers.
 *
 * Registration of the cookie persistence writer is strictly isolated
 * inside useEffect to prevent any render-phase side effects or premature
 * Server Action invocations during hydration.
 */
export function StorageProvider({
  children,
  cookieSnapshot,
}: StorageProviderProps) {
  useEffect(() => {
    // Register the server action bridge exclusively after client hydration completes
    configureCookieWriter(
      async <K extends StorageKey>(
        key: K,
        value: StorageValue<K>
      ) => {
        await setStorageCookie(key, value);
      }
    );
  }, []);

  const context = useMemo(
    () => ({
      cookies: cookieSnapshot,
    }),
    [cookieSnapshot]
  );

  return (
    <StorageContext.Provider value={context}>
      {children}
    </StorageContext.Provider>
  );
}

export function useStorageCookies(): CookieSnapshot {
  const context = useContext(StorageContext);
  if (!context) {
    return {};
  }
  return context.cookies;
}
