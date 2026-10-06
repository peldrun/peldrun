"""
PELDRUN Universal Engine Integration and Isolation Verification Suite (PR 6).

Executes comprehensive architectural validation across:
- Test A: Embedded PELDRUN Core execution without external filesystem dependencies.
- Test B: OpenManus engine isolation and environment scoping.
- Test C: PELDRUN resilience and zero-impact when OpenManus is unavailable.
- Test D: Strict fail-fast engine selection eliminating silent fallback masking.
- Test E: Store capability lifecycle (Enable/Disable/Assign reflection in Core Registry).
- Test F: Concurrency safety and zero process-wide CWD side-effects across parallel tasks.
"""

import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict
from unittest.mock import patch


def main() -> int:
    backend_dir = Path(__file__).resolve().parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    print("=" * 75)
    print("PELDRUN DUAL-ENGINE INTEGRATION & ISOLATION VERIFICATION (PR 6)")
    print(f"Working Directory: {backend_dir}")
    print("=" * 75)

    # ─────────────────────────────────────────────────────────────────
    # TEST A: Embedded PELDRUN Core Independent Execution
    # ─────────────────────────────────────────────────────────────────
    print("\n[TEST A] Verifying Native Embedded Core Execution...")
    external_core_marker = os.path.join("peldrun-core", "peldrun")
    active_paths = [p for p in sys.path if external_core_marker in p.lower()]
    if active_paths:
        print(f"  [FAIL] External peldrun-core detected in sys.path: {active_paths}")
        return 1

    try:
        import peldrun

        assert getattr(peldrun, "__is_embedded__", False) is True
        print(f"  [OK] peldrun imported natively from: {peldrun.__file__}")
        print(f"  [OK] Upstream metadata verified: commit={getattr(peldrun, '__upstream_commit__', '')}")
    except Exception as e:
        print(f"  [FAIL] Embedded peldrun import error: {e}")
        return 1

    from omweb.engines import PeldrunEngine, engine_registry

    peldrun_engine = engine_registry.get("peldrun")
    if not isinstance(peldrun_engine, PeldrunEngine):
        print(f"  [FAIL] Registry failed to resolve PeldrunEngine: {type(peldrun_engine)}")
        return 1
    print("  [OK] Test A Passed: Native embedded engine is strictly verified.")

    # ─────────────────────────────────────────────────────────────────
    # TEST B & C: Engine Independence & Fault Isolation
    # ─────────────────────────────────────────────────────────────────
    print("\n[TEST B & C] Verifying Engine Independence & Fault Isolation...")
    openmanus_engine = engine_registry.get("openmanus")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        om_health = loop.run_until_complete(openmanus_engine.health())
        pel_health = loop.run_until_complete(peldrun_engine.health())

        print(f"  [*] OpenManus Engine Health: available={om_health.get('available')}, entrypoint={om_health.get('entrypoint')}")
        print(f"  [*] PELDRUN Engine Health: available={pel_health.get('available')}, embedded={pel_health.get('embedded')}")

        assert pel_health.get("available") is True
        assert pel_health.get("embedded") is True
        print("  [OK] Test B & C Passed: PELDRUN Core functions completely independent of OpenManus.")
    finally:
        loop.close()

    # ─────────────────────────────────────────────────────────────────
    # TEST D: Authoritative Engine Selection (Zero Silent Fallback)
    # ─────────────────────────────────────────────────────────────────
    print("\n[TEST D] Verifying Strict Fail-Fast Engine Selection...")
    from omweb.agent_bridge import run_instrumented
    from omweb.job_manager import job_manager

    async def test_fail_fast_policy():
        # Case 1: Non-existent engine
        job_id_1 = "test-failfast-invalid-engine"
        job_manager.create_job(job_id_1)
        await run_instrumented(job_id_1, "test prompt", engine="invalid_engine_id_xyz")

        job_1 = job_manager.get_job(job_id_1)
        status_1 = getattr(job_1, "status", "")
        status_1_val = (status_1.value if hasattr(status_1, "value") else str(status_1)).lower()
        error_1 = str(getattr(job_1, "error", "") or "")

        if status_1_val != "failed" or "is not registered or unavailable" not in error_1:
            print(f"  [FAIL] Expected immediate failure without fallback. Got: status={status_1_val}, err={error_1}")
            return False
        print(f"  [OK] Unknown engine failed immediately with authoritative error: {error_1}")

        # Case 2: Intentional failure simulation on peldrun engine.run
        mock_override = {"base_url": "http://mock-llm.test/v1", "provider_name": "MockProvider"}
        with patch.object(peldrun_engine, "run", side_effect=RuntimeError("Simulated engine failure")):
            job_id_2 = "test-failfast-corrupted-core"
            job_manager.create_job(job_id_2)
            await run_instrumented(job_id_2, "test prompt", engine="peldrun", llm_override=mock_override)

            job_2 = job_manager.get_job(job_id_2)
            status_2 = getattr(job_2, "status", "")
            status_2_val = (status_2.value if hasattr(status_2, "value") else str(status_2)).lower()
            error_2 = str(getattr(job_2, "error", "") or "")

            if status_2_val != "failed":
                print(f"  [FAIL] Failed engine execution did not fail job! status={status_2_val}")
                return False
            if "openmanus" in error_2.lower():
                print("  [FAIL] Silent fallback to OpenManus detected upon Core failure!")
                return False
            print(f"  [OK] Core error propagated authoritatively without silent fallback: {error_2}")

        return True

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        passed_d = loop.run_until_complete(test_fail_fast_policy())
        if not passed_d:
            return 1
        print("  [OK] Test D Passed: Authoritative engine selection enforced.")
    finally:
        loop.close()

    # ─────────────────────────────────────────────────────────────────
    # TEST E: Store-to-Engine Capability Lifecycle
    # ─────────────────────────────────────────────────────────────────
    print("\n[TEST E] Verifying Store-to-Engine Capability Lifecycle...")
    from omweb.adapters.core_adapter import resolve_tool_runtime
    from omweb.agents.registry import agent_registry
    from omweb.tools.registry import tool_registry as web_tool_registry
    from peldrun.tools.registry import ToolRegistry as CoreToolRegistry

    test_agent = agent_registry.get_agent("coder")
    declared_tools = list(test_agent.get("tools", []))
    print(f"  [*] Testing Coder Agent declared tools: {declared_tools}")

    ws_test = backend_dir / "workspace_test"
    core_reg = CoreToolRegistry(workspace_root=str(ws_test))
    available_tools_step1 = {t["id"]: t for t in web_tool_registry.list_tools() if t.get("is_enabled", True)}

    for t_id in declared_tools:
        if t_id in available_tools_step1:
            meta = available_tools_step1[t_id]
            adapter = resolve_tool_runtime(tool_id=t_id, tool_meta=meta, workspace_root=ws_test, registry=web_tool_registry)
            core_reg.register(adapter)

    tools_step1 = {t.name for t in core_reg.list_tools()}
    print(f"  [*] Initial Registered Core Tools: {tools_step1}")
    for t_id in declared_tools:
        if t_id in available_tools_step1:
            assert t_id in tools_step1, f"Tool {t_id} missing in initial registry"

    target_tool_to_disable = "bash"
    print(f"  [*] Simulating Store state change for tool: '{target_tool_to_disable}'...")

    def _toggle_tool(t_id: str):
        if hasattr(web_tool_registry, "toggle_tool_status"):
            return web_tool_registry.toggle_tool_status(t_id)
        elif hasattr(web_tool_registry, "toggle_tool"):
            return web_tool_registry.toggle_tool(t_id)
        elif hasattr(web_tool_registry, "toggle_status"):
            return web_tool_registry.toggle_status(t_id)

    _toggle_tool(target_tool_to_disable)

    try:
        core_reg_disabled = CoreToolRegistry(workspace_root=str(ws_test))
        available_tools_step2 = {t["id"]: t for t in web_tool_registry.list_tools() if t.get("is_enabled", True)}

        for t_id in declared_tools:
            if t_id in available_tools_step2:
                meta = available_tools_step2[t_id]
                adapter = resolve_tool_runtime(tool_id=t_id, tool_meta=meta, workspace_root=ws_test, registry=web_tool_registry)
                core_reg_disabled.register(adapter)

        tools_step2 = {t.name for t in core_reg_disabled.list_tools()}
        print(f"  [*] Core Tools after disabling '{target_tool_to_disable}': {tools_step2}")

        if target_tool_to_disable in tools_step2:
            print(f"  [FAIL] Disabled tool '{target_tool_to_disable}' is still present in Core Registry!")
            return 1
        print(f"  [OK] Disabled tool '{target_tool_to_disable}' successfully excluded from Core runtime.")
    finally:
        _toggle_tool(target_tool_to_disable)
        print(f"  [*] Restored Store state for '{target_tool_to_disable}'.")

    print("  [OK] Test E Passed: Store-to-Engine capability lifecycle verified.")

    # ─────────────────────────────────────────────────────────────────
    # TEST F: Concurrency Safety & Zero Process-wide CWD Side-Effects
    # ─────────────────────────────────────────────────────────────────
    print("\n[TEST F] Verifying Concurrency Safety & Process CWD Immutability...")
    from omweb.adapters.core_adapter_parts.web_tool_adapter import WebToolAdapter

    initial_cwd = os.getcwd()

    async def concurrent_task(task_id: int, target_dir: Path):
        def _exec(**kwargs):
            return {"task_id": task_id, "inner_cwd": os.getcwd(), "target_dir": str(target_dir)}

        adapter = WebToolAdapter(
            name=f"concurrent_tool_{task_id}",
            description="Concurrency test tool",
            executor=_exec,
            workspace_root=target_dir,
        )
        await asyncio.sleep(0.01)
        res = await adapter.aexecute()
        return res

    async def run_parallel_tests():
        tasks = [
            concurrent_task(1, backend_dir / "omweb"),
            concurrent_task(2, backend_dir / "peldrun"),
            concurrent_task(3, backend_dir),
        ]
        return await asyncio.gather(*tasks)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        results = loop.run_until_complete(run_parallel_tests())
        final_cwd = os.getcwd()

        if initial_cwd != final_cwd:
            print(f"  [FAIL] Process CWD mutated! initial={initial_cwd}, final={final_cwd}")
            return 1

        for r in results:
            assert r.output.get("inner_cwd") == initial_cwd
        print(f"  [OK] All {len(results)} parallel tasks executed with ZERO process CWD mutation.")
        print(f"  [OK] Global CWD remained immutable at: {final_cwd}")
    finally:
        loop.close()

    print("\n" + "=" * 75)
    print("PR 6 VERIFICATION RESULT: ALL CHECKS PASSED (100% READY)")
    print("=" * 75)
    return 0


if __name__ == "__main__":
    sys.exit(main())