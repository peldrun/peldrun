// src/features/memory/components/ScopeBadge.tsx

/**
 * A pill showing a memory scope (`user` / `project` / `chat` /
 * `agent`).
 *
 * Pure display. Contrast with `ModeBadge`, which supports an
 * interactive variant -- scope is never clickable on its own,
 * because "click the scope" has no consistent meaning across
 * the UI (from a card, it filters the list; from a detail
 * page, it navigates back; from a form, it is locked). Each
 * caller wraps the badge in its own clickable container when
 * interactivity makes sense for that context.
 *
 * Color policy
 * ------------
 *
 * The four scopes get four distinct hues, and the mapping is
 * shared across every surface that displays a scope (cards,
 * detail page, filters sidebar, import/export form, promote
 * dialog). A user learns it once:
 *
 *   * user    -- indigo  (personal, distinct from work)
 *   * project -- sky     (the default write target)
 *   * chat    -- violet  (transient, conversational)
 *   * agent   -- teal    (system-internal)
 *
 * The hues were chosen to be distinguishable in dark mode as
 * well -- the "soft" palette is paired with a -300 text color
 * and a -900/40 background, which stays legible at the
 * smallest rendered size (11px) used in `EntryCard`.
 *
 * Content
 * -------
 *
 * By default the badge shows the scope TYPE label only
 * ("Project"). When `showScopeId` is true, it renders
 * "Project · acme" -- useful on detail pages where the
 * specific project matters, unhelpful in a list where the
 * scope is uniform across every card and repeating the id
 * would just add noise.
 */

'use client';

import { useTranslation } from 'react-i18next';

import { MemoryScope } from '../types/enums';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Style map
// ---------------------------------------------------------------------------

/**
 * Per-scope color classes. Same "3 variants" approach as
 * `ModeBadge` -- a single source of truth per scope, indexed
 * by variant.
 */
const SCOPE_STYLES: Record<
  MemoryScope,
  { soft: string; solid: string; outline: string }
> = {
  [MemoryScope.USER]: {
    soft: 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-300',
    solid: 'bg-indigo-600 text-white',
    outline:
      'border border-indigo-400 text-indigo-800 dark:border-indigo-700 dark:text-indigo-300',
  },
  [MemoryScope.PROJECT]: {
    soft: 'bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-300',
    solid: 'bg-sky-600 text-white',
    outline:
      'border border-sky-400 text-sky-800 dark:border-sky-700 dark:text-sky-300',
  },
  [MemoryScope.CHAT]: {
    soft: 'bg-violet-100 text-violet-800 dark:bg-violet-900/40 dark:text-violet-300',
    solid: 'bg-violet-600 text-white',
    outline:
      'border border-violet-400 text-violet-800 dark:border-violet-700 dark:text-violet-300',
  },
  [MemoryScope.AGENT]: {
    soft: 'bg-teal-100 text-teal-800 dark:bg-teal-900/40 dark:text-teal-300',
    solid: 'bg-teal-600 text-white',
    outline:
      'border border-teal-400 text-teal-800 dark:border-teal-700 dark:text-teal-300',
  },
};

const SIZE_STYLES = {
  xs: 'px-1.5 py-0.5 text-[10px]',
  sm: 'px-2 py-0.5 text-[11px]',
  md: 'px-2.5 py-1 text-xs',
} as const;

type ScopeBadgeSize = keyof typeof SIZE_STYLES;
type ScopeBadgeVariant = 'soft' | 'solid' | 'outline';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ScopeBadgeProps {
  scope_type: MemoryScope;
  /**
   * When provided, renders the id after a middle dot:
   * "Project · acme". Use on detail pages and filter chips;
   * omit in list cards where the id is uniform across the
   * list (the current scope is already displayed in the
   * sidebar).
   */
  scope_id?: string;
  size?: ScopeBadgeSize;
  variant?: ScopeBadgeVariant;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ScopeBadge({
  scope_type,
  scope_id,
  size = 'sm',
  variant = 'soft',
  className,
}: ScopeBadgeProps) {
  const { t } = useTranslation('memory');
  const typeLabel = t(`scope.${scope_type}`);
  const colorClass = SCOPE_STYLES[scope_type][variant];
  const ariaLabel = scope_id
    ? t('a11y.scopeBadgeWithId', { scope: typeLabel, id: scope_id })
    : t('a11y.scopeBadge', { scope: typeLabel });

  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full font-medium whitespace-nowrap',
        SIZE_STYLES[size],
        colorClass,
        className,
      )}
      aria-label={ariaLabel}
      role="status"
    >
      <span>{typeLabel}</span>
      {scope_id && (
        <>
          <span aria-hidden="true" className="opacity-60">
            ·
          </span>
          <span className="font-mono text-[0.9em]">{scope_id}</span>
        </>
      )}
    </span>
  );
}
