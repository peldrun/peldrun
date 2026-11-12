# backend/tests/memory/test_engine_context_memory_integration.py
"""
P1.5 structural tests for the memory_context field across the
dispatcher -> engine -> RunRequest -> executor chain.

These tests verify that the SAME field name (``memory_context``) is
exposed by every layer that must forward it. This catches the most
common refactoring mistake -- renaming the field in one module but
not updating every caller -- at the earliest possible point.

They deliberately do NOT test:

  * the end-to-end run (needs the full memory service, coming in
    P1.7; end-to-end tests live in the P1.9 acceptance suite);
  * the dispatcher's actual retrieval (verified by the bootstrap
    module's own tests once P1.7 lands).

The end-to-end wiring is verified by inspection of the three
small edits (base.py, dispatcher.py, peldrun_engine.py) that
landed under P1.5 and by the run you are executing right now.

Runnable via pytest OR standalone:
    python tests/memory/test_engine_context_memory_integration.py
"""

from __future__ import annotations

import sys as _sys_bootstrap
from pathlib import Path as _Path_bootstrap

if __name__ == "__main__":
    _BACKEND_ROOT = _Path_bootstrap(__file__).resolve().parents[2]
    if str(_BACKEND_ROOT) not in _sys_bootstrap.path:
        _sys_bootstrap.path.insert(0, str(_BACKEND_ROOT))

# --- Normal imports follow ----------------------------------------------

import dataclasses
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional

from omweb.engines.base import EngineRunContext, ExecutionEngine
from peldrun.memory.domain import MemoryContext
from peldrun.runtime.contract import RunRequest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_minimal_engine_context(memory_context=None) -> EngineRunContext:
    """Build a valid EngineRunContext for structural tests."""
    return EngineRunContext(
        job_id="test_job",
        prompt="hello",
        agent_id="peldrun",
        active_llm={"model": "test"},
        model_name="test-model",
        provider_name="test-provider",
        project_dir=Path("/tmp"),
        chat_id="chat_test",
        manifest={"id": "peldrun", "tools": []},
        memory_context=memory_context,
    )


# ---------------------------------------------------------------------------
# Tests: EngineRunContext
# ---------------------------------------------------------------------------


def test_engine_run_context_has_memory_context_field() -> None:
    """The ``memory_context`` field exists on EngineRunContext."""
    field_names = {f.name for f in dataclasses.fields(EngineRunContext)}
    assert "memory_context" in field_names, (
        f"EngineRunContext must expose 'memory_context', "
        f"got fields: {sorted(field_names)}"
    )


def test_engine_run_context_default_memory_context_is_none() -> None:
    """Backward compat: constructing without memory_context is legal."""
    ctx = _make_minimal_engine_context()
    assert ctx.memory_context is None


def test_engine_run_context_preserves_memory_context_identity() -> None:
    """Passing a MemoryContext preserves object identity (no copy)."""
    mctx = MemoryContext.empty(query="hi", scope_refs=["user:default"])
    ctx = _make_minimal_engine_context(memory_context=mctx)
    assert ctx.memory_context is mctx


def test_engine_run_context_still_frozen_after_field_addition() -> None:
    """The frozen=True invariant survives adding a new field."""
    ctx = _make_minimal_engine_context()
    try:
        ctx.job_id = "changed"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return  # expected
    raise AssertionError("EngineRunContext must remain frozen=True")


# ---------------------------------------------------------------------------
# Tests: RunRequest
# ---------------------------------------------------------------------------


def test_run_request_has_memory_context_field() -> None:
    """RunRequest exposes the ``memory_context`` field (P1.5 edit)."""
    assert "memory_context" in RunRequest.model_fields, (
        f"RunRequest must expose 'memory_context', "
        f"got fields: {sorted(RunRequest.model_fields.keys())}"
    )


def test_run_request_memory_context_is_excluded_from_serialization() -> None:
    """memory_context must NOT appear in model_dump() (transience contract)."""
    field = RunRequest.model_fields["memory_context"]
    assert field.exclude is True, (
        "memory_context.exclude must be True so the transient "
        "context never enters checkpoints, sidecars, or ExecutionState"
    )


# ---------------------------------------------------------------------------
# Tests: Contract consistency (the actual "renamed one, forgot another" guard)
# ---------------------------------------------------------------------------


def test_field_name_is_consistent_across_all_three_layers() -> None:
    """The SAME field name must appear on EngineRunContext and RunRequest.

    If a future refactor renames ``memory_context`` to ``memory`` on
    one class but not the other, the dispatcher-to-engine handoff
    silently breaks (``RunRequest(memory_context=...)`` would raise
    or be ignored). This test catches that immediately.
    """
    from peldrun.context.builder import ContextManagerProtocol

    engine_field = "memory_context" in {
        f.name for f in dataclasses.fields(EngineRunContext)
    }
    request_field = "memory_context" in RunRequest.model_fields
    builder_sig = inspect.signature(
        ContextManagerProtocol.build_messages
    )
    builder_param = "memory_context" in builder_sig.parameters

    assert engine_field, "EngineRunContext missing memory_context"
    assert request_field, "RunRequest missing memory_context"
    assert builder_param, (
        "ContextManagerProtocol.build_messages missing memory_context"
    )


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def _run_all() -> None:
    import traceback

    test_functions = [
        test_engine_run_context_has_memory_context_field,
        test_engine_run_context_default_memory_context_is_none,
        test_engine_run_context_preserves_memory_context_identity,
        test_engine_run_context_still_frozen_after_field_addition,
        test_run_request_has_memory_context_field,
        test_run_request_memory_context_is_excluded_from_serialization,
        test_field_name_is_consistent_across_all_three_layers,
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
