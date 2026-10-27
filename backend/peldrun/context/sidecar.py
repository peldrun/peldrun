# Path: backend/peldrun/context/sidecar.py

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from peldrun.context.schema import (
    BudgetMetadata,
    BudgetRatios,
    BudgetSections,
    ContextSidecar,
    ConversationMessage,
    CurrentExecution,
    ExecutionStatus,
    ExecutionStep,
    SummaryState,
)

logger = logging.getLogger("peldrun.context.sidecar")

SIDECAR_FILENAME = "session.context.json"
SESSION_FILENAME = "session.json"
EVENTS_FILENAME = "events.json"

ACTIVE_STATUSES: Set[str] = {"running", "waiting_for_user"}
TERMINAL_STATUSES: Set[str] = {"succeeded", "failed", "cancelled"}
TERMINAL_EVENT_TYPES: Set[str] = {"final", "done", "error"}


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

    candidate_paths: List[Path] = []
    if storage_root:
        candidate_paths.append(storage_root / "models_budget.json")

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
            except Exception as err:
                logger.debug("Failed reading model budget from %s: %s", path, err)

    # Tier 2: consult the models vault, which stores the max context
    # reported by the model server itself (LM Studio, Ollama, and
    # cloud endpoints all populate this field).
    #
    # This is the authoritative source for models that were
    # discovered but not yet configured in Model Budgets. Without
    # this lookup, a freshly added local model (e.g. qwen3:32b with
    # a 262K window) would silently fall through to the 8192
    # default and be truncated to 3% of its capacity.
    vault_paths: List[Path] = []
    if storage_root:
        vault_paths.append(storage_root / "models_vault.json")
    backend_root = Path(__file__).resolve().parent.parent.parent
    vault_paths.append(backend_root / "data" / "models_vault.json")
    vault_paths.append(backend_root / "storage" / "models_vault.json")

    for vpath in vault_paths:
        if not vpath.exists():
            continue
        try:
            with open(vpath, "r", encoding="utf-8") as f:
                vault = json.load(f)
        except Exception as err:
            logger.debug("Failed reading models_vault from %s: %s", vpath, err)
            continue

        max_ctx = _lookup_context_in_vault(vault, model_id)
        if max_ctx is not None and max_ctx > 0:
            # Preserve the model's true context window; reserve a
            # conservative output budget (25% of the window, capped
            # to 8192, floored at 1500) so large models still get a
            # usable input budget.
            reserve = max(1500, min(8192, max_ctx // 4))
            return max_ctx, reserve, default_ratios

    # Tier 3: extended model-family heuristic fallbacks.
    # These cover the common families shipped by major providers and
    # the open-weight ecosystem. They run only when the model is
    # missing from both models_budget.json (user explicit) and
    # models_vault.json (server-reported). See audit report item 3.2.
    mid = model_id.lower()

    # Google
    if "gemini-2" in mid or "gemini-1.5" in mid or "gemini-exp" in mid:
        return 1048576, 8192, default_ratios
    if "gemini" in mid:
        return 32768, 4096, default_ratios

    # Anthropic
    if "claude-3-7" in mid or "claude-3.7" in mid or "claude-3-5" in mid or "claude-3.5" in mid:
        return 200000, 8192, default_ratios
    if "claude" in mid:
        return 200000, 4096, default_ratios

    # OpenAI
    if "gpt-4o" in mid or "gpt-4-turbo" in mid or "o1" in mid or "o3" in mid or "o4" in mid:
        return 128000, 4096, default_ratios
    if "gpt-4" in mid:
        return 8192, 4096, default_ratios
    if "gpt-3.5" in mid:
        return 16384, 4096, default_ratios

    # DeepSeek
    if "deepseek-r1" in mid or "deepseek-v3" in mid or "deepseek-v2" in mid:
        return 131072, 8192, default_ratios
    if "deepseek" in mid:
        return 64000, 8192, default_ratios

    # Meta Llama
    if "llama-3.1" in mid or "llama-3.2" in mid or "llama-3.3" in mid:
        return 131072, 4096, default_ratios
    if "llama-3" in mid:
        return 8192, 4096, default_ratios

    # Alibaba Qwen (very common in local-first setups)
    if "qwen3" in mid:
        return 262144, 8192, default_ratios
    if "qwen2.5" in mid or "qwen2-5" in mid:
        return 131072, 8192, default_ratios
    if "qwen2" in mid:
        return 32768, 4096, default_ratios

    # Mistral
    if "mistral-large" in mid or "codestral" in mid:
        return 131072, 8192, default_ratios
    if "mistral" in mid:
        return 32768, 4096, default_ratios

    # Google Gemma
    if "gemma-3" in mid or "gemma-4" in mid:
        return 131072, 4096, default_ratios
    if "gemma" in mid:
        return 8192, 2048, default_ratios

    # Microsoft Phi
    if "phi-4" in mid:
        return 16384, 4096, default_ratios
    if "phi-3" in mid:
        return 131072, 4096, default_ratios

    # NVIDIA Nemotron
    if "nemotron" in mid:
        return 1048576, 8192, default_ratios

    # Tier 4: safe fallback for genuinely unknown models.
    # We deliberately keep this conservative (8192/1500) rather than
    # guessing a larger window. A too-small window degrades quality;
    # a too-large window triggers a hard error from the provider.
    # Users who need more can configure the model in Model Budgets.
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


def is_active_turn(turn: Dict[str, Any]) -> bool:
    """
    Determines whether a turn represents an ongoing, active execution.
    Prioritizes explicit status attribute when provided.
    Maintains backward compatibility for legacy records lacking status fields.
    """
    status = turn.get("status")
    if status is not None:
        return str(status).lower() in ACTIVE_STATUSES

    # Legacy compatibility fallback: If no explicit status exists,
    # a turn with recorded steps/artifacts is treated as active only if
    # it is not explicitly marked completed via terminal flags.
    if turn.get("completed") is True:
        return False

    return True


def event_run_id(event: Dict[str, Any]) -> Optional[str]:
    """Extract run or job identifier from an event wrapper."""
    val = event.get("run_id") or event.get("job_id")
    if val is not None:
        return str(val)
    payload = event.get("data")
    if isinstance(payload, dict):
        p_val = payload.get("run_id") or payload.get("job_id")
        if p_val is not None:
            return str(p_val)
    return None


def select_current_run_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Filters events to represent only the active execution run.
    Ensures older terminal executions do not mask a newer ongoing run.
    """
    if not events:
        return []

    # 1. Inspect run_id of the latest recorded event
    latest_run_id: Optional[str] = None
    for ev in reversed(events):
        if isinstance(ev, dict):
            rid = event_run_id(ev)
            if rid:
                latest_run_id = rid
                break

    if latest_run_id:
        scoped = [
            ev for ev in events
            if isinstance(ev, dict) and event_run_id(ev) == latest_run_id
        ]
        # If this scoped run already has a terminal event, it is not active
        if any(ev.get("type") in TERMINAL_EVENT_TYPES for ev in scoped):
            return []
        return scoped

    # 2. Legacy fallback: slice events following the last terminal event
    last_term_idx = -1
    for idx, ev in enumerate(events):
        if isinstance(ev, dict) and ev.get("type") in TERMINAL_EVENT_TYPES:
            last_term_idx = idx

    if last_term_idx >= 0:
        trailing = events[last_term_idx + 1:]
        return [ev for ev in trailing if isinstance(ev, dict)]

    return [ev for ev in events if isinstance(ev, dict)]


def extract_conversation_and_execution(
    session_data: Dict[str, Any],
    events_data: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[ConversationMessage], Optional[CurrentExecution]]:
    """
    Separate high-level conversation messages from granular execution steps.
    Seamlessly supports PELDRUN schema ('prompt'/'result' as well as 'user'/'assistant').

    Guarantees Architectural Invariant (RFC-609):
    Completed turns NEVER leak their historical tool steps into CurrentExecution.
    Only active, ongoing turns retain an execution slice.
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

            assistant_msg = (
                turn.get("result")
                or turn.get("assistant")
                or turn.get("response")
                or turn.get("content")
            )
            is_turn_completed = bool(assistant_msg and str(assistant_msg).strip())

            if is_turn_completed:
                messages.append(
                    ConversationMessage(
                        turn_id=turn_id,
                        role="assistant",
                        content=str(assistant_msg).strip(),
                        timestamp=str(turn.get("completed_at") or turn.get("timestamp") or ""),
                    )
                )

            # Capture execution telemetry ONLY if this is the latest turn AND it is active
            is_latest_turn = idx == len(raw_turns) - 1
            if is_latest_turn and is_active_turn(turn):
                turn_steps = turn.get("steps") or turn.get("events") or []
                turn_artifacts = turn.get("artifacts") or turn.get("produced_files") or []

                if turn_steps or turn_artifacts:
                    steps: List[ExecutionStep] = []
                    for step in turn_steps:
                        if isinstance(step, dict):
                            st_type = step.get("type", "tool_call")
                            s_data = step.get("data") if isinstance(step.get("data"), dict) else {}
                            st_name = (
                                step.get("toolName")
                                or step.get("name")
                                or s_data.get("toolName")
                                or s_data.get("name")
                            )
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
                    raw_status = str(turn.get("status") or "running").lower()
                    eff_status: ExecutionStatus = (
                        raw_status if raw_status in ACTIVE_STATUSES else "running"  # type: ignore
                    )
                    current_exec = CurrentExecution(
                        turn_id=turn_id,
                        status=eff_status,
                        steps=steps,
                        artifacts=turn_artifacts,
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

    # Process events.json ONLY if representing an active ongoing execution run
    if events_data and isinstance(events_data, list):
        active_events = select_current_run_events(events_data)
        if active_events:
            latest_turn_id = messages[-1].turn_id if messages else "turn_active"
            if not current_exec:
                current_exec = CurrentExecution(
                    turn_id=latest_turn_id,
                    status="running",
                    steps=[],
                    artifacts=[],
                )

            for ev in active_events:
                ev_type = ev.get("type", "observation")
                ev_data = ev.get("data") if isinstance(ev.get("data"), dict) else {}

                if ev_type in ("tool_call", "tool_called"):
                    tool_name = (
                        ev.get("toolName")
                        or ev.get("name")
                        or ev_data.get("toolName")
                        or ev_data.get("name")
                    )
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
                    obs_result = (
                        ev.get("content")
                        or ev.get("result")
                        or ev_data.get("output")
                        or ev_data.get("content")
                    )
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

    context_window, max_output_tokens, ratios = resolve_model_budget_profile(
        target_model, storage_root=storage_root
    )
    effective_limit, sections = calculate_budget_slices(
        context_window, max_output_tokens, ratios
    )

    conversation, current_exec = extract_conversation_and_execution(
        session_data, events_data
    )

    summary = existing_sidecar.summary if existing_sidecar else SummaryState()

    total_tokens = summary.tokens
    for msg in conversation:
        total_tokens += estimate_string_tokens(msg.content)
    if current_exec:
        for st in current_exec.steps:
            if st.result:
                total_tokens += estimate_string_tokens(st.result)

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
        logger.warning("Error loading sidecar at %s: %s", sidecar_path, err)
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


def _lookup_context_in_vault(
    vault: dict,
    model_id: str,
) -> Optional[int]:
    """
    Find the max context length for `model_id` inside a models_vault
    payload.

    The vault stores provider entries under lmstudio_vault,
    ollama_vault, cloud_vault, and custom_endpoints. Each provider
    may carry either a flat `savedModels` list or a richer
    `metadata` mapping keyed by model id. Both shapes are checked so
    that models discovered at any point are found.

    Returns the context length in tokens, or None when the model is
    not present or carries no context information.
    """
    if not isinstance(vault, dict):
        return None

    candidates: List[dict] = []
    for key in ("lmstudio_vault", "ollama_vault"):
        entry = vault.get(key)
        if isinstance(entry, dict):
            candidates.append(entry)
    for key in ("cloud_vault", "custom_endpoints"):
        entries = vault.get(key)
        if isinstance(entries, list):
            for e in entries:
                if isinstance(e, dict):
                    candidates.append(e)

    for entry in candidates:
        # Shape A: metadata mapping {model_id: {max_context_length, ...}}
        meta_map = entry.get("metadata") or entry.get("models_metadata")
        if isinstance(meta_map, dict) and model_id in meta_map:
            m = meta_map[model_id]
            if isinstance(m, dict):
                ctx = m.get("max_context_length") or m.get("context_length")
                if isinstance(ctx, (int, float)) and ctx > 0:
                    return int(ctx)

        # Shape B: entry for the model with a direct context field
        if entry.get("model") == model_id:
            ctx = entry.get("max_context_length") or entry.get("context_length")
            if isinstance(ctx, (int, float)) and ctx > 0:
                return int(ctx)

        # Shape C: scanned_models only records ids, no context info.
        # Nothing else to extract here.

    return None
