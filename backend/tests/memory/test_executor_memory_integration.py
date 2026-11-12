# backend/tests/memory/test_executor_memory_integration.py
"""
P1.5 integration tests for memory forwarding in ``CoreRuntimeExecutor``.

These tests exercise the CONTRACT between ``executor.py`` and
``ContextManager.build_messages``:

  * on a FRESH run, ``request.memory_context`` is forwarded to
    ``build_messages`` unchanged (the exact same object identity);
  * on a FRESH run with ``memory_context=None``, the builder
    receives ``None`` (not a fabricated empty context);
  * on a RESUMED run, ``build_messages`` IS still called (the code
    calls it unconditionally) with the forwarded
    ``memory_context``, BUT its output is discarded -- the agent
    sees the persisted state's messages, not the fresh build. This
    prevents duplicate memory injection.

The tests use lightweight fakes:

  * ``_FakeContextManager`` returns distinctive content so the
    test can verify whether its output was used or discarded.
  * The adapter factory is mocked to return a dummy adapter so the
    executor reaches ``build_messages``.
  * ``AgentRunner.run`` is mocked to raise AFTER ``build_messages``,
    so the tests can observe the forwarding without executing an
    agent loop.

The resulting ``RunResult`` is always FAILED (the runner raises by
design) -- the test's purpose is to verify the forwarding, not the
end-to-end run outcome.

Runnable via pytest OR standalone (see test_budget_memory_integration.py
for the bootstrap pattern).
"""

from __future__ import annotations

import sys as _sys_bootstrap
from pathlib import Path as _Path_bootstrap

if __name__ == "__main__":
    _BACKEND_ROOT = _Path_bootstrap(__file__).resolve().parents[2]
    if str(_BACKEND_ROOT) not in _sys_bootstrap.path:
        _sys_bootstrap.path.insert(0, str(_BACKEND_ROOT))

# --- Normal imports follow ----------------------------------------------

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from peldrun.engine.state import ExecutionState, MessageRole
from peldrun.memory.domain import (
    MemoryContext,
    MemoryEntry,
    MemoryProvenance,
    MemoryScope,
    MemorySourceType,
    MemoryType,
)
from peldrun.runtime.contract import (
    AgentSpec,
    RunRequest,
    RunStatus,
    WorkspaceContext,
)
from peldrun.runtime.executor import CoreRuntimeExecutor


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeContextManager:
    """Records every build_messages call for post-hoc inspection.

    Returns DISTINCTIVE content so tests can verify whether the
    output was actually used (fresh run) or discarded (resume).
    """

    calls: List[Dict[str, Any]] = []

    def build_messages(
        self,
        sidecar=None,
        current_user_message="",
        system_prompt=None,
        project_rules=None,
        include_execution=True,
        generative_ui=None,
        memory_context=None,
    ):
        _FakeContextManager.calls.append({
            "memory_context": memory_context,
            "current_user_message": current_user_message,
        })
        return (
            [
                {
                    "role": "system",
                    "content": "FAKE_CTX_MGR_BASE_SYSTEM",
                },
                {
                    "role": "user",
                    "content": current_user_message or "",
                },
            ],
            {
                "system_tokens": 1,
                "summary_tokens": 0,
                "recent_tokens": 0,
                "execution_tokens": 0,
                "memory_tokens": (
                    0 if memory_context is None else 10
                ),
                "current_tokens": 1,
                "total_tokens": 2,
                "effective_limit": 8000,
            },
        )

    @classmethod
    def reset(cls) -> None:
        cls.calls = []


def _make_memory_context() -> MemoryContext:
    entry = MemoryEntry.create(
        content="User prefers dark mode",
        memory_type=MemoryType.PREFERENCE,
        scope_type=MemoryScope.USER,
        scope_id="default",
        provenance=MemoryProvenance(
            source_type=MemorySourceType.EXPLICIT_USER,
        ),
    )
    return MemoryContext.empty(
        query="theme?",
        scope_refs=["user:default"],
    ).model_copy(update={"memories": [entry]})


def _make_request(
    memory_context: Optional[MemoryContext] = None,
) -> RunRequest:
    return RunRequest(
        job_id="test_job",
        prompt="hello",
        agent_spec=AgentSpec(
            id="peldrun",
            name="Peldrun",
            system_prompt="You are a test agent.",
            tools=[],
            max_steps=1,
        ),
        workspace=WorkspaceContext(
            workspace_id="ws",
            root_path="/tmp",
        ),
        llm_config={
            "model": "test-model",
            "provider": "openai_compat",
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key": "EMPTY",
        },
        memory_context=memory_context,
    )


def _make_patches():
    """Return the standard patch bundle for executor tests.

    The adapter factory returns a MagicMock (not raises) so the
    executor proceeds past step 4 and REACHES build_messages.
    The runner's ``run`` raises so the executor returns cleanly
    after the assertions can observe the forwarding.
    """
    from peldrun.llm.contracts import ModelCapabilities

    fake_factory = MagicMock()
    fake_factory.create.return_value = MagicMock()  # dummy adapter

    fake_agent_cls = MagicMock()
    fake_agent_instance = MagicMock()
    fake_agent_cls.return_value = fake_agent_instance

    fake_runner_cls = MagicMock()
    fake_runner_instance = MagicMock()
    fake_runner_instance.run = AsyncMock(
        side_effect=RuntimeError("runner short-circuited by test")
    )
    fake_runner_cls.return_value = fake_runner_instance

    patches = {
        "cm": patch(
            "peldrun.runtime.executor.ContextManager",
            _FakeContextManager,
        ),
        "factory": patch(
            "peldrun.runtime.executor.provider_factory",
            fake_factory,
        ),
        "agent": patch(
            "peldrun.runtime.executor.ToolCallAgent",
            fake_agent_cls,
        ),
        "runner": patch(
            "peldrun.runtime.executor.AgentRunner",
            fake_runner_cls,
        ),
        "caps": patch(
            "peldrun.runtime.executor.resolve_model_capabilities",
            lambda **kw: ModelCapabilities(),
        ),
    }
    return patches, fake_agent_instance


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fresh_run_forwards_memory_context() -> None:
    """Fresh run passes request.memory_context into build_messages."""
    _FakeContextManager.reset()
    memory_ctx = _make_memory_context()
    request = _make_request(memory_context=memory_ctx)
    patches, _fake_agent = _make_patches()

    with (
        patches["cm"],
        patches["factory"],
        patches["agent"],
        patches["runner"],
        patches["caps"],
    ):
        executor = CoreRuntimeExecutor()
        result = asyncio.run(executor.execute(request=request))

    assert len(_FakeContextManager.calls) == 1, (
        f"expected 1 build_messages call, "
        f"got {len(_FakeContextManager.calls)}"
    )
    forwarded = _FakeContextManager.calls[0]["memory_context"]
    assert forwarded is memory_ctx, (
        "executor must forward the EXACT memory_context object"
    )
    # Result is FAILED -- the runner mock raises by design. The
    # failure IS the test's stopping point, not a test failure.
    assert result.status == RunStatus.FAILED


def test_fresh_run_without_memory_context() -> None:
    """When request.memory_context is None, build_messages gets None."""
    _FakeContextManager.reset()
    request = _make_request(memory_context=None)
    patches, _fake_agent = _make_patches()

    with (
        patches["cm"],
        patches["factory"],
        patches["agent"],
        patches["runner"],
        patches["caps"],
    ):
        executor = CoreRuntimeExecutor()
        asyncio.run(executor.execute(request=request))

    assert len(_FakeContextManager.calls) == 1
    forwarded = _FakeContextManager.calls[0]["memory_context"]
    assert forwarded is None, (
        "when request.memory_context is None, build_messages "
        "must receive None (not a fabricated empty context)"
    )


def test_resumed_run_uses_restored_state_not_built_messages() -> None:
    """On resume, agent.messages comes from restored_state, not from
    build_messages.

    Note: the executor DOES call build_messages on every execute()
    call -- including resumes -- because the [B14 FIX] structure
    places it before the ``if restored_state is not None:`` branch.
    However, the built output is DISCARDED on resume: the persisted
    state's messages are what reach the agent. This test verifies
    exactly that:

      1. build_messages WAS called (with our memory_context).
      2. Its distinctive content is NOT in agent.messages.
      3. The persisted state's content IS in agent.messages.
    """
    _FakeContextManager.reset()
    memory_ctx = _make_memory_context()
    request = _make_request(memory_context=memory_ctx)

    restored = ExecutionState(
        run_id=str(request.run_id),
        task_prompt="hello",
        agent_name="Peldrun",
        max_steps=1,
        workspace_root="/tmp",
        metadata={},
    )
    restored.add_message(
        role=MessageRole.SYSTEM,
        content="ORIGINAL_SYSTEM_WITH_MEMORY",
    )
    restored.add_message(
        role=MessageRole.USER,
        content="ORIGINAL_USER_PROMPT",
    )

    patches, fake_agent = _make_patches()

    with (
        patches["cm"],
        patches["factory"],
        patches["agent"],
        patches["runner"],
        patches["caps"],
    ):
        executor = CoreRuntimeExecutor()
        asyncio.run(
            executor.execute(
                request=request,
                restored_state=restored,
            )
        )

    # build_messages was called (executor does not skip it) with our memory_ctx.
    assert len(_FakeContextManager.calls) == 1, (
        "the executor calls build_messages even on resume -- the "
        "B14 FIX structure places the call before the resume branch"
    )
    assert _FakeContextManager.calls[0]["memory_context"] is memory_ctx

    # But the fake ctx mgr's distinctive content must NOT appear
    # in the agent -- resume uses restored_state.messages verbatim.
    agent_messages = fake_agent.messages
    contents = [m.get("content", "") for m in agent_messages]

    assert "ORIGINAL_SYSTEM_WITH_MEMORY" in contents, (
        f"restored state content must be preserved in agent.messages, "
        f"got {contents}"
    )
    assert "ORIGINAL_USER_PROMPT" in contents
    assert "FAKE_CTX_MGR_BASE_SYSTEM" not in contents, (
        f"build_messages output must NOT reach the agent on resume, "
        f"but found 'FAKE_CTX_MGR_BASE_SYSTEM' in {contents}"
    )


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def _run_all() -> None:
    import traceback

    test_functions = [
        test_fresh_run_forwards_memory_context,
        test_fresh_run_without_memory_context,
        test_resumed_run_uses_restored_state_not_built_messages,
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
