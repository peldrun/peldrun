// src/features/memory/components/ImportanceSlider.tsx

/**
 * Slider for the `importance` field on an entry (0..100).
 *
 * The value maps to one of three visual tiers, each with a
 * distinct color (see `TIER_COLORS`). Tier boundaries match
 * the spec's §4.7.3 rule:
 *
 *   * 0..40   -- low      (slate)
 *   * 41..70  -- medium   (amber)
 *   * 71..100 -- high     (red)
 *
 * The tier name is rendered next to the numeric value so that
 * screen readers and sighted users get the same information
 * (`82 · High` rather than just `82`).
 *
 * Implementation note
 * -------------------
 *
 * The visible track is a pair of `<div>`s (a base track and a
 * filled overlay whose width equals `value%`). The actual
 * `<input type="range">` is layered on top, fully transparent,
 * and handles input, keyboard, and accessibility. This is the
 * only reliable way to color a native range input across
 * browsers: the default `<input>` cannot be recolored per-value
 * without pseudo-element hacks that break on Safari and
 * Firefox.
 */

'use client';

import { useTranslation } from 'react-i18next';

import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Tier logic
// ---------------------------------------------------------------------------

type ImportanceTier = 'low' | 'medium' | 'high';

/**
 * Map a numeric importance to its tier.
 *
 * Exported for reuse by `EntryCard` (which shows a small
 * colored dot matching the tier) and by any future overview
 * that summarizes importance. Keeping the boundary values in
 * one place is what makes "the dot matches the slider"
 * trivially true.
 */
export function importanceTier(value: number): ImportanceTier {
  if (value <= 40) return 'low';
  if (value <= 70) return 'medium';
  return 'high';
}

/**
 * Per-tier styling. `bg` is the filled portion of the track,
 * `track` is the base (unfilled) track, `text` is the value
 * label color, `border` is the thumb border color (matches
 * `text`).
 */
const TIER_COLORS: Record<
  ImportanceTier,
  {
    bg: string;
    track: string;
    text: string;
    border: string;
  }
> = {
  low: {
    bg: 'bg-slate-500',
    track: 'bg-slate-500/15',
    text: 'text-slate-600 dark:text-slate-300',
    border: 'border-slate-500',
  },
  medium: {
    bg: 'bg-amber-500',
    track: 'bg-amber-500/15',
    text: 'text-amber-700 dark:text-amber-400',
    border: 'border-amber-500',
  },
  high: {
    bg: 'bg-red-500',
    track: 'bg-red-500/15',
    text: 'text-red-600 dark:text-red-400',
    border: 'border-red-500',
  },
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ImportanceSliderProps {
  /** The current importance value (0..100). */
  value: number;

  /** Called with the new value (0..100) as the user drags. */
  onChange: (value: number) => void;

  /** When `true`, the slider is rendered non-interactive. */
  disabled?: boolean;

  /**
   * The `<label htmlFor>` target. When provided, the visible
   * label is associated with the range input for accessibility.
   * When omitted, `aria-label` on the input alone carries the
   * accessible name.
   */
  id?: string;

  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ImportanceSlider({
  value,
  onChange,
  disabled = false,
  id,
  className,
}: ImportanceSliderProps) {
  const { t } = useTranslation('memory');

  // Clamp defensively -- callers might pass out-of-range values
  // from a stale draft; clamping here keeps the visual track
  // consistent with the numeric label.
  const clamped = Math.max(0, Math.min(100, Math.round(value)));
  const tier = importanceTier(clamped);
  const colors = TIER_COLORS[tier];
  const tierLabel = t(`entry.importanceLevels.${tier}`);

  return (
    <div className={cn('space-y-2', className)}>
      {/* ---- Header: label + numeric value + tier --------- */}
      <div className="flex items-center justify-between gap-2">
        {id ? (
          <label
            htmlFor={id}
            className="text-xs font-medium text-foreground"
          >
            {t('entry.fields.importance')}
          </label>
        ) : (
          <span className="text-xs font-medium text-foreground">
            {t('entry.fields.importance')}
          </span>
        )}

        <div className="flex items-baseline gap-1.5">
          <span
            className={cn(
              'text-sm font-semibold tabular-nums',
              colors.text,
            )}
          >
            {clamped}
          </span>
          <span className="text-[11px] text-muted-foreground">
            / 100 · {tierLabel}
          </span>
        </div>
      </div>

      {/* ---- Track + transparent range input ------------- */}
      <div className="relative h-2 w-full">
        {/* Base track. */}
        <div
          aria-hidden="true"
          className={cn(
            'absolute inset-0 rounded-full',
            colors.track,
          )}
        />
        {/* Filled portion, width = value%. */}
        <div
          aria-hidden="true"
          className={cn(
            'absolute inset-y-0 start-0 rounded-full transition-[width] duration-100',
            colors.bg,
          )}
          style={{ width: `${clamped}%` }}
        />
        {/* The actual input -- invisible, full-size, atop the
            track. The `text-*` class on the input drives
            `border-current` on the thumb. */}
        <input
          id={id}
          type="range"
          min={0}
          max={100}
          step={1}
          value={clamped}
          disabled={disabled}
          onChange={(e) => onChange(Number(e.target.value))}
          aria-label={t('a11y.importanceSlider', { value: clamped })}
          className={cn(
            'absolute inset-0 h-full w-full cursor-pointer appearance-none bg-transparent',
            colors.text,
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-background',
            disabled && 'cursor-not-allowed opacity-50',
            // WebKit thumb
            '[&::-webkit-slider-thumb]:appearance-none',
            '[&::-webkit-slider-thumb]:h-4',
            '[&::-webkit-slider-thumb]:w-4',
            '[&::-webkit-slider-thumb]:rounded-full',
            '[&::-webkit-slider-thumb]:bg-card',
            '[&::-webkit-slider-thumb]:border-2',
            '[&::-webkit-slider-thumb]:border-current',
            '[&::-webkit-slider-thumb]:shadow-peldrun-sm',
            // Firefox thumb
            '[&::-moz-range-thumb]:h-4',
            '[&::-moz-range-thumb]:w-4',
            '[&::-moz-range-thumb]:rounded-full',
            '[&::-moz-range-thumb]:bg-card',
            '[&::-moz-range-thumb]:border-2',
            '[&::-moz-range-thumb]:border-current',
          )}
        />
      </div>
    </div>
  );
}
