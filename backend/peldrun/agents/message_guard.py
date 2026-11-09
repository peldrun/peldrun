# Path: backend/peldrun/agents/message_guard.py
"""
PELDRUN Core Message Guard.

Single source of truth for the "system prompt must be present and
must be the first message" invariant that every LLM call relies on.

This module exists to fix B1 in the PELDRUN execution repair plan:
``ToolCallAgent._sync_messages_from_state`` set
``self.messages = [msg.to_llm_dict() for msg in state.messages]``
whenever ``state.messages`` was non-empty, silently DROPPING the
system prompt. The model then ran without any operational
instructions — it had no tool contract, no workspace rules, and no
idea it was supposed to call ``terminate`` when done.

Design invariants
-----------------

1. **One function, one job.**
   ``ensure_system_prompt(messages, system_prompt)`` returns a NEW
   list where the system prompt is guaranteed to be the first
   message. It does not call the LLM, does not touch ExecutionState,
   and does not know what a "tool" is. Every layer that assembles
   messages for an LLM call (ToolCallAgent, ContextProjection,
   executor, chat dispatcher) routes through this function.

2. **Idempotent and non-destructive.**
   Calling twice with the same prompt produces byte-identical
   results. The function never mutates the input list or its
   contained message dicts — it returns a shallow-copied list,
   replacing only the system message when needed. Callers who forget
   to assign the return value (``ensure_system_prompt(...)`` without
   ``messages =``) will see the original messages unchanged, which
   is the SAFE failure mode: it does not silently corrupt anything.

3. **``None`` / empty prompt = no-op.**
   When ``system_prompt`` is ``None``, empty, or whitespace-only,
   the function returns the input list unchanged (shallow copy).
   This lets callers pass the agent's system prompt
   unconditionally, even for agents that intentionally have none
   (e.g. pure-completion tasks), without a redundant guard.

4. **Existing system message: content updated, metadata preserved.**
   When the first message is already a system message with a
   DIFFERENT content, its content is replaced but any additional
   fields (``name``, ``metadata``, future additions) are preserved.
   This prevents a plugin that labels its system message (e.g.
   ``name: "peldrun_directive_v3"``) from losing that label on
   every call.

5. **Displaced system messages: consolidated, not duplicated.**
   If the input contains a system message that is NOT the first
   message (a buggy caller upstream, a corrupted checkpoint, or a
   mid-conversation prompt injection), all system messages are
   removed and the canonical one is prepended. The resulting
   sequence has exactly ONE system message, at index 0. This
   matches the OpenAI contract (multiple system messages confuse
   most providers) and the PELDRUN operational contract
   ([OPERATIONAL DIRECTIVE] must be read before any other
   instructions).

6. **Multimodal safety.**
   ``messages`` may contain user messages whose ``content`` is a
   list of parts (``{"type": "text"}`` / ``{"type": "image_url"}``).
   The function never touches non-system messages, so multimodal
   content passes through untouched. A ``system`` message's
   ``content`` is coerced to plain text (multimodal system
   messages are not a supported concept — the OpenAI SDK rejects
   them).

7. **No external imports.**
   This module depends only on the Python standard library. It can
   be imported by anything — including unit tests that do not load
   the LLM stack.

See also
--------

    * ``backend/peldrun/agents/tool_call_agent.py``
        Phase B will change ``_sync_messages_from_state`` to call
        ``ensure_system_prompt`` instead of the inline branch that
        caused B1.

    * ``backend/peldrun/context/projection.py`` (Phase A, later)
        Phase D builds on this: ``projection.build()`` will apply
        ``ensure_system_prompt`` as its final step so that even
        compacted context keeps the system prompt at index 0.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("peldrun.agents.message_guard")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYSTEM_ROLE: str = "system"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_text(content: Any) -> str:
    """
    Flatten a message ``content`` field into a plain string.

    Handles three shapes:

      * ``str``          — returned as-is.
      * ``None``         — returned as ``""``.
      * ``list`` of parts — concatenates the ``text`` field of every
        ``{"type": "text"}`` part; ignores image parts (they cannot
        be represented in a system message). Parts that are plain
        strings are included as-is.
      * anything else   — coerced with ``str()``.

    This helper is defensive: a caller that accidentally passes a
    multimodal system message gets a reasonable string, not a crash.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        fragments: List[str] = []
        for part in content:
            if isinstance(part, str):
                fragments.append(part)
                continue
            if isinstance(part, dict):
                if part.get("type") == "text":
                    text_val = part.get("text", "")
                    if isinstance(text_val, str) and text_val:
                        fragments.append(text_val)
        return "\n".join(fragments)
    return str(content)


def _get_role(msg: Any) -> Optional[str]:
    """
    Return the lowercased ``role`` of a message dict, or ``None``.

    Only ``dict`` inputs are supported by the public API; this helper
    is defensively typed to accept anything that could reasonably
    reach it (including ``ChatMessage`` instances that a caller
    forgot to convert), so that a wrong-type caller produces a
    coherent error rather than an ``AttributeError`` buried in the
    middle of message assembly.
    """
    if isinstance(msg, dict):
        role = msg.get("role")
    elif hasattr(msg, "role"):
        role = getattr(msg, "role")
        # ``ExecutionStatus`` / ``MessageRole`` are ``str`` subclasses,
        # but some callers pass bare enums. Normalise uniformly.
        if hasattr(role, "value"):
            role = role.value
    else:
        return None
    if role is None:
        return None
    return str(role).strip().lower()


def _coerce_to_dict(msg: Any) -> Dict[str, Any]:
    """
    Coerce a message item to a plain ``dict``.

    Primary use case is a ``dict`` (the contract of this module). But
    ``ChatMessage`` instances from ``peldrun.engine.state`` have a
    ``to_llm_dict()`` that produces exactly the shape we need, and
    refusing them would force every caller to duplicate that
    conversion. We accept and coerce — one line — so the module
    remains a drop-in helper everywhere.
    """
    if isinstance(msg, dict):
        return msg
    if hasattr(msg, "to_llm_dict") and callable(msg.to_llm_dict):
        return msg.to_llm_dict()
    # Defensive fallback — an unrecognised shape is almost certainly a
    # caller bug, but we surface it as a text-user message rather than
    # crashing the whole LLM call.
    logger.warning(
        "message_guard: unexpected message type %s, coercing to text",
        type(msg).__name__,
    )
    return {"role": "user", "content": str(msg)}


# ---------------------------------------------------------------------------
# Predicates and extractors
# ---------------------------------------------------------------------------


def is_system_message(msg: Any) -> bool:
    """Return True if ``msg`` is a message with role ``"system"``."""
    return _get_role(msg) == SYSTEM_ROLE


def has_system_prompt(messages: List[Any]) -> bool:
    """
    Return True if ``messages`` contains AT LEAST ONE system message.

    Note: this checks presence, not position. The "system is at index
    0" invariant is enforced by ``ensure_system_prompt``, not by this
    predicate — a caller that only needs to know "was any
    instruction given?" should use this predicate; a caller that
    needs the strong invariant should use the enforcement function.
    """
    if not messages:
        return False
    return any(is_system_message(m) for m in messages)


def extract_system_prompt(messages: List[Any]) -> Optional[str]:
    """
    Return the concatenated text of all system messages in
    ``messages``, or ``None`` when there are none.

    Concatenation (with blank-line separators) is used rather than
    "return the first" because some upstream callers legitimately
    split the system directive into multiple messages (base
    directive + project rules + GenUI directive). Returning the
    combined text is the most useful thing a diagnostic / debugging
    caller can see.

    This is a READ-ONLY helper. It never enforces position, never
    removes duplicates, and never mutates the input.
    """
    if not messages:
        return None
    fragments: List[str] = []
    for msg in messages:
        if not is_system_message(msg):
            continue
        content = msg.get("content") if isinstance(msg, dict) else None
        text = _safe_text(content)
        if text:
            fragments.append(text)
    if not fragments:
        return None
    return "\n\n".join(fragments)


def strip_system_prompt(messages: List[Any]) -> List[Dict[str, Any]]:
    """
    Return a new list with every system message removed.

    Useful for callers that want to REBUILD the system directive
    from scratch (e.g. to inject a fully re-composed directive
    without preserving stale fragments from an old prompt version).
    The returned messages retain their original order; only the
    system-role entries are dropped.

    The input list and its contents are NOT mutated.
    """
    if not messages:
        return []
    return [
        _coerce_to_dict(m)
        for m in messages
        if not is_system_message(m)
    ]


# ---------------------------------------------------------------------------
# Enforcement: ensure_system_prompt (the main event)
# ---------------------------------------------------------------------------


def ensure_system_prompt(
    messages: List[Any],
    system_prompt: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Return a NEW list where ``system_prompt`` is guaranteed to be the
    first message (role ``"system"``).

    Behavioral contract (in evaluation order):

    ┌─────────────────────────────────────────────────────────────┐
    │ Condition               │ Behavior                           │
    ├─────────────────────────┼────────────────────────────────────┤
    │ system_prompt is        │ Return messages unchanged (shallow │
    │ None / "" / whitespace  │ copy). No system message injected. │
    ├─────────────────────────┼────────────────────────────────────┤
    │ messages is empty       │ Return [{"role": "system",         │
    │                         │          "content": system_prompt}]│
    ├─────────────────────────┼────────────────────────────────────┤
    │ messages[0] is system   │ If content MATCHES → shallow copy  │
    │ AND content matches     │   (no work).                       │
    │                         │ If content DIFFERS → new list with │
    │                         │   messages[0] rebuilt: content is  │
    │                         │   system_prompt; all other fields  │
    │                         │   (name, etc.) preserved.          │
    ├─────────────────────────┼────────────────────────────────────┤
    │ messages[0] is system   │ Shallow copy with messages[0]'s    │
    │ AND content is a LIST   │   content coerced to plain text    │
    │ (multimodal, buggy)     │   first, then the "DIFFERS" case.  │
    ├─────────────────────────┼────────────────────────────────────┤
    │ system message exists   │ Remove ALL system messages, then   │
    │ but NOT at index 0      │   prepend one canonical system     │
    │                         │   message with system_prompt.      │
    ├─────────────────────────┼────────────────────────────────────┤
    │ no system message at    │ Prepend canonical system message.  │
    │ any position            │                                    │
    └─────────────────────────┴────────────────────────────────────┘

    The output list always contains exactly ONE system message (unless
    ``system_prompt`` is empty, in which case it contains NONE —
    matching the input). Non-system messages retain their original
    order, IDs, tool-call payloads, and multimodal content verbatim.

    Parameters
    ----------
    messages
        A list of message dicts (or ``ChatMessage`` instances — they
        are coerced via ``to_llm_dict()``). Not mutated.
    system_prompt
        The authoritative system directive. When non-empty, it
        OVERWRITES any existing system message. When empty, the
        input is returned unchanged — the caller is signalling
        "this agent has no system prompt".

    Returns
    -------
    List[Dict[str, Any]]
        A fresh list. The dicts it contains are either the original
        dicts (shared, unmodified) or a new dict for the system
        message when its content was replaced. Callers may freely
        append to the returned list; mutating the shared dicts is
        forbidden by convention (same as the rest of the runtime).

    Examples
    --------
    >>> ensure_system_prompt([], "You are helpful.")
    [{'role': 'system', 'content': 'You are helpful.'}]

    >>> ensure_system_prompt([{"role": "user", "content": "hi"}], "SYS")
    [{'role': 'system', 'content': 'SYS'},
     {'role': 'user', 'content': 'hi'}]

    >>> ensure_system_prompt(
    ...     [{"role": "system", "content": "OLD"},
    ...      {"role": "user", "content": "hi"}],
    ...     "NEW",
    ... )
    [{'role': 'system', 'content': 'NEW'},
     {'role': 'user', 'content': 'hi'}]

    >>> ensure_system_prompt(
    ...     [{"role": "user", "content": "hi"},
    ...      {"role": "system", "content": "MID"}],
    ...     "SYS",
    ... )
    [{'role': 'system', 'content': 'SYS'},
     {'role': 'user', 'content': 'hi'}]

    >>> ensure_system_prompt([{"role": "user", "content": "hi"}], None)
    [{'role': 'user', 'content': 'hi'}]
    """
    # 1. No system prompt → return a shallow copy of the input list.
    #
    # We still return a NEW list (not the input) so the caller can
    # mutate the result without surprising side effects on the
    # original. This matches the "always return a fresh container"
    # contract that every other helper in this module follows.
    if system_prompt is None or not str(system_prompt).strip():
        return [_coerce_to_dict(m) for m in (messages or [])]

    canonical_text = str(system_prompt).strip()
    canonical_msg: Dict[str, Any] = {
        "role": SYSTEM_ROLE,
        "content": canonical_text,
    }

    # 2. Empty input → return just the canonical system message.
    if not messages:
        return [canonical_msg]

    coerced = [_coerce_to_dict(m) for m in messages]
    first = coerced[0]

    # 3. First message is already a system message.
    if _get_role(first) == SYSTEM_ROLE:
        existing_text = _safe_text(first.get("content"))
        if existing_text.strip() == canonical_text:
            # Idempotent: nothing to change. Return a shallow copy so
            # callers can mutate the list without affecting the input.
            return list(coerced)

        # Content differs → rebuild the first message preserving
        # every field EXCEPT ``content``. This keeps ``name`` and any
        # future OpenAI-compatible extensions intact.
        rebuilt_first: Dict[str, Any] = dict(first)
        rebuilt_first["content"] = canonical_text
        return [rebuilt_first, *coerced[1:]]

    # 4. No system message at index 0. Are there system messages
    #    ANYWHERE else? (Buggy caller, mid-conversation injection,
    #    corrupted checkpoint, ...)
    non_system = [
        m for m in coerced if _get_role(m) != SYSTEM_ROLE
    ]
    if len(non_system) != len(coerced):
        # One or more system messages exist but are not at index 0.
        # Consolidate: drop them all, prepend the canonical one. This
        # is the deterministic fix for the "system prompt is
        # authoritative, exactly one, at index 0" invariant.
        logger.debug(
            "message_guard: displaced %d system message(s) to the "
            "front of the message list",
            len(coerced) - len(non_system),
        )
        return [canonical_msg, *non_system]

    # 5. No system message anywhere → simple prepend.
    return [canonical_msg, *coerced]


# ---------------------------------------------------------------------------
# Convenience wrapper for the specific B1 use case
# ---------------------------------------------------------------------------


def rebuild_for_llm(
    messages: List[Any],
    system_prompt: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Convenience wrapper used by ``ToolCallAgent._sync_messages_from_state``.

    Semantically identical to ``ensure_system_prompt``. It exists as a
    named entry point so the B1 fix (Phase B) reads like a deliberate
    call rather than a generic guard:

        self.messages = message_guard.rebuild_for_llm(
            [msg.to_llm_dict() for msg in state.messages],
            self.system_prompt,
        )

    Keeping the name distinct makes the fix easy to find during code
    review and lets future tooling (static analysis, diagnostic
    tools) identify the exact call site that enforces B1, even if
    additional message-preprocessing steps are added to it later.
    """
    return ensure_system_prompt(messages, system_prompt)


__all__ = [
    "SYSTEM_ROLE",
    "ensure_system_prompt",
    "extract_system_prompt",
    "has_system_prompt",
    "is_system_message",
    "rebuild_for_llm",
    "strip_system_prompt",
]
