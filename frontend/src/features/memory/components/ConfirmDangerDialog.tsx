// src/features/memory/components/ConfirmDangerDialog.tsx

/**
 * A type-to-confirm dialog for irreversible destructive
 * operations.
 *
 * The pattern is deliberately frictionful: the user must type
 * an exact token (usually the word `DELETE` or a project name)
 * to enable the confirmation button. Every hard delete, bulk
 * purge, and project override removal in the memory feature
 * goes through this dialog.
 *
 * Why a custom dialog, not `window.confirm`
 * -----------------------------------------
 *
 * The spec's §6.2 rule forbids `window.confirm`:
 *
 *   * It cannot require a typed token (only yes/no).
 *   * It blocks the event loop (React state updates stall).
 *   * It renders outside the app's theme and RTL handling.
 *   * It cannot show structured secondary content (e.g. the
 *     project name that will be purged).
 *
 * Accessibility
 * -------------
 *
 *   * `role="dialog"` + `aria-modal="true"` on the panel.
 *   * `aria-labelledby` points at the title.
 *   * `aria-describedby` points at the description.
 *   * Focus moves into the input on open (`useEffect`).
 *   * Escape closes the dialog UNLESS a mutation is pending.
 *   * Enter submits when the input matches and `isPending` is
 *     `false` (the input has `onKeyDown` for Enter).
 *   * Click outside the panel closes the dialog under the same
 *     rules as Escape.
 *
 * Unlike a general-purpose dialog, this one does NOT trap
 * focus inside the panel. That is intentional: the type-to-
 * confirm interaction has only two focusable controls (the
 * input and the two buttons), and a hard trap would just make
 * it harder for assistive tech users to reach the browser's
 * own affordances. For a 3-control dialog, this is the right
 * tradeoff. A general dialog in the app should trap.
 */

'use client';

import {
  type KeyboardEvent,
  type MouseEvent,
  useEffect,
  useId,
  useRef,
  useState,
} from 'react';
import { AlertTriangle, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ConfirmDangerDialogProps {
  /** Whether the dialog is currently rendered. */
  open: boolean;

  /** Called when the dialog wants to close (Escape, backdrop, X, cancel). */
  onOpenChange: (open: boolean) => void;

  /** Translated dialog title (e.g. "Permanently delete?"). */
  title: string;

  /** Translated dialog body (e.g. explanation of the consequence). */
  description: string;

  /**
   * The exact string the user must type to enable the confirm
   * button. Case-sensitive. Typically `'DELETE'` or a project
   * name.
   */
  confirmToken: string;

  /** Translated label for the confirm button. Defaults to `t('common.delete')`. */
  confirmLabel?: string;

  /** Translated label for the cancel button. Defaults to `t('common.cancel')`. */
  cancelLabel?: string;

  /** Placeholder for the input. Defaults to the token itself. */
  confirmPlaceholder?: string;

  /** When `true`, the confirm button shows a "pending" state and is disabled. */
  isPending?: boolean;

  /**
   * Called when the user clicks confirm with the correct token.
   * May be async; the dialog stays open until the caller changes
   * `open` to `false`.
   */
  onConfirm: () => void | Promise<void>;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ConfirmDangerDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmToken,
  confirmLabel,
  cancelLabel,
  confirmPlaceholder,
  isPending = false,
  onConfirm,
}: ConfirmDangerDialogProps) {
  const { t } = useTranslation('memory');
  const [entered, setEntered] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  // Reset the typed token whenever the dialog opens or closes.
  // Without this, a user who opens the dialog, types DELETE,
  // cancels, and reopens it would find the input pre-filled --
  // effectively defeating the guard.
  useEffect(() => {
    if (open) {
      setEntered('');
      // Focus the input after the panel renders. The setTimeout
      // is required so the browser paints the element before
      // focus is called (focusing a not-yet-painted element
      // sometimes silently fails on Safari).
      const handle = setTimeout(() => {
        inputRef.current?.focus();
      }, 0);
      return () => clearTimeout(handle);
    }
    return undefined;
  }, [open]);

  // Escape closes the dialog. Registered on `document` so that
  // the shortcut works regardless of where focus is.
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

  if (!open) return null;

  const matches = entered.trim() === confirmToken;
  const canConfirm = matches && !isPending;

  function handleBackdropClick(e: MouseEvent<HTMLDivElement>) {
    // Only close when the click originated on the backdrop
    // itself, not when it bubbled from inside the panel.
    if (e.target === e.currentTarget && !isPending) {
      onOpenChange(false);
    }
  }

  function handleKeyDownInInput(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter' && canConfirm) {
      e.preventDefault();
      void onConfirm();
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
      onClick={handleBackdropClick}
      role="presentation"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        className="w-full max-w-md overflow-hidden rounded-lg border border-border bg-card text-foreground shadow-peldrun-lg"
      >
        {/* ---- Header ---- */}
        <div className="flex items-start gap-3 border-b border-border p-4">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-destructive/15 text-destructive">
            <AlertTriangle size={18} strokeWidth={2} />
          </div>
          <div className="min-w-0 flex-1">
            <h2
              id={titleId}
              className="text-sm font-semibold text-foreground"
            >
              {title}
            </h2>
          </div>
          <button
            type="button"
            onClick={() => {
              if (!isPending) onOpenChange(false);
            }}
            disabled={isPending}
            aria-label={t('common.close')}
            className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            <X size={14} />
          </button>
        </div>

        {/* ---- Body ---- */}
        <div className="space-y-3 p-4">
          <p id={descriptionId} className="text-xs leading-relaxed text-muted-foreground">
            {description}
          </p>

          <div className="space-y-1.5">
            <label
              htmlFor="confirm-token-input"
              className="block text-[11px] font-medium text-foreground"
            >
              {t('common.typeToConfirm', { token: confirmToken })}
            </label>
            <input
              id="confirm-token-input"
              ref={inputRef}
              type="text"
              value={entered}
              onChange={(e) => setEntered(e.target.value)}
              onKeyDown={handleKeyDownInInput}
              placeholder={confirmPlaceholder ?? confirmToken}
              autoComplete="off"
              autoCorrect="off"
              autoCapitalize="off"
              spellCheck={false}
              disabled={isPending}
              aria-invalid={entered.length > 0 && !matches}
              className={cn(
                'w-full rounded-md border bg-background px-2.5 py-2 text-xs text-foreground',
                'placeholder:text-muted-foreground',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:ring-offset-card',
                'disabled:cursor-not-allowed disabled:opacity-60',
                matches
                  ? 'border-emerald-500'
                  : entered.length > 0
                    ? 'border-destructive'
                    : 'border-border',
              )}
            />
          </div>
        </div>

        {/* ---- Footer ---- */}
        <div className="flex items-center justify-end gap-2 border-t border-border bg-muted/40 p-3">
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            disabled={isPending}
            className="inline-flex h-8 items-center justify-center rounded-md border border-border bg-card px-3 text-xs font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed disabled:opacity-60"
          >
            {cancelLabel ?? t('common.cancel')}
          </button>
          <button
            type="button"
            onClick={() => {
              if (canConfirm) void onConfirm();
            }}
            disabled={!canConfirm}
            className={cn(
              'inline-flex h-8 items-center justify-center rounded-md px-3 text-xs font-medium text-white transition-colors',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-destructive focus-visible:ring-offset-2 focus-visible:ring-offset-card',
              canConfirm
                ? 'bg-destructive hover:bg-destructive/90'
                : 'cursor-not-allowed bg-destructive/40',
            )}
          >
            {isPending ? t('common.loading') : (confirmLabel ?? t('common.delete'))}
          </button>
        </div>
      </div>
    </div>
  );
}
