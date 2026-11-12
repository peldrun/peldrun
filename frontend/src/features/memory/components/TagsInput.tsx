// src/features/memory/components/TagsInput.tsx

/**
 * Chip-style editor for an entry's `tags` array.
 *
 * Behaviour
 * ---------
 *
 *   * **Enter** in the input adds the current text as a tag
 *     (after trimming). If the tag is empty, over-length, or
 *     already present, the input is NOT cleared and the
 *     corresponding inline error is shown.
 *
 *   * **Backspace** on an empty input removes the last tag.
 *     This matches the Gmail / GitHub label-editor convention
 *     that most users already know.
 *
 *   * **Comma** and **semicolon** are also tag terminators --
 *     typing `alpha,` adds `alpha`. Users paste comma-separated
 *     lists often enough that this is worth handling.
 *
 *   * **Paste** of a comma-separated list splits into multiple
 *     tags. Each candidate goes through the same validation as
 *     a typed tag.
 *
 *   * **X** on a chip removes that tag. The X is keyboard
 *     focusable (`role="button"` on a `<span>` + tabIndex) so
 *     keyboard users can remove a tag without retyping.
 *
 * Constraints
 * -----------
 *
 *   * Max 32 tags per entry (`MAX_TAGS`).
 *   * Max 64 characters per tag (`MAX_TAG_LENGTH`).
 *   * No duplicates (case-sensitive).
 *
 * These mirror the backend's validation exactly. The input
 * refuses an invalid tag inline rather than letting the whole
 * form submit fail with a `MemoryValidationError`.
 *
 * Why a textarea-like UX, not a full "combobox with
 * suggestions"
 * -----------------------------------------------------------------
 *
 * Tags in memory entries are freeform labels the user invents,
 * not selections from a controlled vocabulary. A combobox would
 * add friction (open dropdown, type to filter, pick from a
 * possibly-empty list) without a corresponding benefit. The
 * plain text input with chip display is the right tool for a
 * set of unbounded strings.
 */

'use client';

import {
  type ClipboardEvent,
  type KeyboardEvent,
  useId,
  useState,
} from 'react';
import { X } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { MAX_TAGS, MAX_TAG_LENGTH } from '../lib/constants';
import { cn } from '../lib/cn';

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface TagsInputProps {
  /** The current tags. Treat as read-only; changes go via `onChange`. */
  value: string[];

  /** Called with the new array whenever the tag set changes. */
  onChange: (tags: string[]) => void;

  /**
   * Placeholder for the input. Defaults to the translated
   * "Type a tag and press Enter" when omitted.
   */
  placeholder?: string;

  /** When `true`, the entire editor is non-interactive. */
  disabled?: boolean;

  /**
   * When `true`, the input renders with a red border and shows
   * the field-level error passed via `aria-describedby` from
   * the caller. The editor itself does not display error text --
   * the caller's form layout places it.
   */
  hasError?: boolean;

  /** The `<label htmlFor>` target for accessibility. */
  id?: string;

  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function TagsInput({
  value,
  onChange,
  placeholder,
  disabled = false,
  hasError = false,
  id,
  className,
}: TagsInputProps) {
  const { t } = useTranslation('memory');
  const [input, setInput] = useState('');
  const [inlineError, setInlineError] = useState<string | null>(null);
  const generatedId = useId();
  const effectiveId = id ?? generatedId;
  const inputPlaceholder = placeholder ?? t('entry.placeholders.tagInput');

  // -------------------------------------------------------------------------
  // Validation + add
  // -------------------------------------------------------------------------

  /**
   * Validate one candidate tag. Returns an i18n key on error,
   * or `null` on success. Does NOT mutate state.
   */
  function validateCandidate(
    candidate: string,
    existing: readonly string[],
  ): string | null {
    if (candidate.length === 0) return 'validation.tagEmpty';
    if (candidate.length > MAX_TAG_LENGTH) return 'validation.tagTooLong';
    if (existing.includes(candidate)) return 'validation.tagDuplicate';
    if (existing.length >= MAX_TAGS) return 'validation.tagsTooMany';
    return null;
  }

  /**
   * Try to add the current input text (or a supplied string) as
   * one or more tags.
   */
  function commit(raw: string): boolean {
    // Split on comma / semicolon so pasting a list works, and
    // so typing "a, b" behaves as expected.
    const candidates = raw
      .split(/[,;]/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0);

    if (candidates.length === 0) {
      setInlineError(null);
      setInput('');
      return false;
    }

    const next = [...value];
    for (const candidate of candidates) {
      const error = validateCandidate(candidate, next);
      if (error) {
        setInlineError(error);
        // Do NOT clear input -- the user should be able to
        // correct the text instead of retyping.
        return false;
      }
      next.push(candidate);
    }

    setInlineError(null);
    setInput('');
    onChange(next);
    return true;
  }

  // -------------------------------------------------------------------------
  // Event handlers
  // -------------------------------------------------------------------------

  function handleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (disabled) return;

    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      if (input.trim().length === 0) return;
      commit(input);
      return;
    }

    if (e.key === 'Backspace' && input.length === 0 && value.length > 0) {
      // Remove the last tag, matching the convention every
      // chip editor uses.
      e.preventDefault();
      onChange(value.slice(0, -1));
      setInlineError(null);
    }
  }

  function handlePaste(e: ClipboardEvent<HTMLInputElement>) {
    if (disabled) return;
    const text = e.clipboardData.getData('text');
    // Only intercept if the paste looks like a list; a single
    // word pasted into the middle of the input should behave
    // normally.
    if (!/[,;\n]/.test(text)) return;
    e.preventDefault();
    // Include the pre-paste text so pasting "a," mid-word
    // correctly continues the current draft.
    commit(input + text);
  }

  function handleBlur() {
    // Commit the leftover text on blur. Users who type a tag
    // and then click "Save" without pressing Enter expect the
    // typed text to be included.
    if (input.trim().length > 0) {
      commit(input);
    }
  }

  function removeTag(tag: string) {
    if (disabled) return;
    onChange(value.filter((t) => t !== tag));
    setInlineError(null);
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  const atLimit = value.length >= MAX_TAGS;

  return (
    <div className={cn('space-y-1', className)}>
      {/* Container styled as a text-input-like box with chips. */}
      <div
        className={cn(
          'flex min-h-[2.5rem] flex-wrap items-center gap-1.5 rounded-md border bg-card px-2 py-1.5 text-xs transition-colors',
          hasError
            ? 'border-destructive focus-within:border-destructive'
            : 'border-border focus-within:border-primary',
          disabled && 'cursor-not-allowed opacity-60',
        )}
      >
        {/* Existing tags. */}
        {value.map((tag) => (
          <span
            key={tag}
            className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 text-[11px] text-foreground"
          >
            <span className="max-w-[16rem] truncate" title={tag}>
              {tag}
            </span>
            <button
              type="button"
              onClick={() => removeTag(tag)}
              disabled={disabled}
              aria-label={t('entry.actions.removeTag') + ' ' + tag}
              className="inline-flex h-3.5 w-3.5 items-center justify-center rounded-full text-muted-foreground hover:bg-foreground/10 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-not-allowed"
            >
              <X size={10} strokeWidth={2.5} />
            </button>
          </span>
        ))}

        {/* Text input. Hidden at tag cap so the box shape stays
            stable, but rendered (disabled) so `id`-based
            labelling and tab order work. */}
        <input
          id={effectiveId}
          type="text"
          value={input}
          onChange={(e) => {
            setInput(e.target.value);
            if (inlineError) setInlineError(null);
          }}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onBlur={handleBlur}
          placeholder={value.length === 0 ? inputPlaceholder : ''}
          disabled={disabled || atLimit}
          maxLength={MAX_TAG_LENGTH * 2}
          aria-label={t('entry.fields.tags')}
          aria-invalid={hasError || inlineError !== null}
          className={cn(
            'flex-1 min-w-[6rem] bg-transparent text-xs text-foreground outline-none placeholder:text-muted-foreground',
            'disabled:cursor-not-allowed',
          )}
        />
      </div>

      {/* Inline error or counter. The counter is the only
          passive feedback; it prevents silent cap-hits. */}
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className={cn(
            'min-h-[1em]',
            inlineError ? 'text-destructive' : 'text-transparent',
          )}
          role={inlineError ? 'alert' : undefined}
        >
          {inlineError ? t(inlineError) : '.'}
        </span>
        <span className="tabular-nums text-muted-foreground">
          {value.length}/{MAX_TAGS}
        </span>
      </div>
    </div>
  );
}
