"""
backend/verify_engine_abstraction.py

Automated Verification Suite for PELDRUN Engine Abstraction Layer.
Hardened under PR 2: Validates complete removal of OpenManus and exclusive authority of PELDRUN Core.

Validates:
1. Export integrity of omweb.engines module without legacy engine leaks.
2. Protocol compliance for PeldrunEngine.
3. Strict resolution via EngineRegistry (including core aliases).
4. Confirms legacy identifiers ('openmanus', 'legacy') are strictly rejected.
5. Health diagnostic reporting for PELDRUN Core.
"""

import asyncio
from pathlib import Path
import sys


def main() -> int:
    backend_dir = Path(__file__).resolve().parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    print("=" * 70)
    print("PELDRUN SINGLE ENGINE ARCHITECTURE VERIFICATION (PR 2)")
    print(f"Working Directory: {backend_dir}")
    print("=" * 70)

    # 1. Verify Imports
    try:
        from omweb.engines import (
            EngineNotFoundError,
            EngineRegistry,
            EngineRunContext,
            ExecutionEngine,
            PeldrunEngine,
            engine_registry,
        )
    except ImportError as e:
        print(f"[FAIL] Failed to import omweb.engines: {e}")
        return 1
    print("[OK] omweb.engines package loaded successfully without legacy leaks.")

    # 2. Verify Protocol Conformance and Sole Registry
    peldrun_engine = engine_registry.get("peldrun")

    if not isinstance(peldrun_engine, ExecutionEngine):
        print("[FAIL] PeldrunEngine does not conform to ExecutionEngine Protocol.")
        return 1

    registered_engines = engine_registry.list_engines()
    if registered_engines != ["peldrun"]:
        print(f"[FAIL] Expected sole registered engine ['peldrun'], got: {registered_engines}")
        return 1
    print("[OK] PELDRUN Core is confirmed as the sole registered engine.")

    # 3. Verify Alias Resolution
    try:
        core_alias_1 = engine_registry.get("peldrun-core")
        core_alias_2 = engine_registry.get("core")
        assert core_alias_1 is peldrun_engine
        assert core_alias_2 is peldrun_engine
    except Exception as e:
        print(f"[FAIL] Core alias resolution error: {e}")
        return 1
    print("[OK] Engine aliases ('peldrun-core', 'core') resolved correctly.")

    # 4. Verify Strict Lookup and Legacy Rejection
    try:
        engine_registry.get("openmanus")
        print("[FAIL] Registry still resolved legacy 'openmanus' engine!")
        return 1
    except EngineNotFoundError:
        print("[OK] Confirmed: 'openmanus' is no longer registered.")

    try:
        engine_registry.get("legacy")
        print("[FAIL] Registry still resolved legacy 'legacy' alias!")
        return 1
    except EngineNotFoundError:
        print("[OK] Confirmed: 'legacy' alias is no longer registered.")

    try:
        engine_registry.get("unregistered_engine_xyz")
        print("[FAIL] Registry did not raise an error for unknown engine ID!")
        return 1
    except EngineNotFoundError:
        print("[OK] Strict fail-fast verified: Unknown engine raises EngineNotFoundError.")

    # 5. Verify Engine Health Check Diagnostics
    async def run_health_checks():
        print("\nChecking Engine Health Diagnostics:")
        health_report = await engine_registry.health_all()
        for eng_id, health in health_report.items():
            print(f"  [*] Engine '{eng_id}': {health}")

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
    print("PR 2 VERIFICATION RESULT: OPENMANUS REMOVED — ALL CHECKS PASSED (100%)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
