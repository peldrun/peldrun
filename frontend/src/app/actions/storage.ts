// Path: frontend/src/app/actions/storage.ts
"use server";

import { cookies } from "next/headers";
import {
  storageSchema,
  type StorageKey,
  type StorageValue,
} from "@/lib/storage/schema";

/**
 * Standard cookie configuration for application state persistence.
 * Configured with Lax SameSite policy and 1-year expiration for session continuity.
 */
const COOKIE_OPTIONS = {
  httpOnly: false,
  secure: process.env.NODE_ENV === "production",
  sameSite: "lax" as const,
  path: "/",
  maxAge: 60 * 60 * 24 * 365,
};

/**
 * Persists a strongly-typed key-value pair into the browser cookies via Next.js Server Action.
 *
 * Implements strict defensive handling to prevent runtime crashes during Next.js 16
 * server-side rendering (SSR) passes or unauthorized mutation phases.
 *
 * @template K - Constrained to registered StorageKey schemas
 * @param key - The unique storage identifier
 * @param value - The value matching the registered schema definition
 */
export async function setStorageCookie<K extends StorageKey>(
  key: K,
  value: StorageValue<K>
): Promise<void> {
  try {
    const definition = storageSchema[key];
    if (!definition || definition.tier !== "cookie") {
      return;
    }

    const validation = definition.schema.safeParse(value);
    if (!validation.success) {
      if (process.env.NODE_ENV !== "production") {
        console.warn(
          `[StorageAction] Schema validation rejected for "${String(key)}":`,
          validation.error.message
        );
      }
      return;
    }

    const cookieStore = await cookies();
    const rawString =
      typeof validation.data === "string"
        ? validation.data
        : JSON.stringify(validation.data);

    cookieStore.set(key, rawString, COOKIE_OPTIONS);
  } catch (error) {
    // Next.js strictly forbids cookie writes during SSR or component render phases.
    // Gracefully catch to maintain component tree stability without crashing the runtime.
    if (process.env.NODE_ENV !== "production") {
      console.warn(
        `[StorageAction] Deferred cookie write for "${String(key)}":`,
        error instanceof Error ? error.message : error
      );
    }
  }
}

/**
 * Removes a specific storage key from client cookies via Next.js Server Action.
 *
 * @template K - Constrained to registered StorageKey schemas
 * @param key - The storage identifier to clear
 */
export async function deleteStorageCookie<K extends StorageKey>(
  key: K
): Promise<void> {
  try {
    const definition = storageSchema[key];
    if (!definition || definition.tier !== "cookie") {
      return;
    }

    const cookieStore = await cookies();
    cookieStore.delete(key);
  } catch (error) {
    if (process.env.NODE_ENV !== "production") {
      console.warn(
        `[StorageAction] Failed to delete cookie "${String(key)}":`,
        error instanceof Error ? error.message : error
      );
    }
  }
}
