import {
  RunState,
  SSEEnvelope,
  StepGroup,
  ToolPair,
  ThoughtItem,
  HumanInputItem,
  ArtifactItem,
  RunStatus,
} from "./types";

export const initialRunState = (jobId: string, prompt: string): RunState => ({
  jobId,
  prompt,
  status: "pending",
  startedAt: Date.now(),
  steps: [],
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

    case "thought":
      return {
        ...base,
        steps: upsertGroup(state.steps, stepNum, stepId, (g) => ({
          ...g,
          items: [
            ...g.items,
            {
              kind: "thought",
              id: eventId || `th_${stepNum}_${g.items.length}`,
              text: event.data.thought || event.data.content || "",
              at: now,
            } satisfies ThoughtItem,
          ],
        })),
      };

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
              args: event.data.arguments || {},
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
      return {
        ...base,
        steps: upsertGroup(state.steps, stepNum, stepId, (g) => ({
          ...g,
          items: [
            ...g.items,
            {
              kind: "artifact",
              id: event.data.artifact_id || `art_${now}`,
              name: event.data.name || event.data.artifact || "artifact",
              path: event.data.relative_path || event.data.path || "",
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

    case "final":
    case "done":
      return {
        ...base,
        status: "completed",
        finalAnswer: event.data.result || event.data.output || "",
      };

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
      if (incomingStatus in ["pending", "queued", "starting", "running", "waiting_for_input", "retrying", "paused", "cancelling", "cancelled", "completed", "failed"]) {
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