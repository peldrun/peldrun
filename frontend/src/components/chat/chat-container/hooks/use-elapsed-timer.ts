// Path: frontend/src/components/chat/chat-container/hooks/use-elapsed-timer.ts
"use client";

import { useEffect, useState } from "react";
import type { ChatStatus } from "../types";

/**
 * Ticks a wall-clock counter while a run is active.
 *
 * The interval is created only while `status === "running"` and is cleared
 * as soon as the status transitions to any terminal state.
 */
export function useElapsedTimer(
  status: ChatStatus
): [number, React.Dispatch<React.SetStateAction<number>>] {
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  useEffect(() => {
    let timer: NodeJS.Timeout | undefined;
    if (status === "running") {
      timer = setInterval(() => setElapsedSeconds((prev) => prev + 1), 1000);
    }
    return () => {
      if (timer) clearInterval(timer);
    };
  }, [status]);

  return [elapsedSeconds, setElapsedSeconds];
}
