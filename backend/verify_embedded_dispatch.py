"""
Automated Verification Suite for PR 3 (Dispatcher Switch to Embedded Runtime).

Validates:
1. Environment isolation: sys.path is free of external peldrun-core.
2. Dispatch routing: run_instrumented routes to engine_registry cleanly.
3. Fail-fast error handling: Unknown engines trigger immediate job failure without silent fallback.
4. Correct inspection of Pydantic Job models and error propagation.
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
    print("PELDRUN DISPATCHER EMBEDDED RUNTIME VERIFICATION (PR 3)")
    print(f"Working Directory: {backend_dir}")
    print("=" * 70)

    # 1. Verify Clean Isolation - Ensure external peldrun-core is NOT in sys.path
    external_core_marker = os.path.join("peldrun-core", "peldrun")
    active_paths = [p for p in sys.path if external_core_marker in p.lower()]
    if active_paths:
        print(f"[FAIL] Detected external peldrun-core in sys.path: {active_paths}")
        return 1
    print("[OK] Environment isolation confirmed (no external sys.path injection).")

    # 2. Verify Imports from agent_bridge and omweb.engines
    try:
        from omweb.agent_bridge import run_instrumented, run_direct_chat
        from omweb.engines import engine_registry, PeldrunEngine
    except ImportError as e:
        print(f"[FAIL] Import error from agent_bridge or engines: {e}")
        return 1
    print("[OK] agent_bridge entry points and engine registry imported cleanly.")

    # 3. Verify Registered Default Engine
    default_engine = engine_registry.get("peldrun")
    if not isinstance(default_engine, PeldrunEngine):
        print(f"[FAIL] 'peldrun' did not resolve to PeldrunEngine instance: {type(default_engine)}")
        return 1
    print("[OK] 'peldrun' resolves natively to PeldrunEngine.")

    # 4. Test Fail-Fast on Unknown Engine via run_instrumented
    async def test_fail_fast_dispatch():
        from omweb.job_manager import job_manager

        test_job_id = "test-failfast-job-001"
        job_manager.create_job(test_job_id)

        print("\nTesting Fail-Fast Dispatch on Unknown Engine...")
        await run_instrumented(
            test_job_id,
            "test prompt",
            engine="non_existent_engine_404"
        )

        job = job_manager.get_job(test_job_id)
        if not job:
            print("[FAIL] Job not found in job_manager.")
            return False

        # Support both Pydantic Job model and dict
        if isinstance(job, dict):
            status_val = str(job.get("status", "")).lower()
            error_msg = str(job.get("error", "") or "")
        else:
            raw_status = getattr(job, "status", "")
            status_val = (raw_status.value if hasattr(raw_status, "value") else str(raw_status)).lower()
            error_msg = str(getattr(job, "error", "") or "")

        if status_val != "failed":
            print(f"[FAIL] Expected job status to be 'failed', got: '{status_val}'")
            return False

        if "is not registered or unavailable" not in error_msg:
            print(f"[FAIL] Unexpected error message recorded: '{error_msg}'")
            return False

        print(f"  [OK] Job failed authoritatively with: {error_msg}")
        return True

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        success = loop.run_until_complete(test_fail_fast_dispatch())
        if not success:
            return 1
    finally:
        loop.close()

    print("\n" + "=" * 70)
    print("PR 3 VERIFICATION RESULT: ALL CHECKS PASSED (100% READY)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())