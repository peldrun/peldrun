// src/features/memory/components/EmptyState.tsx

/**
 * A centered empty-state block: icon, title, optional
 * description, optional action row.
 *
 * Used by every list / dashboard view that can legitimately
 * have zero items:
 *
 *   * `/memory/browse` -- no memories in the selected scope.
 *   * `/memory/projects` -- no project has any memory.
 *   * `/memory/usage` -- no usage recorded in the period.
 *   * `/memory/import` (preview step) -- file contained zero
 *     candidates.
 *
 * Two layouts
 * -----------
 *
 *   * **`compact`** (default) -- small padding, 40px icon,
 *     `text-sm` title. Fits in a card or in a half-width
 *     column.
 *
 *   * **`spacious`** -- larger padding, 56px icon, `text-lg`
 *     title. For a full-page or full-panel empty state where
 *     the emptiness IS the message.
 *
 * The `action` prop accepts a ReactNode rather than a
 * `{ label, onClick }` pair. Reason: empty-state actions are
 * heterogeneous -- sometimes a button, sometimes a `<Link>`,
 * sometimes a "reload" that needs an icon and a spinner. A
 * plain ReactNode lets the caller compose whatever is right
 * without this component needing to know the shape.
 *
 * Copy is the caller's responsibility. The component accepts
 * already-translated `title` and `description` strings -- it
 * does NOT call `t()` itself. This keeps it usable in
 * contexts where the caller wants to render a different
 * language (e.g. an error message from the backend) without
 * going through i18n.
 */

'use client';

import { type LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';

import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface EmptyStateProps {
  /** The icon to display above the title. Optional. */
  icon?: LucideIcon;

  /** The primary text. Already-translated. */
  title: string;

  /** Optional supporting text. Already-translated. */
  description?: string;

  /** Optional action row rendered beneath the description. */
  action?: ReactNode;

  /** Visual density. Defaults to `'compact'`. */
  size?: 'compact' | 'spacious';

  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  size = 'compact',
  className,
}: EmptyStateProps) {
  const isSpacious = size === 'spacious';
  const iconSize = isSpacious ? 28 : 20;
  const iconBox = isSpacious ? 'h-14 w-14' : 'h-10 w-10';

  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center text-center',
        isSpacious ? 'gap-4 py-16 px-6' : 'gap-3 py-10 px-4',
        className,
      )}
      role="status"
      aria-live="polite"
    >
      {Icon && (
        <div
          aria-hidden="true"
          className={cn(
            'flex items-center justify-center rounded-full bg-muted text-muted-foreground',
            iconBox,
          )}
        >
          <Icon size={iconSize} strokeWidth={1.6} />
        </div>
      )}
      <div className="space-y-1">
        <p
          className={cn(
            'font-medium text-foreground',
            isSpacious ? 'text-lg' : 'text-sm',
          )}
        >
          {title}
        </p>
        {description && (
          <p
            className={cn(
              'text-muted-foreground',
              isSpacious ? 'text-sm max-w-md' : 'text-xs max-w-sm',
            )}
          >
            {description}
          </p>
        )}
      </div>
      {action && (
        <div className="flex flex-wrap items-center justify-center gap-2 pt-1">
          {action}
        </div>
      )}
    </div>
  );
}
