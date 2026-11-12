// src/features/memory/components/ImportPreviewTable.tsx

/**
 * The import wizard's "preview" step: candidate table + warnings.
 *
 * Renders the output of `POST /api/memory/import/preview`:
 *
 *   * A summary line: "Will create N, skip M".
 *   * A collapsible warnings panel (each warning is a string
 *     the backend produced; some indicate security rejections
 *     that are easy to miss if the panel defaults to closed).
 *   * A table of candidates with per-row status badges:
 *       - "create" (will be created)
 *       - "skip"   (duplicate, will be skipped)
 *       - "error"  (was rejected by validation; not in
 *                   `candidates` but appeared in warnings)
 *
 * The wizard's Confirm button lives OUTSIDE this component
 * (in the parent page), so that this table stays purely
 * presentational and can be tested without a mutation context.
 */

'use client';

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  SkipForward,
} from 'lucide-react';

import type { ImportPreview } from '../types/responses';
import type { MemoryEntry } from '../types/entry';
import type { MemoryEntry as MemoryEntryT } from '../types/entry';
import { TypeBadge } from './TypeBadge';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ImportPreviewTableProps {
  preview: ImportPreview;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ImportPreviewTable({
  preview,
  className,
}: ImportPreviewTableProps) {
  const { t } = useTranslation('memory');
  const [warningsOpen, setWarningsOpen] = useState(true);

  const { to_create, to_skip, warnings } = preview;
  const candidates = preview.candidates;

  // We do not know which candidates will be skipped (the backend
  // computes that against the current scope at apply time, and
  // the preview's `to_skip` is a scalar). So we render every
  // candidate as "candidate" without a per-row skip status --
  // the summary line is the authoritative count.
  //
  // The one exception is a row whose content is empty: those
  // are guaranteed to be dropped. We mark them inline so the
  // user can see WHY a candidate disappeared from the eventual
  // "created" count.

  return (
    <div className={cn('space-y-3', className)}>
      {/* ---- Summary -------------------------------------- */}
      <div className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-card p-2.5 text-xs">
        <span className="inline-flex items-center gap-1.5 text-emerald-600 dark:text-emerald-400">
          <CheckCircle2 size={12} aria-hidden="true" />
          <span className="font-medium tabular-nums">{to_create}</span>
          <span className="text-muted-foreground">
            {t('import.preview.summary', {
              create: to_create,
              skip: to_skip,
            })}
          </span>
        </span>
      </div>

      {/* ---- Warnings panel ------------------------------- */}
      {warnings.length > 0 && (
        <div className="rounded-md border border-amber-300/60 bg-amber-50/60 dark:border-amber-700/60 dark:bg-amber-900/10">
          <button
            type="button"
            onClick={() => setWarningsOpen((v) => !v)}
            className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-start text-[11px] font-medium text-amber-800 dark:text-amber-300"
            aria-expanded={warningsOpen}
          >
            {warningsOpen ? (
              <ChevronDown size={11} aria-hidden="true" />
            ) : (
              <ChevronRight size={11} aria-hidden="true" />
            )}
            <AlertTriangle size={11} aria-hidden="true" />
            {t('import.preview.warnings', { count: warnings.length })}
          </button>
          {warningsOpen && (
            <ul className="space-y-0.5 border-t border-amber-300/40 px-2.5 py-1.5 dark:border-amber-700/40">
              {warnings.map((w, i) => (
                <li
                  key={i}
                  className="text-[10px] leading-snug text-amber-900/90 dark:text-amber-300/90"
                >
                  · {w}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* ---- Candidate list ------------------------------- */}
      {candidates.length === 0 ? (
        <p className="rounded-md border border-border bg-card p-3 text-center text-xs text-muted-foreground">
          {t('import.preview.noCandidates')}
        </p>
      ) : (
        <ul className="space-y-1" aria-label={t('import.preview.candidates')}>
          {candidates.map((entry, idx) => (
            <CandidateRow key={`${entry.memory_id}-${idx}`} entry={entry} idx={idx + 1} />
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// One candidate
// ---------------------------------------------------------------------------

interface CandidateRowProps {
  entry: MemoryEntry | MemoryEntryT;
  idx: number;
}

function CandidateRow({ entry, idx }: CandidateRowProps) {
  const { t } = useTranslation('memory');
  const empty = entry.content.trim().length === 0;

  return (
    <li
      className={cn(
        'flex items-start gap-2 rounded-md border bg-card px-2 py-1.5 text-xs',
        empty
          ? 'border-destructive/40 bg-destructive/5'
          : 'border-border',
      )}
    >
      <span
        className={cn(
          'mt-0.5 shrink-0 tabular-nums text-[10px]',
          empty ? 'text-destructive' : 'text-muted-foreground',
        )}
      >
        {empty ? (
          <AlertTriangle size={11} aria-hidden="true" />
        ) : (
          <CheckCircle2 size={11} aria-hidden="true" className="text-emerald-500" />
        )}
      </span>
      <span className="shrink-0 tabular-nums text-[10px] text-muted-foreground">
        #{idx}
      </span>
      <TypeBadge memory_type={entry.memory_type} size="xs" />
      <span
        className="min-w-0 flex-1 truncate text-foreground"
        title={entry.content}
      >
        {empty
          ? t('import.preview.noCandidates')
          : entry.content.slice(0, 140)}
      </span>
      {entry.tags.length > 0 && (
        <span className="hidden shrink-0 items-center gap-1 md:inline-flex">
          <SkipForward size={9} aria-hidden="true" className="text-muted-foreground" />
          <span className="text-[10px] text-muted-foreground">
            {entry.tags.slice(0, 2).join(', ')}
          </span>
        </span>
      )}
    </li>
  );
}
