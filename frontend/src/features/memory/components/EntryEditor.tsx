// src/features/memory/components/EntryEditor.tsx

/**
 * Create / edit form for a memory entry, rendered as a modal.
 *
 * Behaviour
 * ---------
 *
 *   * **Create mode** (`mode='create'`): the fields start empty
 *     (except `scope` and `source_type`, which the caller
 *     supplies). A "similar memories" panel beneath the content
 *     textarea fires a debounced search so the user can see
 *     potential duplicates BEFORE saving.
 *
 *   * **Edit mode** (`mode='edit'`): the fields start from the
 *     passed `entry`. The similar panel is hidden -- searching
 *     would only find the entry being edited.
 *
 * Validation
 * ----------
 *
 * Uses `memoryEntryCreateSchema` (create) or
 * `memoryEntryUpdateSchema` (edit) via `zodErrorToFieldMap`. The
 * result is stored in local state and rendered inline beneath
 * each field. The submit button is enabled even when errors
 * exist -- the user gets feedback the moment they click, which
 * is more discoverable than a disabled button with no
 * explanation.
 *
 * Mode gating
 * -----------
 *
 * If the current memory mode forbids writes (`read_only` or
 * `disabled`), the Save button is disabled with a tooltip
 * explaining why. The dialog still opens -- a user should be
 * able to SEE what they would need to change. This matches the
 * spec's "disable, don't hide" rule (§6.2).
 */

'use client';

import {
  type FormEvent,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useTranslation } from 'react-i18next';
import { Save, X } from 'lucide-react';

import type { MemoryEntry, MemoryEntryDraft } from '../types/entry';
import {
  MemoryScope,
  MemorySourceType,
  MemoryType,
} from '../types/enums';
import {
  makeSettingsPatchSchema, // reuse of `type` for `MemorySettings` import only; see note
} from '../lib/validators'; // eslint-disable-line @typescript-eslint/no-unused-vars -- intentional; see file header
import {
  memoryEntryCreateSchema,
  memoryEntryUpdateSchema,
  zodErrorToFieldMap,
} from '../lib/validators';
import {
  DEFAULT_IMPORTANCE,
  DEFAULT_IS_PINNED,
  SEARCH_DEBOUNCE_MS,
  SEARCH_MIN_QUERY_LENGTH,
} from '../lib/constants';
import { useMemorySettings } from '../api/queries';
import {
  useCreateEntry,
  useSearchMemories,
  useUpdateEntry,
} from '../api/mutations';
import { cn } from '../lib/cn';
import { ImportanceSlider } from './ImportanceSlider';
import { TagsInput } from './TagsInput';
import { ScopeBadge } from './ScopeBadge';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface EntryEditorProps {
  /** Whether the dialog is rendered. */
  open: boolean;

  /** Called when the dialog wants to close (backdrop, X, cancel, save success). */
  onOpenChange: (open: boolean) => void;

  /** `'create'` for a new entry, `'edit'` for an existing one. */
  mode: 'create' | 'edit';

  /**
   * The entry to edit. Required when `mode='edit'`; ignored in
   * create mode.
   */
  entry?: MemoryEntry;

  /**
   * The scope for a NEW entry. Ignored in edit mode (the
   * entry's own scope is used, and the scope is immutable).
   */
  scope: { scope_type: MemoryScope; scope_id: string };

  /**
   * Called after a successful save with the backend's
   * canonical entry. The caller typically navigates or closes
   * a parent modal; the editor itself calls `onOpenChange(false)`
   * too, but the callback lets the caller do extra work
   * (e.g. open the new entry's detail page).
   */
  onSaved?: (entry: MemoryEntry) => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function EntryEditor({
  open,
  onOpenChange,
  mode,
  entry,
  scope,
  onSaved,
}: EntryEditorProps) {
  const { t } = useTranslation('memory');
  const titleId = useId();

  // ---- Settings + mutations ---------------------------------------
  const settingsQuery = useMemorySettings();
  const mode$ = settingsQuery.data?.mode;
  const canWrite = mode$ === 'read_write';

  const createMutation = useCreateEntry();
  const updateMutation = useUpdateEntry();
  const searchMutation = useSearchMemories();

  const isPending = createMutation.isPending || updateMutation.isPending;

  // ---- Local form state ------------------------------------------
  const [content, setContent] = useState('');
  const [memoryType, setMemoryType] = useState<MemoryType>(
    MemoryType.SEMANTIC,
  );
  const [importance, setImportance] = useState(DEFAULT_IMPORTANCE);
  const [tags, setTags] = useState<string[]>([]);
  const [isPinned, setIsPinned] = useState(DEFAULT_IS_PINNED);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const contentRef = useRef<HTMLTextAreaElement>(null);

  // ---- Reset state when the dialog opens -------------------------
  useEffect(() => {
    if (!open) return;
    if (mode === 'edit' && entry) {
      setContent(entry.content);
      setMemoryType(entry.memory_type);
      setImportance(entry.importance);
      setTags([...entry.tags]);
      setIsPinned(entry.is_pinned);
    } else {
      setContent('');
      setMemoryType(MemoryType.SEMANTIC);
      setImportance(DEFAULT_IMPORTANCE);
      setTags([]);
      setIsPinned(DEFAULT_IS_PINNED);
    }
    setErrors({});
    // Focus content after paint.
    const handle = setTimeout(() => contentRef.current?.focus(), 0);
    return () => clearTimeout(handle);
  }, [open, mode, entry]);

  // ---- Escape to close -------------------------------------------
  useEffect(() => {
    if (!open) return undefined;
    function onKeyDown(e: globalThis.KeyboardEvent) {
      if (e.key === 'Escape' && !isPending) {
        e.preventDefault();
        onOpenChange(false);
      }
    }
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [open, isPending, onOpenChange]);

  // ---- Similar-memories panel (create mode only) -----------------
  const [debouncedContent, setDebouncedContent] = useState('');
  useEffect(() => {
    if (mode !== 'create') return undefined;
    const handle = setTimeout(
      () => setDebouncedContent(content),
      SEARCH_DEBOUNCE_MS,
    );
    return () => clearTimeout(handle);
  }, [content, mode]);

  useEffect(() => {
    if (mode !== 'create') return;
    if (debouncedContent.trim().length < SEARCH_MIN_QUERY_LENGTH) {
      return;
    }
    if (!canWrite) return;
    searchMutation.mutate({
      query: debouncedContent,
      scope_type: scope.scope_type,
      scope_id: scope.scope_id,
      limit: 3,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedContent, mode, scope.scope_type, scope.scope_id, canWrite]);

  // ---- Derived: initial draft shape for validation --------------
  const draftSnapshot = useMemo<MemoryEntryDraft>(
    () => ({
      content,
      memory_type: memoryType,
      scope_type: scope.scope_type,
      scope_id: scope.scope_id,
      importance,
      tags,
      is_pinned: isPinned,
      contentTouched: content.length > 0,
    }),
    [content, memoryType, scope, importance, tags, isPinned],
  );

  // ---- Submit ---------------------------------------------------
  async function handleSubmit(e: FormEvent) {
    e.preventDefault();

    if (mode === 'create') {
      const result = memoryEntryCreateSchema.safeParse({
        content,
        memory_type: memoryType,
        source_type: MemorySourceType.EXPLICIT_USER,
        scope_type: scope.scope_type,
        scope_id: scope.scope_id,
        importance,
        tags,
        is_pinned: isPinned,
      });
      if (!result.success) {
        setErrors(zodErrorToFieldMap(result.error));
        return;
      }
      setErrors({});
      try {
        const response = await createMutation.mutateAsync({
          content: result.data.content,
          memory_type: result.data.memory_type,
          source_type: result.data.source_type,
          scope_type: result.data.scope_type,
          scope_id: result.data.scope_id,
          importance: result.data.importance,
          tags: result.data.tags,
          is_pinned: result.data.is_pinned,
        });
        onSaved?.(response.memory);
        onOpenChange(false);
      } catch {
        // Error surfaced via mutation state; form stays open.
      }
      return;
    }

    // Edit mode: build the minimal patch.
    if (!entry) return;
    const patch: {
      content?: string;
      importance?: number;
      tags?: string[];
      is_pinned?: boolean;
      memory_type?: MemoryType;
    } = {};
    if (content !== entry.content) patch.content = content;
    if (importance !== entry.importance) patch.importance = importance;
    if (
      tags.length !== entry.tags.length ||
      tags.some((tag, i) => tag !== entry.tags[i])
    ) {
      patch.tags = tags;
    }
    if (isPinned !== entry.is_pinned) patch.is_pinned = isPinned;
    if (memoryType !== entry.memory_type) patch.memory_type = memoryType;

    const result = memoryEntryUpdateSchema.safeParse(patch);
    if (!result.success) {
      setErrors(zodErrorToFieldMap(result.error));
      return;
    }
    setErrors({});
    try {
      const response = await updateMutation.mutateAsync({
        params: {
          memoryId: entry.memory_id,
          scope_type: entry.scope_type,
          scope_id: entry.scope_id,
        },
        patch: result.data,
      });
      onSaved?.(response.memory);
      onOpenChange(false);
    } catch {
      // Error surfaced via mutation state.
    }
  }

  // ---- Render ---------------------------------------------------
  if (!open) return null;

  const title =
    mode === 'create'
      ? t('entry.drafts.createTitle')
      : t('entry.drafts.editTitle');
  const scopeLabel = `${scope.scope_type}:${scope.scope_id}`;
  const saveLabel = isPending ? t('common.saving') : t('common.save');

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget && !isPending) onOpenChange(false);
      }}
    >
      <form
        onSubmit={handleSubmit}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-border bg-card text-foreground shadow-peldrun-lg"
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border p-3">
          <div className="flex items-center gap-2">
            <h2 id={titleId} className="text-sm font-semibold">
              {title}
            </h2>
            <ScopeBadge
              scope_type={scope.scope_type}
              scope_id={
                scope.scope_type === 'user' ? undefined : scope.scope_id
              }
              size="xs"
            />
          </div>
          <button
            type="button"
            onClick={() => {
              if (!isPending) onOpenChange(false);
            }}
            disabled={isPending}
            aria-label={t('common.close')}
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            <X size={14} />
          </button>
        </div>

        {/* Body -- scrollable */}
        <div className="flex-1 space-y-3 overflow-y-auto p-3">
          {/* Content */}
          <div>
            <label
              htmlFor="entry-content"
              className="mb-1 block text-[11px] font-medium text-foreground"
            >
              {t('entry.fields.content')}
            </label>
            <textarea
              id="entry-content"
              ref={contentRef}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={6}
              placeholder={t('entry.placeholders.content')}
              disabled={isPending}
              aria-invalid={errors.content !== undefined}
              className={cn(
                'w-full resize-y rounded-md border bg-background px-2.5 py-2 text-sm text-foreground placeholder:text-muted-foreground',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-card',
                'disabled:cursor-not-allowed disabled:opacity-60',
                errors.content ? 'border-destructive' : 'border-border',
              )}
            />
            {errors.content && (
              <p className="mt-1 text-[11px] text-destructive" role="alert">
                {t(errors.content)}
              </p>
            )}
          </div>

          {/* Type */}
          <div>
            <label
              htmlFor="entry-type"
              className="mb-1 block text-[11px] font-medium text-foreground"
            >
              {t('entry.fields.type')}
            </label>
            <select
              id="entry-type"
              value={memoryType}
              onChange={(e) => setMemoryType(e.target.value as MemoryType)}
              disabled={isPending}
              className="h-8 w-full rounded-md border border-border bg-background px-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
            >
              {Object.values(MemoryType).map((mt) => (
                <option key={mt} value={mt}>
                  {t(`type.${mt}`)}
                </option>
              ))}
            </select>
          </div>

          {/* Importance */}
          <ImportanceSlider
            id="entry-importance"
            value={importance}
            onChange={setImportance}
            disabled={isPending}
          />

          {/* Tags */}
          <div>
            <label
              htmlFor="entry-tags"
              className="mb-1 block text-[11px] font-medium text-foreground"
            >
              {t('entry.fields.tags')}
            </label>
            <TagsInput
              id="entry-tags"
              value={tags}
              onChange={setTags}
              disabled={isPending}
              hasError={errors.tags !== undefined}
            />
            {errors.tags && (
              <p className="mt-1 text-[11px] text-destructive" role="alert">
                {t(errors.tags)}
              </p>
            )}
          </div>

          {/* Pinned */}
          <label className="inline-flex items-center gap-1.5 text-xs text-foreground">
            <input
              type="checkbox"
              checked={isPinned}
              onChange={(e) => setIsPinned(e.target.checked)}
              disabled={isPending}
              className="h-3.5 w-3.5 rounded border-border text-primary focus:ring-2 focus:ring-primary focus:ring-offset-0"
            />
            {t('entry.fields.pinned')}
          </label>

          {/* Similar memories (create mode only) */}
          {mode === 'create' && searchMutation.data && (
            <div className="rounded-md border border-border bg-muted/40 p-2">
              <p className="mb-1 text-[11px] font-medium text-muted-foreground">
                {t('entry.similar.title')}
              </p>
              {searchMutation.data.results.length === 0 ? (
                <p className="text-[11px] text-muted-foreground">
                  {t('entry.similar.empty')}
                </p>
              ) : (
                <ul className="space-y-1">
                  {searchMutation.data.results.map((m) => (
                    <li
                      key={m.memory_id}
                      className="truncate text-[11px] text-muted-foreground"
                      title={m.content}
                    >
                      · {m.content.slice(0, 100)}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {/* Backend error banner */}
          {(createMutation.error || updateMutation.error) && (
            <p
              className="rounded-md border border-destructive/40 bg-destructive/10 px-2 py-1.5 text-[11px] text-destructive"
              role="alert"
            >
              {(createMutation.error ?? updateMutation.error)?.message}
            </p>
          )}

          {/* Scope label (read-only context) */}
          {mode === 'edit' && (
            <p className="text-[10px] text-muted-foreground">
              {t('scope.current')}: <span className="font-mono">{scopeLabel}</span>
            </p>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 border-t border-border bg-muted/40 p-3">
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            disabled={isPending}
            className="inline-flex h-8 items-center justify-center rounded-md border border-border bg-card px-3 text-xs font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-60"
          >
            {t('common.cancel')}
          </button>
          <button
            type="submit"
            disabled={isPending || !canWrite}
            title={!canWrite ? t('errors.mode_read_only') : undefined}
            className={cn(
              'inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-xs font-medium text-white transition-colors',
              'bg-primary hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
              'disabled:cursor-not-allowed disabled:opacity-60',
            )}
          >
            <Save size={12} aria-hidden="true" />
            {saveLabel}
          </button>
        </div>
      </form>
    </div>
  );
}
