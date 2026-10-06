import os
import sys
from pathlib import Path

def main() -> int:
    backend_dir = Path(__file__).resolve().parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    print("=" * 70)
    print("PELDRUN CORE EMBEDDED INTEGRATION VERIFICATION")
    print(f"Working Directory: {backend_dir}")
    print("=" * 70)

    external_core_marker = os.path.join("peldrun-core", "peldrun")
    active_paths = [p for p in sys.path if external_core_marker in p.lower()]
    if active_paths:
        print(f"[FAIL] Detected external peldrun-core in sys.path: {active_paths}")
        return 1
    print("[OK] Environment isolation verified (no external core path in sys.path).")

    try:
        import peldrun
    except ImportError as e:
        print(f"[FAIL] Unable to import peldrun package: {e}")
        return 1

    package_location = Path(peldrun.__file__).resolve().parent
    expected_location = (backend_dir / "peldrun").resolve()
    if package_location != expected_location:
        print(f"[FAIL] Package resolved to wrong location: {package_location}")
        print(f"       Expected: {expected_location}")
        return 1
    print(f"[OK] Package resolved natively at: {package_location}")

    if not getattr(peldrun, "__is_embedded__", False):
        print("[FAIL] peldrun.__is_embedded__ is not set to True.")
        return 1
    print(f"[OK] Metadata verified: Version={getattr(peldrun, '__version__', 'unknown')}, Commit={getattr(peldrun, '__upstream_commit__', 'unknown')}")

    subsystems = [
        ("peldrun.runtime.contract", ["WorkspaceContext", "AgentSpec", "RunRequest"]),
        ("peldrun.tools.contract", ["ToolRuntime", "ToolResult"]),
        ("peldrun.tools.registry", ["ToolRegistry"]),
        ("peldrun.engine.runner", ["AgentRunner"]),
        ("peldrun.engine.state", ["ExecutionState"]),
        ("peldrun.events.bus", ["EventBus"]),
        ("peldrun.security.policy", ["SecurityPolicy"]),
        ("peldrun.sandbox.local_process", ["LocalProcessSandbox"]),
        ("peldrun.agents.react_agent", ["ReActAgent"]),
    ]

    for mod_name, syms in subsystems:
        mod = __import__(mod_name, fromlist=syms)
        for s in syms:
            if not hasattr(mod, s):
                print(f"[FAIL] Missing {s} in {mod_name}")
                return 1
        print(f"  [OK] Verified {mod_name}")

    print("\n" + "=" * 70)
    print("PR 1 VERIFICATION RESULT: ALL CHECKS PASSED (100% READY)")
    print("=" * 70)
    return 0

if __name__ == "__main__":
    sys.exit(main())