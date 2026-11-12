# backend/tests/memory/test_builder_memory_integration.py
"""
P1.5 integration tests for memory injection in ``ContextManager.build_messages``.

Verifies the end-to-end effect of the ``memory_context`` parameter
added under P1.5:

  * backward compatibility when ``memory_context`` is omitted or empty,
  * memory content is merged into the FIRST system message (not a
    separate message -- preserving the small-model compatibility
    invariant),
  * the ``memory_tokens`` usage slice is non-zero and reported
    distinctly from ``system_tokens``.

Runnable via:
    python -m pytest backend/tests/memory/test_builder_memory_integration.py -v
Or standalone:
    python backend/tests/memory/test_builder_memory_integration.py
"""

from __future__ import annotations

# Standalone-run bootstrap (see test_budget_memory_integration.py
# for the full rationale -- same pattern, same purpose).
import sys as _sys_bootstrap
from pathlib import Path as _Path_bootstrap

if __name__ == "__main__":
    _BACKEND_ROOT = _Path_bootstrap(__file__).resolve().parents[2]
    if str(_BACKEND_ROOT) not in _sys_bootstrap.path:
        _sys_bootstrap.path.insert(0, str(_BACKEND_ROOT))

# --- Normal imports follow ----------------------------------------------

from peldrun.context.builder import ContextManager
from peldrun.context.budget import count_messages_total
from peldrun.context.schema import ContextSidecar
from peldrun.memory.domain import (
    MemoryContext,
    MemoryEntry,
    MemoryProvenance,
    MemoryScope,
    MemorySourceType,
    MemoryType,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_sidecar() -> ContextSidecar:
    """Empty sidecar -- minimal ContextSidecar to exercise build_messages.

    An empty ``sidecar.conversation`` short-circuits the recent-msgs
    builder to ``[]``, so the test does not need to populate the
    budget sections from ratios (which is what
    ``build_context_sidecar`` normally does in production). The
    memory-related behavior is unaffected by this -- memory_msgs is
    independent of the recent slice.
    """
    return ContextSidecar(session_id="test_session")


def _make_memory_entry(
    content: str = "User prefers dark mode",
) -> MemoryEntry:
    return MemoryEntry.create(
        content=content,
        memory_type=MemoryType.PREFERENCE,
        scope_type=MemoryScope.USER,
        scope_id="default",
        provenance=MemoryProvenance(
            source_type=MemorySourceType.EXPLICIT_USER,
        ),
    )


def _make_memory_context_with_entries() -> MemoryContext:
    entry = _make_memory_entry()
    ctx = MemoryContext.empty(
        query="what theme do I prefer?",
        scope_refs=["user:default"],
    )
    return ctx.model_copy(update={"memories": [entry]})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_build_messages_without_memory_context() -> None:
    """Backward compat: omitting memory_context behaves like pre-P1.5."""
    cm = ContextManager()
    messages, usage = cm.build_messages(
        sidecar=_make_sidecar(),
        current_user_message="What next?",
        system_prompt="You are a helpful assistant.",
    )
    assert usage["memory_tokens"] == 0
    assert len(messages) == 2, [m["role"] for m in messages]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "You are a helpful assistant."
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "What next?"
    assert "[RETRIEVED MEMORY" not in messages[0]["content"]


def test_build_messages_with_empty_memory_context() -> None:
    """An empty MemoryContext is equivalent to None -- no injection."""
    cm = ContextManager()
    empty_ctx = MemoryContext.empty(
        query="what next?",
        scope_refs=["user:default"],
    )
    messages, usage = cm.build_messages(
        sidecar=_make_sidecar(),
        current_user_message="What next?",
        system_prompt="You are a helpful assistant.",
        memory_context=empty_ctx,
    )
    assert usage["memory_tokens"] == 0
    assert messages[0]["content"] == "You are a helpful assistant."
    assert "[RETRIEVED MEMORY" not in messages[0]["content"]


def test_build_messages_with_memory_context_merges_into_system() -> None:
    """Memory block merges into the first system message."""
    cm = ContextManager()
    memory_ctx = _make_memory_context_with_entries()
    messages, usage = cm.build_messages(
        sidecar=_make_sidecar(),
        current_user_message="What theme do I prefer?",
        system_prompt="You are a helpful assistant.",
        memory_context=memory_ctx,
    )

    # Non-zero memory tokens
    assert usage["memory_tokens"] > 0

    # Message count still 2 -- memory merged, not a new message
    assert len(messages) == 2, [m["role"] for m in messages]

    # System message contains both base and memory
    sys_content = messages[0]["content"]
    assert messages[0]["role"] == "system"
    assert "You are a helpful assistant." in sys_content
    assert "[RETRIEVED MEMORY" in sys_content
    assert "User prefers dark mode" in sys_content

    # system_tokens is the base-only slice (memory not included)
    base_system_tokens = count_messages_total(
        [{"role": "system", "content": "You are a helpful assistant."}]
    )
    assert usage["system_tokens"] == base_system_tokens, (
        f"system_tokens={usage['system_tokens']} should equal base "
        f"system tokens={base_system_tokens}"
    )


def test_build_messages_memory_becomes_system_when_no_base() -> None:
    """Without a base system prompt, memory forms the first system message."""
    cm = ContextManager()
    memory_ctx = _make_memory_context_with_entries()
    messages, usage = cm.build_messages(
        sidecar=_make_sidecar(),
        current_user_message="Hello?",
        system_prompt=None,
        memory_context=memory_ctx,
    )
    assert len(messages) == 2, [m["role"] for m in messages]
    assert messages[0]["role"] == "system"
    assert "[RETRIEVED MEMORY" in messages[0]["content"]
    assert usage["memory_tokens"] > 0


def test_build_messages_preserves_single_leading_system_message() -> None:
    """After merge there is exactly ONE Tier-1 system message."""
    cm = ContextManager()
    memory_ctx = _make_memory_context_with_entries()
    messages, _usage = cm.build_messages(
        sidecar=_make_sidecar(),
        current_user_message="What?",
        system_prompt="Base directive",
        project_rules="Rule 1",
        memory_context=memory_ctx,
    )

    # Count leading system messages (before any non-system message).
    leading_system_count = 0
    for m in messages:
        if m["role"] == "system":
            leading_system_count += 1
        else:
            break

    assert leading_system_count == 1, (
        f"expected exactly 1 leading system message, "
        f"got {leading_system_count}: {[m['role'] for m in messages]}"
    )

    # All three Tier-1 contributions are present in the single message.
    sys_content = messages[0]["content"]
    assert "Base directive" in sys_content
    assert "[Project Rules]" in sys_content
    assert "[RETRIEVED MEMORY" in sys_content


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def _run_all() -> None:
    import traceback

    test_functions = [
        test_build_messages_without_memory_context,
        test_build_messages_with_empty_memory_context,
        test_build_messages_with_memory_context_merges_into_system,
        test_build_messages_memory_becomes_system_when_no_base,
        test_build_messages_preserves_single_leading_system_message,
    ]
    failures = 0
    for fn in test_functions:
        name = fn.__name__
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}: {exc}")
            traceback.print_exc()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {exc}")
            traceback.print_exc()

    print()
    if failures:
        print(f"=== {failures} test(s) failed ===")
        raise SystemExit(1)
    print(f"=== all {len(test_functions)} tests passed ===")


if __name__ == "__main__":
    _run_all()
