// frontend/src/lib/storage/debounce.ts

/**
 * Default debounce delay (milliseconds).
 *
 * Chosen as a balance between:
 *   - Responsiveness: fast enough to feel immediate to the user.
 *   - Stability: long enough to batch rapid successive writes.
 *
 * Override at the call site according to data sensitivity
 * (audit report item 7.1):
 *   - 100–200ms for critical data that must not be lost on close.
 *   - 500–1000ms for analytical/telemetry writes.
 *
 * Callers that must survive a browser close (tab closed, navigation
 * away, crash) SHOULD use the returned `flush` method to force a
 * pending write immediately.
 */
export const DEFAULT_DEBOUNCE_MS = 250;

interface DebouncedWriter<T extends (...args: any[]) => void> {
  (...args: Parameters<T>): void;
  /**
   * Immediately invoke any pending write with the most recent
   * arguments, then clear the timer. Safe to call when no write is
   * pending (it becomes a no-op).
   *
   * Use this from `beforeunload`, `pagehide`, or `visibilitychange`
   * handlers to prevent losing the last edit when the user closes
   * the tab.
   */
  flush(): void;
  /**
   * Cancel any pending write without invoking the writer.
   */
  cancel(): void;
}

export function createDebouncedWriter<T extends (...args: any[]) => void>(
  writer: T,
  delay: number = DEFAULT_DEBOUNCE_MS
): DebouncedWriter<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let pendingArgs: Parameters<T> | undefined;

  const debounced = (...args: Parameters<T>) => {
    pendingArgs = args;
    if (timer) {
      clearTimeout(timer);
    }
    timer = setTimeout(() => {
      timer = undefined;
      const argsToWrite = pendingArgs;
      pendingArgs = undefined;
      if (argsToWrite) {
        writer(...argsToWrite);
      }
    }, delay);
  };

  debounced.flush = () => {
    if (timer) {
      clearTimeout(timer);
      timer = undefined;
    }
    const argsToWrite = pendingArgs;
    pendingArgs = undefined;
    if (argsToWrite) {
      writer(...argsToWrite);
    }
  };

  debounced.cancel = () => {
    if (timer) {
      clearTimeout(timer);
      timer = undefined;
    }
    pendingArgs = undefined;
  };

  return debounced as DebouncedWriter<T>;
}
