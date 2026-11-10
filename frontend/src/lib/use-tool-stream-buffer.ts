// Path: frontend/src/lib/use-tool-stream-buffer.ts
"use client";

import { useCallback, useEffect, useRef } from "react";

/**
 * useToolStreamBuffer — coalesce per-chunk SSE updates into one
 * React state flush per animation frame.
 *
 * ## Why this exists
 *
 * A streaming tool can emit 30–60 chunks per second. Calling
 * `setSteps` (or `setState` in general) for every chunk would trigger
 * one full render of `ChatTimeline` per chunk — and `ChatTimeline`
 * re-parses every markdown fragment on every render. Even with
 * `React.memo` on the whole timeline, the active step's tool card
 * is re-rendered on every chunk, and with three concurrent tools it
 * multiplies by three.
 *
 * The standard 2025/2026 pattern for this problem is:
 *
 *   1. Accumulate chunks in a `useRef` (mutable, no re-render).
 *   2. Schedule ONE flush per animation frame via
 *      `requestAnimationFrame`. The browser caps rAF at ~60 Hz, so
 *      even a burst of 200 chunks per frame is a single render.
 *   3. At flush time, hand the accumulated text to the caller's
 *      `setState` (which IS React state, so the component re-renders
 *      once with all the new text at once).
 *
 * Result: instead of N renders per second (N = chunk rate), the UI
 * renders at ~60 fps (or the caller's own render rate, whichever is
 * lower). Combined with `React.memo` on `ChatTimeline`, only the
 * active tool card re-renders per frame — historical steps are
 * untouched.
 *
 * ## Contract
 *
 *   * `push(toolCallId, stream, chunk)` — called for every
 *     `tool_output` chunk. Batches by `(toolCallId, stream)`. Never
 *     triggers a synchronous render.
 *
 *   * `flush()` — force the pending buffer to be delivered on the
 *     NEXT caller-side update cycle. Use this before dispatching a
 *     terminal event (`observation` / `final` / `error`) so the
 *     final visible text reflects every chunk the user watched,
 *     not just the ones that happened to land in the last frame.
 *
 * ## Cleanup
 *
 * The hook cancels any pending `requestAnimationFrame` on unmount
 * and clears the buffer, so no `setState` ever fires against an
 * unmounted component and the buffer's memory is released
 * immediately (rather than waiting for GC).
 *
 * ## Non-React environments
 *
 * If `requestAnimationFrame` is unavailable (SSR, some test
 * environments, Web Workers used as a renderer), `push` falls back
 * to a synchronous flush. This sacrifices the frame-batching
 * optimization but keeps the feature correct — the caller never
 * loses chunks.
 */
export function useToolStreamBuffer(
  onFlush: (toolCallId: string, text: string, stream: string) => void,
): {
  push: (toolCallId: string, stream: string, chunk: string) => void;
  flush: () => void;
} {
  // Buffer keyed by "<toolCallId>|<stream>". Two chunks from the
  // same tool to the same stream accumulate into one entry; chunks
  // to different streams stay separate so the caller can render
  // stdout vs stderr with different styling.
  const bufferRef = useRef<Map<string, { toolCallId: string; stream: string; text: string }>>(
    new Map(),
  );

  // Pending rAF handle. `null` means no frame is scheduled.
  const rafRef = useRef<number | null>(null);

  // Mirror of the caller's onFlush callback. Keeping it in a ref
  // means the caller can pass an inline arrow function without
  // invalidating `push` / `scheduleFlush` on every render. This is
  // the difference between a stable `push` (safe to include in a
  // `useEffect` dependency array) and a `push` that churns every
  // frame.
  const onFlushRef = useRef(onFlush);
  useEffect(() => {
    onFlushRef.current = onFlush;
  }, [onFlush]);

  const performFlush = useCallback(() => {
    rafRef.current = null;
    if (bufferRef.current.size === 0) return;

    // Snapshot then clear so re-entrant pushes (e.g. a subscriber
    // that synchronously calls push while we iterate) land in the
    // NEXT frame instead of being silently dropped.
    const entries = Array.from(bufferRef.current.values());
    bufferRef.current.clear();

    for (const entry of entries) {
      try {
        onFlushRef.current(entry.toolCallId, entry.text, entry.stream);
      } catch (err) {
        // A subscriber error must never corrupt buffer state or
        // prevent the remaining entries from flushing.
        console.warn("[useToolStreamBuffer] onFlush threw:", err);
      }
    }
  }, []);

  const scheduleFlush = useCallback(() => {
    if (rafRef.current !== null) return; // already scheduled

    // SSR / non-DOM fallback — flush synchronously.
    if (
      typeof window === "undefined" ||
      typeof requestAnimationFrame !== "function"
    ) {
      performFlush();
      return;
    }

    rafRef.current = requestAnimationFrame(performFlush);
  }, [performFlush]);

  const push = useCallback(
    (toolCallId: string, stream: string, chunk: string) => {
      if (!toolCallId || !chunk) return;
      const key = `${toolCallId}|${stream}`;
      const existing = bufferRef.current.get(key);
      if (existing) {
        existing.text += chunk;
      } else {
        bufferRef.current.set(key, { toolCallId, stream, text: chunk });
      }
      scheduleFlush();
    },
    [scheduleFlush],
  );

  /**
   * Force a synchronous flush of everything currently buffered.
   *
   * Use this immediately before dispatching a terminal or
   * observation event so the caller's state reflects every chunk
   * that was ever pushed. Without this, chunks queued in the last
   * frame (typically the last 16 ms of the tool's runtime) would
   * arrive AFTER the observation, which breaks the visual ordering
   * (observation → late chunks) and can cause the reducer to
   * mis-attribute them.
   */
  const flush = useCallback(() => {
    if (rafRef.current !== null) {
      if (typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(rafRef.current);
      }
      rafRef.current = null;
    }
    performFlush();
  }, [performFlush]);

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      if (
        rafRef.current !== null &&
        typeof cancelAnimationFrame === "function"
      ) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      bufferRef.current.clear();
    };
  }, []);

  return { push, flush };
}
