# Path: backend/peldrun/context/builder.py

from __future__ import annotations

import base64
import io
import json
import logging
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
    Union,
)

from peldrun.context.budget import TokenBudgetController
from peldrun.context.generative_ui import build_generative_ui_prompt
from peldrun.context.schema import ContextSidecar, CurrentExecution
from peldrun.llm.schema import ChatMessage, ContentPart, image_url_part, text_part

if TYPE_CHECKING:
    from peldrun.llm.vision_worker import VisionPerceptionWorker

logger = logging.getLogger("peldrun.context.builder")


class ContextManagerProtocol(Protocol):
    """Protocol defining the interface for context assembly engines."""

    def build_messages(
        self,
        sidecar: Optional[ContextSidecar] = None,
        current_user_message: str = "",
        system_prompt: Optional[str] = None,
        project_rules: Optional[str] = None,
        include_execution: bool = True,
        generative_ui: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        ...


class ContextManager:
    """
    Central orchestrator for LLM context assembly.
    Extracts sanitized dialogue from Sidecar and ensures older execution telemetry
    is completely isolated from subsequent LLM turns.
    """

    def __init__(self, default_system_prompt: Optional[str] = None):
        self.default_system_prompt = default_system_prompt

    @staticmethod
    def _format_generative_ui_prompt(generative_ui: Optional[Dict[str, Any]]) -> str:
        """
        Delegates Generative UI directive generation to the decoupled generative_ui module.
        Retained as a backward-compatible adapter.
        """
        return build_generative_ui_prompt(generative_ui)

    def _format_execution_telemetry(self, execution: Optional[CurrentExecution]) -> str:
        """
        Format active turn's execution telemetry into a concise system block.
        Only included for the ongoing turn to inform tool status.
        """
        if not execution or not execution.steps:
            return ""

        lines = ["[Current Turn Execution State]"]
        for idx, step in enumerate(execution.steps[-6:], 1):
            step_type = step.type
            name = step.name or "unknown"
            if step_type == "tool_call":
                args_summary = json.dumps(step.args or {}, ensure_ascii=False)
                if len(args_summary) > 160:
                    args_summary = args_summary[:160] + "..."
                lines.append(f"{idx}. Tool Call: {name}({args_summary})")
            elif step_type == "observation":
                res = str(step.result or "").strip()
                if len(res) > 240:
                    res = res[:240] + "... [truncated]"
                lines.append(f"{idx}. Observation: {res}")
            elif step_type == "status":
                lines.append(f"{idx}. Status: {step.result or step.error or 'running'}")

        if execution.artifacts:
            lines.append(f"Active Artifacts: {', '.join(execution.artifacts)}")

        return "\n".join(lines)

    def build_messages(
        self,
        sidecar: Optional[ContextSidecar] = None,
        current_user_message: str = "",
        system_prompt: Optional[str] = None,
        project_rules: Optional[str] = None,
        include_execution: bool = True,
        generative_ui: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """
        Assemble the architectural context tiers in deterministic sequence:
        1. System Directive (Tier 1A)        ┐
        2. Project Rules (Tier 1B)            ├── Merged into ONE system message
        3. Generative UI Instructions (1C)   ┘
        4. Compacted Context Summary (Tier 2)
        5. Sanitized Recent Dialogue (Tier 3)
        6. Ongoing Execution Telemetry (Tier 4)
        7. Current User Input (Tier 5)

        Engineering note on Tier 1 merging:
            Small and medium-sized models (≤ 14B) reliably read only the FIRST
            `system` message in the sequence and treat additional `system` messages
            as ignorable context. Empirically, splitting base directive + OpenUI
            directive into two separate system messages caused the model to fully
            ignore the OpenUI directive when a base Markdown directive was present.

            To guarantee the OpenUI directive is honored, all Tier 1 content is
            concatenated into a SINGLE system message. This preserves the
            behavior of the disabled (Markdown-only) path while making the
            enabled (OpenUI Lang) path functional across model sizes.
        """
        # ------------------------------------------------------------------
        # Tier 1: Unified System Directive (1A Base + 1B Rules + 1C GenUI)
        # ------------------------------------------------------------------
        system_parts: List[str] = []

        # Tier 1A: Base System Directive
        effective_system = (
            system_prompt
            if system_prompt is not None
            else self.default_system_prompt
        )
        if effective_system and effective_system.strip():
            system_parts.append(effective_system.strip())

        # Tier 1B: Explicit Project Rules Block
        if project_rules and project_rules.strip():
            rules_content = project_rules.strip()
            if not rules_content.startswith("[Project Rules]"):
                rules_content = f"[Project Rules]\n{rules_content}"
            system_parts.append(rules_content)

        # Tier 1C: Generative UI Directive (only when enabled and catalog non-empty)
        if generative_ui and generative_ui.get("enabled"):
            ui_directive = self._format_generative_ui_prompt(generative_ui)
            if ui_directive:
                system_parts.append(ui_directive)

        system_msgs: List[Dict[str, Any]] = []
        if system_parts:
            system_msgs.append({
                "role": "system",
                "content": "\n\n".join(system_parts),
            })

        # ------------------------------------------------------------------
        # Tier 2: Compacted Context Summary & Covered Turns Identification
        # ------------------------------------------------------------------
        summary_msgs: List[Dict[str, Any]] = []
        covered_turn_ids: Set[str] = set()

        if sidecar and sidecar.summary and sidecar.summary.content.strip():
            summary_content = (
                f"[Previous Context Summary]\n{sidecar.summary.content.strip()}"
            )
            summary_msgs.append({"role": "system", "content": summary_content})
            if sidecar.summary.covers_turns:
                covered_turn_ids = set(sidecar.summary.covers_turns)

        # ------------------------------------------------------------------
        # Tier 3: Recent Sanitized Conversation
        # ------------------------------------------------------------------
        recent_msgs: List[Dict[str, Any]] = []
        clean_current_input = current_user_message.strip()

        if sidecar and sidecar.conversation:
            for msg in sidecar.conversation:
                if msg.turn_id and msg.turn_id in covered_turn_ids:
                    continue

                if msg.role in ("user", "assistant"):
                    clean_content = msg.content.strip()
                    if msg.role == "user" and clean_current_input and clean_content == clean_current_input:
                        continue
                    recent_msgs.append({"role": msg.role, "content": msg.content})

        # ------------------------------------------------------------------
        # Tier 4: Execution State (only when running / waiting_for_user)
        # ------------------------------------------------------------------
        exec_msgs: List[Dict[str, Any]] = []
        if include_execution and sidecar and sidecar.current_execution:
            exec_status = getattr(sidecar.current_execution, "status", "running")
            if exec_status in ("running", "waiting_for_user"):
                formatted_exec = self._format_execution_telemetry(sidecar.current_execution)
                if formatted_exec:
                    exec_msgs.append({"role": "system", "content": formatted_exec})

        # ------------------------------------------------------------------
        # Tier 5: Current User Message
        # ------------------------------------------------------------------
        current_msgs: List[Dict[str, Any]] = []
        if clean_current_input:
            current_msgs.append({"role": "user", "content": clean_current_input})

        # ------------------------------------------------------------------
        # Apply Token Budget Controller
        # ------------------------------------------------------------------
        budget_spec = sidecar.budget if sidecar else None
        controller = TokenBudgetController(budget_spec)
        fitted_messages, usage = controller.fit_all(
            system_msgs=system_msgs,
            summary_msgs=summary_msgs,
            recent_msgs=recent_msgs,
            exec_msgs=exec_msgs,
            current_msgs=current_msgs,
        )

        return fitted_messages, usage


# ---------------------------------------------------------------------------
# Vision context optimization
# ---------------------------------------------------------------------------


class VisionContextOptimizer:
    """
    Encodes, downsamples, and evicts images so the active context window
    is never blown by accumulated screenshots.

    Two responsibilities are kept deliberately separate:

    * ``process_and_encode_image`` — deterministic file-to-data-URI
      encoding with mandatory downsampling. Used when a local image
      must be embedded inline for a vision-capable provider.

    * ``apply_sliding_vision_window`` — post-assembly trimming step
      that keeps only the newest N images and replaces every older
      image with a text part. Used after ``ContextManager.build_messages``
      on the final message list, before the provider call.

    The class accepts both ``Dict`` messages (the shape produced by
    ``ContextManager.build_messages``) and typed ``ChatMessage``
    instances. The output preserves the input's per-message type so
    downstream code never has to re-coerce.
    """

    # Providers downscale hard once an image exceeds ~1600px on the long
    # edge; keeping the client-side cap at 1568 avoids uploading pixels
    # the provider will just discard.
    MAX_DIMENSION: int = 1568
    JPEG_QUALITY: int = 85

    # ------------------------------------------------------------------
    # File -> data URI encoding
    # ------------------------------------------------------------------

    @classmethod
    def process_and_encode_image(cls, file_path: Path) -> Optional[str]:
        """
        Load a local image, downsample if oversized, recompress as JPEG,
        and return a ``data:image/jpeg;base64,...`` URI.

        Returns ``None`` when Pillow is unavailable, the file cannot be
        read, or the image is in an unsupported format. Callers must
        treat ``None`` as "skip this image", never as an error to
        propagate.
        """
        try:
            from PIL import Image  # noqa: PLC0415 — optional dependency
        except ImportError:
            logger.warning(
                "Pillow is not installed; image encoding skipped. "
                "Install with: pip install Pillow"
            )
            return None

        try:
            # Pillow 9.0+ moved LANCZOS into Image.Resampling.
            try:
                resample = Image.Resampling.LANCZOS
            except AttributeError:
                resample = Image.LANCZOS  # type: ignore[attr-defined]

            with Image.open(file_path) as img:
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")

                width, height = img.size
                if max(width, height) > cls.MAX_DIMENSION:
                    scale = cls.MAX_DIMENSION / float(max(width, height))
                    new_size = (
                        max(1, int(width * scale)),
                        max(1, int(height * scale)),
                    )
                    img = img.resize(new_size, resample)

                buffer = io.BytesIO()
                img.save(
                    buffer,
                    format="JPEG",
                    quality=cls.JPEG_QUALITY,
                    optimize=True,
                )
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                return f"data:image/jpeg;base64,{encoded}"
        except Exception as exc:
            logger.warning(
                "VisionContextOptimizer failed to encode %s: %s", file_path, exc
            )
            return None

    # ------------------------------------------------------------------
    # Sliding window eviction (sync)
    # ------------------------------------------------------------------

    @classmethod
    def apply_sliding_vision_window(
        cls,
        messages: List[Union[Dict[str, Any], ChatMessage]],
        max_active_images: int = 2,
        summary_fn: Optional[Callable[[str], str]] = None,
    ) -> List[Union[Dict[str, Any], ChatMessage]]:
        """
        Keep only the newest ``max_active_images`` images across the
        entire conversation; older images are replaced with a text part.

        "Newest" means closer to the end of the ``messages`` list.
        Within a single message with multiple images, the image closer
        to the end of the parts list is considered newer.

        ``summary_fn`` is an optional synchronous callable invoked with
        each evicted image's URL. Its return value becomes the text
        that replaces the image. When omitted, a stable placeholder is
        used so the model knows an image existed but was elided.

        Returns a new list; the input is never mutated.
        """
        image_positions = cls._collect_image_positions(messages)
        total = len(image_positions)
        if total <= max_active_images:
            return list(messages)

        evict_count = total - max_active_images
        evict_entries = image_positions[:evict_count]

        replacements: Dict[Tuple[int, int], str] = {}
        for msg_idx, part_idx, url in evict_entries:
            if summary_fn is not None:
                try:
                    replacement = summary_fn(url) or cls._default_eviction_text()
                except Exception:
                    replacement = cls._default_eviction_text()
            else:
                replacement = cls._default_eviction_text()
            replacements[(msg_idx, part_idx)] = replacement

        return cls._rebuild_with_replacements(messages, replacements)

    # ------------------------------------------------------------------
    # Sliding window eviction (async, vision-worker aware)
    # ------------------------------------------------------------------

    @classmethod
    async def apply_sliding_vision_window_async(
        cls,
        messages: List[Union[Dict[str, Any], ChatMessage]],
        max_active_images: int = 2,
        vision_worker: Optional["VisionPerceptionWorker"] = None,
        user_instruction: str = "",
    ) -> List[Union[Dict[str, Any], ChatMessage]]:
        """
        Async variant that produces real textual summaries for evicted
        images via the ``VisionPerceptionWorker``.

        When ``vision_worker`` is ``None`` or fails, falls back to the
        same placeholder used by the sync variant. Never raises.
        """
        image_positions = cls._collect_image_positions(messages)
        total = len(image_positions)
        if total <= max_active_images:
            return list(messages)

        evict_count = total - max_active_images
        evict_entries = image_positions[:evict_count]

        replacements: Dict[Tuple[int, int], str] = {}
        for msg_idx, part_idx, url in evict_entries:
            summary_text: Optional[str] = None
            if vision_worker is not None:
                try:
                    summary_text = await vision_worker.try_describe_image(
                        url, user_instruction=user_instruction
                    )
                except Exception as exc:
                    logger.debug(
                        "Vision worker failed during eviction summary: %s", exc
                    )
                    summary_text = None

            replacements[(msg_idx, part_idx)] = (
                summary_text or cls._default_eviction_text()
            )

        return cls._rebuild_with_replacements(messages, replacements)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def count_active_images(
        messages: List[Union[Dict[str, Any], ChatMessage]],
    ) -> int:
        """Return the total number of ``image_url`` parts in the list."""
        return len(VisionContextOptimizer._collect_image_positions(messages))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _default_eviction_text() -> str:
        return (
            "[Historical image evicted from visual context to preserve "
            "token budget]"
        )

    @staticmethod
    def _collect_image_positions(
        messages: List[Union[Dict[str, Any], ChatMessage]],
    ) -> List[Tuple[int, int, str]]:
        """
        Return ``(msg_idx, part_idx, url)`` for every image part, in
        **newest-first** order. This is the order the eviction logic
        depends on: the first entries are the ones to evict.
        """
        collected: List[Tuple[int, int, str]] = []
        for msg_idx in range(len(messages) - 1, -1, -1):
            msg = messages[msg_idx]
            content = (
                msg.get("content")
                if isinstance(msg, dict)
                else getattr(msg, "content", None)
            )
            if not isinstance(content, list):
                continue
            # Iterate parts newest-first within the message too.
            for part_idx in range(len(content) - 1, -1, -1):
                part = content[part_idx]
                if isinstance(part, dict):
                    if part.get("type") != "image_url":
                        continue
                    image_payload = part.get("image_url")
                    url = (
                        image_payload.get("url", "")
                        if isinstance(image_payload, dict)
                        else ""
                    )
                    collected.append((msg_idx, part_idx, str(url)))
                else:
                    if getattr(part, "type", None) != "image_url":
                        continue
                    image_payload = getattr(part, "image_url", None)
                    url = getattr(image_payload, "url", "") if image_payload else ""
                    collected.append((msg_idx, part_idx, str(url)))
        return collected

    @classmethod
    def _rebuild_with_replacements(
        cls,
        messages: List[Union[Dict[str, Any], ChatMessage]],
        replacements: Dict[Tuple[int, int], str],
    ) -> List[Union[Dict[str, Any], ChatMessage]]:
        """
        Produce a new message list where every ``(msg_idx, part_idx)`` in
        ``replacements`` has its image part swapped for a text part.

        Preserves the per-message type: a ``ChatMessage`` input produces
        ``ChatMessage`` output with typed ``ContentPart`` instances; a
        ``Dict`` input produces ``Dict`` output with ``Dict`` parts.
        """
        rebuilt: List[Union[Dict[str, Any], ChatMessage]] = []
        for msg_idx, msg in enumerate(messages):
            content = (
                msg.get("content")
                if isinstance(msg, dict)
                else getattr(msg, "content", None)
            )
            if not isinstance(content, list):
                rebuilt.append(msg)
                continue

            new_parts_dict: List[Dict[str, Any]] = []
            for part_idx, part in enumerate(content):
                replacement_text = replacements.get((msg_idx, part_idx))
                if replacement_text is not None:
                    new_parts_dict.append(
                        {"type": "text", "text": replacement_text}
                    )
                elif isinstance(part, dict):
                    new_parts_dict.append(part)
                else:
                    new_parts_dict.append(
                        part.to_openai_dict()
                        if hasattr(part, "to_openai_dict")
                        else {"type": getattr(part, "type", "text")}
                    )

            if isinstance(msg, dict):
                rebuilt.append({**msg, "content": new_parts_dict})
            else:
                typed_parts: List[ContentPart] = []
                for p in new_parts_dict:
                    try:
                        typed_parts.append(ContentPart(**p))
                    except Exception:
                        typed_parts.append(
                            ContentPart(type="text", text=str(p))
                        )
                rebuilt.append(
                    msg.model_copy(update={"content": typed_parts})
                )
        return rebuilt


# ---------------------------------------------------------------------------
# High-level integration helper
# ---------------------------------------------------------------------------


async def enrich_user_message_with_vision(
    user_text: str,
    image_sources: List[str],
    vision_worker: Optional["VisionPerceptionWorker"],
    active_model_supports_vision: bool,
    user_instruction: str = "",
    detail_hint: str = "high",
) -> Union[str, List[Dict[str, Any]]]:
    """
    Build the ``content`` field for the current user message, embedding
    images either directly (vision-capable active model) or as textual
    observations (text-only active model routed through the vision
    worker). This is the single integration point that implements the
    "Hybrid / Decoupled Vision Routing" decision described in the
    multimodal architecture plan.

    Returns
    -------
    str
        When there are no images, or when the vision worker failed for
        every image. In both cases the caller receives plain text that
        can be set as the message ``content`` directly.
    List[Dict[str, Any]]
        When at least one image was successfully resolved. Each dict is
        a valid OpenAI multimodal content part.

    Behavior by case
    ----------------
    1. No images → returns ``user_text`` unchanged.
    2. Active model supports vision → returns
       ``[text_part, image_url_part, ...]`` with every image embedded.
    3. Text-only active model + vision worker → returns enriched plain
       text that includes the worker's description for each image.
    4. Text-only active model + no worker → returns the original text
       with a small ``[N image(s) attached — no vision model
       configured]`` marker appended so the agent is aware.
    """
    if not image_sources:
        return user_text

    # Case 2 — direct embedding for vision-capable active model.
    if active_model_supports_vision:
        detail_enum_value = detail_hint or "high"
        parts: List[Dict[str, Any]] = [{"type": "text", "text": user_text}]
        for src in image_sources:
            try:
                # Local file path → downsample + encode.
                path = Path(src).expanduser()
                if path.is_file():
                    data_uri = VisionContextOptimizer.process_and_encode_image(
                        path
                    )
                    if data_uri is None:
                        continue
                    parts.append(
                        image_url_part(data_uri).to_openai_dict()
                    )
                else:
                    # URL or data URI → pass through.
                    parts.append(
                        image_url_part(src).to_openai_dict()
                    )
            except Exception as exc:
                logger.debug(
                    "Skipping image %r in enrichment: %s", src, exc
                )
        if len(parts) == 1:
            return user_text
        return parts

    # Case 3 — text-only model, worker available.
    if vision_worker is not None:
        observations: List[str] = []
        for idx, src in enumerate(image_sources, start=1):
            description = await vision_worker.try_describe_image(
                src, user_instruction=user_instruction
            )
            if description is None:
                observations.append(
                    f"[Image {idx}: vision description unavailable]"
                )
            else:
                observations.append(f"[Image {idx} description]: {description}")

        if not observations:
            return user_text
        return (
            f"{user_text}\n\n"
            f"--- Attached visual context ---\n"
            + "\n".join(observations)
        )

    # Case 4 — text-only model, no worker configured.
    return (
        f"{user_text}\n\n"
        f"[{len(image_sources)} image(s) attached — "
        f"no vision model configured to interpret them]"
    )
