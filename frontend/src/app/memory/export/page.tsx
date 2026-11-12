// src/app/memory/export/page.tsx

/**
 * Export page (`/memory/export`).
 *
 * A thin wrapper around `<ExportForm>`. The form itself owns
 * all logic: scope picker, format toggle, status filter,
 * limit input, download and inspect actions, mode gating
 * (disabled when `mode === 'disabled'`), error surface.
 *
 * Why this is a page and not a dialog
 * -----------------------------------
 *
 * Export is a deliberate action with a clear success moment
 * (a file lands in the browser's download tray). Users often
 * visit, inspect, adjust the scope, inspect again, and only
 * then click Download. A dedicated page gives that iteration
 * a stable home. A dialog would be a worse fit -- too small
 * for the manifest preview, and forced to close on every
 * navigation.
 *
 * No `Suspense` boundary is needed: the page does not call
 * `useSearchParams()`. All client hooks it pulls in
 * (`useMemorySettings`, `useExportZip`, ...) are inside
 * `<ExportForm>`, which is safe to render post-mount (the
 * parent `MemoryLayout` gates rendering on `mounted`).
 */

'use client';

import { useTranslation } from 'react-i18next';

import { ExportForm } from '../../../features/memory/components/ExportForm';

export default function MemoryExportPage() {
  const { t } = useTranslation('memory');

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4">
      {/* ============ Title ============ */}
      <div>
        <h2 className="text-base font-semibold text-foreground">
          {t('export.title')}
        </h2>
        <p className="text-[11px] text-muted-foreground">
          {t('export.description')}
        </p>
      </div>

      {/* ============ Form ============ */}
      <section className="rounded-lg border border-border bg-card p-4">
        <ExportForm />
      </section>
    </div>
  );
}
