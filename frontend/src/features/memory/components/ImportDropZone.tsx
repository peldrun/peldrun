// src/features/memory/components/ImportDropZone.tsx

/**
 * The import wizard's file picker step.
 *
 * Accepts a single file via click-to-select or drag-and-drop,
 * validates it against the backend's size and extension
 * constraints (using the shared `IMPORT_*` constants), and
 * reports the result to the caller.
 *
 * Validation happens CLIENT-SIDE, before any upload:
 *
 *   * Empty file -> `EMPTY` rejection.
 *   * Oversize file (> 100 MB) -> `TOO_LARGE`.
 *   * Unknown extension -> `WRONG_TYPE`.
 *
 * The backend re-validates every one of these; the client-side
 * check just gives an instant error instead of waiting for a
 * 100 MB upload to be rejected.
 *
 * Adapter auto-detection
 * ----------------------
 *
 * The panel infers the adapter the backend would likely pick
 * from the file extension, and passes that as a HINT on the
 * `ImportFileInfo`. The backend re-detects at apply time; the
 * hint only feeds the "auto-detect" label in the wizard.
 *
 * Not a controlled component
 * --------------------------
 *
 * The dropzone does NOT hold the selected file in state. It
 * calls `onFileSelected(file, info)` when validation succeeds,
 * and `onRejected(reason, message)` when it fails. The caller
 * (the import page) owns the file and passes it to `/preview`
 * on the next step. Keeping file state out of this component
 * means "back" and "re-select" are trivial to implement -- the
 * caller just clears its own state.
 */

'use client';

import {
  type DragEvent,
  type KeyboardEvent,
  useId,
  useRef,
  useState,
} from 'react';
import { useTranslation } from 'react-i18next';
import { FileUp, UploadCloud } from 'lucide-react';

import {
  IMPORT_ACCEPT_ATTRIBUTE,
  IMPORT_ACCEPTED_EXTENSIONS,
  IMPORT_MAX_UPLOAD_BYTES,
  type ImportFileExtension,
  type ImportFileInfo,
  type ImportFileRejectionReason,
} from '../types/import-export';
import { ImportFileRejectionReason as Reasons } from '../types/import-export';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Adapter detection
// ---------------------------------------------------------------------------

/**
 * Map a file extension to the adapter the backend would
 * auto-select.
 *
 * This is a HINT shown in the wizard's "auto-detect" label --
 * the backend re-detects from magic bytes at apply time, so a
 * `.json` file that is actually a zip is still handled
 * correctly even though this mapping points to JSON.
 */
function detectAdapter(ext: string): string | null {
  switch (ext) {
    case '.zip':
      return 'json';
    case '.json':
      return 'json';
    case '.md':
    case '.markdown':
      return 'markdown';
    case '.db':
    case '.sqlite':
    case '.sqlite3':
      return 'sqlite';
    default:
      return null;
  }
}

// ---------------------------------------------------------------------------
// Size formatting
// ---------------------------------------------------------------------------

function formatBytes(
  bytes: number,
  t: (key: string, opts?: Record<string, unknown>) => string,
): string {
  if (bytes < 1024) return t('format.bytes', { count: bytes });
  if (bytes < 1024 * 1024) {
    return t('format.kilobytes', { value: (bytes / 1024).toFixed(1) });
  }
  return t('format.megabytes', { value: (bytes / (1024 * 1024)).toFixed(1) });
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ImportDropZoneProps {
  /** Called with the file + metadata when validation succeeds. */
  onFileSelected: (file: File, info: ImportFileInfo) => void;

  /**
   * Called when the file fails validation. The caller typically
   * displays an inline error; the dropzone itself does not
   * render one (the caller's layout has the final say on
   * placement).
   */
  onRejected?: (reason: ImportFileRejectionReason, message: string) => void;

  /** When `true`, the dropzone is not interactive. */
  disabled?: boolean;

  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ImportDropZone({
  onFileSelected,
  onRejected,
  disabled = false,
  className,
}: ImportDropZoneProps) {
  const { t } = useTranslation('memory');
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const id = useId();

  const acceptList = IMPORT_ACCEPTED_EXTENSIONS.join(', ');
  const sizeLimit = formatBytes(IMPORT_MAX_UPLOAD_BYTES, t);

  function validateFile(file: File): boolean {
    // 1. Empty
    if (file.size === 0) {
      const message = t('import.upload.emptyFile');
      onRejected?.(Reasons.EMPTY, message);
      return false;
    }

    // 2. Oversize
    if (file.size > IMPORT_MAX_UPLOAD_BYTES) {
      const message = t('import.upload.tooLarge', {
        actual: formatBytes(file.size, t),
        limit: sizeLimit,
      });
      onRejected?.(Reasons.TOO_LARGE, message);
      return false;
    }

    // 3. Wrong extension
    const ext = (
      '.' + (file.name.split('.').pop()?.toLowerCase() ?? '')
    ) as ImportFileExtension | string;
    if (
      !(IMPORT_ACCEPTED_EXTENSIONS as readonly string[]).includes(ext)
    ) {
      const message = t('import.upload.wrongType', { formats: acceptList });
      onRejected?.(Reasons.WRONG_TYPE, message);
      return false;
    }

    // Validated. Build the info + forward.
    const info: ImportFileInfo = {
      filename: file.name,
      sizeBytes: file.size,
      mimeType: file.type || 'application/octet-stream',
      extension: ext,
      detectedAdapter: detectAdapter(ext),
    };
    onFileSelected(file, info);
    return true;
  }

  function pickFile() {
    if (disabled) return;
    inputRef.current?.click();
  }

  function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    // Only the first file matters; the import API is
    // single-file-per-request.
    validateFile(files[0]);
  }

  // ---- Drag handlers ------------------------------------------------
  function handleDragEnter(e: DragEvent<HTMLDivElement>) {
    if (disabled) return;
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  }

  function handleDragOver(e: DragEvent<HTMLDivElement>) {
    if (disabled) return;
    e.preventDefault();
    e.stopPropagation();
    // Must set `dropEffect = 'copy'` for the cursor to show
    // the "will accept" indicator on every browser.
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
    if (!isDragging) setIsDragging(true);
  }

  function handleDragLeave(e: DragEvent<HTMLDivElement>) {
    if (disabled) return;
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  }

  function handleDrop(e: DragEvent<HTMLDivElement>) {
    if (disabled) return;
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
    handleFiles(e.dataTransfer?.files ?? null);
  }

  // ---- Keyboard handlers --------------------------------------------
  function handleKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (disabled) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      pickFile();
    }
  }

  return (
    <div className={cn('space-y-2', className)}>
      <input
        id={id}
        ref={inputRef}
        type="file"
        accept={IMPORT_ACCEPT_ATTRIBUTE}
        onChange={(e) => {
          handleFiles(e.target.files);
          // Reset the input so re-selecting the same file
          // fires `onChange` again (browser quirk).
          e.target.value = '';
        }}
        disabled={disabled}
        className="hidden"
      />

      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        onClick={pickFile}
        onKeyDown={handleKeyDown}
        onDragEnter={handleDragEnter}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        aria-disabled={disabled}
        aria-label={t('import.upload.dropzone')}
        className={cn(
          'flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed px-4 py-8 text-center transition-colors',
          isDragging
            ? 'border-primary bg-primary/5'
            : 'border-border bg-muted/20 hover:border-primary/40 hover:bg-muted/30',
          disabled && 'cursor-not-allowed opacity-60 hover:border-border',
          !disabled && 'cursor-pointer',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
        )}
      >
        <div
          className={cn(
            'flex h-10 w-10 items-center justify-center rounded-full',
            isDragging
              ? 'bg-primary/15 text-primary'
              : 'bg-muted text-muted-foreground',
          )}
          aria-hidden="true"
        >
          {isDragging ? (
            <FileUp size={18} strokeWidth={1.8} />
          ) : (
            <UploadCloud size={18} strokeWidth={1.8} />
          )}
        </div>
        <p className="text-xs font-medium text-foreground">
          {t('import.upload.dropzone')}
        </p>
        <p className="text-[11px] text-muted-foreground">
          {t('import.upload.acceptedFormats', { formats: acceptList })}
        </p>
        <p className="text-[10px] text-muted-foreground">
          {t('import.upload.sizeLimit', { size: sizeLimit })}
        </p>
      </div>
    </div>
  );
}
