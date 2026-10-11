# backend/tests/test_multi_agent_isolation.py
"""
Unit and integration tests for PELDRUN Native Multi-Agent PR 2 Isolation.
Validates:
1. Rejection of uncloneable/shared specialist instances (Zero Instance Leaks).
2. Concurrency isolation between factory-spawned agent instances.
3. Safe child workspace derivation under parent root.
4. Defense against Path Traversal (dot-dot, absolute path escapes).
5. ContextBridge bounds and sanitization.
6. Filtering of out-of-boundary deliverables.
"""

from __future__ import annotations

from pathlib import Path
import uuid
import pytest

from peldrun.agents.base import AgentConfig, BaseAgent
from peldrun.agents.multi.context_bridge import ContextBridge
from peldrun.agents.multi.coordinator import MultiAgentCoordinator
from peldrun.agents.multi.protocol import DelegatedTask, DelegationStatus
from peldrun.agents.multi.workspace import (
    WorkspaceBoundaryError,
    resolve_child_workspace,
    validate_within_boundary,
)
from peldrun.events.emitter import EventEmitter


class DummySpecialist(BaseAgent):
    """Test stub simulating an agent without a clone method."""
    def __init__(self, name: str = "coder"):
        config = AgentConfig(name=name, system_prompt="Test")
        super().__init__(config=config)
        self.messages = []

    async def _astep(self, state):
        return state

    async def step(self, state):
        return await self._astep(state)


class CloneableSpecialist(DummySpecialist):
    """Test stub providing explicit clone capability."""
    def clone(self):
        new_inst = CloneableSpecialist(name=self.name)
        new_inst.messages = [dict(m) for m in self.messages]
        return new_inst


class TestSpecialistInstanceIsolation:
    """Verifies that agents cannot share state or leak instances across delegations."""

    def test_coordinator_rejects_uncloneable_shared_instance(self):
        emitter = EventEmitter(run_id="test_parent")
        coord = MultiAgentCoordinator(parent_emitter=emitter)

        # Register raw instance that does NOT have a factory or clone() method
        uncloneable = DummySpecialist(name="unsafe_agent")
        coord.register_specialist("unsafe_agent", uncloneable)

        # Attempt resolution -> must return None (Fail-Closed)
        resolved = coord._resolve_specialist_instance("unsafe_agent")
        assert resolved is None

    def test_factory_produces_completely_isolated_instances(self):
        emitter = EventEmitter(run_id="test_parent")
        coord = MultiAgentCoordinator(parent_emitter=emitter)

        coord.register_factory("coder", lambda: DummySpecialist(name="coder"))

        inst_1 = coord._resolve_specialist_instance("coder")
        inst_2 = coord._resolve_specialist_instance("coder")

        assert inst_1 is not None
        assert inst_2 is not None
        assert inst_1 is not inst_2

        # Verify mutation isolation
        inst_1.messages.append({"role": "user", "content": "mutation 1"})
        assert len(inst_1.messages) == 1
        assert len(inst_2.messages) == 0

    def test_cloneable_instance_resolution(self):
        emitter = EventEmitter(run_id="test_parent")
        coord = MultiAgentCoordinator(parent_emitter=emitter)

        original = CloneableSpecialist(name="architect")
        original.messages.append({"role": "system", "content": "base"})
        coord.register_specialist("architect", original)

        cloned = coord._resolve_specialist_instance("architect")
        assert cloned is not None
        assert cloned is not original

        cloned.messages.append({"role": "user", "content": "child message"})
        assert len(cloned.messages) == 2
        assert len(original.messages) == 1


class TestWorkspaceSandboxSecurity:
    """Verifies sandbox boundary enforcement and path traversal defenses."""

    def test_child_workspace_derived_strictly_under_parent(self, tmp_path: Path):
        parent_dir = tmp_path / "workspace_parent"
        child_id = uuid.uuid4()

        child_ws = resolve_child_workspace(parent_dir, child_id)

        assert child_ws.exists()
        assert child_ws.is_relative_to(parent_dir)
        assert child_ws.name == str(child_id)
        assert ".peldrun" in child_ws.parts

    def test_path_traversal_detection(self, tmp_path: Path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()

        # Valid subpaths must succeed
        valid_file = validate_within_boundary(sandbox, "reports/summary.txt")
        assert valid_file.is_relative_to(sandbox)

        # Dot-dot traversal escape attempts must raise WorkspaceBoundaryError
        with pytest.raises(WorkspaceBoundaryError):
            validate_within_boundary(sandbox, "../../etc/passwd")

        with pytest.raises(WorkspaceBoundaryError):
            validate_within_boundary(sandbox, "sub/../../../../outside.txt")

    def test_absolute_path_escape_rejected(self, tmp_path: Path):
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()

        outside = tmp_path / "other_chat" / "sensitive.json"

        with pytest.raises(WorkspaceBoundaryError):
            validate_within_boundary(sandbox, outside)


class TestContextBridge:
    """Verifies prompt framing and context payload bounds."""

    def test_empty_instruction_rejected(self):
        with pytest.raises(ValueError):
            ContextBridge.format_child_prompt(instruction="")

    def test_context_bridge_formats_structured_prompt(self):
        prompt = ContextBridge.format_child_prompt(
            instruction="Implement matrix factorizer",
            context_data={"target_lang": "python", "numpy_version": "2.0"},
            delegation_depth=1,
        )
        assert "[DELEGATED SUBTASK DIRECTIVE (DEPTH 1)]" in prompt
        assert "Implement matrix factorizer" in prompt
        assert "[DELEGATED CONTEXT PARAMETERS]" in prompt
        assert "target_lang" in prompt
        assert "[SYSTEM BOUNDARY]" in prompt


@pytest.mark.asyncio
class TestCoordinatorDelegationSecurity:
    """Verifies end-to-end coordinator security behavior."""

    async def test_delegate_rejects_missing_factory_fail_closed(self, tmp_path: Path):
        emitter = EventEmitter(run_id="parent_run")
        coord = MultiAgentCoordinator(parent_emitter=emitter, workspace_root=str(tmp_path))

        task = DelegatedTask(
            parent_run_id=uuid.uuid4(),
            target_agent_id="non_existent_specialist",
            instruction="Do task",
        )

        res = await coord.delegate(task)
        assert res.success is False
        assert res.status == DelegationStatus.FAILED
        assert res.error_code == "SPECIALIST_INITIALIZATION_FAILED"
