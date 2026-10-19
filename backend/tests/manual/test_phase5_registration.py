# backend/tests/manual/test_phase5_registration.py
"""Verify get_operation / list_operations are fully registered."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"[ OK ] {label}")
        else:
            failures.append(label)
            print(f"[FAIL] {label}" + (f" — {detail}" if detail else ""))

    # --- 1. tools/__init__.py factory registry ---------------------
    from integrations.source_control import tools as tools_pkg

    expected_actions = {
        "list_providers",
        "list_repositories",
        "get_repository",
        "list_branches",
        "read_file",
        "create_workspace",
        "create_branch",
        "get_diff",
        "run_checks",
        "request_approval",
        "push_branch",
        "create_change_request",
        "get_operation",
        "list_operations",
    }
    builtin_names = {
        n.replace("source_control.", "")
        for n in tools_pkg.BUILTIN_TOOL_NAMES
    }
    check(
        "tools/__init__: BUILTIN_TOOL_NAMES has 14 tools",
        builtin_names == expected_actions,
        f"missing={expected_actions - builtin_names}, extra={builtin_names - expected_actions}",
    )
    check(
        "tools/__init__: _TOOL_CLASS_IMPORTS includes new classes",
        {"GetOperationTool", "ListOperationsTool"}.issubset(
            set(tools_pkg._TOOL_CLASS_IMPORTS.keys())
        ),
    )
    check(
        "tools/__init__: __all__ includes new classes",
        {"GetOperationTool", "ListOperationsTool"}.issubset(
            set(tools_pkg.__all__)
        ),
    )

    # --- 2. source_control_tool.py dispatcher registry -------------
    from peldrun.tools.builtins.source_control_tool import (
        SourceControlTool,
        _ACTION_ALIASES,
        _ACTION_MAP,
    )

    check(
        "_ACTION_MAP has 14 actions",
        len(_ACTION_MAP) == 14,
        f"got {len(_ACTION_MAP)}",
    )
    check(
        "_ACTION_MAP contains get_operation",
        "get_operation" in _ACTION_MAP,
    )
    check(
        "_ACTION_MAP contains list_operations",
        "list_operations" in _ACTION_MAP,
    )
    for alias, target in _ACTION_ALIASES.items():
        if target not in _ACTION_MAP:
            check(f"alias {alias!r}", False, f"-> unknown {target!r}")
            break
    else:
        check("all aliases resolve", True)

    # --- 3. Instantiate both new sub-tools via dispatcher ----------
    tool = SourceControlTool()
    for action in ("get_operation", "list_operations"):
        try:
            sub = tool._get_subtool(action)
            check(
                f"dispatcher: _get_subtool({action!r})",
                sub is not None,
                f"got {sub!r}",
            )
        except Exception as exc:
            check(
                f"dispatcher: _get_subtool({action!r})",
                False,
                f"{type(exc).__name__}: {exc}",
            )

    # --- 4. source_control.json ------------------------------------
    p = Path("backend/omweb/tools/builtins/source_control.json")
    data = json.loads(p.read_text(encoding="utf-8"))
    enum = data["parameters"]["properties"]["action"]["enum"]
    check(
        "source_control.json: action.enum has 14 entries",
        len(enum) == 14,
        f"got {len(enum)}",
    )
    check(
        "source_control.json: get_operation in enum",
        "get_operation" in enum,
    )
    check(
        "source_control.json: list_operations in enum",
        "list_operations" in enum,
    )
    props = data["parameters"]["properties"]
    for param in ("include_events", "limit", "status", "kind"):
        check(
            f"source_control.json: {param} parameter exists",
            param in props,
        )
    check(
        "source_control.json: description mentions get_operation",
        "get_operation" in data["description"],
    )
    check(
        "source_control.json: description mentions list_operations",
        "list_operations" in data["description"],
    )

    # --- Summary --------------------------------------------------
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        return 1
    print("PHASE 5 (REGISTRATION) ACCEPTANCE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
