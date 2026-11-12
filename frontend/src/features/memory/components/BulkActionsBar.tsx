// src/features/memory/components/BulkActionsBar.tsx

/**
 * Destructive bulk operations for one project.
 *
 * Two actions:
 *
 *   * **Archive all** -- reversible; every active entry becomes
 *     `archived`. Confirm token: `ARCHIVE` (uppercase, easy to
 *     type, unambiguous).
 *
 *   * **Purge all** -- irreversible; every entry is deleted.
 *     Confirm token: the project id itself, matching the spec's
 *     "type the project name" rule (§4.7.3).
 *
 * Both actions require `mode === 'read_write'`; the buttons are
 * disabled with a tooltip when memory is read-only or disabled.
 * The mode gate is a UI hint -- the backend enforces it too
 * (with `errorType: 'mode_read_only'`), so the mutation cannot
 * silently succeed if the gate is bypassed.
 *
 * Confirmation dialogs use the shared `ConfirmDangerDialog`
 * (typed token + explicit danger color). `window.confirm` is
 * forbidden by the spec (§6.2).
 */

'use client';

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Archive, Trash2 } from 'lucide-react';

import { useBulkArchive, useBulkPurge } from '../api/mutations';
import { useMemorySettings } from '../api/queries';
import { cn } from '../lib/cn';
import { ConfirmDangerDialog } from './ConfirmDangerDialog';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * The token a user must type to archive. Not localized --
 * it is an input value, not displayed text.
 */
const ARCHIVE_CONFIRM_TOKEN = 'ARCHIVE';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface BulkActionsBarProps {
  projectId: string;
  /** Total entry count in the project (used to disable when empty). */
  total: number;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function BulkActionsBar({
  projectId,
  total,
  className,
}: BulkActionsBarProps) {
  const { t } = useTranslation('memory');

  const settingsQuery = useMemorySettings();
  const canWrite = settingsQuery.data?.mode === 'read_write';

  const archiveMutation = useBulkArchive();
  const purgeMutation = useBulkPurge();

  const [archiveOpen, setArchiveOpen] = useState(false);
  const [purgeOpen, setPurgeOpen] = useState(false);

  const isEmpty = total === 0;
  const isPending = archiveMutation.isPending || purgeMutation.isPending;

  async function handleArchive() {
    try {
      await archiveMutation.mutateAsync({ projectId });
      setArchiveOpen(false);
    } catch {
      // error surfaced via mutation.error
    }
  }

  async function handlePurge() {
    try {
      await purgeMutation.mutateAsync({ projectId });
      setPurgeOpen(false);
    } catch {
      // error surfaced via mutation.error
    }
  }

  return (
    <>
      <div
        className={cn(
          'flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card p-2.5',
          className,
        )}
      >
        {/* Archive */}
        <button
          type="button"
          onClick={() => setArchiveOpen(true)}
          disabled={isEmpty || !canWrite || isPending}
          title={!canWrite ? t('errors.mode_read_only') : undefined}
          className={cn(
            'inline-flex h-7 items-center gap-1.5 rounded-md border border-border bg-card px-2 text-[11px] font-medium text-foreground',
            'hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
            'disabled:cursor-not-allowed disabled:opacity-50',
          )}
        >
          <Archive size={11} aria-hidden="true" />
          {t('projects.bulk.archiveButton')}
        </button>

        {/* Purge */}
        <button
          type="button"
          onClick={() => setPurgeOpen(true)}
          disabled={isEmpty || !canWrite || isPending}
          title={!canWrite ? t('errors.mode_read_only') : undefined}
          className={cn(
            'inline-flex h-7 items-center gap-1.5 rounded-md border border-destructive/40 bg-destructive/10 px-2 text-[11px] font-medium text-destructive',
            'hover:bg-destructive/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive focus-visible:ring-offset-1',
            'disabled:cursor-not-allowed disabled:opacity-50',
          )}
        >
          <Trash2 size={11} aria-hidden="true" />
          {t('projects.bulk.purgeButton')}
        </button>

        {/* Inline error, if any */}
        {(archiveMutation.error || purgeMutation.error) && (
          <span
            className="text-[10px] text-destructive"
            role="alert"
          >
            {(archiveMutation.error ?? purgeMutation.error)?.message}
          </span>
        )}
      </div>

      {/* ---- Archive confirmation ---- */}
      <ConfirmDangerDialog
        open={archiveOpen}
        onOpenChange={setArchiveOpen}
        title={t('projects.bulk.archiveTitle')}
        description={t('projects.bulk.archiveDescription')}
        confirmToken={ARCHIVE_CONFIRM_TOKEN}
        confirmLabel={t('projects.bulk.archiveButton')}
        isPending={archiveMutation.isPending}
        onConfirm={handleArchive}
      />

      {/* ---- Purge confirmation (type project name) ---- */}
      <ConfirmDangerDialog
        open={purgeOpen}
        onOpenChange={setPurgeOpen}
        title={t('projects.bulk.purgeTitle')}
        description={t('projects.bulk.purgeDescription')}
        confirmToken={projectId}
        confirmPlaceholder={projectId}
        confirmLabel={t('projects.bulk.purgeButton')}
        isPending={purgeMutation.isPending}
        onConfirm={handlePurge}
      />
    </>
  );
}
