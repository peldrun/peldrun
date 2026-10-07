"""
Sidecar Builder and Persistence Engine for peldrun-core.

Implements the non-destructive Sidecar Pattern:
Constructs, updates, and atomically persists 'session.context.json' alongside
the presentation-tier 'session.json' without mutating existing frontend records.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from peldrun.context.schema import (
    BudgetMetadata,
    BudgetRatios,
    BudgetSections,
    ContextSidecar,
    ConversationMessage,
    CurrentExecution,
    ExecutionStep,
    SummaryState,
)

SIDECAR_FILENAME = "session.context.json"
SESSION_FILENAME = "session.json"
EVENTS_FILENAME = "events.json"


def get_sidecar_path(session_dir: Union[str, Path]) -> Path:
    """Return the absolute path to the session.context.json file."""
    return Path(session_dir) / SIDECAR_FILENAME


def resolve_model_budget_profile(
    model_id: Optional[str],
    storage_root: Optional[Path] = None,
) -> Tuple[int, int, BudgetRatios]:
    """
    Resolve context_window, max_output_tokens, and section ratios for a model.

    Reads persisted configurations from models_budget.json, falling back
    gracefully to standard defaults if the profile is not yet configured.
    """
    default_context = 8192
    default_output = 1500
    default_ratios = BudgetRatios()

    if not model_id:
        return default_context, default_output, default_ratios

    # Search for models_budget.json in known paths
    candidate_paths: List[Path] = []
    if storage_root:
        candidate_paths.append(storage_root / "models_budget.json")

    # Default backend locations
    backend_root = Path(__file__).resolve().parent.parent.parent
    candidate_paths.append(backend_root / "storage" / "models_budget.json")
    candidate_paths.append(backend_root / "data" / "models_budget.json")

    for path in candidate_paths:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and model_id in data:
                        m_conf = data[model_id]
                        c_win = int(m_conf.get("context_window", default_context))
                        m_out = int(m_conf.get("max_output_tokens", default_output))

                        r_data = m_conf.get("ratios")
                        if isinstance(r_data, dict):
                            ratios = BudgetRatios(
                                system=int(r_data.get("system", 10)),
                                summary=int(r_data.get("summary", 15)),
                                recent=int(r_data.get("recent", 50)),
                                execution=int(r_data.get("execution", 15)),
                                current=int(r_data.get("current", 10)),
                            )
                        else:
                            ratios = default_ratios

                        return c_win, m_out, ratios
            except Exception:
                pass

    # Model family fallbacks
    mid = model_id.lower()
    if "gemini" in mid:
        return 1048576, 8192, default_ratios
    if "claude" in mid:
        return 200000, 8192, default_ratios
    if "gpt-4" in mid or "o1" in mid or "o3" in mid:
        return 128000, 4096, default_ratios
    if "deepseek" in mid:
        return 64000, 8192, default_ratios

    return default_context, default_output, default_ratios


def calculate_budget_slices(
    context_window: int,
    max_output_tokens: int,
    ratios: BudgetRatios,
) -> Tuple[int, BudgetSections]:
    """Calculate effective context budget and per-section token quotas."""
    effective_limit = max(100, context_window - max_output_tokens)

    sec_system = round(effective_limit * (ratios.system / 100.0))
    sec_summary = round(effective_limit * (ratios.summary / 100.0))
    sec_recent = round(effective_limit * (ratios.recent / 100.0))
    sec_execution = round(effective_limit * (ratios.execution / 100.0))
    sec_current = round(effective_limit * (ratios.current / 100.0))

    sections = BudgetSections(
        system=sec_system,
        summary=sec_summary,
        recent=sec_recent,
        execution=sec_execution,
        current=sec_current,
    )

    return effective_limit, sections


def estimate_string_tokens(text: str) -> int:
    """Heuristic token estimator (~4 characters per token)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def extract_conversation_and_execution(
    session_data: Dict[str, Any],
    events_data: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[ConversationMessage], Optional[CurrentExecution]]:
    """
    Separate high-level conversation messages from granular execution steps.
    Seamlessly supports PELDRUN schema ('prompt'/'result' as well as 'user'/'assistant').
    """
    messages: List[ConversationMessage] = []
    current_exec: Optional[CurrentExecution] = None

    raw_turns = session_data.get("turns") or []
    raw_messages = session_data.get("messages") or []

    if raw_turns and isinstance(raw_turns, list):
        for idx, turn in enumerate(raw_turns):
            if not isinstance(turn, dict):
                continue
            turn_id = str(turn.get("id") or turn.get("turn_id") or turn.get("job_id") or f"t_{idx + 1}")

            # PELDRUN standard: 'prompt' or 'user' or 'query'
            user_msg = turn.get("prompt") or turn.get("user") or turn.get("query")
            if user_msg:
                messages.append(
                    ConversationMessage(
                        turn_id=turn_id,
                        role="user",
                        content=str(user_msg).strip(),
                        timestamp=str(turn.get("timestamp") or turn.get("created_at") or ""),
                    )
                )

            # PELDRUN standard: 'result' or 'assistant' or 'response' or 'content'
            assistant_msg = turn.get("result") or turn.get("assistant") or turn.get("response") or turn.get("content")
            if assistant_msg:
                messages.append(
                    ConversationMessage(
                        turn_id=turn_id,
                        role="assistant",
                        content=str(assistant_msg).strip(),
                        timestamp=str(turn.get("completed_at") or turn.get("timestamp") or ""),
                    )
                )

            # Extract execution steps if embedded in the latest turn
            is_latest_turn = idx == len(raw_turns) - 1
            if is_latest_turn and (turn.get("steps") or turn.get("tool_calls") or turn.get("events")):
                turn_steps = turn.get("steps") or turn.get("events") or []
                steps: List[ExecutionStep] = []
                for step in turn_steps:
                    if isinstance(step, dict):
                        st_type = step.get("type", "tool_call")
                        s_data = step.get("data") if isinstance(step.get("data"), dict) else {}
                        st_name = step.get("toolName") or step.get("name") or s_data.get("toolName") or s_data.get("name")
                        st_args = step.get("args") or s_data.get("arguments") or step.get("content")
                        if isinstance(st_args, str) and st_args.startswith("{"):
                            try:
                                st_args = json.loads(st_args)
                            except Exception:
                                st_args = {"raw": st_args}
                        elif not isinstance(st_args, dict):
                            st_args = {"raw": str(st_args)} if st_args else None

                        st_res = step.get("result") or s_data.get("output") or step.get("content")
                        steps.append(
                            ExecutionStep(
                                type=st_type if st_type in ("tool_call", "observation", "status") else "status",
                                name=st_name,
                                args=st_args,
                                result=str(st_res) if st_res is not None else None,
                                error=step.get("error") or s_data.get("error"),
                            )
                        )
                current_exec = CurrentExecution(
                    turn_id=turn_id,
                    steps=steps,
                    artifacts=turn.get("artifacts") or turn.get("produced_files") or [],
                )

    elif raw_messages and isinstance(raw_messages, list):
        for idx, msg in enumerate(raw_messages):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "user")
            content = msg.get("content") or msg.get("prompt") or msg.get("result") or ""
            turn_id = str(msg.get("turn_id") or msg.get("id") or f"t_{idx + 1}")

            if role in ("user", "assistant", "system") and content:
                messages.append(
                    ConversationMessage(
                        turn_id=turn_id,
                        role=role,
                        content=str(content).strip(),
                        timestamp=str(msg.get("timestamp") or ""),
                    )
                )

    # Process events.json telemetry with true PELDRUN event schema
    if events_data and isinstance(events_data, list):
        latest_turn_id = messages[-1].turn_id if messages else "turn_active"
        if not current_exec:
            current_exec = CurrentExecution(turn_id=latest_turn_id, steps=[], artifacts=[])

        for ev in events_data:
            if not isinstance(ev, dict):
                continue
            ev_type = ev.get("type", "observation")
            ev_data = ev.get("data") if isinstance(ev.get("data"), dict) else {}

            if ev_type in ("tool_call", "tool_called"):
                tool_name = ev.get("toolName") or ev.get("name") or ev_data.get("toolName") or ev_data.get("name")
                raw_args = ev.get("args") or ev_data.get("arguments") or ev.get("content")
                parsed_args = None
                if isinstance(raw_args, dict):
                    parsed_args = raw_args
                elif isinstance(raw_args, str) and raw_args.strip().startswith("{"):
                    try:
                        parsed_args = json.loads(raw_args)
                    except Exception:
                        parsed_args = {"raw": raw_args}
                elif raw_args:
                    parsed_args = {"raw": str(raw_args)}

                current_exec.steps.append(
                    ExecutionStep(
                        type="tool_call",
                        name=tool_name,
                        args=parsed_args,
                        result=None,
                        error=None,
                    )
                )
            elif ev_type in ("observation", "tool_completed"):
                obs_result = ev.get("content") or ev.get("result") or ev_data.get("output") or ev_data.get("content")
                current_exec.steps.append(
                    ExecutionStep(
                        type="observation",
                        name=None,
                        args=None,
                        result=str(obs_result) if obs_result is not None else "",
                        error=ev.get("error") or ev_data.get("error"),
                    )
                )
            elif ev_type == "artifact_created":
                artifact_name = ev_data.get("artifact") or ev_data.get("path") or ev.get("toolName")
                if artifact_name and artifact_name not in current_exec.artifacts:
                    current_exec.artifacts.append(str(artifact_name))

    return messages, current_exec


def build_context_sidecar(
    session_id: str,
    session_data: Dict[str, Any],
    events_data: Optional[List[Dict[str, Any]]] = None,
    existing_sidecar: Optional[ContextSidecar] = None,
    model_id: Optional[str] = None,
    storage_root: Optional[Path] = None,
) -> ContextSidecar:
    """
    Construct a complete ContextSidecar instance from raw session and events data.

    Respects existing compaction summaries and resolves per-model token limits.
    """
    target_model = model_id or session_data.get("model") or (existing_sidecar.model_id if existing_sidecar else None)

    # 1. Resolve limits and dynamic ratios
    context_window, max_output_tokens, ratios = resolve_model_budget_profile(
        target_model, storage_root=storage_root
    )
    effective_limit, sections = calculate_budget_slices(
        context_window, max_output_tokens, ratios
    )

    # 2. Extract separated conversation and execution state
    conversation, current_exec = extract_conversation_and_execution(
        session_data, events_data
    )

    # 3. Preserve or initialize summary state
    summary = existing_sidecar.summary if existing_sidecar else SummaryState()

    # 4. Calculate total prompt tokens currently consumed
    total_tokens = summary.tokens
    for msg in conversation:
        total_tokens += estimate_string_tokens(msg.content)
    if current_exec:
        for st in current_exec.steps:
            if st.result:
                total_tokens += estimate_string_tokens(st.result)

    # 5. Assemble budget metadata
    budget = BudgetMetadata(
        total_tokens=total_tokens,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        effective_limit=effective_limit,
        ratios=ratios,
        sections=sections,
    )

    return ContextSidecar(
        session_id=session_id,
        schema_version=1,
        built_at=datetime.now(timezone.utc).isoformat(),
        model_id=target_model,
        summary=summary,
        conversation=conversation,
        current_execution=current_exec,
        budget=budget,
    )


def load_context_sidecar(session_dir: Union[str, Path]) -> Optional[ContextSidecar]:
    """Load and parse session.context.json from the session folder if present."""
    sidecar_path = get_sidecar_path(session_dir)
    if not sidecar_path.exists():
        return None

    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return ContextSidecar(**data)
    except Exception as err:
        print(f"[Sidecar] Error loading sidecar at {sidecar_path}: {err}")
        return None


def save_context_sidecar(
    session_dir: Union[str, Path],
    sidecar: ContextSidecar,
) -> Path:
    """
    Persist the ContextSidecar to disk atomically via a temporary file.

    Guarantees no partial reads by concurrent threads or processes.
    """
    target_dir = Path(session_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    sidecar_path = target_dir / SIDECAR_FILENAME
    temp_path = target_dir / f"{SIDECAR_FILENAME}.tmp"

    data_dict = sidecar.model_dump() if hasattr(sidecar, "model_dump") else sidecar.dict()

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data_dict, f, indent=2, ensure_ascii=False)

    temp_path.replace(sidecar_path)
    return sidecar_path