// frontend/src/features/source-control/components/AuthReconnectBanner.tsx

"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  Loader2,
  RefreshCw,
} from "lucide-react";
import {
  sourceControlClient,
  SourceControlApiError,
} from "../api/source-control-client";

/**
 * Inline banner shown when any Source Control API call returns 401.
 *
 * The banner progresses through four visual phases, each rendered
 * as a distinct row so the user always knows what is happening:
 *
 *   1. idle       — the standard "Reconnect" prompt.
 *   2. opening    — the browser is being handed the OAuth URL.
 *   3. waiting    — polling the backend until GitHub confirms.
 *   4. success    — shown for ~0.9s, then the banner unmounts.
 *
 * Design goals:
 *   - The user never leaves the chat to re-authenticate.
 *   - The loader is unmistakable: a full-row spinner at every
 *     connecting phase, plus a Cancel button so the user is never
 *     trapped in a spinner they cannot dismiss.
 *   - Polling is fast at the start (1s for the first 10 attempts)
 *     so the common case feels instant, and slows to 2s afterwards
 *     so a slow user does not waste CPU.
 *   - Switching back to the tab triggers an immediate re-poll, so
 *     the user does not have to wait for the next scheduled tick.
 *
 * Event contract:
 *   - `peldrun:auth-required`      (received)
 *   - `peldrun:auth-reconnected`   (emitted on success)
 *   - `peldrun:artifact-updated`   (emitted on success, secondary)
 */
type Phase = "idle" | "opening" | "waiting" | "success";

const POLL_INTERVAL_FAST_MS = 1000;
const POLL_INTERVAL_SLOW_MS = 2000;
const FAST_POLL_ATTEMPTS = 10;
const MAX_ATTEMPTS = 90; // ~2 min at avg 1.5s
const SUCCESS_VISIBLE_MS = 900;

export function AuthReconnectBanner() {
  const [needsReconnect, setNeedsReconnect] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [connectError, setConnectError] = useState<string | null>(null);

  // The polling timer is stored in a ref so it survives re-renders
  // and can be cleared synchronously from event handlers.
  const pollTimerRef = useRef<number | null>(null);
  // Guards against firing more than one successful "reconnected"
  // event when several poll ticks resolve after the connection
  // comes up.
  const successFiredRef = useRef(false);

  const isConnecting = phase === "opening" || phase === "waiting";

  // --- (1) Listen for 401s from the API client ---
  useEffect(() => {
    const onAuthRequired = (e: Event) => {
      // While the user is mid-reconnect, a stale request that
      // resolves after the token has already been refreshed will
      // still emit auth-required. Ignore it so the banner does not
      // flash back to "idle" right as the success message appears.
      if (phase === "opening" || phase === "waiting" || phase === "success") {
        return;
      }
      const detail =
        (e as CustomEvent<{ message?: string }>).detail || {};
      setNeedsReconnect(true);
      setMessage(detail.message || null);
      setPhase("idle");
    };
    window.addEventListener("peldrun:auth-required", onAuthRequired);
    return () => {
      window.removeEventListener("peldrun:auth-required", onAuthRequired);
    };
  }, [phase]);

  // --- (2) Clean up the timer on unmount ---
  useEffect(() => {
    return () => {
      if (pollTimerRef.current !== null) {
        window.clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, []);

  // --- (3) Re-poll immediately when the user returns to this tab ---
  //
  // A common flow is: user clicks Reconnect, the browser window
  // opens, the user authorizes there, and returns to the PELDRUN
  // tab. Without this listener the user would sit on a spinner for
  // up to 2 seconds waiting for the next scheduled poll. Instead,
  // we fire a single check on visibility change so the banner
  // snaps to "Connected" the instant the user comes back.
  useEffect(() => {
    if (phase !== "waiting") return;
    const onVisible = () => {
      if (document.visibilityState === "visible") {
        void checkOnce();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
    };
    // `checkOnce` is defined below and is stable per phase; include
    // it in deps via the ref pattern is unnecessary because the
    // effect re-registers on phase change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase]);

  // --- One-shot connection check, used by both polling and the
  // visibilitychange handler. Returns true once the connection is
  // confirmed, at which point the caller stops scheduling.
  async function checkOnce(): Promise<boolean> {
    try {
      const connections = await sourceControlClient.listConnections();
      const github = connections.find((c) => c.provider === "github");
      if (github?.is_connected) {
        // Transition to "success" and let a short delay show the
        // green banner before it unmounts.
        if (successFiredRef.current) return true;
        successFiredRef.current = true;
        setPhase("success");
        if (pollTimerRef.current !== null) {
          window.clearTimeout(pollTimerRef.current);
          pollTimerRef.current = null;
        }
        window.setTimeout(() => {
          setNeedsReconnect(false);
          setMessage(null);
          setPhase("idle");
          successFiredRef.current = false;
          // Notify every listener so live views remount and
          // re-fetch with the fresh token.
          window.dispatchEvent(
            new CustomEvent("peldrun:auth-reconnected")
          );
          window.dispatchEvent(
            new CustomEvent("peldrun:artifact-updated", {
              detail: { source: "auth-reconnect" },
            })
          );
        }, SUCCESS_VISIBLE_MS);
        return true;
      }
      return false;
    } catch {
      // Transient — the same error that triggered the banner will
      // simply re-trigger it if the connection is still broken.
      return false;
    }
  }

  // --- (4) Reconnect flow ---
  const handleReconnect = async () => {
    setConnectError(null);
    setPhase("opening");

    try {
      const response = await sourceControlClient.beginConnection(
        "github",
        {}
      );

      // Open the OAuth page. Calling `window.open` from the click
      // handler (user gesture) means the browser does not block the
      // popup.
      //
      // IMPORTANT — why `noopener`/`noreferrer` are NOT passed in
      // the features argument:
      //
      // Modern browsers (Chrome 88+, Firefox, Safari) return `null`
      // from `window.open` whenever `noopener` is set, regardless
      // of whether the tab actually opened. That makes the
      // `if (!win) throw "Pop-up blocked"` check fire on EVERY
      // successful open, which is the exact bug this branch fixes.
      //
      // Security is preserved by nulling `win.opener` ourselves
      // immediately after opening. That achieves the same isolation
      // as `noopener` without triggering the null-return behavior.
      const win = window.open(response.authorization_url, "_blank");
      if (!win) {
        // `null` here means the popup was genuinely blocked, because
        // `noopener` is not set and thus a reference should be
        // returned on success.
        throw new Error(
          "Pop-up blocked. Please allow pop-ups for this site and try again."
        );
      }
      try {
        // Detach the child window from this window. Equivalent to
        // the `noopener` feature flag, but applied after the fact
        // so the reference is not lost.
        win.opener = null;
      } catch {
        // Cross-origin access to `opener` can throw in some
        // browsers. That is fine: if we cannot null it, it means
        // the child window is already on a different origin and
        // cannot access this window's `opener` anyway.
      }

      setPhase("waiting");

      // Kick off the polling loop.
      let attempts = 0;
      const tick = async () => {
        attempts++;
        const connected = await checkOnce();
        if (connected) return;
        if (attempts >= MAX_ATTEMPTS) {
          setPhase("idle");
          setConnectError(
            "Connection timed out. Please try again."
          );
          return;
        }
        const delay =
          attempts < FAST_POLL_ATTEMPTS
            ? POLL_INTERVAL_FAST_MS
            : POLL_INTERVAL_SLOW_MS;
        pollTimerRef.current = window.setTimeout(tick, delay);
      };
      void tick();
    } catch (err) {
      setPhase("idle");
      if (err instanceof SourceControlApiError) {
        // A 400 with "not configured" means the OAuth App client ID
        // / secret are missing from the vault. That is the ONE case
        // where the user MUST visit the Settings page; surface a
        // clear message instead of a generic error.
        if (
          err.status === 400 &&
          /not configured/i.test(err.detail || "")
        ) {
          setConnectError(
            "OAuth is not configured yet. Please set up Client ID + Secret in Settings → Source Control."
          );
        } else {
          setConnectError(err.detail);
        }
      } else {
        setConnectError(
          err instanceof Error
            ? err.message
            : "Failed to start the OAuth flow."
        );
      }
    }
  };

  // --- (5) Cancel — user gives up on the current attempt ---
  const handleCancel = () => {
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    successFiredRef.current = false;
    setConnectError(null);
    setPhase("idle");
  };

  if (!needsReconnect) return null;

  // ------------------------------------------------------------------
  // Render phase: success
  // ------------------------------------------------------------------
  if (phase === "success") {
    return (
      <div className="flex items-center gap-2 px-3 py-2 border-b border-green-300 bg-green-50 text-xs text-green-900 shrink-0">
        <CheckCircle2 className="w-4 h-4 shrink-0 text-green-700" />
        <span className="font-medium">Connected! Reloading…</span>
        <Loader2 className="w-3.5 h-3.5 animate-spin ml-auto text-green-700" />
      </div>
    );
  }

  // ------------------------------------------------------------------
  // Render phase: connecting (opening / waiting)
  // ------------------------------------------------------------------
  if (isConnecting) {
    return (
      <div className="flex items-start gap-2 px-3 py-2 border-b border-blue-300 bg-blue-50 text-xs text-blue-900 shrink-0">
        <Loader2 className="w-4 h-4 shrink-0 mt-0.5 text-blue-700 animate-spin" />
        <div className="flex-1 min-w-0">
          <div className="font-medium">
            {phase === "opening"
              ? "Opening GitHub…"
              : "Waiting for authorization…"}
          </div>
          <div className="text-[11px] mt-0.5 text-blue-800">
            {phase === "opening"
              ? "Preparing the OAuth flow."
              : "Complete the authorization in the browser window, then come back here. The connection will be detected automatically."}
          </div>
        </div>
        <button
          type="button"
          onClick={handleCancel}
          className="shrink-0 text-[11px] px-2 py-1 rounded border border-blue-400 bg-blue-100 hover:bg-blue-200 inline-flex items-center gap-1"
        >
          Cancel
        </button>
      </div>
    );
  }

  // ------------------------------------------------------------------
  // Render phase: idle / needs reconnect
  // ------------------------------------------------------------------
  return (
    <div className="flex items-start gap-2 px-3 py-2 border-b border-amber-300 bg-amber-50 text-xs text-amber-900 shrink-0">
      <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-amber-700" />
      <div className="flex-1 min-w-0">
        <div className="font-medium">
          GitHub authentication required
        </div>
        <div className="text-[11px] mt-0.5 text-amber-800">
          {message ||
            "Your GitHub token has expired or been revoked. Reconnect to continue."}
        </div>
        {connectError && (
          <div className="text-[11px] mt-1 text-red-700">
            {connectError}
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={handleReconnect}
        className="shrink-0 text-[11px] px-2 py-1 rounded border border-amber-400 bg-amber-100 hover:bg-amber-200 inline-flex items-center gap-1"
      >
        <RefreshCw className="w-3 h-3" />
        Reconnect
      </button>
    </div>
  );
}
