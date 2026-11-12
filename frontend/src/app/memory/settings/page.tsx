// src/app/memory/settings/page.tsx

/**
 * The memory settings page (`/memory/settings`).
 *
 * This page MUST be reachable even when `mode === 'disabled'`.
 * It is the only path back to a working memory subsystem after
 * an accidental disable (spec §1.3 and §6.1 rule 1). The page
 * therefore renders unconditionally -- no mode-gate, no
 * redirect, no disabled state on the form itself.
 *
 * Two sections:
 *
 *   1. `SettingsForm` -- the global settings editor.
 *
 *   2. `Projects with overrides` -- a read-only list of which
 *      projects have customized settings, with links to each
 *      project's detail page (where `ProjectOverrideForm`
 *      lives). Adding or editing an override is done from the
 *      project detail page, not from here -- keeping the
 *      override workflow colocated with the project it
 *      affects avoids a project picker on this page.
 */

'use client';

import Link from 'next/link';
import { useTranslation } from 'react-i18next';
import { ArrowRight, FolderCog } from 'lucide-react';

import { useMemorySettings } from '../../../features/memory/api/queries';
import { SettingsForm } from '../../../features/memory/components/SettingsForm';
import { cn } from '../../../features/memory/lib/cn';

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function MemorySettingsPage() {
  const { t } = useTranslation('memory');
  const settingsQuery = useMemorySettings();

  const overrides = settingsQuery.data?.project_overrides ?? {};
  const overrideEntries = Object.entries(overrides);

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      {/* ============ Title ============ */}
      <div>
        <h2 className="text-base font-semibold text-foreground">
          {t('settings.title')}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t('settings.description')}
        </p>
      </div>

      {/* ============ Settings form ============ */}
      <section className="rounded-lg border border-border bg-card p-4">
        <SettingsForm />
      </section>

      {/* ============ Projects with overrides ============ */}
      <section className="rounded-lg border border-border bg-card p-4">
        <div className="mb-2 flex items-center gap-1.5">
          <FolderCog
            size={12}
            aria-hidden="true"
            className="text-muted-foreground"
          />
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {t('settings.sections.projectOverrides')}
          </h3>
        </div>

        {settingsQuery.isLoading ? (
          <div className="space-y-1.5" aria-busy="true">
            {[0, 1].map((i) => (
              <div
                key={i}
                className="h-8 animate-pulse rounded-md bg-muted"
              />
            ))}
          </div>
        ) : overrideEntries.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">
            {t('settings.projectOverridesEmpty')}
          </p>
        ) : (
          <ul className="space-y-1.5">
            {overrideEntries.map(([projectId, override]) => {
              const fieldCount = countOverrideFields(override);
              return (
                <li key={projectId}>
                  <Link
                    href={`/memory/projects/${encodeURIComponent(projectId)}`}
                    className={cn(
                      'group flex items-center justify-between gap-2 rounded-md border border-border bg-background px-2.5 py-1.5 text-[11px]',
                      'hover:border-primary/40 hover:bg-muted/40',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-1',
                    )}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-foreground">
                        {projectId}
                      </span>
                      <span className="text-[10px] text-muted-foreground">
                        {fieldCount}{' '}
                        {fieldCount === 1 ? 'custom field' : 'custom fields'}
                      </span>
                    </span>
                    <ArrowRight
                      size={11}
                      aria-hidden="true"
                      className="shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 rtl:group-hover:-translate-x-0.5"
                    />
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helper
// ---------------------------------------------------------------------------

/**
 * Count how many fields a project override actually customizes.
 *
 * The backend may return a full settings shape (with defaults
 * filled in) as the override -- we cannot trust `Object.keys`
 * alone to tell us "how customized is this project". The safest
 * heuristic is a shallow count of top-level fields; a nested
 * `token_budget` override counts as one field even if only
 * `max_tokens` differs inside it. That is accurate enough for
 * the summary label and avoids a per-project query just to
 * diff against global defaults.
 */
function countOverrideFields(
  override: Record<string, unknown> | object,
): number {
  let count = 0;
  for (const value of Object.values(override)) {
    if (value === undefined) continue;
    if (value === null) continue;
    count += 1;
  }
  return count;
}
