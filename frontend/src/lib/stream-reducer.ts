// frontend/src/lib/stream-reducer.ts
import {
  RunState,
  SSEEnvelope,
  StepGroup,
  ToolPair,
  ThoughtItem,
  HumanInputItem,
  ArtifactItem,
  RunStatus,
  ChildRunState,
  DelegationApprovalRequest,
} from "./types";

export const initialRunState = (jobId: string, prompt: string): RunState => ({
  jobId,
  prompt,
  status: "pending",
  startedAt: Date.now(),
  steps: [],
  childRuns: {},
  pendingApprovals: {},
  finalAnswer: null,
  error: null,
  lastEventAt: Date.now(),
  droppedEvents: 0,
  lastSequence: 0,
  seenEventIds: [],
});

function getGroupId(stepNum: number, stepId?: string): string {
  return stepId || `step_${stepNum}`;
}

function upsertGroup(
  steps: StepGroup[],
  stepNum: number,
  stepId: string | undefined,
  updater: (group: StepGroup) => StepGroup
): StepGroup[] {
  const targetId = getGroupId(stepNum, stepId);
  const index = steps.findIndex((g) => g.id === targetId || g.step === stepNum);
  if (index === -1) {
    const newGroup: StepGroup = {
      id: targetId,
      step: stepNum,
      step_id: stepId || targetId,
      startedAt: Date.now(),
      finishedAt: null,
      durationMs: null,
      items: [],
    };
    return [...steps, updater(newGroup)];
  }
  const next = [...steps];
  next[index] = updater(next[index]);
  return next;
}

function patchTool(
  steps: StepGroup[],
  stepNum: number,
  toolCallId: string | undefined,
  stepId: string | undefined,
  updater: (t: ToolPair) => ToolPair
): StepGroup[] {
  const targetId = getGroupId(stepNum, stepId);
  let patched = false;
  return steps.map((g) => {
    const isTargetGroup = g.id === targetId || g.step === stepNum;
    const containsTool = toolCallId ? g.items.some((i) => i.kind === "tool" && i.id === toolCallId) : false;
    if (!isTargetGroup && !containsTool) return g;

    const items = g.items.map((item) => {
      if (item.kind === "tool" && (!toolCallId || item.id === toolCallId) && !patched) {
        patched = true;
        return updater(item);
      }
      return item;
    });
    return { ...g, items };
  });
}

export function reduceEvent(state: RunState, event: SSEEnvelope): RunState {
  const eventId =
    event.event_id ||
    event.id ||
    (event.data && (event.data.event_id || event.data.id));

  const eventSeq =
    event.sequence ??
    event.seq ??
    (event.data && (event.data.sequence ?? event.data.seq)) ??
    0;

  // Deduplication check: drop duplicate events received across reconnects
  if (eventId && state.seenEventIds.includes(eventId)) {
    return { ...state, droppedEvents: state.droppedEvents + 1 };
  }

  const now = Date.now();
  const nextSeenEventIds = eventId
    ? state.seenEventIds.length > 500
      ? [...state.seenEventIds.slice(-499), eventId]
      : [...state.seenEventIds, eventId]
    : state.seenEventIds;

  const nextSequence = Math.max(state.lastSequence || 0, eventSeq);
  const base: RunState = {
    ...state,
    lastEventAt: now,
    lastSequence: nextSequence,
    seenEventIds: nextSeenEventIds,
    childRuns: { ...(state.childRuns || {}) },
    pendingApprovals: { ...(state.pendingApprovals || {}) },
  };

  const stepNum = event.step || 1;
  const stepId = event.step_id || (event.data && event.data.step_id);

  switch (event.type) {
    case "step_start":
      return {
        ...base,
        status: "running",
        steps: upsertGroup(state.steps, stepNum, stepId, (g) => ({ ...g, startedAt: now })),
      };

    case "thought": {
      // [FIX-THOUGHT-RESET] Handle the retry-reset sentinel emitted
      // by the backend BEFORE each retry attempt. The reset event has
      // no ``thought`` / ``content`` field, so the historical code
      // would (a) NOT clear the failed attempt's reasoning, AND
      // (b) append a new ``ThoughtItem`` with ``text=""`` on every
      // reset — producing a trail of empty bubbles in the timeline.
      //
      // The reset must REMOVE the failed attempt's thought items from
      // the current step so a retry's reasoning starts fresh. Thought
      // items from earlier COMPLETED steps are preserved because the
      // reset only fires for the CURRENT step (the backend correlates
      // resets with the active attempt).
      if (event.data.reset === true) {
        const targetId = getGroupId(stepNum, stepId);
        return {
          ...base,
          steps: state.steps.map((g) => {
            if (g.id !== targetId && g.step !== stepNum) return g;
            return {
              ...g,
              items: g.items.filter((i) => i.kind !== "thought"),
            };
          }),
        };
      }

      // [FIX-THOUGHT-EMPTY] Reject events that carry no thought text.
      // The historical code appended an empty ``ThoughtItem`` for
      // every such event (resets being the most common case, but any
      // whitespace-only or missing-field event hit the same bug).
      // Empty items render as empty bubbles and inflate the item
      // count shown in the step header.
      const thoughtText =
        (typeof event.data.thought === "string" && event.data.thought) ||
        (typeof event.data.content === "string" && event.data.content) ||
        "";
      if (!thoughtText) return base;

      return {
        ...base,
        steps: upsertGroup(state.steps, stepNum, stepId, (g) => ({
          ...g,
          items: [
            ...g.items,
            {
              kind: "thought",
              id: eventId || `th_${stepNum}_${g.items.length}`,
              text: thoughtText,
              at: now,
            } satisfies ThoughtItem,
          ],
        })),
      };
    }

    // Streaming content delta from agent mode (EventType.CHUNK).
    // Accumulated into `finalAnswer` so the deliverable bubble renders
    // the response as it is generated. The `final` event later
    // overwrites (or confirms) the same field with the authoritative
    // text — see `case "final"` below.
    //
    // Before this branch existed, CHUNK events fell through to
    // `default` and were silently dropped, which had two visible
    // symptoms:
    //   1. During streaming: no visible output → the run looked frozen.
    //   2. On completion: `run_result.output` was generic → the final
    //      bubble showed either nothing or an unrelated reasoning
    //      fragment.
    case "chunk": {
      // [FIX-CHUNK-RESET] Handle the retry-reset sentinel emitted by
      // the backend BEFORE each retry attempt. The reset payload has
      // no ``delta`` / ``text`` / ``content`` field — the historical
      // code therefore skipped it entirely (``if (!delta) return base``)
      // and left ``finalAnswer`` with the failed attempt's text. When
      // the retry subsequently streamed a shorter or malformed
      // response, the reducer APPENDED it on top of the stale text,
      // producing a corrupted concatenation that was neither attempt
      // in isolation.
      //
      // The reset must clear ``finalAnswer`` so the retry starts from
      // a clean slate. ``attempt_id`` is preserved for diagnostics —
      // a future reducer extension could correlate chunks to their
      // originating attempt without a second reset check.
      if (event.data.reset === true) {
        return {
          ...base,
          status: "running",
          finalAnswer: "",
        };
      }

      const delta =
        event.data.delta || event.data.text || event.data.content || "";
      if (!delta) return base;
      return {
        ...base,
        status: "running",
        finalAnswer: (state.finalAnswer || "") + delta,
      };
    }

    case "tool_call":
    case "tool_called": {
      const toolCallId =
        event.data.tool_call_id ||
        event.data.toolCallId ||
        `tc_${stepNum}_${Date.now()}`;

      const toolName =
        event.data.tool_name ||
        event.data.toolName ||
        event.data.tool ||
        "unknown_tool";

      const isHumanAsk =
        toolName === "ask_human" ||
        toolName === "human_input" ||
        Boolean(event.data.requires_input || event.data.requiresInput);

      // [FIX] Parse arguments if they are sent as a stringified JSON
      const rawArgs = event.data.arguments || event.data.args || {};
      let parsedArgs = rawArgs;
      if (typeof rawArgs === "string") {
        try {
          parsedArgs = JSON.parse(rawArgs);
        } catch {
          parsedArgs = { raw: rawArgs };
        }
      }

      return {
        ...base,
        status: isHumanAsk ? "waiting_for_input" : base.status,
        steps: upsertGroup(state.steps, stepNum, stepId, (g) => ({
          ...g,
          items: [
            ...g.items,
            {
              kind: "tool",
              id: toolCallId,
              tool: toolName,
              args: parsedArgs,
              startedAt: now,
              output: null,
              durationMs: null,
              attempt: event.data.attempt || 1,
              maxAttempts: event.data.max_attempts || event.data.maxAttempts || 1,
              status: isHumanAsk ? "waiting_for_input" : "running",
              requiresInput: isHumanAsk,
              requestId: event.data.request_id || event.data.requestId,
            } satisfies ToolPair,
          ],
        })),
      };
    }

    case "tool_retry": {
      const toolCallId = event.data.tool_call_id || event.data.toolCallId;
      return {
        ...base,
        status: "retrying",
        steps: patchTool(state.steps, stepNum, toolCallId, stepId, (t) => ({
          ...t,
          attempt: event.data.attempt || (t.attempt ? t.attempt + 1 : 2),
          maxAttempts: event.data.max_attempts || t.maxAttempts || 3,
          status: "retrying",
          error: event.data.error || "Retrying after transient error",
          retryDelay: event.data.delay_seconds || event.data.delaySeconds || 0,
        })),
      };
    }

    // ==========================================
    // Tool Output Streaming — raw stdout/stderr
    // ==========================================
    //
    // Emitted WHILE a tool is executing (between its TOOL_CALL and
    // OBSERVATION). Each event carries a `sequence` (monotonic per
    // attempt, starting at 1) and an `attempt` (1-based retry index).
    //
    // Processing order matters:
    //
    //   1. Drop stale attempts first. After a retry, chunks from the
    //      previous attempt must not interleave with the new one's,
    //      even if the transport delivers them out of order.
    //   2. If `attempt` is NEW, clear the previous attempt's visible
    //      text (`liveOutput` / `liveStreams`) and start fresh. This
    //      is what guarantees a failed attempt's output does not
    //      bleed into the successful one.
    //   3. If `attempt` is the same (or first event), drop duplicates
    //      using `sequence`. This is the at-least-once delivery fix:
    //      after an SSE reconnect that replays a few chunks, the
    //      reducer silently skips the repeats.
    //   4. Append and update the sequence / attempt high-water marks.
    //
    // The live stream is deliberately kept INDEPENDENT of the final
    // `output` field (populated by OBSERVATION). Observation does
    // NOT clear `liveOutput`: the user watched that text stream in
    // and it stays visible as the historical record of the run.
    case "tool_output": {
      const toolCallId = event.data.tool_call_id || event.data.toolCallId;
      if (!toolCallId) return base;

      // Narrow the wire-level `stream` string to the exact union that
      // both the backend's `Literal` and `ToolPair.liveStreams` agree
      // on. Any unexpected value falls back to "stdout" so a rogue
      // emitter cannot introduce a fourth key into `liveStreams`
      // (which TypeScript would reject at the indexing site below).
      const rawStream = String(event.data.stream || "stdout");
      const stream: "stdout" | "stderr" | "output" =
        rawStream === "stderr"
          ? "stderr"
          : rawStream === "output"
          ? "output"
          : "stdout";

      const chunk = String(event.data.chunk || "");
      const sequence = typeof event.data.sequence === "number" ? event.data.sequence : 0;
      const attempt = typeof event.data.attempt === "number" ? event.data.attempt : 1;

      if (!chunk) return base;

      return {
        ...base,
        steps: patchTool(state.steps, stepNum, toolCallId, stepId, (t) => {
          // (1) Stale attempt — drop.
          if (typeof t.lastAttempt === "number" && attempt < t.lastAttempt) {
            return t;
          }
          // (2) New attempt — reset the visible buffer.
          if (typeof t.lastAttempt === "number" && attempt > t.lastAttempt) {
            return {
              ...t,
              liveOutput: chunk,
              liveStreams: { [stream]: chunk },
              outputSequence: sequence,
              lastAttempt: attempt,
            };
          }
          // (3) Same attempt — dedupe by sequence.
          if (
            typeof t.outputSequence === "number" &&
            sequence <= t.outputSequence
          ) {
            return t;
          }
          // (4) Append.
          const prevStreams = t.liveStreams || {};
          return {
            ...t,
            liveOutput: (t.liveOutput || "") + chunk,
            liveStreams: {
              ...prevStreams,
              [stream]: (prevStreams[stream] || "") + chunk,
            },
            outputSequence: sequence,
            lastAttempt: attempt,
          };
        }),
      };
    }

    // ==========================================
    // Tool Output Streaming — structured progress
    // ==========================================
    //
    // Same sequence/attempt contract as `tool_output`, but the payload
    // carries `current` / `total` / `phase` instead of a raw chunk.
    // Rendered as a progress bar, NOT appended to the text area.
    //
    // `outputSequence` is SHARED with `tool_output` because both
    // events are emitted by the same context object and share a
    // single monotonic counter. Splitting the dedup state into two
    // fields would create an inconsistent window: a progress chunk
    // with sequence=5 would be accepted, then a stdout chunk with
    // sequence=4 would be appended out of order. Sharing the counter
    // prevents that.
    case "tool_progress": {
      const toolCallId = event.data.tool_call_id || event.data.toolCallId;
      if (!toolCallId) return base;

      const sequence = typeof event.data.sequence === "number" ? event.data.sequence : 0;
      const attempt = typeof event.data.attempt === "number" ? event.data.attempt : 1;
      const current = typeof event.data.current === "number" ? event.data.current : undefined;
      const total = typeof event.data.total === "number" ? event.data.total : undefined;
      const phase = typeof event.data.phase === "string" ? event.data.phase : undefined;
      const message =
        typeof event.data.chunk === "string"
          ? event.data.chunk
          : typeof event.data.message === "string"
          ? event.data.message
          : undefined;

      return {
        ...base,
        steps: patchTool(state.steps, stepNum, toolCallId, stepId, (t) => {
          // Stale attempt — drop.
          if (typeof t.lastAttempt === "number" && attempt < t.lastAttempt) {
            return t;
          }
          // Sequence dedupe (shared with tool_output).
          if (
            typeof t.outputSequence === "number" &&
            sequence <= t.outputSequence
          ) {
            return t;
          }
          return {
            ...t,
            progress: { current, total, phase, message },
            outputSequence: sequence,
            lastAttempt: attempt,
          };
        }),
      };
    }

    case "observation": {
      const toolCallId = event.data.tool_call_id || event.data.toolCallId;
      const isError = Boolean(event.data.is_error || event.data.isError);
      return {
        ...base,
        status: base.status === "retrying" ? "running" : base.status,
        steps: patchTool(state.steps, stepNum, toolCallId, stepId, (t) => ({
          ...t,
          output: event.data.output ?? "",
          durationMs: now - t.startedAt,
          isError: isError,
          status: isError ? "failed" : "completed",
        })),
      };
    }

    case "ask_human": {
      const reqId = event.data.request_id || event.data.requestId || `hi_${now}`;
      const question = event.data.question || event.data.prompt || "";
      const options = event.data.options || [];
      const inputType = event.data.input_type || "text";

      return {
        ...base,
        status: "waiting_for_input",
        steps: upsertGroup(state.steps, stepNum, stepId, (g) => ({
          ...g,
          items: [
            ...g.items,
            {
              kind: "human_input",
              id: reqId,
              requestId: reqId,
              question,
              options,
              inputType,
              status: "pending",
              at: now,
            } satisfies HumanInputItem,
          ],
        })),
      };
    }

    case "artifact_created":
    case "artifact_updated":
    case "artifact_deleted": {
      const op = (event.type.replace("artifact_", "") as any) || "created";
      const artifactName = event.data.name || event.data.artifact || "artifact";
      const artifactPath = event.data.relative_path || event.data.path || "";
      const sizeText = event.data.size_bytes ? ` (${event.data.size_bytes} bytes)` : "";

      // [FIX] If the backend didn't send an 'observation' for this tool,
      // we synthesize an output message from the artifact event so the user sees the result.
      const updatedSteps = patchTool(state.steps, stepNum, undefined, stepId, (t) => {
        if (!t.output || t.output === "") {
          return {
            ...t,
            output: `File ${op}: ${artifactPath}${sizeText}`,
            status: "completed",
            isError: false,
          };
        }
        return t;
      });

      return {
        ...base,
        steps: upsertGroup(updatedSteps, stepNum, stepId, (g) => ({
          ...g,
          items: [
            ...g.items,
            {
              kind: "artifact",
              id: event.data.artifact_id || `art_${now}`,
              name: artifactName,
              path: artifactPath,
              operation: op,
              sizeBytes: event.data.size_bytes,
              revision: event.data.revision,
              at: now,
            } satisfies ArtifactItem,
          ],
        })),
      };
    }

    case "step_end":
      return {
        ...base,
        steps: upsertGroup(state.steps, stepNum, stepId, (g) => ({
          ...g,
          finishedAt: now,
          durationMs: event.data.durationMs ?? (g.startedAt ? now - g.startedAt : null),
        })),
      };

    // ==========================================
    // Multi-Agent Event Reduction Handlers
    // ==========================================
    case "approval_required": {
      const approvalId = event.data.approval_request_id || `appr_${now}`;
      const approvalItem: DelegationApprovalRequest = {
        approvalRequestId: approvalId,
        taskId: event.data.task_id || "",
        parentRunId: event.data.parent_run_id || "",
        targetAgentId: event.data.target_agent_id || "specialist",
        instruction: event.data.instruction || "",
        status: "pending",
        timeoutSeconds: event.data.timeout_seconds || 300,
        createdAt: now,
      };

      return {
        ...base,
        status: "waiting_for_input",
        pendingApprovals: {
          ...base.pendingApprovals,
          [approvalId]: approvalItem,
        },
      };
    }

    case "approval_resolved": {
      const approvalId = event.data.approval_request_id || "";
      const currentApprovals = { ...(base.pendingApprovals || {}) };
      if (approvalId && currentApprovals[approvalId]) {
        delete currentApprovals[approvalId];
      }

      return {
        ...base,
        status: base.status === "waiting_for_input" ? "running" : base.status,
        pendingApprovals: currentApprovals,
      };
    }

    case "child_run_started": {
      const childId = event.data.child_run_id || `child_${now}`;
      const newChild: ChildRunState = {
        childRunId: childId,
        parentRunId: event.data.parent_run_id || "",
        taskId: event.data.task_id || "",
        targetAgentId: event.data.target_agent_id || "specialist",
        instruction: event.data.instruction || "",
        status: "running",
        startedAt: now,
        steps: [],
        deliverables: [],
        tokensUsed: 0,
      };

      return {
        ...base,
        childRuns: {
          ...base.childRuns,
          [childId]: newChild,
        },
      };
    }

    case "child_run_step": {
      const childId =
        event.metadata?.child_run_id ||
        event.data.child_run_id ||
        Object.keys(base.childRuns || {}).slice(-1)[0];

      if (!childId || !base.childRuns?.[childId]) {
        return base;
      }

      const existingChild = base.childRuns[childId];
      const updatedSteps = upsertGroup(existingChild.steps, stepNum, stepId, (g) => ({
        ...g,
        startedAt: g.startedAt || now,
      }));

      return {
        ...base,
        childRuns: {
          ...base.childRuns,
          [childId]: {
            ...existingChild,
            steps: updatedSteps,
          },
        },
      };
    }

    case "child_run_completed":
    case "child_run_failed":
    case "child_run_cancelled": {
      const childId =
        event.metadata?.child_run_id ||
        event.data.child_run_id ||
        Object.keys(base.childRuns || {}).slice(-1)[0];

      if (!childId || !base.childRuns?.[childId]) {
        return base;
      }

      const outcomeStatus =
        event.type === "child_run_completed"
          ? "completed"
          : event.type === "child_run_cancelled"
          ? "cancelled"
          : "failed";

      const existingChild = base.childRuns[childId];
      return {
        ...base,
        childRuns: {
          ...base.childRuns,
          [childId]: {
            ...existingChild,
            status: outcomeStatus,
            finishedAt: now,
            durationSeconds: event.data.duration_seconds || (now - existingChild.startedAt) / 1000,
            deliverables: event.data.deliverables || existingChild.deliverables,
            tokensUsed: event.data.tokens_used || existingChild.tokensUsed,
            costNanoUsd: event.data.cost_nano_usd || existingChild.costNanoUsd,
            error: event.data.error || null,
          },
        },
      };
    }

    case "final":
    case "done": {
      // Preserve accumulated streaming content when the FINAL payload
      // is empty. Background: agent mode may synthesize a FINAL with
      // `result=""` when the run terminated via a tool that did not
      // echo content back. The streamed `finalAnswer` already holds
      // the authoritative text in that case, so overwriting it with
      // "" would erase the visible output the user just watched
      // stream in.
      const authoritative =
        event.data.result || event.data.output || "";
      const finalAnswer =
        authoritative || state.finalAnswer || "";
      return {
        ...base,
        status: "completed",
        finalAnswer,
      };
    }

    case "cancelled":
      return {
        ...base,
        status: "cancelled",
        error: event.data.message || "Execution cancelled by operator",
      };

    case "error":
      return {
        ...base,
        status:
          event.data.code === "cancelled" || event.data.status === "cancelled"
            ? "cancelled"
            : "failed",
        error: event.data.message || event.data.error || "Execution failed",
      };

    case "status":
    case "run_started":
    case "run_accepted": {
      const incomingStatus = (event.data.status || event.data.state || "").toLowerCase();
      let mappedStatus: RunStatus = state.status;
      if (
        [
          "pending",
          "queued",
          "starting",
          "running",
          "waiting_for_input",
          "retrying",
          "paused",
          "cancelling",
          "cancelled",
          "completed",
          "failed",
          // Interrupted is included in the accepted list so that a
          // recovery event (or a reload after a crash) can propagate
          // the canonical terminal state into the reducer.
          "interrupted",
        ].includes(incomingStatus)
      ) {
        mappedStatus = incomingStatus as RunStatus;
      }
      return {
        ...base,
        status: mappedStatus,
      };
    }

    case "ping":
      return base;

    default:
      return { ...base, droppedEvents: state.droppedEvents + 1 };
  }
}
