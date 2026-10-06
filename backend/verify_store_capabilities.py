"""
Automated Verification Suite for PR 4 (Store & Capability Correctness).

Validates:
1. AgentRegistry strict lookup and fail-fast: Unknown agent raises AgentNotFoundError.
2. Capability scoping: Agent tools are strictly governed by manifest without essential_tool_ids injection.
3. Concurrency safety: WebToolAdapter executes cleanly with ZERO process-wide os.chdir side-effects.
4. Dispatcher fail-fast on unknown agent ID.
"""

import asyncio
import os
import sys
from pathlib import Path


def main() -> int:
    backend_dir = Path(__file__).resolve().parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    print("=" * 70)
    print("PELDRUN STORE & CAPABILITY CORRECTNESS VERIFICATION (PR 4)")
    print(f"Working Directory: {backend_dir}")
    print("=" * 70)

    # 1. Test Agent Registry Strict Lookup (No Silent Fallback)
    print("\n[TEST 1] Verifying AgentRegistry Fail-Fast Lookup...")
    from omweb.agents.registry import AgentNotFoundError, agent_registry

    try:
        peldrun_agent = agent_registry.get_agent("peldrun")
        assert peldrun_agent["id"] == "peldrun"
        print("  [OK] Built-in agent 'peldrun' resolved successfully.")
    except Exception as e:
        print(f"  [FAIL] Failed resolving 'peldrun': {e}")
        return 1

    try:
        agent_registry.get_agent("unknown_agent_random_999")
        print("  [FAIL] Unknown agent did NOT raise AgentNotFoundError! Silent fallback detected.")
        return 1
    except AgentNotFoundError as e:
        print(f"  [OK] Strict fail-fast confirmed: {e}")
    except Exception as e:
        print(f"  [FAIL] Unexpected exception raised: {type(e)}: {e}")
        return 1

    # 2. Test Capability Scoping (No essential_tool_ids injection)
    print("\n[TEST 2] Verifying Capability Scoping (Elimination of essential_tool_ids)...")
    analyst_agent = agent_registry.get_agent("analyst")
    declared_tools = set(analyst_agent.get("tools", []))
    print(f"  [*] Declared tools for 'analyst': {declared_tools}")

    # Analyst must NOT request bash or web_search
    if "bash" in declared_tools or "web_search" in declared_tools:
        print("  [FAIL] Analyst agent definition has unexpected tool bindings.")
        return 1

    # Simulate tool registry resolution as performed in PeldrunEngine
    from peldrun.tools.builtins.terminate import TerminateTool
    from peldrun.tools.registry import ToolRegistry as CoreToolRegistry
    from omweb.adapters.core_adapter import resolve_tool_runtime
    from omweb.tools.registry import tool_registry as web_tool_registry

    core_registry = CoreToolRegistry(workspace_root=str(backend_dir))
    core_registry.register(TerminateTool(workspace_root=str(backend_dir)))

    available_web_tools = {
        t["id"]: t for t in web_tool_registry.list_tools() if t.get("is_enabled", True)
    }

    # Follow the new clean resolution logic (no essential_tool_ids)
    for req_tool in declared_tools:
        if req_tool in ("terminate", "file_saver"):
            continue
        if req_tool in available_web_tools:
            real_adapter = resolve_tool_runtime(
                tool_id=req_tool,
                tool_meta=available_web_tools[req_tool],
                workspace_root=backend_dir,
                registry=web_tool_registry,
            )
            core_registry.register(real_adapter)

    # Core ToolRegistry returns List[ToolRuntime], extract names robustly
    raw_tools = core_registry.list_tools()
    registered_tool_names = set()
    for t in raw_tools:
        if hasattr(t, "name"):
            registered_tool_names.add(t.name)
        elif isinstance(t, str):
            registered_tool_names.add(t)
        elif isinstance(t, dict):
            registered_tool_names.add(t.get("name") or t.get("id"))

    print(f"  [*] Registered Core Tools for 'analyst': {registered_tool_names}")

    # Expected: terminate + declared tools only
    expected_tools = {"terminate"}.union(declared_tools.intersection(set(available_web_tools.keys())))
    if registered_tool_names != expected_tools:
        print(f"  [FAIL] Tool mismatch! Registered: {registered_tool_names}, Expected: {expected_tools}")
        return 1
    if "bash" in registered_tool_names or "web_search" in registered_tool_names:
        print("  [FAIL] essential_tool_ids pollution detected! 'bash' or 'web_search' injected into analyst.")
        return 1
    print("  [OK] Capability isolation verified: Agent received strictly assigned tools.")

    # 3. Test Zero Process CWD Side-Effects
    print("\n[TEST 3] Verifying WebToolAdapter Zero CWD Mutation (Concurrency Safety)...")
    from omweb.adapters.core_adapter_parts.web_tool_adapter import WebToolAdapter

    initial_cwd = os.getcwd()
    test_subfolder = backend_dir / "omweb"

    def dummy_executor(val: str = "test", **kwargs):
        return f"executed with val={val}, process_cwd={os.getcwd()}"

    adapter = WebToolAdapter(
        name="test_tool",
        description="A test tool verifying zero cwd mutation",
        executor=dummy_executor,
        workspace_root=test_subfolder,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        res = loop.run_until_complete(adapter.aexecute(val="concurrency_check"))
        after_cwd = os.getcwd()

        if initial_cwd != after_cwd:
            print(f"  [FAIL] Process CWD was mutated! Before: {initial_cwd}, After: {after_cwd}")
            return 1
        print(f"  [OK] Process CWD remained completely immutable: {after_cwd}")
        print(f"  [OK] Tool output: {res.output}")
    finally:
        loop.close()

    # 4. Test Dispatcher Fail-Fast on Unknown Agent
    print("\n[TEST 4] Verifying Dispatcher Fail-Fast on Unknown Agent...")
    from omweb.agent_bridge import run_instrumented
    from omweb.job_manager import job_manager

    async def test_unknown_agent_dispatch():
        test_job_id = "test-failfast-agent-001"
        job_manager.create_job(test_job_id)

        await run_instrumented(
            test_job_id,
            "test prompt",
            agent_id="non_existent_agent_999",
            engine="peldrun",
        )

        job = job_manager.get_job(test_job_id)
        if not job:
            print("  [FAIL] Job not found.")
            return False

        raw_status = getattr(job, "status", "")
        status_val = (raw_status.value if hasattr(raw_status, "value") else str(raw_status)).lower()
        error_msg = str(getattr(job, "error", "") or "")

        if status_val != "failed":
            print(f"  [FAIL] Expected job status to be 'failed', got: '{status_val}'")
            return False

        if "is not registered in the Agent Store" not in error_msg:
            print(f"  [FAIL] Unexpected error message: '{error_msg}'")
            return False

        print(f"  [OK] Job failed authoritatively with: {error_msg}")
        return True

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        passed = loop.run_until_complete(test_unknown_agent_dispatch())
        if not passed:
            return 1
    finally:
        loop.close()

    print("\n" + "=" * 70)
    print("PR 4 VERIFICATION RESULT: ALL CHECKS PASSED (100% READY)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())