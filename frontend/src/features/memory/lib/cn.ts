// src/features/memory/lib/cn.ts

/**
 * Class-name composition utility.
 *
 * Combines `clsx` (conditional class logic) with
 * `tailwind-merge` (conflict resolution) into the single `cn`
 * helper that every memory component uses for conditional
 * Tailwind classes.
 *
 * Why `tailwind-merge` is essential
 * ---------------------------------
 *
 * Without it, two conflicting utilities both reach the DOM and
 * the winner is decided by source order in the compiled
 * stylesheet -- NOT by the order in which they appear in the
 * className string. A component that does
 *
 *     cn('bg-gray-100', isActive && 'bg-blue-100')
 *
 * would be non-deterministic about which background wins.
 * `tailwind-merge` resolves the conflict in favor of the
 * LAST occurrence, which matches the intuition of every
 * developer writing the call.
 *
 * Why this module instead of a shared `lib/utils`
 * ------------------------------------------------
 *
 * The memory feature is the only part of the codebase
 * guaranteed to use this exact helper. If the app later grows
 * a shared `lib/utils/cn.ts` (many Next.js projects do), the
 * import in every memory component can be updated in one
 * search-and-replace -- no source files change, only this
 * module's body.
 *
 * Type contract
 * -------------
 *
 * Accepts any mix of strings, arrays, objects, and falsy values
 * that `clsx` accepts. Returns a string; never `undefined` or
 * `null`. This makes the return value safe to pass directly to
 * React's `className` prop without a fallback.
 */

import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/**
 * Compose className strings with Tailwind-aware conflict
 * resolution.
 *
 * @example
 * cn('px-2 py-1', 'bg-blue-100')
 * // => 'px-2 py-1 bg-blue-100'
 *
 * @example
 * cn('px-2', isSmall && 'px-1', 'py-1')
 * // => 'px-1 py-1'  (last px wins, py preserved)
 *
 * @example
 * cn('rounded', { 'bg-red-500': hasError, 'bg-green-500': !hasError })
 * // => 'rounded bg-red-500' or 'rounded bg-green-500'
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
