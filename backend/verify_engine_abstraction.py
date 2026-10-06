"""
Automated Verification Suite for PR 2 (Engine Abstraction Layer).

Validates:
1. Export integrity of omweb.engines module.
2. Protocol compliance for both PeldrunEngine and OpenManusEngine.
3. Strict resolution via EngineRegistry (including aliases).
4. Prevention of silent fallback: Unknown engines raise EngineNotFoundError.
5. Health diagnostic reporting for registered engines.
"""

import asyncio
import sys
from pathlib import Path


def main() -> int:
    backend_dir = Path(__file__).resolve().parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    print("=" * 70)
    print("PELDRUN ENGINE ABSTRACTION LAYER VERIFICATION (PR 2)")
    print(f"Working Directory: {backend_dir}")
    print("=" * 70)

    # 1. Verify Imports
    try:
        from omweb.engines import (
            EngineNotFoundError,
            EngineRegistry,
            EngineRunContext,
            ExecutionEngine,
            OpenManusEngine,
            PeldrunEngine,
            engine_registry,
        )
    except ImportError as e:
        print(f"[FAIL] Failed to import omweb.engines: {e}")
        return 1
    print("[OK] omweb.engines package loaded successfully.")

    # 2. Verify Protocol Conformance
    peldrun_engine = engine_registry.get("peldrun")
    openmanus_engine = engine_registry.get("openmanus")

    if not isinstance(peldrun_engine, ExecutionEngine):
        print("[FAIL] PeldrunEngine does not conform to ExecutionEngine Protocol.")
        return 1
    if not isinstance(openmanus_engine, ExecutionEngine):
        print("[FAIL] OpenManusEngine does not conform to ExecutionEngine Protocol.")
        return 1
    print("[OK] Protocol conformance confirmed for all engines.")

    # 3. Verify Alias Resolution
    try:
        core_alias = engine_registry.get("peldrun-core")
        legacy_alias = engine_registry.get("legacy")
        assert core_alias is peldrun_engine
        assert legacy_alias is openmanus_engine
    except Exception as e:
        print(f"[FAIL] Alias resolution error: {e}")
        return 1
    print("[OK] Engine aliases ('peldrun-core', 'legacy') resolved correctly.")

    # 4. Verify Strict Lookup (No Silent Fallback)
    try:
        engine_registry.get("unregistered_engine_xyz")
        print("[FAIL] Registry did not raise an error for unknown engine ID!")
        return 1
    except EngineNotFoundError:
        print("[OK] Strict fail-fast verified: Unknown engine raises EngineNotFoundError.")
    except Exception as e:
        print(f"[FAIL] Unexpected exception type raised: {type(e)}: {e}")
        return 1

    # 5. Verify Engine Health Check Diagnostics
    async def run_health_checks():
        print("\nChecking Engine Health Diagnostics:")
        health_report = await engine_registry.health_all()
        for eng_id, health in health_report.items():
            print(f"  [*] Engine '{eng_id}': {health}")
        
        # Verify PeldrunEngine health details
        p_health = health_report.get("peldrun", {})
        if not p_health.get("available") or not p_health.get("embedded"):
            print("[FAIL] PeldrunEngine health indicates unavailable or non-embedded state.")
            return False
        return True

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        passed = loop.run_until_complete(run_health_checks())
        if not passed:
            return 1
    finally:
        loop.close()

    print("\n" + "=" * 70)
    print("PR 2 VERIFICATION RESULT: ALL CHECKS PASSED (100% READY)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())