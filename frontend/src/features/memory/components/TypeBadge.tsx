// src/features/memory/components/TypeBadge.tsx

/**
 * A pill showing a memory type (`semantic` / `episodic` /
 * `procedural` / `preference` / `decision`).
 *
 * Pure display. Same pattern as `ScopeBadge` -- the two are
 * visually and structurally parallel so that a reader of one
 * already knows how to read the other. Only the value set and
 * the color map differ.
 *
 * Color policy
 * ------------
 *
 * The five types are close in meaning (facts vs. events vs.
 * preferences), so the palette is deliberately MORE varied
 * than the scope palette -- the type is the more informative
 * of the two when scanning a list:
 *
 *   * semantic   -- slate      (neutral fact)
 *   * episodic   -- pink       (event, time-anchored)
 *   * procedural -- cyan       (how-to)
 *   * preference -- rose       (user taste)
 *   * decision   -- amber      (committed choice)
 *
 * Decision gets the most attention-drawing hue on purpose:
 * it is the type whose retrieval changes user behavior most
 * often ("we already decided X, stop suggesting Y").
 *
 * Optional description
 * --------------------
 *
 * The `withDescription` prop renders the type name and a
 * shorter subtitle underneath on the detail page. In list
 * cards, the compact single-word form is what fits -- a card
 * that renders "Decision — a committed choice the team
 * agreed on" wastes vertical space repeating information a
 * user has already learned.
 */

'use client';

import { useTranslation } from 'react-i18next';

import { MemoryType } from '../types/enums';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Style map
// ---------------------------------------------------------------------------

const TYPE_STYLES: Record<
  MemoryType,
  { soft: string; solid: string; outline: string }
> = {
  [MemoryType.SEMANTIC]: {
    soft: 'bg-slate-100 text-slate-800 dark:bg-slate-800/60 dark:text-slate-200',
    solid: 'bg-slate-600 text-white',
    outline:
      'border border-slate-400 text-slate-800 dark:border-slate-700 dark:text-slate-200',
  },
  [MemoryType.EPISODIC]: {
    soft: 'bg-pink-100 text-pink-800 dark:bg-pink-900/40 dark:text-pink-300',
    solid: 'bg-pink-600 text-white',
    outline:
      'border border-pink-400 text-pink-800 dark:border-pink-700 dark:text-pink-300',
  },
  [MemoryType.PROCEDURAL]: {
    soft: 'bg-cyan-100 text-cyan-800 dark:bg-cyan-900/40 dark:text-cyan-300',
    solid: 'bg-cyan-600 text-white',
    outline:
      'border border-cyan-400 text-cyan-800 dark:border-cyan-700 dark:text-cyan-300',
  },
  [MemoryType.PREFERENCE]: {
    soft: 'bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300',
    solid: 'bg-rose-600 text-white',
    outline:
      'border border-rose-400 text-rose-800 dark:border-rose-700 dark:text-rose-300',
  },
  [MemoryType.DECISION]: {
    soft: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
    solid: 'bg-amber-600 text-white',
    outline:
      'border border-amber-400 text-amber-800 dark:border-amber-700 dark:text-amber-300',
  },
};

const SIZE_STYLES = {
  xs: 'px-1.5 py-0.5 text-[10px]',
  sm: 'px-2 py-0.5 text-[11px]',
  md: 'px-2.5 py-1 text-xs',
} as const;

type TypeBadgeSize = keyof typeof SIZE_STYLES;
type TypeBadgeVariant = 'soft' | 'solid' | 'outline';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface TypeBadgeProps {
  memory_type: MemoryType;
  size?: TypeBadgeSize;
  variant?: TypeBadgeVariant;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function TypeBadge({
  memory_type,
  size = 'sm',
  variant = 'soft',
  className,
}: TypeBadgeProps) {
  const { t } = useTranslation('memory');
  const label = t(`type.${memory_type}`);
  const colorClass = TYPE_STYLES[memory_type][variant];
  const ariaLabel = t('a11y.typeBadge', { type: label });

  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full font-medium whitespace-nowrap',
        SIZE_STYLES[size],
        colorClass,
        className,
      )}
      aria-label={ariaLabel}
      role="status"
    >
      {label}
    </span>
  );
}
