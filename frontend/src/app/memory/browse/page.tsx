// src/app/memory/browse/page.tsx

/**
 * The browse page (`/memory/browse`) -- the primary entry list.
 *
 * Three-column layout on desktop:
 *
 *   * Left: scope sidebar (user + projects that have memory).
 *   * Center: filter bar, list, and "Load more".
 *   * Right: an always-visible "New memory" button.
 *
 * On mobile, the sidebar collapses above the list as a
 * horizontal scope picker -- the same component, restyled.
 *
 * URL contract
 * ------------
 *
 * The `?scope=<type>:<id>` query parameter drives the whole
 * page. It is read on mount to seed the store, and it is
 * rewritten (via `router.replace`) whenever the user picks a
 * different scope. This makes links shareable and preserves
 * the user's scope across a page reload. The other filters
 * (status / type / pinned) live ONLY in the Zustand store --
 * they are not URL-synced, because they are transient view
 * preferences, not addressing information.
 *
 * Pagination
 * ----------
 *
 * Offset-based, "Load more" style. The page accumulates
 * entries in local state rather than using infinite scroll:
 *
 *   1. Offset 0 returns the first page -> replace `entries`.
 *   2. Offset 50 (after "Load more") -> append, de-duped.
 *   3. Changing scope or any filter resets offset to 0 and
 *      replaces `entries` with the first page.
 *
 * The append is guarded by checking `response.offset ===
 * currentOffset` -- a stale response from a page that was
 * cancelled by a filter change is ignored rather than
 * duplicating rows.
 *
 * Why `useQuery` directly, not `useEntryList`
 * -------------------------------------------
 *
 * The wrapper in `queries.ts` does not pass through
 * `placeholderData` / `keepPreviousData`, and pagination
 * NEEDS those: without them, moving to offset 50 makes
 * `data` undefined for a tick, which would flash the
 * empty state before the new page arrives. The direct
 * `useQuery` call lets us keep the previous page visible
 * while the next loads.
 */

'use client';

import { useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import {
  Brain,
  Plus,
  User,
  FolderOpen,
  AlertTriangle,
  type LucideIcon,
} from 'lucide-react';

import type { MemoryEntry, MemoryScopeRef } from '../../../features/memory/types/entry';
import type { MemoryApiError } from '../../../features/memory/types/errors';
import {
  MemoryScope,
  MemoryStatusFilter,
} from '../../../features/memory/types/enums';
import {
  memoryClient,
} from '../../../features/memory/api/client';
import {
  memoryKeys,
} from '../../../features/memory/api/keys';
import {
  useProjectsList,
  useMemorySettings,
} from '../../../features/memory/api/queries';
import {
  useDeleteEntry,
  usePromoteEntry,
} from '../../../features/memory/api/mutations';
import {
  DEFAULT_PAGE_SIZE,
  HARD_DELETE_CONFIRM_TOKEN,
} from '../../../features/memory/lib/constants';
import {
  DEFAULT_FILTERS,
  useMemoryUIStore,
} from '../../../features/memory/stores/memory-ui-store';
import { cn } from '../../../features/memory/lib/cn';
import { EntryFilters } from '../../../features/memory/components/EntryFilters';
import { EntryList } from '../../../features/memory/components/EntryList';
import { EntryEditor } from '../../../features/memory/components/EntryEditor';
import { ConfirmDangerDialog } from '../../../features/memory/components/ConfirmDangerDialog';
import { EmptyState } from '../../../features/memory/components/EmptyState';
import { ScopeBadge } from '../../../features/memory/components/ScopeBadge';

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function MemoryBrowsePage() {
  const { t } = useTranslation('memory');
  const router = useRouter();
  const searchParams = useSearchParams();

  // ---- Zustand: filters + active scope ---------------------------
  const filters = useMemoryUIStore((s) => s.filters);
  const activeScope = useMemoryUIStore((s) => s.active_scope);
  const setActiveScope = useMemoryUIStore((s) => s.setActiveScope);

  // ---- URL -> store sync on mount --------------------------------
  //
  // We read the URL param ONCE on mount. Subsequent changes go
  // the other way (store -> URL) so that the user's clicks
  // remain the source of truth. This prevents a feedback loop
  // where URL and store overwrite each other.
  useEffect(() => {
    const scopeParam = searchParams?.get('scope');
    if (scopeParam) {
      useMemoryUIStore.getState().setActiveScopeByString(scopeParam);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- Projects list (for the scope sidebar) ---------------------
  const projectsQuery = useProjectsList();
  const settingsQuery = useMemorySettings();
  const canWrite = settingsQuery.data?.mode === 'read_write';
  const canRead = settingsQuery.data?.mode !== 'disabled';

  // ---- Pagination state ------------------------------------------
  const [offset, setOffset] = useState(0);
  const [entries, setEntries] = useState<MemoryEntry[]>([]);

  const scopeRef: MemoryScopeRef | null = activeScope;
  const scopeString = scopeRef
    ? `${scopeRef.scope_type}:${scopeRef.scope_id}`
    : '';

  // Reset pagination when scope or filters change.
  useEffect(() => {
    setOffset(0);
    setEntries([]);
  }, [
    scopeString,
    filters.status,
    filters.memory_type,
    filters.is_pinned,
  ]);

  // ---- The list query --------------------------------------------
  const listQuery = useQuery<
    Awaited<ReturnType<typeof memoryClient.entries.list>>,
    MemoryApiError
  >({
    queryKey: memoryKeys.entries.list({
      scope: scopeString,
      status: filters.status,
      memory_type: filters.memory_type,
      is_pinned: filters.is_pinned,
      limit: DEFAULT_PAGE_SIZE,
      offset,
    }),
    queryFn: async ({ signal }) => {
      const [scopeType, ...rest] = scopeString.split(':');
      const scopeId = rest.join(':');
      return memoryClient.entries.list(
        {
          scope_type: scopeType,
          scope_id: scopeId,
          limit: DEFAULT_PAGE_SIZE,
          offset,
          status: filters.status,
          memory_type: filters.memory_type,
          is_pinned: filters.is_pinned,
        },
        { signal },
      );
    },
    enabled: canRead && scopeRef !== null && scopeString.length > 0,
    staleTime: 10_000,
    placeholderData: keepPreviousData,
  });

  // ---- Accumulate pages ------------------------------------------
  //
  // Guarded by `response.offset === currentOffset` -- a late
  // response from an abandoned page is ignored rather than
  // duplicating entries.
  useEffect(() => {
    const response = listQuery.data;
    if (!response) return;
    if (response.offset !== offset) return;

    if (offset === 0) {
      setEntries(response.memories);
      return;
    }

    setEntries((prev) => {
      const existing = new Set(prev.map((e) => e.memory_id));
      const fresh = response.memories.filter(
        (e) => !existing.has(e.memory_id),
      );
      return fresh.length === 0 ? prev : [...prev, ...fresh];
    });
  }, [listQuery.data, offset]);

  // ---- Derived flags ---------------------------------------------
  const hasMore =
    (listQuery.data?.count ?? 0) === DEFAULT_PAGE_SIZE &&
    entries.length > 0 &&
    entries.length === offset + (listQuery.data?.count ?? 0);

  const isLoading = listQuery.isPending && !listQuery.data;
  const isLoadingMore = listQuery.isFetching && offset > 0;
  const error = listQuery.error;

  // ---- Dialogs ---------------------------------------------------
  const [editorOpen, setEditorOpen] = useState(false);
  const [editingEntry, setEditingEntry] = useState<MemoryEntry | undefined>();
  const [deletingEntry, setDeletingEntry] = useState<MemoryEntry | null>(null);
  const [promotingEntry, setPromotingEntry] = useState<MemoryEntry | null>(
    null,
  );

  const deleteMutation = useDeleteEntry();
  const promoteMutation = usePromoteEntry();

  // ---- Scope switching ------------------------------------------
  function pickScope(ref: MemoryScopeRef) {
    setActiveScope(ref);
    const params = new URLSearchParams(searchParams?.toString() ?? '');
    params.set('scope', `${ref.scope_type}:${ref.scope_id}`);
    router.replace(`/memory/browse?${params.toString()}`);
  }

  // ---- Handlers --------------------------------------------------
  function handleEdit(entry: MemoryEntry) {
    setEditingEntry(entry);
    setEditorOpen(true);
  }

  function handleCreate() {
    setEditingEntry(undefined);
    setEditorOpen(true);
  }

  async function handleDeleteConfirm() {
    if (!deletingEntry) return;
    try {
      await deleteMutation.mutateAsync({
        memoryId: deletingEntry.memory_id,
        scope_type: deletingEntry.scope_type,
        scope_id: deletingEntry.scope_id,
        hard: true,
      });
      // Remove from local list immediately (the mutation
      // invalidates server queries; our local `entries` is
      // independent and needs an explicit drop).
      setEntries((prev) =>
        prev.filter((e) => e.memory_id !== deletingEntry.memory_id),
      );
      setDeletingEntry(null);
    } catch {
      // error surfaced in the dialog
    }
  }

  async function handlePromoteConfirm() {
    if (!promotingEntry) return;
    try {
      await promoteMutation.mutateAsync({
        params: {
          memoryId: promotingEntry.memory_id,
          scope_type: promotingEntry.scope_type,
          scope_id: promotingEntry.scope_id,
        },
        body: { target_scope_type: 'user', target_scope_id: 'default' },
      });
      setPromotingEntry(null);
    } catch {
      // error surfaced in the dialog
    }
  }

  // ---- Loading/skeleton scope picker -----------------------------
  const projectScopes: MemoryScopeRef[] = useMemo(
    () =>
      (projectsQuery.data?.projects ?? []).map((p) => ({
        scope_type: MemoryScope.PROJECT,
        scope_id: p.project_id,
      })),
    [projectsQuery.data?.projects],
  );

  return (
    <div className="mx-auto grid max-w-6xl grid-cols-1 gap-4 md:grid-cols-[14rem_1fr]">
      {/* ============ Sidebar ============ */}
      <aside className="space-y-3">
        <section>
          <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t('browse.scopeSidebar.personal')}
          </h3>
          <ScopeButton
            active={
              activeScope?.scope_type === MemoryScope.USER &&
              activeScope.scope_id === 'default'
            }
            onClick={() =>
              pickScope({
                scope_type: MemoryScope.USER,
                scope_id: 'default',
              })
            }
            icon={User}
            label={t('scope.userDefault')}
          />
        </section>

        <section>
          <h3 className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t('browse.scopeSidebar.projects')}
          </h3>
          {projectsQuery.isLoading ? (
            <div className="space-y-1" aria-busy="true">
              {[0, 1, 2].map((i) => (
                <div
                  key={i}
                  className="h-7 animate-pulse rounded-md bg-muted"
                />
              ))}
            </div>
          ) : projectScopes.length === 0 ? (
            <p className="text-[11px] text-muted-foreground">
              {t('projects.empty.title')}
            </p>
          ) : (
            <div className="space-y-0.5">
              {projectScopes.map((ref) => (
                <ScopeButton
                  key={ref.scope_id}
                  active={
                    activeScope?.scope_type === ref.scope_type &&
                    activeScope.scope_id === ref.scope_id
                  }
                  onClick={() => pickScope(ref)}
                  icon={FolderOpen}
                  label={ref.scope_id}
                />
              ))}
            </div>
          )}
        </section>
      </aside>

      {/* ============ Main column ============ */}
      <div className="space-y-3">
        {/* Header */}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold text-foreground">
              {t('browse.title')}
            </h2>
            {activeScope && (
              <ScopeBadge
                scope_type={activeScope.scope_type}
                scope_id={
                  activeScope.scope_type === 'user'
                    ? undefined
                    : activeScope.scope_id
                }
                size="xs"
              />
            )}
          </div>
          <button
            type="button"
            onClick={handleCreate}
            disabled={!canWrite || !activeScope}
            title={!canWrite ? t('errors.mode_read_only') : undefined}
            className={cn(
              'inline-flex h-8 items-center gap-1.5 rounded-md bg-primary px-3 text-xs font-medium text-primary-foreground',
              'hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
              'disabled:cursor-not-allowed disabled:opacity-60',
            )}
          >
            <Plus size={12} aria-hidden="true" />
            {t('browse.empty.action')}
          </button>
        </div>

        {/* Disabled notice */}
        {!canRead && (
          <div className="flex items-center gap-2 rounded-md border border-amber-300/60 bg-amber-50/60 p-2 text-[11px] dark:border-amber-700/60 dark:bg-amber-900/10">
            <AlertTriangle
              size={11}
              aria-hidden="true"
              className="text-amber-700 dark:text-amber-400"
            />
            <span className="text-amber-800 dark:text-amber-300">
              {t('errors.mode_disabled')}{' '}
              <Link
                href="/memory/settings"
                className="underline"
              >
                {t('errors.withSettings')}
              </Link>
            </span>
          </div>
        )}

        {/* Filters */}
        {activeScope && (
          <EntryFilters orientation="horizontal" hideClear={false} />
        )}

        {/* Content */}
        {!activeScope ? (
          <EmptyState
            icon={Brain}
            title={t('browse.empty.title')}
            description={t('scope.selectScope')}
            size="spacious"
          />
        ) : (
          <EntryList
            entries={entries}
            isLoading={isLoading}
            isLoadingMore={isLoadingMore}
            error={error}
            hasMore={hasMore}
            onLoadMore={() => setOffset((o) => o + DEFAULT_PAGE_SIZE)}
            onRetry={() => listQuery.refetch()}
            onEdit={
              canWrite
                ? (entry) => handleEdit(entry)
                : undefined
            }
            onDelete={
              canWrite
                ? (entry) => setDeletingEntry(entry)
                : undefined
            }
            onPromote={
              canWrite
                ? (entry) => setPromotingEntry(entry)
                : undefined
            }
          />
        )}
      </div>

      {/* ============ Dialogs ============ */}
      {activeScope && (
        <EntryEditor
          open={editorOpen}
          onOpenChange={(open) => {
            setEditorOpen(open);
            if (!open) setEditingEntry(undefined);
          }}
          mode={editingEntry ? 'edit' : 'create'}
          entry={editingEntry}
          scope={
            editingEntry
              ? {
                  scope_type: editingEntry.scope_type,
                  scope_id: editingEntry.scope_id,
                }
              : activeScope
          }
          onSaved={() => {
            // The mutation already invalidated the queries.
            // Our local `entries` is independent -- refresh
            // the first page to pick up the new/updated row
            // without disturbing the current scroll.
            if (offset === 0) {
              listQuery.refetch();
            } else {
              setOffset(0);
            }
          }}
        />
      )}

      <ConfirmDangerDialog
        open={deletingEntry !== null}
        onOpenChange={(open) => {
          if (!open) setDeletingEntry(null);
        }}
        title={t('entry.delete.hardTitle')}
        description={t('entry.delete.hardDescription')}
        confirmToken={HARD_DELETE_CONFIRM_TOKEN}
        confirmPlaceholder={t('entry.delete.hardConfirmPlaceholder')}
        confirmLabel={t('common.delete')}
        isPending={deleteMutation.isPending}
        onConfirm={handleDeleteConfirm}
      />

      <ConfirmDangerDialog
        open={promotingEntry !== null}
        onOpenChange={(open) => {
          if (!open) setPromotingEntry(null);
        }}
        title={t('entry.promote.title')}
        description={t('entry.promote.description')}
        confirmToken={promotingEntry?.scope_id ?? ''}
        confirmLabel={t('entry.actions.promote')}
        isPending={promoteMutation.isPending}
        onConfirm={handlePromoteConfirm}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Scope button
// ---------------------------------------------------------------------------

interface ScopeButtonProps {
  active: boolean;
  onClick: () => void;
  icon: LucideIcon;
  label: string;
}

function ScopeButton({
  active,
  onClick,
  icon: Icon,
  label,
}: ScopeButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? 'true' : undefined}
      className={cn(
        'flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-start text-[11px] transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
        active
          ? 'bg-primary/10 font-medium text-primary'
          : 'text-muted-foreground hover:bg-muted hover:text-foreground',
      )}
    >
      <Icon size={11} aria-hidden="true" className="shrink-0" />
      <span className="truncate font-mono">{label}</span>
    </button>
  );
}
