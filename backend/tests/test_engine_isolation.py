"""
Pytest Integration Suite for Dual-Engine Isolation and Architecture Contracts.
"""

import asyncio
import os
from pathlib import Path
import sys
from unittest.mock import patch
import pytest

from omweb.agent_bridge import run_instrumented
from omweb.agents.registry import agent_registry
from omweb.engines import PeldrunEngine, engine_registry
from omweb.job_manager import job_manager
from omweb.tools.registry import tool_registry as web_tool_registry
from peldrun.tools.registry import ToolRegistry as CoreToolRegistry


def test_test_a_embedded_peldrun_isolation():
    """Test A: Verify PELDRUN Core runs natively embedded without external sys.path injection."""
    import peldrun

    assert getattr(peldrun, "__is_embedded__", False) is True

    external_marker = os.path.join("peldrun-core", "peldrun")
    active_paths = [p for p in sys.path if external_marker in p.lower()]
    assert len(active_paths) == 0, f"Found external core in sys.path: {active_paths}"

    engine = engine_registry.get("peldrun")
    assert isinstance(engine, PeldrunEngine)


@pytest.mark.asyncio
async def test_test_bc_engine_independence():
    """Test B & C: Verify PELDRUN Core is completely operational regardless of OpenManus availability."""
    peldrun_engine = engine_registry.get("peldrun")
    openmanus_engine = engine_registry.get("openmanus")

    om_health = await openmanus_engine.health()
    pel_health = await peldrun_engine.health()

    assert pel_health.get("available") is True
    assert pel_health.get("embedded") is True
    assert isinstance(om_health.get("available"), bool)


@pytest.mark.asyncio
async def test_test_d_strict_fail_fast_on_invalid_engine():
    """Test D: Requesting an unknown engine must fail fast without silent fallback."""
    job_id = "pytest-failfast-engine-test"
    job_manager.create_job(job_id)

    await run_instrumented(job_id, "test prompt", engine="unregistered_engine_xyz")

    job = job_manager.get_job(job_id)
    raw_status = getattr(job, "status", "")
    status_str = (raw_status.value if hasattr(raw_status, "value") else str(raw_status)).lower()
    error_str = str(getattr(job, "error", "") or "")

    assert status_str == "failed"
    assert "is not registered or unavailable" in error_str


@pytest.mark.asyncio
async def test_test_e_store_capability_scoping_lifecycle(tmp_path):
    """Test E: Agent capabilities strictly respect Store state and immediate toggle reflection."""
    agent_manifest = agent_registry.get_agent("coder")
    declared_tools = list(agent_manifest.get("tools", []))
    assert "bash" in declared_tools

    from omweb.adapters.core_adapter import resolve_tool_runtime

    core_reg = CoreToolRegistry(workspace_root=str(tmp_path))
    available_tools = {t["id"]: t for t in web_tool_registry.list_tools() if t.get("is_enabled", True)}

    for t_id in declared_tools:
        if t_id in available_tools:
            meta = available_tools[t_id]
            adapter = resolve_tool_runtime(tool_id=t_id, tool_meta=meta, workspace_root=tmp_path, registry=web_tool_registry)
            core_reg.register(adapter)

    tools_initial = {t.name for t in core_reg.list_tools()}
    assert "bash" in tools_initial

    # Toggle tool off
    def _toggle(t_name: str):
        if hasattr(web_tool_registry, "toggle_tool_status"):
            return web_tool_registry.toggle_tool_status(t_name)
        elif hasattr(web_tool_registry, "toggle_tool"):
            return web_tool_registry.toggle_tool(t_name)
        elif hasattr(web_tool_registry, "toggle_status"):
            return web_tool_registry.toggle_status(t_name)

    _toggle("bash")
    try:
        core_reg_disabled = CoreToolRegistry(workspace_root=str(tmp_path))
        available_after = {t["id"]: t for t in web_tool_registry.list_tools() if t.get("is_enabled", True)}
        for t_id in declared_tools:
            if t_id in available_after:
                adapter = resolve_tool_runtime(tool_id=t_id, tool_meta=available_after[t_id], workspace_root=tmp_path, registry=web_tool_registry)
                core_reg_disabled.register(adapter)

        tools_after = {t.name for t in core_reg_disabled.list_tools()}
        assert "bash" not in tools_after
    finally:
        _toggle("bash")


@pytest.mark.asyncio
async def test_test_f_zero_cwd_mutation_concurrency(tmp_path):
    """Test F: Parallel executions must execute with zero process-wide CWD side-effects."""
    from omweb.adapters.core_adapter_parts.web_tool_adapter import WebToolAdapter

    initial_cwd = os.getcwd()
    sub_dir_1 = tmp_path / "sub1"
    sub_dir_2 = tmp_path / "sub2"
    sub_dir_1.mkdir()
    sub_dir_2.mkdir()

    async def run_adapter(w_root: Path):
        def _exec(**kwargs):
            return os.getcwd()

        adapter = WebToolAdapter(name="cwd_probe", description="CWD test", executor=_exec, workspace_root=w_root)
        res = await adapter.aexecute()
        return res.output

    results = await asyncio.gather(run_adapter(sub_dir_1), run_adapter(sub_dir_2))

    assert os.getcwd() == initial_cwd
    for r in results:
        assert r == initial_cwd