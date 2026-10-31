# backend/peldrun/llm/schema.py
"""
Typed multimodal message schemas for the PELDRUN LLM subsystem.

This module defines the canonical Pydantic models used to represent
multimodal chat content (text + images) that flow between the context
builder, the vision router, and the provider payload builders.

Design notes
------------
- The runtime continues to accept ``List[Dict[str, Any]]`` at the client
  boundary for backwards compatibility with existing provider code and
  cache layers. The typed models below provide a stable contract for
  NEW code (context optimization, vision routing, multimodal payload
  assembly) that needs structural guarantees.

- ``ChatMessage`` and ``ContentPart`` mirror the OpenAI multimodal
  message specification, so a typed instance can be lowered to a
  provider payload via ``to_openai_dict()`` without lossy
  transformation.

- The ``coerce_*`` helpers accept either a plain dict or a typed
  instance, letting callers migrate incrementally without breaking
  existing call sites.

- No function in this module raises on malformed input. Schema drift at
  the boundary must never crash the agent loop; the coercion helpers
  degrade unknown shapes to a text-only user message instead.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Discriminators and payload primitives
# ---------------------------------------------------------------------------


class ContentType(str, Enum):
    """Discriminator for multimodal content parts."""

    TEXT = "text"
    IMAGE_URL = "image_url"


class ImageDetail(str, Enum):
    """OpenAI-compatible vision resolution hint."""

    AUTO = "auto"
    LOW = "low"
    HIGH = "high"


class ImageUrlPayload(BaseModel):
    """
    Payload carried by an ``image_url`` content part.

    ``url`` may be:
      * an ``http(s)://`` URL reachable by the provider;
      * a ``data:image/...;base64,...`` URI (inline transport);
      * an internal ``artifact://<file_id>`` scheme that the provider
        adapter resolves against the workspace before dispatch.
    """

    model_config = ConfigDict(extra="ignore")

    url: str = Field(
        ...,
        description="Image URL, data URI, or artifact:// reference.",
    )
    detail: ImageDetail = Field(
        default=ImageDetail.AUTO,
        description="Resolution hint for the vision encoder.",
    )


class ContentPart(BaseModel):
    """A single typed part inside a multimodal message."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["text", "image_url"] = Field(
        ..., description="Discriminator for the part kind."
    )
    text: Optional[str] = Field(
        default=None,
        description="Populated only when ``type == 'text'``.",
    )
    image_url: Optional[ImageUrlPayload] = Field(
        default=None,
        description="Populated only when ``type == 'image_url'``.",
    )

    def to_openai_dict(self) -> Dict[str, Any]:
        """Lower this part to an OpenAI-compatible dict."""
        if self.type == "text":
            return {"type": "text", "text": self.text or ""}
        if self.type == "image_url":
            payload = self.image_url or ImageUrlPayload(url="")
            return {
                "type": "image_url",
                "image_url": {
                    "url": payload.url,
                    "detail": payload.detail.value,
                },
            }
        # Defensive fallback for future part kinds not yet modelled.
        return {"type": self.type}


# ---------------------------------------------------------------------------
# Chat message model
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    """
    A single message in a chat conversation, possibly multimodal.

    ``content`` is a union because most messages remain plain text.
    When ``content`` is a list, it is interpreted as ordered
    ``ContentPart`` instances (text and image intermixed).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    role: Literal["system", "user", "assistant", "tool"] = Field(
        ..., description="Role of the message author."
    )
    content: Union[str, List[ContentPart]] = Field(
        ...,
        description=(
            "Either a plain text string (legacy) or a list of typed "
            "content parts (multimodal)."
        ),
    )
    name: Optional[str] = Field(
        default=None,
        description="Optional author name (for tool calls).",
    )
    tool_call_id: Optional[str] = Field(
        default=None,
        description="Tool call correlation id (for role == 'tool').",
    )

    def is_multimodal(self) -> bool:
        """Return True when content is a list of content parts."""
        return isinstance(self.content, list)

    def has_images(self) -> bool:
        """Return True when at least one content part is an image."""
        if not isinstance(self.content, list):
            return False
        return any(p.type == "image_url" for p in self.content)

    def to_openai_dict(self) -> Dict[str, Any]:
        """Lower this message to an OpenAI-compatible dict."""
        out: Dict[str, Any] = {"role": self.role}
        if isinstance(self.content, list):
            out["content"] = [p.to_openai_dict() for p in self.content]
        else:
            out["content"] = self.content
        if self.name:
            out["name"] = self.name
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        return out


# ---------------------------------------------------------------------------
# Coercion helpers (Dict <-> Typed)
# ---------------------------------------------------------------------------


def coerce_message(raw: Union[ChatMessage, Dict[str, Any], Any]) -> ChatMessage:
    """
    Coerce a raw message (dict or typed) into a ``ChatMessage``.

    This function never raises. Unknown or malformed shapes degrade to
    a text-only ``user`` message so that a schema mismatch in one turn
    never takes down the whole agent loop.
    """
    if isinstance(raw, ChatMessage):
        return raw

    if not isinstance(raw, dict):
        return ChatMessage(role="user", content=str(raw))

    raw_role = str(raw.get("role", "user")).lower()
    role: Literal["system", "user", "assistant", "tool"]
    if raw_role == "system":
        role = "system"
    elif raw_role == "assistant":
        role = "assistant"
    elif raw_role == "tool":
        role = "tool"
    else:
        role = "user"

    raw_content = raw.get("content", "")
    content: Union[str, List[ContentPart]]

    if isinstance(raw_content, str):
        content = raw_content
    elif isinstance(raw_content, list):
        typed_parts: List[ContentPart] = []
        for item in raw_content:
            if isinstance(item, ContentPart):
                typed_parts.append(item)
                continue
            if isinstance(item, dict):
                try:
                    typed_parts.append(ContentPart(**item))
                    continue
                except Exception:
                    pass
            # Unknown item shape: preserve it as text so no data is lost.
            typed_parts.append(ContentPart(type="text", text=str(item)))
        content = typed_parts
    else:
        content = str(raw_content)

    return ChatMessage(
        role=role,
        content=content,
        name=raw.get("name"),
        tool_call_id=raw.get("tool_call_id"),
    )


def coerce_messages(
    messages: List[Union[ChatMessage, Dict[str, Any]]],
) -> List[ChatMessage]:
    """Coerce a list of messages into typed ``ChatMessage`` instances."""
    return [coerce_message(m) for m in messages]


def messages_to_openai_dicts(
    messages: List[Union[ChatMessage, Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    Lower a mixed list of messages into OpenAI-compatible dicts.

    This is the boundary helper used by providers before delegating to
    ``AsyncLLMClient.chat_completion``, which expects plain dicts.
    """
    return [coerce_message(m).to_openai_dict() for m in messages]


# ---------------------------------------------------------------------------
# Convenience factories
# ---------------------------------------------------------------------------


def text_part(text: str) -> ContentPart:
    """Build a text content part."""
    return ContentPart(type="text", text=text)


def image_url_part(
    url: str, detail: ImageDetail = ImageDetail.AUTO
) -> ContentPart:
    """Build an image_url content part."""
    return ContentPart(
        type="image_url",
        image_url=ImageUrlPayload(url=url, detail=detail),
    )


def image_data_uri(encoded_base64: str, mime_type: str = "image/jpeg") -> str:
    """
    Compose a ``data:`` URI from a base64 payload and a MIME type.

    Callers are responsible for producing the base64 string (typically
    from ``base64.b64encode(bytes).decode('ascii')``). This helper only
    performs the URI assembly so that MIME prefixing stays consistent.
    """
    return f"data:{mime_type};base64,{encoded_base64}"


# ---------------------------------------------------------------------------
# Extractors (used by vision routing and context optimization)
# ---------------------------------------------------------------------------


def extract_text(message: Union[ChatMessage, Dict[str, Any]]) -> str:
    """Return the concatenated text of a message (empty if image-only)."""
    msg = coerce_message(message)
    if isinstance(msg.content, str):
        return msg.content
    return "\n".join(
        p.text for p in msg.content if p.type == "text" and p.text
    )


def extract_image_urls(
    message: Union[ChatMessage, Dict[str, Any]],
) -> List[str]:
    """Return the URLs of all image_url parts in the message."""
    msg = coerce_message(message)
    if isinstance(msg.content, str):
        return []
    return [
        p.image_url.url
        for p in msg.content
        if p.type == "image_url" and p.image_url is not None
    ]


__all__ = [
    "ChatMessage",
    "ContentPart",
    "ContentType",
    "ImageDetail",
    "ImageUrlPayload",
    "coerce_message",
    "coerce_messages",
    "extract_image_urls",
    "extract_text",
    "image_data_uri",
    "image_url_part",
    "messages_to_openai_dicts",
    "text_part",
]
