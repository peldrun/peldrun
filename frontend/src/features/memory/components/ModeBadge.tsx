// src/features/memory/components/ModeBadge.tsx

/**
 * A pill showing the current memory mode.
 *
 * Used everywhere the user needs an at-a-glance indicator of
 * whether memory is on, read-only, or fully writable:
 *
 *   * The memory layout's header (top-right corner).
 *   * The chat composer's tiny mode indicator (F8.1).
 *   * The settings page's mode section heading.
 *   * Any future "your memory is disabled" banner.
 *
 * The badge has two operational modes:
 *
 *   1. **Display-only** (`onClick` prop omitted) -- a static
 *      pill. Right for the chat composer, where clicking
 *      would navigate away from the conversation.
 *
 *   2. **Interactive** (`onClick` provided) -- rendered as a
 *      `<button>` with proper focus and keyboard handling.
 *      Right for the layout header, where clicking opens
 *      settings.
 *
 * Data source
 * -----------
 *
 * By default the badge reads the current mode from
 * `useMemorySettings()`. Two escape hatches exist:
 *
 *   * The `mode` prop overrides the query -- useful in tests
 *     and in contexts where the caller already has the mode
 *     and does not want a per-render query subscription
 *     (e.g. inside a list that already derived it from a
 *     parent query).
 *
 *   * When neither the prop nor the query has produced a mode
 *     (still loading, or error), the badge renders a muted
 *     "..." placeholder. It NEVER blocks the surrounding
 *     layout on a pending query -- a header that shifts
 *     height when the query resolves is worse than a badge
 *     that updates in place.
 *
 * Color policy
 * ------------
 *
 * The three modes have distinct, non-overlapping hues:
 *
 *   * disabled   -- slate/gray. Conveys "off" without alarm.
 *   * read_only  -- amber. Warns that writes are blocked.
 *   * read_write -- emerald. The "healthy" default.
 *
 * The same palette is used in `EntryCard`'s mode-related
 * indicators and in `SettingsForm`'s mode radio group, so a
 * user learns the mapping once.
 *
 * Accessibility
 * -------------
 *
 * The badge always carries an aria-label combining the mode
 * name and its description (e.g. "Mode: Read-only — Memory
 * can be read..."). Screen reader users get the same
 * information a sighted user reads visually.
 */

'use client';

import {
  Lock,
  PowerOff,
  Unlock,
  type LucideIcon,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { MemoryMode } from '../types/enums';
import { useMemorySettings } from '../api/queries';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Visual style map
// ---------------------------------------------------------------------------

/**
 * Per-mode styling metadata.
 *
 * Kept as a single frozen object (rather than a `switch`
 * statement in the render body) so that:
 *
 *   * The three representations -- color classes, icon, and
 *     translation key prefix -- stay colocated. Adding a mode
 *     means adding one entry here, not three places.
 *
 *   * TypeScript enforces exhaustiveness via the
 *     `Record<MemoryMode, ...>` annotation: a missing mode is
 *     a compile error, not a runtime `undefined` lookup.
 */
const MODE_VISUALS: Record<
  MemoryMode,
  {
    icon: LucideIcon;
    soft: string;
    solid: string;
    outline: string;
  }
> = {
  [MemoryMode.DISABLED]: {
    icon: PowerOff,
    soft: 'bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300',
    solid: 'bg-slate-600 text-white dark:bg-slate-500',
    outline:
      'border border-slate-300 text-slate-700 dark:border-slate-700 dark:text-slate-300',
  },
  [MemoryMode.READ_ONLY]: {
    icon: Lock,
    soft: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
    solid: 'bg-amber-600 text-white',
    outline:
      'border border-amber-400 text-amber-800 dark:border-amber-700 dark:text-amber-300',
  },
  [MemoryMode.READ_WRITE]: {
    icon: Unlock,
    soft: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300',
    solid: 'bg-emerald-600 text-white',
    outline:
      'border border-emerald-400 text-emerald-800 dark:border-emerald-700 dark:text-emerald-300',
  },
};

// ---------------------------------------------------------------------------
// Size map
// ---------------------------------------------------------------------------

/**
 * Padding, text size, and icon size per `size` variant.
 *
 * Kept as a separate object (not inlined in the render body)
 * because both the static and interactive branches of the
 * component need the same values -- duplicating them across
 * the two branches would create a drift risk.
 */
const SIZE_STYLES = {
  xs: {
    container: 'gap-0.5 px-1 py-0 text-[10px]',
    icon: 9,
  },
  sm: {
    container: 'gap-1 px-1.5 py-0.5 text-[11px]',
    icon: 11,
  },
  md: {
    container: 'gap-1.5 px-2.5 py-1 text-xs',
    icon: 13,
  },
} as const;

type ModeBadgeSize = keyof typeof SIZE_STYLES;
type ModeBadgeVariant = 'soft' | 'solid' | 'outline';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ModeBadgeProps {
  /**
   * Explicit mode override. When provided, the component uses
   * this value and does NOT subscribe to the settings query.
   *
   * Useful in:
   *   * tests (no QueryClientProvider required),
   *   * storybook stories,
   *   * call sites that already have the mode from a parent
   *     query and want to avoid a redundant subscription.
   */
  mode?: MemoryMode;

  /** Visual density. Defaults to `'md'`. */
  size?: ModeBadgeSize;

  /** Color treatment. Defaults to `'soft'`. */
  variant?: ModeBadgeVariant;

  /** Whether to render the leading icon. Defaults to `true`. */
  showIcon?: boolean;

  /**
   * When provided, the badge becomes an interactive `<button>`
   * that calls this on click / Enter / Space.
   *
   * When omitted, the badge renders as a non-interactive
   * `<span>` -- correct for display-only contexts (chat
   * composer, entry cards).
   */
  onClick?: () => void;

  /**
   * Accessible name override. When omitted, the component
   * builds one from the mode's translated name and
   * description.
   *
   * Providing this is an escape hatch for contexts where the
   * surrounding UI supplies its own context (e.g. a form
   * label that already reads "Mode: Read-only").
   */
  'aria-label'?: string;

  /** Additional class names for the outer element. */
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * Render a pill indicating the current memory mode.
 *
 * See the module JSDoc for the full contract.
 */
export function ModeBadge({
  mode,
  size = 'md',
  variant = 'soft',
  showIcon = true,
  onClick,
  'aria-label': ariaLabelOverride,
  className,
}: ModeBadgeProps) {
  const { t } = useTranslation('memory');

  // ---- Resolve the mode -----------------------------------------
  //
  // The hook is ALWAYS called (React rules of hooks), but its
  // result is ignored when `mode` is provided. Passing
  // `enabled: mode === undefined` avoids firing a network
  // request when an explicit mode was supplied -- important in
  // tests where there is no QueryClientProvider.
  const settingsQuery = useMemorySettings({
    enabled: mode === undefined,
  });
  const resolvedMode: MemoryMode | null =
    mode ?? settingsQuery.data?.mode ?? null;

  // ---- Loading placeholder --------------------------------------
  //
  // A muted "..." with the same pill shape as the real badge,
  // so the layout does not shift when data arrives. This is
  // better than returning `null`: a null-return would cause
  // the header to jump by the badge's height the moment the
  // query resolves.
  if (resolvedMode === null) {
    return (
      <span
        aria-hidden="true"
        className={cn(
          'inline-flex items-center rounded-full font-medium',
          SIZE_STYLES[size].container,
          'bg-slate-100 text-slate-400 dark:bg-slate-800 dark:text-slate-500',
          className,
        )}
      >
        {showIcon && (
          <span
            aria-hidden="true"
            className="h-2 w-2 animate-pulse rounded-full bg-current"
          />
        )}
        <span className="tabular-nums">...</span>
      </span>
    );
  }

  // ---- Resolved mode --------------------------------------------
  const visuals = MODE_VISUALS[resolvedMode];
  const Icon = visuals.icon;
  const sizeStyles = SIZE_STYLES[size];
  const label = t(`mode.${resolvedMode}.badge`);
  const description = t(`mode.${resolvedMode}.description`);
  const ariaLabel =
    ariaLabelOverride ?? t('a11y.modeBadge', {
      mode: label,
      description,
      // The a11y key expects a single "mode" interpolation in
      // the JSON; the extra description is included inline in
      // the translation string itself. This keeps the API
      // surface small.
    });

  const colorClass =
    variant === 'solid'
      ? visuals.solid
      : variant === 'outline'
        ? visuals.outline
        : visuals.soft;

  const baseClasses = cn(
    'inline-flex items-center rounded-full font-medium',
    'whitespace-nowrap',
    sizeStyles.container,
    colorClass,
    // Interactive states only apply when the component is a
    // button; on a span these classes are inert (Tailwind
    // emits them, the browser never triggers them).
    onClick && 'cursor-pointer transition-colors hover:opacity-90',
    onClick &&
      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-emerald-500',
    className,
  );

  const content = (
    <>
      {showIcon && (
        <Icon
          size={sizeStyles.icon}
          aria-hidden="true"
          className="shrink-0"
        />
      )}
      <span>{label}</span>
    </>
  );

  // ---- Non-interactive: render a span ---------------------------
  if (!onClick) {
    return (
      <span
        className={baseClasses}
        aria-label={ariaLabel}
        role="status"
      >
        {content}
      </span>
    );
  }

  // ---- Interactive: render a button -----------------------------
  return (
    <button
      type="button"
      onClick={onClick}
      className={baseClasses}
      aria-label={ariaLabel}
    >
      {content}
    </button>
  );
}
