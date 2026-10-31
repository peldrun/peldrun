# backend/peldrun/llm/vision_worker.py
"""
Isolated vision perception worker for the PELDRUN LLM subsystem.

The worker runs a vision-capable LLM to describe images on behalf of a
text-only reasoning agent. It is the second half of the "Hybrid /
Decoupled Vision Routing" architecture: when the active model is
text-only, the agent loop calls this worker to convert each image into
a textual observation that can be injected into the context.

Design invariants
-----------------
- Never raises on provider failure. Failures are surfaced as a clearly
  marked fallback string so the agent loop continues with a
  degraded-but-usable observation, rather than crashing.

- Accepts three image source shapes transparently:
    * a full ``data:image/...;base64,...`` URI;
    * an ``http(s)://`` URL reachable by the vision provider;
    * a local filesystem path (read and base64-encoded on the fly).

- Delegates HTTP, retry, timeout, and reasoning extraction entirely to
  ``AsyncLLMClient``. This module owns only prompt construction, image
  source normalization, and response extraction.

- Produces structurally identical output regardless of whether the
  caller requested a single image or a combined multi-image
  description: a plain string ready to be injected into a text-only
  prompt.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, List, Optional

from peldrun.llm.client import AsyncLLMClient, LLMResponse
from peldrun.llm.schema import (
    ChatMessage,
    ImageDetail,
    image_data_uri,
    image_url_part,
    messages_to_openai_dicts,
    text_part,
)

logger = logging.getLogger("peldrun.llm.vision_worker")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

VISION_ANALYSIS_SYSTEM_PROMPT = (
    "You are a vision perception module working on behalf of a text-only "
    "reasoning agent. Your job is to describe visual inputs with enough "
    "precision that the reasoning agent can act on them without ever "
    "seeing the image itself."
)

VISION_ANALYSIS_USER_PROMPT = (
    "Analyze the attached image for a text-only AI software assistant. "
    "Produce a compact, structured description covering:\n"
    "1. Overall context (what kind of image: screen capture, UI, document, photo, chart).\n"
    "2. Layout and structure (major regions, panels, headers, grids).\n"
    "3. Visible interactive elements (buttons, inputs, menus, tabs) with their visible labels.\n"
    "4. Exact visible text that is actionable or informative (error messages, URLs, file names, numbers).\n"
    "5. Visual anomalies (overlays, broken layouts, warnings, badges).\n"
    "6. Any state that would affect the next action (focus, selection, disabled controls, loading spinners).\n"
    "Keep the answer under 600 words. Use plain text with short labeled sections. "
    "Never invent details you cannot verify from the pixels."
)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class VisionPerceptionWorker:
    """
    Text-to-vision bridge for the PELDRUN agent loop.

    Typical usage from a text-only agent's context builder::

        worker = VisionPerceptionWorker(vision_client, "qwen2.5-vl-7b")
        observation = await worker.describe_image_for_text_agent(
            "/path/to/screenshot.png",
            user_instruction="What button should I click to log in?",
        )
        # observation is a plain string, safe to inject into the prompt.

    Callers that want to distinguish success from failure can use
    ``try_describe_image`` (returns ``Optional[str]``) or
    ``is_fallback(description)``.
    """

    FALLBACK_PREFIX = "[Vision perception unavailable"

    def __init__(
        self,
        vision_llm_client: AsyncLLMClient,
        vision_model_name: str,
        temperature: float = 0.1,
        max_tokens: int = 800,
        default_detail: ImageDetail = ImageDetail.HIGH,
    ) -> None:
        """
        Args:
            vision_llm_client: An initialized ``AsyncLLMClient`` bound to
                a vision-capable endpoint.
            vision_model_name: The provider-side model identifier (e.g.
                ``"qwen2.5-vl-7b"``, ``"gpt-4o-mini"``).
            temperature: Sampling temperature. Vision tasks benefit from
                very low temperatures to avoid hallucinated UI details.
            max_tokens: Upper bound on the description length.
            default_detail: Default resolution hint for images. ``HIGH``
                is used because screen analysis needs to resolve small
                UI elements reliably; callers that only need a coarse
                summary can override per call.
        """
        self.client = vision_llm_client
        self.model_name = vision_model_name
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.default_detail = default_detail

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        llm_config: Any,
        vision_model_name: str,
        **kwargs: Any,
    ) -> "VisionPerceptionWorker":
        """
        Build a worker around a freshly constructed ``AsyncLLMClient``
        bound to ``llm_config``.

        ``llm_config`` accepts the same shapes as ``AsyncLLMClient``
        itself (``LLMConfig``, a plain dict, or ``None`` for defaults).
        """
        client = AsyncLLMClient(config=llm_config)
        return cls(client, vision_model_name, **kwargs)

    # ------------------------------------------------------------------
    # Public API — single image
    # ------------------------------------------------------------------

    async def describe_image_for_text_agent(
        self,
        image_source: str,
        user_instruction: str = "",
        detail: Optional[ImageDetail] = None,
    ) -> str:
        """
        Describe a single image for a text-only agent.

        Returns a descriptive string on success, or a clearly-marked
        fallback string on failure. This function never raises.
        """
        result = await self._describe_single(
            image_source, user_instruction, detail
        )
        if result is None:
            truncated_source = image_source[:60] if image_source else "<empty>"
            return (
                f"{self.FALLBACK_PREFIX}: single image could not be "
                f"analyzed ({truncated_source!r})]"
            )
        return result

    async def try_describe_image(
        self,
        image_source: str,
        user_instruction: str = "",
        detail: Optional[ImageDetail] = None,
    ) -> Optional[str]:
        """
        Same as ``describe_image_for_text_agent`` but returns ``None``
        on failure, for callers that need to detect the difference
        between a valid description and a fallback.
        """
        return await self._describe_single(
            image_source, user_instruction, detail
        )

    # ------------------------------------------------------------------
    # Public API — combined multi-image
    # ------------------------------------------------------------------

    async def describe_images_combined(
        self,
        image_sources: List[str],
        user_instruction: str = "",
        detail: Optional[ImageDetail] = None,
    ) -> str:
        """
        Describe multiple images in a single vision call, producing one
        combined description.

        Useful when several screenshots belong to the same UI state and
        a single narrative is more useful than per-image summaries.
        """
        if not image_sources:
            return f"{self.FALLBACK_PREFIX}: no images supplied]"

        resolved: List[str] = []
        for src in image_sources:
            url = self._resolve_image_url(src)
            if url is not None:
                resolved.append(url)

        if not resolved:
            return (
                f"{self.FALLBACK_PREFIX}: none of the "
                f"{len(image_sources)} image sources could be resolved]"
            )

        effective_detail = detail if detail is not None else self.default_detail
        result = await self._call_vision_model(
            resolved, user_instruction, effective_detail
        )
        if result is None:
            return (
                f"{self.FALLBACK_PREFIX}: the vision model did not "
                f"return a description]"
            )
        return result

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def is_fallback(self, description: str) -> bool:
        """Return True when ``description`` is a failure fallback."""
        return description.startswith(self.FALLBACK_PREFIX)

    # ------------------------------------------------------------------
    # Internal — single-image helper
    # ------------------------------------------------------------------

    async def _describe_single(
        self,
        image_source: str,
        user_instruction: str,
        detail: Optional[ImageDetail],
    ) -> Optional[str]:
        """Internal single-image flow returning ``None`` on failure."""
        resolved = self._resolve_image_url(image_source)
        if resolved is None:
            return None
        effective_detail = detail if detail is not None else self.default_detail
        return await self._call_vision_model(
            [resolved], user_instruction, effective_detail
        )

    # ------------------------------------------------------------------
    # Internal — source normalization
    # ------------------------------------------------------------------

    def _resolve_image_url(self, source: str) -> Optional[str]:
        """
        Normalize ``source`` into a URL the provider can consume.

        Filesystem paths are read and base64-encoded into a data URI.
        URLs and data URIs pass through unchanged.
        """
        if not source:
            return None
        src = source.strip()
        if not src:
            return None

        # Pass-through for already-resolved sources. ``artifact://``
        # URLs are forwarded unchanged; resolving them against the
        # workspace is the provider adapter's responsibility, not this
        # worker's.
        if src.startswith(("data:", "http://", "https://", "artifact://")):
            return src

        try:
            path = Path(src).expanduser()
            if not path.is_file():
                return None
            raw = path.read_bytes()
            encoded = base64.b64encode(raw).decode("ascii")
            mime = self._guess_mime_type(path.suffix.lower())
            return image_data_uri(encoded, mime_type=mime)
        except Exception as exc:
            logger.debug(
                "Vision worker failed to read image path %r: %s", src, exc
            )
            return None

    @staticmethod
    def _guess_mime_type(suffix: str) -> str:
        """Return a conservative MIME type for the given file extension."""
        return {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(suffix, "image/png")

    # ------------------------------------------------------------------
    # Internal — provider call
    # ------------------------------------------------------------------

    async def _call_vision_model(
        self,
        image_urls: List[str],
        user_instruction: str,
        detail: ImageDetail,
    ) -> Optional[str]:
        """
        Execute one vision completion and extract the description text.

        Returns ``None`` on any failure so callers can produce a uniform
        fallback string without bespoke try/except blocks.
        """
        try:
            parts = []
            intro = VISION_ANALYSIS_USER_PROMPT
            if user_instruction and user_instruction.strip():
                intro = (
                    f"{intro}\n\nContext query: {user_instruction.strip()}"
                )
            parts.append(text_part(intro))
            for url in image_urls:
                parts.append(image_url_part(url, detail=detail))

            messages = [
                ChatMessage(
                    role="system", content=VISION_ANALYSIS_SYSTEM_PROMPT
                ),
                ChatMessage(role="user", content=parts),
            ]

            payload = messages_to_openai_dicts(messages)

            response: LLMResponse = await self.client.chat_completion(
                messages=payload,
                model=self.model_name,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                stream=False,
            )

            content = (response.content or "").strip()
            if not content:
                logger.warning(
                    "Vision model %r returned an empty description.",
                    self.model_name,
                )
                return None
            return content
        except Exception as exc:
            logger.warning(
                "Vision perception call failed for model %r: %s",
                self.model_name,
                exc,
            )
            return None


__all__ = ["VisionPerceptionWorker"]
