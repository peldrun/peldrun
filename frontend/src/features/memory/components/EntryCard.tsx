// src/features/memory/components/EntryCard.tsx

/**
 * One memory entry, rendered as a card.
 *
 * The card is the primary display surface in every list view
 * (`browse`, `projects/[id]`, `similar memories` panel). It
 * shows:
 *
 *   * A header with the type, scope, importance dot, and
 *     pinned indicator.
 *   * The content, rendered as Markdown (GFM-enabled).
 *   * The tag list (as plain chips, not editable here).
 *   * An actions row (pin, edit, promote, delete) that appears
 *     on hover / focus.
 *
 * Actions
 * -------
 *
 * The card OWNS the pin action -- it uses `usePinEntry`
 * directly, including the optimistic update. Pin is
 * reversible and low-risk (spec §6.1 rule 3), so making it
 * inline-optimistic is correct.
 *
 * Every other action (edit, delete, promote) is a caller-
 * provided callback. Rationale:
 *
 *   * **Edit** navigates to the detail page (or opens a modal)
 *     -- the caller decides.
 *   * **Delete** must go through a confirm dialog (soft vs.
 *     hard) -- the dialog belongs to the caller, which knows
 *     whether this card is in a bulk-selection context.
 *   * **Promote** must go through a "this will copy, not
 *     move" confirmation -- the caller owns that dialog.
 *
 * Markdown rendering
 * ------------------
 *
 * `react-markdown` with `remark-gfm`. The plugin enables
 * tables, task lists, strikethrough, and autolinks -- the
 * GFM features users expect when pasting markdown into a
 * knowledge-base entry. Custom "prose" styling is provided
 * via `[&_*]` arbitrary variants since the project does not
 * include `@tailwindcss/typography`. The rules are inline in
 * the render tree (see `proseClasses`) so their intent is
 * visible at the point of use.
 */

'use client';

import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Copy,
  MoreHorizontal,
  Pencil,
  Pin,
  PinOff,
  Trash2,
} from 'lucide-react';

import type { MemoryEntry } from '../types/entry';
import { usePinEntry } from '../api/mutations';
import { cn } from '../lib/cn';
import { importanceTier } from './ImportanceSlider';
import { ScopeBadge } from './ScopeBadge';
import { TypeBadge } from './TypeBadge';

// ---------------------------------------------------------------------------
// Importance dot
// ---------------------------------------------------------------------------

/**
 * The color of the importance indicator dot, matching the
 * slider tier palette. Rendered at 8px, no label -- the tier
 * name is accessible via `title` and `aria-label`.
 */
const IMPORTANCE_DOT_CLASSES: Record<
  ReturnType<typeof importanceTier>,
  string
> = {
  low: 'bg-slate-400',
  medium: 'bg-amber-500',
  high: 'bg-red-500',
};

// ---------------------------------------------------------------------------
// Markdown prose classes
// ---------------------------------------------------------------------------

/**
 * Tailwind arbitrary-variant rules for Markdown output.
 *
 * These replace what `@tailwindcss/typography`'s `prose` class
 * would provide, tailored to the memory card's small-scale
 * typography. Rendered inside the content wrapper.
 *
 * Notes:
 *   * `ps-*` (padding-inline-start) instead of `pl-*` so lists
 *     indent correctly in RTL.
 *   * `code` and `pre` get `bg-muted` (matches the app's
 *     secondary surface), not a separate "code" theme color.
 *   * Headings are capped at `text-base` -- a memory entry
 *     containing an `# H1` should not render larger than the
 *     card's own title.
 */
const PROSE_CLASSES = [
  // Paragraphs
  '[&_p]:my-1.5',
  '[&_p:first-child]:mt-0',
  '[&_p:last-child]:mb-0',
  // Headings
  '[&_h1]:mt-3 [&_h1]:mb-1.5 [&_h1]:text-base [&_h1]:font-semibold',
  '[&_h2]:mt-3 [&_h2]:mb-1.5 [&_h2]:text-sm [&_h2]:font-semibold',
  '[&_h3]:mt-2 [&_h3]:mb-1 [&_h3]:text-sm [&_h3]:font-semibold',
  // Lists
  '[&_ul]:my-1.5 [&_ul]:list-disc [&_ul]:ps-5',
  '[&_ol]:my-1.5 [&_ol]:list-decimal [&_ol]:ps-5',
  '[&_li]:my-0.5',
  // Links
  '[&_a]:text-primary [&_a]:underline [&_a]:underline-offset-2 hover:[&_a]:text-primary/80',
  // Inline code
  '[&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[0.9em]',
  // Code block (undo the inline-code styles on the nested <code>)
  '[&_pre]:my-2 [&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:bg-muted [&_pre]:p-3 [&_pre]:text-[0.85em]',
  '[&_pre_code]:bg-transparent [&_pre_code]:p-0',
  // Blockquote
  '[&_blockquote]:my-2 [&_blockquote]:border-s-4 [&_blockquote]:border-border [&_blockquote]:ps-3 [&_blockquote]:italic [&_blockquote]:text-muted-foreground',
  // Tables
  '[&_table]:my-2 [&_table]:w-full [&_table]:border-collapse [&_table]:text-[0.9em]',
  '[&_th]:border [&_th]:border-border [&_th]:bg-muted [&_th]:px-2 [&_th]:py-1 [&_th]:text-start [&_th]:font-medium',
  '[&_td]:border [&_td]:border-border [&_td]:px-2 [&_td]:py-1',
  // Horizontal rule
  '[&_hr]:my-3 [&_hr]:border-border',
].join(' ');

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface EntryCardProps {
  /** The entry to render. */
  entry: MemoryEntry;

  /** Called when the user clicks "Edit". */
  onEdit?: () => void;

  /** Called when the user clicks "Delete". */
  onDelete?: () => void;

  /** Called when the user clicks "Promote". */
  onPromote?: () => void;

  /**
   * When `false`, hides the actions row entirely. Used in
   * read-only contexts (search preview panel, embed views).
   * Defaults to `true`.
   */
  showActions?: boolean;

  /**
   * When `false`, does not render the actions row until the
   * user hovers or focuses the card. Defaults to `true` on
   * desktop, effectively true on touch (no hover state, so
   * the row is always visible there via `md:` breakpoint).
   */
  actionsAlwaysVisible?: boolean;

  /** Additional class names on the outer card. */
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function EntryCard({
  entry,
  onEdit,
  onDelete,
  onPromote,
  showActions = true,
  actionsAlwaysVisible = false,
  className,
}: EntryCardProps) {
  const { t } = useTranslation('memory');

  const pinMutation = usePinEntry();

  const tier = importanceTier(entry.importance);
  const showMenu = showActions && (onEdit || onDelete || onPromote);

  function handlePin() {
    pinMutation.mutate({
      memoryId: entry.memory_id,
      scope_type: entry.scope_type,
      scope_id: entry.scope_id,
      pinned: !entry.is_pinned,
    });
  }

  return (
    <article
      className={cn(
        'group relative flex flex-col gap-2 rounded-lg border border-border bg-card p-3 text-foreground shadow-peldrun-sm transition-colors hover:border-border/80',
        className,
      )}
      aria-label={entry.content.slice(0, 80)}
    >
      {/* ---- Header row ------------------------------------- */}
      <header className="flex flex-wrap items-center gap-1.5">
        <TypeBadge memory_type={entry.memory_type} size="sm" />
        <ScopeBadge
          scope_type={entry.scope_type}
          scope_id={
            entry.scope_type === 'user' ? undefined : entry.scope_id
          }
          size="sm"
        />

        {/* Importance dot */}
        <span
          aria-label={t('a11y.importanceDot', {
            value: entry.importance,
            tier: t(`entry.importanceLevels.${tier}`),
          })}
          title={`${entry.importance} / 100`}
          className="inline-flex items-center gap-1 text-[11px] text-muted-foreground"
        >
          <span
            aria-hidden="true"
            className={cn('h-2 w-2 rounded-full', IMPORTANCE_DOT_CLASSES[tier])}
          />
          <span className="tabular-nums">{entry.importance}</span>
        </span>

        {/* Pinned indicator */}
        {entry.is_pinned && (
          <span
            className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
            aria-label={t('a11y.pinnedIndicator')}
          >
            <Pin size={9} strokeWidth={2.5} aria-hidden="true" />
            {t('entry.fields.pinned')}
          </span>
        )}

        {/* Status pill for non-active entries */}
        {entry.status !== 'active' && (
          <span className="inline-flex items-center rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
            {t(`status.${entry.status}`)}
          </span>
        )}
      </header>

      {/* ---- Content ---------------------------------------- */}
      <div
        className={cn(
          'text-sm leading-relaxed text-foreground',
          PROSE_CLASSES,
        )}
      >
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {entry.content}
        </ReactMarkdown>
      </div>

      {/* ---- Tags ------------------------------------------- */}
      {entry.tags.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {entry.tags.map((tag) => (
            <span
              key={tag}
              className="inline-flex items-center rounded-full bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
            >
              {tag}
            </span>
          ))}
        </div>
      )}

      {/* ---- Actions ---------------------------------------- */}
      {showMenu && (
        <footer
          className={cn(
            'mt-1 flex items-center gap-1 border-t border-border pt-2',
            actionsAlwaysVisible
              ? 'opacity-100'
              : 'opacity-100 md:opacity-0 md:transition-opacity md:group-hover:opacity-100 md:focus-within:opacity-100',
          )}
        >
          <ActionButton
            icon={entry.is_pinned ? PinOff : Pin}
            label={
              entry.is_pinned
                ? t('entry.actions.unpin')
                : t('entry.actions.pin')
            }
            onClick={handlePin}
            disabled={pinMutation.isPending}
            active={entry.is_pinned}
          />
          {onEdit && (
            <ActionButton
              icon={Pencil}
              label={t('entry.actions.edit')}
              onClick={onEdit}
            />
          )}
          {onPromote && (
            <ActionButton
              icon={Copy}
              label={t('entry.actions.promote')}
              onClick={onPromote}
            />
          )}
          {onDelete && (
            <ActionButton
              icon={Trash2}
              label={t('entry.actions.delete')}
              onClick={onDelete}
              danger
            />
          )}

          <span className="ms-auto text-[10px] text-muted-foreground tabular-nums">
            {entry.memory_id.slice(-6)}
          </span>

          {/* Trailing "more" affordance for future menu. Hidden
              for now; kept in the markup so the layout is
              stable when the menu is added. */}
          {false && (
            <span
              aria-hidden="true"
              className="inline-flex h-6 w-6 items-center justify-center text-muted-foreground"
            >
              <MoreHorizontal size={14} />
            </span>
          )}
        </footer>
      )}
    </article>
  );
}

// ---------------------------------------------------------------------------
// Action button
// ---------------------------------------------------------------------------

interface ActionButtonProps {
  icon: React.ComponentType<{ size?: number; strokeWidth?: number }>;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  active?: boolean;
  danger?: boolean;
}

function ActionButton({
  icon: Icon,
  label,
  onClick,
  disabled = false,
  active = false,
  danger = false,
}: ActionButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      aria-pressed={active || undefined}
      title={label}
      className={cn(
        'inline-flex h-6 items-center gap-1 rounded-md px-1.5 text-[11px] font-medium transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1 focus-visible:ring-offset-card',
        'disabled:cursor-not-allowed disabled:opacity-50',
        danger
          ? 'text-destructive hover:bg-destructive/10'
          : active
            ? 'text-amber-700 hover:bg-amber-100/60 dark:text-amber-400 dark:hover:bg-amber-900/20'
            : 'text-muted-foreground hover:bg-muted hover:text-foreground',
      )}
    >
      <Icon size={12} strokeWidth={2} />
    </button>
  );
}
