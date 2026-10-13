// Path: frontend/src/components/chat/chat-thread/HumanInputCard.tsx
"use client";

/**
 * Human-in-the-loop suspension card.
 *
 * Renders the interactive dialog shown while the agent is paused waiting
 * for a human decision. Encapsulates the full submission flow:
 *
 *   idle       -> interactive card with options + free-text input
 *   sending    -> card animates away (letter-send effect)
 *   verifying  -> compact loader until the pending call resolves upstream
 *   success    -> green confirmation held briefly
 *   fading     -> confirmation fades out with a subtle scale-down
 *
 * The phase state machine lives inside this component so it naturally
 * resets whenever the card unmounts (i.e. when the agent proceeds).
 */

import React, { useEffect, useState } from "react";
import { HelpCircle, CheckCircle2, MessageSquare, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import AIThinkingLoader from "@/components/AIThinkingLoader";
import {
  HumanSubmitPhase,
  SENDING_ANIMATION_MS,
  SUCCESS_HOLD_MS,
  FADING_DURATION_MS,
  VERIFY_FALLBACK_MS,
} from "./types";

export interface HumanInputCardProps {
  /**
   * Whether the agent is currently waiting for human input. Used to
   * detect resolution of the pending call during the `verifying` phase.
   */
  isHumanWaiting: boolean;

  /** Prompt text the agent wants answered. */
  detectedPrompt: string;

  /** Optional quick-pick options supplied by the agent. */
  detectedOptions: string[];

  /** Controlled free-text answer value. */
  humanAnswer: string;

  /** Setter for the free-text answer value. */
  setHumanAnswer: (val: string) => void;

  /**
   * Upstream submission handler. Called exactly once when the user
   * confirms their response (either via an option button or the Send
   * button / Enter key).
   */
  onSendHumanAnswer: (customAnswer?: string) => void;
}

export function HumanInputCard({
  isHumanWaiting,
  detectedPrompt,
  detectedOptions,
  humanAnswer,
  setHumanAnswer,
  onSendHumanAnswer,
}: HumanInputCardProps) {
  /**
   * Visual submission state machine for the human-in-the-loop card.
   * Drives the send -> verify -> success -> fade transition sequence.
   */
  const [humanSubmitPhase, setHumanSubmitPhase] = useState<HumanSubmitPhase>("idle");

  /**
   * Guarded submission handler. Prevents duplicate submissions while a
   * submission animation is already in progress, then forwards the answer
   * upstream and kicks off the visual transition to `sending`.
   */
  const handleHumanSubmit = (answer?: string) => {
    if (humanSubmitPhase !== "idle") return;
    setHumanSubmitPhase("sending");
    onSendHumanAnswer(answer);
  };

  // sending -> verifying: fly-away animation window
  useEffect(() => {
    if (humanSubmitPhase !== "sending") return;
    const t = setTimeout(() => {
      setHumanSubmitPhase((prev) => (prev === "sending" ? "verifying" : prev));
    }, SENDING_ANIMATION_MS);
    return () => clearTimeout(t);
  }, [humanSubmitPhase]);

  // verifying -> success: wait until the pending human call is resolved
  // upstream, falling back to a fixed delay if the resolution signal is lost.
  useEffect(() => {
    if (humanSubmitPhase !== "verifying") return;
    if (!isHumanWaiting) {
      setHumanSubmitPhase("success");
      return;
    }
    const t = setTimeout(() => {
      setHumanSubmitPhase((prev) => (prev === "verifying" ? "success" : prev));
    }, VERIFY_FALLBACK_MS);
    return () => clearTimeout(t);
  }, [humanSubmitPhase, isHumanWaiting]);

  // success -> fading: hold the green confirmation briefly
  useEffect(() => {
    if (humanSubmitPhase !== "success") return;
    const t = setTimeout(() => setHumanSubmitPhase("fading"), SUCCESS_HOLD_MS);
    return () => clearTimeout(t);
  }, [humanSubmitPhase]);

  // fading -> idle: collapse back to baseline after fade-out completes
  useEffect(() => {
    if (humanSubmitPhase !== "fading") return;
    const t = setTimeout(() => setHumanSubmitPhase("idle"), FADING_DURATION_MS);
    return () => clearTimeout(t);
  }, [humanSubmitPhase]);

  /**
   * Idle / Sending shell:
   *  - During `idle`, this is the fully interactive card with option
   *    buttons, text input, and Send button.
   *  - During `sending`, the same DOM is kept (so the letter-fly animation
   *    has something to animate) and all interactive controls are disabled
   *    while the card transitions away.
   */
  if (humanSubmitPhase === "idle" || humanSubmitPhase === "sending") {
    return (
      <div
        className={`p-4 sm:p-5 rounded-md bg-gigbg border border-custom text-xs space-y-3.5 shadow-md origin-top-right ${
          humanSubmitPhase === "sending"
            ? "opacity-0 translate-x-[110px] -translate-y-[40px] scale-[0.8] rotate-[7deg] pointer-events-none transition-all duration-[500ms] ease-[cubic-bezier(0.34,1.2,0.64,1)]"
            : ""
        }`}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 font-semibold text-[#274AB3] dark:text-[#305CDE] text-xs">
            <HelpCircle size={15} className="shrink-0 animate-bounce" />
            <span>Agent Paused &amp; Waiting for Your Decision:</span>
          </div>
          <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-500/20 font-bold inline-flex justify-center items-center">
            <span className="relative flex h-2 w-2 mr-1">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75"></span>
              <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-500"></span>
            </span>
            Action Required
          </span>
        </div>

        <div className="p-3 rounded-sm bg-custom border border-border text-black text-sm font-medium leading-relaxed whitespace-pre-wrap">
          {detectedPrompt || "The agent requires your input to proceed."}
        </div>

        {/* Quick-pick option buttons */}
        {detectedOptions.length > 0 && (
          <div className="space-y-1.5">
            <span className="text-[11px] font-medium text-muted-foreground block">
              Select an option to proceed:
            </span>
            <div className="flex flex-wrap items-center gap-2">
              {detectedOptions.map((opt) => (
                <button
                  key={opt}
                  type="button"
                  onClick={() => handleHumanSubmit(opt)}
                  disabled={humanSubmitPhase !== "idle"}
                  className="px-3.5 py-1.5 rounded-sm text-xs capitalize bg-primary text-primary-foreground hover:bg-primary/90 transition-all shadow-xs cursor-pointer flex items-center gap-1.5 active:scale-95 disabled:opacity-60 disabled:cursor-not-allowed"
                >
                  <CheckCircle2 size={13} />
                  <span>{opt}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        <div className="flex items-center gap-2 pt-1">
          <div className="relative flex-1">
            <input
              type="text"
              value={humanAnswer}
              onChange={(e) => setHumanAnswer(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleHumanSubmit()}
              placeholder={
                detectedOptions.length > 0
                  ? "Or type a custom response to the agent..."
                  : "Type your response to the agent..."
              }
              disabled={humanSubmitPhase !== "idle"}
              className="w-full bg-background border border-border rounded-sm pl-3 pr-8 py-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-primary shadow-xs disabled:opacity-60"
            />
            <MessageSquare
              size={13}
              className="absolute right-2.5 top-2.5 text-muted-foreground/60 pointer-events-none"
            />
          </div>
          <Button
            variant="primary"
            onClick={() => handleHumanSubmit()}
            disabled={!humanAnswer.trim() || humanSubmitPhase !== "idle"}
            className="text-xs px-4 h-8.5 rounded-sm cursor-pointer font-medium"
          >
            Send
          </Button>
        </div>
      </div>
    );
  }

  if (humanSubmitPhase === "verifying") {
    return (
      <div className="p-3.5 rounded-md bg-gigbg border border-custom shadow-md flex items-center justify-center">
        <AIThinkingLoader
          message="Sending your response..."
          color="blue"
          speed={5}
          textAnimation="shimmer"
          showDots={false}
        />
      </div>
    );
  }

  // success | fading
  return (
    <div
      className={`p-3.5 rounded-md bg-emerald-500/5 border border-emerald-500/25 shadow-md flex items-center justify-center gap-2 transition-all duration-500 ease-out ${
        humanSubmitPhase === "fading" ? "opacity-0 scale-90" : "opacity-100 scale-100"
      }`}
    >
      <div className="w-6 h-6 rounded-full bg-emerald-500 flex items-center justify-center shadow-sm">
        <Check size={14} className="text-white" strokeWidth={3} />
      </div>
      <span className="text-xs font-medium text-emerald-600 dark:text-emerald-400">
        Response sent
      </span>
    </div>
  );
}

export default HumanInputCard;
