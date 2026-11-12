// src/features/memory/hooks/useKeyboardShortcuts.ts

/**
 * Register one or more keyboard shortcuts on `document`.
 *
 * The hook is feature-agnostic: it works for any shortcut, not
 * just memory's. Used by the memory browse page (planned) to
 * wire `Cmd+K` for "focus scope picker", `N` for "new memory",
 * and `Esc` for "close dialogs". Because each caller registers
 * its own shortcuts, there is no global registry that can
 * leak between pages.
 *
 * Cross-platform modifier
 * -----------------------
 *
 * `mod` in a shortcut string matches EITHER `metaKey` (Cmd on
 * macOS) OR `ctrlKey` (Ctrl on Windows / Linux). This is what
 * every well-behaved web app does -- developers should not
 * have to write a `isMac ? 'Cmd+K' : 'Ctrl+K'` branch.
 *
 * Shortcut string format
 * ----------------------
 *
 *   `[mod+][shift+][alt+]KEY`
 *
 *     * `mod`   -- metaKey OR ctrlKey
 *     * `shift` -- shiftKey
 *     * `alt`   -- altKey
 *     * `KEY`   -- case-insensitive `event.key` value
 *                  (`K`, `Enter`, `Escape`, `1`, ...)
 *
 * Examples:
 *
 *     'mod+k'       -- Cmd+K / Ctrl+K
 *     'mod+shift+n' -- Cmd+Shift+N / Ctrl+Shift+N
 *     'escape'      -- Escape
 *     'n'           -- plain `n` (no modifier)
 *
 * Filters
 * -------
 *
 *   * `preventDefault` (default `true`) -- stops the browser
 *     from executing its native action (e.g. opening the
 *     search bar with Cmd+K).
 *
 *   * `skipWhenInputFocused` (default `true`) -- ignores the
 *     shortcut when focus is inside an `<input>`,
 *     `<textarea>`, or `contentEditable` element. Prevents
 *     typing `n` in a text field from triggering "new
 *     memory".
 *
 * Cleanup
 * -------
 *
 * The listener is removed on unmount (or when the `shortcuts`
 * array identity changes). No manual cleanup is needed by the
 * caller.
 */

'use client';

import { useEffect, useRef } from 'react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface KeyboardShortcut {
  /**
   * The shortcut in `[mod+][shift+][alt+]KEY` format. See the
   * module JSDoc for the full grammar.
   */
  keys: string;

  /** Called when the shortcut fires. */
  handler: (event: KeyboardEvent) => void;

  /**
   * When `true` (default), suppresses the browser's default
   * action for the shortcut combination.
   */
  preventDefault?: boolean;

  /**
   * When `true` (default), the shortcut is ignored while focus
   * is inside a text-input-like element.
   */
  skipWhenInputFocused?: boolean;
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

interface ParsedShortcut {
  key: string;
  mod: boolean;
  shift: boolean;
  alt: boolean;
}

/**
 * Parse a shortcut string into a comparable shape.
 *
 * Returns `null` if the string is empty or contains only
 * modifiers (no actual key) -- a caller that writes
 * `'mod+'` by mistake gets a no-op rather than a shortcut that
 * fires on every modifier press.
 */
function parseShortcut(spec: string): ParsedShortcut | null {
  const parts = spec.toLowerCase().split('+').map((p) => p.trim());
  const key = parts[parts.length - 1];
  if (!key || key === 'mod' || key === 'shift' || key === 'alt') {
    return null;
  }
  return {
    key,
    mod: parts.includes('mod'),
    shift: parts.includes('shift'),
    alt: parts.includes('alt'),
  };
}

/**
 * `true` when focus is inside an element that swallows ordinary
 * keystrokes (input / textarea / contentEditable).
 */
function isInputFocused(): boolean {
  if (typeof document === 'undefined') return false;
  const el = document.activeElement as HTMLElement | null;
  if (!el) return false;
  const tag = el.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return true;
  if (el.isContentEditable) return true;
  return false;
}

/**
 * `true` if the pressed key matches the parsed shortcut, using
 * a case-insensitive comparison on `event.key`.
 */
function matches(
  event: KeyboardEvent,
  parsed: ParsedShortcut,
): boolean {
  const eventKey = event.key.toLowerCase();
  const wantMod = event.metaKey || event.ctrlKey;
  return (
    eventKey === parsed.key &&
    wantMod === parsed.mod &&
    event.shiftKey === parsed.shift &&
    event.altKey === parsed.alt
  );
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useKeyboardShortcuts(
  shortcuts: KeyboardShortcut[],
): void {
  // Keep a stable reference to the latest array so the
  // listener does not need to re-bind on every render.
  const shortcutsRef = useRef<KeyboardShortcut[]>(shortcuts);
  shortcutsRef.current = shortcuts;

  useEffect(() => {
    if (typeof document === 'undefined') return undefined;

    function onKeyDown(event: KeyboardEvent) {
      for (const sc of shortcutsRef.current) {
        const parsed = parseShortcut(sc.keys);
        if (!parsed) continue;
        if (!matches(event, parsed)) continue;

        const skipInput = sc.skipWhenInputFocused ?? true;
        if (skipInput && isInputFocused()) continue;

        if (sc.preventDefault ?? true) event.preventDefault();
        sc.handler(event);
        return;
      }
    }

    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
    };
  }, []);
}
