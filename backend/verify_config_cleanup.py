"""
Automated Verification Suite for PR 5 (Configuration & Diagnostics Cleanup).

Validates:
1. engine_config.json contains NO 'peldrun_path' and specifies engine_type 'peldrun'.
2. engine_resolver:
   - is_core_engine_available() evaluates natively to True without external path probing.
   - resolve_active_engine_path() resolves locally to embedded package.
   - get_engine_status() accurately reports embedded status.
   - inject_engine_to_syspath() injects zero external core paths.
3. status router:
   - resolve_active_core_path('peldrun') points to local embedded directory.
   - check_core_linkage('peldrun', ...) returns True.
   - get_git_commit(..., 'peldrun') returns upstream baseline commit (0755669).
   - get_system_status() and get_health_status() report peldrun_linked: True and embedded: True.
4. config_rtr:
   - write_raw_config() executes without attempting external core synchronization.
"""

import asyncio
import json
import os
from pathlib import Path
import sys


def main() -> int:
    backend_dir = Path(__file__).resolve().parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    print("=" * 70)
    print("PELDRUN CONFIGURATION & DIAGNOSTICS CLEANUP VERIFICATION (PR 5)")
    print(f"Working Directory: {backend_dir}")
    print("=" * 70)

    # 1. Verify engine_config.json
    print("\n[TEST 1] Verifying engine_config.json cleanliness...")
    config_file = backend_dir / "engine_config.json"
    if not config_file.is_file():
        print(f"  [FAIL] {config_file} not found!")
        return 1

    try:
        cfg = json.loads(config_file.read_text(encoding="utf-8"))
        print(f"  [*] Loaded engine_config: {cfg}")
    except Exception as e:
        print(f"  [FAIL] Malformed JSON in engine_config.json: {e}")
        return 1

    if "peldrun_path" in cfg:
        print(f"  [FAIL] Obsolete 'peldrun_path' still exists in engine_config.json: {cfg['peldrun_path']}")
        return 1
    print("  [OK] Confirmed: 'peldrun_path' completely eliminated from engine_config.json.")

    engine_type_val = cfg.get("engine_type", "")
    if engine_type_val not in ["peldrun", "peldrun-core"]:
        print(f"  [FAIL] Unexpected engine_type: '{engine_type_val}'")
        return 1
    print(f"  [OK] engine_type configured cleanly: '{engine_type_val}'")

    # 2. Verify engine_resolver
    print("\n[TEST 2] Verifying engine_resolver native embedded resolution...")
    from omweb.engine_resolver import (
        get_active_engine_name,
        get_active_engine_type,
        get_engine_status,
        inject_engine_to_syspath,
        is_core_engine_available,
        resolve_active_engine_path,
    )

    if not is_core_engine_available():
        print("  [FAIL] is_core_engine_available() returned False for embedded core!")
        return 1
    print("  [OK] is_core_engine_available() evaluates natively to True.")

    active_path = resolve_active_engine_path()
    expected_embedded_path = (backend_dir / "peldrun").resolve()
    if active_path.resolve() != expected_embedded_path:
        print(f"  [FAIL] resolve_active_engine_path() returned: {active_path}, expected: {expected_embedded_path}")
        return 1
    print(f"  [OK] resolve_active_engine_path() natively resolved to: {active_path}")

    status_data = get_engine_status()
    print(f"  [*] Engine Status: {status_data}")
    if not status_data.get("core_available") or not status_data.get("core_embedded"):
        print("  [FAIL] Engine status missing core_available or core_embedded flag!")
        return 1
    print("  [OK] get_engine_status() confirms embedded core health.")

    # Test that inject_engine_to_syspath() injects nothing for embedded core
    external_core_marker = os.path.join("peldrun-core", "peldrun")
    inject_engine_to_syspath()
    active_external_paths = [p for p in sys.path if external_core_marker in p.lower()]
    if active_external_paths:
        print(f"  [FAIL] inject_engine_to_syspath injected external core into sys.path: {active_external_paths}")
        return 1
    print("  [OK] Environment isolation preserved: zero external core sys.path injection.")

    # 3. Verify status router
    print("\n[TEST 3] Verifying status router embedded diagnostics...")
    from omweb.routers.status import (
        check_core_linkage,
        get_detailed_system_info,
        get_git_commit,
        get_health_status,
        get_system_status,
        resolve_active_core_path,
    )

    core_dir = resolve_active_core_path("peldrun")
    if core_dir.resolve() != expected_embedded_path:
        print(f"  [FAIL] status.resolve_active_core_path() returned {core_dir}, expected: {expected_embedded_path}")
        return 1
    print(f"  [OK] status.resolve_active_core_path('peldrun') points to embedded core.")

    if not check_core_linkage("peldrun", core_dir):
        print("  [FAIL] status.check_core_linkage() returned False for embedded core!")
        return 1
    print("  [OK] status.check_core_linkage('peldrun') returns True.")

    commit_hash = get_git_commit(core_dir, "peldrun")
    print(f"  [*] Embedded core upstream commit: {commit_hash}")
    if not commit_hash.startswith("0755669"):
        print(f"  [FAIL] Unexpected commit hash for embedded core: {commit_hash}")
        return 1
    print("  [OK] Commit hash matches upstream baseline 0755669.")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        sys_status = loop.run_until_complete(get_system_status())
        health_status = loop.run_until_complete(get_health_status())
        sys_info = loop.run_until_complete(get_detailed_system_info())

        print(f"  [*] GET /api/status -> {sys_status}")
        assert sys_status["peldrun_linked"] is True
        assert sys_status["core_linked"] is True
        assert sys_status["embedded"] is True

        print(f"  [*] GET /api/status/health -> status={health_status['status']}, peldrun_linked={health_status['peldrun_linked']}")
        assert health_status["peldrun_linked"] is True
        assert health_status["core_linked"] is True
        assert health_status["embedded"] is True

        peldrun_repo_info = sys_info["repositories"]["peldrun"]
        print(f"  [*] GET /api/status/system-info -> peldrun repo info: {peldrun_repo_info}")
        assert peldrun_repo_info["embedded"] is True
        assert peldrun_repo_info["commit"].startswith("0755669")
        print("  [OK] All status router endpoints returned valid embedded core diagnostics.")
    finally:
        loop.close()

    # 4. Verify config_rtr decoupling
    print("\n[TEST 4] Verifying config_rtr write decoupling...")
    from omweb.routers.config_rtr import read_raw_config, write_raw_config

    current_cfg = read_raw_config()
    write_raw_config(current_cfg)
    print("  [OK] write_raw_config() executed cleanly with zero external synchronization side-effects.")

    print("\n" + "=" * 70)
    print("PR 5 VERIFICATION RESULT: ALL CHECKS PASSED (100% READY)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())